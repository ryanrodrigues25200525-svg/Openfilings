from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from openfilings.adapters.base import SourceDocument
from openfilings.exceptions import FinancialsUnavailableError
from openfilings.models import Filing
from openfilings.xbrl import extract_filing_financials
from openfilings.xbrl.pdf_statements import (
    extract_pdf_ocr_financials,
    extract_pdf_source_financials,
    extract_pdf_table_financials,
    extract_pdf_text_financials,
)


def test_english_pdf_tables_build_scaled_financial_statements() -> None:
    financials = extract_pdf_table_financials(
        _english_report(),
        _filing(period_end=date(2025, 6, 30)),
        source_url="https://example.test/annual-report.pdf",
        sha256="a" * 64,
    )

    assert financials.extraction_method == "pdf-markdown-tables"
    assert financials.taxonomy_namespaces == ("pdf-derived",)
    assert financials.fact_count == 18
    assert [statement.statement_type for statement in financials.statements] == [
        "income_statement",
        "balance_sheet",
        "cash_flow_statement",
    ]

    income = financials.income_statement()
    assert income is not None
    assert income.currency == "SGD"
    revenue = next(item for item in income.line_items if item.code == "revenue")
    assert revenue.concept == "pdf-label:revenue"
    assert [value.value for value in revenue.values] == [
        Decimal("1234000000"),
        Decimal("1100000000"),
    ]
    assert revenue.values[0].period.start_date == date(2024, 7, 1)
    assert revenue.values[0].period.end_date == date(2025, 6, 30)
    assert revenue.values[0].decimals == "-6"

    balance_sheet = financials.balance_sheet()
    assert balance_sheet is not None
    assets = next(
        item for item in balance_sheet.line_items if item.code == "total_assets"
    )
    assert assets.values[0].period.kind == "instant"
    assert assets.values[0].value == Decimal("5000000000")

    cash_flow = financials.cash_flow_statement()
    assert cash_flow is not None
    investing = next(
        item for item in cash_flow.line_items if item.code == "investing_cash_flow"
    )
    assert investing.values[0].value == Decimal("-210000000")


@pytest.mark.parametrize(
    ("markdown", "currency", "expected_revenue"),
    [
        (
            """
            # Demonstrações financeiras consolidadas

            R$ milhões

            |  | 2025 | 2024 |
            | --- | ---: | ---: |
            | Receita líquida | 2.500 | 2.100 |
            | Lucro operacional | 420 | 390 |
            | Lucro antes do imposto de renda | 380 | 350 |
            """,
            "BRL",
            Decimal("2500000000"),
        ),
        (
            """
            # 合併綜合損益表

            新台幣仟元

            | 項目 | 2025 | 2024 |
            | --- | ---: | ---: |
            | 營業收入 | 1,200,000 | 1,050,000 |
            | 營業利益 | 180,000 | 160,000 |
            | 稅前淨利 | 150,000 | 140,000 |
            """,
            "TWD",
            Decimal("1200000000"),
        ),
    ],
)
def test_pdf_tables_support_portuguese_and_traditional_chinese_labels(
    markdown: str,
    currency: str,
    expected_revenue: Decimal,
) -> None:
    financials = extract_pdf_table_financials(
        markdown,
        _filing(period_end=date(2025, 12, 31)),
        source_url="https://example.test/report.pdf",
        sha256="b" * 64,
    )

    income = financials.income_statement()
    assert income is not None
    assert income.currency == currency
    revenue = next(item for item in income.line_items if item.code == "revenue")
    assert revenue.values[0].value == expected_revenue


def test_pdf_table_extraction_rejects_unrelated_and_single_metric_tables() -> None:
    markdown = """
    # Five-year overview

    | Statistic | 2025 | 2024 |
    | --- | ---: | ---: |
    | Employees | 10,000 | 9,500 |
    | Revenue | 100 | 90 |
    """

    with pytest.raises(FinancialsUnavailableError, match="supported statement"):
        extract_pdf_table_financials(
            markdown,
            _filing(),
            source_url="https://example.test/report.pdf",
            sha256="c" * 64,
        )


def test_pdf_table_extraction_covers_common_english_summary_labels() -> None:
    """MD&A financial-performance tables commonly use "Net Revenue", "Income
    from Operations", "Income before Income Tax" (singular), "Income Tax
    Expenses" (plural), and "Net Income" - none of which are the primary
    alias for their line item. A PDF ligature-extraction artifact also
    turns "Gross Profit" into "Gross Proft" ("fi" loses its "i")."""
    markdown = """
    |Item|2024|2023|Difference|%|
    |---|---|---|---|---|
    |Net Revenue|2,894,307,699|2,161,735,841|732,571,858|34%|
    |Cost of Revenue|1,269,954,135|986,625,213|283,328,922|29%|
    |Gross Proft|1,624,353,564|1,175,110,628|449,242,936|38%|
    |Income from Operations|1,322,053,050|921,465,606|400,587,444|43%|
    |Income before Income Tax|1,405,838,635|979,171,324|426,667,311|44%|
    |Income Tax Expenses|233,406,876|141,403,807|92,003,069|65%|
    |Net Income|1,172,431,759|837,767,517|334,664,242|40%|
    """

    financials = extract_pdf_table_financials(
        markdown,
        _filing(period_end=date(2024, 12, 31), source="sgx", filing_type="annual"),
        source_url="https://example.test/summary-report.pdf",
        sha256="h" * 64,
    )

    income = financials.income_statement()
    assert income is not None
    codes = {item.code: item.values[0].value for item in income.line_items}
    assert codes["revenue"] == Decimal("2894307699")
    assert codes["gross_profit"] == Decimal("1624353564")
    assert codes["operating_income_loss"] == Decimal("1322053050")
    assert codes["profit_before_tax"] == Decimal("1405838635")
    assert codes["income_tax"] == Decimal("233406876")
    assert codes["net_income_loss"] == Decimal("1172431759")


