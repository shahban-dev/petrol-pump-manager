"""Repository for suppliers and posted fuel deliveries.

Thin, parameterized SQL only — no business logic, no transaction control.
Deliveries are posted facts (immutable via Group C triggers); suppliers are
mutable master data.
"""

from __future__ import annotations

import sqlite3

_DELIVERY_SELECT = (
    "SELECT d.*, s.name AS supplier_name, p.code AS fuel_product_code,"
    " t.code AS tank_code"
    " FROM fuel_deliveries d"
    " JOIN suppliers s ON s.id = d.supplier_id"
    " JOIN fuel_products p ON p.id = d.fuel_product_id"
    " LEFT JOIN tanks t ON t.id = d.tank_id"
)


class DeliveryRepository:
    """CRUD + queries for the supplier/delivery aggregate."""

    # ------------------------------------------------------------------
    # suppliers
    # ------------------------------------------------------------------
    @staticmethod
    def insert_supplier(
        conn: sqlite3.Connection,
        name: str,
        phone: str | None,
        address: str | None,
        created_by: str,
    ) -> int:
        """Insert a supplier; returns its id."""
        cur = conn.execute(
            "INSERT INTO suppliers (name, phone, address, created_by)"
            " VALUES (?, ?, ?, ?)",
            (name, phone, address, created_by),
        )
        return int(cur.lastrowid)

    @staticmethod
    def list_suppliers(
        conn: sqlite3.Connection, active_only: bool = True
    ) -> list[sqlite3.Row]:
        """Suppliers in id order, optionally active only."""
        sql = "SELECT * FROM suppliers"
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY id"
        return conn.execute(sql).fetchall()

    @staticmethod
    def get_supplier(
        conn: sqlite3.Connection, supplier_id: int
    ) -> sqlite3.Row | None:
        """One supplier by id, or None."""
        return conn.execute(
            "SELECT * FROM suppliers WHERE id = ?", (supplier_id,)
        ).fetchone()

    @staticmethod
    def get_supplier_by_name(
        conn: sqlite3.Connection, name: str
    ) -> sqlite3.Row | None:
        """One supplier by exact name (case-sensitive), or None."""
        return conn.execute(
            "SELECT * FROM suppliers WHERE name = ?", (name,)
        ).fetchone()

    @staticmethod
    def set_supplier_active(
        conn: sqlite3.Connection, supplier_id: int, active: bool
    ) -> None:
        """Flip a supplier's active flag (audited by the au trigger)."""
        conn.execute(
            "UPDATE suppliers SET is_active = ? WHERE id = ?",
            (1 if active else 0, supplier_id),
        )

    # ------------------------------------------------------------------
    # fuel_deliveries
    # ------------------------------------------------------------------
    @staticmethod
    def insert_delivery(
        conn: sqlite3.Connection,
        uuid: str,
        supplier_id: int,
        fuel_product_id: int,
        tank_id: int | None,
        business_date: str,
        quantity_ml: int,
        unit_cost_10k: int,
        total_cost_paisa: int,
        other_cost_paisa: int,
        supplier_invoice_no: str | None,
        amount_paid_paisa: int,
        payment_status: str,
        occurred_at_utc: str | None,
        created_by: str,
    ) -> int:
        """Insert a posted delivery; returns its id.

        ``occurred_at_utc=None`` lets the database default apply (UTC
        ``strftime``); pass an explicit value to pin the event time.
        """
        columns = (
            "uuid, supplier_id, fuel_product_id, tank_id, business_date,"
            " quantity_ml, unit_cost_10k, total_cost_paisa, other_cost_paisa,"
            " supplier_invoice_no, amount_paid_paisa, payment_status,"
            " created_by"
        )
        values = (
            uuid,
            supplier_id,
            fuel_product_id,
            tank_id,
            business_date,
            quantity_ml,
            unit_cost_10k,
            total_cost_paisa,
            other_cost_paisa,
            supplier_invoice_no,
            amount_paid_paisa,
            payment_status,
            created_by,
        )
        if occurred_at_utc is not None:
            columns += ", occurred_at_utc"
            values += (occurred_at_utc,)
        placeholders = ", ".join(["?"] * len(values))
        cur = conn.execute(
            f"INSERT INTO fuel_deliveries ({columns}) VALUES ({placeholders})",
            values,
        )
        return int(cur.lastrowid)

    @staticmethod
    def get_delivery(
        conn: sqlite3.Connection, delivery_id: int
    ) -> sqlite3.Row | None:
        """One delivery with supplier/product/tank names, or None."""
        return conn.execute(
            _DELIVERY_SELECT + " WHERE d.id = ?", (delivery_id,)
        ).fetchone()

    @staticmethod
    def get_delivery_by_uuid(
        conn: sqlite3.Connection, uuid: str
    ) -> sqlite3.Row | None:
        """One delivery by its idempotency uuid, or None."""
        return conn.execute(
            _DELIVERY_SELECT + " WHERE d.uuid = ?", (uuid,)
        ).fetchone()

    @staticmethod
    def list_deliveries(
        conn: sqlite3.Connection,
        business_date: str | None = None,
        supplier_id: int | None = None,
    ) -> list[sqlite3.Row]:
        """Deliveries, optionally filtered by day and/or supplier, oldest
        first."""
        sql = _DELIVERY_SELECT
        clauses: list[str] = []
        params: list = []
        if business_date is not None:
            clauses.append("d.business_date = ?")
            params.append(business_date)
        if supplier_id is not None:
            clauses.append("d.supplier_id = ?")
            params.append(supplier_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY d.id"
        return conn.execute(sql, params).fetchall()

    @staticmethod
    def supplier_totals(
        conn: sqlite3.Connection, supplier_id: int
    ) -> tuple[int, int]:
        """``(invoiced_paisa, paid_paisa)`` for a supplier across all time.

        Invoiced = Σ (fuel total + other costs); paid = Σ amount_paid_paisa.
        """
        row = conn.execute(
            "SELECT COALESCE(SUM(total_cost_paisa + other_cost_paisa), 0),"
            "       COALESCE(SUM(amount_paid_paisa), 0)"
            " FROM fuel_deliveries WHERE supplier_id = ?",
            (supplier_id,),
        ).fetchone()
        return (int(row[0]), int(row[1]))
