"""Phase-5 service tests: weighted-average COGS, P&L snapshots, estimates.

Covers the task contract's Phase-5 accounting surface::

    AccountingService.daily_pnl / period_pnl / cost_snapshot_conn
    OtherMaterialsService.add_product / record_purchase / record_sale /
        record_adjustment / stock / list_transactions

plus the close flow's frozen cost/P&L snapshot and the §11.11–12 costing
rules (periodic weighted average per business day; estimate labeling).

Design plan: 00_design_plan.md §5 (numeric standards), §11 assumptions
11–12, acceptance test 20 (financial calculations).
"""

from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP

import pytest

from app.services import errors

from .conftest import (
    close_prior_day,
    insert_price_row,
    phase4_services,  # noqa: F401  (fixture)
    phase5_services,  # noqa: F401  (fixture)
    pkt_clock_utc_iso,
    pkt_midnight_utc_iso,
    record_reset_interval,
    seed_phase3_day,
)

DAY = "2026-10-15"
PREV = "2026-10-14"
PREV_READINGS = {
    "1 PG+": "84938",
    "2 PG+": "81234",
    "3 HSD": "60000",
    "4 HSD": "70000",
}

#: Clean integer revenue math: 250 PKR/L → exactly 25 paisa per ml.
PRICE_250 = Decimal("250")

NOZZLES = ("1 PG+", "2 PG+", "3 HSD", "4 HSD")


def _first_valued_day(
    svc,
    business_date: str,
    *,
    qty_litres: str,
    value_pkr: str,
    price_pkr: Decimal = PRICE_250,
) -> None:
    """First-day setup with a manager-entered opening stock VALUE for both
    products (the §11.12 cost basis), prices, and a supplier."""
    svc.days.open_day(business_date, opening_cash_paisa=2_000_000)
    svc.deliveries.add_supplier("PSO Depot")
    effective = pkt_midnight_utc_iso(business_date)
    svc.prices.set_price("PETROL", Decimal(price_pkr), effective, business_date)
    svc.prices.set_price("DIESEL", Decimal(price_pkr), effective, business_date)
    svc.inventory.set_opening_stock(
        "PETROL", Decimal(qty_litres), business_date, value_pkr=Decimal(value_pkr)
    )
    svc.inventory.set_opening_stock(
        "DIESEL", Decimal(qty_litres), business_date, value_pkr=Decimal(value_pkr)
    )


def _post_sale(svc, day: str, nozzle: str, litres: str, prev_map: dict) -> None:
    """Record a metered sale of *litres* L on *nozzle*, reset the others."""
    svc.nozzles.record_reading(
        day, nozzle, str(Decimal(prev_map[nozzle]) + Decimal(litres))
    )
    for code in NOZZLES:
        if code != nozzle:
            record_reset_interval(svc, day, code, prev_map[code])
    svc.sales.post_day_sales(day)


def _close_meter_day(
    svc,
    day: str,
    *,
    sale_litres: str,
    physical_cash_paisa: int,
    expense_paisa: int = 0,
    opening_stock_litres: Decimal = Decimal("10000"),
) -> None:
    """Close the prior day (stock quantity only, no carrying value), then
    open *day*, post one metered sale, settle it all-cash, and close it."""
    close_prior_day(
        svc, PREV, PREV_READINGS, price_pkr=PRICE_250,
        opening_stock_litres=opening_stock_litres,
    )
    seed_phase3_day(svc, day)
    _post_sale(svc, day, "1 PG+", sale_litres, PREV_READINGS)
    revenue = int(Decimal(sale_litres) * 1000 * 25)
    if expense_paisa:
        svc.expenses.record_expense(
            business_date=day,
            category="Salaries",
            amount_paisa=expense_paisa,
            method="cash",
            occurred_at_utc=pkt_clock_utc_iso(day, 12),
        )
    svc.cash.record_settlements(
        day,
        cash_by_product={"PETROL": revenue},
        occurred_at_utc=pkt_clock_utc_iso(day, 18),
    )
    svc.days.close_day(day, physical_cash_paisa=physical_cash_paisa)


# ---------------------------------------------------------------------------
# weighted-average COGS
# ---------------------------------------------------------------------------


