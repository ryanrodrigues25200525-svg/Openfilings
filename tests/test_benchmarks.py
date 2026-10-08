from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from openfilings.benchmarks import (
    ACCURACY_BENCHMARKS,
    AccuracyBenchmark,
    ReferenceFact,
    run_live_accuracy_benchmarks,
)
from openfilings.models import (
    FilingFinancials,
    FinancialLineItem,
    FinancialStatement,
    FinancialValue,
    ReportingPeriod,
)


def _financials(value: Decimal = Decimal("100")) -> FilingFinancials:
    return FilingFinancials(
        filing_id="fixture",
        company_id="fixture-company",
        source_url="https://example.test/filing",
        fact_count=1,
        sha256="a" * 64,
        statements=(
            FinancialStatement(
                statement_type="balance_sheet",
                title="Balance sheet",
                line_items=(
                    FinancialLineItem(
                        code="total_assets",
                        name="Total assets",
                        concept="ifrs-full:Assets",
                        values=(
                            FinancialValue(
                                period=ReportingPeriod(
                                    id="instant",
                                    end_date=date(2025, 12, 31),
                                    kind="instant",
                                    fiscal_period="instant",
                                ),
                                value=value,
                                unit="USD",
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )


class _FakeService:
    async def get_filing_financials(self, *_: object, **__: object) -> FilingFinancials:
        return _financials()


@pytest.mark.asyncio
async def test_accuracy_benchmark_checks_source_reviewed_values() -> None:
    benchmark = AccuracyBenchmark(
        label="fixture",
        filing_id="fixture",
        filing_url="https://example.test/filing",
        facts=(
            ReferenceFact(
                "total_assets",
                "balance_sheet",
                date(2025, 12, 31),
                Decimal("100"),
                "USD",
                "ifrs-full:Assets",
            ),
        ),
    )

    result = await run_live_accuracy_benchmarks(
        _FakeService(), benchmarks=(benchmark,), timeout_seconds=1
    )

    assert result[0].facts_checked == 1


@pytest.mark.asyncio
async def test_accuracy_benchmark_reports_a_value_mismatch() -> None:
    benchmark = AccuracyBenchmark(
        label="fixture",
        filing_id="fixture",
        filing_url="https://example.test/filing",
        facts=(
            ReferenceFact(
                "total_assets",
                "balance_sheet",
                date(2025, 12, 31),
                Decimal("101"),
                "USD",
                "ifrs-full:Assets",
            ),
        ),
    )

    with pytest.raises(RuntimeError, match=r"expected 101 USD.*got 100 USD"):
        await run_live_accuracy_benchmarks(
            _FakeService(), benchmarks=(benchmark,), timeout_seconds=1
        )


def test_benchmarks_cover_volvo_and_keppel_regression_guards() -> None:
    """Issue #7: Sweden (AB Volvo) and Singapore (Keppel) are named
    regression guards whose filings derive a total, so the smoke identity
    check cannot fire on them. Pinned reference facts are the mechanism
    built for exactly this."""

    labels = [benchmark.label for benchmark in ACCURACY_BENCHMARKS]
    assert any("Volvo" in label for label in labels)
    assert any("Keppel" in label for label in labels)
