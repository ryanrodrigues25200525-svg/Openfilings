"""Read-only client for the FCA National Storage Mechanism public search."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import re
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from openfilings._version import __version__
from openfilings.adapters._common import bounded_request
from openfilings.adapters.base import SourceDocument
from openfilings.exceptions import DocumentUnavailableError, SourceError
from openfilings.models import Company, Filing, IssuerReference

SEARCH_API_URL = "https://api.data.fca.org.uk/search"
SEARCH_INDEX = "nsm-search"
NSM_WEB_URL = "https://data.fca.org.uk/#/nsm/nationalstoragemechanism"
ARTEFACT_BASE_URL = "https://data.fca.org.uk/artefacts/"

_LEI_PATTERN = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")
_MAX_DOCUMENT_BYTES = 100 * 1024 * 1024
# The NSM search API matches a raw name substring, not tokens, and returns
# hits in publication-date order, not relevance order: query "BP" returns a
# first page of 50 fund names containing "gbp" ("IVZ GBP Overnight Return
# SWAP£") before BP P.L.C. appears about 100 hits down (confirmed live).
# Issuer search therefore pages through hits, bounded, until enough
# candidates pass token matching.
_ISSUER_PAGE_SIZE = 50
_MAX_ISSUER_PAGES = 6
# Pooled products (funds, ETFs, structured notes) share an issuer's brand
# but are not the operating company a brand query is looking for, so they
# rank below every candidate without such a token.
_POOLED_PRODUCT_TOKENS = frozenset(
    {
        "etf",
        "etfs",
        "etc",
        "etn",
        "etp",
        "ucits",
        "fund",
        "funds",
        "icav",
        "sicav",
        "swap",
    }
)


class FcaNsmClient:
    """Search and download public NSM disclosures without browser automation."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._max_retries = max_retries
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout_seconds),
            headers={
                "User-Agent": f"openfilings/{__version__}",
                "Origin": "https://data.fca.org.uk",
            },
        )

    async def __aenter__(self) -> FcaNsmClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search_issuers(self, query: str, *, limit: int = 10) -> list[Company]:
        clean_query = query.strip()
        if not clean_query:
            return []

        criteria = [
            self._company_lei_criterion(company=clean_query),
            {"name": "latest_flag", "value": "Y"},
        ]

        # Rank every candidate before cutting to the limit: one entity can
        # surface under several names across hits (the hit's own company
        # field plus its related_org entries), and a broad query can match
        # many distinct entities, so the API's raw hit order must not decide
        # the winner. Paging is bounded and stops as soon as the requested
        # number of candidates has matched.
        by_key: dict[str, tuple[tuple[int, int], int, str, str | None]] = {}
        order = 0
        for page in range(_MAX_ISSUER_PAGES):
            hits = await self._search(
                criteria=criteria,
                size=_ISSUER_PAGE_SIZE,
                offset=page * _ISSUER_PAGE_SIZE,
            )
            if not hits:
                break
            for hit in hits:
                source = hit.get("_source", {})
                for name, lei in self._issuer_pairs(source):
                    rank = self._issuer_match_rank(clean_query, name, lei)
                    if rank is None:
                        continue
                    key = lei or self._normalize_name(name)
                    if not key:
                        continue
                    order += 1
                    current = by_key.get(key)
                    if current is None or (rank, order) < (current[0], current[1]):
                        by_key[key] = (rank, order, name, lei)
            # Enough non-pooled candidates to fill the result set; stop
            # early. A page of pure fund hits must not end the search - the
            # operating company a brand query wants can sit pages below,
            # which is exactly how "HSBC" first returned only ETFs.
            clean = sum(1 for rank, *_ in by_key.values() if rank[0] == 0)
            if clean >= limit:
                break
        best = sorted(by_key.values(), key=lambda item: (item[0], item[1]))
        return [self._company(name, lei) for _, _, name, lei in best[:limit]]

    async def list_filings(
        self,
        issuer_id_or_lei_or_name: str,
        *,
        limit: int = 25,
        type_codes: list[str] | None = None,
    ) -> list[Filing]:
        identifier = issuer_id_or_lei_or_name.strip()
        if not identifier:
            return []

        lei = self.normalize_lei(identifier)
        criteria = [
            self._company_lei_criterion(
                company="" if lei else identifier,
                lei=lei or "",
            ),
            {"name": "latest_flag", "value": "Y"},
        ]
        if type_codes:
            criteria.append(
                {"name": "type_code", "value": [code.lower() for code in type_codes]}
            )

        hits = await self._search(criteria=criteria, size=min(max(limit, 1), 1000))
        company_id = self.company_id(lei=lei, name=identifier)
        return [
            self._filing_from_hit(hit, company_id=company_id) for hit in hits[:limit]
        ]

    async def search_disclosures(
        self,
        keyword: str | None,
        *,
        type_codes: list[str] | None = None,
        limit: int = 25,
    ) -> list[Filing]:
        """Full-text search across every issuer's disclosures, not scoped to
        one company. Matches against the disclosure headline - the NSM
        search API's own top-level "keyword" field is a no-op (confirmed
        live: it doesn't change result counts at all), so this uses a
        "headline" criterion instead, which does filter. Pass keyword=None
        to browse every disclosure of the given type_codes instead."""

        clean_keyword = keyword.strip() if keyword else ""
        if keyword is not None and not clean_keyword:
            return []
        criteria: list[dict[str, Any]] = [{"name": "latest_flag", "value": "Y"}]
        if clean_keyword:
            criteria.append({"name": "headline", "value": clean_keyword})
        if type_codes:
            criteria.append(
                {"name": "type_code", "value": [code.lower() for code in type_codes]}
            )
        hits = await self._search(criteria=criteria, size=min(max(limit, 1), 1000))
        return [self._filing_from_hit(hit) for hit in hits[:limit]]

    async def get_filing(self, disclosure_id: str) -> Filing:
        clean_id = disclosure_id.strip()
        hits = await self._search(
            criteria=[{"name": "disclosure_id", "value": clean_id}],
            size=50,
            sort="hist_seq",
        )
        if not hits:
            raise SourceError(f"FCA NSM disclosure {clean_id} was not found.")

        latest = next(
            (hit for hit in hits if hit.get("_source", {}).get("latest_flag") == "Y"),
            hits[0],
        )
        return self._filing_from_hit(latest)

    async def download_document(self, document_path: str) -> SourceDocument:
        source_url = self.document_url(document_path)
        response = await self._request("GET", source_url)
        if not response.content:
            raise DocumentUnavailableError(
                f"FCA NSM document {document_path} was empty."
            )
        if len(response.content) > _MAX_DOCUMENT_BYTES:
            raise DocumentUnavailableError(
                "FCA NSM document exceeds the "
                f"{_MAX_DOCUMENT_BYTES // 1024 // 1024} MB limit."
            )

        media_type = response.headers.get("content-type", "").split(";", 1)[0]
        if not media_type or media_type == "application/octet-stream":
            media_type = mimetypes.guess_type(source_url)[0] or media_type
        return SourceDocument(
            data=response.content,
            media_type=media_type or "application/octet-stream",
            source_url=source_url,
        )

    async def _search(
        self,
        *,
        criteria: list[dict[str, Any]],
        size: int,
        sort: str = "publication_date",
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        payload = {
            "from": offset,
            "size": size,
            "sort": sort,
            "keyword": None,
            "sortorder": "desc",
            "criteriaObj": {"criteria": criteria, "dateCriteria": None},
        }
        response = await self._request(
            "POST",
            SEARCH_API_URL,
            params={"index": SEARCH_INDEX},
            json=payload,
        )
        try:
            body = response.json()
            return list(body["hits"]["hits"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(
                "FCA NSM returned an unexpected search response."
            ) from exc

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            try:
                response = await bounded_request(self._client, method, url, **kwargs)
            except httpx.RequestError as exc:
                if attempt >= self._max_retries:
                    raise SourceError(f"FCA NSM request failed: {exc}") from exc
                await asyncio.sleep(0.25 * (2**attempt))
                continue

            if response.status_code not in {429, 500, 502, 503, 504}:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    detail = self._error_detail(response)
                    raise SourceError(
                        f"FCA NSM returned {response.status_code}: {detail}"
                    ) from exc
                return response

            if attempt >= self._max_retries:
                raise SourceError(
                    f"FCA NSM returned {response.status_code}: "
                    f"{self._error_detail(response)}"
                )
            await asyncio.sleep(self._retry_delay(response, attempt))

        raise AssertionError("retry loop exited unexpectedly")

    @staticmethod
    def normalize_lei(issuer_id_or_lei: str) -> str | None:
        value = issuer_id_or_lei.strip()
        if value.lower().startswith("uk_lei_"):
            value = value[7:]
        value = value.upper()
        return value if _LEI_PATTERN.fullmatch(value) else None

    @classmethod
    def company_id(cls, *, lei: str | None, name: str) -> str:
        if lei:
            return f"uk_lei_{lei}"
        digest = hashlib.sha256(cls._normalize_name(name).encode()).hexdigest()[:16]
        return f"uk_nsm_issuer_{digest}"

    @staticmethod
    def document_url(document_path: str) -> str:
        clean_path = document_path.strip().lstrip("/")
        url = urljoin(ARTEFACT_BASE_URL, clean_path)
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "data.fca.org.uk"
            or not parsed.path.startswith("/artefacts/NSM/")
        ):
            raise DocumentUnavailableError("Unsafe FCA NSM document path.")
        return url

    @staticmethod
    def _company_lei_criterion(*, company: str = "", lei: str = "") -> dict[str, Any]:
        return {
            "name": "company_lei",
            "value": [company, lei, "disclose_org", "related_org"],
        }

    @classmethod
    def _company(cls, name: str, lei: str | None) -> Company:
        return Company(
            id=cls.company_id(lei=lei, name=name),
            source_id=lei or cls._normalize_name(name),
            name=name,
            sources=("fca_nsm",),
            lei=lei,
            status="listed issuer",
            company_type="issuer",
            source_url=NSM_WEB_URL,
        )

    @classmethod
    def _filing_from_hit(
        cls, hit: dict[str, Any], *, company_id: str | None = None
    ) -> Filing:
        source = hit.get("_source", {})
        disclosure_id = str(source.get("disclosure_id") or hit.get("_id") or "")
        if not disclosure_id:
            raise SourceError("FCA NSM result did not include a disclosure ID.")

        issuer_name = cls._first_value(source.get("company")) or "Unknown issuer"
        issuer_lei = cls._first_value(source.get("lei"), uppercase=True)
        document_path = str(source.get("download_link") or "").strip() or None
        publication = cls._parse_datetime(source.get("publication_date"))
        filing_date = cls._parse_date(source.get("document_date"), fallback=publication)
        related = tuple(
            IssuerReference(
                name=str(item.get("company") or "Unknown issuer").strip(),
                lei=cls._first_value(item.get("lei"), uppercase=True),
            )
            for item in source.get("related_org") or []
            if isinstance(item, dict)
        )
        source_url = cls.document_url(document_path) if document_path else NSM_WEB_URL

        return Filing(
            id=f"uk_nsm_{disclosure_id}",
            company_id=company_id or cls.company_id(lei=issuer_lei, name=issuer_name),
            source="fca_nsm",
            source_id=disclosure_id,
            title=str(source.get("headline") or source.get("type") or "NSM disclosure"),
            category=str(source.get("type") or "regulated information"),
            filing_type=str(source.get("type_code") or "unknown"),
            filing_date=filing_date,
            published_at=publication,
            description=source.get("headline"),
            document_id=document_path,
            media_type=cls._guess_media_type(document_path),
            issuer_name=issuer_name,
            issuer_lei=issuer_lei,
            related_issuers=related,
            source_url=source_url,
        )

    @classmethod
    def _issuer_pairs(cls, source: dict[str, Any]) -> list[tuple[str, str | None]]:
        companies = cls._split_values(source.get("company"))
        leis = cls._split_values(source.get("lei"), uppercase=True)
        pairs = [
            (name, leis[index] if index < len(leis) else None)
            for index, name in enumerate(companies)
        ]
        for related in source.get("related_org") or []:
            if not isinstance(related, dict):
                continue
            name = str(related.get("company") or "").strip()
            if name:
                pairs.append(
                    (name, cls._first_value(related.get("lei"), uppercase=True))
                )
        return pairs

    @classmethod
    def _issuer_match_rank(
        cls, query: str, name: str, lei: str | None
    ) -> tuple[int, int] | None:
        """Match quality for one candidate issuer, or None when it is not a
        match at all. The returned sort key is the pooled-product flag (so a
        fund, ETF or structured note sharing a brand ranks below the
        operating company) and then the number of name tokens the query did
        not consume - the fewer, the better.

        Matching is token-aware, never raw substring: "BP" must not match
        "IVZ GBP Overnight Return SWAP£" merely because those letters sit
        inside "GBP". A query token matches a name token exactly, or - for
        tokens of three or more characters - as its start ("Glaxo" against
        "GLAXOSMITHKLINE PLC"); shorter tokens ("BP"-style tickers) require
        an exact token."""
        clean_query = query.strip().upper()
        if lei and clean_query == lei:
            return (-1, 0)
        query_tokens = cls._normalize_name(query).split()
        name_tokens = cls._normalize_name(name).split()
        if not query_tokens:
            return None
        for token in query_tokens:
            if not any(
                token == candidate or (len(token) >= 3 and candidate.startswith(token))
                for candidate in name_tokens
            ):
                return None
        pooled = int(any(token in _POOLED_PRODUCT_TOKENS for token in name_tokens))
        return (pooled, max(len(name_tokens) - len(query_tokens), 0))

    @staticmethod
    def _normalize_name(value: str) -> str:
        return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())

    @classmethod
    def _split_values(cls, value: Any, *, uppercase: bool = False) -> list[str]:
        if value is None:
            return []
        values = [part.strip() for part in str(value).split(";") if part.strip()]
        return [part.upper() for part in values] if uppercase else values

    @classmethod
    def _first_value(cls, value: Any, *, uppercase: bool = False) -> str | None:
        values = cls._split_values(value, uppercase=uppercase)
        return values[0] if values else None

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    @staticmethod
    def _parse_date(value: Any, *, fallback: datetime | None) -> date:
        if value:
            try:
                return date.fromisoformat(str(value)[:10])
            except ValueError:
                pass
        return fallback.date() if fallback else date.min

    @staticmethod
    def _guess_media_type(document_path: str | None) -> str | None:
        if not document_path:
            return None
        return mimetypes.guess_type(document_path)[0] or "application/octet-stream"

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        try:
            payload = response.json()
            if isinstance(payload, dict):
                return str(payload.get("error") or payload.get("message") or payload)
            return str(payload)
        except ValueError:
            return response.text[:300] or response.reason_phrase

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), 30.0)
            except ValueError:
                try:
                    delay = (
                        parsedate_to_datetime(retry_after).timestamp()
                        - parsedate_to_datetime(response.headers["date"]).timestamp()
                    )
                    return min(max(delay, 0.0), 30.0)
                except (KeyError, TypeError, ValueError):
                    pass
        return 0.25 * (2**attempt)
