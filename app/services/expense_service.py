"""Expenses: categories, posting, reversal (Phase 5).

Accounting rule: an expense is a dated, categorized cost with a payment
method. Only ``method='cash'`` posts a derived ``cash_payments`` row
(category ``'expense'``) — in the *same* transaction as the expense, so the
two can never diverge. ``bank``/``other`` expenses never touch physical
cash (design §11.14); they still count as operating expenses in P&L.

Reversal = mirror row (``reverses_uuid``) + posted → reversed flip + the
derived cash row reversed — one transaction, always.
"""

from __future__ import annotations

import sqlite3
import uuid as _uuid

from app.calculations import time as timecalc
from app.models.expense import ExpenseCategoryInfo, ExpenseInfo
from app.repositories.cashbook_repository import CashbookRepository
from app.repositories.expense_repository import ExpenseRepository
from app.services import errors
from app.services._base import ServiceBase, require_writable_day

_METHODS = ("cash", "bank", "other")


def _to_category_info(row) -> ExpenseCategoryInfo:
    return ExpenseCategoryInfo(
        id=int(row["id"]),
        name_en=row["name_en"],
        name_ur=row["name_ur"],
        is_active=bool(row["is_active"]),
    )


def _to_expense_info(row) -> ExpenseInfo:
    amount = int(row["amount_paisa"])
    if row["reverses_uuid"] is not None:
        effective = -amount
    elif row["status"] == "reversed":
        effective = 0
    else:
        effective = amount
    return ExpenseInfo(
        id=int(row["id"]),
        uuid=row["uuid"],
        business_date=row["business_date"],
        category_id=int(row["category_id"]),
        category_en=row["category_en"],
        amount_paisa=amount,
        effective_paisa=effective,
        method=row["method"],
        payee=row["payee"],
        reference=row["reference"],
        status=row["status"],
        occurred_at_utc=row["occurred_at_utc"],
        created_by=row["created_by"],
    )