def test_weighted_average_cogs(phase5_services, db):
    """Opening 5,000 L @ 1,325,000 paisa + a 2,000 L delivery @ PKR 265.75/L
    → sell 500 L → avg = (V0 + C) / (Q0 + Q1); COGS = 500,000 ml × avg
    rounded once, half-up. All integers, exact."""
    svc = phase5_services
    _first_valued_day(svc, PREV, qty_litres="5000", value_pkr="13250")
    for code, litres in PREV_READINGS.items():
        svc.nozzles.record_reading(PREV, code, litres)
    svc.sales.post_day_sales(PREV)
    svc.days.close_day(PREV, physical_cash_paisa=2_000_000)

    seed_phase3_day(svc, DAY)
    delivery_id = svc.deliveries.record_delivery(
        supplier="PSO Depot",
        fuel_product="PETROL",
        tank="TANK-PETROL",
        business_date=DAY,
        quantity_litres=Decimal("2000"),
        unit_cost_pkr=Decimal("265.75"),
        supplier_invoice_no="INV-001",
        occurred_at_utc=pkt_clock_utc_iso(DAY, 9),
    )
    assert (
        db.execute(
            "SELECT total_cost_paisa FROM fuel_deliveries WHERE id = ?",
            (delivery_id,),
        ).fetchone()["total_cost_paisa"]
        == 53_150_000  # exactly 2,000 L × 265.75
    )
    _post_sale(svc, DAY, "1 PG+", "500", PREV_READINGS)  # 500 L @ 250
    svc.expenses.record_expense(
        business_date=DAY,
        category="Salaries",
        amount_paisa=200_000,
        method="cash",
        occurred_at_utc=pkt_clock_utc_iso(DAY, 12),
    )
    svc.cash.record_settlements(
        DAY,
        cash_by_product={"PETROL": 12_500_000},
        occurred_at_utc=pkt_clock_utc_iso(DAY, 18),
    )
    svc.days.close_day(DAY, physical_cash_paisa=2_000_000 + 12_500_000 - 200_000)

    # Independent arithmetic: avg = 54,475,000 / 7,000,000 paisa per ml.
    opening_value = 1_325_000
    delivery_cost = 53_150_000
    sold_ml = 500_000
    avg = Decimal(opening_value + delivery_cost) / Decimal(7_000_000)
    cogs = int(
        (Decimal(sold_ml) * avg).to_integral_value(rounding=ROUND_HALF_UP)
    )
    assert cogs == 3_891_071
    revenue = 12_500_000
    gross = revenue - cogs
    net = gross - 200_000

    pnl = svc.accounting.daily_pnl(DAY)
    assert pnl.source == "closing_snapshot"
    assert pnl.estimated is False
    assert pnl.estimate_reasons == []
    assert pnl.total_revenue_paisa == revenue
    assert pnl.total_cogs_paisa == cogs
    assert pnl.gross_profit_paisa == gross
    assert pnl.operating_expenses_paisa == 200_000
    assert pnl.net_profit_paisa == net
    assert pnl.receivables_created_paisa == 0
    (petrol,) = [
        line for line in pnl.lines if line.fuel_product_code == "PETROL"
    ]
    assert petrol.revenue_paisa == revenue
    assert petrol.cogs_paisa == cogs
    assert petrol.gross_profit_paisa == gross

    # The frozen cost snapshot in the closing record carries the same math.
    summary = json.loads(
        db.execute(
            "SELECT summary_json FROM business_day_closings"
            " WHERE business_date = ? ORDER BY closing_revision DESC LIMIT 1",
            (DAY,),
        ).fetchone()["summary_json"]
    )
    snapshots = summary["cost_snapshots"]
    products = (
        snapshots["products"] if isinstance(snapshots, dict) else snapshots
    )
    snap = next(
        p for p in products if p["fuel_product_code"] == "PETROL"
    )
    assert snap["opening_qty_ml"] == 5_000_000
    assert snap["opening_value_paisa"] == opening_value
    assert snap["delivery_qty_ml"] == 2_000_000
    assert snap["delivery_cost_paisa"] == delivery_cost
    assert snap["avg_cost_paisa_per_ml"] == "7.782143"
    assert snap["cogs_paisa"] == cogs
    assert snap["closing_qty_ml"] == 6_500_000
    closing_value = int(
        (Decimal(6_500_000) * avg).to_integral_value(rounding=ROUND_HALF_UP)
    )
    assert snap["closing_value_paisa"] == closing_value
    assert snap["estimated"] is False


# ---------------------------------------------------------------------------
# snapshot immutability
# ---------------------------------------------------------------------------


