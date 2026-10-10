"""Bounded live checks for keyless regulator integrations.

Beyond "did search/filings return something," each financial-checked case
fetches the latest filing's financials and verifies the fundamental
balance-sheet identity (assets = liabilities + equity) holds. Spot-checking
by hand is how every real bug this project has found actually surfaced -
this turns that into a repeatable, scheduled check instead of something
that only happens when someone thinks to look.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from openfilings.service import OpenFilingsService

# Liability-side buckets the identity can still be verified from when the
# filer tags no usable total (see _balance_sheet_identity).
_LIABILITY_COMPONENT_CODES = ("current_liabilities", "noncurrent_liabilities")
_SUBORDINATED_LIABILITIES_CODE = "subordinated_liabilities"
# A tiny relative tolerance absorbs rounding noise in source data (seen in
# real filings, e.g. a few cents on figures in the hundreds of billions) -
# not genuine reconciliation gaps, which are orders of magnitude larger.
_IDENTITY_TOLERANCE = Decimal("0.001")
# Proofs that rest on a source-tagged total_liabilities - the ones a case
# with require_source_balance_sheet=True accepts.
_HELD = "held"
_HELD_EXCLUDES_SUBORDINATED = "held (liabilities exclude subordinated liabilities)"
_HELD_DERIVED_COMPONENTS = "held (total liabilities derived; components foot)"
_SOURCE_TOTAL_IDENTITIES = frozenset({_HELD, _HELD_EXCLUDES_SUBORDINATED})


def identity_holds(identity: str) -> bool:
    """True when the identity check reconciled the balance sheet.

    A held proof can carry a parenthetical composition note (a filer whose
    liabilities subtotal excludes subordinated debt, or components that foot
    while the total itself is derived). Every one of them reconciles
    source-extracted facts, so the prefix decides - compare with
    identity_uses_source_totals() for the stricter question."""

    return identity.startswith("held")


def identity_uses_source_totals(identity: str) -> bool:
    """True when the proof rests on a source-tagged total liabilities figure."""

    return identity in _SOURCE_TOTAL_IDENTITIES


@dataclass(frozen=True, slots=True)
class SmokeCase:
    label: str
    query: str
    source: str
    check_financials: bool = True
    # Only enable this for sources whose selected filing is known to expose
    # all three totals as direct facts. Many valid filings tag a subset and
    # OpenFilings derives the remaining total, which is useful output but not
    # independent smoke-test evidence.
    require_source_balance_sheet: bool = False


@dataclass(frozen=True, slots=True)
class SmokeResult:
    label: str
    company_id: str
    filing_id: str | None
    identity_check: str


SMOKE_CASES = (
    SmokeCase("UK FCA NSM", "Tesco PLC", "fca_nsm"),
    SmokeCase("ESEF Netherlands", "ASML", "esef"),
    SmokeCase("ESEF France", "TotalEnergies", "esef"),
    SmokeCase("ESEF Spain", "Iberdrola", "esef"),
    SmokeCase("ESEF Italy", "Enel", "esef", require_source_balance_sheet=True),
    SmokeCase(
        "ESEF Denmark", "Novo Nordisk", "esef", require_source_balance_sheet=True
    ),
    # Regression guard: AB Volvo's disposal-group held-for-sale liabilities
    # bucket previously fell outside the current+noncurrent derivation.
    SmokeCase("ESEF Sweden", "Aktiebolaget Volvo", "esef"),
    SmokeCase("ESEF Finland", "Nokia", "esef", require_source_balance_sheet=True),
    SmokeCase("ESEF Norway", "Equinor", "esef", require_source_balance_sheet=True),
    SmokeCase("ESEF Poland", "Orlen", "esef", require_source_balance_sheet=True),
    SmokeCase("ESEF Belgium", "KBC", "esef", require_source_balance_sheet=True),
    # OMV's recent filings omit a tagged total_liabilities line item.
    SmokeCase("ESEF Austria", "Verbund", "esef"),
    SmokeCase(
        "ESEF Luxembourg", "ArcelorMittal", "esef", require_source_balance_sheet=True
    ),
    # filings.xbrl.org indexes EDP Renovaveis under both ES and PT for the same
    # LEI, and the bare query "EDP" ranks the Spain-attributed record first -
    # name the Portuguese parent so this case actually exercises Portugal.
    SmokeCase("ESEF Portugal", "EDP, S.A.", "esef", require_source_balance_sheet=True),
    SmokeCase("Brazil CVM", "Petrobras", "cvm"),
    # Regression guard: Keppel's "net assets" presentation previously
    # misread its non-current liabilities subtotal.
    SmokeCase("Singapore SGX", "Keppel Ltd", "sgx"),
    SmokeCase("Mexico BMV", "AMX", "bmv", require_source_balance_sheet=True),
    SmokeCase("India NSE", "RELIANCE", "nse", require_source_balance_sheet=True),
    SmokeCase("Peru SMV", "Alicorp", "smv", require_source_balance_sheet=True),
    # A bank specifically, to exercise the CUIF structured balance-sheet
    # path rather than just the PDF-covered income statement. Banco de Bogota
    # was the original pick and had to be replaced: SFC's issuer registry now
    # returns no "ACCION" (listed equity) records for it at all, only bonds
    # and certificates, so it is no longer a listed-equity issuer this
    # library covers. Davivienda is the stable equivalent.
    SmokeCase("Colombia SFC", "Davivienda", "sfc", require_source_balance_sheet=True),
    SmokeCase("Turkey KAP", "Turkcell", "kap", require_source_balance_sheet=True),
    # ASX publishes a keyless listed-company directory but no keyless filing
    # history: its announcements feed accepts no issuer filter, so financials
    # aren't checked here. See the module docstring in adapters/asx.py.
    SmokeCase("Australia ASX", "BHP", "asx", check_financials=False),
    # SEDAR+ has no public filing-search API by design; only company
    # discovery is keyless, so financials aren't checked here.
    SmokeCase("Canada TSX", "Shopify", "sedar", check_financials=False),
    # Company search is keyless; the filing API needs a free EDINET key
    # not available in CI, so financials aren't checked here.
    SmokeCase("Japan EDINET", "Toyota", "edinet", check_financials=False),
)


async def run_live_smoke(
    service: Any,
    *,
    cases: tuple[SmokeCase, ...] = SMOKE_CASES,
    timeout_seconds: float = 240.0,
    concurrency: int = 4,
) -> tuple[SmokeResult, ...]:
    """Check one company, its latest filing, and (where applicable) that
    filing's balance-sheet identity per keyless source family."""

    if concurrency < 1:
        raise ValueError("concurrency must be at least one")
    semaphore = asyncio.Semaphore(concurrency)

    async def check(case: SmokeCase) -> SmokeResult | Exception:
        try:
            async with semaphore:
                return await asyncio.wait_for(
                    _run_case(service, case), timeout=timeout_seconds
                )
        except Exception as exc:
            return exc

    completed = await asyncio.gather(*(check(case) for case in cases))
    results: list[SmokeResult] = []
    failures: list[str] = []
    for case, result in zip(cases, completed, strict=True):
        if isinstance(result, Exception):
            failures.append(f"{case.label}: {type(result).__name__}: {result}")
        else:
            results.append(result)
    if failures:
        raise RuntimeError("Live smoke failures:\n" + "\n".join(failures))
    return tuple(results)


