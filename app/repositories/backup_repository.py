"""Repository for ``backup_history`` and the ``backup.*`` app settings.

Thin, parameterized SQL only — no business logic, no transaction control.
The caller (``BackupService``) owns the transaction boundary; status
UPDATEs on ``backup_history`` are deliberate and audited per the migration
013 contract (no ``bu_no_update`` trigger by design).

Settings keys (design plan §3, extended for Phase 8):
    backup.drive_folder         absolute path of the Google Drive for
                                desktop folder (unset = local-only)
    backup.keep_local           validated local backups to keep (default 30)
    backup.keep_drive           validated Drive copies to keep (default 14)
    backup.on_day_close         auto-backup at day close (default '1')
    backup.on_shutdown          auto-backup at app shutdown (default '0')
    backup.on_upgrade           auto-backup before upgrade (default '1')
    backup.periodic_minutes     scheduled interval, '0' = disabled
    backup.last_scheduled_at_utc last scheduled run (UTC ISO, machine time)
"""

from __future__ import annotations

import sqlite3


class BackupRepository:
    """CRUD + queries for the backup_history aggregate."""

    # ------------------------------------------------------------------
    # backup_history
    # ------------------------------------------------------------------
    @staticmethod
    def insert_history_row(
        conn: sqlite3.Connection,
        *,
        filename: str,
        destination: str,
        status: str = "requested",
        trigger: str | None = None,
        error: str | None = None,
        size_bytes: int | None = None,
        sha256: str | None = None,
        verified_at_utc: str | None = None,
    ) -> int:
        """Insert a ``backup_history`` row; return its id.

        ``requested_at_utc`` comes from the column default (machine time,
        UTC — design plan §5.1).

        Schema-tolerant: the pre-upgrade safety backup runs in
        ``bootstrap()`` *before* ``migrate()`` applies migration 015, so on
        a v14 database ``backup_history.trigger`` does not exist yet. The
        column list is built from ``PRAGMA table_info`` and ``trigger`` is
        omitted when absent — the safety backup must succeed on a pre-015
        DB (otherwise the upgrade could never proceed).
        """
        table_columns = set()
        for col in conn.execute("PRAGMA table_info(backup_history)").fetchall():
            # Works with both sqlite3.Row (named access) and plain tuples.
            try:
                table_columns.add(col["name"])
            except (TypeError, KeyError, IndexError):
                table_columns.add(col[1])
        columns = [
            "filename",
            "destination",
            "status",
            "error",
            "size_bytes",
            "sha256",
            "verified_at_utc",
        ]
        values: list = [
            filename,
            destination,
            status,
            error,
            size_bytes,
            sha256,
            verified_at_utc,
        ]
        if "trigger" in table_columns:
            columns.insert(3, "trigger")
            values.insert(3, trigger)
        placeholders = ", ".join("?" for _ in columns)
        cur = conn.execute(
            f"INSERT INTO backup_history ({', '.join(columns)})"
            f" VALUES ({placeholders})",
            values,
        )
        return int(cur.lastrowid)

    @staticmethod
    def update_status(
        conn: sqlite3.Connection,
        backup_id: int,
        status: str,
        *,
        error: str | None = None,
        size_bytes: int | None = None,
        sha256: str | None = None,
        verified_at_utc: str | None = None,
        filename: str | None = None,
    ) -> None:
        """Advance one row through the §9 pipeline statuses.

        Only the supplied columns change; ``error`` is cleared only when a
        new error is *not* supplied — a terminal ``failed`` row keeps its
        error text. (Callers pass ``error=None`` on success steps and the
        row retains whatever was recorded before.)
        """
        conn.execute(
            "UPDATE backup_history"
            " SET status = ?,"
            "     error = COALESCE(?, error),"
            "     size_bytes = COALESCE(?, size_bytes),"
            "     sha256 = COALESCE(?, sha256),"
            "     verified_at_utc = COALESCE(?, verified_at_utc),"
            "     filename = COALESCE(?, filename)"
            " WHERE id = ?",
            (
                status,
                error,
                size_bytes,
                sha256,
                verified_at_utc,
                filename,
                backup_id,
            ),
        )

    @staticmethod
    def list_history(
        conn: sqlite3.Connection,
        limit: int = 100,
        destination: str | None = None,
    ) -> list[sqlite3.Row]:
        """Newest-first ``backup_history`` rows, optionally one destination."""
        sql = "SELECT * FROM backup_history"
        params: list = []
        if destination is not None:
            sql += " WHERE destination = ?"
            params.append(destination)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return conn.execute(sql, params).fetchall()

    @staticmethod
    def get_by_id(
        conn: sqlite3.Connection, backup_id: int
    ) -> sqlite3.Row | None:
        """One ``backup_history`` row, or None."""
        return conn.execute(
            "SELECT * FROM backup_history WHERE id = ?", (backup_id,)
        ).fetchone()

    @staticmethod
    def known_good_rows(
        conn: sqlite3.Connection, destination: str
    ) -> list[sqlite3.Row]:
        """Rows whose files are restorable: validated local snapshots and
        verified Drive copies, newest first."""
        return conn.execute(
            "SELECT * FROM backup_history"
            " WHERE destination = ?"
            "   AND status IN ('validated', 'copied_to_drive', 'sync_confirmed')"
            " ORDER BY id DESC",
            (destination,),
        ).fetchall()

    # ------------------------------------------------------------------
    # app_settings (mirror of business_day_repository's get/set pattern)
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
