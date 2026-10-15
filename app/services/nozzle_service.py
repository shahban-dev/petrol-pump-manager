"""Nozzle readings, meter events and corrections (Phase 2).

Rules enforced here (mirrored by the database triggers, which stay the
backstop):
* a day's previous reading is shown automatically — the manager only types
  today's final reading;
* liters sold = current − previous, in integer milliliters;
* a backwards reading needs a recorded meter event first (§11.1);
* readings are immutable: corrections are a new meter event + a
  replacement row linked to the original — never an edit or delete;
* the effective reading (original or latest correction) is what every
  calculation uses.

The UI never does this math: it calls these methods and displays the
returned DTOs (master prompt §26 / design plan §1).
"""

from __future__ import annotations

from decimal import Decimal

from app.calculations import money as calc
from app.calculations import time as timecalc
from app.models.nozzle import DayReading, MeterEventInfo, NozzleInfo
from app.repositories.nozzle_repository import NozzleRepository
from app.services import errors
from app.services._base import ServiceBase

#: Meter-event types the manager may record directly.
METER_EVENT_TYPES = ("reset", "rollover", "replacement", "correction")


class NozzleService(ServiceBase):
    """Records and corrects nozzle meter readings inside transactions."""

    def __init__(self, db_manager, actor: str | None = None) -> None:
        super().__init__(db_manager, actor)
        self._nozzles = NozzleRepository()

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def list_nozzles(self, *, active_only: bool = True) -> list[NozzleInfo]:
        """Configured nozzles (never hard-coded names in callers)."""
        with self.read_connection() as conn:
            rows = self._nozzles.list_nozzles(conn, active_only=active_only)
        return [
            NozzleInfo(
                id=int(row["id"]),
                code=row["code"],
                fuel_product_id=int(row["fuel_product_id"]),
                fuel_product_code=row["fuel_product_code"],
                label_en=row["label_en"],
                is_active=bool(row["is_active"]),
                sort_order=int(row["sort_order"]),
            )
            for row in rows
        ]

    def day_readings(self, business_date: str) -> list[DayReading]:
        """Every active nozzle's previous / current / liters for a date.

        Previous readings come from the previous approved final readings —
        the manager does not type them (master prompt §11).

        ``liters_sold_ml`` is ``current − previous`` when both are known
        and the interval is forward. When the current reading is lower
        than the previous one, a meter event authorized it (§11.1: a
        reset is never a negative sale) and the interval quantity is not
        computable from readings alone — ``liters_sold_ml`` is None and
        the meter event explains why.
        """
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            nozzles = self._nozzles.list_nozzles(conn, active_only=True)
            result: list[DayReading] = []
            for nozzle in nozzles:
                nozzle_id = int(nozzle["id"])
                current = self._nozzles.get_effective_reading(
                    conn, nozzle_id, business_date
                )
                previous = self._nozzles.get_previous_effective_reading(
                    conn, nozzle_id, business_date
                )
                current_ml = int(current["reading_ml"]) if current else None
                previous_ml = int(previous["reading_ml"]) if previous else None
                if current_ml is not None and previous_ml is not None:
                    liters = (
                        current_ml - previous_ml
                        if current_ml >= previous_ml
                        else None
                    )
                else:
                    liters = None
                result.append(
                    DayReading(
                        nozzle_id=nozzle_id,
                        nozzle_code=nozzle["code"],
                        fuel_product_code=nozzle["fuel_product_code"],
                        previous_ml=previous_ml,
                        current_ml=current_ml,
                        liters_sold_ml=liters,
                        has_reading=current is not None,
                    )
                )
        return result

    def meter_events(self, nozzle_id: int) -> list[MeterEventInfo]:
        """Recorded meter events for a nozzle, oldest first."""
        with self.read_connection() as conn:
            rows = self._nozzles.list_meter_events(conn, nozzle_id)
        return [
            MeterEventInfo(
                id=int(row["id"]),
                nozzle_id=int(row["nozzle_id"]),
                event_type=row["event_type"],
                reading_before_ml=(
                    int(row["reading_before_ml"])
                    if row["reading_before_ml"] is not None
                    else None
                ),
                reading_after_ml=(
                    int(row["reading_after_ml"])
                    if row["reading_after_ml"] is not None
                    else None
                ),
                occurred_at_utc=row["occurred_at_utc"],
                reason=row["reason"],
                authorized_by=row["authorized_by"],
            )
            for row in rows
        ]

    def has_meter_events(self, nozzle_id: int) -> bool:
        """True when any meter event is recorded for the nozzle."""
        return bool(self.meter_events(nozzle_id))

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    def record_reading(
        self,
        business_date: str,
        nozzle: str | int,
        litres: Decimal | str | int,
        *,
        actor: str | None = None,
    ) -> int:
        """Record today's final (closing) reading for a nozzle.

        Idempotent: retrying with the *identical* value returns the existing
        row instead of raising a duplicate error; a *different* value for
        an already-recorded reading raises :class:`DuplicateError` — use
        :meth:`correct_reading` for that.

        Returns the reading row id.
        """
        reading_ml = calc.to_ml(litres)  # rejects float, negatives
        nozzle_row = self._require_writable_nozzle(nozzle)
        nozzle_id = int(nozzle_row["id"])
        day = self._require_writable_day(business_date)
        who = self.resolve_actor(actor)

        with self.transaction() as conn:
            existing = self._nozzles.get_effective_reading(
                conn, nozzle_id, business_date
            )
            if existing is not None:
                if int(existing["reading_ml"]) == reading_ml:
                    return int(existing["id"])  # identical retry: no-op
                raise errors.DuplicateError(
                    f"Nozzle {nozzle_row['code']} already has a closing reading"
                    f" for {business_date}. To change it, use the correction"
                    " workflow (it preserves the original).",
                    key="err.reading_already_recorded",
                    params={
                        "code": nozzle_row["code"],
                        "day_date": business_date,
                    },
                )
            reading_id = self._nozzles.insert_reading(
                conn,
                nozzle_id=nozzle_id,
                business_date=business_date,
                reading_ml=reading_ml,
                reading_kind="closing",
                occurred_at_utc=timecalc.utc_now_iso(),
                created_by=who,
            )
        return reading_id

    def record_segment_reading(
        self,
        business_date: str,
        nozzle: str | int,
        litres: Decimal | str | int,
        *,
        occurred_at_utc: str,
        actor: str | None = None,
    ) -> int:
        """Record an intraday meter snapshot (``reading_kind='segment'``).

        Snapshots pin the meter value at a price-change moment so each sale
        interval is priced by the price effective at its own time (§11.2):
        a price change is blocked until every active nozzle of the product
        has one. Multiple segment readings per day are allowed (no E1
        uniqueness on segments); the timestamp's Pakistan calendar date must
        equal *business_date*. The E2 trigger still validates the row
        (active nozzle, non-negative, meter-event authorization when lower
        than the previous closing reading). Returns the reading row id.
        """
        reading_ml = calc.to_ml(litres)  # rejects float, negatives
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
                f"The reading timestamp ({stamp}) falls on a different"
                f" Pakistan calendar date than business date {business_date}.",
                key="err.reading_wrong_day",
                params={"stamp": stamp, "day_date": business_date},
            )
        nozzle_row = self._require_writable_nozzle(nozzle)
        self._require_writable_day(business_date)
        who = self.resolve_actor(actor)

        with self.transaction() as conn:
            reading_id = self._nozzles.insert_reading(
                conn,
                nozzle_id=int(nozzle_row["id"]),
                business_date=business_date,
                reading_ml=reading_ml,
                reading_kind="segment",
                occurred_at_utc=stamp,
                created_by=who,
            )
        return reading_id

    def record_meter_event(
        self,
        nozzle: str | int,
        event_type: str,
        *,
        reason: str,
        reading_before_litres: Decimal | str | int | None = None,
        reading_after_litres: Decimal | str | int | None = None,
        authorized_by: str | None = None,
        actor: str | None = None,
    ) -> int:
        """Record a meter reset / rollover / replacement / correction event.

        A reason is mandatory; without it the event is rejected. This is the
        authorization a backwards reading needs (§11.1) — the event itself
        never changes sales figures.
        """
        if event_type not in METER_EVENT_TYPES:
            raise errors.ValidationError(
                f"Unknown meter event type: {event_type!r}.",
                key="err.unknown_meter_event_type",
                params={"event_type": event_type},
            )
        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise errors.ValidationError(
                "A meter event needs a reason (what happened and why).",
                key="err.meter_event_needs_reason",
            )
        nozzle_row = self._require_nozzle(nozzle)
        before_ml = (
            calc.to_ml(reading_before_litres)
            if reading_before_litres is not None
            else None
        )
        after_ml = (
            calc.to_ml(reading_after_litres)
            if reading_after_litres is not None
            else None
        )
        who = self.resolve_actor(actor)
        authorizer = (
            authorized_by.strip()
            if authorized_by and authorized_by.strip()
            else who
        )
        with self.transaction() as conn:
            event_id = self._nozzles.insert_meter_event(
                conn,
                nozzle_id=int(nozzle_row["id"]),
                event_type=event_type,
                reason=clean_reason,
                reading_before_ml=before_ml,
                reading_after_ml=after_ml,
                occurred_at_utc=timecalc.utc_now_iso(),
                authorized_by=authorizer,
            )
        return event_id

    def correct_reading(
        self,
        business_date: str,
        nozzle: str | int,
        litres: Decimal | str | int,
        *,
        reason: str,
        actor: str | None = None,
    ) -> int:
        """Correct an already-recorded reading without touching history.

        Inserts a ``correction`` meter event (before/after, reason,
        authorizer) plus a replacement reading row linked to the original
        via ``supersedes_reading_id``. The original row is preserved; the
        replacement becomes the effective reading everywhere. Returns the
        new reading row id.
        """
        reading_ml = calc.to_ml(litres)
        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise errors.ValidationError(
                "A correction needs a reason — the original record is kept"
                " and the reason explains what changed (no silent corrections).",
                key="err.correction_needs_reason",
            )
        nozzle_row = self._require_writable_nozzle(nozzle)
        nozzle_id = int(nozzle_row["id"])
        self._require_writable_day(business_date)
        who = self.resolve_actor(actor)

        with self.transaction() as conn:
            original = self._nozzles.get_effective_reading(
                conn, nozzle_id, business_date
            )
            if original is None:
                raise errors.NotFoundError(
                    f"Nozzle {nozzle_row['code']} has no reading for"
                    f" {business_date} yet — record it first instead of"
                    " correcting.",
                    key="err.no_reading_to_correct",
                    params={
                        "code": nozzle_row["code"],
                        "day_date": business_date,
                    },
                )
            original_ml = int(original["reading_ml"])
            if original_ml == reading_ml:
                return int(original["id"])  # nothing to change
            occurred = timecalc.utc_now_iso()
            self._nozzles.insert_meter_event(
                conn,
                nozzle_id=nozzle_id,
                event_type="correction",
                reason=clean_reason,
                reading_before_ml=original_ml,
                reading_after_ml=reading_ml,
                occurred_at_utc=occurred,
                authorized_by=who,
            )
            # The correction event above authorizes a lower value at the
            # trigger level; it is NOT interpreted as a negative sale.
            new_id = self._nozzles.insert_reading(
                conn,
                nozzle_id=nozzle_id,
                business_date=business_date,
                reading_ml=reading_ml,
                reading_kind="closing",
                supersedes_reading_id=int(original["id"]),
                occurred_at_utc=occurred,
                created_by=who,
            )
        return new_id

    # ------------------------------------------------------------------
    # internal guards (friendly messages; triggers stay the backstop)
    # ------------------------------------------------------------------
    def _require_nozzle(self, nozzle: str | int):
        with self.read_connection() as conn:
            if isinstance(nozzle, int):
                row = self._nozzles.get_nozzle(conn, nozzle)
            else:
                row = self._nozzles.get_nozzle_by_code(conn, str(nozzle))
        if row is None:
            raise errors.NotFoundError(
                f"Unknown nozzle: {nozzle!r}.",
                key="err.unknown_nozzle",
                params={"ref": nozzle},
            )
        return row

    def _require_writable_nozzle(self, nozzle: str | int):
        row = self._require_nozzle(nozzle)
        if not row["is_active"]:
            raise errors.BusinessRuleError(
                f"Nozzle {row['code']} is inactive.",
                key="err.nozzle_inactive",
                params={"code": row["code"]},
            )
        return row

    def _require_writable_day(self, business_date: str):
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            day = self._days.get(conn, business_date)
        if day is None:
            raise errors.NotFoundError(
                f"No business day {business_date}. Open the day first.",
                key="err.no_business_day",
                params={"day_date": business_date},
            )
        status = day["status"]
        if status == "closed":
            raise errors.ClosedDayError(
                f"Business day {business_date} is closed. Reopen it first to"
                " make corrections.",
                key="err.day_closed",
                params={"day_date": business_date},
            )
        if status == "non_operating":
            raise errors.ClosedDayError(
                f"Business day {business_date} is marked non-operating; no"
                " readings may be posted to it.",
                key="err.nozzle_day_non_operating",
                params={"day_date": business_date},
            )
        return day
