"""Derive normalized financial statements from bounded Markdown PDF tables."""

from __future__ import annotations

import operator
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from rapidfuzz import fuzz

from openfilings.exceptions import ExtractionError, FinancialsUnavailableError
from openfilings.extraction.pdf import pdf_to_markdown
from openfilings.models import (
    Filing,
    FilingFinancials,
    FinancialLineItem,
    FinancialStatement,
    FinancialValue,
    ReportingPeriod,
    StatementType,
)
from openfilings.xbrl.mappings import LINE_ITEMS, LineItemDefinition

_YEAR_PATTERN = re.compile(r"\b(20\d{2})\b")
_DIVIDER_CELL_PATTERN = re.compile(r"^:?-{2,}:?$")
_FOOTNOTE_PATTERN = re.compile(r"(?:\s|\*)+(?:note\s*)?\d+[a-z]?$", re.IGNORECASE)
_WORD_PATTERN = re.compile(r"[^\W\d_]{2,}", re.UNICODE)
_PLACEHOLDER_CELL_VALUES = frozenset({"-", "\N{EM DASH}", "\N{EN DASH}", "n/a", "nm"})
# A row's own values sit within a short, contiguous run of lines after its
# label. A run of prose this long (footnotes, a page footer) means the scan
# has left the statement - a number found beyond it is not this row's value.
_MAX_CONSECUTIVE_NON_NUMERIC_LINES = 2
# A grand total describes the entity's full scope - a note reusing the same
# label (e.g. a structured-entity or subsidiary disclosure) can only be a
# subset of it, never larger. The same reasoning applies to the current/
# non-current subtotals: a filer that reports both standalone ("separate")
# and consolidated statements for the same fiscal year (common in
# Colombia) can have BOTH scopes' sections tie on period count. Without
# these in the same superset set as the "total_" codes, total_assets could
# be selected from the (larger) consolidated scope while current_assets/
# noncurrent_assets default to "first found" from the (smaller) standalone
# scope - producing an internally inconsistent balance sheet where
# current + non-current no longer sums to the total, even though each
# individual figure is a real, correctly-extracted number from the filing.
_SCOPE_SUPERSET_CODES = frozenset(
    {
        "total_assets",
        "total_liabilities",
        "total_equity",
        "current_assets",
        "noncurrent_assets",
        "current_liabilities",
        "noncurrent_liabilities",
    }
)
# A properly presented balance sheet never reports these as negative - a
# negative match is a sign the scan landed on an unrelated note (a cash-
# flow reconciliation adjustment, an impairment/amortization movement)
# rather than the entity's own asset balance, even though the label text
# matched. Confirmed live: Cemex's "Activos intangibles" alias matched a
# cash-flow note's period-on-period movement, not a real balance-sheet
# total - the note values are parenthesized (negative) by construction.
_NEVER_NEGATIVE_ASSET_CODES = frozenset(
    {
        "cash_and_cash_equivalents",
        "trade_receivables",
        "inventory",
        "property_plant_equipment",
        "goodwill",
        "intangible_assets",
        "current_assets",
        "noncurrent_assets",
        "total_assets",
    }
)
_NON_LABEL_PATTERN = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE_PATTERN = re.compile(r"\s+")
# Indian lakh/crore grouping (e.g. "2,57,935" or "10,22,401"): the last
# group is 3 digits, every group before it is 2 digits.
_INDIAN_GROUPING_PATTERN = re.compile(r"^\d{1,2}(,\d{2})*,\d{3}$")
_STATEMENT_TITLES: dict[StatementType, str] = {
    "income_statement": "Income statement",
    "balance_sheet": "Balance sheet",
    "cash_flow_statement": "Cash flow statement",
    "comprehensive_income": "Statement of comprehensive income",
    "changes_in_equity": "Statement of changes in equity",
}
_DEFINITIONS = {definition.code: definition for definition in LINE_ITEMS}
_LINE_ITEM_ALIASES: dict[str, tuple[str, ...]] = {
    "revenue": (
        "revenue",
        "total revenue",
        "net revenue",
        "operating revenue",
        "net sales",
        "sales revenue",
        "turnover",
        "receita liquida",
        "receita operacional liquida",
        "receitas",
        "ingresos",
        "ingresos de actividades ordinarias",
        "營業收入",
        "营业收入",
        "收入合計",
        "收益",
    ),
    "cost_of_revenue": (
        "cost of revenue",
        "cost of revenues",
        "cost of sales",
        "custo das vendas",
        "costo de ventas",
        "costo de actividades ordinarias",
        "營業成本",
        "营业成本",
    ),
    "gross_profit": (
        "gross profit",
        # "Proft" covers a PDF text-extraction artifact that drops the "i"
        # from the "fi" ligature (also seen as "offcer" for "officer" in
        # the same documents).
        "gross proft",
        "lucro bruto",
        "ganancia bruta",
        "utilidad bruta",
        "ganancia perdida bruta",
        "營業毛利",
        "营业毛利",
        "營業毛損",
    ),
    "operating_income_loss": (
        "operating profit",
        "operating loss",
        "profit from operations",
        "income from operations",
        "loss from operations",
        "lucro operacional",
        "prejuizo operacional",
        "ganancia operativa",
        "utilidad operativa",
        "ganancia perdida operativa",
        "utilidad de actividades operacionales",
        "ganancia de actividades operacionales",
        "營業利益",
        "营业利润",
        "營業損失",
    ),
    "profit_before_tax": (
        "profit before tax",
        "loss before tax",
        "profit before income tax",
        "income before income tax",
        "income before income taxes",
        "loss before income tax",
        "lucro antes do imposto de renda",
        "prejuizo antes do imposto de renda",
        "ganancia antes de impuestos",
        "utilidad antes de impuestos",
        "ganancia perdida antes de impuestos",
        "稅前淨利",
        "税前利润",
        "稅前淨損",
        "稅前利益",
    ),
    "income_tax": (
        "income tax expense",
        "income tax expenses",
        "income tax benefit",
        "imposto de renda e contribuicao social",
        "gasto por impuesto a las ganancias",
        "ingreso gasto por impuesto",
        "impuesto sobre las ganancias",
        "impuestos sobre las ganancias",
        "所得稅費用",
        "所得税费用",
        "所得稅利益",
    ),
    "net_income_loss": (
        "profit for the year",
        "loss for the year",
        "profit for the period",
        "loss for the period",
        "net profit",
        "net income",
        "net loss",
        "net profit after tax",
        "net loss after tax",
        "lucro liquido",
        "prejuizo liquido",
        "ganancia neta",
        "utilidad neta",
        "resultado del ejercicio",
        "ganancia perdida neta del ejercicio",
        "本期淨利",
        "净利润",
        "本期淨損",
        "本年度淨利",
        "本年度淨損",
    ),
    "basic_eps": (
        "basic earnings per share",
        "basic loss per share",
        "lucro basico por acao",
        "基本每股盈餘",
        "基本每股虧損",
    ),
    "diluted_eps": (
        "diluted earnings per share",
        "diluted loss per share",
        "lucro diluido por acao",
        "稀釋每股盈餘",
        "稀釋每股虧損",
    ),
    "total_comprehensive_income": (
        "total comprehensive income",
        "total comprehensive loss",
        "resultado abrangente total",
        "total resultado integral del ejercicio",
        "本期綜合損益總額",
    ),
    "cash_and_cash_equivalents": (
        "cash and cash equivalents",
        "caixa e equivalentes de caixa",
        "efectivo y equivalentes al efectivo",
        "現金及約當現金",
        "货币资金",
    ),
    "trade_receivables": (
        "trade and other receivables",
        "trade receivables",
        "contas a receber",
        "cuentas por cobrar comerciales",
        "應收帳款",
        "应收账款",
    ),
    "inventory": (
        "inventories",
        "inventory",
        "estoques",
        "inventarios",
        "存貨",
        "存货",
    ),
    "property_plant_equipment": (
        "property plant and equipment",
        "imobilizado",
        "propiedades planta y equipo",
        "不動產廠房及設備",
        "固定资产",
    ),
    "goodwill": ("goodwill", "agio", "商譽"),
    "intangible_assets": (
        "intangible assets",
        "intangivel",
        "activos intangibles",
        "無形資產",
    ),
    "current_assets": (
        "current assets",
        "total current assets",
        "ativo circulante",
        "activos corrientes",
        "total activos corrientes",
        # Peruvian bank balance sheets use the singular ("activo") rather
        # than the plural ("activos") used elsewhere.
        "activo corriente",
        "total activo corriente",
        # Mexican filers use "circulante" instead of "corriente".
        "activo circulante",
        "total activo circulante",
        "total del activo circulante",
        "流動資產",
        "流动资产",
    ),
    "noncurrent_assets": (
        "non current assets",
        "noncurrent assets",
        "total non current assets",
        "total noncurrent assets",
        "ativo nao circulante",
        "activos no corrientes",
        "total activos no corrientes",
        "activo no corriente",
        "total activo no corriente",
        "activo no circulante",
        "total activo no circulante",
        "total del activo no circulante",
        "非流動資產",
        "非流动资产",
    ),
    "total_assets": (
        "total assets",
        "ativo total",
        "total de activos",
        "total activos",
        "total activo",
        "total del activo",
        "資產總計",
        "资产总计",
    ),
    "current_liabilities": (
        "current liabilities",
        "total current liabilities",
        "passivo circulante",
        "pasivos corrientes",
        "total pasivos corrientes",
        "pasivo corriente",
        "total pasivo corriente",
        "pasivo circulante",
        "total pasivo circulante",
        "total del pasivo circulante",
        "流動負債",
        "流动负债",
    ),
    "noncurrent_liabilities": (
        "non current liabilities",
        "noncurrent liabilities",
        "total non current liabilities",
        "total noncurrent liabilities",
        "passivo nao circulante",
        "pasivos no corrientes",
        "total pasivos no corrientes",
        "pasivo no corriente",
        "total pasivo no corriente",
        "pasivo no circulante",
        "total pasivo no circulante",
        "total del pasivo no circulante",
        "非流動負債",
        "非流动负债",
    ),
    "total_liabilities": (
        "total liabilities",
        "passivo total",
        "passivo circulante e nao circulante",
        "total de pasivos",
        "total pasivos",
        "total pasivo",
        "total del pasivo",
        "負債總計",
        "负债合计",
        "負債總額",
    ),
    "total_equity": (
        "total equity",
        "net assets",
        "shareholders equity",
        "patrimonio liquido",
        "total patrimonio",
        "patrimonio atribuible a los propietarios",
        # Mexican filers use "Capital Contable" for equity instead of
        # "Patrimonio".
        "total del capital contable",
        "total de capital contable",
        "權益總計",
        "所有者权益合计",
        "權益總額",
    ),
    "operating_cash_flow": (
        "net cash from operating activities",
        "net cash used in operating activities",
        "net cash generated from operating activities",
        "cash generated from operating activities",
        "caixa liquido gerado pelas atividades operacionais",
        "flujos de efectivo y equivalente al efectivo procedente de utilizados "
        "en actividades de operacion",
        "營業活動之淨現金流入",
        "營業活動之淨現金流出",
        "營業活動淨現金流量",
    ),
    "investing_cash_flow": (
        "net cash from investing activities",
        "net cash used in investing activities",
        "net cash generated from investing activities",
        "caixa liquido aplicado nas atividades de investimento",
        "flujos de efectivo y equivalente al efectivo procedente de utilizados "
        "en actividades de inversion",
        "投資活動之淨現金流入",
        "投資活動之淨現金流出",
        "投資活動淨現金流量",
    ),
    "financing_cash_flow": (
        "net cash from financing activities",
        "net cash used in financing activities",
        "net cash generated from financing activities",
        "caixa liquido gerado pelas atividades de financiamento",
        "flujos de efectivo y equivalente al efectivo procedente de utilizados "
        "en actividades de financiacion",
        "融資活動之淨現金流入",
        "融資活動之淨現金流出",
        "籌資活動淨現金流量",
    ),
    "capital_expenditure": (
        "capital expenditure",
        "purchase of property plant and equipment",
        "aquisicao de imobilizado",
        "compra de propiedades planta y equipo",
        "取得不動產廠房及設備",
    ),
    "dividends_paid": (
        "dividends paid",
        "dividendos pagos",
        "支付股利",
        "發放現金股利",
    ),
    "net_change_in_cash": (
        "net change in cash and cash equivalents",
        "aumento liquido de caixa e equivalentes de caixa",
        "現金及約當現金淨增加",
        "現金及約當現金淨減少",
    ),
}
_CURRENCY_MARKERS = (
    ("SGD", ("s$", "sgd", "singapore dollar")),
    ("HKD", ("hk$", "hkd", "hong kong dollar")),
    ("BRL", ("r$", "brl", "reais", "real brasileiro")),
    ("TWD", ("nt$", "ntd", "twd", "新台幣")),
    ("CNY", ("rmb", "cny", "人民幣")),
    ("INR", ("inr", "₹", "indian rupee")),
    ("MXN", ("mxn", "mexican peso", "pesos mexicanos")),
    ("CAD", ("c$", "cad", "canadian dollar")),
    ("PEN", ("s/", "pen", "soles")),
    ("COP", ("cop", "pesos colombianos")),
    ("JPY", ("jpy", "円", "日圓")),
    ("USD", ("us$", "usd", "u.s. dollar")),
    ("EUR", ("eur", "€", "euro")),
    ("GBP", ("gbp", "£", "pound sterling")),
)
_DEFAULT_CURRENCY_BY_SOURCE = {
    "cvm": "BRL",
    "sgx": "SGD",
    "bmv": "MXN",
    "nse": "INR",
    "sedar": "CAD",
    "smv": "PEN",
    "sfc": "COP",
}
# Currencies a source's filings must never report, regardless of what the
# marker scan finds. "S/" is the Peruvian-sol thousands marker ("S/ 000")
# that also matches the bare "s/" PEN marker, so UK filings whose tables
# state scale as "S/ 000" (or which merely mention soles in a segment
# note) were labelled PEN (issue #6: Unilever 20-F via the FCA NSM).
# A filing retrieved from the FCA's UK storage mechanism must never be
# labelled Peruvian soles; when the scan says PEN for an FCA filing,
# fall through to the source default (None for fca_nsm) instead.
_NEVER_CURRENCY_BY_SOURCE = {
    "fca_nsm": frozenset({"PEN"}),
}
_STATEMENT_HEADINGS: dict[StatementType, tuple[str, ...]] = {
    "income_statement": (
        "statement of comprehensive income",
        "consolidated income statement",
        "income statement",
        "statement of profit or loss",
        # "Consolidated Statement of Profit or Loss and Other Comprehensive
        # Income" (Sheng Siong and other SGX filers) starts with
        # "consolidated", so it isn't a prefix-match for either
        # "consolidated income statement" or "statement of profit or loss"
        # on its own.
        "consolidated statement of profit or loss",
        "demonstracao do resultado",
        "estado de resultados",
        "estado del resultado",
        # Some filers (e.g. Cemex) use the plural "Estados de Resultados".
        "estados de resultados",
        # Colombian filers (e.g. Grupo Argos) use plural "Estados" with
        # singular "Resultado" and "del" instead of "de".
        "estados del resultado",
        "綜合損益表",
        "利润表",
        "損益表",
    ),
    "balance_sheet": (
        "statement of financial position",
        # Some filers (e.g. Sheng Siong) title this "Statements of
        # Financial Position" - plural - even when every other statement
        # in the same filing uses the singular "Statement of ...".
        "statements of financial position",
        "consolidated balance sheet",
        "balance sheet",
        "balanco patrimonial",
        "estado de situacion financiera",
        "estado de situación financiera",
        "estados de situacion financiera",
        "資產負債表",
        "资产负债表",
    ),
    "cash_flow_statement": (
        "consolidated statement of cash flows",
        "statement of cash flows",
        "cash flow statement",
        "demonstracao dos fluxos de caixa",
        "estado de flujos de efectivo",
        "estados de flujo de efectivo",
        "現金流量表",
        "现金流量表",
    ),
    "comprehensive_income": (
        "statement of other comprehensive income",
        "demonstracao do resultado abrangente",
        "estado de resultados integrales",
        "estados de utilidad integral",
        "estados de otro resultado integral",
        "其他綜合損益",
    ),
    "changes_in_equity": (
        "consolidated statement of changes in equity",
        "statement of changes in equity",
        "demonstracao das mutacoes do patrimonio liquido",
        "estado de cambios en el patrimonio",
        "estados de variaciones en el capital contable",
        "權益變動表",
        "所有者权益变动表",
    ),
}


