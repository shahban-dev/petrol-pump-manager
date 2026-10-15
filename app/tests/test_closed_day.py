"""Group D closed-day protection and the audited reopen workflow — all
through DIRECT SQL on a raw second connection (db2), proving the database
itself (not the application) enforces the rules.

Design plan §4 groups D/H/I3, §10 acceptance test 10, §11 assumption 13.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

DAY = "2026-10-10"


def _close_day(db2):
    db2.execute(
        "UPDATE business_days SET status = 'closed'"
        f" WHERE business_date = '{DAY}'"
    )
    db2.commit()


def _reopen_day(db2):
    db2.execute(
        "INSERT INTO business_day_events (business_date, event_type, reason, actor)"
        f" VALUES ('{DAY}', 'reopen', 'correction of meter reading', 'manager')"
    )
    db2.execute(
        "UPDATE business_days SET status = 'reopened'"
        f" WHERE business_date = '{DAY}'"
    )
    db2.commit()


@pytest.fixture
def open_day_db2(db2):
    """Raw connection with one open business day and a posted reading."""
    db2.execute(
        f"INSERT INTO business_days (business_date, status) VALUES ('{DAY}', 'open')"
    )
    db2.execute(
        "INSERT INTO nozzle_readings (nozzle_id, business_date, reading_ml)"
        f" VALUES (1, '{DAY}', 84985000)"
    )
    db2.commit()
    return db2


def test_insert_on_closed_day_rejected(open_day_db2):
    """D1: raw-SQL INSERT of a reading into a closed day is rejected."""
    db2 = open_day_db2
    _close_day(db2)

    with pytest.raises(sqlite3.IntegrityError, match="closed"):
        db2.execute(
            "INSERT INTO nozzle_readings (nozzle_id, business_date, reading_ml)"
            f" VALUES (2, '{DAY}', 50000000)"
        )
    db2.rollback()

    assert (
        db2.execute(
            "SELECT COUNT(*) FROM nozzle_readings WHERE nozzle_id = 2"
        ).fetchone()[0]
        == 0
    )


def test_reopen_workflow_allows_correction_and_is_audited(open_day_db2):
    """Authorized reopen (audited event + reason) permits new audited rows."""
    db2 = open_day_db2
    _close_day(db2)
    _reopen_day(db2)

    db2.execute(
        "INSERT INTO nozzle_readings (nozzle_id, business_date, reading_ml)"
        f" VALUES (2, '{DAY}', 50000000)"
    )
    db2.commit()

    row = db2.execute(
        "SELECT reading_ml FROM nozzle_readings"
        " WHERE nozzle_id = 2 AND business_date = ?", (DAY,)
    ).fetchone()
    assert row[0] == 50_000_000

    # The reopen event and the new reading are both in the audit log.
    events = db2.execute(
        "SELECT new_values_json FROM audit_log"
        " WHERE table_name = 'business_day_events' AND action = 'INSERT'"
    ).fetchall()
    assert any(
        json.loads(r[0])["event_type"] == "reopen" for r in events
    ), "reopen event must be audited"
    readings = db2.execute(
        "SELECT new_values_json FROM audit_log"
        " WHERE table_name = 'nozzle_readings' AND action = 'INSERT'"
        " AND business_date = ?", (DAY,)
    ).fetchall()
    assert len(readings) >= 2  # original reading + the correction

    # Closing again after the correction is a legal transition.
    db2.execute(
        f"UPDATE business_days SET status = 'closed' WHERE business_date = '{DAY}'"
    )
    db2.commit()
    assert (
        db2.execute(
            f"SELECT status FROM business_days WHERE business_date = '{DAY}'"
        ).fetchone()[0]
        == "closed"
    )


def test_reopened_day_accepts_no_new_writes_after_reclose(open_day_db2):
    """After reopen -> correct -> close, the day is protected again."""
    db2 = open_day_db2
    _close_day(db2)
    _reopen_day(db2)
    db2.execute(
        f"UPDATE business_days SET status = 'closed' WHERE business_date = '{DAY}'"
    )
    db2.commit()

    with pytest.raises(sqlite3.IntegrityError, match="closed"):
        db2.execute(
            "INSERT INTO nozzle_readings (nozzle_id, business_date, reading_ml)"
            f" VALUES (2, '{DAY}', 50000000)"
        )
    db2.rollback()


def test_illegal_status_jump_open_to_reopened_rejected(db2):
    """I3: 'reopened' is only reachable from 'closed' — open -> reopened
    directly is an illegal jump."""
    db2.execute(
        f"INSERT INTO business_days (business_date, status) VALUES ('{DAY}', 'open')"
    )
    db2.commit()

    with pytest.raises(sqlite3.IntegrityError, match="illegal"):
        db2.execute(
            f"UPDATE business_days SET status = 'reopened' WHERE business_date = '{DAY}'"
        )
    db2.rollback()
    assert (
        db2.execute(
            f"SELECT status FROM business_days WHERE business_date = '{DAY}'"
        ).fetchone()[0]
        == "open"
    )


def test_two_open_days_rejected_on_insert(db2):
    """H2: at most one business day may be open at a time (INSERT path)."""
    db2.execute(
        "INSERT INTO business_days (business_date, status) VALUES ('2026-10-10', 'open')"
    )
    db2.commit()

    with pytest.raises(sqlite3.IntegrityError, match="only one business day"):
        db2.execute(
            "INSERT INTO business_days (business_date, status)"
            " VALUES ('2026-10-11', 'open')"
        )
    db2.rollback()


def test_two_open_days_rejected_on_update(db2):
    """H2: reopening an old day while another is open is rejected too."""
    db2.execute(
        "INSERT INTO business_days (business_date, status) VALUES ('2026-10-10', 'closed')"
    )
    db2.execute(
        "INSERT INTO business_days (business_date, status) VALUES ('2026-10-11', 'open')"
    )
    db2.commit()

    with pytest.raises(sqlite3.IntegrityError):
        db2.execute(
            "UPDATE business_days SET status = 'open'"
            " WHERE business_date = '2026-10-10'"
        )
    db2.rollback()


def test_open_to_non_operating_allowed(db2):
    """open -> non_operating is a legal transition (station closed day)."""
    db2.execute(
        f"INSERT INTO business_days (business_date, status) VALUES ('{DAY}', 'open')"
    )
    db2.commit()
    db2.execute(
        f"UPDATE business_days SET status = 'non_operating' WHERE business_date = '{DAY}'"
    )
    db2.commit()
    assert (
        db2.execute(
            f"SELECT status FROM business_days WHERE business_date = '{DAY}'"
        ).fetchone()[0]
        == "non_operating"
    )
