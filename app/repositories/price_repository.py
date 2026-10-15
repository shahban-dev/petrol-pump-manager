"""Repository for fuel products and the immutable price timeline.

Thin, parameterized SQL only — no business logic, no transaction control.
"Effective" price = the row with the greatest ``effective_from_utc`` not
later than the queried timestamp; old rows are never edited or deleted.
"""

from __future__ import annotations

import sqlite3

_PRICE_SELECT = (
    "SELECT ph.*, p.code AS fuel_product_code"
    " FROM price_history ph JOIN fuel_products p ON p.id = ph.fuel_product_id"
)


class PriceRepository:
    """CRUD + queries for fuel products and ``price_history``."""

    # ------------------------------------------------------------------
    # fuel_products (master data)
    # ------------------------------------------------------------------
    @staticmethod
    def list_products(conn: sqlite3.Connection) -> list[sqlite3.Row]:
        """All fuel products: id, code, name_en, is_active — in id order."""
        return conn.execute(
            "SELECT id, code, name_en, is_active FROM fuel_products ORDER BY id"
        ).fetchall()

    @staticmethod
    def get_product(
        conn: sqlite3.Connection, product_id: int
    ) -> sqlite3.Row | None:
        """One fuel product by id, or None."""
        return conn.execute(
            "SELECT id, code, name_en, is_active FROM fuel_products"
            " WHERE id = ?",
            (product_id,),
        ).fetchone()

    @staticmethod
    def get_product_by_code(
        conn: sqlite3.Connection, code: str
    ) -> sqlite3.Row | None:
        """One fuel product by exact code (case-sensitive), or None."""
        return conn.execute(
            "SELECT id, code, name_en, is_active FROM fuel_products"
            " WHERE code = ?",
            (code,),
        ).fetchone()

    # ------------------------------------------------------------------
    # price_history (immutable timeline)
    # ------------------------------------------------------------------
    @staticmethod
    def insert(
        conn: sqlite3.Connection,
        fuel_product_id: int,
        effective_from_utc: str,
        unit_price_10k: int,
        business_date: str,
        created_by: str,
    ) -> int:
        """Append a price row; returns its id. Old rows are never touched."""
        cur = conn.execute(
            "INSERT INTO price_history"
            " (fuel_product_id, effective_from_utc, unit_price_10k,"
            "  business_date, created_by)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                fuel_product_id,
                effective_from_utc,
                unit_price_10k,
                business_date,
                created_by,
            ),
        )
        return int(cur.lastrowid)

    @staticmethod
    def get_effective(
        conn: sqlite3.Connection, fuel_product_id: int, at_utc: str
    ) -> sqlite3.Row | None:
        """The price row effective at *at_utc* (latest
        ``effective_from_utc`` <= *at_utc*), or None when no price exists yet.
        """
        return conn.execute(
            _PRICE_SELECT
            + " WHERE ph.fuel_product_id = ? AND ph.effective_from_utc <= ?"
            " ORDER BY ph.effective_from_utc DESC, ph.id DESC LIMIT 1",
            (fuel_product_id, at_utc),
        ).fetchone()

    @staticmethod
    def latest(
        conn: sqlite3.Connection, fuel_product_id: int
    ) -> sqlite3.Row | None:
        """The newest price row for a product (greatest effective_from_utc),
        or None."""
        return conn.execute(
            _PRICE_SELECT
            + " WHERE ph.fuel_product_id = ?"
            " ORDER BY ph.effective_from_utc DESC, ph.id DESC LIMIT 1",
            (fuel_product_id,),
        ).fetchone()

    @staticmethod
    def get_effective_strict(
        conn: sqlite3.Connection, fuel_product_id: int, at_utc: str
    ) -> sqlite3.Row | None:
        """The price row strictly before *at_utc* (greatest
        ``effective_from_utc`` < *at_utc*), or None.

        Sale intervals use this: a price change takes effect *after* the
        snapshot reading that authorizes it (§11.2), so an interval ending
        exactly at the change moment is still priced at the old price —
        today's price is never applied retrospectively.
        """
        return conn.execute(
            _PRICE_SELECT
            + " WHERE ph.fuel_product_id = ? AND ph.effective_from_utc < ?"
            " ORDER BY ph.effective_from_utc DESC, ph.id DESC LIMIT 1",
            (fuel_product_id, at_utc),
        ).fetchone()

    @staticmethod
    def exists_at(
        conn: sqlite3.Connection, fuel_product_id: int, effective_from_utc: str
    ) -> bool:
        """True when a price row already exists for the exact timestamp."""
        row = conn.execute(
            "SELECT 1 FROM price_history"
            " WHERE fuel_product_id = ? AND effective_from_utc = ? LIMIT 1",
            (fuel_product_id, effective_from_utc),
        ).fetchone()
        return row is not None

    @staticmethod
    def list_for_product(
        conn: sqlite3.Connection, fuel_product_id: int
    ) -> list[sqlite3.Row]:
        """All price rows for a product, newest effective time first."""
        return conn.execute(
            _PRICE_SELECT
            + " WHERE ph.fuel_product_id = ?"
            " ORDER BY ph.effective_from_utc DESC, ph.id DESC",
            (fuel_product_id,),
        ).fetchall()
