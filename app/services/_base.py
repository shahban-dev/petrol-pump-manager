"""Shared service plumbing: transactions, actor resolution, error mapping.

Every mutating service method follows the same shape:

    with self.transaction() as conn:
        ... validate (raise BusinessRuleError on failure) ...
        ... repository writes ...
    # commit happens on clean exit; any exception rolls back.

Design plan §1: services own the transaction boundary (BEGIN … COMMIT /
ROLLBACK). The database triggers remain the backstop; service checks exist
to produce clear, actionable messages before the trigger would abort.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from app.calculations import time as timecalc
from app.database.connection import DatabaseManager
from app.repositories.business_day_repository import BusinessDayRepository
from app.services import errors


class ServiceBase:
    """Holds the DatabaseManager and resolves the acting operator."""

    def __init__(
        self, db_manager: DatabaseManager, actor: str | None = None
    ) -> None:
        self._db = db_manager
        self._actor = actor
        self._days = BusinessDayRepository()

    # ------------------------------------------------------------------
    # transactions
    # ------------------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Open a connection, BEGIN IMMEDIATE, commit on success.

        Any exception rolls the whole unit of work back and is re-raised;
        ``sqlite3.IntegrityError`` is first translated to the matching
        :mod:`app.services.errors` subclass so callers never see raw DB
        errors.
        """
        conn = self._db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            try:
                conn.commit()
            except Exception:
                # A failed COMMIT (e.g. deferred constraint) must not leave
                # a half-open transaction behind.
                conn.rollback()
                raise
        except errors.BusinessRuleError:
            raise
        except sqlite3.IntegrityError as exc:
            raise _translate_integrity_error(exc) from exc
        finally:
            conn.close()

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        """A plain read connection (no transaction needed)."""
        conn = self._db.connect()
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # actor
    # ------------------------------------------------------------------
    def resolve_actor(self, actor: str | None) -> str:
        """Explicit actor, else the configured operator name, else 'system'.

        Never invents a username: when nothing is configured the audit log
        records the explicit value ``'system'`` (design plan §11.10).
        """
        if actor and actor.strip():
            return actor.strip()
        if self._actor and self._actor.strip():
            return self._actor.strip()
        try:
            with self.read_connection() as conn:
                configured = self._days.get_setting(conn, "operator_name")
        except Exception:
            configured = None
        if configured and configured.strip():
            return configured.strip()
        return "system"


