"""Fuel deliveries: suppliers, posted delivery facts, stock intake (Phase 3).

A delivery is one posted fact plus exactly one ``'delivery'`` stock movement,
committed in ONE transaction. No cash rows are created here — the cash book
is Phase 5, and an unpaid invoice must never touch physical cash.
"""

from __future__ import annotations

import uuid as _uuid
from decimal import Decimal

from app.calculations import money as calc
from app.calculations import time as timecalc
from app.models.delivery import DeliveryInfo, SupplierInfo
from app.repositories.delivery_repository import DeliveryRepository
from app.repositories.inventory_repository import InventoryRepository
from app.services import errors
from app.services._base import ServiceBase, require_writable_day
from app.services.inventory_service import resolve_tank
from app.services.price_service import resolve_fuel_product


def _to_supplier_info(row) -> SupplierInfo:
    return SupplierInfo(
        id=int(row["id"]),
        name=row["name"],
        phone=row["phone"],
        address=row["address"],
        is_active=bool(row["is_active"]),
    )


def _to_delivery_info(row) -> DeliveryInfo:
    return DeliveryInfo(
        id=int(row["id"]),
        uuid=row["uuid"],
        supplier_id=int(row["supplier_id"]),
        supplier_name=row["supplier_name"],
        fuel_product_id=int(row["fuel_product_id"]),
        fuel_product_code=row["fuel_product_code"],
        tank_id=int(row["tank_id"]) if row["tank_id"] is not None else None,
        tank_code=row["tank_code"],
        business_date=row["business_date"],
        quantity_ml=int(row["quantity_ml"]),
        unit_cost_10k=int(row["unit_cost_10k"]),
        total_cost_paisa=int(row["total_cost_paisa"]),
        other_cost_paisa=int(row["other_cost_paisa"]),
        supplier_invoice_no=row["supplier_invoice_no"],
        amount_paid_paisa=int(row["amount_paid_paisa"]),
        payment_status=row["payment_status"],
        occurred_at_utc=row["occurred_at_utc"],
        created_by=row["created_by"],
    )


