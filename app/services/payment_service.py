"""Customer payments and payment allocations (Phase 4).

A payment is one posted fact (money received). An allocation links a payment
to one or more credit tickets; the default order is FIFO (oldest unpaid
ticket first), configurable via ``app_settings.allocation_order``
(``fifo`` | ``manual``). The database's Group G trigger is the backstop:
over-allocating aborts the whole operation atomically (acceptance
Test 12).

Money taken *at the moment of a credit sale* (``paid_now_paisa``) always
stays on that ticket — it is never treated as a payment row and never
settles older debt (§11.4). Recovering old debt is a payment row plus
allocations: a receipt, never revenue (§6).

The cash book (Phase 5) is NOT touched here.
"""

from __future__ import annotations

import sqlite3
import uuid as _uuid

from app.calculations import time as timecalc
from app.models import credit as dto
from app.models.credit import PaymentInfo
from app.repositories.customer_repository import CustomerRepository
from app.services import errors
from app.services._base import ServiceBase, require_writable_day

_PAYMENT_METHODS = ("cash", "bank", "other")
_ALLOCATION_ORDERS = ("fifo", "manual")
_ALLOCATION_ORDER_KEY = "allocation_order"


class PaymentService(ServiceBase):
    """Records customer payments and allocates them to credit tickets."""

    def __init__(self, db_manager, actor: str | None = None) -> None:
        super().__init__(db_manager, actor)
        self._payments = CustomerRepository()

    # ------------------------------------------------------------------
    # allocation order setting
    # ------------------------------------------------------------------
    def allocation_order(self) -> str:
        """The configured allocation order; a missing setting means
        ``'fifo'`` (the §11.4 default)."""
        with self.read_connection() as conn:
            value = self._days.get_setting(conn, _ALLOCATION_ORDER_KEY)
        if value not in _ALLOCATION_ORDERS:
            return "fifo"
        return value

    def set_allocation_order(
        self, order: str, *, actor: str | None = None  # noqa: ARG002
    ) -> str:
        """Set the allocation order (``'fifo'`` or ``'manual'``).

        A global app setting — not day-scoped. Anything else raises
        ``ValidationError``.
        """
        clean = (order or "").strip().lower()
        if clean not in _ALLOCATION_ORDERS:
            raise errors.ValidationError(
                f"Invalid allocation order: {order!r}."
                f" Use one of: {', '.join(_ALLOCATION_ORDERS)}.",
                key="err.invalid_allocation_order",
                params={
                    "order": order,
                    "choices": ", ".join(_ALLOCATION_ORDERS),
                },
            )
        with self.transaction() as conn:
            self._days.set_setting(conn, _ALLOCATION_ORDER_KEY, clean)
        return clean

    # ------------------------------------------------------------------
    # recording payments
    # ------------------------------------------------------------------
    def record_payment(
        self,
        customer: str | int,
        business_date: str,
        amount_paisa: int,
        method: str = "cash",
        *,
        remarks: str | None = None,
        receipt_no: str | None = None,
        occurred_at_utc: str | None = None,
        auto_allocate: bool = True,
        actor: str | None = None,
    ) -> PaymentInfo:
        """Record money received from a customer.

        * ``amount_paisa``: positive integer paisa (never float);
        * ``method``: ``cash`` | ``bank`` | ``other``;
        * ``receipt_no``: auto-generated ``'R-<YYYYMMDD>-<seq>'`` when not
          given (``seq`` from the day's payment count);
        * with ``auto_allocate`` and ``allocation_order == 'fifo'``, the
          payment is allocated to the oldest unpaid tickets in the SAME
          transaction.

        Returns the payment with its allocations. Creates no cash-book or
        revenue rows (Phase 5 owns the cash book).
        """
        if isinstance(amount_paisa, bool) or not isinstance(amount_paisa, int):
            raise errors.ValidationError(
                "Payment amount must be an integer number of paisa.",
                key="err.payment_amount_integer",
            )
        if amount_paisa <= 0:
            raise errors.ValidationError(
                "Payment amount must be greater than zero.",
                key="err.payment_amount_greater_than_zero",
            )
        if method not in _PAYMENT_METHODS:
            raise errors.ValidationError(
                f"Invalid payment method: {method!r}."
                f" Use one of: {', '.join(_PAYMENT_METHODS)}.",
                key="err.invalid_payment_method",
                params={
                    "method": method,
                    "choices": ", ".join(_PAYMENT_METHODS),
                },
            )
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        stamp = self._require_stamp_on_day(occurred_at_utc, business_date)
        receipt = (receipt_no or "").strip() or None
        who = self.resolve_actor(actor)
        payment_uuid = _uuid.uuid4().hex

        with self.transaction() as conn:
            cust = self._resolve_customer(conn, customer)
            require_writable_day(conn, business_date)
            if receipt is None:
                seq = self._payments.count_payments_by_day(conn, business_date)
                receipt = f"R-{business_date.replace('-', '')}-{seq + 1:03d}"
            payment_id = self._payments.insert_payment(
                conn,
                payment_uuid,
                int(cust["id"]),
                business_date,
                amount_paisa,
                method,
                receipt,
                None,  # reverses_uuid
                stamp,
                (remarks or "").strip() or None,
                who,
            )
            if auto_allocate and self._allocation_order_conn(conn) == "fifo":
                splits = self._fifo_splits_conn(
                    conn, int(cust["id"]), payment_id, amount_paisa
                )
                for ticket_id, split_amount in splits:
                    self._payments.insert_allocation(
                        conn,
                        payment_id,
                        ticket_id,
                        split_amount,
                        business_date,
                        stamp,
                        who,
                    )
            return self._payment_details_conn(conn, payment_id)

    def allocate(
        self,
        payment: str | int,
        splits: list[dict] | None = None,
        *,
        business_date: str | None = None,
        occurred_at_utc: str | None = None,
        actor: str | None = None,
    ) -> PaymentInfo:
        """Allocate a payment to tickets.

        * ``splits=None`` → FIFO over the customer's oldest unpaid tickets;
        * otherwise ``splits = [{ticket_uuid, amount_paisa}, …]``.

        Every split is validated (positive integer paisa, posted
        non-reversal ticket of the same customer, no duplicate ticket) and
        all rows are inserted in ONE transaction — over-allocating the
        payment aborts atomically (the Group G trigger is the backstop).
        ``business_date`` defaults to the payment's day. Returns the
        payment with its allocations.
        """
        who = self.resolve_actor(actor)
        with self.transaction() as conn:
            pay = self._resolve_payment(conn, payment)
            if pay["status"] != "posted" or pay["reverses_uuid"] is not None:
                raise errors.BusinessRuleError(
                    "Only a posted payment can be allocated.",
                    key="err.only_posted_payment_allocatable",
                )
            payment_id = int(pay["id"])
            day = business_date or pay["business_date"]
            if not timecalc.is_valid_business_date(day):
                raise errors.ValidationError(
                    f"Invalid business date: {day!r}.",
                    key="err.invalid_business_date",
                    params={"day_date": day},
                )
            require_writable_day(conn, day)
            stamp = self._require_stamp_on_day(occurred_at_utc, day)
            plan = self._resolve_splits(
                conn, payment_id, int(pay["customer_id"]), splits
            )
            unallocated = self._payments.payment_unallocated(conn, payment_id)
            if unallocated is None:  # pragma: no cover — row was just read
                raise errors.NotFoundError(
                    f"Unknown payment: {payment!r}.",
                    key="err.unknown_payment",
                    params={"ref": payment},
                )
            total = sum(amount for _, amount in plan)
            if total > unallocated:
                raise errors.ValidationError(
                    f"Allocations total {total} paisa but only {unallocated}"
                    " paisa of the payment is unallocated.",
                    key="err.allocations_exceed_unallocated",
                    params={
                        "total_paisa": total,
                        "unallocated_paisa": unallocated,
                    },
                )
            for ticket_id, amount in plan:
                self._payments.insert_allocation(
                    conn, payment_id, ticket_id, amount, day, stamp, who
                )
            return self._payment_details_conn(conn, payment_id)

    def reallocate(
        self,
        allocation_id: int,
        new_splits: list[dict],
        reason: str,
        business_date: str,
        *,
        actor: str | None = None,
    ) -> PaymentInfo:
        """Re-allocate: fully reverse one allocation (audited, *reason*
        required) and apply new splits for the same payment — one
        transaction.

        Returns the payment with its allocations.
        """
        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise errors.ValidationError(
                "A reason is required to re-allocate a payment.",
                key="err.reallocate_needs_reason",
            )
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        who = self.resolve_actor(actor)
        with self.transaction() as conn:
            alloc = self._payments.get_allocation(conn, allocation_id)
            if alloc is None:
                raise errors.NotFoundError(
                    f"Unknown allocation: {allocation_id!r}.",
                    key="err.unknown_allocation",
                    params={"ref": allocation_id},
                )
            payment_id = int(alloc["payment_id"])
            pay = self._payments.get_payment(conn, payment_id)
            if (
                pay is None
                or pay["status"] != "posted"
                or pay["reverses_uuid"] is not None
            ):
                raise errors.BusinessRuleError(
                    "Only allocations of a posted payment can be re-allocated.",
                    key="err.only_posted_payment_reallocatable",
                )
            require_writable_day(conn, business_date)
            remaining = int(alloc["effective_paisa"])
            if remaining <= 0:
                raise errors.BusinessRuleError(
                    "This allocation is already fully reversed.",
                    key="err.allocation_fully_reversed",
                )
            self._payments.insert_reversal(
                conn,
                _uuid.uuid4().hex,
                allocation_id,
                payment_id,
                int(alloc["credit_sale_id"]),
                remaining,
                business_date,
                clean_reason,
                None,
                who,
            )
            plan = self._resolve_splits(
                conn, payment_id, int(pay["customer_id"]), new_splits
            )
            unallocated = self._payments.payment_unallocated(conn, payment_id)
            total = sum(amount for _, amount in plan)
            if total > (unallocated or 0):
                raise errors.ValidationError(
                    f"Allocations total {total} paisa but only {unallocated}"
                    " paisa of the payment is unallocated.",
                    key="err.allocations_exceed_unallocated",
                    params={
                        "total_paisa": total,
                        "unallocated_paisa": unallocated,
                    },
                )
            for ticket_id, amount in plan:
                self._payments.insert_allocation(
                    conn, payment_id, ticket_id, amount, business_date, None,
                    who,
                )
            return self._payment_details_conn(conn, payment_id)

    def reverse_payment(
        self,
        payment_uuid: str,
        reason: str,
        business_date: str,
        *,
        actor: str | None = None,
    ) -> PaymentInfo:
        """Reverse a payment: its allocations are reversed first (audited),
        then a reversal payment row is posted on *business_date* and the
        original flips ``posted → reversed`` — one transaction.

        Both *business_date* and the original payment's day must be
        writable (``ClosedDayError`` otherwise, §11.13). Returns the
        reversal payment.
        """
        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise errors.ValidationError(
                "A reason is required to reverse a payment.",
                key="err.reverse_payment_needs_reason2",
            )
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        who = self.resolve_actor(actor)
        with self.transaction() as conn:
            original = self._payments.get_payment_by_uuid(conn, payment_uuid)
            if original is None:
                raise errors.NotFoundError(
                    f"Unknown payment: {payment_uuid!r}.",
                    key="err.unknown_payment",
                    params={"ref": payment_uuid},
                )
            if (
                original["status"] != "posted"
                or original["reverses_uuid"] is not None
            ):
                raise errors.BusinessRuleError(
                    "Only a posted payment can be reversed.",
                    key="err.only_posted_payment_reversible",
                )
            original_id = int(original["id"])
            require_writable_day(conn, business_date)
            self._require_original_day_writable(conn, original["business_date"])
            self._reverse_allocations_for_payment(
                conn, original_id, clean_reason, business_date, who
            )
            reversal_uuid = _uuid.uuid4().hex
            seq = self._payments.count_payments_by_day(conn, business_date)
            receipt = f"R-{business_date.replace('-', '')}-{seq + 1:03d}"
            reversal_id = self._payments.insert_payment(
                conn,
                reversal_uuid,
                int(original["customer_id"]),
                business_date,
                int(original["amount_paisa"]),
                original["method"],
                receipt,
                payment_uuid,
                None,
                original["remarks"],
                who,
            )
            self._payments.mark_payment_reversed(conn, original_id, reversal_uuid)
            return self._payment_details_conn(conn, reversal_id)

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def payment_details(self, payment: str | int) -> PaymentInfo:
        """A payment with its allocations and unallocated remainder."""
        with self.read_connection() as conn:
            pay = self._resolve_payment(conn, payment)
            return self._payment_details_conn(conn, int(pay["id"]))

    def day_payments(self, business_date: str) -> list[PaymentInfo]:
        """The day's payments (including reversal rows), id order."""
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            rows = self._payments.list_payments_by_day(conn, business_date)
            return [
                self._payment_details_conn(conn, int(row["id"])) for row in rows
            ]

    def customer_payments(self, customer: str | int) -> list[PaymentInfo]:
        """A customer's payments, id order."""
        with self.read_connection() as conn:
            cust = self._resolve_customer(conn, customer)
            rows = self._payments.list_payments_by_customer(
                conn, int(cust["id"])
            )
            return [
                self._payment_details_conn(conn, int(row["id"])) for row in rows
            ]

    def ticket_unpaid(self, ticket: str | int) -> int:
        """A ticket's effective unpaid balance (gross − paid_now −
        effective allocations); 0 for reversed tickets and reversal rows."""
        with self.read_connection() as conn:
            ticket_id = self._resolve_ticket_id(conn, ticket)
            unpaid = self._payments.ticket_unpaid(conn, ticket_id)
        return unpaid if unpaid is not None else 0

    def allocation_integrity_rows(self, business_date: str) -> list[dict]:
        """Allocation rows backing the close checklist's
        ``allocations_valid`` check: every allocation of the day's payments
        with the payment and ticket facts re-verified."""
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            rows = self._payments.allocation_integrity_rows(
                conn, business_date
            )
        return [dict(row) for row in rows]

    def receipt_data(self, payment: str | int) -> dict:
        """Printable receipt data: payment, customer, allocations with
        ticket references, the unallocated remainder, and the generation
        timestamp."""
        with self.read_connection() as conn:
            pay = self._resolve_payment(conn, payment)
            details = self._payment_details_conn(conn, int(pay["id"]))
            cust = self._payments.get_customer(conn, int(pay["customer_id"]))
        return {
            "payment": {
                "uuid": details.uuid,
                "receipt_no": details.receipt_no,
                "business_date": details.business_date,
                "amount_paisa": details.amount_paisa,
                "method": details.method,
                "status": details.status,
                "occurred_at_utc": details.occurred_at_utc,
                "remarks": details.remarks,
            },
            "customer": {
                "customer_code": cust["customer_code"],
                "name": cust["name"],
                "business_name": cust["business_name"],
                "phone": cust["phone"],
                "outstanding_paisa": int(cust["outstanding_paisa"]),
            },
            "allocations": [
                {
                    "ticket_uuid": alloc.ticket_uuid,
                    "ticket_receipt_no": alloc.ticket_receipt_no,
                    "amount_paisa": alloc.amount_paisa,
                    "effective_paisa": alloc.effective_paisa,
                }
                for alloc in details.allocations
            ],
            "unallocated_paisa": details.unallocated_paisa,
            "generated_at_utc": timecalc.utc_now_iso(),
        }

    # ------------------------------------------------------------------
    # connection-taking helpers (for other services' transactions)
    # ------------------------------------------------------------------
    def _reverse_allocations_for_ticket(
        self,
        conn: sqlite3.Connection,
        ticket_id: int,
        reason: str,
        business_date: str,
        who: str,
    ) -> int:
        """Reverse every still-effective allocation on a ticket (audited).

        Used by ``CustomerCreditService.reverse_ticket`` inside its own
        transaction. Returns the number of reversal rows written.
        """
        count = 0
        for alloc in self._payments.list_allocations_for_ticket(
            conn, ticket_id
        ):
            remaining = int(alloc["effective_paisa"])
            if remaining <= 0:
                continue
            self._payments.insert_reversal(
                conn,
                _uuid.uuid4().hex,
                int(alloc["allocation_id"]),
                int(alloc["payment_id"]),
                ticket_id,
                remaining,
                business_date,
                reason,
                None,
                who,
            )
            count += 1
        return count

    def _reverse_allocations_for_payment(
        self,
        conn: sqlite3.Connection,
        payment_id: int,
        reason: str,
        business_date: str,
        who: str,
    ) -> int:
        """Reverse every still-effective allocation of a payment (audited).

        Returns the number of reversal rows written.
        """
        count = 0
        for alloc in self._payments.list_allocations_for_payment(
            conn, payment_id
        ):
            remaining = int(alloc["effective_paisa"])
            if remaining <= 0:
                continue
            self._payments.insert_reversal(
                conn,
                _uuid.uuid4().hex,
                int(alloc["allocation_id"]),
                payment_id,
                int(alloc["credit_sale_id"]),
                remaining,
                business_date,
                reason,
                None,
                who,
            )
            count += 1
        return count

    def _payment_details_conn(
        self, conn: sqlite3.Connection, payment_id: int
    ) -> PaymentInfo:
        """Payment DTO with allocations on a caller-owned connection."""
        pay = self._payments.get_payment(conn, payment_id)
        if pay is None:  # pragma: no cover — defensive
            raise errors.NotFoundError(
                f"Unknown payment: {payment_id!r}.",
                key="err.unknown_payment",
                params={"ref": payment_id},
            )
        allocations = tuple(
            dto.allocation_info_from_row(row)
            for row in self._payments.list_allocations_for_payment(
                conn, payment_id
            )
        )
        unallocated = self._payments.payment_unallocated(conn, payment_id) or 0
        return dto.payment_info_from_row(
            pay, allocations=allocations, unallocated_paisa=unallocated
        )

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------
    def _allocation_order_conn(self, conn: sqlite3.Connection) -> str:
        value = self._days.get_setting(conn, _ALLOCATION_ORDER_KEY)
        return value if value in _ALLOCATION_ORDERS else "fifo"

    def _resolve_customer(self, conn, customer: str | int):
        if isinstance(customer, bool):
            raise errors.ValidationError(
                f"Invalid customer reference: {customer!r}.",
                key="err.invalid_customer_ref",
                params={"ref": customer},
            )
        if isinstance(customer, int):
            row = self._payments.get_customer(conn, customer)
        elif isinstance(customer, str):
            code = customer.strip()
            if not code:
                raise errors.ValidationError(
                    "Customer code must not be empty.",
                    key="err.customer_code_empty",
                )
            row = self._payments.get_by_code(conn, code)
        else:
            raise errors.ValidationError(
                f"Invalid customer reference: {customer!r}.",
                key="err.invalid_customer_ref",
                params={"ref": customer},
            )
        if row is None:
            raise errors.NotFoundError(
                f"Unknown customer: {customer!r}.",
                key="err.unknown_customer",
                params={"ref": customer},
            )
        return row

    def _resolve_payment(self, conn, payment: str | int):
        if isinstance(payment, bool):
            raise errors.ValidationError(
                f"Invalid payment reference: {payment!r}.",
                key="err.invalid_payment_ref",
                params={"ref": payment},
            )
        if isinstance(payment, int):
            row = self._payments.get_payment(conn, payment)
        elif isinstance(payment, str):
            ref = payment.strip()
            if not ref:
                raise errors.ValidationError(
                    "Payment reference must not be empty.",
                    key="err.payment_ref_empty",
                )
            row = self._payments.get_payment_by_uuid(conn, ref)
        else:
            raise errors.ValidationError(
                f"Invalid payment reference: {payment!r}.",
                key="err.invalid_payment_ref",
                params={"ref": payment},
            )
        if row is None:
            raise errors.NotFoundError(
                f"Unknown payment: {payment!r}.",
                key="err.unknown_payment",
                params={"ref": payment},
            )
        return row

    def _resolve_ticket_id(self, conn, ticket: str | int) -> int:
        if isinstance(ticket, bool):
            raise errors.ValidationError(
                f"Invalid ticket reference: {ticket!r}.",
                key="err.invalid_ticket_ref",
                params={"ref": ticket},
            )
        if isinstance(ticket, int):
            row = self._payments.get_ticket(conn, ticket)
        elif isinstance(ticket, str):
            ref = ticket.strip()
            if not ref:
                raise errors.ValidationError(
                    "Ticket reference must not be empty.",
                    key="err.ticket_ref_empty",
                )
            row = self._payments.get_ticket_by_uuid(conn, ref)
        else:
            raise errors.ValidationError(
                f"Invalid ticket reference: {ticket!r}.",
                key="err.invalid_ticket_ref",
                params={"ref": ticket},
            )
        if row is None:
            raise errors.NotFoundError(
                f"Unknown credit ticket: {ticket!r}.",
                key="err.unknown_ticket",
                params={"ref": ticket},
            )
        return int(row["id"])

    def _fifo_splits_conn(
        self,
        conn: sqlite3.Connection,
        customer_id: int,
        payment_id: int,  # noqa: ARG002 — kept for signature symmetry
        max_paisa: int,
    ) -> list[tuple[int, int]]:
        """``(ticket_id, amount)`` oldest-unpaid-first, capped at
        *max_paisa*."""
        splits: list[tuple[int, int]] = []
        remaining = max_paisa
        for ticket in self._payments.outstanding_tickets_fifo(
            conn, customer_id
        ):
            if remaining <= 0:
                break
            unpaid = int(ticket["unpaid_paisa"])
            take = min(unpaid, remaining)
            if take > 0:
                splits.append((int(ticket["id"]), take))
                remaining -= take
        return splits

    def _resolve_splits(
        self,
        conn: sqlite3.Connection,
        payment_id: int,
        customer_id: int,
        splits: list[dict] | None,
    ) -> list[tuple[int, int]]:
        """Normalize ``splits`` to ``[(ticket_id, amount)]``.

        ``None`` → FIFO. Each explicit split is validated: positive integer
        paisa, a posted non-reversal ticket of the same customer, no
        duplicate ticket.
        """
        if splits is None:
            unallocated = self._payments.payment_unallocated(conn, payment_id)
            return self._fifo_splits_conn(
                conn, customer_id, payment_id, unallocated or 0
            )
        if not splits:
            raise errors.ValidationError(
                "Allocation splits must not be empty.",
                key="err.allocation_splits_empty",
            )
        plan: list[tuple[int, int]] = []
        seen: set[int] = set()
        for split in splits:
            amount = split.get("amount_paisa")
            if (
                isinstance(amount, bool)
                or not isinstance(amount, int)
                or amount <= 0
            ):
                raise errors.ValidationError(
                    "Each allocation amount must be a positive integer"
                    " number of paisa.",
                    key="err.allocation_amount_positive",
                )
            ticket_uuid = split.get("ticket_uuid")
            if not ticket_uuid or not str(ticket_uuid).strip():
                raise errors.ValidationError(
                    "Each allocation split needs a ticket_uuid.",
                    key="err.allocation_split_needs_ticket",
                )
            ticket = self._payments.get_ticket_by_uuid(
                conn, str(ticket_uuid).strip()
            )
            if ticket is None:
                raise errors.NotFoundError(
                    f"Unknown credit ticket: {ticket_uuid!r}.",
                    key="err.unknown_ticket",
                    params={"ref": ticket_uuid},
                )
            if (
                ticket["status"] != "posted"
                or ticket["reverses_uuid"] is not None
            ):
                raise errors.BusinessRuleError(
                    "Payments can only be allocated to posted credit"
                    f" tickets (ticket {ticket_uuid!r} is not allocatable).",
                    key="err.ticket_not_allocatable",
                    params={"ticket": ticket_uuid},
                )
            if int(ticket["customer_id"]) != customer_id:
                raise errors.ValidationError(
                    "The payment and the ticket belong to different"
                    " customers.",
                    key="err.payment_ticket_customer_mismatch",
                )
            ticket_id = int(ticket["id"])
            if ticket_id in seen:
                raise errors.ValidationError(
                    "Duplicate ticket in the allocation splits.",
                    key="err.duplicate_ticket_in_splits",
                )
            seen.add(ticket_id)
            plan.append((ticket_id, amount))
        return plan

    @staticmethod
    def _require_stamp_on_day(
        occurred_at_utc: str | None, business_date: str
    ) -> str:
        """Validate (or default) the event timestamp and pin it to the
        business day's Asia/Karachi calendar date."""
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

    @staticmethod
    def _require_original_day_writable(
        conn: sqlite3.Connection, original_date: str
    ) -> None:
        """The reversal's original row lives on its own day — that day must
        also accept writes (§11.13)."""
        require_writable_day(conn, original_date)
