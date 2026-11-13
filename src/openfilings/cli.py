"""Command-line interface for supported public filing markets."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import time
from collections.abc import Coroutine
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from openfilings.adapters.base import SourceDocument
from openfilings.adapters.sedar import MAX_SEDAR_DOCUMENT_BYTES
from openfilings.config import Settings
from openfilings.exceptions import (
    ConfigurationError,
    DocumentUnavailableError,
    OpenFilingsError,
)
from openfilings.extraction.document import extract_document
from openfilings.models import StatementType
from openfilings.service import OpenFilingsService
from openfilings.validation import validate_financials, validation_view

app = typer.Typer(
    name="openfilings",
    no_args_is_help=True,
    help="Search supported public filings and convert them to Markdown.",
)
cache_app = typer.Typer(no_args_is_help=True, help="Inspect or prune the local cache.")
app.add_typer(cache_app, name="cache")


class SourceOption(StrEnum):
    all = "all"
    fca_nsm = "fca-nsm"
    edinet = "edinet"
    esef = "esef"
    cvm = "cvm"
    sgx = "sgx"
    bmv = "bmv"
    nse = "nse"
    sedar = "sedar"
    smv = "smv"
    sfc = "sfc"
    dart = "dart"
    asx = "asx"
    kap = "kap"


class OcrOption(StrEnum):
    auto = "auto"
    never = "never"
    always = "always"


class StatementOption(StrEnum):
    income_statement = "income-statement"
    balance_sheet = "balance-sheet"
    cash_flow_statement = "cash-flow-statement"
    comprehensive_income = "comprehensive-income"
    changes_in_equity = "changes-in-equity"

    @property
    def model_value(self) -> StatementType:
        return self.value.replace("-", "_")  # type: ignore[return-value]


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Company name, ticker, or ISIN.")],
    limit: Annotated[int, typer.Option(min=1, max=100)] = 10,
    source: Annotated[SourceOption, typer.Option(help="Filing source to search.")] = (
        SourceOption.all
    ),
) -> None:
    """Search companies through configured public filing sources."""

    async def run() -> None:
        async with OpenFilingsService.from_settings() as service:
            companies = await service.search_companies(
                query, limit=limit, source=source.value
            )
        if not companies:
            typer.echo("No companies found.")
            return
        for company in companies:
            typer.echo(
                f"{company.id}\t{company.market}\t{company.name}\t"
                f"{company.ticker or company.lei or '-'}\t"
                f"{','.join(company.sources)}\t{company.status or '-'}\t"
                f"{company.address or '-'}"
            )

    _run(run())


@app.command()
def filings(
    company_id: Annotated[str, typer.Argument(help="OpenFilings company ID.")],
    category: Annotated[
        str | None,
        typer.Option(help="Filing category; use an empty value for all."),
    ] = "accounts",
    limit: Annotated[int, typer.Option(min=1, max=500)] = 25,
    source: Annotated[SourceOption, typer.Option(help="Filing source to include.")] = (
        SourceOption.all
    ),
    history_days: Annotated[
        int,
        typer.Option(
            min=1,
            max=3661,
            help="EDINET lookback window; defaults to 120 days.",
        ),
    ] = 120,
) -> None:
    """List a company's filings, newest first."""

    async def run() -> None:
        async with OpenFilingsService.from_settings() as service:
            results = await service.list_filings(
                company_id,
                category=category or None,
                limit=limit,
                source=source.value,
                edinet_lookback_days=history_days,
            )
        if not results:
            typer.echo("No filings found.")
            return
        for filing in results:
            availability = filing.media_type or (
                "DOCUMENT" if filing.has_document else "NO-DOCUMENT"
            )
            typer.echo(
                f"{filing.id}\t{filing.filing_date.isoformat()}\t"
                f"{filing.source}\t{filing.filing_type}\t{availability}\t"
                f"{filing.title}"
            )

    _run(run())


@app.command()
def fetch(
    filing_id: Annotated[str, typer.Argument(help="OpenFilings filing ID.")],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write Markdown to this file."),
    ] = None,
    refresh: Annotated[
        bool, typer.Option(help="Ignore locally cached Markdown.")
    ] = False,
    ocr: Annotated[
        OcrOption | None,
        typer.Option(help="Override OCR routing: auto, never, or always."),
    ] = None,
) -> None:
    """Download a filing document and return LLM-ready Markdown."""

    async def run() -> None:
        async with OpenFilingsService.from_settings() as service:
            content = await service.get_filing_markdown(
                filing_id,
                refresh=refresh,
                ocr_mode=ocr.value if ocr else None,
            )
        if output is None:
            typer.echo(content.markdown, nl=False)
            return
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content.markdown, encoding="utf-8")
        origin = "cache" if content.from_cache else "source"
        typer.echo(
            f"Wrote {output} ({origin}; {content.extraction_method}; "
            f"quality {content.quality.score}/100)."
        )

    _run(run())


