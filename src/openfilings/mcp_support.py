"""Small response-formatting helpers for the token-bounded MCP surface."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from openfilings.domain import DocumentSection
from openfilings.models import (
    Company,
    CompanyFacts,
    Filing,
    FilingFinancials,
    FinancialStatement,
    InsiderDealing,
    MajorHolderNotification,
    StatementType,
)
from openfilings.validation import validate_financials, validation_view

MAX_METADATA_RESULTS = 50
MAX_MARKDOWN_CHARS = 24_000
MAX_OUTLINE_SECTIONS = 200
MAX_SEARCH_RESULTS = 10
MAX_SNIPPET_CHARS = 3_000
MAX_FINANCIAL_PERIODS = 12
MAX_FINANCIAL_LINE_ITEMS = 100
_MIN_TEXT_CHARS = 200
_WORD = re.compile(r"\w+", re.UNICODE)
_STATEMENT_TYPES = {
    "income_statement",
    "balance_sheet",
    "cash_flow_statement",
    "comprehensive_income",
    "changes_in_equity",
}


@dataclass(frozen=True, slots=True)
class TextWindow:
    """One bounded page from a longer text value."""

    text: str
    offset: int
    total_chars: int
    next_offset: int | None

    @property
    def truncated(self) -> bool:
        return self.next_offset is not None

    def metadata(self) -> dict[str, Any]:
        return {
            "offset": self.offset,
            "total_chars": self.total_chars,
            "next_offset": self.next_offset,
            "truncated": self.truncated,
        }


def success(data: dict[str, Any], *, next_steps: Sequence[str] = ()) -> dict[str, Any]:
    response: dict[str, Any] = {"success": True, "data": data}
    if next_steps:
        response["next_steps"] = list(next_steps)
    return response


def failure(
    message: str,
    *,
    error_code: str = "REQUEST_FAILED",
    suggestions: Sequence[str] = (),
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "success": False,
        "error": message,
        "error_code": error_code,
    }
    if suggestions:
        response["suggestions"] = list(suggestions)
    return response


def validate_limit(value: int, *, maximum: int, name: str = "limit") -> int:
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}.")
    return value


def text_window(text: str, *, offset: int, max_chars: int) -> TextWindow:
    if offset < 0:
        raise ValueError("offset cannot be negative.")
    if not _MIN_TEXT_CHARS <= max_chars <= MAX_MARKDOWN_CHARS:
        raise ValueError(
            f"max_chars must be between {_MIN_TEXT_CHARS} and {MAX_MARKDOWN_CHARS}."
        )
    total_chars = len(text)
    start = min(offset, total_chars)
    end = min(start + max_chars, total_chars)
    next_offset = end if end < total_chars else None
    return TextWindow(text[start:end], start, total_chars, next_offset)


def company_summary(company: Company) -> dict[str, Any]:
    return _without_none(
        {
            "id": company.id,
            "name": company.name,
            "market": company.market,
            "country_code": company.country_code,
            "ticker": company.ticker,
            "lei": company.lei,
            "local_code": company.local_code,
            "sources": list(company.sources),
            "status": company.status,
            "source_url": company.source_url,
            # Only present after same-LEI search dedup (issue #12): the
            # jurisdictions that also carry this legal entity's filings.
            "other_jurisdictions": list(company.other_jurisdictions) or None,
        }
    )


def filing_summary(filing: Filing) -> dict[str, Any]:
    return _without_none(
        {
            "id": filing.id,
            "company_id": filing.company_id,
            "source": filing.source,
            "title": filing.title,
            "category": filing.category,
            "filing_type": filing.filing_type,
            "filing_date": filing.filing_date.isoformat(),
            "published_at": (
                filing.published_at.isoformat() if filing.published_at else None
            ),
            "period_end": filing.period_end.isoformat() if filing.period_end else None,
            "media_type": filing.media_type,
            "language": filing.language,
            "has_document": filing.has_document,
            "xbrl_available": filing.xbrl_available,
            "pdf_available": filing.pdf_available,
            "source_url": filing.source_url,
        }
    )


def major_holder_summary(holder: MajorHolderNotification) -> dict[str, Any]:
    return _without_none(
        {
            "filing_id": holder.filing_id,
            "company_id": holder.company_id,
            "issuer_name": holder.issuer_name,
            "holder_name": holder.holder_name,
            "isin": holder.isin,
            "reason": holder.reason,
            "position_date": (
                holder.position_date.isoformat() if holder.position_date else None
            ),
            "date_crossed": (
                holder.date_crossed.isoformat() if holder.date_crossed else None
            ),
            "date_notified": (
                holder.date_notified.isoformat() if holder.date_notified else None
            ),
            "total_percent": (
                str(holder.total_percent) if holder.total_percent is not None else None
            ),
            "total_voting_rights": holder.total_voting_rights,
            "source_url": holder.source_url,
        }
    )


def insider_dealing_summary(dealing: InsiderDealing) -> dict[str, Any]:
    return _without_none(
        {
            "filing_id": dealing.filing_id,
            "company_id": dealing.company_id,
            "issuer_name": dealing.issuer_name,
            "person_name": dealing.person_name,
            "position": dealing.position,
            "initial_or_amendment": dealing.initial_or_amendment,
            "instrument": dealing.instrument,
            "isin": dealing.isin,
            "transaction_natures": list(dealing.transaction_natures),
            "price_volume": [
                _without_none(
                    {
                        "price": str(lot.price) if lot.price is not None else None,
                        "currency": lot.currency,
                        "volume": lot.volume,
                    }
                )
                for lot in dealing.price_volume
            ],
            "transaction_dates": [
                transaction_date.isoformat()
                for transaction_date in dealing.transaction_dates
            ],
            "places": list(dealing.places),
            "source_url": dealing.source_url,
        }
    )


def section_summary(section: DocumentSection) -> dict[str, Any]:
    return {
        "title": section.title,
        "level": section.level,
        "start_line": section.start_line,
        "character_count": len(section.markdown),
    }


def query_excerpt(text: str, query: str, *, max_chars: int) -> str:
    validate_limit(max_chars, maximum=MAX_SNIPPET_CHARS, name="snippet_chars")
    if len(text) <= max_chars:
        return text
    positions = [
        text.casefold().find(term)
        for term in _WORD.findall(query.casefold())
        if term and text.casefold().find(term) >= 0
    ]
    center = min(positions) if positions else 0
    start = max(0, center - max_chars // 3)
    end = min(len(text), start + max_chars)
    start = max(0, end - max_chars)
    excerpt = text[start:end]
    if start:
        excerpt = "…" + excerpt[1:]
    if end < len(text):
        excerpt = excerpt[:-1] + "…"
    return excerpt


def financials_view(
    financials: FilingFinancials,
    *,
    statements: Sequence[StatementType] | None,
    periods: int,
    detail: str,
    max_line_items: int,
) -> dict[str, Any]:
    validate_limit(periods, maximum=MAX_FINANCIAL_PERIODS, name="periods")
    validate_limit(
        max_line_items,
        maximum=MAX_FINANCIAL_LINE_ITEMS,
        name="max_line_items",
    )
    if detail not in {"minimal", "standard", "full"}:
        raise ValueError("detail must be minimal, standard, or full.")
    selected_types = _validated_statement_types(statements)
    selected = [
        statement
        for statement in financials.statements
        if selected_types is None or statement.statement_type in selected_types
    ]
    view: dict[str, Any] = {
        "filing_id": financials.filing_id,
        "company_id": financials.company_id,
        "extraction_method": financials.extraction_method,
        "fact_count": financials.fact_count,
        "statements": [
            _statement_view(
                statement,
                periods=periods,
                detail=detail,
                max_line_items=max_line_items,
            )
            for statement in selected
        ],
    }
    validation = validation_view(validate_financials(financials))
    if validation is not None:
        view["validation"] = validation
    return view


def company_facts_view(
    facts: CompanyFacts,
    *,
    statements: Sequence[StatementType] | None,
    periods: int,
    detail: str,
    max_line_items: int,
) -> dict[str, Any]:
    validate_limit(periods, maximum=MAX_FINANCIAL_PERIODS, name="periods")
    validate_limit(
        max_line_items,
        maximum=MAX_FINANCIAL_LINE_ITEMS,
        name="max_line_items",
    )
    if detail not in {"minimal", "standard", "full"}:
        raise ValueError("detail must be minimal, standard, or full.")
    selected_types = _validated_statement_types(statements)
    selected = [
        statement
        for statement in facts.statements
        if selected_types is None or statement.statement_type in selected_types
    ]
    return {
        "company_id": facts.company_id,
        "filing_ids": list(facts.filing_ids),
        "statements": [
            _statement_view(
                statement,
                periods=periods,
                detail=detail,
                max_line_items=max_line_items,
            )
            for statement in selected
        ],
    }


def _statement_view(
    statement: FinancialStatement,
    *,
    periods: int,
    detail: str,
    max_line_items: int,
) -> dict[str, Any]:
    selected_periods = sorted(
        statement.periods,
        key=lambda period: period.end_date,
        reverse=True,
    )[:periods]
    labels = [period.label for period in selected_periods]
    result: dict[str, Any] = {
        "statement_type": statement.statement_type,
        "title": statement.title,
        "currency": statement.currency,
        "periods": labels,
        "line_item_count": len(statement.line_items),
    }
    if detail == "minimal":
        return _without_none(result)
    selected_items = statement.line_items[:max_line_items]
    result["line_items"] = [
        _line_item_view(item, labels=labels, detail=detail) for item in selected_items
    ]
    result["truncated"] = len(selected_items) < len(statement.line_items)
    return _without_none(result)


def _line_item_view(item: Any, *, labels: Sequence[str], detail: str) -> dict[str, Any]:
    values = {
        value.period.label: str(value.value)
        for value in item.values
        if value.period.label in labels
    }
    result: dict[str, Any] = {
        "code": item.code,
        "name": item.name,
        "values": values,
    }
    if detail == "full":
        result["concept"] = item.concept
        result["provenance"] = [
            _without_none(
                {
                    "period": value.period.label,
                    "unit": value.unit,
                    "decimals": value.decimals,
                    "dimensions": [list(dimension) for dimension in value.dimensions],
                    "provenance": value.provenance,
                    "confidence": value.confidence,
                    "source_context": value.source_context,
                    "derived_from": list(value.derived_from) or None,
                }
            )
            for value in item.values
            if value.period.label in labels
        ]
    return result


def _validated_statement_types(
    statements: Sequence[StatementType] | None,
) -> set[str] | None:
    if not statements:
        return None
    invalid = set(statements) - _STATEMENT_TYPES
    if invalid:
        raise ValueError(f"Unsupported statement type: {sorted(invalid)[0]}.")
    return set(statements)


def _without_none(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}
