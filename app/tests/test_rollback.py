"""Transaction atomicity: a mid-transaction failure leaves zero partial
writes — no orphaned days, readings, or audit rows.

Design plan §10 acceptance test 17.
"""

from __future__ import annotations

import sqlite3

import pytest

from .conftest import audit_count, nozzle_id

DAY = "2026-10-10"


@pytest.fixture
def manual_txn(db):
    """The shared connection in manual-transaction mode (autocommit off,
    explicit BEGIN/COMMIT/ROLLBACK) for the duration of one test."""
    db.isolation_level = None  # DB-API autocommit: we own the transaction
    yield db
    # Defensive cleanup: never leak an open transaction into the fixture.
    try:
        db.rollback()
    except sqlite3.Error:
        pass


def test_failed_transaction_leaves_zero_partial_writes(manual_txn):
    """BEGIN; insert day; insert reading; force duplicate-closing error;
    ROLLBACK -> nothing persisted, including audit rows."""
    db = manual_txn
    before_audit = audit_count(db)

    db.execute("BEGIN IMMEDIATE")
    db.execute(
        "INSERT INTO business_days (business_date, status) VALUES (?, 'open')", (DAY,)
    )
    db.execute(
        "INSERT INTO nozzle_readings (nozzle_id, business_date, reading_ml)"
        " VALUES (?, ?, ?)",
        (nozzle_id(db, "1 PG+"), DAY, 84_985_000),
    )
    # Force the error: a second closing reading for the same nozzle+date (E1).
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO nozzle_readings (nozzle_id, business_date, reading_ml)"
            " VALUES (?, ?, ?)",
            (nozzle_id(db, "1 PG+"), DAY, 84_990_000),
        )
    db.execute("ROLLBACK")

    assert db.execute("SELECT COUNT(*) FROM business_days").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM nozzle_readings").fetchone()[0] == 0
    # Audit rows are written inside the same transaction -> rolled back too.
    assert audit_count(db) == before_audit


def test_failed_trigger_abort_rolls_back_statement(manual_txn):
    """A trigger ABORT inside an explicit transaction fails the statement;
    after ROLLBACK the earlier statements of the transaction are gone."""
    db = manual_txn

    db.execute("BEGIN IMMEDIATE")
    db.execute(
        "INSERT INTO business_days (business_date, status) VALUES (?, 'open')", (DAY,)
    )
    # Reading for an inactive nozzle -> E2 aborts the statement.
    db.execute("UPDATE nozzles SET is_active = 0 WHERE code = '1 PG+'")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO nozzle_readings (nozzle_id, business_date, reading_ml)"
            " VALUES (?, ?, ?)",
            (nozzle_id(db, "1 PG+"), DAY, 84_985_000),
        )
    db.execute("ROLLBACK")

    assert db.execute("SELECT COUNT(*) FROM business_days").fetchone()[0] == 0
    # The nozzle deactivation was inside the rolled-back transaction too.
    assert (
        db.execute("SELECT is_active FROM nozzles WHERE code = '1 PG+'").fetchone()[0]
        == 1
    )


def test_successful_transaction_commits_everything(manual_txn):
    """The happy path: BEGIN; day + reading; COMMIT -> both persist."""
    db = manual_txn

    db.execute("BEGIN IMMEDIATE")
    db.execute(
        "INSERT INTO business_days (business_date, status) VALUES (?, 'open')", (DAY,)
    )
    db.execute(
        "INSERT INTO nozzle_readings (nozzle_id, business_date, reading_ml)"
        " VALUES (?, ?, ?)",
        (nozzle_id(db, "1 PG+"), DAY, 84_985_000),
    )
    db.execute("COMMIT")

    assert db.execute("SELECT COUNT(*) FROM business_days").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM nozzle_readings").fetchone()[0] == 1