@app.command()
def financials(
    filing_id: Annotated[
        str, typer.Argument(help="OpenFilings ID for a tagged annual report.")
    ],
    statement: Annotated[
        StatementOption | None,
        typer.Option(help="Optionally return only one statement type."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write structured JSON to this file."),
    ] = None,
    refresh: Annotated[
        bool, typer.Option(help="Ignore locally cached structured financials.")
    ] = False,
) -> None:
    """Extract normalized statements from tagged filings or supported PDFs."""

    async def run() -> None:
        async with OpenFilingsService.from_settings() as service:
            result = await service.get_filing_financials(filing_id, refresh=refresh)
        payload = result.model_dump(mode="json")
        validation = validation_view(validate_financials(result))
        if validation is not None:
            payload["validation"] = validation
            if not validation["ok"]:
                typer.echo(
                    "Warning: "
                    f"{validation['checks_failed']} accounting-identity check(s) "
                    "failed - this filing's figures may be inconsistent "
                    "(see 'validation' in the output).",
                    err=True,
                )
        if statement is not None:
            payload["statements"] = [
                item
                for item in payload["statements"]
                if item["statement_type"] == statement.model_value
            ]
        rendered = json.dumps(payload, indent=2)
        if output is None:
            typer.echo(rendered)
            return
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
        origin = "cache" if result.from_cache else "source"
        typer.echo(
            f"Wrote {output} ({origin}; {len(payload['statements'])} statement(s); "
            f"{result.fact_count} tagged facts)."
        )

    _run(run())


@app.command("search-disclosures")
def search_disclosures(
    keyword: Annotated[str, typer.Argument(help="Full-text keyword to search for.")],
    limit: Annotated[int, typer.Option(min=1, max=200)] = 25,
    source: Annotated[
        SourceOption,
        typer.Option(help="Only fca_nsm and cvm support full-text search."),
    ] = SourceOption.all,
) -> None:
    """Full-text search across every issuer's disclosures (fca_nsm, cvm)."""

    async def run() -> None:
        async with OpenFilingsService.from_settings() as service:
            results = await service.search_disclosures(
                keyword, limit=limit, source=source.value
            )
        if not results:
            typer.echo("No disclosures found.")
            return
        for filing in results:
            typer.echo(
                f"{filing.id}\t{filing.filing_date.isoformat()}\t"
                f"{filing.source}\t{filing.issuer_name}\t{filing.title}"
            )

    _run(run())


@app.command()
def facts(
    company_id: Annotated[str, typer.Argument(help="OpenFilings company ID.")],
    periods: Annotated[int, typer.Option(min=1, max=40)] = 8,
    source: Annotated[SourceOption, typer.Option()] = SourceOption.all,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write structured JSON to this file."),
    ] = None,
) -> None:
    """Merge a company's recent filings into one multi-period fact series."""

    async def run() -> None:
        async with OpenFilingsService.from_settings() as service:
            result = await service.get_company_facts(
                company_id, periods=periods, source=source.value
            )
        rendered = json.dumps(result.model_dump(mode="json"), indent=2)
        if output is None:
            typer.echo(rendered)
            return
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
        typer.echo(f"Wrote {output} ({len(result.filing_ids)} filing(s) merged).")

    _run(run())


@app.command("major-holders")
def major_holders(
    company_id: Annotated[str, typer.Argument(help="OpenFilings company ID.")],
    limit: Annotated[int, typer.Option(min=1, max=100)] = 25,
) -> None:
    """List a UK issuer's TR-1 major-shareholding notifications."""

    async def run() -> None:
        async with OpenFilingsService.from_settings() as service:
            holders = await service.list_major_holders(company_id, limit=limit)
        if not holders:
            typer.echo("No major-holder notifications found.")
            return
        for holder in holders:
            typer.echo(
                f"{holder.holder_name}\t{holder.total_percent}\t"
                f"{holder.date_crossed}\t{holder.filing_id}"
            )

    _run(run())


@app.command("search-major-holders")
def search_major_holders(
    holder_name: Annotated[str, typer.Argument(help="Holder name to search for.")],
    scan_limit: Annotated[
        int,
        typer.Option(
            min=1,
            max=1000,
            help="How many recent UK TR-1 filings to scan (not the full history).",
        ),
    ] = 200,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 25,
) -> None:
    """Bounded 13F-style reverse lookup: what has this holder disclosed a
    >5% position in, across UK issuers?"""

    async def run() -> None:
        async with OpenFilingsService.from_settings() as service:
            matches = await service.search_major_holders(
                holder_name, scan_limit=scan_limit, limit=limit
            )
        if not matches:
            typer.echo("No matching major-holder notifications found.")
            return
        for holder in matches:
            typer.echo(
                f"{holder.issuer_name}\t{holder.total_percent}\t"
                f"{holder.date_crossed}\t{holder.filing_id}"
            )

    _run(run())


