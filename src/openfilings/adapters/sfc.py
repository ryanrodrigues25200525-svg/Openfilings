"""Official Colombia SFC/SIMEV public-filings client."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

import httpx

from openfilings._version import __version__
from openfilings.adapters._common import RetryingClient, ranked_matches
from openfilings.adapters.base import SourceDocument
from openfilings.exceptions import DocumentUnavailableError, SourceError
from openfilings.limits import MAX_TAGGED_DOCUMENT_BYTES
from openfilings.models import Company, Filing, SourceName

API_BASE = "https://www.superfinanciera.gov.co/sfcservices/SIMEV2"
ISSUER_SEARCH_URL = f"{API_BASE}/emisores-inscritos-vigentes/reporte"
FILINGS_URL = (
    f"{API_BASE}/fin-ejercicio/tipo-entidad/{{entity_type}}/codigo-entidad/{{code}}"
)
DOWNLOAD_URL = (
    f"{API_BASE}/fin-ejercicio/archivo-fin-ejercicio/id-archivo/{{document_id}}"
)
PORTAL_URL = "https://www.superfinanciera.gov.co/SIMEV2/rnve/emisoresinscritosvigentes"

# This is the public client identifier shipped by the SIMEV frontend, not a user secret.
_PUBLIC_API_KEY = "D4p2MNknJeQbz4mipjjEKOjPL1Eb4zwq"

# datos.gov.co's Socrata dataset for SFC's CUIF (Catalogo Unico de Informacion
# Financiera) - a supervisory chart of accounts reported by every SFC-regulated
# entity type (banks, insurers, pension fund managers, brokers, ...), not just
# banks. Balance-sheet-family accounts (class 1/2/3: assets/liabilities/equity)
# are stock figures and reconcile exactly; income/expense accounts (class 4/5)
# are reported unclosed for supervisory purposes (revenue exactly equals
# expenses even at year-end) and are not usable as an income statement.
CUIF_DATASET_URL = "https://www.datos.gov.co/resource/mxk5-ce6w.json"
# The balance-sheet-family account codes recognized by
# openfilings.xbrl.sfc_cuif_structured - kept as a SoQL $where filter so the
# query returns only these rows instead of every sub-account under them
# (an entity's full account tree can run into the thousands of rows).
_BALANCE_SHEET_ACCOUNT_CODES = ("100000", "200000", "300000", "110000")


def _is_equity_row(row: dict[str, object]) -> bool:
    """True for the registry line that lists the issuer's common shares."""

    return str(row.get("nmEspecie", "")).strip().casefold() == "accion"