@dataclass(frozen=True, slots=True)
class _MarkdownTable:
    context: str
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class _TableFormat:
    periods: tuple[tuple[int, int], ...]
    currency: str | None
    scale: Decimal
    decimals: str


def extract_pdf_table_financials(
    markdown: str,
    filing: Filing,
    *,
    source_url: str,
    sha256: str,
    extraction_method: str = "pdf-markdown-tables",
) -> FilingFinancials:
    """Return high-confidence standardized values from Markdown table rows."""

    selected: dict[str, FinancialLineItem] = {}
    for table in _markdown_tables(markdown):
        line_items = _line_items_from_table(table, filing)
        if len(line_items) < 2:
            continue
        for item in line_items:
            previous = selected.get(item.code)
            if previous is None or len(item.values) > len(previous.values):
                selected[item.code] = item

    statements = _statements(tuple(selected.values()))
    if not statements:
        raise FinancialsUnavailableError(
            "The PDF contains no high-confidence supported statement tables."
        )
    return FilingFinancials(
        filing_id=filing.id,
        company_id=filing.company_id,
        source_url=source_url,
        extraction_method=extraction_method,
        statements=statements,
        fact_count=sum(
            len(item.values)
            for statement in statements
            for item in statement.line_items
        ),
        taxonomy_namespaces=("pdf-derived",),
        sha256=sha256,
    )


