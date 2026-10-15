"""Phase 7: sale-movement correction after meter corrections.

Acceptance-test territory 10/17/19. Regression tests for the fixed bug:
previously, when a reading was corrected AFTER its sale movement had been
posted, the re-post safety net only checked movement EXISTENCE, so the
stale quantity stayed on the ledger and theoretical stock was wrong
(e.g. 100 L posted, corrected to 216 L, ledger kept -100 L).

The posting is now correction-aware: the net posted quantity (original
sale + prior correction adjustments) is compared with the effective
interval, and any divergence appends a single ``adjustment`` movement for
the delta. The original row is never edited or deleted (append-only
ledger, Group C).
"""

from __future__ import annotations

from decimal import Decimal

from .conftest import (
    new_uuid,
    phase5_services,  # noqa: F401  (fixture)
    seed_first_day,
    seed_phase3_day,
)

PREV = "2026-10-06"
DAY = "2026-10-07"

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

REASON = "meter misread on 2 PG+ — verified against meter photo"


def _setup_posted_day(svc):
    """PREV baselined and closed; DAY opened with deliveries, 100 L/nozzle
    readings, and sales posted. Returns the supplier id."""
    seed_first_day(svc, PREV, opening_stock_litres=Decimal("5000"))
    svc.inventory.set_opening_stock("DIESEL", Decimal("5000"), PREV)
    for code, litres in PREV_READINGS.items():
        svc.nozzles.record_reading(PREV, code, litres)
    svc.sales.post_day_sales(PREV)  # first readings: unknown qty, no-op
    svc.days.close_day(PREV, physical_cash_paisa=2_000_000)

    supplier = seed_phase3_day(svc, DAY)
    svc.deliveries.record_delivery(
        supplier=supplier,
        fuel_product="PETROL",
        tank="TANK-PETROL",
        business_date=DAY,
        quantity_litres=Decimal("2000"),
        unit_cost_pkr=Decimal("250.0000"),
        supplier_invoice_no="INV-P7C-1",
        delivery_uuid=new_uuid(),
    )
    svc.deliveries.record_delivery(
        supplier=supplier,
        fuel_product="DIESEL",
        tank="TANK-DIESEL",
        business_date=DAY,
        quantity_litres=Decimal("2000"),
        unit_cost_pkr=Decimal("240.0000"),
        supplier_invoice_no="INV-P7C-2",
        delivery_uuid=new_uuid(),
    )
    for code, litres in DAY_READINGS.items():
        svc.nozzles.record_reading(DAY, code, litres)
    posted = svc.sales.post_day_sales(DAY)
    assert posted["posted"] == 4, posted
    return supplier


def _movements_for_interval(db, business_date, nozzle_code):
    """All ledger rows for one nozzle's sale interval on a date."""
    nozzle_id = db.execute(
        "SELECT id FROM nozzles WHERE code = ?", (nozzle_code,)
    ).fetchone()[0]
    return db.execute(
        "SELECT movement_type, quantity_ml, source_uuid, reason"
        " FROM inventory_movements"
        " WHERE business_date = ?"
        " AND (source_uuid = ?"
        " OR source_uuid LIKE ? ESCAPE '\\')",
        (
            business_date,
            f"nozzle-sale:{business_date}:{nozzle_id}:0",
            f"nozzle-sale-corr:{business_date}:{nozzle_id}:0:"
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
            + "%",
        ),
    ).fetchall()


def _theoretical_ml(svc, business_date, product_code):
    positions = svc.inventory.stock_positions(business_date)
    return sum(
        p.theoretical_ml for p in positions if p.fuel_product_code == product_code
    )


def test_correction_after_posting_appends_delta_adjustment(
    phase5_services, db
):
    """Correct 100 L → 216 L after posting: one -116 L adjustment is
    appended; the original -100 L row is untouched; net is -216 L."""
    svc = phase5_services
    _setup_posted_day(svc)

    svc.nozzles.correct_reading(DAY, "2 PG+", "81450", reason=REASON)

    result = svc.sales.post_day_sales(DAY)
    assert result["posted"] == 0, result
    assert result["corrected"] == 1, result
    assert result["already_posted"] == 3, result

    rows = _movements_for_interval(db, DAY, "2 PG+")
    by_type = [(r["movement_type"], r["quantity_ml"]) for r in rows]
    assert ("sale", -100_000) in by_type  # original preserved
    assert ("adjustment", -116_000) in by_type  # delta appended
    assert sum(q for _, q in by_type) == -216_000
    adj = next(r for r in rows if r["movement_type"] == "adjustment")
    assert "Meter correction" in adj["reason"]

    # Theoretical stock reflects the corrected sale:
    # petrol 5000 + 2000 - (100 + 216) = 6684 L.
    assert _theoretical_ml(svc, DAY, "PETROL") == 6_684_000

    # Re-running the post is a no-op (idempotent): no second correction.
    again = svc.sales.post_day_sales(DAY)
    assert again["corrected"] == 0, again
    assert again["already_posted"] == 4, again
    assert len(_movements_for_interval(db, DAY, "2 PG+")) == 2


