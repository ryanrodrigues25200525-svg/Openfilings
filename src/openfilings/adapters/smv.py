"""Official Peru SMV open-financial-data client."""

from __future__ import annotations

import asyncio
import html
import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx

from openfilings._version import __version__
from openfilings.adapters._common import RetryingClient, ranked_matches
from openfilings.adapters.base import SourceDocument
from openfilings.exceptions import DocumentUnavailableError, SourceError
from openfilings.limits import MAX_TAGGED_DOCUMENT_BYTES
from openfilings.models import Company, Filing, SourceName

_FINANCIAL_SERVICE_URL = (
    "https://mvnet.smv.gob.pe/ws_OD_EEFF/WebServiceInfoFinanciera.asmx"
)
FINANCIAL_DATA_URL = f"{_FINANCIAL_SERVICE_URL}/obtener_EFData"
DATASET_URL = (
    "https://mvnet.smv.gob.pe/SMV.OpenData.Web/Views/Datasets/"
    "Informacion_Financiera.aspx"
)

_ISSUER_TYPES = {"EMPRESAS EMISORAS", "EMPRESAS MERCADO ALTERNATIVO DE VALORES"}
# RPJ formats have widened beyond the legacy one-letter-plus-five-digits
# shape (B30006): live 2025-2026 registries also carry two-letter codes
# (CM0001 Volcan, CM0006 Cerro Verde, OE2305 Intercorp), three-letter codes
# (AFP002) and all-digit codes (023106 AENZA). The legacy-only pattern
# silently dropped 134 of 274 issuer rows in one year's registry - about
# half of the market was unfindable - so accept every observed shape; the
# row's company type and name are still validated separately.
_RPJ_PATTERN = re.compile(r"[A-Z]\d{5}|[A-Z]{2}\d{4}|[A-Z]{3}\d{3}|\d{6}")
_STATEMENTS = (
    ("BG", "Estado de situación financiera"),
    ("GP", "Estado de resultados"),
    ("FE", "Estado de flujos de efectivo"),
    ("RI", "Estado de resultados integrales"),
)
_PAGE_SIZE = 500
# How many recent years of SMV's summary dataset to merge into the issuer
# universe. Two covers an issuer that has not filed yet this calendar year
# without paying for a full decade of requests on first search.
_REGISTRY_YEARS = 2
_MAX_PAGES = 100
_MAX_CONCURRENT_STATEMENTS = 2
_STATEMENT_OPERATIONS = {
    "BG": "obtener_BalanceGeneral",
    "GP": "obtener_GanciaPerdida",
    "FE": "obtener_FlujoEfectivo",
    "RI": "obtener_ResultadosIntegrales",
}