def test_scale_ignores_a_plain_figure_that_contains_three_zeros() -> None:
    """ "'000" (a currency symbol/apostrophe followed by three zeros) is a
    standard scale-of-thousands marker, but _normalize_label strips the
    punctuation around it down to a bare "000" - indistinguishable, after
    normalization, from a plain figure that happens to contain three
    consecutive zero digits (e.g. "8,600,031" -> "8600031"). Confirmed
    live on a Peru SMV filing: a cash-flow context value "8600031"
    silently applied a 1000x scale across an entire unrelated table. Only
    a standalone "000" token is a real marker; embedded in a longer digit
    run, it must not be."""
    from openfilings.xbrl.pdf_statements import _scale

    assert _scale("Resultado del ejercicio 8600031 6945406") == Decimal("1")
    assert _scale("S$'000") == Decimal("1000")
    assert _scale("Amounts in S/ 000") == Decimal("1000")


def test_fca_filing_never_reports_pen_currency() -> None:
    """Issue #6: Unilever's 20-F via the FCA NSM was labelled PEN.

    "S/ 000" (a scale-of-thousands marker) matches the bare "s/" PEN
    marker, so the marker scan says PEN for a UK filing. The source
    guard vetoes PEN for fca_nsm filings, falling through to the
    source default (None) instead; a Peruvian SMV filing with the same
    marker must still report PEN. Table/scale heuristics untouched.
    """
    from openfilings.xbrl.pdf_statements import _currency

    assert _currency("Amounts in S/ 000") == "PEN"
    assert _currency("Amounts in S/ 000", source="fca_nsm") is None
    assert _currency("Amounts in S/ 000", source="smv") == "PEN"

    markdown = """
    # Consolidated balance sheet

    S/ 000

    |  | 2025 | 2024 |
    | --- | ---: | ---: |
    | Total assets | 79,750 | 75,000 |
    | Total liabilities | 60,000 | 58,000 |
    """

    fca_financials = extract_pdf_table_financials(
        markdown,
        _filing(period_end=date(2025, 12, 31), source="fca_nsm", filing_type="annual"),
        source_url="https://example.test/unilever-20f.pdf",
        sha256="p" * 64,
    )
    balance_sheet = fca_financials.balance_sheet()
    assert balance_sheet is not None
    assert balance_sheet.currency != "PEN"

    smv_financials = extract_pdf_table_financials(
        markdown,
        _filing(period_end=date(2025, 12, 31), source="smv", filing_type="annual"),
        source_url="https://example.test/smv-report.pdf",
        sha256="q" * 64,
    )
    smv_balance = smv_financials.balance_sheet()
    assert smv_balance is not None
    assert smv_balance.currency == "PEN"


def test_indian_crore_and_lakh_headers_scale() -> None:
    """Indian filers report in crore (10^7) or lakh (10^5).

    Without these markers an Indian PDF's figures are understated by seven
    orders of magnitude, and - because India's XBRL path only covers filings
    from April 2025 - land in the same multi-period series as correctly
    scaled tagged values. Confirmed live: TCS's FY2024 annual report gave
    total assets of 143651 instead of 1436510000000.
    """
    from openfilings.xbrl.pdf_statements import _scale

    assert _scale("(₹ in crore)") == Decimal("10000000")
    assert _scale("Rs. in crores") == Decimal("10000000")
    assert _scale("Amounts in lakhs") == Decimal("100000")
    assert _scale("₹ in lacs") == Decimal("100000")
    # A plain figure must not trip either marker.
    assert _scale("Total assets 143651 129505") == Decimal("1")


def test_pdf_table_scale_is_not_thrown_off_by_a_figure_with_embedded_zeros() -> None:
    markdown = """
    # Estado de flujos de efectivo

    | Cuenta | 2025 | 2024 |
    | --- | ---: | ---: |
    | RESULTADO ANTES DE IMPUESTO A LA RENTA | 8600031 | 6945406 |
    | Dividendos pagados | -4746884 | -4478340 |
    | Efectivo y equivalentes al efectivo | 43059012 | 42842917 |
    """

    financials = extract_pdf_table_financials(
        markdown,
        _filing(period_end=date(2025, 12, 31), source="smv", filing_type="annual"),
        source_url="https://example.test/smv-report.pdf",
        sha256="o" * 64,
    )

    cash_flow = financials.cash_flow_statement()
    assert cash_flow is not None
    dividends = next(
        item for item in cash_flow.line_items if item.code == "dividends_paid"
    )
    assert dividends.values[0].value == Decimal("-4746884")


def test_aligned_pdf_text_builds_group_financial_statements() -> None:
    financials = extract_pdf_text_financials(
        (_sgx_income_text(), _sgx_position_text(), _sgx_cash_flow_text()),
        _filing(period_end=date(2025, 6, 30)),
        source_url="https://example.test/sgx-report.pdf",
        sha256="d" * 64,
    )

    assert financials.extraction_method == "pdf-aligned-text"
    income = financials.income_statement()
    assert income is not None
    assert income.currency == "SGD"
    assert next(item for item in income.line_items if item.code == "revenue").values[
        0
    ].value == Decimal("1298167000")
    assert next(
        item for item in income.line_items if item.code == "operating_income_loss"
    ).values[0].value == Decimal("742843000")
    assert next(
        item for item in income.line_items if item.code == "profit_before_tax"
    ).values[0].value == Decimal("785891000")

    balance = financials.balance_sheet()
    assert balance is not None
    assert next(
        item for item in balance.line_items if item.code == "total_assets"
    ).values[0].value == Decimal("4143988000")

    cash_flow = financials.cash_flow_statement()
    assert cash_flow is not None
    assert next(
        item for item in cash_flow.line_items if item.code == "operating_cash_flow"
    ).values[0].value == Decimal("841670000")


