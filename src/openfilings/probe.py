"""Bounded monthly multi-issuer probe (issue #9).

The weekly smoke suite checks one issuer per market: it proves a regulator
endpoint still responds and one document parses, not that a market works.
Probing two issuers per market that the smoke suite never touches found four
defects smoke could not structurally have caught (India's crore/lakh scale,
dropped non-controlling interests, non-ASCII search, Peru's one-year issuer
universe - all fixed). That probe was a throwaway script, so this module
commits it as a repeatable check, deliberately separate from the smoke suite
because it is slower and its purpose is different.

Bounded and polite by design: 2-3 reviewed issuers per market, none of them
the smoke issuer, resolving each company, listing its filings, extracting
one filing's financials, and classifying the outcome per issuer as
verified / unverifiable / failed rather than pass/fail - so "unverifiable"
stays visible instead of reading as success. Monthly cadence is enough;
see ``.github/workflows/live-probe.yml``.

Offline behaviour matches the smoke suite's contract: network failures mark
the issuer failed with the error, and the module runner reports instead of
raising when the network itself is unreachable, so CI treats "offline" as
skipped rather than red.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from openfilings.service import OpenFilingsService
from openfilings.smoke import _balance_sheet_identity

ProbeOutcome = str  # "verified" | "unverifiable" | "failed"


@dataclass(frozen=True, slots=True)
class ProbeCase:
    """One reviewed issuer: a market plus a query naming a company in it.

    Queries must name issuers the smoke suite never touches (see
    ``SMOKE_CASES`` in ``openfilings.smoke``), so this probe exercises
    different registry entries, document shapes, and name spellings.
    ``check_financials=False`` marks markets whose keyless surface is
    company-discovery only (no filing search to extract from).
    """

    label: str
    query: str
    source: str
    check_financials: bool = True


@dataclass(frozen=True, slots=True)
class ProbeResult:
    label: str
    query: str
    company_id: str | None
    filing_id: str | None
    outcome: ProbeOutcome
    detail: str


# Reviewed list, 2-3 issuers per keyless market, none of them the smoke
# issuer. Each was chosen to differ from its smoke counterpart along the
# axis that once hid a defect: a different document path (India XBRL vs
# PDF, Colombia bank vs the smoke bank), a name with diacritics the
# registry spells differently, or simply a second large issuer so one
# company's clean filing cannot stand in for the market.
PROBE_CASES = (
    ProbeCase("UK FCA NSM (2)", "Unilever PLC", "fca_nsm"),
    ProbeCase("UK FCA NSM (3)", "BP PLC", "fca_nsm"),
    ProbeCase("ESEF Netherlands (2)", "Philips", "esef"),
    ProbeCase("ESEF Netherlands (3)", "ING", "esef"),
    ProbeCase("ESEF France (2)", "L'Oreal", "esef"),
    ProbeCase("ESEF France (3)", "Airbus", "esef"),
    ProbeCase("ESEF Spain (2)", "Telefonica", "esef"),
    ProbeCase("ESEF Spain (3)", "Repsol", "esef"),
    ProbeCase("ESEF Italy (2)", "Intesa Sanpaolo", "esef"),
    ProbeCase("ESEF Italy (3)", "Ferrari", "esef"),
    ProbeCase("ESEF Denmark (2)", "Danske Bank", "esef"),
    ProbeCase("ESEF Denmark (3)", "Carlsberg", "esef"),
    ProbeCase("ESEF Sweden (2)", "Ericsson", "esef"),
    ProbeCase("ESEF Sweden (3)", "Atlas Copco", "esef"),
    ProbeCase("ESEF Finland (2)", "Fortum", "esef"),
    ProbeCase("ESEF Finland (3)", "Kone", "esef"),
    ProbeCase("ESEF Norway (2)", "Telenor ASA", "esef"),
    ProbeCase("ESEF Norway (3)", "Orkla", "esef"),
    ProbeCase("ESEF Poland (2)", "PKO Bank Polski", "esef"),
    ProbeCase("ESEF Poland (3)", "PZU", "esef"),
    ProbeCase("ESEF Belgium (2)", "Solvay", "esef"),
    ProbeCase("ESEF Belgium (3)", "Umicore", "esef"),
    ProbeCase("ESEF Austria (2)", "Voestalpine", "esef"),
    ProbeCase("ESEF Austria (3)", "Raiffeisen Bank", "esef"),
    ProbeCase("ESEF Luxembourg (2)", "SES", "esef"),
    ProbeCase("ESEF Luxembourg (3)", "Luxempart", "esef"),
    ProbeCase("ESEF Portugal (2)", "EDP Renovaveis", "esef"),
    ProbeCase("ESEF Portugal (3)", "Jerónimo Martins", "esef"),
    ProbeCase("Brazil CVM (2)", "Vale", "cvm"),
    ProbeCase("Brazil CVM (3)", "Itaú Unibanco", "cvm"),
    ProbeCase("Singapore SGX (2)", "DBS", "sgx"),
    ProbeCase("Singapore SGX (3)", "Singtel", "sgx"),
    ProbeCase("Mexico BMV (2)", "Cemex", "bmv"),
    ProbeCase("Mexico BMV (3)", "Femsa", "bmv"),
    ProbeCase("India NSE (2)", "Tata Consultancy Services", "nse"),
    ProbeCase("India NSE (3)", "Infosys", "nse"),
    ProbeCase("Peru SMV (2)", "Cementos Pacasmayo", "smv"),
    ProbeCase("Peru SMV (3)", "Ferreycorp", "smv"),
    ProbeCase("Colombia SFC (2)", "Bancolombia", "sfc"),
    ProbeCase("Colombia SFC (3)", "Ecopetrol", "sfc"),
    ProbeCase("Turkey KAP (2)", "Ford Otosan", "kap"),
    ProbeCase("Turkey KAP (3)", "BIM", "kap"),
    ProbeCase("Australia ASX (2)", "CBA", "asx", check_financials=False),
    ProbeCase("Australia ASX (3)", "Woolworths", "asx", check_financials=False),
    ProbeCase("Canada TSX (2)", "Barrick Gold", "sedar", check_financials=False),
    ProbeCase(
        "Canada TSX (3)", "Royal Bank of Canada", "sedar", check_financials=False
    ),
    ProbeCase("Japan EDINET (2)", "Sony", "edinet", check_financials=False),
    ProbeCase("Japan EDINET (3)", "Nintendo", "edinet", check_financials=False),
)


async def run_live_probe(
    service: Any,
    *,
    cases: tuple[ProbeCase, ...] = PROBE_CASES,
    timeout_seconds: float = 240.0,
    concurrency: int = 4,
) -> tuple[ProbeResult, ...]:
    """Resolve, list filings for, and extract one filing per probe issuer.

    Never raises per-issuer errors: every case yields a ProbeResult, so the
    summary distinguishes verified (identity held), unverifiable
    (extracted but nothing to check against), and failed (with the error).
    """

    if concurrency < 1:
        raise ValueError("concurrency must be at least one")
    semaphore = asyncio.Semaphore(concurrency)

    async def check(case: ProbeCase) -> ProbeResult:
        try:
            async with semaphore:
                return await asyncio.wait_for(
                    _run_case(service, case), timeout=timeout_seconds
                )
        except Exception as exc:
            return ProbeResult(
                case.label,
                case.query,
                None,
                None,
                "failed",
                f"{type(exc).__name__}: {exc}",
            )

    return tuple(await asyncio.gather(*(check(case) for case in cases)))


async def _run_case(service: Any, case: ProbeCase) -> ProbeResult:
    try:
        company = await service.company(case.query, source=case.source)
    except Exception as exc:
        return ProbeResult(
            case.label,
            case.query,
            None,
            None,
            "failed",
            f"company resolution: {type(exc).__name__}: {exc}",
        )
    if not case.check_financials:
        return ProbeResult(
            case.label,
            case.query,
            company.id,
            None,
            "unverifiable",
            "search_only (no keyless filing search on this market)",
        )
    try:
        filings = await company.get_filings(source=case.source, limit=5)
    except Exception as exc:
        return ProbeResult(
            case.label,
            case.query,
            company.id,
            None,
            "failed",
            f"filing discovery: {type(exc).__name__}: {exc}",
        )
    if filings.latest() is None:
        return ProbeResult(
            case.label,
            case.query,
            company.id,
            None,
            "failed",
            "filing discovery: no filings listed",
        )
    # Mirror the smoke suite: any one of the most recent few filings
    # succeeding proves the market path works; only fail if none do.
    last_error = "no filings attempted"
    for filing in filings[:3]:
        try:
            financials = await filing.financials(refresh=True)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        identity = _balance_sheet_identity(financials)
        if identity == "held":
            return ProbeResult(
                case.label,
                case.query,
                company.id,
                filing.id,
                "verified",
                "balance-sheet identity held",
            )
        return ProbeResult(
            case.label,
            case.query,
            company.id,
            filing.id,
            "unverifiable",
            identity,
        )
    return ProbeResult(
        case.label,
        case.query,
        company.id,
        None,
        "failed",
        f"extraction: {last_error}",
    )


def summarize(results: tuple[ProbeResult, ...] | list[ProbeResult]) -> str:
    """One human-readable tally plus a line per non-verified issuer."""

    verified = sum(1 for item in results if item.outcome == "verified")
    unverifiable = sum(1 for item in results if item.outcome == "unverifiable")
    failed = sum(1 for item in results if item.outcome == "failed")
    lines = [
        f"{len(results)} issuers: {verified} verified, "
        f"{unverifiable} unverifiable, {failed} failed."
    ]
    for item in results:
        if item.outcome != "verified":
            lines.append(
                f"{item.outcome.upper()}\t{item.label}\t{item.query}\t"
                f"{item.company_id or '-'}\t{item.filing_id or '-'}\t"
                f"{item.detail}"
            )
    return "\n".join(lines)


def _looks_offline(results: tuple[ProbeResult, ...]) -> bool:
    """True when every case failed - the signature of no network, not N failures."""

    return bool(results) and all(item.outcome == "failed" for item in results)


async def _main() -> None:
    async with OpenFilingsService.from_settings() as service:
        results = await run_live_probe(service)
    if _looks_offline(results):
        print(
            "SKIP\tmulti-issuer probe: no network "
            f"({results[0].detail}); {len(results)} cases unattempted"
        )
        return
    for item in results:
        print(
            f"{item.outcome.upper()}\t{item.label}\t{item.query}\t"
            f"{item.company_id or '-'}\t{item.filing_id or '-'}\t{item.detail}"
        )
    failed = sum(1 for item in results if item.outcome == "failed")
    print(f"\n{summarize(results)}")
    if failed:
        raise SystemExit(
            f"multi-issuer probe: {failed} issuer(s) failed "
            "(unverifiable outcomes do not fail the run)"
        )


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
