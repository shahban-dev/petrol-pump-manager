"""Business-day lifecycle: open, guided close, authorized reopen (Phase 2).

Master prompt §10 / design plan §10:

* at most one open day at a time (also enforced by the H2 trigger);
* a new day opens only after the previous recorded day is closed
  (date gaps need an explicit, audited reason — the administrative
  recovery path);
* opening cash is carried from the previous *closing record*
  (``business_day_closings.closing_cash_paisa``); every carry-forward
  value is stored in ``opening_balances`` together with the source
  closing it came from — never typed in, never duplicated;
* closing runs the §19 checklist: blocking errors stop the close,
  warnings stay visible; the immutable closing record (with a revision
  number) and the day-status flip commit in ONE transaction;
* reopening needs a manager reason, writes an audited event, and the
  next close creates a NEW revision — the old one is preserved.

Non-operating days (§11.6) carry balances forward unchanged and accept no
readings; they are terminal (never "closed").
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from app import config
from app.calculations import time as timecalc
from app.localization import render_en
from app.models.business_day import (
    BusinessDay,
    CloseCheckItem,
    CloseValidationReport,
    ClosingRecord,
    OpeningBalance,
)
from app.repositories.nozzle_repository import NozzleRepository
from app.services import errors
from app.services._base import ServiceBase
from app.services.accounting_service import AccountingService
from app.services.cashbook_service import CashbookService
from app.services.customer_credit_service import CustomerCreditService
from app.services.delivery_service import DeliveryService
from app.services.inventory_service import InventoryService
from app.services.nozzle_service import NozzleService
from app.services.payment_service import PaymentService
from app.services.price_service import PriceService
from app.services.sales_service import (
    SalesService,
    _is_postable,
    sale_movement_source_uuid,
)

#: Checklist items with no supporting module yet. They are shown as "not
#: applicable in this phase" — never as a pass, never blocking — so the
#: close screen is honest about what it does and does not verify.
#: (Phase 5 enforces the settlement ledger and the cash book for real, so
#: this tuple is now empty; it is kept for documentation.)
_NA_CHECKS: tuple[tuple[str, str], ...] = ()


class BusinessDayService(ServiceBase):
    """Opens, validates, closes and reopens business days."""

    def __init__(self, db_manager, actor: str | None = None) -> None:
        super().__init__(db_manager, actor)
        self._nozzle_service = NozzleService(db_manager, actor)
        self._nozzles = NozzleRepository()
        self._price_service = PriceService(db_manager, actor)
        self._delivery_service = DeliveryService(db_manager, actor)
        self._inventory_service = InventoryService(db_manager, actor)
        self._sales_service = SalesService(db_manager, actor)
        self._credit_service = CustomerCreditService(db_manager, actor)
        self._payment_service = PaymentService(db_manager, actor)
        self._cashbook_service = CashbookService(db_manager, actor)
        self._accounting_service = AccountingService(db_manager, actor)

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def get_day(self, business_date: str) -> BusinessDay | None:
        """The day row, or None."""
        with self.read_connection() as conn:
            row = self._days.get(conn, business_date)
        return _to_business_day(row) if row else None

    def get_open_day(self) -> BusinessDay | None:
        """The currently open (or reopened) day, or None."""
        with self.read_connection() as conn:
            row = self._days.get_open_day(conn)
        return _to_business_day(row) if row else None

    def opening_balances(self, business_date: str) -> list[OpeningBalance]:
        """Carry-forward values for a day, each with its source closing."""
        with self.read_connection() as conn:
            rows = self._days.get_opening_balances(conn, business_date)
        return [_to_opening_balance(row) for row in rows]

    def closings(self, business_date: str) -> list[ClosingRecord]:
        """All closing revisions for a date, oldest first."""
        with self.read_connection() as conn:
            rows = self._days.list_closings(conn, business_date)
        return [_to_closing_record(row) for row in rows]

    def day_events(self, business_date: str) -> list[dict]:
        """Lifecycle events (open/close/reopen/note) for a date."""
        with self.read_connection() as conn:
            rows = self._days.list_events(conn, business_date)
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # open
    # ------------------------------------------------------------------
    def open_day(
        self,
        business_date: str,
        *,
        opening_cash_paisa: int | None = None,
        non_operating: bool = False,
        gap_reason: str | None = None,
        actor: str | None = None,
    ) -> BusinessDay:
        """Open a business day, carrying balances forward from the previous
        closing record.

        * First day ever: *opening_cash_paisa* is required — the manager
          enters the initial cash once (§11.5); it is stored, not computed.
        * Later days: opening cash comes from the latest closing record's
          counted cash; the source (date + revision) is recorded.
        * The new date must follow the latest recorded day; a gap needs
          *gap_reason* (audited recovery path).
        * Re-opening an already-open day is idempotent: no duplicate rows.

        Raises:
            ValidationError: bad date, date not after latest day, gap
                without reason, first day without opening cash, previous
                day not closed.
            BusinessRuleError: day already closed / non-operating, or
                another day is open.
        """
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r} (expected YYYY-MM-DD).",
                key="err.invalid_business_date_format",
                params={"day_date": business_date},
            )
        if opening_cash_paisa is not None and (
            not isinstance(opening_cash_paisa, int)
            or isinstance(opening_cash_paisa, bool)
            or opening_cash_paisa < 0
        ):
            raise errors.ValidationError(
                "Opening cash must be a non-negative integer number of paisa.",
                key="err.opening_cash_paisa",
            )
        who = self.resolve_actor(actor)

        with self.read_connection() as conn:
            existing = self._days.get(conn, business_date)
            latest = self._days.latest_date(conn)

        if existing is not None:
            return self._ensure_open_artifacts(existing, who)

        if latest is not None and business_date <= latest:
            raise errors.ValidationError(
                f"Business day {business_date} is not after the latest recorded"
                f" day ({latest}). Days are opened in date order.",
                key="err.day_not_after_latest",
                params={"day_date": business_date, "latest_date": latest},
            )

        # Date-gap recovery path: consecutive days are the norm; a gap is
        # allowed only with an explicit, audited reason.
        clean_gap_reason = (gap_reason or "").strip() or None
        prev_closing_revision: int | None = None
        if latest is not None:
            expected = (date.fromisoformat(latest) + timedelta(days=1)).isoformat()
            if business_date != expected and clean_gap_reason is None:
                raise errors.ValidationError(
                    f"There is a gap between {latest} and {business_date}. Open"
                    " the missing day(s) first (as non-operating if the station"
                    " was closed), or provide a reason for the gap.",
                    key="err.date_gap",
                    params={"latest_date": latest, "day_date": business_date},
                )
            with self.read_connection() as conn:
                prev_row = self._days.get(conn, latest)
            if prev_row is not None and prev_row["status"] not in (
                "closed",
                "non_operating",
            ):
                # Unreachable while the H2 trigger stands (it forbids a second
                # open day), but the message is clearer than the trigger's.
                raise errors.BusinessRuleError(
                    f"The previous day ({latest}) is {prev_row['status']}; close"
                    " it before opening a new day.",
                    key="err.previous_day_not_closed",
                    params={
                        "latest_date": latest,
                        "status": prev_row["status"],
                    },
                )
            if prev_row is not None and prev_row["closing_revision"] is not None:
                prev_closing_revision = int(prev_row["closing_revision"])

        # Carry-forward source: the latest closing record before this date.
        with self.read_connection() as conn:
            source = self._days.latest_closing_before(conn, business_date)
        if source is not None:
            carry_cash = int(source["closing_cash_paisa"])
            source_date = source["business_date"]
            source_revision = int(source["closing_revision"])
            source_kind = "closing"
            prev_closing_date = source_date
        elif latest is None:
            # Very first day: the manager enters initial balances once.
            if opening_cash_paisa is None:
                raise errors.ValidationError(
                    "This is the first business day: enter the opening cash"
                    " (counted cash on hand). It will be carried forward"
                    " automatically from tomorrow on.",
                    key="err.first_day_needs_opening_cash",
                )
            carry_cash = opening_cash_paisa
            source_date = None
            source_revision = None
            source_kind = "initial"
            prev_closing_date = None
        else:
            # Defensive: days exist but no closing record (e.g. rows created
            # outside the service). Carry from the previous day row and say so.
            with self.read_connection() as conn:
                prev_row = self._days.get(conn, latest)
            physical = prev_row["physical_cash_paisa"]
            carry_cash = (
                int(physical)
                if physical is not None
                else int(prev_row["opening_cash_paisa"])
            )
            source_date = latest
            source_revision = None
            source_kind = "day_row_fallback"
            prev_closing_date = latest

        status = "non_operating" if non_operating else "open"
        opened_at = timecalc.utc_now_iso()
        payload = {
            "opening_cash_paisa": carry_cash,
            "source": source_kind,
            "source_business_date": source_date,
            "source_closing_revision": source_revision,
            "non_operating": non_operating,
            "gap_reason": clean_gap_reason,
        }

        with self.transaction() as conn:
            self._days.insert(
                conn,
                business_date=business_date,
                status=status,
                opening_cash_paisa=carry_cash,
                prev_closing_date=prev_closing_date,
                opened_at_utc=opened_at,
            )
            self._days.insert_opening_balance(
                conn,
                business_date=business_date,
                balance_type="cash",
                fuel_product_id=None,
                amount_paisa=carry_cash,
                quantity_ml=None,
                source_business_date=source_date,
                source_closing_revision=source_revision,
                created_by=who,
            )
            # Phase 3: carry each fuel product's theoretical closing stock
            # into the new day (never typed in, never duplicated). The very
            # first day is skipped — the manager enters opening stock via
            # InventoryService.set_opening_stock.
            self._carry_stock_forward(
                conn,
                business_date=business_date,
                prev_date=latest,
                prev_closing_revision=prev_closing_revision,
                actor=who,
            )
            self._days.insert_event(
                conn,
                business_date=business_date,
                event_type="open",
                actor=who,
                reason=clean_gap_reason,
                payload_json=json.dumps(payload, sort_keys=True),
                occurred_at_utc=opened_at,
            )
            row = self._days.get(conn, business_date)
        return _to_business_day(row)

    def _ensure_open_artifacts(self, existing, who: str) -> BusinessDay:
        """Idempotent re-open: an already-open day returns its state and any
        missing derived rows (opening balance, open event) are repaired —
        never duplicated."""
        status = existing["status"]
        business_date = existing["business_date"]
        if status == "closed":
            raise errors.BusinessRuleError(
                f"Business day {business_date} is already closed. Reopen it"
                " to make corrections.",
                key="err.day_already_closed",
                params={"day_date": business_date},
            )
        if status == "non_operating":
            raise errors.BusinessRuleError(
                f"Business day {business_date} is marked non-operating.",
                key="err.day_marked_non_operating",
                params={"day_date": business_date},
            )
        with self.transaction() as conn:
            if self._days.get_opening_cash(conn, business_date) is None:
                self._days.insert_opening_balance(
                    conn,
                    business_date=business_date,
                    balance_type="cash",
                    fuel_product_id=None,
                    amount_paisa=int(existing["opening_cash_paisa"]),
                    quantity_ml=None,
                    source_business_date=existing["prev_closing_date"],
                    source_closing_revision=None,
                    created_by=who,
                )
            if not self._days.has_event_type(conn, business_date, "open"):
                self._days.insert_event(
                    conn,
                    business_date=business_date,
                    event_type="open",
                    actor=who,
                    reason=None,
                    payload_json=json.dumps(
                        {
                            "opening_cash_paisa": int(
                                existing["opening_cash_paisa"]
                            ),
                            "source": "repaired",
                            "note": "open event recreated by idempotent open",
                        },
                        sort_keys=True,
                    ),
                    occurred_at_utc=timecalc.utc_now_iso(),
                )
            row = self._days.get(conn, business_date)
        return _to_business_day(row)

    def _carry_stock_forward(
        self,
        conn,
        *,
        business_date: str,
        prev_date: str | None,
        prev_closing_revision: int | None,
        actor: str,
    ) -> None:
        """Carry each fuel product's theoretical closing stock into a new day.

        Runs inside the open-day transaction on the caller's connection.
        ``theoretical`` for a product = its previous-day opening balance +
        the previous day's movements (deliveries − sales ± adjustments),
        summed across the product's tanks. A product is carried only when
        the previous day tracked it (has a 'stock' opening row) — rows are
        never invented for untracked products, so the manager can still
        enter opening stock for those via
        :meth:`InventoryService.set_opening_stock`. Existing rows for the
        new date are never duplicated; each carried row is stamped with its
        source day + closing revision.
        """
        if prev_date is None:
            return  # first day ever: manager enters opening stock via the UI
        products = self._price_service._list_products_conn(conn)
        if not products:
            return
        prev_tracked = {
            row["fuel_product_id"]
            for row in conn.execute(
                "SELECT DISTINCT fuel_product_id FROM opening_balances"
                " WHERE business_date = ? AND balance_type = 'stock'",
                (prev_date,),
            )
        }
        if not prev_tracked:
            return
        positions = self._inventory_service._stock_positions_conn(conn, prev_date)
        theoretical_by_code: dict[str, int] = {}
        for position in positions:
            theoretical_by_code[position.fuel_product_code] = (
                theoretical_by_code.get(position.fuel_product_code, 0)
                + position.theoretical_ml
            )
        for product in products:
            product_id = int(product["id"])
            if product_id not in prev_tracked:
                continue  # untracked on the previous day: nothing to carry
            exists = conn.execute(
                "SELECT 1 FROM opening_balances"
                " WHERE business_date = ? AND balance_type = 'stock'"
                "   AND fuel_product_id = ? LIMIT 1",
                (business_date, product_id),
            ).fetchone()
            if exists is not None:
                continue
            self._days.insert_opening_balance(
                conn,
                business_date=business_date,
                balance_type="stock",
                fuel_product_id=product_id,
                amount_paisa=None,
                quantity_ml=theoretical_by_code.get(product["code"], 0),
                source_business_date=prev_date,
                source_closing_revision=prev_closing_revision,
                created_by=actor,
            )

    # ------------------------------------------------------------------
    # guided close
    # ------------------------------------------------------------------
    def validate_close(
        self,
        business_date: str,
        physical_cash_paisa: int | None = None,
    ) -> CloseValidationReport:
        """Run the §19 closing checklist WITHOUT writing anything.

        Returns the report (the recoverable draft): blocking ``fail`` items
        stop the close, ``warning`` items stay visible, ``na`` items name the
        phase that will enforce them.
        """
        return self._validate_close_impl(business_date, physical_cash_paisa, None)

    def _validate_close_impl(
        self,
        business_date: str,
        physical_cash_paisa: int | None,
        conn,
    ) -> CloseValidationReport:
        """The checklist body.

        ``conn`` is None for the standalone (read-only) validation, or the
        close transaction's connection for the in-transaction re-validation:
        the Phase-5 checks (settlements, cash book) must see the backfilled
        rows that ``close_day`` wrote uncommitted, which a fresh connection
        cannot. All other checks read pre-existing committed data, for which
        a separate read connection is safe under the reserved lock.
        """
        report = CloseValidationReport(business_date=business_date)
        day = self.get_day(business_date)
        if day is None:
            report.items.append(
                CloseCheckItem(
                    "day_open", "close.check.day_open", "fail",
                    f"No business day {business_date} exists.",
                    detail_key="close.detail.day_open_missing",
                    detail_params={"day_date": business_date},
                )
            )
            return report
        if day.status not in ("open", "reopened"):
            report.items.append(
                CloseCheckItem(
                    "day_open", "close.check.day_open", "fail",
                    f"Day status is '{day.status}'; only an open day can be closed.",
                    detail_key="close.detail.day_open_status",
                    detail_params={"status": day.status},
                )
            )
            return report
        report.items.append(
            CloseCheckItem(
                "day_open", "close.check.day_open", "pass",
                f"Day is {day.status}.",
                detail_key="close.detail.day_open_ok",
                detail_params={"status": day.status},
            )
        )

        readings = self._nozzle_service.day_readings(business_date)
        if not readings:
            report.items.append(
                CloseCheckItem(
                    "readings_complete", "close.check.readings_complete", "fail",
                    "No active nozzles are configured.",
                    detail_key="close.detail.readings_none",
                )
            )
        else:
            missing = [r.nozzle_code for r in readings if not r.has_reading]
            if missing:
                report.items.append(
                    CloseCheckItem(
                        "readings_complete", "close.check.readings_complete",
                        "fail",
                        "Missing final readings: " + ", ".join(missing) + ".",
                        detail_key="close.detail.readings_missing",
                        detail_params={"codes": ", ".join(missing)},
                    )
                )
            else:
                report.items.append(
                    CloseCheckItem(
                        "readings_complete", "close.check.readings_complete",
                        "pass", f"{len(readings)} nozzle readings recorded.",
                        detail_key="close.detail.readings_ok",
                        detail_params={"count": len(readings)},
                    )
                )

        # Interval validity: a backwards interval is only possible with an
        # authorizing meter event (the E2 trigger rejects it otherwise), and
        # §11.1 forbids interpreting it as a negative sale — its quantity is
        # unknown from readings alone, so it warns and is excluded from
        # product totals instead of blocking the close.
        reset_intervals: list[str] = []
        unexplained: list[str] = []
        for r in readings:
            if (
                r.current_ml is not None
                and r.previous_ml is not None
                and r.current_ml < r.previous_ml
            ):
                if self._nozzle_service.has_meter_events(r.nozzle_id):
                    reset_intervals.append(r.nozzle_code)
                else:
                    unexplained.append(r.nozzle_code)
        if unexplained:
            report.items.append(
                CloseCheckItem(
                    "intervals_valid", "close.check.intervals_valid", "fail",
                    "Negative sales interval with no meter event on: "
                    + ", ".join(unexplained) + ".",
                    detail_key="close.detail.intervals_unexplained",
                    detail_params={"codes": ", ".join(unexplained)},
                )
            )
        elif reset_intervals:
            report.items.append(
                CloseCheckItem(
                    "intervals_valid", "close.check.intervals_valid", "warning",
                    "Interval not computable from readings (meter reset /"
                    " replacement recorded — never a negative sale); excluded"
                    " from product totals: " + ", ".join(reset_intervals) + ".",
                    detail_key="close.detail.intervals_reset",
                    detail_params={"codes": ", ".join(reset_intervals)},
                )
            )
        else:
            report.items.append(
                CloseCheckItem(
                    "intervals_valid", "close.check.intervals_valid", "pass",
                    "All reading intervals are non-negative.",
                    detail_key="close.detail.intervals_ok",
                )
            )

        liters_by_product: dict[str, int] = {}
        first_reading: list[str] = []
        for r in readings:
            if r.liters_sold_ml is None:
                if r.has_reading:
                    first_reading.append(r.nozzle_code)
                continue
            liters_by_product[r.fuel_product_code] = (
                liters_by_product.get(r.fuel_product_code, 0)
                + r.liters_sold_ml
            )
        report.liters_by_product_ml = liters_by_product
        if first_reading:
            report.items.append(
                CloseCheckItem(
                    "first_reading", "close.check.first_reading", "warning",
                    "No previous reading for "
                    + ", ".join(first_reading)
                    + " — liters sold cannot be computed until the next reading.",
                    detail_key="close.detail.first_reading",
                    detail_params={"codes": ", ".join(first_reading)},
                )
            )
        detail = ", ".join(
            f"{product}: {ml / 1000:,.3f} L"
            for product, ml in sorted(liters_by_product.items())
        ) or "no intervals yet"
        report.items.append(
            CloseCheckItem(
                "liters_reconciled", "close.check.liters_reconciled", "pass",
                "Meter-derived liters by product — " + detail + ".",
                detail_key="close.detail.liters_by_product",
                detail_params={"breakdown": detail},
            )
        )

        self._check_prices_recorded(report, business_date)
        self._check_deliveries_posted(report, business_date)
        self._check_stock_calculated(report, business_date)
        self._check_tank_readings(report, business_date)
        self._check_credit_tickets_saved(report, business_date)
        self._check_allocations_valid(report, business_date)
        self._check_settlements_recorded(report, business_date, conn)
        self._check_cashbook_complete(report, business_date, conn)

        cash = physical_cash_paisa
        if cash is None:
            cash = day.physical_cash_paisa
        report.physical_cash_paisa = cash
        if cash is None:
            report.items.append(
                CloseCheckItem(
                    "physical_cash", "close.check.physical_cash", "fail",
                    "Counted (physical) cash has not been entered.",
                    detail_key="close.detail.cash_missing",
                )
            )
        elif cash < 0:
            report.items.append(
                CloseCheckItem(
                    "physical_cash", "close.check.physical_cash", "fail",
                    "Physical cash cannot be negative.",
                    detail_key="close.detail.cash_negative",
                )
            )
        else:
            report.items.append(
                CloseCheckItem(
                    "physical_cash", "close.check.physical_cash", "pass",
                    "Physical cash entered.",
                    detail_key="close.detail.cash_ok",
                )
            )

        for key, label_key in _NA_CHECKS:
            report.items.append(
                CloseCheckItem(
                    key, label_key, "na",
                    "Not enforced in Phase 2 — arrives with its module.",
                    detail_key="close.detail.na",
                )
            )
        return report

    # ------------------------------------------------------------------
    # Phase-3 close checks (real enforcement, one method per checklist key)
    # ------------------------------------------------------------------
    def _check_prices_recorded(
        self, report: CloseValidationReport, business_date: str
    ) -> None:
        """Every active fuel product needs a price effective by end of day."""
        key, label = "prices_recorded", "close.check.prices_recorded"
        products = self._price_service.list_fuel_products(active_only=True)
        missing = [
            product["code"]
            for product in products
            if self._price_service.price_effective_at_day_end(
                product["code"], business_date
            )
            is None
        ]
        if missing:
            report.items.append(
                CloseCheckItem(
                    key, label, "fail",
                    "No price effective by end of day for: "
                    + ", ".join(missing)
                    + ". Set the price before closing.",
                    detail_key="close.detail.prices_missing",
                    detail_params={"codes": ", ".join(missing)},
                )
            )
        else:
            report.items.append(
                CloseCheckItem(
                    key, label, "pass",
                    f"Prices recorded for {len(products)} product(s).",
                    detail_key="close.detail.prices_ok",
                    detail_params={"count": len(products)},
                )
            )

    def _check_deliveries_posted(
        self, report: CloseValidationReport, business_date: str
    ) -> None:
        """Every delivery of the day must have exactly one stock movement."""
        key, label = "deliveries_posted", "close.check.deliveries_posted"
        deliveries = self._delivery_service.list_deliveries(
            business_date=business_date
        )
        if not deliveries:
            report.items.append(
                CloseCheckItem(
                    key, label, "pass", "No deliveries recorded today.",
                    detail_key="close.detail.deliveries_none",
                )
            )
            return
        broken: list[str] = []
        with self.read_connection() as conn:
            for delivery in deliveries:
                if not self._inventory_service._movement_exists_conn(
                    conn, delivery.uuid
                ):
                    broken.append(
                        delivery.supplier_invoice_no or delivery.uuid[:8]
                    )
        if broken:
            report.items.append(
                CloseCheckItem(
                    key, label, "fail",
                    "Deliveries missing their stock intake movement: "
                    + ", ".join(broken)
                    + ".",
                    detail_key="close.detail.deliveries_missing_movement",
                    detail_params={"items": ", ".join(broken)},
                )
            )
        else:
            total_ml = sum(d.quantity_ml for d in deliveries)
            report.items.append(
                CloseCheckItem(
                    key, label, "pass",
                    f"{len(deliveries)} deliveries posted"
                    f" ({total_ml / 1000:,.3f} L).",
                    detail_key="close.detail.deliveries_ok",
                    detail_params={
                        "count": len(deliveries),
                        "total_ml": total_ml,
                    },
                )
            )

    def _check_stock_calculated(
        self, report: CloseValidationReport, business_date: str
    ) -> None:
        """All metered sales must be posted to stock and no theoretical
        balance may be negative.

        Fails when a postable (known, non-zero liter) interval has no posted
        sale movement, when no readings exist at all (the day's sales were
        never calculated), or when any tank's theoretical stock is negative.
        Zero-liter intervals post nothing (the ledger forbids them) and
        first-reading days have no intervals — neither blocks the close.
        """
        key, label = "stock_calculated", "close.check.stock_calculated"
        intervals = self._sales_service.day_intervals(business_date)
        unposted: list[str] = []
        with self.read_connection() as conn:
            for interval in intervals:
                if not _is_postable(interval):
                    continue  # unknown qty is warned under intervals_valid
                if not self._inventory_service._movement_exists_conn(
                    conn,
                    sale_movement_source_uuid(
                        business_date, interval.nozzle_id, interval.seq
                    ),
                ):
                    unposted.append(
                        f"{interval.nozzle_code}#{interval.seq}"
                    )
        if unposted:
            report.items.append(
                CloseCheckItem(
                    key, label, "fail",
                    "Metered sales not posted to stock — post the day's sales"
                    " first: " + ", ".join(unposted) + ".",
                    detail_key="close.detail.stock_unposted",
                    detail_params={"items": ", ".join(unposted)},
                )
            )
            return
        # A reading correction that landed after posting leaves the ledger
        # behind until the safety-net post inside close_day appends the
        # audited adjustment. Warn (never block): the manager should know
        # the close will change posted stock figures.
        diverged = self._sales_service.diverged_intervals(business_date)
        if diverged:
            codes = ", ".join(
                sorted({d["nozzle_code"] for d in diverged})
            )
            report.items.append(
                CloseCheckItem(
                    key, label, "warning",
                    "Meter corrections changed posted sales on: " + codes
                    + ". The stock difference posts as an audited"
                    " adjustment when the day closes.",
                    detail_key="close.detail.stock_corrections_pending",
                    detail_params={"codes": codes},
                )
            )
        if not intervals:
            readings = self._nozzle_service.day_readings(business_date)
            if not any(r.has_reading for r in readings):
                report.items.append(
                    CloseCheckItem(
                        key, label, "fail",
                        "No nozzle readings recorded — the day's sales have"
                        " not been calculated or posted to stock.",
                        detail_key="close.detail.stock_no_readings",
                    )
                )
                return
        positions = self._inventory_service.stock_positions(business_date)
        negative = [
            position.tank_code
            for position in positions
            if position.theoretical_ml < 0
        ]
        if negative:
            report.items.append(
                CloseCheckItem(
                    key, label, "fail",
                    "Negative theoretical stock on: " + ", ".join(negative)
                    + ". Record the missing delivery or an authorized"
                    " adjustment.",
                    detail_key="close.detail.stock_negative",
                    detail_params={"codes": ", ".join(negative)},
                )
            )
            return
        by_product: dict[str, int] = {}
        for position in positions:
            by_product[position.fuel_product_code] = (
                by_product.get(position.fuel_product_code, 0)
                + position.theoretical_ml
            )
        detail = ", ".join(
            f"{code}: {ml / 1000:,.3f} L"
            for code, ml in sorted(by_product.items())
        ) or "no tanks"
        report.items.append(
            CloseCheckItem(
                key, label, "pass", "Theoretical stock — " + detail + ".",
                detail_key="close.detail.stock_ok",
                detail_params={"breakdown": detail},
            )
        )

    def _check_tank_readings(
        self, report: CloseValidationReport, business_date: str
    ) -> None:
        """Every active tank should have a dip reading; missing ones warn."""
        key, label = "tank_readings", "close.check.tank_readings"
        tanks = self._inventory_service.list_tanks(active_only=True)
        positions = {
            position.tank_id: position
            for position in self._inventory_service.stock_positions(business_date)
        }
        missing = [
            tank.code
            for tank in tanks
            if tank.id not in positions
            or positions[tank.id].physical_ml is None
        ]
        if missing:
            report.items.append(
                CloseCheckItem(
                    key, label, "warning",
                    "No dip reading recorded (not measured): "
                    + ", ".join(missing)
                    + ".",
                    detail_key="close.detail.tank_missing",
                    detail_params={"codes": ", ".join(missing)},
                )
            )
        else:
            variances = ", ".join(
                f"{position.tank_code}: {position.variance_ml / 1000:+,.3f} L"
                for position in positions.values()
                if position.variance_ml is not None
            )
            detail = f"Dip readings recorded for {len(tanks)} tank(s)"
            if variances:
                detail += "; variances — " + variances
            detail += "."
            if variances:
                tank_key = "close.detail.tank_variances"
                tank_params = {
                    "count": len(tanks),
                    "variances": variances,
                }
            else:
                tank_key = "close.detail.tank_ok"
                tank_params = {"count": len(tanks)}
            report.items.append(
                CloseCheckItem(
                    key, label, "pass", detail,
                    detail_key=tank_key,
                    detail_params=tank_params,
                )
            )

    # ------------------------------------------------------------------
    # Phase-4 close checks (real enforcement, one method per checklist key)
    # ------------------------------------------------------------------
    def _check_credit_tickets_saved(
        self, report: CloseValidationReport, business_date: str
    ) -> None:
        """Every credit ticket of the day must be internally consistent.

        Fails naming each ticket whose :meth:`ticket_integrity` finds
        problems. The metered-vs-credit liters guard
        (``sales_service.credit_liters_guard``) is surfaced as ``warning``
        items under the same key — credit liters exceeding metered liters
        warn, they never block the close.
        """
        key, label = "credit_tickets_saved", "close.check.credit_tickets_saved"
        tickets = self._credit_service.day_tickets(business_date)
        broken: list[str] = []
        broken_segments: list[tuple[str, dict]] = []
        _ref_key = {
            "close.detail.ticket_gross_mismatch":
                "close.detail.ticket_ref_gross_mismatch",
            "close.detail.ticket_line_amount":
                "close.detail.ticket_ref_line_amount",
            "close.detail.ticket_nozzle_missing":
                "close.detail.ticket_ref_nozzle_missing",
            "close.detail.ticket_nozzle_product":
                "close.detail.ticket_ref_nozzle_product",
        }
        for ticket in tickets:
            segments = self._credit_service.ticket_problem_segments(ticket.id)
            if segments:
                ref = ticket.receipt_no or ticket.uuid[:8]
                broken.append(
                    f"{ref}: "
                    + "; ".join(
                        render_en(seg_key, seg_params)
                        for seg_key, seg_params in segments
                    )
                )
                for seg_key, seg_params in segments:
                    broken_segments.append(
                        (
                            _ref_key[seg_key],
                            {"ref": ref, **seg_params},
                        )
                    )
        guard = self._sales_service.credit_liters_guard(business_date)
        guard_warnings = (
            guard.get("warnings", []) if guard.get("checked") else []
        )
        guard_segments = (
            guard.get("warning_segments", []) if guard.get("checked") else []
        )
        if broken:
            report.items.append(
                CloseCheckItem(
                    key, label, "fail",
                    "Credit ticket integrity problems — "
                    + " | ".join(broken),
                    detail_key="close.detail.ticket_problems",
                    detail_params={"issues": broken_segments},
                )
            )
        elif tickets:
            report.items.append(
                CloseCheckItem(
                    key, label, "pass",
                    f"{len(tickets)} credit ticket(s) saved and"
                    " integrity-checked.",
                    detail_key="close.detail.tickets_ok",
                    detail_params={"count": len(tickets)},
                )
            )
        else:
            report.items.append(
                CloseCheckItem(
                    key, label, "pass", "No credit tickets recorded today.",
                    detail_key="close.detail.tickets_none",
                )
            )
        if guard_warnings:
            report.items.append(
                CloseCheckItem(
                    key, label, "warning",
                    "Credit liters exceed metered sales — "
                    + " | ".join(guard_warnings),
                    detail_key="close.detail.credit_exceeds_metered",
                    detail_params={"issues": guard_segments},
                )
            )

    def _check_allocations_valid(
        self, report: CloseValidationReport, business_date: str
    ) -> None:
        """Every payment of the day must allocate consistently.

        Recomputes, per payment: effective (net-of-reversal) allocations ≤
        payment amount; every still-effective allocation points at a posted,
        non-reversal ticket of the same customer. Fully reversed
        allocations (effective 0) are legitimate history and do not block.
        """
        key, label = "allocations_valid", "close.check.allocations_valid"
        payments = self._payment_service.day_payments(business_date)
        broken: list[str] = []
        broken_segments: list[tuple[str, dict]] = []
        rows = self._payment_service.allocation_integrity_rows(business_date)
        by_payment: dict[int, list] = {}
        for row in rows:
            by_payment.setdefault(int(row["payment_id"]), []).append(row)
        for payment in payments:
            allocs = [
                row
                for row in by_payment.get(payment.id, [])
                if int(row["effective_paisa"]) > 0
            ]
            total = sum(int(row["effective_paisa"]) for row in allocs)
            ref = payment.receipt_no or payment.uuid[:8]
            if total > payment.amount_paisa:
                broken.append(
                    f"{ref}: effective allocations"
                    f" {total / 100:,.2f} exceed the payment"
                    f" {payment.amount_paisa / 100:,.2f}."
                )
                broken_segments.append(
                    (
                        "close.detail.alloc_exceeds_payment",
                        {
                            "ref": ref,
                            "total_paisa": total,
                            "payment_paisa": payment.amount_paisa,
                        },
                    )
                )
            for row in allocs:
                if (
                    row["ticket_status"] != "posted"
                    or row["ticket_reverses_uuid"] is not None
                ):
                    broken.append(
                        f"{ref}: allocation {int(row['allocation_id'])}"
                        " points at a non-posted credit ticket."
                    )
                    broken_segments.append(
                        (
                            "close.detail.alloc_bad_ticket",
                            {
                                "ref": ref,
                                "allocation_id": int(row["allocation_id"]),
                            },
                        )
                    )
                elif int(row["ticket_customer_id"]) != int(
                    row["payment_customer_id"]
                ):
                    broken.append(
                        f"{ref}: allocation {int(row['allocation_id'])}"
                        " links a payment and a ticket of different"
                        " customers."
                    )
                    broken_segments.append(
                        (
                            "close.detail.alloc_customer_mismatch",
                            {
                                "ref": ref,
                                "allocation_id": int(row["allocation_id"]),
                            },
                        )
                    )
        if broken:
            report.items.append(
                CloseCheckItem(
                    key, label, "fail",
                    "Payment allocation problems — " + " | ".join(broken),
                    detail_key="close.detail.allocation_problems",
                    detail_params={"issues": broken_segments},
                )
            )
        elif payments:
            report.items.append(
                CloseCheckItem(
                    key, label, "pass",
                    f"{len(payments)} payment(s) with consistent"
                    " allocations.",
                    detail_key="close.detail.allocations_ok",
                    detail_params={"count": len(payments)},
                )
            )
        else:
            report.items.append(
                CloseCheckItem(
                    key, label, "pass", "No customer payments recorded today.",
                    detail_key="close.detail.allocations_none",
                )
            )

    # ------------------------------------------------------------------
    # Phase-5 close checks (real enforcement, one method per checklist key)
    # ------------------------------------------------------------------
    def _check_settlements_recorded(
        self, report: CloseValidationReport, business_date: str, conn=None
    ) -> None:
        """The day's meter revenue must be fully classified in the
        settlement ledger and the fundamental identity must hold:

        ``meter_revenue == settled_against_today_sales + new_receivables``

        (per product where tickets allow a product split, and always on the
        day total). Old-debt recovery is never part of this identity — it
        is a cash receipt, not revenue. A day with no meter revenue and no
        credit tickets has nothing to settle and passes with a note.
        """
        key, label = "settlements_recorded", "close.check.settlements_recorded"
        if conn is not None:
            status = self._cashbook_service.settlement_status_conn(
                conn, business_date
            )
        else:
            status = self._cashbook_service.settlement_status(business_date)
        if not status["recorded"]:
            if status.get("reversed_batches"):
                note_key = "close.detail.settlements_reversed"
            else:
                note_key = "close.detail.settlements_none"
            report.items.append(
                CloseCheckItem(
                    key, label, "pass",
                    status.get("note")
                    or "No sales to settle today.",
                    detail_key=note_key,
                )
            )
            return
        bad = [
            p for p in status["per_product"] if not p["identity_ok"]
        ]
        if bad:
            detail = "; ".join(
                f"{p['fuel_product_code']}: meter"
                f" {p['meter_revenue_paisa'] / 100:,.2f} vs settled +"
                f" receivables"
                f" {(p['settled_paisa'] + p['new_receivables_paisa']) / 100:,.2f}"
                for p in bad
            )
            report.items.append(
                CloseCheckItem(
                    key, label, "fail",
                    "Settlement identity broken — " + detail + ".",
                    detail_key="close.detail.settlement_identity",
                    detail_params={
                        "issues": [
                            (
                                "close.detail.settlement_identity_product",
                                {
                                    "product": p["fuel_product_code"],
                                    "meter_paisa": p["meter_revenue_paisa"],
                                    "settled_paisa": (
                                        p["settled_paisa"]
                                        + p["new_receivables_paisa"]
                                    ),
                                },
                            )
                            for p in bad
                        ],
                    },
                )
            )
        else:
            total = sum(p["settled_paisa"] for p in status["per_product"])
            report.items.append(
                CloseCheckItem(
                    key, label, "pass",
                    f"Settlements recorded ({total / 100:,.2f} settled;"
                    " identity meter == settled + new receivables holds).",
                    detail_key="close.detail.settlements_ok",
                    detail_params={"settled_paisa": total},
                )
            )

    def _check_cashbook_complete(
        self, report: CloseValidationReport, business_date: str, conn=None
    ) -> None:
        """Every derived cash posting must exist exactly once.

        Cash receipts derived from cash customer payments and paid-now
        ticket amounts, and cash payments derived from cash expenses,
        cash supplier payments and cash other-material transactions, are
        deterministic derivations — the checklist fails naming each
        missing one (the Cash Book "sync" action or the close flow
        backfills them; the checklist itself never writes).
        """
        key, label = "cashbook_complete", "close.check.cashbook_complete"
        if conn is not None:
            missing = self._cashbook_service.missing_cash_postings_conn(
                conn, business_date
            )
        else:
            missing = self._cashbook_service.missing_cash_postings(
                business_date
            )
        if missing:
            detail = "; ".join(
                f"{m['description']} ({m['amount_paisa'] / 100:,.2f})"
                for m in missing[:5]
            )
            if len(missing) > 5:
                detail += f"; … and {len(missing) - 5} more"
            report.items.append(
                CloseCheckItem(
                    key, label, "fail",
                    f"{len(missing)} cash-book posting(s) missing — "
                    + detail
                    + ". Sync the cash book before closing.",
                    detail_key="close.detail.cashbook_missing",
                    detail_params={
                        "count": len(missing),
                        "issues": [
                            (
                                "close.detail.cash_posting_missing",
                                {
                                    "description": m["description"],
                                    "amount_paisa": m["amount_paisa"],
                                },
                            )
                            for m in missing[:5]
                        ]
                        + (
                            [
                                (
                                    "close.detail.more_items",
                                    {"count": len(missing) - 5},
                                )
                            ]
                            if len(missing) > 5
                            else []
                        ),
                    },
                )
            )
        else:
            report.items.append(
                CloseCheckItem(
                    key, label, "pass",
                    "All derived cash receipts and payments are posted"
                    " exactly once.",
                    detail_key="close.detail.cashbook_ok",
                )
            )

    def close_day(
        self,
        business_date: str,
        *,
        physical_cash_paisa: int,
        actor: str | None = None,
    ) -> ClosingRecord:
        """Close a business day after the checklist passes.

        Everything — validation, the immutable closing record (new revision),
        the day-status flip and the audited close event — commits in ONE
        transaction. Any failure rolls the whole attempt back and the day
        stays open with the validation report available (recoverable draft).
        """
        if (
            not isinstance(physical_cash_paisa, int)
            or isinstance(physical_cash_paisa, bool)
            or physical_cash_paisa < 0
        ):
            raise errors.ValidationError(
                "Physical cash must be a non-negative integer number of paisa.",
                key="err.physical_cash_paisa",
            )
        who = self.resolve_actor(actor)

        # A missing or non-open day is a close-validation failure, not a
        # lookup error: the checklist report (with its day_open fail item)
        # rides along. This check runs before the ensure backfills so their
        # require_writable_day guard can never shadow the CloseBlockedError
        # contract the close flow has always offered.
        day = self.get_day(business_date)
        if day is None or day.status not in ("open", "reopened"):
            report = self.validate_close(business_date, physical_cash_paisa)
            if day:
                raise errors.CloseBlockedError(
                    f"Business day {business_date} cannot be closed"
                    f" (status: {day.status}).",
                    report,
                    key="err.close_blocked_status",
                    params={"day_date": business_date, "status": day.status},
                )
            raise errors.CloseBlockedError(
                f"Business day {business_date} cannot be closed.",
                report,
                key="err.close_blocked_no_day",
                params={"day_date": business_date},
            )

        with self.transaction() as conn:
            # Phase 5: backfill deterministic cash-book derivations (paid-now
            # receipts, credit-recovery receipts, cash expense / supplier /
            # other-material rows) and the day's settlement batch (cash =
            # residual) BEFORE the checklist runs, so the closing record and
            # the ledger always agree. Both helpers are idempotent.
            self._cashbook_service.ensure_cash_postings_conn(
                conn, business_date, who
            )
            self._cashbook_service.ensure_settlements_conn(
                conn, business_date, who
            )
            # Re-validate inside the transaction on fresh reads: the checklist
            # the manager confirmed must still hold at commit time.
            report = self._validate_close_conn(
                conn, business_date, physical_cash_paisa
            )
            if not report.can_close:
                raise errors.CloseBlockedError(
                    "The day cannot be closed: "
                    + str(len(report.blocking_errors))
                    + " blocking error(s). Resolve them and try again.",
                    report,
                    key="err.close_blocked_count",
                    params={"count": len(report.blocking_errors)},
                )

            day_row = self._days.get(conn, business_date)
            assert day_row is not None  # validated above

            # Post the day's metered sales to stock (idempotent — a no-op
            # when they were already posted) BEFORE the immutable closing
            # record is written, so the ledger and the record always agree.
            # Any F2 negative-stock abort rolls the whole close back.
            post_result = self._sales_service._post_day_sales_conn(
                conn, business_date, who
            )
            day_sales = self._sales_service._day_sales_conn(conn, business_date)
            positions = self._inventory_service._stock_positions_conn(
                conn, business_date
            )
            theoretical_stock_ml: dict[str, int] = {}
            for position in positions:
                theoretical_stock_ml[position.fuel_product_code] = (
                    theoretical_stock_ml.get(position.fuel_product_code, 0)
                    + position.theoretical_ml
                )

            revision = self._days.next_revision(conn, business_date)
            prev_closing = self._days.latest_closing_before(conn, business_date)
            prev_closing_id = (
                int(prev_closing["id"]) if prev_closing is not None else None
            )
            closed_at = timecalc.utc_now_iso()

            nozzle_rows = []
            for r in self._nozzle_service.day_readings(business_date):
                nozzle_rows.append(
                    {
                        "nozzle_code": r.nozzle_code,
                        "fuel_product": r.fuel_product_code,
                        "previous_ml": r.previous_ml,
                        "current_ml": r.current_ml,
                        "liters_sold_ml": r.liters_sold_ml,
                    }
                )
            warnings = [w.detail for w in report.warnings]
            # Phase 5: freeze the day's cost basis (periodic weighted-average
            # COGS per product) and the P&L into the immutable closing
            # record, plus the settlement status — old reports read this
            # snapshot and never change when new purchases are entered.
            close_snapshot = self._accounting_service.snapshot_for_close_conn(
                conn, business_date
            )
            settlement_status = (
                self._cashbook_service.settlement_status_conn(
                    conn, business_date
                )
            )
            summary = {
                "business_date": business_date,
                "closing_revision": revision,
                "opening_cash_paisa": int(day_row["opening_cash_paisa"]),
                "physical_cash_paisa": physical_cash_paisa,
                "liters_by_product_ml": report.liters_by_product_ml,
                "revenue_by_product_paisa": day_sales.revenue_by_product_paisa,
                "theoretical_stock_ml": theoretical_stock_ml,
                "posted_sales_movements": (
                    post_result["posted"]
                    + post_result["already_posted"]
                    + post_result["corrected"]
                ),
                "nozzles": nozzle_rows,
                "prev_closing_date": day_row["prev_closing_date"],
                "readings_source": "v_effective_nozzle_readings",
                "cost_snapshots": close_snapshot["cost_snapshots"],
                "pnl": close_snapshot["pnl"],
                "settlements": settlement_status,
                "app_version": config.APP_VERSION,
            }
            petrol_ml = int(report.liters_by_product_ml.get("PETROL", 0))
            diesel_ml = int(report.liters_by_product_ml.get("DIESEL", 0))

            closing_id = self._days.insert_closing(
                conn,
                business_date=business_date,
                closing_revision=revision,
                closed_at_utc=closed_at,
                prev_closing_id=prev_closing_id,
                actor=who,
                opening_cash_paisa=int(day_row["opening_cash_paisa"]),
                closing_cash_paisa=physical_cash_paisa,
                petrol_litres_ml=petrol_ml,
                diesel_litres_ml=diesel_ml,
                summary_json=json.dumps(summary, sort_keys=True),
                warnings_json=json.dumps(warnings, sort_keys=True),
                created_by=who,
            )
            self._days.apply_close(
                conn,
                business_date,
                closing_revision=revision,
                closed_at_utc=closed_at,
                physical_cash_paisa=physical_cash_paisa,
            )
            self._days.insert_event(
                conn,
                business_date=business_date,
                event_type="close",
                actor=who,
                reason=None,
                payload_json=json.dumps(
                    {
                        "closing_revision": revision,
                        "closing_id": closing_id,
                        "liters_by_product_ml": report.liters_by_product_ml,
                        "warnings": warnings,
                    },
                    sort_keys=True,
                ),
                occurred_at_utc=closed_at,
            )
            row = self._days.get_closing(conn, business_date, revision)
        closing = _to_closing_record(row)
        # Auto-backup hook (recovery.md §9 design): runs AFTER the closing
        # transaction has committed, never inside it. A backup failure must
        # never roll back or fail the close — the helper is strictly
        # log-only on failure.
        self._auto_backup_after_day_close(business_date, who)
        return closing

    def _auto_backup_after_day_close(self, business_date: str, who: str) -> None:
        """Best-effort automatic backup after a committed day close.

        Never raises: the day is already closed at this point, so a backup
        failure is logged as a warning and the close stands. Backup support
        modules are imported lazily to avoid circular imports.

        NOTE: the enable/disable setting is read with
        ``BackupRepository.get_setting`` on this service's own read
        connection — the hook only needs one setting value and already
        holds this connection, so there is no reason to construct a
        ``BackupService`` just to read it (``BackupService.get_setting``
        exists as a convenience accessor for post-startup code that
        already owns a ``BackupService``). A missing key counts as the
        default (enabled).
        """
        logger = logging.getLogger("petrol_pump_manager")
        try:
            from app.repositories.backup_repository import BackupRepository
            from app.services.backup_service import BackupService
        except ImportError as exc:
            logger.warning(
                "Automatic day-close backup for %s skipped: backup support "
                "unavailable (%s).", business_date, exc
            )
            return
        try:
            with self.read_connection() as conn:
                enabled = BackupRepository.get_setting(
                    conn, "backup.on_day_close"
                )
            # Missing key counts as the default: enabled ('1').
            if (enabled if enabled is not None else "1") == "0":
                logger.info(
                    "Automatic day-close backup for %s skipped "
                    "(backup.on_day_close disabled).", business_date
                )
                return
            # The backup must cover THIS service's database — pass self._db
            # explicitly. A default-constructed BackupService would bind the
            # live database path, which is wrong whenever this service
            # manages any other database (tests, tooling, multi-station).
            # In production self._db already is the live database, so
            # behaviour there is unchanged.
            result = BackupService(self._db, actor=who).run_backup("day_close")
            logger.info(
                "Automatic day-close backup for %s completed: %s",
                business_date, result
            )
        except Exception as exc:
            logger.warning(
                "Automatic day-close backup for %s FAILED: %s. The day close "
                "is committed and unaffected — take a manual backup from the "
                "backup settings.", business_date, exc
            )

    def _validate_close_conn(
        self, conn, business_date: str, physical_cash_paisa: int | None
    ) -> CloseValidationReport:
        """Same checklist as :meth:`validate_close` but on a caller-owned
        connection (used inside the close transaction)."""
        return self._validate_close_impl(business_date, physical_cash_paisa, conn)

    # ------------------------------------------------------------------
    # reopen
    # ------------------------------------------------------------------
    def reopen_day(
        self, business_date: str, *, reason: str, actor: str | None = None
    ) -> BusinessDay:
        """Authorized reopen of a closed day for corrections.

        Requires a non-empty manager reason; writes the audited reopen
        event (previous revision + timestamp + reason) and flips the day
        to 'reopened' in one transaction. Only the most recent business
        day may be reopened — older days' figures already feed newer days'
        opening balances.
        """
        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise errors.ValidationError(
                "Reopening a closed day needs a reason — it is written to the"
                " audit log with the previous closing revision.",
                key="err.reopen_needs_reason",
            )
        who = self.resolve_actor(actor)

        with self.read_connection() as conn:
            day_row = self._days.get(conn, business_date)
            latest = self._days.latest_date(conn)
        if day_row is None:
            raise errors.NotFoundError(
                f"No business day {business_date} exists.",
                key="err.no_day_exists",
                params={"day_date": business_date},
            )
        if day_row["status"] != "closed":
            raise errors.BusinessRuleError(
                f"Business day {business_date} is '{day_row['status']}', not"
                " closed — only a closed day can be reopened.",
                key="err.day_not_closed",
                params={"day_date": business_date, "status": day_row["status"]},
            )
        if latest != business_date:
            raise errors.BusinessRuleError(
                f"Only the most recent business day ({latest}) can be reopened;"
                f" {business_date} already feeds newer days' opening balances.",
                key="err.only_latest_reopen",
                params={"latest_date": latest, "day_date": business_date},
            )

        with self.transaction() as conn:
            self._days.insert_event(
                conn,
                business_date=business_date,
                event_type="reopen",
                actor=who,
                reason=clean_reason,
                payload_json=json.dumps(
                    {
                        "previous_closing_revision": int(
                            day_row["closing_revision"]
                        ),
                        "reopened_at_utc": timecalc.utc_now_iso(),
                    },
                    sort_keys=True,
                ),
                occurred_at_utc=timecalc.utc_now_iso(),
            )
            self._days.update_status(conn, business_date, "reopened")
            row = self._days.get(conn, business_date)
        return _to_business_day(row)


# ---------------------------------------------------------------------------
# row -> DTO mapping
# ---------------------------------------------------------------------------
def _to_business_day(row) -> BusinessDay:
    return BusinessDay(
        business_date=row["business_date"],
        status=row["status"],
        opening_cash_paisa=int(row["opening_cash_paisa"]),
        prev_closing_date=row["prev_closing_date"],
        opened_at_utc=row["opened_at_utc"],
        closed_at_utc=row["closed_at_utc"],
        closing_revision=int(row["closing_revision"]),
        physical_cash_paisa=(
            int(row["physical_cash_paisa"])
            if row["physical_cash_paisa"] is not None
            else None
        ),
    )


def _to_opening_balance(row) -> OpeningBalance:
    return OpeningBalance(
        business_date=row["business_date"],
        balance_type=row["balance_type"],
        fuel_product_id=(
            int(row["fuel_product_id"])
            if row["fuel_product_id"] is not None
            else None
        ),
        amount_paisa=(
            int(row["amount_paisa"]) if row["amount_paisa"] is not None else None
        ),
        quantity_ml=(
            int(row["quantity_ml"]) if row["quantity_ml"] is not None else None
        ),
        source_business_date=row["source_business_date"],
        source_closing_revision=(
            int(row["source_closing_revision"])
            if row["source_closing_revision"] is not None
            else None
        ),
    )


def _to_closing_record(row) -> ClosingRecord:
    return ClosingRecord(
        id=int(row["id"]),
        business_date=row["business_date"],
        closing_revision=int(row["closing_revision"]),
        closed_at_utc=row["closed_at_utc"],
        prev_closing_id=(
            int(row["prev_closing_id"])
            if row["prev_closing_id"] is not None
            else None
        ),
        actor=row["actor"],
        opening_cash_paisa=int(row["opening_cash_paisa"]),
        closing_cash_paisa=int(row["closing_cash_paisa"]),
        petrol_litres_ml=int(row["petrol_litres_ml"]),
        diesel_litres_ml=int(row["diesel_litres_ml"]),
        summary=json.loads(row["summary_json"]),
        warnings=json.loads(row["warnings_json"]),
    )
