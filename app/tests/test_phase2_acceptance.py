"""Phase-2 acceptance scenarios (task contract).

Each scenario from the Phase-2 assignment, stated explicitly and run
against temporary databases:

1. carry-forward traceability and idempotency
2. open/close happy path
3. closing blocked when validations fail
4. duplicate reading rejected
5. backwards reading rejected without meter event
6. reopen workflow audit + new revision preserving the old
7. midnight business-date handling (Asia/Karachi)
8. retry-after-failure posts nothing twice
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.calculations import time as timecalc
from app.services import errors

from .conftest import ensure_phase3_close_preconditions

DAY1 = "2026-10-10"
DAY2 = "2026-10-11"

NOZZLES = ("1 PG+", "2 PG+", "3 HSD", "4 HSD")


def _record_all(nozzle_service, business_date, litres):
    for code in NOZZLES:
        nozzle_service.record_reading(business_date, code, litres)


# 1. carry-forward traceability and idempotency -------------------------------
def test_1_carry_forward_traceable_and_idempotent(services, db, db_file):
    day_service, nozzle_service = services
    day_service.open_day(DAY1, opening_cash_paisa=5_000_000)
    ensure_phase3_close_preconditions(db_file, DAY1)
    _record_all(nozzle_service, DAY1, "100")
    day_service.close_day(DAY1, physical_cash_paisa=5_750_000)

    # Opening the next day twice: same result, no duplicated rows.
    day_a = day_service.open_day(DAY2)
    day_b = day_service.open_day(DAY2)
    assert day_a.opening_cash_paisa == day_b.opening_cash_paisa == 5_750_000

    balances = day_service.opening_balances(DAY2)
    cash_rows = [b for b in balances if b.balance_type == "cash"]
    assert len(cash_rows) == 1
    assert cash_rows[0].source_business_date == DAY1
    assert cash_rows[0].source_closing_revision == 1

    # Previous nozzle readings carried forward automatically.
    readings = nozzle_service.day_readings(DAY2)
    assert all(r.previous_ml == 100_000 for r in readings)
    assert all(not r.has_reading for r in readings)

    assert db.execute("SELECT COUNT(*) FROM business_days").fetchone()[0] == 2


# 2. open/close happy path ---------------------------------------------------
def test_2_open_close_happy_path(services, db_file):
    day_service, nozzle_service = services
    day = day_service.open_day(DAY1, opening_cash_paisa=1_000_000)
    ensure_phase3_close_preconditions(db_file, DAY1)
    assert day.status == "open"

    _record_all(nozzle_service, DAY1, "50")
    report = day_service.validate_close(DAY1, 1_100_000)
    assert report.can_close

    closing = day_service.close_day(DAY1, physical_cash_paisa=1_100_000)
    assert closing.closing_revision == 1
    assert day_service.get_day(DAY1).status == "closed"


# 3. closing blocked when validations fail -----------------------------------
def test_3_closing_blocked_when_validations_fail(services, db):
    day_service, nozzle_service = services
    day_service.open_day(DAY1, opening_cash_paisa=1_000_000)
    nozzle_service.record_reading(DAY1, "1 PG+", "50")  # others missing

    with pytest.raises(errors.CloseBlockedError) as excinfo:
        day_service.close_day(DAY1, physical_cash_paisa=1_000_000)
    assert not excinfo.value.report.can_close
    assert any(
        i.key == "readings_complete" for i in excinfo.value.report.blocking_errors
    )
    assert db.execute("SELECT COUNT(*) FROM business_day_closings").fetchone()[0] == 0
    assert day_service.get_day(DAY1).status == "open"


# 4. duplicate reading rejected ----------------------------------------------
def test_4_duplicate_reading_rejected(services, db):
    day_service, nozzle_service = services
    day_service.open_day(DAY1, opening_cash_paisa=1_000_000)
    nozzle_service.record_reading(DAY1, "1 PG+", "84985")

    with pytest.raises(errors.DuplicateError):
        nozzle_service.record_reading(DAY1, "1 PG+", "84990")

    count = db.execute(
        "SELECT COUNT(*) FROM nozzle_readings WHERE business_date = ?", (DAY1,)
    ).fetchone()[0]
    assert count == 1


# 5. backwards reading rejected without meter event ---------------------------
def test_5_backwards_reading_rejected(services, db, db_file):
    day_service, nozzle_service = services
    day_service.open_day(DAY1, opening_cash_paisa=1_000_000)
    ensure_phase3_close_preconditions(db_file, DAY1)
    nozzle_service.record_reading(DAY1, "1 PG+", "84938")
    _record_all(nozzle_service, DAY1, "84938")
    day_service.close_day(DAY1, physical_cash_paisa=1_000_000)
    day_service.open_day(DAY2)

    with pytest.raises(errors.BusinessRuleError, match="lower than the previous"):
        nozzle_service.record_reading(DAY2, "1 PG+", "84900")

    count = db.execute(
        "SELECT COUNT(*) FROM nozzle_readings WHERE business_date = ?", (DAY2,)
    ).fetchone()[0]
    assert count == 0


# 6. reopen workflow: audit + new revision, old preserved ---------------------
def test_6_reopen_audit_and_new_revision(services, db, db_file):
    day_service, nozzle_service = services
    day_service.open_day(DAY1, opening_cash_paisa=1_000_000)
    ensure_phase3_close_preconditions(db_file, DAY1)
    _record_all(nozzle_service, DAY1, "100")
    rev1 = day_service.close_day(DAY1, physical_cash_paisa=1_000_000)

    day_service.reopen_day(DAY1, reason="correct 1 PG+ reading")
    nozzle_service.correct_reading(DAY1, "1 PG+", "150", reason="verified")
    rev2 = day_service.close_day(DAY1, physical_cash_paisa=1_000_000)

    assert rev2.closing_revision == 2
    closings = day_service.closings(DAY1)
    assert len(closings) == 2
    assert closings[0].summary == rev1.summary  # old revision untouched

    reopen_events = db.execute(
        "SELECT new_values_json FROM audit_log"
        " WHERE table_name = 'business_day_events' AND action = 'INSERT'"
    ).fetchall()
    snapshots = [json.loads(r[0]) for r in reopen_events]
    assert any(s["event_type"] == "reopen" for s in snapshots)
    assert any(s.get("reason") == "correct 1 PG+ reading" for s in snapshots)


# 7. midnight business-date handling (Asia/Karachi) -----------------------------
def test_7_midnight_business_date_handling():
    utc = timezone.utc
    # 2026-10-15 00:30 in Karachi == 2026-10-14 19:30 UTC → business date 10-15.
    moment = datetime(2026, 10, 14, 19, 30, tzinfo=utc)
    assert timecalc.business_date_for(moment) == "2026-10-15"
    # 23:59 PKT stays on the same business date.
    moment = datetime(2026, 10, 14, 18, 59, tzinfo=utc)
    assert timecalc.business_date_for(moment) == "2026-10-14"
    # Exactly midnight PKT starts the new local date.
    moment = datetime(2026, 10, 14, 19, 0, tzinfo=utc)
    assert timecalc.business_date_for(moment) == "2026-10-15"
    # Storage format is UTC with millisecond precision.
    assert timecalc.utc_now_iso().endswith("Z")
    assert len(timecalc.utc_now_iso()) == 24  # YYYY-MM-DDTHH:MM:SS.SSSZ


def test_7_transaction_after_midnight_joins_open_day(services):
    """A reading recorded after midnight PKT belongs to the open day the
    manager opened — the app never auto-rolls the business date."""
    day_service, nozzle_service = services
    day_service.open_day(DAY1, opening_cash_paisa=1_000_000)
    # The wall clock may say 00:20 PKT on DAY2, but DAY1 is still the open
    # business day: the reading posts to DAY1, not to a phantom DAY2.
    reading_id = nozzle_service.record_reading(DAY1, "1 PG+", "84985")
    assert reading_id > 0
    assert day_service.get_day(DAY2) is None


# 8. retry-after-failure posts nothing twice ------------------------------------
def test_8_retry_after_failure_posts_nothing_twice(services, db, db_file):
    day_service, nozzle_service = services
    day_service.open_day(DAY1, opening_cash_paisa=1_000_000)
    ensure_phase3_close_preconditions(db_file, DAY1)

    # Failed close writes nothing…
    with pytest.raises(errors.CloseBlockedError):
        day_service.close_day(DAY1, physical_cash_paisa=1_000_000)
    # …failed reading writes nothing…
    with pytest.raises(errors.BusinessRuleError):
        nozzle_service.record_reading(DAY1, "9 XYZ", "10")

    assert db.execute("SELECT COUNT(*) FROM business_day_closings").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM nozzle_readings").fetchone()[0] == 0

    # …and the successful retry posts exactly once.
    _record_all(nozzle_service, DAY1, "10")
    closing = day_service.close_day(DAY1, physical_cash_paisa=1_000_000)
    assert closing.closing_revision == 1
    assert db.execute("SELECT COUNT(*) FROM business_day_closings").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM nozzle_readings").fetchone()[0] == 4
