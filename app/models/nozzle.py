"""Phase-2 data transfer objects: nozzles, readings, meter events.

DTOs only — no SQL, no business logic (design plan §1).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NozzleInfo:
    """One row of ``nozzles`` with its fuel product."""

    id: int
    code: str
    fuel_product_id: int
    fuel_product_code: str
    label_en: str | None
    is_active: bool
    sort_order: int


@dataclass(frozen=True)
class DayReading:
    """One nozzle's position for a business day.

    ``previous_ml`` is the effective reading of the latest earlier date
    (None on the nozzle's very first reading); ``current_ml`` is the
    effective closing reading for the day (None when not yet recorded);
    ``liters_sold_ml`` is ``current - previous`` when both are known and
    the interval runs forward. When the current reading is lower than the
    previous one, a meter event authorized it (§11.1) and the quantity is
    not computable from readings alone — ``liters_sold_ml`` is None.
    """

    nozzle_id: int
    nozzle_code: str
    fuel_product_code: str
    previous_ml: int | None
    current_ml: int | None
    liters_sold_ml: int | None
    has_reading: bool


@dataclass(frozen=True)
class MeterEventInfo:
    """One row of ``meter_events``."""

    id: int
    nozzle_id: int
    event_type: str
    reading_before_ml: int | None
    reading_after_ml: int | None
    occurred_at_utc: str
    reason: str
    authorized_by: str
