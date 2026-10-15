"""Repository for nozzles, readings and meter events.

Thin, parameterized SQL only — no business logic, no transaction control.
"Effective" reading = the closing-kind row no correction supersedes
(``v_effective_nozzle_readings``); all previous-reading and day-summary
queries use it so a correction is honoured everywhere.
"""

from __future__ import annotations

import sqlite3


class NozzleRepository:
    """CRUD + queries for the nozzle aggregate."""

    # ------------------------------------------------------------------
    # nozzles
    # ------------------------------------------------------------------
    @staticmethod
    def list_nozzles(
        conn: sqlite3.Connection, *, active_only: bool = True
    ) -> list[sqlite3.Row]:
        """Nozzles with their fuel product, in sort order."""
        sql = (
            "SELECT n.*, p.code AS fuel_product_code"
            " FROM nozzles n JOIN fuel_products p ON p.id = n.fuel_product_id"
        )
        if active_only:
            sql += " WHERE n.is_active = 1"
        sql += " ORDER BY n.sort_order, n.id"
        return conn.execute(sql).fetchall()

    @staticmethod
    def get_nozzle(conn: sqlite3.Connection, nozzle_id: int) -> sqlite3.Row | None:
        """One nozzle with its fuel product, or None."""
        return conn.execute(
            "SELECT n.*, p.code AS fuel_product_code"
            " FROM nozzles n JOIN fuel_products p ON p.id = n.fuel_product_id"
            " WHERE n.id = ?",
            (nozzle_id,),
        ).fetchone()

    @staticmethod
    def get_nozzle_by_code(
        conn: sqlite3.Connection, code: str
    ) -> sqlite3.Row | None:
        """One nozzle looked up by its (unique) code, or None."""
        return conn.execute(
            "SELECT n.*, p.code AS fuel_product_code"
            " FROM nozzles n JOIN fuel_products p ON p.id = n.fuel_product_id"
            " WHERE n.code = ?",
            (code,),
        ).fetchone()

    # ------------------------------------------------------------------
    # nozzle_readings
    # ------------------------------------------------------------------
    @staticmethod
    def insert_reading(
        conn: sqlite3.Connection,
        *,
        nozzle_id: int,
        business_date: str,
        reading_ml: int,
        reading_kind: str = "closing",
        supersedes_reading_id: int | None = None,
        occurred_at_utc: str | None = None,
        created_by: str = "system",
    ) -> int:
        """Insert a reading row; returns its id.

        ``occurred_at_utc=None`` lets the database default apply
        (UTC ``strftime``); pass an explicit value only in tests.
        """
        if occurred_at_utc is None:
            cur = conn.execute(
                "INSERT INTO nozzle_readings"
                " (nozzle_id, business_date, reading_kind, reading_ml,"
                "  supersedes_reading_id, created_by)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    nozzle_id,
                    business_date,
                    reading_kind,
                    reading_ml,
                    supersedes_reading_id,
                    created_by,
                ),
            )
        else:
            cur = conn.execute(
                "INSERT INTO nozzle_readings"
                " (nozzle_id, business_date, reading_kind, reading_ml,"
                "  supersedes_reading_id, occurred_at_utc, created_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    nozzle_id,
                    business_date,
                    reading_kind,
                    reading_ml,
                    supersedes_reading_id,
                    occurred_at_utc,
                    created_by,
                ),
            )
        return int(cur.lastrowid)

    @staticmethod
    def get_effective_reading(
        conn: sqlite3.Connection, nozzle_id: int, business_date: str
    ) -> sqlite3.Row | None:
        """The effective closing reading for a nozzle+date, or None."""
        return conn.execute(
            "SELECT * FROM v_effective_nozzle_readings"
            " WHERE nozzle_id = ? AND business_date = ? LIMIT 1",
            (nozzle_id, business_date),
        ).fetchone()

    @staticmethod
    def get_previous_effective_reading(
        conn: sqlite3.Connection, nozzle_id: int, business_date: str
    ) -> sqlite3.Row | None:
        """The effective reading of the latest date strictly before
        *business_date* — the previous reading for liter calculations."""
        return conn.execute(
            "SELECT * FROM v_effective_nozzle_readings"
            " WHERE nozzle_id = ? AND business_date < ?"
            " ORDER BY business_date DESC, id DESC LIMIT 1",
            (nozzle_id, business_date),
        ).fetchone()

    @staticmethod
    def list_effective_readings_for_day(
        conn: sqlite3.Connection, business_date: str
    ) -> list[sqlite3.Row]:
        """Effective readings of every nozzle for one business date."""
        return conn.execute(
            "SELECT * FROM v_effective_nozzle_readings"
            " WHERE business_date = ? ORDER BY nozzle_id",
            (business_date,),
        ).fetchall()

    @staticmethod
    def get_reading(conn: sqlite3.Connection, reading_id: int) -> sqlite3.Row | None:
        """One reading row by id (any kind, including superseded)."""
        return conn.execute(
            "SELECT * FROM nozzle_readings WHERE id = ?", (reading_id,)
        ).fetchone()

    @staticmethod
    def list_segments(
        conn: sqlite3.Connection, nozzle_id: int, business_date: str
    ) -> list[sqlite3.Row]:
        """Intraday snapshot readings (``reading_kind='segment'``) for a
        nozzle+date, in occurrence order. Multiple per day are allowed —
        there is no E1 uniqueness on segments."""
        return conn.execute(
            "SELECT * FROM nozzle_readings"
            " WHERE nozzle_id = ? AND business_date = ?"
            "   AND reading_kind = 'segment'"
            " ORDER BY occurred_at_utc, id",
            (nozzle_id, business_date),
        ).fetchall()

    @staticmethod
    def has_segment_before(
        conn: sqlite3.Connection,
        nozzle_id: int,
        business_date: str,
        at_utc: str,
    ) -> bool:
        """True when the nozzle has a segment reading for *business_date*
        taken at or before *at_utc* (§11.2 snapshot rule)."""
        row = conn.execute(
            "SELECT 1 FROM nozzle_readings"
            " WHERE nozzle_id = ? AND business_date = ?"
            "   AND reading_kind = 'segment' AND occurred_at_utc <= ?"
            " LIMIT 1",
            (nozzle_id, business_date, at_utc),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # meter_events
    # ------------------------------------------------------------------
    @staticmethod
    def insert_meter_event(
        conn: sqlite3.Connection,
        *,
        nozzle_id: int,
        event_type: str,
        reason: str,
        reading_before_ml: int | None,
        reading_after_ml: int | None,
        occurred_at_utc: str | None = None,
        authorized_by: str = "system",
    ) -> int:
        """Insert a meter event; returns its id."""
        if occurred_at_utc is None:
            cur = conn.execute(
                "INSERT INTO meter_events"
                " (nozzle_id, event_type, reading_before_ml, reading_after_ml,"
                "  reason, authorized_by)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    nozzle_id,
                    event_type,
                    reading_before_ml,
                    reading_after_ml,
                    reason,
                    authorized_by,
                ),
            )
        else:
            cur = conn.execute(
                "INSERT INTO meter_events"
                " (nozzle_id, event_type, reading_before_ml, reading_after_ml,"
                "  occurred_at_utc, reason, authorized_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    nozzle_id,
                    event_type,
                    reading_before_ml,
                    reading_after_ml,
                    occurred_at_utc,
                    reason,
                    authorized_by,
                ),
            )
        return int(cur.lastrowid)

    @staticmethod
    def list_meter_events(
        conn: sqlite3.Connection, nozzle_id: int
    ) -> list[sqlite3.Row]:
        """Meter events for a nozzle, oldest first."""
        return conn.execute(
            "SELECT * FROM meter_events WHERE nozzle_id = ? ORDER BY id",
            (nozzle_id,),
        ).fetchall()
