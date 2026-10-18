"""Cash book: settlements, receipts/payments, cash position (Phase 5).

Accounting rules implemented here:

* **Settlement identity** (design §6): per fuel product,
  ``meter_revenue = cash + bank + paid_now_on_ticket + receivable + other``.
  ``sales_settlements`` rows *classify* meter-derived revenue; they are not
  cash movements. Old-debt recovery never appears here.
* **Effective-amount rule** (matches the v10/v11 views): posted →
  ``+amount``; reversal row (``reverses_uuid`` / ``is_reversal=1``) →
  ``-amount``. A row flipped to ``status='reversed'`` keeps its posted
  contribution — the flip is only a marker; the reversal row negates it.
  Net effect of a reversal: 0 (migration 011, mirroring 009).
* **Cash-method only** (design §11.14): ``cash_receipts`` / ``cash_payments``
  hold physical cash exclusively. Bank/other activity never touches them.
* **Derived cash rows** are pure bookkeeping, never manager judgment:
  ``ensure_cash_postings_conn`` backfills exactly one cash row per source
  (paid-now tickets, cash recoveries, cash expenses, cash supplier payments,
  cash other-product purchases/sales, settlement cash legs), guarded by the
  unique source indexes, and reverses derived rows whose source was
  reversed. ``record_settlements`` writes the ``'cash'`` settlement leg
  *and* its ``cash_sales`` receipt in the same transaction — the leg
  classifies revenue, the receipt records the drawer; they can never
  disagree.
* A day's cash difference (physical − expected) is reported, never
  auto-written-off (design §11.8).
"""

from __future__ import annotations

import sqlite3
import uuid as _uuid
from dataclasses import dataclass, field
from decimal import Decimal

from app.calculations import money as calc
from app.calculations import time as timecalc
from app.models.cash import (
    CashCategoryInfo,
    CashPaymentInfo,
    CashPosition,
    CashReceiptInfo,
    SettlementLeg,
)
from app.repositories.cashbook_repository import CashbookRepository
from app.repositories.customer_repository import CustomerRepository
from app.services import errors
from app.services._base import ServiceBase, require_writable_day
from app.services.price_service import resolve_fuel_product
from app.services.sales_service import SalesService

_MANAGER_CATEGORIES = ("cash", "bank", "other")
_TICKET_CATEGORIES = ("paid_now_on_ticket", "receivable")


def _to_category_info(row) -> CashCategoryInfo:
    return CashCategoryInfo(
        id=int(row["id"]),
        kind=row["kind"],
        name=row["name"],
        name_en=row["name_en"],
        name_ur=row["name_ur"],
        is_active=bool(row["is_active"]),
    )


def _effective(amount: int, status: str, reverses_uuid) -> int:
    """Net-of-reversal amount for a cash/expense/payment row.

    Corrected rule (migration 011, mirroring 009): a reversed original
    keeps its posted contribution — the status flip is only a marker —
    and the reversal row negates it. Net effect of a reversal: 0.
    """
    if reverses_uuid is not None:
        return -amount
    return amount


def _to_receipt_info(row) -> CashReceiptInfo:
    amount = int(row["amount_paisa"])
    return CashReceiptInfo(
        id=int(row["id"]),
        uuid=row["uuid"],
        business_date=row["business_date"],
        category=row["category"],
        category_en=row["category_en"],
        amount_paisa=amount,
        effective_paisa=_effective(amount, row["status"], row["reverses_uuid"]),
        source_table=row["source_table"],
        source_id=int(row["source_id"]) if row["source_id"] is not None else None,
        customer_id=int(row["customer_id"])
        if row["customer_id"] is not None
        else None,
        customer_name=row["customer_name"],
        remarks=row["remarks"],
        status=row["status"],
        occurred_at_utc=row["occurred_at_utc"],
        created_by=row["created_by"],
    )


def _to_payment_info(row) -> CashPaymentInfo:
    amount = int(row["amount_paisa"])
    return CashPaymentInfo(
        id=int(row["id"]),
        uuid=row["uuid"],
        business_date=row["business_date"],
        category=row["category"],
        category_en=row["category_en"],
        amount_paisa=amount,
        effective_paisa=_effective(amount, row["status"], row["reverses_uuid"]),
        source_table=row["source_table"],
        source_id=int(row["source_id"]) if row["source_id"] is not None else None,
        payee=row["payee"],
        remarks=row["remarks"],
        status=row["status"],
        occurred_at_utc=row["occurred_at_utc"],
        created_by=row["created_by"],
    )


def _to_settlement_leg(row) -> SettlementLeg:
    return SettlementLeg(
        fuel_product_code=row["fuel_product_code"],
        category=row["category"],
        amount_paisa=int(row["amount_paisa"]),
        source_table=row["source_table"],
        source_id=int(row["source_id"]) if row["source_id"] is not None else None,
        batch_uuid=row["batch_uuid"],
        is_reversal=bool(row["is_reversal"]),
        status=row["status"],
        notes=row["notes"],
    )


@dataclass
class _CashGap:
    """One derived cash posting that is missing or has gone stale.

    Shared by ``ensure_cash_postings_conn`` (writes it) and
    ``missing_cash_postings`` (reports it) so the backfill and the close
    checklist can never disagree.
    """

    action: str  # 'create' | 'reverse'
    kind: str  # 'receipt' | 'payment'
    category: str  # machine name
    amount_paisa: int
    source_table: str
    source_id: int
    business_date: str
    customer_id: int | None = None
    payee: str | None = None
    remarks: str | None = None
    occurred_at_utc: str | None = None
    cash_uuid: str | None = None  # the derived row to flip, for reversals
    description: str = ""


@dataclass
class _TicketLegs:
    """Ticket-derived settlement legs for a day.

    ``written`` holds the legs actually persisted (amounts > 0 with their
    source line); ``signed_receivable`` / ``signed_paid_now`` hold the
    signed per-product aggregates (reversal tickets negate) used for the
    live receivables figure.
    """

    written: list[dict] = field(default_factory=list)
    signed_receivable: dict[str, int] = field(default_factory=dict)
    signed_paid_now: dict[str, int] = field(default_factory=dict)


