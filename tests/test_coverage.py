"""Offline unit tests for the per-asset coverage sweep."""

from __future__ import annotations

import asyncio
import csv
import io
import json

import pytest

from openfilings.coverage import (
    CSV_HEADER,
    CoverageReport,
    CoverageRow,
    parse_ticker_list,
    render_summary,
    sweep,
    to_csv,
    to_json,
)
from openfilings.exceptions import SourceError
from openfilings.models import Company

# --- fakes -----------------------------------------------------------------


class _Filing:
    def __init__(self, filing_id: str) -> None:
        self.id = filing_id


class _Financials:
    def __init__(self, statement_count: int) -> None:
        self.statements = tuple(object() for _ in range(statement_count))


def _company(company_id: str, name: str, market: str = "UK") -> Company:
    return Company(
        id=company_id,
        source_id=company_id,
        name=name,
        market=market,  # type: ignore[arg-type]
        source_url="https://example.test/entity",
    )


class _FakeService:
    """Stands in for ``OpenFilingsService``'s coverage surface, per query."""

    def __init__(
        self,
        *,
        companies: dict[str, list[Company]] | None = None,
        filings: dict[str, list[_Filing]] | None = None,
        financials: dict[str, _Financials] | None = None,
        resolve_errors: dict[str, Exception] | None = None,
        filings_errors: dict[str, Exception] | None = None,
        extraction_errors: dict[str, Exception] | None = None,
        delay: float = 0.0,
    ) -> None:
        self.companies = companies or {}
        self.filings = filings or {}
        self.financials = financials or {}
        self.resolve_errors = resolve_errors or {}
        self.filings_errors = filings_errors or {}
        self.extraction_errors = extraction_errors or {}
        self.delay = delay
        self.in_flight = 0
        self.max_in_flight = 0
        self.search_calls: list[str] = []
        self.list_calls: list[tuple[str, int]] = []
        self.financial_calls: list[str] = []

    async def search_companies(self, query: str) -> list[Company]:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            self.search_calls.append(query)
            if self.delay:
                await asyncio.sleep(self.delay)
            if query in self.resolve_errors:
                raise self.resolve_errors[query]
            return list(self.companies.get(query, []))
        finally:
            self.in_flight -= 1

    async def list_filings(self, company_id: str, *, limit: int = 25) -> list[_Filing]:
        self.list_calls.append((company_id, limit))
        if company_id in self.filings_errors:
            raise self.filings_errors[company_id]
        return list(self.filings.get(company_id, []))[:limit]

    async def get_filing_financials(
        self, filing_id: str, *, refresh: bool = False
    ) -> _Financials:
        self.financial_calls.append(filing_id)
        if filing_id in self.extraction_errors:
            raise self.extraction_errors[filing_id]
        return self.financials.get(filing_id, _Financials(0))


def _row(
    query: str,
    market: str | None,
    *,
    company_id: str | None = None,
    company_name: str | None = None,
    resolution: str = "ok",
    filings_count: int = 0,
    latest_filing_id: str | None = None,
    statements: str = "not_reached",
    reason: str = "ok",
    error: str | None = None,
) -> CoverageRow:
    return CoverageRow(
        query=query,
        market=market,
        company_id=company_id,
        company_name=company_name,
        resolution=resolution,
        filings_count=filings_count,
        latest_filing_id=latest_filing_id,
        statements=statements,
        reason=reason,
        error=error,
    )


def _mixed_report() -> CoverageReport:
    """Seven rows covering every stage outcome of the funnel."""

    return CoverageReport(
        rows=(
            _row(
                "AAA",
                "uk",
                company_id="uk_lei_1",
                filings_count=2,
                latest_filing_id="f1",
                statements="ok",
            ),
            _row("BBB", "uk", company_id="uk_lei_2", reason="no_filings"),
            _row("CCC", "uk", resolution="no_match", reason="resolve_failed:no_match"),
            _row(
                "DDD",
                "nl",
                company_id="nl_lei_1",
                filings_count=1,
                latest_filing_id="f2",
                statements="extract_failed",
                reason="extraction_error:SourceError",
                error="SourceError: no tables, retried",
            ),
            _row(
                "EEE",
                "nl",
                company_id="nl_lei_2",
                filings_count=1,
                latest_filing_id="f3",
                statements="no_statements",
                reason="no_statements_in_filing",
            ),
            _row(
                "FFF",
                None,
                company_id="uk_lei_3",
                filings_count=1,
                latest_filing_id="f4",
                statements="not_attempted",
            ),
            _row(
                "GGG",
                "us",
                resolution="error",
                reason="resolve_failed:SourceError",
                error="SourceError: nope",
            ),
        )
    )


# --- parsing ---------------------------------------------------------------


