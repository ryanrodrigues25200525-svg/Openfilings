"""Client for country-scoped European Single Electronic Format filings."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx

from openfilings._version import __version__
from openfilings.adapters._common import bounded_request, normalize_text, ranked_matches
from openfilings.adapters.base import SourceDocument
from openfilings.exceptions import DocumentUnavailableError, SourceError
from openfilings.limits import MAX_TAGGED_DOCUMENT_BYTES
from openfilings.models import Company, Filing

API_BASE_URL = "https://filings.xbrl.org/api/"
CONTENT_BASE_URL = "https://filings.xbrl.org/"

_LEI_PATTERN = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")
_FILING_ID_PATTERN = re.compile(r"^\d+$")
_LANGUAGE_PATTERN = re.compile(r"[-_]([a-z]{2})(?:/|\.x?html?$)", re.IGNORECASE)
# Accent-mismatch fallback: how many leading characters to match on, and how
# many candidates to pull back for local ranking. Three characters keeps the
# upstream result set small enough for one page while still surviving a
# diacritic in the fourth position or later.
_FALLBACK_PREFIX = 3
_FALLBACK_PAGE_SIZE = 40


@dataclass(frozen=True, slots=True)
class EsefMarket:
    """Country identity used to scope one reusable ESEF client."""

    country_code: str
    market_code: str
    country_name: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Z]{2}", self.country_code):
            raise ValueError("ESEF country_code must be a two-letter uppercase code.")
        if not re.fullmatch(r"[A-Z]{2}", self.market_code):
            raise ValueError("ESEF market_code must be a two-letter uppercase code.")

    @property
    def id_prefix(self) -> str:
        return self.country_code.casefold()


NETHERLANDS = EsefMarket(
    country_code="NL",
    market_code="NL",
    country_name="Netherlands",
)
FRANCE = EsefMarket(
    country_code="FR",
    market_code="FR",
    country_name="France",
)
SPAIN = EsefMarket(
    country_code="ES",
    market_code="ES",
    country_name="Spain",
)
ITALY = EsefMarket(
    country_code="IT",
    market_code="IT",
    country_name="Italy",
)
DENMARK = EsefMarket(
    country_code="DK",
    market_code="DK",
    country_name="Denmark",
)
SWEDEN = EsefMarket(
    country_code="SE",
    market_code="SE",
    country_name="Sweden",
)
FINLAND = EsefMarket(
    country_code="FI",
    market_code="FI",
    country_name="Finland",
)
NORWAY = EsefMarket(
    country_code="NO",
    market_code="NO",
    country_name="Norway",
)
POLAND = EsefMarket(
    country_code="PL",
    market_code="PL",
    country_name="Poland",
)
BELGIUM = EsefMarket(
    country_code="BE",
    market_code="BE",
    country_name="Belgium",
)
AUSTRIA = EsefMarket(
    country_code="AT",
    market_code="AT",
    country_name="Austria",
)
LUXEMBOURG = EsefMarket(
    country_code="LU",
    market_code="LU",
    country_name="Luxembourg",
)
PORTUGAL = EsefMarket(
    country_code="PT",
    market_code="PT",
    country_name="Portugal",
)
ENABLED_ESEF_MARKETS = (
    NETHERLANDS,
    FRANCE,
    SPAIN,
    ITALY,
    DENMARK,
    SWEDEN,
    FINLAND,
    NORWAY,
    POLAND,
    BELGIUM,
    AUSTRIA,
    LUXEMBOURG,
    PORTUGAL,
)


class EsefClient:
    """Search and download public Inline XBRL reports for one ESEF market."""

    def __init__(
        self,
        market: EsefMarket,
        *,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.market = market
        self._max_retries = max_retries
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": f"openfilings/{__version__}"},
        )

    async def __aenter__(self) -> EsefClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search_companies(self, query: str, *, limit: int = 10) -> list[Company]:
        clean_query = query.strip()
        if not clean_query:
            return []

        lei = self.normalize_lei(clean_query, required=False)
        if lei:
            return await self._entity_query(lei, limit=limit, lei=True)

        # Fetch on the raw ilike and rank locally. Upstream returns matches in
        # registry order, not by relevance, so keeping its first `limit` rows
        # let a short query lose to an unrelated name that merely *contains*
        # it: "ING" resolved to "N.V. Bever Holding" ahead of "ING GROEP N.V."
        candidates = await self._entity_query(
            f"%{clean_query}%", limit=_FALLBACK_PAGE_SIZE, truncate=False
        )
        matches = ranked_matches(
            clean_query,
            [((company.name,), company) for company in candidates],
            limit=limit,
        )
        if matches:
            return matches
        if candidates:
            # Upstream ilike matched but local folding did not (a query of
            # punctuation, for instance): keep its answer over none.
            return candidates[:limit]

        # filings.xbrl.org matches names with a byte-wise SQL ilike, so a
        # query missing a diacritic never matches ("Jeronimo" against
        # "JERONIMO MARTINS SGPS SA" with an accented O). Retry on short
        # accent-free fragments, then rank locally where normalize_text folds
        # the diacritics away. Only runs when the direct match found nothing.
        for pattern in self._fallback_patterns(clean_query):
            candidates = await self._entity_query(
                pattern, limit=_FALLBACK_PAGE_SIZE, truncate=False
            )
            if not candidates:
                continue
            records = [((company.name,), company) for company in candidates]
            matches = ranked_matches(clean_query, records, limit=limit)
            if matches:
                return matches
        return []

    @staticmethod
    def _fallback_patterns(query: str) -> list[str]:
        """Short fragments likely to survive an accent mismatch upstream."""

        first = query.split()[0] if query.split() else query
        normalized = normalize_text(first)
        patterns: list[str] = []
        candidates = [
            first[:_FALLBACK_PREFIX],
            normalized[:_FALLBACK_PREFIX],
            # Skip the first character too: a name whose *leading* letter is
            # the accented one ("Orsted" against "ORSTED A/S" with a slashed
            # O) has no usable accent-free prefix at all.
            normalized[1 : 1 + _FALLBACK_PREFIX],
        ]
        # Diacritics sit on vowels, so the longest consonant run of the query
        # is the one fragment that cannot carry one ("Wartsila" against
        # "WÄRTSILÄ OYJ ABP"). Longest first; ties keep their original order.
        runs = sorted(
            (run for run in re.findall(r"[^aeiouy]+", normalized)),
            key=len,
            reverse=True,
        )
        candidates.extend(runs[:2])
        for candidate in candidates:
            fragment = candidate.strip()
            if len(fragment) >= _FALLBACK_PREFIX and f"%{fragment}%" not in patterns:
                patterns.append(f"%{fragment}%")
        return patterns

    async def _entity_query(
        self, value: str, *, limit: int, lei: bool = False, truncate: bool = True
    ) -> list[Company]:
        filters = [
            {
                "name": "identifier" if lei else "name",
                "op": "eq" if lei else "ilike",
                "val": value,
            },
            {
                "name": "filings.country",
                "op": "eq",
                "val": self.market.country_code,
            },
        ]
        body = await self._get_json(
            "entities",
            params={
                "filter": json.dumps(filters, separators=(",", ":")),
                "page[size]": min(max(limit * 5, 25), 200),
            },
        )
        resources = self._resource_list(body, resource_name="entities")
        companies: list[Company] = []
        seen: set[str] = set()
        for resource in resources:
            company = self._company_from_resource(resource)
            if company is None or company.lei in seen:
                continue
            seen.add(company.lei or company.source_id)
            companies.append(company)
            # Callers that rank locally need every row the page returned;
            # the identifier lookup is unique anyway, so truncation there
            # only bounds a pathological upstream response.
            if truncate and len(companies) >= max(1, limit):
                break
        return companies

    async def list_filings(
        self,
        issuer_id_or_lei: str,
        *,
        limit: int = 25,
        category: str | None = "accounts",
    ) -> list[Filing]:
        if category and category.casefold() != "accounts":
            return []
        lei = self.normalize_lei(issuer_id_or_lei)
        body = await self._get_json(
            f"entities/{lei}/filings",
            params={
                "filter[country]": self.market.country_code,
                "page[size]": min(max(limit, 1), 200),
                "include": "entity",
                "sort": "-period_end",
            },
        )
        return self._filings_from_document(body, fallback_lei=lei)[: max(1, limit)]

    async def get_filing(self, source_id: str) -> Filing:
        filing_id = self.normalize_filing_id(source_id)
        body = await self._get_json(
            f"filings/{filing_id}", params={"include": "entity"}
        )
        resource = self._resource_item(body, resource_name="filing")
        attributes = self._attributes(resource)
        if attributes.get("country") != self.market.country_code:
            raise SourceError(
                f"ESEF filing {filing_id} does not belong to "
                f"{self.market.country_name}."
            )
        filings = self._filings_from_document(
            {"data": [resource], "included": body.get("included", [])}
        )
        if not filings:
            raise SourceError(f"ESEF filing {filing_id} has invalid metadata.")
        return filings[0]

    async def download_document(self, document_path: str) -> SourceDocument:
        source_url = self.document_url(document_path)
        response = await self._request("GET", source_url)
        if not response.content:
            raise DocumentUnavailableError("The ESEF report was empty.")
        if len(response.content) > MAX_TAGGED_DOCUMENT_BYTES:
            raise DocumentUnavailableError("The ESEF report exceeds the 150 MB limit.")

        media_type = response.headers.get("content-type", "").split(";", 1)[0]
        if not media_type or media_type == "application/octet-stream":
            media_type = mimetypes.guess_type(source_url)[0] or media_type
        return SourceDocument(
            data=response.content,
            media_type=media_type or "application/xhtml+xml",
            source_url=source_url,
            profile="esef",
        )

    def matches_company_id(self, value: str) -> bool:
        return value.casefold().startswith(f"{self.market.id_prefix}_lei_")

    def matches_filing_id(self, value: str) -> bool:
        return value.casefold().startswith(f"{self.market.id_prefix}_esef_")

    def normalize_lei(self, value: str, *, required: bool = True) -> str | None:
        clean_value = value.strip()
        prefix = f"{self.market.id_prefix}_lei_"
        if clean_value.casefold().startswith(prefix):
            clean_value = clean_value[len(prefix) :]
        clean_value = clean_value.upper()
        if _LEI_PATTERN.fullmatch(clean_value):
            return clean_value
        if required:
            example = f"{self.market.id_prefix}_lei_{{LEI}}"
            raise SourceError(f"Expected an ESEF company ID shaped like {example}.")
        return None

    def normalize_filing_id(self, value: str) -> str:
        clean_value = value.strip()
        prefix = f"{self.market.id_prefix}_esef_"
        if clean_value.casefold().startswith(prefix):
            clean_value = clean_value[len(prefix) :]
        if not _FILING_ID_PATTERN.fullmatch(clean_value):
            example = f"{self.market.id_prefix}_esef_{{numeric_id}}"
            raise SourceError(f"Expected an ESEF filing ID shaped like {example}.")
        return clean_value

    @staticmethod
    def document_url(document_path: str) -> str:
        clean_path = document_path.strip()
        url = urljoin(CONTENT_BASE_URL, clean_path.lstrip("/"))
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "filings.xbrl.org"
            or not parsed.path.startswith("/")
        ):
            raise DocumentUnavailableError("Unsafe ESEF document path.")
        return url

    def _filings_from_document(
        self, body: dict[str, Any], *, fallback_lei: str | None = None
    ) -> list[Filing]:
        entities = self._included_entities(body)
        filings: list[Filing] = []
        for resource in self._resource_list(body, resource_name="filings"):
            filing = self._filing_from_resource(
                resource, entities=entities, fallback_lei=fallback_lei
            )
            if filing is not None:
                filings.append(filing)
        return filings

    def _filing_from_resource(
        self,
        resource: dict[str, Any],
        *,
        entities: dict[str, tuple[str, str]],
        fallback_lei: str | None,
    ) -> Filing | None:
        source_id = str(resource.get("id") or "")
        attributes = self._attributes(resource)
        if (
            not _FILING_ID_PATTERN.fullmatch(source_id)
            or attributes.get("country") != self.market.country_code
        ):
            return None

        period_end = self._parse_date(attributes.get("period_end"))
        added_at = self._parse_datetime(attributes.get("date_added"))
        document_path = str(
            attributes.get("report_url") or attributes.get("package_url") or ""
        ).strip()
        if period_end is None or added_at is None or not document_path:
            return None

        relationship = resource.get("relationships", {}).get("entity", {})
        entity_id = str(relationship.get("data", {}).get("id") or "")
        issuer_name, relationship_lei = entities.get(entity_id, ("", ""))
        lei = relationship_lei or fallback_lei
        if not lei or not _LEI_PATTERN.fullmatch(lei):
            return None

        fxo_id = str(attributes.get("fxo_id") or "").strip()
        validation = self._validation_description(attributes)
        return Filing(
            id=f"{self.market.id_prefix}_esef_{source_id}",
            company_id=f"{self.market.id_prefix}_lei_{lei}",
            source="esef",
            source_id=source_id,
            title=f"Financial report for the period ended {period_end.isoformat()}",
            category="accounts",
            filing_type="financial_report",
            filing_date=added_at.date(),
            period_end=period_end,
            description=validation,
            document_id=document_path,
            media_type=self._media_type(document_path),
            issuer_name=issuer_name or "Unknown issuer",
            issuer_lei=lei,
            language=self._language(document_path),
            xbrl_available=True,
            source_url=(
                f"{CONTENT_BASE_URL}filing/{quote(fxo_id, safe='')}"
                if fxo_id
                else self.document_url(document_path)
            ),
        )

    def _company_from_resource(self, resource: dict[str, Any]) -> Company | None:
        attributes = self._attributes(resource)
        lei = str(attributes.get("identifier") or "").upper()
        name = str(attributes.get("name") or "").strip()
        if not name or not _LEI_PATTERN.fullmatch(lei):
            return None
        return Company(
            id=f"{self.market.id_prefix}_lei_{lei}",
            source_id=lei,
            name=name,
            sources=("esef",),
            lei=lei,
            market=self.market.market_code,
            country_code=self.market.country_code,
            status="listed issuer",
            company_type="ESEF issuer",
            source_url=f"{CONTENT_BASE_URL}entity/{lei}",
        )

    async def _get_json(
        self, resource: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        response = await self._request(
            "GET", urljoin(API_BASE_URL, resource.lstrip("/")), params=params
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise SourceError("The ESEF API returned invalid JSON.") from exc
        if not isinstance(body, dict):
            raise SourceError("The ESEF API returned an unexpected response.")
        return body

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            try:
                response = await bounded_request(self._client, method, url, **kwargs)
            except httpx.RequestError as exc:
                if attempt >= self._max_retries:
                    raise SourceError(f"ESEF request failed: {exc}") from exc
                await asyncio.sleep(0.25 * (2**attempt))
                continue

            if response.status_code not in {429, 500, 502, 503, 504}:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise SourceError(
                        f"ESEF returned {response.status_code}: "
                        f"{self._error_detail(response)}"
                    ) from exc
                return response
            if attempt >= self._max_retries:
                raise SourceError(
                    f"ESEF returned {response.status_code}: "
                    f"{self._error_detail(response)}"
                )
            await asyncio.sleep(self._retry_delay(response, attempt))
        raise AssertionError("retry loop exited unexpectedly")

    @staticmethod
    def _resource_list(
        body: dict[str, Any], *, resource_name: str
    ) -> list[dict[str, Any]]:
        resources = body.get("data")
        if not isinstance(resources, list) or not all(
            isinstance(resource, dict) for resource in resources
        ):
            raise SourceError(f"The ESEF API returned invalid {resource_name} data.")
        return resources

    @staticmethod
    def _resource_item(body: dict[str, Any], *, resource_name: str) -> dict[str, Any]:
        resource = body.get("data")
        if not isinstance(resource, dict):
            raise SourceError(f"The ESEF API returned invalid {resource_name} data.")
        return resource

    @staticmethod
    def _attributes(resource: dict[str, Any]) -> dict[str, Any]:
        attributes = resource.get("attributes")
        return attributes if isinstance(attributes, dict) else {}

    @classmethod
    def _included_entities(cls, body: dict[str, Any]) -> dict[str, tuple[str, str]]:
        entities: dict[str, tuple[str, str]] = {}
        included = body.get("included", [])
        if not isinstance(included, list):
            return entities
        for resource in included:
            if not isinstance(resource, dict) or resource.get("type") != "entity":
                continue
            attributes = cls._attributes(resource)
            entities[str(resource.get("id") or "")] = (
                str(attributes.get("name") or "").strip(),
                str(attributes.get("identifier") or "").upper(),
            )
        return entities

    @staticmethod
    def _parse_date(value: Any) -> date | None:
        try:
            return date.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    @staticmethod
    def _media_type(document_path: str) -> str:
        return (
            "application/xhtml+xml"
            if document_path.casefold().endswith((".xhtml", ".html", ".htm"))
            else "application/zip"
        )

    @staticmethod
    def _language(document_path: str) -> str:
        matches = _LANGUAGE_PATTERN.findall(document_path)
        return matches[-1].casefold() if matches else "und"

    @staticmethod
    def _validation_description(attributes: dict[str, Any]) -> str:
        errors = int(attributes.get("error_count") or 0)
        warnings = int(attributes.get("warning_count") or 0)
        return f"ESEF validation: {errors} error(s), {warnings} warning(s)."

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after")
        try:
            return min(float(retry_after), 30.0) if retry_after else 0.5 * (2**attempt)
        except ValueError:
            return 0.5 * (2**attempt)

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        try:
            body = response.json()
            errors = body.get("errors", []) if isinstance(body, dict) else []
            if errors and isinstance(errors[0], dict):
                return str(errors[0].get("detail") or errors[0].get("title"))[:300]
        except ValueError:
            pass
        return response.text.strip()[:300] or response.reason_phrase
