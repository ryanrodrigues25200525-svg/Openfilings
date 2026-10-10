"""Deterministic accounting-identity checks via the finvariant library.

finvariant verifies that a set of financial statements is internally
consistent (assets = liabilities + equity, subtotals foot, cash ties out) -
exactly the kind of arithmetic error a PDF-heuristic extractor can produce
when it silently matches the wrong row. It never parses or builds
statements, only checks them, so this module's only job is mapping
OpenFilings' FinancialStatement/FinancialLineItem schema into finvariant's
canonical field names.

Only whole-statement subtotals are mapped, never sub-line items (cash,
receivables, inventory, ...): finvariant treats a partially-supplied
section as "not footing" rather than skipping it, and OpenFilings' PDF
extraction rarely captures every component of a section - supplying a
partial breakdown would produce a false failure, not a useful check.

One deliberate exception: ``total_liabilities`` is adjusted upward by
``subordinated_liabilities`` where the filer's own facts prove its
"Liabilities" subtotal excludes subordinated debt (see
``_fold_excluded_subordinated_liabilities``). Without that, a correct
insurer balance sheet reads as an accounting failure.
"""

from __future__ import annotations

import finvariant as fv

from openfilings.models import FilingFinancials, FinancialStatement

# OpenFilings line-item code -> (finvariant field, take absolute value).
# finvariant's income-statement expenses (cogs, tax) are positive
# magnitudes; OpenFilings' PDF-heuristic values often carry the source's
# own sign (frequently negative, shown in parentheses in the filing).
#
# No income-statement values are mapped. finvariant's gross-profit check
# assumes a single, directly comparable cost-of-sales subtotal. Real tagged
# reports need not use that presentation (for example, Tesco's FY2026
# ``CostOfSales`` and ``GrossProfit`` tags are both valid but do not foot to
# one another). Its operating-income and pretax-income checks also need
# operating_expenses/
# other_income/interest_expense to compute a correct expected value; when
# those are absent (which OpenFilings' headline-figure extraction always
# is - it has no such granularity), finvariant treats the missing minus-
# term as zero rather than skipping the check entirely, so it would
# compare operating_income_loss straight against gross_profit and
# profit_before_tax straight against operating_income - which are
# genuinely different figures for any real company, producing a false
# failure on every single filing rather than flagging a real bug.
# Mapping no income-statement fields is intentional: a false accounting
# warning is worse than an omitted low-confidence check in a research tool.
_INCOME_STATEMENT_FIELDS: dict[str, tuple[str, bool]] = {}
_BALANCE_SHEET_FIELDS: dict[str, tuple[str, bool]] = {
    "current_assets": ("total_current_assets", False),
    "noncurrent_assets": ("total_non_current_assets", False),
    "total_assets": ("total_assets", False),
    "current_liabilities": ("total_current_liabilities", False),
    "noncurrent_liabilities": ("total_non_current_liabilities", False),
    "total_liabilities": ("total_liabilities", False),
    "total_equity": ("total_equity", False),
}
# Net cash movement commonly includes FX translation, restricted-cash, and
# other reconciliation lines that OpenFilings intentionally does not force
# into its normalized headline model. Do not assert CFO + CFI + CFF equals
# the reported movement without those components.
_CASH_FLOW_FIELDS: dict[str, tuple[str, bool]] = {}
_FIELDS_BY_STATEMENT_TYPE = {
    "income_statement": _INCOME_STATEMENT_FIELDS,
    "balance_sheet": _BALANCE_SHEET_FIELDS,
    "cash_flow_statement": _CASH_FLOW_FIELDS,
}


def _statement_periods(
    statement: FinancialStatement, field_map: dict[str, tuple[str, bool]]
) -> dict[str, dict[str, float]]:
    by_period: dict[str, dict[str, float]] = {}
    for item in statement.line_items:
        mapping = field_map.get(item.code)
        if mapping is None:
            continue
        field_name, use_abs = mapping
        for value in item.values:
            if value.dimensions:
                # The extraction fell back to a dimension-qualified fact (a
                # segment or class split) because the statement tagged no
                # dimensionless one; asserting it as the consolidated total
                # manufactures failures.
                continue
            if value.provenance == "derived":
                # A derived total is our own arithmetic, not a source fact.
                # Asserting it against source components only manufactures
                # failures: Volvo's 2024 ESEF report tags current 261,298m
                # plus non-current 211,216m liabilities, assets 714,564m and
                # equity 197,361m, because the 44,689m held-for-sale liability
                # bucket (IFRS 5) sits in neither subtotal. The derived
                # total_liabilities would be reported as "not equal to
                # current + non-current" for a filing that is correct.
                continue
            period_key = value.period.end_date.isoformat()
            amount = abs(value.value) if use_abs else value.value
            by_period.setdefault(period_key, {})[field_name] = float(amount)
    return by_period


