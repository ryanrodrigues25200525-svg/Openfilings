"""Official EDINET v2 client for Japanese corporate filings."""

from __future__ import annotations

import asyncio
import csv
import io
import re
import time
import unicodedata
import zipfile
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from openfilings._version import __version__
from openfilings.adapters._common import bounded_request
from openfilings.adapters.base import SourceDocument
from openfilings.exceptions import (
    ConfigurationError,
    DocumentUnavailableError,
    SourceError,
)
from openfilings.limits import MAX_TAGGED_DOCUMENT_BYTES
from openfilings.models import Company, Filing

API_BASE_URL = "https://api.edinet-fsa.go.jp/api/v2"
VIEWER_URL = "https://disclosure2.edinet-fsa.go.jp/"
CODE_LIST_URL = (
    "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
)

_EDINET_CODE = re.compile(r"^E\d{5}$", re.IGNORECASE)
_DOCUMENT_ID = re.compile(r"^S\d{7}$", re.IGNORECASE)
_MAX_ARCHIVE_FILES = 10
_MAX_CODE_LIST_BYTES = 5 * 1024 * 1024
_MAX_CODE_LIST_EXPANDED_BYTES = 20 * 1024 * 1024
_JST = ZoneInfo("Asia/Tokyo")

_DOCUMENT_TYPES: dict[str, tuple[str, str]] = {
    "120": ("accounts", "annual"),
    "130": ("accounts", "annual_amendment"),
    "135": ("confirmation", "confirmation"),
    "136": ("confirmation", "confirmation_amendment"),
    "140": ("accounts", "quarterly"),
    "150": ("accounts", "quarterly_amendment"),
    "160": ("accounts", "semiannual"),
    "170": ("accounts", "semiannual_amendment"),
    "180": ("current", "current_report"),
    "190": ("current", "current_report_amendment"),
    "200": ("governance", "parent_company_status"),
    "210": ("governance", "parent_company_status_amendment"),
    "220": ("capital", "share_buyback"),
    "230": ("capital", "share_buyback_amendment"),
    "235": ("internal_control", "internal_control"),
    "236": ("internal_control", "internal_control_amendment"),
}


def _field_matches(query: str, field: str) -> bool:
    """Whether a normalized query keyword matches a normalized field.

    For a single-word query the keyword may match inside one word of the
    field - "shel" reaching "SHELL" - but never across a word boundary:
    the whole-field substring would let "PKO" match "KSP Kokusai ...",
    whose joined form contains "pko" in the "ksp"+"kokusai" junction.
    """

    if not field:
        return False
    if query in field:
        return True
    if " " in query:
        return False
    return any(query in word for word in field.split(" "))


