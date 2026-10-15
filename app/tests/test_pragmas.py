"""Every connect() returns a pragma-verified connection (§10 acceptance).

Design plan §1: connection.py sets AND reads back foreign_keys, busy_timeout,
journal_mode and synchronous on every connection.
"""

from __future__ import annotations

from app.database.connection import DatabaseManager


def _pragmas(conn):
    return {
        "foreign_keys": conn.execute("PRAGMA foreign_keys").fetchone()[0],
        "busy_timeout": conn.execute("PRAGMA busy_timeout").fetchone()[0],
        "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
        "synchronous": conn.execute("PRAGMA synchronous").fetchone()[0],
    }


def test_connect_returns_verified_pragmas(db_file):
    manager = DatabaseManager(db_file)
    conn = manager.connect()
    try:
        p = _pragmas(conn)
        assert p["foreign_keys"] == 1
        assert p["busy_timeout"] == 5000
        assert str(p["journal_mode"]).lower() == "wal"
        assert p["synchronous"] == 2  # FULL
    finally:
        conn.close()


def test_every_connect_is_verified(db_file):
    """Each connect() call re-applies and re-verifies — not just the first."""
    manager = DatabaseManager(db_file)
    conns = [manager.connect() for _ in range(3)]
    try:
        for conn in conns:
            p = _pragmas(conn)
            assert p["foreign_keys"] == 1
            assert p["busy_timeout"] == 5000
            assert str(p["journal_mode"]).lower() == "wal"
            assert p["synchronous"] == 2
    finally:
        for conn in conns:
            conn.close()


def test_pragmas_survive_migration(db):
    """Migrating the schema does not disturb the verified pragmas."""
    p = _pragmas(db)
    assert p["foreign_keys"] == 1
    assert p["busy_timeout"] == 5000
    assert str(p["journal_mode"]).lower() == "wal"
    assert p["synchronous"] == 2