def _merge(
    target: dict[str, dict[str, float]], source: dict[str, dict[str, float]]
) -> None:
    for period, fields in source.items():
        target.setdefault(period, {}).update(fields)


def _close(expected: float, actual: float) -> bool:
    """finvariant's own default tolerances, so a fold decision here and a
    pass/fail decision there agree on the same facts."""

    return abs(actual - expected) <= max(0.5, 0.0001 * abs(expected))


def _subordinated_liability_values(statement: FinancialStatement) -> dict[str, float]:
    """Source subordinated-liability figures by period, for the equation fold.

    Deliberately not a mapped field: finvariant validates period fields
    against its canonical schema (it has no subordinated role, and its
    ``other_*`` slots belong to the section checks), so this figure must
    never reach the dict handed to it.
    """

    values: dict[str, float] = {}
    for item in statement.line_items:
        if item.code != "subordinated_liabilities":
            continue
        for value in item.values:
            if value.dimensions or value.provenance == "derived":
                continue
            values[value.period.end_date.isoformat()] = float(value.value)
    return values


def _fold_excluded_subordinated_liabilities(
    periods: dict[str, dict[str, float]],
    subordinated: dict[str, float],
) -> None:
    """Absorb subordinated debt into total liabilities for the equation check.

    Some filers present a "Liabilities" subtotal that excludes subordinated
    debt - Storebrand Livsforsikring's 2024 ESEF report tags Liabilities
    811,744m, SubordinatedLiabilities 9,979m and Equity 17,810m against
    total assets of 839,533m. Its own facts therefore fail assets =
    liabilities + equity while footing exactly once the subordinated leg is
    added. Filers that fold subordinated debt *into* Liabilities already
    satisfy the plain equation, so this only fires where the source facts
    themselves prove the subtraction happened: the plain equation misses and
    the tagged subordinated figure closes it exactly. Without it, every such
    filing reports a false accounting failure to the user.
    """

    for period, fields in periods.items():
        if not {"total_assets", "total_liabilities", "total_equity"} <= fields.keys():
            continue
        extra = subordinated.get(period)
        if extra is None:
            continue
        assets = fields["total_assets"]
        liabilities = fields["total_liabilities"]
        equity = fields["total_equity"]
        if _close(assets, liabilities + equity) or not _close(
            assets, liabilities + extra + equity
        ):
            continue
        fields["total_liabilities"] = liabilities + extra


def to_finvariant_statements(financials: FilingFinancials) -> fv.Statements | None:
    """Map a FilingFinancials into finvariant's canonical schema.

    Returns None when there's nothing worth checking (no statement of a
    type finvariant understands, or no periods at all).
    """
    income_statement: dict[str, dict[str, float]] = {}
    balance_sheet: dict[str, dict[str, float]] = {}
    cash_flow: dict[str, dict[str, float]] = {}
    subordinated: dict[str, float] = {}
    for statement in financials.statements:
        field_map = _FIELDS_BY_STATEMENT_TYPE.get(statement.statement_type)
        if field_map is None:
            continue
        periods = _statement_periods(statement, field_map)
        if statement.statement_type == "income_statement":
            _merge(income_statement, periods)
        elif statement.statement_type == "balance_sheet":
            _merge(balance_sheet, periods)
            subordinated.update(_subordinated_liability_values(statement))
        elif statement.statement_type == "cash_flow_statement":
            _merge(cash_flow, periods)

    _fold_excluded_subordinated_liabilities(balance_sheet, subordinated)

    all_periods = sorted(
        set(income_statement) | set(balance_sheet) | set(cash_flow), reverse=True
    )
    if not all_periods:
        return None
    return fv.Statements(
        periods=all_periods,
        income_statement=income_statement,
        balance_sheet=balance_sheet,
        cash_flow=cash_flow,
    )


def validate_financials(financials: FilingFinancials) -> fv.AuditReport | None:
    """Run finvariant's deterministic integrity checks, or None if there
    was nothing mappable to check."""
    statements = to_finvariant_statements(financials)
    if statements is None:
        return None
    return fv.check(statements)


def validation_view(report: fv.AuditReport | None) -> dict[str, object] | None:
    """A compact JSON view of an AuditReport for an MCP response."""
    if report is None:
        return None
    return {
        "ok": report.ok,
        "checks_passed": report.n_passed,
        "checks_failed": report.n_failed,
        "checks_skipped": report.n_skipped,
        "findings": [
            {
                "rule_id": finding.rule_id,
                "description": finding.description,
                "period": finding.period,
                "expected": finding.expected,
                "actual": finding.actual,
                "difference": finding.difference,
            }
            for finding in report.findings
            if finding.status == "fail"
        ],
    }