async def _run_case(service: Any, case: SmokeCase) -> SmokeResult:
    company = await service.company(case.query, source=case.source)
    if not case.check_financials:
        return SmokeResult(case.label, company.id, None, "search_only")
    filings = await company.get_filings(source=case.source, limit=5)
    if filings.latest() is None:
        raise RuntimeError(f"{company.id} returned no filings")
    # A source can list several recent "accounts"-category disclosures
    # where only some actually carry extractable data (e.g. FCA NSM tags a
    # short announcement pointing at the full report the same way it tags
    # the report itself). Any one of the most recent few succeeding proves
    # the source is healthy; only fail if none of them do.
    last_error: Exception | None = None
    for filing in filings[:3]:
        try:
            financials = await filing.financials(refresh=True)
        except Exception as exc:  # reported in the final error, not swallowed
            last_error = exc
            continue
        identity = _balance_sheet_identity(financials)
        if case.require_source_balance_sheet and not identity_uses_source_totals(
            identity
        ):
            raise RuntimeError(f"{case.label} ({company.id}/{filing.id}): {identity}")
        return SmokeResult(case.label, company.id, filing.id, identity)
    raise RuntimeError(
        f"{case.label} ({company.id}): no usable financials in the "
        f"{min(3, len(filings))} most recent filings ({last_error})"
    )


def _source_values(by_code: dict[str, Any], code: str) -> dict[date, Any]:
    """Newest-period source values for one code: dimensionless, not derived."""

    item = by_code.get(code)
    if item is None:
        return {}
    return {
        value.period.end_date: value
        for value in item.values
        if not value.dimensions and value.provenance != "derived"
    }


