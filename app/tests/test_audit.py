"""Group B audit history: every mutation leaves a JSON-snapshotted trail;
audit_log itself is never audited and never modifiable.

Design plan §4 group B, §10 acceptance test 11.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from .conftest import (
    audit_count,
    audit_rows,
    insert_business_day,
    insert_day_event,
    insert_meter_event,
    insert_reading,
)

DAY = "2026-10-10"


def test_insert_creates_complete_audit_row(db):
    """INSERT on a business table -> one audit row with action, table_name,
    record_id, new-values JSON, business_date and actor."""
    before = audit_count(db)
    insert_business_day(db, DAY)
    reading_id = insert_reading(db, "1 PG+", DAY, 84_985_000, created_by="manager")

    rows = db.execute(
        "SELECT * FROM audit_log WHERE id > ? ORDER BY id", (before,)
    ).fetchall()
    assert len(rows) == 2  # exactly: the day insert + the reading insert

    day_row = rows[0]
    assert day_row["table_name"] == "business_days"
    assert day_row["action"] == "INSERT"
    assert day_row["business_date"] == DAY
    assert day_row["old_values_json"] is None
    new = json.loads(day_row["new_values_json"])
    assert new["business_date"] == DAY and new["status"] == "open"

    reading_row = rows[1]
    assert reading_row["table_name"] == "nozzle_readings"
    assert reading_row["action"] == "INSERT"
    assert reading_row["record_id"] == reading_id
    assert reading_row["business_date"] == DAY
    assert reading_row["actor"] == "manager"  # from created_by
    new = json.loads(reading_row["new_values_json"])
    assert new["reading_ml"] == 84_985_000
    assert new["reading_kind"] == "closing"


def test_update_creates_old_and_new_snapshots(db):
    """UPDATE -> exactly one audit row with both JSON snapshots.

    NOTE on trigger interaction: the A2 timestamp-touch trigger performs its
    touch as an inner UPDATE of the same row. That inner UPDATE changes only
    updated_at_utc, and the …_au_audit triggers carry a WHEN guard on the
    business columns (same list as the touch trigger), so the touch writes no
    audit row. One business-column UPDATE therefore yields exactly one audit
    row — no timestamp-maintenance noise in the audit log.
    """
    db.execute("UPDATE nozzles SET label_en = 'renamed' WHERE code = '1 PG+'")
    rows = audit_rows(db, table_name="nozzles", action="UPDATE")
    assert len(rows) == 1

    row = rows[0]
    assert row["actor"] == "system"  # nozzles has no created_by column
    old = json.loads(row["old_values_json"])
    new = json.loads(row["new_values_json"])
    assert old["label_en"] == "1 PG+" and new["label_en"] == "renamed"
    assert old["id"] == new["id"]


def test_delete_attempt_on_deletable_table_is_audited(db):
    """DELETE on a table without a Group-C guard (meter_events) succeeds and
    leaves a DELETE audit row with the old snapshot."""
    event_id = insert_meter_event(db, "1 PG+", "reset")
    before = audit_count(db)

    db.execute("DELETE FROM meter_events WHERE id = ?", (event_id,))

    rows = db.execute(
        "SELECT * FROM audit_log WHERE id > ? ORDER BY id", (before,)
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["table_name"] == "meter_events"
    assert row["action"] == "DELETE"
    assert row["record_id"] == event_id
    assert row["new_values_json"] is None
    old = json.loads(row["old_values_json"])
    assert old["event_type"] == "reset"


def test_day_event_insert_is_audited(db):
    insert_business_day(db, DAY)
    event_id = insert_day_event(db, DAY, "reopen", reason="correction needed")
    rows = audit_rows(db, table_name="business_day_events", action="INSERT")
    assert len(rows) == 1
    row = rows[0]
    assert row["record_id"] == event_id
    assert row["business_date"] == DAY
    assert json.loads(row["new_values_json"])["reason"] == "correction needed"


def test_audit_log_has_no_audit_triggers(db):
    """Inserting into business tables creates audit rows only for those
    tables — never a cascade of audit-of-audit rows."""
    insert_business_day(db, DAY)
    insert_reading(db, "1 PG+", DAY, 84_985_000)
    insert_meter_event(db, "1 PG+", "reset")

    assert audit_rows(db, table_name="audit_log") == []
    assert audit_count(db) == 3  # day + reading + meter event, nothing more


def test_audit_log_cannot_be_updated(db):
    insert_business_day(db, DAY)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE audit_log SET actor = 'intruder'")
    assert db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1


def test_audit_log_cannot_be_deleted(db):
    insert_business_day(db, DAY)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM audit_log")
    assert db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1


def test_failed_delete_leaves_no_audit_row(db):
    """A DELETE aborted by a Group-C guard rolls the whole statement back —
    including the BEFORE DELETE audit insert. Tampering leaves no trace gap
    because nothing was persisted at all."""
    insert_business_day(db, DAY)
    insert_reading(db, "1 PG+", DAY, 84_985_000)
    before = audit_count(db)

    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM nozzle_readings")

    # The reading is still there and no audit row was left behind.
    assert db.execute("SELECT COUNT(*) FROM nozzle_readings").fetchone()[0] == 1
    assert audit_count(db) == before


def test_timestamp_touch_writes_no_audit_row(db):
    """The A2 timestamp-touch inner UPDATE changes only updated_at_utc.

    It must (a) advance updated_at_utc on the row, and (b) produce zero audit
    rows of its own, thanks to the business-column WHEN guard on the
    …_au_audit triggers. Exactly one UPDATE audit row per business change.
    """
    before_touch = db.execute(
        "SELECT updated_at_utc FROM nozzles WHERE code = '1 PG+'"
    ).fetchone()[0]
    audits_before = audit_count(db)

    db.execute("UPDATE nozzles SET label_en = 'touched' WHERE code = '1 PG+'")

    after_touch = db.execute(
        "SELECT updated_at_utc FROM nozzles WHERE code = '1 PG+'"
    ).fetchone()[0]
    assert after_touch > before_touch  # touch survived the outer UPDATE

    update_audits = audit_rows(db, table_name="nozzles", action="UPDATE")
    assert len(update_audits) == 1
    assert audit_count(db) == audits_before + 1
