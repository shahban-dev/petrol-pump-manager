"""Supplier payments against delivery invoices (Phase 5).

Accounting rule: a supplier's outstanding liability is *computed*
(Σ invoice totals − paid at delivery − Σ effective payments), never typed.
An unpaid invoice never reduces physical cash — only a ``method='cash'``
payment creates a derived ``cash_payments`` row (category
``'supplier_payment'``), in the same transaction as the payment. A
delivery-linked payment can never exceed that invoice's unpaid amount
(service pre-check for a clear message; the Group I trigger is the
backstop).
"""

from __future__ import annotations

import sqlite3
import uuid as _uuid

from app.calculations import time as timecalc
from app.models.supplier import SupplierBalance, SupplierPaymentInfo
from app.repositories.cashbook_repository import CashbookRepository
from app.repositories.delivery_repository import DeliveryRepository
from app.repositories.supplier_repository import SupplierRepository
from app.services import errors
from app.services._base import ServiceBase, require_writable_day

_METHODS = ("cash", "bank", "other")


def _to_payment_info(row) -> SupplierPaymentInfo:
    amount = int(row["amount_paisa"])
    if row["reverses_uuid"] is not None:
        effective = -amount
    elif row["status"] == "reversed":
        effective = 0
    else:
        effective = amount
    return SupplierPaymentInfo(
        id=int(row["id"]),
        uuid=row["uuid"],
        supplier_id=int(row["supplier_id"]),
        supplier_name=row["supplier_name"],
        delivery_id=int(row["delivery_id"])
        if row["delivery_id"] is not None
        else None,
        business_date=row["business_date"],
        amount_paisa=amount,
        effective_paisa=effective,
        method=row["method"],
        status=row["status"],
        occurred_at_utc=row["occurred_at_utc"],
        created_by=row["created_by"],
    )


def _to_balance(row) -> SupplierBalance:
    return SupplierBalance(
        supplier_id=int(row["supplier_id"]),
        supplier_name=row["supplier_name"],
        invoiced_paisa=int(row["invoiced_paisa"]),
        paid_at_delivery_paisa=int(row["paid_at_delivery_paisa"]),
        paid_paisa=int(row["paid_paisa"]),
        outstanding_paisa=int(row["outstanding_paisa"]),
    )


def _to_delivery_dict(row, outstanding_paisa: int) -> dict:
    return {
        "id": int(row["id"]),
        "uuid": row["uuid"],
        "supplier_id": int(row["supplier_id"]),
        "supplier_name": row["supplier_name"],
        "fuel_product_code": row["fuel_product_code"],
        "tank_code": row["tank_code"],
        "business_date": row["business_date"],
        "quantity_ml": int(row["quantity_ml"]),
        "unit_cost_10k": int(row["unit_cost_10k"]),
        "total_cost_paisa": int(row["total_cost_paisa"]),
        "other_cost_paisa": int(row["other_cost_paisa"]),
        "supplier_invoice_no": row["supplier_invoice_no"],
        "amount_paid_paisa": int(row["amount_paid_paisa"]),
        "payment_status": row["payment_status"],
        "outstanding_paisa": outstanding_paisa,
    }


