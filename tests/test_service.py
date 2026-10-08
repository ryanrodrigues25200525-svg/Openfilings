from __future__ import annotations

import json
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from openfilings.adapters.base import SourceDocument
from openfilings.adapters.esef import NETHERLANDS, EsefClient
from openfilings.adapters.fca_nsm import FcaNsmClient
from openfilings.exceptions import ConfigurationError, FinancialsUnavailableError
from openfilings.models import (
    Filing,
    FilingFinancials,
    FinancialLineItem,
    FinancialStatement,
    FinancialValue,
    ReportingPeriod,
)
from openfilings.service import OpenFilingsService
from openfilings.storage.sqlite import SQLiteCache


@pytest.mark.asyncio
async def test_fca_pipeline_uses_compressed_content_cache(tmp_path) -> None:
    calls: Counter[str] = Counter()

    def handler(request: httpx.Request) -> httpx.Response:
        calls[request.url.path] += 1
        if request.method == "POST":
            return httpx.Response(200, json=_nsm_search_response())
        if request.url.path == "/artefacts/NSM/RNS/disclosure-123.html":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"<html><body><h2>Trading update</h2></body></html>",
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        nsm = FcaNsmClient(client=http_client)
        cache = SQLiteCache(tmp_path / "cache.sqlite3")
        service = OpenFilingsService(cache, nsm_source=nsm)

        companies = await service.search_companies("Tesco")
        filings = await service.list_filings(companies[0].id)
        first = await service.get_filing_markdown(filings[0].id)
        second = await service.get_filing_markdown(filings[0].id)
        cache.close()

    assert first.from_cache is False
    assert second.from_cache is True
    assert first.markdown == second.markdown
    assert companies[0].id == "uk_lei_2138002P5RNKC5W2JZ46"
    assert companies[0].sources == ("fca_nsm",)
    assert {filing.source for filing in filings} == {"fca_nsm"}
    assert "## Trading update" in first.markdown
    assert calls["/artefacts/NSM/RNS/disclosure-123.html"] == 1


@pytest.mark.asyncio
async def test_nsm_insider_and_major_holdings_categories_map_to_type_codes(
    tmp_path,
) -> None:
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json=_nsm_search_response())
        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        nsm = FcaNsmClient(client=http_client)
        cache = SQLiteCache(tmp_path / "cache.sqlite3")
        service = OpenFilingsService(cache, nsm_source=nsm)

        await service.list_filings("uk_lei_2138002P5RNKC5W2JZ46", category="insider")
        await service.list_filings(
            "uk_lei_2138002P5RNKC5W2JZ46", category="major_holdings"
        )
        await service.list_filings(
            "uk_lei_2138002P5RNKC5W2JZ46", category="pdmr_dealings"
        )
        await service.list_filings(
            "uk_lei_2138002P5RNKC5W2JZ46", category="current_report"
        )
        await service.list_filings("uk_lei_2138002P5RNKC5W2JZ46", category="proxy")
        cache.close()

    insider_criteria = payloads[-5]["criteriaObj"]["criteria"]
    major_holdings_criteria = payloads[-4]["criteriaObj"]["criteria"]
    pdmr_criteria = payloads[-3]["criteriaObj"]["criteria"]
    current_report_criteria = payloads[-2]["criteriaObj"]["criteria"]
    proxy_criteria = payloads[-1]["criteriaObj"]["criteria"]
    assert {"name": "type_code", "value": ["dsh"]} in insider_criteria
    assert {"name": "type_code", "value": ["hol"]} in major_holdings_criteria
    assert {"name": "type_code", "value": ["dsh"]} in pdmr_criteria
    assert {
        "name": "type_code",
        "value": ["upd", "acq", "dis", "tst", "boa"],
    } in current_report_criteria
    assert {"name": "type_code", "value": ["rag", "noa", "rom"]} in proxy_criteria


@pytest.mark.asyncio
async def test_search_disclosures_rejects_unsupported_source(tmp_path) -> None:
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    service = OpenFilingsService(cache)

    with pytest.raises(ConfigurationError, match="fca_nsm and cvm"):
        await service.search_disclosures("lithium", source="sedar")  # type: ignore[arg-type]

    cache.close()


