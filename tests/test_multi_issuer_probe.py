"""Offline unit tests for the monthly multi-issuer probe and LEI dedup."""

from __future__ import annotations

import pytest

from openfilings.models import Company, deduplicate_companies_by_lei
from openfilings.probe import (
    PROBE_CASES,
    ProbeCase,
    _looks_offline,
    run_live_probe,
    summarize,
)
from openfilings.service import OpenFilingsService
from openfilings.smoke import SMOKE_CASES
from openfilings.storage.sqlite import SQLiteCache

_EDP_LEI = "529900MUFAH07Q1TAX06"
_TELENOR_LEI = "5967007LIEEXZXHDL433"


def _company(
    company_id: str,
    name: str,
    market: str,
    country: str,
    lei: str | None = None,
) -> Company:
    return Company(
        id=company_id,
        source_id=lei or company_id,
        name=name,
        sources=("esef",),
        lei=lei,
        market=market,  # type: ignore[arg-type]
        country_code=country,  # type: ignore[arg-type]
        source_url="https://example.test/entity",
    )


def test_probe_cases_cover_every_smoke_source_without_reusing_smoke_issuers() -> None:
    smoke_sources = {case.source for case in SMOKE_CASES}
    probe_sources = {case.source for case in PROBE_CASES}
    assert probe_sources == smoke_sources
    smoke_queries = {case.query for case in SMOKE_CASES}
    overlap = [case.query for case in PROBE_CASES if case.query in smoke_queries]
    assert overlap == []
    # Bounded and polite: a handful of issuers per market, never the smoke one.
    assert 2 <= len(PROBE_CASES) <= 60


def test_probe_runner_classifies_verified_unverifiable_and_failed() -> None:
    service = _ProbeService(
        {
            "good": _Outcome(company=True, filings=True, financials="held"),
            "thin": _Outcome(company=True, filings=True, financials="no-balance"),
            "gone": _Outcome(company=False),
        }
    )
    cases = (
        ProbeCase("good label", "good", "esef"),
        ProbeCase("thin label", "thin", "esef"),
        ProbeCase("gone label", "gone", "esef"),
        ProbeCase("search only", "good", "asx", check_financials=False),
    )
    results = _run(service, cases)
    by_label = {item.label: item for item in results}
    assert by_label["good label"].outcome == "verified"
    assert by_label["thin label"].outcome == "unverifiable"
    assert by_label["gone label"].outcome == "failed"
    assert "resolution" in by_label["gone label"].detail
    assert by_label["search only"].outcome == "unverifiable"
    assert by_label["search only"].filing_id is None


def test_probe_runner_never_raises_and_timeout_marks_failed() -> None:
    service = _ProbeService({"slow": _Outcome(company=True, filings="hang")})
    results = _run(
        service, (ProbeCase("slow label", "slow", "esef"),), timeout_seconds=0.01
    )
    assert results[0].outcome == "failed"


def test_probe_runner_validates_concurrency() -> None:
    with pytest.raises(ValueError, match="concurrency"):
        _run(_ProbeService({}), (), concurrency=0)


def test_probe_summary_tallies_outcomes_and_lists_only_non_verified() -> None:
    service = _ProbeService(
        {
            "good": _Outcome(company=True, filings=True, financials="held"),
            "gone": _Outcome(company=False),
        }
    )
    results = _run(
        service,
        (
            ProbeCase("good label", "good", "esef"),
            ProbeCase("gone label", "gone", "esef"),
        ),
    )
    summary = summarize(results)
    assert "2 issuers: 1 verified, 0 unverifiable, 1 failed." in summary
    assert "FAILED\tgone label" in summary
    assert "good label" not in summary.splitlines()[1]


def test_probe_offline_signature_is_all_cases_failed() -> None:
    service = _ProbeService({})
    results = _run(
        service,
        (
            ProbeCase("a", "missing-a", "esef"),
            ProbeCase("b", "missing-b", "esef"),
        ),
    )
    assert _looks_offline(results) is True
    assert _looks_offline(()) is False


