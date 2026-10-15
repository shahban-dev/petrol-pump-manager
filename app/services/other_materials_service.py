"""Other products: engine oil, lubricants, similar (Phase 5).

Stock is always *computed* (``v_other_product_stock``), never typed.
``purchase`` (+) and ``sale`` (−) are immutable facts; a cash purchase/sale
posts exactly one derived cash-book row in the same transaction.
``adjustment`` (±, signed, reasoned) corrects stock with no cash movement.
A sale may never exceed on-hand units (design §11.3 analogue).
"""

from __future__ import annotations

import sqlite3
import uuid as _uuid

from app.calculations import time as timecalc
from app.models.other_materials import OtherProductInfo, OtherTxnInfo
from app.repositories.cashbook_repository import CashbookRepository
from app.repositories.other_materials_repository import OtherMaterialsRepository
from app.services import errors
from app.services._base import ServiceBase, require_writable_day

_METHODS = ("cash", "bank", "other")


def _to_product_info(row, on_hand_units: int | None = None) -> OtherProductInfo:
    return OtherProductInfo(
        id=int(row["id"]),
        name=row["name"],
        unit=row["unit"],
        opening_value_paisa=int(row["opening_value_paisa"]),
        is_active=bool(row["is_active"]),
        on_hand_units=on_hand_units,
    )


def _to_txn_info(row) -> OtherTxnInfo:
    return OtherTxnInfo(
        id=int(row["id"]),
        uuid=row["uuid"],
        business_date=row["business_date"],
        product_id=int(row["product_id"]),
        product_name=row["product_name"],
        txn_type=row["txn_type"],
        quantity_units=int(row["quantity_units"]),
        unit_price_paisa=int(row["unit_price_paisa"]),
        total_paisa=int(row["total_paisa"]),
        method=row["method"],
        reference=row["reference"],
        occurred_at_utc=row["occurred_at_utc"],
        created_by=row["created_by"],
    )


