"""Per-asset coverage sweep across the keyless markets.

Given a watchlist of ``TICKER[,MARKET[,NAME]]`` rows, this answers how far
``openfilings`` gets for each one - resolve a company, list its filings,
extract the latest filing's statements - and reports the results as a
funnel, so coverage gaps are visible as data instead of a live debugging
session.

Counterpart to ``openfilings.probe`` (reviewed per-issuer cases) and
``openfilings.smoke`` (one issuer per market): this module sweeps an
arbitrary list with bounded concurrency and never lets one row's failure
hide the rest. The ``resolver`` hook is where ``openfilings.crosswalk``
plugs in, so tickers/ISINs the source registries cannot match by name can
still resolve to a company ID.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from openfilings.service import OpenFilingsService

#: Column order shared by ``to_csv`` and the row objects of ``to_json``.
CSV_HEADER = (
    "ticker",
    "market",
    "company_id",
    "company_name",
    "resolution",
    "filings_count",
    "latest_filing_id",
    "statements",
    "reason",
    "error",
)

_FUNNEL_STAGES = ("listed", "resolved", "with_filings", "with_statements", "ok")


@dataclass(frozen=True, slots=True)
class CoverageRow:
    """One watchlist entry's journey: resolve -> filings -> statements.

    ``company_name`` is the resolved company's name, falling back to the
    list's NAME column when resolution never produced one. ``reason`` is a
    short machine-friendly token such as ``"ok"``, ``"no_filings"``,
    ``"resolve_failed:no_match"`` or ``"extraction_error:SourceError"``.
    """

    query: str
    market: str | None
    company_id: str | None
    company_name: str | None
    resolution: str  # "ok" | "no_match" | "error"
    filings_count: int
    latest_filing_id: str | None
    statements: str
    reason: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """All rows of one sweep, with a per-market funnel view."""

    rows: tuple[CoverageRow, ...]

    def funnel(self) -> dict[str, dict[str, int]]:
        """Per-market (or ``"-"`` when market-less) counts per stage.

        ``listed`` counts every row; ``resolved`` rows that found a
        company; ``with_filings`` rows whose company returned a filing;
        ``with_statements`` rows whose latest filing extracted statements;
        ``ok`` fully successful rows. A sweep that skipped the statements
        step (``with_statements=False``) still counts as ok once the
        filings step succeeded.
        """

        funnel: dict[str, dict[str, int]] = {}
        for row in self.rows:
            counts = funnel.setdefault(
                row.market or "-",
                dict.fromkeys(_FUNNEL_STAGES, 0),
            )
            counts["listed"] += 1
            resolved = row.company_id is not None
            with_filings = resolved and row.filings_count > 0
            with_statements = with_filings and row.statements == "ok"
            counts["resolved"] += resolved
            counts["with_filings"] += with_filings
            counts["with_statements"] += with_statements
            if with_statements or (with_filings and row.statements == "not_attempted"):
                counts["ok"] += 1
        return funnel


def parse_ticker_list(text: str) -> list[tuple[str, str | None, str | None]]:
    """Parse a watchlist of ``TICKER[,MARKET[,NAME]]`` lines.

    Blank lines and ``#`` comments (whole-line or trailing) are skipped;
    fields are stripped and the market is lowercased. A missing or empty
    market/name becomes ``None``, and columns beyond NAME are ignored so a
    richer CSV can be pasted in unwrapped. A line whose ticker field is
    empty (e.g. ``",uk"``) is skipped - there is nothing to sweep.
    """

    entries: list[tuple[str, str | None, str | None]] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = [field.strip() for field in line.split(",")]
        query = fields[0]
        if not query:
            continue
        market = fields[1].lower() if len(fields) > 1 and fields[1] else None
        name = fields[2] if len(fields) > 2 and fields[2] else None
        entries.append((query, market, name))
    return entries


async def sweep(
    entries: Iterable[tuple[str, str | None, str | None]],
    *,
    service: OpenFilingsService,
    resolver: Callable[[str, str | None], Awaitable[Any]] | None = None,
    concurrency: int = 4,
    filings_limit: int = 3,
    with_statements: bool = True,
) -> CoverageReport:
    """Run every entry through resolve -> filings -> statements.

    Bounded by ``concurrency`` exactly like ``openfilings.probe``; one
    entry's exception is captured on its own row and never aborts the
    sweep. Rows come back in input order. ``resolver`` defaults to
    ``service.search_companies`` filtered to the entry's market; pass the
    crosswalk-backed resolver to resolve tickers/ISINs the registries
    cannot match by name.
    """

    if concurrency < 1:
        raise ValueError("concurrency must be at least one")
    semaphore = asyncio.Semaphore(concurrency)
    effective_resolver = resolver or _service_resolver(service)

    async def check(entry: Any) -> CoverageRow:
        # Accept the three-tuples from parse_ticker_list; tolerate a stray
        # shorter entry as a row the sweep can still report on rather than
        # an exception that kills the whole gather.
        if isinstance(entry, (tuple, list)) and entry:
            query = str(entry[0])
            market = entry[1] if len(entry) > 1 else None
            name = entry[2] if len(entry) > 2 else None
        else:
            query, market, name = str(entry), None, None
        async with semaphore:
            try:
                return await _run_entry(
                    service,
                    effective_resolver,
                    query,
                    market,
                    name,
                    filings_limit=filings_limit,
                    with_statements=with_statements,
                )
            except Exception as exc:
                return CoverageRow(
                    query=query,
                    market=market,
                    company_id=None,
                    company_name=name,
                    resolution="error",
                    filings_count=0,
                    latest_filing_id=None,
                    statements="not_reached",
                    reason=f"sweep_error:{type(exc).__name__}",
                    error=f"{type(exc).__name__}: {exc}",
                )

    return CoverageReport(
        tuple(await asyncio.gather(*(check(entry) for entry in entries)))
    )


def render_summary(report: CoverageReport) -> str:
    """Compact per-market funnel table plus the five most common reasons."""

    funnel = report.funnel()
    totals = {
        stage: sum(counts[stage] for counts in funnel.values())
        for stage in _FUNNEL_STAGES
    }
    table: list[list[str]] = [["market", *_FUNNEL_STAGES]]
    for market in sorted(funnel):
        counts = funnel[market]
        table.append([market, *(str(counts[stage]) for stage in _FUNNEL_STAGES)])
    widths = [max(len(line[index]) for line in table) for index in range(len(table[0]))]
    table_lines = [
        "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(line)).rstrip()
        for line in table
    ]

    reason_counts: dict[str, int] = {}
    for row in report.rows:
        reason_counts[row.reason] = reason_counts.get(row.reason, 0) + 1
    top_reasons = sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))[
        :5
    ]
    reason_lines = [f"  {count}  {reason}" for reason, count in top_reasons] or [
        "  (none)"
    ]

    header = (
        f"Coverage: {totals['listed']} queries | resolved {totals['resolved']} | "
        f"with filings {totals['with_filings']} | "
        f"with statements {totals['with_statements']} | ok {totals['ok']}"
    )
    return "\n".join([header, *table_lines, "", "Top reasons:", *reason_lines])


def to_csv(report: CoverageReport) -> str:
    """CSV in the canonical ``CSV_HEADER`` column order; ``None`` is empty."""

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for row in report.rows:
        writer.writerow(
            [
                row.query,
                row.market or "",
                row.company_id or "",
                row.company_name or "",
                row.resolution,
                row.filings_count,
                row.latest_filing_id or "",
                row.statements,
                row.reason,
                row.error or "",
            ]
        )
    return buffer.getvalue()


def to_json(report: CoverageReport) -> str:
    """JSON object ``{"rows": [...], "funnel": {...}}``; keys mirror CSV."""

    payload = {
        "rows": [
            {
                "ticker": row.query,
                "market": row.market,
                "company_id": row.company_id,
                "company_name": row.company_name,
                "resolution": row.resolution,
                "filings_count": row.filings_count,
                "latest_filing_id": row.latest_filing_id,
                "statements": row.statements,
                "reason": row.reason,
                "error": row.error,
            }
            for row in report.rows
        ],
        "funnel": report.funnel(),
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _service_resolver(
    service: OpenFilingsService,
) -> Callable[[str, str | None], Awaitable[Any]]:
    """Default resolver: name search, then the entry market when given."""

    async def resolve(query: str, market: str | None) -> Any:
        companies = [
            company
            for company in await service.search_companies(query)
            if _company_like(company)
        ]
        if market is not None:
            return next(
                (
                    company
                    for company in companies
                    if _market_matches(company.market, market)
                ),
                None,
            )
        return companies[0] if companies else None

    return resolve


async def _run_entry(
    service: OpenFilingsService,
    resolver: Callable[[str, str | None], Awaitable[Any]],
    query: str,
    market: str | None,
    name: str | None,
    *,
    filings_limit: int,
    with_statements: bool,
) -> CoverageRow:
    try:
        company = _pick_company(await resolver(query, market), market)
    except Exception as exc:
        return CoverageRow(
            query=query,
            market=market,
            company_id=None,
            company_name=name,
            resolution="error",
            filings_count=0,
            latest_filing_id=None,
            statements="not_reached",
            reason=f"resolve_failed:{type(exc).__name__}",
            error=f"{type(exc).__name__}: {exc}",
        )
    if company is None:
        return CoverageRow(
            query=query,
            market=market,
            company_id=None,
            company_name=name,
            resolution="no_match",
            filings_count=0,
            latest_filing_id=None,
            statements="not_reached",
            reason="resolve_failed:no_match",
        )

    company_id = str(company.id)
    company_name = company.name or name
    try:
        filings = list(await service.list_filings(company_id, limit=filings_limit))
    except Exception as exc:
        return CoverageRow(
            query=query,
            market=market,
            company_id=company_id,
            company_name=company_name,
            resolution="ok",
            filings_count=0,
            latest_filing_id=None,
            statements="not_reached",
            reason=f"filings_error:{type(exc).__name__}",
            error=f"{type(exc).__name__}: {exc}",
        )
    if not filings:
        return CoverageRow(
            query=query,
            market=market,
            company_id=company_id,
            company_name=company_name,
            resolution="ok",
            filings_count=0,
            latest_filing_id=None,
            statements="not_reached",
            reason="no_filings",
        )

    # list_filings returns newest first, so the head is the latest filing.
    latest_id = str(filings[0].id)
    if not with_statements:
        return CoverageRow(
            query=query,
            market=market,
            company_id=company_id,
            company_name=company_name,
            resolution="ok",
            filings_count=len(filings),
            latest_filing_id=latest_id,
            statements="not_attempted",
            reason="ok",
        )

    try:
        financials = await service.get_filing_financials(latest_id)
    except Exception as exc:
        return CoverageRow(
            query=query,
            market=market,
            company_id=company_id,
            company_name=company_name,
            resolution="ok",
            filings_count=len(filings),
            latest_filing_id=latest_id,
            statements="extract_failed",
            reason=f"extraction_error:{type(exc).__name__}",
            error=f"{type(exc).__name__}: {exc}",
        )
    if not _statement_count(financials):
        return CoverageRow(
            query=query,
            market=market,
            company_id=company_id,
            company_name=company_name,
            resolution="ok",
            filings_count=len(filings),
            latest_filing_id=latest_id,
            statements="no_statements",
            reason="no_statements_in_filing",
        )
    return CoverageRow(
        query=query,
        market=market,
        company_id=company_id,
        company_name=company_name,
        resolution="ok",
        filings_count=len(filings),
        latest_filing_id=latest_id,
        statements="ok",
        reason="ok",
    )


def _pick_company(result: Any, market: str | None) -> Any:
    """Normalize a resolver result to one company-like object or ``None``.

    Accepts a company-like object, a list/tuple of them in preference
    order, or ``None``. With an entry market, the first company whose
    market matches wins; otherwise the first company.
    """

    if result is None:
        return None
    if isinstance(result, (list, tuple)):
        candidates = [item for item in result if _company_like(item)]
    elif _company_like(result):
        return result
    else:
        raise TypeError(
            f"resolver returned {type(result).__name__}; expected a Company, "
            "list[Company], or None"
        )
    if not candidates:
        return None
    if market is not None:
        for company in candidates:
            if _market_matches(company.market, market):
                return company
    return candidates[0]


def _company_like(value: Any) -> bool:
    return all(hasattr(value, attribute) for attribute in ("id", "name", "market"))


def _market_matches(company_market: Any, entry_market: str) -> bool:
    """Casefold compare; ``Company.market`` is "UK"/"NL", entries lowercase."""

    return (
        isinstance(company_market, str)
        and company_market.casefold() == entry_market.casefold()
    )


def _statement_count(financials: Any) -> int:
    statements = getattr(financials, "statements", None) or ()
    return len(statements)