@pytest.mark.asyncio
async def test_get_company_facts_merges_multiple_filings_newest_first(
    tmp_path,
) -> None:
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    service = OpenFilingsService(cache)

    filings = [
        Filing(
            id="f1",
            company_id="c1",
            source="cvm",
            source_id="1",
            title="t1",
            category="accounts",
            filing_type="annual",
            filing_date=date(2026, 3, 1),
            period_end=date(2025, 12, 31),
            issuer_name="X",
            source_url="https://example.test/1",
        ),
        Filing(
            id="f2",
            company_id="c1",
            source="cvm",
            source_id="2",
            title="t2",
            category="accounts",
            filing_type="annual",
            filing_date=date(2025, 3, 1),
            period_end=date(2024, 12, 31),
            issuer_name="X",
            source_url="https://example.test/2",
        ),
    ]
    period_a = ReportingPeriod(
        id="pa", end_date=date(2025, 12, 31), kind="instant", fiscal_period="instant"
    )
    period_b = ReportingPeriod(
        id="pb", end_date=date(2024, 12, 31), kind="instant", fiscal_period="instant"
    )
    financials_by_filing = {
        "f1": FilingFinancials(
            filing_id="f1",
            company_id="c1",
            source_url="https://example.test/1",
            statements=(
                FinancialStatement(
                    statement_type="balance_sheet",
                    title="Balance sheet",
                    currency="USD",
                    line_items=(
                        FinancialLineItem(
                            code="total_assets",
                            name="Total assets",
                            concept="Assets",
                            values=(
                                FinancialValue(period=period_a, value=Decimal("100")),
                            ),
                        ),
                    ),
                ),
            ),
            fact_count=1,
            sha256="0" * 64,
        ),
        "f2": FilingFinancials(
            filing_id="f2",
            company_id="c1",
            source_url="https://example.test/2",
            statements=(
                FinancialStatement(
                    statement_type="balance_sheet",
                    title="Balance sheet",
                    currency="USD",
                    line_items=(
                        FinancialLineItem(
                            code="total_assets",
                            name="Total assets",
                            concept="Assets",
                            values=(
                                FinancialValue(period=period_b, value=Decimal("90")),
                            ),
                        ),
                    ),
                ),
            ),
            fact_count=1,
            sha256="0" * 64,
        ),
    }

    async def fake_list_filings(company_id, **_kwargs):
        return filings

    async def fake_get_financials(filing_id, **_kwargs):
        return financials_by_filing[filing_id]

    service.list_filings = fake_list_filings  # type: ignore[method-assign]
    service.get_filing_financials = fake_get_financials  # type: ignore[method-assign]

    facts = await service.get_company_facts("c1", periods=2)
    cache.close()

    assets = facts.balance_sheet().line_items[0]
    assert {value.period.label: value.value for value in assets.values} == {
        period_a.label: Decimal("100"),
        period_b.label: Decimal("90"),
    }
    assert facts.filing_ids == ("f1", "f2")


@pytest.mark.asyncio
async def test_get_company_facts_raises_when_no_filing_has_financials(
    tmp_path,
) -> None:
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    service = OpenFilingsService(cache)

    async def fake_list_filings(company_id, **_kwargs):
        return []

    service.list_filings = fake_list_filings  # type: ignore[method-assign]

    with pytest.raises(FinancialsUnavailableError):
        await service.get_company_facts("c1", periods=2)

    cache.close()