def test_aligned_pdf_text_finds_total_behind_unlabeled_segment_breakdown() -> None:
    """Revenue can be broken down by segment with no repeated "Total revenue"
    label before its own total row appears - the total must not be confused
    with the first segment's own values."""
    financials = extract_pdf_text_financials(
        (_segment_breakdown_income_text(),),
        _filing(period_end=date(2024, 12, 31), source="sgx", filing_type="annual"),
        source_url="https://example.test/segment-report.pdf",
        sha256="f" * 64,
    )

    income = financials.income_statement()
    assert income is not None
    assert next(item for item in income.line_items if item.code == "revenue").values[
        0
    ].value == Decimal("660257000000")
    assert next(
        item for item in income.line_items if item.code == "cost_of_revenue"
    ).values[0].value == Decimal("-311011000000")
    assert next(
        item for item in income.line_items if item.code == "gross_profit"
    ).values[0].value == Decimal("349246000000")


def test_aligned_pdf_text_does_not_confuse_subtotal_with_its_component() -> None:
    """A combined subtotal label ("Passivo circulante e nao circulante" -
    total liabilities) starts with a component's full name ("Passivo
    circulante" - current liabilities) plus a conjunction. The component's
    own row must not be overwritten by the later subtotal match."""
    financials = extract_pdf_text_financials(
        (_cvm_balance_sheet_text(),),
        _filing(period_end=date(2025, 12, 31), source="cvm", filing_type="annual"),
        source_url="https://example.test/cvm-report.pdf",
        sha256="g" * 64,
    )

    balance = financials.balance_sheet()
    assert balance is not None
    assert next(
        item for item in balance.line_items if item.code == "current_liabilities"
    ).values[0].value == Decimal("198368000000")
    assert next(
        item for item in balance.line_items if item.code == "total_liabilities"
    ).values[0].value == Decimal("805802000000")


def test_aligned_pdf_text_handles_indian_filing_conventions() -> None:
    """Covers three conventions seen in Indian (NSE) annual reports: lakh/
    crore comma grouping ("2,57,935") that Western 3-digit grouping logic
    can't parse; a bare section header ("Current Liabilities") appearing
    before its own "Total X" row, which must not keep the header's
    (wrong) nearby numbers once the real total is found; and a grand
    total ("Total Equity and Liabilities") that restates total assets and
    must not be read as the equity line item. The heading also sits past
    the first 12 lines, exercising content-based statement detection.

    The fixture's "(Rs in crore)" header must also scale the figures by
    10^7. This assertion previously expected the unscaled numbers, which
    locked the missing-crore-marker bug into the suite."""
    financials = extract_pdf_text_financials(
        (_nse_balance_sheet_text(),),
        _filing(period_end=date(2025, 3, 31), source="nse", filing_type="annual"),
        source_url="https://example.test/nse-report.pdf",
        sha256="i" * 64,
    )

    balance = financials.balance_sheet()
    assert balance is not None
    codes = {item.code: item.values[0].value for item in balance.line_items}
    crore = Decimal("10000000")
    assert codes["current_liabilities"] == Decimal("257935") * crore
    assert codes["total_equity"] == Decimal("543087") * crore
    assert codes["total_assets"] == Decimal("1022401") * crore
    assert codes["total_equity"] + codes["total_liabilities"] == codes["total_assets"]


def test_definition_for_label_rejects_ratio_disclosures() -> None:
    """A mandatory "Ratio Analysis" note (e.g. Schedule III filings) lists
    metrics like "Inventory Turnover Ratio" that start with a line item's
    full name but are not that line item's balance-sheet value."""
    from openfilings.xbrl.pdf_statements import _definition_for_label

    assert _definition_for_label("Inventory Turnover Ratio") is None
    assert _definition_for_label("Trade Receivables Turnover Ratio") is None
    assert _definition_for_label("Net Profit Margin (%)") is None
    assert _definition_for_label("Inventories").code == "inventory"


def test_definition_for_label_rejects_ifrs5_disposal_group_disclosures() -> None:
    """Peru's SMV-filed IFRS balance sheets carry a standard IFRS 5 caption
    ("Activos no Corrientes o Grupos de Activos para su Disposicion
    Clasificados como Mantenidos para la Venta...") that starts with the
    same words as the "Activos no Corrientes" (noncurrent_assets) alias but
    is a disposal-group disclosure, not the category total."""
    from openfilings.xbrl.pdf_statements import _definition_for_label

    assert (
        _definition_for_label(
            "Activos no Corrientes o Grupos de Activos para su "
            "Disposicion Clasificados como Mantenidos para la Venta"
        )
        is None
    )
    assert _definition_for_label("Total Activos No Corrientes").code == (
        "noncurrent_assets"
    )


def test_definition_for_label_rejects_total_pasivos_y_patrimonio() -> None:
    """ "Total Pasivos y Patrimonio" restates total assets (liabilities plus
    equity), not the "Total Pasivos" (total_liabilities) line item alone."""
    from openfilings.xbrl.pdf_statements import _definition_for_label

    assert _definition_for_label("Total Pasivos y Patrimonio") is None
    assert _definition_for_label("Total Pasivos").code == "total_liabilities"


def test_number_rejects_page_footer_and_note_reference_text() -> None:
    """A page footer ("DBS Annual Report 2025 ... Financial statements 115")
    or a stray note reference ("Note 27") carries a real word alongside an
    embedded digit. Stripping every non-numeric character before parsing
    would otherwise turn that digit into a false numeric value."""
    from openfilings.xbrl.pdf_statements import _number

    assert _number("DBS Annual Report 2025 A beacon of stability") is None
    assert _number("Note 27") is None
    assert _number("114") == Decimal("114")
    assert _number("(1,234)") == Decimal("-1234")


def test_aligned_pdf_text_does_not_bleed_into_equity_statement_row() -> None:
    """A "Net profit" row inside the Statement of Changes in Equity (whose
    columns are equity components, not fiscal years, and whose heading is
    set in sentence case: "Consolidated statement of changes in equity")
    must not overwrite the income statement's own "Net profit" row, and a
    bare page number reachable only past a run of footnote prose must never
    be read as a value."""
    financials = extract_pdf_text_financials(
        (_income_statement_followed_by_equity_statement_text(),),
        _filing(period_end=date(2025, 12, 31), source="sgx", filing_type="annual"),
        source_url="https://example.test/dbs-report.pdf",
        sha256="h" * 64,
    )

    income = financials.income_statement()
    assert income is not None
    assert next(
        item for item in income.line_items if item.code == "net_income_loss"
    ).values[0].value == Decimal("10934000000")