def extract_pdf_text_financials(
    sections: tuple[str, ...],
    filing: Filing,
    *,
    source_url: str,
    sha256: str,
    extraction_method: str = "pdf-aligned-text",
) -> FilingFinancials:
    """Return standardized values from aligned text on statement pages."""

    selected: dict[str, FinancialLineItem] = {}
    for section in sections:
        section_items = _line_items_from_text(section, filing)
        if len(section_items) < 2:
            continue
        for item in section_items:
            previous = selected.get(item.code)
            if previous is None or len(item.values) > len(previous.values):
                selected[item.code] = item
                continue
            if len(item.values) != len(previous.values):
                continue
            if _period_ids(item.values) != _period_ids(previous.values):
                # A PDF can bundle multiple full annual reports (e.g. a
                # 20-F including 3 years of complete audited comparatives,
                # each covering its own 2-year pair). Two candidates tying
                # on period count here don't describe the same fiscal
                # years at all - they're from a different vintage/section,
                # not a scope difference - so the magnitude contest below
                # would blend unrelated years. The more recent vintage is
                # the filing's own current-year data.
                if _latest_end_date(item.values) > _latest_end_date(previous.values):
                    selected[item.code] = item
                continue
            if item.code in _SCOPE_SUPERSET_CODES and _magnitude(
                item.values
            ) > _magnitude(previous.values):
                # A grand total ("Total assets", "Total liabilities",
                # "Total equity") is definitionally the entity's full
                # scope - a note reusing the same label for a narrower
                # disclosure (a subsidiary, a structured entity) can only
                # describe a subset of it. When two candidates tie on the
                # same fiscal years, the larger one is the real total.
                selected[item.code] = item
    statements = _statements(tuple(selected.values()))
    if not statements:
        raise FinancialsUnavailableError(
            "The PDF contains no high-confidence aligned statement text."
        )
    return FilingFinancials(
        filing_id=filing.id,
        company_id=filing.company_id,
        source_url=source_url,
        extraction_method=extraction_method,
        statements=statements,
        fact_count=sum(
            len(item.values)
            for statement in statements
            for item in statement.line_items
        ),
        taxonomy_namespaces=("pdf-derived",),
        sha256=sha256,
    )


def extract_pdf_ocr_financials(
    markdown: str,
    filing: Filing,
    *,
    source_url: str,
    sha256: str,
) -> FilingFinancials:
    """Return structured values from page-delimited OCR output."""

    pages = tuple(
        page.strip()
        for page in re.split(r"(?m)^## Page \d+\s*$", markdown)
        if page.strip()
    )
    if not pages:
        pages = (markdown,)
    sections = tuple(
        f"{page}\n{pages[index + 1] if index + 1 < len(pages) else ''}"
        for index, page in enumerate(pages)
        if _statement_type(
            tuple(line.strip() for line in page.splitlines() if line.strip())
        )
        is not None
    )
    return extract_pdf_text_financials(
        sections,
        filing,
        source_url=source_url,
        sha256=sha256,
        extraction_method="pdf-ocr-text",
    )


def extract_pdf_source_financials(
    pdf_bytes: bytes,
    filing: Filing,
    *,
    source_url: str,
    sha256: str,
) -> FilingFinancials:
    """Extract a PDF through aligned text, then Markdown tables as fallback."""

    sections = _pdf_statement_sections(pdf_bytes)
    if sections:
        try:
            return extract_pdf_text_financials(
                sections,
                filing,
                source_url=source_url,
                sha256=sha256,
            )
        except FinancialsUnavailableError:
            pass
    try:
        markdown = pdf_to_markdown(pdf_bytes)
    except ExtractionError as exc:
        raise FinancialsUnavailableError(
            f"The filing PDF could not be read for financial tables: {exc}"
        ) from exc
    return extract_pdf_table_financials(
        markdown,
        filing,
        source_url=source_url,
        sha256=sha256,
    )


def _markdown_tables(markdown: str) -> tuple[_MarkdownTable, ...]:
    lines = [line.strip() for line in markdown.splitlines()]
    tables: list[_MarkdownTable] = []
    index = 0
    while index < len(lines):
        if not _is_table_line(lines[index]):
            index += 1
            continue
        start = index
        raw_rows: list[str] = []
        while index < len(lines) and _is_table_line(lines[index]):
            raw_rows.append(lines[index])
            index += 1
        rows = tuple(_split_row(row) for row in raw_rows if not _is_divider_row(row))
        if len(rows) >= 2:
            context = "\n".join(lines[max(0, start - 8) : start])
            tables.append(_MarkdownTable(context=context, rows=rows))
    return tuple(tables)


def _line_items_from_text(
    section: str, filing: Filing
) -> tuple[FinancialLineItem, ...]:
    lines = tuple(line.strip() for line in section.splitlines() if line.strip())
    statement_type = _statement_type(lines)
    years = _text_years(lines, filing.period_end)
    if statement_type is None or not years:
        return ()
    multiplier = _period_multiplier(lines, years)
    lines = _truncated_at_next_statement(lines, statement_type)
    header = "\n".join(lines[:30])
    currency = _currency(
        header, source=filing.source
    ) or _DEFAULT_CURRENCY_BY_SOURCE.get(filing.source)
    scale = _scale(header)
    decimals = f"-{len(str(int(scale))) - 1}" if scale > 1 else "0"
    items: dict[str, FinancialLineItem] = {}
    for index, label in enumerate(lines):
        definition = _definition_for_label(label)
        if definition is None or definition.statement_type != statement_type:
            continue
        numbers = _aligned_numbers(
            lines, index + 1, len(years), definition, multiplier=multiplier
        )
        if not numbers:
            continue
        if definition.code in _NEVER_NEGATIVE_ASSET_CODES and any(
            number < 0 for number in numbers
        ):
            continue
        values = _values_from_numbers(
            numbers,
            years,
            definition,
            filing,
            currency=currency,
            scale=scale,
            decimals=decimals,
        )
        previous = items.get(definition.code)
        concept = f"pdf-label:{_concept_label(label)}"
        # The exact same label text repeating within one statement page/
        # continuation window is a note reusing the primary statement's
        # own wording (e.g. a "changes in working capital" cash-flow
        # reconciliation reusing "Trade and other receivables" verbatim)
        # - not a different, more specific label that happens to resolve
        # to the same concept (where the later, more general total is
        # usually the real one). Keep the first occurrence unless the
        # later one is demonstrably more complete (more periods).
        if (
            previous is not None
            and previous.concept == concept
            and not _all_zero(previous.values)
            and (_all_zero(values) or len(values) <= len(previous.values))
        ):
            continue
        items[definition.code] = FinancialLineItem(
            code=definition.code,
            name=definition.name,
            concept=concept,
            values=values,
        )
    return tuple(items.values())


