"""Phase-3 data transfer objects: suppliers and fuel deliveries.

DTOs only — no SQL, no business logic (design plan §1).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SupplierInfo:
    """One row of ``suppliers`` (mutable master data)."""

    id: int
    name: str
    phone: str | None
    address: str | None
    is_active: bool


@dataclass(frozen=True)
class DeliveryInfo:
    """One row of ``fuel_deliveries`` — a posted, immutable delivery fact.

    Recording a delivery also posts exactly one ``inventory_movements`` row
    (type ``'delivery'``). No cash rows are created here: the cash book is
    Phase 5, and an unpaid invoice must not touch physical cash.
    """

    id: int
    uuid: str
    supplier_id: int
    supplier_name: str
    fuel_product_id: int
    fuel_product_code: str
    tank_id: int | None
    tank_code: str | None
    business_date: str
    quantity_ml: int
    unit_cost_10k: int
    total_cost_paisa: int
    other_cost_paisa: int
    supplier_invoice_no: str | None
    amount_paid_paisa: int
    payment_status: str  # 'unpaid' | 'partial' | 'paid'
    occurred_at_utc: str
    created_by: str
