"""Cross-taxonomy line-item mappings for UK-GAAP and IFRS reporters."""

from __future__ import annotations

import re
from dataclasses import dataclass

from openfilings.models import StatementType


@dataclass(frozen=True, slots=True)
class LineItemDefinition:
    code: str
    name: str
    statement_type: StatementType
    concepts: tuple[str, ...]


def _normalize_concept(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


LINE_ITEMS = (
    LineItemDefinition(
        "revenue",
        "Revenue",
        "income_statement",
        (
            "Revenue",
            "Turnover",
            "TurnoverRevenue",
            "RevenueFromSaleOfGoodsAndServices",
            "RevenueFromContractsWithCustomers",
            "SalesRevenue",
            "SalesRevenueNet",
            "NetSales",
            "OperatingRevenue1",
            "OperatingRevenue",
        ),
    ),
    LineItemDefinition(
        "cost_of_revenue",
        "Cost of revenue",
        "income_statement",
        ("CostOfSales", "CostOfRevenue", "CostOfGoodsSold"),
    ),
    LineItemDefinition(
        "gross_profit",
        "Gross profit",
        "income_statement",
        ("GrossProfit", "GrossProfitLoss"),
    ),
    LineItemDefinition(
        "operating_income_loss",
        "Operating profit (loss)",
        "income_statement",
        (
            "ProfitLossFromOperatingActivities",
            "OperatingProfitLoss",
            "OperatingProfit",
            "OperatingIncomeLoss",
        ),
    ),
    LineItemDefinition(
        "finance_income",
        "Finance income",
        "income_statement",
        ("FinanceIncome", "InterestIncome", "InvestmentIncome"),
    ),
    LineItemDefinition(
        "finance_costs",
        "Finance costs",
        "income_statement",
        ("FinanceCosts", "InterestExpense", "InterestPayable"),
    ),
    LineItemDefinition(
        "profit_before_tax",
        "Profit (loss) before tax",
        "income_statement",
        (
            "ProfitLossBeforeTax",
            "ProfitLossOnOrdinaryActivitiesBeforeTax",
            "ProfitBeforeTax",
            "IncomeBeforeIncomeTaxes",
            "OrdinaryIncomeLoss",
        ),
    ),
    LineItemDefinition(
        "income_tax",
        "Income tax expense (benefit)",
        "income_statement",
        (
            "IncomeTaxExpenseContinuingOperations",
            "TaxExpenseIncome",
            "TaxOnProfitOrLossOnOrdinaryActivities",
        ),
    ),
    LineItemDefinition(
        "net_income_loss",
        "Profit (loss) for the period",
        "income_statement",
        (
            "ProfitLoss",
            "ProfitLossForPeriod",
            "ProfitLossForFinancialYear",
            "ProfitLossOnOrdinaryActivitiesAfterTax",
            "ProfitLossAttributableToOwnersOfParent",
            "ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults",
        ),
    ),
    LineItemDefinition(
        "basic_eps",
        "Basic earnings per share",
        "income_statement",
        ("BasicEarningsLossPerShare", "BasicEarningsPerShare"),
    ),
    LineItemDefinition(
        "diluted_eps",
        "Diluted earnings per share",
        "income_statement",
        ("DilutedEarningsLossPerShare", "DilutedEarningsPerShare"),
    ),
    LineItemDefinition(
        "total_comprehensive_income",
        "Total comprehensive income",
        "comprehensive_income",
        (
            "ComprehensiveIncome",
            "ComprehensiveIncomeForTheYear",
            "OtherComprehensiveIncome",
        ),
    ),
    LineItemDefinition(
        "cash_and_cash_equivalents",
        "Cash and cash equivalents",
        "balance_sheet",
        (
            "CashAndCashEquivalents",
            "CashAndCashEquivalentsAtCarryingValue",
            "CashBankOnHand",
            "CashAndDeposits",
        ),
    ),
    LineItemDefinition(
        "trade_receivables",
        "Trade and other receivables",
        "balance_sheet",
        (
            "TradeAndOtherCurrentReceivables",
            "Debtors",
            "TradeReceivables",
            "NotesAndAccountsReceivableTrade",
            "NotesAccountsReceivableTradeAndContractAssets",
        ),
    ),
    LineItemDefinition(
        "inventory",
        "Inventories",
        "balance_sheet",
        ("Inventories", "Stocks", "Inventory", "MerchandiseAndFinishedGoods"),
    ),
    LineItemDefinition(
        "property_plant_equipment",
        "Property, plant and equipment",
        "balance_sheet",
        (
            "PropertyPlantAndEquipment",
            "PropertyPlantAndEquipmentCarryingAmount",
            "TangibleFixedAssets",
            "PropertyPlantAndEquipmentNet",
        ),
    ),
    LineItemDefinition(
        "goodwill",
        "Goodwill",
        "balance_sheet",
        ("Goodwill",),
    ),
    LineItemDefinition(
        "intangible_assets",
        "Intangible assets",
        "balance_sheet",
        (
            "IntangibleAssetsOtherThanGoodwill",
            "IntangibleAssets",
            "IntangibleFixedAssets",
        ),
    ),
    LineItemDefinition(
        "current_assets",
        "Current assets",
        "balance_sheet",
        ("CurrentAssets",),
    ),
    LineItemDefinition(
        "noncurrent_assets",
        "Non-current assets",
        "balance_sheet",
        ("NoncurrentAssets", "FixedAssets"),
    ),
    LineItemDefinition(
        "total_assets",
        "Total assets",
        "balance_sheet",
        ("Assets", "TotalAssets"),
    ),
    LineItemDefinition(
        "current_liabilities",
        "Current liabilities",
        "balance_sheet",
        ("CurrentLiabilities", "CreditorsDueWithinOneYear"),
    ),
    LineItemDefinition(
        "noncurrent_liabilities",
        "Non-current liabilities",
        "balance_sheet",
        ("NoncurrentLiabilities", "CreditorsDueAfterOneYear"),
    ),
    LineItemDefinition(
        "subordinated_liabilities",
        "Subordinated liabilities",
        "balance_sheet",
        ("SubordinatedLiabilities",),
    ),
    LineItemDefinition(
        "total_liabilities",
        "Total liabilities",
        "balance_sheet",
        ("Liabilities", "TotalLiabilities"),
    ),
    LineItemDefinition(
        "total_equity",
        "Total equity",
        "balance_sheet",
        (
            "Equity",
            "TotalEquity",
            "EquityAttributableToOwnersOfParent",
            "ShareholdersFunds",
            "NetAssets",
            "ShareholdersEquity",
        ),
    ),
    LineItemDefinition(
        "operating_cash_flow",
        "Net cash from operating activities",
        "cash_flow_statement",
        (
            "CashFlowsFromUsedInOperatingActivities",
            "NetCashFlowsFromUsedInOperatingActivities",
            "NetCashFlowFromOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivities",
        ),
    ),
    LineItemDefinition(
        "investing_cash_flow",
        "Net cash from investing activities",
        "cash_flow_statement",
        (
            "CashFlowsFromUsedInInvestingActivities",
            "NetCashFlowsFromUsedInInvestingActivities",
            "NetCashFlowFromInvestingActivities",
            "NetCashProvidedByUsedInInvestingActivities",
        ),
    ),
    LineItemDefinition(
        "financing_cash_flow",
        "Net cash from financing activities",
        "cash_flow_statement",
        (
            "CashFlowsFromUsedInFinancingActivities",
            "NetCashFlowsFromUsedInFinancingActivities",
            "NetCashFlowFromFinancingActivities",
            "NetCashProvidedByUsedInFinancingActivities",
        ),
    ),
    LineItemDefinition(
        "capital_expenditure",
        "Capital expenditure",
        "cash_flow_statement",
        (
            "PurchaseOfPropertyPlantAndEquipment",
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PurchaseOfTangibleFixedAssets",
            "PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssets",
        ),
    ),
    LineItemDefinition(
        "dividends_paid",
        "Dividends paid",
        "cash_flow_statement",
        ("DividendsPaid", "DividendsPaidClassifiedAsFinancingActivities"),
    ),
    LineItemDefinition(
        "net_change_in_cash",
        "Net change in cash",
        "cash_flow_statement",
        (
            "IncreaseDecreaseInCashAndCashEquivalents",
            "NetIncreaseDecreaseInCashAndCashEquivalents",
        ),
    ),
)

_BY_CONCEPT = {
    _normalize_concept(concept): definition
    for definition in LINE_ITEMS
    for concept in definition.concepts
}


def definition_for_concept(concept: str) -> LineItemDefinition | None:
    """Map a taxonomy-qualified concept to a standardized definition."""

    local_name = concept.rsplit(":", 1)[-1]
    return _BY_CONCEPT.get(_normalize_concept(local_name))


def concept_priority(concept: str) -> int:
    """Rank a concept among the aliases of the line item it maps to.

    Lower is better; the alias tuples are written most-authoritative first.
    A statement can legitimately tag several aliases of one line item - an
    IFRS balance sheet carries both ``EquityAttributableToOwnersOfParent``
    and the real ``Equity`` total - and taking whichever appears first in
    the document silently picks the parent-only figure for any group with
    non-controlling interests, breaking assets = liabilities + equity by
    exactly the NCI. Confirmed live on BIM (Turkey/KAP).
    """

    definition = definition_for_concept(concept)
    if definition is None:
        return len(_LONGEST_ALIAS_TUPLE)
    local_name = _normalize_concept(concept.rsplit(":", 1)[-1])
    for index, alias in enumerate(definition.concepts):
        if _normalize_concept(alias) == local_name:
            return index
    return len(definition.concepts)


_LONGEST_ALIAS_TUPLE = max(
    (definition.concepts for definition in LINE_ITEMS), key=len, default=()
)