def _pdf_statement_sections(pdf_bytes: bytes) -> tuple[str, ...]:
    try:
        import pymupdf

        document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        try:
            page_texts = tuple(page.get_text() for page in document)
        finally:
            document.close()
    except Exception as exc:
        raise FinancialsUnavailableError(
            f"The filing PDF could not be inspected for statement pages: {exc}"
        ) from exc

    if sum(len(text.strip()) for text in page_texts) < 100:
        raise FinancialsUnavailableError(
            "The filing PDF is image-only; structured financial tables require "
            "OCR. Automatic OCR is used by OpenFilingsService when enabled and "
            "Tesseract is installed."
        )

    sections: list[str] = []
    for index, text in enumerate(page_texts):
        lines = tuple(line.strip() for line in text.splitlines() if line.strip())
        if _statement_type(lines) is None:
            continue
        continuation = page_texts[index + 1] if index + 1 < len(page_texts) else ""
        sections.append(f"{text}\n{continuation}")
    return tuple(sections)


def _line_items_from_table(
    table: _MarkdownTable, filing: Filing
) -> tuple[FinancialLineItem, ...]:
    table_format = _table_format(table, source=filing.source)
    if table_format is None:
        return ()
    if table_format.currency is None:
        table_format = _TableFormat(
            periods=table_format.periods,
            currency=_DEFAULT_CURRENCY_BY_SOURCE.get(filing.source),
            scale=table_format.scale,
            decimals=table_format.decimals,
        )
    items: dict[str, FinancialLineItem] = {}
    locked_codes: frozenset[str] = frozenset()
    first_value_column = min(column for column, _ in table_format.periods)
    for row in table.rows:
        if len(row) <= first_value_column:
            continue
        label = _row_label(row[:first_value_column])
        definition = _definition_for_label(label)
        if definition is None or definition.code in locked_codes:
            continue
        values = _row_values(row, definition, table_format, filing)
        if not values:
            continue
        if definition.code in _NEVER_NEGATIVE_ASSET_CODES and any(
            value.value < 0 for value in values
        ):
            continue
        previous = items.get(definition.code)
        concept = f"pdf-label:{_concept_label(label)}"
        if (
            previous is not None
            and previous.concept == concept
            and not _all_zero(previous.values)
            and (_all_zero(values) or len(values) <= len(previous.values))
        ):
            # See _line_items_from_text: the exact same label text
            # repeating within one table (e.g. a note reusing a balance-
            # sheet row's wording) is a reuse, not a more complete
            # restatement - keep the first occurrence.
            continue
        if (
            previous is not None
            and previous.concept != concept
            and _all_zero(values)
            and not _all_zero(previous.values)
        ):
            # IFRS balance sheets often repeat a label across both a
            # current and a non-current breakdown (e.g. "Cuentas por
            # Cobrar Comerciales" under both current and non-current
            # assets). A later all-zero row for the same code is far more
            # likely to be an unrelated, genuinely-empty sub-breakdown
            # than proof the earlier reported value was wrong.
            continue
        items[definition.code] = FinancialLineItem(
            code=definition.code,
            name=definition.name,
            concept=concept,
            values=values,
        )
        if definition.code == "current_assets":
            # A "Total Current Assets" row marks the end of the current-
            # asset breakdown. Everything gathered so far (cash, trade
            # receivables, inventory) is scoped to it - a non-current
            # section further down can reuse the exact same generic
            # labels for unrelated accounts and must not overwrite them.
            locked_codes = frozenset(items) - {"current_assets"}
    return tuple(items.values())


def _all_zero(values: tuple[FinancialValue, ...]) -> bool:
    return all(value.value == 0 for value in values)


def _magnitude(values: tuple[FinancialValue, ...]) -> Decimal:
    return sum((abs(value.value) for value in values), start=Decimal(0))


def _period_ids(values: tuple[FinancialValue, ...]) -> frozenset[str]:
    return frozenset(value.period.id for value in values)


def _latest_end_date(values: tuple[FinancialValue, ...]) -> date:
    return max(value.period.end_date for value in values)


def _table_format(
    table: _MarkdownTable, *, source: str | None = None
) -> _TableFormat | None:
    header = max(table.rows[:4], key=_year_cell_count)
    period_columns: list[tuple[int, int]] = []
    seen_years: set[int] = set()
    for column, cell in enumerate(header):
        match = _YEAR_PATTERN.search(cell)
        if match is None:
            continue
        year = int(match.group(1))
        if year not in seen_years:
            seen_years.add(year)
            period_columns.append((column, year))
    if not period_columns:
        return None
    context = f"{table.context}\n{' '.join(header)}"
    scale = _scale(context)
    return _TableFormat(
        periods=tuple(period_columns),
        currency=_currency(context, source=source),
        scale=scale,
        decimals=f"-{len(str(int(scale))) - 1}" if scale > 1 else "0",
    )


def _row_values(
    row: tuple[str, ...],
    definition: LineItemDefinition,
    table_format: _TableFormat,
    filing: Filing,
) -> tuple[FinancialValue, ...]:
    values: list[FinancialValue] = []
    scale = (
        Decimal(1)
        if definition.code in {"basic_eps", "diluted_eps"}
        else table_format.scale
    )
    for column, year in table_format.periods:
        if column >= len(row):
            continue
        number = _number(row[column])
        if number is None:
            continue
        period = _reporting_period(year, definition.statement_type, filing)
        unit = table_format.currency
        if definition.code in {"basic_eps", "diluted_eps"} and unit:
            unit = f"{unit} / shares"
        values.append(
            FinancialValue(
                period=period,
                value=number * scale,
                unit=unit,
                decimals=(
                    "0"
                    if definition.code in {"basic_eps", "diluted_eps"}
                    else table_format.decimals
                ),
                provenance="pdf_table",
                confidence=75,
            )
        )
    return tuple(values)


def _values_from_numbers(
    numbers: tuple[Decimal, ...],
    years: tuple[int, ...],
    definition: LineItemDefinition,
    filing: Filing,
    *,
    currency: str | None,
    scale: Decimal,
    decimals: str,
) -> tuple[FinancialValue, ...]:
    item_scale = (
        Decimal(1) if definition.code in {"basic_eps", "diluted_eps"} else scale
    )
    unit = currency
    if definition.code in {"basic_eps", "diluted_eps"} and unit:
        unit = f"{unit} / shares"
    return tuple(
        FinancialValue(
            period=_reporting_period(year, definition.statement_type, filing),
            value=number * item_scale,
            unit=unit,
            decimals=(
                "0" if definition.code in {"basic_eps", "diluted_eps"} else decimals
            ),
            provenance="pdf_table",
            confidence=75,
        )
        for number, year in zip(numbers, years, strict=True)
    )


