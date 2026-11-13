from __future__ import annotations

import csv
import io
import zipfile
from datetime import date

import httpx
import pytest

from openfilings.adapters.base import SourceDocument
from openfilings.adapters.edinet import EdinetClient
from openfilings.exceptions import DocumentUnavailableError
from openfilings.extraction.document import extract_document
from openfilings.models import Filing
from openfilings.service import OpenFilingsService
from openfilings.storage.sqlite import SQLiteCache
from openfilings.xbrl import extract_filing_financials


@pytest.mark.asyncio
async def test_company_search_uses_official_cp932_code_archive() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/codelist/Edinetcode.zip")
        return httpx.Response(200, content=_company_archive())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = EdinetClient(client=http)
        by_english = await client.search_companies("sony")
        by_ticker = await client.search_companies("6758")

    assert by_english[0].id == "jp_E01777"
    assert by_english[0].name == "ソニーグループ株式会社"
    assert by_english[0].english_name == "SONY GROUP CORPORATION"
    assert by_ticker[0].ticker == "6758"
    assert by_ticker[0].market == "JP"


@pytest.mark.asyncio
async def test_company_search_does_not_match_across_word_boundaries() -> None:
    """Live, `openfilings resolve PKO` answered with "KSP Kokusai
    Investments, LLC": the keyword matched the "ksp"+"kokusai" junction of a
    whitespace-stripped archive field. A single-word keyword may match inside
    one word of a name, but never across the boundary between two."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_company_archive())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = EdinetClient(client=http)
        assert await client.search_companies("PKO") == []
        kokusai = await client.search_companies("kokusai")

    assert [company.id for company in kokusai] == ["jp_E37641"]


@pytest.mark.asyncio
async def test_document_lists_are_normalized_and_api_key_is_not_persisted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["Subscription-Key"] == "secret-key"
        return httpx.Response(
            200,
            json={
                "metadata": {"status": "200", "message": "OK"},
                "results": [_document_record()],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = EdinetClient("secret-key", client=http, min_request_interval=0)
        filings = await client.list_filings(
            "jp_E01777",
            start_date=date(2026, 6, 25),
            end_date=date(2026, 6, 25),
        )

    filing = filings[0]
    assert filing.id == "jp_edinet_S1000001"
    assert filing.filing_type == "annual"
    assert filing.period_end == date(2026, 3, 31)
    assert filing.xbrl_available is True
    assert "secret-key" not in filing.source_url


@pytest.mark.asyncio
async def test_document_download_detects_json_errors_with_http_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json; charset=utf-8"},
            json={"metadata": {"status": "404", "message": "Not Found"}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = EdinetClient("key", client=http, min_request_interval=0)
        with pytest.raises(DocumentUnavailableError, match="Not Found"):
            await client.download_document("S1000001")


def test_multidocument_edinet_package_produces_markdown_and_financials() -> None:
    archive = _filing_archive()
    source = SourceDocument(
        data=archive,
        media_type="application/zip",
        source_url="https://api.edinet-fsa.go.jp/api/v2/documents/S1000001",
        profile="edinet",
    )
    filing = _filing()

    extracted = extract_document(source)
    financials = extract_filing_financials(source, filing)

    assert "事業の状況" in extracted.markdown
    assert "連結財務諸表" in extracted.markdown
    assert extracted.method == "edinet-zip-html+markdownify"
    codes = {
        item.code
        for statement in financials.statements
        for item in statement.line_items
    }
    assert {"revenue", "operating_income_loss", "total_assets"} <= codes
    assert financials.extraction_method == "inline-xbrl-stream"


@pytest.mark.asyncio
async def test_service_routes_japanese_search_history_and_cached_markdown(
    tmp_path,
) -> None:
    calls: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls[request.url.path] = calls.get(request.url.path, 0) + 1
        if request.url.path.endswith("/codelist/Edinetcode.zip"):
            return httpx.Response(200, content=_company_archive())
        if request.url.path.endswith("/documents.json"):
            return httpx.Response(
                200,
                json={
                    "metadata": {"status": "200", "message": "OK"},
                    "results": [_document_record()],
                },
            )
        if request.url.path.endswith("/documents/S1000001"):
            return httpx.Response(
                200,
                headers={"content-type": "application/octet-stream"},
                content=_filing_archive(),
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        edinet = EdinetClient("key", client=http, min_request_interval=0)
        cache = SQLiteCache(tmp_path / "cache.sqlite3")
        service = OpenFilingsService(cache, edinet_source=edinet)

        companies = await service.search_companies("Sony", source="edinet")
        filings = await service.list_filings(
            companies[0].id,
            source="edinet",
            edinet_lookback_days=1,
        )
        first = await service.get_filing_markdown(filings[0].id)
        second = await service.get_filing_markdown(filings[0].id)
        cache.close()

    assert first.from_cache is False
    assert second.from_cache is True
    assert first.markdown == second.markdown
    assert calls["/api/v2/documents/S1000001"] == 1


def _company_archive() -> bytes:
    rows = [
        ["ダウンロード実行日", "2026年07月22日現在", "件数", "2件"],
        [
            "ＥＤＩＮＥＴコード",
            "提出者種別",
            "上場区分",
            "連結の有無",
            "資本金",
            "決算日",
            "提出者名",
            "提出者名(英字)",
            "提出者名(ヨミ)",
            "所在地",
            "提出者業種",
            "証券コード",
            "提出者法人番号",
        ],
        [
            "E01777",
            "内国法人・組合",
            "上場",
            "有",
            "880365",
            "3月31日",
            "ソニーグループ株式会社",
            "SONY GROUP CORPORATION",
            "ソニーグループ",
            "東京都港区",
            "電気機器",
            "67580",
            "5010401067252",
        ],
        [
            "E37641",
            "外国法人",
            "非上場",
            "無",
            "",
            "12月31日",
            "ＫＳＰ　Ｋｏｋｕｓａｉ　Ｉｎｖｅｓｔｍｅｎｔｓ，ＬＬＣ",  # noqa: RUF001
            "",
            "",
            "4111 East 37th Street North, Wichita, Kansas 67220 U.S.A.",
            "投資業",
            "",
            "",
        ],
    ]
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\r\n").writerows(rows)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("EdinetcodeDlInfo.csv", output.getvalue().encode("cp932"))
    return archive.getvalue()


def _document_record() -> dict[str, object]:
    return {
        "docID": "S1000001",
        "edinetCode": "E01777",
        "secCode": "67580",
        "filerName": "ソニーグループ株式会社",
        "docTypeCode": "120",
        "periodStart": "2025-04-01",
        "periodEnd": "2026-03-31",
        "submitDateTime": f"{date.today().isoformat()} 09:30",
        "docDescription": "有価証券報告書-第109期",
        "withdrawalStatus": "0",
        "disclosureStatus": "0",
        "legalStatus": "1",
        "xbrlFlag": "1",
        "pdfFlag": "1",
        "csvFlag": "1",
    }


def _filing() -> Filing:
    return Filing(
        id="jp_edinet_S1000001",
        company_id="jp_E01777",
        source="edinet",
        source_id="S1000001",
        title="有価証券報告書",
        category="accounts",
        filing_type="annual",
        filing_date=date(2026, 6, 25),
        document_id="S1000001",
        media_type="application/zip",
        issuer_name="ソニーグループ株式会社",
        language="ja",
        xbrl_available=True,
        source_url="https://api.edinet-fsa.go.jp/api/v2/documents/S1000001",
    )


def _filing_archive() -> bytes:
    context = """
    <xbrli:context id="CurrentYearDuration">
      <xbrli:period>
        <xbrli:startDate>2025-04-01</xbrli:startDate>
        <xbrli:endDate>2026-03-31</xbrli:endDate>
      </xbrli:period>
    </xbrli:context>
    <xbrli:context id="CurrentYearInstant">
      <xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period>
    </xbrli:context>
    <xbrli:unit id="JPY"><xbrli:measure>iso4217:JPY</xbrli:measure></xbrli:unit>
    """
    first = f"""
    <html><body>{context}<h1>事業の状況</h1>
      <ix:nonFraction name="jppfs_cor:NetSales" contextRef="CurrentYearDuration"
        unitRef="JPY" decimals="-6">120000000</ix:nonFraction>
      <ix:nonFraction name="jppfs_cor:OperatingIncomeLoss"
        contextRef="CurrentYearDuration" unitRef="JPY" decimals="-6">
        15000000
      </ix:nonFraction>
    </body></html>
    """
    second = f"""
    <html><body>{context}<h1>連結財務諸表</h1>
      <ix:nonFraction name="jppfs_cor:Assets" contextRef="CurrentYearInstant"
        unitRef="JPY" decimals="-6">500000000</ix:nonFraction>
    </body></html>
    """
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("XBRL/PublicDoc/0001.htm", first.encode())
        bundle.writestr("XBRL/PublicDoc/0002.htm", second.encode())
        bundle.writestr("XBRL/AuditDoc/audit.htm", b"<html>audit</html>")
    return archive.getvalue()