def test_aligned_pdf_text_finds_unlabeled_subtotal_in_net_assets_presentation() -> None:
    """A "net assets" style balance sheet ("Represented by: ... Non-current
    liabilities" as a section header, its subtotal appearing several lines
    later with no label of its own, directly followed by "Net assets" with
    no textual separator) must not read "Net assets" itself as the
    non-current liabilities total (they aren't the same figure), nor the
    last individual breakdown item before the real, unlabeled subtotal."""
    financials = extract_pdf_text_financials(
        (_net_assets_style_balance_sheet_text(),),
        _filing(period_end=date(2025, 12, 31), source="sgx", filing_type="annual"),
        source_url="https://example.test/keppel-report.pdf",
        sha256="j" * 64,
    )

    balance = financials.balance_sheet()
    assert balance is not None
    noncurrent_liabilities = next(
        item for item in balance.line_items if item.code == "noncurrent_liabilities"
    )
    assert noncurrent_liabilities.values[0].value == Decimal("9500000")
    total_equity = next(
        item for item in balance.line_items if item.code == "total_equity"
    )
    assert total_equity.values[0].value == Decimal("9300000")


def test_aligned_pdf_text_resolves_group_company_breakdown_with_multiline_labels() -> (
    None
):
    """Keppel-style "Group"/"Company" balance sheets indent sub-items
    across two short lines ("Amounts due from:" / "- subsidiaries"), which
    would otherwise trip the same bail-out that protects against wandering
    into footnote prose. The real subtotal directly follows the last
    sub-item's values with no separator, and is itself followed by an
    unrelated "Net current assets" row before the next real section -
    neither should be read as "Total current liabilities"."""
    financials = extract_pdf_text_financials(
        (_group_company_current_liabilities_text(),),
        _filing(period_end=date(2025, 12, 31), source="sgx", filing_type="annual"),
        source_url="https://example.test/keppel-report.pdf",
        sha256="l" * 64,
    )

    balance = financials.balance_sheet()
    assert balance is not None
    current_liabilities = next(
        item for item in balance.line_items if item.code == "current_liabilities"
    )
    assert [value.value for value in current_liabilities.values] == [
        Decimal("5778714000"),
        Decimal("4771044000"),
    ]
    # Derived from current_liabilities + noncurrent_liabilities, since no
    # combined "Total liabilities" row exists on this page.
    total_liabilities = next(
        item for item in balance.line_items if item.code == "total_liabilities"
    )
    assert [value.value for value in total_liabilities.values] == [
        Decimal("15901637000"),
        Decimal("16232693000"),
    ]


def test_aligned_pdf_text_prefers_larger_total_when_periods_tie() -> None:
    """A note reusing a grand-total label ("Total assets") for a narrower
    scope (a structured entity, a subsidiary) can only describe a subset of
    the entity's real total. When two sections' candidates tie on period
    count, the larger one is the real consolidated figure."""
    financials = extract_pdf_text_financials(
        (_note_scoped_total_assets_text(), _consolidated_balance_sheet_text()),
        _filing(period_end=date(2025, 12, 31), source="sgx", filing_type="annual"),
        source_url="https://example.test/dbs-report.pdf",
        sha256="i" * 64,
    )

    balance = financials.balance_sheet()
    assert balance is not None
    assert next(
        item for item in balance.line_items if item.code == "total_assets"
    ).values[0].value == Decimal("897488000000")


def test_aligned_pdf_text_does_not_blend_bundled_annual_report_vintages() -> None:
    """A PDF can bundle multiple full annual reports (e.g. a 20-F including
    3 years of complete audited comparatives, each covering its own 2-year
    pair - Cemex's filings do this). Two sections tying on period count
    here describe genuinely DIFFERENT fiscal years, not a scope difference
    - the older vintage's same-length candidate must never win a magnitude
    contest against the more recent one just because it happens to be a
    bigger number."""
    financials = extract_pdf_text_financials(
        (
            _older_vintage_income_statement_text(),
            _current_vintage_income_statement_text(),
        ),
        _filing(period_end=date(2023, 12, 31), source="bmv", filing_type="annual"),
        source_url="https://example.test/cemex-report.pdf",
        sha256="m" * 64,
    )

    income = financials.income_statement()
    assert income is not None
    revenue = next(item for item in income.line_items if item.code == "revenue")
    assert [value.value for value in revenue.values] == [
        Decimal("17388"),
        Decimal("15577"),
    ]


def test_aligned_pdf_text_rejects_negative_intangible_assets_from_a_note() -> None:
    """A properly presented balance sheet never reports intangible assets
    as negative. Confirmed live on Cemex's filing: "Activos intangibles"
    matched a cash-flow reconciliation note's period-on-period movement
    (a parenthesized, and therefore negative, adjustment) rather than the
    entity's real balance-sheet total - Cemex doesn't even report
    intangibles standalone (it's combined with goodwill), so the correct
    outcome is no intangible_assets figure at all, not a fabricated
    negative one."""
    text = """
    Estado de situacion financiera
    Al 31 de diciembre
    2023
    2022
    Activos intangibles
    17
    (192)
    (53)
    (116)
    Inventarios
    1,789
    1,669
    Total activos
    28,433
    26,447
    """
    financials = extract_pdf_text_financials(
        (text,),
        _filing(period_end=date(2023, 12, 31), source="bmv", filing_type="annual"),
        source_url="https://example.test/cemex-report.pdf",
        sha256="n" * 64,
    )

    balance = financials.balance_sheet()
    assert balance is not None
    codes = {item.code for item in balance.line_items}
    assert "intangible_assets" not in codes