@pytest.mark.asyncio
async def test_get_company_facts_scans_past_recent_nonfinancial_disclosures(
    tmp_path,
) -> None:
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    service = OpenFilingsService(cache)
    filings = [
        Filing(
            id=filing_id,
            company_id="c1",
            source="fca_nsm",
            source_id=filing_id,
            title=filing_id,
            category="accounts",
            filing_type="annual",
            filing_date=date(2026, 3, index),
            issuer_name="X",
            source_url=f"https://example.test/{filing_id}",
        )
        for index, filing_id in enumerate(("notice", "annual"), start=1)
    ]
    period = ReportingPeriod(id="fy2025", end_date=date(2025, 12, 31), kind="instant")
    annual = FilingFinancials(
        filing_id="annual",
        company_id="c1",
        source_url="https://example.test/annual",
        statements=(
            FinancialStatement(
                statement_type="balance_sheet",
                title="Balance sheet",
                line_items=(
                    FinancialLineItem(
                        code="total_assets",
                        name="Total assets",
                        concept="Assets",
                        values=(FinancialValue(period=period, value=Decimal("100")),),
                    ),
                ),
            ),
        ),
        fact_count=1,
        sha256="0" * 64,
    )
    received_limits: list[int] = []

    async def fake_list_filings(company_id, **kwargs):
        received_limits.append(kwargs["limit"])
        return filings

    async def fake_get_financials(filing_id, **_kwargs):
        if filing_id == "notice":
            raise FinancialsUnavailableError("not a financial report")
        return annual

    service.list_filings = fake_list_filings  # type: ignore[method-assign]
    service.get_filing_financials = fake_get_financials  # type: ignore[method-assign]

    facts = await service.get_company_facts("c1", periods=1)
    cache.close()

    assert facts.filing_ids == ("annual",)
    assert received_limits == [10]


