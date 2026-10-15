"""Customer credit: customers, credit tickets, opening adjustments (Phase 4).

A credit ticket classifies metered sales (§6) — it creates NO inventory
movement and NO revenue entry: the liters were already sold through the
meter and are only being attributed to a customer. Ticket math uses the
price effective at the ticket's ``occurred_at_utc`` (latest price row with
``effective_from_utc <= occurred_at_utc``); the ticket never reprices when
prices later change.

Credit limits are warning-only (§11.7): exceeding one is recorded in the
ticket's warnings, never blocks the sale. Corrections use reversal tickets
(§11.13) — posted tickets are immutable.
"""

from __future__ import annotations

import sqlite3
import uuid as _uuid

from app.calculations import money as calc
from app.calculations import time as timecalc
from app.models import credit as dto
from app.models.credit import (
    AdjustmentInfo,
    CustomerInfo,
    CustomerStatement,
    LedgerEntry,
    TicketInfo,
)
from app.repositories.customer_repository import CustomerRepository
from app.repositories.nozzle_repository import NozzleRepository
from app.services import errors
from app.services._base import ServiceBase, require_writable_day
from app.services.payment_service import PaymentService
from app.services.price_service import PriceService, resolve_fuel_product


def _require_paisa(value, name: str, *, allow_negative: bool = False) -> int:
    """Validate an integer-paisa service argument.

    Money crosses service boundaries as integer paisa (§5.2) — never float,
    Decimal or str. Rejects bools too (``isinstance(True, int)``).
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise errors.ValidationError(
            f"{name} must be an integer number of paisa.",
            key="err.paisa_must_be_integer",
            params={"name": name},
        )
    if value < 0 and not allow_negative:
        raise errors.ValidationError(
            f"{name} must not be negative.",
            key="err.paisa_must_not_be_negative",
            params={"name": name},
        )
    return value


class CustomerCreditService(ServiceBase):
    """Manages customers, credit tickets and opening-balance adjustments."""

    def __init__(self, db_manager, actor: str | None = None) -> None:
        super().__init__(db_manager, actor)
        self._customers = CustomerRepository()
        self._nozzles = NozzleRepository()
        self._prices = PriceService(db_manager, actor)

    # ------------------------------------------------------------------
    # customers
    # ------------------------------------------------------------------
    def create_customer(
        self,
        name: str,
        *,
        customer_code: str | None = None,
        business_name: str | None = None,
        phone: str | None = None,
        address: str | None = None,
        opening_receivable_paisa: int = 0,
        credit_limit_paisa: int | None = None,
        notes: str | None = None,
        actor: str | None = None,
    ) -> CustomerInfo:
        """Create a customer.

        * ``customer_code`` defaults to the next ``'CUST-%04d'`` code;
        * ``opening_receivable_paisa`` is the once-entered opening balance
          in integer paisa (§11.5) — non-negative, never editable
          afterwards;
        * ``credit_limit_paisa`` is optional; exceeding it later only warns
          (§11.7).

        Returns the customer with its outstanding balance.
        """
        clean_name = (name or "").strip()
        if not clean_name:
            raise errors.ValidationError(
                "Customer name must not be empty.",
                key="err.customer_name_empty",
            )
        opening_paisa = _require_paisa(
            opening_receivable_paisa, "Opening receivable"
        )
        if credit_limit_paisa is None:
            limit_paisa = None
        else:
            limit_paisa = _require_paisa(credit_limit_paisa, "Credit limit")
        code = (customer_code or "").strip()
        who = self.resolve_actor(actor)
        with self.transaction() as conn:
            if not code:
                code = self._customers.next_customer_code(conn)
            customer_id = self._customers.insert_customer(
                conn,
                code,
                clean_name,
                (business_name or "").strip() or None,
                (phone or "").strip() or None,
                (address or "").strip() or None,
                opening_paisa,
                limit_paisa,
                (notes or "").strip() or None,
                who,
            )
            return self._customer_info_conn(conn, customer_id)

    def update_customer(
        self,
        customer_id: int,
        *,
        name: str | None = None,
        business_name: str | None = None,
        phone: str | None = None,
        address: str | None = None,
        credit_limit_paisa: int | None = None,
        clear_credit_limit: bool = False,
        status: str | None = None,
        notes: str | None = None,
        actor: str | None = None,  # noqa: ARG002 — signature symmetry
    ) -> CustomerInfo:
        """Update mutable customer fields; ``None`` means "no change".

        The opening receivable is NOT updatable here — balance errors are
        fixed with :meth:`adjust_opening_balance`. Pass
        ``clear_credit_limit=True`` to remove a credit limit. Returns the
        updated customer.
        """
        fields: dict = {}
        if name is not None:
            clean = name.strip()
            if not clean:
                raise errors.ValidationError(
                    "Customer name must not be empty.",
                    key="err.customer_name_empty",
                )
            fields["name"] = clean
        if business_name is not None:
            fields["business_name"] = business_name.strip() or None
        if phone is not None:
            fields["phone"] = phone.strip() or None
        if address is not None:
            fields["address"] = address.strip() or None
        if notes is not None:
            fields["notes"] = notes.strip() or None
        if clear_credit_limit:
            fields["credit_limit_paisa"] = None
        elif credit_limit_paisa is not None:
            fields["credit_limit_paisa"] = _require_paisa(
                credit_limit_paisa, "Credit limit"
            )
        if status is not None:
            if status not in ("active", "inactive"):
                raise errors.ValidationError(
                    f"Invalid customer status: {status!r}.",
                    key="err.invalid_customer_status",
                    params={"status": status},
                )
            fields["status"] = status
        with self.transaction() as conn:
            row = self._customers.get_customer(conn, customer_id)
            if row is None:
                raise errors.NotFoundError(
                    f"Unknown customer: {customer_id!r}.",
                    key="err.unknown_customer",
                    params={"ref": customer_id},
                )
            if fields:
                self._customers.update_customer(
                    conn, int(row["id"]), fields
                )
            return self._customer_info_conn(conn, int(row["id"]))

    def get_customer(self, customer_id: int) -> CustomerInfo:
        """One customer with its outstanding balance."""
        with self.read_connection() as conn:
            return self._customer_info_conn(conn, customer_id)

    def get_customer_by_code(self, customer_code: str) -> CustomerInfo:
        """One customer looked up by exact code."""
        with self.read_connection() as conn:
            row = self._customers.get_by_code(conn, customer_code.strip())
            if row is None:
                raise errors.NotFoundError(
                    f"Unknown customer code: {customer_code!r}.",
                    key="err.unknown_customer_code",
                    params={"code": customer_code},
                )
            return dto.customer_info_from_row(row)

    def list_customers(self, active_only: bool = True) -> list[CustomerInfo]:
        """Customers with outstanding balances, code order."""
        with self.read_connection() as conn:
            rows = self._customers.list_customers(conn, active_only=active_only)
        return [dto.customer_info_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # opening-balance adjustments
    # ------------------------------------------------------------------
    def adjust_opening_balance(
        self,
        customer_id: int,
        adjustment_paisa: int,
        reason: str,
        business_date: str,
        *,
        authorized_by: str | None = None,
        occurred_at_utc: str | None = None,
        actor: str | None = None,
    ) -> AdjustmentInfo:
        """Post an audited, signed opening-balance adjustment.

        ``adjustment_paisa`` is a signed non-zero integer (positive
        increases what the customer owes); ``reason`` and the authorizer
        are mandatory — the computed ledger balance itself is never edited
        (§11.5).
        """
        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise errors.ValidationError(
                "A reason is required for an opening-balance adjustment.",
                key="err.adjustment_needs_reason",
            )
        signed_paisa = _require_paisa(
            adjustment_paisa, "Adjustment", allow_negative=True
        )
        if signed_paisa == 0:
            raise errors.ValidationError(
                "The adjustment must be non-zero.",
                key="err.adjustment_nonzero",
            )
        adjustment_paisa = signed_paisa
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        stamp = self._require_stamp_on_day(occurred_at_utc, business_date)
        who = self.resolve_actor(actor)
        authorized = (authorized_by or "").strip() or who
        with self.transaction() as conn:
            row = self._customers.get_customer(conn, customer_id)
            if row is None:
                raise errors.NotFoundError(
                    f"Unknown customer: {customer_id!r}.",
                    key="err.unknown_customer",
                    params={"ref": customer_id},
                )
            require_writable_day(conn, business_date)
            adjustment_id = self._customers.insert_adjustment(
                conn,
                _uuid.uuid4().hex,
                int(row["id"]),
                business_date,
                adjustment_paisa,
                clean_reason,
                authorized,
                stamp,
                who,
            )
            adj_row = conn.execute(
                "SELECT * FROM customer_opening_adjustments WHERE id = ?",
                (adjustment_id,),
            ).fetchone()
            return dto.adjustment_info_from_row(adj_row)

    # ------------------------------------------------------------------
    # credit tickets
    # ------------------------------------------------------------------
    def issue_ticket(
        self,
        customer: str | int,
        business_date: str,
        lines: list[dict],
        *,
        paid_now_paisa: int = 0,
        receipt_no: str | None = None,
        occurred_at_utc: str | None = None,
        notes: str | None = None,
        actor: str | None = None,
    ) -> TicketInfo:
        """Issue a credit ticket (one or more fuel lines).

        * ``lines``: ``[{fuel_product_code, nozzle_code|None,
          quantity_litres}, …]`` — each line is priced by the price
          effective at the ticket's ``occurred_at_utc``;
        * ``paid_now_paisa``: integer paisa of cash taken at the sale moment
          — it settles THIS ticket only, never older debt (§11.4);
        * ``receipt_no``: auto-generated ``'T-<YYYYMMDD>-<seq>'`` when not
          given;
        * a configured credit limit that the new unpaid amount would
          exceed produces a WARNING in the ticket, never a block (§11.7).

        The ticket classifies metered sales: it writes NO inventory
        movement and NO revenue entry. Returns the ticket with its lines.
        """
        if not lines:
            raise errors.ValidationError(
                "A credit ticket needs at least one line.",
                key="err.ticket_needs_line",
            )
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        paid_now_paisa = _require_paisa(paid_now_paisa, "Paid-now amount")
        stamp = self._require_stamp_on_day(occurred_at_utc, business_date)
        receipt = (receipt_no or "").strip() or None
        who = self.resolve_actor(actor)

        with self.transaction() as conn:
            cust = self._resolve_customer(conn, customer)
            if cust["status"] != "active":
                raise errors.BusinessRuleError(
                    f"Customer {cust['customer_code']} is inactive.",
                    key="err.customer_inactive",
                    params={"code": cust["customer_code"]},
                )
            require_writable_day(conn, business_date)
            priced_lines = [
                self._price_ticket_line(conn, line, stamp)
                for line in lines
            ]
            gross = sum(line["line_amount_paisa"] for line in priced_lines)
            if paid_now_paisa > gross:
                raise errors.ValidationError(
                    "Paid-now cannot exceed the ticket gross amount.",
                    key="err.paid_now_exceeds_gross",
                )
            warnings: list[str] = []
            limit = cust["credit_limit_paisa"]
            if limit is not None:
                outstanding = self._customers.outstanding_for_customer(
                    conn, int(cust["id"])
                ) or 0
                projected = outstanding + (gross - paid_now_paisa)
                if projected > int(limit):
                    warnings.append(
                        "Credit limit exceeded: "
                        f"{calc.format_pkr(projected)} outstanding would"
                        f" exceed the {calc.format_pkr(int(limit))} limit."
                        " Recorded as a warning only."
                    )
            if receipt is None:
                seq = self._customers.count_tickets_by_day(conn, business_date)
                receipt = f"T-{business_date.replace('-', '')}-{seq + 1:03d}"
            ticket_uuid = _uuid.uuid4().hex
            ticket_id = self._customers.insert_ticket(
                conn,
                ticket_uuid,
                int(cust["id"]),
                business_date,
                gross,
                paid_now_paisa,
                receipt,
                None,  # reverses_uuid
                stamp,
                (notes or "").strip() or None,
                who,
            )
            for seq_no, line in enumerate(priced_lines, start=1):
                self._customers.insert_line(
                    conn,
                    ticket_id,
                    seq_no,
                    line["fuel_product_id"],
                    line["nozzle_id"],
                    line["quantity_ml"],
                    line["unit_price_10k"],
                    line["line_amount_paisa"],
                    stamp,
                    who,
                )
            return self._ticket_info_conn(
                conn, ticket_id, warnings=tuple(warnings)
            )

    def reverse_ticket(
        self,
        ticket_uuid: str,
        reason: str,
        business_date: str,
        *,
        actor: str | None = None,
    ) -> TicketInfo:
        """Reverse a credit ticket.

        Existing allocations to the ticket are reversed first (audited),
        then a reversal ticket row (mirrored lines, ``paid_now=0``) is
        posted on *business_date* and the original flips ``posted →
        reversed`` — one transaction. Both *business_date* and the original
        ticket's day must be writable (``ClosedDayError`` otherwise,
        §11.13). Returns the reversal ticket.
        """
        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise errors.ValidationError(
                "A reason is required to reverse a credit ticket.",
                key="err.reverse_ticket_needs_reason",
            )
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        who = self.resolve_actor(actor)
        with self.transaction() as conn:
            original = self._customers.get_ticket_by_uuid(conn, ticket_uuid)
            if original is None:
                raise errors.NotFoundError(
                    f"Unknown credit ticket: {ticket_uuid!r}.",
                    key="err.unknown_ticket",
                    params={"ref": ticket_uuid},
                )
            if (
                original["status"] != "posted"
                or original["reverses_uuid"] is not None
            ):
                raise errors.BusinessRuleError(
                    "Only a posted credit ticket can be reversed.",
                    key="err.only_posted_ticket_reversible",
                )
            original_id = int(original["id"])
            require_writable_day(conn, business_date)
            require_writable_day(conn, original["business_date"])
            payments = PaymentService(self._db, self._actor)
            payments._reverse_allocations_for_ticket(
                conn, original_id, clean_reason, business_date, who
            )
            reversal_uuid = _uuid.uuid4().hex
            seq = self._customers.count_tickets_by_day(conn, business_date)
            receipt = f"T-{business_date.replace('-', '')}-{seq + 1:03d}"
            reversal_id = self._customers.insert_ticket(
                conn,
                reversal_uuid,
                int(original["customer_id"]),
                business_date,
                int(original["gross_amount_paisa"]),
                0,
                receipt,
                ticket_uuid,
                None,
                f"Reversal of {original['receipt_no'] or ticket_uuid[:8]}:"
                f" {clean_reason}",
                who,
            )
            original_lines = self._customers.list_lines_for_ticket(
                conn, original_id
            )
            for line in original_lines:
                self._customers.insert_line(
                    conn,
                    reversal_id,
                    int(line["seq_no"]),
                    int(line["fuel_product_id"]),
                    (
                        int(line["nozzle_id"])
                        if line["nozzle_id"] is not None
                        else None
                    ),
                    int(line["quantity_ml"]),
                    int(line["unit_price_10k"]),
                    int(line["line_amount_paisa"]),
                    None,
                    who,
                )
            self._customers.mark_ticket_reversed(
                conn, original_id, reversal_uuid
            )
            return self._ticket_info_conn(conn, reversal_id)

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def ticket_details(self, ticket: str | int) -> TicketInfo:
        """A ticket with its lines and effective unpaid balance."""
        with self.read_connection() as conn:
            ticket_id = self._resolve_ticket_id(conn, ticket)
            return self._ticket_info_conn(conn, ticket_id)

    def day_tickets(self, business_date: str) -> list[TicketInfo]:
        """The day's tickets (including reversal rows), id order."""
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            rows = self._customers.list_tickets_by_day(conn, business_date)
            return [
                self._ticket_info_conn(conn, int(row["id"])) for row in rows
            ]

    def outstanding(self, customer: str | int) -> int:
        """Ledger-derived outstanding receivable in paisa (design plan
        §7)."""
        with self.read_connection() as conn:
            cust = self._resolve_customer(conn, customer)
            value = self._customers.outstanding_for_customer(
                conn, int(cust["id"])
            )
            return value if value is not None else 0

    def outstanding_all(self) -> list[CustomerInfo]:
        """Every customer with its outstanding balance, code order."""
        with self.read_connection() as conn:
            rows = self._customers.list_customers(conn, active_only=False)
        return [dto.customer_info_from_row(row) for row in rows]

    def customer_ledger(self, customer: str | int) -> list[LedgerEntry]:
        """The customer's chronological ledger with running balances
        (``v_customer_ledger``)."""
        with self.read_connection() as conn:
            cust = self._resolve_customer(conn, customer)
            rows = self._customers.ledger_for_customer(conn, int(cust["id"]))
        return [dto.ledger_entry_from_row(row) for row in rows]

    def statement(
        self,
        customer: str | int,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> CustomerStatement:
        """A date-filtered customer statement.

        Entries whose Asia/Karachi timestamp falls in
        ``[date_from, date_to]`` (inclusive); the opening balance is the
        ledger balance just before *date_from*.
        """
        for label, value in (("date_from", date_from), ("date_to", date_to)):
            if value is not None and not timecalc.is_valid_business_date(value):
                raise errors.ValidationError(
                    f"Invalid {label}: {value!r}.",
                    key="err.invalid_report_value",
                    params={"label": label, "value": value},
                )
        start = (
            timecalc.karachi_start_of_day_utc(date_from)
            if date_from
            else None
        )
        end = (
            timecalc.karachi_end_of_day_utc(date_to) if date_to else None
        )
        with self.read_connection() as conn:
            cust = self._resolve_customer(conn, customer)
            info = dto.customer_info_from_row(cust)
            rows = self._customers.ledger_for_customer(conn, int(cust["id"]))
        opening = 0
        entries: list[LedgerEntry] = []
        for row in rows:
            entry = dto.ledger_entry_from_row(row)
            stamp = entry.occurred_at_utc
            if start is not None and stamp < start:
                opening += entry.debit_paisa - entry.credit_paisa
                continue
            if end is not None and stamp > end:
                continue
            entries.append(entry)
        debit = sum(e.debit_paisa for e in entries)
        credit = sum(e.credit_paisa for e in entries)
        return CustomerStatement(
            customer=info,
            date_from=date_from,
            date_to=date_to,
            entries=tuple(entries),
            opening_paisa=opening,
            debit_paisa=debit,
            credit_paisa=credit,
            closing_paisa=opening + debit - credit,
        )

    def ticket_integrity(self, ticket: str | int) -> list[str]:
        """Re-verify a ticket's stored math (used by the close checklist).

        Checks: header gross == Σ line amounts; each line amount ==
        ``quantity × unit price`` (canonical formula); nozzle/product
        consistency. Returns the list of problems (empty = clean).

        The English strings are rendered deterministically (always
        English) from the same keyed segments the checklist localizes —
        see :meth:`ticket_problem_segments`.
        """
        from app.localization import render_en

        return [
            render_en(key, params)
            for key, params in self.ticket_problem_segments(ticket)
        ]

    def ticket_problem_segments(
        self, ticket: str | int
    ) -> list[tuple[str, dict]]:
        """Keyed ``(message_key, params)`` integrity problems for a ticket.

        Same checks as :meth:`ticket_integrity`; the close checklist
        renders these bilingually at display time.
        """
        with self.read_connection() as conn:
            ticket_id = self._resolve_ticket_id(conn, ticket)
            return self._ticket_problem_segments_conn(conn, ticket_id)

    def _ticket_problem_segments_conn(
        self, conn, ticket_id: int
    ) -> list[tuple[str, dict]]:
        """Integrity problems as ``(message_key, params)`` segments."""
        segments: list[tuple[str, dict]] = []
        row = self._customers.get_ticket(conn, ticket_id)
        lines = self._customers.list_lines_for_ticket(conn, ticket_id)
        line_total = sum(int(line["line_amount_paisa"]) for line in lines)
        gross = int(row["gross_amount_paisa"])
        if line_total != gross:
            segments.append(
                (
                    "close.detail.ticket_gross_mismatch",
                    {"gross_paisa": gross, "lines_paisa": line_total},
                )
            )
        for line in lines:
            seq = int(line["seq_no"])
            expected = calc.amount_paisa_for(
                int(line["quantity_ml"]), int(line["unit_price_10k"])
            )
            if expected != int(line["line_amount_paisa"]):
                segments.append(
                    (
                        "close.detail.ticket_line_amount",
                        {
                            "seq": seq,
                            "stored_paisa": int(line["line_amount_paisa"]),
                            "expected_paisa": expected,
                        },
                    )
                )
            if line["nozzle_id"] is not None:
                nozzle = self._nozzles.get_nozzle(
                    conn, int(line["nozzle_id"])
                )
                if nozzle is None:
                    segments.append(
                        (
                            "close.detail.ticket_nozzle_missing",
                            {"seq": seq, "nozzle_id": int(line["nozzle_id"])},
                        )
                    )
                elif int(nozzle["fuel_product_id"]) != int(
                    line["fuel_product_id"]
                ):
                    segments.append(
                        (
                            "close.detail.ticket_nozzle_product",
                            {"seq": seq, "nozzle_code": nozzle["code"]},
                        )
                    )
        return segments

    # ------------------------------------------------------------------
    # allocation order (passthrough to PaymentService, which owns the
    # app_settings key)
    # ------------------------------------------------------------------
    def allocation_order(self) -> str:
        """The configured allocation order (``'fifo'`` default)."""
        return PaymentService(self._db, self._actor).allocation_order()

    def set_allocation_order(
        self, order: str, *, actor: str | None = None
    ) -> str:
        """Set the allocation order (``'fifo'`` | ``'manual'``)."""
        return PaymentService(self._db, self._actor).set_allocation_order(
            order, actor=actor
        )

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------
    def _customer_info_conn(
        self, conn: sqlite3.Connection, customer_id: int
    ) -> CustomerInfo:
        row = self._customers.get_customer(conn, customer_id)
        if row is None:  # pragma: no cover — defensive
            raise errors.NotFoundError(
                f"Unknown customer: {customer_id!r}.",
                key="err.unknown_customer",
                params={"ref": customer_id},
            )
        return dto.customer_info_from_row(row)

    def _ticket_info_conn(
        self,
        conn: sqlite3.Connection,
        ticket_id: int,
        *,
        warnings: tuple[str, ...] = (),
    ) -> TicketInfo:
        row = self._customers.get_ticket(conn, ticket_id)
        if row is None:  # pragma: no cover — defensive
            raise errors.NotFoundError(
                f"Unknown credit ticket: {ticket_id!r}.",
                key="err.unknown_ticket",
                params={"ref": ticket_id},
            )
        lines = tuple(
            dto.ticket_line_info_from_row(line)
            for line in self._customers.list_lines_for_ticket(conn, ticket_id)
        )
        if row["status"] == "posted" and row["reverses_uuid"] is None:
            unpaid = self._customers.ticket_unpaid(conn, ticket_id) or 0
        else:
            unpaid = 0
        return dto.ticket_info_from_row(
            row, lines=lines, warnings=warnings, unpaid_paisa=unpaid
        )

    def _resolve_customer(self, conn, customer: str | int):
        if isinstance(customer, bool):
            raise errors.ValidationError(
                f"Invalid customer reference: {customer!r}.",
                key="err.invalid_customer_ref",
                params={"ref": customer},
            )
        if isinstance(customer, int):
            row = self._customers.get_customer(conn, customer)
        elif isinstance(customer, str):
            code = customer.strip()
            if not code:
                raise errors.ValidationError(
                    "Customer code must not be empty.",
                    key="err.customer_code_empty",
                )
            row = self._customers.get_by_code(conn, code)
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

    def _resolve_ticket_id(self, conn, ticket: str | int) -> int:
        if isinstance(ticket, bool):
            raise errors.ValidationError(
                f"Invalid ticket reference: {ticket!r}.",
                key="err.invalid_ticket_ref",
                params={"ref": ticket},
            )
        if isinstance(ticket, int):
            row = self._customers.get_ticket(conn, ticket)
        elif isinstance(ticket, str):
            ref = ticket.strip()
            if not ref:
                raise errors.ValidationError(
                    "Ticket reference must not be empty.",
                    key="err.ticket_ref_empty",
                )
            row = self._customers.get_ticket_by_uuid(conn, ref)
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

    def _price_ticket_line(
        self, conn: sqlite3.Connection, line: dict, stamp: str
    ) -> dict:
        """Validate one ticket line dict and price it from history.

        Returns ``{fuel_product_id, nozzle_id, quantity_ml, unit_price_10k,
        line_amount_paisa}``.
        """
        if not isinstance(line, dict):
            raise errors.ValidationError(
                "Each ticket line must be a mapping with fuel_product_code,"
                " quantity_litres and an optional nozzle_code.",
                key="err.ticket_line_invalid",
            )
        product = line.get("fuel_product_code")
        prod = resolve_fuel_product(conn, product)
        product_id = int(prod["id"])
        try:
            quantity_ml = calc.to_ml(line.get("quantity_litres"))
        except (TypeError, ValueError) as exc:
            raise errors.ValidationError(
                f"Invalid line quantity: {exc}",
                key="err.invalid_line_quantity",
                params={"detail": str(exc)},
            ) from exc
        if quantity_ml <= 0:
            raise errors.ValidationError(
                "Each ticket line quantity must be greater than zero.",
                key="err.line_quantity_positive",
            )
        nozzle_code = line.get("nozzle_code")
        nozzle_id: int | None = None
        if nozzle_code is not None:
            code = str(nozzle_code).strip()
            if not code:
                raise errors.ValidationError(
                    "Nozzle code must not be empty.",
                    key="err.nozzle_code_empty",
                )
            nozzle = self._nozzles.get_nozzle_by_code(conn, code)
            if nozzle is None:
                raise errors.NotFoundError(
                    f"Unknown nozzle: {code!r}.",
                    key="err.unknown_nozzle",
                    params={"ref": code},
                )
            if not nozzle["is_active"]:
                raise errors.BusinessRuleError(
                    f"Nozzle {code} is inactive.",
                    key="err.nozzle_inactive",
                    params={"code": code},
                )
            if int(nozzle["fuel_product_id"]) != product_id:
                raise errors.ValidationError(
                    f"Nozzle {code} dispenses {nozzle['fuel_product_code']},"
                    f" not {prod['code']}.",
                    key="err.nozzle_product_mismatch",
                    params={
                        "code": code,
                        "stored": nozzle["fuel_product_code"],
                        "product": prod["code"],
                    },
                )
            nozzle_id = int(nozzle["id"])
        price = self._prices._get_effective_conn(conn, product_id, stamp)
        if price is None:
            raise errors.NotFoundError(
                f"No price recorded for {prod['code']} effective at"
                f" {stamp} — set the price first.",
                key="err.no_price_for_product",
                params={"code": prod["code"], "stamp": stamp},
            )
        unit_price_10k = int(price.unit_price_10k)
        return {
            "fuel_product_id": product_id,
            "nozzle_id": nozzle_id,
            "quantity_ml": quantity_ml,
            "unit_price_10k": unit_price_10k,
            "line_amount_paisa": calc.amount_paisa_for(
                quantity_ml, unit_price_10k
            ),
        }

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
