"""Phase-6 reporting service: the 21 read-only reports (master-prompt §22).

Every report is computed from **committed** records only — existing views
(``v_cashbook``, ``v_daily_cash_position``, ``v_effective_settlements``,
``v_customer_ledger``, ``v_outstanding_receivables``, ``v_supplier_balances``,
…) and the read paths of the Phase 2–5 services. The service never writes;
it holds no transaction, only read connections.

Accounting identities reproduced here come from the design plan, not from
new formulas:

* §6 — ``meter_revenue == settled_against_today_sales + new_receivables``
  (old-debt recovery is a cash receipt, never revenue);
* §7 — ``outstanding = opening + Σ ticket gross − Σ paid_now
  − Σ allocations ± Σ adjustments`` (via ``v_customer_ledger``);
* §8 — daily periodic weighted-average COGS; P&L for closed days is read
  from the immutable closing-record snapshot (``AccountingService``), so
  old P&L never changes; longer periods sum daily snapshots.

Rows carry raw integers (money = paisa, quantities = milliliters, prices =
0.0001 PKR/L scaled integers) — never float. Formatting happens at the
render/export boundary per the ``app.models.report`` contract.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace

from app.calculations import time as timecalc
from app.models.report import (
    REPORT_IDS,
    ReportColumn,
    ReportFilter,
    ReportResult,
    ReportSpec,
)
from app.services import errors
from app.services._base import ServiceBase
from app.services.accounting_service import AccountingService
from app.services.cashbook_service import CashbookService
from app.services.customer_credit_service import CustomerCreditService
from app.services.delivery_service import DeliveryService
from app.services.expense_service import ExpenseService
from app.services.inventory_service import InventoryService
from app.services.nozzle_service import NozzleService
from app.services.other_materials_service import OtherMaterialsService
from app.services.payment_service import PaymentService
from app.services.price_service import PriceService
from app.services.sales_service import SalesService
from app.services.supplier_service import SupplierService

#: Cap for the audit-history report; the audit log is append-only and can
#: grow large. Truncation is disclosed in the report notes.
_AUDIT_ROW_LIMIT = 5000

_EN_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_UR_MONTHS = (
    "جنوری", "فروری", "مارچ", "اپریل", "مئی", "جون",
    "جولائی", "اگست", "ستمبر", "اکتوبر", "نومبر", "دسمبر",
)


def _flt(name: str, en: str, ur: str, kind: str, required: bool = False) -> ReportFilter:
    """Shorthand for a report filter declaration."""
    return ReportFilter(name=name, label_en=en, label_ur=ur, kind=kind, required=required)


def _col(key: str, en: str, ur: str, kind: str = "text") -> ReportColumn:
    """Shorthand for a report column; numeric kinds align right."""
    align = "right" if kind in ("money", "quantity", "price", "number") else "left"
    return ReportColumn(key=key, label_en=en, label_ur=ur, align=align, kind=kind)


#: The report catalog: id, EN/UR titles, declared filters. The builder
#: method is looked up as ``_build_<report_id>``.
_CATALOG: tuple[tuple[str, str, str, tuple[ReportFilter, ...]], ...] = (
    (
        "daily_register", "Daily Register", "روزانہ رجسٹر",
        (_flt("business_date", "Business date", "کاروباری تاریخ", "date"),),
    ),
    (
        "nozzle_readings", "Nozzle Readings", "نوزل ریڈنگ",
        (
            _flt("business_date", "Business date", "کاروباری تاریخ", "date"),
            _flt("date_from", "From date", "آغاز تاریخ", "date"),
            _flt("date_to", "To date", "اختتام تاریخ", "date"),
            _flt("fuel_product_id", "Fuel product", "ایندھن", "fuel_product"),
        ),
    ),
    (
        "fuel_sales", "Fuel Sales", "ایندھن کی فروخت",
        (
            _flt("business_date", "Business date", "کاروباری تاریخ", "date"),
            _flt("date_from", "From date", "آغاز تاریخ", "date"),
            _flt("date_to", "To date", "اختتام تاریخ", "date"),
            _flt("fuel_product_id", "Fuel product", "ایندھن", "fuel_product"),
        ),
    ),
    (
        "cashbook", "Cash Book", "کیش بک",
        (
            _flt("business_date", "Business date", "کاروباری تاریخ", "date"),
            _flt("date_from", "From date", "آغاز تاریخ", "date"),
            _flt("date_to", "To date", "اختتام تاریخ", "date"),
            _flt("category", "Cash category", "نقد کی قسم", "category"),
        ),
    ),
    (
        "sales_reconciliation", "Sales Reconciliation", "فروخت کی تصدیق",
        (_flt("business_date", "Business date", "کاروباری تاریخ", "date"),),
    ),
    (
        "customer_credit_register", "Customer Credit Register", "ادھار کھاتہ رجسٹر",
        (
            _flt("date_from", "From date", "آغاز تاریخ", "date"),
            _flt("date_to", "To date", "اختتام تاریخ", "date"),
            _flt("customer_id", "Customer", "گاہک", "customer"),
        ),
    ),
    (
        "customer_statement", "Customer Statement", "گاہک کا کھاتہ",
        (
            _flt("customer_id", "Customer", "گاہک", "customer", required=True),
            _flt("date_from", "From date", "آغاز تاریخ", "date"),
            _flt("date_to", "To date", "اختتام تاریخ", "date"),
        ),
    ),
    (
        "payment_receipt", "Payment Receipt", "ادائیگی کی رسید",
        (
            _flt(
                "reference",
                "Payment reference (uuid or receipt no.)",
                "ادائیگی کا حوالہ (یو یو آئی ڈی یا رسید نمبر)",
                "text",
                required=True,
            ),
        ),
    ),
    (
        "outstanding_balances", "Outstanding Balances", "بقایا جات",
        (_flt("date_to", "As of date", "بمطابق تاریخ", "date"),),
    ),
    (
        "fuel_stock", "Fuel Stock", "ایندھن کا اسٹاک",
        (_flt("business_date", "Business date", "کاروباری تاریخ", "date"),),
    ),
    (
        "tank_reconciliation", "Tank Reconciliation", "ٹینک کی تصدیق",
        (_flt("business_date", "Business date", "کاروباری تاریخ", "date"),),
    ),
    (
        "deliveries", "Fuel Deliveries", "ایندھن کی ترسیل",
        (
            _flt("date_from", "From date", "آغاز تاریخ", "date"),
            _flt("date_to", "To date", "اختتام تاریخ", "date"),
            _flt("fuel_product_id", "Fuel product", "ایندھن", "fuel_product"),
        ),
    ),
    (
        "supplier_balances", "Supplier Balances", "سپلائر بقایا جات",
        (),
    ),
    (
        "other_materials", "Other Materials", "دیگر اشیاء",
        (
            _flt("date_from", "From date", "آغاز تاریخ", "date"),
            _flt("date_to", "To date", "اختتام تاریخ", "date"),
            _flt("category", "Product", "شے", "category"),
        ),
    ),
    (
        "expenses", "Expenses", "اخراجات",
        (
            _flt("date_from", "From date", "آغاز تاریخ", "date"),
            _flt("date_to", "To date", "اختتام تاریخ", "date"),
            _flt("category", "Expense category", "اخراجات کی قسم", "category"),
        ),
    ),
    (
        "pnl_daily", "Daily Profit & Loss", "روزانہ نفع و نقصان",
        (_flt("business_date", "Business date", "کاروباری تاریخ", "date"),),
    ),
    (
        "pnl_monthly", "Monthly Profit & Loss", "ماہانہ نفع و نقصان",
        (
            _flt(
                "business_date", "Any day in the month",
                "مہینے کا کوئی دن", "date",
            ),
        ),
    ),
    (
        "pnl_annual", "Annual Profit & Loss", "سالانہ نفع و نقصان",
        (
            _flt(
                "business_date", "Any day in the year",
                "سال کا کوئی دن", "date",
            ),
        ),
    ),
    (
        "cash_difference", "Cash Difference", "نقد کا فرق",
        (
            _flt("business_date", "Business date", "کاروباری تاریخ", "date"),
            _flt("date_from", "From date", "آغاز تاریخ", "date"),
            _flt("date_to", "To date", "اختتام تاریخ", "date"),
        ),
    ),
    (
        "audit_history", "Audit History", "آڈٹ ریکارڈ",
        (
            _flt("date_from", "From date", "آغاز تاریخ", "date"),
            _flt("date_to", "To date", "اختتام تاریخ", "date"),
        ),
    ),
    (
        "backup_history", "Backup History", "بیک اپ ریکارڈ",
        (
            _flt("date_from", "From date", "آغاز تاریخ", "date"),
            _flt("date_to", "To date", "اختتام تاریخ", "date"),
        ),
    ),
)

assert {entry[0] for entry in _CATALOG} == set(REPORT_IDS), (
    "report catalog does not match REPORT_IDS"
)


class ReportingService(ServiceBase):
    """Builds the 21 read-only reports from committed records.

    Reuses the Phase 2–5 services and database views; it performs no
    writes and opens no write transactions. Every row value is a raw
    integer (paisa / milliliters / scaled price), a date string, or text —
    never float.
    """

    def __init__(self, db_manager, actor: str | None = None) -> None:
        super().__init__(db_manager, actor)
        self._accounting = AccountingService(db_manager, actor)
        self._cash = CashbookService(db_manager, actor)
        self._credit = CustomerCreditService(db_manager, actor)
        self._deliveries = DeliveryService(db_manager, actor)
        self._expenses = ExpenseService(db_manager, actor)
        self._inventory = InventoryService(db_manager, actor)
        self._nozzles = NozzleService(db_manager, actor)
        self._materials = OtherMaterialsService(db_manager, actor)
        self._payments = PaymentService(db_manager, actor)
        self._prices = PriceService(db_manager, actor)
        self._sales = SalesService(db_manager, actor)
        self._suppliers = SupplierService(db_manager, actor)

    # ------------------------------------------------------------------
    # catalog & entry point
    # ------------------------------------------------------------------
    def list_reports(self) -> list[ReportSpec]:
        """The catalog of the 21 reports with EN/UR titles and filters."""
        return [
            ReportSpec(
                report_id=report_id,
                title_en=title_en,
                title_ur=title_ur,
                filters=filters,
            )
            for report_id, title_en, title_ur, filters in _CATALOG
        ]

    def generate_report(
        self,
        report_id: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        business_date: str | None = None,
        fuel_product_id=None,
        customer_id=None,
        category=None,
        reference: str | None = None,
    ) -> ReportResult:
        """Build one report. Raises ``ValidationError``/``NotFoundError``.

        * unknown ``report_id`` → ``ValidationError``;
        * malformed dates or ``date_from > date_to`` → ``ValidationError``;
        * unknown ``fuel_product_id`` / ``customer_id`` / ``category``
          → ``ValidationError``; a missing required filter →
          ``ValidationError``;
        * an unknown ``reference`` (payment_receipt) → ``NotFoundError``;
        * a valid filter combination with no data → a valid *empty* report,
          never an error.
        """
        builder_name = self._builder_name(report_id)  # validates report_id
        with self.read_connection() as conn:
            filters = self._normalize_filters(
                conn,
                date_from=date_from,
                date_to=date_to,
                business_date=business_date,
                fuel_product_id=fuel_product_id,
                customer_id=customer_id,
                category=category,
                reference=reference,
            )
            result = getattr(self, builder_name)(conn, filters)
        result.generated_at_utc = timecalc.utc_now_iso()
        return result

    @staticmethod
    def _builder_name(report_id: str) -> str:
        if report_id not in REPORT_IDS:
            known = ", ".join(REPORT_IDS)
            raise errors.ValidationError(
                f"Unknown report: {report_id!r}. Known reports: {known}.",
                key="err.report_unknown",
                params={"report_id": report_id, "known": known},
            )
        return f"_build_{report_id}"

    # ------------------------------------------------------------------
    # filter validation & normalization
    # ------------------------------------------------------------------
    def _normalize_filters(
        self,
        conn: sqlite3.Connection,
        *,
        date_from: str | None,
        date_to: str | None,
        business_date: str | None,
        fuel_product_id,
        customer_id,
        category,
        reference: str | None,
    ) -> SimpleNamespace:
        for label, value in (
            ("date_from", date_from),
            ("date_to", date_to),
            ("business_date", business_date),
        ):
            if value is not None and not timecalc.is_valid_business_date(value):
                raise errors.ValidationError(
                    f"Invalid {label}: {value!r}. Expected 'YYYY-MM-DD'.",
                    key="err.report_invalid_date",
                    params={"label": label, "value": value},
                )
        if date_from is not None and date_to is not None and date_from > date_to:
            raise errors.ValidationError(
                f"date_from {date_from} is after date_to {date_to}.",
                key="err.report_date_from_after_to",
                params={"date_from": date_from, "date_to": date_to},
            )
        return SimpleNamespace(
            date_from=date_from,
            date_to=date_to,
            business_date=business_date,
            fuel_product=self._resolve_fuel_product(conn, fuel_product_id),
            customer=self._resolve_customer(conn, customer_id),
            category=category,
            reference=reference,
        )

    @staticmethod
    def _resolve_fuel_product(
        conn: sqlite3.Connection, value
    ) -> dict | None:
        """``{'id', 'code', 'name_en'}`` for an id or code; None when unset."""
        if value is None:
            return None
        if isinstance(value, bool):
            raise errors.ValidationError(
                f"Invalid fuel product: {value!r}.",
                key="err.report_invalid_product",
                params={"value": value},
            )
        if isinstance(value, int):
            row = conn.execute(
                "SELECT id, code, name_en FROM fuel_products WHERE id = ?",
                (value,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, code, name_en FROM fuel_products"
                " WHERE code = ? OR name_en = ?",
                (str(value), str(value)),
            ).fetchone()
        if row is None:
            raise errors.ValidationError(
                f"Unknown fuel product: {value!r}.",
                key="err.report_unknown_product",
                params={"value": value},
            )
        return {"id": int(row["id"]), "code": row["code"], "name_en": row["name_en"]}

    @staticmethod
    def _resolve_customer(conn: sqlite3.Connection, value) -> dict | None:
        """``{'id', 'code', 'name'}`` for an id or customer code; None when unset."""
        if value is None:
            return None
        if isinstance(value, bool):
            raise errors.ValidationError(
                f"Invalid customer: {value!r}.",
                key="err.report_invalid_customer",
                params={"value": value},
            )
        if isinstance(value, int):
            row = conn.execute(
                "SELECT id, customer_code, name FROM customers WHERE id = ?",
                (value,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, customer_code, name FROM customers"
                " WHERE customer_code = ?",
                (str(value),),
            ).fetchone()
        if row is None:
            raise errors.ValidationError(
                f"Unknown customer: {value!r}.",
                key="err.report_unknown_customer",
                params={"value": value},
            )
        return {
            "id": int(row["id"]),
            "code": row["customer_code"],
            "name": row["name"],
        }

    def _resolve_expense_category(
        self, conn: sqlite3.Connection, value
    ) -> dict | None:
        """``{'id', 'name_en'}`` for an expense-category id or name."""
        if value is None:
            return None
        if isinstance(value, bool):
            raise errors.ValidationError(
                f"Invalid category: {value!r}.",
                key="err.report_invalid_category",
                params={"value": value},
            )
        if isinstance(value, int):
            row = conn.execute(
                "SELECT id, name_en FROM expense_categories WHERE id = ?",
                (value,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, name_en FROM expense_categories WHERE name_en = ?",
                (str(value),),
            ).fetchone()
        if row is None:
            raise errors.ValidationError(
                f"Unknown expense category: {value!r}.",
                key="err.report_unknown_expense_category",
                params={"value": value},
            )
        return {"id": int(row["id"]), "name_en": row["name_en"]}

    def _resolve_other_product(
        self, conn: sqlite3.Connection, value
    ) -> dict | None:
        """``{'id', 'name'}`` for an other-product id or name."""
        if value is None:
            return None
        if isinstance(value, bool):
            raise errors.ValidationError(
                f"Invalid category: {value!r}.",
                key="err.report_invalid_category",
                params={"value": value},
            )
        if isinstance(value, int):
            row = conn.execute(
                "SELECT id, name FROM other_products WHERE id = ?", (value,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, name FROM other_products WHERE name = ?",
                (str(value),),
            ).fetchone()
        if row is None:
            raise errors.ValidationError(
                f"Unknown product: {value!r}.",
                key="err.report_unknown_product2",
                params={"value": value},
            )
        return {"id": int(row["id"]), "name": row["name"]}

    def _resolve_cash_category(
        self, conn: sqlite3.Connection, value
    ) -> dict | None:
        """``{'id', 'name', 'name_en'}`` for a cash-book category key or name."""
        if value is None:
            return None
        if isinstance(value, bool):
            raise errors.ValidationError(
                f"Invalid category: {value!r}.",
                key="err.report_invalid_category",
                params={"value": value},
            )
        if isinstance(value, int):
            row = conn.execute(
                "SELECT id, name, name_en FROM cash_categories WHERE id = ?",
                (value,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, name, name_en FROM cash_categories"
                " WHERE name = ? OR name_en = ?",
                (str(value), str(value)),
            ).fetchone()
        if row is None:
            raise errors.ValidationError(
                f"Unknown cash category: {value!r}.",
                key="err.report_unknown_cash_category",
                params={"value": value},
            )
        return {
            "id": int(row["id"]),
            "name": row["name"],
            "name_en": row["name_en"],
        }

    # ------------------------------------------------------------------
    # day helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _open_day(conn: sqlite3.Connection) -> str | None:
        row = conn.execute(
            "SELECT business_date FROM business_days WHERE status = 'open'"
            " ORDER BY business_date LIMIT 1"
        ).fetchone()
        return row["business_date"] if row is not None else None

    def _single_day(
        self, conn: sqlite3.Connection, f: SimpleNamespace, result: ReportResult
    ) -> str:
        """The one business date for single-day reports.

        ``business_date`` wins; a degenerate ``date_from == date_to`` range
        is honored; otherwise the open day is used (noted). Raises
        ``ValidationError`` when no date can be determined.
        """
        if f.business_date is not None:
            return f.business_date
        if f.date_from is not None and f.date_from == f.date_to:
            return f.date_from
        open_day = self._open_day(conn)
        if open_day is None:
            raise errors.ValidationError(
                "No business date given and no business day is open;"
                " pass business_date.",
                key="err.report_no_day_for_date",
            )
        result.notes_en.append(
            f"No business date given; using the open day {open_day}."
        )
        result.notes_ur.append(
            f"تاریخ نہیں دی گئی؛ کھلا دن {open_day} استعمال کیا گیا۔"
        )
        return open_day

    def _range_days(
        self, conn: sqlite3.Connection, f: SimpleNamespace, result: ReportResult
    ) -> list[str]:
        """Inclusive business-date list for range reports.

        ``date_from``/``date_to`` win; a lone bound or ``business_date``
        collapses to one day; otherwise the open day is used (noted).
        """
        if f.date_from is not None or f.date_to is not None:
            start = f.date_from or f.date_to
            end = f.date_to or f.date_from
            return self._date_range(start, end)
        if f.business_date is not None:
            return [f.business_date]
        open_day = self._open_day(conn)
        if open_day is None:
            raise errors.ValidationError(
                "No date range given and no business day is open;"
                " pass date_from/date_to.",
                key="err.report_no_day_for_range",
            )
        result.notes_en.append(
            f"No date range given; using the open day {open_day}."
        )
        result.notes_ur.append(
            f"مدت نہیں دی گئی؛ کھلا دن {open_day} استعمال کیا گیا۔"
        )
        return [open_day]

    @staticmethod
    def _date_range(start_date: str, end_date: str) -> list[str]:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        days: list[str] = []
        current = start
        while current <= end:
            days.append(current.isoformat())
            current += timedelta(days=1)
        return days

    # ------------------------------------------------------------------
    # closing-revision labeling & period labels
    # ------------------------------------------------------------------
    def _attach_closing(
        self,
        conn: sqlite3.Connection,
        result: ReportResult,
        business_date: str,
    ) -> None:
        """Set ``closing_revision``; note reopened days so corrected vs
        historical reports stay distinguishable."""
        row = conn.execute(
            "SELECT COUNT(*), MAX(closing_revision)"
            " FROM business_day_closings WHERE business_date = ?",
            (business_date,),
        ).fetchone()
        count = int(row[0])
        revision = int(row[1]) if row[1] is not None else None
        result.business_date = business_date
        result.closing_revision = revision
        if count == 0:
            return
        if count > 1 or (revision is not None and revision > 1):
            result.notes_en.append(
                f"Business day {business_date} was closed {count} time(s);"
                f" this report reflects the latest closing revision"
                f" {revision}. Figures from earlier revisions are superseded."
            )
            result.notes_ur.append(
                f"کاروباری دن {business_date} {count} بار بند ہوا؛ یہ رپورٹ"
                f" تازہ ترین بندش (نظرثانی {revision}) کے مطابق ہے۔ پرانی"
                " نظرثانیوں کے اعداد کالعدم ہیں۔"
            )

    @staticmethod
    def _period_single(day: str) -> tuple[str, str]:
        return f"Business day {day}", f"کاروباری دن {day}"

    @staticmethod
    def _period_range(start: str, end: str) -> tuple[str, str]:
        if start == end:
            return f"Business day {start}", f"کاروباری دن {start}"
        return f"{start} to {end}", f"{start} تا {end}"

    @staticmethod
    def _period_month(year: int, month: int) -> tuple[str, str]:
        return (
            f"{_EN_MONTHS[month - 1]} {year}",
            f"{_UR_MONTHS[month - 1]} {year}",
        )

    @staticmethod
    def _period_year(year: int) -> tuple[str, str]:
        return f"Year {year}", f"سال {year}"

    # ------------------------------------------------------------------
    # small utilities
    # ------------------------------------------------------------------
    @staticmethod
    def _new_result(
        report_id: str,
        period_en: str,
        period_ur: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> ReportResult:
        for rid, title_en, title_ur, _filters in _CATALOG:
            if rid == report_id:
                break
        return ReportResult(
            report_id=report_id,
            title_en=title_en,
            title_ur=title_ur,
            generated_at_utc="",
            date_from=date_from,
            date_to=date_to,
            period_label_en=period_en,
            period_label_ur=period_ur,
        )

    @staticmethod
    def _totals(rows: list[dict], keys: list[str]) -> dict:
        """Sum raw integer columns, skipping None (money/quantity/number)."""
        out: dict = {}
        for key in keys:
            values = [
                row[key]
                for row in rows
                if isinstance(row.get(key), int) and not isinstance(row.get(key), bool)
            ]
            out[key] = sum(values)
        return out

    # ------------------------------------------------------------------
    # 1. daily_register — register-style daily summary
    # ------------------------------------------------------------------
    def _build_daily_register(
        self, conn: sqlite3.Connection, f: SimpleNamespace
    ) -> ReportResult:
        period_en, period_ur = self._period_single(
            f.business_date or f.date_from or "?"
        )
        result = self._new_result("daily_register", period_en, period_ur)
        day = self._single_day(conn, f, result)
        result.period_label_en, result.period_label_ur = self._period_single(day)
        result.date_from = result.date_to = day
        self._attach_closing(conn, result, day)

        result.columns = [
            _col("section", "Section", "حصہ"),
            _col("description", "Description", "تفصیل"),
            _col("quantity_ml", "Quantity (ml)", "مقدار (ملی لیٹر)", "quantity"),
            _col("amount_paisa", "Amount (PKR)", "رقم (روپے)", "money"),
            _col("price_10k", "Price (PKR/L)", "فی لیٹر قیمت (روپے)", "price"),
            _col("note", "Note", "نوٹ"),
        ]
        rows: list[dict] = []

        # --- nozzle readings ------------------------------------------------
        readings = {r.nozzle_code: r for r in self._nozzles.day_readings(day)}
        sales = self._sales.day_sales(day)
        per_nozzle: dict[str, dict] = {}
        for interval in sales.intervals:
            bucket = per_nozzle.setdefault(
                interval.nozzle_code,
                {"product": interval.fuel_product_code, "qty": 0, "amount": 0,
                 "prices": set()},
            )
            if interval.quantity_ml is not None:
                bucket["qty"] += int(interval.quantity_ml)
            if interval.amount_paisa is not None:
                bucket["amount"] += int(interval.amount_paisa)
            if interval.unit_price_10k is not None:
                bucket["prices"].add(int(interval.unit_price_10k))
        day_end_price: dict[str, int | None] = {}
        for code in sorted({r.fuel_product_code for r in readings.values()}):
            info = self._prices.price_effective_at_day_end(code, day)
            day_end_price[code] = info.unit_price_10k if info else None
        for code in sorted(per_nozzle):
            bucket = per_nozzle[code]
            reading = readings.get(code)
            prev = reading.previous_ml if reading else None
            curr = reading.current_ml if reading else None
            price = day_end_price.get(bucket["product"])
            note = ""
            if prev is not None and curr is not None:
                note = f"{prev:,} ml → {curr:,} ml"
            if len(bucket["prices"]) > 1:
                note = (note + "; " if note else "") + "mid-day price change"
            rows.append(
                {
                    "section": "Nozzle readings",
                    "description": f"Nozzle {code} ({bucket['product']})",
                    "quantity_ml": bucket["qty"] or None,
                    "amount_paisa": bucket["amount"] or None,
                    "price_10k": price,
                    "note": note or None,
                }
            )

        # --- fuel sales ------------------------------------------------------
        total_liters = 0
        total_revenue = 0
        for product in self._prices.list_fuel_products():
            code = product["code"]
            liters = int(sales.liters_by_product_ml.get(code, 0))
            revenue = int(sales.revenue_by_product_paisa.get(code, 0))
            if liters == 0 and revenue == 0:
                continue
            total_liters += liters
            total_revenue += revenue
            rows.append(
                {
                    "section": "Fuel sales",
                    "description": f"{code} ({product['name_en']})",
                    "quantity_ml": liters,
                    "amount_paisa": revenue,
                    "price_10k": day_end_price.get(code),
                    "note": None,
                }
            )
        rows.append(
            {
                "section": "Fuel sales",
                "description": "Total fuel sales",
                "quantity_ml": total_liters,
                "amount_paisa": total_revenue,
                "price_10k": None,
                "note": None,
            }
        )

        # --- stock -----------------------------------------------------------
        for position in self._inventory.stock_positions(day):
            if position.physical_ml is None:
                note = "No dip reading recorded"
            else:
                variance = position.variance_ml or 0
                note = (
                    f"Physical {position.physical_ml:,} ml,"
                    f" variance {variance:+,} ml"
                )
            rows.append(
                {
                    "section": "Stock",
                    "description": f"Tank {position.tank_code}"
                    f" ({position.fuel_product_code})",
                    "quantity_ml": position.theoretical_ml,
                    "amount_paisa": None,
                    "price_10k": None,
                    "note": note,
                }
            )

        # --- receipts & payments (v_cashbook: net of reversals) --------------
        cash_rows = conn.execute(
            "SELECT kind, category_en, SUM(signed_paisa) AS total"
            " FROM v_cashbook WHERE business_date = ?"
            " GROUP BY kind, category_en ORDER BY kind, category_en",
            (day,),
        ).fetchall()
        receipt_total = 0
        payment_total = 0
        receipt_rows: list[dict] = []
        payment_rows: list[dict] = []
        for row in cash_rows:
            total = int(row["total"] or 0)
            entry = {
                "description": row["category_en"],
                "quantity_ml": None,
                "amount_paisa": abs(total),
                "price_10k": None,
                "note": None,
            }
            if row["kind"] == "receipt":
                # Receipts precede payments (paper-register order): keep the
                # query's kind ordering but regroup into section order, each
                # section closed by its total row.
                receipt_total += total
                entry["section"] = "Receipts"
                receipt_rows.append(entry)
            else:
                payment_total += -total
                entry["section"] = "Payments"
                payment_rows.append(entry)
        rows.extend(receipt_rows)
        rows.append(
            {
                "section": "Receipts",
                "description": "Total receipts",
                "quantity_ml": None,
                "amount_paisa": receipt_total,
                "price_10k": None,
                "note": None,
            }
        )
        rows.extend(payment_rows)
        rows.append(
            {
                "section": "Payments",
                "description": "Total payments",
                "quantity_ml": None,
                "amount_paisa": payment_total,
                "price_10k": None,
                "note": None,
            }
        )

        # --- customer credit -------------------------------------------------
        new_receivables = 0
        paid_now_total = 0
        ticket_count = 0
        for ticket in self._credit.day_tickets(day):
            net = ticket.gross_amount_paisa - ticket.paid_now_paisa
            if ticket.reverses_uuid is not None:
                new_receivables -= net
            else:
                new_receivables += net
                paid_now_total += ticket.paid_now_paisa
                ticket_count += 1
        recovery = conn.execute(
            "SELECT COALESCE(SUM(signed_paisa), 0) FROM v_cashbook"
            " WHERE business_date = ? AND kind = 'receipt'"
            " AND category = 'credit_recovery'",
            (day,),
        ).fetchone()[0]
        rows.append(
            {
                "section": "Customer credit",
                "description": f"Credit tickets issued ({ticket_count})",
                "quantity_ml": None,
                "amount_paisa": new_receivables,
                "price_10k": None,
                "note": "New receivables (gross − paid now)",
            }
        )
        rows.append(
            {
                "section": "Customer credit",
                "description": "Paid now at sale",
                "quantity_ml": None,
                "amount_paisa": paid_now_total,
                "price_10k": None,
                "note": None,
            }
        )
        rows.append(
            {
                "section": "Customer credit",
                "description": "Old-debt recovery (cash)",
                "quantity_ml": None,
                "amount_paisa": int(recovery or 0),
                "price_10k": None,
                "note": "Not revenue — reduces receivables",
            }
        )

        # --- daily balances --------------------------------------------------
        position_row = conn.execute(
            "SELECT * FROM v_daily_cash_position WHERE business_date = ?",
            (day,),
        ).fetchone()
        if position_row is not None:
            for description, value in (
                ("Opening cash", position_row["opening_cash_paisa"]),
                ("Receipts", position_row["receipts_paisa"]),
                ("Payments", position_row["payments_paisa"]),
                ("Expected cash", position_row["expected_paisa"]),
                ("Physical cash counted", position_row["physical_cash_paisa"]),
                ("Difference (physical − expected)",
                 position_row["difference_paisa"]),
            ):
                rows.append(
                    {
                        "section": "Daily balances",
                        "description": description,
                        "quantity_ml": None,
                        "amount_paisa": (
                            int(value) if value is not None else None
                        ),
                        "price_10k": None,
                        "note": (
                            None
                            if position_row["physical_cash_paisa"] is not None
                            or description != "Physical cash counted"
                            else "Not counted yet"
                        ),
                    }
                )
        result.rows = rows
        # Totals cover the fuel-sales section only (register convention:
        # receipts/payments are cash movements, not sales).
        result.totals = {
            "quantity_ml": total_liters,
            "amount_paisa": total_revenue,
        }
        result.notes_en.append(
            "Register-style summary mirroring the paper register: nozzle"
            " readings, fuel sales, stock, receipts, payments, customer"
            " credit and daily balances. Totals cover fuel sales only."
        )
        result.notes_ur.append(
            "کاغذی رجسٹر کے طرز پر روزانہ خلاصہ: نوزل ریڈنگ، ایندھن کی"
            " فروخت، اسٹاک، وصولیاں، ادائیگیاں، ادھار کھاتہ اور روزانہ"
            " بیلنس۔ مجموعے صرف ایندھن کی فروخت کے ہیں۔"
        )
        return result

    # ------------------------------------------------------------------
    # 2. nozzle_readings
    # ------------------------------------------------------------------
    def _build_nozzle_readings(
        self, conn: sqlite3.Connection, f: SimpleNamespace
    ) -> ReportResult:
        result = self._new_result("nozzle_readings", "", "")
        days = self._range_days(conn, f, result)
        result.period_label_en, result.period_label_ur = self._period_range(
            days[0], days[-1]
        )
        result.date_from, result.date_to = days[0], days[-1]
        if len(days) == 1:
            self._attach_closing(conn, result, days[0])
        result.columns = [
            _col("business_date", "Date", "تاریخ", "date"),
            _col("nozzle", "Nozzle", "نوزل"),
            _col("product", "Product", "ایندھن"),
            _col("previous_ml", "Previous (ml)", "پچھلی (ملی لیٹر)", "quantity"),
            _col("current_ml", "Current (ml)", "موجودہ (ملی لیٹر)", "quantity"),
            _col("liters_sold_ml", "Sold (ml)", "فروخت (ملی لیٹر)", "quantity"),
            _col("note", "Note", "نوٹ"),
        ]
        product = f.fuel_product["code"] if f.fuel_product else None
        rows: list[dict] = []
        for day in days:
            for reading in self._nozzles.day_readings(day):
                if product and reading.fuel_product_code != product:
                    continue
                note = None
                if not reading.has_reading:
                    note = "No reading recorded"
                elif reading.liters_sold_ml is None:
                    note = (
                        "Quantity not computable from readings"
                        " (meter event / first reading)"
                    )
                rows.append(
                    {
                        "business_date": day,
                        "nozzle": reading.nozzle_code,
                        "product": reading.fuel_product_code,
                        "previous_ml": reading.previous_ml,
                        "current_ml": reading.current_ml,
                        "liters_sold_ml": reading.liters_sold_ml,
                        "note": note,
                    }
                )
        result.rows = rows
        result.totals = self._totals(rows, ["liters_sold_ml"])
        return result

    # ------------------------------------------------------------------
    # 3. fuel_sales — petrol/diesel split per day
    # ------------------------------------------------------------------
    def _build_fuel_sales(
        self, conn: sqlite3.Connection, f: SimpleNamespace
    ) -> ReportResult:
        result = self._new_result("fuel_sales", "", "")
        days = self._range_days(conn, f, result)
        result.period_label_en, result.period_label_ur = self._period_range(
            days[0], days[-1]
        )
        result.date_from, result.date_to = days[0], days[-1]
        if len(days) == 1:
            self._attach_closing(conn, result, days[0])
        result.columns = [
            _col("business_date", "Date", "تاریخ", "date"),
            _col("product", "Product", "ایندھن"),
            _col("liters_ml", "Liters (ml)", "لیٹر (ملی لیٹر)", "quantity"),
            _col("price_10k", "Price (PKR/L)", "فی لیٹر قیمت (روپے)", "price"),
            _col("revenue_paisa", "Revenue (PKR)", "آمدن (روپے)", "money"),
        ]
        wanted = f.fuel_product["code"] if f.fuel_product else None
        rows: list[dict] = []
        for day in days:
            sales = self._sales.day_sales(day)
            for product in self._prices.list_fuel_products():
                code = product["code"]
                if wanted and code != wanted:
                    continue
                liters = int(sales.liters_by_product_ml.get(code, 0))
                revenue = int(sales.revenue_by_product_paisa.get(code, 0))
                if liters == 0 and revenue == 0:
                    continue
                price = self._prices.price_effective_at_day_end(code, day)
                rows.append(
                    {
                        "business_date": day,
                        "product": code,
                        "liters_ml": liters,
                        "price_10k": price.unit_price_10k if price else None,
                        "revenue_paisa": revenue,
                    }
                )
        result.rows = rows
        result.totals = self._totals(rows, ["liters_ml", "revenue_paisa"])
        result.notes_en.append(
            "Meter-derived sales: each reading interval is priced by the"
            " price effective at the interval's timestamp."
        )
        result.notes_ur.append(
            "میٹر سے حاصل شدہ فروخت: ہر ریڈنگ وقفہ اس وقت کی نافذ قیمت پر"
            " لگایا گیا ہے۔"
        )
        return result

    # ------------------------------------------------------------------
    # 4. cashbook — chronological cash book with running balance
    # ------------------------------------------------------------------
    def _build_cashbook(
        self, conn: sqlite3.Connection, f: SimpleNamespace
    ) -> ReportResult:
        result = self._new_result("cashbook", "", "")
        days = self._range_days(conn, f, result)
        result.period_label_en, result.period_label_ur = self._period_range(
            days[0], days[-1]
        )
        result.date_from, result.date_to = days[0], days[-1]
        if len(days) == 1:
            self._attach_closing(conn, result, days[0])
        category = self._resolve_cash_category(conn, f.category)
        result.columns = [
            _col("business_date", "Date", "تاریخ", "date"),
            _col("occurred_at_utc", "Time (UTC)", "وقت"),
            _col("kind", "Kind", "قسم"),
            _col("category", "Category", "قسم"),
            _col("remarks", "Remarks", "تبصرے"),
            _col("receipt_paisa", "Receipt (PKR)", "وصولی (روپے)", "money"),
            _col("payment_paisa", "Payment (PKR)", "ادائیگی (روپے)", "money"),
            _col("balance_paisa", "Balance (PKR)", "بیلنس (روپے)", "money"),
        ]
        where = "business_date BETWEEN ? AND ?"
        params: list = [days[0], days[-1]]
        if category is not None:
            where += " AND category = ?"
            params.append(category["name"])
        entries = conn.execute(
            "SELECT business_date, occurred_at_utc, kind, category_en,"
            " remarks, signed_paisa, uuid, status FROM v_cashbook"
            f" WHERE {where} ORDER BY occurred_at_utc, uuid",
            params,
        ).fetchall()
        first_day = conn.execute(
            "SELECT opening_cash_paisa FROM business_days WHERE business_date = ?",
            (days[0],),
        ).fetchone()
        opening = int(first_day["opening_cash_paisa"]) if first_day else 0
        balance = opening
        rows: list[dict] = []
        for entry in entries:
            signed = int(entry["signed_paisa"] or 0)
            balance += signed
            rows.append(
                {
                    "business_date": entry["business_date"],
                    "occurred_at_utc": entry["occurred_at_utc"],
                    "kind": "Receipt" if entry["kind"] == "receipt" else "Payment",
                    "category": entry["category_en"],
                    "remarks": entry["remarks"],
                    "receipt_paisa": signed if signed > 0 else None,
                    "payment_paisa": -signed if signed < 0 else None,
                    "balance_paisa": balance,
                    "_status": entry["status"],
                }
            )
        # Strip the private helper key before returning.
        for row in rows:
            del row["_status"]
        result.rows = rows
        result.totals = self._totals(rows, ["receipt_paisa", "payment_paisa"])
        result.notes_en.append(
            f"Running balance starts from the opening cash of {days[0]}"
            f" ({opening:,} paisa). Cash book counts physical cash only;"
            " bank/other activity never touches it (design §11.14)."
        )
        result.notes_ur.append(
            f"جاری بیلنس {days[0]} کی ابتدائی نقد سے شروع ہوتا ہے۔ کیش بک"
            " میں صرف جسمانی نقد درج ہوتا ہے؛ بینک/دیگر سرگرمی اس میں شامل"
            " نہیں۔"
        )
        return result

    # ------------------------------------------------------------------
    # 5. sales_reconciliation — the §6 identity
    # ------------------------------------------------------------------
    def _build_sales_reconciliation(
        self, conn: sqlite3.Connection, f: SimpleNamespace
    ) -> ReportResult:
        result = self._new_result("sales_reconciliation", "", "")
        day = self._single_day(conn, f, result)
        result.period_label_en, result.period_label_ur = self._period_single(day)
        result.date_from = result.date_to = day
        self._attach_closing(conn, result, day)
        result.columns = [
            _col("product", "Product", "ایندھن"),
            _col("meter_revenue_paisa", "Meter revenue (PKR)", "میٹر آمدن (روپے)", "money"),
            _col("cash_paisa", "Cash (PKR)", "نقد (روپے)", "money"),
            _col("bank_paisa", "Bank (PKR)", "بینک (روپے)", "money"),
            _col("paid_now_on_ticket_paisa", "Paid now (PKR)", "ابھی ادا (روپے)", "money"),
            _col("other_paisa", "Other (PKR)", "دیگر (روپے)", "money"),
            _col("settled_paisa", "Settled (PKR)", "تصفیہ شدہ (روپے)", "money"),
            _col("new_receivables_paisa", "New receivables (PKR)", "نئے قابل وصول (روپے)", "money"),
            _col("unresolved_paisa", "Unresolved (PKR)", "غیر حل شدہ (روپے)", "money"),
        ]
        recon = self._sales.reconcile_day(day)
        by_product = {
            row["fuel_product_code"]: row
            for row in recon["settlements_by_product"]
        }
        rows: list[dict] = []
        meter_total = 0
        settled_total = 0
        for code in sorted(recon["meter_revenue_by_product_paisa"]):
            meter = int(recon["meter_revenue_by_product_paisa"][code])
            legs = by_product.get(code, {})
            cash = int(legs.get("cash_paisa", 0))
            bank = int(legs.get("bank_paisa", 0))
            paid_now = int(legs.get("paid_now_on_ticket_paisa", 0))
            other = int(legs.get("other_paisa", 0))
            settled = cash + bank + paid_now + other
            meter_total += meter
            settled_total += settled
            rows.append(
                {
                    "product": code,
                    "meter_revenue_paisa": meter,
                    "cash_paisa": cash,
                    "bank_paisa": bank,
                    "paid_now_on_ticket_paisa": paid_now,
                    "other_paisa": other,
                    "settled_paisa": settled,
                    "new_receivables_paisa": None,
                    "unresolved_paisa": None,
                }
            )
        new_receivables = int(recon["new_receivables_paisa"])
        unresolved = meter_total - settled_total - new_receivables
        rows.append(
            {
                "product": "Total",
                "meter_revenue_paisa": meter_total,
                "cash_paisa": sum(r["cash_paisa"] for r in rows),
                "bank_paisa": sum(r["bank_paisa"] for r in rows),
                "paid_now_on_ticket_paisa": sum(
                    r["paid_now_on_ticket_paisa"] for r in rows
                ),
                "other_paisa": sum(r["other_paisa"] for r in rows),
                "settled_paisa": settled_total,
                "new_receivables_paisa": new_receivables,
                "unresolved_paisa": unresolved,
            }
        )
        result.rows = rows
        result.totals = {
            "meter_revenue_paisa": meter_total,
            "settled_paisa": settled_total,
            "new_receivables_paisa": new_receivables,
            "unresolved_paisa": unresolved,
        }
        result.notes_en.append(
            "Identity (§6): meter revenue = settled against today's sales"
            " + new receivables. Old-debt recovery is a cash receipt and"
            " never appears here. New receivables are a day total — they"
            " cannot be split per product from ticket data."
        )
        result.notes_ur.append(
            "تصدیق: میٹر آمدن = آج کی فروخت کا تصفیہ + نئے قابل وصول۔ پرانے"
            " قرض کی وصولی یہاں ظاہر نہیں ہوتی۔"
        )
        if not recon["settlements_recorded"]:
            result.notes_en.append("No settlements recorded for this day yet.")
            result.notes_ur.append("اس دن کا تصفیہ ابھی درج نہیں ہوا۔")
        if unresolved != 0:
            result.notes_en.append(
                f"WARNING: unresolved difference of {unresolved:,} paisa —"
                " the §6 identity does not balance. This is a warning, not"
                " an error; the day cannot be closed until it is 0."
            )
            result.notes_ur.append(
                f"تنبیہ: {unresolved:,} پیسے کا غیر حل شدہ فرق — تصدیق متوازن"
                " نہیں۔ یہ تنبیہ ہے، خرابی نہیں؛ فرق صفر ہونے تک دن بند نہیں"
                " ہو سکتا۔"
            )
        return result

    # ------------------------------------------------------------------
    # 6. customer_credit_register — tickets issued + payments received
    # ------------------------------------------------------------------
    def _build_customer_credit_register(
        self, conn: sqlite3.Connection, f: SimpleNamespace
    ) -> ReportResult:
        result = self._new_result("customer_credit_register", "", "")
        days = self._range_days(conn, f, result)
        result.period_label_en, result.period_label_ur = self._period_range(
            days[0], days[-1]
        )
        result.date_from, result.date_to = days[0], days[-1]
        if len(days) == 1:
            self._attach_closing(conn, result, days[0])
        customer = f.customer["id"] if f.customer else None
        customer_names = {
            row["id"]: row["name"]
            for row in conn.execute("SELECT id, name FROM customers").fetchall()
        }
        result.columns = [
            _col("business_date", "Date", "تاریخ", "date"),
            _col("kind", "Kind", "قسم"),
            _col("receipt_no", "Receipt no.", "رسید نمبر"),
            _col("customer", "Customer", "گاہک"),
            _col("gross_paisa", "Gross (PKR)", "کل (روپے)", "money"),
            _col("paid_now_paisa", "Paid now (PKR)", "ابھی ادا (روپے)", "money"),
            _col("payment_paisa", "Payment (PKR)", "ادائیگی (روپے)", "money"),
            _col("unpaid_paisa", "Unpaid (PKR)", "بقایا (روپے)", "money"),
            _col("status", "Status", "حیثیت"),
        ]
        rows: list[dict] = []
        for day in days:
            for ticket in self._credit.day_tickets(day):
                if customer is not None and ticket.customer_id != customer:
                    continue
                # Corrected reversal rule (009/011): a flipped original keeps
                # its posted contribution; the reversal row negates it.
                is_reversal = ticket.reverses_uuid is not None
                sign = -1 if is_reversal else 1
                rows.append(
                    {
                        "business_date": day,
                        "kind": "Ticket reversal" if is_reversal else "Ticket",
                        "receipt_no": ticket.receipt_no,
                        "customer": (
                            f"{ticket.customer_code} — "
                            f"{customer_names.get(ticket.customer_id, '')}".rstrip(
                                " —"
                            )
                        ),
                        "gross_paisa": sign * ticket.gross_amount_paisa,
                        "paid_now_paisa": (
                            0 if is_reversal else ticket.paid_now_paisa
                        ),
                        "payment_paisa": None,
                        "unpaid_paisa": ticket.unpaid_paisa,
                        "status": "reversal" if is_reversal else ticket.status,
                    }
                )
            for payment in self._payments.day_payments(day):
                if customer is not None and payment.customer_id != customer:
                    continue
                is_reversal = payment.reverses_uuid is not None
                rows.append(
                    {
                        "business_date": day,
                        "kind": (
                            "Payment reversal" if is_reversal else "Payment"
                        ),
                        "receipt_no": payment.receipt_no,
                        "customer": payment.customer_code,
                        "gross_paisa": None,
                        "paid_now_paisa": None,
                        "payment_paisa": (
                            -payment.amount_paisa
                            if is_reversal
                            else payment.amount_paisa
                        ),
                        "unpaid_paisa": None,
                        "status": "reversal" if is_reversal else payment.status,
                    }
                )
        result.rows = rows
        result.totals = self._totals(
            rows, ["gross_paisa", "paid_now_paisa", "payment_paisa"]
        )
        result.notes_en.append(
            "Credit tickets issued and customer payments received. Payments"
            " reduce receivables; they are never revenue."
        )
        result.notes_ur.append(
            "جاری کردہ ادھار پرچیاں اور موصول شدہ ادائیگیاں۔ ادائیگیاں قابل"
            " وصول کم کرتی ہیں؛ یہ آمدن نہیں۔"
        )
        return result

    # ------------------------------------------------------------------
    # 7. customer_statement — per-customer ledger with running balance
    # ------------------------------------------------------------------
    def _build_customer_statement(
        self, conn: sqlite3.Connection, f: SimpleNamespace
    ) -> ReportResult:
        if f.customer is None:
            raise errors.ValidationError(
                "customer_statement requires customer_id.",
                key="err.report_customer_required",
            )
        customer = f.customer
        result = self._new_result(
            "customer_statement",
            *self._period_range(
                f.date_from or "…", f.date_to or "…"
            ),
            date_from=f.date_from,
            date_to=f.date_to,
        )
        if f.date_from and f.date_to:
            result.period_label_en, result.period_label_ur = self._period_range(
                f.date_from, f.date_to
            )
        else:
            result.period_label_en = (
                f"Customer {customer['code']} — all history"
            )
            result.period_label_ur = (
                f"گاہک {customer['code']} — مکمل ریکارڈ"
            )
        statement = self._credit.statement(
            customer["id"], date_from=f.date_from, date_to=f.date_to
        )
        result.columns = [
            _col("business_date", "Date", "تاریخ", "date"),
            _col("description", "Description", "تفصیل"),
            _col("debit_paisa", "Debit (PKR)", "ڈیبٹ (روپے)", "money"),
            _col("credit_paisa", "Credit (PKR)", "کریڈٹ (روپے)", "money"),
            _col("balance_paisa", "Balance (PKR)", "بیلنس (روپے)", "money"),
        ]
        rows: list[dict] = [
            {
                "business_date": None,
                "description": "Opening balance",
                "debit_paisa": None,
                "credit_paisa": None,
                "balance_paisa": statement.opening_paisa,
            }
        ]
        running = statement.opening_paisa
        for entry in statement.entries:
            running += entry.debit_paisa - entry.credit_paisa
            rows.append(
                {
                    "business_date": entry.business_date,
                    "description": entry.description,
                    "debit_paisa": entry.debit_paisa or None,
                    "credit_paisa": entry.credit_paisa or None,
                    "balance_paisa": running,
                }
            )
        assert running == statement.closing_paisa, (
            "statement running balance diverged from the ledger formula"
        )
        result.rows = rows
        result.totals = self._totals(rows, ["debit_paisa", "credit_paisa"])
        result.notes_en.append(
            f"Statement for {customer['code']} — {customer['name']}."
            f" Closing balance {statement.closing_paisa:,} paisa = opening"
            " + charges − paid-now − allocations − adjustments (§7)."
        )
        result.notes_ur.append(
            f"گاہک {customer['code']} کا کھاتہ۔ اختتامی بیلنس = ابتدائی بیلنس"
            " + واجبات − فوری ادائیگی − تقسیم − ایڈجسٹمنٹ۔"
        )
        return result

    # ------------------------------------------------------------------
    # 8. payment_receipt — one receipt, by uuid or receipt_no
    # ------------------------------------------------------------------
    def _resolve_payment_uuid(
        self, conn: sqlite3.Connection, reference: str
    ) -> str:
        row = conn.execute(
            "SELECT uuid FROM customer_payments WHERE uuid = ?", (reference,)
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT uuid FROM customer_payments WHERE receipt_no = ?",
                (reference,),
            ).fetchone()
        if row is None:
            raise errors.NotFoundError(
                f"Unknown payment: {reference!r}.",
                key="err.report_unknown_payment",
                params={"reference": reference},
            )
        return row["uuid"]

    def _build_payment_receipt(
        self, conn: sqlite3.Connection, f: SimpleNamespace
    ) -> ReportResult:
        if not f.reference or not str(f.reference).strip():
            raise errors.ValidationError(
                "payment_receipt requires reference"
                " (payment uuid or receipt no.).",
                key="err.report_reference_required",
            )
        reference = str(f.reference).strip()
        uuid = self._resolve_payment_uuid(conn, reference)
        data = self._payments.receipt_data(uuid)
        payment = data["payment"]
        customer = data["customer"]
        period_en, period_ur = self._period_single(payment["business_date"])
        result = self._new_result("payment_receipt", period_en, period_ur)
        result.date_from = result.date_to = payment["business_date"]
        self._attach_closing(conn, result, payment["business_date"])
        result.columns = [
            _col("field", "Field", "خانہ"),
            _col("value", "Value", "قدر"),
            _col("amount_paisa", "Amount (PKR)", "رقم (روپے)", "money"),
        ]
        rows: list[dict] = [
            {"field": "Receipt no.", "value": payment["receipt_no"], "amount_paisa": None},
            {"field": "Date", "value": payment["business_date"], "amount_paisa": None},
            {
                "field": "Customer",
                "value": f"{customer['customer_code']} — {customer['name']}",
                "amount_paisa": None,
            },
            {"field": "Method", "value": payment["method"], "amount_paisa": None},
            {
                "field": "Amount received",
                "value": None,
                "amount_paisa": payment["amount_paisa"],
            },
        ]
        for alloc in data["allocations"]:
            rows.append(
                {
                    "field": "Allocated to ticket",
                    "value": alloc["ticket_receipt_no"] or alloc["ticket_uuid"][:8],
                    "amount_paisa": alloc["effective_paisa"],
                }
            )
        rows.append(
            {
                "field": "Unallocated",
                "value": None,
                "amount_paisa": data["unallocated_paisa"],
            }
        )
        result.rows = rows
        result.totals = {"amount_paisa": payment["amount_paisa"]}
        result.notes_en.append(
            f"Payment {payment['receipt_no']} ({payment['status']})."
            f" Customer outstanding after this receipt:"
            f" {customer['outstanding_paisa']:,} paisa."
        )
        result.notes_ur.append(
            f"ادائیگی {payment['receipt_no']} ({payment['status']})۔"
        )
        if payment["status"] == "reversed":
            result.notes_en.append(
                "This payment was reversed; the reversal row negates it in"
                " the ledger."
            )
            result.notes_ur.append("یہ ادائیگی منسوخ ہو چکی ہے۔")
        return result

    # ------------------------------------------------------------------
    # 9. outstanding_balances — ledger-derived, optionally as of a date
    # ------------------------------------------------------------------
    def _build_outstanding_balances(
        self, conn: sqlite3.Connection, f: SimpleNamespace
    ) -> ReportResult:
        if f.date_to:
            period_en = f"Outstanding as of {f.date_to}"
            period_ur = f"{f.date_to} تک بقایا جات"
        else:
            period_en = "Outstanding balances (current)"
            period_ur = "بقایا جات (موجودہ)"
        result = self._new_result(
            "outstanding_balances", period_en, period_ur, date_to=f.date_to
        )
        if f.date_to:
            self._attach_closing(conn, result, f.date_to)
        result.columns = [
            _col("customer_code", "Code", "کوڈ"),
            _col("customer", "Customer", "گاہک"),
            _col("credit_limit_paisa", "Credit limit (PKR)", "ادھار حد (روپے)", "money"),
            _col("outstanding_paisa", "Outstanding (PKR)", "بقایا (روپے)", "money"),
        ]
        cutoff = (
            timecalc.karachi_end_of_day_utc(f.date_to) if f.date_to else None
        )
        customers = conn.execute(
            "SELECT id, customer_code, name, credit_limit_paisa, created_at_utc"
            " FROM customers ORDER BY customer_code"
        ).fetchall()
        rows: list[dict] = []
        for customer in customers:
            if cutoff is not None:
                balance = conn.execute(
                    "SELECT COALESCE(SUM(debit_paisa - credit_paisa), 0)"
                    " FROM v_customer_ledger"
                    " WHERE customer_id = ? AND occurred_at_utc <= ?",
                    (customer["id"], cutoff),
                ).fetchone()[0]
                has_entries = (
                    conn.execute(
                        "SELECT COUNT(*) FROM v_customer_ledger"
                        " WHERE customer_id = ? AND occurred_at_utc <= ?",
                        (customer["id"], cutoff),
                    ).fetchone()[0]
                    > 0
                )
                if not has_entries and customer["created_at_utc"] > cutoff:
                    continue  # did not exist yet at the as-of date
            else:
                balance = conn.execute(
                    "SELECT COALESCE(SUM(debit_paisa - credit_paisa), 0)"
                    " FROM v_customer_ledger WHERE customer_id = ?",
                    (customer["id"],),
                ).fetchone()[0]
            rows.append(
                {
                    "customer_code": customer["customer_code"],
                    "customer": customer["name"],
                    "credit_limit_paisa": customer["credit_limit_paisa"],
                    "outstanding_paisa": int(balance),
                }
            )
        result.rows = rows
        result.totals = self._totals(rows, ["outstanding_paisa"])
        result.notes_en.append(
            "Ledger-derived (§7): opening + Σ ticket gross − Σ paid-now"
            " − Σ allocations ± Σ adjustments. Equals the sum of the"
            " per-customer statement closing balances."
        )
        result.notes_ur.append(
            "کھاتے سے حاصل شدہ: ابتدائی بیلنس + ٹکٹ − فوری ادائیگی − تقسیم"
            " ± ایڈجسٹمنٹ۔"
        )
        return result

    # ------------------------------------------------------------------
    # 10. fuel_stock — per-tank movement detail for a day
    # ------------------------------------------------------------------
    def _build_fuel_stock(
        self, conn: sqlite3.Connection, f: SimpleNamespace
    ) -> ReportResult:
        result = self._new_result("fuel_stock", "", "")
        day = self._single_day(conn, f, result)
        result.period_label_en, result.period_label_ur = self._period_single(day)
        result.date_from = result.date_to = day
        self._attach_closing(conn, result, day)
        result.columns = [
            _col("tank", "Tank", "ٹینک"),
            _col("product", "Product", "ایندھن"),
            _col("opening_ml", "Opening (ml)", "ابتدائی (ملی لیٹر)", "quantity"),
            _col("received_ml", "Received (ml)", "موصول (ملی لیٹر)", "quantity"),
            _col("sold_ml", "Sold (ml)", "فروخت (ملی لیٹر)", "quantity"),
            _col("adjustments_ml", "Adjustments (ml)", "ایڈجسٹمنٹ (ملی لیٹر)", "quantity"),
            _col("theoretical_ml", "Theoretical (ml)", "نظریاتی (ملی لیٹر)", "quantity"),
            _col("physical_ml", "Physical (ml)", "جسمانی (ملی لیٹر)", "quantity"),
            _col("variance_ml", "Variance (ml)", "فرق (ملی لیٹر)", "quantity"),
        ]
        rows = [
            {
                "tank": position.tank_code,
                "product": position.fuel_product_code,
                "opening_ml": position.opening_ml,
                "received_ml": position.received_ml,
                "sold_ml": position.sold_ml,
                "adjustments_ml": position.adjustments_ml,
                "theoretical_ml": position.theoretical_ml,
                "physical_ml": position.physical_ml,
                "variance_ml": position.variance_ml,
            }
            for position in self._inventory.stock_positions(day)
        ]
        result.rows = rows
        result.totals = self._totals(
            rows, ["opening_ml", "received_ml", "sold_ml", "theoretical_ml"]
        )
        result.notes_en.append(
            "Theoretical = opening + received − sold + adjustments"
            " (design §8). Physical counts never overwrite the theoretical"
            " figure; an approved variance becomes an adjustment movement."
        )
        result.notes_ur.append(
            "نظریاتی اسٹاک = ابتدائی + موصول − فروخت + ایڈجسٹمنٹ۔ جسمانی گنتی"
            " نظریاتی اعداد کو ختم نہیں کرتی۔"
        )
        return result

    # ------------------------------------------------------------------
    # 11. tank_reconciliation — theoretical vs dip reading
    # ------------------------------------------------------------------
    def _build_tank_reconciliation(
        self, conn: sqlite3.Connection, f: SimpleNamespace
    ) -> ReportResult:
        result = self._new_result("tank_reconciliation", "", "")
        day = self._single_day(conn, f, result)
        result.period_label_en, result.period_label_ur = self._period_single(day)
        result.date_from = result.date_to = day
        self._attach_closing(conn, result, day)
        result.columns = [
            _col("tank", "Tank", "ٹینک"),
            _col("product", "Product", "ایندھن"),
            _col("theoretical_ml", "Theoretical (ml)", "نظریاتی (ملی لیٹر)", "quantity"),
            _col("physical_ml", "Physical (ml)", "جسمانی (ملی لیٹر)", "quantity"),
            _col("variance_ml", "Variance (ml)", "فرق (ملی لیٹر)", "quantity"),
            _col("status", "Status", "حیثیت"),
        ]
        rows: list[dict] = []
        for position in self._inventory.stock_positions(day):
            if position.physical_ml is None:
                status = "No dip reading"
            elif position.variance_ml == 0:
                status = "OK"
            else:
                status = "Variance"
            rows.append(
                {
                    "tank": position.tank_code,
                    "product": position.fuel_product_code,
                    "theoretical_ml": position.theoretical_ml,
                    "physical_ml": position.physical_ml,
                    "variance_ml": position.variance_ml,
                    "status": status,
                }
            )
        result.rows = rows
        result.totals = self._totals(rows, ["variance_ml"])
        result.notes_en.append(
            "Variance = physical dip reading − theoretical stock. A missing"
            " dip reading is a gap, not a zero."
        )
        result.notes_ur.append(
            "فرق = جسمانی پیمائش − نظریاتی اسٹاک۔ گمشدہ پیمائش صفر نہیں۔"
        )
        return result

    # ------------------------------------------------------------------
    # 12. deliveries — fuel purchases
    # ------------------------------------------------------------------
    def _build_deliveries(
        self, conn: sqlite3.Connection, f: SimpleNamespace
    ) -> ReportResult:
        result = self._new_result("deliveries", "", "")
        days = self._range_days(conn, f, result)
        result.period_label_en, result.period_label_ur = self._period_range(
            days[0], days[-1]
        )
        result.date_from, result.date_to = days[0], days[-1]
        if len(days) == 1:
            self._attach_closing(conn, result, days[0])
        wanted = f.fuel_product["code"] if f.fuel_product else None
        result.columns = [
            _col("business_date", "Date", "تاریخ", "date"),
            _col("supplier", "Supplier", "سپلائر"),
            _col("product", "Product", "ایندھن"),
            _col("quantity_ml", "Quantity (ml)", "مقدار (ملی لیٹر)", "quantity"),
            _col("unit_cost_10k", "Unit cost (PKR/L)", "فی لیٹر لاگت (روپے)", "price"),
            _col("total_cost_paisa", "Total cost (PKR)", "کل لاگت (روپے)", "money"),
            _col("paid_paisa", "Paid (PKR)", "ادا شدہ (روپے)", "money"),
            _col("outstanding_paisa", "Outstanding (PKR)", "بقایا (روپے)", "money"),
            _col("invoice_no", "Invoice no.", "انوائس نمبر"),
        ]
        rows: list[dict] = []
        for day in days:
            for delivery in self._deliveries.list_deliveries(business_date=day):
                if wanted and delivery.fuel_product_code != wanted:
                    continue
                invoice_total = (
                    delivery.total_cost_paisa + (delivery.other_cost_paisa or 0)
                )
                rows.append(
                    {
                        "business_date": day,
                        "supplier": delivery.supplier_name,
                        "product": delivery.fuel_product_code,
                        "quantity_ml": delivery.quantity_ml,
                        "unit_cost_10k": delivery.unit_cost_10k,
                        "total_cost_paisa": invoice_total,
                        "paid_paisa": delivery.amount_paid_paisa,
                        "outstanding_paisa": invoice_total
                        - delivery.amount_paid_paisa,
                        "invoice_no": delivery.supplier_invoice_no,
                    }
                )
        result.rows = rows
        result.totals = self._totals(
            rows, ["quantity_ml", "total_cost_paisa", "paid_paisa", "outstanding_paisa"]
        )
        result.notes_en.append(
            "Total cost includes other delivery costs. Deliveries post"
            " stock-intake movements; they never touch physical cash."
        )
        result.notes_ur.append(
            "کل لاگت میں دیگر اخراجات شامل ہیں۔ ترسیل اسٹاک میں اضافہ کرتی"
            " ہے؛ نقد کو متاثر نہیں کرتی۔"
        )
        return result

    # ------------------------------------------------------------------
    # 13. supplier_balances — computed liabilities
    # ------------------------------------------------------------------
    def _build_supplier_balances(
        self, conn: sqlite3.Connection, f: SimpleNamespace
    ) -> ReportResult:
        result = self._new_result(
            "supplier_balances",
            "Current supplier balances",
            "موجودہ سپلائر بقایا جات",
        )
        result.columns = [
            _col("supplier", "Supplier", "سپلائر"),
            _col("invoiced_paisa", "Invoiced (PKR)", "انوائس (روپے)", "money"),
            _col("paid_at_delivery_paisa", "Paid at delivery (PKR)", "ترسیل پر ادا (روپے)", "money"),
            _col("paid_paisa", "Paid later (PKR)", "بعد میں ادا (روپے)", "money"),
            _col("outstanding_paisa", "Outstanding (PKR)", "بقایا (روپے)", "money"),
        ]
        rows = [
            {
                "supplier": balance.supplier_name,
                "invoiced_paisa": balance.invoiced_paisa,
                "paid_at_delivery_paisa": balance.paid_at_delivery_paisa,
                "paid_paisa": balance.paid_paisa,
                "outstanding_paisa": balance.outstanding_paisa,
            }
            for balance in self._suppliers.supplier_balances()
        ]
        result.rows = rows
        result.totals = self._totals(
            rows, ["invoiced_paisa", "paid_at_delivery_paisa", "paid_paisa", "outstanding_paisa"]
        )
        result.notes_en.append(
            "Computed (design §8): outstanding = invoiced − paid at"
            " delivery − later payments, net of reversals."
        )
        result.notes_ur.append(
            "حساب شدہ: بقایا = انوائس − ترسیل پر ادائیگی − بعد کی ادائیگیاں۔"
        )
        return result

    # ------------------------------------------------------------------
    # 14. other_materials — purchase/sale/adjustment transactions + stock
    # ------------------------------------------------------------------
    def _build_other_materials(
        self, conn: sqlite3.Connection, f: SimpleNamespace
    ) -> ReportResult:
        result = self._new_result("other_materials", "", "")
        days = self._range_days(conn, f, result)
        result.period_label_en, result.period_label_ur = self._period_range(
            days[0], days[-1]
        )
        result.date_from, result.date_to = days[0], days[-1]
        if len(days) == 1:
            self._attach_closing(conn, result, days[0])
        product = self._resolve_other_product(conn, f.category)
        product_id = product["id"] if product else None
        result.columns = [
            _col("business_date", "Date", "تاریخ", "date"),
            _col("product", "Product", "شے"),
            _col("txn_type", "Type", "قسم"),
            _col("quantity_units", "Qty (units)", "مقدار", "number"),
            _col("unit_price_paisa", "Unit price (PKR)", "فی اکائی قیمت (روپے)", "money"),
            _col("total_paisa", "Total (PKR)", "کل (روپے)", "money"),
        ]
        rows: list[dict] = []
        for day in days:
            for txn in self._materials.list_transactions(
                product_id=product_id, business_date=day
            ):
                signed_total = txn.total_paisa
                signed_qty = txn.quantity_units
                if txn.txn_type == "sale":
                    signed_total = -txn.total_paisa
                    signed_qty = -txn.quantity_units
                rows.append(
                    {
                        "business_date": day,
                        "product": txn.product_name,
                        "txn_type": txn.txn_type,
                        "quantity_units": signed_qty,
                        "unit_price_paisa": txn.unit_price_paisa,
                        "total_paisa": signed_total,
                    }
                )
        result.rows = rows
        result.totals = self._totals(rows, ["total_paisa"])
        stock_lines = []
        for item in self._materials.stock(product_id=product_id):
            stock_lines.append(f"{item.name}: {item.on_hand_units} units")
        if stock_lines:
            result.notes_en.append(
                "Current on-hand stock — " + "; ".join(stock_lines) + "."
                " Purchases add, sales subtract; totals are signed."
            )
            result.notes_ur.append(
                "موجودہ اسٹاک — " + "؛ ".join(stock_lines) + "۔"
            )
        return result

    # ------------------------------------------------------------------
    # 15. expenses
    # ------------------------------------------------------------------
    def _build_expenses(
        self, conn: sqlite3.Connection, f: SimpleNamespace
    ) -> ReportResult:
        result = self._new_result("expenses", "", "")
        days = self._range_days(conn, f, result)
        result.period_label_en, result.period_label_ur = self._period_range(
            days[0], days[-1]
        )
        result.date_from, result.date_to = days[0], days[-1]
        if len(days) == 1:
            self._attach_closing(conn, result, days[0])
        category = self._resolve_expense_category(conn, f.category)
        result.columns = [
            _col("business_date", "Date", "تاریخ", "date"),
            _col("category", "Category", "قسم"),
            _col("payee", "Payee", "وصول کنندہ"),
            _col("method", "Method", "طریقہ"),
            _col("amount_paisa", "Amount (PKR)", "رقم (روپے)", "money"),
            _col("status", "Status", "حیثیت"),
        ]
        # The corrected reversal rule (011, mirroring 009): a flipped
        # original keeps its posted contribution (the status flip is only a
        # marker); the reversal row negates it. Net of a void: 0. Both rows
        # are shown, exactly like the cash book.
        where = "e.business_date BETWEEN ? AND ?"
        params: list = [days[0], days[-1]]
        if category is not None:
            where += " AND e.category_id = ?"
            params.append(category["id"])
        expense_rows = conn.execute(
            "SELECT e.business_date, c.name_en AS category, e.payee,"
            " e.method, e.status,"
            " CASE WHEN e.reverses_uuid IS NOT NULL THEN -e.amount_paisa"
            "      ELSE e.amount_paisa END AS signed_paisa,"
            " CASE WHEN e.reverses_uuid IS NOT NULL THEN 'reversal'"
            "      ELSE e.status END AS display_status"
            " FROM expenses e JOIN expense_categories c ON c.id = e.category_id"
            f" WHERE {where} ORDER BY e.occurred_at_utc, e.id",
            params,
        ).fetchall()
        rows = [
            {
                "business_date": row["business_date"],
                "category": row["category"],
                "payee": row["payee"],
                "method": row["method"],
                "amount_paisa": int(row["signed_paisa"]),
                "status": row["display_status"],
            }
            for row in expense_rows
        ]
        result.rows = rows
        result.totals = self._totals(rows, ["amount_paisa"])
        result.notes_en.append(
            "Net of reversals. Cash expenses also appear in the cash book;"
            " bank/other expenses never touch physical cash (§11.13)."
        )
        result.notes_ur.append(
            "منسوخیوں کے بعد خالص۔ نقد اخراجات کیش بک میں بھی ظاہر ہوتے ہیں۔"
        )
        return result

    # ------------------------------------------------------------------
    # 16. pnl_daily — frozen closing snapshot for closed days
    # ------------------------------------------------------------------
    def _build_pnl_daily(
        self, conn: sqlite3.Connection, f: SimpleNamespace
    ) -> ReportResult:
        result = self._new_result("pnl_daily", "", "")
        day = self._single_day(conn, f, result)
        result.period_label_en, result.period_label_ur = self._period_single(day)
        result.date_from = result.date_to = day
        self._attach_closing(conn, result, day)
        result.columns = [
            _col("product", "Product", "ایندھن"),
            _col("revenue_paisa", "Revenue (PKR)", "آمدن (روپے)", "money"),
            _col("cogs_paisa", "COGS (PKR)", "لاگت (روپے)", "money"),
            _col("gross_profit_paisa", "Gross profit (PKR)", "مجموعی منافع (روپے)", "money"),
        ]
        pnl = self._accounting.daily_pnl(day)
        rows: list[dict] = []
        for line in pnl.lines:
            rows.append(
                {
                    "product": line.fuel_product_code or line.product_name,
                    "revenue_paisa": line.revenue_paisa,
                    "cogs_paisa": line.cogs_paisa,
                    "gross_profit_paisa": line.gross_profit_paisa,
                }
            )
        if pnl.other_revenue_paisa or pnl.other_cogs_paisa:
            other_gross = (
                pnl.other_revenue_paisa - pnl.other_cogs_paisa
                if pnl.other_cogs_paisa is not None
                else None
            )
            rows.append(
                {
                    "product": "Other products",
                    "revenue_paisa": pnl.other_revenue_paisa,
                    "cogs_paisa": pnl.other_cogs_paisa,
                    "gross_profit_paisa": other_gross,
                }
            )
        rows.append(
            {
                "product": "Total",
                "revenue_paisa": pnl.total_revenue_paisa,
                "cogs_paisa": pnl.total_cogs_paisa,
                "gross_profit_paisa": pnl.gross_profit_paisa,
            }
        )
        rows.append(
            {
                "product": "Operating expenses",
                "revenue_paisa": None,
                "cogs_paisa": None,
                "gross_profit_paisa": -pnl.operating_expenses_paisa,
            }
        )
        rows.append(
            {
                "product": "Net profit",
                "revenue_paisa": None,
                "cogs_paisa": None,
                "gross_profit_paisa": pnl.net_profit_paisa,
            }
        )
        result.rows = rows
        result.totals = {
            "revenue_paisa": pnl.total_revenue_paisa,
            "cogs_paisa": pnl.total_cogs_paisa,
            "gross_profit_paisa": pnl.gross_profit_paisa,
            "net_profit_paisa": pnl.net_profit_paisa,
        }
        if pnl.source == "closing_snapshot":
            result.notes_en.append(
                "Figures are from the immutable closing-record snapshot;"
                " the day's P&L can never change afterwards."
            )
            result.notes_ur.append(
                "اعداد بندش کے ناقابل تبدیل ریکارڈ سے ہیں؛ بعد میں تبدیل"
                " نہیں ہو سکتے۔"
            )
        else:
            result.notes_en.append(
                "Day is not closed: figures are computed live, not frozen."
            )
            result.notes_ur.append(
                "دن بند نہیں: اعداد براہ راست حساب شدہ ہیں، محفوظ نہیں۔"
            )
        if pnl.estimated:
            result.notes_en.append(
                "Estimated figures: " + "; ".join(pnl.estimate_reasons)
            )
            result.notes_ur.append(
                "تخمینی اعداد: " + "؛ ".join(pnl.estimate_reasons)
            )
        result.notes_en.append(
            f"New receivables created: {pnl.receivables_created_paisa:,} paisa"
            " (not revenue)."
        )
        return result

    # ------------------------------------------------------------------
    # 17. pnl_monthly — per-day rows + summed period P&L
    # ------------------------------------------------------------------
    def _month_bounds(self, f: SimpleNamespace, conn: sqlite3.Connection) -> tuple[int, int]:
        anchor = f.business_date or f.date_from or f.date_to or self._open_day(conn)
        if anchor is None:
            row = conn.execute(
                "SELECT MAX(business_date) FROM business_days"
            ).fetchone()
            anchor = row[0] if row else None
        if anchor is None:
            raise errors.ValidationError(
                "Cannot determine the month: no business day exists and no"
                " date was given.",
                key="err.report_no_month",
            )
        year, month = int(anchor[:4]), int(anchor[5:7])
        return year, month

    @staticmethod
    def _month_range(year: int, month: int) -> tuple[str, str]:
        start = date(year, month, 1)
        end = date(year + 1, 1, 1) - timedelta(days=1) if month == 12 else date(
            year, month + 1, 1
        ) - timedelta(days=1)
        return start.isoformat(), end.isoformat()

    def _build_pnl_monthly(
        self, conn: sqlite3.Connection, f: SimpleNamespace
    ) -> ReportResult:
        year, month = self._month_bounds(f, conn)
        start, end = self._month_range(year, month)
        period_en, period_ur = self._period_month(year, month)
        result = self._new_result(
            "pnl_monthly", period_en, period_ur, date_from=start, date_to=end
        )
        result.columns = [
            _col("business_date", "Date", "تاریخ", "date"),
            _col("revenue_paisa", "Revenue (PKR)", "آمدن (روپے)", "money"),
            _col("cogs_paisa", "COGS (PKR)", "لاگت (روپے)", "money"),
            _col("gross_profit_paisa", "Gross profit (PKR)", "مجموعی منافع (روپے)", "money"),
            _col("operating_expenses_paisa", "Expenses (PKR)", "اخراجات (روپے)", "money"),
            _col("net_profit_paisa", "Net profit (PKR)", "خالص منافع (روپے)", "money"),
        ]
        rows: list[dict] = []
        for day in self._date_range(start, end):
            try:
                pnl = self._accounting.daily_pnl(day)
            except errors.NotFoundError:
                continue  # no business day — nothing to report
            rows.append(
                {
                    "business_date": day,
                    "revenue_paisa": pnl.total_revenue_paisa,
                    "cogs_paisa": pnl.total_cogs_paisa,
                    "gross_profit_paisa": pnl.gross_profit_paisa,
                    "operating_expenses_paisa": pnl.operating_expenses_paisa,
                    "net_profit_paisa": pnl.net_profit_paisa,
                }
            )
            if pnl.estimated:
                result.notes_en.append(
                    f"{day}: estimated — " + "; ".join(pnl.estimate_reasons)
                )
        # The month total reuses the period summation (closed days contribute
        # their immutable snapshots; design §8).
        period = self._accounting.period_pnl(start, end)
        rows.append(
            {
                "business_date": "Total",
                "revenue_paisa": period.total_revenue_paisa,
                "cogs_paisa": period.total_cogs_paisa,
                "gross_profit_paisa": period.gross_profit_paisa,
                "operating_expenses_paisa": period.operating_expenses_paisa,
                "net_profit_paisa": period.net_profit_paisa,
            }
        )
        result.rows = rows
        result.totals = {
            "revenue_paisa": period.total_revenue_paisa,
            "cogs_paisa": period.total_cogs_paisa,
            "gross_profit_paisa": period.gross_profit_paisa,
            "operating_expenses_paisa": period.operating_expenses_paisa,
            "net_profit_paisa": period.net_profit_paisa,
        }
        if period.estimated:
            result.notes_en.append(
                "Period figures are estimated: "
                + "; ".join(period.estimate_reasons)
            )
            result.notes_ur.append(
                "مدت کے اعداد تخمینی ہیں۔"
            )
        else:
            result.notes_en.append(
                "Closed days contribute their frozen snapshots; old P&L"
                " never changes."
            )
            result.notes_ur.append(
                "بند دنوں کے محفوظ شدہ اعداد شامل ہیں؛ پرانا نفع و نقصان کبھی"
                " نہیں بدلتا۔"
            )
        return result

    # ------------------------------------------------------------------
    # 18. pnl_annual — per-month rows + summed year P&L
    # ------------------------------------------------------------------
    def _build_pnl_annual(
        self, conn: sqlite3.Connection, f: SimpleNamespace
    ) -> ReportResult:
        year, _month = self._month_bounds(f, conn)
        period_en, period_ur = self._period_year(year)
        result = self._new_result(
            "pnl_annual", period_en, period_ur,
            date_from=f"{year}-01-01", date_to=f"{year}-12-31",
        )
        result.columns = [
            _col("month", "Month", "مہینہ"),
            _col("revenue_paisa", "Revenue (PKR)", "آمدن (روپے)", "money"),
            _col("cogs_paisa", "COGS (PKR)", "لاگت (روپے)", "money"),
            _col("gross_profit_paisa", "Gross profit (PKR)", "مجموعی منافع (روپے)", "money"),
            _col("operating_expenses_paisa", "Expenses (PKR)", "اخراجات (روپے)", "money"),
            _col("net_profit_paisa", "Net profit (PKR)", "خالص منافع (روپے)", "money"),
        ]
        rows: list[dict] = []
        for month in range(1, 13):
            start, end = self._month_range(year, month)
            period = self._accounting.period_pnl(start, end)
            label_en, _label_ur = self._period_month(year, month)
            rows.append(
                {
                    "month": label_en,
                    "revenue_paisa": period.total_revenue_paisa,
                    "cogs_paisa": period.total_cogs_paisa,
                    "gross_profit_paisa": period.gross_profit_paisa,
                    "operating_expenses_paisa": period.operating_expenses_paisa,
                    "net_profit_paisa": period.net_profit_paisa,
                }
            )
            if period.estimated and any(
                v
                for v in (
                    period.total_revenue_paisa,
                    period.operating_expenses_paisa,
                )
            ):
                result.notes_en.append(
                    f"{label_en}: estimated — "
                    + "; ".join(period.estimate_reasons[:2])
                )
        year_total = self._accounting.period_pnl(f"{year}-01-01", f"{year}-12-31")
        rows.append(
            {
                "month": "Total",
                "revenue_paisa": year_total.total_revenue_paisa,
                "cogs_paisa": year_total.total_cogs_paisa,
                "gross_profit_paisa": year_total.gross_profit_paisa,
                "operating_expenses_paisa": year_total.operating_expenses_paisa,
                "net_profit_paisa": year_total.net_profit_paisa,
            }
        )
        result.rows = rows
        result.totals = {
            "revenue_paisa": year_total.total_revenue_paisa,
            "cogs_paisa": year_total.total_cogs_paisa,
            "gross_profit_paisa": year_total.gross_profit_paisa,
            "operating_expenses_paisa": year_total.operating_expenses_paisa,
            "net_profit_paisa": year_total.net_profit_paisa,
        }
        result.notes_en.append(
            "Each month sums its daily snapshots (closed days contribute"
            " frozen figures, design §8)."
        )
        result.notes_ur.append(
            "ہر مہینہ اپنے روزانہ اعداد کا مجموعہ ہے۔"
        )
        return result

    # ------------------------------------------------------------------
    # 19. cash_difference — shortage/surplus, never auto-written-off
    # ------------------------------------------------------------------
    def _build_cash_difference(
        self, conn: sqlite3.Connection, f: SimpleNamespace
    ) -> ReportResult:
        result = self._new_result("cash_difference", "", "")
        days = self._range_days(conn, f, result)
        result.period_label_en, result.period_label_ur = self._period_range(
            days[0], days[-1]
        )
        result.date_from, result.date_to = days[0], days[-1]
        if len(days) == 1:
            self._attach_closing(conn, result, days[0])
        result.columns = [
            _col("business_date", "Date", "تاریخ", "date"),
            _col("opening_paisa", "Opening (PKR)", "ابتدائی (روپے)", "money"),
            _col("receipts_paisa", "Receipts (PKR)", "وصولیاں (روپے)", "money"),
            _col("payments_paisa", "Payments (PKR)", "ادائیگیاں (روپے)", "money"),
            _col("expected_paisa", "Expected (PKR)", "متوقع (روپے)", "money"),
            _col("physical_paisa", "Physical (PKR)", "جسمانی (روپے)", "money"),
            _col("difference_paisa", "Difference (PKR)", "فرق (روپے)", "money"),
            _col("shortage_paisa", "Shortage (PKR)", "کمی (روپے)", "money"),
            _col("surplus_paisa", "Surplus (PKR)", "اضافہ (روپے)", "money"),
        ]
        placeholders = ",".join("?" for _ in days)
        position_rows = {
            row["business_date"]: row
            for row in conn.execute(
                "SELECT * FROM v_daily_cash_position"
                f" WHERE business_date IN ({placeholders})",
                days,
            ).fetchall()
        }
        rows: list[dict] = []
        for day in days:
            prow = position_rows.get(day)
            if prow is None:
                rows.append(
                    {
                        "business_date": day,
                        "opening_paisa": None,
                        "receipts_paisa": None,
                        "payments_paisa": None,
                        "expected_paisa": None,
                        "physical_paisa": None,
                        "difference_paisa": None,
                        "shortage_paisa": None,
                        "surplus_paisa": None,
                    }
                )
                continue
            difference = (
                int(prow["difference_paisa"])
                if prow["difference_paisa"] is not None
                else None
            )
            shortage = (
                -difference if difference is not None and difference < 0 else
                (0 if difference is not None else None)
            )
            surplus = (
                difference if difference is not None and difference > 0 else
                (0 if difference is not None else None)
            )
            rows.append(
                {
                    "business_date": day,
                    "opening_paisa": int(prow["opening_cash_paisa"]),
                    "receipts_paisa": int(prow["receipts_paisa"]),
                    "payments_paisa": int(prow["payments_paisa"]),
                    "expected_paisa": int(prow["expected_paisa"]),
                    "physical_paisa": (
                        int(prow["physical_cash_paisa"])
                        if prow["physical_cash_paisa"] is not None
                        else None
                    ),
                    "difference_paisa": difference,
                    "shortage_paisa": shortage,
                    "surplus_paisa": surplus,
                }
            )
        result.rows = rows
        result.totals = self._totals(
            rows, ["difference_paisa", "shortage_paisa", "surplus_paisa"]
        )
        result.notes_en.append(
            "Shortage/surplus is reported only — it is never"
            " auto-written-off or auto-adjusted (design §11.8). Resolution"
            " is a manager decision recorded as an audited note or expense."
        )
        result.notes_ur.append(
            "کمی/اضافہ صرف رپورٹ ہوتا ہے — یہ خودکار طور پر ختم یا درست"
            " نہیں کیا جاتا۔ اس کا حل مینیجر کا فیصلہ ہے۔"
        )
        return result

    # ------------------------------------------------------------------
    # 20. audit_history — newest first, filterable by date range
    # ------------------------------------------------------------------
    def _build_audit_history(
        self, conn: sqlite3.Connection, f: SimpleNamespace
    ) -> ReportResult:
        if f.date_from and f.date_to:
            period_en, period_ur = self._period_range(f.date_from, f.date_to)
        elif f.date_from or f.date_to:
            bound = f.date_from or f.date_to
            period_en, period_ur = self._period_single(bound)
        else:
            period_en, period_ur = "All history", "مکمل ریکارڈ"
        result = self._new_result(
            "audit_history", period_en, period_ur,
            date_from=f.date_from, date_to=f.date_to,
        )
        result.columns = [
            _col("id", "ID", "شناخت", "number"),
            _col("occurred_at_utc", "Time (UTC)", "وقت"),
            _col("table_name", "Table", "ٹیبل"),
            _col("record_id", "Record", "ریکارڈ", "number"),
            _col("action", "Action", "عمل"),
            _col("business_date", "Business date", "کاروباری تاریخ", "date"),
            _col("actor", "Actor", "فاعل"),
            _col("reason", "Reason", "وجہ"),
        ]
        where = "1 = 1"
        params: list = []
        if f.date_from:
            where += " AND occurred_at_utc >= ?"
            params.append(timecalc.karachi_start_of_day_utc(f.date_from))
        if f.date_to:
            where += " AND occurred_at_utc <= ?"
            params.append(timecalc.karachi_end_of_day_utc(f.date_to))
        audit_rows = conn.execute(
            "SELECT id, occurred_at_utc, table_name, record_id, action,"
            " business_date, actor, reason FROM audit_log"
            f" WHERE {where} ORDER BY id DESC LIMIT ?",
            (*params, _AUDIT_ROW_LIMIT + 1),
        ).fetchall()
        truncated = len(audit_rows) > _AUDIT_ROW_LIMIT
        rows = [
            {
                "id": int(row["id"]),
                "occurred_at_utc": row["occurred_at_utc"],
                "table_name": row["table_name"],
                "record_id": int(row["record_id"]),
                "action": row["action"],
                "business_date": row["business_date"],
                "actor": row["actor"],
                "reason": row["reason"],
            }
            for row in audit_rows[:_AUDIT_ROW_LIMIT]
        ]
        result.rows = rows
        result.totals = {"id": len(rows)}
        result.notes_en.append(
            "Newest first. The date range filters on the audit event time"
            " (occurred_at_utc, machine UTC); each row also carries the"
            " business date of the underlying record where the table has one."
        )
        result.notes_ur.append(
            "تازہ ترین پہلے۔ مدت آڈٹ واقعے کے وقت پر لاگو ہوتی ہے۔"
        )
        if truncated:
            result.notes_en.append(
                f"Truncated to the newest {_AUDIT_ROW_LIMIT} entries;"
                " narrow the date range for older history."
            )
            result.notes_ur.append(
                f"صرف تازہ ترین {_AUDIT_ROW_LIMIT} اندراجات دکھائے گئے ہیں۔"
            )
        return result

    # ------------------------------------------------------------------
    # 21. backup_history
    # ------------------------------------------------------------------
    def _build_backup_history(
        self, conn: sqlite3.Connection, f: SimpleNamespace
    ) -> ReportResult:
        if f.date_from and f.date_to:
            period_en, period_ur = self._period_range(f.date_from, f.date_to)
        elif f.date_from or f.date_to:
            bound = f.date_from or f.date_to
            period_en, period_ur = self._period_single(bound)
        else:
            period_en, period_ur = "All history", "مکمل ریکارڈ"
        result = self._new_result(
            "backup_history", period_en, period_ur,
            date_from=f.date_from, date_to=f.date_to,
        )
        result.columns = [
            _col("id", "ID", "شناخت", "number"),
            _col("requested_at_utc", "Requested (UTC)", "درخواست کا وقت"),
            _col("filename", "File", "فائل"),
            _col("destination", "Destination", "مقام"),
            _col("status", "Status", "حیثیت"),
            _col("size_bytes", "Size (bytes)", "سائز (بائٹس)", "number"),
            _col("verified_at_utc", "Verified (UTC)", "تصدیق کا وقت"),
            _col("error", "Error", "خرابی"),
        ]
        where = "1 = 1"
        params: list = []
        if f.date_from:
            where += " AND requested_at_utc >= ?"
            params.append(timecalc.karachi_start_of_day_utc(f.date_from))
        if f.date_to:
            where += " AND requested_at_utc <= ?"
            params.append(timecalc.karachi_end_of_day_utc(f.date_to))
        backup_rows = conn.execute(
            "SELECT id, requested_at_utc, filename, destination, status,"
            " size_bytes, verified_at_utc, error FROM backup_history"
            f" WHERE {where} ORDER BY id DESC",
            params,
        ).fetchall()
        rows = [
            {
                "id": int(row["id"]),
                "requested_at_utc": row["requested_at_utc"],
                "filename": row["filename"],
                "destination": row["destination"],
                "status": row["status"],
                "size_bytes": (
                    int(row["size_bytes"])
                    if row["size_bytes"] is not None
                    else None
                ),
                "verified_at_utc": row["verified_at_utc"],
                "error": row["error"],
            }
            for row in backup_rows
        ]
        result.rows = rows
        result.totals = {"id": len(rows)}
        return result
