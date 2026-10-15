"""Phase-3 delivery + inventory tests (acceptance Tests 4, 6 and stock guards).

Covers DeliveryService / InventoryService against the task contract::

    DeliveryService.add_supplier(name, phone=None, address=None, *, actor=None) -> int
    DeliveryService.record_delivery(*, supplier, fuel_product, tank, business_date,
        quantity_litres, unit_cost_pkr, supplier_invoice_no=None,
        other_cost_pkr=Decimal("0"), amount_paid_pkr=Decimal("0"),
        occurred_at_utc=None, delivery_uuid=None, actor=None) -> int
    DeliveryService.list_deliveries(business_date=None)
    DeliveryService.supplier_balance(supplier_id)
    InventoryService.set_opening_stock(fuel_product, quantity_litres, business_date, *, actor=None)
    InventoryService.stock_positions(business_date)
    InventoryService.record_physical_count(tank, business_date, quantity_litres, *, measured_by=None, note=None, actor=None)
    InventoryService.correct_physical_count(tank, business_date, quantity_litres, *, reason, actor=None)
    InventoryService.record_adjustment(*, fuel_product, tank, business_date,
        quantity_litres_signed, reason, authorized_by=None, actor=None)

Notes:
  * Opening stock is entered on the FIRST day (``close_prior_day``); later
    days carry it forward automatically — ``set_opening_stock`` refuses a
    second row for the same (date, product).
  * Tests that post sales record readings only for the moving nozzle(s):
    a 0 L interval cannot be posted (a 0-quantity sale movement violates the
    ledger CHECK), so static nozzles are left unread on days that are not
    closed.
  * ``stock_positions`` rows carry ``tank_code`` / ``theoretical_ml`` /
    ``physical_ml`` / ``variance_ml``; ``supplier_balance`` returns a dict
    with ``outstanding_paisa``.

Design plan: 00_design_plan.md §8, §11 assumptions 3 and 11, acceptance
tests 4 and 13.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from app.services import errors

from .conftest import (
    close_prior_day,
    dto_field,
    fuel_product_id,
    new_uuid,
    phase3_services,  # noqa: F401  (fixture)
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

#: 2000 L @ PKR 250.0000/L -> exactly 50,000,000 paisa (no rounding residue).
QTY_L = Decimal("2000")
UNIT_COST = Decimal("250.0000")
TOTAL_PAISA = 50_000_000


def _delivery_kwargs(supplier, **overrides):
    kwargs = dict(
        supplier=supplier,
        fuel_product="PETROL",
        tank="TANK-PETROL",
        business_date=DAY,
        quantity_litres=QTY_L,
        unit_cost_pkr=UNIT_COST,
    )
    kwargs.update(overrides)
    return kwargs


def _position_for(positions, tank_code: str):
    for pos in positions:
        if dto_field(pos, "tank_code", "code") == tank_code:
            return pos
    raise AssertionError(
        f"no stock position for tank {tank_code!r} in {positions!r}"
    )


def _db_theoretical_ml(db, business_date: str, tank_code: str) -> int:
    """Ground-truth theoretical stock: opening_balances(stock) + movements.

    Mirrors the F2 trigger's tracked-balance definition, independent of the
    service's ``stock_positions`` presentation.
    """
    tank = db.execute(
        "SELECT id, fuel_product_id FROM tanks WHERE code = ?", (tank_code,)
    ).fetchone()
    opening = db.execute(
        "SELECT COALESCE(SUM(quantity_ml), 0) FROM opening_balances"
        " WHERE business_date = ? AND balance_type = 'stock'"
        " AND fuel_product_id = ?",
        (business_date, tank["fuel_product_id"]),
    ).fetchone()[0]
    moves = db.execute(
        "SELECT COALESCE(SUM(quantity_ml), 0) FROM inventory_movements"
        " WHERE business_date = ? AND tank_id = ?",
        (business_date, tank["id"]),
    ).fetchone()[0]
    return int(opening) + int(moves)


def _movement_count(db, business_date: str, movement_type: str | None = None) -> int:
    sql = "SELECT COUNT(*) FROM inventory_movements WHERE business_date = ?"
    params: list = [business_date]
    if movement_type is not None:
        sql += " AND movement_type = ?"
        params.append(movement_type)
    return int(db.execute(sql, params).fetchone()[0])


def _supplier_balance_paisa(svc, supplier_id) -> int:
    value = svc.deliveries.supplier_balance(supplier_id)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, dict) and "outstanding_paisa" in value:
        return int(value["outstanding_paisa"])
    return int(dto_field(value, "outstanding_paisa", "balance_paisa"))


# 4. automatic stock, no double deduction (acceptance Test 4) -----------------
def test_automatic_stock_no_double_deduction(phase3_services, db):
    """5000 + 2000 − 500 = 6500 L theoretical; posting twice is a no-op, never
    a second deduction (source_uuid uniqueness, F1)."""
    svc = phase3_services
    close_prior_day(
        svc, PREV, PREV_READINGS, opening_stock_litres=Decimal("5000")
    )
    supplier = seed_phase3_day(svc, DAY)
    svc.deliveries.record_delivery(
        **_delivery_kwargs(supplier, supplier_invoice_no="INV-004-1",
                           delivery_uuid=new_uuid())
    )
    svc.nozzles.record_reading(DAY, "1 PG+", "85438")  # 500 L sale

    first = svc.sales.post_day_sales(DAY)
    assert first["posted"] >= 1
    assert _movement_count(db, DAY, "sale") == first["posted"]
    position = _position_for(svc.inventory.stock_positions(DAY), "TANK-PETROL")
    assert dto_field(position, "theoretical_ml") == 6_500_000
    assert _db_theoretical_ml(db, DAY, "TANK-PETROL") == 6_500_000
    movements_before = _movement_count(db, DAY)

    second = svc.sales.post_day_sales(DAY)
    assert second["posted"] == 0
    assert second["already_posted"] > 0
    assert _movement_count(db, DAY) == movements_before
    position = _position_for(svc.inventory.stock_positions(DAY), "TANK-PETROL")
    assert dto_field(position, "theoretical_ml") == 6_500_000
    assert _db_theoretical_ml(db, DAY, "TANK-PETROL") == 6_500_000


# 6. negative stock blocked (acceptance Test 13) --------------------------------
def test_negative_stock_blocked(phase3_services, db, db2):
    """A 500 L sale against 100 L of tracked stock is refused atomically
    (service + F2 trigger, §11.3); an authorized adjustment unblocks it."""
    svc = phase3_services
    # No opening stock: the carried opening balance is 0 L.
    close_prior_day(svc, PREV, PREV_READINGS)
    supplier = seed_phase3_day(svc, DAY)
    svc.deliveries.record_delivery(
        **_delivery_kwargs(
            supplier,
            quantity_litres=Decimal("100"),
            supplier_invoice_no="INV-006-1",
            delivery_uuid=new_uuid(),
        )
    )
    svc.nozzles.record_reading(DAY, "1 PG+", "85438")  # 500 L sale > 100 L stock

    with pytest.raises(errors.BusinessRuleError):
        svc.sales.post_day_sales(DAY)
    # Atomic: zero sale movements committed; the delivery movement stands.
    assert _movement_count(db, DAY, "sale") == 0
    assert _movement_count(db, DAY, "delivery") == 1

    # Raw SQL: a sale bigger than the tracked balance is rejected (F2).
    prod = fuel_product_id(db, "PETROL")
    tank = tank_id(db, "TANK-PETROL")
    with pytest.raises(sqlite3.IntegrityError):
        db2.execute(
            "INSERT INTO inventory_movements (uuid, business_date,"
            " fuel_product_id, tank_id, movement_type, quantity_ml,"
            " source_table, source_uuid)"
            " VALUES (?, ?, ?, ?, 'sale', ?, 'test', ?)",
            (new_uuid(), DAY, prod, tank, -200_000_000, new_uuid()),
        )
    db2.rollback()

    # An authorized +500 L adjustment names the cause; the sale then posts.
    svc.inventory.record_adjustment(
        fuel_product="PETROL",
        tank="TANK-PETROL",
        business_date=DAY,
        quantity_litres_signed=Decimal("500"),
        reason="missed delivery top-up entered after dip check",
        authorized_by="manager",
    )
    sale_source = new_uuid()
    db2.execute(
        "INSERT INTO inventory_movements (uuid, business_date,"
        " fuel_product_id, tank_id, movement_type, quantity_ml,"
        " source_table, source_uuid)"
        " VALUES (?, ?, ?, ?, 'sale', ?, 'test', ?)",
        (new_uuid(), DAY, prod, tank, -500_000, sale_source),
    )
    db2.commit()
    row = db.execute(
        "SELECT quantity_ml FROM inventory_movements WHERE source_uuid = ?",
        (sale_source,),
    ).fetchone()
    assert row["quantity_ml"] == -500_000
    assert _db_theoretical_ml(db, DAY, "TANK-PETROL") == 100_000


# 7. delivery idempotency ---------------------------------------------------------
def test_delivery_idempotency(phase3_services, db):
    """Retrying record_delivery with the same delivery_uuid returns the same
    id — exactly one fuel_deliveries row and one delivery movement."""
    svc = phase3_services
    supplier = seed_first_day(svc, DAY)
    kwargs = _delivery_kwargs(
        supplier,
        supplier_invoice_no="INV-007-1",
        delivery_uuid="test-uuid-1",
    )
    first = svc.deliveries.record_delivery(**kwargs)
    second = svc.deliveries.record_delivery(**kwargs)
    assert first == second
    assert db.execute("SELECT COUNT(*) FROM fuel_deliveries").fetchone()[0] == 1
    assert _movement_count(db, DAY, "delivery") == 1


# 8. duplicate invoice rejected ---------------------------------------------------
def test_duplicate_invoice_rejected(phase3_services, db):
    """Same supplier + same invoice number -> DuplicateError (H4); a
    different supplier may reuse the number; NULL invoices never collide."""
    svc = phase3_services
    supplier_a = seed_first_day(svc, DAY, supplier_name="PSO Depot")
    supplier_b = svc.deliveries.add_supplier("Shell Depot")

    svc.deliveries.record_delivery(
        **_delivery_kwargs(supplier_a, supplier_invoice_no="INV-008-A",
                           delivery_uuid=new_uuid())
    )
    with pytest.raises(errors.DuplicateError):
        svc.deliveries.record_delivery(
            **_delivery_kwargs(supplier_a, supplier_invoice_no="INV-008-A",
                               delivery_uuid=new_uuid())
        )
    # Different supplier, same invoice number: allowed.
    svc.deliveries.record_delivery(
        **_delivery_kwargs(supplier_b, supplier_invoice_no="INV-008-A",
                           delivery_uuid=new_uuid())
    )
    # Same supplier, NULL invoice twice: allowed (NULLs are distinct), both posted.
    svc.deliveries.record_delivery(
        **_delivery_kwargs(supplier_a, supplier_invoice_no=None,
                           delivery_uuid=new_uuid())
    )
    svc.deliveries.record_delivery(
        **_delivery_kwargs(supplier_a, supplier_invoice_no=None,
                           delivery_uuid=new_uuid())
    )
    assert db.execute("SELECT COUNT(*) FROM fuel_deliveries").fetchone()[0] == 4
    assert _movement_count(db, DAY, "delivery") == 4


# 9. unpaid / partial / paid invoices ----------------------------------------------
def test_unpaid_invoice_no_cash_touch(phase3_services, db):
    """An unpaid delivery touches stock only: one delivery movement, no
    payment rows anywhere. Partial/full payments set the status and the
    supplier's outstanding balance exactly."""
    svc = phase3_services
    seed_first_day(svc, DAY)
    sup_unpaid = svc.deliveries.add_supplier("Unpaid Supplier")
    sup_partial = svc.deliveries.add_supplier("Partial Supplier")
    sup_paid = svc.deliveries.add_supplier("Paid Supplier")

    d_unpaid = svc.deliveries.record_delivery(
        **_delivery_kwargs(sup_unpaid, supplier_invoice_no="INV-009-1",
                           amount_paid_pkr=Decimal("0"),
                           delivery_uuid=new_uuid())
    )
    row = db.execute(
        "SELECT payment_status, total_cost_paisa, amount_paid_paisa"
        " FROM fuel_deliveries WHERE id = ?",
        (d_unpaid,),
    ).fetchone()
    assert row["payment_status"] == "unpaid"
    assert row["total_cost_paisa"] == TOTAL_PAISA
    assert row["amount_paid_paisa"] == 0
    # Exactly one movement (the delivery itself) — no payment rows anywhere.
    moves = db.execute(
        "SELECT movement_type FROM inventory_movements ORDER BY id"
    ).fetchall()
    assert [m["movement_type"] for m in moves] == ["delivery"]
    assert _supplier_balance_paisa(svc, sup_unpaid) == TOTAL_PAISA

    d_partial = svc.deliveries.record_delivery(
        **_delivery_kwargs(sup_partial, supplier_invoice_no="INV-009-2",
                           amount_paid_pkr=Decimal("250000.00"),
                           delivery_uuid=new_uuid())
    )
    row = db.execute(
        "SELECT payment_status, amount_paid_paisa FROM fuel_deliveries WHERE id = ?",
        (d_partial,),
    ).fetchone()
    assert row["payment_status"] == "partial"
    assert row["amount_paid_paisa"] == 25_000_000
    assert _supplier_balance_paisa(svc, sup_partial) == TOTAL_PAISA - 25_000_000

    d_paid = svc.deliveries.record_delivery(
        **_delivery_kwargs(sup_paid, supplier_invoice_no="INV-009-3",
                           amount_paid_pkr=Decimal("500000.00"),
                           delivery_uuid=new_uuid())
    )
    row = db.execute(
        "SELECT payment_status FROM fuel_deliveries WHERE id = ?", (d_paid,)
    ).fetchone()
    assert row["payment_status"] == "paid"
    assert _supplier_balance_paisa(svc, sup_paid) == 0


