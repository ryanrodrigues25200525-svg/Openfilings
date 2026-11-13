"""Ticker resolution through the LEI crosswalk (service integration)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from openfilings.adapters.esef import NETHERLANDS, EsefClient
from openfilings.adapters.fca_nsm import FcaNsmClient
from openfilings.exceptions import SourceError
from openfilings.models import Company
from openfilings.service import OpenFilingsService
from openfilings.storage.sqlite import SQLiteCache

_LEI = "21380068P1DRHMJ8KU70"
_COMPANY_ID = f"uk_lei_{_LEI}"


class _FakeCrosswalk:
    """Duck-typed stand-in for openfilings.crosswalk.Crosswalk."""

    def __init__(
        self,
        hit: SimpleNamespace | None,
        *,
        raises: bool = False,
        market_hits: dict[str | None, SimpleNamespace | None] | None = None,
    ) -> None:
        self.hit = hit
        self.raises = raises
        self.market_hits = market_hits or {}
        self.calls: list[dict[str, Any]] = []

    async def resolve(
        self,
        query: str,
        *,
        market: str | None = None,
        verifier: Any = None,
    ) -> SimpleNamespace:
        call: dict[str, Any] = {"query": query, "market": market}
        self.calls.append(call)
        if self.raises:
            raise SourceError("OpenFIGI request failed")
        hit = self.market_hits.get(market, self.hit)
        if hit is not None and hit.company_id is not None and verifier is not None:
            call["verified"] = await verifier(hit.company_id)
        return hit if hit is not None else _no_hit()


def _hit() -> SimpleNamespace:
    return SimpleNamespace(
        query="SHEL",
        market="uk",
        ticker="SHEL",
        name="SHELL PLC",
        isin=None,
        lei=_LEI,
        company_id=_COMPANY_ID,
        method="ticker",
        confidence="verified",
    )


def _no_hit() -> SimpleNamespace:
    """No candidate found - the answer a real Crosswalk returns, not None."""

    return SimpleNamespace(
        query="",
        market=None,
        ticker=None,
        name=None,
        isin=None,
        lei=None,
        company_id=None,
        method="ticker",
        confidence="candidate",
    )


def _nsm_handler(
    lei_hits: bool = True,
    name_hits: bool = False,
    *,
    company_name: str = "TESCO PLC",
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        criteria = payload.get("criteriaObj", {}).get("criteria", [])
        haystack = json.dumps(criteria)
        if _LEI in haystack:
            hits = [_nsm_filing_hit()] if lei_hits else []
        elif name_hits:
            hits = [_nsm_company_hit(company_name)]
        else:
            hits = []
        return httpx.Response(
            200,
            json={
                "hits": {
                    "total": {"value": len(hits), "relation": "eq"},
                    "hits": hits,
                }
            },
        )

    return httpx.MockTransport(handler)


def _nsm_company_hit(company: str = "TESCO PLC") -> dict[str, Any]:
    return {
        "_id": "tesco-company",
        "_source": {
            "disclosure_id": "tesco-company",
            "company": company,
            "lei": _LEI,
            "related_org": [],
            "headline": "Annual Report",
            "type": "Annual Report",
            "type_code": "ACS",
            "document_date": "2026-03-01",
            "publication_date": "2026-03-01T07:00:00Z",
            "download_link": "NSM/RNS/disclosure-900.html",
            "latest_flag": "Y",
        },
    }


def _nsm_filing_hit() -> dict[str, Any]:
    return {
        "_id": "disclosure-900",
        "_source": {
            "disclosure_id": "disclosure-900",
            "company": "SHELL PLC",
            "lei": _LEI,
            "related_org": [],
            "headline": "Annual Report",
            "type": "Annual Report",
            "type_code": "ACS",
            "document_date": "2026-03-01",
            "publication_date": "2026-03-01T07:00:00Z",
            "download_link": "NSM/RNS/disclosure-900.html",
            "latest_flag": "Y",
        },
    }


class _JunkSource:
    """Market-source stub returning fuzzy, unrelated hits.

    Mirrors what EDINET does to a Latin ticker - it answers almost any
    string with a page of unrelated Japanese issuers.
    """

    source = "smv"

    def __init__(self, companies: tuple[Company, ...]) -> None:
        self._companies = companies

    async def search_companies(self, query: str, *, limit: int = 10) -> list[Company]:
        return list(self._companies[:limit])

    async def aclose(self) -> None:
        return None


def _junk_company() -> Company:
    return Company(
        id="mx_smv_1",
        source_id="1",
        name="ORIGINAL ENGINEERING",
        sources=("smv",),
        market="MX",
        country_code="MX",
        source_url="https://example.test/mx_smv_1",
    )


def _service(
    tmp_path: Any,
    crosswalk: _FakeCrosswalk,
    *,
    lei_hits: bool = True,
    name_hits: bool = False,
    company_name: str = "TESCO PLC",
    esef: bool = False,
    market_sources: tuple[Any, ...] = (),
) -> tuple[OpenFilingsService, httpx.AsyncClient]:
    http_client = httpx.AsyncClient(
        transport=_nsm_handler(
            lei_hits=lei_hits, name_hits=name_hits, company_name=company_name
        )
    )
    nsm = FcaNsmClient(client=http_client)
    esef_sources = (EsefClient(NETHERLANDS, client=http_client),) if esef else ()
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    service = OpenFilingsService(
        cache,
        nsm_source=nsm,
        esef_sources=esef_sources,
        market_sources=market_sources,  # type: ignore[arg-type]
        crosswalk=crosswalk,  # type: ignore[arg-type]
    )
    return service, http_client


@pytest.mark.asyncio
async def test_ticker_search_falls_back_to_crosswalk(tmp_path) -> None:
    crosswalk = _FakeCrosswalk(_hit())
    service, http_client = _service(tmp_path, crosswalk)
    async with http_client:
        companies = await service.search_companies("SHEL")
        cache_hit = service._cache.get_company(_COMPANY_ID)
        cache_hit_name = cache_hit.name if cache_hit is not None else None

    assert [company.id for company in companies] == [_COMPANY_ID]
    assert companies[0].market == "UK"
    assert companies[0].country_code == "UK"
    assert companies[0].sources == ("fca_nsm",)
    assert companies[0].name == "SHELL PLC"
    assert companies[0].ticker == "SHEL"
    assert companies[0].lei == _LEI
    assert companies[0].source_url.startswith("https://data.fca.org.uk/")
    assert cache_hit_name == "SHELL PLC"
    assert crosswalk.calls == [{"query": "SHEL", "market": None, "verified": True}]


@pytest.mark.asyncio
async def test_resolve_company_infers_market_for_tickers(tmp_path) -> None:
    crosswalk = _FakeCrosswalk(_hit())
    service, http_client = _service(tmp_path, crosswalk)
    async with http_client:
        company = await service.resolve_company("Shell", market="uk")

    # Ticker-shaped queries resolve through the market-inferring fallback, so
    # no hint is needed; the returned market still honors the request.
    assert company is not None
    assert company.id == _COMPANY_ID
    assert company.market == "UK"
    assert crosswalk.calls == [{"query": "Shell", "market": None, "verified": True}]


@pytest.mark.asyncio
async def test_crosswalk_company_passes_market_hint(tmp_path) -> None:
    crosswalk = _FakeCrosswalk(_hit())
    service, http_client = _service(tmp_path, crosswalk)
    async with http_client:
        company = await service._crosswalk_company("Shell plc", market="UK")

    assert company is not None
    assert company.id == _COMPANY_ID
    assert crosswalk.calls == [{"query": "Shell plc", "market": "UK", "verified": True}]


@pytest.mark.asyncio
async def test_resolve_company_skips_crosswalk_when_name_matches(tmp_path) -> None:
    crosswalk = _FakeCrosswalk(_hit())
    service, http_client = _service(tmp_path, crosswalk, name_hits=True)
    async with http_client:
        company = await service.resolve_company("Tesco", market="UK")

    assert company is not None
    assert company.id == _COMPANY_ID
    assert company.name == "TESCO PLC"
    assert crosswalk.calls == []


@pytest.mark.asyncio
async def test_prefix_name_hit_does_not_answer_a_ticker(tmp_path) -> None:
    """Regression: SHEL matches SHELL INTERNATIONAL FINANCE B.V. as a mere
    letter prefix, but the ticker belongs to Shell plc (live 2026-10-10,
    where the resolve returned the finance vehicle)."""

    crosswalk = _FakeCrosswalk(_hit())
    service, http_client = _service(
        tmp_path,
        crosswalk,
        name_hits=True,
        company_name="SHELL INTERNATIONAL FINANCE B.V.",
    )
    async with http_client:
        company = await service.resolve_company("SHEL", market="UK")

    assert company is not None
    assert company.id == _COMPANY_ID
    assert company.name == "SHELL PLC"
    assert crosswalk.calls == [{"query": "SHEL", "market": None, "verified": True}]


@pytest.mark.asyncio
async def test_verified_crosswalk_hit_beats_a_prefix_name_hit(tmp_path) -> None:
    crosswalk = _FakeCrosswalk(_no_hit(), market_hits={"UK": _hit()})
    service, http_client = _service(
        tmp_path,
        crosswalk,
        name_hits=True,
        company_name="SHELL INTERNATIONAL FINANCE B.V.",
    )
    async with http_client:
        company = await service.resolve_company("SHEL", market="UK")

    # The market-scoped lookup runs the crosswalk itself for a name the
    # query only prefixes, so the request's own market still gets its
    # verified answer rather than the prefix hit.
    assert company is not None
    assert company.id == _COMPANY_ID
    assert crosswalk.calls == [
        {"query": "SHEL", "market": None},
        {"query": "SHEL", "market": "UK", "verified": True},
    ]


@pytest.mark.asyncio
async def test_prefix_name_hit_stands_when_the_crosswalk_has_no_answer(
    tmp_path,
) -> None:
    crosswalk = _FakeCrosswalk(_no_hit())
    service, http_client = _service(
        tmp_path,
        crosswalk,
        name_hits=True,
        company_name="SHELL INTERNATIONAL FINANCE B.V.",
    )
    async with http_client:
        company = await service.resolve_company("SHEL", market="UK")

    assert company is not None
    assert company.name == "SHELL INTERNATIONAL FINANCE B.V."
    assert crosswalk.calls == [
        {"query": "SHEL", "market": None},
        {"query": "SHEL", "market": "UK"},
    ]


@pytest.mark.asyncio
async def test_name_queries_never_reach_the_crosswalk(tmp_path) -> None:
    crosswalk = _FakeCrosswalk(_hit())
    service, http_client = _service(tmp_path, crosswalk)
    async with http_client:
        companies = await service.search_companies("Shell plc")

    assert companies == []
    assert crosswalk.calls == []


@pytest.mark.asyncio
async def test_unrelated_fuzzy_hits_do_not_block_the_crosswalk(tmp_path) -> None:
    crosswalk = _FakeCrosswalk(_hit())
    service, http_client = _service(
        tmp_path, crosswalk, market_sources=(_JunkSource((_junk_company(),)),)
    )
    async with http_client:
        companies = await service.search_companies("TSCO")

    assert companies[0].id == _COMPANY_ID
    assert any(company.name == "ORIGINAL ENGINEERING" for company in companies)
    assert crosswalk.calls == [{"query": "TSCO", "market": None, "verified": True}]


@pytest.mark.asyncio
async def test_non_lei_market_does_not_use_crosswalk(tmp_path) -> None:
    crosswalk = _FakeCrosswalk(_hit())
    service, http_client = _service(tmp_path, crosswalk)
    async with http_client:
        company = await service.resolve_company("SHEL", market="TR")

    # The market-agnostic ticker fallback may resolve elsewhere (market=None),
    # but a company outside the requested market is never returned.
    assert company is None
    assert [call["market"] for call in crosswalk.calls] == [None]


@pytest.mark.asyncio
async def test_unverified_candidate_still_returns_a_company(tmp_path) -> None:
    crosswalk = _FakeCrosswalk(_hit())
    service, http_client = _service(tmp_path, crosswalk, lei_hits=False)
    async with http_client:
        companies = await service.search_companies("SHEL")

    assert [company.id for company in companies] == [_COMPANY_ID]
    assert crosswalk.calls == [{"query": "SHEL", "market": None, "verified": False}]


@pytest.mark.asyncio
async def test_crosswalk_failure_degrades_to_empty_search(tmp_path) -> None:
    crosswalk = _FakeCrosswalk(None, raises=True)
    service, http_client = _service(tmp_path, crosswalk)
    async with http_client:
        companies = await service.search_companies("SHEL")

    assert companies == []


def test_lei_keyed_source_classification(tmp_path) -> None:
    crosswalk = _FakeCrosswalk(None)
    service, _http_client = _service(tmp_path, crosswalk, esef=True)
    try:
        assert service._lei_keyed_source("UK") == "fca_nsm"
        assert service._lei_keyed_source("NL") == "esef"
        assert service._lei_keyed_source("CH") is None
        assert service._lei_keyed_source_for_company_id(f"nl_lei_{_LEI}") == "esef"
        assert service._lei_keyed_source_for_company_id(f"uk_lei_{_LEI}") == "fca_nsm"
        assert service._lei_keyed_source_for_company_id(f"de_lei_{_LEI}") is None
        assert service._lei_keyed_source_for_company_id(_LEI) is None
    finally:
        service._cache.close()