class EdinetClient:
    """Search EDINET filers and retrieve v2 filing metadata/documents."""

    def __init__(
        self,
        api_key: str = "",
        *,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        min_request_interval: float = 0.25,
        list_concurrency: int = 4,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._max_retries = max_retries
        self._min_request_interval = max(0.0, min_request_interval)
        self._list_concurrency = max(1, min(list_concurrency, 8))
        self._request_lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": f"openfilings/{__version__}"},
        )

    async def __aenter__(self) -> EdinetClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search_companies(self, query: str, *, limit: int = 10) -> list[Company]:
        """Search the official keyless EDINET issuer-code archive."""

        clean_query = query.strip()
        if not clean_query:
            return []
        response = await self._public_request(CODE_LIST_URL)
        if len(response.content) > _MAX_CODE_LIST_BYTES:
            raise SourceError("The EDINET issuer-code archive is unexpectedly large.")
        companies = self.parse_company_archive(response.content)
        normalized = self._normalize_search(clean_query)
        ranked: list[tuple[int, str, Company]] = []
        for company in companies:
            fields = (
                company.source_id,
                company.ticker or "",
                company.name,
                company.english_name or "",
            )
            normalized_fields = tuple(self._normalize_search(value) for value in fields)
            if not normalized or not any(
                _field_matches(normalized, value) for value in normalized_fields
            ):
                continue
            if normalized in normalized_fields[:2]:
                rank = 0
            elif any(value.startswith(normalized) for value in normalized_fields[2:]):
                rank = 1
            else:
                rank = 2
            ranked.append((rank, company.name, company))
        ranked.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in ranked[: max(1, limit)]]

    async def list_filings(
        self,
        edinet_code: str,
        *,
        limit: int = 25,
        category: str | None = "accounts",
        start_date: date | None = None,
        end_date: date | None = None,
        lookback_days: int = 120,
    ) -> list[Filing]:
        """List filings by scanning EDINET's date-based document endpoint."""

        code = self.normalize_edinet_code(edinet_code)
        end = end_date or date.today()
        start = start_date or end - timedelta(days=max(1, lookback_days) - 1)
        if start > end:
            raise SourceError("EDINET history start date must not be after end date.")
        if (end - start).days > 3_660:
            raise SourceError("EDINET history requests are limited to ten years.")

        semaphore = asyncio.Semaphore(self._list_concurrency)

        async def fetch(day: date) -> list[dict[str, Any]]:
            async with semaphore:
                return await self.list_documents(day)

        days = [
            start + timedelta(days=offset) for offset in range((end - start).days + 1)
        ]
        results = await asyncio.gather(*(fetch(day) for day in days))
        by_id: dict[str, Filing] = {}
        for record in (record for result in results for record in result):
            if str(record.get("edinetCode") or "").upper() != code:
                continue
            filing = self._filing_from_record(record)
            if filing is None or (category and filing.category != category):
                continue
            by_id[filing.id] = filing
        filings = sorted(
            by_id.values(),
            key=lambda item: (
                item.published_at or datetime.min.replace(tzinfo=UTC),
                item.id,
            ),
            reverse=True,
        )
        return filings[: max(1, limit)]

    async def list_documents(self, day: date) -> list[dict[str, Any]]:
        response = await self._api_request(
            "GET",
            f"{API_BASE_URL}/documents.json",
            params={"date": day.isoformat(), "type": "2"},
        )
        try:
            body = response.json()
            metadata = body["metadata"]
            if str(metadata.get("status")) != "200":
                raise SourceError(
                    f"EDINET returned {metadata.get('status')}: "
                    f"{metadata.get('message', 'unknown error')}"
                )
            results = body.get("results") or []
            if not isinstance(results, list):
                raise TypeError("results is not a list")
            return [item for item in results if isinstance(item, dict)]
        except SourceError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(
                "EDINET returned an unexpected document-list response."
            ) from exc

    async def download_document(
        self, document_id: str, *, document_type: int = 1
    ) -> SourceDocument:
        doc_id = self.normalize_document_id(document_id)
        if document_type not in {1, 2, 3, 4, 5}:
            raise DocumentUnavailableError("Unsupported EDINET document type.")
        source_url = f"{API_BASE_URL}/documents/{doc_id}"
        response = await self._api_request(
            "GET", source_url, params={"type": str(document_type)}
        )
        media_type = response.headers.get("content-type", "").split(";", 1)[0]
        if media_type == "application/json":
            raise DocumentUnavailableError(self._json_error(response))
        if not response.content:
            raise DocumentUnavailableError(f"EDINET document {doc_id} was empty.")
        if len(response.content) > MAX_TAGGED_DOCUMENT_BYTES:
            limit_mb = MAX_TAGGED_DOCUMENT_BYTES // 1024 // 1024
            raise DocumentUnavailableError(
                f"EDINET document exceeds the {limit_mb} MB limit."
            )
        expected_type = "application/pdf" if document_type == 2 else "application/zip"
        return SourceDocument(
            data=response.content,
            media_type=media_type or expected_type,
            source_url=source_url,
            profile="edinet" if document_type == 1 else None,
        )

    async def _api_request(
        self, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        if not self._api_key:
            raise ConfigurationError(
                "EDINET_API_KEY is required for Japanese filing history and downloads. "
                "Company search works without it."
            )
        params = dict(kwargs.pop("params", {}))
        params["Subscription-Key"] = self._api_key
        return await self._request(method, url, params=params, paced=True, **kwargs)

    async def _public_request(self, url: str) -> httpx.Response:
        return await self._request("GET", url, paced=False)

    async def _request(
        self, method: str, url: str, *, paced: bool, **kwargs: Any
    ) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            if paced:
                await self._pace()
            try:
                response = await bounded_request(self._client, method, url, **kwargs)
            except httpx.RequestError as exc:
                if attempt >= self._max_retries:
                    raise SourceError(f"EDINET request failed: {exc}") from exc
                await asyncio.sleep(0.25 * (2**attempt))
                continue

            if response.status_code not in {429, 500, 502, 503, 504}:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    detail = self._json_error(response)
                    raise SourceError(
                        f"EDINET returned {response.status_code}: {detail}"
                    ) from exc
                return response
            if attempt >= self._max_retries:
                detail = self._json_error(response)
                raise SourceError(f"EDINET returned {response.status_code}: {detail}")
            retry_after = response.headers.get("retry-after")
            try:
                delay = float(retry_after) if retry_after else 0.5 * (2**attempt)
            except ValueError:
                delay = 0.5 * (2**attempt)
            await asyncio.sleep(min(max(delay, 0.0), 30.0))
        raise AssertionError("retry loop exited unexpectedly")

    async def _pace(self) -> None:
        async with self._request_lock:
            now = time.monotonic()
            if self._next_request_at > now:
                await asyncio.sleep(self._next_request_at - now)
            self._next_request_at = time.monotonic() + self._min_request_interval

    @classmethod
    def parse_company_archive(cls, archive_bytes: bytes) -> list[Company]:
        """Parse the bounded CP932 CSV distributed by EDINET."""

        try:
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                members = [
                    member for member in archive.infolist() if not member.is_dir()
                ]
                if not members or len(members) > _MAX_ARCHIVE_FILES:
                    raise SourceError(
                        "The EDINET issuer archive has an invalid file count."
                    )
                csv_members = [
                    member
                    for member in members
                    if member.filename.casefold().endswith(".csv")
                ]
                if len(csv_members) != 1:
                    raise SourceError("The EDINET issuer archive must contain one CSV.")
                member = csv_members[0]
                if member.file_size > _MAX_CODE_LIST_EXPANDED_BYTES:
                    raise SourceError("The EDINET issuer CSV is unexpectedly large.")
                text = archive.read(member).decode("cp932")
        except SourceError:
            raise
        except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
            raise SourceError(
                f"Could not read the EDINET issuer archive: {exc}"
            ) from exc

        rows = csv.reader(io.StringIO(text, newline=""))
        next(rows, None)  # Download date and count.
        next(rows, None)  # Japanese column names.
        companies: list[Company] = []
        for row in rows:
            if len(row) < 13:
                continue
            code = row[0].strip().upper()
            name = row[6].strip()
            if not _EDINET_CODE.fullmatch(code) or not name:
                continue
            ticker = cls._ticker(row[11])
            companies.append(
                Company(
                    id=f"jp_{code}",
                    source_id=code,
                    name=name,
                    sources=("edinet",),
                    market="JP",
                    country_code="JP",
                    ticker=ticker,
                    local_code=code,
                    english_name=row[7].strip() or None,
                    status=row[2].strip() or None,
                    company_type=row[1].strip() or None,
                    address=row[9].strip() or None,
                    source_url=VIEWER_URL,
                )
            )
        return companies

    @staticmethod
    def normalize_edinet_code(value: str) -> str:
        code = value.strip()
        if code.casefold().startswith("jp_"):
            code = code[3:]
        code = code.upper()
        if not _EDINET_CODE.fullmatch(code):
            raise SourceError("Expected a Japanese company ID like jp_E12345.")
        return code

    @staticmethod
    def normalize_document_id(value: str) -> str:
        doc_id = value.strip()
        if doc_id.casefold().startswith("jp_edinet_"):
            doc_id = doc_id[10:]
        doc_id = doc_id.upper()
        if not _DOCUMENT_ID.fullmatch(doc_id):
            raise DocumentUnavailableError(
                "Expected an EDINET filing ID like jp_edinet_S1234567."
            )
        return doc_id

    @classmethod
    def _filing_from_record(cls, record: dict[str, Any]) -> Filing | None:
        doc_id = str(record.get("docID") or "").upper()
        code = str(record.get("edinetCode") or "").upper()
        if not _DOCUMENT_ID.fullmatch(doc_id) or not _EDINET_CODE.fullmatch(code):
            return None
        if str(record.get("withdrawalStatus") or "0") == "2":
            return None
        if str(record.get("disclosureStatus") or "0") == "2":
            return None
        if str(record.get("legalStatus") or "1") == "0":
            return None

        doc_type = str(record.get("docTypeCode") or "unknown")
        category, filing_type = _DOCUMENT_TYPES.get(doc_type, ("other", doc_type))
        published = cls._parse_datetime(record.get("submitDateTime"))
        filed = published.date() if published else date.today()
        title = str(record.get("docDescription") or filing_type).strip()
        return Filing(
            id=f"jp_edinet_{doc_id}",
            company_id=f"jp_{code}",
            source="edinet",
            source_id=doc_id,
            title=title,
            category=category,
            filing_type=filing_type,
            filing_date=filed,
            published_at=published,
            period_start=cls._parse_date(record.get("periodStart")),
            period_end=cls._parse_date(record.get("periodEnd")),
            description=title,
            document_id=doc_id,
            media_type="application/zip",
            issuer_name=str(record.get("filerName") or "Unknown filer").strip(),
            language="ja",
            xbrl_available=str(record.get("xbrlFlag") or "0") == "1",
            pdf_available=str(record.get("pdfFlag") or "0") == "1",
            csv_available=str(record.get("csvFlag") or "0") == "1",
            source_url=f"{API_BASE_URL}/documents/{doc_id}",
        )

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M")
        except ValueError:
            return None
        return parsed.replace(tzinfo=_JST).astimezone(UTC)

    @staticmethod
    def _parse_date(value: Any) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            return None

    @staticmethod
    def _ticker(value: str) -> str | None:
        code = value.strip()
        if not code:
            return None
        return code[:-1] if len(code) == 5 and code.endswith("0") else code

    @staticmethod
    def _normalize_search(value: str) -> str:
        """Fold fullwidth text to ASCII and keep words apart.

        Separators become spaces instead of vanishing: joining a field
        into one word lets a keyword match across a word boundary of
        unrelated words (live, "PKO" matched "KSP Kokusai Investments,
        LLC"), which `resolve PKO` then returned as a company.
        """

        normalized = unicodedata.normalize("NFKC", value).casefold()
        folded = "".join(
            character if character.isalnum() else " " for character in normalized
        )
        return " ".join(folded.split())

    @staticmethod
    def _json_error(response: httpx.Response) -> str:
        try:
            body = response.json()
            metadata = body.get("metadata") if isinstance(body, dict) else None
            if isinstance(metadata, dict):
                return str(metadata.get("message") or metadata.get("status") or body)
            if isinstance(body, dict):
                return str(body.get("message") or body)
            return str(body)
        except ValueError:
            return response.text[:300] or "empty response"
