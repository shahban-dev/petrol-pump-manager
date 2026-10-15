"""Phase 7: reopen end-to-end (acceptance Test 19, full guard stack).

Design plan documentation/00_design_plan.md §4 group D, §11 assumption
#13 (reversal vs. reopen), §6 (reconciliation), acceptance tests 10/19:

* close a FULLY-POSTED day — readings + posted deliveries + posted sales —
  at revision 1;
* a direct-SQL UPDATE of a posted row on a SECOND (raw, no-pragma)
  connection is rejected by the database triggers themselves (Group C);
* authorized ``reopen_day`` with a manager reason;
* the correction goes through the PROPER workflow — ``correct_reading``
  (a ``correction`` meter event + a replacement reading row linked via
  ``supersedes_reading_id``; the original row is never touched) — followed
  by ``reverse_settlements`` + re-settle, the reversal + re-record path;
* reclose → revision 2; revision 1 is byte-identical; the audit log holds
  the ordered events (close r1 → reopen with reason → correction rows →
  close r2); the corrected figures appear only in r2;
* reopening an older (non-latest) day is rejected.

Extends app/tests/test_close_reopen.py — it does not duplicate its
open→close→reopen cases; here the day is fully posted (sales on the
ledger) and the raw-SQL guard runs on the second connection.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

import pytest

from app.services import errors

from .conftest import (
    ensure_phase3_close_preconditions,
    new_uuid,
    phase5_services,  # noqa: F401  (fixture)
    seed_first_day,
    seed_phase3_day,
)

PREV = "2026-10-06"
DAY = "2026-10-07"

DAY1 = "2026-10-10"
DAY2 = "2026-10-11"

PREV_READINGS = {
    "1 PG+": "84938",
    "2 PG+": "81234",
    "3 HSD": "90123",
    "4 HSD": "88456",
}
# 100 L intervals against PREV_READINGS (all postable).
DAY_READINGS = {
    "1 PG+": "85038",
    "2 PG+": "81334",
    "3 HSD": "90223",
    "4 HSD": "88556",
}
# The meter on 2 PG+ was misread: the true closing reading was 116 L more.
CORRECTED_2PG_LITRES = "81450"

REASON = "meter misread on 2 PG+ — verified against meter photo"


def _nozzle(summary: dict, code: str) -> dict:
    return next(n for n in summary["nozzles"] if n["nozzle_code"] == code)


def _close_fully_posted_day(svc, db_file):
    """Open PREV (baselines), then DAY with deliveries + readings + sales,
    and close DAY at revision 1. Returns (rev1, rev1_summary_bytes)."""
    seed_first_day(svc, PREV, opening_stock_litres=Decimal("5000"))
    svc.inventory.set_opening_stock("DIESEL", Decimal("5000"), PREV)
    for code, litres in PREV_READINGS.items():
        svc.nozzles.record_reading(PREV, code, litres)
    svc.sales.post_day_sales(PREV)  # first readings: unknown qty, no-op
    svc.days.close_day(PREV, physical_cash_paisa=2_000_000)

    supplier = seed_phase3_day(svc, DAY)  # prices + stock carried forward
    svc.deliveries.record_delivery(
        supplier=supplier,
        fuel_product="PETROL",
        tank="TANK-PETROL",
        business_date=DAY,
        quantity_litres=Decimal("2000"),
        unit_cost_pkr=Decimal("250.0000"),
        supplier_invoice_no="INV-P7-1",
        delivery_uuid=new_uuid(),
    )
    svc.deliveries.record_delivery(
        supplier=supplier,
        fuel_product="DIESEL",
        tank="TANK-DIESEL",
        business_date=DAY,
        quantity_litres=Decimal("2000"),
        unit_cost_pkr=Decimal("240.0000"),
        supplier_invoice_no="INV-P7-2",
        delivery_uuid=new_uuid(),
    )
    for code, litres in DAY_READINGS.items():
        svc.nozzles.record_reading(DAY, code, litres)
    posted = svc.sales.post_day_sales(DAY)
    assert posted["posted"] == 4, posted  # every interval on the ledger
    rev1 = svc.days.close_day(DAY, physical_cash_paisa=3_500_000)
    assert rev1.closing_revision == 1
    return rev1


def test_reopen_full_cycle_triggers_reject_direct_sql(
    phase5_services, db, db2, db_file
):
    """Acceptance Test 19 end-to-end: close r1 (fully posted) → direct SQL
    rejected on a second connection → authorized reopen → proper
    correction (meter event + replacement row, settlement reversal +
    re-record) → reclose r2; r1 byte-identical; audit ordered; corrected
    figures only in r2."""
    svc = phase5_services
    rev1 = _close_fully_posted_day(svc, db_file)
    rev1_summary = rev1.summary
    rev1_bytes = db.execute(
        "SELECT summary_json FROM business_day_closings"
        " WHERE business_date = ? AND closing_revision = 1",
        (DAY,),
    ).fetchone()[0]

    # -- 1. The database itself rejects edits to posted rows, even from raw
    # -- SQL on a second, pragma-less connection (Group C triggers).
    reading_id = db.execute(
        "SELECT id FROM nozzle_readings"
        " WHERE business_date = ?"
        " AND nozzle_id = (SELECT id FROM nozzles WHERE code = '2 PG+')"
        " AND supersedes_reading_id IS NULL",
        (DAY,),
    ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db2.execute(
            "UPDATE nozzle_readings SET reading_ml = ? WHERE id = ?",
            (99_000_000, reading_id),
        )
    db2.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db2.execute(
            "UPDATE business_day_closings SET closing_cash_paisa = ?"
            " WHERE id = ?",
            (1, rev1.id),
        )
    db2.rollback()

    # -- 2. Authorized reopen with a manager reason.
    reopened = svc.days.reopen_day(DAY, reason=REASON)
    assert reopened.status == "reopened"

    # -- 3. The correction goes through the PROPER workflow: a 'correction'
    # -- meter event + a replacement reading row (original preserved), then
    # -- the stale settlement batch is reversed so the reclose re-settles
    # -- from the corrected meter figures (reversal + re-record).
    new_id = svc.nozzles.correct_reading(
        DAY, "2 PG+", CORRECTED_2PG_LITRES, reason=REASON
    )
    assert new_id > 0
    meter_event = db.execute(
        "SELECT event_type, reading_before_ml, reading_after_ml, reason"
        " FROM meter_events WHERE event_type = 'correction'"
    ).fetchone()
    assert meter_event["reading_before_ml"] == 81_334_000
    assert meter_event["reading_after_ml"] == 81_450_000
    assert meter_event["reason"] == REASON
    replacement = db.execute(
        "SELECT id, reading_ml, supersedes_reading_id FROM nozzle_readings"
        " WHERE supersedes_reading_id IS NOT NULL"
    ).fetchone()
    assert int(replacement["id"]) == int(new_id)
    assert replacement["reading_ml"] == 81_450_000
    assert int(replacement["supersedes_reading_id"]) == int(reading_id)
    # The original row is untouched.
    original_ml = db.execute(
        "SELECT reading_ml FROM nozzle_readings WHERE id = ?", (reading_id,)
    ).fetchone()[0]
    assert original_ml == 81_334_000

    svc.cash.reverse_settlements(DAY, reason="re-settle after meter correction")

    # -- 4. Reclose → revision 2.
    rev2 = svc.days.close_day(DAY, physical_cash_paisa=3_500_000)
    assert rev2.closing_revision == 2

    # -- 5. Revision 1 is preserved byte-identical (same summary values).
    closings = svc.days.closings(DAY)
    assert [c.closing_revision for c in closings] == [1, 2]
    assert closings[0].summary == rev1_summary
    still_bytes = db.execute(
        "SELECT summary_json FROM business_day_closings"
        " WHERE business_date = ? AND closing_revision = 1",
        (DAY,),
    ).fetchone()[0]
    assert still_bytes == rev1_bytes

    # -- 6. The corrected figures appear only in revision 2.
    assert _nozzle(closings[0].summary, "2 PG+")["current_ml"] == 81_334_000
    assert _nozzle(closings[0].summary, "2 PG+")["liters_sold_ml"] == 100_000
    assert _nozzle(closings[1].summary, "2 PG+")["current_ml"] == 81_450_000
    assert _nozzle(closings[1].summary, "2 PG+")["liters_sold_ml"] == 216_000
    # The other nozzles are untouched by the correction in both revisions.
    for code in ("1 PG+", "3 HSD", "4 HSD"):
        assert _nozzle(closings[0].summary, code) == _nozzle(
            closings[1].summary, code
        )

    # -- 7. The audit log holds the ordered events: close r1 → reopen (with
    # -- reason) → correction rows → close r2.
    events = svc.days.day_events(DAY)
    assert [e["event_type"] for e in events] == [
        "open",
        "close",
        "reopen",
        "close",
    ]
    reopen_event = next(e for e in events if e["event_type"] == "reopen")
    assert reopen_event["reason"] == REASON
    assert json.loads(reopen_event["payload_json"])[
        "previous_closing_revision"
    ] == 1

    audit = db.execute(
        "SELECT id, table_name, record_id, action FROM audit_log ORDER BY id"
    ).fetchall()

    def _audit_id(table: str, record_id: int) -> int:
        ids = [
            r["id"]
            for r in audit
            if r["table_name"] == table and r["record_id"] == record_id
        ]
        assert len(ids) == 1, f"expected one audit row for {table}:{record_id}"
        return int(ids[0])

    reopen_audit = _audit_id("business_day_events", reopen_event["id"])
    correction_event_id = meter_event_row_id(db)
    correction_audit = _audit_id("meter_events", correction_event_id)
    replacement_audit = _audit_id("nozzle_readings", int(new_id))
    r2_closing_audit = _audit_id("business_day_closings", closings[1].id)
    close_events = sorted(
        e["id"] for e in events if e["event_type"] == "close"
    )
    r2_close_event_audit = _audit_id("business_day_events", close_events[-1])
    r1_close_event_audit = _audit_id("business_day_events", close_events[0])

    assert r1_close_event_audit < reopen_audit  # r1 close came first
    assert reopen_audit < correction_audit < replacement_audit
    assert replacement_audit < r2_closing_audit < r2_close_event_audit

    # The reversal itself is audited too (reversal rows, never deletes).
    reversal_rows = db.execute(
        "SELECT COUNT(*) FROM sales_settlements"
        " WHERE business_date = ? AND is_reversal = 1",
        (DAY,),
    ).fetchone()[0]
    assert reversal_rows >= 1


def meter_event_row_id(db) -> int:
    """Primary key of the 'correction' meter event row."""
    row = db.execute(
        "SELECT id FROM meter_events WHERE event_type = 'correction'"
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_reopen_non_latest_day_rejected(services, db_file):
    """Only the most recent business day may be reopened (Phase-2 rule,
    design §11): an older closed day can no longer be reopened once a
    later day exists, because its figures already feed newer days'
    opening balances."""
    day_service, nozzle_service = services
    day_service.open_day(DAY1, opening_cash_paisa=2_000_000)
    ensure_phase3_close_preconditions(db_file, DAY1)
    for code in ("1 PG+", "2 PG+", "3 HSD", "4 HSD"):
        nozzle_service.record_reading(DAY1, code, "10")
    day_service.close_day(DAY1, physical_cash_paisa=2_300_000)

    # A later day now exists (non-operating: carries balances, needs no
    # readings, and is terminal — never closed).
    day_service.open_day(DAY2, non_operating=True)

    with pytest.raises(errors.BusinessRuleError) as excinfo:
        day_service.reopen_day(DAY1, reason="too late")
    assert excinfo.value.message_key == "err.only_latest_reopen"

    # DAY1 is still closed and untouched; DAY2 is the latest recorded day.
    assert day_service.get_day(DAY1).status == "closed"
    assert day_service.get_day(DAY2).status == "non_operating"