def test_pnl_snapshot_frozen_at_close(phase5_services, db):
    """Yesterday's P&L never changes: a later price change and a later
    delivery leave the closed day's snapshot (source='closing_snapshot')
    byte-identical."""
    svc = phase5_services
    _close_meter_day(
        svc, DAY, sale_litres="100", physical_cash_paisa=4_500_000,
    )
    before = svc.accounting.daily_pnl(DAY)
    assert before.source == "closing_snapshot"
    frozen = before.to_dict()

    day2 = "2026-10-16"
    seed_phase3_day(svc, day2)
    insert_price_row(db, "PETROL", pkt_midnight_utc_iso(day2), 3_000_000, day2)
    svc.deliveries.record_delivery(
        supplier="PSO Depot",
        fuel_product="PETROL",
        tank="TANK-PETROL",
        business_date=day2,
        quantity_litres=Decimal("1000"),
        unit_cost_pkr=Decimal("265"),
        occurred_at_utc=pkt_clock_utc_iso(day2, 9),
    )

    after = svc.accounting.daily_pnl(DAY)
    assert after.source == "closing_snapshot"
    assert after.to_dict() == frozen


# ---------------------------------------------------------------------------
# estimate labeling
# ---------------------------------------------------------------------------


def test_pnl_estimate_labeling(phase5_services):
    """No opening value and no deliveries → the P&L is flagged estimated
    with reasons, but it is still returned — never silently zeroed."""
    svc = phase5_services
    # Opening stock WITHOUT a carrying value: quantity known, cost unknown.
    _close_meter_day(
        svc, DAY, sale_litres="100", physical_cash_paisa=4_500_000,
        opening_stock_litres=Decimal("500"),
    )
    pnl = svc.accounting.daily_pnl(DAY)
    assert pnl.source == "closing_snapshot"
    assert pnl.estimated is True
    assert pnl.estimate_reasons, "an estimate must never be unlabeled"
    assert any(
        "opening stock value unknown" in reason
        for reason in pnl.estimate_reasons
    )
    # …and the revenue is still reported, never silently zeroed.
    assert pnl.total_revenue_paisa == 2_500_000


# ---------------------------------------------------------------------------
# other materials
# ---------------------------------------------------------------------------


def test_other_materials_basic(phase5_services):
    """Purchase 10 @ 500 → sell 2 @ 800 → 8 on hand; cash rows post once;
    overselling is refused and stock never goes negative."""
    svc = phase5_services
    svc.days.open_day(DAY, opening_cash_paisa=2_000_000)
    product_id = svc.materials.add_product("Engine Oil 1L", unit="bottle")

    purchase = svc.materials.record_purchase(
        product=product_id,
        business_date=DAY,
        quantity_units=10,
        unit_price_paisa=500,
        method="cash",
        occurred_at_utc=pkt_clock_utc_iso(DAY, 9),
    )
    assert purchase.total_paisa == 5_000
    assert purchase.txn_type == "purchase"
    assert svc.materials.stock(product_id)[0].on_hand_units == 10

    sale = svc.materials.record_sale(
        product=product_id,
        business_date=DAY,
        quantity_units=2,
        unit_price_paisa=800,
        method="cash",
        occurred_at_utc=pkt_clock_utc_iso(DAY, 10),
    )
    assert sale.total_paisa == 1_600
    assert sale.txn_type == "sale"
    assert svc.materials.stock(product_id)[0].on_hand_units == 8

    # Cash rows posted exactly once each.
    receipts = [
        r for r in svc.cash.list_receipts(DAY) if r.category == "other_sales"
    ]
    payments = [
        p
        for p in svc.cash.list_payments(DAY)
        if p.category == "other_materials_purchase"
    ]
    assert [r.amount_paisa for r in receipts] == [1_600]
    assert [p.amount_paisa for p in payments] == [5_000]

    txns = {
        t.txn_type: t
        for t in svc.materials.list_transactions(product_id=product_id)
    }
    assert set(txns) == {"purchase", "sale"}

    # Stock can never go negative.
    with pytest.raises(errors.BusinessRuleError, match="only 8 on hand"):
        svc.materials.record_sale(
            product=product_id,
            business_date=DAY,
            quantity_units=9,
            unit_price_paisa=800,
            method="cash",
            occurred_at_utc=pkt_clock_utc_iso(DAY, 11),
        )
    assert svc.materials.stock(product_id)[0].on_hand_units == 8


# ---------------------------------------------------------------------------
# period P&L
# ---------------------------------------------------------------------------