def test_aligned_pdf_text_keeps_first_occurrence_of_a_repeated_label() -> None:
    """A cash-flow reconciliation note ("changes in working capital") can
    reuse a balance-sheet row's exact label ("Trade and other receivables")
    within the same statement page/continuation window. The note's number
    is a period-on-period movement, not the balance-sheet figure, and must
    not overwrite the real row that already appeared earlier in the same
    section."""
    financials = extract_pdf_text_financials(
        (_balance_sheet_with_reused_label_text(),),
        _filing(period_end=date(2025, 6, 30), source="asx", filing_type="half_year"),
        source_url="https://example.test/asx-report.pdf",
        sha256="j" * 64,
    )

    balance = financials.balance_sheet()
    assert balance is not None
    assert next(
        item for item in balance.line_items if item.code == "trade_receivables"
    ).values[0].value == Decimal("5452000000")


def test_definition_for_label_matches_peruvian_bank_singular_totals() -> None:
    """Peruvian bank balance sheets (e.g. Credicorp, BCP consolidated) use
    the singular "activo"/"pasivo" for grand totals, unlike the plural
    "activos"/"pasivos" used by industrial filers elsewhere in the region -
    without these aliases the totals go missing entirely (not wrong, just
    absent) with no error raised."""
    from openfilings.xbrl.pdf_statements import _definition_for_label

    assert _definition_for_label("TOTAL ACTIVO").code == "total_assets"
    assert _definition_for_label("TOTAL ACTIVO CORRIENTE").code == "current_assets"
    assert (
        _definition_for_label("TOTAL ACTIVO NO CORRIENTE").code == "noncurrent_assets"
    )
    assert _definition_for_label("TOTAL PASIVO").code == "total_liabilities"
    assert _definition_for_label("TOTAL PASIVO CORRIENTE").code == "current_liabilities"
    # BCP's individual (non-consolidated) statement uses a third phrasing,
    # "del", that must resolve to the same codes.
    assert _definition_for_label("TOTAL DEL ACTIVO").code == "total_assets"
    assert _definition_for_label("TOTAL DEL PASIVO").code == "total_liabilities"


def test_definition_for_label_rejects_total_pasivo_y_capital_contable() -> None:
    """Mexican filers use "Capital Contable" for equity instead of
    "Patrimonio" - "Total del Pasivo y Capital Contable" restates total
    assets (liabilities + equity), not total liabilities alone, the same
    pattern as "Total Pasivos y Patrimonio" in other Spanish-language
    filings."""
    from openfilings.xbrl.pdf_statements import _definition_for_label

    assert _definition_for_label("TOTAL DEL PASIVO Y CAPITAL CONTABLE") is None
    assert _definition_for_label("TOTAL DEL CAPITAL CONTABLE").code == "total_equity"


def test_heading_at_rejects_mid_sentence_fragment_wrapped_across_page_break() -> None:
    """A sentence wrapped across a PDF page break can leave a lowercase
    fragment starting the next page's text (e.g. "...generó una ganancia
    por recompra reconocida en el" / "estado de resultados del año
    concluido..."). This fragment happens to start with a real statement
    heading but is MD&A narrative prose continuing from the previous page,
    not the heading of a new statement."""
    from openfilings.xbrl.pdf_statements import _statement_type

    lines = (
        "PARTE I",
        "estado de resultados del año concluido el 31 de diciembre de 2022.",
        "De conformidad con los Instrumentos Financieros...",
    )
    assert _statement_type(lines) is None


def test_heading_at_rejects_md_and_a_subsection_title_containing_heading() -> None:
    """An MD&A subsection title like "Información del Estado de Resultados"
    contains a real statement heading as a substring but is not the
    statement itself - matching it silently pulls narrative summary
    figures into the statement."""
    from openfilings.xbrl.pdf_statements import _statement_type

    lines = (
        "Información del Estado de Resultados:",
        "Los ingresos totales aumentaron un 8%, pasando de $14,379 millones",
    )
    assert _statement_type(lines) is None


def test_heading_at_matches_colombian_estados_del_resultado() -> None:
    """Colombian filers (e.g. Grupo Argos) title the income statement
    "Estados del Resultado" - plural "Estados", singular "Resultado", and
    "del" instead of "de" - a fourth combination distinct from the other
    Spanish/Portuguese variants already covered. Missing it meant the real
    statement page was skipped entirely and a footnote/MD&A page with the
    same generic line-item labels won instead, producing plausible but
    fabricated figures."""
    from openfilings.xbrl.pdf_statements import _statement_type

    lines = ("Grupo Argos S.A.", "ESTADOS DEL RESULTADO SEPARADO")
    assert _statement_type(lines) == "income_statement"


def test_colombian_income_statement_ignores_later_auditor_report_year() -> None:
    """Grupo Argos SFC filing 125070 reports 2025/2024 values, but the
    auditor's March 2026 report date appears before those column headings.
    The aligned-text path must not shift the correct values forward one
    year by mistaking that signature date for a reporting column."""
    financials = extract_pdf_text_financials(
        (
            """
            Grupo Argos S.A.
            ESTADOS DEL RESULTADO SEPARADO
            Años terminados el 31 de diciembre
            Jorge Mario Velásquez Jaramillo
            Presidente
            (Véase informe del 2 de marzo de 2026)
            Nota
            2025
            2024
            Ingresos de actividades ordinarias
            100
            90
            Costo de actividades ordinarias
            (40)
            (35)
            Ganancia bruta
            60
            55
            """,
        ),
        _filing(
            period_end=date(2025, 12, 31),
            source="sfc",
            filing_type="annual",
        ),
        source_url="https://example.test/grupo-argos-2025.pdf",
        sha256="2" * 64,
    )

    income = financials.income_statement()
    assert income is not None
    revenue = next(item for item in income.line_items if item.code == "revenue")
    assert [value.period.end_date for value in revenue.values] == [
        date(2025, 12, 31),
        date(2024, 12, 31),
    ]
    assert [value.value for value in revenue.values] == [
        Decimal("100"),
        Decimal("90"),
    ]


