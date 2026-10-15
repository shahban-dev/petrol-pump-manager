"""Phase 6 report data contract.

Shared by ``reporting_service`` (data), the export engine (PDF/XLSX/CSV)
and the Reports UI page. Every report is produced from **committed**
database records only; rows carry raw values (money as integer paisa,
fuel quantities as integer milliliters, prices as scaled integers) — all
formatting (Decimal conversion, rounding, locale digits) happens at the
render/export boundary, never in this contract.

Design plan §5.2: never float/REAL for money or quantities.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Stable report ids — the 21 reports from master-prompt §22.
REPORT_IDS: tuple[str, ...] = (
    "daily_register",
    "nozzle_readings",
    "fuel_sales",
    "cashbook",
    "sales_reconciliation",
    "customer_credit_register",
    "customer_statement",
    "payment_receipt",
    "outstanding_balances",
    "fuel_stock",
    "tank_reconciliation",
    "deliveries",
    "supplier_balances",
    "other_materials",
    "expenses",
    "pnl_daily",
    "pnl_monthly",
    "pnl_annual",
    "cash_difference",
    "audit_history",
    "backup_history",
)

#: Column kinds: text | money | quantity | price | date | number
#:   money    -> integer paisa, rendered as PKR
#:   quantity -> integer milliliters, rendered as liters (3 decimals)
#:   price    -> integer 0.0001 PKR/L, rendered as PKR per liter
#:   number   -> plain integer (counts)
#:   date     -> 'YYYY-MM-DD' business date, rendered DD-MM-YYYY


@dataclass(frozen=True)
class ReportColumn:
    """One column of a report table."""

    key: str
    label_en: str
    label_ur: str
    align: str = "left"  # left | right | center
    kind: str = "text"  # text | money | quantity | price | date | number


@dataclass(frozen=True)
class ReportFilter:
    """One accepted filter for a report."""

    name: str  # e.g. "date_from", "customer_id"
    label_en: str
    label_ur: str
    kind: str  # date | customer | fuel_product | category | text
    required: bool = False


@dataclass(frozen=True)
class ReportSpec:
    """Catalog entry describing a report and its filters."""

    report_id: str
    title_en: str
    title_ur: str
    filters: tuple[ReportFilter, ...] = ()


@dataclass
class ReportResult:
    """Fully computed report data, ready for preview/print/export."""

    report_id: str
    title_en: str
    title_ur: str
    generated_at_utc: str  # 'YYYY-MM-DDTHH:MM:SS.SSSZ'
    date_from: str | None = None  # 'YYYY-MM-DD'
    date_to: str | None = None  # 'YYYY-MM-DD'
    business_date: str | None = None  # single-day reports
    closing_revision: int | None = None  # set for reopened-day reports
    period_label_en: str = ""
    period_label_ur: str = ""
    columns: list[ReportColumn] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    totals: dict = field(default_factory=dict)  # column key -> raw total
    notes_en: list[str] = field(default_factory=list)
    notes_ur: list[str] = field(default_factory=list)
