"""Repository for supplier payments and computed supplier balances.

Thin, parameterized SQL only — no business logic, no transaction control.
Outstanding amounts are *computed* (invoice totals − paid at delivery −
effective payments), never typed; the Group I trigger caps delivery-linked
payments at the unpaid amount as the backstop.
"""

from __future__ import annotations

import sqlite3

_PAYMENT_SELECT = (
    "SELECT sp.*, s.name AS supplier_name"
    " FROM supplier_payments sp"
    " JOIN suppliers s ON s.id = sp.supplier_id"
)


class SupplierRepository:
    """CRUD + computed balances for supplier payments."""

    # ------------------------------------------------------------------
    # supplier_payments
    # ------------------------------------------------------------------
    @staticmethod
    def insert_payment(
        conn: sqlite3.Connection,
        uuid: str,
        supplier_id: int,
        delivery_id: int | None,
        business_date: str,
        amount_paisa: int,
        method: str,
        reverses_uuid: str | None,
        occurred_at_utc: str,
        created_by: str,
    ) -> int:
        """Insert a posted supplier payment; returns its id."""
        cur = conn.execute(
            "INSERT INTO supplier_payments (uuid, supplier_id, delivery_id,"
            " business_date, amount_paisa, method, reverses_uuid,"
            " occurred_at_utc, created_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid,
                supplier_id,
                delivery_id,
                business_date,
                amount_paisa,
                method,
                reverses_uuid,
                occurred_at_utc,
                created_by,
            ),
        )
        return int(cur.lastrowid)

    @staticmethod
    def get_payment_by_uuid(
        conn: sqlite3.Connection, uuid: str
    ) -> sqlite3.Row | None:
        """One payment with its supplier name, or None."""
        return conn.execute(
            _PAYMENT_SELECT + " WHERE sp.uuid = ?", (uuid,)
        ).fetchone()

    @staticmethod
    def flip_payment_reversed(
        conn: sqlite3.Connection, payment_id: int, reversed_by_uuid: str
    ) -> None:
        """Whitelisted posted → reversed flip (Group C trigger)."""
        conn.execute(
            "UPDATE supplier_payments SET status = 'reversed',"
            " reversed_by_uuid = ? WHERE id = ?",
            (reversed_by_uuid, payment_id),
        )

    @staticmethod
    def list_payments(
        conn: sqlite3.Connection,
        supplier_id: int | None = None,
        business_date: str | None = None,
    ) -> list[sqlite3.Row]:
        """Payments, optionally filtered, chronological."""
        sql = _PAYMENT_SELECT
        clauses: list[str] = []
        params: list = []
        if supplier_id is not None:
            clauses.append("sp.supplier_id = ?")
            params.append(supplier_id)
        if business_date is not None:
            clauses.append("sp.business_date = ?")
            params.append(business_date)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY sp.occurred_at_utc, sp.id"
        return conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------------
    # computed balances
    # ------------------------------------------------------------------
    @staticmethod
    def supplier_balances(conn: sqlite3.Connection) -> list[sqlite3.Row]:
        """Every supplier's computed balance, ordered by name."""
        return conn.execute(
            "SELECT * FROM v_supplier_balances ORDER BY supplier_name"
        ).fetchall()

    @staticmethod
    def effective_payments_for_delivery(
        conn: sqlite3.Connection, delivery_id: int
    ) -> int:
        """Net (posted − reversed) payments linked to one delivery."""
        row = conn.execute(
            "SELECT COALESCE(SUM(CASE"
            "  WHEN reverses_uuid IS NOT NULL THEN -amount_paisa"
            "  ELSE amount_paisa END), 0) AS total"
            " FROM supplier_payments WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
        return int(row["total"])

    @staticmethod
    def deliveries_with_outstanding(
        conn: sqlite3.Connection, supplier_id: int | None = None
    ) -> list[sqlite3.Row]:
        """Deliveries with their computed outstanding amounts, oldest first."""
        sql = (
            "SELECT d.*, s.name AS supplier_name,"
            " p.code AS fuel_product_code, t.code AS tank_code,"
            " (d.total_cost_paisa + COALESCE(d.other_cost_paisa, 0)"
            "  - COALESCE(d.amount_paid_paisa, 0)"
            "  - COALESCE((SELECT SUM(CASE"
            "       WHEN sp.reverses_uuid IS NOT NULL THEN -sp.amount_paisa"
            "       ELSE sp.amount_paisa END)"
            "     FROM supplier_payments sp"
            "     WHERE sp.delivery_id = d.id), 0)) AS outstanding_paisa"
            " FROM fuel_deliveries d"
            " JOIN suppliers s ON s.id = d.supplier_id"
            " JOIN fuel_products p ON p.id = d.fuel_product_id"
            " LEFT JOIN tanks t ON t.id = d.tank_id"
        )
        params: list = []
        if supplier_id is not None:
            sql += " WHERE d.supplier_id = ?"
            params.append(supplier_id)
        sql += " ORDER BY d.business_date, d.id"
        return conn.execute(sql, params).fetchall()