class SfcClient(RetryingClient):
    """Search BVC-listed issuers and retrieve SFC year-end report PDFs."""

    source: SourceName = "sfc"

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            "SFC",
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            headers={
                "User-Agent": f"openfilings/{__version__}",
                "api-key": _PUBLIC_API_KEY,
                "Referer": PORTAL_URL,
            },
            client=client,
        )

    async def search_companies(self, query: str, *, limit: int = 10) -> list[Company]:
        clean_query = query.strip()
        if clean_query.casefold().startswith("co_sfc_"):
            clean_query = clean_query.rsplit("_", 1)[-1]
        if len(clean_query) < 3:
            return []
        response = await self._request(
            "GET",
            ISSUER_SEARCH_URL,
            params={"nombreEntidad": clean_query},
        )
        try:
            rows = response.json()
        except ValueError as exc:
            raise SourceError("SFC returned an invalid issuer response.") from exc
        if not isinstance(rows, list):
            raise SourceError("SFC returned an invalid issuer response.")
        # The registry exposes one row per *security*, so a name query returns
        # the same issuer once per species (ACCION, BONO, ...). Collect every
        # issuer rather than the first `limit` rows: upstream order is not
        # alphabetical, and truncating before local ranking once hid Ecopetrol
        # (equity) behind its own bond lines. Equity-registered issuers rank
        # first - an issuer with listed equity is the better answer to a name
        # query - and bond-only lines stay as lower-ranked, still-labelable
        # candidates rather than being dropped.
        companies: dict[str, Company] = {}
        equity_ids: set[str] = set()
        for row in rows:
            company = self._company_from_row(row)
            if company is None:
                continue
            companies.setdefault(company.id, company)
            if isinstance(row, dict) and _is_equity_row(row):
                equity_ids.add(company.id)
        if not companies:
            return []
        ranked = ranked_matches(
            clean_query,
            [((company.name,), company) for company in companies.values()],
            limit=len(companies),
        )
        if not ranked:
            # The registry matched the name but local folding did not (an
            # accent- or punctuation-only difference): keep its own answer
            # rather than dropping a valid hit.
            ranked = list(companies.values())
        ordered = [company for company in ranked if company.id in equity_ids]
        ordered += [company for company in ranked if company.id not in equity_ids]
        return ordered[: max(1, limit)]

    async def list_filings(
        self,
        company_id: str,
        *,
        category: str | None = "accounts",
        limit: int = 25,
    ) -> list[Filing]:
        entity_type, code = self._company_key(company_id)
        response = await self._request(
            "GET",
            FILINGS_URL.format(entity_type=entity_type, code=code),
        )
        try:
            rows = response.json()
        except ValueError as exc:
            raise SourceError("SFC returned an invalid filing response.") from exc
        if not isinstance(rows, list):
            raise SourceError("SFC returned an invalid filing response.")
        filings = [
            filing
            for row in rows
            if (filing := self._filing_from_row(company_id, row)) is not None
        ]
        filings.sort(key=lambda filing: filing.published_at, reverse=True)
        return filings[: max(1, limit)]

    async def download_document(self, document_id: str) -> SourceDocument:
        clean_id = self._document_id(document_id)
        source_url = DOWNLOAD_URL.format(document_id=clean_id)
        response = await self._request("GET", source_url)
        data = response.content
        if not data:
            raise DocumentUnavailableError("The SFC filing document was empty.")
        if len(data) > MAX_TAGGED_DOCUMENT_BYTES:
            raise DocumentUnavailableError("The SFC filing exceeds the 150 MB limit.")
        if not data.startswith(b"%PDF"):
            raise DocumentUnavailableError("SFC did not return a PDF filing.")
        return SourceDocument(
            data=data,
            media_type="application/pdf",
            source_url=source_url,
        )

    async def cuif_balance_sheet_rows(
        self, entity_type: str, entity_code: str, period_end: date
    ) -> list[dict[str, object]] | None:
        """Fetch CUIF balance-sheet accounts for one entity and cut-off date
        from datos.gov.co. Returns None if that entity/date combination has
        no published rows, so callers can fall back to the PDF filing."""

        fecha_corte = f"{period_end.isoformat()}T00:00:00.000"
        codes = ",".join(f"'{code}'" for code in _BALANCE_SHEET_ACCOUNT_CODES)
        response = await self._request(
            "GET",
            CUIF_DATASET_URL,
            params={
                "tipo_entidad": entity_type,
                "codigo_entidad": entity_code,
                "fecha_corte": fecha_corte,
                "moneda": "0",
                "$where": f"cuenta in ({codes})",
            },
        )
        message = "datos.gov.co returned an invalid CUIF response."
        try:
            rows = response.json()
        except ValueError as exc:
            raise SourceError(message) from exc
        if not isinstance(rows, list):
            raise SourceError(message)
        return rows or None

    def matches_company_id(self, value: str) -> bool:
        return value.casefold().startswith("co_sfc_")

    def matches_filing_id(self, value: str) -> bool:
        return value.casefold().startswith("co_sfc_filing_")

    @staticmethod
    def _company_from_row(row: object) -> Company | None:
        if not isinstance(row, dict):
            return None
        if str(row.get("cdBolsa", "")).strip().upper() != "BVC":
            return None
        entity_type = str(row.get("rnCotien", "")).strip()
        code = str(row.get("rnCodent", "")).strip()
        name = str(row.get("nmEntidad", "")).strip()
        nit = str(row.get("nrNitEntidad", "")).strip()
        if not (entity_type.isdigit() and code.isdigit() and name):
            return None
        address = ", ".join(
            part
            for part in (
                str(row.get("dsDireccion", "")).strip(),
                str(row.get("ciudad", "")).strip(),
            )
            if part
        )
        equity = _is_equity_row(row)
        return Company(
            id=f"co_sfc_{entity_type}_{code}",
            source_id=f"{entity_type}:{code}",
            name=name,
            sources=("sfc",),
            market="CO",
            country_code="CO",
            local_code=nit or None,
            status=(
                "active BVC equity issuer"
                if equity
                else "active BVC issuer (debt securities only)"
            ),
            company_type=str(row.get("nombre", "")).strip() or "SFC issuer",
            address=address or None,
            source_url=PORTAL_URL,
        )

    def _filing_from_row(self, company_id: str, row: object) -> Filing | None:
        if not isinstance(row, dict):
            return None
        attachment = row.get("archivoInfoRelevante")
        entity = row.get("entidad")
        if not isinstance(attachment, dict) or not isinstance(entity, dict):
            return None
        document_id = str(attachment.get("idArchivoInfoRelevante", "")).strip()
        name = str(attachment.get("nombre", "")).strip()
        description = str(row.get("resumen", "")).strip()
        content_type = str(attachment.get("contentType", "")).strip().casefold()
        if not document_id.isdigit() or content_type != "application/pdf":
            return None
        text = f"{name} {description}".casefold()
        if not any(
            marker in text
            for marker in ("fin de ejercicio", "estados financieros", "periódico")
        ):
            return None
        try:
            published_at = datetime.strptime(
                str(row.get("fechaRegistro", "")),
                "%Y-%m-%dT%H:%M:%S.%f%z",
            ).astimezone(UTC)
        except ValueError:
            return None
        quarterly = "trimestral" in text
        year_match = re.search(r"\b(20\d{2})\b", text)
        period_end = (
            date(int(year_match.group(1)), 12, 31)
            if year_match and not quarterly
            else None
        )
        issuer_name = str(entity.get("razonSocial", "")).strip() or company_id
        source_url = DOWNLOAD_URL.format(document_id=document_id)
        return Filing(
            id=f"co_sfc_filing_{document_id}",
            company_id=company_id,
            source="sfc",
            source_id=document_id,
            title=description or name,
            category="accounts",
            filing_type="quarterly" if quarterly else "annual",
            filing_date=published_at.date(),
            published_at=published_at,
            period_end=period_end,
            description=description or None,
            document_id=document_id,
            media_type="application/pdf",
            issuer_name=issuer_name,
            language="es",
            pdf_available=True,
            source_url=source_url,
        )

    @staticmethod
    def _company_key(value: str) -> tuple[str, str]:
        match = re.fullmatch(r"co_sfc_(\d{1,4})_(\d{1,6})", value.strip(), re.I)
        if match is None:
            raise SourceError(
                "Expected a Colombian company ID shaped like co_sfc_260_036."
            )
        return match.group(1), match.group(2)

    @staticmethod
    def _document_id(value: str) -> str:
        clean = value.strip()
        if clean.casefold().startswith("co_sfc_filing_"):
            clean = clean[len("co_sfc_filing_") :]
        if not clean.isdigit():
            raise DocumentUnavailableError("Unsafe SFC document identifier.")
        return clean
