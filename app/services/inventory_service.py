"""Tank stock: opening balances, movements, dip readings, adjustments (Phase 3).

Design plan §8/§11.3/§11.11: the tracked balance of a product is its
``opening_balances`` 'stock' row (carried forward day to day) plus the day's
movements; a sale that would drive it negative is blocked (§11.3, trigger
F2). Stock is tracked per tank; product-level rollup is the fallback.
"""

from __future__ import annotations

import uuid as _uuid
from decimal import Decimal

from app.calculations import money as calc
from app.calculations import time as timecalc
from app.models.inventory import (
    StockPosition,
    TankInfo,
    TankReadingInfo,
)
from app.repositories.inventory_repository import InventoryRepository
from app.services import errors
from app.services._base import ServiceBase, require_writable_day
from app.services.price_service import resolve_fuel_product


def resolve_tank(conn, tank: str | int):
    """Tank row (with fuel product) by id (int) or exact code (str).

    Shared by the Phase-3 services. Raises :class:`NotFoundError` when
    unknown, :class:`ValidationError` for a malformed reference.
    """
    if isinstance(tank, bool):
        raise errors.ValidationError(
            f"Invalid tank reference: {tank!r}.",
            key="err.invalid_tank_ref",
            params={"ref": tank},
        )
    if isinstance(tank, int):
        row = InventoryRepository.get_tank(conn, tank)
    elif isinstance(tank, str):
        code = tank.strip()
        if not code:
            raise errors.ValidationError(
                "Tank code must not be empty.", key="err.tank_code_empty"
            )
        row = InventoryRepository.get_tank_by_code(conn, code)
    else:
        raise errors.ValidationError(
            f"Invalid tank reference: {tank!r}.",
            key="err.invalid_tank_ref",
            params={"ref": tank},
        )
    if row is None:
        raise errors.NotFoundError(
            f"Unknown tank: {tank!r}.",
            key="err.unknown_tank",
            params={"ref": tank},
        )
    return row


def _to_tank_info(row) -> TankInfo:
    return TankInfo(
        id=int(row["id"]),
        code=row["code"],
        fuel_product_id=int(row["fuel_product_id"]),
        fuel_product_code=row["fuel_product_code"],
        label_en=row["label_en"],
        capacity_ml=(
            int(row["capacity_ml"]) if row["capacity_ml"] is not None else None
        ),
        is_active=bool(row["is_active"]),
    )


def _to_tank_reading_info(row) -> TankReadingInfo:
    return TankReadingInfo(
        id=int(row["id"]),
        tank_id=int(row["tank_id"]),
        tank_code=row["tank_code"],
        business_date=row["business_date"],
        quantity_ml=int(row["quantity_ml"]),
        occurred_at_utc=row["occurred_at_utc"],
        measured_by=row["measured_by"],
        note=row["note"],
    )


def _to_signed_ml(value, name: str) -> int:
    """Signed litres → signed integer milliliters, rounded half-up once.

    Unlike :func:`money.to_ml` this accepts negatives (adjustments are
    signed); zero is rejected — a no-op adjustment is a mistake, not data.
    """
    if isinstance(value, float):
        raise TypeError(
            f"{name}: float is not allowed for quantity math; use Decimal or str"
        )
    try:
        signed = Decimal(value)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{name}: cannot interpret {value!r} as Decimal") from exc
    ml = calc.to_ml(abs(signed))
    if ml == 0:
        raise errors.ValidationError(
            "Adjustment quantity cannot be zero — post a compensating"
            " adjustment instead of a no-op.",
            key="err.adjustment_not_zero",
        )
    return ml if signed >= 0 else -ml