def _balance_sheet_identity(financials: Any) -> str:
    balance = financials.balance_sheet()
    if balance is None:
        return "not_applicable (no balance sheet extracted)"
    by_code = {item.code: item for item in balance.line_items}

    # Validate one common, dimensionless period. Comparing each item's first
    # value can silently mix fiscal years, while derived totals only prove the
    # arithmetic used to create them rather than the extraction's accuracy.
    assets_values = _source_values(by_code, "total_assets")
    equity_values = _source_values(by_code, "total_equity")
    liability_periods: set[date] = set()
    for code in ("total_liabilities", *_LIABILITY_COMPONENT_CODES):
        liability_periods |= set(_source_values(by_code, code))
    common_periods = (set(assets_values) & set(equity_values)) & liability_periods
    if not common_periods:
        return "not_applicable (no common source-extracted balance-sheet period)"
    period = max(common_periods)
    assets = assets_values[period].value
    equity = equity_values[period].value
    tolerance = abs(assets) * _IDENTITY_TOLERANCE

    liabilities = _source_values(by_code, "total_liabilities").get(period)
    if liabilities is not None:
        combined = liabilities.value + equity
        if abs(combined - assets) <= tolerance:
            return _HELD
        # Storebrand Livsforsikring's insurer presentation tags Liabilities
        # *excluding* subordinated debt (2024: 811,744m + 9,979m + equity
        # 17,810m = assets 839,533m), so its own facts foot only with the
        # subordinated leg added. This applies only when the plain identity
        # fails and the extra source leg closes it exactly - filers that fold
        # subordinated debt into Liabilities already hold above and never
        # reach here. Verified live on its 2024 and 2023 ESEF reports.
        subordinated = _source_values(by_code, _SUBORDINATED_LIABILITIES_CODE).get(
            period
        )
        if (
            subordinated is not None
            and abs(liabilities.value + subordinated.value + equity - assets)
            <= tolerance
        ):
            return _HELD_EXCLUDES_SUBORDINATED
        return f"GAP at {period}: liabilities+equity={combined} vs assets={assets}"

    # No source-tagged total. Checking the derived total_liabilities against
    # its own inputs would prove only the derivation's arithmetic, but the
    # untagged side usually is not the components: when the filer tags both
    # current and non-current liabilities, they are independent source facts
    # and the identity is still verifiable from them - and a real mismatch
    # here means the components leave a bucket out (IFRS 5 disposal-group
    # liabilities, for example), which is worth reporting rather than hiding.
    components = [
        _source_values(by_code, code).get(period) for code in _LIABILITY_COMPONENT_CODES
    ]
    if any(component is None for component in components):
        return "not_applicable (total liabilities not tagged and components incomplete)"
    combined = equity + sum(
        component.value for component in components if component is not None
    )
    if abs(combined - assets) <= tolerance:
        return _HELD_DERIVED_COMPONENTS
    return f"GAP at {period}: current+noncurrent+equity={combined} vs assets={assets}"


async def _main() -> None:
    async with OpenFilingsService.from_settings() as service:
        results = await run_live_smoke(service)
    for result in results:
        print(
            f"PASS\t{result.label}\t{result.company_id}\t"
            f"{result.filing_id}\t{result.identity_check}"
        )
    print("\n" + summarize_results(results))


def summarize_results(results: list[SmokeResult]) -> str:
    """One human-readable tally for a smoke run.

    "PASS ... not_applicable" only means the source responded and something
    was extracted - the identity itself proved nothing, because the filing
    tagged neither a total liabilities figure nor both of its components.
    Count that explicitly so a green run is not mistaken for full
    reconciliation coverage, and count reported gaps separately: a GAP is
    not missing coverage but a contradicted sum (Volvo's 2024 ESEF report,
    whose IFRS 5 held-for-sale liability bucket sits in neither the current
    nor the non-current subtotal).
    """
    held = sum(1 for result in results if identity_holds(result.identity_check))
    gaps = sum(
        1
        for result in results
        if not identity_holds(result.identity_check)
        and result.identity_check.startswith("GAP")
    )
    search_only = sum(1 for result in results if result.identity_check == "search_only")
    unverified = len(results) - held - search_only - gaps
    return (
        f"{len(results)} cases: {held} balance-sheet identity verified, "
        f"{gaps} with a reported identity gap, {unverified} extracted but "
        f"unverifiable (no verifiable balance sheet), "
        f"{search_only} company-search only."
    )


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