def _translate_integrity_error(exc: sqlite3.IntegrityError) -> errors.BusinessRuleError:
    """Map a trigger/constraint rejection to a meaningful service error."""
    message = str(exc)
    lowered = message.lower()
    if "business day is closed" in lowered:
        return errors.ClosedDayError(
            "The business day is closed. Reopen it first to make corrections.",
            key="err.trg_day_closed",
        )
    if "non-operating" in lowered:
        return errors.ClosedDayError(
            "The business day is marked non-operating; no readings or sales"
            " may be posted to it.",
            key="err.trg_day_non_operating",
        )
    if "only one business day may be open" in lowered:
        return errors.BusinessRuleError(
            "Another business day is already open. Close it before opening"
            " a new one.",
            key="err.trg_one_open_day",
        )
    if "illegal business day status transition" in lowered:
        return errors.BusinessRuleError(
            "That day-status change is not allowed.",
            key="err.trg_day_status_transition",
        )
    if "lower than the previous" in lowered:
        return errors.BusinessRuleError(
            "The reading is lower than the previous closing reading. Record a"
            " meter event (reset / rollover / replacement / correction) first;"
            " a reset is never treated as a negative sale.",
            key="err.trg_reading_lower",
        )
    if "unknown or inactive" in lowered:
        return errors.BusinessRuleError(
            "The nozzle is unknown or inactive.", key="err.trg_nozzle_unknown"
        )
    # --- Phase-3 trigger messages (exact strings from 007_phase3.sql) ---
    if "sale would drive tracked stock negative" in lowered:
        return errors.BusinessRuleError(
            "The sale would drive tracked stock negative. Record the missing"
            " delivery or an authorized stock adjustment first.",
            key="err.trg_negative_stock",
        )
    if "delivery total does not match" in lowered:
        return errors.BusinessRuleError(
            "The delivery total does not match quantity × unit cost"
            " (the database rejected the row).",
            key="err.trg_delivery_total",
        )
    if "amount paid exceeds" in lowered:
        return errors.ValidationError(
            "The amount paid exceeds the delivery total (fuel cost + other"
            " costs).",
            key="err.trg_amount_paid_exceeds",
        )
    if "fuel_deliveries.supplier_id, fuel_deliveries.supplier_invoice_no" in lowered:
        return errors.DuplicateError(
            "This supplier invoice is already recorded for this supplier.",
            key="err.trg_duplicate_invoice",
        )
    if "inventory_movements.source_uuid" in lowered:
        return errors.DuplicateError(
            "This transaction was already posted to stock.",
            key="err.trg_duplicate_posting",
        )
    if "price_history.fuel_product_id, price_history.effective_from_utc" in lowered:
        return errors.DuplicateError(
            "A price already exists for this product at that time.",
            key="err.trg_duplicate_price",
        )
    # --- Phase-4 trigger messages (exact strings from 008_phase4.sql) ---
    if "credit ticket customer is unknown or inactive" in lowered:
        return errors.BusinessRuleError(
            "The credit ticket's customer is unknown or inactive.",
            key="err.trg_ticket_customer",
        )
    if "payment customer is unknown" in lowered:
        return errors.ValidationError(
            "The payment's customer is unknown.", key="err.trg_payment_customer"
        )
    if "credit ticket line amount does not match quantity" in lowered:
        return errors.ValidationError(
            "A ticket line amount does not match quantity × unit price.",
            key="err.trg_line_amount",
        )
    if "nozzle fuel product does not match the line fuel product" in lowered:
        return errors.ValidationError(
            "The nozzle dispenses a different fuel product than the"
            " ticket line.",
            key="err.trg_nozzle_product",
        )
    if "cannot add lines to a reversed credit ticket" in lowered:
        return errors.BusinessRuleError(
            "Lines cannot be added to a reversed credit ticket.",
            key="err.trg_lines_on_reversed",
        )
    if "credit ticket lines exceed the ticket gross amount" in lowered:
        return errors.ValidationError(
            "The ticket's lines exceed the ticket gross amount.",
            key="err.trg_lines_exceed_gross",
        )
    if "reversal ticket must reference a posted original ticket" in lowered:
        return errors.BusinessRuleError(
            "A reversal ticket must reference a posted original ticket.",
            key="err.trg_reversal_needs_posted",
        )
    if "reversal ticket must match the original ticket" in lowered:
        return errors.BusinessRuleError(
            "A reversal ticket must match the original ticket (same"
            " customer, same gross, no paid-now).",
            key="err.trg_reversal_must_match",
        )
    if "reversal payment must reference a posted original payment" in lowered:
        return errors.BusinessRuleError(
            "A reversal payment must reference a posted original payment.",
            key="err.trg_reversal_payment_posted",
        )
    if "reversal payment must match the original payment" in lowered:
        return errors.BusinessRuleError(
            "A reversal payment must match the original payment (same"
            " customer, amount and method).",
            key="err.trg_reversal_payment_match",
        )
    if "allocation reversal references a missing allocation" in lowered:
        return errors.BusinessRuleError(
            "The allocation reversal references a missing allocation.",
            key="err.trg_alloc_rev_missing",
        )
    if "allocation reversal must reference the same payment and ticket" in lowered:
        return errors.BusinessRuleError(
            "An allocation reversal must reference the same payment and"
            " ticket as the allocation.",
            key="err.trg_alloc_rev_match",
        )
    if "allocation reversal exceeds the remaining allocated amount" in lowered:
        return errors.BusinessRuleError(
            "The allocation reversal exceeds the allocation's remaining"
            " (unreversed) amount.",
            key="err.trg_alloc_rev_exceeds",
        )
    if "cannot allocate a reversed or missing payment" in lowered:
        return errors.BusinessRuleError(
            "Cannot allocate a reversed or missing payment.",
            key="err.trg_alloc_payment",
        )
    if "cannot allocate to a reversed or missing credit ticket" in lowered:
        return errors.BusinessRuleError(
            "Cannot allocate to a reversed or missing credit ticket.",
            key="err.trg_alloc_ticket",
        )
    if "payment and ticket belong to different customers" in lowered:
        return errors.ValidationError(
            "The payment and the ticket belong to different customers.",
            key="err.payment_ticket_customer_mismatch",
        )
    if "allocation exceeds the ticket's unpaid balance" in lowered:
        return errors.BusinessRuleError(
            "The allocation exceeds the ticket's unpaid balance.",
            key="err.trg_alloc_exceeds_balance",
        )
    if "total allocations exceed the payment amount" in lowered:
        return errors.BusinessRuleError(
            "Total allocations exceed the payment amount.",
            key="err.trg_alloc_exceeds_payment",
        )
    if "credit_sales is immutable" in lowered:
        return errors.BusinessRuleError(
            "Credit tickets are immutable; reverse the ticket with a"
            " reversal row instead of editing it.",
            key="err.trg_immutable_ticket",
        )
    if "customer_payments is immutable" in lowered:
        return errors.BusinessRuleError(
            "Customer payments are immutable; reverse the payment with a"
            " reversal row instead of editing it.",
            key="err.trg_immutable_payment",
        )
    if "payment_allocations is immutable" in lowered:
        return errors.BusinessRuleError(
            "Payment allocations are immutable; change them through the"
            " audited re-allocation workflow instead.",
            key="err.trg_immutable_allocation",
        )
    if "payment_allocation_reversals is immutable" in lowered:
        return errors.BusinessRuleError(
            "Allocation reversals are immutable; post a compensating row"
            " instead.",
            key="err.trg_immutable_alloc_reversal",
        )
    if "customer_opening_adjustments is immutable" in lowered:
        return errors.BusinessRuleError(
            "Opening adjustments are immutable; post a compensating"
            " adjustment instead.",
            key="err.trg_immutable_opening",
        )
    if "customers.customer_code" in lowered:
        return errors.DuplicateError(
            "A customer with this code already exists.",
            key="err.trg_duplicate_customer",
        )
    if "credit_sales.receipt_no" in lowered:
        return errors.DuplicateError(
            "A credit ticket with this receipt number already exists.",
            key="err.trg_duplicate_ticket_receipt",
        )
    if "customer_payments.receipt_no" in lowered:
        return errors.DuplicateError(
            "A payment with this receipt number already exists.",
            key="err.trg_duplicate_payment_receipt",
        )
    if (
        "credit_sales.uuid" in lowered
        or "customer_payments.uuid" in lowered
        or "payment_allocation_reversals.uuid" in lowered
        or "customer_opening_adjustments.uuid" in lowered
    ):
        return errors.DuplicateError(
            "This record was already saved (duplicate identifier).",
            key="err.trg_duplicate_uuid",
        )
    if (
        "payment_allocations.payment_id, payment_allocations.credit_sale_id"
        in lowered
    ):
        return errors.DuplicateError(
            "This payment is already allocated to that ticket; use"
            " re-allocation to change it.",
            key="err.trg_duplicate_allocation",
        )
    # --- Phase-5 trigger messages (exact strings from 010_phase5.sql) ---
    if "sales_settlements is immutable" in lowered:
        return errors.BusinessRuleError(
            "Sales settlements are immutable; reverse the batch with a"
            " reversal batch instead of editing it.",
            key="err.trg_immutable_settlement",
        )
    if "cash_receipts is immutable" in lowered:
        return errors.BusinessRuleError(
            "Cash receipts are immutable; reverse the receipt with a"
            " reversal row instead of editing it.",
            key="err.trg_immutable_cash_receipt",
        )
    if "cash_payments is immutable" in lowered:
        return errors.BusinessRuleError(
            "Cash payments are immutable; reverse the payment with a"
            " reversal row instead of editing it.",
            key="err.trg_immutable_cash_payment",
        )
    if "expenses is immutable" in lowered:
        return errors.BusinessRuleError(
            "Expenses are immutable; reverse the expense with a reversal"
            " row instead of editing it.",
            key="err.trg_immutable_expense",
        )
    if "supplier_payments is immutable" in lowered:
        return errors.BusinessRuleError(
            "Supplier payments are immutable; reverse the payment with a"
            " reversal row instead of editing it.",
            key="err.trg_immutable_supplier_payment",
        )
    if "other_product_transactions is immutable" in lowered:
        return errors.BusinessRuleError(
            "Other-product transactions are immutable; post an adjustment"
            " transaction instead.",
            key="err.trg_immutable_other_txn",
        )
    if "supplier is unknown or inactive" in lowered:
        return errors.ValidationError(
            "The supplier is unknown or inactive.", key="err.trg_supplier_unknown"
        )
    if "references a delivery of a different supplier" in lowered:
        return errors.ValidationError(
            "The payment references a delivery that belongs to a different"
            " supplier.",
            key="err.payment_delivery_supplier_mismatch",
        )
    if "exceeds the delivery outstanding amount" in lowered:
        return errors.BusinessRuleError(
            "The payment exceeds the delivery's outstanding amount.",
            key="err.trg_payment_exceeds_outstanding",
        )
    if "reversal references a missing or already-reversed" in lowered:
        return errors.BusinessRuleError(
            "The reversal references a missing or already-reversed record.",
            key="err.trg_reversal_missing",
        )
    if "reversal must match the original" in lowered:
        return errors.BusinessRuleError(
            "A reversal must match the original record (same amounts,"
            " category, method and business date).",
            key="err.trg_reversal_match",
        )
    if (
        "uq_cash_receipt_source" in lowered
        or "uq_cash_payment_source" in lowered
    ):
        return errors.DuplicateError(
            "This transaction already has its cash-book row; it was not"
            " posted twice.",
            key="err.trg_duplicate_cash_row",
        )
    if (
        "uq_settlement_ticket_leg" in lowered
        or "uq_settlement_manager_leg" in lowered
    ):
        return errors.DuplicateError(
            "Settlements for this day are already recorded. Reverse them"
            " first to record a different split.",
            key="err.trg_duplicate_settlement",
        )
    if "unique" in lowered:
        return errors.DuplicateError(
            "A record for this day already exists.",
            key="err.trg_duplicate_day",
        )
    return errors.BusinessRuleError(
        f"The database rejected the write: {message}",
        key="err.trg_db_rejected",
        params={"detail": message},
    )


