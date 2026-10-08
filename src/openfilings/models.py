"""Normalized public models shared by the CLI, cache, adapters, and MCP."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


SourceName = Literal[
    "fca_nsm",
    "edinet",
    "esef",
    "cvm",
    "sgx",
    "bmv",
    "nse",
    "sedar",
    "smv",
    "sfc",
    "dart",
    "asx",
    "kap",
]
SourceSelection = Literal[
    "all",
    "fca_nsm",
    "edinet",
    "esef",
    "cvm",
    "sgx",
    "bmv",
    "nse",
    "sedar",
    "smv",
    "sfc",
    "dart",
    "asx",
    "kap",
]
SUPPORTED_SOURCE_NAMES = frozenset(
    {
        "fca_nsm",
        "edinet",
        "esef",
        "cvm",
        "sgx",
        "bmv",
        "nse",
        "sedar",
        "smv",
        "asx",
        "sfc",
        "dart",
        "kap",
    }
)
# Bump whenever financial-statement extraction logic changes (XBRL mappings,
# structured-source extractors, PDF statement parsing). The SQLite cache
# stamps stored financials with this version and treats a mismatch as a
# cache miss, so corrections propagate without manual re-extraction.
FINANCIALS_EXTRACTOR_VERSION = 1
MarketCode = Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
CountryCode = Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
QualityStatus = Literal["good", "degraded", "unusable"]
OcrMode = Literal["auto", "never", "always"]
FactProvenance = Literal[
    "tagged_xbrl",
    "regulated_structured_data",
    "pdf_table",
    "derived",
]
StatementType = Literal[
    "income_statement",
    "balance_sheet",
    "cash_flow_statement",
    "comprehensive_income",
    "changes_in_equity",
]


class ExtractionQuality(DomainModel):
    score: int = Field(default=50, ge=0, le=100)
    status: QualityStatus = "degraded"
    character_count: int = Field(default=0, ge=0)
    page_count: int | None = Field(default=None, ge=0)
    characters_per_page: float | None = Field(default=None, ge=0)
    alphanumeric_ratio: float = Field(default=0.0, ge=0, le=1)
    replacement_character_ratio: float = Field(default=0.0, ge=0, le=1)
    warnings: tuple[str, ...] = ("quality_not_recorded",)


class IssuerReference(DomainModel):
    name: str
    lei: str | None = None


class Company(DomainModel):
    id: str
    source_id: str
    name: str
    sources: tuple[SourceName, ...] = ("fca_nsm",)
    lei: str | None = None
    market: MarketCode = "UK"
    country_code: CountryCode = "GB"
    ticker: str | None = None
    local_code: str | None = None
    english_name: str | None = None
    status: str | None = None
    company_type: str | None = None
    address: str | None = None
    # Other jurisdictions where the same legal entity (same LEI) also files.
    # Populated by search-result deduplication (issue #12): one LEI can
    # produce one company ID per ESEF jurisdiction, so a bare-query search
    # collapses them to a single record and lists the rest here. Filing
    # discovery is unaffected - every per-jurisdiction ID still resolves
    # and lists only its own market's filings.
    other_jurisdictions: tuple[str, ...] = ()
    source_url: str


def deduplicate_companies_by_lei(companies: Sequence[Company]) -> list[Company]:
    """Collapse same-LEI company records to one record per legal entity.

    Distinct LEIs never merge (similar names stay distinct). The surviving
    record is chosen deterministically - alphabetically first by
    (market, country code, id) - so a bare query for a dual-jurisdiction
    issuer always binds the same jurisdiction instead of depending on
    result order. The dropped jurisdictions are listed on
    ``other_jurisdictions``; records without an LEI pass through untouched.
    """

    result: list[Company] = []
    lei_index: dict[str, int] = {}
    for company in companies:
        lei = (company.lei or "").strip().upper()
        if not lei:
            result.append(company)
            continue
        index = lei_index.get(lei)
        if index is None:
            lei_index[lei] = len(result)
            result.append(company)
            continue
        preferred, dropped = sorted(
            (result[index], company),
            key=lambda item: (item.market, item.country_code, item.id),
        )
        jurisdictions = tuple(
            sorted(
                {
                    *preferred.other_jurisdictions,
                    dropped.country_code,
                    *dropped.other_jurisdictions,
                }
                - {preferred.country_code}
            )
        )
        sources = tuple(dict.fromkeys((*preferred.sources, *dropped.sources)))
        result[index] = preferred.model_copy(
            update={
                "sources": sources,
                "lei": preferred.lei or dropped.lei,
                "other_jurisdictions": jurisdictions,
            }
        )
    return result


class Filing(DomainModel):
    id: str
    company_id: str
    source: SourceName = "fca_nsm"
    source_id: str
    title: str
    category: str
    filing_type: str
    filing_date: date
    published_at: datetime | None = None
    period_start: date | None = None
    period_end: date | None = None
    description: str | None = None
    pages: int | None = None
    document_id: str | None = None
    document_metadata_url: str | None = None
    media_type: str | None = None
    issuer_name: str | None = None
    issuer_lei: str | None = None
    related_issuers: tuple[IssuerReference, ...] = ()
    language: str = "en"
    xbrl_available: bool = False
    pdf_available: bool = False
    csv_available: bool = False
    source_url: str

    @property
    def has_document(self) -> bool:
        return self.document_id is not None


class FilingContent(DomainModel):
    filing_id: str
    markdown: str
    source_url: str
    media_type: str = "application/pdf"
    extraction_method: str = "pymupdf4llm"
    quality: ExtractionQuality = Field(default_factory=ExtractionQuality)
    sha256: str = Field(min_length=64, max_length=64)
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    from_cache: bool = False


class ReportingPeriod(DomainModel):
    """A duration or instant context attached to one or more XBRL facts."""

    id: str
    start_date: date | None = None
    end_date: date
    kind: Literal["instant", "duration"]
    fiscal_period: str | None = None

    @property
    def label(self) -> str:
        prefix = f"{self.fiscal_period} " if self.fiscal_period else ""
        return f"{prefix}{self.end_date.isoformat()}"


class FinancialValue(DomainModel):
    """One normalized numeric fact with extraction provenance and confidence.

    ``confidence`` expresses confidence in the extraction, not a judgement on
    an issuer's accounting. Consumers should retain ``provenance`` and the
    report-level source URL when using a value in an investment decision.
    """

    period: ReportingPeriod
    value: Decimal
    unit: str | None = None
    decimals: str | None = None
    dimensions: tuple[tuple[str, str], ...] = ()
    provenance: FactProvenance = "tagged_xbrl"
    confidence: int = Field(default=100, ge=0, le=100)
    source_context: str | None = None
    derived_from: tuple[str, ...] = ()


class FinancialLineItem(DomainModel):
    """A standardized line item backed by a source taxonomy concept."""

    code: str
    name: str
    concept: str
    values: tuple[FinancialValue, ...]


class FinancialStatement(DomainModel):
    """A compact, cross-period financial statement."""

    statement_type: StatementType
    title: str
    currency: str | None = None
    line_items: tuple[FinancialLineItem, ...]

    @property
    def periods(self) -> tuple[ReportingPeriod, ...]:
        periods: dict[str, ReportingPeriod] = {}
        for item in self.line_items:
            for value in item.values:
                periods.setdefault(value.period.label, value.period)
        return tuple(periods.values())

    def to_records(self) -> tuple[dict[str, object], ...]:
        labels = [period.label for period in self.periods]
        records: list[dict[str, object]] = []
        for item in self.line_items:
            values = {value.period.label: value.value for value in item.values}
            records.append(
                {
                    "code": item.code,
                    "name": item.name,
                    "concept": item.concept,
                    **{label: values.get(label) for label in labels},
                }
            )
        return tuple(records)

    def to_markdown(self) -> str:
        labels = [period.label for period in self.periods]
        heading = f"## {self.title}"
        currency = f" ({self.currency})" if self.currency else ""
        header = ["Line item", *labels]
        divider = ["---", *("---:" for _ in labels)]
        rows = [
            [
                str(record["name"]),
                *(_markdown_value(record[label]) for label in labels),
            ]
            for record in self.to_records()
        ]
        table = "\n".join(_markdown_row(row) for row in [header, divider, *rows])
        return f"{heading}{currency}\n\n{table}\n"

    def to_dataframe(self) -> Any:
        """Return a pandas DataFrame when the optional dependency is installed."""

        try:
            import pandas as pd
        except ImportError as exc:
            raise ImportError(
                "Install openfilings[dataframe] to use to_dataframe()."
            ) from exc
        return pd.DataFrame(self.to_records())


class FilingFinancials(DomainModel):
    """Structured statements extracted from a tagged filing."""

    filing_id: str
    company_id: str
    source_url: str
    extraction_method: str = "inline-xbrl-stream"
    statements: tuple[FinancialStatement, ...]
    fact_count: int = Field(ge=0)
    taxonomy_namespaces: tuple[str, ...] = ()
    sha256: str = Field(min_length=64, max_length=64)
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    from_cache: bool = False

    def statement(self, statement_type: StatementType) -> FinancialStatement | None:
        return next(
            (
                statement
                for statement in self.statements
                if statement.statement_type == statement_type
            ),
            None,
        )

    def income_statement(self) -> FinancialStatement | None:
        return self.statement("income_statement")

    def balance_sheet(self) -> FinancialStatement | None:
        return self.statement("balance_sheet")

    def cash_flow_statement(self) -> FinancialStatement | None:
        return self.statement("cash_flow_statement")

    def comprehensive_income(self) -> FinancialStatement | None:
        return self.statement("comprehensive_income")

    def changes_in_equity(self) -> FinancialStatement | None:
        return self.statement("changes_in_equity")

    def to_markdown(self) -> str:
        return "\n".join(statement.to_markdown() for statement in self.statements)


class CompanyFacts(DomainModel):
    """A multi-period time series per line item, merged across a company's
    most recent structured filings - EdgarTools' ``get_facts()`` concept,
    not tied to any single filing."""

    company_id: str
    statements: tuple[FinancialStatement, ...]
    filing_ids: tuple[str, ...]
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def statement(self, statement_type: StatementType) -> FinancialStatement | None:
        return next(
            (
                statement
                for statement in self.statements
                if statement.statement_type == statement_type
            ),
            None,
        )

    def income_statement(self) -> FinancialStatement | None:
        return self.statement("income_statement")

    def balance_sheet(self) -> FinancialStatement | None:
        return self.statement("balance_sheet")

    def cash_flow_statement(self) -> FinancialStatement | None:
        return self.statement("cash_flow_statement")

    def comprehensive_income(self) -> FinancialStatement | None:
        return self.statement("comprehensive_income")

    def changes_in_equity(self) -> FinancialStatement | None:
        return self.statement("changes_in_equity")

    def to_markdown(self) -> str:
        return "\n".join(statement.to_markdown() for statement in self.statements)


HistoricalView = Literal["as_reported", "latest_restated", "as_of"]


class HistoricalFact(DomainModel):
    """An immutable, filing-scoped normalized fact for historical research.

    It deliberately retains the filing and report timestamp so a query can
    distinguish a value originally reported for a period from a later restatement.
    Raw source taxonomies remain represented by ``concept`` and ``source_context``.
    """

    company_id: str
    filing_id: str
    source: SourceName
    reported_at: datetime
    statement_type: StatementType
    code: str
    name: str
    concept: str
    period: ReportingPeriod
    value: Decimal
    unit: str | None = None
    decimals: str | None = None
    dimensions: tuple[tuple[str, str], ...] = ()
    provenance: FactProvenance = "tagged_xbrl"
    confidence: int = Field(default=100, ge=0, le=100)
    source_context: str | None = None
    derived_from: tuple[str, ...] = ()


class HistoricalBackfillResult(DomainModel):
    company_id: str
    source: SourceName
    discovered_filings: int
    processed_filings: int
    stored_facts: int
    failures: tuple[str, ...] = ()


class MajorHolderNotification(DomainModel):
    """One major-shareholding notification or regulator-published position."""

    filing_id: str
    company_id: str
    issuer_name: str
    holder_name: str
    isin: str | None = None
    reason: str | None = None
    position_date: date | None = None
    date_crossed: date | None = None
    date_notified: date | None = None
    total_percent: Decimal | None = None
    total_voting_rights: int | None = None
    source_url: str


class InsiderPriceVolume(DomainModel):
    price: Decimal | None = None
    currency: str | None = None
    volume: int | None = None


class InsiderDealing(DomainModel):
    """One UK MAR PDMR/PCA notification parsed from an FCA NSM filing."""

    filing_id: str
    company_id: str
    issuer_name: str
    person_name: str
    position: str | None = None
    initial_or_amendment: str | None = None
    instrument: str | None = None
    isin: str | None = None
    transaction_natures: tuple[str, ...] = ()
    price_volume: tuple[InsiderPriceVolume, ...] = ()
    transaction_dates: tuple[date, ...] = ()
    places: tuple[str, ...] = ()
    source_url: str


class CacheStats(DomainModel):
    companies: int = Field(ge=0)
    filings: int = Field(ge=0)
    documents: int = Field(ge=0)
    source_documents: int = Field(default=0, ge=0)
    financial_reports: int = Field(default=0, ge=0)
    compressed_content_bytes: int = Field(ge=0)
    compressed_source_bytes: int = Field(default=0, ge=0)
    compressed_financial_bytes: int = Field(default=0, ge=0)
    database_bytes: int = Field(ge=0)


class CachePruneResult(DomainModel):
    removed_documents: int = Field(ge=0)
    removed_source_documents: int = Field(default=0, ge=0)
    removed_financial_reports: int = Field(default=0, ge=0)
    before: CacheStats
    after: CacheStats


def _markdown_row(values: list[str]) -> str:
    return "| " + " | ".join(value.replace("|", "\\|") for value in values) + " |"


def _markdown_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)
