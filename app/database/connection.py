"""Pragma-verified SQLite connections.

Every connection is opened with foreign-key enforcement, WAL journaling, a
busy timeout and FULL synchronous mode — and each setting is *read back and
verified*, so a misbehaving platform or a copied database file fails fast
instead of silently running in an unsafe mode.

Design plan: documentation/00_design_plan.md §1 (``app/database/connection.py``).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import TracebackType


class DatabaseError(Exception):
    """Raised when a database connection cannot be opened safely."""


class DatabaseManager:
    """Opens pragma-verified SQLite connections.

    The manager holds no open connection between calls: :meth:`connect` opens
    a brand-new connection on every call and the caller owns its lifetime
    (close it, or use the manager as a context manager).

    All SQL here is static text — no string interpolation of values.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._ctx_conn: sqlite3.Connection | None = None

    @property
    def db_path(self) -> Path:
        """Filesystem path of the database file."""
        return self._db_path

    # ------------------------------------------------------------------
    # connection factory
    # ------------------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        """Open a new, pragma-verified connection.

        Raises:
            DatabaseError: if any required pragma cannot be set and verified.
        """
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            self._apply_pragmas(conn)
        except Exception:
            conn.close()
            raise
        return conn

    @staticmethod
    def _apply_pragmas(conn: sqlite3.Connection) -> None:
        """Set and verify the required pragmas on an open connection."""
        # Foreign keys are per-connection in SQLite: must be set every time.
        conn.execute("PRAGMA foreign_keys=ON")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise DatabaseError("PRAGMA foreign_keys=ON was not honoured")

        conn.execute("PRAGMA busy_timeout=5000")
        if conn.execute("PRAGMA busy_timeout").fetchone()[0] != 5000:
            raise DatabaseError("PRAGMA busy_timeout=5000 was not honoured")

        # WAL persists on the database file, but verify anyway: a :memory:
        # database (or a read problem) reports something other than 'wal'.
        journal_mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if journal_mode is None or str(journal_mode).lower() != "wal":
            raise DatabaseError(
                "PRAGMA journal_mode=WAL was not honoured "
                f"(reported {journal_mode!r}); refusing to open"
            )

        # Note: `PRAGMA synchronous=FULL` returns no row, so the set and the
        # read-back must be separate statements.
        conn.execute("PRAGMA synchronous=FULL")
        if conn.execute("PRAGMA synchronous").fetchone()[0] != 2:
            raise DatabaseError("PRAGMA synchronous=FULL was not honoured")

        # Trigger semantics below (Group A timestamp touches, audit triggers)
        # depend on non-recursive trigger behaviour; pin it explicitly.
        conn.execute("PRAGMA recursive_triggers=OFF")
        if conn.execute("PRAGMA recursive_triggers").fetchone()[0] != 0:
            raise DatabaseError("PRAGMA recursive_triggers=OFF was not honoured")

    # ------------------------------------------------------------------
    # context-manager use: `with DatabaseManager(path) as conn: ...`
    # ------------------------------------------------------------------
    def __enter__(self) -> sqlite3.Connection:
        self._ctx_conn = self.connect()
        return self._ctx_conn

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        conn, self._ctx_conn = self._ctx_conn, None
        if conn is None:
            return
        try:
            if exc_type is None:
                conn.commit()
            else:
                conn.rollback()
        finally:
            conn.close()

    def close(self) -> None:
        """Close the context-manager connection, if one is currently open."""
        conn, self._ctx_conn = self._ctx_conn, None
        if conn is not None:
            conn.close()

    def close_all(self) -> None:
        """Close every connection currently held by this manager.

        The manager normally holds no open connection between calls —
        :meth:`connect` hands ownership of each connection to its caller,
        and the service layers close theirs — but the context-manager form
        keeps one connection open. Backup/restore code calls this before
        replacing the database file so no pooled handle keeps the file
        locked (on Windows, ``os.replace`` fails on an open file).

        Callers must still have finished their own ``connect()``-owned
        connections first; those are not tracked here.
        """
        self.close()