# 10. delivery total / paid-cap guards (raw SQL) ------------------------------------
def test_duplicate_delivery_total_guard(phase3_services, db, db2):
    """The DB itself rejects a delivery whose total does not equal
    quantity × unit cost (I), and any amount_paid above total + other (I)."""
    svc = phase3_services
    supplier = seed_first_day(svc, DAY)
    prod = fuel_product_id(db, "PETROL")
    tank = tank_id(db, "TANK-PETROL")

    def _attempt(total_paisa, paid_paisa, invoice):
        db2.execute(
            "INSERT INTO fuel_deliveries (uuid, supplier_id, fuel_product_id,"
            " tank_id, business_date, quantity_ml, unit_cost_10k,"
            " total_cost_paisa, supplier_invoice_no, amount_paid_paisa)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_uuid(), supplier, prod, tank, DAY,
                2_000_000, 2_500_000, total_paisa, invoice, paid_paisa,
            ),
        )

    # Correct total for 2000 L @ 250.0000 is 50,000,000; a wrong total aborts.
    with pytest.raises(sqlite3.IntegrityError):
        _attempt(49_999_999, 0, "INV-010-1")
    db2.rollback()
    # amount_paid above total + other aborts.
    with pytest.raises(sqlite3.IntegrityError):
        _attempt(TOTAL_PAISA, TOTAL_PAISA + 1, "INV-010-2")
    db2.rollback()
    assert db.execute("SELECT COUNT(*) FROM fuel_deliveries").fetchone()[0] == 0