def test_parse_ticker_list_handles_comments_blanks_and_whitespace() -> None:
    text = (
        "# watchlist for the sweep\n"
        "\n"
        "  aapl , us , Apple Inc.  \n"
        "msft\n"
        "SAP,de\n"
        "tesla,us,Tesla Inc,extra,columns\n"
        "   # indented comment\n"
        "xyz,uk # inline comment\n"
        "abc,,Some Co\n"
    )
    assert parse_ticker_list(text) == [
        ("aapl", "us", "Apple Inc."),
        ("msft", None, None),
        ("SAP", "de", None),
        ("tesla", "us", "Tesla Inc"),
        ("xyz", "uk", None),
        ("abc", None, "Some Co"),
    ]


def test_parse_ticker_list_skips_tickerless_lines() -> None:
    assert parse_ticker_list("") == []
    assert parse_ticker_list(",,uk\n   \n# only comments\n") == []


# --- funnel ----------------------------------------------------------------


def test_funnel_counts_each_stage_per_market() -> None:
    assert _mixed_report().funnel() == {
        "-": {
            "listed": 1,
            "resolved": 1,
            "with_filings": 1,
            "with_statements": 0,
            "ok": 1,
        },
        "nl": {
            "listed": 2,
            "resolved": 2,
            "with_filings": 2,
            "with_statements": 0,
            "ok": 0,
        },
        "uk": {
            "listed": 3,
            "resolved": 2,
            "with_filings": 1,
            "with_statements": 1,
            "ok": 1,
        },
        "us": {
            "listed": 1,
            "resolved": 0,
            "with_filings": 0,
            "with_statements": 0,
            "ok": 0,
        },
    }


def test_funnel_of_empty_report_is_empty() -> None:
    assert CoverageReport(rows=()).funnel() == {}


# --- sweep: reason tokens per failure class --------------------------------


@pytest.mark.asyncio
async def test_reason_tokens_for_resolution_failures() -> None:
    service = _FakeService(
        companies={"MIXED": [_company("uk_lei_ok", "Mixed PLC")]},
        filings={"uk_lei_ok": [_Filing("f1")]},
        financials={"f1": _Financials(1)},
        resolve_errors={"BOOM": SourceError("resolve blew up")},
    )
    report = await sweep(
        [
            ("BOOM", "uk", "Boom Ltd"),
            ("NONE", "uk", None),
            ("MIXED", "uk", None),
        ],
        service=service,
    )
    assert [row.query for row in report.rows] == ["BOOM", "NONE", "MIXED"]
    boom, none, mixed = report.rows
    assert (boom.resolution, boom.statements, boom.reason) == (
        "error",
        "not_reached",
        "resolve_failed:SourceError",
    )
    assert boom.error == "SourceError: resolve blew up"
    assert boom.company_name == "Boom Ltd"  # falls back to the list's NAME
    assert (none.resolution, none.company_id, none.statements, none.reason) == (
        "no_match",
        None,
        "not_reached",
        "resolve_failed:no_match",
    )
    assert (
        mixed.resolution,
        mixed.company_id,
        mixed.filings_count,
        mixed.latest_filing_id,
        mixed.statements,
        mixed.reason,
        mixed.error,
    ) == ("ok", "uk_lei_ok", 1, "f1", "ok", "ok", None)


@pytest.mark.asyncio
async def test_reason_tokens_for_filings_and_statement_failures() -> None:
    service = _FakeService(
        companies={
            "EMPTY": [_company("uk_lei_1", "Empty PLC")],
            "LISTERR": [_company("uk_lei_2", "List Error PLC")],
            "EXTFAIL": [_company("uk_lei_3", "Extract Fail PLC")],
            "NOSTMT": [_company("uk_lei_4", "No Statements PLC")],
        },
        filings={
            "uk_lei_2": [_Filing("f2")],
            "uk_lei_3": [_Filing("f3")],
            "uk_lei_4": [_Filing("f4")],
        },
        financials={"f3": _Financials(2), "f4": _Financials(0)},
        filings_errors={"uk_lei_2": SourceError("registry down")},
        extraction_errors={"f3": SourceError("no tables")},
    )
    report = await sweep(
        [
            ("EMPTY", "uk", None),
            ("LISTERR", "uk", None),
            ("EXTFAIL", "uk", None),
            ("NOSTMT", "uk", None),
        ],
        service=service,
    )
    empty, listerr, extfail, nostmt = report.rows
    assert (
        empty.resolution,
        empty.company_id,
        empty.filings_count,
        empty.statements,
        empty.reason,
    ) == ("ok", "uk_lei_1", 0, "not_reached", "no_filings")
    assert (
        listerr.resolution,
        listerr.company_id,
        listerr.filings_count,
        listerr.statements,
        listerr.reason,
        listerr.error,
    ) == (
        "ok",
        "uk_lei_2",
        0,
        "not_reached",
        "filings_error:SourceError",
        "SourceError: registry down",
    )
    assert (
        extfail.filings_count,
        extfail.latest_filing_id,
        extfail.statements,
        extfail.reason,
        extfail.error,
    ) == (
        1,
        "f3",
        "extract_failed",
        "extraction_error:SourceError",
        "SourceError: no tables",
    )
    assert (nostmt.statements, nostmt.reason) == (
        "no_statements",
        "no_statements_in_filing",
    )


