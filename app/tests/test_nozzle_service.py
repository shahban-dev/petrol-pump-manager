"""Nozzle service: readings, liters, meter events, corrections (Phase 2).

Acceptance test 1 (nozzle calculation): previous 84,938 L, current
84,985 L → 47 L (47,000 ml), carried forward to the next day.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services import errors

from .conftest import ensure_phase3_close_preconditions

DAY1 = "2026-10-10"
DAY2 = "2026-10-11"
DAY3 = "2026-10-12"

PREV_LITRES = "84938"  # the register's previous reading
CURR_LITRES = "84985"


def _open(day_service, day, cash=1_000_000):
    return day_service.open_day(day, opening_cash_paisa=cash)


def _close(day_service, nozzle_service, day, db_file, cash=1_000_000):
    # Phase-3 close preconditions: applicable fuel prices must be recorded.
    # These closes are all first-reading days (no sale intervals), so no
    # opening stock is needed.
    ensure_phase3_close_preconditions(db_file, day)
    recorded = {
        r.nozzle_code for r in nozzle_service.day_readings(day) if r.has_reading
    }
    for code in ("1 PG+", "2 PG+", "3 HSD", "4 HSD"):
        if code not in recorded:
            nozzle_service.record_reading(day, code, "10")
    return day_service.close_day(day, physical_cash_paisa=cash)


# ---------------------------------------------------------------------------
# record_reading
# ---------------------------------------------------------------------------
def test_nozzle_calculation_and_carry_forward(services, db_file):
    """84,938 → 84,985 = 47 L; next day shows 84,985 as previous."""
    day_service, nozzle_service = services
    _open(day_service, DAY1)
    nozzle_service.record_reading(DAY1, "1 PG+", PREV_LITRES)

    day1 = nozzle_service.day_readings(DAY1)[0]
    assert day1.nozzle_code == "1 PG+"
    assert day1.previous_ml is None  # first ever reading: no previous
    assert day1.current_ml == 84_938_000
    assert day1.liters_sold_ml is None

    _close(day_service, nozzle_service, DAY1, db_file)
    _open(day_service, DAY2)

    before = nozzle_service.day_readings(DAY2)[0]
    assert before.previous_ml == 84_938_000
    assert not before.has_reading

    nozzle_service.record_reading(DAY2, "1 PG+", CURR_LITRES)
    after = nozzle_service.day_readings(DAY2)[0]
    assert after.current_ml == 84_985_000
    assert after.liters_sold_ml == 47_000  # 47 liters, exact


def test_record_reading_accepts_decimal_and_str(services):
    day_service, nozzle_service = services
    _open(day_service, DAY1)
    nozzle_service.record_reading(DAY1, "1 PG+", Decimal("84938.5"))
    nozzle_service.record_reading(DAY1, "2 PG+", "84938.500")
    rows = nozzle_service.day_readings(DAY1)
    assert rows[0].current_ml == 84_938_500
    assert rows[1].current_ml == 84_938_500


def test_record_reading_rejects_float(services):
    """Floats are never accepted for quantity math — fail loud."""
    day_service, nozzle_service = services
    _open(day_service, DAY1)
    with pytest.raises(TypeError):
        nozzle_service.record_reading(DAY1, "1 PG+", 84938.5)


def test_record_reading_rejects_negative(services):
    day_service, nozzle_service = services
    _open(day_service, DAY1)
    with pytest.raises((ValueError, errors.BusinessRuleError)):
        nozzle_service.record_reading(DAY1, "1 PG+", "-5")


def test_identical_retry_is_idempotent(services, db):
    """Retrying with the same value returns the existing row — no duplicate."""
    day_service, nozzle_service = services
    _open(day_service, DAY1)
    first = nozzle_service.record_reading(DAY1, "1 PG+", CURR_LITRES)
    second = nozzle_service.record_reading(DAY1, "1 PG+", CURR_LITRES)
    assert first == second
    assert db.execute("SELECT COUNT(*) FROM nozzle_readings").fetchone()[0] == 1


def test_different_value_for_recorded_reading_rejected(services, db):
    """A second, different reading needs the correction workflow."""
    day_service, nozzle_service = services
    _open(day_service, DAY1)
    nozzle_service.record_reading(DAY1, "1 PG+", CURR_LITRES)
    with pytest.raises(errors.DuplicateError, match="correction"):
        nozzle_service.record_reading(DAY1, "1 PG+", "84990")
    # The original stands.
    row = nozzle_service.day_readings(DAY1)[0]
    assert row.current_ml == 84_985_000
    assert db.execute("SELECT COUNT(*) FROM nozzle_readings").fetchone()[0] == 1


def test_reading_on_closed_day_rejected(services, db_file):
    day_service, nozzle_service = services
    _open(day_service, DAY1)
    _close(day_service, nozzle_service, DAY1, db_file)
    with pytest.raises(errors.ClosedDayError, match="closed"):
        nozzle_service.record_reading(DAY1, "2 PG+", "50")


def test_reading_for_unknown_nozzle_rejected(services):
    day_service, nozzle_service = services
    _open(day_service, DAY1)
    with pytest.raises(errors.NotFoundError):
        nozzle_service.record_reading(DAY1, "9 XYZ", "50")


def test_reading_for_inactive_nozzle_rejected(services, db):
    day_service, nozzle_service = services
    _open(day_service, DAY1)
    db.execute("UPDATE nozzles SET is_active = 0 WHERE code = '1 PG+'")
    db.commit()
    with pytest.raises(errors.BusinessRuleError, match="inactive"):
        nozzle_service.record_reading(DAY1, "1 PG+", "50")


def test_reading_without_open_day_rejected(services):
    _, nozzle_service = services
    with pytest.raises(errors.NotFoundError, match="Open the day"):
        nozzle_service.record_reading(DAY1, "1 PG+", "50")


def test_backwards_reading_rejected_without_meter_event(services, db, db_file):
    """A lower reading is rejected — a reset is never a negative sale."""
    day_service, nozzle_service = services
    _open(day_service, DAY1)
    nozzle_service.record_reading(DAY1, "1 PG+", PREV_LITRES)
    _close(day_service, nozzle_service, DAY1, db_file)
    _open(day_service, DAY2)

    with pytest.raises(errors.BusinessRuleError, match="lower than the previous"):
        nozzle_service.record_reading(DAY2, "1 PG+", "84900")
    assert db.execute(
        "SELECT COUNT(*) FROM nozzle_readings WHERE business_date = ?", (DAY2,)
    ).fetchone()[0] == 0


def test_backwards_reading_allowed_after_meter_event(services, db_file):
    day_service, nozzle_service = services
    _open(day_service, DAY1)
    nozzle_service.record_reading(DAY1, "1 PG+", PREV_LITRES)
    _close(day_service, nozzle_service, DAY1, db_file)
    _open(day_service, DAY2)

    event_id = nozzle_service.record_meter_event(
        "1 PG+",
        "reset",
        reason="meter replaced after fault",
        reading_before_litres=PREV_LITRES,
        reading_after_litres="12",
    )
    assert event_id > 0
    nozzle_service.record_reading(DAY2, "1 PG+", "12")
    row = nozzle_service.day_readings(DAY2)[0]
    assert row.current_ml == 12_000
    # §11.1: a reset is never a negative sale — the interval quantity is
    # unknown from readings alone, so liters stay None (not negative).
    assert row.liters_sold_ml is None


def test_meter_event_requires_reason(services):
    day_service, nozzle_service = services
    _open(day_service, DAY1)
    with pytest.raises(errors.ValidationError, match="reason"):
        nozzle_service.record_meter_event("1 PG+", "reset", reason="  ")
    with pytest.raises(errors.ValidationError, match="Unknown meter event"):
        nozzle_service.record_meter_event("1 PG+", "teleport", reason="x")


def test_meter_events_listed(services):
    day_service, nozzle_service = services
    _open(day_service, DAY1)
    nozzle = nozzle_service.list_nozzles()[0]
    nozzle_service.record_meter_event(
        nozzle.id, "rollover", reason="meter wrapped at 999999"
    )
    events = nozzle_service.meter_events(nozzle.id)
    assert len(events) == 1
    assert events[0].event_type == "rollover"
    assert events[0].reason == "meter wrapped at 999999"


# ---------------------------------------------------------------------------
# correct_reading — no silent corrections
# ---------------------------------------------------------------------------
def test_correct_reading_preserves_original(services, db):
    """Correction = meter event + replacement row; original untouched."""
    day_service, nozzle_service = services
    _open(day_service, DAY1)
    nozzle_service.record_reading(DAY1, "1 PG+", CURR_LITRES)

    new_id = nozzle_service.correct_reading(
        DAY1, "1 PG+", "84990", reason="misread digit, verified on meter"
    )
    assert new_id > 0

    # Two rows now: the original and its replacement.
    rows = db.execute(
        "SELECT id, reading_ml, supersedes_reading_id FROM nozzle_readings"
        " WHERE business_date = ? ORDER BY id",
        (DAY1,),
    ).fetchall()
    assert len(rows) == 2
    original, replacement = rows
    assert original["reading_ml"] == 84_985_000
    assert original["supersedes_reading_id"] is None
    assert replacement["reading_ml"] == 84_990_000
    assert replacement["supersedes_reading_id"] == original["id"]

    # The correction meter event explains what changed.
    events = nozzle_service.meter_events(
        db.execute("SELECT id FROM nozzles WHERE code = '1 PG+'").fetchone()[0]
    )
    corrections = [e for e in events if e.event_type == "correction"]
    assert len(corrections) == 1
    assert corrections[0].reading_before_ml == 84_985_000
    assert corrections[0].reading_after_ml == 84_990_000
    assert corrections[0].reason == "misread digit, verified on meter"

    # Everywhere else sees the corrected (effective) reading.
    row = nozzle_service.day_readings(DAY1)[0]
    assert row.current_ml == 84_990_000

    # Both the event and the replacement are audited.
    audit_tables = {
        r[0]
        for r in db.execute(
            "SELECT DISTINCT table_name FROM audit_log"
            " WHERE table_name IN ('meter_events', 'nozzle_readings')"
        ).fetchall()
    }
    assert audit_tables == {"meter_events", "nozzle_readings"}


def test_correction_flows_into_next_day_liters(services, db_file):
    """The day after a correction computes liters from the corrected value."""
    day_service, nozzle_service = services
    _open(day_service, DAY1)
    nozzle_service.record_reading(DAY1, "1 PG+", CURR_LITRES)
    nozzle_service.correct_reading(DAY1, "1 PG+", "84990", reason="typo")
    _close(day_service, nozzle_service, DAY1, db_file)
    _open(day_service, DAY2)

    before = nozzle_service.day_readings(DAY2)[0]
    assert before.previous_ml == 84_990_000  # corrected, not the typo

    nozzle_service.record_reading(DAY2, "1 PG+", "85000")
    after = nozzle_service.day_readings(DAY2)[0]
    assert after.liters_sold_ml == 10_000


def test_correct_reading_requires_reason(services):
    day_service, nozzle_service = services
    _open(day_service, DAY1)
    nozzle_service.record_reading(DAY1, "1 PG+", CURR_LITRES)
    with pytest.raises(errors.ValidationError, match="reason"):
        nozzle_service.correct_reading(DAY1, "1 PG+", "84990", reason="")


def test_correct_reading_needs_existing_reading(services):
    day_service, nozzle_service = services
    _open(day_service, DAY1)
    with pytest.raises(errors.NotFoundError, match="no reading"):
        nozzle_service.correct_reading(DAY1, "1 PG+", "84990", reason="x")


def test_correct_reading_on_closed_day_rejected(services, db_file):
    day_service, nozzle_service = services
    _open(day_service, DAY1)
    _close(day_service, nozzle_service, DAY1, db_file)
    with pytest.raises(errors.ClosedDayError):
        nozzle_service.correct_reading(DAY1, "1 PG+", "84990", reason="x")


def test_correct_reading_same_value_is_noop(services, db):
    day_service, nozzle_service = services
    _open(day_service, DAY1)
    first = nozzle_service.record_reading(DAY1, "1 PG+", CURR_LITRES)
    assert nozzle_service.correct_reading(DAY1, "1 PG+", CURR_LITRES, reason="x") == first
    assert db.execute("SELECT COUNT(*) FROM nozzle_readings").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM meter_events").fetchone()[0] == 0