class OtherMaterialsService(ServiceBase):
    """Manages other products and their purchase/sale/adjustment facts."""

    def __init__(self, db_manager, actor: str | None = None) -> None:
        super().__init__(db_manager, actor)
        self._products = OtherMaterialsRepository()
        self._cash = CashbookRepository()

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

    def _resolve_product(
        self, conn: sqlite3.Connection, product: str | int, *, active_only: bool = True
    ):
        """Product row by id or exact name; active required for postings."""
        if isinstance(product, bool):
            raise errors.ValidationError(
                f"Invalid product reference: {product!r}.",
                key="err.invalid_product_ref2",
                params={"ref": product},
            )
        row = None
        if isinstance(product, int):
            row = self._products.get_product(conn, product)
        elif isinstance(product, str):
            clean = product.strip()
            if not clean:
                raise errors.ValidationError(
                "Product name must not be empty.",
                key="err.product_name_empty",
            )
            row = self._products.get_product_by_name(conn, clean)
        else:
            raise errors.ValidationError(
                f"Invalid product reference: {product!r}.",
                key="err.invalid_product_ref2",
                params={"ref": product},
            )
        if row is None:
            raise errors.NotFoundError(
                f"Unknown product: {product!r}.",
                key="err.unknown_product2",
                params={"ref": product},
            )
        if active_only and not row["is_active"]:
            raise errors.ValidationError(
                f"Product {row['name']!r} is inactive.",
                key="err.product_inactive",
                params={"name": row["name"]},
            )
        return row

    @staticmethod
    def _require_units(quantity_units, name: str, *, signed: bool = False) -> int:
        if isinstance(quantity_units, bool) or not isinstance(quantity_units, int):
            raise errors.ValidationError(
                f"{name} must be an integer.",
                key="err.units_must_be_integer",
                params={"name": name},
            )
        if signed:
            if quantity_units == 0:
                raise errors.ValidationError(
                    f"{name} must not be zero.",
                    key="err.units_not_zero",
                    params={"name": name},
                )
        elif quantity_units <= 0:
            raise errors.ValidationError(
                f"{name} must be positive.",
                key="err.units_positive",
                params={"name": name},
            )
        return quantity_units

    @staticmethod
    def _require_unit_price(unit_price_paisa) -> int:
        if isinstance(unit_price_paisa, bool) or not isinstance(
            unit_price_paisa, int
        ):
            raise errors.ValidationError(
                "unit_price_paisa must be integer paisa.",
                key="err.unit_price_paisa_integer",
            )
        if unit_price_paisa < 0:
            raise errors.ValidationError(
                "unit_price_paisa must not be negative.",
                key="err.unit_price_not_negative",
            )
        return unit_price_paisa

    # ------------------------------------------------------------------
    # products (master data)
    # ------------------------------------------------------------------
    def list_products(self, active_only: bool = True) -> list[OtherProductInfo]:
        """Products with computed on-hand units."""
        with self.read_connection() as conn:
            rows = self._products.stock_levels(conn)
        out = []
        for row in rows:
            if active_only and not row["is_active"]:
                continue
            out.append(_to_product_info(row, int(row["on_hand_units"])))
        return out

    def add_product(
        self,
        name: str,
        unit: str = "piece",
        opening_value_paisa: int = 0,
        *,
        actor: str | None = None,
    ) -> int:
        """Add an other product. The name is mandatory and unique."""
        clean = (name or "").strip()
        if not clean:
            raise errors.ValidationError(
                "Product name must not be empty.",
                key="err.product_name_empty",
            )
        clean_unit = (unit or "").strip()
        if not clean_unit:
            raise errors.ValidationError(
                "Product unit must not be empty.",
                key="err.product_unit_empty",
            )
        value = self._require_unit_price(opening_value_paisa)
        who = self.resolve_actor(actor)
        with self.transaction() as conn:
            if self._products.get_product_by_name(conn, clean) is not None:
                raise errors.DuplicateError(
                    f"A product named {clean!r} already exists.",
                    key="err.product_exists",
                    params={"name": clean},
                )
            return self._products.insert_product(
                conn, clean, clean_unit, value, who
            )

    def set_product_active(
        self, product_id: int, active: bool, *, actor: str | None = None
    ) -> None:
        """Activate/deactivate a product. Unknown id → NotFoundError."""
        with self.transaction() as conn:
            row = self._products.get_product(conn, int(product_id))
            if row is None:
                raise errors.NotFoundError(
                    f"Unknown product: {product_id!r}.",
                    key="err.unknown_product2",
                    params={"ref": product_id},
                )
            self._products.set_product_active(conn, int(row["id"]), bool(active))

    # ------------------------------------------------------------------
    # transactions
    # ------------------------------------------------------------------
    def _insert_txn(
        self,
        conn: sqlite3.Connection,
        business_date: str,
        product_id: int,
        txn_type: str,
        quantity_units: int,
        unit_price_paisa: int,
        method: str,
        reference: str | None,
        stamp: str,
        who: str,
    ) -> str:
        """Insert the transaction row (+ derived cash row for cash); uuid."""
        total_paisa = quantity_units * unit_price_paisa
        txn_uuid = _uuid.uuid4().hex
        self._products.insert_transaction(
            conn,
            txn_uuid,
            business_date,
            product_id,
            txn_type,
            quantity_units,
            unit_price_paisa,
            total_paisa,
            method,
            reference,
            stamp,
            who,
        )
        txn_id = int(
            conn.execute(
                "SELECT id FROM other_product_transactions WHERE uuid = ?",
                (txn_uuid,),
            ).fetchone()["id"]
        )
        if method == "cash" and txn_type in ("purchase", "sale"):
            if txn_type == "purchase":
                cash_cat = self._cash.get_category(
                    conn, "payment", "other_materials_purchase"
                )
                if cash_cat is None or not cash_cat["is_active"]:
                    raise errors.BusinessRuleError(
                        "Cash category 'other_materials_purchase' is missing"
                        " or inactive; cannot post the derived cash row.",
                        key="err.derived_cash_category_missing",
                        params={"category": "other_materials_purchase"},
                    )
                self._cash.insert_payment(
                    conn,
                    _uuid.uuid4().hex,
                    business_date,
                    int(cash_cat["id"]),
                    total_paisa,
                    None,
                    "other_product_transactions",
                    txn_id,
                    reference,
                    None,
                    stamp,
                    who,
                )
            else:
                cash_cat = self._cash.get_category(conn, "receipt", "other_sales")
                if cash_cat is None or not cash_cat["is_active"]:
                    raise errors.BusinessRuleError(
                        "Cash category 'other_sales' is missing or inactive;"
                        " cannot post the derived cash row.",
                        key="err.derived_cash_category_missing",
                        params={"category": "other_sales"},
                    )
                self._cash.insert_receipt(
                    conn,
                    _uuid.uuid4().hex,
                    business_date,
                    int(cash_cat["id"]),
                    total_paisa,
                    "other_product_transactions",
                    txn_id,
                    None,
                    reference,
                    None,
                    stamp,
                    who,
                )
        return txn_uuid

    def record_purchase(
        self,
        *,
        product: str | int,
        business_date: str,
        quantity_units: int,
        unit_price_paisa: int,
        method: str = "cash",
        reference: str | None = None,
        occurred_at_utc: str | None = None,
        actor: str | None = None,
    ) -> OtherTxnInfo:
        """Record a purchase (stock +, exact ``qty × price`` total).

        ``method='cash'`` also writes the derived ``cash_payments`` row
        (category ``'other_materials_purchase'``) in the SAME transaction.
        """
        quantity = self._require_units(quantity_units, "quantity_units")
        price = self._require_unit_price(unit_price_paisa)
        if method not in _METHODS:
            raise errors.ValidationError(
                f"Unknown method: {method!r} (cash|bank|other).",
                key="err.unknown_method",
                params={"method": method},
            )
        stamp = self._require_stamp_on_day(occurred_at_utc, business_date)
        who = self.resolve_actor(actor)
        clean_reference = (reference or "").strip() or None
        with self.transaction() as conn:
            require_writable_day(conn, business_date)
            row = self._resolve_product(conn, product)
            txn_uuid = self._insert_txn(
                conn,
                business_date,
                int(row["id"]),
                "purchase",
                quantity,
                price,
                method,
                clean_reference,
                stamp,
                who,
            )
            txn = self._products.list_transactions(conn, int(row["id"]), None)
            return _to_txn_info(
                next(t for t in txn if t["uuid"] == txn_uuid)
            )

    def record_sale(
        self,
        *,
        product: str | int,
        business_date: str,
        quantity_units: int,
        unit_price_paisa: int,
        method: str = "cash",
        reference: str | None = None,
        occurred_at_utc: str | None = None,
        actor: str | None = None,
    ) -> OtherTxnInfo:
        """Record a sale (stock −; quantity may not exceed on-hand).

        ``method='cash'`` also writes the derived ``cash_receipts`` row
        (category ``'other_sales'``) in the SAME transaction.
        """
        quantity = self._require_units(quantity_units, "quantity_units")
        price = self._require_unit_price(unit_price_paisa)
        if method not in _METHODS:
            raise errors.ValidationError(
                f"Unknown method: {method!r} (cash|bank|other).",
                key="err.unknown_method",
                params={"method": method},
            )
        stamp = self._require_stamp_on_day(occurred_at_utc, business_date)
        who = self.resolve_actor(actor)
        clean_reference = (reference or "").strip() or None
        with self.transaction() as conn:
            require_writable_day(conn, business_date)
            row = self._resolve_product(conn, product)
            on_hand = int(
                self._products.stock_levels(conn, int(row["id"]))[0][
                    "on_hand_units"
                ]
            )
            if quantity > on_hand:
                raise errors.BusinessRuleError(
                    f"Cannot sell {quantity} {row['unit']} of"
                    f" {row['name']!r}: only {on_hand} on hand.",
                    key="err.insufficient_stock",
                    params={
                        "quantity": quantity,
                        "unit": row["unit"],
                        "name": row["name"],
                        "on_hand": on_hand,
                    },
                )
            txn_uuid = self._insert_txn(
                conn,
                business_date,
                int(row["id"]),
                "sale",
                quantity,
                price,
                method,
                clean_reference,
                stamp,
                who,
            )
            txn = self._products.list_transactions(conn, int(row["id"]), None)
            return _to_txn_info(
                next(t for t in txn if t["uuid"] == txn_uuid)
            )

    def record_adjustment(
        self,
        *,
        product: str | int,
        business_date: str,
        quantity_units: int,
        reason: str,
        reference: str | None = None,
        occurred_at_utc: str | None = None,
        actor: str | None = None,
    ) -> OtherTxnInfo:
        """Record a signed stock adjustment (±, reasoned). No cash movement.

        A non-empty reason is required; the adjustment is audited.
        """
        quantity = self._require_units(
            quantity_units, "quantity_units", signed=True
        )
        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise errors.ValidationError(
                "A reason is required for a stock adjustment.",
                key="err.adjustment_needs_reason2",
            )
        stamp = self._require_stamp_on_day(occurred_at_utc, business_date)
        who = self.resolve_actor(actor)
        clean_reference = (reference or "").strip() or None
        note = (
            f"{clean_reference} — {clean_reason}"
            if clean_reference
            else clean_reason
        )
        with self.transaction() as conn:
            require_writable_day(conn, business_date)
            row = self._resolve_product(conn, product)
            txn_uuid = self._insert_txn(
                conn,
                business_date,
                int(row["id"]),
                "adjustment",
                quantity,
                0,
                "cash",
                note,
                stamp,
                who,
            )
            txn = self._products.list_transactions(conn, int(row["id"]), None)
            return _to_txn_info(
                next(t for t in txn if t["uuid"] == txn_uuid)
            )

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def stock(self, product_id: int | None = None) -> list[OtherProductInfo]:
        """Computed on-hand units per product (optionally one product)."""
        with self.read_connection() as conn:
            rows = self._products.stock_levels(conn, product_id)
        return [_to_product_info(row, int(row["on_hand_units"])) for row in rows]

    def list_transactions(
        self,
        *,
        product_id: int | None = None,
        business_date: str | None = None,
    ) -> list[OtherTxnInfo]:
        """Transactions, optionally filtered by product and/or day."""
        if business_date is not None and not timecalc.is_valid_business_date(
            business_date
        ):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            rows = self._products.list_transactions(
                conn, product_id=product_id, business_date=business_date
            )
        return [_to_txn_info(row) for row in rows]
