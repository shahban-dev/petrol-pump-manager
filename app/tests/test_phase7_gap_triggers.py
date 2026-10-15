"""Phase-7 gap-closing triggers (migration 013).

Covers the three confirmed schema gaps:
  1. backup_history — timestamp columns, A1/A2 triggers, B audit
     (ai/au/bd), C2 no-delete. UPDATEs stay allowed by design (Phase 8's
     backup pipeline performs audited status UPDATEs on the single row).
  2. meter_events — timestamp columns, A1/A2 triggers; updates stay
     allowed+audited (wrap_value_ml may be recorded after the event).
  3. schema_migrations — UPDATE/DELETE rejected, INSERT allowed.

Design plan §4 (trigger groups A/B/C); migration 013 header documents the
backup_history update contract and the table-rebuild implementation note.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.database.migrator import (
    CURRENT_SCHEMA_VERSION,
    get_schema_version,
    verify_schema,
)
from app.tests.conftest import (
    TIMESTAMP_RE,
    audit_count,
    audit_rows,
    insert_meter_event,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _insert_backup(db) -> int:
    cur = db.execute(
        "INSERT INTO backup_history (filename, destination, status)"
        " VALUES ('backup-2026-10-15.sqlite3', 'local', 'requested')"
    )
    return int(cur.lastrowid)


def _backup_row(db, row_id: int) -> sqlite3.Row:
    return db.execute(
        "SELECT * FROM backup_history WHERE id = ?", (row_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# migration mechanics
# ---------------------------------------------------------------------------

def test_migrated_to_current_version_and_schema_verifies(db):
    # Version-agnostic: later Phase-7 migrations (e.g. 014) must not break
    # this check.
    assert get_schema_version(db) == CURRENT_SCHEMA_VERSION
    assert verify_schema(db) == []


def test_new_timestamp_columns_exist_and_default(db):
    for table in ("backup_history", "meter_events"):
        cols = {
            r[1]: r for r in db.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for col in ("created_at_utc", "updated_at_utc"):
            assert col in cols, f"{table} missing {col}"
            info = cols[col]
            assert info[2] == "TEXT"          # type
            assert info[3] == 1              # notnull
            assert "strftime" in (info[4] or "")  # default expression


# ---------------------------------------------------------------------------
# backup_history
# ---------------------------------------------------------------------------

def test_backup_history_insert_gets_timestamps_and_audit_row(db):
    before = audit_count(db)
    row_id = _insert_backup(db)
    db.commit()

    row = _backup_row(db, row_id)
    assert TIMESTAMP_RE.match(row["created_at_utc"])
    assert TIMESTAMP_RE.match(row["updated_at_utc"])

    rows = audit_rows(db, table_name="backup_history", action="INSERT")
    assert len(rows) == audit_count(db) - before == 1
    entry = rows[0]
    assert entry["record_id"] == row_id
    assert entry["actor"] == "system"
    new = json.loads(entry["new_values_json"])
    assert new["filename"] == "backup-2026-10-15.sqlite3"
    assert new["status"] == "requested"
    assert new["created_at_utc"] == row["created_at_utc"]


def test_backup_history_status_update_allowed_audited_and_touch(db):
    """The Phase-8 contract: audited status UPDATEs on the row are allowed."""
    row_id = _insert_backup(db)
    db.commit()
    created_before = _backup_row(db, row_id)["created_at_utc"]
    updated_before = _backup_row(db, row_id)["updated_at_utc"]
    before = audit_count(db)

    # A pipeline-style status transition, as Phase 8 will perform.
    db.execute(
        "UPDATE backup_history SET status = 'snapshot_created',"
        " size_bytes = 12345 WHERE id = ?",
        (row_id,),
    )
    db.commit()

    row = _backup_row(db, row_id)
    assert row["status"] == "snapshot_created"
    assert row["created_at_utc"] == created_before          # A1: immutable
    assert row["updated_at_utc"] >= updated_before          # A2: touched

    rows = audit_rows(db, table_name="backup_history", action="UPDATE")
    assert len(rows) == audit_count(db) - before == 1       # exactly one row
    old = json.loads(rows[0]["old_values_json"])
    new = json.loads(rows[0]["new_values_json"])
    assert old["status"] == "requested" and new["status"] == "snapshot_created"
    assert new["size_bytes"] == 12345


def test_backup_history_created_at_utc_is_immutable(db):
    row_id = _insert_backup(db)
    db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="created_at_utc is immutable"):
        db.execute(
            "UPDATE backup_history SET created_at_utc = '2000-01-01T00:00:00.000Z'"
            " WHERE id = ?",
            (row_id,),
        )
    db.rollback()


def test_backup_history_noop_update_writes_no_audit_row(db):
    row_id = _insert_backup(db)
    db.commit()
    before = audit_count(db)
    updated_before = _backup_row(db, row_id)["updated_at_utc"]

    db.execute("UPDATE backup_history SET status = 'requested' WHERE id = ?", (row_id,))
    db.commit()

    assert audit_count(db) == before                       # no business change…
    assert _backup_row(db, row_id)["updated_at_utc"] == updated_before  # …no touch


def test_backup_history_delete_rejected(db):
    row_id = _insert_backup(db)
    db.commit()
    before = audit_count(db)
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        db.execute("DELETE FROM backup_history WHERE id = ?", (row_id,))
    db.rollback()
    # The aborted DELETE's audit insert rolled back with it.
    assert audit_count(db) == before
    assert _backup_row(db, row_id) is not None


# ---------------------------------------------------------------------------
# meter_events
# ---------------------------------------------------------------------------

def test_meter_events_timestamps_and_single_audit_row_on_update(db):
    event_id = insert_meter_event(db, "1 PG+", "rollover")
    db.commit()
    row = db.execute(
        "SELECT * FROM meter_events WHERE id = ?", (event_id,)
    ).fetchone()
    assert TIMESTAMP_RE.match(row["created_at_utc"])
    assert TIMESTAMP_RE.match(row["updated_at_utc"])
    created_before = row["created_at_utc"]
    updated_before = row["updated_at_utc"]
    before = audit_count(db)

    # wrap_value_ml recorded after the event (pinned by
    # test_phase3_acceptance.py::test_rollover_wrap_math): allowed, audited.
    db.execute(
        "UPDATE meter_events SET wrap_value_ml = ? WHERE id = ?",
        (100_000_000, event_id),
    )
    db.commit()

    row = db.execute(
        "SELECT * FROM meter_events WHERE id = ?", (event_id,)
    ).fetchone()
    assert row["wrap_value_ml"] == 100_000_000
    assert row["created_at_utc"] == created_before
    assert row["updated_at_utc"] >= updated_before

    rows = audit_rows(db, table_name="meter_events", action="UPDATE")
    assert len(rows) == audit_count(db) - before == 1  # touch writes no row


def test_meter_events_created_at_utc_is_immutable(db):
    event_id = insert_meter_event(db, "1 PG+", "reset")
    db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="created_at_utc is immutable"):
        db.execute(
            "UPDATE meter_events SET created_at_utc = '2000-01-01T00:00:00.000Z'"
            " WHERE id = ?",
            (event_id,),
        )
    db.rollback()


def test_meter_events_noop_update_writes_no_audit_row(db):
    event_id = insert_meter_event(db, "1 PG+", "reset")
    db.commit()
    before = audit_count(db)
    updated_before = db.execute(
        "SELECT updated_at_utc FROM meter_events WHERE id = ?", (event_id,)
    ).fetchone()[0]

    db.execute("UPDATE meter_events SET reason = reason WHERE id = ?", (event_id,))
    db.commit()

    assert audit_count(db) == before
    assert (
        db.execute(
            "SELECT updated_at_utc FROM meter_events WHERE id = ?", (event_id,)
        ).fetchone()[0]
        == updated_before
    )


# ---------------------------------------------------------------------------
# schema_migrations tamper guard
# ---------------------------------------------------------------------------

def test_schema_migrations_update_rejected(db):
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute(
            "UPDATE schema_migrations SET description = 'tampered' WHERE version = 13"
        )
    db.rollback()


def test_schema_migrations_delete_rejected(db):
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (CURRENT_SCHEMA_VERSION,),
        )
    db.rollback()
    assert get_schema_version(db) == CURRENT_SCHEMA_VERSION


def test_schema_migrations_insert_still_allowed(db):
    """The migrator records versions with INSERT only — that must keep working."""
    db.execute(
        "INSERT INTO schema_migrations (version, description) VALUES (999, 'probe')"
    )
    db.commit()
    row = db.execute(
        "SELECT description FROM schema_migrations WHERE version = 999"
    ).fetchone()
    assert row[0] == "probe"