def _aligned_numbers(
    lines: tuple[str, ...],
    start: int,
    period_count: int,
    definition: LineItemDefinition,
    *,
    multiplier: int = 1,
) -> tuple[Decimal, ...]:
    window = lines[start : start + 14]
    # Normally a row's own values sit directly on the lines right after its
    # label (a footnote digit may lead them), so the first period_count
    # numbers found are correct and scanning should not chase later,
    # unrelated rows. But when the label is immediately followed by
    # unlabeled sub-item text (e.g. "Revenues" broken down by segment with
    # no repeated "Total revenue" line), the row's own total instead sits
    # in the run of numbers closest to the next recognized line item. A
    # breakdown can span more lines than a plain row, so it gets a larger
    # scan window to still reach its own total.
    first_line = window[0] if window else None
    leads_with_text = (
        first_line is not None
        and not _is_placeholder_cell(first_line)
        and _number(first_line) is None
        and _definition_for_label(first_line) is None
    )
    if leads_with_text:
        return _breakdown_aligned_numbers(
            lines[start : start + 40 * multiplier],
            period_count,
            definition,
            multiplier=multiplier,
        )
    numbers: list[Decimal] = []
    consecutive_non_numeric = 0
    for line in window:
        next_definition = _definition_for_label(line)
        if next_definition is not None and next_definition.code != definition.code:
            break
        number = _number(line)
        if number is not None:
            consecutive_non_numeric = 0
            numbers.append(number)
            if len(numbers) > period_count:
                # One extra number beyond period_count is kept only to let
                # the footnote-marker check below strip a leading footnote
                # digit - anything past that is a later, unrelated row, not
                # this one's own values.
                break
            continue
        if _is_placeholder_cell(line):
            continue
        consecutive_non_numeric += 1
        if consecutive_non_numeric >= _MAX_CONSECUTIVE_NON_NUMERIC_LINES:
            # Two or more lines in a row that are neither a number, a blank
            # placeholder cell, nor a recognized label mean the scan has
            # wandered out of the statement and into footnote prose or a
            # page footer - a number found beyond that point (e.g. a bare
            # page number) is not this row's value.
            break
    if len(numbers) > period_count and _looks_like_note(numbers[0], numbers[1:]):
        numbers.pop(0)
    if len(numbers) < period_count:
        return ()
    return tuple(numbers[:period_count])


_MAX_SUB_LABEL_WORDS = 7
# "Net current assets"/"Net current liabilities" is a distinct working-
# capital metric (current assets minus current liabilities) that commonly
# follows the current-liabilities subtotal directly. It matches no line-
# item alias, so an unbounded breakdown scan would otherwise treat it as
# just another unlabeled sub-item and keep going, letting its own values
# become the new "last run" and silently replace the real subtotal found
# just before it.
_BREAKDOWN_BOUNDARY_PHRASES = ("net current assets", "net current liabilities")


def _breakdown_aligned_numbers(
    window: tuple[str, ...],
    period_count: int,
    definition: LineItemDefinition,
    *,
    multiplier: int = 1,
) -> tuple[Decimal, ...]:
    """Resolve a label followed by an unlabeled breakdown before its total.

    Numbers are grouped into runs split by non-numeric lines. The row's own
    total is the run closest to the next recognized line item, not the
    first run found.
    """
    runs: list[list[Decimal]] = [[]]
    consecutive_non_numeric = 0
    for line in window:
        next_definition = _definition_for_label(line)
        if next_definition is not None and next_definition.code != definition.code:
            break
        if _normalize_label(line) in _BREAKDOWN_BOUNDARY_PHRASES:
            break
        number = _number(line)
        if number is None:
            if runs[-1]:
                runs.append([])
            if _is_placeholder_cell(line):
                continue
            # A dual-entity layout (e.g. "Group"/"Company" columns) often
            # indents a sub-item across two short lines ("Amounts due
            # from:" / "- subsidiaries"), which would otherwise look
            # identical to genuine footnote prose leaking in. A short
            # fragment is still a label, not prose - only a long,
            # sentence-like line signals the scan has left the statement.
            if multiplier > 1 and len(line.split()) <= _MAX_SUB_LABEL_WORDS:
                continue
            consecutive_non_numeric += 1
            if consecutive_non_numeric >= _MAX_CONSECUTIVE_NON_NUMERIC_LINES:
                # See _aligned_numbers: a run of prose this long means the
                # scan has left the statement (footnotes, a page footer),
                # so no later run of numbers belongs to this row.
                break
            continue
        consecutive_non_numeric = 0
        runs[-1].append(number)
    runs = [run for run in runs if run]
    if not runs:
        return ()
    candidate = runs[-1]
    if len(candidate) > period_count and _looks_like_note(candidate[0], candidate[1:]):
        candidate = candidate[1:]
    if multiplier > 1:
        # Each logical row is period_count * multiplier numbers wide (e.g.
        # Group-2025, Group-2024, Company-2025, Company-2024), but a
        # missing separator between the last breakdown item and the
        # unlabeled total can merge them into one run with no marker to
        # split on. The total is always the most recent complete row
        # before the scan stopped - and only its first period_count
        # values (Group) are ever returned, regardless of which column
        # happens to be numerically larger.
        row_width = period_count * multiplier
        if len(candidate) >= row_width:
            candidate = candidate[-row_width:]
            return tuple(candidate[:period_count])
        return ()
    if len(candidate) > period_count:
        split = _footnote_split(candidate, period_count)
        if split is not None:
            candidate = candidate[split:]
    if len(candidate) < period_count:
        return ()
    if len(candidate) == period_count:
        return tuple(candidate)
    # Multiple rows can end up merged into one run when an unlabeled total
    # directly follows the last breakdown item with no textual separator at
    # all (not even a footnote digit _footnote_split could anchor on). A
    # genuine total is, by definition, the sum of the items before it, so
    # it's the largest-magnitude group of period_count consecutive values -
    # unlike a plain footnote lead, which _footnote_split already handles.
    return _largest_magnitude_window(candidate, period_count)


def _largest_magnitude_window(
    candidate: list[Decimal], period_count: int
) -> tuple[Decimal, ...]:
    best = candidate[:period_count]
    best_magnitude = sum(abs(value) for value in best)
    for start in range(1, len(candidate) - period_count + 1):
        window = candidate[start : start + period_count]
        magnitude = sum(abs(value) for value in window)
        if magnitude > best_magnitude:
            best, best_magnitude = window, magnitude
    return tuple(best)


def _footnote_split(candidate: list[Decimal], period_count: int) -> int | None:
    """Find a footnote marker directly ahead of the row's own values.

    A footnote reference can sit right before a row's total (possibly after
    an unlabeled breakdown sharing the same run, with nothing textual to
    separate them). If the marker is positioned so exactly `period_count`
    values follow it, that marker is the boundary.
    """
    index = len(candidate) - period_count - 1
    if index < 0:
        return None
    if _looks_like_note(candidate[index], candidate[index + 1 :]):
        return index + 1
    return None


def _looks_like_note(value: Decimal, following: list[Decimal]) -> bool:
    return (
        value == value.to_integral_value()
        and 0 <= value <= 99
        and any(abs(candidate) >= 100 for candidate in following)
    )


def _statement_type(lines: tuple[str, ...]) -> StatementType | None:
    for index in range(len(lines)):
        match = _heading_at(lines, index)
        if match is not None:
            return match
    return None


def _heading_at(lines: tuple[str, ...], index: int) -> StatementType | None:
    """Match a heading starting at `index`, joining up to two following
    lines first - PDF rendering can wrap a heading like "Consolidated
    statement of changes in equity" across separate extracted lines.

    Most headings sit in the first 12 lines, but a two-column layout can
    place the title between an assets table and a liabilities table below
    it, further down the page. Beyond the first 12 lines, a match only
    counts if the heading starts the window and the window looks like a
    heading rather than prose - otherwise an incidental sentence that
    happens to open with the same words (e.g. "Balance sheet dates, any
    protective rights...") would misclassify a notes page as a statement
    page.

    Within the first 12 lines, a match only requires the heading text to
    start the current line - but that line must itself start with an
    uppercase letter. A title is capitalized even when the rest of it is
    sentence case (e.g. "Demonstração do resultado"); a sentence wrapped
    across a page break can otherwise leave a lowercase mid-sentence
    fragment ("estado de resultados del año concluido...") starting a
    page's text, which is prose continuing from the previous page, not a
    heading. An MD&A subsection title like "Información del Estado de
    Resultados" ("Income Statement Information") contains the real heading
    as a substring further into the line, not at its start, and is
    excluded the same way.
    """
    window = " ".join(lines[index : index + 3])
    normalized_window = _normalize_label(window)
    starts_with_capital = bool(lines[index]) and lines[index][0].isupper()
    for statement_type, headings in _STATEMENT_HEADINGS.items():
        for heading in headings:
            normalized_heading = _normalize_label(heading)
            if (
                index < 12
                and starts_with_capital
                and normalized_window.startswith(normalized_heading)
            ):
                return statement_type
            is_prefix = normalized_window.startswith(normalized_heading)
            if not is_prefix:
                continue
            if _looks_like_heading(window, heading) or _followed_by_period_marker(
                normalized_window, normalized_heading
            ):
                return statement_type
    return None


