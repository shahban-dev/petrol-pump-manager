"""Phase-3 data transfer objects: tanks, stock movements, adjustments.

DTOs only — no SQL, no business logic (design plan §1).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TankInfo:
    """One row of ``tanks`` with its fuel product (mutable master data)."""

    id: int
    code: str
    fuel_product_id: int
    fuel_product_code: str
    label_en: str | None
    capacity_ml: int | None
    is_active: bool


@dataclass(frozen=True)
class MovementInfo:
    """One row of ``inventory_movements`` — the append-only stock ledger.

    ``movement_type`` is one of ``'delivery'`` (positive), ``'sale'``
    (negative), ``'adjustment'`` (signed) or ``'opening'`` (non-negative).
    """

    id: int
    uuid: str
    business_date: str
    fuel_product_id: int
    fuel_product_code: str
    tank_id: int
    tank_code: str
    movement_type: str
    quantity_ml: int
    source_table: str
    source_uuid: str
    occurred_at_utc: str
    reason: str | None


@dataclass(frozen=True)
class AdjustmentInfo:
    """One row of ``inventory_adjustments`` — an authorized stock correction.

    Immutable: a wrong adjustment is fixed by a compensating row, never an
    edit. Each adjustment posts exactly one ``'adjustment'`` movement.
    """

    id: int
    uuid: str
    fuel_product_id: int
    tank_id: int
    tank_code: str
    business_date: str
    quantity_ml: int
    reason: str
    authorized_by: str
    occurred_at_utc: str


@dataclass(frozen=True)
class TankReadingInfo:
    """One row of ``tank_readings`` — a dip/stick reading per tank per day
    (mutable; corrections are audited updates, never deletes)."""

    id: int
    tank_id: int
    tank_code: str
    business_date: str
    quantity_ml: int
    occurred_at_utc: str
    measured_by: str
    note: str | None


@dataclass(frozen=True)
class StockPosition:
    """One tank's stock position for a business day.

    ``theoretical_ml = opening + received - sold + adjustments`` where
    ``sold`` is reported positive (the ledger stores it negative).
    ``physical_ml``/``variance_ml`` are None until a dip reading is recorded.
    """

    tank_id: int
    tank_code: str
    fuel_product_code: str
    opening_ml: int
    received_ml: int
    sold_ml: int
    adjustments_ml: int
    theoretical_ml: int
    physical_ml: int | None
    variance_ml: int | None