def test_second_correction_appends_another_delta(phase5_services, db):
    """A second correction (216 L → 250 L) appends one more adjustment;
    the net posted quantity always tracks the effective interval."""
    svc = phase5_services
    _setup_posted_day(svc)

    svc.nozzles.correct_reading(DAY, "2 PG+", "81450", reason=REASON)
    assert svc.sales.post_day_sales(DAY)["corrected"] == 1

    svc.nozzles.correct_reading(DAY, "2 PG+", "81484", reason="second check")
    result = svc.sales.post_day_sales(DAY)
    assert result["corrected"] == 1, result

    rows = _movements_for_interval(db, DAY, "2 PG+")
    quantities = sorted(r["quantity_ml"] for r in rows)
    assert quantities == [-116_000, -100_000, -34_000]
    assert sum(quantities) == -250_000
    assert len(rows) == 3  # original sale + two correction adjustments
    assert _theoretical_ml(svc, DAY, "PETROL") == 6_650_000  # 7000 - 350


def test_correction_to_zero_fully_reverses_posted_sale(
    phase5_services, db
):
    """Correcting the reading back to the previous value (0 L interval)
    fully reverses the posted sale: net posted becomes 0."""
    svc = phase5_services
    _setup_posted_day(svc)

    # Previous reading for 2 PG+ was 81234: correcting to it => 0 L.
    svc.nozzles.correct_reading(DAY, "2 PG+", "81234", reason=REASON)
    result = svc.sales.post_day_sales(DAY)
    assert result["corrected"] == 1, result

    rows = _movements_for_interval(db, DAY, "2 PG+")
    assert sum(r["quantity_ml"] for r in rows) == 0
    assert ("adjustment", 100_000) in [
        (r["movement_type"], r["quantity_ml"]) for r in rows
    ]
    # Petrol stock: nothing sold on 2 PG+ any more (7000 - 100).
    assert _theoretical_ml(svc, DAY, "PETROL") == 6_900_000


def test_checklist_warns_about_pending_correction(phase5_services):
    """validate_close warns (not blocks) when a posted sale diverges from
    its corrected interval — the manager sees the pending adjustment
    before the close applies it."""
    svc = phase5_services
    _setup_posted_day(svc)

    report = svc.days.validate_close(DAY, 3_500_000)
    item = next(i for i in report.items if i.key == "stock_calculated")
    assert item.status == "pass"

    svc.nozzles.correct_reading(DAY, "2 PG+", "81450", reason=REASON)
    report = svc.days.validate_close(DAY, 3_500_000)
    assert report.can_close  # warning, never blocking
    item = next(i for i in report.items if i.key == "stock_calculated")
    assert item.status == "warning"
    assert "2 PG+" in item.detail

    # After the safety-net post inside close_day, the warning is gone.
    svc.days.close_day(DAY, physical_cash_paisa=3_500_000)


def test_close_after_correction_records_corrected_stock(phase5_services, db):
    """End-to-end: correct after posting, then close — the immutable
    closing record's theoretical stock matches the corrected ledger."""
    svc = phase5_services
    _setup_posted_day(svc)

    svc.nozzles.correct_reading(DAY, "2 PG+", "81450", reason=REASON)
    rev = svc.days.close_day(DAY, physical_cash_paisa=3_500_000)
    assert rev.closing_revision == 1

    theoretical = db.execute(
        "SELECT summary_json FROM business_day_closings"
        " WHERE business_date = ? AND closing_revision = 1",
        (DAY,),
    ).fetchone()[0]
    import json as _json

    summary = _json.loads(theoretical)
    assert summary["theoretical_stock_ml"]["PETROL"] == 6_684_000
    assert summary["theoretical_stock_ml"]["DIESEL"] == 6_800_000