@pytest.mark.asyncio
async def test_with_statements_false_stops_after_filings() -> None:
    service = _FakeService(
        companies={"ONE": [_company("uk_lei_1", "One PLC")]},
        filings={"uk_lei_1": [_Filing("f1")]},
    )
    report = await sweep([("ONE", "uk", None)], service=service, with_statements=False)
    row = report.rows[0]
    assert (
        row.statements,
        row.reason,
        row.filings_count,
        row.latest_filing_id,
    ) == ("not_attempted", "ok", 1, "f1")
    assert service.financial_calls == []


# --- sweep: resolution plumbing --------------------------------------------


@pytest.mark.asyncio
async def test_default_resolver_keeps_only_companies_in_the_entry_market() -> None:
    service = _FakeService(
        companies={
            "BOTH": [
                _company("nl_lei_1", "Both PLC", "NL"),
                _company("uk_lei_1", "Both PLC", "UK"),
            ]
        },
        filings={"uk_lei_1": [_Filing("f1")]},
        financials={"f1": _Financials(1)},
    )
    report = await sweep([("BOTH", "uk", None)], service=service)
    assert report.rows[0].company_id == "uk_lei_1"
    assert report.rows[0].statements == "ok"

    # Casefolded both ways: "nl" as parse_ticker_list lowercases it.
    report = await sweep([("BOTH", "nl", None)], service=service)
    assert report.rows[0].company_id == "nl_lei_1"
    assert report.rows[0].reason == "no_filings"

    # No company in the requested market is a no-match, not a wrong pick.
    report = await sweep([("BOTH", "jp", None)], service=service)
    assert (report.rows[0].resolution, report.rows[0].reason) == (
        "no_match",
        "resolve_failed:no_match",
    )

    # No market: take the first search result; no filtering to apply.
    report = await sweep([("BOTH", None, None)], service=service)
    assert report.rows[0].company_id == "nl_lei_1"


@pytest.mark.asyncio
async def test_resolver_accepts_a_company_list_and_prefers_the_market_match() -> None:
    nl = _company("nl_lei_1", "Both PLC", "NL")
    uk = _company("uk_lei_1", "Both PLC", "UK")

    async def resolver(query: str, market: str | None) -> object:
        assert (query, market) == ("BOTH", "uk")
        return [nl, uk]

    service = _FakeService(
        filings={"uk_lei_1": [_Filing("f1")]},
        financials={"f1": _Financials(1)},
    )
    report = await sweep([("BOTH", "uk", None)], service=service, resolver=resolver)
    assert report.rows[0].company_id == "uk_lei_1"
    assert report.rows[0].statements == "ok"
    assert service.search_calls == []  # a custom resolver replaces the default


@pytest.mark.asyncio
async def test_resolver_accepts_a_single_company_or_none() -> None:
    company = _company("uk_lei_9", "Single PLC", "UK")

    async def single(query: str, market: str | None) -> object:
        return company

    async def missing(query: str, market: str | None) -> object:
        return None

    service = _FakeService(
        filings={"uk_lei_9": [_Filing("f9")]},
        financials={"f9": _Financials(1)},
    )
    report = await sweep([("ONE", "uk", None)], service=service, resolver=single)
    assert report.rows[0].company_id == "uk_lei_9"
    assert report.rows[0].reason == "ok"

    report = await sweep([("GONE", "uk", None)], service=service, resolver=missing)
    assert (report.rows[0].resolution, report.rows[0].reason) == (
        "no_match",
        "resolve_failed:no_match",
    )


@pytest.mark.asyncio
async def test_resolver_returning_an_unsupported_value_fails_only_that_row() -> None:
    async def bad_resolver(query: str, market: str | None) -> object:
        return 42

    service = _FakeService()
    report = await sweep(
        [("X", "uk", None), ("X", "uk", None)],
        service=service,
        resolver=bad_resolver,
    )
    for row in report.rows:
        assert (row.resolution, row.statements) == ("error", "not_reached")
        assert row.reason == "resolve_failed:TypeError"
        assert row.error is not None and row.error.startswith("TypeError")


