"""Application service shared by command-line and MCP interfaces."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable, Coroutine
from datetime import UTC, date, datetime, timedelta
from typing import Any, TypeVar, cast

from openfilings.adapters.asx import AsxClient
from openfilings.adapters.base import PublicMarketClient, SourceDocument
from openfilings.adapters.bmv import BmvClient
from openfilings.adapters.cvm import CvmClient
from openfilings.adapters.dart import REPORT_CODE_PERIOD, DartClient
from openfilings.adapters.edinet import EdinetClient
from openfilings.adapters.esef import ENABLED_ESEF_MARKETS, EsefClient
from openfilings.adapters.fca_nsm import FcaNsmClient
from openfilings.adapters.kap import KapClient
from openfilings.adapters.nse import NseClient
from openfilings.adapters.sedar import (
    SedarClient,
    validate_sedar_document_url,
    validate_sedar_pdf,
)
from openfilings.adapters.sfc import CUIF_DATASET_URL, SfcClient
from openfilings.adapters.sgx import SgxClient
from openfilings.adapters.smv import SmvClient
from openfilings.config import Settings
from openfilings.domain import FilingDocument
from openfilings.exceptions import (
    CompanyNotFoundError,
    ConfigurationError,
    DocumentUnavailableError,
    ExtractionError,
    FilingNotFoundError,
    FinancialsUnavailableError,
    SourceError,
)
from openfilings.extraction.document import OcrConverter, extract_document
from openfilings.extraction.html import html_to_markdown
from openfilings.extraction.ocr import ocr_pdf_to_markdown, tesseract_available
from openfilings.extraction.pdf import pdf_to_markdown
from openfilings.insider import extract_nsm_insider_dealings
from openfilings.models import (
    SUPPORTED_SOURCE_NAMES,
    CachePruneResult,
    CacheStats,
    Company,
    CompanyFacts,
    ExtractionQuality,
    Filing,
    FilingContent,
    FilingFinancials,
    FinancialLineItem,
    FinancialStatement,
    FinancialValue,
    HistoricalBackfillResult,
    HistoricalFact,
    InsiderDealing,
    MajorHolderNotification,
    OcrMode,
    ReportingPeriod,
    SourceSelection,
    StatementType,
)
from openfilings.ownership import extract_nsm_major_holder
from openfilings.resources import (
    CompanyResource,
    CompanyResources,
    FilingResource,
    FilingResources,
)
from openfilings.storage.sqlite import SQLiteCache
from openfilings.xbrl import extract_filing_financials
from openfilings.xbrl.cvm_structured import extract_cvm_structured_financials
from openfilings.xbrl.dart_structured import extract_dart_structured_financials
from openfilings.xbrl.extract import extract_nse_integrated_filing_financials
from openfilings.xbrl.kap_structured import extract_kap_structured_financials
from openfilings.xbrl.pdf_statements import extract_pdf_ocr_financials
from openfilings.xbrl.sfc_cuif_structured import extract_sfc_cuif_balance_sheet

_Result = TypeVar("_Result")

# FCA NSM has no generic "category" filter of its own, only disclosure type
# codes. "DSH" (Director/PDMR Shareholding) and "HOL" (Holding(s) in Company)
# are the NSM type codes for MAR Article 19 dealing notifications and DTR5
# major-shareholding notifications, confirmed live against NSM's own search
# results.
_NSM_CATEGORY_TYPE_CODES: dict[str, list[str]] = {
    "accounts": ["ACS"],
    "insider": ["DSH"],
    "major_holdings": ["HOL"],
    "pdmr_dealings": ["DSH"],
    # FCA NSM's own disclosure taxonomy provides stable codes for the
    # closest non-US equivalents of SEC current reports and proxy materials.
    "current_report": ["UPD", "ACQ", "DIS", "TST", "BOA"],
    "proxy": ["RAG", "NOA", "ROM"],
}
_FACTS_STATEMENT_TITLES: dict[StatementType, str] = {
    "income_statement": "Income statement",
    "balance_sheet": "Balance sheet",
    "cash_flow_statement": "Cash flow statement",
    "comprehensive_income": "Statement of comprehensive income",
    "changes_in_equity": "Statement of changes in equity",
}


class OpenFilingsService:
    """Coordinates source retrieval, identity resolution, extraction, and caching."""

    def __init__(
        self,
        cache: SQLiteCache,
        *,
        nsm_source: FcaNsmClient | None = None,
        edinet_source: EdinetClient | None = None,
        esef_sources: tuple[EsefClient, ...] = (),
        cvm_source: CvmClient | None = None,
        sgx_source: SgxClient | None = None,
        dart_source: DartClient | None = None,
        market_sources: tuple[PublicMarketClient, ...] = (),
        converter: Callable[[bytes], str] = pdf_to_markdown,
        html_converter: Callable[[bytes], str] = html_to_markdown,
        ocr_converter: OcrConverter = ocr_pdf_to_markdown,
        ocr_available: Callable[[str], bool] = tesseract_available,
        ocr_mode: OcrMode = "auto",
        ocr_language: str = "eng",
        ocr_dpi: int = 200,
        ocr_max_pages: int = 250,
        ocr_executable: str = "tesseract",
        cache_max_mb: int = 512,
        owns_resources: bool = False,
    ) -> None:
        self._nsm = nsm_source
        self._edinet = edinet_source
        self._esef_sources = esef_sources
        self._cvm = cvm_source
        self._sgx = sgx_source
        self._dart = dart_source
        self._market_sources = market_sources
        self._market_sources_by_name = {
            market_source.source: market_source for market_source in market_sources
        }
        self._cache = cache
        self._pdf_converter = converter
        self._html_converter = html_converter
        self._ocr_converter = ocr_converter
        self._ocr_available = ocr_available
        self._ocr_mode = ocr_mode
        self._ocr_language = ocr_language
        self._ocr_dpi = ocr_dpi
        self._ocr_max_pages = ocr_max_pages
        self._ocr_executable = ocr_executable
        self._cache_max_bytes = cache_max_mb * 1024 * 1024
        self._owns_resources = owns_resources

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> OpenFilingsService:
        settings = settings or Settings.from_env()
        nsm = FcaNsmClient(
            timeout_seconds=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
        )
        edinet = EdinetClient(
            settings.edinet_api_key,
            timeout_seconds=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
        )
        esef_sources = tuple(
            EsefClient(
                market,
                timeout_seconds=settings.request_timeout_seconds,
                max_retries=settings.max_retries,
            )
            for market in ENABLED_ESEF_MARKETS
        )
        cvm = CvmClient(
            timeout_seconds=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
        )
        sgx = SgxClient(
            timeout_seconds=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
        )
        dart = DartClient(
            settings.dart_api_key,
            timeout_seconds=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
        )
        market_sources: tuple[PublicMarketClient, ...] = (
            BmvClient(
                timeout_seconds=settings.request_timeout_seconds,
                max_retries=settings.max_retries,
            ),
            NseClient(
                timeout_seconds=settings.request_timeout_seconds,
                max_retries=settings.max_retries,
            ),
            SedarClient(
                timeout_seconds=settings.request_timeout_seconds,
                max_retries=settings.max_retries,
            ),
            SmvClient(
                timeout_seconds=settings.request_timeout_seconds,
                max_retries=settings.max_retries,
            ),
            SfcClient(
                timeout_seconds=settings.request_timeout_seconds,
                max_retries=settings.max_retries,
            ),
            AsxClient(
                timeout_seconds=settings.request_timeout_seconds,
                max_retries=settings.max_retries,
            ),
            KapClient(
                timeout_seconds=settings.request_timeout_seconds,
                max_retries=settings.max_retries,
            ),
        )
        cache = SQLiteCache(settings.database_path)
        return cls(
            cache,
            nsm_source=nsm,
            edinet_source=edinet,
            esef_sources=esef_sources,
            cvm_source=cvm,
            sgx_source=sgx,
            dart_source=dart,
            market_sources=market_sources,
            ocr_mode=settings.ocr_mode,
            ocr_language=settings.ocr_language,
            ocr_dpi=settings.ocr_dpi,
            ocr_max_pages=settings.ocr_max_pages,
            ocr_executable=settings.ocr_executable,
            cache_max_mb=settings.cache_max_mb,
            owns_resources=True,
        )

    async def __aenter__(self) -> OpenFilingsService:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if not self._owns_resources:
            return
        if self._nsm is not None:
            await self._nsm.aclose()
        if self._edinet is not None:
            await self._edinet.aclose()
        for esef in self._esef_sources:
            await esef.aclose()
        if self._cvm is not None:
            await self._cvm.aclose()
        if self._sgx is not None:
            await self._sgx.aclose()
        if self._dart is not None:
            await self._dart.aclose()
        for market_source in self._market_sources:
            await market_source.aclose()
        self._cache.close()

    async def search_companies(
        self,
        query: str,
        *,
        limit: int = 10,
        source: SourceSelection = "all",
    ) -> list[Company]:
        selection = self._validate_source(source)
        calls: list[Coroutine[Any, Any, list[Company]]] = []
        if selection in {"all", "fca_nsm"}:
            if self._nsm is None:
                if selection == "fca_nsm":
                    raise ConfigurationError("The FCA NSM source is not configured.")
            else:
                calls.append(self._nsm.search_issuers(query, limit=limit))
        if selection in {"all", "edinet"}:
            if self._edinet is None:
                if selection == "edinet":
                    raise ConfigurationError("The EDINET source is not configured.")
            else:
                calls.append(self._edinet.search_companies(query, limit=limit))
        if selection in {"all", "esef"}:
            if not self._esef_sources and selection == "esef":
                raise ConfigurationError("The ESEF source is not configured.")
            calls.extend(
                esef.search_companies(query, limit=limit) for esef in self._esef_sources
            )
        if selection in {"all", "cvm"}:
            if self._cvm is None:
                if selection == "cvm":
                    raise ConfigurationError("The CVM source is not configured.")
            else:
                calls.append(self._cvm.search_companies(query, limit=limit))
        if selection in {"all", "sgx"}:
            if self._sgx is None:
                if selection == "sgx":
                    raise ConfigurationError("The SGX source is not configured.")
            else:
                calls.append(self._sgx.search_companies(query, limit=limit))
        if selection in {"all", "dart"}:
            if self._dart is None:
                if selection == "dart":
                    raise ConfigurationError("The DART source is not configured.")
            else:
                calls.append(self._dart.search_companies(query, limit=limit))
        for source_name, market_source in self._market_sources_by_name.items():
            if selection in {"all", source_name}:
                calls.append(market_source.search_companies(query, limit=limit))

        if selection != "all" and not calls:
            raise ConfigurationError(f"The {selection} source is not configured.")

        results = await self._gather_available(calls)
        companies = self._merge_companies(
            [company for result in results for company in result]
        )[:limit]
        self._cache.put_companies(companies)
        return companies

    async def search_disclosures(
        self,
        keyword: str,
        *,
        limit: int = 25,
        source: SourceSelection = "all",
    ) -> list[Filing]:
        """Full-text search across every issuer's disclosures for one
        source, not scoped to a single company. Only sources whose feed
        supports keyword matching participate - currently FCA NSM (headline
        search) and CVM (the yearly IPE archive covers every issuer
        already, so this filters it by subject/type instead of a new
        endpoint)."""

        selection = self._validate_source(source)
        calls: list[Coroutine[Any, Any, list[Filing]]] = []
        if selection in {"all", "fca_nsm"}:
            if self._nsm is None:
                if selection == "fca_nsm":
                    raise ConfigurationError("The FCA NSM source is not configured.")
            else:
                calls.append(self._nsm.search_disclosures(keyword, limit=limit))
        if selection in {"all", "cvm"}:
            if self._cvm is None:
                if selection == "cvm":
                    raise ConfigurationError("The CVM source is not configured.")
            else:
                calls.append(self._cvm.search_disclosures(keyword, limit=limit))

        if selection not in {"all", "fca_nsm", "cvm"}:
            raise ConfigurationError(
                f"Full-text disclosure search is not supported for {selection}; "
                "only fca_nsm and cvm currently support it."
            )

        results = await self._gather_available(calls)
        filings = self._deduplicate_filings(
            [filing for result in results for filing in result]
        )
        filings.sort(key=self._filing_sort_key, reverse=True)
        return filings[:limit]

    async def company(
        self,
        query: str,
        *,
        source: SourceSelection = "all",
        offline: bool = False,
    ) -> CompanyResource:
        """Find one company and bind it to filing operations."""

        companies = await self.companies(
            query,
            limit=10,
            source=source,
            offline=offline,
        )
        if not companies:
            raise CompanyNotFoundError(f"No company matched {query!r}.")
        return companies.find(query) or companies[0]

    async def companies(
        self,
        query: str,
        *,
        limit: int = 10,
        source: SourceSelection = "all",
        offline: bool = False,
    ) -> CompanyResources:
        """Search companies and bind the results to filing operations."""

        selection = self._validate_source(source)
        if offline:
            records = self._cache.search_companies(query, limit=limit)
            if selection != "all":
                records = [record for record in records if selection in record.sources]
        else:
            records = await self.search_companies(
                query,
                limit=limit,
                source=selection,
            )
        return CompanyResources(records, self)

    async def list_filings(
        self,
        company_id: str,
        *,
        category: str | None = "accounts",
        limit: int = 25,
        source: SourceSelection = "all",
        nsm_type_codes: list[str] | None = None,
        edinet_lookback_days: int = 120,
    ) -> list[Filing]:
        selection = self._validate_source(source)
        calls: list[Coroutine[Any, Any, list[Filing]]] = []
        cached_market_filings: list[Filing] = []

        if selection in {"all", "fca_nsm"}:
            nsm_identifier = self._nsm_identifier(company_id)
            if self._nsm is None:
                if selection == "fca_nsm":
                    raise ConfigurationError("The FCA NSM source is not configured.")
            elif nsm_identifier:
                # FCA NSM has no generic "category" filter of its own, only
                # disclosure type codes - without this, category="accounts"
                # (the default) is silently a no-op and the latest filing
                # can be any announcement type (a director dealing, an
                # admission notice, ...), not a financial statement.
                effective_type_codes = nsm_type_codes
                if effective_type_codes is None:
                    effective_type_codes = _NSM_CATEGORY_TYPE_CODES.get(category or "")
                calls.append(
                    self._nsm.list_filings(
                        nsm_identifier,
                        limit=limit,
                        type_codes=effective_type_codes,
                    )
                )
            elif selection == "fca_nsm":
                raise ConfigurationError(
                    "Search the issuer first so its LEI can be resolved, or pass "
                    "an ID shaped like uk_lei_{LEI}."
                )

        if selection in {"all", "edinet"} and self._is_edinet_company_id(company_id):
            if self._edinet is None:
                raise ConfigurationError("The EDINET source is not configured.")
            calls.append(
                self._list_edinet_filings(
                    company_id,
                    category=category,
                    limit=limit,
                    lookback_days=edinet_lookback_days,
                )
            )
        elif selection == "edinet":
            raise ConfigurationError(
                "Expected a Japanese company ID shaped like jp_E12345."
            )

        if selection in {"all", "esef"} and self._is_esef_company_id(company_id):
            esef = self._esef_for_company_id(company_id)
            if esef is None:
                raise ConfigurationError("The requested ESEF market is not enabled.")
            calls.append(
                esef.list_filings(
                    company_id,
                    category=category,
                    limit=limit,
                )
            )
        elif selection == "esef":
            examples = " or ".join(
                f"{esef.market.id_prefix}_lei_{{LEI}}" for esef in self._esef_sources
            )
            raise ConfigurationError(
                f"Expected an enabled ESEF company ID such as {examples}."
            )

        if selection in {"all", "cvm"} and self._is_cvm_company_id(company_id):
            if self._cvm is None:
                raise ConfigurationError("The CVM source is not configured.")
            calls.append(
                self._cvm.list_filings(
                    company_id,
                    category=category,
                    limit=limit,
                )
            )
        elif selection == "cvm":
            raise ConfigurationError(
                "Expected a Brazilian company ID shaped like br_cvm_{numeric_code}."
            )

        if selection in {"all", "sgx"} and self._is_sgx_company_id(company_id):
            if self._sgx is None:
                raise ConfigurationError("The SGX source is not configured.")
            calls.append(
                self._sgx.list_filings(
                    company_id,
                    category=category,
                    limit=limit,
                )
            )
        elif selection == "sgx":
            raise ConfigurationError(
                "Expected a Singapore company ID shaped like sg_sgx_{IBM_code}."
            )

        if selection in {"all", "dart"} and self._is_dart_company_id(company_id):
            if self._dart is None:
                raise ConfigurationError("The DART source is not configured.")
            calls.append(
                self._dart.list_filings(
                    company_id,
                    category=category,
                    limit=limit,
                )
            )
        elif selection == "dart":
            raise ConfigurationError(
                "Expected a Korean company ID shaped like kr_dart_00126380."
            )

        market_source = self._market_source_for_company(company_id, selection)
        if market_source is not None:
            cached_market_filings = (
                self._cache.list_filings(
                    company_id,
                    source="sedar",
                    category=category,
                    limit=limit,
                )
                if market_source.source == "sedar"
                else []
            )
            if not cached_market_filings:
                calls.append(
                    market_source.list_filings(
                        company_id,
                        category=category,
                        limit=limit,
                    )
                )
        elif selection in self._market_sources_by_name:
            raise ConfigurationError(
                f"The company ID does not belong to the {selection} source."
            )

        results = await self._gather_available(calls)
        combined = [
            *cached_market_filings,
            *(filing for result in results for filing in result),
        ]
        filings = self._deduplicate_filings(combined)
        filings.sort(key=self._filing_sort_key, reverse=True)
        filings = filings[:limit]
        self._cache.put_filings(filings)
        return filings

    async def filings(
        self,
        company_id: str,
        *,
        category: str | None = "accounts",
        limit: int = 25,
        source: SourceSelection = "all",
        nsm_type_codes: list[str] | None = None,
        edinet_lookback_days: int = 120,
        offline: bool = False,
    ) -> FilingResources:
        """Return an immutable, filterable filing collection."""

        selection = self._validate_source(source)
        if offline:
            items = self._cache.list_filings(
                company_id,
                source=None if selection == "all" else selection,
                category=category,
                limit=limit,
            )
        else:
            items = await self.list_filings(
                company_id,
                category=category,
                limit=limit,
                source=selection,
                nsm_type_codes=nsm_type_codes,
                edinet_lookback_days=edinet_lookback_days,
            )
        return FilingResources(items, self)

    async def filing(self, filing_id: str) -> FilingResource:
        """Resolve one filing and bind document and financial operations."""

        filing = self._cache.get_filing(filing_id)
        if filing is None:
            filing = await self._resolve_filing(filing_id)
            self._cache.put_filings([filing])
        return FilingResource(filing, self)

    async def import_sedar_filing(
        self,
        company_id: str,
        *,
        document_url: str,
        title: str,
        filing_date: date,
        period_end: date | None = None,
        filing_type: str = "annual",
        category: str = "accounts",
        document_data: bytes | None = None,
    ) -> FilingResource:
        """Register a user-selected SEDAR+ PDF in the normal filing pipeline."""

        company = self._cache.get_company(company_id)
        if company is None or "sedar" not in company.sources:
            raise CompanyNotFoundError(
                "Search and cache the TSX/TSXV company before importing its filing."
            )
        if not title.strip():
            raise ConfigurationError("The filing title cannot be empty.")
        if not filing_type.strip() or not category.strip():
            raise ConfigurationError("Filing type and category cannot be empty.")

        if document_data is None:
            source = self._market_sources_by_name.get("sedar")
            if source is None:
                raise ConfigurationError("The SEDAR+ source is not configured.")
            document = await source.download_document(document_url)
        else:
            if not document_url.casefold().startswith("file://"):
                document_url = validate_sedar_document_url(document_url)
            document = validate_sedar_pdf(
                document_data,
                source_url=document_url,
            )

        digest = hashlib.sha256(document.data).hexdigest()
        identity = hashlib.sha256(f"{company.id}:{digest}".encode()).hexdigest()
        filing_id = f"ca_sedar_filing_{identity[:24]}"
        filing = Filing(
            id=filing_id,
            company_id=company.id,
            source="sedar",
            source_id=digest,
            title=title.strip(),
            category=category.strip(),
            filing_type=filing_type.strip(),
            filing_date=filing_date,
            period_end=period_end,
            document_id=f"cache:{filing_id}",
            media_type="application/pdf",
            issuer_name=company.name,
            issuer_lei=company.lei,
            pdf_available=True,
            source_url=document.source_url,
        )
        self._cache.put_filings([filing])
        self._cache.put_source_document(filing.id, document)
        self._enforce_cache_limit()
        if self._cache.get_source_document(filing.id) is None:
            self._cache.delete_filing(filing.id)
            raise DocumentUnavailableError(
                "The PDF exceeds the configured cache budget and could not be retained."
            )
        return FilingResource(filing, self)

    async def get_filing_markdown(
        self,
        filing_id: str,
        *,
        refresh: bool = False,
        ocr_mode: OcrMode | None = None,
    ) -> FilingContent:
        if not refresh:
            cached = self._cache.get_content(filing_id)
            if cached is not None:
                return cached

        filing = self._cache.get_filing(filing_id)
        if filing is None:
            filing = await self._resolve_filing(filing_id)
            self._cache.put_filings([filing])

        if not filing.document_id:
            raise DocumentUnavailableError(
                f"Filing {filing.id} does not expose a downloadable document."
            )

        document = await self._download_document(filing)
        digest = hashlib.sha256(document.data).hexdigest()
        duplicate = self._cache.get_content_by_sha256(digest)
        if duplicate is not None and not refresh:
            content = FilingContent(
                filing_id=filing.id,
                markdown=self._add_header(
                    filing,
                    self._markdown_body(duplicate.markdown),
                    quality=duplicate.quality,
                    extraction_method=duplicate.extraction_method,
                ),
                source_url=document.source_url,
                media_type=document.media_type,
                extraction_method=duplicate.extraction_method,
                quality=duplicate.quality,
                sha256=digest,
                from_cache=True,
            )
            self._cache.put_content(content)
            self._enforce_cache_limit()
            return content

        result = await asyncio.to_thread(
            extract_document,
            document,
            pdf_converter=self._pdf_converter,
            html_converter=self._html_converter,
            ocr_converter=self._ocr_converter,
            ocr_available=self._ocr_available,
            ocr_mode=ocr_mode or self._ocr_mode,
            ocr_language=self._ocr_language,
            ocr_dpi=self._ocr_dpi,
            ocr_max_pages=self._ocr_max_pages,
            ocr_executable=self._ocr_executable,
        )
        content = FilingContent(
            filing_id=filing.id,
            markdown=self._add_header(
                filing,
                result.markdown,
                quality=result.quality,
                extraction_method=result.method,
            ),
            source_url=document.source_url,
            media_type=document.media_type,
            extraction_method=result.method,
            quality=result.quality,
            sha256=digest,
        )
        self._cache.put_content(content)
        self._enforce_cache_limit()
        return content

    async def get_filing_document(
        self,
        filing_id: str,
        *,
        refresh: bool = False,
        ocr_mode: OcrMode | None = None,
    ) -> FilingDocument:
        """Return Markdown with EdgarTools-style section navigation and search."""

        content = await self.get_filing_markdown(
            filing_id,
            refresh=refresh,
            ocr_mode=ocr_mode,
        )
        return FilingDocument.from_content(content)

    async def get_filing_financials(
        self,
        filing_id: str,
        *,
        refresh: bool = False,
    ) -> FilingFinancials:
        """Return normalized statements from a tagged or supported PDF filing."""

        if not refresh:
            cached = self._cache.get_financials(filing_id)
            if cached is not None:
                return cached

        filing = self._cache.get_filing(filing_id)
        if filing is None:
            filing = await self._resolve_filing(filing_id)
            self._cache.put_filings([filing])

        if filing.source == "cvm":
            structured = await self._extract_cvm_structured_financials(filing)
            if structured is not None:
                self._cache.put_financials(structured)
                self._enforce_cache_limit()
                return structured

        if filing.source == "dart":
            structured = await self._extract_dart_structured_financials(filing)
            if structured is not None:
                self._cache.put_financials(structured)
                self._enforce_cache_limit()
                return structured

        if filing.source == "nse":
            structured = await self._extract_nse_structured_financials(filing)
            if structured is not None:
                self._cache.put_financials(structured)
                self._enforce_cache_limit()
                return structured

        if filing.source == "kap":
            structured = await self._extract_kap_structured_financials(filing)
            if structured is not None:
                self._cache.put_financials(structured)
                self._enforce_cache_limit()
                return structured

        cuif_balance_sheet = (
            await self._extract_sfc_cuif_balance_sheet(filing)
            if filing.source == "sfc"
            else None
        )

        if not filing.document_id:
            if cuif_balance_sheet is None:
                raise DocumentUnavailableError(
                    f"Filing {filing.id} does not expose a downloadable document."
                )
            financials = self._sfc_cuif_only_financials(filing, cuif_balance_sheet)
            self._cache.put_financials(financials)
            self._enforce_cache_limit()
            return financials

        document = await self._download_document(filing)
        used_cuif_only = False
        try:
            financials = await asyncio.to_thread(
                extract_filing_financials,
                document,
                filing,
            )
        except FinancialsUnavailableError as direct_error:
            try:
                financials = await self._extract_ocr_financials(
                    document,
                    filing,
                    direct_error,
                )
            except FinancialsUnavailableError:
                if cuif_balance_sheet is None:
                    raise
                financials = self._sfc_cuif_only_financials(filing, cuif_balance_sheet)
                used_cuif_only = True

        if cuif_balance_sheet is not None and not used_cuif_only:
            # CUIF's balance-sheet accounts are supervisory stock figures and
            # reconcile exactly; a PDF-derived balance sheet is heuristic, so
            # CUIF replaces it here while the PDF still supplies every other
            # statement family (income statement, cash flow, ...).
            other_statements = tuple(
                statement
                for statement in financials.statements
                if statement.statement_type != "balance_sheet"
            )
            financials = financials.model_copy(
                update={
                    "statements": (*other_statements, cuif_balance_sheet),
                    "extraction_method": (
                        f"{financials.extraction_method}+sfc-cuif-supervisory-data"
                    ),
                }
            )

        self._cache.put_financials(financials)
        self._enforce_cache_limit()
        return financials

    async def get_company_facts(
        self,
        company_id: str,
        *,
        source: SourceSelection = "all",
        periods: int = 8,
        category: str | None = "accounts",
    ) -> CompanyFacts:
        """Merge a company's most recent structured filings into one
        multi-period time series per line item - EdgarTools' ``get_facts()``
        concept. Works for every market that already has structured or
        PDF-derived financials; no adapter changes needed. Later filings are
        fetched first (list_filings already sorts newest-first), so when two
        filings report the same period, the newer filing's value wins."""

        # The latest disclosure is often an RNS, proxy notice, or other
        # non-financial announcement even when a source labels it broadly as
        # an account-related filing. Scan a modest bounded window instead of
        # treating ``periods`` as a raw filing count, then stop once enough
        # reports with structured financials have been found.
        scan_limit = min(max(periods * 5, 10), 50)
        filings = await self.list_filings(
            company_id, category=category, limit=scan_limit, source=source
        )
        names: dict[tuple[StatementType, str], tuple[str, str]] = {}
        values: dict[tuple[StatementType, str], dict[str, FinancialValue]] = {}
        currencies: dict[StatementType, str | None] = {}
        filing_ids: list[str] = []
        for filing in filings:
            try:
                financials = await self.get_filing_financials(filing.id)
            except (
                FinancialsUnavailableError,
                DocumentUnavailableError,
                ExtractionError,
                SourceError,
            ):
                continue
            filing_ids.append(filing.id)
            for statement in financials.statements:
                currencies.setdefault(statement.statement_type, statement.currency)
                for item in statement.line_items:
                    key = (statement.statement_type, item.code)
                    names.setdefault(key, (item.name, item.concept))
                    bucket = values.setdefault(key, {})
                    for value in item.values:
                        bucket.setdefault(value.period.label, value)
            if len(filing_ids) >= periods:
                break

        statement_types = {statement_type for statement_type, _ in names}
        statements = tuple(
            FinancialStatement(
                statement_type=statement_type,
                title=_FACTS_STATEMENT_TITLES[statement_type],
                currency=currencies.get(statement_type),
                line_items=tuple(
                    FinancialLineItem(
                        code=code,
                        name=name,
                        concept=concept,
                        values=tuple(
                            sorted(
                                values[(candidate_type, code)].values(),
                                key=lambda value: value.period.end_date,
                            )
                        ),
                    )
                    for (candidate_type, code), (name, concept) in names.items()
                    if candidate_type == statement_type
                ),
            )
            for statement_type in statement_types
        )
        if not statements:
            raise FinancialsUnavailableError(
                f"No structured financials found across the {len(filings)} most "
                f"recent filings for {company_id}."
            )
        return CompanyFacts(
            company_id=company_id,
            statements=statements,
            filing_ids=tuple(filing_ids),
        )

    async def backfill_company_history(
        self,
        company_id: str,
        *,
        source: SourceSelection,
        limit: int = 100,
    ) -> HistoricalBackfillResult:
        """Persist available structured UK/ESEF filing history locally.

        Source archives vary in pagination and retention. This bounded job is
        resumable: rerunning it only inserts facts not already stored.
        """

        if source not in {"fca_nsm", "esef"}:
            raise ConfigurationError(
                "Historical backfill currently supports fca_nsm and esef only."
            )
        filings = await self.list_filings(
            company_id, source=source, category="accounts", limit=limit
        )
        processed = stored = 0
        failures: list[str] = []
        for filing in filings:
            try:
                financials = await self.get_filing_financials(filing.id)
            except (
                FinancialsUnavailableError,
                DocumentUnavailableError,
                ExtractionError,
                SourceError,
            ) as exc:
                failures.append(f"{filing.id}: {type(exc).__name__}")
                continue
            processed += 1
            stored += self._cache.put_historical_facts(filing, financials)
        return HistoricalBackfillResult(
            company_id=company_id,
            source=source,
            discovered_filings=len(filings),
            processed_filings=processed,
            stored_facts=stored,
            failures=tuple(failures),
        )

    def historical_facts(
        self,
        company_id: str,
        *,
        codes: list[str] | None = None,
        view: str = "latest_restated",
        as_of: date | None = None,
        limit: int = 10_000,
    ) -> list[HistoricalFact]:
        """Query locally persisted filing-scoped facts without network access."""

        return self._cache.historical_facts(
            company_id,
            codes=set(codes) if codes else None,
            view=view,
            as_of=as_of,
            limit=limit,
        )

    async def get_major_holder(self, filing_id: str) -> MajorHolderNotification:
        """Parse one UK TR-1 major-shareholding filing into structured
        fields (holder name, position, dates) instead of a bare filing
        pointer. Only FCA NSM's ``category="major_holdings"`` filings are
        supported - other sources return their major-holdings disclosures
        as plain ``Filing`` objects today."""

        filing = self._cache.get_filing(filing_id)
        if filing is None:
            filing = await self._resolve_filing(filing_id)
        if filing.source != "fca_nsm" or filing.filing_type != "HOL":
            raise ExtractionError(
                "Structured major-holder extraction is only available for "
                'FCA NSM category="major_holdings" filings.'
            )
        document = await self._download_document(filing)
        html = document.data.decode("utf-8", errors="replace")
        holder = extract_nsm_major_holder(html, filing)
        if holder is None:
            raise ExtractionError(
                f"Could not parse a TR-1 major-holder notification from {filing_id}."
            )
        return holder

    async def get_insider_dealings(self, filing_id: str) -> list[InsiderDealing]:
        """Parse one FCA DSH filing's standard UK MAR notification tables."""

        filing = self._cache.get_filing(filing_id)
        if filing is None:
            filing = await self._resolve_filing(filing_id)
        if filing.source != "fca_nsm" or filing.filing_type != "DSH":
            raise ExtractionError(
                "Structured insider-dealing extraction is only available for "
                'FCA NSM category="pdmr_dealings" filings.'
            )
        document = await self._download_document(filing)
        html = document.data.decode("utf-8", errors="replace")
        dealings = extract_nsm_insider_dealings(html, filing)
        if not dealings:
            raise ExtractionError(
                f"Could not parse a UK MAR PDMR notification from {filing_id}."
            )
        return dealings

    async def list_insider_dealings(
        self,
        company_id: str,
        *,
        limit: int = 25,
    ) -> list[InsiderDealing]:
        """List structured UK MAR PDMR/PCA transaction notifications."""

        filings = await self.list_filings(
            company_id,
            category="pdmr_dealings",
            limit=limit,
            source="fca_nsm",
        )
        dealings: list[InsiderDealing] = []
        for filing in filings:
            try:
                dealings.extend(await self.get_insider_dealings(filing.id))
            except (ExtractionError, DocumentUnavailableError, SourceError):
                continue
            if len(dealings) >= limit:
                break
        return dealings[:limit]

    async def list_major_holders(
        self, company_id: str, *, limit: int = 25
    ) -> list[MajorHolderNotification]:
        """List structured major-shareholding disclosures or positions."""

        if self._is_cvm_company_id(company_id):
            if self._cvm is None:
                raise ConfigurationError("The CVM source is not configured.")
            return await self._cvm.list_major_holders(company_id, limit=limit)
        if self._nsm_identifier(company_id) is None:
            raise ConfigurationError(
                "Expected a UK company ID shaped like uk_lei_{LEI} or a Brazilian "
                "company ID shaped like br_cvm_{numeric_code}."
            )
        filings = await self.list_filings(
            company_id, category="major_holdings", limit=limit, source="fca_nsm"
        )
        holders: list[MajorHolderNotification] = []
        for filing in filings:
            try:
                holders.append(await self.get_major_holder(filing.id))
            except (ExtractionError, DocumentUnavailableError, SourceError):
                continue
        return holders

    async def search_major_holders(
        self,
        holder_name: str,
        *,
        scan_limit: int = 200,
        limit: int = 25,
    ) -> list[MajorHolderNotification]:
        """Search holder positions across FCA NSM and CVM public disclosures."""

        clean_name = self._normalize_holder_name(holder_name)
        if not clean_name:
            return []
        matches: list[MajorHolderNotification] = []
        if self._nsm is not None:
            filings = await self._nsm.search_disclosures(
                None, type_codes=["HOL"], limit=scan_limit
            )
            for filing in filings:
                try:
                    holder = await self.get_major_holder(filing.id)
                except (ExtractionError, DocumentUnavailableError, SourceError):
                    continue
                if clean_name in self._normalize_holder_name(holder.holder_name):
                    matches.append(holder)
        if self._cvm is not None:
            matches.extend(
                await self._cvm.search_major_holders(holder_name, limit=limit)
            )
        if self._nsm is None and self._cvm is None:
            raise ConfigurationError(
                "Neither the FCA NSM nor CVM source is configured."
            )
        matches.sort(
            key=lambda holder: (
                holder.position_date
                or holder.date_notified
                or holder.date_crossed
                or date.min,
                holder.filing_id,
            ),
            reverse=True,
        )
        return matches[:limit]

    @staticmethod
    def _normalize_holder_name(value: str) -> str:
        return " ".join(value.casefold().split())

    async def _extract_sfc_cuif_balance_sheet(
        self, filing: Filing
    ) -> FinancialStatement | None:
        """Prefer SFC's CUIF supervisory dataset (datos.gov.co) for the
        balance sheet over heuristic PDF parsing - its assets/liabilities/
        equity accounts are stock figures that reconcile exactly. Falls back
        to None (letting the PDF path supply the balance sheet, as before)
        if the entity or cut-off date isn't covered."""

        sfc = self._market_sources_by_name.get("sfc")
        if not isinstance(sfc, SfcClient) or filing.period_end is None:
            return None
        match = re.fullmatch(r"co_sfc_(\d{1,4})_(\d{1,6})", filing.company_id, re.I)
        if match is None:
            return None
        try:
            rows = await sfc.cuif_balance_sheet_rows(
                match.group(1), match.group(2), filing.period_end
            )
        except SourceError:
            return None
        if rows is None:
            return None
        return extract_sfc_cuif_balance_sheet(rows, filing)

    @staticmethod
    def _sfc_cuif_only_financials(
        filing: Filing, balance_sheet: FinancialStatement
    ) -> FilingFinancials:
        return FilingFinancials(
            filing_id=filing.id,
            company_id=filing.company_id,
            source_url=CUIF_DATASET_URL,
            extraction_method="sfc-cuif-supervisory-data",
            statements=(balance_sheet,),
            fact_count=len(balance_sheet.line_items),
            taxonomy_namespaces=("sfc-cuif",),
            sha256=hashlib.sha256(
                f"{filing.id}:{filing.period_end}".encode()
            ).hexdigest(),
        )

    async def _extract_cvm_structured_financials(
        self, filing: Filing
    ) -> FilingFinancials | None:
        """Prefer CVM's Open Data DFP/ITR dataset over the PDF filing - it's
        already tagged to a standardized chart of accounts, so it doesn't
        need heuristic PDF parsing at all. Falls back to None (letting the
        caller use the PDF path) if the company or year isn't covered."""

        if self._cvm is None or filing.period_end is None:
            return None
        match = re.fullmatch(r"br_cvm_(\d{1,6})", filing.company_id, re.IGNORECASE)
        if match is None:
            return None
        cd_cvm = match.group(1).zfill(6)
        dataset = "itr" if filing.filing_type == "interim" else "dfp"
        try:
            archive = await self._cvm.structured_archive(
                dataset, filing.period_end.year
            )
        except SourceError:
            return None
        if archive is None:
            return None
        digest = hashlib.sha256(archive).hexdigest()
        try:
            return await asyncio.to_thread(
                extract_cvm_structured_financials,
                archive,
                filing,
                cd_cvm=cd_cvm,
                source_url=CvmClient.structured_archive_url(
                    dataset, filing.period_end.year
                ),
                sha256=digest,
            )
        except FinancialsUnavailableError:
            return None

    async def _extract_nse_structured_financials(
        self, filing: Filing
    ) -> FilingFinancials | None:
        """Prefer NSE's Integrated Filing - Financials XBRL over the annual-
        report PDF - since April 2025 it's the only format SEBI Regulation
        33 financial results are filed in (PDF submission was
        discontinued), and its taxonomy already matches the standard IFRS
        concepts this codebase recognizes elsewhere. Falls back to None
        (letting the caller use the PDF annual report) if no audited filing
        covers this exact fiscal-year-end."""

        nse = self._market_sources_by_name.get("nse")
        if not isinstance(nse, NseClient) or filing.period_end is None:
            return None
        match = re.fullmatch(r"in_nse_([A-Za-z0-9&-]{1,30})", filing.company_id, re.I)
        if match is None:
            return None
        try:
            result = await nse.integrated_filing_xbrl(match.group(1), filing.period_end)
        except SourceError:
            return None
        if result is None:
            return None
        data, source_url = result
        try:
            return await asyncio.to_thread(
                extract_nse_integrated_filing_financials,
                data,
                filing,
                source_url=source_url,
                sha256=hashlib.sha256(data).hexdigest(),
            )
        except FinancialsUnavailableError:
            return None

    async def _extract_dart_structured_financials(
        self, filing: Filing
    ) -> FilingFinancials | None:
        """Prefer DART's fnlttSinglAcntAll.json over the filing's zipped
        report package - it's already tagged to IFRS-XBRL standard account
        IDs, so it doesn't need any DART-specific parsing at all. Falls back
        to None (letting the caller use the downloaded document) if the
        company or period isn't covered, or if it has no recognized IFRS
        accounts."""

        if self._dart is None or filing.period_end is None:
            return None
        match = re.fullmatch(r"kr_dart_(\d{8})", filing.company_id, re.IGNORECASE)
        if match is None:
            return None
        corp_code = match.group(1)
        reprt_code = next(
            (
                code
                for code, (_, month, day) in REPORT_CODE_PERIOD.items()
                if (month, day) == (filing.period_end.month, filing.period_end.day)
            ),
            None,
        )
        if reprt_code is None:
            return None
        bsns_year = str(filing.period_end.year)
        rows = await self._dart.financial_statements(
            corp_code, bsns_year=bsns_year, reprt_code=reprt_code, fs_div="CFS"
        )
        if not rows:
            rows = await self._dart.financial_statements(
                corp_code, bsns_year=bsns_year, reprt_code=reprt_code, fs_div="OFS"
            )
        if not rows:
            return None
        fiscal_period, _, _ = REPORT_CODE_PERIOD[reprt_code]
        period = ReportingPeriod(
            id=f"dart-{fiscal_period.casefold()}-{filing.period_end.isoformat()}",
            start_date=date(filing.period_end.year, 1, 1),
            end_date=filing.period_end,
            kind="duration",
            fiscal_period=fiscal_period,
        )
        source_url = (
            "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json?"
            f"corp_code={corp_code}&bsns_year={bsns_year}&reprt_code={reprt_code}"
        )
        try:
            return extract_dart_structured_financials(
                rows,
                filing,
                period=period,
                source_url=source_url,
                sha256=hashlib.sha256(
                    f"{filing.id}:{bsns_year}:{reprt_code}".encode()
                ).hexdigest(),
            )
        except FinancialsUnavailableError:
            return None

    async def _extract_kap_structured_financials(
        self, filing: Filing
    ) -> FilingFinancials | None:
        """Prefer KAP's rendered "Finansal Rapor" viewer tables over the
        filing's PDF attachment - they carry the filer's literal IFRS-tagged
        concepts, so they don't need heuristic PDF parsing at all. Falls
        back to None (letting the caller use the PDF) if the disclosure
        isn't a financial report or has no recognized IFRS facts."""

        kap = self._market_sources_by_name.get("kap")
        if not isinstance(kap, KapClient) or filing.filing_type != "financial_report":
            return None
        bodies = await kap.financial_report_bodies(filing.source_id)
        if not bodies:
            return None
        source_url = filing.source_url
        try:
            return extract_kap_structured_financials(
                bodies,
                filing,
                source_url=source_url,
                sha256=hashlib.sha256(
                    f"{filing.id}:{len(bodies)}".encode()
                ).hexdigest(),
            )
        except FinancialsUnavailableError:
            return None

    async def _extract_ocr_financials(
        self,
        document: SourceDocument,
        filing: Filing,
        direct_error: FinancialsUnavailableError,
    ) -> FilingFinancials:
        is_pdf = (
            document.media_type.casefold() == "application/pdf"
            or document.data[:4] == b"%PDF"
        )
        if not is_pdf or self._ocr_mode == "never":
            raise direct_error
        if not self._ocr_available(self._ocr_executable):
            raise FinancialsUnavailableError(
                f"{direct_error} Install Tesseract or set OPENFILINGS_OCR_MODE=never."
            ) from direct_error
        try:
            markdown = await asyncio.to_thread(
                self._ocr_converter,
                document.data,
                language=self._ocr_language,
                dpi=self._ocr_dpi,
                max_pages=self._ocr_max_pages,
                executable=self._ocr_executable,
            )
        except ExtractionError as exc:
            raise FinancialsUnavailableError(
                f"The filing could not be OCR-processed for financial tables: {exc}"
            ) from exc
        digest = hashlib.sha256(document.data).hexdigest()
        return await asyncio.to_thread(
            extract_pdf_ocr_financials,
            markdown,
            filing,
            source_url=document.source_url,
            sha256=digest,
        )

    def cache_stats(self) -> CacheStats:
        return self._cache.stats()

    def prune_cache(self, *, max_mb: int) -> CachePruneResult:
        if max_mb < 0:
            raise ConfigurationError("Cache size cannot be negative.")
        before = self._cache.stats()
        max_bytes = max_mb * 1024 * 1024
        removed_sources = self._cache.prune_source_documents(
            max(
                0,
                max_bytes
                - before.compressed_content_bytes
                - before.compressed_financial_bytes,
            )
        )
        interim = self._cache.stats()
        removed = self._cache.prune_content(
            max(
                0,
                max_bytes
                - interim.compressed_source_bytes
                - interim.compressed_financial_bytes,
            )
        )
        interim = self._cache.stats()
        removed_financials = self._cache.prune_financials(
            max(
                0,
                max_bytes
                - interim.compressed_source_bytes
                - interim.compressed_content_bytes,
            )
        )
        self._cache.vacuum()
        return CachePruneResult(
            removed_documents=removed,
            removed_source_documents=removed_sources,
            removed_financial_reports=removed_financials,
            before=before,
            after=self._cache.stats(),
        )

    async def _resolve_filing(self, filing_id: str) -> Filing:
        if filing_id.casefold().startswith("jp_edinet_"):
            raise FilingNotFoundError(
                "EDINET filing metadata is not cached. List the Japanese company's "
                "filings before fetching the document."
            )
        if filing_id.lower().startswith("uk_nsm_"):
            if self._nsm is None:
                raise ConfigurationError("The FCA NSM source is not configured.")
            disclosure_id = filing_id[len("uk_nsm_") :]
            if not disclosure_id:
                raise FilingNotFoundError(
                    "Expected an FCA filing ID shaped like uk_nsm_{disclosure_id}."
                )
            return await self._nsm.get_filing(disclosure_id)
        esef = self._esef_for_filing_id(filing_id)
        if esef is not None:
            return await esef.get_filing(filing_id)
        if filing_id.casefold().startswith("br_cvm_"):
            raise FilingNotFoundError(
                "CVM filing metadata is not cached. List the Brazilian company's "
                "filings before fetching the document."
            )
        if filing_id.casefold().startswith("sg_sgx_"):
            raise FilingNotFoundError(
                "SGX filing metadata is not cached. List the Singapore company's "
                "filings before fetching the document."
            )
        if filing_id.casefold().startswith("kr_dart_filing_"):
            raise FilingNotFoundError(
                "DART filing metadata is not cached. List the Korean company's "
                "filings before fetching the document."
            )
        market_source = next(
            (
                source
                for source in self._market_sources
                if source.matches_filing_id(filing_id)
            ),
            None,
        )
        if market_source is not None:
            raise FilingNotFoundError(
                f"{market_source.source.upper()} filing metadata is not cached. "
                "List the company's filings before fetching the document."
            )

        raise FilingNotFoundError(f"Unsupported filing ID: {filing_id}.")

    async def _download_document(self, filing: Filing) -> SourceDocument:
        cached = self._cache.get_source_document(filing.id)
        if cached is not None:
            return cached
        if filing.source == "edinet":
            if self._edinet is None:
                raise ConfigurationError("The EDINET source is not configured.")
            return await self._edinet.download_document(filing.document_id or "")
        if filing.source == "fca_nsm":
            if self._nsm is None:
                raise ConfigurationError("The FCA NSM source is not configured.")
            return await self._nsm.download_document(filing.document_id or "")
        if filing.source == "esef":
            esef = self._esef_for_company_id(filing.company_id)
            if esef is None:
                raise ConfigurationError("The filing's ESEF market is not enabled.")
            return await esef.download_document(filing.document_id or "")
        if filing.source == "cvm":
            if self._cvm is None:
                raise ConfigurationError("The CVM source is not configured.")
            return await self._cvm.download_document(filing.document_id or "")
        if filing.source == "sgx":
            if self._sgx is None:
                raise ConfigurationError("The SGX source is not configured.")
            return await self._sgx.download_document(filing.document_id or "")
        if filing.source == "dart":
            if self._dart is None:
                raise ConfigurationError("The DART source is not configured.")
            return await self._dart.download_document(filing.document_id or "")
        market_source = self._market_sources_by_name.get(filing.source)
        if market_source is not None:
            return await market_source.download_document(filing.document_id or "")
        raise DocumentUnavailableError(f"Unsupported filing source: {filing.source}.")

    async def _list_edinet_filings(
        self,
        company_id: str,
        *,
        category: str | None,
        limit: int,
        lookback_days: int,
    ) -> list[Filing]:
        if self._edinet is None:
            raise ConfigurationError("The EDINET source is not configured.")
        if not 1 <= lookback_days <= 3_661:
            raise ConfigurationError("EDINET history days must be between 1 and 3661.")
        code = self._edinet.normalize_edinet_code(company_id)
        state_key = f"edinet-history:{code}:{category or 'all'}:{lookback_days}:{limit}"
        state = self._cache.get_market_state(state_key)
        if state:
            try:
                cached_at = datetime.fromisoformat(state)
            except ValueError:
                cached_at = datetime.min.replace(tzinfo=UTC)
            if datetime.now(UTC) - cached_at < timedelta(hours=6):
                return self._cache.list_filings(
                    company_id,
                    source="edinet",
                    category=category,
                    limit=limit,
                )

        filings = await self._edinet.list_filings(
            code,
            category=category,
            limit=limit,
            lookback_days=lookback_days,
        )
        self._cache.put_filings(filings)
        self._cache.put_market_state(state_key, datetime.now(UTC).isoformat())
        return filings

    def _nsm_identifier(self, company_id: str) -> str | None:
        if company_id.lower().startswith("uk_lei_"):
            return company_id
        company = self._cache.get_company(company_id)
        if company is None:
            return None
        if company.market != "UK":
            return None
        if company.lei:
            return company.lei
        return company.name if "fca_nsm" in company.sources else None

    def _enforce_cache_limit(self) -> None:
        stats = self._cache.stats()
        self._cache.prune_source_documents(
            max(
                0,
                self._cache_max_bytes
                - stats.compressed_content_bytes
                - stats.compressed_financial_bytes,
            )
        )
        stats = self._cache.stats()
        self._cache.prune_content(
            max(
                0,
                self._cache_max_bytes
                - stats.compressed_source_bytes
                - stats.compressed_financial_bytes,
            )
        )
        stats = self._cache.stats()
        self._cache.prune_financials(
            max(
                0,
                self._cache_max_bytes
                - stats.compressed_source_bytes
                - stats.compressed_content_bytes,
            )
        )
        # SQLite retains freed pages and WAL bytes until explicitly compacted;
        # payload-only limits are not a meaningful disk limit for a long-lived
        # research cache.
        if self._cache.stats().database_bytes > self._cache_max_bytes:
            self._cache.enforce_database_limit(self._cache_max_bytes)

    @staticmethod
    async def _gather_available(
        calls: list[Coroutine[Any, Any, list[_Result]]],
    ) -> list[list[_Result]]:
        if not calls:
            return []
        results = await asyncio.gather(*calls, return_exceptions=True)
        successes = [result for result in results if isinstance(result, list)]
        if successes:
            return successes
        first_error = next(
            (result for result in results if isinstance(result, Exception)), None
        )
        if first_error:
            raise first_error
        return []

    @classmethod
    def _merge_companies(cls, companies: list[Company]) -> list[Company]:
        merged: dict[str, Company] = {}
        order: list[str] = []
        for company in companies:
            key = cls._company_key(company.market, company.name)
            existing = merged.get(key)
            if existing is None:
                merged[key] = company
                order.append(key)
                continue
            sources = tuple(dict.fromkeys((*existing.sources, *company.sources)))
            merged[key] = existing.model_copy(
                update={
                    "sources": sources,
                    "lei": existing.lei or company.lei,
                }
            )
        return [merged[key] for key in order]

    @classmethod
    def _deduplicate_filings(cls, filings: list[Filing]) -> list[Filing]:
        seen: set[tuple[str, ...]] = set()
        unique: list[Filing] = []
        for filing in filings:
            url_key = ("url", filing.source_url.casefold())
            metadata_key = (
                "metadata",
                filing.filing_date.isoformat(),
                cls._normalize_text(filing.title),
            )
            if url_key in seen or metadata_key in seen:
                continue
            seen.update((url_key, metadata_key))
            unique.append(filing)
        return unique

    @classmethod
    def _company_key(cls, market: str, name: str) -> str:
        if market == "JP":
            normalized = "".join(
                character for character in name.casefold() if character.isalnum()
            )
            return f"jp:{normalized}"
        normalized = cls._normalize_text(name)
        suffixes = (" public limited company", " limited", " ltd", " plc")
        for suffix in suffixes:
            if normalized.endswith(suffix):
                normalized = normalized[: -len(suffix)].strip()
                break
        return f"{market.casefold()}:{normalized}"

    @staticmethod
    def _normalize_text(value: str) -> str:
        return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())

    @classmethod
    def _filing_sort_key(cls, filing: Filing) -> tuple[datetime, date, str]:
        # Same-day filings (e.g. ESEF re-filings sharing date_added) broke ties
        # on raw filing id, which can predate the newer period (issue #14:
        # it_esef_7112 from 2021 outranked it_esef_6868 from 2022). Prefer the
        # newer reporting period before falling back to the id.
        return (
            filing.published_at or cls._date_sort_value(filing),
            filing.period_end or date.min,
            filing.id,
        )

    @staticmethod
    def _date_sort_value(filing: Filing) -> datetime:
        return datetime.combine(filing.filing_date, datetime.min.time(), tzinfo=UTC)

    @staticmethod
    def _validate_source(source: str) -> SourceSelection:
        normalized = source.strip().casefold().replace("-", "_")
        supported = {"all", *SUPPORTED_SOURCE_NAMES}
        if normalized not in supported:
            source_names = (
                "all, fca_nsm, edinet, esef, cvm, sgx, bmv, nse, sedar, smv, "
                "sfc, dart, asx, kap"
            )
            raise ConfigurationError(f"Source must be one of: {source_names}.")
        return cast(SourceSelection, normalized)

    def _market_source_for_company(
        self,
        company_id: str,
        selection: SourceSelection,
    ) -> PublicMarketClient | None:
        if selection != "all":
            source = self._market_sources_by_name.get(selection)
            return source if source and source.matches_company_id(company_id) else None
        return next(
            (
                source
                for source in self._market_sources
                if source.matches_company_id(company_id)
            ),
            None,
        )

    @staticmethod
    def _is_edinet_company_id(company_id: str) -> bool:
        return company_id.casefold().startswith("jp_e")

    @staticmethod
    def _is_cvm_company_id(company_id: str) -> bool:
        return company_id.casefold().startswith("br_cvm_")

    @staticmethod
    def _is_sgx_company_id(company_id: str) -> bool:
        return company_id.casefold().startswith("sg_sgx_")

    @staticmethod
    def _is_dart_company_id(company_id: str) -> bool:
        return company_id.casefold().startswith("kr_dart_")

    def _is_esef_company_id(self, company_id: str) -> bool:
        return self._esef_for_company_id(company_id) is not None

    def _esef_for_company_id(self, company_id: str) -> EsefClient | None:
        return next(
            (
                esef
                for esef in self._esef_sources
                if esef.matches_company_id(company_id)
            ),
            None,
        )

    def _esef_for_filing_id(self, filing_id: str) -> EsefClient | None:
        return next(
            (esef for esef in self._esef_sources if esef.matches_filing_id(filing_id)),
            None,
        )

    @staticmethod
    def _markdown_body(markdown: str) -> str:
        marker = "\n---\n\n"
        return markdown.split(marker, 1)[1] if marker in markdown else markdown

    @staticmethod
    def _add_header(
        filing: Filing,
        markdown: str,
        *,
        quality: ExtractionQuality | None = None,
        extraction_method: str | None = None,
    ) -> str:
        body = markdown.strip()
        issuer = filing.issuer_name or filing.company_id
        quality_line = (
            f"- Extraction quality: {quality.status} ({quality.score}/100)\n"
            if quality
            else ""
        )
        method_line = (
            f"- Extraction method: `{extraction_method}`\n" if extraction_method else ""
        )
        return (
            f"# {filing.title}\n\n"
            f"- Issuer: {issuer}\n"
            f"- Company ID: `{filing.company_id}`\n"
            f"- Filed: {filing.filing_date.isoformat()}\n"
            f"- Filing type: `{filing.filing_type}`\n"
            f"- Source system: `{filing.source}`\n"
            f"{method_line}"
            f"{quality_line}"
            f"- Source: {filing.source_url}\n\n"
            f"---\n\n{body}\n"
        )
