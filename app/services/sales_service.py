"""Meter-derived sales: intervals, pricing, stock posting, reconciliation (Phase 3).

Design plan §6: the meter is the source of truth. Each nozzle's validated
reading chain — previous closing, intraday segments, closing — yields sale
intervals; each interval is priced by the ``price_history`` row effective at
its ``occurred_at_utc`` (§11.2). Posting writes one ``'sale'`` movement per
interval (idempotent via ``source_uuid``); trigger F2 blocks any sale that
would drive tracked stock negative, rolling the whole post back.
"""

from __future__ import annotations

import sqlite3
import uuid as _uuid

from app.calculations import money as calc
from app.calculations import time as timecalc
from app.models.price import DaySales, SaleInterval
from app.repositories.inventory_repository import InventoryRepository
from app.repositories.nozzle_repository import NozzleRepository
from app.services import errors
from app.services._base import ServiceBase, require_writable_day
from app.services.price_service import PriceService, resolve_fuel_product


def sale_movement_source_uuid(
    business_date: str, nozzle_id: int, seq: int
) -> str:
    """Idempotency key for one posted sale interval.

    Shared with the close checklist so "posted?" checks and posting agree.
    """
    return f"nozzle-sale:{business_date}:{nozzle_id}:{seq}"


def sale_correction_prefix(
    business_date: str, nozzle_id: int, seq: int
) -> str:
    """Prefix for correction adjustments of one posted sale interval.

    When a reading correction changes an interval AFTER its sale movement
    was posted, the original movement is never edited or deleted
    (append-only ledger). Instead an ``adjustment`` movement carrying the
    delta is appended. Correction rows are numbered
    ``{prefix}1``, ``{prefix}2``, … so re-running the posting stays
    idempotent: the net posted quantity is compared against the effective
    interval, never just the existence of the original row.
    """
    return f"nozzle-sale-corr:{business_date}:{nozzle_id}:{seq}:"


def _is_postable(interval: SaleInterval) -> bool:
    """An interval is postable when its quantity is known and non-zero.

    Zero-liter intervals post nothing: the ledger's CHECK constraint
    forbids ``'sale'`` movements with ``quantity_ml = 0``, and a zero
    movement would be meaningless.
    """
    return interval.quantity_ml is not None and interval.quantity_ml != 0