def test_definition_for_label_prefers_continuing_over_discontinued_operations() -> None:
    """A filer reporting a discontinued-operations split states the same
    subtotal label twice, scoped differently ("Utilidad Antes de Impuestos
    por Operaciones Continuadas" vs "...Operaciones Discontinuadas",
    "Impuestos sobre las Ganancias por Operaciones Continuadas" vs
    "...Discontinuadas"). The discontinued-operations variant must not
    match - it's a narrower figure than the entity's headline pre-tax
    profit or tax expense."""
    from openfilings.xbrl.pdf_statements import _definition_for_label

    assert (
        _definition_for_label(
            "Utilidad antes de impuestos por operaciones continuadas"
        ).code
        == "profit_before_tax"
    )
    assert (
        _definition_for_label("Utilidad antes de impuestos operaciones discontinuadas")
        is None
    )
    assert (
        _definition_for_label(
            "Impuestos sobre las ganancias por operaciones continuadas"
        ).code
        == "income_tax"
    )
    assert (
        _definition_for_label(
            "Impuestos sobre las ganancias por operaciones discontinuadas"
        )
        is None
    )


def test_derives_missing_balance_sheet_total_from_the_other_two() -> None:
    """When a page states total liabilities and total equity but never a
    literal "Total assets" row (common in some Singapore/SFRS filings that
    only state the balancing "Net assets" figure), the accounting identity
    still determines it."""
    financials = extract_pdf_text_financials(
        (
            """
            Statement of financial position
            S$ million
            2025
            2024
            Total liabilities
            700
            650
            Total equity
            300
            250
            """,
        ),
        _filing(period_end=date(2025, 12, 31)),
        source_url="https://example.test/report.pdf",
        sha256="k" * 64,
    )

    balance = financials.balance_sheet()
    assert balance is not None
    total_assets = next(
        item for item in balance.line_items if item.code == "total_assets"
    )
    assert [value.value for value in total_assets.values] == [
        Decimal("1000000000"),
        Decimal("900000000"),
    ]


def test_single_word_alias_requires_glued_continuation() -> None:
    """A single-word alias (e.g. "revenue", "goodwill") must only match a
    directly-glued continuation (a plural "s", a footnote digit) - a
    space-separated continuation is a new word starting a different
    concept or sentence, not a variant of the same value (Singapore's
    Schedule-style disclosures: "Revenue Reserves" is an equity reserve,
    not revenue; "Goodwill is reviewed on an annual basis..." is
    accounting-policy prose, not a goodwill figure)."""
    from openfilings.xbrl.pdf_statements import _definition_for_label

    assert _definition_for_label("Revenue Reserves") is None
    assert _definition_for_label("Goodwill is reviewed on an annual basis") is None
    assert (
        _definition_for_label("Total Assets of the Sponsored Structured Entities")
        is None
    )
    assert _definition_for_label("Revenues").code == "revenue"
    assert _definition_for_label("Goodwill").code == "goodwill"


def test_fuzzy_fallback_catches_near_miss_spellings() -> None:
    """A PDF's text layer can introduce near-miss spellings that exact and
    prefix matching won't catch verbatim - a transposed letter, a dropped
    letter, a misread character. The fuzzy fallback catches these while
    still resolving cleanly-spelled labels via the faster exact/prefix
    tiers first."""
    from openfilings.xbrl.pdf_statements import _definition_for_label

    assert _definition_for_label("Grose profit").code == "gross_profit"
    assert _definition_for_label("Toatl assets").code == "total_assets"
    assert _definition_for_label("Cost of Revenu").code == "cost_of_revenue"


def test_fuzzy_fallback_rejects_ambiguous_current_noncurrent_typo() -> None:
    """ "Current" and "noncurrent" aliases are themselves highly similar
    strings - a misspelled "Current liabilities" must not be fuzzy-matched
    to "noncurrent_liabilities" just because that alias also scores highly;
    the correct code must clearly beat every other code's best match, not
    merely clear a flat score threshold."""
    from openfilings.xbrl.pdf_statements import _definition_for_label

    assert _definition_for_label("Curent liabilities").code == "current_liabilities"
    assert _definition_for_label("Net current assets") is None


def test_fuzzy_fallback_rejects_other_qualifier_prefix() -> None:
    """A leading "Other" qualifier ("Other non-current liabilities", "Other
    current assets") is a distinct sub-item, not a formatting variant of
    the base category - confirmed live on Keppel's real filing, where
    "Other non-current liabilities" fuzzy-matched noncurrent_liabilities
    (score 88.5) and silently overwrote the real section subtotal with
    just that one sub-item's value. The threshold must sit above the
    worst observed "Other X" collision."""
    from openfilings.xbrl.pdf_statements import _definition_for_label

    assert _definition_for_label("Other non-current liabilities") is None
    assert _definition_for_label("Other current liabilities") is None
    assert _definition_for_label("Other non-current assets") is None
    assert _definition_for_label("Other current assets") is None


def test_financial_extractor_routes_pdf_documents_to_table_parser(monkeypatch) -> None:
    monkeypatch.setattr(
        "openfilings.xbrl.pdf_statements._pdf_statement_sections", lambda _: ()
    )
    monkeypatch.setattr(
        "openfilings.xbrl.pdf_statements.pdf_to_markdown", lambda _: _english_report()
    )
    document = SourceDocument(
        data=b"%PDF-1.7 synthetic",
        media_type="application/pdf",
        source_url="https://example.test/annual-report.pdf",
    )

    financials = extract_filing_financials(
        document,
        _filing(period_end=date(2025, 6, 30)),
    )

    assert financials.extraction_method == "pdf-markdown-tables"
    assert financials.source_url == document.source_url
    assert financials.sha256 != ""


