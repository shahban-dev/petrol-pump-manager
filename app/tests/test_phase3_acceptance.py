"""Phase-3 cross-cutting acceptance tests.

Covers the task contract's SalesService / BusinessDayService surface::

    SalesService.day_intervals(business_date)
    SalesService.day_sales(business_date)
    SalesService.post_day_sales(business_date, *, actor=None)
        -> dict(posted, already_posted, skipped_unknown_qty)
    SalesService.reconcile_day(business_date)
    BusinessDayService.open_day / validate_close / close_day  (Phase 2, reused)

plus the meter-event wrap math, the Group-C immutability triggers, the
closed-day (D1) guards and the Phase-3 close checklist.

Notes:
  * Opening stock is entered on the first day; prices carry forward until
    changed mid-day behind segment snapshots (see D1 in
    test_price_service.py).
  * Tests that post sales record readings only for the moving nozzle(s), or
    use documented meter resets for the rest: a 0 L interval cannot be
    posted (a 0-quantity sale movement violates the ledger CHECK), so static
    nozzles are left unread on days that are not closed.
  * ``day_sales`` returns a ``DaySales`` DTO with ``revenue_by_product_paisa``;
    ``reconcile_day`` returns a dict with ``meter_revenue_by_product_paisa``,
    ``posted_sales_by_product_paisa`` and ``identity_ok``.
  * ``validate_close`` items are ``CloseCheckItem(key, status, …)``;
    ``close_day`` returns a ``ClosingRecord`` whose ``summary`` dict carries
    ``revenue_by_product_paisa``.

Design plan: 00_design_plan.md §6 (reconciliation), §8 (stock),
§11 assumptions 1–3 and 11, acceptance tests 8 and 13, trigger groups C/D/F.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

import pytest

from .conftest import (
    close_prior_day,
    dto_field,
    fuel_product_id,
    new_uuid,
    phase3_services,  # noqa: F401  (fixture)
    pkt_midnight_utc_iso,
    record_reset_interval,
    seed_first_day,
    seed_phase3_day,
    tank_id,
)

PREV = "2026-10-14"
DAY = "2026-10-15"

PREV_READINGS = {
    "1 PG+": "84938",
    "2 PG+": "81234",
    "3 HSD": "90123",
    "4 HSD": "88456",
}

P1 = Decimal("265.9630")


def _sales_revenue_by_product(day_sales) -> dict[str, int]:
    """{fuel product code: revenue_paisa} from the ``DaySales`` DTO."""
    if isinstance(day_sales, dict):
        return {str(k): int(v) for k, v in day_sales.items()}
    return {
        str(k): int(v)
        for k, v in dto_field(day_sales, "revenue_by_product_paisa").items()
    }


def _reconcile_figures(report) -> tuple[dict[str, int], dict[str, int], bool]:
    """(meter_revenue, posted_revenue, identity_ok) from reconcile_day."""
    meter = {
        str(k): int(v)
        for k, v in dto_field(report, "meter_revenue_by_product_paisa").items()
    }
    posted = {
        str(k): int(v)
        for k, v in dto_field(report, "posted_sales_by_product_paisa").items()
    }
    return meter, posted, bool(dto_field(report, "identity_ok"))


# 5. sales reconciliation identity (acceptance Test 8) --------------------------
def test_sales_reconciliation_identity(phase3_services, db, db2):
    """Meter-derived revenue = Σ interval amounts = day_sales revenue =
    reconcile_day revenue, per product; and a duplicated source_uuid is
    rejected by the database (F1)."""
    svc = phase3_services
    close_prior_day(
        svc, PREV, PREV_READINGS, opening_stock_litres=Decimal("5000")
    )
    seed_phase3_day(svc, DAY)
    svc.nozzles.record_reading(DAY, "1 PG+", "84985")  # 47 L @ 265.9630
    svc.nozzles.record_reading(DAY, "2 PG+", "81254")  # 20 L @ 265.9630
    result = svc.sales.post_day_sales(DAY)
    assert result["posted"] >= 1

    # Identity 1 (design §6): revenue per product = Σ priced intervals.
    interval_revenue: dict[str, int] = {}
    for interval in svc.sales.day_intervals(DAY):
        code = dto_field(interval, "fuel_product_code", "product_code")
        interval_revenue[code] = interval_revenue.get(code, 0) + int(
            dto_field(interval, "amount_paisa")
        )
    sales_revenue = _sales_revenue_by_product(svc.sales.day_sales(DAY))
    for code, revenue in sales_revenue.items():
        assert interval_revenue.get(code, 0) == revenue, (
            f"product {code}: Σ intervals = {interval_revenue.get(code, 0)},"
            f" day_sales = {revenue}"
        )
    # 47 L + 20 L = 67 L @ 265.9630
    assert sales_revenue["PETROL"] == 1_250_026 + 531_926 == 1_781_952

    # Identity 2: reconcile_day agrees with day_sales, per product.
    meter_revenue, posted_revenue, identity_ok = _reconcile_figures(
        svc.sales.reconcile_day(DAY)
    )
    assert meter_revenue == sales_revenue
    assert posted_revenue == sales_revenue
    assert identity_ok

    # F1: the same source_uuid cannot post its movement twice — raw SQL
    # proves the guard lives in the database, not just the service.
    existing = db.execute(
        "SELECT source_uuid, business_date, fuel_product_id, tank_id"
        " FROM inventory_movements WHERE movement_type = 'sale' LIMIT 1"
    ).fetchone()
    assert existing is not None
    with pytest.raises(sqlite3.IntegrityError):
        db2.execute(
            "INSERT INTO inventory_movements (uuid, business_date,"
            " fuel_product_id, tank_id, movement_type, quantity_ml,"
            " source_table, source_uuid)"
            " VALUES (?, ?, ?, ?, 'sale', ?, 'nozzle_sale_intervals', ?)",
            (
                new_uuid(),
                existing["business_date"],
                existing["fuel_product_id"],
                existing["tank_id"],
                -1000,
                existing["source_uuid"],
            ),
        )
    db2.rollback()


# 12. rollover wrap math ----------------------------------------------------------
def test_rollover_wrap_math(phase3_services, db):
    """A documented rollover computes (wrap − before) + after; a reset with
    no wrap leaves the interval quantity unknown — never negative (§11.1)."""
    svc = phase3_services
    close_prior_day(
        svc,
        PREV,
        {"1 PG+": "99900", "2 PG+": "81234", "3 HSD": "90123", "4 HSD": "88456"},
        opening_stock_litres=Decimal("5000"),
    )
    seed_phase3_day(svc, DAY)

    # The meter rolled over at 100,000 L; the wrap value is recorded
    # explicitly (migration 007: meter_events.wrap_value_ml). The service's
    # record_meter_event has no wrap parameter (per the task contract), and
    # meter_events is mutable/audited, so the test sets it directly.
    event_id = svc.nozzles.record_meter_event(
        "1 PG+",
        "rollover",
        reason="meter rolled over",
        reading_before_litres=Decimal("99900"),
        reading_after_litres=Decimal("50"),
    )
    db.execute(
        "UPDATE meter_events SET wrap_value_ml = ? WHERE id = ?",
        (100_000_000, event_id),
    )
    db.commit()
    svc.nozzles.record_reading(DAY, "1 PG+", "50")  # (100000-99900)+50 = 150 L

    # A reset with no wrap value: quantity unknowable, never a negative sale.
    svc.nozzles.record_meter_event(
        "2 PG+",
        "reset",
        reason="meter reset by technician",
        reading_before_litres=Decimal("81234"),
        reading_after_litres=Decimal("10"),
    )
    svc.nozzles.record_reading(DAY, "2 PG+", "10")

    by_nozzle: dict[str, list] = {}
    for interval in svc.sales.day_intervals(DAY):
        by_nozzle.setdefault(dto_field(interval, "nozzle_code"), []).append(interval)

    rollover_rows = by_nozzle.get("1 PG+", [])
    assert len(rollover_rows) == 1
    assert dto_field(rollover_rows[0], "quantity_ml") == 150_000

    reset_rows = by_nozzle.get("2 PG+", [])
    assert reset_rows, "the reset interval must be present (never silently dropped)"
    assert all(dto_field(r, "quantity_ml") is None for r in reset_rows), (
        "a reset without a wrap value has unknown quantity — never negative"
    )

    # The wrap-derived sale posts against carried stock; the reset is skipped.
    result = svc.sales.post_day_sales(DAY)
    assert result["posted"] >= 1
    assert "2 PG+" in result["skipped_unknown_qty"]


# 13. immutability triggers (Group C) ----------------------------------------------
def test_immutability_triggers(phase3_services, db, db2):
    """UPDATE/DELETE on the posted Phase-3 ledgers is rejected from any
    connection; tank_readings (a mutable measurement) stays updatable and
    audited."""
    svc = phase3_services
    close_prior_day(
        svc, PREV, PREV_READINGS, opening_stock_litres=Decimal("5000")
    )
    supplier = seed_phase3_day(svc, DAY)
    svc.deliveries.record_delivery(
        supplier=supplier,
        fuel_product="PETROL",
        tank="TANK-PETROL",
        business_date=DAY,
        quantity_litres=Decimal("2000"),
        unit_cost_pkr=Decimal("250.0000"),
        supplier_invoice_no="INV-013-1",
        delivery_uuid=new_uuid(),
    )
    svc.nozzles.record_reading(DAY, "1 PG+", "85438")  # 500 L sale
    assert svc.sales.post_day_sales(DAY)["posted"] >= 1
    svc.inventory.record_adjustment(
        fuel_product="PETROL",
        tank="TANK-PETROL",
        business_date=DAY,
        quantity_litres_signed=Decimal("-20"),
        reason="dip variance",
        authorized_by="manager",
    )
    svc.inventory.record_physical_count("TANK-PETROL", DAY, Decimal("6480"))

    def _blocked(sql: str) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            db2.execute(sql)
        db2.rollback()

    _blocked("UPDATE fuel_deliveries SET total_cost_paisa = total_cost_paisa + 1")
    _blocked("DELETE FROM fuel_deliveries")
    _blocked("UPDATE inventory_movements SET quantity_ml = 0")
    _blocked("DELETE FROM inventory_movements")
    _blocked("UPDATE price_history SET unit_price_10k = unit_price_10k + 1")
    _blocked("DELETE FROM price_history")
    _blocked("UPDATE inventory_adjustments SET quantity_ml = 1")
    _blocked("DELETE FROM inventory_adjustments")

    # The mutable exception: a dip reading may be corrected — and the
    # correction is audited.
    tank = tank_id(db, "TANK-PETROL")
    audits_before = db.execute(
        "SELECT COUNT(*) FROM audit_log"
        " WHERE table_name = 'tank_readings' AND action = 'UPDATE'"
    ).fetchone()[0]
    db2.execute(
        "UPDATE tank_readings SET quantity_ml = ?"
        " WHERE tank_id = ? AND business_date = ?",
        (6_481_000, tank, DAY),
    )
    db2.commit()
    assert (
        db.execute(
            "SELECT quantity_ml FROM tank_readings WHERE tank_id = ? AND business_date = ?",
            (tank, DAY),
        ).fetchone()["quantity_ml"]
        == 6_481_000
    )
    audits_after = db.execute(
        "SELECT COUNT(*) FROM audit_log"
        " WHERE table_name = 'tank_readings' AND action = 'UPDATE'"
    ).fetchone()[0]
    assert audits_after == audits_before + 1


# 14. closed-day guards (Group D1) --------------------------------------------------
def test_closed_day_guards(phase3_services, db, db2):
    """After close_day, direct SQL inserts into the Phase-3 date-scoped
    tables for that date are rejected (D1)."""
    svc = phase3_services
    close_prior_day(
        svc, PREV, PREV_READINGS, opening_stock_litres=Decimal("5000")
    )
    supplier = seed_phase3_day(svc, DAY)
    svc.deliveries.record_delivery(
        supplier=supplier,
        fuel_product="PETROL",
        tank="TANK-PETROL",
        business_date=DAY,
        quantity_litres=Decimal("2000"),
        unit_cost_pkr=Decimal("250.0000"),
        supplier_invoice_no="INV-014-1",
        delivery_uuid=new_uuid(),
    )
    svc.nozzles.record_reading(DAY, "1 PG+", "84985")  # 47 L sale
    record_reset_interval(svc, DAY, "2 PG+", PREV_READINGS["2 PG+"])
    record_reset_interval(svc, DAY, "3 HSD", PREV_READINGS["3 HSD"])
    record_reset_interval(svc, DAY, "4 HSD", PREV_READINGS["4 HSD"])
    assert svc.sales.post_day_sales(DAY)["posted"] >= 1
    svc.days.close_day(DAY, physical_cash_paisa=2_000_000)
    assert svc.days.get_day(DAY).status == "closed"

    prod = fuel_product_id(db, "PETROL")
    tank = tank_id(db, "TANK-PETROL")
    diesel_tank = tank_id(db, "TANK-DIESEL")

    with pytest.raises(sqlite3.IntegrityError):
        db2.execute(
            "INSERT INTO fuel_deliveries (uuid, supplier_id, fuel_product_id,"
            " tank_id, business_date, quantity_ml, unit_cost_10k,"
            " total_cost_paisa, supplier_invoice_no, amount_paid_paisa)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_uuid(), supplier, prod, tank, DAY,
                1_000_000, 2_500_000, 25_000_000, "INV-014-2", 0,
            ),
        )
    db2.rollback()

    with pytest.raises(sqlite3.IntegrityError):
        db2.execute(
            "INSERT INTO price_history (fuel_product_id, effective_from_utc,"
            " unit_price_10k, business_date) VALUES (?, ?, ?, ?)",
            (prod, "2026-10-15T10:00:00.000Z", 2_700_000, DAY),
        )
    db2.rollback()

    with pytest.raises(sqlite3.IntegrityError):
        db2.execute(
            "INSERT INTO tank_readings (tank_id, business_date, quantity_ml)"
            " VALUES (?, ?, ?)",
            (diesel_tank, DAY, 6_000_000),
        )
    db2.rollback()


# 15. Phase-3 close checklist ---------------------------------------------------------
def test_close_checklist_phase3(phase3_services, db):
    """The guided close enforces the Phase-3 items: prices_recorded fails
    until prices exist, tank_readings warns until a dip is entered,
    stock_calculated fails until the day's sales are posted — then the day
    closes and the summary carries revenue."""
    svc = phase3_services

    # Part A — first day: prices_recorded fails before any price exists.
    day_a = "2026-10-14"
    svc.days.open_day(day_a, opening_cash_paisa=2_000_000)

    def _status(report, key: str) -> str:
        for item in report.items:
            if item.key == key:
                return item.status
        raise AssertionError(f"checklist has no item {key!r}")

    assert _status(svc.days.validate_close(day_a), "prices_recorded") == "fail"
    svc.prices.set_price("PETROL", P1, pkt_midnight_utc_iso(day_a), day_a)
    svc.prices.set_price("DIESEL", P1, pkt_midnight_utc_iso(day_a), day_a)
    assert _status(svc.days.validate_close(day_a), "prices_recorded") == "pass"

    # Finish day A so it can serve as the prior day: opening stock, readings
    # (no previous readings -> unknown intervals), post (no-op), close.
    svc.inventory.set_opening_stock("PETROL", Decimal("5000"), day_a)
    for code, litres in PREV_READINGS.items():
        svc.nozzles.record_reading(day_a, code, litres)
    svc.sales.post_day_sales(day_a)
    svc.days.close_day(day_a, physical_cash_paisa=2_000_000)

    # Part B — next day: dip warning, stock_calculated fail->pass, close.
    day_b = "2026-10-15"
    svc.days.open_day(day_b, opening_cash_paisa=2_000_000)
    assert _status(svc.days.validate_close(day_b), "tank_readings") == "warning"

    svc.nozzles.record_reading(day_b, "1 PG+", "84985")  # 47 L sale
    record_reset_interval(svc, day_b, "2 PG+", PREV_READINGS["2 PG+"])
    record_reset_interval(svc, day_b, "3 HSD", PREV_READINGS["3 HSD"])
    record_reset_interval(svc, day_b, "4 HSD", PREV_READINGS["4 HSD"])
    assert _status(svc.days.validate_close(day_b), "stock_calculated") == "fail"
    assert svc.sales.post_day_sales(day_b)["posted"] >= 1
    assert _status(svc.days.validate_close(day_b), "stock_calculated") == "pass"

    closing = svc.days.close_day(day_b, physical_cash_paisa=2_100_000)
    assert closing.closing_revision == 1
    assert svc.days.get_day(day_b).status == "closed"
    assert "revenue" in json.dumps(closing.summary).lower(), (
        f"closing summary must include revenue: {closing.summary!r}"
    )
