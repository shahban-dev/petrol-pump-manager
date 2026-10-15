"""Group C posted-ledger immutability: corrections go through new rows,
never edits or deletes.

Design plan §4 group C, §10 acceptance test 11.
"""

from __future__ import annotations

import sqlite3

import pytest

from .conftest import (
    audit_count,
    insert_business_day,
    insert_day_event,
    insert_reading,
)

DAY = "2026-10-10"


@pytest.fixture
def posted(db):
    """A business day with an event and a posted closing reading."""
    insert_business_day(db, DAY)
    insert_day_event(db, DAY, "close")
    insert_reading(db, "1 PG+", DAY, 84_985_000)
    return db


def test_nozzle_reading_update_blocked(posted):
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        posted.execute("UPDATE nozzle_readings SET reading_ml = 1")
    # The row is untouched.
    row = posted.execute("SELECT reading_ml FROM nozzle_readings").fetchone()
    assert row[0] == 84_985_000


def test_nozzle_reading_delete_blocked(posted):
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        posted.execute("DELETE FROM nozzle_readings WHERE business_date = ?", (DAY,))
    assert posted.execute("SELECT COUNT(*) FROM nozzle_readings").fetchone()[0] == 1


def test_business_day_event_update_blocked(posted):
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        posted.execute("UPDATE business_day_events SET reason = 'rewritten'")
    row = posted.execute("SELECT reason FROM business_day_events").fetchone()
    assert row[0] is None


def test_business_day_event_delete_blocked(posted):
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        posted.execute("DELETE FROM business_day_events")
    assert posted.execute("SELECT COUNT(*) FROM business_day_events").fetchone()[0] == 1


def test_business_day_delete_blocked(posted):
    """business_days rows are never deleted — history must survive."""
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        posted.execute("DELETE FROM business_days WHERE business_date = ?", (DAY,))
    assert posted.execute("SELECT COUNT(*) FROM business_days").fetchone()[0] == 1


def test_blocked_mutation_changes_nothing(posted):
    """A blocked UPDATE/DELETE leaves the table, the audit log and the
    transaction state exactly as before (ABORT rolls the statement back)."""
    before_audit = audit_count(posted)
    for sql in (
        "UPDATE nozzle_readings SET reading_ml = 1",
        "DELETE FROM nozzle_readings",
        "UPDATE business_day_events SET actor = 'x'",
        "DELETE FROM business_day_events",
        "DELETE FROM business_days",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            posted.execute(sql)
    assert posted.execute("SELECT COUNT(*) FROM nozzle_readings").fetchone()[0] == 1
    assert posted.execute("SELECT COUNT(*) FROM business_day_events").fetchone()[0] == 1
    assert posted.execute("SELECT COUNT(*) FROM business_days").fetchone()[0] == 1
    assert audit_count(posted) == before_audit
