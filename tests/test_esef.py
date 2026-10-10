from __future__ import annotations

import json

import httpx
import pytest

from openfilings.adapters.esef import (
    DENMARK,
    ENABLED_ESEF_MARKETS,
    FINLAND,
    FRANCE,
    ITALY,
    SPAIN,
    SWEDEN,
    EsefClient,
    EsefMarket,
)
from openfilings.exceptions import DocumentUnavailableError, SourceError

NETHERLANDS = EsefMarket(
    country_code="NL",
    market_code="NL",
    country_name="Netherlands",
)
ASML_LEI = "724500Y6DUVHQD6OXN27"


def test_enabled_esef_markets() -> None:
    assert tuple(market.country_code for market in ENABLED_ESEF_MARKETS) == (
        "NL",
        "FR",
        "ES",
        "IT",
        "DK",
        "SE",
        "FI",
        "NO",
        "PL",
        "BE",
        "AT",
        "LU",
        "PT",
    )


@pytest.mark.asyncio
async def test_search_companies_filters_name_and_market() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/entities"
        filters = json.loads(request.url.params["filter"])
        assert filters == [
            {"name": "name", "op": "ilike", "val": "%ASML%"},
            {"name": "filings.country", "op": "eq", "val": "NL"},
        ]
        return httpx.Response(200, json=_entity_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        source = EsefClient(NETHERLANDS, client=http)
        companies = await source.search_companies("ASML")

    assert len(companies) == 1
    assert companies[0].id == f"nl_lei_{ASML_LEI}"
    assert companies[0].name == "ASML Holding N.V."
    assert companies[0].market == "NL"
    assert companies[0].country_code == "NL"
    assert companies[0].sources == ("esef",)


@pytest.mark.asyncio
async def test_search_companies_accepts_prefixed_lei() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        filters = json.loads(request.url.params["filter"])
        assert filters[0] == {"name": "identifier", "op": "eq", "val": ASML_LEI}
        return httpx.Response(200, json=_entity_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        source = EsefClient(NETHERLANDS, client=http)
        companies = await source.search_companies(f"nl_lei_{ASML_LEI}")

    assert companies[0].lei == ASML_LEI


def _entity(name: str, lei: str, entity_id: str) -> dict[str, object]:
    return {
        "type": "entity",
        "id": entity_id,
        "attributes": {"name": name, "identifier": lei},
    }


@pytest.mark.asyncio
async def test_search_companies_ranks_locally_instead_of_trusting_registry_order() -> (
    None
):
    """Upstream returns matches in registry order, so slicing its first
    `limit` rows made "ING" resolve to N.V. Bever Holding - an unrelated
    name that merely contains "ing" inside "Holding" - ahead of ING GROEP
    N.V. Ranking locally must put the prefix matches first."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/entities"
        filters = json.loads(request.url.params["filter"])
        assert filters[0] == {"name": "name", "op": "ilike", "val": "%ING%"}
        return httpx.Response(
            200,
            json={
                "data": [
                    _entity("N.V. Bever Holding", "724500X2YFDG0L56AS37", "111"),
                    _entity("ING GROEP N.V.", "549300NYKK9MWM7GGW15", "222"),
                    _entity("ING BANK N.V.", "3TK20IVIUJ8J3ZU0QE75", "333"),
                ],
                "meta": {"count": 3},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        source = EsefClient(NETHERLANDS, client=http)
        companies = await source.search_companies("ING", limit=2)

    assert {company.name for company in companies} == {
        "ING BANK N.V.",
        "ING GROEP N.V.",
    }


@pytest.mark.asyncio
async def test_search_companies_retries_a_fragment_when_a_diacritic_blocks_ilike() -> (
    None
):
    """filings.xbrl.org matches names with a byte-wise ilike, so "Wartsila"
    can never match "WÄRTSILÄ OYJ ABP". The accent-free consonant run "rts"
    can, and folding the returned row locally then ranks it."""

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        value = json.loads(request.url.params["filter"])[0]["val"]
        seen.append(value)
        if value == "%rts%":
            return httpx.Response(
                200,
                json={
                    "data": [_entity("WÄRTSILÄ OYJ ABP", "743700G7A9J1PHM3X223", "9")],
                    "meta": {"count": 1},
                },
            )
        return httpx.Response(200, json={"data": [], "meta": {"count": 0}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        source = EsefClient(FINLAND, client=http)
        companies = await source.search_companies("Wartsila")

    assert seen[0] == "%Wartsila%"
    assert "%rts%" in seen
    assert [company.name for company in companies] == ["WÄRTSILÄ OYJ ABP"]


def test_fallback_patterns_carry_the_accent_free_consonant_run() -> None:
    """Both live misses: a diacritic mid-name (WÄRTSILÄ) and a leading one
    (ØRSTED A/S) - the first character of the query is unusable there, so
    the run has to carry the retry."""

    assert "%rts%" in EsefClient._fallback_patterns("Wartsila")
    assert "%rst%" in EsefClient._fallback_patterns("Ørsted")


@pytest.mark.asyncio
async def test_list_filings_maps_inline_report_and_period() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/entities/{ASML_LEI}/filings"
        assert request.url.params["filter[country]"] == "NL"
        assert request.url.params["sort"] == "-period_end"
        return httpx.Response(200, json=_filing_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        source = EsefClient(NETHERLANDS, client=http)
        filings = await source.list_filings(f"nl_lei_{ASML_LEI}")

    assert len(filings) == 1
    filing = filings[0]
    assert filing.id == "nl_esef_23718"
    assert filing.company_id == f"nl_lei_{ASML_LEI}"
    assert filing.source == "esef"
    assert filing.filing_type == "financial_report"
    assert filing.title == "Financial report for the period ended 2025-12-31"
    assert filing.filing_date.isoformat() == "2026-03-04"
    assert filing.period_end and filing.period_end.isoformat() == "2025-12-31"
    assert filing.media_type == "application/xhtml+xml"
    assert filing.language == "en"
    assert filing.xbrl_available is True
    assert filing.document_id and filing.document_id.endswith("asml-report-en.xhtml")


@pytest.mark.asyncio
async def test_get_filing_rejects_a_different_country() -> None:
    payload = _filing_response()
    payload["data"][0]["attributes"]["country"] = "FR"  # type: ignore[index]

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": payload["data"][0],  # type: ignore[index]
                "included": payload["included"],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        source = EsefClient(NETHERLANDS, client=http)
        with pytest.raises(SourceError, match="does not belong to Netherlands"):
            await source.get_filing("23718")


@pytest.mark.asyncio
async def test_download_document_returns_xhtml_with_provenance() -> None:
    path = f"/{ASML_LEI}/2025-12-31/ESEF/NL/0/asml-report-en.xhtml"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == path
        return httpx.Response(
            200,
            headers={"content-type": "application/xhtml+xml; charset=UTF-8"},
            content=b"<html><body><h1>Annual report</h1></body></html>",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        source = EsefClient(NETHERLANDS, client=http)
        document = await source.download_document(path)

    assert document.media_type == "application/xhtml+xml"
    assert document.profile == "esef"
    assert document.source_url == f"https://filings.xbrl.org{path}"


def test_document_url_rejects_external_hosts() -> None:
    with pytest.raises(DocumentUnavailableError, match="Unsafe"):
        EsefClient.document_url("https://example.test/report.xhtml")


@pytest.mark.parametrize(
    ("market", "lei", "name"),
    (
        (FRANCE, "529900S21EQ1BO4ESM68", "Example France S.A."),
        (SPAIN, "5QK37QC7NWOJ8D7WVQ45", "Example España S.A."),
        (ITALY, "54930000000000000001", "Example Italia S.p.A."),
        (DENMARK, "52990000000000000001", "Example Danmark A/S"),
        (SWEDEN, "54930000000000000001", "Example Sverige AB"),
        (FINLAND, "74370000000000000001", "Example Suomi Oyj"),
    ),
)
@pytest.mark.asyncio
async def test_same_adapter_generates_country_specific_market_ids(
    market: EsefMarket, lei: str, name: str
) -> None:

    def handler(request: httpx.Request) -> httpx.Response:
        filters = json.loads(request.url.params["filter"])
        assert filters[1]["val"] == market.country_code
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "type": "entity",
                        "id": "100",
                        "attributes": {
                            "name": name,
                            "identifier": lei,
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        source = EsefClient(market, client=http)
        companies = await source.search_companies(name)

    assert companies[0].id == f"{market.id_prefix}_lei_{lei}"
    assert companies[0].market == market.market_code
    assert companies[0].country_code == market.country_code


def _entity_response() -> dict[str, object]:
    return {
        "data": [
            {
                "type": "entity",
                "id": "1969",
                "attributes": {
                    "name": "ASML Holding N.V.",
                    "identifier": ASML_LEI,
                },
            }
        ],
        "meta": {"count": 1},
    }


def _filing_response() -> dict[str, object]:
    report_path = f"/{ASML_LEI}/2025-12-31/ESEF/NL/0/asml-report-en.xhtml"
    return {
        "data": [
            {
                "type": "filing",
                "id": "23718",
                "attributes": {
                    "country": "NL",
                    "period_end": "2025-12-31",
                    "date_added": "2026-03-04 13:55:19.061861",
                    "processed": "2026-03-10 14:54:03.949595",
                    "fxo_id": f"{ASML_LEI}-2025-12-31-ESEF-NL-0",
                    "report_url": report_path,
                    "package_url": f"/{ASML_LEI}/report.xbri",
                    "error_count": 0,
                    "warning_count": 6,
                },
                "relationships": {"entity": {"data": {"type": "entity", "id": "1969"}}},
            }
        ],
        "included": _entity_response()["data"],
        "meta": {"count": 1},
    }
