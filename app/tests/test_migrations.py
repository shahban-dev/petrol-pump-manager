"""Migration mechanics: versioning, idempotency, verification, fail-safe.

Design plan §1: schema changes only via numbered migrations; startup fails
safe (clear error, no data loss) on incompatible versions.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.database import migrator
from app.database.connection import DatabaseManager
from app.database.migrator import (
    CURRENT_SCHEMA_VERSION,
    MigrationError,
    get_schema_version,
    migrate,
    verify_schema,
)


def test_migrate_reaches_current_version(db_file):
    manager = DatabaseManager(db_file)
    conn = manager.connect()
    try:
        applied = migrate(conn)
        assert applied == CURRENT_SCHEMA_VERSION
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
        assert verify_schema(conn) == []
    finally:
        conn.close()


def test_get_schema_version_unmigrated_is_zero(db_file):
    raw = sqlite3.connect(str(db_file))
    try:
        assert get_schema_version(raw) == 0
    finally:
        raw.close()


def test_migrate_is_idempotent(db):
    """Re-running migrate() is a safe no-op: 0 applied, version unchanged."""
    before = get_schema_version(db)
    assert before == CURRENT_SCHEMA_VERSION
    assert migrate(db) == 0
    assert get_schema_version(db) == CURRENT_SCHEMA_VERSION
    assert verify_schema(db) == []


def test_migrate_refuses_newer_database(db):
    """A database claiming a NEWER version than this build raises
    MigrationError and migrate() refuses — fail safe, no data loss."""
    db.execute(
        "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
        (CURRENT_SCHEMA_VERSION + 1, "hypothetical future migration"),
    )
    db.commit()
    assert get_schema_version(db) == CURRENT_SCHEMA_VERSION + 1
    with pytest.raises(MigrationError, match="newer"):
        migrate(db)


def test_migrate_requires_quiet_connection(db):
    """migrate() refuses to run with an open transaction."""
    db.execute("INSERT INTO app_settings (key, value) VALUES ('x', 'y')")
    assert db.in_transaction
    with pytest.raises(MigrationError):
        migrate(db)
    db.rollback()


def test_verify_schema_detects_missing_object(db):
    """verify_schema() names exactly what is missing."""
    db.execute("DROP TRIGGER trg_nozzle_readings_bi_validate")
    missing = verify_schema(db)
    assert "trigger:trg_nozzle_readings_bi_validate" in missing
    assert all(m.startswith("trigger:") for m in missing)
