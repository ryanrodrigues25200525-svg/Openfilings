from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from openfilings.smoke import (
    SMOKE_CASES,
    SmokeCase,
    SmokeResult,
    _balance_sheet_identity,
    identity_holds,
    identity_uses_source_totals,
    run_live_smoke,
    summarize_results,
)

ROOT = Path(__file__).resolve().parents[1]


def test_keyless_smoke_matrix_covers_every_keyless_source() -> None:
    assert {case.source for case in SMOKE_CASES} == {
        "fca_nsm",
        "esef",
        "cvm",
        "sgx",
        "bmv",
        "nse",
        "smv",
        "sfc",
        "sedar",
        "edinet",
        "kap",
        "asx",
    }
    assert all(case.query and case.label for case in SMOKE_CASES)
    # SEDAR+, EDINET and ASX have no keyless filing-search API (SEDAR+ blocks
    # automated queries; EDINET's filing API needs a free key; ASX's
    # announcements feed accepts no issuer filter), so only company discovery
    # is checked for them.
    assert {case.source for case in SMOKE_CASES if not case.check_financials} == {
        "sedar",
        "edinet",
        "asx",
    }


@pytest.mark.asyncio
async def test_live_smoke_runner_checks_company_and_filing_resolution() -> None:
    service = _FakeService()

    cases = tuple(
        SmokeCase(
            case.label,
            case.query,
            case.source,
            require_source_balance_sheet=False,
        )
        for case in SMOKE_CASES[:2]
    )
    results = await run_live_smoke(service, cases=cases, timeout_seconds=1)

    assert len(results) == 2
    assert results[0].company_id == "company-fca_nsm"
    assert results[0].filing_id == "filing-fca_nsm"
    assert results[0].identity_check == "not_applicable (no balance sheet extracted)"
    assert service.sources == ["fca_nsm", "esef"]


@pytest.mark.asyncio
async def test_live_smoke_runner_validates_concurrency() -> None:
    with pytest.raises(ValueError, match="concurrency"):
        await run_live_smoke(_FakeService(), cases=(), concurrency=0)


def test_smoke_summary_counts_every_outcome_exactly_once() -> None:
    def result(label: str, identity_check: str) -> SmokeResult:
        return SmokeResult(label, f"company-{label}", f"filing-{label}", identity_check)

    results = [
        result("a", "held"),
        result("b", "held (liabilities exclude subordinated liabilities)"),
        result("c", "held (total liabilities derived; components foot)"),
        result(
            "d",
            "GAP at 2024-12-31: current+noncurrent+equity=517203 vs assets=472514",
        ),
        result("e", "not_applicable (no balance sheet extracted)"),
        result(
            "f",
            "not_applicable (total liabilities not tagged and components incomplete)",
        ),
        result("g", "search_only"),
    ]

    summary = summarize_results(results)

    assert summary.startswith("7 cases: 3 balance-sheet identity verified")
    assert "1 with a reported identity gap" in summary
    assert "2 extracted but unverifiable" in summary
    assert summary.endswith("1 company-search only.")
    # Every result lands in exactly one bucket: an identity string matching
    # none of them would silently inflate the unverifiable count.
    unknown = summarize_results([result("h", "something new")])
    assert "0 balance-sheet identity verified" in unknown
    assert "1 extracted but unverifiable" in unknown


def test_balance_sheet_identity_requires_common_source_values() -> None:
    class _Value:
        def __init__(self, end_date: date, value: int, provenance: str = "tagged_xbrl"):
            self.period = type("Period", (), {"end_date": end_date})()
            self.value = value
            self.provenance = provenance
            self.dimensions = ()

    class _Item:
        def __init__(self, code: str, values: list[_Value]):
            self.code = code
            self.values = values

    class _Balance:
        line_items = (
            _Item("total_assets", [_Value(date(2025, 12, 31), 100)]),
            _Item("total_liabilities", [_Value(date(2024, 12, 31), 60)]),
            _Item("total_equity", [_Value(date(2025, 12, 31), 40)]),
        )

    class _Financials:
        @staticmethod
        def balance_sheet() -> _Balance:
            return _Balance()

    assert _balance_sheet_identity(_Financials()) == (
        "not_applicable (no common source-extracted balance-sheet period)"
    )


class _FakePeriod:
    def __init__(self, end_date: date) -> None:
        self.end_date = end_date


class _FakeValue:
    def __init__(
        self, end_date: date, value: int, provenance: str = "tagged_xbrl"
    ) -> None:
        self.period = _FakePeriod(end_date)
        self.value = value
        self.provenance = provenance
        self.dimensions = ()


class _FakeItem:
    def __init__(self, code: str, values: tuple[_FakeValue, ...]) -> None:
        self.code = code
        self.values = values


def _identity(*items: tuple[str, tuple[_FakeValue, ...]]) -> str:
    """Run _balance_sheet_identity over (code, values) balance-sheet facts."""

    class _Balance:
        line_items = tuple(_FakeItem(code, values) for code, values in items)

    class _Financials:
        @staticmethod
        def balance_sheet() -> _Balance:
            return _Balance()

    return _balance_sheet_identity(_Financials())


def test_balance_sheet_identity_accepts_the_subordinated_liability_variant() -> None:
    """Storebrand Livsforsikring's insurer presentation tags Liabilities
    *excluding* subordinated debt, so liabilities + equity alone misses total
    assets by exactly the tagged subordinated figure."""

    period = date(2024, 12, 31)
    assert (
        _identity(
            ("total_assets", (_FakeValue(period, 839_533),)),
            ("total_liabilities", (_FakeValue(period, 811_744),)),
            ("subordinated_liabilities", (_FakeValue(period, 9_979),)),
            ("total_equity", (_FakeValue(period, 17_810),)),
        )
        == "held (liabilities exclude subordinated liabilities)"
    )