class DeliveryService(ServiceBase):
    """Manages suppliers and records posted fuel deliveries."""

    def __init__(self, db_manager, actor: str | None = None) -> None:
        super().__init__(db_manager, actor)
        self._deliveries = DeliveryRepository()
        self._inventory = InventoryRepository()

    # ------------------------------------------------------------------
    # suppliers
    # ------------------------------------------------------------------
    def list_suppliers(self, active_only: bool = True) -> list[SupplierInfo]:
        """Suppliers, optionally active only."""
        with self.read_connection() as conn:
            rows = self._deliveries.list_suppliers(conn, active_only=active_only)
        return [_to_supplier_info(row) for row in rows]

    def add_supplier(
        self,
        name: str,
        phone: str | None = None,
        address: str | None = None,
        *,
        actor: str | None = None,
    ) -> int:
        """Add a supplier. The name is mandatory. Returns the supplier id."""
        clean = (name or "").strip()
        if not clean:
            raise errors.ValidationError(
                "Supplier name must not be empty.",
                key="err.supplier_name_empty",
            )
        who = self.resolve_actor(actor)
        with self.transaction() as conn:
            supplier_id = self._deliveries.insert_supplier(
                conn, clean, phone, address, who
            )
        return supplier_id

    def set_supplier_active(
        self,
        supplier_id: int,
        active: bool,
        *,
        actor: str | None = None,  # noqa: ARG002 — kept for signature symmetry
    ) -> None:
        """Activate/deactivate a supplier (audited). Unknown id → NotFoundError."""
        with self.transaction() as conn:
            row = self._deliveries.get_supplier(conn, supplier_id)
            if row is None:
                raise errors.NotFoundError(
                    f"Unknown supplier: {supplier_id!r}.",
                    key="err.unknown_supplier",
                    params={"ref": supplier_id},
                )
            self._deliveries.set_supplier_active(conn, int(row["id"]), bool(active))

    # ------------------------------------------------------------------
    # deliveries
    # ------------------------------------------------------------------
    def record_delivery(
        self,
        *,
        supplier: str | int,
        fuel_product: str | int,
        tank: str | int,
        business_date: str,
        quantity_litres,
        unit_cost_pkr,
        supplier_invoice_no: str | None = None,
        other_cost_pkr=Decimal("0"),
        amount_paid_pkr=Decimal("0"),
        occurred_at_utc: str | None = None,
        delivery_uuid: str | None = None,
        actor: str | None = None,
    ) -> int:
        """Record a posted fuel delivery and its stock intake movement.

        * ``supplier``: id or exact name (must be active);
        * ``fuel_product``: id or exact code; ``tank``: id or exact code
          (active, and storing that product);
        * amounts follow §5.2: ``total = round_half_up(qty_ml × unit_cost_10k
          / 100000)``; ``paid > total + other`` is rejected;
        * ``payment_status`` derives from paid: 0 → ``'unpaid'``,
          ≥ total+other → ``'paid'``, else ``'partial'``;
        * idempotent: a retry with the same *delivery_uuid* returns the
          existing delivery id without writing anything.

        Returns the delivery row id. The delivery + its single
        ``'delivery'`` movement commit in ONE transaction.
        """
        try:
            quantity_ml = calc.to_ml(quantity_litres)
        except (TypeError, ValueError) as exc:
            raise errors.ValidationError(
                f"Invalid delivery quantity: {exc}",
                key="err.invalid_delivery_quantity",
                params={"detail": str(exc)},
            ) from exc
        if quantity_ml <= 0:
            raise errors.ValidationError(
                "Delivery quantity must be greater than zero.",
                key="err.delivery_quantity_positive",
            )
        try:
            unit_cost_10k = calc.to_price_10k(unit_cost_pkr)
        except (TypeError, ValueError) as exc:
            raise errors.ValidationError(
                f"Invalid unit cost: {exc}",
                key="err.invalid_unit_cost",
                params={"detail": str(exc)},
            ) from exc
        if unit_cost_10k <= 0:
            raise errors.ValidationError(
                "Unit cost must be greater than zero.",
                key="err.unit_cost_positive",
            )
        try:
            other_paisa = calc.to_paisa(other_cost_pkr)
            paid_paisa = calc.to_paisa(amount_paid_pkr)
        except (TypeError, ValueError) as exc:
            raise errors.ValidationError(
                f"Invalid cost/payment amount: {exc}",
                key="err.invalid_cost_payment",
                params={"detail": str(exc)},
            ) from exc
        total_paisa = calc.amount_paisa_for(quantity_ml, unit_cost_10k)
        if paid_paisa > total_paisa + other_paisa:
            raise errors.ValidationError(
                "Amount paid cannot exceed the delivery total (fuel cost +"
                " other costs).",
                key="err.amount_paid_exceeds_total",
            )
        if paid_paisa == 0:
            payment_status = "unpaid"
        elif paid_paisa >= total_paisa + other_paisa:
            payment_status = "paid"
        else:
            payment_status = "partial"
        invoice_no = (supplier_invoice_no or "").strip() or None
        stamp: str | None
        if occurred_at_utc is not None:
            try:
                stamp = timecalc.require_utc_iso(occurred_at_utc)
            except ValueError as exc:
                raise errors.ValidationError(
                    str(exc),
                    key="err.invalid_utc_timestamp",
                    params={"value": occurred_at_utc},
                ) from exc
        else:
            stamp = None
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        uuid = (delivery_uuid or "").strip() or _uuid.uuid4().hex
        who = self.resolve_actor(actor)

        with self.transaction() as conn:
            existing = self._deliveries.get_delivery_by_uuid(conn, uuid)
            if existing is not None:
                return int(existing["id"])  # identical retry: no-op
            sup = self._resolve_supplier(conn, supplier)
            if not sup["is_active"]:
                raise errors.BusinessRuleError(
                    f"Supplier {sup['name']} is inactive.",
                    key="err.supplier_inactive",
                    params={"name": sup["name"]},
                )
            prod = resolve_fuel_product(conn, fuel_product)
            tank_row = resolve_tank(conn, tank)
            if not tank_row["is_active"]:
                raise errors.BusinessRuleError(
                    f"Tank {tank_row['code']} is inactive.",
                    key="err.tank_inactive",
                    params={"code": tank_row["code"]},
                )
            if int(tank_row["fuel_product_id"]) != int(prod["id"]):
                raise errors.ValidationError(
                    f"Tank {tank_row['code']} stores"
                    f" {tank_row['fuel_product_code']}, not {prod['code']}.",
                    key="err.tank_product_mismatch",
                    params={
                        "code": tank_row["code"],
                        "stored": tank_row["fuel_product_code"],
                        "product": prod["code"],
                    },
                )
            require_writable_day(conn, business_date)
            delivery_id = self._deliveries.insert_delivery(
                conn,
                uuid,
                int(sup["id"]),
                int(prod["id"]),
                int(tank_row["id"]),
                business_date,
                quantity_ml,
                unit_cost_10k,
                total_paisa,
                other_paisa,
                invoice_no,
                paid_paisa,
                payment_status,
                stamp,
                who,
            )
            # Exactly one stock movement for the intake (positive quantity).
            self._inventory.insert_movement(
                conn,
                _uuid.uuid4().hex,
                business_date,
                int(prod["id"]),
                int(tank_row["id"]),
                "delivery",
                quantity_ml,
                "fuel_deliveries",
                delivery_id,
                uuid,
                stamp,
                f"Delivery {invoice_no or uuid[:8]}",
                who,
            )
        return delivery_id

    def list_deliveries(
        self,
        business_date: str | None = None,
        supplier_id: int | None = None,
    ) -> list[DeliveryInfo]:
        """Deliveries, optionally filtered by day and/or supplier."""
        with self.read_connection() as conn:
            rows = self._deliveries.list_deliveries(
                conn, business_date=business_date, supplier_id=supplier_id
            )
        return [_to_delivery_info(row) for row in rows]

    def supplier_balance(self, supplier_id: int) -> dict:
        """``{total_invoiced_paisa, total_paid_paisa, outstanding_paisa}``
        across all of a supplier's deliveries."""
        with self.read_connection() as conn:
            row = self._deliveries.get_supplier(conn, supplier_id)
            if row is None:
                raise errors.NotFoundError(
                    f"Unknown supplier: {supplier_id!r}.",
                    key="err.unknown_supplier",
                    params={"ref": supplier_id},
                )
            invoiced, paid = self._deliveries.supplier_totals(
                conn, int(row["id"])
            )
        return {
            "total_invoiced_paisa": invoiced,
            "total_paid_paisa": paid,
            "outstanding_paisa": invoiced - paid,
        }

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------
    def _resolve_supplier(self, conn, supplier: str | int):
        if isinstance(supplier, bool):
            raise errors.ValidationError(
                f"Invalid supplier reference: {supplier!r}.",
                key="err.invalid_supplier_ref",
                params={"ref": supplier},
            )
        if isinstance(supplier, int):
            row = self._deliveries.get_supplier(conn, supplier)
        elif isinstance(supplier, str):
            name = supplier.strip()
            if not name:
                raise errors.ValidationError(
                "Supplier name must not be empty.",
                key="err.supplier_name_empty",
            )
            row = self._deliveries.get_supplier_by_name(conn, name)
        else:
            raise errors.ValidationError(
                f"Invalid supplier reference: {supplier!r}.",
                key="err.invalid_supplier_ref",
                params={"ref": supplier},
            )
        if row is None:
            raise errors.NotFoundError(
                f"Unknown supplier: {supplier!r}.",
                key="err.unknown_supplier",
                params={"ref": supplier},
            )
        return row