class ExpenseService(ServiceBase):
    """Manages expense categories and posts dated expenses."""

    def __init__(self, db_manager, actor: str | None = None) -> None:
        super().__init__(db_manager, actor)
        self._expenses = ExpenseRepository()
        self._cash = CashbookRepository()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _require_stamp_on_day(
        occurred_at_utc: str | None, business_date: str
    ) -> str:
        """Validate (or default) the event timestamp and pin it to the
        business day's Asia/Karachi calendar date (Phase 4 §11.2, strict)."""
        if occurred_at_utc is None:
            stamp = timecalc.utc_now_iso()
        else:
            try:
                stamp = timecalc.require_utc_iso(occurred_at_utc)
            except ValueError as exc:
                raise errors.ValidationError(
                    str(exc),
                    key="err.invalid_utc_timestamp",
                    params={"value": occurred_at_utc},
                ) from exc
        if timecalc.utc_calendar_date_in_karachi(stamp) != business_date:
            raise errors.ValidationError(
                "The event time falls on"
                f" {timecalc.utc_calendar_date_in_karachi(stamp)} in"
                f" Asia/Karachi, not the business date {business_date}.",
                key="err.event_time_wrong_day",
                params={
                    "event_date": timecalc.utc_calendar_date_in_karachi(stamp),
                    "day_date": business_date,
                },
            )
        return stamp

    def _resolve_category(
        self, conn: sqlite3.Connection, category: str | int
    ):
        """Expense category by id or English name; must exist and be active."""
        if isinstance(category, bool):
            raise errors.ValidationError(
                f"Invalid expense category: {category!r}.",
                key="err.invalid_expense_category",
                params={"ref": category},
            )
        row = None
        if isinstance(category, int):
            row = self._expenses.get_category(conn, category)
        elif isinstance(category, str):
            clean = category.strip()
            if not clean:
                raise errors.ValidationError(
                    "Expense category must not be empty.",
                    key="err.expense_category_empty",
                )
            row = self._expenses.get_category_by_name(conn, clean)
        else:
            raise errors.ValidationError(
                f"Invalid expense category: {category!r}.",
                key="err.invalid_expense_category",
                params={"ref": category},
            )
        if row is None or not row["is_active"]:
            raise errors.ValidationError(
                f"Unknown or inactive expense category: {category!r}.",
                key="err.unknown_expense_category",
                params={"ref": category},
            )
        return row

    # ------------------------------------------------------------------
    # categories
    # ------------------------------------------------------------------
    def list_categories(self, active_only: bool = True) -> list[ExpenseCategoryInfo]:
        """Expense categories, optionally active only."""
        with self.read_connection() as conn:
            rows = self._expenses.list_categories(conn, active_only=active_only)
        return [_to_category_info(row) for row in rows]

    def add_category(
        self,
        name_en: str,
        name_ur: str | None = None,
        *,
        actor: str | None = None,
    ) -> int:
        """Add an expense category. The English name is mandatory and unique."""
        clean = (name_en or "").strip()
        if not clean:
            raise errors.ValidationError(
                "Category name must not be empty.",
                key="err.category_name_empty",
            )
        who = self.resolve_actor(actor)
        with self.transaction() as conn:
            if self._expenses.get_category_by_name(conn, clean) is not None:
                raise errors.DuplicateError(
                    f"An expense category named {clean!r} already exists.",
                    key="err.expense_category_exists",
                    params={"name": clean},
                )
            return self._expenses.insert_category(
                conn, clean, (name_ur or "").strip() or None, who
            )

    def set_category_active(
        self, category_id: int, active: bool, *, actor: str | None = None
    ) -> None:
        """Activate/deactivate a category. Unknown id → NotFoundError."""
        with self.transaction() as conn:
            row = self._expenses.get_category(conn, int(category_id))
            if row is None:
                raise errors.NotFoundError(
                    f"Unknown expense category: {category_id!r}.",
                    key="err.unknown_expense_category_id",
                    params={"ref": category_id},
                )
            self._expenses.set_category_active(conn, int(row["id"]), bool(active))

    # ------------------------------------------------------------------
    # expenses
    # ------------------------------------------------------------------
    def record_expense(
        self,
        *,
        business_date: str,
        category: str | int,
        amount_paisa: int,
        method: str = "cash",
        payee: str | None = None,
        reference: str | None = None,
        occurred_at_utc: str | None = None,
        actor: str | None = None,
    ) -> ExpenseInfo:
        """Record a posted expense.

        ``method='cash'`` additionally writes the derived ``cash_payments``
        row (category ``'expense'``) in the SAME transaction; ``bank``/``other``
        never touch physical cash. Returns the expense.
        """
        if isinstance(amount_paisa, bool) or not isinstance(amount_paisa, int):
            raise errors.ValidationError(
                "amount_paisa must be integer paisa.",
                key="err.expense_amount_paisa",
            )
        if amount_paisa <= 0:
            raise errors.ValidationError(
                "Expense amount must be positive.",
                key="err.expense_amount_positive",
            )
        if method not in _METHODS:
            raise errors.ValidationError(
                f"Unknown payment method: {method!r} (cash|bank|other).",
                key="err.unknown_payment_method",
                params={"method": method},
            )
        stamp = self._require_stamp_on_day(occurred_at_utc, business_date)
        who = self.resolve_actor(actor)
        clean_payee = (payee or "").strip() or None
        clean_reference = (reference or "").strip() or None
        with self.transaction() as conn:
            require_writable_day(conn, business_date)
            cat = self._resolve_category(conn, category)
            expense_uuid = _uuid.uuid4().hex
            self._expenses.insert_expense(
                conn,
                expense_uuid,
                business_date,
                int(cat["id"]),
                amount_paisa,
                method,
                clean_payee,
                clean_reference,
                None,
                stamp,
                who,
            )
            expense_id = int(
                conn.execute(
                    "SELECT id FROM expenses WHERE uuid = ?", (expense_uuid,)
                ).fetchone()["id"]
            )
            if method == "cash":
                cash_cat = self._cash.get_category(conn, "payment", "expense")
                if cash_cat is None or not cash_cat["is_active"]:
                    raise errors.BusinessRuleError(
                        "Cash category 'expense' is missing or inactive;"
                        " cannot post the derived cash payment.",
                        key="err.derived_cash_category_missing",
                        params={"category": "expense"},
                    )
                self._cash.insert_payment(
                    conn,
                    _uuid.uuid4().hex,
                    business_date,
                    int(cash_cat["id"]),
                    amount_paisa,
                    clean_payee,
                    "expenses",
                    expense_id,
                    clean_reference,
                    None,
                    stamp,
                    who,
                )
            row = self._expenses.get_expense_by_uuid(conn, expense_uuid)
            return _to_expense_info(row)

    def reverse_expense(
        self, expense_uuid: str, *, reason: str, actor: str | None = None
    ) -> ExpenseInfo:
        """Reverse an expense: mirror row + flip + derived cash reversal.

        All three steps commit in ONE transaction. A non-empty reason is
        required. Returns the reversal row's info.
        """
        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise errors.ValidationError(
                "A reason is required to reverse an expense.",
                key="err.reverse_expense_needs_reason",
            )
        who = self.resolve_actor(actor)
        stamp = timecalc.utc_now_iso()
        with self.transaction() as conn:
            row = self._expenses.get_expense_by_uuid(conn, expense_uuid)
            if row is None:
                raise errors.NotFoundError(
                    f"Unknown expense: {expense_uuid!r}.",
                    key="err.unknown_expense",
                    params={"ref": expense_uuid},
                )
            if row["reverses_uuid"] is not None:
                raise errors.BusinessRuleError(
                    "A reversal expense cannot itself be reversed.",
                    key="err.reversal_expense_not_reversible",
                )
            if row["status"] != "posted" or row["reversed_by_uuid"] is not None:
                raise errors.BusinessRuleError(
                    "Only a posted expense can be reversed.",
                    key="err.only_posted_expense_reversible",
                )
            require_writable_day(conn, row["business_date"])
            reversal_uuid = _uuid.uuid4().hex
            reference = row["reference"]
            note = (
                f"{reference} — reversal: {clean_reason}"
                if reference
                else f"Reversal: {clean_reason}"
            )
            self._expenses.insert_expense(
                conn,
                reversal_uuid,
                row["business_date"],
                int(row["category_id"]),
                int(row["amount_paisa"]),
                row["method"],
                row["payee"],
                note,
                row["uuid"],
                stamp,
                who,
            )
            self._expenses.flip_expense_reversed(
                conn, int(row["id"]), reversal_uuid
            )
            if row["method"] == "cash":
                derived = self._cash.derived_payment(
                    conn, "expenses", int(row["id"])
                )
                if derived is not None:
                    cash_reversal_uuid = _uuid.uuid4().hex
                    d_remarks = derived["remarks"]
                    d_note = (
                        f"{d_remarks} — reversal: source reversed"
                        if d_remarks
                        else "Reversal: source reversed"
                    )
                    self._cash.insert_payment(
                        conn,
                        cash_reversal_uuid,
                        derived["business_date"],
                        int(derived["category_id"]),
                        int(derived["amount_paisa"]),
                        derived["payee"],
                        derived["source_table"],
                        int(derived["source_id"]),
                        d_note,
                        derived["uuid"],
                        stamp,
                        who,
                    )
                    self._cash.flip_payment_reversed(
                        conn, int(derived["id"]), cash_reversal_uuid
                    )
            return _to_expense_info(
                self._expenses.get_expense_by_uuid(conn, reversal_uuid)
            )

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def list_expenses(
        self,
        *,
        business_date: str | None = None,
        category_id: int | None = None,
    ) -> list[ExpenseInfo]:
        """Expenses, optionally filtered by day and/or category."""
        if business_date is not None and not timecalc.is_valid_business_date(
            business_date
        ):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            rows = self._expenses.list_expenses(
                conn,
                business_date=business_date,
                category_id=category_id,
            )
        return [_to_expense_info(row) for row in rows]

    def expense_totals(self, business_date: str) -> dict:
        """Net expense totals per category for a day (posted net of reversals)."""
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            rows = self._expenses.effective_totals_by_category(conn, business_date)
        by_category = [
            {
                "category_en": row["category_en"],
                "total_paisa": int(row["total_paisa"] or 0),
            }
            for row in rows
        ]
        return {
            "business_date": business_date,
            "by_category": by_category,
            "total_paisa": sum(item["total_paisa"] for item in by_category),
        }