def _followed_by_period_marker(normalized_window: str, normalized_heading: str) -> bool:
    """A genuine statement heading is immediately followed by its reporting
    period ("for the year ended ...", "as at ..."). Some filers set
    headings in sentence case ("Consolidated statement of changes in
    equity") rather than title case, which _looks_like_heading cannot tell
    apart from a prose sentence that happens to open with the same words -
    but a prose sentence is never followed by this exact formulaic phrase."""
    remainder = normalized_window[len(normalized_heading) :].strip()
    return remainder.startswith(("for the year", "for the period", "as at", "as of"))


def _looks_like_heading(text: str, heading: str) -> bool:
    """A genuine heading is short, and each of its words is capitalized in
    the source text - unlike a sentence that merely opens with the same
    words (only its first letter is capitalized as the sentence start).

    The length check only covers the words the heading itself needs, not
    the full lookahead window - a heading is routinely followed on the same
    joined window by a "for the year ended ..." subtitle line, which would
    otherwise push a perfectly short heading over the threshold."""
    words = text.strip().split()
    heading_span = " ".join(words[: len(heading.split())])
    if len(heading_span) > 80:
        return False
    for word in words[: len(heading.split())]:
        letters = [character for character in word if character.isalpha()]
        if letters and not letters[0].isupper():
            return False
    return True


def _truncated_at_next_statement(
    lines: tuple[str, ...], own_type: StatementType
) -> tuple[str, ...]:
    """A continuation page can bundle a different statement onto the end
    of this one (e.g. a Statement of Changes in Equity, a row-by-
    transaction matrix, follows an Income Statement page). Its row labels
    can reuse the same words with unrelated meaning (a "Net profit" row
    whose columns are equity components, not fiscal years), so scanning
    stops at the next different statement's heading rather than reading
    its content under this statement's type.

    The search starts past the first 12 lines - a page's own running
    header can list the current statement's title directly followed by a
    preview of the NEXT statement's title (e.g. "Balance sheets ... DBS
    Group Holdings Ltd and its Subsidiaries Consolidated statement of
    changes in equity"), both before any real row data. Treating that
    adjacent mention as a mid-page transition truncates the section down
    to nothing; a genuine bundled second statement only ever starts after
    real content, well beyond the header block.
    """
    for index in range(12, len(lines)):
        match = _heading_at(lines, index)
        if match is not None and match != own_type:
            return lines[:index]
    return lines


def _text_years(
    lines: tuple[str, ...], filing_period_end: date | None = None
) -> tuple[int, ...]:
    years: list[int] = []
    for line in lines[:30]:
        for match in _YEAR_PATTERN.finditer(line):
            year = int(match.group(1))
            # Grupo Argos SFC filing 125070 reports 2025/2024, but its income
            # statement places the auditor's "2 March 2026" signature before
            # those column headers. A year later than the known reporting
            # date is an issuance/audit date, never a statement-value column.
            if filing_period_end is not None and year > filing_period_end.year:
                continue
            if year not in years:
                years.append(year)
    return tuple(years[:2])


_GROUP_MARKERS = frozenset({"group", "the group"})
_COMPANY_MARKERS = frozenset({"company", "the company"})


def _period_multiplier(lines: tuple[str, ...], years: tuple[int, ...]) -> int:
    """Detect a repeated per-entity block in a statement's header (e.g.
    Singapore filers commonly show "Group" and "Company" columns side by
    side, each with the same year sequence: "2025 2024 2025 2024"). When
    detected, each row is twice as wide as the period count alone would
    suggest - the row's OWN Group values are always the first
    `period_count` of the (period_count * 2) numbers, but a breakdown's
    true subtotal can't be located without knowing the row is that wide.

    Both a "Group"/"Company" marker line and each year appearing at least
    twice are required - a title subtitle ("as at 31 December 2025") can
    legitimately repeat a year once more without the layout being dual-
    entity, so an exact count would false-negative on that page; the
    marker line is the real signal, the repeat count only confirms it.
    """
    if len(years) < 2:
        return 1
    header = lines[:30]
    normalized_header = [_normalize_label(line) for line in header]
    if not (
        any(line in _GROUP_MARKERS for line in normalized_header)
        and any(line in _COMPANY_MARKERS for line in normalized_header)
    ):
        return 1
    counts = {year: 0 for year in years}
    for line in header:
        for match in _YEAR_PATTERN.finditer(line):
            year = int(match.group(1))
            if year in counts:
                counts[year] += 1
    return 2 if all(count >= 2 for count in counts.values()) else 1


_BALANCE_SHEET_IDENTITY_CODES = ("total_assets", "total_liabilities", "total_equity")
_CURRENT_NONCURRENT_SUM_TARGETS = (
    ("total_assets", "current_assets", "noncurrent_assets"),
    ("total_liabilities", "current_liabilities", "noncurrent_liabilities"),
)


def _with_derived_current_noncurrent_totals(
    items: tuple[FinancialLineItem, ...],
) -> tuple[FinancialLineItem, ...]:
    """Derive a missing "Total assets"/"Total liabilities" from its current
    and non-current components when a filer states both halves of the
    balance sheet but never states the combined figure on the same page
    (e.g. some Singapore filers present the balancing figure only as "Net
    assets" further down, with no literal "Total assets" row at all)."""
    by_code = {item.code: item for item in items}
    added: list[FinancialLineItem] = []
    for target, current_code, noncurrent_code in _CURRENT_NONCURRENT_SUM_TARGETS:
        if target in by_code:
            continue
        current = by_code.get(current_code)
        noncurrent = by_code.get(noncurrent_code)
        if current is None or noncurrent is None:
            continue
        noncurrent_by_period = {value.period.id: value for value in noncurrent.values}
        values = tuple(
            FinancialValue(
                period=current_value.period,
                value=current_value.value
                + noncurrent_by_period[current_value.period.id].value,
                unit=current_value.unit,
                decimals=current_value.decimals,
                provenance="derived",
                confidence=min(
                    current_value.confidence,
                    noncurrent_by_period[current_value.period.id].confidence,
                ),
                derived_from=(current_code, noncurrent_code),
            )
            for current_value in current.values
            if current_value.period.id in noncurrent_by_period
        )
        if not values:
            continue
        definition = _DEFINITIONS[target]
        derived = FinancialLineItem(
            code=target,
            name=definition.name,
            concept="pdf-derived:current-noncurrent-sum",
            values=values,
        )
        added.append(derived)
        by_code[target] = derived
    return (*items, *added)


def _with_derived_balance_sheet_total(
    items: tuple[FinancialLineItem, ...],
) -> tuple[FinancialLineItem, ...]:
    """Derive a missing grand total from the other two via the accounting
    identity `total_assets = total_liabilities + total_equity` when a PDF
    heuristic finds exactly two of the three (e.g. a page never states a
    literal "Total assets" label, or a label-matching gap drops one)."""
    by_code = {item.code: item for item in items}
    present = [code for code in _BALANCE_SHEET_IDENTITY_CODES if code in by_code]
    if len(present) != 2:
        return items
    target = next(code for code in _BALANCE_SHEET_IDENTITY_CODES if code not in by_code)
    if target == "total_assets":
        base, subtrahend, op = (
            by_code["total_liabilities"],
            by_code["total_equity"],
            operator.add,
        )
    elif target == "total_liabilities":
        base, subtrahend, op = (
            by_code["total_assets"],
            by_code["total_equity"],
            operator.sub,
        )
    else:
        base, subtrahend, op = (
            by_code["total_assets"],
            by_code["total_liabilities"],
            operator.sub,
        )
    subtrahend_by_period = {value.period.id: value for value in subtrahend.values}
    values = tuple(
        FinancialValue(
            period=base_value.period,
            value=op(
                base_value.value, subtrahend_by_period[base_value.period.id].value
            ),
            unit=base_value.unit,
            decimals=base_value.decimals,
            provenance="derived",
            confidence=min(
                base_value.confidence,
                subtrahend_by_period[base_value.period.id].confidence,
            ),
            derived_from=(base.code, subtrahend.code),
        )
        for base_value in base.values
        if base_value.period.id in subtrahend_by_period
    )
    if not values:
        return items
    definition = _DEFINITIONS[target]
    derived = FinancialLineItem(
        code=target,
        name=definition.name,
        concept="pdf-derived:balance-sheet-identity",
        values=values,
    )
    return (*items, derived)