class InventoryService(ServiceBase):
    """Tank stock positions, dip readings and authorized adjustments."""

    def __init__(self, db_manager, actor: str | None = None) -> None:
        super().__init__(db_manager, actor)
        self._inventory = InventoryRepository()

    # ------------------------------------------------------------------
    # tanks
    # ------------------------------------------------------------------
    def list_tanks(self, active_only: bool = True) -> list[TankInfo]:
        """Configured tanks with their fuel product."""
        with self.read_connection() as conn:
            rows = self._inventory.list_tanks(conn, active_only=active_only)
        return [_to_tank_info(row) for row in rows]

    def _resolve_tank(self, conn, tank: str | int):
        return resolve_tank(conn, tank)

    # ------------------------------------------------------------------
    # opening stock
    # ------------------------------------------------------------------
    def set_opening_stock(
        self,
        fuel_product: str | int,
        quantity_litres,
        business_date: str,
        *,
        source_business_date: str | None = None,
        source_closing_revision: int | None = None,
        value_pkr=None,
        actor: str | None = None,
    ) -> int:
        """Record the manager-entered opening stock for a product and day.

        The first-day entry path: later days carry stock forward
        automatically at open. The day must exist and be writable; a second
        'stock' row for the same (date, product) raises DuplicateError.
        *value_pkr* (optional, Phase 5) records the carrying VALUE of that
        opening stock — the cost basis for periodic weighted-average COGS.
        It is stored in ``stock_opening_values`` (migration 012; the
        migration-005 CHECK on ``opening_balances`` forces amount_paisa NULL
        for stock rows and cannot be relaxed in place). When omitted the
        day's P&L cost basis is estimated and labelled as such.
        Returns the ``opening_balances`` row id.
        """
        try:
            quantity_ml = calc.to_ml(quantity_litres)
        except (TypeError, ValueError) as exc:
            raise errors.ValidationError(
                f"Invalid quantity: {exc}",
                key="err.invalid_quantity_detail",
                params={"detail": str(exc)},
            ) from exc
        value_paisa: int | None = None
        if value_pkr is not None:
            try:
                value_paisa = calc.to_paisa(value_pkr)
            except (TypeError, ValueError) as exc:
                raise errors.ValidationError(
                    f"Invalid opening stock value: {exc}",
                    key="err.invalid_opening_value",
                    params={"detail": str(exc)},
                ) from exc
            if value_paisa < 0:
                raise errors.ValidationError(
                    "Opening stock value cannot be negative.",
                    key="err.opening_value_negative",
                )
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        if source_business_date is not None and not timecalc.is_valid_business_date(
            source_business_date
        ):
            raise errors.ValidationError(
                f"Invalid source business date: {source_business_date!r}.",
                key="err.invalid_source_business_date",
                params={"day_date": source_business_date},
            )
        if source_closing_revision is not None and (
            not isinstance(source_closing_revision, int)
            or isinstance(source_closing_revision, bool)
            or source_closing_revision < 1
        ):
            raise errors.ValidationError(
                "Source closing revision must be a positive integer.",
                key="err.source_revision_positive",
            )
        who = self.resolve_actor(actor)
        with self.transaction() as conn:
            prod = resolve_fuel_product(conn, fuel_product)
            require_writable_day(conn, business_date)
            dup = conn.execute(
                "SELECT 1 FROM opening_balances"
                " WHERE business_date = ? AND balance_type = 'stock'"
                "   AND fuel_product_id = ? LIMIT 1",
                (business_date, int(prod["id"])),
            ).fetchone()
            if dup is not None:
                raise errors.DuplicateError(
                    f"Opening stock for {prod['code']} on {business_date} is"
                    " already recorded.",
                    key="err.opening_stock_exists",
                    params={
                        "product": prod["code"],
                        "day_date": business_date,
                    },
                )
            balance_id = self._days.insert_opening_balance(
                conn,
                business_date=business_date,
                balance_type="stock",
                fuel_product_id=int(prod["id"]),
                amount_paisa=None,
                quantity_ml=quantity_ml,
                source_business_date=source_business_date,
                source_closing_revision=source_closing_revision,
                created_by=who,
            )
            if value_paisa is not None:
                conn.execute(
                    "INSERT INTO stock_opening_values"
                    " (business_date, fuel_product_id, value_paisa, created_by)"
                    " VALUES (?, ?, ?, ?)",
                    (business_date, int(prod["id"]), value_paisa, who),
                )
        return balance_id

    # ------------------------------------------------------------------
    # stock positions
    # ------------------------------------------------------------------
    def stock_positions(self, business_date: str) -> list[StockPosition]:
        """Every active tank's stock position for a business day."""
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            return self._stock_positions_conn(conn, business_date)

    def _stock_positions_conn(self, conn, business_date: str) -> list[StockPosition]:
        """Positions on a caller-owned connection (used by the close flow)."""
        positions: list[StockPosition] = []
        for tank in self._inventory.list_tanks(conn, active_only=True):
            tank_id = int(tank["id"])
            product_id = int(tank["fuel_product_id"])
            opening = self._opening_stock_ml_conn(conn, business_date, product_id)
            received = self._inventory.sum_movements(
                conn, product_id, business_date,
                tank_id=tank_id, movement_type="delivery",
            )
            sold_ledger = self._inventory.sum_movements(
                conn, product_id, business_date,
                tank_id=tank_id, movement_type="sale",
            )
            adjustments = self._inventory.sum_movements(
                conn, product_id, business_date,
                tank_id=tank_id, movement_type="adjustment",
            )
            sold = -sold_ledger  # reported positive; the ledger stores it negative
            theoretical = opening + received - sold + adjustments
            reading = self._inventory.get_tank_reading(conn, tank_id, business_date)
            physical = (
                int(reading["quantity_ml"]) if reading is not None else None
            )
            variance = physical - theoretical if physical is not None else None
            positions.append(
                StockPosition(
                    tank_id=tank_id,
                    tank_code=tank["code"],
                    fuel_product_code=tank["fuel_product_code"],
                    opening_ml=opening,
                    received_ml=received,
                    sold_ml=sold,
                    adjustments_ml=adjustments,
                    theoretical_ml=theoretical,
                    physical_ml=physical,
                    variance_ml=variance,
                )
            )
        return positions

    def fuel_stock_summary(self, business_date: str) -> list[dict]:
        """Per-fuel stock summary for a business day (the client's sheet).

        Aggregates the per-tank positions by fuel product, in the client's
        own terms: total stock (opening), sold today, new stock (received),
        total including new stock (theoretical), dip reading (physical),
        stock as per dip, and difference (physical − theoretical).

        ``physical_ml``/``variance_ml`` are None for a fuel until every
        one of its tanks has a dip reading that day.
        """
        positions = self.stock_positions(business_date)
        by_fuel: dict[str, dict] = {}
        for pos in positions:
            fuel = pos.fuel_product_code
            agg = by_fuel.setdefault(
                fuel,
                {
                    "fuel_product_code": fuel,
                    "opening_ml": 0,
                    "received_ml": 0,
                    "sold_ml": 0,
                    "adjustments_ml": 0,
                    "theoretical_ml": 0,
                    "physical_ml": 0,
                    "tanks_with_dip": 0,
                    "tank_count": 0,
                },
            )
            agg["opening_ml"] += pos.opening_ml
            agg["received_ml"] += pos.received_ml
            agg["sold_ml"] += pos.sold_ml
            agg["adjustments_ml"] += pos.adjustments_ml
            agg["theoretical_ml"] += pos.theoretical_ml
            agg["tank_count"] += 1
            if pos.physical_ml is not None:
                agg["physical_ml"] += pos.physical_ml
                agg["tanks_with_dip"] += 1
        summary = []
        for agg in by_fuel.values():
            if agg["tanks_with_dip"] == agg["tank_count"] and agg["tank_count"] > 0:
                physical_ml: int | None = agg["physical_ml"]
                variance_ml: int | None = physical_ml - agg["theoretical_ml"]
            else:
                physical_ml = None
                variance_ml = None
            summary.append(
                {
                    "fuel_product_code": agg["fuel_product_code"],
                    "opening_ml": agg["opening_ml"],
                    "received_ml": agg["received_ml"],
                    "sold_ml": agg["sold_ml"],
                    "adjustments_ml": agg["adjustments_ml"],
                    "theoretical_ml": agg["theoretical_ml"],
                    "physical_ml": physical_ml,
                    "variance_ml": variance_ml,
                }
            )
        return summary

    def _opening_stock_ml_conn(
        self, conn, business_date: str, fuel_product_id: int
    ) -> int:
        """The 'stock' opening balance for (date, product), or 0."""
        row = conn.execute(
            "SELECT quantity_ml FROM opening_balances"
            " WHERE business_date = ? AND balance_type = 'stock'"
            "   AND fuel_product_id = ? LIMIT 1",
            (business_date, fuel_product_id),
        ).fetchone()
        if row is None or row["quantity_ml"] is None:
            return 0
        return int(row["quantity_ml"])

    def _movement_exists_conn(self, conn, source_uuid: str) -> bool:
        """Idempotency-key lookup on a caller-owned connection."""
        return self._inventory.movement_exists(conn, source_uuid)

    # ------------------------------------------------------------------
    # dip readings
    # ------------------------------------------------------------------
    def record_physical_count(
        self,
        tank: str | int,
        business_date: str,
        quantity_litres,
        *,
        measured_by: str | None = None,
        note: str | None = None,
        actor: str | None = None,
    ) -> int:
        """Record a tank dip reading for a day.

        One reading per tank per day: a second recording raises
        DuplicateError — use :meth:`correct_physical_count` to change it.
        Returns the reading row id.
        """
        try:
            quantity_ml = calc.to_ml(quantity_litres)
        except (TypeError, ValueError) as exc:
            raise errors.ValidationError(
                f"Invalid quantity: {exc}",
                key="err.invalid_quantity_detail",
                params={"detail": str(exc)},
            ) from exc
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        who = self.resolve_actor(actor)
        measurer = (measured_by or "").strip() or who
        with self.transaction() as conn:
            tank_row = self._resolve_tank(conn, tank)
            require_writable_day(conn, business_date)
            if (
                self._inventory.get_tank_reading(
                    conn, int(tank_row["id"]), business_date
                )
                is not None
            ):
                raise errors.DuplicateError(
                    f"Tank {tank_row['code']} already has a dip reading for"
                    f" {business_date}; use correct_physical_count to change it.",
                    key="err.dip_already_recorded",
                    params={
                        "code": tank_row["code"],
                        "day_date": business_date,
                    },
                )
            reading_id = self._inventory.insert_tank_reading(
                conn,
                int(tank_row["id"]),
                business_date,
                quantity_ml,
                None,
                measurer,
                note,
                who,
            )
        return reading_id

    def correct_physical_count(
        self,
        tank: str | int,
        business_date: str,
        quantity_litres,
        *,
        reason: str,
        actor: str | None = None,
    ) -> int:
        """Correct a recorded dip reading via an audited UPDATE.

        The reason is mandatory (no silent corrections); the change itself
        is snapshotted old/new by the audit trigger. Missing reading →
        NotFoundError. Returns the reading row id.
        """
        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise errors.ValidationError(
                "A dip-reading correction needs a reason — the change is"
                " audited and the reason explains what changed.",
                key="err.dip_correction_needs_reason",
            )
        try:
            quantity_ml = calc.to_ml(quantity_litres)
        except (TypeError, ValueError) as exc:
            raise errors.ValidationError(
                f"Invalid quantity: {exc}",
                key="err.invalid_quantity_detail",
                params={"detail": str(exc)},
            ) from exc
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        who = self.resolve_actor(actor)
        with self.transaction() as conn:
            tank_row = self._resolve_tank(conn, tank)
            require_writable_day(conn, business_date)
            existing = self._inventory.get_tank_reading(
                conn, int(tank_row["id"]), business_date
            )
            if existing is None:
                raise errors.NotFoundError(
                    f"Tank {tank_row['code']} has no dip reading for"
                    f" {business_date} yet — record it first instead of"
                    " correcting.",
                    key="err.no_dip_to_correct",
                    params={
                        "code": tank_row["code"],
                        "day_date": business_date,
                    },
                )
            if int(existing["quantity_ml"]) == quantity_ml:
                return int(existing["id"])  # nothing to change
            # The correction is attributed to the actor; the mandatory reason
            # authorizes it (a reason column is a future schema amendment).
            self._inventory.update_tank_reading(
                conn,
                int(tank_row["id"]),
                business_date,
                quantity_ml,
                who,
                existing["note"],
            )
            return int(existing["id"])

    def tank_readings(self, business_date: str) -> list[TankReadingInfo]:
        """All dip readings recorded for a business day."""
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            rows = self._inventory.list_tank_readings(conn, business_date)
        return [_to_tank_reading_info(row) for row in rows]

    # ------------------------------------------------------------------
    # adjustments
    # ------------------------------------------------------------------
    def record_adjustment(
        self,
        *,
        fuel_product: str | int,
        tank: str | int,
        business_date: str,
        quantity_litres_signed,
        reason: str,
        authorized_by: str | None = None,
        actor: str | None = None,
    ) -> int:
        """Post an authorized stock adjustment and its ledger movement.

        The quantity is signed (positive = stock found, negative = shortage);
        the reason and an authorizer are mandatory — this is the audited path
        §11.3 requires before any negative-stock sale. The adjustment row +
        its single ``'adjustment'`` movement commit in ONE transaction.
        Returns the adjustment row id.
        """
        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise errors.ValidationError(
                "A stock adjustment needs a reason (what happened and why).",
                key="err.stock_adjustment_needs_reason",
            )
        try:
            signed_ml = _to_signed_ml(quantity_litres_signed, "quantity_litres_signed")
        except (TypeError, ValueError) as exc:
            raise errors.ValidationError(
                str(exc),
                key="err.invalid_quantity_detail",
                params={"detail": str(exc)},
            ) from exc
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        who = self.resolve_actor(actor)
        authorizer = (authorized_by or "").strip() or who
        with self.transaction() as conn:
            prod = resolve_fuel_product(conn, fuel_product)
            tank_row = self._resolve_tank(conn, tank)
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
            adjustment_uuid = _uuid.uuid4().hex
            occurred = timecalc.utc_now_iso()
            adjustment_id = self._inventory.insert_adjustment(
                conn,
                adjustment_uuid,
                int(prod["id"]),
                int(tank_row["id"]),
                business_date,
                signed_ml,
                clean_reason,
                authorizer,
                occurred,
                who,
            )
            self._inventory.insert_movement(
                conn,
                _uuid.uuid4().hex,
                business_date,
                int(prod["id"]),
                int(tank_row["id"]),
                "adjustment",
                signed_ml,
                "inventory_adjustments",
                adjustment_id,
                adjustment_uuid,
                occurred,
                clean_reason,
                who,
            )
        return adjustment_id