def test_interim_pdf_values_use_year_to_date_reporting_period() -> None:
    filing = _filing(
        period_end=date(2025, 3, 31),
        source="cvm",
        filing_type="interim",
    )

    financials = extract_pdf_text_financials(
        (
            """
            Demonstração do resultado
            Período encerrado em 31 de março de 2025
            R$ milhares
            2025
            2024
            Receita líquida
            2.500
            2.100
            Lucro operacional
            420
            390
            Lucro antes do imposto de renda
            380
            350
            """,
        ),
        filing,
        source_url="https://example.test/interim.pdf",
        sha256="e" * 64,
    )

    income = financials.income_statement()
    assert income is not None
    value = next(item for item in income.line_items if item.code == "revenue").values[0]
    assert value.period.start_date == date(2025, 1, 1)
    assert value.period.end_date == date(2025, 3, 31)
    assert value.period.fiscal_period == "Q"


def test_image_only_pdf_reports_clear_ocr_requirement() -> None:
    import pymupdf

    document = pymupdf.open()
    document.new_page()
    pdf_bytes = document.tobytes()
    document.close()

    with pytest.raises(FinancialsUnavailableError, match=r"image-only.*OCR"):
        extract_pdf_source_financials(
            pdf_bytes,
            _filing(source="sgx"),
            source_url="https://example.test/scanned.pdf",
            sha256="f" * 64,
        )


def test_ocr_pages_build_structured_financials() -> None:
    markdown = "\n\n".join(
        (
            "## Page 1\n\nCover page",
            f"## Page 2\n\n{_sgx_income_text()}",
            f"## Page 3\n\n{_sgx_position_text()}",
            f"## Page 4\n\n{_sgx_cash_flow_text()}",
        )
    )

    financials = extract_pdf_ocr_financials(
        markdown,
        _filing(period_end=date(2025, 6, 30)),
        source_url="https://example.test/scanned.pdf",
        sha256="1" * 64,
    )

    assert financials.extraction_method == "pdf-ocr-text"
    assert financials.fact_count >= 6
    assert financials.income_statement() is not None
    assert financials.balance_sheet() is not None
    assert financials.cash_flow_statement() is not None


def _filing(
    *,
    period_end: date = date(2025, 12, 31),
    source: str = "sgx",
    filing_type: str = "annual",
) -> Filing:
    return Filing(
        id="sg_sgx_report",
        company_id="sg_sgx_1J26",
        source=source,  # type: ignore[arg-type]
        source_id="report",
        title="2025 Annual Report",
        category="accounts",
        filing_type=filing_type,
        filing_date=date(2026, 3, 1),
        period_end=period_end,
        document_id="https://example.test/report.pdf",
        media_type="application/pdf",
        issuer_name="Example Limited",
        pdf_available=True,
        source_url="https://example.test/report.pdf",
    )


def _english_report() -> str:
    return """
    # Consolidated income statement

    S$ million

    |  | 2025 | 2024 |
    | --- | ---: | ---: |
    | Revenue | 1,234 | 1,100 |
    | Operating profit | 300 | 250 |
    | Profit before tax | 280 | 230 |
    | Profit for the year | 210 | 180 |

    # Statements of financial position

    S$ million

    |  | 2025 | 2024 |
    | --- | ---: | ---: |
    | Total assets | 5,000 | 4,500 |
    | Total liabilities | 3,000 | 2,700 |
    | Total equity | 2,000 | 1,800 |

    # Consolidated statement of cash flows

    S$ million

    |  | 2025 | 2024 |
    | --- | ---: | ---: |
    | Net cash from operating activities | 450 | 400 |
    | Net cash used in investing activities | (210) | (180) |
    """


def _nse_balance_sheet_text() -> str:
    return """
    Reliance Industries Limited
    Integrated Annual Report 2024-25
    (Rs in crore)
    Notes
    As at
    31st March, 2025
    As at
    31st March, 2024
    Assets
    Non-Current Assets
    Property, Plant and Equipment
    1
    2,67,096
    2,58,911
    Current Assets
    Inventories
    6
    89,216
    85,100
    Trade Receivables
    8
    15,591
    14,740
    Cash and Cash Equivalents
    9
    82,471
    69,248
    Total Assets
    10,22,401
    9,59,643
    Balance Sheet
    As at 31st March, 2025
    Equity and Liabilities
    Equity
    Total Equity
    5,43,087
    5,15,096
    Liabilities
    Non-Current Liabilities
    Total Non-Current Liabilities
    2,21,379
    2,04,533
    Current Liabilities
    Financial Liabilities
    Borrowings
    20
    26,788
    50,731
    Provisions
    24
    1,156
    972
    Total Current Liabilities
    2,57,935
    2,40,014
    Total Liabilities
    4,79,314
    4,44,547
    Total Equity and Liabilities
    10,22,401
    9,59,643
    """


def _cvm_balance_sheet_text() -> str:
    return """
    Balanco Patrimonial
    Exercicios findos em 31 de dezembro (Em milhoes de reais)
    Consolidado
    Passivo
    Notas
    2025
    2024
    Fornecedores
    17
    40.948
    37.659
    Passivo circulante
    198.368
    194.808
    Financiamentos
    30
    133.462
    127.539
    Passivo nao circulante
    607.434
    562.475
    Passivo circulante e nao circulante
    805.802
    757.283
    Patrimonio liquido
    417.587
    367.514
    """


def _group_company_current_liabilities_text() -> str:
    return """
    Balance Sheets
    as at 31 December 2025
    GROUP
    COMPANY
    Note
    2025
    $'000
    2024
    $'000
    2025
    $'000
    2024
    $'000
    Current liabilities
    Creditors
    23
    2,383,827
    2,730,241
    95,055
    95,514
    Provisions
    24
    120,174
    138,420
    -
    -
    Amounts due to:
    - subsidiaries
    19
    -
    -
    241,471
    184,010
    Term loans
    25
    1,906,467
    1,389,004
    1,457,963
    1,098,473
    4,960,122
    4,771,044
    1,811,938
    1,445,215
    Liabilities directly associated with disposal group and assets classified
    as held for sale
    38
    818,592
    -
    -
    -
    5,778,714
    4,771,044
    1,811,938
    1,445,215
    Net current assets
    1,984,113
    1,848,053
    8,826,589
    8,077,833
    Non-current liabilities
    10,122,923
    11,461,649
    9,207,590
    8,547,590
    """