def _statements(
    items: tuple[FinancialLineItem, ...],
) -> tuple[FinancialStatement, ...]:
    items = _with_derived_current_noncurrent_totals(items)
    items = _with_derived_balance_sheet_total(items)
    statements: list[FinancialStatement] = []
    for statement_type, title in _STATEMENT_TITLES.items():
        statement_items = tuple(
            item
            for item in items
            if _DEFINITIONS[item.code].statement_type == statement_type
        )
        if not statement_items:
            continue
        currencies = {
            value.unit.split(" / ", 1)[0]
            for item in statement_items
            for value in item.values
            if value.unit
        }
        statements.append(
            FinancialStatement(
                statement_type=statement_type,
                title=title,
                currency=next(iter(currencies)) if len(currencies) == 1 else None,
                line_items=statement_items,
            )
        )
    return tuple(statements)


_DISQUALIFYING_SUFFIX_MARKERS = (
    # Ratio-analysis disclosures (e.g. "Inventory Turnover Ratio") start
    # with a line item's full name too, but are not that line item's value.
    "ratio",
    "turnover",
    "margin",
    "growth",
    "yield",
    "coverage",
    # A qualified variant (e.g. "Intangible Assets Under Development",
    # "Property Held for Sale") is a distinct line item from its base
    # category, not a formatting variant of the same value.
    "under development",
    "held for sale",
    "classified as held for sale",
    # "Activos no Corrientes o Grupos de Activos para su Disposicion
    # Clasificados como Mantenidos para la Venta..." is the IFRS 5 Spanish
    # disposal-group disclosure, scoped separately from the base category.
    # Some filers use the shorter form without the "o Grupos de" clause
    # ("Activos no Corrientes Mantenidos para la Venta"), which still needs
    # excluding from the base "activos no corrientes" category.
    "o grupos de",
    "mantenidos para la venta",
    # "Total Equity and Liabilities" is a grand total restating total
    # assets, not the equity line item alone. "Total Pasivos y Patrimonio"
    # is the same pattern in Spanish, restating total assets rather than
    # total liabilities alone. Mexican filers use "Capital Contable" for
    # equity instead of "Patrimonio" ("Total Pasivo y Capital Contable").
    "and liabilities",
    "y patrimonio",
    "y capital contable",
    # "Total Assets of the Sponsored Structured Entities" is scoped to a
    # narrow disclosure perimeter, not the entity's own total.
    "of the",
    "attributable to",
    # "Utilidad Neta de la Participación Controladora/No Controladora" is
    # net income attributable to just the controlling or non-controlling
    # interest - a split of consolidated net income, not the total itself.
    "de la participacion controladora",
    "de la participacion no controladora",
    # A filer reporting a discontinued-operations split states the same
    # subtotal label twice - once scoped to continuing operations, once to
    # discontinued operations (e.g. "Utilidad Antes de Impuestos por
    # Operaciones Continuadas" vs "Utilidad Antes de Impuestos Operaciones
    # Discontinuadas"). The discontinued-operations variant is a narrower
    # figure, not the entity's headline pre-tax profit or tax expense.
    "operaciones discontinuadas",
    "por operaciones discontinuadas",
    "discontinued operations",
)


def _definition_for_label(label: str) -> LineItemDefinition | None:
    normalized = _normalize_label(label)
    # An exact match is checked across every code first. A subtotal label
    # can otherwise start with a different line item's full name plus a
    # conjunction (e.g. "Passivo circulante e nao circulante" - total
    # liabilities - starts with "Passivo circulante" - current liabilities),
    # which the looser prefix match below would misclassify.
    for code, aliases in _LINE_ITEM_ALIASES.items():
        for alias in aliases:
            if normalized == _normalize_label(alias):
                return _DEFINITIONS[code]
    for code, aliases in _LINE_ITEM_ALIASES.items():
        for alias in aliases:
            normalized_alias = _normalize_label(alias)
            if not normalized.startswith(normalized_alias):
                continue
            remainder = normalized[len(normalized_alias) :]
            if " " in normalized_alias:
                # A multi-word alias requires a space before any
                # continuation (e.g. "Passivo circulante e nao
                # circulante" - total liabilities - starting with
                # "Passivo circulante" - current liabilities).
                is_prefix = remainder.startswith(" ")
            else:
                # A single-word alias only accepts a directly-glued
                # continuation (e.g. plural "Revenues"), never a new,
                # space-separated word - otherwise any prose sentence
                # starting with that word matches too (e.g. "Goodwill is
                # reviewed on an annual basis...", "Revenue Reserves").
                is_prefix = not remainder.startswith(" ")
            disqualified = _has_disqualifying_suffix(normalized, normalized_alias)
            if is_prefix and not disqualified:
                return _DEFINITIONS[code]
    return _fuzzy_definition_for_label(normalized)


_FUZZY_MATCH_MIN_SCORE = 90
_FUZZY_MATCH_MIN_MARGIN = 5
_FUZZY_MATCH_MAX_LENGTH = 45


def _fuzzy_definition_for_label(normalized: str) -> LineItemDefinition | None:
    """A last-resort fallback for near-miss spellings a PDF's text layer
    can introduce - a dropped ligature ("Proft" for "Profit"), an OCR
    misread, a missing accent - that exact and prefix matching won't
    catch verbatim.

    Deliberately conservative in three ways: whole-string ratio scoring
    already penalizes length mismatches on its own, so a label carrying
    extra qualifying text ("... Under Development", "... Attributable
    To") scores far below the threshold without needing the disqualifying
    -suffix list to catch it - but NOT far enough below for a leading
    "Other" qualifier ("Other non-current liabilities" scores 88.5
    against "non current liabilities", confirmed live on Keppel's filing:
    it silently overwrote the real Non-current liabilities subtotal with
    just that one sub-item's value). The threshold is set above the
    worst observed "Other X" collision (88.5) while still catching real
    typos (91.7+). "Current" and "noncurrent" (and "operating"/
    "investing"/"financing" cash flow) aliases are themselves 90%+
    similar to each other by construction too - a flat threshold alone
    isn't safe on its own, so the best match must also clear the runner-
    up (from a DIFFERENT code) by a minimum margin, or the match is
    ambiguous and rejected rather than guessed at. The label is
    additionally capped to roughly the length of the longest alias, so a
    long prose sentence that merely shares some words with an alias can
    never win a fuzzy match.
    """
    if len(normalized) > _FUZZY_MATCH_MAX_LENGTH:
        return None
    best_code: str | None = None
    best_score = 0.0
    runner_up_score = 0.0
    for code, aliases in _LINE_ITEM_ALIASES.items():
        code_best_score = max(
            (fuzz.ratio(normalized, _normalize_label(alias)) for alias in aliases),
            default=0.0,
        )
        if code_best_score > best_score:
            best_code, best_score, runner_up_score = code, code_best_score, best_score
        elif code_best_score > runner_up_score:
            runner_up_score = code_best_score
    if (
        best_code is not None
        and best_score > _FUZZY_MATCH_MIN_SCORE
        and best_score - runner_up_score >= _FUZZY_MATCH_MIN_MARGIN
    ):
        return _DEFINITIONS[best_code]
    return None