class SupplierService(ServiceBase):
    """Records supplier payments and reports computed supplier balances."""

    def __init__(self, db_manager, actor: str | None = None) -> None:
        super().__init__(db_manager, actor)
        self._suppliers = SupplierRepository()
        self._deliveries = DeliveryRepository()
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

    def _resolve_supplier(self, conn: sqlite3.Connection, supplier: str | int):
        """Supplier row by id or exact name; must exist and be active."""
        if isinstance(supplier, bool):
            raise errors.ValidationError(
                f"Invalid supplier reference: {supplier!r}.",
                key="err.invalid_supplier_ref",
                params={"ref": supplier},
            )
        row = None
        if isinstance(supplier, int):
            row = self._deliveries.get_supplier(conn, supplier)
        elif isinstance(supplier, str):
            clean = supplier.strip()
            if not clean:
                raise errors.ValidationError(
                    "Supplier name must not be empty.",
                    key="err.supplier_name_empty",
                )
            row = self._deliveries.get_supplier_by_name(conn, clean)
        else:
            raise errors.ValidationError(
                f"Invalid supplier reference: {supplier!r}.",
                key="err.invalid_supplier_ref",
                params={"ref": supplier},
            )
        if row is None or not row["is_active"]:
            raise errors.ValidationError(
                f"Unknown or inactive supplier: {supplier!r}.",
                key="err.unknown_supplier2",
                params={"ref": supplier},
            )
        return row

    def _delivery_outstanding_conn(
        self, conn: sqlite3.Connection, delivery_id: int
    ) -> int:
        """Computed unpaid amount of one delivery invoice (≥ 0)."""
        delivery = self._deliveries.get_delivery(conn, delivery_id)
        if delivery is None:
            raise errors.NotFoundError(
                f"Unknown delivery: {delivery_id!r}.",
                key="err.unknown_delivery",
                params={"ref": delivery_id},
            )
        invoice = int(delivery["total_cost_paisa"]) + int(
            delivery["other_cost_paisa"] or 0
        )
        paid_at_delivery = int(delivery["amount_paid_paisa"] or 0)
        paid = self._suppliers.effective_payments_for_delivery(conn, delivery_id)
        return max(0, invoice - paid_at_delivery - paid)

    # ------------------------------------------------------------------
    # payments
    # ------------------------------------------------------------------
    def record_supplier_payment(
        self,
        *,
        supplier: str | int,
        business_date: str,
        amount_paisa: int,
        method: str = "cash",
        delivery_id: int | None = None,
        occurred_at_utc: str | None = None,
        actor: str | None = None,
    ) -> SupplierPaymentInfo:
        """Record a payment against a supplier (optionally one invoice).

        A delivery-linked payment may not exceed that invoice's computed
        outstanding amount. ``method='cash'`` additionally writes the
        derived ``cash_payments`` row (category ``'supplier_payment'``) in
        the SAME transaction.
        """
        if isinstance(amount_paisa, bool) or not isinstance(amount_paisa, int):
            raise errors.ValidationError(
                "amount_paisa must be integer paisa.",
                key="err.expense_amount_paisa",
            )
        if amount_paisa <= 0:
            raise errors.ValidationError(
                "Payment amount must be positive.",
                key="err.payment_amount_positive",
            )
        if method not in _METHODS:
            raise errors.ValidationError(
                f"Unknown payment method: {method!r} (cash|bank|other).",
                key="err.unknown_payment_method",
                params={"method": method},
            )
        stamp = self._require_stamp_on_day(occurred_at_utc, business_date)
        who = self.resolve_actor(actor)
        with self.transaction() as conn:
            require_writable_day(conn, business_date)
            supplier_row = self._resolve_supplier(conn, supplier)
            clean_delivery_id = None
            if delivery_id is not None:
                delivery = self._deliveries.get_delivery(conn, int(delivery_id))
                if delivery is None:
                    raise errors.NotFoundError(
                        f"Unknown delivery: {delivery_id!r}.",
                        key="err.unknown_delivery",
                        params={"ref": delivery_id},
                    )
                if int(delivery["supplier_id"]) != int(supplier_row["id"]):
                    raise errors.ValidationError(
                        "The payment references a delivery that belongs to a"
                        " different supplier.",
                        key="err.payment_delivery_supplier_mismatch",
                    )
                clean_delivery_id = int(delivery["id"])
                outstanding = self._delivery_outstanding_conn(
                    conn, clean_delivery_id
                )
                if amount_paisa > outstanding:
                    raise errors.BusinessRuleError(
                        "The payment exceeds the delivery's outstanding amount"
                        f" ({outstanding} paisa outstanding).",
                        key="err.payment_exceeds_outstanding",
                        params={"outstanding_paisa": outstanding},
                    )
            payment_uuid = _uuid.uuid4().hex
            self._suppliers.insert_payment(
                conn,
                payment_uuid,
                int(supplier_row["id"]),
                clean_delivery_id,
                business_date,
                amount_paisa,
                method,
                None,
                stamp,
                who,
            )
            payment_id = int(
                conn.execute(
                    "SELECT id FROM supplier_payments WHERE uuid = ?",
                    (payment_uuid,),
                ).fetchone()["id"]
            )
            if method == "cash":
                cash_cat = self._cash.get_category(
                    conn, "payment", "supplier_payment"
                )
                if cash_cat is None or not cash_cat["is_active"]:
                    raise errors.BusinessRuleError(
                        "Cash category 'supplier_payment' is missing or"
                        " inactive; cannot post the derived cash payment.",
                        key="err.derived_cash_category_missing",
                        params={"category": "supplier_payment"},
                    )
                self._cash.insert_payment(
                    conn,
                    _uuid.uuid4().hex,
                    business_date,
                    int(cash_cat["id"]),
                    amount_paisa,
                    supplier_row["name"],
                    "supplier_payments",
                    payment_id,
                    None,
                    None,
                    stamp,
                    who,
                )
            row = self._suppliers.get_payment_by_uuid(conn, payment_uuid)
            return _to_payment_info(row)

    def reverse_supplier_payment(
        self, payment_uuid: str, *, reason: str, actor: str | None = None
    ) -> SupplierPaymentInfo:
        """Reverse a supplier payment: mirror row + flip + cash reversal.

        All steps commit in ONE transaction. A non-empty reason is required.
        Returns the reversal row's info.
        """
        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise errors.ValidationError(
                "A reason is required to reverse a supplier payment.",
                key="err.reverse_supplier_payment_needs_reason",
            )
        who = self.resolve_actor(actor)
        stamp = timecalc.utc_now_iso()
        with self.transaction() as conn:
            row = self._suppliers.get_payment_by_uuid(conn, payment_uuid)
            if row is None:
                raise errors.NotFoundError(
                    f"Unknown supplier payment: {payment_uuid!r}.",
                    key="err.unknown_supplier_payment",
                    params={"ref": payment_uuid},
                )
            if row["reverses_uuid"] is not None:
                raise errors.BusinessRuleError(
                    "A reversal payment cannot itself be reversed.",
                    key="err.reversal_payment_not_reversible",
                )
            if row["status"] != "posted" or row["reversed_by_uuid"] is not None:
                raise errors.BusinessRuleError(
                    "Only a posted supplier payment can be reversed.",
                    key="err.only_posted_supplier_payment_reversible",
                )
            require_writable_day(conn, row["business_date"])
            reversal_uuid = _uuid.uuid4().hex
            self._suppliers.insert_payment(
                conn,
                reversal_uuid,
                int(row["supplier_id"]),
                int(row["delivery_id"])
                if row["delivery_id"] is not None
                else None,
                row["business_date"],
                int(row["amount_paisa"]),
                row["method"],
                row["uuid"],
                stamp,
                who,
            )
            self._suppliers.flip_payment_reversed(
                conn, int(row["id"]), reversal_uuid
            )
            if row["method"] == "cash":
                derived = self._cash.derived_payment(
                    conn, "supplier_payments", int(row["id"])
                )
                if derived is not None:
                    cash_reversal_uuid = _uuid.uuid4().hex
                    self._cash.insert_payment(
                        conn,
                        cash_reversal_uuid,
                        derived["business_date"],
                        int(derived["category_id"]),
                        int(derived["amount_paisa"]),
                        derived["payee"],
                        derived["source_table"],
                        int(derived["source_id"]),
                        "Reversal: source reversed",
                        derived["uuid"],
                        stamp,
                        who,
                    )
                    self._cash.flip_payment_reversed(
                        conn, int(derived["id"]), cash_reversal_uuid
                    )
            return _to_payment_info(
                self._suppliers.get_payment_by_uuid(conn, reversal_uuid)
            )

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def supplier_balances(self) -> list[SupplierBalance]:
        """Every supplier's computed balance, ordered by name."""
        with self.read_connection() as conn:
            rows = self._suppliers.supplier_balances(conn)
        return [_to_balance(row) for row in rows]

    def delivery_outstanding(self, delivery_id: int) -> int:
        """One delivery invoice's computed unpaid amount (≥ 0)."""
        with self.read_connection() as conn:
            return self._delivery_outstanding_conn(conn, int(delivery_id))

    def list_payments(
        self,
        *,
        supplier_id: int | None = None,
        business_date: str | None = None,
    ) -> list[SupplierPaymentInfo]:
        """Supplier payments, optionally filtered."""
        if business_date is not None and not timecalc.is_valid_business_date(
            business_date
        ):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            rows = self._suppliers.list_payments(
                conn, supplier_id=supplier_id, business_date=business_date
            )
        return [_to_payment_info(row) for row in rows]

    def deliveries_with_outstanding(
        self, supplier_id: int | None = None
    ) -> list[dict]:
        """Deliveries with their computed outstanding amounts, oldest first."""
        with self.read_connection() as conn:
            if supplier_id is not None:
                supplier = self._deliveries.get_supplier(conn, int(supplier_id))
                if supplier is None:
                    raise errors.NotFoundError(
                        f"Unknown supplier: {supplier_id!r}.",
                        key="err.unknown_supplier",
                        params={"ref": supplier_id},
                    )
            rows = self._suppliers.deliveries_with_outstanding(
                conn, supplier_id=supplier_id
            )
        return [
            _to_delivery_dict(row, max(0, int(row["outstanding_paisa"] or 0)))
            for row in rows
        ]