@app.command("import-sedar")
def import_sedar(
    company_id: Annotated[
        str, typer.Argument(help="Cached TSX/TSXV company ID from `search`.")
    ],
    document: Annotated[
        str,
        typer.Argument(help="SEDAR+ generated HTTPS URL or local PDF path."),
    ],
    title: Annotated[str, typer.Option(help="Human-readable filing title.")],
    filing_date: Annotated[
        datetime, typer.Option(help="Submission date in YYYY-MM-DD format.")
    ],
    period_end: Annotated[
        datetime | None, typer.Option(help="Optional reporting period end.")
    ] = None,
    filing_type: Annotated[
        str, typer.Option(help="Normalized filing type, such as annual or interim.")
    ] = "annual",
    category: Annotated[
        str, typer.Option(help="Normalized category, normally accounts.")
    ] = "accounts",
    source_url: Annotated[
        str | None,
        typer.Option(
            help="Optional SEDAR+ generated URL to retain when importing a local PDF."
        ),
    ] = None,
) -> None:
    """Import a user-selected Canadian filing without browser automation."""

    async def run() -> None:
        is_remote = document.casefold().startswith(("https://", "http://"))
        document_data: bytes | None = None
        provenance_url = document
        if not is_remote:
            path = Path(document).expanduser()
            if not path.is_file():
                raise DocumentUnavailableError(f"Local PDF does not exist: {path}")
            if path.stat().st_size > MAX_SEDAR_DOCUMENT_BYTES:
                raise DocumentUnavailableError(
                    "The SEDAR+ document exceeds the 100 MB limit."
                )
            document_data = path.read_bytes()
            provenance_url = source_url or path.resolve().as_uri()
        elif source_url is not None:
            raise ConfigurationError(
                "--source-url is only valid when importing a local PDF."
            )

        async with OpenFilingsService.from_settings() as service:
            filing = await service.import_sedar_filing(
                company_id,
                document_url=provenance_url,
                document_data=document_data,
                title=title,
                filing_date=filing_date.date(),
                period_end=period_end.date() if period_end else None,
                filing_type=filing_type,
                category=category,
            )
        typer.echo(
            f"Imported {filing.id}. Run `openfilings fetch {filing.id}` "
            "or use the MCP filing tools."
        )

    _run(run())


@app.command("sections")
def sections(
    filing_id: Annotated[str, typer.Argument(help="OpenFilings filing ID.")],
    query: Annotated[
        str | None, typer.Option(help="Only show sections containing this text.")
    ] = None,
) -> None:
    """List navigable sections from a filing's cached or extracted Markdown."""

    async def run() -> None:
        async with OpenFilingsService.from_settings() as service:
            document = await service.get_filing_document(filing_id)
        selected = document.search(query) if query else document.sections
        for section in selected:
            typer.echo(f"{section.start_line}\tH{section.level}\t{section.title}")

    _run(run())


@app.command("inspect-document")
def inspect_document(
    path: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
    ],
    ocr: Annotated[
        OcrOption | None,
        typer.Option(help="Override OCR routing: auto, never, or always."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Optionally write extracted Markdown."),
    ] = None,
) -> None:
    """Extract a local document and report method, quality, and elapsed time."""

    try:
        settings = Settings.from_env()
        data = path.read_bytes()
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        started = time.perf_counter()
        result = extract_document(
            SourceDocument(
                data=data,
                media_type=media_type,
                source_url=path.resolve().as_uri(),
            ),
            ocr_mode=ocr.value if ocr else settings.ocr_mode,
            ocr_language=settings.ocr_language,
            ocr_dpi=settings.ocr_dpi,
            ocr_max_pages=settings.ocr_max_pages,
            ocr_executable=settings.ocr_executable,
        )
    except OpenFilingsError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    elapsed_seconds = time.perf_counter() - started

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(result.markdown, encoding="utf-8")
    typer.echo(
        json.dumps(
            {
                "path": str(path),
                "media_type": media_type,
                "source_bytes": len(data),
                "elapsed_seconds": round(elapsed_seconds, 4),
                "extraction_method": result.method,
                "quality": result.quality.model_dump(mode="json"),
                "output": str(output) if output else None,
            },
            indent=2,
        )
    )