class SmvClient(RetryingClient):
    """Search Peruvian public issuers and expose official SMV statement tables."""

    source: SourceName = "smv"

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
        today: Callable[[], date] = date.today,
    ) -> None:
        super().__init__(
            "SMV",
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            headers={"User-Agent": f"openfilings/{__version__}"},
            client=client,
        )
        self._today = today
        self._company_year: int | None = None
        self._companies: tuple[Company, ...] | None = None
        self._summary_rows: dict[tuple[int, str], list[dict[str, Any]]] = {}

    async def search_companies(self, query: str, *, limit: int = 10) -> list[Company]:
        companies = await self._company_registry()
        records = [
            (
                (
                    company.source_id,
                    company.local_code or "",
                    company.name,
                ),
                company,
            )
            for company in companies
        ]
        return ranked_matches(query.removeprefix("pe_smv_"), records, limit=limit)

    async def list_filings(
        self,
        company_id: str,
        *,
        category: str | None = "accounts",
        limit: int = 25,
    ) -> list[Filing]:
        rpj = self._rpj(company_id)
        await self._company_registry()
        filings: list[Filing] = []
        start_year = self._company_year or self._today().year
        for year in range(start_year, max(start_year - 10, 2012), -1):
            for information_type in ("C", "I"):
                rows = await self._financial_rows(
                    year,
                    information_type,
                    statement="IF",
                    summary=True,
                )
                row = next(
                    (item for item in rows if str(item.get("RPJ", "")) == rpj),
                    None,
                )
                if row is None:
                    continue
                filing = self._filing_from_row(row, information_type)
                if filing is not None:
                    filings.append(filing)
            if len(filings) >= max(1, limit):
                break
        filings.sort(key=lambda filing: filing.filing_date, reverse=True)
        return filings[: max(1, limit)]

    async def download_document(self, document_id: str) -> SourceDocument:
        year, information_type, rpj = self._document_key(document_id)
        sections: list[str] = []
        issuer_name = rpj
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_STATEMENTS)

        async def statement_rows(statement_code: str) -> list[dict[str, Any]]:
            async with semaphore:
                return await self._financial_rows(
                    year,
                    information_type,
                    statement=statement_code,
                    summary=False,
                    rpj=rpj,
                )

        datasets = await asyncio.gather(
            *(statement_rows(statement_code) for statement_code, _ in _STATEMENTS)
        )
        for (_, title), rows in zip(_STATEMENTS, datasets, strict=True):
            if not rows:
                continue
            issuer_name = str(rows[0].get("NombreEmpresa", rpj)).strip()
            sections.append(self._statement_html(title, rows))
        if not sections:
            raise DocumentUnavailableError(
                f"SMV returned no statement rows for {rpj} in {year}."
            )
        document = (
            "<!doctype html><html><head><meta charset='utf-8'><title>"
            f"{html.escape(issuer_name)} {year}</title></head><body>"
            f"<h1>{html.escape(issuer_name)} — {year}</h1>"
            f"<p>Source: SMV Open Data</p>{''.join(sections)}</body></html>"
        ).encode()
        return SourceDocument(
            data=document,
            media_type="text/html",
            source_url=self._source_url(year, information_type, "IF"),
            profile="smv",
        )

    def matches_company_id(self, value: str) -> bool:
        return value.casefold().startswith("pe_smv_")

    def matches_filing_id(self, value: str) -> bool:
        return value.casefold().startswith("pe_smv_filing_")

    async def _company_registry(self) -> tuple[Company, ...]:
        if self._companies is not None:
            return self._companies
        # The issuer universe is whoever appears in SMV's summary financial
        # dataset, so a single year is a biased sample: early in a calendar
        # year only the handful of issuers who have already filed are
        # present, and everyone else becomes unfindable. Confirmed live -
        # one year returned 94 issuers and omitted Cementos Pacasmayo
        # entirely. Merge the most recent years that carry data instead.
        companies: dict[str, Company] = {}
        years_with_data = 0
        for year in range(self._today().year, max(self._today().year - 10, 2012), -1):
            rows: list[dict[str, Any]] = []
            for information_type in ("I", "C"):
                rows.extend(
                    await self._financial_rows(
                        year,
                        information_type,
                        statement="IF",
                        summary=True,
                    )
                )
            found = False
            for row in rows:
                company = self._company_from_row(row)
                if company is not None:
                    companies.setdefault(company.id, company)
                    found = True
            if not found:
                continue
            if self._company_year is None:
                self._company_year = year
            years_with_data += 1
            if years_with_data >= _REGISTRY_YEARS:
                break
        if not companies:
            raise SourceError("SMV returned no recent public-issuer financial dataset.")
        self._companies = tuple(companies.values())
        return self._companies

    async def _financial_rows(
        self,
        year: int,
        information_type: str,
        *,
        statement: str,
        summary: bool,
        rpj: str | None = None,
    ) -> list[dict[str, Any]]:
        key = (year, information_type)
        if summary and key in self._summary_rows:
            return self._summary_rows[key]
        if rpj is not None:
            return await self._statement_rows(year, information_type, statement, rpj)
        clean_rows: list[dict[str, Any]] = []
        for page in range(1, _MAX_PAGES + 1):
            rows, total_pages = await self._financial_page(
                year,
                information_type,
                statement=statement,
                page=page,
            )
            if rpj is None:
                clean_rows.extend(rows)
            else:
                clean_rows.extend(
                    row
                    for row in rows
                    if str(row.get("RPJ", "")).strip().upper() == rpj
                )
                last_rpj = str(rows[-1].get("RPJ", "")).strip().upper() if rows else ""
                if last_rpj > rpj:
                    break
            if page >= total_pages:
                break
        else:
            raise SourceError("SMV financial dataset exceeded the page limit.")
        if summary:
            self._summary_rows[key] = clean_rows
        return clean_rows

    async def _statement_rows(
        self,
        year: int,
        information_type: str,
        statement: str,
        rpj: str,
    ) -> list[dict[str, Any]]:
        operation = _STATEMENT_OPERATIONS.get(statement)
        if operation is None:
            raise SourceError(f"SMV does not support statement code {statement}.")
        response = await self._request(
            "POST",
            f"{_FINANCIAL_SERVICE_URL}/{operation}",
            json={
                "Ejercicio": str(year),
                "Periodo": "A",
                "Tipo": information_type,
            },
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        if len(response.content) > MAX_TAGGED_DOCUMENT_BYTES:
            raise SourceError("SMV statement response exceeds the 150 MB limit.")
        try:
            serialized = response.json().get("d", "[]")
            rows = json.loads(serialized) if isinstance(serialized, str) else serialized
        except (json.JSONDecodeError, ValueError, AttributeError) as exc:
            raise SourceError("SMV returned an invalid statement dataset.") from exc
        if not isinstance(rows, list):
            raise SourceError("SMV returned an invalid statement dataset.")
        return [
            row
            for row in rows
            if isinstance(row, dict) and str(row.get("RPJ", "")).strip().upper() == rpj
        ]

    async def _financial_page(
        self,
        year: int,
        information_type: str,
        *,
        statement: str,
        page: int,
    ) -> tuple[list[dict[str, Any]], int]:
        response = await self._request(
            "POST",
            FINANCIAL_DATA_URL,
            params={
                "ejercicio": year,
                "periodo": "A",
                "tipo": information_type,
                "estado": statement,
            },
            json={
                "_search": False,
                "rows": _PAGE_SIZE,
                "page": page,
                "sidx": "RPJ",
                "sord": "asc",
            },
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            payload = response.json().get("d", {})
            rows = payload.get("rows") or []
            total_pages = max(1, int(payload.get("total") or 1))
        except (TypeError, ValueError, AttributeError) as exc:
            raise SourceError("SMV returned an invalid financial dataset.") from exc
        if not isinstance(rows, list):
            raise SourceError("SMV returned an invalid financial dataset.")
        return [row for row in rows if isinstance(row, dict)], total_pages

    @staticmethod
    def _company_from_row(row: dict[str, Any]) -> Company | None:
        company_type = str(row.get("TipoEmpresa", "")).strip().upper()
        rpj = str(row.get("RPJ", "")).strip().upper()
        name = str(row.get("NombreEmpresa", "")).strip()
        ruc = str(row.get("RUC", "")).strip()
        if (
            company_type not in _ISSUER_TYPES
            or not _RPJ_PATTERN.fullmatch(rpj)
            or not name
        ):
            return None
        return Company(
            id=f"pe_smv_{rpj}",
            source_id=rpj,
            name=name,
            sources=("smv",),
            market="PE",
            country_code="PE",
            local_code=ruc or None,
            status="public SMV issuer",
            company_type=company_type.title(),
            source_url=DATASET_URL,
        )

    @staticmethod
    def _filing_from_row(row: dict[str, Any], information_type: str) -> Filing | None:
        rpj = str(row.get("RPJ", "")).strip().upper()
        name = str(row.get("NombreEmpresa", "")).strip()
        try:
            year = int(row.get("Ejercicio", 0))
        except (TypeError, ValueError):
            return None
        if not _RPJ_PATTERN.fullmatch(rpj) or year < 2000:
            return None
        type_name = "Consolidated" if information_type == "C" else "Individual"
        document_id = f"smv:{year}:A:{information_type}:{rpj}"
        source_url = SmvClient._source_url(year, information_type, "IF")
        filed_on = date(year + 1, 4, 30)
        return Filing(
            id=f"pe_smv_filing_{rpj}_{year}_{information_type}",
            company_id=f"pe_smv_{rpj}",
            source="smv",
            source_id=document_id,
            title=f"{type_name} Annual Financial Statements {year}",
            category="accounts",
            filing_type="annual_consolidated" if information_type == "C" else "annual",
            filing_date=filed_on,
            published_at=datetime.combine(filed_on, datetime.min.time(), tzinfo=UTC),
            period_start=date(year, 1, 1),
            period_end=date(year, 12, 31),
            document_id=document_id,
            media_type="text/html",
            issuer_name=name,
            language="es",
            source_url=source_url,
        )

    @staticmethod
    def _statement_html(title: str, rows: list[dict[str, Any]]) -> str:
        body = []
        for row in rows:
            description = html.escape(str(row.get("DescripcionCuenta", "")).strip())
            if not description:
                continue
            current = html.escape(str(row.get("Monto1", "")))
            comparative = html.escape(str(row.get("Monto2", "")))
            body.append(
                f"<tr><td>{description}</td><td>{current}</td>"
                f"<td>{comparative}</td></tr>"
            )
        try:
            current_year = int(rows[0].get("Ejercicio", 0))
        except (IndexError, TypeError, ValueError):
            current_year = 0
        current_label = str(current_year) if current_year else "Periodo actual"
        comparative_label = str(current_year - 1) if current_year else "Comparativo"
        return (
            f"<h2>{html.escape(title)}</h2><table><thead><tr>"
            f"<th>Cuenta</th><th>{current_label}</th><th>{comparative_label}</th>"
            f"</tr></thead><tbody>{''.join(body)}</tbody></table>"
        )

    @staticmethod
    def _source_url(year: int, information_type: str, statement: str) -> str:
        return (
            f"{FINANCIAL_DATA_URL}?ejercicio={year}&periodo=A&"
            f"tipo={information_type}&estado={statement}"
        )

    @staticmethod
    def _rpj(value: str) -> str:
        clean = value.strip().upper()
        if clean.casefold().startswith("pe_smv_"):
            clean = clean[len("pe_smv_") :]
        if not _RPJ_PATTERN.fullmatch(clean):
            raise SourceError(
                "Expected a Peruvian company ID shaped like pe_smv_B30006."
            )
        return clean

    @staticmethod
    def _document_key(value: str) -> tuple[int, str, str]:
        match = re.fullmatch(
            rf"smv:(20\d{{2}}):A:([CI]):({_RPJ_PATTERN.pattern})", value.strip()
        )
        if match is None:
            raise DocumentUnavailableError("Unsafe SMV document identifier.")
        return int(match.group(1)), match.group(2), match.group(3)