# 11. physical variance workflow ------------------------------------------------------
def test_physical_variance_workflow(phase3_services, db):
    """Dip count -> variance -> authorized adjustment -> theoretical and
    variance reconcile; every step is audited; the reading is correctable."""
    svc = phase3_services
    close_prior_day(
        svc, PREV, PREV_READINGS, opening_stock_litres=Decimal("5000")
    )
    supplier = seed_phase3_day(svc, DAY)
    svc.deliveries.record_delivery(
        **_delivery_kwargs(supplier, supplier_invoice_no="INV-011-1",
                           delivery_uuid=new_uuid())
    )
    svc.nozzles.record_reading(DAY, "1 PG+", "85438")  # 500 L sale
    assert svc.sales.post_day_sales(DAY)["posted"] >= 1
    assert _db_theoretical_ml(db, DAY, "TANK-PETROL") == 6_500_000

    # Physical dip: 6480 L against 6500 L theoretical -> -20 L variance.
    svc.inventory.record_physical_count(
        "TANK-PETROL", DAY, Decimal("6480"), measured_by="manager", note="dip stick"
    )
    position = _position_for(svc.inventory.stock_positions(DAY), "TANK-PETROL")
    assert dto_field(position, "physical_ml") == 6_480_000
    assert dto_field(position, "variance_ml") == -20_000

    # Authorized adjustment absorbs the variance; books reconcile.
    svc.inventory.record_adjustment(
        fuel_product="PETROL",
        tank="TANK-PETROL",
        business_date=DAY,
        quantity_litres_signed=Decimal("-20"),
        reason="dip variance: 20 L shortage vs book stock",
        authorized_by="manager",
    )
    position = _position_for(svc.inventory.stock_positions(DAY), "TANK-PETROL")
    assert dto_field(position, "theoretical_ml") == 6_480_000
    assert dto_field(position, "variance_ml") == 0
    assert _db_theoretical_ml(db, DAY, "TANK-PETROL") == 6_480_000

    audits = db.execute(
        "SELECT business_date, action FROM audit_log"
        " WHERE table_name = 'inventory_adjustments'"
    ).fetchall()
    assert any(a["action"] == "INSERT" and a["business_date"] == DAY for a in audits), (
        "the adjustment must leave an audited INSERT row"
    )

    # The dip reading itself is correctable (mutable, audited) — not the ledger.
    svc.inventory.correct_physical_count(
        "TANK-PETROL", DAY, Decimal("6485"), reason="re-dip verified"
    )
    reading = db.execute(
        "SELECT quantity_ml FROM tank_readings WHERE tank_id = ? AND business_date = ?",
        (tank_id(db, "TANK-PETROL"), DAY),
    ).fetchone()
    assert reading["quantity_ml"] == 6_485_000
    updates = db.execute(
        "SELECT COUNT(*) FROM audit_log"
        " WHERE table_name = 'tank_readings' AND action = 'UPDATE'"
    ).fetchone()[0]
    assert updates >= 1
