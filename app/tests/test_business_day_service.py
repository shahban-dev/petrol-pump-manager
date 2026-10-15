"""Business-day service: open, carry-forward, close, reopen (Phase 2).

All scenarios run against temporary databases through the service layer —
the same path the UI uses. Design plan §10, master prompt §10.

Conventions: money in integer paisa, readings in liters (Decimal/str) at
the service boundary.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.services import errors

from .conftest import ensure_phase3_close_preconditions

DAY1 = "2026-10-10"
DAY2 = "2026-10-11"
DAY3 = "2026-10-12"

NOZZLES = ("1 PG+", "2 PG+", "3 HSD", "4 HSD")


def _record_all(nozzle_service, business_date, litres):
    for code in NOZZLES:
        nozzle_service.record_reading(business_date, code, litres)


# ---------------------------------------------------------------------------
# open_day
# ---------------------------------------------------------------------------
def test_open_first_day_requires_opening_cash(services):
    """The very first day needs a manager-entered opening cash (§11.5)."""
    day_service, _ = services
    with pytest.raises(errors.ValidationError, match="first business day"):
        day_service.open_day(DAY1)
    assert day_service.get_day(DAY1) is None  # nothing was written


def test_open_first_day_with_cash(services, db):
    """First-day open stores cash + traceable balance + audited open event."""
    day_service, _ = services
    day = day_service.open_day(DAY1, opening_cash_paisa=2_000_000)

    assert day.status == "open"
    assert day.opening_cash_paisa == 2_000_000
    assert day.prev_closing_date is None

    balances = day_service.opening_balances(DAY1)
    assert len(balances) == 1
    cash = balances[0]
    assert cash.balance_type == "cash"
    assert cash.amount_paisa == 2_000_000
    # Traceability: first-day balances have no source closing (manager-entered).
    assert cash.source_business_date is None
    assert cash.source_closing_revision is None

    events = day_service.day_events(DAY1)
    assert [e["event_type"] for e in events] == ["open"]
    payload = json.loads(events[0]["payload_json"])
    assert payload["source"] == "initial"
    assert payload["opening_cash_paisa"] == 2_000_000


def test_open_day_is_idempotent(services, db):
    """Re-opening an open day never duplicates rows."""
    day_service, _ = services
    first = day_service.open_day(DAY1, opening_cash_paisa=1_000_000)
    second = day_service.open_day(DAY1, opening_cash_paisa=9_999_999)

    assert second.business_date == first.business_date
    assert second.opening_cash_paisa == 1_000_000  # original kept, not overwritten
    assert db.execute("SELECT COUNT(*) FROM business_days").fetchone()[0] == 1
    assert (
        db.execute("SELECT COUNT(*) FROM opening_balances").fetchone()[0] == 1
    )
    assert (
        db.execute(
            "SELECT COUNT(*) FROM business_day_events WHERE event_type = 'open'"
        ).fetchone()[0]
        == 1
    )


def test_open_day_blocked_while_previous_open(services):
    """A new day cannot open until the previous day is closed."""
    day_service, _ = services
    day_service.open_day(DAY1, opening_cash_paisa=1_000_000)
    with pytest.raises(errors.BusinessRuleError, match="close it before opening"):
        day_service.open_day(DAY2)
    assert day_service.get_day(DAY2) is None


def test_open_day_rejects_past_date(services):
    """Days open in date order."""
    day_service, _ = services
    day_service.open_day(DAY2, opening_cash_paisa=1_000_000)
    with pytest.raises(errors.ValidationError, match="not after"):
        day_service.open_day(DAY1)


def test_open_closed_day_refused(services, db_file):
    """An already-closed day cannot be (re)opened — reopen workflow instead."""
    day_service, nozzle_service = services
    day_service.open_day(DAY1, opening_cash_paisa=1_000_000)
    ensure_phase3_close_preconditions(db_file, DAY1)
    _record_all(nozzle_service, DAY1, "10")
    day_service.close_day(DAY1, physical_cash_paisa=1_000_000)

    with pytest.raises(errors.BusinessRuleError, match="already closed"):
        day_service.open_day(DAY1)


def test_date_gap_requires_reason(services, db, db_file):
    """Skipped dates need the audited recovery path (reason)."""
    day_service, nozzle_service = services
    day_service.open_day(DAY1, opening_cash_paisa=1_000_000)
    ensure_phase3_close_preconditions(db_file, DAY1)
    _record_all(nozzle_service, DAY1, "10")
    day_service.close_day(DAY1, physical_cash_paisa=1_000_000)

    with pytest.raises(errors.ValidationError, match="gap"):
        day_service.open_day(DAY3)
    assert day_service.get_day(DAY3) is None  # failed open wrote nothing

    day = day_service.open_day(DAY3, gap_reason="station closed, no records")
    assert day.status == "open"
    events = day_service.day_events(DAY3)
    payload = json.loads(events[0]["payload_json"])
    assert payload["gap_reason"] == "station closed, no records"


def test_carry_forward_from_previous_closing(services, db_file):
    """Opening cash = previous closing's counted cash, source recorded."""
    day_service, nozzle_service = services
    day_service.open_day(DAY1, opening_cash_paisa=2_000_000)
    ensure_phase3_close_preconditions(db_file, DAY1)
    _record_all(nozzle_service, DAY1, "10")
    closing = day_service.close_day(DAY1, physical_cash_paisa=2_500_000)
    assert closing.closing_revision == 1

    day2 = day_service.open_day(DAY2)
    assert day2.opening_cash_paisa == 2_500_000
    assert day2.prev_closing_date == DAY1

    balances = day_service.opening_balances(DAY2)
    cash = next(b for b in balances if b.balance_type == "cash")
    assert cash.amount_paisa == 2_500_000
    assert cash.source_business_date == DAY1
    assert cash.source_closing_revision == 1