class CashbookService(ServiceBase):
    """Records settlements, cash receipts/payments and the cash position."""

    def __init__(self, db_manager, actor: str | None = None) -> None:
        super().__init__(db_manager, actor)
        self._cash = CashbookRepository()
        self._customers = CustomerRepository()
        self._sales = SalesService(db_manager, actor)

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

    @staticmethod
    def _require_paisa(value, name: str) -> int:
        """Money crossing the service boundary is integer paisa (never
        float, never bool)."""
        if isinstance(value, bool) or not isinstance(value, int):
            raise errors.ValidationError(
                f"{name} must be integer paisa.",
                key="err.must_be_integer_paisa",
                params={"name": name},
            )
        return value

    def _resolve_category(
        self, conn: sqlite3.Connection, kind: str, name: str
    ):
        """Active cash category by machine name, or raise."""
        clean = (name or "").strip()
        if not clean:
            raise errors.ValidationError(
                "Category must not be empty.", key="err.category_empty"
            )
        row = self._cash.get_category(conn, kind, clean)
        if row is None or not row["is_active"]:
            raise errors.ValidationError(
                f"Unknown or inactive {kind} category: {clean!r}.",
                key="err.unknown_category",
                params={"kind": kind, "name": clean},
            )
        return row

    # ------------------------------------------------------------------
    # settlement ticket legs
    # ------------------------------------------------------------------
    def _ticket_legs_conn(self, conn: sqlite3.Connection, business_date: str) -> _TicketLegs:
        """Compute the day's ticket-derived settlement legs.

        Each posted ticket's ``paid_now`` is split across its lines pro-rata
        by ``line_amount_paisa`` (round-half-up per line, the last line takes
        the remainder so Σ = paid_now). Per line two legs are produced:
        ``paid_now_on_ticket`` (the share) and ``receivable``
        (line − share); only legs with amount > 0 are written
        (``sales_settlements.amount_paisa`` is unsigned).

        Sign rule (migration 011, mirroring 009): original tickets
        contribute +legs — including tickets flipped to ``'reversed'`` (the
        flip is only a marker; the row is never erased); reversal tickets
        (``reverses_uuid`` set, ``paid_now = 0``) negate. Net effect of
        voiding a ticket: 0.
        """
        out = _TicketLegs()
        by_ticket: dict[int, dict] = {}
        order: list[int] = []
        for line in self._cash.posted_ticket_lines(conn, business_date):
            ticket_id = int(line["credit_sale_id"])
            bucket = by_ticket.get(ticket_id)
            if bucket is None:
                bucket = {
                    "paid_now": int(line["paid_now_paisa"]),
                    "reverses": line["reverses_uuid"] is not None,
                    "lines": [],
                }
                by_ticket[ticket_id] = bucket
                order.append(ticket_id)
            bucket["lines"].append(line)
        for ticket_id in order:
            bucket = by_ticket[ticket_id]
            lines = bucket["lines"]
            paid_now = bucket["paid_now"]
            sign = -1 if bucket["reverses"] else 1
            total_line = sum(int(line["line_amount_paisa"]) for line in lines)
            remaining = paid_now
            for index, line in enumerate(lines):
                line_amount = int(line["line_amount_paisa"])
                code = line["fuel_product_code"]
                if index == len(lines) - 1:
                    share = remaining
                elif total_line > 0:
                    share = calc.round_half_up(
                        Decimal(paid_now)
                        * Decimal(line_amount)
                        / Decimal(total_line)
                    )
                else:
                    share = 0
                remaining -= share
                paid_now_leg = sign * share
                receivable_leg = sign * (line_amount - share)
                out.signed_paid_now[code] = (
                    out.signed_paid_now.get(code, 0) + paid_now_leg
                )
                out.signed_receivable[code] = (
                    out.signed_receivable.get(code, 0) + receivable_leg
                )
                leg_base = {
                    "fuel_product_id": int(line["fuel_product_id"]),
                    "fuel_product_code": code,
                    "source_table": "credit_sale_lines",
                    "source_id": int(line["line_id"]),
                }
                if paid_now_leg > 0:
                    out.written.append(
                        {**leg_base, "category": "paid_now_on_ticket",
                         "amount_paisa": paid_now_leg}
                    )
                if receivable_leg > 0:
                    out.written.append(
                        {**leg_base, "category": "receivable",
                         "amount_paisa": receivable_leg}
                    )
        return out

    def _resolve_manager_products(
        self, conn: sqlite3.Connection, *maps: dict[str, int] | None
    ) -> dict[str, int]:
        """Resolve fuel-product codes from the manager split maps to ids."""
        codes: set[str] = set()
        for mapping in maps:
            if mapping:
                codes.update(mapping.keys())
        resolved: dict[str, int] = {}
        for code in codes:
            if not isinstance(code, str):
                raise errors.ValidationError(
                    f"Fuel product keys must be product codes, got {code!r}.",
                    key="err.product_keys_must_be_codes",
                    params={"code": code},
                )
            resolved[code] = int(resolve_fuel_product(conn, code)["id"])
        return resolved

    @staticmethod
    def _check_split_map(mapping: dict[str, int] | None, name: str) -> dict[str, int]:
        """Validate one manager split map (int paisa ≥ 0)."""
        if mapping is None:
            return {}
        if not isinstance(mapping, dict):
            raise errors.ValidationError(
                f"{name} must be a product→paisa map.",
                key="err.split_map_invalid",
                params={"name": name},
            )
        clean: dict[str, int] = {}
        for code, amount in mapping.items():
            if isinstance(amount, bool) or not isinstance(amount, int):
                raise errors.ValidationError(
                    f"{name}[{code!r}] must be integer paisa.",
                    key="err.split_entry_paisa",
                    params={"name": name, "code": code},
                )
            if amount < 0:
                raise errors.ValidationError(
                    f"{name}[{code!r}] must not be negative.",
                    key="err.split_entry_negative",
                    params={"name": name, "code": code},
                )
            clean[code] = amount
        return clean

    # ------------------------------------------------------------------
    # record_settlements
    # ------------------------------------------------------------------
    def record_settlements(
        self,
        business_date: str,
        *,
        cash_by_product: dict[str, int],
        bank_by_product: dict[str, int] | None = None,
        other_by_product: dict[str, int] | None = None,
        notes: str | None = None,
        occurred_at_utc: str | None = None,
        actor: str | None = None,
    ) -> str:
        """Record the day's settlement batch (the §6 identity, per product).

        Ticket legs are derived from the day's posted credit-sale lines
        (paid-now split pro-rata); manager legs come from the three split
        maps. The identity
        ``cash + bank + paid_now + receivable + other == meter_revenue``
        is enforced per product *and* on the day total — a mismatch names
        the product and the difference.

        Idempotent: an identical retry returns the existing ``batch_uuid``;
        a different split for an already-settled day raises ``DuplicateError``
        ("already recorded; reverse first"). Returns the batch uuid.
        """
        stamp = self._require_stamp_on_day(occurred_at_utc, business_date)
        who = self.resolve_actor(actor)
        cash = self._check_split_map(cash_by_product, "cash_by_product")
        bank = self._check_split_map(bank_by_product, "bank_by_product")
        other = self._check_split_map(other_by_product, "other_by_product")
        with self.transaction() as conn:
            return self._record_settlements_conn(
                conn, business_date, cash, bank, other, notes, stamp, who
            )

    def _record_settlements_conn(
        self,
        conn: sqlite3.Connection,
        business_date: str,
        cash: dict[str, int],
        bank: dict[str, int],
        other: dict[str, int],
        notes: str | None,
        stamp: str,
        who: str,
    ) -> str:
        """Conn-level settlement writer (single transaction, no nesting)."""
        require_writable_day(conn, business_date)
        product_ids = self._resolve_manager_products(conn, cash, bank, other)
        meter = self._sales._day_sales_conn(
            conn, business_date
        ).revenue_by_product_paisa
        legs = self._ticket_legs_conn(conn, business_date)

        ticket_cash: dict[str, int] = {}
        ticket_recv: dict[str, int] = {}
        for leg in legs.written:
            code = leg["fuel_product_code"]
            if leg["category"] == "paid_now_on_ticket":
                ticket_cash[code] = ticket_cash.get(code, 0) + leg["amount_paisa"]
            else:
                ticket_recv[code] = ticket_recv.get(code, 0) + leg["amount_paisa"]

        products = set(meter) | set(product_ids) | set(ticket_cash) | set(ticket_recv)
        per_product: dict[str, dict[str, int]] = {}
        for code in products:
            row = {
                "cash": cash.get(code, 0),
                "bank": bank.get(code, 0),
                "other": other.get(code, 0),
                "paid_now": ticket_cash.get(code, 0),
                "receivable": ticket_recv.get(code, 0),
                "meter": meter.get(code, 0),
            }
            settled = (
                row["cash"] + row["bank"] + row["paid_now"]
                + row["receivable"] + row["other"]
            )
            if settled != row["meter"]:
                diff = settled - row["meter"]
                raise errors.ValidationError(
                    "Settlement identity broken for product"
                    f" {code!r}: cash + bank + paid-now + receivable + other"
                    f" ({settled} paisa) != meter revenue"
                    f" ({row['meter']} paisa); difference {diff:+d} paisa.",
                    key="err.settlement_identity_product",
                    params={
                        "code": code,
                        "settled_paisa": settled,
                        "meter_paisa": row["meter"],
                        "diff": diff,
                    },
                )
            per_product[code] = row
        total_settled = sum(
            r["cash"] + r["bank"] + r["paid_now"] + r["receivable"] + r["other"]
            for r in per_product.values()
        )
        total_meter = sum(meter.values())
        if total_settled != total_meter:
            raise errors.ValidationError(
                "Settlement day total broken: settled"
                f" {total_settled} paisa != meter {total_meter} paisa.",
                key="err.settlement_day_total",
                params={
                    "settled_paisa": total_settled,
                    "meter_paisa": total_meter,
                },
            )

        # Idempotency: an active batch with identical legs → return it.
        existing = self._cash.active_batch_uuid(conn, business_date)
        if existing is not None:
            current = self._batch_leg_keys(conn, existing)
            wanted = self._wanted_leg_keys(
                legs.written, cash, bank, other, product_ids
            )
            if current == wanted:
                return existing
            raise errors.DuplicateError(
                f"Settlements for {business_date} are already recorded;"
                " reverse the batch first to record a different split.",
                key="err.settlements_already_recorded",
                params={"day_date": business_date},
            )

        batch_uuid = _uuid.uuid4().hex
        for leg in legs.written:
            self._cash.insert_settlement(
                conn,
                _uuid.uuid4().hex,
                business_date,
                leg["fuel_product_id"],
                leg["category"],
                leg["amount_paisa"],
                leg["source_table"],
                leg["source_id"],
                batch_uuid,
                False,
                None,
                notes,
                stamp,
                who,
            )
        for code in sorted(products):
            for category in _MANAGER_CATEGORIES:
                amount = {"cash": cash, "bank": bank, "other": other}[
                    category
                ].get(code, 0)
                if amount > 0:
                    leg_id = self._cash.insert_settlement(
                        conn,
                        _uuid.uuid4().hex,
                        business_date,
                        product_ids[code],
                        category,
                        amount,
                        None,
                        None,
                        batch_uuid,
                        False,
                        None,
                        notes,
                        stamp,
                        who,
                    )
                    if category == "cash":
                        # The cash leg classifies revenue AND posts the
                        # day's cash-sales receipt — one row, exactly once
                        # (unique source ref), in the same transaction.
                        self._insert_cash_sales_receipt_conn(
                            conn,
                            business_date,
                            leg_id,
                            amount,
                            code,
                            stamp,
                            who,
                        )
        return batch_uuid

    def _insert_cash_sales_receipt_conn(
        self,
        conn: sqlite3.Connection,
        business_date: str,
        settlement_leg_id: int,
        amount_paisa: int,
        fuel_product_code: str,
        stamp: str,
        who: str,
    ) -> None:
        """Derived ``cash_sales`` receipt for one settlement cash leg.

        Idempotent: the unique ``(source_table, source_id)`` index plus the
        ``derived_receipt`` guard mean a retry never double-posts.
        """
        cat = self._cash.get_category(conn, "receipt", "cash_sales")
        if cat is None or not cat["is_active"]:
            raise errors.BusinessRuleError(
                "Cash category 'cash_sales' is missing or inactive; cannot"
                " post derived cash rows.",
                key="err.derived_cash_category_missing",
                params={"category": "cash_sales"},
            )
        if (
            self._cash.derived_receipt(
                conn, "sales_settlements", settlement_leg_id
            )
            is not None
        ):
            return
        self._cash.insert_receipt(
            conn,
            _uuid.uuid4().hex,
            business_date,
            int(cat["id"]),
            amount_paisa,
            "sales_settlements",
            settlement_leg_id,
            None,
            f"Cash sales {fuel_product_code} (settlement)",
            None,
            stamp,
            who,
        )

    @staticmethod
    def _batch_leg_keys(
        conn: sqlite3.Connection, batch_uuid: str
    ) -> set[tuple]:
        """Identity key set of an active batch's legs (for idempotency)."""
        rows = CashbookRepository.settlement_rows_for_batch(conn, batch_uuid)
        return {
            (
                row["fuel_product_code"],
                row["category"],
                int(row["amount_paisa"]),
                row["source_table"],
                row["source_id"],
            )
            for row in rows
            if row["status"] == "posted"
        }

    @staticmethod
    def _wanted_leg_keys(
        written: list[dict],
        cash: dict[str, int],
        bank: dict[str, int],
        other: dict[str, int],
        product_ids: dict[str, int],
    ) -> set[tuple]:
        keys = {
            (
                leg["fuel_product_code"],
                leg["category"],
                leg["amount_paisa"],
                leg["source_table"],
                leg["source_id"],
            )
            for leg in written
        }
        for code in set(cash) | set(bank) | set(other):
            for category, mapping in (
                ("cash", cash),
                ("bank", bank),
                ("other", other),
            ):
                amount = mapping.get(code, 0)
                if amount > 0:
                    keys.add((code, category, amount, None, None))
        return keys

    # ------------------------------------------------------------------
    # reverse_settlements
    # ------------------------------------------------------------------
    def reverse_settlements(
        self, business_date: str, *, reason: str, actor: str | None = None
    ) -> str:
        """Reverse the day's active settlement batch, in ONE transaction.

        Every leg (ticket legs and manager legs) gets a reversal row
        (``is_reversal=1``, same amounts, new batch uuid) and the originals
        flip to ``'reversed'``. Derived cash rows sourced from this batch's
        settlement rows — and the paid-now receipts of the batch's tickets —
        are reversed through the same cash-reversal helper. Returns the new
        (reversal) batch uuid. A non-empty reason is required.
        """
        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise errors.ValidationError(
                "A reason is required to reverse settlements.",
                key="err.reverse_settlements_needs_reason",
            )
        who = self.resolve_actor(actor)
        stamp = timecalc.utc_now_iso()
        with self.transaction() as conn:
            require_writable_day(conn, business_date)
            batch_uuid = self._cash.active_batch_uuid(conn, business_date)
            if batch_uuid is None:
                raise errors.NotFoundError(
                    f"No active settlement batch for {business_date}.",
                    key="err.no_active_settlement_batch",
                    params={"day_date": business_date},
                )
            rows = self._cash.settlement_rows_for_batch(conn, batch_uuid)
            posted = [r for r in rows if r["status"] == "posted"]
            if not posted:
                raise errors.NotFoundError(
                    f"No active settlement batch for {business_date}.",
                    key="err.no_active_settlement_batch",
                    params={"day_date": business_date},
                )
            reversal_batch = _uuid.uuid4().hex
            note = (
                f"Reversal of settlement batch {batch_uuid[:8]}:"
                f" {clean_reason}"
            )
            for row in posted:
                self._cash.insert_settlement(
                    conn,
                    _uuid.uuid4().hex,
                    business_date,
                    int(row["fuel_product_id"]),
                    row["category"],
                    int(row["amount_paisa"]),
                    row["source_table"],
                    int(row["source_id"])
                    if row["source_id"] is not None
                    else None,
                    reversal_batch,
                    True,
                    int(row["id"]),
                    note,
                    stamp,
                    who,
                )
            for row in posted:
                self._cash.flip_settlement_reversed(
                    conn, int(row["id"]), reversal_batch
                )
            # Derived cash rows: settlement-sourced receipts, and the
            # paid-now receipts of this batch's tickets.
            settlement_ids = {int(r["id"]) for r in posted}
            for cash_row in self._cash.list_receipts(conn, business_date):
                if (
                    cash_row["source_table"] == "sales_settlements"
                    and cash_row["source_id"] in settlement_ids
                    and cash_row["status"] == "posted"
                    and cash_row["reverses_uuid"] is None
                ):
                    self._reverse_receipt_conn(
                        conn, cash_row, clean_reason, who, stamp
                    )
            ticket_ids = {
                int(line["credit_sale_id"])
                for line in self._cash.posted_ticket_lines(conn, business_date)
            }
            batch_ticket_ids: set[int] = set()
            for row in posted:
                if row["source_table"] == "credit_sale_lines":
                    line = self._ticket_line_ticket(conn, int(row["source_id"]))
                    if line is not None:
                        batch_ticket_ids.add(line)
            batch_ticket_ids &= ticket_ids
            for cash_row in self._cash.list_receipts(conn, business_date):
                if (
                    cash_row["source_table"] == "credit_sales"
                    and cash_row["source_id"] in batch_ticket_ids
                    and cash_row["status"] == "posted"
                    and cash_row["reverses_uuid"] is None
                ):
                    self._reverse_receipt_conn(
                        conn, cash_row, clean_reason, who, stamp
                    )
            return reversal_batch

    @staticmethod
    def _ticket_line_ticket(
        conn: sqlite3.Connection, line_id: int
    ) -> int | None:
        row = conn.execute(
            "SELECT credit_sale_id FROM credit_sale_lines WHERE id = ?",
            (line_id,),
        ).fetchone()
        return int(row["credit_sale_id"]) if row else None

    # ------------------------------------------------------------------
    # settlement_status (+ conn variant)
    # ------------------------------------------------------------------
    def settlement_status(self, business_date: str) -> dict:
        """The day's settlement state (read-only)."""
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            return self._settlement_status_conn(conn, business_date)

    def settlement_status_conn(
        self, conn: sqlite3.Connection, business_date: str
    ) -> dict:
        """Conn-level ``settlement_status`` for the close flow.

        Runs on a caller-owned connection so it sees uncommitted rows
        inside the close transaction (a fresh connection would not).
        """
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        return self._settlement_status_conn(conn, business_date)

    def _settlement_status_conn(
        self, conn: sqlite3.Connection, business_date: str
    ) -> dict:
        """The settlement identity per product from live data.

        ``settled`` = cash + bank + paid-now + other (the ``receivable`` leg
        classifies new credit; it is not settled cash). ``new_receivables``
        is the live ticket truth — Σ signed (line − paid-now share) — so a
        ticket change after recording shows up as a broken identity.
        """
        meter = self._sales._day_sales_conn(
            conn, business_date
        ).revenue_by_product_paisa
        legs: dict[str, dict[str, int]] = {}
        for row in self._cash.effective_legs_by_product(conn, business_date):
            legs.setdefault(row["fuel_product_code"], {})[row["category"]] = int(
                row["total_paisa"] or 0
            )
        ticket = self._ticket_legs_conn(conn, business_date)
        products = (
            set(meter)
            | set(legs)
            | set(ticket.signed_receivable)
            | set(ticket.signed_paid_now)
        )
        per_product = []
        identity_ok = True
        for code in sorted(products):
            cats = legs.get(code, {})
            cash = cats.get("cash", 0)
            bank = cats.get("bank", 0)
            paid_now = cats.get("paid_now_on_ticket", 0)
            receivable_leg = cats.get("receivable", 0)
            other = cats.get("other", 0)
            settled = cash + bank + paid_now + other
            new_receivables = ticket.signed_receivable.get(code, 0)
            meter_revenue = meter.get(code, 0)
            difference = meter_revenue - settled - new_receivables
            row_ok = difference == 0
            identity_ok = identity_ok and row_ok
            per_product.append(
                {
                    "fuel_product_code": code,
                    "meter_revenue_paisa": meter_revenue,
                    "cash_paisa": cash,
                    "bank_paisa": bank,
                    "paid_now_paisa": paid_now,
                    "receivable_paisa": receivable_leg,
                    "other_paisa": other,
                    "settled_paisa": settled,
                    "new_receivables_paisa": new_receivables,
                    "identity_ok": row_ok,
                    "difference_paisa": difference,
                }
            )
        batch_uuid = self._cash.active_batch_uuid(conn, business_date)
        reversed_batches = self._cash.reversed_batch_uuids(conn, business_date)
        recorded = batch_uuid is not None
        if recorded:
            note = (
                "Settlements recorded; Σ effective legs (cash + bank +"
                " paid-now + other) plus live ticket receivables must equal"
                " meter revenue per product."
            )
        elif reversed_batches:
            note = (
                "The settlement batch was reversed; record a new batch"
                " before closing."
            )
        else:
            note = "No settlements recorded for this day yet."
        return {
            "business_date": business_date,
            "recorded": recorded,
            "batch_uuid": batch_uuid,
            "reversed_batches": len(reversed_batches),
            "per_product": per_product,
            "identity_ok": identity_ok,
            "note": note,
        }

    # ------------------------------------------------------------------
    # ensure_settlements_conn (close flow)
    # ------------------------------------------------------------------
    def ensure_settlements_conn(
        self, conn: sqlite3.Connection, business_date: str, actor: str | None
    ) -> dict:
        """Idempotent close-flow helper: settle the day when needed.

        * Active batch exists → ``{"created": False, "batch_uuid"}``.
        * No meter revenue and no posted tickets → ``{"created": False,
          "batch_uuid": None, "note": "no sales to settle"}``.
        * Otherwise auto-record one batch with bank/other = 0 and cash =
          residual per product (meter − paid-now − receivable); a negative
          residual (ticket legs exceed meter revenue — data anomaly) raises
          ``BusinessRuleError`` instead of recording a negative cash leg.
        """
        who = self.resolve_actor(actor)
        require_writable_day(conn, business_date)
        existing = self._cash.active_batch_uuid(conn, business_date)
        if existing is not None:
            return {"created": False, "batch_uuid": existing}
        meter = self._sales._day_sales_conn(
            conn, business_date
        ).revenue_by_product_paisa
        meter_total = sum(meter.values())
        ticket_count = self._cash.count_posted_tickets(conn, business_date)
        if meter_total == 0 and ticket_count == 0:
            return {
                "created": False,
                "batch_uuid": None,
                "note": "no sales to settle",
            }
        legs = self._ticket_legs_conn(conn, business_date)
        ticket_cash: dict[str, int] = {}
        ticket_recv: dict[str, int] = {}
        for leg in legs.written:
            code = leg["fuel_product_code"]
            if leg["category"] == "paid_now_on_ticket":
                ticket_cash[code] = ticket_cash.get(code, 0) + leg["amount_paisa"]
            else:
                ticket_recv[code] = ticket_recv.get(code, 0) + leg["amount_paisa"]
        cash: dict[str, int] = {}
        for code in set(meter) | set(ticket_cash) | set(ticket_recv):
            residual = (
                meter.get(code, 0)
                - ticket_cash.get(code, 0)
                - ticket_recv.get(code, 0)
            )
            if residual < 0:
                raise errors.BusinessRuleError(
                    "Cannot auto-settle product"
                    f" {code!r}: ticket legs exceed meter revenue by"
                    f" {-residual} paisa. Reverse the tickets or correct the"
                    " readings first; a negative cash leg is never recorded.",
                    key="err.auto_settle_negative",
                    params={"code": code, "excess_paisa": -residual},
                )
            if residual > 0:
                cash[code] = residual
        stamp = timecalc.utc_now_iso()
        batch_uuid = self._record_settlements_conn(
            conn,
            business_date,
            cash,
            {},
            {},
            "auto-recorded at close: all unsettled sales treated as cash;"
            " reverse and re-record to split bank/other",
            stamp,
            who,
        )
        return {"created": True, "batch_uuid": batch_uuid}

    # ------------------------------------------------------------------
    # manual receipts / payments
    # ------------------------------------------------------------------
    def record_receipt(
        self,
        *,
        business_date: str,
        category: str,
        amount_paisa: int,
        customer_id: int | None = None,
        remarks: str | None = None,
        occurred_at_utc: str | None = None,
        actor: str | None = None,
    ) -> CashReceiptInfo:
        """Record a manual cash receipt (source NULL — manager-entered).

        ``category`` is the machine name of an active ``'receipt'`` category.
        """
        amount = self._require_paisa(amount_paisa, "amount_paisa")
        if amount <= 0:
            raise errors.ValidationError(
                "Receipt amount must be positive.",
                key="err.receipt_amount_positive",
            )
        stamp = self._require_stamp_on_day(occurred_at_utc, business_date)
        who = self.resolve_actor(actor)
        with self.transaction() as conn:
            require_writable_day(conn, business_date)
            cat = self._resolve_category(conn, "receipt", category)
            customer_name = None
            if customer_id is not None:
                customer = self._customers.get_customer(conn, int(customer_id))
                if customer is None:
                    raise errors.NotFoundError(
                        f"Unknown customer: {customer_id!r}.",
                        key="err.unknown_customer",
                        params={"ref": customer_id},
                    )
                customer_name = customer["name"]
            receipt_uuid = _uuid.uuid4().hex
            self._cash.insert_receipt(
                conn,
                receipt_uuid,
                business_date,
                int(cat["id"]),
                amount,
                None,
                None,
                int(customer_id) if customer_id is not None else None,
                (remarks or "").strip() or None,
                None,
                stamp,
                who,
            )
            row = self._cash.get_receipt_by_uuid(conn, receipt_uuid)
            return _to_receipt_info(row)

    def record_payment(
        self,
        *,
        business_date: str,
        category: str,
        amount_paisa: int,
        payee: str | None = None,
        remarks: str | None = None,
        occurred_at_utc: str | None = None,
        actor: str | None = None,
    ) -> CashPaymentInfo:
        """Record a manual cash payment (source NULL — manager-entered)."""
        amount = self._require_paisa(amount_paisa, "amount_paisa")
        if amount <= 0:
            raise errors.ValidationError(
                "Payment amount must be positive.",
                key="err.payment_amount_positive",
            )
        stamp = self._require_stamp_on_day(occurred_at_utc, business_date)
        who = self.resolve_actor(actor)
        with self.transaction() as conn:
            require_writable_day(conn, business_date)
            cat = self._resolve_category(conn, "payment", category)
            payment_uuid = _uuid.uuid4().hex
            self._cash.insert_payment(
                conn,
                payment_uuid,
                business_date,
                int(cat["id"]),
                amount,
                (payee or "").strip() or None,
                None,
                None,
                (remarks or "").strip() or None,
                None,
                stamp,
                who,
            )
            row = self._cash.get_payment_by_uuid(conn, payment_uuid)
            return _to_payment_info(row)

    # ------------------------------------------------------------------
    # cash reversals
    # ------------------------------------------------------------------
    def reverse_cash_receipt(
        self, receipt_uuid: str, *, reason: str, actor: str | None = None
    ) -> CashReceiptInfo:
        """Reverse a cash receipt: mirror row + posted → reversed flip.

        A non-empty reason is required. Returns the reversal row's info.
        """
        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise errors.ValidationError(
                "A reason is required to reverse a cash receipt.",
                key="err.reverse_receipt_needs_reason",
            )
        who = self.resolve_actor(actor)
        stamp = timecalc.utc_now_iso()
        with self.transaction() as conn:
            row = self._cash.get_receipt_by_uuid(conn, receipt_uuid)
            if row is None:
                raise errors.NotFoundError(
                    f"Unknown cash receipt: {receipt_uuid!r}.",
                    key="err.unknown_cash_receipt",
                    params={"ref": receipt_uuid},
                )
            new_uuid = self._reverse_receipt_conn(
                conn, row, clean_reason, who, stamp
            )
            return _to_receipt_info(
                self._cash.get_receipt_by_uuid(conn, new_uuid)
            )

    def reverse_cash_payment(
        self, payment_uuid: str, *, reason: str, actor: str | None = None
    ) -> CashPaymentInfo:
        """Reverse a cash payment: mirror row + posted → reversed flip."""
        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise errors.ValidationError(
                "A reason is required to reverse a cash payment.",
                key="err.reverse_payment_needs_reason",
            )
        who = self.resolve_actor(actor)
        stamp = timecalc.utc_now_iso()
        with self.transaction() as conn:
            row = self._cash.get_payment_by_uuid(conn, payment_uuid)
            if row is None:
                raise errors.NotFoundError(
                    f"Unknown cash payment: {payment_uuid!r}.",
                    key="err.unknown_cash_payment",
                    params={"ref": payment_uuid},
                )
            new_uuid = self._reverse_payment_conn(
                conn, row, clean_reason, who, stamp
            )
            return _to_payment_info(
                self._cash.get_payment_by_uuid(conn, new_uuid)
            )

    def _reverse_receipt_conn(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        reason: str,
        who: str,
        stamp: str,
    ) -> str:
        """Conn-level receipt reversal; returns the new row's uuid."""
        self._require_reversible(row, "receipt")
        require_writable_day(conn, row["business_date"])
        new_uuid = _uuid.uuid4().hex
        remarks = row["remarks"]
        note = f"{remarks} — reversal: {reason}" if remarks else f"Reversal: {reason}"
        self._cash.insert_receipt(
            conn,
            new_uuid,
            row["business_date"],
            int(row["category_id"]),
            int(row["amount_paisa"]),
            row["source_table"],
            int(row["source_id"]) if row["source_id"] is not None else None,
            int(row["customer_id"]) if row["customer_id"] is not None else None,
            note,
            row["uuid"],
            stamp,
            who,
        )
        self._cash.flip_receipt_reversed(conn, int(row["id"]), new_uuid)
        return new_uuid

    def _reverse_payment_conn(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        reason: str,
        who: str,
        stamp: str,
    ) -> str:
        """Conn-level payment reversal; returns the new row's uuid."""
        self._require_reversible(row, "payment")
        require_writable_day(conn, row["business_date"])
        new_uuid = _uuid.uuid4().hex
        remarks = row["remarks"]
        note = f"{remarks} — reversal: {reason}" if remarks else f"Reversal: {reason}"
        self._cash.insert_payment(
            conn,
            new_uuid,
            row["business_date"],
            int(row["category_id"]),
            int(row["amount_paisa"]),
            row["payee"],
            row["source_table"],
            int(row["source_id"]) if row["source_id"] is not None else None,
            note,
            row["uuid"],
            stamp,
            who,
        )
        self._cash.flip_payment_reversed(conn, int(row["id"]), new_uuid)
        return new_uuid

    @staticmethod
    def _require_reversible(row: sqlite3.Row, what: str) -> None:
        if row["reverses_uuid"] is not None:
            raise errors.BusinessRuleError(
                f"A reversal {what} cannot itself be reversed; post a"
                " compensating entry instead.",
                key="err.reversal_of_reversal",
                params={"what": what},
            )
        if row["status"] != "posted" or row["reversed_by_uuid"] is not None:
            raise errors.BusinessRuleError(
                f"Only a posted {what} can be reversed.",
                key="err.only_posted_reversible",
                params={"what": what},
            )

    # ------------------------------------------------------------------
    # derived cash postings (backfill + checklist)
    # ------------------------------------------------------------------
    def _derive_cash_gaps_conn(
        self, conn: sqlite3.Connection, business_date: str
    ) -> list[_CashGap]:
        """Every derived posting missing for the day, plus stale ones.

        Creation derivations (each guarded by the unique source index):
        (a) posted tickets with ``paid_now > 0`` → ``paid_now_on_ticket``
            receipts; (b) cash customer payments → ``credit_recovery``;
        (c) cash expenses → ``expense`` payments; (d) cash supplier payments
        → ``supplier_payment`` payments; (e) cash other-product purchases /
        sales → ``other_materials_purchase`` / ``other_sales``.
        Reversal rows of sources (``reverses_uuid``) never post derived rows.
        Additionally, a derived row still posted while its source was
        reversed is reported for reversal ("source reversed").
        """
        gaps: list[_CashGap] = []

        def _fmt(amount: int) -> str:
            return f"PKR {calc.format_pkr(amount)}"

        # (a) paid-now on tickets -------------------------------------------
        for ticket in conn.execute(
            "SELECT id, customer_id, paid_now_paisa, occurred_at_utc"
            " FROM credit_sales"
            " WHERE business_date = ? AND status = 'posted'"
            " AND reverses_uuid IS NULL AND paid_now_paisa > 0",
            (business_date,),
        ).fetchall():
            source_id = int(ticket["id"])
            if self._cash.derived_receipt(conn, "credit_sales", source_id) is None:
                amount = int(ticket["paid_now_paisa"])
                gaps.append(
                    _CashGap(
                        action="create",
                        kind="receipt",
                        category="paid_now_on_ticket",
                        amount_paisa=amount,
                        source_table="credit_sales",
                        source_id=source_id,
                        business_date=business_date,
                        customer_id=int(ticket["customer_id"]),
                        occurred_at_utc=ticket["occurred_at_utc"],
                        description=(
                            f"Missing cash receipt 'Paid at sale (credit)'"
                            f" {_fmt(amount)} for credit ticket #{source_id}"
                            " (paid-now)."
                        ),
                    )
                )
        # (b) cash customer payments (old-debt recovery) ---------------------
        for payment in conn.execute(
            "SELECT id, customer_id, amount_paisa, occurred_at_utc"
            " FROM customer_payments"
            " WHERE business_date = ? AND status = 'posted'"
            " AND reverses_uuid IS NULL AND method = 'cash'",
            (business_date,),
        ).fetchall():
            source_id = int(payment["id"])
            if self._cash.derived_receipt(conn, "customer_payments", source_id) is None:
                amount = int(payment["amount_paisa"])
                gaps.append(
                    _CashGap(
                        action="create",
                        kind="receipt",
                        category="credit_recovery",
                        amount_paisa=amount,
                        source_table="customer_payments",
                        source_id=source_id,
                        business_date=business_date,
                        customer_id=int(payment["customer_id"]),
                        occurred_at_utc=payment["occurred_at_utc"],
                        description=(
                            f"Missing cash receipt 'Old debt recovery'"
                            f" {_fmt(amount)} for customer payment"
                            f" #{source_id}."
                        ),
                    )
                )
        # (c) cash expenses ---------------------------------------------------
        for expense in conn.execute(
            "SELECT id, amount_paisa, payee, reference, occurred_at_utc"
            " FROM expenses"
            " WHERE business_date = ? AND status = 'posted'"
            " AND reverses_uuid IS NULL AND method = 'cash'",
            (business_date,),
        ).fetchall():
            source_id = int(expense["id"])
            if self._cash.derived_payment(conn, "expenses", source_id) is None:
                amount = int(expense["amount_paisa"])
                gaps.append(
                    _CashGap(
                        action="create",
                        kind="payment",
                        category="expense",
                        amount_paisa=amount,
                        source_table="expenses",
                        source_id=source_id,
                        business_date=business_date,
                        payee=expense["payee"],
                        remarks=expense["reference"],
                        occurred_at_utc=expense["occurred_at_utc"],
                        description=(
                            f"Missing cash payment 'Expense' {_fmt(amount)}"
                            f" for expense #{source_id}."
                        ),
                    )
                )
        # (d) cash supplier payments ------------------------------------------
        for payment in conn.execute(
            "SELECT sp.id, sp.amount_paisa, sp.occurred_at_utc,"
            " s.name AS supplier_name"
            " FROM supplier_payments sp"
            " JOIN suppliers s ON s.id = sp.supplier_id"
            " WHERE sp.business_date = ? AND sp.status = 'posted'"
            " AND sp.reverses_uuid IS NULL AND sp.method = 'cash'",
            (business_date,),
        ).fetchall():
            source_id = int(payment["id"])
            if self._cash.derived_payment(
                conn, "supplier_payments", source_id
            ) is None:
                amount = int(payment["amount_paisa"])
                gaps.append(
                    _CashGap(
                        action="create",
                        kind="payment",
                        category="supplier_payment",
                        amount_paisa=amount,
                        source_table="supplier_payments",
                        source_id=source_id,
                        business_date=business_date,
                        payee=payment["supplier_name"],
                        occurred_at_utc=payment["occurred_at_utc"],
                        description=(
                            f"Missing cash payment 'Supplier payment'"
                            f" {_fmt(amount)} for supplier payment"
                            f" #{source_id}."
                        ),
                    )
                )
        # (e) cash other-product purchases / sales -----------------------------
        for txn in conn.execute(
            "SELECT id, txn_type, total_paisa, reference, occurred_at_utc"
            " FROM other_product_transactions"
            " WHERE business_date = ? AND method = 'cash'"
            " AND txn_type IN ('purchase', 'sale')",
            (business_date,),
        ).fetchall():
            source_id = int(txn["id"])
            amount = int(txn["total_paisa"])
            if txn["txn_type"] == "purchase":
                if self._cash.derived_payment(
                    conn, "other_product_transactions", source_id
                ) is None:
                    gaps.append(
                        _CashGap(
                            action="create",
                            kind="payment",
                            category="other_materials_purchase",
                            amount_paisa=amount,
                            source_table="other_product_transactions",
                            source_id=source_id,
                            business_date=business_date,
                            remarks=txn["reference"],
                            occurred_at_utc=txn["occurred_at_utc"],
                            description=(
                                "Missing cash payment 'Other materials"
                                f" purchase' {_fmt(amount)} for purchase"
                                f" #{source_id}."
                            ),
                        )
                    )
            else:
                if self._cash.derived_receipt(
                    conn, "other_product_transactions", source_id
                ) is None:
                    gaps.append(
                        _CashGap(
                            action="create",
                            kind="receipt",
                            category="other_sales",
                            amount_paisa=amount,
                            source_table="other_product_transactions",
                            source_id=source_id,
                            business_date=business_date,
                            remarks=txn["reference"],
                            occurred_at_utc=txn["occurred_at_utc"],
                            description=(
                                f"Missing cash receipt 'Other product sales'"
                                f" {_fmt(amount)} for sale #{source_id}."
                            ),
                        )
                    )
        # (f) settlement 'cash' legs → cash_sales receipts --------------------
        # Normally created inline by _record_settlements_conn; this is the
        # backfill/safety net so the checklist and the writer can never
        # disagree about what "complete" means.
        for leg in conn.execute(
            "SELECT s.id, s.amount_paisa, s.occurred_at_utc,"
            " p.code AS fuel_product_code"
            " FROM sales_settlements s"
            " JOIN fuel_products p ON p.id = s.fuel_product_id"
            " WHERE s.business_date = ? AND s.category = 'cash'"
            " AND s.status = 'posted' AND s.is_reversal = 0",
            (business_date,),
        ).fetchall():
            source_id = int(leg["id"])
            if self._cash.derived_receipt(
                conn, "sales_settlements", source_id
            ) is None:
                amount = int(leg["amount_paisa"])
                gaps.append(
                    _CashGap(
                        action="create",
                        kind="receipt",
                        category="cash_sales",
                        amount_paisa=amount,
                        source_table="sales_settlements",
                        source_id=source_id,
                        business_date=business_date,
                        remarks=(
                            "Cash sales"
                            f" {leg['fuel_product_code']} (settlement)"
                        ),
                        occurred_at_utc=leg["occurred_at_utc"],
                        description=(
                            "Missing cash receipt 'Cash sales'"
                            f" {_fmt(amount)} for settlement cash leg"
                            f" #{source_id}."
                        ),
                    )
                )
        # Stale derived rows: source reversed, cash row still posted ----------
        stale_sources = [
            ("credit_sales", "receipt"),
            ("customer_payments", "receipt"),
            ("expenses", "payment"),
            ("supplier_payments", "payment"),
        ]
        for source_table, kind in stale_sources:
            for source in conn.execute(
                f"SELECT id FROM {source_table}"
                " WHERE business_date = ? AND status = 'reversed'"
                " AND reverses_uuid IS NULL",
                (business_date,),
            ).fetchall():
                source_id = int(source["id"])
                if kind == "receipt":
                    derived = self._cash.derived_receipt(
                        conn, source_table, source_id
                    )
                else:
                    derived = self._cash.derived_payment(
                        conn, source_table, source_id
                    )
                if derived is not None:
                    gaps.append(
                        _CashGap(
                            action="reverse",
                            kind=kind,
                            category=derived["category"],
                            amount_paisa=int(derived["amount_paisa"]),
                            source_table=source_table,
                            source_id=source_id,
                            business_date=business_date,
                            cash_uuid=derived["uuid"],
                            description=(
                                f"Cash {kind} '{derived['category_en']}'"
                                f" {_fmt(int(derived['amount_paisa']))} for"
                                f" {source_table} #{source_id} is stale — the"
                                " source was reversed and the cash row must be"
                                " reversed too."
                            ),
                        )
                    )
        # Stale settlement-leg receipts: the leg was reversed (batch
        # reversal) but its cash_sales receipt is still posted. The ordinary
        # reverse_settlements path reverses these inline; this is the net.
        for leg in conn.execute(
            "SELECT id FROM sales_settlements"
            " WHERE business_date = ? AND category = 'cash'"
            " AND status = 'reversed' AND is_reversal = 0",
            (business_date,),
        ).fetchall():
            source_id = int(leg["id"])
            derived = self._cash.derived_receipt(
                conn, "sales_settlements", source_id
            )
            if derived is not None:
                gaps.append(
                    _CashGap(
                        action="reverse",
                        kind="receipt",
                        category=derived["category"],
                        amount_paisa=int(derived["amount_paisa"]),
                        source_table="sales_settlements",
                        source_id=source_id,
                        business_date=business_date,
                        cash_uuid=derived["uuid"],
                        description=(
                            f"Cash receipt '{derived['category_en']}'"
                            f" {_fmt(int(derived['amount_paisa']))} for"
                            f" settlement cash leg #{source_id} is stale —"
                            " the leg was reversed and the cash row must be"
                            " reversed too."
                        ),
                    )
                )
        return gaps

    def ensure_cash_postings_conn(
        self, conn: sqlite3.Connection, business_date: str, actor: str | None
    ) -> dict:
        """Idempotent backfill of derived cash rows (close flow).

        Pure derivations — no manager judgment. Each creation is guarded by
        the unique source index (a retry never double-posts); derived rows
        whose source was reversed are reversed with reason
        ``'source reversed'``. Returns counts of created/reversed rows.
        """
        who = self.resolve_actor(actor)
        require_writable_day(conn, business_date)
        created_receipts = created_payments = 0
        reversed_receipts = reversed_payments = 0
        for gap in self._derive_cash_gaps_conn(conn, business_date):
            if gap.action == "create":
                cat = self._cash.get_category(conn, gap.kind, gap.category)
                if cat is None or not cat["is_active"]:
                    raise errors.BusinessRuleError(
                        f"Cash category {gap.category!r} is missing or"
                        " inactive; cannot post derived cash rows.",
                        key="err.derived_cash_category_missing",
                        params={"category": gap.category},
                    )
                new_uuid = _uuid.uuid4().hex
                if gap.kind == "receipt":
                    self._cash.insert_receipt(
                        conn,
                        new_uuid,
                        gap.business_date,
                        int(cat["id"]),
                        gap.amount_paisa,
                        gap.source_table,
                        gap.source_id,
                        gap.customer_id,
                        gap.remarks,
                        None,
                        gap.occurred_at_utc or timecalc.utc_now_iso(),
                        who,
                    )
                    created_receipts += 1
                else:
                    self._cash.insert_payment(
                        conn,
                        new_uuid,
                        gap.business_date,
                        int(cat["id"]),
                        gap.amount_paisa,
                        gap.payee,
                        gap.source_table,
                        gap.source_id,
                        gap.remarks,
                        None,
                        gap.occurred_at_utc or timecalc.utc_now_iso(),
                        who,
                    )
                    created_payments += 1
            else:
                stamp = timecalc.utc_now_iso()
                if gap.kind == "receipt":
                    row = self._cash.get_receipt_by_uuid(conn, gap.cash_uuid)
                    self._reverse_receipt_conn(
                        conn, row, "source reversed", who, stamp
                    )
                    reversed_receipts += 1
                else:
                    row = self._cash.get_payment_by_uuid(conn, gap.cash_uuid)
                    self._reverse_payment_conn(
                        conn, row, "source reversed", who, stamp
                    )
                    reversed_payments += 1
        return {
            "receipts_created": created_receipts,
            "payments_created": created_payments,
            "receipts_reversed": reversed_receipts,
            "payments_reversed": reversed_payments,
        }

    def missing_cash_postings(self, business_date: str) -> list[dict]:
        """Derived cash postings the backfill *would* create (read-only).

        Same derivation as :meth:`ensure_cash_postings_conn` — the close
        checklist reports these as fail items without writing anything.
        """
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            return self._missing_cash_postings_conn(conn, business_date)

    def missing_cash_postings_conn(
        self, conn: sqlite3.Connection, business_date: str
    ) -> list[dict]:
        """Conn-level ``missing_cash_postings`` for the close flow.

        Runs on a caller-owned connection so the checklist sees the same
        uncommitted backfill rows that ``ensure_cash_postings_conn`` wrote
        inside the close transaction (a fresh connection would not).
        """
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        return self._missing_cash_postings_conn(conn, business_date)

    def _missing_cash_postings_conn(
        self, conn: sqlite3.Connection, business_date: str
    ) -> list[dict]:
        """The shared derivation behind both public variants."""
        gaps = self._derive_cash_gaps_conn(conn, business_date)
        return [
            {
                "source_table": gap.source_table,
                "source_id": gap.source_id,
                "kind": gap.kind,
                "category": gap.category,
                "amount_paisa": gap.amount_paisa,
                "description": gap.description,
            }
            for gap in gaps
        ]

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def cash_position(self, business_date: str) -> CashPosition:
        """The day's cash position from ``v_daily_cash_position``."""
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            row = self._cash.daily_cash_position(conn, business_date)
            if row is None:
                raise errors.NotFoundError(
                    f"No business day {business_date}.",
                    key="err.no_business_day_short",
                    params={"day_date": business_date},
                )
            physical = row["physical_cash_paisa"]
            difference = (
                int(row["difference_paisa"])
                if row["difference_paisa"] is not None
                else None
            )
            return CashPosition(
                business_date=business_date,
                opening_cash_paisa=int(row["opening_cash_paisa"]),
                receipts_paisa=int(row["receipts_paisa"]),
                payments_paisa=int(row["payments_paisa"]),
                expected_paisa=int(row["expected_paisa"]),
                physical_cash_paisa=int(physical)
                if physical is not None
                else None,
                difference_paisa=difference,
                shortage_paisa=max(0, -difference) if difference is not None else 0,
                surplus_paisa=max(0, difference) if difference is not None else 0,
            )

    def list_receipts(self, business_date: str) -> list[CashReceiptInfo]:
        """The day's cash receipts, chronological."""
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            rows = self._cash.list_receipts(conn, business_date)
        return [_to_receipt_info(row) for row in rows]

    def list_payments(self, business_date: str) -> list[CashPaymentInfo]:
        """The day's cash payments, chronological."""
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            rows = self._cash.list_payments(conn, business_date)
        return [_to_payment_info(row) for row in rows]

    def other_income_total(self, business_date: str) -> dict:
        """The day's other income, net of reversals.

        "Other income" = ``credit_recovery`` (old debt recovered — the
        client's "recovered from whom fuel was given on borrow") plus
        ``other_receipt``. Both are cash receipts, never revenue (design
        §6: recovering old debt is a receipt, not a sale).

        Returns ``{"credit_recovery_paisa", "other_receipt_paisa",
        "total_paisa", "recoveries"}`` where ``recoveries`` lists each
        debt-recovery receipt with the customer it came from.
        """
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            rows = self._cash.list_receipts(conn, business_date)
        infos = [_to_receipt_info(row) for row in rows]
        recovery = [
            info for info in infos if info.category == "credit_recovery"
        ]
        other = [info for info in infos if info.category == "other_receipt"]
        recovery_paisa = sum(i.effective_paisa for i in recovery)
        other_paisa = sum(i.effective_paisa for i in other)
        return {
            "credit_recovery_paisa": recovery_paisa,
            "other_receipt_paisa": other_paisa,
            "total_paisa": recovery_paisa + other_paisa,
            "recoveries": [
                {
                    "customer_name": i.customer_name,
                    "amount_paisa": i.effective_paisa,
                    "occurred_at_utc": i.occurred_at_utc,
                    "remarks": i.remarks,
                }
                for i in recovery
            ],
        }

    def cashbook_entries(self, business_date: str) -> list[dict]:
        """The day's cash-book rows from ``v_cashbook`` (signed, chronological)."""
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            rows = self._cash.cashbook_entries(conn, business_date)
        return [
            {
                "business_date": row["business_date"],
                "occurred_at_utc": row["occurred_at_utc"],
                "kind": row["kind"],
                "category": row["category"],
                "category_en": row["category_en"],
                "customer_id": row["customer_id"],
                "payee": row["payee"],
                "signed_paisa": int(row["signed_paisa"]),
                "remarks": row["remarks"],
                "uuid": row["uuid"],
                "status": row["status"],
            }
            for row in rows
        ]

    def list_settlements(self, business_date: str) -> list[SettlementLeg]:
        """All settlement rows written for a day (any status)."""
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            rows = self._cash.list_settlements_by_date(conn, business_date)
        return [_to_settlement_leg(row) for row in rows]

    # ------------------------------------------------------------------
    # categories (master data)
    # ------------------------------------------------------------------
    def list_categories(self, kind: str) -> list[CashCategoryInfo]:
        """Active categories of a kind (``'receipt'`` | ``'payment'``)."""
        if kind not in ("receipt", "payment"):
            raise errors.ValidationError(
                f"Unknown cash category kind: {kind!r}.",
                key="err.unknown_category_kind",
                params={"kind": kind},
            )
        with self.read_connection() as conn:
            rows = self._cash.list_categories(conn, kind)
        return [_to_category_info(row) for row in rows]

    def add_category(
        self,
        kind: str,
        name: str,
        name_en: str,
        name_ur: str | None = None,
        *,
        actor: str | None = None,
    ) -> int:
        """Add a manager-defined cash category. Returns its id."""
        if kind not in ("receipt", "payment"):
            raise errors.ValidationError(
                f"Unknown cash category kind: {kind!r}.",
                key="err.unknown_category_kind",
                params={"kind": kind},
            )
        machine = (name or "").strip()
        english = (name_en or "").strip()
        if not machine:
            raise errors.ValidationError(
                "Category name must not be empty.",
                key="err.category_name_empty",
            )
        if not english:
            raise errors.ValidationError(
                "Category English label must not be empty.",
                key="err.category_label_empty",
            )
        who = self.resolve_actor(actor)
        with self.transaction() as conn:
            if self._cash.get_category(conn, kind, machine) is not None:
                raise errors.DuplicateError(
                    f"A {kind} category named {machine!r} already exists.",
                    key="err.category_exists",
                    params={"kind": kind, "name": machine},
                )
            return self._cash.insert_category(
                conn, kind, machine, english,
                (name_ur or "").strip() or None, who,
            )

    def set_category_active(
        self, category_id: int, active: bool, *, actor: str | None = None
    ) -> None:
        """Activate/deactivate a category.

        Refuses to deactivate a category already referenced by cash rows
        (history must keep resolving). Unknown id → ``NotFoundError``.
        """
        with self.transaction() as conn:
            row = self._cash.get_category_by_id(conn, int(category_id))
            if row is None:
                raise errors.NotFoundError(
                    f"Unknown cash category: {category_id!r}.",
                    key="err.unknown_cash_category",
                    params={"ref": category_id},
                )
            if not active and self._cash.category_in_use(conn, int(row["id"])):
                raise errors.BusinessRuleError(
                    f"Category {row['name']!r} is referenced by cash-book rows"
                    " and cannot be deactivated.",
                    key="err.category_in_use",
                    params={"name": row["name"]},
                )
            self._cash.set_category_active(conn, int(row["id"]), bool(active))