def test_bare_query_for_dual_jurisdiction_lei_returns_one_home_record() -> None:
    """Issue #12: the same LEI filed in ES and PT collapses to one record."""

    companies = [
        _company(
            "es_lei_" + _EDP_LEI,
            "EDP RENOVAVEIS SOCIEDAD ANONIMA",
            "ES",
            "ES",
            _EDP_LEI,
        ),
        _company(
            "pt_lei_" + _EDP_LEI,
            "EDP RENOVAVEIS SOCIEDAD ANONIMA",
            "PT",
            "PT",
            _EDP_LEI,
        ),
    ]
    assert companies[0].id.startswith("es_")  # ES ranks first: the silent harm
    merged = deduplicate_companies_by_lei(list(reversed(companies)))
    assert [company.id for company in merged] == ["es_lei_" + _EDP_LEI]
    assert merged[0].country_code == "ES"
    assert merged[0].other_jurisdictions == ("PT",)


def test_dedup_is_order_independent_and_keeps_distinct_leis_distinct() -> None:
    es = _company("es_lei_" + _EDP_LEI, "EDP RENOVAVEIS SA", "ES", "ES", _EDP_LEI)
    pt = _company("pt_lei_" + _EDP_LEI, "EDP RENOVAVEIS SA", "PT", "PT", _EDP_LEI)
    telenor = _company(
        "no_lei_" + _TELENOR_LEI, "Telenor ASA", "NO", "NO", _TELENOR_LEI
    )
    telenor_dk = _company(
        "dk_lei_98450053D8E1B002C409",
        "Telenor Danmark Holding A/S",
        "DK",
        "DK",
        "98450053D8E1B002C409",
    )
    forward = deduplicate_companies_by_lei([es, pt, telenor, telenor_dk])
    backward = deduplicate_companies_by_lei([pt, es, telenor_dk, telenor])
    assert {company.id for company in forward} == {
        "es_lei_" + _EDP_LEI,
        "dk_lei_98450053D8E1B002C409",
        "no_lei_" + _TELENOR_LEI,
    }
    assert {company.id for company in forward} == {company.id for company in backward}
    assert len(forward) == 3  # EDP collapsed; the two Telenors stay distinct


def test_dedup_passes_through_records_without_lei() -> None:
    first = _company("mx_bmv_1", "Cemex", "MX", "MX")
    second = _company("mx_bmv_1", "Cemex", "MX", "MX")
    assert deduplicate_companies_by_lei([first, second]) == [first, second]


def test_search_companies_dedups_same_lei_but_list_filings_stays_per_market(
    tmp_path,
) -> None:
    """End to end through the service: search merges, filings do not."""

    import httpx

    from openfilings.adapters.esef import PORTUGAL, SPAIN, EsefClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/entities":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "type": "entity",
                            "id": "724",
                            "attributes": {
                                "name": "EDP RENOVAVEIS SOCIEDAD ANONIMA",
                                "identifier": _EDP_LEI,
                            },
                        }
                    ]
                },
            )
        if request.url.path == f"/api/entities/{_EDP_LEI}/filings":
            country = request.url.params.get("filter[country]")
            source_id = "111" if country == "ES" else "222"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "type": "filing",
                            "id": source_id,
                            "attributes": {
                                "country": country,
                                "period_end": "2024-12-31",
                                "date_added": "2025-03-01 10:00:00",
                                "report_url": (
                                    f"/{_EDP_LEI}/2024-12-31/ESEF/{country}/0/report.xhtml"
                                ),
                                "fxo_id": f"{_EDP_LEI}-2024-12-31-ESEF-{country}-0",
                                "error_count": 0,
                                "warning_count": 0,
                            },
                            "relationships": {"entity": {"data": {"id": "724"}}},
                        }
                    ],
                    "included": [
                        {
                            "type": "entity",
                            "id": "724",
                            "attributes": {
                                "name": "EDP RENOVAVEIS SOCIEDAD ANONIMA",
                                "identifier": _EDP_LEI,
                            },
                        }
                    ],
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    async def scenario() -> tuple[list[Company], list[str], list[str]]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            cache = SQLiteCache(tmp_path / "cache.sqlite3")
            service = OpenFilingsService(
                cache,
                esef_sources=(
                    EsefClient(SPAIN, client=http),
                    EsefClient(PORTUGAL, client=http),
                ),
            )
            companies = await service.search_companies("EDP")
            es_filings = await service.list_filings("es_lei_" + _EDP_LEI)
            pt_filings = await service.list_filings("pt_lei_" + _EDP_LEI)
            cache.close()
            return (
                companies,
                [filing.id for filing in es_filings],
                [filing.id for filing in pt_filings],
            )

    import asyncio

    companies, es_ids, pt_ids = asyncio.run(scenario())
    assert [company.id for company in companies] == ["es_lei_" + _EDP_LEI]
    assert companies[0].other_jurisdictions == ("PT",)
    # Per-market filing discovery is intact: each ID lists only its own filing.
    assert es_ids == ["es_esef_111"]
    assert pt_ids == ["pt_esef_222"]