def _has_disqualifying_suffix(normalized: str, normalized_alias: str) -> bool:
    """A prefix match can land on a different disclosure or line item that
    merely starts with the same words (see _DISQUALIFYING_SUFFIX_MARKERS)."""
    suffix = normalized[len(normalized_alias) :].strip()
    return any(
        suffix.startswith(_normalize_label(marker))
        for marker in _DISQUALIFYING_SUFFIX_MARKERS
    )


def _reporting_period(
    year: int, statement_type: StatementType, filing: Filing
) -> ReportingPeriod:
    end_date = _period_end(year, filing.period_end)
    if statement_type == "balance_sheet":
        return ReportingPeriod(
            id=f"pdf-instant-{end_date.isoformat()}",
            end_date=end_date,
            kind="instant",
            fiscal_period="instant",
        )
    start_date = _duration_start(end_date, filing)
    fiscal_period = _fiscal_period(start_date, end_date)
    return ReportingPeriod(
        id=f"pdf-{fiscal_period.casefold()}-{end_date.isoformat()}",
        start_date=start_date,
        end_date=end_date,
        kind="duration",
        fiscal_period=fiscal_period,
    )


def _period_end(year: int, filing_period_end: date | None) -> date:
    if filing_period_end is None:
        return date(year, 12, 31)
    return _replace_year(filing_period_end, year)


def _replace_year(value: date, year: int) -> date:
    try:
        return value.replace(year=year)
    except ValueError:
        return value.replace(year=year, day=28)


def _duration_start(end_date: date, filing: Filing) -> date:
    if filing.period_start is not None:
        return _replace_year(filing.period_start, end_date.year)
    if filing.filing_type.casefold() == "annual":
        previous_end = _replace_year(end_date, end_date.year - 1)
        return previous_end + timedelta(days=1)
    return date(end_date.year, 1, 1)


def _fiscal_period(start_date: date, end_date: date) -> str:
    days = (end_date - start_date).days + 1
    if 75 <= days <= 105:
        return "Q"
    if 170 <= days <= 200:
        return "H1"
    if 250 <= days <= 290:
        return "9M"
    if 300 <= days <= 380:
        return "FY"
    return "YTD"


def _row_label(cells: tuple[str, ...]) -> str:
    for cell in cells:
        clean = _FOOTNOTE_PATTERN.sub("", cell).strip()
        if clean and not clean.isdigit() and clean.casefold() not in {"note", "notes"}:
            return clean
    return ""


def _is_placeholder_cell(value: str) -> bool:
    """A blank/not-applicable table cell (e.g. "-" for a zero or n/a
    column), not prose - it shouldn't count as leaving the statement the
    way real footnote or footer text does."""
    clean = value.strip().replace("\u00a0", " ")
    return not clean or clean.casefold() in _PLACEHOLDER_CELL_VALUES


def _number(value: str) -> Decimal | None:
    clean = value.strip().replace("\u00a0", " ")
    if not clean or clean.casefold() in _PLACEHOLDER_CELL_VALUES:
        return None
    if _WORD_PATTERN.search(clean) is not None:
        # A page header/footer ("DBS Annual Report 2025 ... 115") or a
        # stray note reference ("Note 27") carries a real word alongside
        # an embedded digit. Stripping every non-numeric character would
        # otherwise turn that digit into a false "value" for whatever row
        # a forward scan happens to be looking for. A genuine numeric cell
        # never contains a run of letters this long (currency symbols and
        # single-letter unit suffixes are one character, footnote markers
        # like "(a)" are parenthesized).
        return None
    negative = clean.startswith("(") and clean.endswith(")")
    clean = clean.strip("() ").replace(" ", "").replace("'", "")
    clean = re.sub(r"[^0-9,.-]", "", clean)
    if clean.count("-") > 1 or ("-" in clean and not clean.startswith("-")):
        return None
    clean = _normalized_number(clean)
    try:
        number = Decimal(clean)
    except InvalidOperation:
        return None
    return -abs(number) if negative else number


def _normalized_number(value: str) -> str:
    unsigned = value.lstrip("-")
    sign = "-" if value.startswith("-") else ""
    if "," in unsigned and "." in unsigned:
        decimal = "," if unsigned.rfind(",") > unsigned.rfind(".") else "."
        grouping = "." if decimal == "," else ","
        return sign + unsigned.replace(grouping, "").replace(decimal, ".")
    if "," in unsigned:
        return sign + _single_separator_number(unsigned, ",")
    if "." in unsigned:
        return sign + _single_separator_number(unsigned, ".")
    return value


def _single_separator_number(value: str, separator: str) -> str:
    groups = value.split(separator)
    if len(groups) > 1 and all(len(group) == 3 for group in groups[1:]):
        return "".join(groups)
    if separator == "," and _INDIAN_GROUPING_PATTERN.fullmatch(value):
        return value.replace(",", "")
    return value.replace(separator, ".")


def _currency(value: str, *, source: str | None = None) -> str | None:
    normalized = value.casefold()
    detected = next(
        (
            code
            for code, markers in _CURRENCY_MARKERS
            if any(_has_currency_marker(normalized, marker) for marker in markers)
        ),
        None,
    )
    if (
        detected is not None
        and source is not None
        and detected in _NEVER_CURRENCY_BY_SOURCE.get(source, frozenset())
    ):
        return None
    return detected


def _has_currency_marker(value: str, marker: str) -> bool:
    normalized_marker = marker.casefold()
    if normalized_marker.isascii() and normalized_marker.replace(" ", "").isalnum():
        return bool(
            re.search(
                rf"(?<!\w){re.escape(normalized_marker)}(?!\w)",
                value,
            )
        )
    return normalized_marker in value


def _scale(value: str) -> Decimal:
    normalized = _normalize_label(value)
    if any(marker in normalized for marker in ("billion", "bilhoes", "十億元")):
        return Decimal(1_000_000_000)
    if any(marker in normalized for marker in ("million", "milhoes", "百萬元", "百萬")):
        return Decimal(1_000_000)
    if any(marker in normalized for marker in ("thousand", "milhares", "千元", "仟元")):
        return Decimal(1_000)
    # Indian filers state amounts in crore (10^7) or lakh (10^5) - "(₹ in
    # crore)" is the near-universal header on SEBI-regulated annual reports.
    # Without these, an Indian PDF's figures come out understated by seven
    # orders of magnitude and, worse, land in the same multi-period series
    # as correctly scaled tagged-XBRL values from the same issuer.
    if any(marker in normalized for marker in ("crore", "crores")):
        return Decimal(10_000_000)
    if any(marker in normalized for marker in ("lakh", "lakhs", "lacs")):
        return Decimal(100_000)
    # "000" is only a scale marker as its own token (e.g. "$'000", "S/
    # 000") - _normalize_label strips the surrounding punctuation
    # ("$'000" becomes a standalone "000"), but a plain figure that
    # happens to contain three consecutive zeros (e.g. "8,600,031" ->
    # "8600031") must not trigger a false 1000x scale on the entire
    # table. Confirmed live: Peru's BCP filing had a cash-flow context
    # figure "8600031" silently applying a 1000x multiplier to unrelated
    # cash/dividend values on the same table.
    if "000" in normalized.split():
        return Decimal(1_000)
    return Decimal(1)


def _normalize_label(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_marks = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return _WHITESPACE_PATTERN.sub(
        " ", _NON_LABEL_PATTERN.sub(" ", without_marks)
    ).strip()


def _concept_label(value: str) -> str:
    return _normalize_label(value).replace(" ", "-") or "unknown"


def _is_table_line(value: str) -> bool:
    return value.startswith("|") and value.endswith("|") and value.count("|") >= 3


def _split_row(value: str) -> tuple[str, ...]:
    return tuple(cell.strip() for cell in value.strip("|").split("|"))


def _is_divider_row(value: str) -> bool:
    cells = _split_row(value)
    return bool(cells) and all(_DIVIDER_CELL_PATTERN.fullmatch(cell) for cell in cells)


def _year_cell_count(row: tuple[str, ...]) -> int:
    return sum(_YEAR_PATTERN.search(cell) is not None for cell in row)