def _net_assets_style_balance_sheet_text() -> str:
    return """
    Balance Sheets
    As at 31 December 2025
    Group
    Company
    Note
    2025
    $'000
    2024
    $'000
    2025
    $'000
    2024
    $'000
    Share capital & reserves
    9,000
    8,500
    5,000
    4,500
    Total equity
    9,300
    8,700
    5,200
    4,700
    Represented by:
    Long term assets
    17
    18,000
    19,000
    7,000
    7,500
    Current assets
    5,700
    6,600
    10,600
    9,500
    Current liabilities
    4,900
    4,700
    1,800
    1,400
    Net current assets
    800
    1,900
    8,800
    8,100
    Non-current liabilities
    Term loans
    25
    9,400
    10,500
    8,400
    8,100
    Lease liabilities
    100
    130
    -
    -
    9,500
    11,461
    8,647
    8,240
    Net assets
    9,300
    8,700
    5,200
    4,700
    """


def _income_statement_followed_by_equity_statement_text() -> str:
    return """
    Consolidated income statement
    for the year ended 31 December 2025
    In $ millions
    2025
    2024
    Profit before tax
    12,999
    12,884
    Income tax expense
    2,065
    1,594
    Net profit
    10,934
    11,290
    Consolidated statement of changes in equity
    for the year ended 31 December 2025
    The Group
    Share capital
    Other reserves
    Retained earnings
    Attributable to shareholders
    Total shareholders' funds
    Non-controlling interests
    Total
    Balance at 1 January
    -
    -
    -
    -
    -
    -
    -
    Net profit
    -
    -
    -
    11,289
    11,289
    1
    11,290
    Other comprehensive income
    -
    -
    1,689
    (118)
    1,571
    (1)
    1,570
    Balance at 31 December
    11,537
    2,392
    1,694
    53,163
    68,786
    47
    68,833
    (a) Includes distributions paid on capital securities classified
    as equity and Capital Return dividends declared in the year
    (see notes on pages 118 to 169 as well as the Risk Management
    section which form part of these financial statements)
    DBS Annual Report 2025 A beacon of stability
    114
    115
    Financial statements
    """


def _note_scoped_total_assets_text() -> str:
    return """
    Notes to the Financial Statements
    24. Interests in Structured Entities
    In $ millions
    2025
    2024
    Goodwill
    41
    39
    Total assets
    6,178
    5,666
    Total assets of the sponsored structured entities
    3,159
    1,114
    """


def _older_vintage_income_statement_text() -> str:
    return """
    Estado de resultados
    Años terminados el 31 de diciembre
    2022
    2021
    Ingresos
    99,999
    88,888
    Utilidad neta
    9,999
    8,888
    """


def _current_vintage_income_statement_text() -> str:
    return """
    Estado de resultados
    Años terminados el 31 de diciembre
    2023
    2022
    Ingresos
    17,388
    15,577
    Utilidad neta
    885
    778
    """


def _consolidated_balance_sheet_text() -> str:
    return """
    Consolidated balance sheet
    as at 31 December 2025
    In $ millions
    2025
    2024
    Goodwill
    6,139
    6,171
    Total assets
    897,488
    827,219
    """


def _balance_sheet_with_reused_label_text() -> str:
    return """
    Consolidated Balance Sheet
    as at 30 June 2025
    In $ millions
    2025
    Trade and other receivables
    5,452
    Inventories
    5,817
    Total assets
    116,012
    Total liabilities
    60,547
    Total equity
    55,465
    Note 12: Reconciliation of profit to net cash flow
    Changes in working capital
    Trade and other receivables
    (1,331)
    Inventories
    (672)
    """


def _segment_breakdown_income_text() -> str:
    return """
    Consolidated Income Statement
    For the year ended 31 December 2024
    Year ended 31 December
    2024
    2023
    Note
    RMB'Million
    RMB'Million
    Revenues
    Value-added Services
    319,168
    298,375
    Marketing Services
    121,374
    101,482
    FinTech and Business Services
    211,956
    203,763
    Others
    7,759
    5,395
    6
    660,257
    609,015
    Cost of revenues
    7
    (311,011)
    (315,906)
    Gross profit
    349,246
    293,109
    """


def _sgx_income_text() -> str:
    return """
    Statement of Comprehensive Income
    For the financial year ended 30 June 2025
    Group
    Company
    Note
    2025
    $'000
    2024
    $'000
    2025
    $'000
    2024
    $'000
    Operating revenue
    Fixed Income, Currencies and Commodities
    4
    350,059
    322,497
    -
    -
    Operating revenue less transaction-based expenses
    (net revenue)
    1,298,167
    1,162,028
    802,349
    760,046
    Operating profit
    8
    742,843
    606,382
    508,099
    480,564
    Profit before tax
    785,891
    709,017
    541,019
    272,343
    Net profit after tax
    648,127
    597,578
    535,455
    268,887
    """


def _sgx_position_text() -> str:
    return """
    Statement of Financial Position
    As at 30 June 2025
    Group
    Company
    2025
    $'000
    2024
    $'000
    2025
    $'000
    2024
    $'000
    Cash and cash equivalents
    11
    1,129,979
    998,111
    260,731
    182,296
    Current assets
    2,449,142
    2,247,017
    533,684
    386,505
    Total assets
    4,143,988
    3,984,962
    2,349,749
    2,206,343
    Total liabilities
    1,944,000
    2,024,000
    1,000,000
    900,000
    Total equity
    2,199,988
    1,960,962
    1,349,749
    1,306,343
    """


def _sgx_cash_flow_text() -> str:
    return """
    Consolidated Statement of Cash Flows
    For the financial year ended 30 June 2025
    Group
    2025
    $'000
    2024
    $'000
    Net cash generated from operating activities
    841,670
    615,798
    Net cash used in investing activities
    (210,000)
    (180,000)
    Net cash used in financing activities
    (450,000)
    (420,000)
    """