class SalesService(ServiceBase):
    """Derives sales from meter readings and posts them to stock."""

    def __init__(self, db_manager, actor: str | None = None) -> None:
        super().__init__(db_manager, actor)
        self._nozzles = NozzleRepository()
        self._inventory = InventoryRepository()
        self._prices = PriceService(db_manager, actor)

    # ------------------------------------------------------------------
    # intervals & day sales
    # ------------------------------------------------------------------
    def day_intervals(self, business_date: str) -> list[SaleInterval]:
        """Every nozzle's priced sale intervals for a business day."""
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            return self._day_intervals_conn(conn, business_date)

    def _day_intervals_conn(
        self, conn: sqlite3.Connection, business_date: str
    ) -> list[SaleInterval]:
        intervals: list[SaleInterval] = []
        for nozzle in self._nozzles.list_nozzles(conn, active_only=True):
            nozzle_id = int(nozzle["id"])
            chain: list = []
            previous = self._nozzles.get_previous_effective_reading(
                conn, nozzle_id, business_date
            )
            if previous is not None:
                chain.append(previous)
            chain.extend(self._nozzles.list_segments(conn, nozzle_id, business_date))
            current = self._nozzles.get_effective_reading(
                conn, nozzle_id, business_date
            )
            if current is not None:
                chain.append(current)
            events = self._nozzles.list_meter_events(conn, nozzle_id)
            for seq, (start, end) in enumerate(zip(chain, chain[1:])):
                start_ml = int(start["reading_ml"])
                end_ml = int(end["reading_ml"])
                quantity_ml = self._interval_quantity(
                    start, end, start_ml, end_ml, events
                )
                unit_price_10k: int | None = None
                amount_paisa: int | None = None
                if quantity_ml is not None:
                    # Priced strictly before the pair-end timestamp and never
                    # after the business day's end: a price change takes
                    # effect *after* its authorizing snapshot (§11.2), so the
                    # interval ending at the change moment still belongs to
                    # the old price — never retrospective.
                    price = self._prices._get_effective_strict_conn(
                        conn,
                        int(nozzle["fuel_product_id"]),
                        end["occurred_at_utc"],
                        business_date,
                    )
                    if price is not None:
                        unit_price_10k = price.unit_price_10k
                        amount_paisa = calc.amount_paisa_for(
                            quantity_ml, unit_price_10k
                        )
                intervals.append(
                    SaleInterval(
                        nozzle_id=nozzle_id,
                        nozzle_code=nozzle["code"],
                        fuel_product_code=nozzle["fuel_product_code"],
                        tank_id=(
                            int(nozzle["tank_id"])
                            if nozzle["tank_id"] is not None
                            else None
                        ),
                        business_date=business_date,
                        seq=seq,
                        start_reading_ml=start_ml,
                        end_reading_ml=end_ml,
                        occurred_at_utc=end["occurred_at_utc"],
                        quantity_ml=quantity_ml,
                        unit_price_10k=unit_price_10k,
                        amount_paisa=amount_paisa,
                    )
                )
        return intervals

    @staticmethod
    def _interval_quantity(
        start, end, start_ml: int, end_ml: int, events: list
    ) -> int | None:
        """Liters for one reading pair, or None when not computable.

        Forward intervals are exact. A backwards interval is priced only
        when a ``rollover`` meter event with a documented wrap value covers
        the pair: ``(wrap - start) + end``. Anything else backwards is
        unknown (§11.1 — a reset/replacement is never a negative sale).
        """
        if end_ml >= start_ml:
            return end_ml - start_ml
        for event in events:
            wrap = event["wrap_value_ml"]
            if (
                event["event_type"] == "rollover"
                and wrap is not None
                and int(wrap) > start_ml
                and start["occurred_at_utc"]
                <= event["occurred_at_utc"]
                <= end["occurred_at_utc"]
            ):
                return (int(wrap) - start_ml) + end_ml
        return None

    def day_sales(self, business_date: str) -> DaySales:
        """A day's meter-derived sales with per-product aggregates."""
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            return self._day_sales_conn(conn, business_date)

    def _day_sales_conn(
        self, conn: sqlite3.Connection, business_date: str
    ) -> DaySales:
        intervals = self._day_intervals_conn(conn, business_date)
        liters: dict[str, int] = {}
        revenue: dict[str, int] = {}
        for interval in intervals:
            if interval.quantity_ml is None:
                continue
            code = interval.fuel_product_code
            liters[code] = liters.get(code, 0) + interval.quantity_ml
            if interval.amount_paisa is not None:
                revenue[code] = revenue.get(code, 0) + interval.amount_paisa
        return DaySales(
            business_date=business_date,
            intervals=intervals,
            liters_by_product_ml=liters,
            revenue_by_product_paisa=revenue,
        )

    # ------------------------------------------------------------------
    # posting
    # ------------------------------------------------------------------
    def post_day_sales(
        self, business_date: str, *, actor: str | None = None
    ) -> dict:
        """Post the day's known sale intervals to stock, idempotently.

        One ``'sale'`` movement per interval (negative quantity), keyed by
        ``nozzle-sale:{date}:{nozzle_id}:{seq}``. Re-running posts nothing
        new. Zero-liter intervals post nothing (the ledger forbids a
        ``'sale'`` movement with ``quantity_ml = 0``) and are reported under
        ``skipped_zero_qty``. If any sale would drive tracked stock negative,
        trigger F2 aborts and the WHOLE post rolls back (translated to
        BusinessRuleError) — post the missing delivery/adjustment first.

        Correction-aware: when a reading correction changes an interval
        AFTER its sale movement was posted, the net posted quantity
        (original sale + any prior correction adjustments) is compared with
        the effective interval. A divergence posts a single ``adjustment``
        movement for the delta — the original row is never edited. If the
        interval's quantity became unknown (e.g. reset without wrap value)
        or zero, the posted sale is fully reversed by an adjustment. This
        keeps the ledger and the closing record in agreement even when
        corrections land between posting and close.

        Returns ``{"posted": n, "already_posted": m, "corrected": k,
        "skipped_unknown_qty": [nozzle codes],
        "skipped_zero_qty": [nozzle codes]}``.
        """
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        who = self.resolve_actor(actor)
        with self.transaction() as conn:
            return self._post_day_sales(conn, business_date, who)

    def _post_day_sales_conn(
        self, conn: sqlite3.Connection, business_date: str, actor: str | None
    ) -> dict:
        """Posting on a caller-owned connection (used by ``close_day``)."""
        return self._post_day_sales(conn, business_date, self.resolve_actor(actor))

    def _post_day_sales(
        self, conn: sqlite3.Connection, business_date: str, who: str
    ) -> dict:
        require_writable_day(conn, business_date)
        intervals = self._day_intervals_conn(conn, business_date)
        product_ids: dict[str, int] = {}
        posted = 0
        already_posted = 0
        corrected = 0
        skipped: list[str] = []
        skipped_zero: list[str] = []
        for interval in intervals:
            source_uuid = sale_movement_source_uuid(
                business_date, interval.nozzle_id, interval.seq
            )
            corr_prefix = sale_correction_prefix(
                business_date, interval.nozzle_id, interval.seq
            )
            existing = self._inventory.get_movement_by_source_uuid(
                conn, source_uuid
            )
            if existing is not None:
                # A sale movement is already posted for this interval.
                # Compare the NET posted quantity (original + corrections)
                # with the effective interval — a reading correction may
                # have changed the interval after posting.
                net_posted_ml = int(existing["quantity_ml"]) + (
                    self._inventory.sum_corrections_for_interval(conn, corr_prefix)
                )
                target_ml = (
                    -interval.quantity_ml
                    if interval.quantity_ml
                    else None
                )
                if target_ml is None:
                    # Quantity became unknown or zero: fully reverse the
                    # posted sale so stock stops claiming it. The close
                    # checklist warns about the unknown interval.
                    if net_posted_ml == 0:
                        already_posted += 1
                        continue
                    delta_ml = -net_posted_ml
                    reason = (
                        f"Full reversal of sale movement {existing['uuid']}:"
                        f" interval quantity became"
                        f" {'unknown' if interval.quantity_ml is None else 'zero'}"
                        f" after meter correction"
                    )
                elif net_posted_ml == target_ml:
                    already_posted += 1
                    continue
                else:
                    delta_ml = target_ml - net_posted_ml
                    reason = (
                        f"Meter correction: sale movement {existing['uuid']}"
                        f" posted {net_posted_ml} ml net; effective interval"
                        f" is {target_ml} ml; adjusting by {delta_ml} ml"
                    )
                corr_n = (
                    self._inventory.count_corrections_for_interval(
                        conn, corr_prefix
                    )
                    + 1
                )
                self._inventory.insert_movement(
                    conn,
                    _uuid.uuid4().hex,
                    business_date,
                    int(existing["fuel_product_id"]),
                    int(existing["tank_id"]),
                    "adjustment",
                    delta_ml,
                    "nozzle_sale_intervals",
                    None,
                    f"{corr_prefix}{corr_n}",
                    interval.occurred_at_utc,
                    reason,
                    who,
                )
                corrected += 1
                if interval.nozzle_code not in skipped and target_ml is None:
                    skipped.append(interval.nozzle_code)
                continue
            if interval.quantity_ml is None:
                if interval.nozzle_code not in skipped:
                    skipped.append(interval.nozzle_code)
                continue
            if interval.quantity_ml == 0:
                # No movement for a zero sale: the ledger CHECK forbids
                # 'sale' movements with quantity_ml = 0, and a zero
                # movement would be meaningless anyway.
                if interval.nozzle_code not in skipped_zero:
                    skipped_zero.append(interval.nozzle_code)
                continue
            if interval.tank_id is None:
                raise errors.BusinessRuleError(
                    f"Nozzle {interval.nozzle_code} is not wired to a tank;"
                    " sales cannot be posted to stock.",
                    key="err.nozzle_not_wired_to_tank",
                    params={"code": interval.nozzle_code},
                )
            code = interval.fuel_product_code
            if code not in product_ids:
                product_ids[code] = int(resolve_fuel_product(conn, code)["id"])
            self._inventory.insert_movement(
                conn,
                _uuid.uuid4().hex,
                business_date,
                product_ids[code],
                interval.tank_id,
                "sale",
                -interval.quantity_ml,
                "nozzle_sale_intervals",
                None,
                source_uuid,
                interval.occurred_at_utc,
                f"Nozzle {interval.nozzle_code} {business_date}",
                who,
            )
            posted += 1
        return {
            "posted": posted,
            "already_posted": already_posted,
            "corrected": corrected,
            "skipped_unknown_qty": skipped,
            "skipped_zero_qty": skipped_zero,
        }

    def diverged_intervals(self, business_date: str) -> list[dict]:
        """Intervals whose net posted sale differs from the effective interval.

        Read-only. A divergence means a reading correction landed after the
        sale movement was posted; the next :meth:`post_day_sales` (including
        the safety-net post inside ``close_day``) appends an audited
        ``adjustment`` for the difference. Each entry carries
        ``nozzle_code``, ``seq``, ``posted_ml`` (net posted, negative) and
        ``effective_ml`` (target, negative) — ``effective_ml`` is None when
        the quantity became unknown/zero and a full reversal is pending.
        """
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r} (expected YYYY-MM-DD)."
            )
        out: list[dict] = []
        with self.read_connection() as conn:
            for interval in self._day_intervals_conn(conn, business_date):
                source_uuid = sale_movement_source_uuid(
                    business_date, interval.nozzle_id, interval.seq
                )
                existing = self._inventory.get_movement_by_source_uuid(
                    conn, source_uuid
                )
                if existing is None:
                    continue
                corr_prefix = sale_correction_prefix(
                    business_date, interval.nozzle_id, interval.seq
                )
                net_ml = int(existing["quantity_ml"]) + (
                    self._inventory.sum_corrections_for_interval(
                        conn, corr_prefix
                    )
                )
                target_ml = (
                    -interval.quantity_ml if interval.quantity_ml else None
                )
                if target_ml != net_ml:
                    out.append(
                        {
                            "nozzle_code": interval.nozzle_code,
                            "seq": interval.seq,
                            "posted_ml": net_ml,
                            "effective_ml": target_ml,
                        }
                    )
        return out

    # ------------------------------------------------------------------
    # reconciliation
    # ------------------------------------------------------------------
    def reconcile_day(self, business_date: str) -> dict:
        """Meter revenue vs posted sales for a day (design plan §6).

        The identity ``meter_revenue == posted_sales_amounts`` must hold for
        a clean close: a posted sale movement always carries the interval's
        priced amount. The new-receivables leg is real (Σ of the day's
        posted credit tickets, §6). The settlement leg is real since Phase 5:
        ``sales_settlements`` classifies meter revenue into cash / bank /
        paid_now_on_ticket / other, and the fundamental identity
        ``meter_revenue == settled_against_today_sales + new_receivables``
        is checked per product. Old-debt recovery never appears here — it
        is a cash receipt, not revenue.
        """
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            sales = self._day_sales_conn(conn, business_date)
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            # New receivables (§6): Σ signed (gross − paid_now) over the
            # day's tickets. Corrected rule (migration 011, mirroring 009):
            # a reversed original keeps its posted contribution (the flip is
            # a marker); reversal rows negate. Net effect of voiding: 0.
            if "credit_sales" in tables:
                new_receivables_paisa = int(
                    conn.execute(
                        "SELECT COALESCE(SUM(CASE"
                        "  WHEN reverses_uuid IS NOT NULL"
                        "   THEN -(gross_amount_paisa - paid_now_paisa)"
                        "  ELSE (gross_amount_paisa - paid_now_paisa)"
                        " END), 0)"
                        " FROM credit_sales WHERE business_date = ?",
                        (business_date,),
                    ).fetchone()[0]
                )
                new_receivables_note = (
                    "Σ (gross − paid-now) over the day's posted credit"
                    " tickets; reversal rows negate."
                )
            else:
                new_receivables_paisa = 0
                new_receivables_note = (
                    "Customer credit tickets arrive with the credit module"
                    " (Phase 4)."
                )
            posted: dict[str, int] = {}
            unposted: list[dict] = []
            unpriced: list[str] = []
            for interval in sales.intervals:
                if not _is_postable(interval):
                    continue  # unknown qty is warned elsewhere; 0 L posts nothing
                if interval.amount_paisa is None:
                    if interval.nozzle_code not in unpriced:
                        unpriced.append(interval.nozzle_code)
                    continue
                source_uuid = sale_movement_source_uuid(
                    business_date, interval.nozzle_id, interval.seq
                )
                if self._inventory.movement_exists(conn, source_uuid):
                    code = interval.fuel_product_code
                    posted[code] = posted.get(code, 0) + interval.amount_paisa
                else:
                    unposted.append(
                        {
                            "nozzle_code": interval.nozzle_code,
                            "seq": interval.seq,
                            "amount_paisa": interval.amount_paisa,
                        }
                    )
            meter = sales.revenue_by_product_paisa
            products = [
                {
                    "fuel_product_code": code,
                    "meter_revenue_paisa": meter.get(code, 0),
                    "posted_revenue_paisa": posted.get(code, 0),
                    "identity_ok": meter.get(code, 0) == posted.get(code, 0),
                }
                for code in meter
            ]
            identity_ok = all(row["identity_ok"] for row in products)

            # Settlement leg (Phase 5, real): effective settlement amounts
            # per product per category from v_effective_settlements. The
            # 'receivable' category is the ticket classification leg — it is
            # reported separately and excluded from "settled" so the
            # fundamental identity stays
            # meter_revenue == settled + new_receivables.
            settlements: dict[str, dict[str, int]] = {}
            if "v_effective_settlements" in {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'view'"
                )
            }:
                for row in conn.execute(
                    "SELECT fuel_product_code, category,"
                    " SUM(effective_paisa) AS total_paisa"
                    " FROM v_effective_settlements"
                    " WHERE business_date = ?"
                    " GROUP BY fuel_product_code, category",
                    (business_date,),
                ):
                    settlements.setdefault(row["fuel_product_code"], {})[
                        row["category"]
                    ] = int(row["total_paisa"] or 0)
            settlement_products = []
            settlement_identity_ok = True
            total_settled = 0
            meter_total = sum(meter.values())
            for code in meter:
                cats = settlements.get(code, {})
                cash = cats.get("cash", 0)
                bank = cats.get("bank", 0)
                paid_now = cats.get("paid_now_on_ticket", 0)
                other = cats.get("other", 0)
                receivable_leg = cats.get("receivable", 0)
                settled = cash + bank + paid_now + other
                total_settled += settled
                settlement_products.append(
                    {
                        "fuel_product_code": code,
                        "cash_paisa": cash,
                        "bank_paisa": bank,
                        "paid_now_on_ticket_paisa": paid_now,
                        "other_paisa": other,
                        "receivable_leg_paisa": receivable_leg,
                        "settled_paisa": settled,
                    }
                )
            # Per-product new receivables would need a ticket→product split;
            # the identity is checked on the day total, where it is exact:
            # Σ meter == Σ settled + Σ new receivables.
            settlement_identity_ok = (
                total_settled + new_receivables_paisa == meter_total
            )
            settlements_recorded = bool(settlements)
            settlements_note = (
                "Σ effective settlement legs (cash + bank + paid-now + other);"
                " the 'receivable' leg classifies new credit, it is not"
                " settled cash. Old-debt recovery never appears here."
                if settlements_recorded
                else "No settlements recorded for this day yet."
            )
        return {
            "business_date": business_date,
            "products": products,
            "meter_revenue_by_product_paisa": dict(meter),
            "posted_sales_by_product_paisa": posted,
            "identity_ok": identity_ok,
            "unposted_intervals": unposted,
            "unpriced_intervals": unpriced,
            "settlements_recorded": settlements_recorded,
            "settlements_by_product": settlement_products,
            "settlements_recorded_paisa": total_settled,
            "settlements_note": settlements_note,
            "new_receivables_paisa": new_receivables_paisa,
            "new_receivables_note": new_receivables_note,
            "settlement_identity_ok": settlement_identity_ok,
            "settlement_identity_note": (
                "meter revenue == settled against today's sales + new"
                " receivables (day total)"
            ),
        }

    def credit_liters_guard(self, business_date: str) -> dict:
        """Cross-check metered liters against credit-ticket liters.

        Phase 3 has no credit module: reports ``checked: False`` honestly
        instead of a fake pass. When the Phase-4 ``credit_sales`` table
        exists, per-product credit liters are compared to metered liters.
        """
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "credit_sales" not in tables:
                return {
                    "checked": False,
                    "reason": (
                        "customer credit module (Phase 4) not installed"
                    ),
                }
            try:
                return self._credit_guard_conn(conn, business_date)
            except sqlite3.Error:
                return {
                    "checked": False,
                    "reason": (
                        "credit_sales table present but its schema is not"
                        " recognized"
                    ),
                }

    def _credit_guard_conn(
        self, conn: sqlite3.Connection, business_date: str
    ) -> dict:
        """Per-product credit liters vs metered liters for a business day.

        Signed credit liters: each line's ``quantity_ml`` counts +1 on a
        normal posted ticket and −1 on a reversal ticket row
        (``reverses_uuid IS NOT NULL``); tickets whose status flipped to
        ``'reversed'`` contribute nothing (the reversal row carries the
        negation). Only ``status='posted'`` tickets count.
        """
        credit_rows = conn.execute(
            "SELECT l.fuel_product_id,"
            "       COALESCE(SUM(l.quantity_ml"
            "                    * CASE WHEN cs.reverses_uuid IS NULL"
            "                         THEN 1 ELSE -1 END), 0)"
            "  FROM credit_sale_lines l"
            "  JOIN credit_sales cs ON cs.id = l.credit_sale_id"
            " WHERE cs.business_date = ? AND cs.status = 'posted'"
            " GROUP BY l.fuel_product_id",
            (business_date,),
        ).fetchall()
        products = {
            int(row["id"]): row["code"]
            for row in self._prices._list_products_conn(conn, active_only=False)
        }
        metered = self._day_sales_conn(conn, business_date).liters_by_product_ml
        warnings: list[str] = []
        warning_segments: list[tuple[str, dict]] = []
        for row in credit_rows:
            code = products.get(int(row[0]), f"product#{row[0]}")
            credit_ml = int(row[1])
            metered_ml = metered.get(code, 0)
            if credit_ml > metered_ml:
                warnings.append(
                    f"{code}: credit tickets {credit_ml / 1000:,.3f} L exceed"
                    f" metered {metered_ml / 1000:,.3f} L."
                )
                warning_segments.append(
                    (
                        "close.detail.credit_liters_product",
                        {
                            "product": code,
                            "credit_ml": credit_ml,
                            "metered_ml": metered_ml,
                        },
                    )
                )
        return {"checked": True, "warnings": warnings, "warning_segments": warning_segments}
