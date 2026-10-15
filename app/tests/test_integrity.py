"""Database integrity checks on a freshly migrated temp database.

Design plan §1 (integrity_checks.py), §10 acceptance: the app must open and
verify the database safely at startup.
"""

from __future__ import annotations

from app.database import integrity_checks
from app.database.migrator import CURRENT_SCHEMA_VERSION, get_schema_version

from .conftest import insert_business_day, insert_reading


def test_integrity_check_ok(db):
    assert integrity_checks.check_integrity(db) == "ok"


def test_foreign_key_check_clean(db):
    assert integrity_checks.check_foreign_keys(db) == []


def test_required_objects_complete(db):
    assert integrity_checks.check_required_objects(db) == []


def test_integrity_holds_with_data(db):
    """Checks stay green after realistic writes (days, readings)."""
    insert_business_day(db, "2026-10-10")
    insert_reading(db, "1 PG+", "2026-10-10", 84_985_000)
    insert_reading(db, "3 HSD", "2026-10-10", 120_000_000)

    assert integrity_checks.check_integrity(db) == "ok"
    assert integrity_checks.check_foreign_keys(db) == []
    assert integrity_checks.check_required_objects(db) == []
    assert get_schema_version(db) == CURRENT_SCHEMA_VERSION