@pytest.mark.asyncio
async def test_list_filings_prefers_newer_period_on_same_filing_date(
    tmp_path,
) -> None:
    """Issue #14: Intesa's 2021 re-filing (it_esef_7112) outranked its 2022
    filing (it_esef_6868) because both share filing_date 2023-05-30 and the
    tie broke on raw filing id. Same-day filings must break ties on period."""

    from openfilings.adapters.esef import ITALY, EsefClient

    lei = "2W8N8UU78PMDQKZENC08"
    old_id, new_id = "7112", "6868"
    old_period, new_period = "2021-12-31", "2022-12-31"

    def payload(filing_id: str, period_end: str) -> dict[str, object]:
        return {
            "type": "filing",
            "id": filing_id,
            "attributes": {
                "country": "IT",
                "period_end": period_end,
                "date_added": "2023-05-30 18:13:12.336496",
                "fxo_id": f"{lei}-{period_end}-ESEF-IT-0",
                "report_url": f"/{lei}/{period_end}/ESEF/IT/0/report.xhtml",
                "error_count": 0,
                "warning_count": 1,
            },
            "relationships": {"entity": {"data": {"type": "entity", "id": "3377"}}},
        }

    def handler(request: httpx.Request) -> httpx.Response:
        # Upstream returns the newer filing first; the service must keep it
        # first even though its numeric id is smaller.
        return httpx.Response(
            200,
            json={
                "data": [
                    payload(new_id, new_period),
                    payload(old_id, old_period),
                ],
                "included": [
                    {
                        "type": "entity",
                        "id": "3377",
                        "attributes": {
                            "name": "INTESA SANPAOLO SPA",
                            "identifier": lei,
                        },
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        esef = EsefClient(ITALY, client=http)
        cache = SQLiteCache(tmp_path / "cache.sqlite3")
        service = OpenFilingsService(cache, esef_sources=(esef,))
        filings = await service.list_filings(f"it_lei_{lei}")
        cache.close()

    assert [filing.id for filing in filings] == [
        f"it_esef_{new_id}",
        f"it_esef_{old_id}",
    ]


@pytest.mark.asyncio
async def test_major_holders_pipeline_lists_and_reverse_searches(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=_nsm_hol_search_response())
        if request.url.path.endswith("/tr1.html"):
            return httpx.Response(200, text=_tr1_fixture())
        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        nsm = FcaNsmClient(client=http_client)
        cache = SQLiteCache(tmp_path / "cache.sqlite3")
        service = OpenFilingsService(cache, nsm_source=nsm)

        holders = await service.list_major_holders(
            "uk_lei_213800TSKOLX4EU6L377", limit=5
        )
        matches = await service.search_major_holders(
            "Boston Partners", scan_limit=5, limit=5
        )
        no_matches = await service.search_major_holders(
            "Nobody Holdings Ltd", scan_limit=5, limit=5
        )
        cache.close()

    assert len(holders) == 1
    assert holders[0].holder_name == (
        "Boston Partners FKA Robeco Investment Management, Inc."
    )
    assert len(matches) == 1
    assert no_matches == []


@pytest.mark.asyncio
async def test_insider_dealings_pipeline_lists_structured_mar_forms(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=_nsm_dsh_search_response())
        if request.url.path.endswith("/pdmar.html"):
            return httpx.Response(200, text=_pdmar_fixture())
        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        nsm = FcaNsmClient(client=http_client)
        cache = SQLiteCache(tmp_path / "cache.sqlite3")
        service = OpenFilingsService(cache, nsm_source=nsm)

        dealings = await service.list_insider_dealings(
            "uk_lei_213800EC7997ZBLZJH69", limit=5
        )
        cache.close()

    assert len(dealings) == 1
    assert dealings[0].person_name == "Nikki Grady-Smith"
    assert dealings[0].transaction_dates == (date(2026, 3, 23),)


def _nsm_hol_search_response() -> dict[str, object]:
    return {
        "hits": {
            "hits": [
                {
                    "_source": {
                        "disclosure_id": "tr1-disclosure",
                        "company": "BABCOCK INTERNATIONAL GROUP PLC",
                        "lei": "213800TSKOLX4EU6L377",
                        "type": "Holding(s) in Company",
                        "headline": "Holding(s) in Company",
                        "type_code": "HOL",
                        "download_link": "NSM/RNS/tr1.html",
                        "publication_date": "2026-07-24T16:22:27Z",
                        "document_date": "2026-07-24T16:22:27Z",
                    }
                }
            ]
        }
    }


def _nsm_dsh_search_response() -> dict[str, object]:
    return {
        "hits": {
            "hits": [
                {
                    "_source": {
                        "disclosure_id": "pdmar-disclosure",
                        "company": "Rolls-Royce Holdings plc",
                        "lei": "213800EC7997ZBLZJH69",
                        "type": "Director/PDMR Shareholding",
                        "headline": "Director/PDMR Shareholding",
                        "type_code": "DSH",
                        "download_link": "NSM/RNS/pdmar.html",
                        "publication_date": "2026-03-24T16:22:27Z",
                        "document_date": "2026-03-24T16:22:27Z",
                    }
                }
            ]
        }
    }


def _tr1_fixture() -> str:
    path = Path(__file__).parent / "fixtures" / "fca_nsm_tr1_holding.html"
    return path.read_text(encoding="utf-8")


def _pdmar_fixture() -> str:
    path = Path(__file__).parent / "fixtures" / "fca_nsm_pdmar_dealing.html"
    return path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_unknown_source_is_rejected(tmp_path) -> None:
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    service = OpenFilingsService(cache)

    with pytest.raises(ConfigurationError, match="fca_nsm, edinet, esef"):
        await service.search_companies("Tesco", source="private-registry")  # type: ignore[arg-type]

    cache.close()


@pytest.mark.asyncio
async def test_netherlands_esef_pipeline_searches_lists_and_extracts(tmp_path) -> None:
    lei = "724500Y6DUVHQD6OXN27"
    report_path = f"/{lei}/2025-12-31/ESEF/NL/0/asml-report-en.xhtml"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/entities":
            return httpx.Response(200, json=_esef_entity_response(lei))
        if request.url.path == f"/api/entities/{lei}/filings":
            return httpx.Response(200, json=_esef_filing_response(lei, report_path))
        if request.url.path == report_path:
            return httpx.Response(
                200,
                headers={"content-type": "application/xhtml+xml"},
                content=(
                    b"<html><body><h2>Financial statements</h2>"
                    b"<p>Revenue and operating income.</p></body></html>"
                ),
            )
        raise AssertionError(f"Unexpected ESEF request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        esef = EsefClient(NETHERLANDS, client=http)
        cache = SQLiteCache(tmp_path / "cache.sqlite3")
        service = OpenFilingsService(cache, esef_sources=(esef,))

        company = await service.company("ASML", source="esef")
        filings = await company.get_filings(source="esef")
        filing = filings.latest()
        assert filing is not None
        document = await filing.obj()
        markdown = await filing.markdown()
        search_results = await filing.search("revenue operating")
        resolved = await service.filing(filing.id)
        prefetch = await filings.prefetch()

        offline_service = OpenFilingsService(cache)
        offline_company = await offline_service.company("ASML", offline=True)
        offline_filings = await offline_company.get_filings(offline=True)
        cache.close()

    assert company.id == f"nl_lei_{lei}"
    assert company.name == "ASML Holding N.V."
    assert filing.id == "nl_esef_23718"
    assert "## Financial statements" in document.markdown
    assert document.content.extraction_method == "markdownify"
    assert markdown == document.markdown
    assert search_results[0].section.title == "Financial statements"
    assert resolved.record == filing.record
    assert prefetch.documents_cached == 1
    assert prefetch.failures == ()
    assert offline_company.id == company.id
    assert offline_filings.latest() is not None
    assert offline_filings.latest().id == filing.id


@pytest.mark.asyncio
async def test_image_only_financials_use_configured_ocr(tmp_path, monkeypatch) -> None:
    import pymupdf

    pdf = pymupdf.open()
    pdf.new_page()
    pdf_bytes = pdf.tobytes()
    pdf.close()

    filing = Filing(
        id="sg_sgx_scanned",
        company_id="sg_sgx_1L01",
        source="sgx",
        source_id="scanned",
        title="2025 Annual Report",
        category="accounts",
        filing_type="annual",
        filing_date="2026-03-01",
        period_end="2025-12-31",
        document_id="https://example.test/scanned.pdf",
        media_type="application/pdf",
        issuer_name="Example Limited",
        pdf_available=True,
        source_url="https://example.test/scanned.pdf",
    )
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    cache.put_filings([filing])
    service = OpenFilingsService(
        cache,
        ocr_available=lambda _: True,
        ocr_converter=lambda *_args, **_kwargs: _ocr_financial_statement(),
    )

    async def download(_filing: Filing) -> SourceDocument:
        return SourceDocument(
            data=pdf_bytes,
            media_type="application/pdf",
            source_url="https://example.test/scanned.pdf",
        )

    monkeypatch.setattr(service, "_download_document", download)
    financials = await service.get_filing_financials(filing.id)
    cache.close()

    assert financials.extraction_method == "pdf-ocr-text"
    assert financials.income_statement() is not None


def _ocr_financial_statement() -> str:
    return """
    ## Page 1

    Consolidated income statement
    S$ million
    2025
    2024
    Revenue
    1,234
    1,100
    Operating profit
    300
    250
    Profit before tax
    280
    230
    """


def _nsm_search_response() -> dict[str, object]:
    return {
        "hits": {
            "total": {"value": 1, "relation": "eq"},
            "hits": [
                {
                    "_id": "disclosure-123",
                    "_source": {
                        "disclosure_id": "disclosure-123",
                        "company": "TESCO PLC",
                        "lei": "2138002P5RNKC5W2JZ46",
                        "related_org": [],
                        "headline": "Trading update",
                        "type": "Trading Statement",
                        "type_code": "TST",
                        "document_date": "2026-07-01",
                        "publication_date": "2026-07-01T07:00:00Z",
                        "download_link": "NSM/RNS/disclosure-123.html",
                        "latest_flag": "Y",
                    },
                }
            ],
        }
    }


def _esef_entity_response(lei: str) -> dict[str, object]:
    return {
        "data": [
            {
                "type": "entity",
                "id": "1969",
                "attributes": {"name": "ASML Holding N.V.", "identifier": lei},
            }
        ]
    }


def _esef_filing_response(lei: str, report_path: str) -> dict[str, object]:
    return {
        "data": [
            {
                "type": "filing",
                "id": "23718",
                "attributes": {
                    "country": "NL",
                    "period_end": "2025-12-31",
                    "date_added": "2026-03-04 13:55:19",
                    "fxo_id": f"{lei}-2025-12-31-ESEF-NL-0",
                    "report_url": report_path,
                    "error_count": 0,
                    "warning_count": 6,
                },
                "relationships": {"entity": {"data": {"type": "entity", "id": "1969"}}},
            }
        ],
        "included": _esef_entity_response(lei)["data"],
    }