@pytest.mark.asyncio
async def test_filings_limit_is_forwarded_and_latest_filing_is_first() -> None:
    service = _FakeService(
        companies={"ONE": [_company("uk_lei_1", "One PLC")]},
        filings={"uk_lei_1": [_Filing("new"), _Filing("old")]},
        financials={"new": _Financials(1)},
    )
    report = await sweep([("ONE", "uk", None)], service=service)
    assert service.list_calls == [("uk_lei_1", 3)]  # default filings_limit
    assert report.rows[0].filings_count == 2
    assert report.rows[0].latest_filing_id == "new"
    assert service.financial_calls == ["new"]

    report = await sweep([("ONE", "uk", None)], service=service, filings_limit=1)
    assert service.list_calls[-1] == ("uk_lei_1", 1)
    assert report.rows[0].filings_count == 1


@pytest.mark.asyncio
async def test_sweep_bounds_in_flight_entries_by_concurrency() -> None:
    service = _FakeService(delay=0.05)
    entries = []
    for index in range(6):
        query = f"CO{index}"
        company_id = f"uk_lei_{index}"
        service.companies[query] = [_company(company_id, f"Co {index}")]
        service.filings[company_id] = [_Filing(f"f{index}")]
        entries.append((query, "uk", None))

    report = await sweep(entries, service=service, concurrency=2, with_statements=False)
    assert len(report.rows) == 6
    assert [row.query for row in report.rows] == [query for query, _, _ in entries]
    assert all(row.resolution == "ok" for row in report.rows)
    assert len(service.search_calls) == 6
    assert service.max_in_flight <= 2  # never above the cap
    assert service.max_in_flight == 2  # ... and the cap is actually exercised


@pytest.mark.asyncio
async def test_sweep_rejects_zero_concurrency() -> None:
    with pytest.raises(ValueError, match="concurrency"):
        await sweep([], service=_FakeService(), concurrency=0)


# --- outputs ----------------------------------------------------------------


def test_csv_round_trip_shape() -> None:
    report = _mixed_report()
    text = to_csv(report)
    lines = text.splitlines()
    assert lines[0] == (
        "ticker,market,company_id,company_name,resolution,filings_count,"
        "latest_filing_id,statements,reason,error"
    )
    assert lines[0] == ",".join(CSV_HEADER)
    assert text.endswith("\n")
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert len(parsed) == len(report.rows)
    assert parsed[0] == {
        "ticker": "AAA",
        "market": "uk",
        "company_id": "uk_lei_1",
        "company_name": "",
        "resolution": "ok",
        "filings_count": "2",
        "latest_filing_id": "f1",
        "statements": "ok",
        "reason": "ok",
        "error": "",
    }
    assert parsed[3]["error"] == "SourceError: no tables, retried"  # quoting
    assert parsed[5]["market"] == ""  # market-less row -> empty cell
    assert parsed[6]["resolution"] == "error"


def test_json_round_trip_shape() -> None:
    report = _mixed_report()
    payload = json.loads(to_json(report))
    assert set(payload) == {"rows", "funnel"}
    rows = payload["rows"]
    assert len(rows) == len(report.rows)
    assert list(rows[0]) == list(CSV_HEADER)  # keys mirror the CSV header
    assert rows[0] == {
        "ticker": "AAA",
        "market": "uk",
        "company_id": "uk_lei_1",
        "company_name": None,
        "resolution": "ok",
        "filings_count": 2,
        "latest_filing_id": "f1",
        "statements": "ok",
        "reason": "ok",
        "error": None,
    }
    assert rows[5]["market"] is None
    assert rows[6]["reason"] == "resolve_failed:SourceError"
    assert payload["funnel"] == report.funnel()


def test_render_summary_shows_totals_funnel_and_top_reasons() -> None:
    summary = render_summary(_mixed_report())
    lines = summary.splitlines()
    assert lines[0] == (
        "Coverage: 7 queries | resolved 5 | with filings 4 | with statements 1 | ok 2"
    )
    assert lines[1].split() == [
        "market",
        "listed",
        "resolved",
        "with_filings",
        "with_statements",
        "ok",
    ]
    uk_line = next(line for line in lines if line.startswith("uk"))
    assert uk_line.split() == ["uk", "3", "2", "1", "1", "1"]
    assert any(line.startswith("nl") for line in lines)
    reasons = summary.split("Top reasons:\n", 1)[1].splitlines()
    assert len(reasons) == 5  # capped at five
    assert reasons[0].strip() == "2  ok"
    assert reasons[1].strip() == "1  extraction_error:SourceError"


def test_render_summary_caps_reasons_and_handles_empty_report() -> None:
    rows = tuple(
        _row(f"T{index}", "uk", resolution="error", reason=f"reason_{index}")
        for index in range(7)
    )
    summary = render_summary(CoverageReport(rows=rows))
    reasons = summary.split("Top reasons:\n", 1)[1].splitlines()
    assert len(reasons) == 5
    assert reasons[0].strip() == "1  reason_0"

    empty = render_summary(CoverageReport(rows=()))
    assert "Coverage: 0 queries" in empty
    assert "(none)" in empty