def require_writable_day(
    conn: sqlite3.Connection, business_date: str
) -> sqlite3.Row:
    """Return the ``business_days`` row when the day exists and accepts writes.

    A shared guard for the Phase-3 services (prices, deliveries, inventory):
    every write lands on an open/reopened day inside the caller's
    transaction, so the check and the write commit atomically.

    Raises:
        ValidationError: malformed business date.
        NotFoundError: no such day — open it first.
        ClosedDayError: the day is closed or non-operating.
    """
    if not timecalc.is_valid_business_date(business_date):
        raise errors.ValidationError(
            f"Invalid business date: {business_date!r}.",
            key="err.invalid_business_date",
            params={"day_date": business_date},
        )
    row = conn.execute(
        "SELECT * FROM business_days WHERE business_date = ?",
        (business_date,),
    ).fetchone()
    if row is None:
        raise errors.NotFoundError(
            f"No business day {business_date}. Open the day first.",
            key="err.no_business_day",
            params={"day_date": business_date},
        )
    status = row["status"]
    if status == "closed":
        raise errors.ClosedDayError(
            f"Business day {business_date} is closed. Reopen it first to"
            " make corrections.",
            key="err.day_closed",
            params={"day_date": business_date},
        )
    if status == "non_operating":
        raise errors.ClosedDayError(
            f"Business day {business_date} is marked non-operating; nothing"
            " may be posted to it.",
            key="err.day_non_operating",
            params={"day_date": business_date},
        )
    return row