def test_balance_sheet_identity_reports_a_gap_the_subordinated_leg_misses() -> None:
    """The subordinated leg only closes *exactly*: anything short of that is a
    real gap and must not be credited as a variant proof."""

    period = date(2024, 12, 31)
    gap = _identity(
        ("total_assets", (_FakeValue(period, 839_533),)),
        ("total_liabilities", (_FakeValue(period, 811_744),)),
        ("subordinated_liabilities", (_FakeValue(period, 100),)),
        ("total_equity", (_FakeValue(period, 17_810),)),
    )

    assert gap.startswith("GAP at 2024-12-31: liabilities+equity=")
    assert "subordinated" not in gap


def test_balance_sheet_identity_credits_footing_current_noncurrent_components() -> None:
    """With no source total_liabilities, both components are independent source
    facts and verify the identity; a derived total must not be credited for
    it."""

    period = date(2025, 12, 31)
    assert (
        _identity(
            ("total_assets", (_FakeValue(period, 100),)),
            ("total_liabilities", (_FakeValue(period, 70, "derived"),)),
            ("current_liabilities", (_FakeValue(period, 30),)),
            ("noncurrent_liabilities", (_FakeValue(period, 40),)),
            ("total_equity", (_FakeValue(period, 30),)),
        )
        == "held (total liabilities derived; components foot)"
    )


def test_balance_sheet_identity_reports_a_component_gap() -> None:
    """Volvo's 2024 ESEF report tags current 261,298m and non-current
    211,216m liabilities, assets 714,564m and equity 197,361m; the missing
    44,689m is the IFRS 5 held-for-sale liability bucket, which sits in
    neither subtotal. The gap must be reported, not hidden as not_applicable."""

    period = date(2024, 12, 31)
    assert _identity(
        ("total_assets", (_FakeValue(period, 714_564),)),
        ("current_liabilities", (_FakeValue(period, 261_298),)),
        ("noncurrent_liabilities", (_FakeValue(period, 211_216),)),
        ("total_equity", (_FakeValue(period, 197_361),)),
    ) == ("GAP at 2024-12-31: current+noncurrent+equity=669875 vs assets=714564")


def test_balance_sheet_identity_stays_silent_on_incomplete_components() -> None:
    """One component alone proves nothing: a filing that tags no total and
    only some of the components must not be accused of a gap."""

    period = date(2025, 12, 31)
    assert _identity(
        ("total_assets", (_FakeValue(period, 100),)),
        ("current_liabilities", (_FakeValue(period, 30),)),
        ("total_equity", (_FakeValue(period, 70),)),
    ) == ("not_applicable (total liabilities not tagged and components incomplete)")


def test_identity_proof_classification_keeps_variants_and_gaps_apart() -> None:
    """identity_holds accepts every reconciliation proof; only proofs resting
    on a source-tagged total satisfy the stricter source-total question a
    require_source_balance_sheet case asks."""

    assert identity_holds("held")
    assert identity_holds("held (liabilities exclude subordinated liabilities)")
    assert identity_holds("held (total liabilities derived; components foot)")
    assert not identity_holds("GAP at 2024-12-31: liabilities+equity=1 vs assets=2")
    assert not identity_holds("not_applicable (no balance sheet extracted)")
    assert identity_uses_source_totals("held")
    assert identity_uses_source_totals(
        "held (liabilities exclude subordinated liabilities)"
    )
    assert not identity_uses_source_totals(
        "held (total liabilities derived; components foot)"
    )


def test_ci_workflows_enforce_tests_security_and_keyless_live_checks() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    security = (ROOT / ".github/workflows/security.yml").read_text(encoding="utf-8")
    live = (ROOT / ".github/workflows/live-smoke.yml").read_text(encoding="utf-8")
    probe = (ROOT / ".github/workflows/live-probe.yml").read_text(encoding="utf-8")

    assert "uv sync --locked --all-extras --dev" in ci
    assert "uv run ruff check ." in ci
    assert "uv run ruff format --check ." in ci
    assert "uv run pytest" in ci
    assert "uv build" in ci
    assert "codeql-action/analyze@f52b05f4acaaa234e44466e66d29050e135ea9ef" in security
    assert "pip-audit" in security
    assert "openfilings.smoke" in live
    assert "EDINET_API_KEY" not in live
    # The monthly multi-issuer probe (issue #9) is a separate scheduled job:
    # slower than smoke, monthly cadence, never gated on secrets.
    assert "openfilings.probe" in probe
    assert "EDINET_API_KEY" not in probe
    assert "DART_API_KEY" not in probe


class _FakeFinancials:
    @staticmethod
    def balance_sheet() -> None:
        return None


class _FakeFiling:
    def __init__(self, source: str) -> None:
        self.id = f"filing-{source}"

    async def financials(self, **_: object) -> _FakeFinancials:
        return _FakeFinancials()


class _FakeFilings:
    def __init__(self, source: str) -> None:
        self._filing = _FakeFiling(source)

    def latest(self) -> _FakeFiling:
        return self._filing

    def __getitem__(self, index: slice) -> list[_FakeFiling]:
        return [self._filing][index]


class _FakeCompany:
    def __init__(self, source: str) -> None:
        self.id = f"company-{source}"
        self._source = source

    async def get_filings(self, **_: object) -> _FakeFilings:
        return _FakeFilings(self._source)


class _FakeService:
    def __init__(self) -> None:
        self.sources: list[str] = []

    async def company(self, _: str, *, source: str) -> _FakeCompany:
        self.sources.append(source)
        return _FakeCompany(source)
