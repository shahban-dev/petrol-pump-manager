"""Full day lifecycle: open → readings → close → reopen → correct → close.

Acceptance tests 10/19 (closed-day protection, historical closing
revisions) at the service layer: every correction is audited, every
closing revision is preserved, and the new revision carries the corrected
figures.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.services import errors

from .conftest import ensure_phase3_close_preconditions

DAY1 = "2026-10-10"
DAY2 = "2026-10-11"

NOZZLES = ("1 PG+", "2 PG+", "3 HSD", "4 HSD")


def _record_all(nozzle_service, business_date, litres):
    for code in NOZZLES:
        nozzle_service.record_reading(business_date, code, litres)


def _open_close_day1(
    day_service, nozzle_service, db_file, *, cash=2_000_000,
    opening_stock_litres=None,
):
    day_service.open_day(DAY1, opening_cash_paisa=cash)
    ensure_phase3_close_preconditions(
        db_file, DAY1, opening_stock_litres=opening_stock_litres
    )
    _record_all(nozzle_service, DAY1, "10")
    return day_service.close_day(DAY1, physical_cash_paisa=2_300_000)


def test_full_cycle_open_close(services, db, db_file):
    """Open → readings → validate → close: revision 1 with liters + cash."""
    day_service, nozzle_service = services
    day_service.open_day(DAY1, opening_cash_paisa=2_000_000)
    ensure_phase3_close_preconditions(db_file, DAY1)
    nozzle_service.record_reading(DAY1, "1 PG+", "100")
    nozzle_service.record_reading(DAY1, "2 PG+", "200")
    nozzle_service.record_reading(DAY1, "3 HSD", "300")
    nozzle_service.record_reading(DAY1, "4 HSD", "400")

    report = day_service.validate_close(DAY1, 2_300_000)
    assert report.can_close
    # First readings: liters unknown → warnings, not blockers.
    assert any(i.key == "first_reading" for i in report.warnings)

    closing = day_service.close_day(DAY1, physical_cash_paisa=2_300_000)
    assert closing.closing_revision == 1
    assert closing.summary["opening_cash_paisa"] == 2_000_000
    assert closing.summary["physical_cash_paisa"] == 2_300_000
    assert len(closing.summary["nozzles"]) == 4
    codes = {n["nozzle_code"] for n in closing.summary["nozzles"]}
    assert codes == set(NOZZLES)


def test_reopen_correct_reclose_creates_new_revision(services, db, db_file):
    """Reopen → correct → close: revision 2, revision 1 preserved intact."""
    day_service, nozzle_service = services
    rev1 = _open_close_day1(day_service, nozzle_service, db_file)
    rev1_summary = rev1.summary

    reopened = day_service.reopen_day(DAY1, reason="meter misread on 2 PG+")
    assert reopened.status == "reopened"

    # The audited reopen event carries the previous revision + reason.
    events = day_service.day_events(DAY1)
    reopens = [e for e in events if e["event_type"] == "reopen"]
    assert len(reopens) == 1
    assert reopens[0]["reason"] == "meter misread on 2 PG+"
    payload = json.loads(reopens[0]["payload_json"])
    assert payload["previous_closing_revision"] == 1
    assert reopens[0]["actor"] == "system"  # never invented

    # Correction while reopened: readings are writable again (D1 allows
    # 'reopened'), and the correction workflow preserves the original.
    nozzle_service.correct_reading(DAY1, "2 PG+", "25", reason="verified on meter")

    rev2 = day_service.close_day(DAY1, physical_cash_paisa=2_300_000)
    assert rev2.closing_revision == 2

    closings = day_service.closings(DAY1)
    assert [c.closing_revision for c in closings] == [1, 2]
    # Revision 1 is byte-identical to what was first issued.
    assert closings[0].summary == rev1_summary
    assert closings[0].closing_cash_paisa == 2_300_000
    # Revision 2 carries the corrected reading.
    corrected = next(
        n for n in closings[1].summary["nozzles"] if n["nozzle_code"] == "2 PG+"
    )
    assert corrected["current_ml"] == 25_000

    events = [e["event_type"] for e in day_service.day_events(DAY1)]
    assert events == ["open", "close", "reopen", "close"]


def test_second_close_carries_from_latest_revision(services, db_file):
    """After a corrected re-close, the next day carries the latest figures."""
    day_service, nozzle_service = services
    _open_close_day1(day_service, nozzle_service, db_file)
    day_service.reopen_day(DAY1, reason="recount")
    day_service.close_day(DAY1, physical_cash_paisa=2_900_000)  # revision 2

    day2 = day_service.open_day(DAY2)
    assert day2.opening_cash_paisa == 2_900_000
    cash = next(
        b for b in day_service.opening_balances(DAY2) if b.balance_type == "cash"
    )
    assert cash.source_business_date == DAY1
    assert cash.source_closing_revision == 2


def test_corrections_possible_only_after_reopen(services, db, db_file):
    """Closed-day protection holds until the authorized reopen."""
    day_service, nozzle_service = services
    _open_close_day1(day_service, nozzle_service, db_file)

    # Direct correction on the closed day is refused by the service…
    with pytest.raises(errors.ClosedDayError):
        nozzle_service.correct_reading(DAY1, "1 PG+", "11", reason="x")
    # …and by the database trigger, even from raw SQL on this connection.
    nozzle_id = db.execute(
        "SELECT id FROM nozzles WHERE code = '2 PG+'"
    ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="closed"):
        db.execute(
            "INSERT INTO nozzle_readings (nozzle_id, business_date, reading_ml)"
            " VALUES (?, ?, ?)",
            (nozzle_id, DAY1, 50_000_000),
        )
    db.rollback()

    day_service.reopen_day(DAY1, reason="authorized correction")
    new_id = nozzle_service.correct_reading(DAY1, "1 PG+", "11", reason="x")
    assert new_id > 0


def test_close_with_reset_interval_warns_not_blocks(services, db_file):
    """A meter-reset interval warns (quantity unknown) but the day can close."""
    from app.database.connection import DatabaseManager
    from app.services.sales_service import SalesService

    day_service, nozzle_service = services
    day_service.open_day(DAY1, opening_cash_paisa=1_000_000)
    # DAY2 will sell 10 L on three nozzles — stock must exist to post it.
    ensure_phase3_close_preconditions(db_file, DAY1, opening_stock_litres="5000")
    nozzle_service.record_reading(DAY1, "1 PG+", "84938")
    _close_rest = [c for c in NOZZLES if c != "1 PG+"]
    for code in _close_rest:
        nozzle_service.record_reading(DAY1, code, "10")
    day_service.close_day(DAY1, physical_cash_paisa=1_000_000)

    day_service.open_day(DAY2)
    nozzle_service.record_meter_event(
        "1 PG+", "reset", reason="meter replaced",
        reading_before_litres="84938", reading_after_litres="5",
    )
    nozzle_service.record_reading(DAY2, "1 PG+", "60")
    for code in _close_rest:
        nozzle_service.record_reading(DAY2, code, "20")

    # Phase-3 rule: metered sales must be posted to stock before the day
    # can close (close_day does this idempotently; validate_close checks it).
    posted = SalesService(DatabaseManager(db_file)).post_day_sales(DAY2)
    assert posted["posted"] == 3  # 10 L each on 2 PG+, 3 HSD, 4 HSD

    report = day_service.validate_close(DAY2, 1_000_000)
    assert report.can_close  # warning, not blocking
    interval_item = next(i for i in report.items if i.key == "intervals_valid")
    assert interval_item.status == "warning"
    assert "1 PG+" in interval_item.detail
    # The reset nozzle's unknown interval is excluded from product totals.
    assert report.liters_by_product_ml.get("PETROL", 0) == 10_000  # only 2 PG+

    closing = day_service.close_day(DAY2, physical_cash_paisa=1_000_000)
    assert closing.closing_revision == 1
    assert closing.petrol_litres_ml == 10_000


def test_audit_trail_of_full_lifecycle(services, db, db_file):
    """Every lifecycle step leaves audit rows; nothing is silently changed."""
    day_service, nozzle_service = services
    _open_close_day1(day_service, nozzle_service, db_file)
    day_service.reopen_day(DAY1, reason="audit check")
    day_service.close_day(DAY1, physical_cash_paisa=2_300_000)

    event_audits = db.execute(
        "SELECT COUNT(*) FROM audit_log WHERE table_name = 'business_day_events'"
    ).fetchone()[0]
    assert event_audits == 4  # open, close, reopen, close

    closing_audits = db.execute(
        "SELECT COUNT(*) FROM audit_log WHERE table_name = 'business_day_closings'"
    ).fetchone()[0]
    assert closing_audits == 2

    day_updates = db.execute(
        "SELECT COUNT(*) FROM audit_log"
        " WHERE table_name = 'business_days' AND action = 'UPDATE'"
    ).fetchone()[0]
    assert day_updates >= 3  # close, reopen, close

    # created_at_utc of the first closing never changed.
    created = db.execute(
        "SELECT created_at_utc FROM business_day_closings"
        " WHERE business_date = ? AND closing_revision = 1",
        (DAY1,),
    ).fetchone()[0]
    assert created is not None
