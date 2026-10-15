"""Repository for business days, day events, closings and opening balances.

Thin, parameterized SQL only — no business logic, no transaction control.
The caller (a service) owns the connection and its transaction boundary.
Design plan §1: repository layer.
"""

from __future__ import annotations

import sqlite3


class BusinessDayRepository:
    """CRUD + queries for the business-day aggregate."""

    # ------------------------------------------------------------------
    # business_days
    # ------------------------------------------------------------------
    @staticmethod
    def get(conn: sqlite3.Connection, business_date: str) -> sqlite3.Row | None:
        """The ``business_days`` row for *business_date*, or None."""
        return conn.execute(
            "SELECT * FROM business_days WHERE business_date = ?",
            (business_date,),
        ).fetchone()

    @staticmethod
    def latest_date(conn: sqlite3.Connection) -> str | None:
        """The greatest recorded business date, or None when no days exist."""
        row = conn.execute("SELECT MAX(business_date) FROM business_days").fetchone()
        return row[0] if row and row[0] is not None else None

    @staticmethod
    def get_open_day(conn: sqlite3.Connection) -> sqlite3.Row | None:
        """The single open/reopened day, or None."""
        return conn.execute(
            "SELECT * FROM business_days WHERE status IN ('open', 'reopened')"
            " ORDER BY business_date DESC LIMIT 1"
        ).fetchone()

    @staticmethod
    def insert(
        conn: sqlite3.Connection,
        *,
        business_date: str,
        status: str,
        opening_cash_paisa: int,
        prev_closing_date: str | None,
        opened_at_utc: str,
    ) -> None:
        """Insert a new business-day row."""
        conn.execute(
            "INSERT INTO business_days"
            " (business_date, status, opening_cash_paisa, prev_closing_date,"
            "  opened_at_utc)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                business_date,
                status,
                opening_cash_paisa,
                prev_closing_date,
                opened_at_utc,
            ),
        )

    @staticmethod
    def update_status(
        conn: sqlite3.Connection, business_date: str, status: str
    ) -> None:
        """Change a day's status (transition legality is enforced by the
        ``trg_business_days_bu_status_flow`` trigger)."""
        conn.execute(
            "UPDATE business_days SET status = ? WHERE business_date = ?",
            (status, business_date),
        )

    @staticmethod
    def apply_close(
        conn: sqlite3.Connection,
        business_date: str,
        *,
        closing_revision: int,
        closed_at_utc: str,
        physical_cash_paisa: int,
    ) -> None:
        """Stamp a day closed: status, revision, close time, counted cash."""
        conn.execute(
            "UPDATE business_days"
            " SET status = 'closed', closing_revision = ?,"
            "     closed_at_utc = ?, physical_cash_paisa = ?"
            " WHERE business_date = ?",
            (closing_revision, closed_at_utc, physical_cash_paisa, business_date),
        )

    # ------------------------------------------------------------------
    # business_day_events
    # ------------------------------------------------------------------
    @staticmethod
    def insert_event(
        conn: sqlite3.Connection,
        *,
        business_date: str,
        event_type: str,
        actor: str,
        reason: str | None,
        payload_json: str | None,
        occurred_at_utc: str,
    ) -> int:
        """Append a day-lifecycle event; returns its id."""
        cur = conn.execute(
            "INSERT INTO business_day_events"
            " (business_date, event_type, occurred_at_utc, actor, reason,"
            "  payload_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                business_date,
                event_type,
                occurred_at_utc,
                actor,
                reason,
                payload_json,
            ),
        )
        return int(cur.lastrowid)

    @staticmethod
    def list_events(
        conn: sqlite3.Connection, business_date: str
    ) -> list[sqlite3.Row]:
        """Day-lifecycle events for a date, oldest first."""
        return conn.execute(
            "SELECT * FROM business_day_events WHERE business_date = ?"
            " ORDER BY id",
            (business_date,),
        ).fetchall()

    @staticmethod
    def has_event_type(
        conn: sqlite3.Connection, business_date: str, event_type: str
    ) -> bool:
        """True when at least one event of *event_type* exists for the date."""
        row = conn.execute(
            "SELECT 1 FROM business_day_events"
            " WHERE business_date = ? AND event_type = ? LIMIT 1",
            (business_date, event_type),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # business_day_closings
    # ------------------------------------------------------------------
    @staticmethod
    def next_revision(conn: sqlite3.Connection, business_date: str) -> int:
        """The revision number the next closing for this date must use."""
        row = conn.execute(
            "SELECT MAX(closing_revision) FROM business_day_closings"
            " WHERE business_date = ?",
            (business_date,),
        ).fetchone()
        current = row[0] if row and row[0] is not None else 0
        return int(current) + 1

    @staticmethod
    def insert_closing(
        conn: sqlite3.Connection,
        *,
        business_date: str,
        closing_revision: int,
        closed_at_utc: str,
        prev_closing_id: int | None,
        actor: str,
        opening_cash_paisa: int,
        closing_cash_paisa: int,
        petrol_litres_ml: int,
        diesel_litres_ml: int,
        summary_json: str,
        warnings_json: str,
        created_by: str,
    ) -> int:
        """Insert an immutable closing record; returns its id."""
        cur = conn.execute(
            "INSERT INTO business_day_closings"
            " (business_date, closing_revision, closed_at_utc, prev_closing_id,"
            "  actor, opening_cash_paisa, closing_cash_paisa,"
            "  petrol_litres_ml, diesel_litres_ml,"
            "  summary_json, warnings_json, created_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                business_date,
                closing_revision,
                closed_at_utc,
                prev_closing_id,
                actor,
                opening_cash_paisa,
                closing_cash_paisa,
                petrol_litres_ml,
                diesel_litres_ml,
                summary_json,
                warnings_json,
                created_by,
            ),
        )
        return int(cur.lastrowid)

    @staticmethod
    def get_closing(
        conn: sqlite3.Connection, business_date: str, closing_revision: int
    ) -> sqlite3.Row | None:
        """One closing record by date + revision."""
        return conn.execute(
            "SELECT * FROM business_day_closings"
            " WHERE business_date = ? AND closing_revision = ?",
            (business_date, closing_revision),
        ).fetchone()

    @staticmethod
    def list_closings(
        conn: sqlite3.Connection, business_date: str
    ) -> list[sqlite3.Row]:
        """All closing revisions for a date, oldest revision first."""
        return conn.execute(
            "SELECT * FROM business_day_closings WHERE business_date = ?"
            " ORDER BY closing_revision",
            (business_date,),
        ).fetchall()

    @staticmethod
    def latest_closing_before(
        conn: sqlite3.Connection, business_date: str
    ) -> sqlite3.Row | None:
        """The newest closing record strictly before *business_date*.

        The carry-forward source: opening balances of a new day are derived
        from this record (design plan §10).
        """
        return conn.execute(
            "SELECT * FROM business_day_closings"
            " WHERE business_date < ?"
            " ORDER BY business_date DESC, closing_revision DESC LIMIT 1",
            (business_date,),
        ).fetchone()

    # ------------------------------------------------------------------
    # opening_balances
    # ------------------------------------------------------------------
    @staticmethod
    def insert_opening_balance(
        conn: sqlite3.Connection,
        *,
        business_date: str,
        balance_type: str,
        fuel_product_id: int | None,
        amount_paisa: int | None,
        quantity_ml: int | None,
        source_business_date: str | None,
        source_closing_revision: int | None,
        created_by: str,
    ) -> int:
        """Record one carry-forward value with its source closing."""
        cur = conn.execute(
            "INSERT INTO opening_balances"
            " (business_date, balance_type, fuel_product_id, amount_paisa,"
            "  quantity_ml, source_business_date, source_closing_revision,"
            "  created_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                business_date,
                balance_type,
                fuel_product_id,
                amount_paisa,
                quantity_ml,
                source_business_date,
                source_closing_revision,
                created_by,
            ),
        )
        return int(cur.lastrowid)

    @staticmethod
    def get_opening_balances(
        conn: sqlite3.Connection, business_date: str
    ) -> list[sqlite3.Row]:
        """All opening-balance rows for a date."""
        return conn.execute(
            "SELECT * FROM opening_balances WHERE business_date = ? ORDER BY id",
            (business_date,),
        ).fetchall()

    @staticmethod
    def get_opening_cash(
        conn: sqlite3.Connection, business_date: str
    ) -> sqlite3.Row | None:
        """The 'cash' opening-balance row for a date, or None."""
        return conn.execute(
            "SELECT * FROM opening_balances"
            " WHERE business_date = ? AND balance_type = 'cash' LIMIT 1",
            (business_date,),
        ).fetchone()

    # ------------------------------------------------------------------
    # app_settings (operator identity for audit attribution)
    # ------------------------------------------------------------------
    @staticmethod
    def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
        """An ``app_settings`` value, or None when unset."""
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
        """Upsert an ``app_settings`` value."""
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
