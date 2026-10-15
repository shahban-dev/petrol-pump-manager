"""Nozzle-reading validation triggers: E1 uniqueness, E2 row checks.

Design plan §4 groups E1/E2 and §10 acceptance test 2 (duplicate daily
reading). Writes are direct SQL — the database must reject them itself.
"""

from __future__ import annotations

import sqlite3

import pytest

from .conftest import (
    insert_business_day,
    insert_meter_event,
    insert_reading,
    nozzle_id,
)

DAY1 = "2026-10-10"
DAY2 = "2026-10-11"
READING_DAY1_ML = 84_938_000  # 84,938 L — the register's previous reading


def test_duplicate_closing_reading_rejected(db):
    """E1: a second 'closing' reading for the same nozzle+date is rejected."""
    insert_business_day(db, DAY1)
    insert_reading(db, "1 PG+", DAY1, READING_DAY1_ML)

    with pytest.raises(sqlite3.IntegrityError):
        insert_reading(db, "1 PG+", DAY1, 84_985_000)

    # The first reading is untouched.
    rows = db.execute(
        "SELECT reading_ml FROM nozzle_readings WHERE business_date = ?", (DAY1,)
    ).fetchall()
    assert [r[0] for r in rows] == [READING_DAY1_ML]


def test_duplicate_closing_rejected_for_other_nozzles_independently(db):
    """E1 is per (nozzle, date): other nozzles may still post their closing."""
    insert_business_day(db, DAY1)
    insert_reading(db, "1 PG+", DAY1, READING_DAY1_ML)
    # Nozzle 2 on the same day is a different (nozzle_id, business_date) pair.
    insert_reading(db, "2 PG+", DAY1, 50_000_000)
    rows = db.execute("SELECT COUNT(*) FROM nozzle_readings").fetchone()
    assert rows[0] == 2


def test_reading_for_inactive_nozzle_rejected(db):
    """E2: readings for a deactivated nozzle are rejected."""
    insert_business_day(db, DAY1)
    db.execute("UPDATE nozzles SET is_active = 0 WHERE code = '1 PG+'")
    db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="inactive"):
        insert_reading(db, "1 PG+", DAY1, READING_DAY1_ML)

    assert db.execute("SELECT COUNT(*) FROM nozzle_readings").fetchone()[0] == 0


def test_reading_for_unknown_nozzle_rejected(db):
    """E2: readings for a nozzle id that does not exist are rejected."""
    insert_business_day(db, DAY1)
    with pytest.raises(sqlite3.IntegrityError, match="unknown or inactive"):
        db.execute(
            "INSERT INTO nozzle_readings (nozzle_id, business_date, reading_ml)"
            " VALUES (9999, ?, ?)",
            (DAY1, READING_DAY1_ML),
        )


def test_reading_for_nonexistent_business_date_rejected(db):
    """E2 + FK: a reading for a business date with no business_days row."""
    with pytest.raises(sqlite3.IntegrityError):
        insert_reading(db, "1 PG+", "2026-10-12", READING_DAY1_ML)


def test_negative_reading_rejected(db):
    """reading_ml < 0 is rejected by the CHECK constraint (and E2)."""
    insert_business_day(db, DAY1)
    with pytest.raises(sqlite3.IntegrityError):
        insert_reading(db, "1 PG+", DAY1, -1000)
    assert db.execute("SELECT COUNT(*) FROM nozzle_readings").fetchone()[0] == 0


def test_backwards_reading_rejected_without_meter_event(db):
    """E2: a reading lower than the previous closing is rejected unless a
    meter_events row explains it (assumption §11.1 — a reset is never a
    negative sale)."""
    insert_business_day(db, DAY1)
    insert_reading(db, "1 PG+", DAY1, READING_DAY1_ML)
    db.execute("UPDATE business_days SET status = 'closed' WHERE business_date = ?", (DAY1,))
    insert_business_day(db, DAY2)

    with pytest.raises(sqlite3.IntegrityError, match="lower than the previous"):
        insert_reading(db, "1 PG+", DAY2, READING_DAY1_ML - 1_000)

    assert (
        db.execute(
            "SELECT COUNT(*) FROM nozzle_readings WHERE business_date = ?", (DAY2,)
        ).fetchone()[0]
        == 0
    )


def test_backwards_reading_accepted_with_meter_event(db):
    """E2: after the manager records a meter event, the lower reading posts."""
    insert_business_day(db, DAY1)
    insert_reading(db, "1 PG+", DAY1, READING_DAY1_ML)
    db.execute("UPDATE business_days SET status = 'closed' WHERE business_date = ?", (DAY1,))
    insert_business_day(db, DAY2)

    insert_meter_event(
        db,
        "1 PG+",
        "reset",
        reason="meter replaced after fault",
        reading_before_ml=READING_DAY1_ML,
        reading_after_ml=12_000,
    )

    row_id = insert_reading(db, "1 PG+", DAY2, 12_000)
    row = db.execute(
        "SELECT reading_ml FROM nozzle_readings WHERE id = ?", (row_id,)
    ).fetchone()
    assert row[0] == 12_000


def test_forward_reading_accepted(db):
    """The normal case: a higher closing reading posts without any events."""
    insert_business_day(db, DAY1)
    row_id = insert_reading(db, "1 PG+", DAY1, READING_DAY1_ML + 47_000)
    assert row_id > 0


def test_seed_nozzles_present(db):
    """Phase-1 seed: 4 configurable nozzles (2 petrol, 2 diesel)."""
    rows = db.execute(
        "SELECT n.code, p.code FROM nozzles n"
        " JOIN fuel_products p ON p.id = n.fuel_product_id"
        " ORDER BY n.sort_order"
    ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [
        ("1 PG+", "PETROL"),
        ("2 PG+", "PETROL"),
        ("3 HSD", "DIESEL"),
        ("4 HSD", "DIESEL"),
    ]
    assert nozzle_id(db, "1 PG+") > 0
