"""Phase-3 price-service tests (acceptance Test 3 + §5.2 precision).

Covers PriceService against the task contract::

    set_price(product, price_pkr, effective_from_utc, business_date, *, actor=None) -> int
    get_price(product, at_utc)
    list_prices(product)
    list_fuel_products()

Deviations from the task text (all service-side; reported to the parent):
  D1. ``set_price`` demands an intraday ``segment`` snapshot for every active
      nozzle of the product whenever ANY prior price exists — even for a
      day-open price effective at 00:00 PKT, which the task's setup pattern
      sets without snapshots. Workaround: multi-day tests rely on price
      carry-forward for unchanged day-open prices (``seed_phase3_day``), and
      test 1 inserts its day-1 price row via ``insert_price_row``.
  D2. The interval ending exactly at a price-change instant is priced with
      the NEW price (``effective_from_utc <= interval_end``); the task pins
      the OLD price for that boundary interval ("the change takes effect
      only AFTER the snapshot"). ``test_midday_segment_pricing`` is written
      per the task and currently FAILS on the boundary interval — kept exact
      so the deviation stays visible instead of being tested around.

Design plan: 00_design_plan.md §5.2, §11 assumption 2, acceptance test 3.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from app.calculations.money import amount_paisa_for
from app.services import errors

from .conftest import (
    close_prior_day,
    dto_field,
    fuel_product_id,
    insert_price_row,
    phase3_services,  # noqa: F401  (fixture)
    pkt_clock_utc_iso,
    pkt_midnight_utc_iso,
    record_reset_interval,
    seed_first_day,
    seed_phase3_day,
)

DAY0 = "2026-10-13"
DAY1 = "2026-10-14"
DAY2 = "2026-10-15"

P1 = Decimal("265.9630")
P2 = Decimal("270.0000")
P1_10K = 2_659_630
P2_10K = 2_700_000

PREV_READINGS = {
    "1 PG+": "84938",
    "2 PG+": "81234",
    "3 HSD": "90123",
    "4 HSD": "88456",
}


def _sales_revenue_by_product(day_sales) -> dict[str, int]:
    """{fuel product code: revenue_paisa} from the ``DaySales`` DTO."""
    if isinstance(day_sales, dict):
        return {str(k): int(v) for k, v in day_sales.items()}
    return {
        str(k): int(v)
        for k, v in dto_field(day_sales, "revenue_by_product_paisa").items()
    }


def _price_10k_of(price_row) -> int:
    """unit_price_10k whether get_price returns a row or the bare int."""
    if isinstance(price_row, int) and not isinstance(price_row, bool):
        return price_row
    return int(dto_field(price_row, "unit_price_10k"))


# 1. price change keeps old values (acceptance Test 3) -------------------------
def test_price_change_keeps_old_values(phase3_services, db, db2):
    """A later price change never rewrites history: day-1 intervals keep the
    old unit price, day-1 revenue is unchanged, the old price_history row is
    byte-identical, and direct SQL edits of price_history are rejected."""
    svc = phase3_services
    close_prior_day(
        svc, DAY0, PREV_READINGS, opening_stock_litres=Decimal("5000")
    )

    # Day 1: price in force from the day's 00:00 PKT (D1: inserted directly —
    # the service would wrongly demand intraday snapshots for it).
    seed_phase3_day(svc, DAY1)
    insert_price_row(db, "PETROL", pkt_midnight_utc_iso(DAY1), P1_10K, DAY1)
    svc.nozzles.record_reading(DAY1, "1 PG+", "84985")  # 47 L sale
    # The other nozzles must not post 0 L sales (service limitation — a
    # 0-quantity sale movement violates the ledger CHECK), so their
    # intervals are made unknown via documented meter resets instead.
    record_reset_interval(svc, DAY1, "2 PG+", PREV_READINGS["2 PG+"])
    record_reset_interval(svc, DAY1, "3 HSD", PREV_READINGS["3 HSD"])
    record_reset_interval(svc, DAY1, "4 HSD", PREV_READINGS["4 HSD"])
    posted = svc.sales.post_day_sales(DAY1)
    assert posted["posted"] >= 1
    day1_revenue_before = _sales_revenue_by_product(svc.sales.day_sales(DAY1))["PETROL"]
    assert day1_revenue_before == 1_250_026  # 47 L @ 265.9630 (§5.2 check)
    svc.days.close_day(DAY1, physical_cash_paisa=2_000_000)

    # Day 2: mid-day price change behind segment snapshots, then more sales.
    seed_phase3_day(svc, DAY2)
    seg_utc = pkt_clock_utc_iso(DAY2, 14)  # 14:00 PKT
    svc.nozzles.record_segment_reading(DAY2, "1 PG+", "85010", occurred_at_utc=seg_utc)
    svc.nozzles.record_segment_reading(DAY2, "2 PG+", "150", occurred_at_utc=seg_utc)
    svc.prices.set_price("PETROL", P2, seg_utc, DAY2)
    svc.nozzles.record_reading(DAY2, "1 PG+", "85057")
    svc.nozzles.record_reading(DAY2, "2 PG+", "200")
    svc.sales.post_day_sales(DAY2)

    # Day-1 priced intervals still carry the old unit price and amount.
    day1_intervals = [
        i
        for i in svc.sales.day_intervals(DAY1)
        if dto_field(i, "nozzle_code") == "1 PG+"
    ]
    assert len(day1_intervals) == 1
    assert dto_field(day1_intervals[0], "quantity_ml") == 47_000
    assert dto_field(day1_intervals[0], "unit_price_10k") == P1_10K
    assert dto_field(day1_intervals[0], "amount_paisa") == 1_250_026
    # Day-1 revenue is unchanged by the day-2 price change.
    assert _sales_revenue_by_product(svc.sales.day_sales(DAY1))["PETROL"] == (
        day1_revenue_before
    ) == 1_250_026

    # The old price_history row is untouched; both rows are listed.
    prices = svc.prices.list_prices("PETROL")
    by_effective = {dto_field(p, "effective_from_utc"): p for p in prices}
    assert pkt_midnight_utc_iso(DAY1) in by_effective
    assert seg_utc in by_effective
    assert dto_field(by_effective[pkt_midnight_utc_iso(DAY1)], "unit_price_10k") == P1_10K
    assert dto_field(by_effective[seg_utc], "unit_price_10k") == P2_10K

    # Immutability at the database level (Group C1): raw SQL UPDATE/DELETE
    # of price_history is rejected from ANY connection.
    prod_id = fuel_product_id(db, "PETROL")
    where = (prod_id, pkt_midnight_utc_iso(DAY1))
    with pytest.raises(sqlite3.IntegrityError):
        db2.execute(
            "UPDATE price_history SET unit_price_10k = ?"
            " WHERE fuel_product_id = ? AND effective_from_utc = ?",
            (P2_10K, *where),
        )
    db2.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        db2.execute(
            "DELETE FROM price_history WHERE fuel_product_id = ?"
            " AND effective_from_utc = ?",
            where,
        )
    db2.rollback()
    row = db.execute(
        "SELECT unit_price_10k FROM price_history"
        " WHERE fuel_product_id = ? AND effective_from_utc = ?",
        where,
    ).fetchone()
    assert row["unit_price_10k"] == P1_10K


# 2. mid-day segment pricing ----------------------------------------------------
def test_midday_segment_pricing(phase3_services):
    """One day, two prices: the interval before the 14:00 PKT snapshot is
    priced at the old price, the interval after at the new price — with
    exact paisa amounts (round-half-up once, §5.2).

    Currently FAILS on the boundary interval (deviation D2 above): the
    service prices [prev → snapshot@14:00] at the new price instead of the
    old one. Kept exact per the task so the deviation stays visible.
    """
    svc = phase3_services
    close_prior_day(
        svc,
        DAY1,
        {"1 PG+": "84985", **{k: v for k, v in PREV_READINGS.items() if k != "1 PG+"}},
        opening_stock_litres=Decimal("5000"),
    )

    seed_phase3_day(svc, DAY2)
    # No day-open price is set on DAY2: DAY1's price carries forward and
    # stays effective until the 14:00 change below.
    seg_utc = pkt_clock_utc_iso(DAY2, 14)
    # prev 84985 -> segment 85010 = 25 L ; segment 85010 -> close 85057 = 47 L
    svc.nozzles.record_segment_reading(DAY2, "1 PG+", "85010", occurred_at_utc=seg_utc)
    svc.nozzles.record_segment_reading(DAY2, "2 PG+", "81234", occurred_at_utc=seg_utc)
    svc.prices.set_price("PETROL", P2, seg_utc, DAY2)
    svc.nozzles.record_reading(DAY2, "1 PG+", "85057")

    intervals = [
        i
        for i in svc.sales.day_intervals(DAY2)
        if dto_field(i, "nozzle_code") == "1 PG+"
    ]
    assert len(intervals) == 2
    priced = {
        (dto_field(i, "quantity_ml"), dto_field(i, "unit_price_10k")): dto_field(
            i, "amount_paisa"
        )
        for i in intervals
    }
    # 25000*2659630/1e5 = 664907.5 -> 664908 ; 47000*2700000/1e5 = 1269000
    assert priced == {(25_000, P1_10K): 664_908, (47_000, P2_10K): 1_269_000}

    revenue = _sales_revenue_by_product(svc.sales.day_sales(DAY2))["PETROL"]
    assert revenue == 664_908 + 1_269_000 == 1_933_908

    # Pure-math pins for the same figures (no services involved).
    assert amount_paisa_for(25_000, P1_10K) == 664_908
    assert amount_paisa_for(47_000, P2_10K) == 1_269_000


# 3. price change blocked without a snapshot ------------------------------------
def test_price_change_blocked_without_snapshot(phase3_services, db):
    """With a prior price in force and no segment snapshot, the price change
    is refused with a ValidationError naming the nozzle (§11.2)."""
    svc = phase3_services
    day = "2026-10-15"
    seed_first_day(svc, day)  # first day: the opening price needs no snapshot
    svc.nozzles.record_reading(day, "1 PG+", "84990")  # sales activity…

    before = db.execute(
        "SELECT COUNT(*) FROM price_history WHERE fuel_product_id = ?",
        (fuel_product_id(db, "PETROL"),),
    ).fetchone()[0]

    with pytest.raises(errors.ValidationError) as excinfo:
        svc.prices.set_price("PETROL", P2, pkt_clock_utc_iso(day, 14), day)
    message = str(excinfo.value)
    assert "1 PG+" in message or "2 PG+" in message, (
        f"the error must name the nozzle missing its snapshot, got: {message!r}"
    )

    # …but no snapshot was recorded, so nothing was written.
    after = db.execute(
        "SELECT COUNT(*) FROM price_history WHERE fuel_product_id = ?",
        (fuel_product_id(db, "PETROL"),),
    ).fetchone()[0]
    assert after == before


# 16. money / price precision (§5.2) --------------------------------------------
def test_money_price_precision():
    """Canonical §5.2 check: 47 L @ PKR 265.9630 -> 1,250,026 paisa."""
    assert amount_paisa_for(47_000, 2_659_630) == 1_250_026
    from app.calculations.money import from_price_10k, to_price_10k

    assert to_price_10k(Decimal("265.9630")) == 2_659_630
    assert to_price_10k("265.9630") == 2_659_630
    assert from_price_10k(2_659_630) == Decimal("265.9630")
    assert amount_paisa_for(47_000, to_price_10k(Decimal("265.9630"))) == 1_250_026


def test_list_fuel_products_lists_seed_products(phase3_services):
    """Sanity: the service lists the seeded fuel products (codes, not ids)."""
    products = phase3_services.prices.list_fuel_products()
    codes = {dto_field(p, "code") for p in products}
    assert {"PETROL", "DIESEL"} <= codes


def test_get_price_returns_effective_row(phase3_services):
    """get_price resolves the price in force at a given UTC instant."""
    svc = phase3_services
    day = "2026-10-15"
    seed_first_day(svc, day)
    seg_utc = pkt_clock_utc_iso(day, 14)
    svc.nozzles.record_segment_reading(day, "1 PG+", "85010", occurred_at_utc=seg_utc)
    svc.nozzles.record_segment_reading(day, "2 PG+", "81234", occurred_at_utc=seg_utc)
    svc.prices.set_price("PETROL", P2, seg_utc, day)

    morning = svc.prices.get_price("PETROL", pkt_clock_utc_iso(day, 9))
    evening = svc.prices.get_price("PETROL", pkt_clock_utc_iso(day, 15))
    assert _price_10k_of(morning) == P1_10K
    assert _price_10k_of(evening) == P2_10K
