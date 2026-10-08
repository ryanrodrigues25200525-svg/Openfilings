from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from openfilings.smoke import (
    SMOKE_CASES,
    SmokeCase,
    _balance_sheet_identity,
    run_live_smoke,
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


def test_ci_workflows_enforce_tests_security_and_keyless_live_checks() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    security = (ROOT / ".github/workflows/security.yml").read_text(encoding="utf-8")
    live = (ROOT / ".github/workflows/live-smoke.yml").read_text(encoding="utf-8")

    assert "uv sync --locked --all-extras --dev" in ci
    assert "uv run ruff check ." in ci
    assert "uv run ruff format --check ." in ci
    assert "uv run pytest" in ci
    assert "uv build" in ci
    assert "codeql-action/analyze@7211b7c8077ea37d8641b6271f6a365a22a5fbfa" in security
    assert "pip-audit" in security
    assert "openfilings.smoke" in live
    assert "EDINET_API_KEY" not in live


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