def test_open_non_operating_day(services, db_file):
    """A non-operating day carries balances unchanged and takes no readings."""
    day_service, nozzle_service = services
    day_service.open_day(DAY1, opening_cash_paisa=3_000_000)
    ensure_phase3_close_preconditions(db_file, DAY1)
    _record_all(nozzle_service, DAY1, "10")
    day_service.close_day(DAY1, physical_cash_paisa=3_100_000)

    nonop = day_service.open_day(DAY2, non_operating=True)
    assert nonop.status == "non_operating"
    assert nonop.opening_cash_paisa == 3_100_000  # carried unchanged

    with pytest.raises(errors.ClosedDayError, match="non-operating"):
        nozzle_service.record_reading(DAY2, "1 PG+", "20")

    # The next operating day carries forward from the last *closing*.
    day3 = day_service.open_day(DAY3)
    assert day3.opening_cash_paisa == 3_100_000
    balances = day_service.opening_balances(DAY3)
    cash = next(b for b in balances if b.balance_type == "cash")
    assert cash.source_business_date == DAY1
    assert cash.source_closing_revision == 1


def test_invalid_date_rejected(services):
    """Malformed business dates never reach the database."""
    day_service, _ = services
    for bad in ("2026-13-40", "not-a-date", "2026/10/10", ""):
        with pytest.raises(errors.ValidationError):
            day_service.open_day(bad)


def test_negative_opening_cash_rejected(services):
    day_service, _ = services
    with pytest.raises(errors.ValidationError):
        day_service.open_day(DAY1, opening_cash_paisa=-100)


# ---------------------------------------------------------------------------
# validate_close / close_day
# ---------------------------------------------------------------------------
def test_validate_close_reports_blocking_items(services):
    """Missing readings and missing physical cash block the close."""
    day_service, nozzle_service = services
    day_service.open_day(DAY1, opening_cash_paisa=1_000_000)
    nozzle_service.record_reading(DAY1, "1 PG+", "100")

    report = day_service.validate_close(DAY1)
    assert not report.can_close
    by_key = {item.key: item for item in report.items}
    assert by_key["readings_complete"].status == "fail"
    assert "2 PG+" in by_key["readings_complete"].detail
    assert by_key["physical_cash"].status == "fail"
    # Phase-3 honesty: prices are now a real blocking check (no price set
    # here → fail); stock has no postable intervals on first readings → pass.
    assert by_key["prices_recorded"].status == "fail"
    assert by_key["stock_calculated"].status == "pass"