@cache_app.command("status")
def cache_status() -> None:
    """Show local cache record counts and disk usage."""

    async def run() -> None:
        async with OpenFilingsService.from_settings() as service:
            stats = service.cache_stats()
        typer.echo(
            f"Companies: {stats.companies}\n"
            f"Filings: {stats.filings}\n"
            f"Documents: {stats.documents}\n"
            f"Cached source documents: {stats.source_documents}\n"
            f"Financial reports: {stats.financial_reports}\n"
            f"Compressed Markdown: {_format_bytes(stats.compressed_content_bytes)}\n"
            f"Compressed source documents: "
            f"{_format_bytes(stats.compressed_source_bytes)}\n"
            f"Compressed financials: "
            f"{_format_bytes(stats.compressed_financial_bytes)}\n"
            f"SQLite files: {_format_bytes(stats.database_bytes)}"
        )

    _run(run())


@cache_app.command("prune")
def cache_prune(
    max_mb: Annotated[
        int,
        typer.Option(min=0, help="Maximum compressed Markdown cache size."),
    ] = 512,
) -> None:
    """Remove the oldest cached documents until the requested limit is met."""

    async def run() -> None:
        async with OpenFilingsService.from_settings() as service:
            result = service.prune_cache(max_mb=max_mb)
        compressed_bytes = (
            result.after.compressed_content_bytes
            + result.after.compressed_source_bytes
            + result.after.compressed_financial_bytes
        )
        typer.echo(
            f"Removed {result.removed_documents} Markdown document(s), "
            f"{result.removed_source_documents} source document(s), and "
            f"{result.removed_financial_reports} financial report(s). "
            f"Compressed cache is now "
            f"{_format_bytes(compressed_bytes)}."
        )

    _run(run())


@app.command()
def resolve(
    query: Annotated[str, typer.Argument(help="Ticker, ISIN, LEI, or company name.")],
    market: Annotated[
        str | None,
        typer.Option(
            help="Two-letter market code (UK, NL, FR, ...) to disambiguate an "
            "LEI-keyed market."
        ),
    ] = None,
) -> None:
    """Resolve a ticker or ISIN to an OpenFilings company ID."""

    async def run() -> None:
        async with OpenFilingsService.from_settings() as service:
            company = await service.resolve_company(query, market=market)
        if company is None:
            typer.echo(f"No company matched {query!r}.", err=True)
            raise typer.Exit(code=1)
        typer.echo(
            json.dumps(company.model_dump(mode="json"), indent=2, sort_keys=True)
        )

    _run(run())


@app.command()
def coverage(
    tickers: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            help="Ticker list file: TICKER[,MARKET[,NAME]] per line.",
        ),
    ],
    market: Annotated[
        str | None,
        typer.Option(help="Default market for rows that omit one."),
    ] = None,
    concurrency: Annotated[int, typer.Option(min=1, max=16)] = 4,
    statements: Annotated[
        bool,
        typer.Option(
            "--statements/--no-statements",
            help="Extract the latest accounts filing's statements per ticker.",
        ),
    ] = True,
    json_out: Annotated[
        Path | None,
        typer.Option("--json", help="Write the full report as JSON."),
    ] = None,
    csv_out: Annotated[
        Path | None,
        typer.Option("--csv", help="Write one row per ticker as CSV."),
    ] = None,
) -> None:
    """Sweep a ticker list: resolve, list filings, extract statements."""

    async def run() -> None:
        from openfilings.coverage import parse_ticker_list, render_summary, sweep

        entries = parse_ticker_list(tickers.read_text(encoding="utf-8"))
        if market is not None:
            default_market = market.strip().upper()
            entries = [
                (query, item_market or default_market, name)
                for query, item_market, name in entries
            ]
        async with OpenFilingsService.from_settings() as service:

            async def resolver(query: str, item_market: str | None) -> Any:
                return await service.resolve_company(query, market=item_market)

            report = await sweep(
                entries,
                service=service,
                resolver=resolver,
                concurrency=concurrency,
                with_statements=statements,
            )
        typer.echo(render_summary(report))
        if csv_out is not None:
            from openfilings.coverage import to_csv

            csv_out.parent.mkdir(parents=True, exist_ok=True)
            csv_out.write_text(to_csv(report), encoding="utf-8")
            typer.echo(f"Wrote {csv_out}.")
        if json_out is not None:
            from openfilings.coverage import to_json

            json_out.parent.mkdir(parents=True, exist_ok=True)
            json_out.write_text(to_json(report), encoding="utf-8")
            typer.echo(f"Wrote {json_out}.")

    _run(run())


@app.command()
def serve() -> None:
    """Start the OpenFilings MCP server over stdio."""

    from openfilings.server import run

    run()


def _run(coroutine: Coroutine[Any, Any, None]) -> None:
    try:
        asyncio.run(coroutine)
    except OpenFilingsError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if amount < 1024 or unit == "GB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")
