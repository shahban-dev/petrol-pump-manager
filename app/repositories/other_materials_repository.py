"""Repository for other products and their immutable transactions.

Thin, parameterized SQL only — no business logic, no transaction control.
On-hand stock is always read from ``v_other_product_stock`` (computed),
never stored.
"""

from __future__ import annotations

import sqlite3

_TXN_SELECT = (
    "SELECT t.*, p.name AS product_name"
    " FROM other_product_transactions t"
    " JOIN other_products p ON p.id = t.product_id"
)


class OtherMaterialsRepository:
    """CRUD for other products; inserts + readers for transactions."""

    # ------------------------------------------------------------------
    # other_products
    # ------------------------------------------------------------------
    @staticmethod
    def list_products(
        conn: sqlite3.Connection, active_only: bool = True
    ) -> list[sqlite3.Row]:
        """Products, optionally active only, in id order."""
        sql = "SELECT * FROM other_products"
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY id"
        return conn.execute(sql).fetchall()

    @staticmethod
    def get_product(
        conn: sqlite3.Connection, product_id: int
    ) -> sqlite3.Row | None:
        """One product by id, or None."""
        return conn.execute(
            "SELECT * FROM other_products WHERE id = ?", (product_id,)
        ).fetchone()

    @staticmethod
    def get_product_by_name(
        conn: sqlite3.Connection, name: str
    ) -> sqlite3.Row | None:
        """One product by exact name, or None."""
        return conn.execute(
            "SELECT * FROM other_products WHERE name = ?", (name,)
        ).fetchone()

    @staticmethod
    def insert_product(
        conn: sqlite3.Connection,
        name: str,
        unit: str,
        opening_value_paisa: int,
        created_by: str,
    ) -> int:
        """Insert a product; returns its id."""
        cur = conn.execute(
            "INSERT INTO other_products (name, unit, opening_value_paisa,"
            " created_by) VALUES (?, ?, ?, ?)",
            (name, unit, opening_value_paisa, created_by),
        )
        return int(cur.lastrowid)

    @staticmethod
    def set_product_active(
        conn: sqlite3.Connection, product_id: int, active: bool
    ) -> None:
        """Flip a product's active flag (audited)."""
        conn.execute(
            "UPDATE other_products SET is_active = ? WHERE id = ?",
            (1 if active else 0, product_id),
        )

    # ------------------------------------------------------------------
    # other_product_transactions
    # ------------------------------------------------------------------
    @staticmethod
    def insert_transaction(
        conn: sqlite3.Connection,
        uuid: str,
        business_date: str,
        product_id: int,
        txn_type: str,
        quantity_units: int,
        unit_price_paisa: int,
        total_paisa: int,
        method: str,
        reference: str | None,
        occurred_at_utc: str,
        created_by: str,
    ) -> int:
        """Insert an immutable transaction; returns its id."""
        cur = conn.execute(
            "INSERT INTO other_product_transactions (uuid, business_date,"
            " product_id, txn_type, quantity_units, unit_price_paisa,"
            " total_paisa, method, reference, occurred_at_utc, created_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid,
                business_date,
                product_id,
                txn_type,
                quantity_units,
                unit_price_paisa,
                total_paisa,
                method,
                reference,
                occurred_at_utc,
                created_by,
            ),
        )
        return int(cur.lastrowid)

    @staticmethod
    def list_transactions(
        conn: sqlite3.Connection,
        product_id: int | None = None,
        business_date: str | None = None,
    ) -> list[sqlite3.Row]:
        """Transactions, optionally filtered, chronological."""
        sql = _TXN_SELECT
        clauses: list[str] = []
        params: list = []
        if product_id is not None:
            clauses.append("t.product_id = ?")
            params.append(product_id)
        if business_date is not None:
            clauses.append("t.business_date = ?")
            params.append(business_date)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY t.occurred_at_utc, t.id"
        return conn.execute(sql, params).fetchall()

    @staticmethod
    def stock_levels(
        conn: sqlite3.Connection, product_id: int | None = None
    ) -> list[sqlite3.Row]:
        """Computed on-hand units per product (from the view)."""
        sql = (
            "SELECT p.*, COALESCE(v.on_hand_units, 0) AS on_hand_units"
            " FROM other_products p"
            " LEFT JOIN v_other_product_stock v"
            "  ON v.product_id = p.id"
        )
        params: list = []
        if product_id is not None:
            sql += " WHERE p.id = ?"
            params.append(product_id)
        sql += " ORDER BY p.id"
        return conn.execute(sql, params).fetchall()

    @staticmethod
    def purchase_totals(
        conn: sqlite3.Connection,
        product_id: int,
        business_date: str | None = None,
    ) -> sqlite3.Row:
        """Σ quantity and Σ total over purchase transactions.

        With ``business_date`` set the totals cover that day only; without
        it they cover the product's lifetime (used for average costing).
        """
        sql = (
            "SELECT COALESCE(SUM(quantity_units), 0) AS qty_units,"
            " COALESCE(SUM(total_paisa), 0) AS total_paisa"
            " FROM other_product_transactions"
            " WHERE product_id = ? AND txn_type = 'purchase'"
        )
        params: list = [product_id]
        if business_date is not None:
            sql += " AND business_date = ?"
            params.append(business_date)
        return conn.execute(sql, params).fetchone()

    @staticmethod
    def sale_quantity(
        conn: sqlite3.Connection, product_id: int, business_date: str
    ) -> int:
        """Σ sale units for a product on one day."""
        row = conn.execute(
            "SELECT COALESCE(SUM(quantity_units), 0) AS qty"
            " FROM other_product_transactions"
            " WHERE product_id = ? AND txn_type = 'sale'"
            " AND business_date = ?",
            (product_id, business_date),
        ).fetchone()
        return int(row["qty"])

    @staticmethod
    def sale_revenue(
        conn: sqlite3.Connection, business_date: str
    ) -> int:
        """Σ sale totals for one day (other-product revenue)."""
        row = conn.execute(
            "SELECT COALESCE(SUM(total_paisa), 0) AS total"
            " FROM other_product_transactions"
            " WHERE txn_type = 'sale' AND business_date = ?",
            (business_date,),
        ).fetchone()
        return int(row["total"])