def test_period_pnl_sums_days(phase5_services):
    """Two closed days contribute their frozen snapshots; the open day is
    computed live and flagged estimated (lifetime-average other-materials
    COGS) — period totals equal Σ of the daily reports."""
    svc = phase5_services
    _first_valued_day(svc, PREV, qty_litres="10000", value_pkr="26500")
    for code, litres in PREV_READINGS.items():
        svc.nozzles.record_reading(PREV, code, litres)
    svc.sales.post_day_sales(PREV)
    svc.days.close_day(PREV, physical_cash_paisa=2_000_000)

    day1, day2, day3 = "2026-10-15", "2026-10-16", "2026-10-17"
    prev_map = dict(PREV_READINGS)

    # Day 1: 100 L sale + 1,000 expense.
    seed_phase3_day(svc, day1)
    _post_sale(svc, day1, "1 PG+", "100", prev_map)
    prev_map["1 PG+"] = str(Decimal(prev_map["1 PG+"]) + 100)
    product_id = svc.materials.add_product("Wiper Blade", unit="piece")
    svc.materials.record_purchase(
        product=product_id, business_date=day1, quantity_units=10,
        unit_price_paisa=500, method="cash",
        occurred_at_utc=pkt_clock_utc_iso(day1, 9),
    )
    svc.expenses.record_expense(
        business_date=day1, category="Salaries", amount_paisa=100_000,
        method="cash", occurred_at_utc=pkt_clock_utc_iso(day1, 12),
    )
    svc.cash.record_settlements(
        day1, cash_by_product={"PETROL": 2_500_000},
        occurred_at_utc=pkt_clock_utc_iso(day1, 18),
    )
    svc.days.close_day(day1, physical_cash_paisa=4_395_000)

    # Day 2: 50 L sale, no expense.
    seed_phase3_day(svc, day2)
    _post_sale(svc, day2, "1 PG+", "50", prev_map)
    svc.cash.record_settlements(
        day2, cash_by_product={"PETROL": 1_250_000},
        occurred_at_utc=pkt_clock_utc_iso(day2, 18),
    )
    svc.days.close_day(day2, physical_cash_paisa=3_250_000)

    # Day 3: open; an other-materials sale with no purchase that day forces
    # the lifetime-average (estimated) COGS path.
    seed_phase3_day(svc, day3)
    svc.materials.record_sale(
        product=product_id, business_date=day3, quantity_units=2,
        unit_price_paisa=800, method="cash",
        occurred_at_utc=pkt_clock_utc_iso(day3, 10),
    )

    d1 = svc.accounting.daily_pnl(day1)
    d2 = svc.accounting.daily_pnl(day2)
    d3 = svc.accounting.daily_pnl(day3)
    assert d1.source == "closing_snapshot"
    assert d2.source == "closing_snapshot"
    assert d3.estimated is True
    assert any("lifetime average" in r for r in d3.estimate_reasons)

    period = svc.accounting.period_pnl(day1, day3)
    assert period.start_date == day1
    assert period.end_date == day3
    assert period.estimated is True
    assert period.total_revenue_paisa == (
        d1.total_revenue_paisa + d2.total_revenue_paisa
        + d3.total_revenue_paisa
    )
    assert period.total_cogs_paisa == (
        d1.total_cogs_paisa + d2.total_cogs_paisa + d3.total_cogs_paisa
    )
    assert period.gross_profit_paisa == (
        d1.gross_profit_paisa + d2.gross_profit_paisa
        + d3.gross_profit_paisa
    )
    assert period.operating_expenses_paisa == (
        d1.operating_expenses_paisa + d2.operating_expenses_paisa
        + d3.operating_expenses_paisa
    )
    assert period.net_profit_paisa == (
        d1.net_profit_paisa + d2.net_profit_paisa + d3.net_profit_paisa
    )
    assert period.receivables_created_paisa == (
        d1.receivables_created_paisa + d2.receivables_created_paisa
        + d3.receivables_created_paisa
    )
    # And the exact expected figures: 0.265 paisa/ml average on both days.
    assert d1.total_cogs_paisa == 26_500  # 100,000 ml × 0.265
    assert d2.total_cogs_paisa == 13_250  # 50,000 ml × 0.265
    assert period.total_revenue_paisa == 3_751_600  # 3,750,000 fuel + 1,600 other
    assert period.total_cogs_paisa == 40_750  # 39,750 fuel + 1,000 other
    assert period.gross_profit_paisa == 3_710_850
    assert period.operating_expenses_paisa == 100_000
    assert period.net_profit_paisa == 3_610_850
    assert period.receivables_created_paisa == 0