class _Outcome:
    def __init__(
        self,
        *,
        company: bool = False,
        filings: bool | str = False,
        financials: str = "held",
    ) -> None:
        self.company = company
        self.filings = filings
        self.financials = financials


class _ProbeService:
    def __init__(self, outcomes: dict[str, _Outcome]) -> None:
        self._outcomes = outcomes

    async def company(self, query: str, source: str = "all") -> _ProbeCompany:
        outcome = self._outcomes.get(query)
        if outcome is None or not outcome.company:
            from openfilings.exceptions import CompanyNotFoundError

            raise CompanyNotFoundError(f"No company matched {query!r}.")
        return _ProbeCompany(query, source, outcome)


class _ProbeCompany:
    def __init__(self, query: str, source: str, outcome: _Outcome) -> None:
        self.id = f"company-{query}"
        self._outcome = outcome

    async def get_filings(self, source: str = "all", limit: int = 5) -> _Filings:
        if self._outcome.filings == "hang":
            import asyncio

            await asyncio.sleep(10)
        return _Filings(self._outcome)


class _Filings:
    def __init__(self, outcome: _Outcome) -> None:
        self._outcome = outcome
        self._filings = (
            [_ProbeFiling(outcome.financials)] if outcome.filings is True else []
        )

    def latest(self) -> _ProbeFiling | None:
        return self._filings[0] if self._filings else None

    def __getitem__(self, index: object) -> object:
        return self._filings[index]  # type: ignore[index]


class _ProbeFiling:
    def __init__(self, financials: str) -> None:
        self.id = "filing-1"
        self._financials = financials

    async def financials(self, refresh: bool = True) -> object:
        if self._financials == "held":
            return _HeldFinancials()
        if self._financials == "no-balance":
            return _NoBalanceFinancials()
        from openfilings.exceptions import FinancialsUnavailableError

        raise FinancialsUnavailableError("no financials")


class _HeldFinancials:
    def balance_sheet(self) -> object:
        from datetime import date
        from decimal import Decimal

        from openfilings.models import (
            FinancialLineItem,
            FinancialStatement,
            FinancialValue,
            ReportingPeriod,
        )

        def item(code: str, value: Decimal) -> FinancialLineItem:
            return FinancialLineItem(
                code=code,  # type: ignore[arg-type]
                name=code,
                concept=f"ifrs-full:{code}",
                values=(
                    FinancialValue(
                        period=ReportingPeriod(
                            id="i",
                            end_date=date(2024, 12, 31),
                            kind="instant",
                            fiscal_period="instant",
                        ),
                        value=value,
                        unit="EUR",
                    ),
                ),
            )

        return FinancialStatement(
            statement_type="balance_sheet",
            title="Balance sheet",
            line_items=(
                item("total_assets", Decimal("300")),
                item("total_liabilities", Decimal("200")),
                item("total_equity", Decimal("100")),
            ),
        )


class _NoBalanceFinancials:
    def balance_sheet(self) -> None:
        return None


def _run(service: object, cases: object, **kwargs: object) -> object:
    import asyncio

    return asyncio.run(
        run_live_probe(service, cases=cases, **kwargs)  # type: ignore[arg-type]
    )
