from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from openfilings.benchmarks import (
    ACCURACY_BENCHMARKS,
    AccuracyBenchmark,
    ReferenceFact,
    _assert_reference_facts,
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


def _pinned_facts_financials() -> FilingFinancials:
    """Fixture carrying exactly the Volvo/Keppel source-reviewed figures."""

    def values(
        end_date: date,
        value: Decimal,
        unit: str,
        provenance: str = "tagged_xbrl",
        confidence: int = 100,
    ) -> tuple[FinancialValue, ...]:
        return (
            FinancialValue(
                period=ReportingPeriod(
                    id=f"instant-{end_date.isoformat()}",
                    end_date=end_date,
                    kind="instant",
                    fiscal_period="instant",
                ),
                value=value,
                unit=unit,
                provenance=provenance,  # type: ignore[arg-type]
                confidence=confidence,
            ),
        )

    return FilingFinancials(
        filing_id="pins",
        company_id="pins-company",
        source_url="https://example.test/pins",
        fact_count=4,
        sha256="b" * 64,
        statements=(
            FinancialStatement(
                statement_type="balance_sheet",
                title="Balance sheet",
                line_items=(
                    FinancialLineItem(
                        code="total_assets",
                        name="Total assets",
                        concept="ifrs-full:Assets",
                        values=values(
                            date(2024, 12, 31),
                            Decimal("714564000000"),
                            "iso4217:SEK",
                        ),
                    ),
                    FinancialLineItem(
                        code="total_equity",
                        name="Total equity",
                        concept="ifrs-full:Equity",
                        values=values(
                            date(2024, 12, 31),
                            Decimal("197361000000"),
                            "iso4217:SEK",
                        ),
                    ),
                    FinancialLineItem(
                        code="total_equity",
                        name="Net assets",
                        concept="pdf-label:net-assets",
                        values=values(
                            date(2025, 12, 31),
                            Decimal("11186180000"),
                            "SGD",
                            provenance="pdf_table",
                            confidence=75,
                        ),
                    ),
                    FinancialLineItem(
                        code="noncurrent_liabilities",
                        name="Non-current liabilities",
                        concept="pdf-label:non-current-liabilities",
                        values=values(
                            date(2025, 12, 31),
                            Decimal("10122923000"),
                            "SGD",
                            provenance="pdf_table",
                            confidence=75,
                        ),
                    ),
                ),
            ),
        ),
    )


def test_pinned_volvo_and_keppel_facts_match_source_reviewed_figures() -> None:
    """The pins themselves are the load-bearing assertions: codes, values,
    units, and concepts must equal the independently transcribed figures,
    and a statement set carrying those figures must satisfy the checker."""

    by_label = {benchmark.label: benchmark for benchmark in ACCURACY_BENCHMARKS}
    volvo = next(
        benchmark for label, benchmark in by_label.items() if "Volvo" in label
    )
    keppel = next(
        benchmark for label, benchmark in by_label.items() if "Keppel" in label
    )
    assert {
        (fact.code, fact.period_end, fact.value, fact.unit, fact.concept)
        for fact in volvo.facts
    } == {
        ("total_assets", date(2024, 12, 31), Decimal("714564000000"), "iso4217:SEK", "ifrs-full:Assets"),
        ("total_equity", date(2024, 12, 31), Decimal("197361000000"), "iso4217:SEK", "ifrs-full:Equity"),
    }
    assert {
        (fact.code, fact.period_end, fact.value, fact.unit, fact.concept)
        for fact in keppel.facts
    } == {
        ("total_equity", date(2025, 12, 31), Decimal("11186180000"), "SGD", "pdf-label:net-assets"),
        ("noncurrent_liabilities", date(2025, 12, 31), Decimal("10122923000"), "SGD", "pdf-label:non-current-liabilities"),
    }

    financials = _pinned_facts_financials()
    _assert_reference_facts(financials, volvo)
    _assert_reference_facts(financials, keppel)


def test_pinned_facts_checker_rejects_a_wrong_figure() -> None:
    """A perturbed figure must fail loudly, proving the pins above guard
    values rather than merely existing."""

    by_label = {benchmark.label: benchmark for benchmark in ACCURACY_BENCHMARKS}
    volvo = next(
        benchmark for label, benchmark in by_label.items() if "Volvo" in label
    )
    financials = _pinned_facts_financials()
    statements = financials.statements[0].line_items
    perturbed = tuple(
        item.model_copy(
            update={
                "values": tuple(
                    value.model_copy(update={"value": value.value + 1})
                    for value in item.values
                )
            }
        )
        for item in statements
    )
    wrong = financials.model_copy(
        update={
            "statements": (
                financials.statements[0].model_copy(
                    update={"line_items": perturbed}
                ),
            )
        }
    )
    with pytest.raises(AssertionError, match="expected"):
        _assert_reference_facts(wrong, volvo)
