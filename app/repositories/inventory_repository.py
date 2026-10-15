"""Repository for tanks, the stock ledger, adjustments and dip readings.

Thin, parameterized SQL only — no business logic, no transaction control.
Movements and adjustments are posted facts (immutable via Group C);
tank readings are mutable (audited updates).
"""

from __future__ import annotations

import sqlite3

_TANK_SELECT = (
    "SELECT t.*, p.code AS fuel_product_code"
    " FROM tanks t JOIN fuel_products p ON p.id = t.fuel_product_id"
)

_TANK_READING_SELECT = (
    "SELECT r.*, t.code AS tank_code"
    " FROM tank_readings r JOIN tanks t ON t.id = r.tank_id"
)


class InventoryRepository:
    """CRUD + queries for the inventory aggregate."""

    # ------------------------------------------------------------------
    # tanks
    # ------------------------------------------------------------------
    @staticmethod
    def list_tanks(
        conn: sqlite3.Connection, active_only: bool = True
    ) -> list[sqlite3.Row]:
        """Tanks with their fuel product, in id order."""
        sql = _TANK_SELECT
        if active_only:
            sql += " WHERE t.is_active = 1"
        sql += " ORDER BY t.id"
        return conn.execute(sql).fetchall()

    @staticmethod
    def get_tank(conn: sqlite3.Connection, tank_id: int) -> sqlite3.Row | None:
        """One tank with its fuel product, or None."""
        return conn.execute(_TANK_SELECT + " WHERE t.id = ?", (tank_id,)).fetchone()

    @staticmethod
    def get_tank_by_code(
        conn: sqlite3.Connection, code: str
    ) -> sqlite3.Row | None:
        """One tank looked up by its (unique) code, or None."""
        return conn.execute(
            _TANK_SELECT + " WHERE t.code = ?", (code,)
        ).fetchone()

    # ------------------------------------------------------------------
    # inventory_movements (append-only stock ledger)
    # ------------------------------------------------------------------
    @staticmethod
    def insert_movement(
        conn: sqlite3.Connection,
        uuid: str,
        business_date: str,
        fuel_product_id: int,
        tank_id: int,
        movement_type: str,
        quantity_ml: int,
        source_table: str,
        source_id: int | None,
        source_uuid: str,
        occurred_at_utc: str | None,
        reason: str | None,
        created_by: str,
    ) -> int:
        """Append one stock movement; returns its id.

        ``occurred_at_utc=None`` lets the database default apply; pass an
        explicit value to pin the event time (e.g. the sale interval's).
        """
        columns = (
            "uuid, business_date, fuel_product_id, tank_id, movement_type,"
            " quantity_ml, source_table, source_id, source_uuid, reason,"
            " created_by"
        )
        values: tuple = (
            uuid,
            business_date,
            fuel_product_id,
            tank_id,
            movement_type,
            quantity_ml,
            source_table,
            source_id,
            source_uuid,
            reason,
            created_by,
        )
        if occurred_at_utc is not None:
            columns += ", occurred_at_utc"
            values += (occurred_at_utc,)
        placeholders = ", ".join(["?"] * len(values))
        cur = conn.execute(
            f"INSERT INTO inventory_movements ({columns})"
            f" VALUES ({placeholders})",
            values,
        )
        return int(cur.lastrowid)

    @staticmethod
    def movement_exists(conn: sqlite3.Connection, source_uuid: str) -> bool:
        """True when a movement with this idempotency key is already posted."""
        row = conn.execute(
            "SELECT 1 FROM inventory_movements WHERE source_uuid = ? LIMIT 1",
            (source_uuid,),
        ).fetchone()
        return row is not None

    @staticmethod
    def get_movement_by_source_uuid(
        conn: sqlite3.Connection, source_uuid: str
    ) -> sqlite3.Row | None:
        """The movement row for an idempotency key, or None."""
        return conn.execute(
            "SELECT * FROM inventory_movements WHERE source_uuid = ? LIMIT 1",
            (source_uuid,),
        ).fetchone()

    @staticmethod
    def sum_corrections_for_interval(
        conn: sqlite3.Connection, correction_prefix: str
    ) -> int:
        """Signed Σ quantity_ml of correction adjustments for one sale interval.

        Correction rows use source_uuid values starting with
        ``correction_prefix`` (see sales_service). Returns 0 when none exist.
        """
        row = conn.execute(
            "SELECT COALESCE(SUM(quantity_ml), 0) FROM inventory_movements"
            " WHERE source_uuid LIKE ? ESCAPE '\\'",
            (correction_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def count_corrections_for_interval(
        conn: sqlite3.Connection, correction_prefix: str
    ) -> int:
        """Number of correction rows already posted for one sale interval."""
        row = conn.execute(
            "SELECT COUNT(*) FROM inventory_movements"
            " WHERE source_uuid LIKE ? ESCAPE '\\'",
            (correction_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def sum_movements(
        conn: sqlite3.Connection,
        fuel_product_id: int,
        business_date: str,
        tank_id: int | None = None,
        movement_type: str | None = None,
    ) -> int:
        """Signed Σ quantity_ml for the given scope (0 when no rows)."""
        sql = (
            "SELECT COALESCE(SUM(quantity_ml), 0) FROM inventory_movements"
            " WHERE fuel_product_id = ? AND business_date = ?"
        )
        params: list = [fuel_product_id, business_date]
        if tank_id is not None:
            sql += " AND tank_id = ?"
            params.append(tank_id)
        if movement_type is not None:
            sql += " AND movement_type = ?"
            params.append(movement_type)
        row = conn.execute(sql, params).fetchone()
        return int(row[0])

    @staticmethod
    def count_movements(conn: sqlite3.Connection) -> int:
        """Total movement rows in the ledger."""
        row = conn.execute("SELECT COUNT(*) FROM inventory_movements").fetchone()
        return int(row[0])

    @staticmethod
    def count_movements_for_day(
        conn: sqlite3.Connection,
        business_date: str,
        movement_type: str | None = None,
    ) -> int:
        """Movement rows for one business date, optionally of one type."""
        sql = "SELECT COUNT(*) FROM inventory_movements WHERE business_date = ?"
        params: list = [business_date]
        if movement_type is not None:
            sql += " AND movement_type = ?"
            params.append(movement_type)
        row = conn.execute(sql, params).fetchone()
        return int(row[0])

    # ------------------------------------------------------------------
    # inventory_adjustments
    # ------------------------------------------------------------------
    @staticmethod
    def insert_adjustment(
        conn: sqlite3.Connection,
        uuid: str,
        fuel_product_id: int,
        tank_id: int,
        business_date: str,
        quantity_ml: int,
        reason: str,
        authorized_by: str,
        occurred_at_utc: str | None,
        created_by: str,
    ) -> int:
        """Insert an authorized stock adjustment; returns its id."""
        columns = (
            "uuid, fuel_product_id, tank_id, business_date, quantity_ml,"
            " reason, authorized_by, created_by"
        )
        values: tuple = (
            uuid,
            fuel_product_id,
            tank_id,
            business_date,
            quantity_ml,
            reason,
            authorized_by,
            created_by,
        )
        if occurred_at_utc is not None:
            columns += ", occurred_at_utc"
            values += (occurred_at_utc,)
        placeholders = ", ".join(["?"] * len(values))
        cur = conn.execute(
            f"INSERT INTO inventory_adjustments ({columns})"
            f" VALUES ({placeholders})",
            values,
        )
        return int(cur.lastrowid)

    # ------------------------------------------------------------------
    # tank_readings (mutable dip readings)
    # ------------------------------------------------------------------
    @staticmethod
    def insert_tank_reading(
        conn: sqlite3.Connection,
        tank_id: int,
        business_date: str,
        quantity_ml: int,
        occurred_at_utc: str | None,
        measured_by: str,
        note: str | None,
        created_by: str,
    ) -> int:
        """Insert a dip reading; returns its id. One per tank per day
        (UNIQUE guard); ``occurred_at_utc=None`` uses the DB default."""
        columns = (
            "tank_id, business_date, quantity_ml, measured_by, note, created_by"
        )
        values: tuple = (
            tank_id,
            business_date,
            quantity_ml,
            measured_by,
            note,
            created_by,
        )
        if occurred_at_utc is not None:
            columns += ", occurred_at_utc"
            values += (occurred_at_utc,)
        placeholders = ", ".join(["?"] * len(values))
        cur = conn.execute(
            f"INSERT INTO tank_readings ({columns}) VALUES ({placeholders})",
            values,
        )
        return int(cur.lastrowid)

    @staticmethod
    def get_tank_reading(
        conn: sqlite3.Connection, tank_id: int, business_date: str
    ) -> sqlite3.Row | None:
        """The dip reading for a tank+date, or None."""
        return conn.execute(
            _TANK_READING_SELECT + " WHERE r.tank_id = ? AND r.business_date = ?",
            (tank_id, business_date),
        ).fetchone()

    @staticmethod
    def update_tank_reading(
        conn: sqlite3.Connection,
        tank_id: int,
        business_date: str,
        quantity_ml: int,
        measured_by: str,
        note: str | None,
    ) -> None:
        """Audited correction of a dip reading: quantity, measurer, note and
        the measurement time are replaced (the au trigger snapshots old/new).
        """
        conn.execute(
            "UPDATE tank_readings"
            " SET quantity_ml = ?, measured_by = ?, note = ?,"
            "     occurred_at_utc = strftime('%Y-%m-%dT%H:%M:%fZ','now')"
            " WHERE tank_id = ? AND business_date = ?",
            (quantity_ml, measured_by, note, tank_id, business_date),
        )

    @staticmethod
    def list_tank_readings(
        conn: sqlite3.Connection, business_date: str
    ) -> list[sqlite3.Row]:
        """All dip readings for a date, with tank codes, in tank order."""
        return conn.execute(
            _TANK_READING_SELECT + " WHERE r.business_date = ? ORDER BY r.tank_id",
            (business_date,),
        ).fetchall()
