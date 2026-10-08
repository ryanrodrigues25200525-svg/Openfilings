"""Ergonomic domain objects inspired by EdgarTools' collection-first API."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from math import log

from openfilings.models import Filing, FilingContent

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_BARE_FIGURE = re.compile(r"^[~$]?[\d,.]+\s*(?:m|b|bn|k|cts|x|%)?$", re.IGNORECASE)
# A glossy annual report's front-matter (stat callouts, pull-quotes) can
# render with heading-level styling that a layout-aware PDF-to-markdown
# converter classifies as a genuine '#' heading - confirmed live on a
# Keppel filing, where "$1,100m", "18.7%", and a full page-break-wrapped
# prose sentence ("On 27 February 2023 and 28 February 2023, the Asset Co
# Transaction...") all rendered as top-level headings and buried the real
# "Balance Sheets" section under dozens of decorative fragments.
_MAX_HEADING_WORDS = 18


class Filings(Sequence[Filing]):
    """Immutable filing collection with filtering and latest/head helpers."""

    def __init__(self, filings: Sequence[Filing] = ()) -> None:
        self._filings = tuple(filings)

    def __len__(self) -> int:
        return len(self._filings)

    def __iter__(self) -> Iterator[Filing]:
        return iter(self._filings)

    def __getitem__(self, index: int | slice) -> Filing | Filings:
        value = self._filings[index]
        return Filings(value) if isinstance(index, slice) else value

    @property
    def empty(self) -> bool:
        return not self._filings

    def head(self, count: int = 5) -> Filings:
        if count < 0:
            raise ValueError("count cannot be negative")
        return Filings(self._filings[:count])

    def latest(self, count: int = 1) -> Filing | Filings | None:
        if count < 1:
            raise ValueError("count must be at least one")
        ordered = sorted(
            self._filings,
            key=lambda filing: (
                filing.published_at or filing.filing_date,
                filing.period_end or date.min,
                filing.id,
            ),
            reverse=True,
        )
        selected = Filings(ordered[:count])
        if count == 1:
            return selected[0] if selected else None
        return selected

    def filter(
        self,
        *,
        source: str | None = None,
        filing_type: str | Sequence[str] | None = None,
        category: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        has_document: bool | None = None,
    ) -> Filings:
        types = (
            {filing_type.casefold()}
            if isinstance(filing_type, str)
            else {value.casefold() for value in filing_type or ()}
        )
        return Filings(
            filing
            for filing in self._filings
            if (source is None or filing.source == source)
            and (not types or filing.filing_type.casefold() in types)
            and (category is None or filing.category.casefold() == category.casefold())
            and (date_from is None or filing.filing_date >= date_from)
            and (date_to is None or filing.filing_date <= date_to)
            and (has_document is None or filing.has_document is has_document)
        )


@dataclass(frozen=True, slots=True)
class DocumentSection:
    """One Markdown section from a parsed filing document."""

    title: str
    level: int
    markdown: str
    start_line: int

    @property
    def text(self) -> str:
        return _HEADING.sub(r"\2", self.markdown, count=1).strip()


def _looks_like_heading(title: str) -> bool:
    """A bare stat callout ("$1,100m", "18.7%") carries no navigational
    value as a section title, and a genuine sentence-length passage is
    prose that merely inherited heading styling, not a real heading."""
    stripped = title.strip("*_ ")
    if _BARE_FIGURE.fullmatch(stripped):
        return False
    return len(stripped.split()) <= _MAX_HEADING_WORDS


@dataclass(frozen=True, slots=True)
class SectionSearchResult:
    """One ranked document-section match."""

    section: DocumentSection
    score: float
    matched_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _SearchCorpus:
    terms: tuple[str, ...]
    document_frequency: dict[str, int]
    average_length: float
    document_count: int

    def term_score(self, term: str, frequency: int, document_length: int) -> float:
        k1 = 1.5
        b = 0.75
        frequency_in_documents = self.document_frequency[term]
        inverse_frequency = log(
            1
            + (self.document_count - frequency_in_documents + 0.5)
            / (frequency_in_documents + 0.5)
        )
        length_ratio = document_length / self.average_length
        denominator = frequency + k1 * (1 - b + b * length_ratio)
        return inverse_frequency * frequency * (k1 + 1) / denominator


@dataclass(frozen=True, slots=True)
class FilingDocument:
    """A filing's Markdown plus section navigation and local search."""

    content: FilingContent
    sections: tuple[DocumentSection, ...]

    @classmethod
    def from_content(cls, content: FilingContent) -> FilingDocument:
        lines = content.markdown.splitlines()
        headings: list[tuple[int, int, str]] = []
        for line_number, line in enumerate(lines):
            match = _HEADING.match(line)
            if match and _looks_like_heading(match.group(2)):
                headings.append((line_number, len(match.group(1)), match.group(2)))

        sections: list[DocumentSection] = []
        for position, (start, level, title) in enumerate(headings):
            end = len(lines)
            for candidate_start, candidate_level, _ in headings[position + 1 :]:
                if candidate_level <= level:
                    end = candidate_start
                    break
            sections.append(
                DocumentSection(
                    title=title,
                    level=level,
                    markdown="\n".join(lines[start:end]).strip() + "\n",
                    start_line=start + 1,
                )
            )
        return cls(content=content, sections=tuple(sections))

    @property
    def markdown(self) -> str:
        return self.content.markdown

    def section(self, title: str) -> DocumentSection | None:
        wanted = title.casefold()
        return next(
            (
                section
                for section in self.sections
                if wanted in section.title.casefold()
            ),
            None,
        )

    def search(self, query: str, *, limit: int = 20) -> tuple[DocumentSection, ...]:
        return tuple(
            result.section for result in self.ranked_search(query, limit=limit)
        )

    def ranked_search(
        self, query: str, *, limit: int = 20
    ) -> tuple[SectionSearchResult, ...]:
        terms = tuple(sorted(set(_tokens(query))))
        if not terms or limit < 1 or not self.sections:
            return ()
        document_tokens = [_tokens(section.markdown) for section in self.sections]
        document_frequency = {
            term: sum(term in tokens for tokens in document_tokens) for term in terms
        }
        average_length = sum(map(len, document_tokens)) / len(document_tokens)
        corpus = _SearchCorpus(
            terms,
            document_frequency,
            average_length,
            len(self.sections),
        )
        results = [
            _search_result(section, tokens, corpus)
            for section, tokens in zip(self.sections, document_tokens, strict=True)
        ]
        matches = [result for result in results if result.matched_terms]
        matches.sort(key=lambda result: (-result.score, result.section.start_line))
        return tuple(matches[:limit])


def _search_result(
    section: DocumentSection,
    tokens: list[str],
    corpus: _SearchCorpus,
) -> SectionSearchResult:
    counts = Counter(tokens)
    matched = tuple(term for term in corpus.terms if counts[term])
    score = sum(corpus.term_score(term, counts[term], len(tokens)) for term in matched)
    title_terms = set(_tokens(section.title))
    score += sum(1.5 for term in matched if term in title_terms)
    return SectionSearchResult(section, round(score, 6), matched)


def _tokens(value: str) -> list[str]:
    return [match.group(0).casefold() for match in _WORD.finditer(value)]