def test_close_day_happy_path(services, db, db_file):
    """Full close: immutable revision-1 record, audited, in one transaction."""
    day_service, nozzle_service = services
    day_service.open_day(DAY1, opening_cash_paisa=1_000_000)
    ensure_phase3_close_preconditions(db_file, DAY1)
    _record_all(nozzle_service, DAY1, "10")

    closing = day_service.close_day(DAY1, physical_cash_paisa=1_250_000)

    assert closing.closing_revision == 1
    assert closing.opening_cash_paisa == 1_000_000
    assert closing.closing_cash_paisa == 1_250_000
    assert closing.business_date == DAY1

    day = day_service.get_day(DAY1)
    assert day.status == "closed"
    assert day.closing_revision == 1
    assert day.physical_cash_paisa == 1_250_000
    assert day.closed_at_utc is not None

    events = [e["event_type"] for e in day_service.day_events(DAY1)]
    assert events == ["open", "close"]

    # The closing ledger row is immutable, even to raw SQL.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE business_day_closings SET closing_cash_paisa = 1"
            " WHERE business_date = ?",
            (DAY1,),
        )
    db.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "DELETE FROM business_day_closings WHERE business_date = ?",
            (DAY1,),
        )
    db.rollback()


def test_close_day_blocked_rolls_back_cleanly(services, db, db_file):
    """A blocked close writes nothing: no closing, day stays open."""
    day_service, nozzle_service = services
    day_service.open_day(DAY1, opening_cash_paisa=1_000_000)
    ensure_phase3_close_preconditions(db_file, DAY1)
    nozzle_service.record_reading(DAY1, "1 PG+", "100")  # 3 nozzles missing

    with pytest.raises(errors.CloseBlockedError) as excinfo:
        day_service.close_day(DAY1, physical_cash_paisa=1_000_000)

    report = excinfo.value.report
    assert not report.can_close
    assert report.blocking_errors

    assert db.execute("SELECT COUNT(*) FROM business_day_closings").fetchone()[0] == 0
    assert day_service.get_day(DAY1).status == "open"
    assert "close" not in [e["event_type"] for e in day_service.day_events(DAY1)]

    # Retry after fixing posts exactly once.
    _record_all(nozzle_service, DAY1, "100")
    closing = day_service.close_day(DAY1, physical_cash_paisa=1_000_000)
    assert closing.closing_revision == 1
    assert (
        db.execute("SELECT COUNT(*) FROM business_day_closings").fetchone()[0]
        == 1
    )


def test_close_day_negative_cash_rejected(services):
    day_service, _ = services
    day_service.open_day(DAY1, opening_cash_paisa=1_000_000)
    with pytest.raises(errors.ValidationError):
        day_service.close_day(DAY1, physical_cash_paisa=-5)


def test_close_requires_open_day(services, db_file):
    """Closing a non-existent or already-closed day is refused."""
    day_service, nozzle_service = services
    with pytest.raises(errors.CloseBlockedError):
        day_service.close_day("2026-01-01", physical_cash_paisa=100)

    day_service.open_day(DAY1, opening_cash_paisa=1_000_000)
    ensure_phase3_close_preconditions(db_file, DAY1)
    _record_all(nozzle_service, DAY1, "10")
    day_service.close_day(DAY1, physical_cash_paisa=1_000_000)
    with pytest.raises(errors.CloseBlockedError):
        day_service.close_day(DAY1, physical_cash_paisa=1_000_000)


# ---------------------------------------------------------------------------
# reopen_day
# ---------------------------------------------------------------------------
def test_reopen_requires_reason(services, db_file):
    day_service, nozzle_service = services
    day_service.open_day(DAY1, opening_cash_paisa=1_000_000)
    ensure_phase3_close_preconditions(db_file, DAY1)
    _record_all(nozzle_service, DAY1, "10")
    day_service.close_day(DAY1, physical_cash_paisa=1_000_000)

    for bad_reason in ("", "   ", None):
        with pytest.raises(errors.ValidationError, match="reason"):
            day_service.reopen_day(DAY1, reason=bad_reason)
    assert day_service.get_day(DAY1).status == "closed"


def test_reopen_only_latest_day(services, db_file):
    """An older day already feeds newer opening balances — not reopenable."""
    day_service, nozzle_service = services
    day_service.open_day(DAY1, opening_cash_paisa=1_000_000)
    ensure_phase3_close_preconditions(db_file, DAY1)
    _record_all(nozzle_service, DAY1, "10")
    day_service.close_day(DAY1, physical_cash_paisa=1_000_000)
    day_service.open_day(DAY2)

    with pytest.raises(errors.BusinessRuleError, match="most recent"):
        day_service.reopen_day(DAY1, reason="should fail")


def test_reopen_non_closed_day_refused(services):
    day_service, _ = services
    day_service.open_day(DAY1, opening_cash_paisa=1_000_000)
    with pytest.raises(errors.BusinessRuleError, match="not closed"):
        day_service.reopen_day(DAY1, reason="nope")
