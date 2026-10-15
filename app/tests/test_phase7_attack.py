"""Phase-7 attack-style security tests — raw-SQL attacks on a SECOND connection.

Every test attacks through ``atk``: a raw ``sqlite3`` connection (no
``DatabaseManager``, no pragma setup) with ``PRAGMA foreign_keys=ON``,
proving the *database itself* — triggers + constraints — rejects hostile
writes from any client, including direct SQL that bypasses the app layer.

Two database setups (fresh temp DB per test, never the live DB):

* ``pop_a`` — an OPEN business day populated with posted rows via minimal
  raw SQL inserts that respect every FK and BEFORE INSERT trigger (the
  task explicitly allows this). Used for immutability, duplicates,
  over-allocation, price-edit, audit-log and created_at attacks.
* ``closed`` — a day built through the SERVICE layer (supplier, prices,
  deliveries, readings, posted sales, credit ticket, payment + allocation,
  cash book, expenses, supplier payment, other-materials sale, stock
  adjustment, tank readings) and then closed via
  ``BusinessDayService.close_day``. Used for the closed-day guard attacks.

For EVERY attack the test asserts BOTH:

1. the statement raises (``sqlite3.IntegrityError`` / ``OperationalError``
   from a trigger ABORT or constraint, or the service-layer
   ``BusinessRuleError`` where the attack goes through a service), and
2. no partial write occurred — row counts and the attacked row's full
   values are identical before and after (failed statement rolled back).

Known gaps documented:

* ``tank_readings`` closed-day UPDATE/DELETE were unblocked at the time
  these tests were written (D1 only); migration 014 added the D2/D3 guards
  and ``test_closed_day_tank_readings_update_delete_gap`` now asserts the
  block. Open-day updates stay allowed + audited by deliberate design.
* ``audit_log`` accepts manual INSERTs (no guard); UPDATE/DELETE are
  rejected. See ``test_audit_log_manual_insert_observed``.

Design plan: documentation/00_design_plan.md §4 (trigger groups A–I),
§10 acceptance tests 2/3/10/11/12.
"""

from __future__ import annotations

import sqlite3
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from .conftest import (
    close_prior_day,
    dto_field,
    pkt_clock_utc_iso,
    pkt_midnight_utc_iso,
    seed_phase3_day,
)

DAY = "2026-10-10"
PREV = "2026-10-09"
NOZZLES = ("1 PG+", "2 PG+", "3 HSD", "4 HSD")

#: Trigger ABORTs and constraint violations both surface as these.
REJECT = (sqlite3.IntegrityError, sqlite3.OperationalError)


# ---------------------------------------------------------------------------
# attack connection + assertion helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def atk(db2):
    """The attacker's connection: raw sqlite3 with ``PRAGMA foreign_keys=ON``.

    ``db2`` (conftest) deliberately has no pragma setup at all; the task
    requires the attacks to run with foreign-key enforcement on.
    """
    db2.execute("PRAGMA foreign_keys=ON")
    assert db2.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    return db2


def _count(atk, table: str) -> int:
    return int(atk.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _full_row(atk, table: str, where: str, params: tuple):
    row = atk.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchone()
    return None if row is None else tuple(row)


def _attack_rejected_insert(atk, table: str, sql: str, params: tuple = ()) -> None:
    """INSERT must raise; the table's row count must be unchanged."""
    before = _count(atk, table)
    with pytest.raises(REJECT):
        atk.execute(sql, params)
    atk.rollback()
    assert _count(atk, table) == before, f"partial write into {table}"


def _attack_rejected_update(
    atk, table: str, where: str, params: tuple, column: str, tampered
) -> None:
    """UPDATE must raise; count and the row's full values must be unchanged."""
    before_n = _count(atk, table)
    before = _full_row(atk, table, where, params)
    assert before is not None, f"seed row missing in {table}"
    with pytest.raises(REJECT):
        atk.execute(
            f"UPDATE {table} SET {column} = ? WHERE {where}", (tampered, *params)
        )
    atk.rollback()
    assert _count(atk, table) == before_n, f"partial write into {table}"
    assert _full_row(atk, table, where, params) == before, (
        f"row silently modified in {table}"
    )


def _attack_rejected_delete(atk, table: str, where: str, params: tuple) -> None:
    """DELETE must raise; count and the row must be unchanged."""
    before_n = _count(atk, table)
    before = _full_row(atk, table, where, params)
    assert before is not None, f"seed row missing in {table}"
    with pytest.raises(REJECT):
        atk.execute(f"DELETE FROM {table} WHERE {where}", params)
    atk.rollback()
    assert _count(atk, table) == before_n, f"partial write into {table}"
    assert _full_row(atk, table, where, params) == before, (
        f"row silently deleted from {table}"
    )


def _by_id(table: str, row_id: int) -> tuple[str, tuple]:
    return "id = ?", (row_id,)


# ---------------------------------------------------------------------------
# Seed A — open day, posted rows via FK-respecting raw SQL
# ---------------------------------------------------------------------------


def _seed_open_day(db, day: str) -> SimpleNamespace:
    """Populate one OPEN business day with a posted row per ledger table."""
    g = SimpleNamespace()
    db.execute(
        "INSERT INTO business_days (business_date, status, opening_cash_paisa)"
        " VALUES (?, 'open', 2000000)",
        (day,),
    )
    db.execute(
        "INSERT INTO business_day_events (business_date, event_type, actor)"
        " VALUES (?, 'open', 'system')",
        (day,),
    )
    g.supplier_id = db.execute(
        "INSERT INTO suppliers (name) VALUES ('PSO Depot')"
    ).lastrowid
    g.petrol_id = db.execute(
        "SELECT id FROM fuel_products WHERE code = 'PETROL'"
    ).fetchone()[0]
    g.diesel_id = db.execute(
        "SELECT id FROM fuel_products WHERE code = 'DIESEL'"
    ).fetchone()[0]
    g.tank_p = db.execute(
        "SELECT id FROM tanks WHERE code = 'TANK-PETROL'"
    ).fetchone()[0]
    g.tank_d = db.execute(
        "SELECT id FROM tanks WHERE code = 'TANK-DIESEL'"
    ).fetchone()[0]
    g.nozzles = {
        code: db.execute(
            "SELECT id FROM nozzles WHERE code = ?", (code,)
        ).fetchone()[0]
        for code in NOZZLES
    }
    eff = pkt_midnight_utc_iso(day)
    g.price_petrol_id = db.execute(
        "INSERT INTO price_history (fuel_product_id, effective_from_utc,"
        " unit_price_10k, business_date) VALUES (?, ?, 2659630, ?)",
        (g.petrol_id, eff, day),
    ).lastrowid
    db.execute(
        "INSERT INTO price_history (fuel_product_id, effective_from_utc,"
        " unit_price_10k, business_date) VALUES (?, ?, 2670000, ?)",
        (g.diesel_id, eff, day),
    )
    g.reading_ids = []
    for i, code in enumerate(NOZZLES):
        g.reading_ids.append(
            db.execute(
                "INSERT INTO nozzle_readings (nozzle_id, business_date,"
                " reading_kind, reading_ml) VALUES (?, ?, 'closing', ?)",
                (g.nozzles[code], day, 100_000 * (i + 1)),
            ).lastrowid
        )
    db.execute(
        "INSERT INTO opening_balances (business_date, balance_type,"
        " fuel_product_id, quantity_ml) VALUES (?, 'stock', ?, 5000000)",
        (day, g.petrol_id),
    )
    g.opening_cash_id = db.execute(
        "INSERT INTO opening_balances (business_date, balance_type,"
        " amount_paisa) VALUES (?, 'cash', 2000000)",
        (day,),
    ).lastrowid
    # delivery movement first (F2 no-negative needs stock before the sale)
    delivery_uuid = uuid.uuid4().hex
    g.movement_delivery_id = db.execute(
        "INSERT INTO inventory_movements (uuid, business_date, fuel_product_id,"
        " tank_id, movement_type, quantity_ml, source_table, source_uuid)"
        " VALUES (?, ?, ?, ?, 'delivery', 2000000, 'fuel_deliveries', ?)",
        (delivery_uuid, day, g.petrol_id, g.tank_p, delivery_uuid),
    ).lastrowid
    g.movement_sale_id = db.execute(
        "INSERT INTO inventory_movements (uuid, business_date, fuel_product_id,"
        " tank_id, movement_type, quantity_ml, source_table, source_uuid)"
        " VALUES (?, ?, ?, ?, 'sale', -100000, 'nozzle_reading_intervals', ?)",
        (uuid.uuid4().hex, day, g.petrol_id, g.tank_p, uuid.uuid4().hex),
    ).lastrowid
    g.adjustment_id = db.execute(
        "INSERT INTO inventory_adjustments (uuid, fuel_product_id, tank_id,"
        " business_date, quantity_ml, reason, authorized_by)"
        " VALUES (?, ?, ?, ?, 50000, 'dip variance approved', 'manager')",
        (uuid.uuid4().hex, g.petrol_id, g.tank_p, day),
    ).lastrowid
    total = (2_000_000 * 2_600_000 + 50_000) // 100_000
    g.delivery_id = db.execute(
        "INSERT INTO fuel_deliveries (uuid, supplier_id, fuel_product_id,"
        " tank_id, business_date, quantity_ml, unit_cost_10k, total_cost_paisa,"
        " other_cost_paisa, supplier_invoice_no, amount_paid_paisa,"
        " payment_status) VALUES (?, ?, ?, ?, ?, 2000000, 2600000, ?, 0,"
        " 'INV-001', 0, 'unpaid')",
        (delivery_uuid, g.supplier_id, g.petrol_id, g.tank_p, day, total),
    ).lastrowid
    g.customer_id = db.execute(
        "INSERT INTO customers (customer_code, name)"
        " VALUES ('C-001', 'Test Customer')"
    ).lastrowid
    line_amt = (40_000 * 2_700_000 + 50_000) // 100_000  # 1,080,000
    g.ticket1_id = db.execute(
        "INSERT INTO credit_sales (uuid, customer_id, business_date,"
        " gross_amount_paisa, paid_now_paisa, receipt_no)"
        " VALUES (?, ?, ?, ?, 300000, 'CR-1')",
        (uuid.uuid4().hex, g.customer_id, day, line_amt),
    ).lastrowid
    g.line1_id = db.execute(
        "INSERT INTO credit_sale_lines (credit_sale_id, seq_no,"
        " fuel_product_id, nozzle_id, quantity_ml, unit_price_10k,"
        " line_amount_paisa) VALUES (?, 1, ?, ?, 40000, 2700000, ?)",
        (g.ticket1_id, g.petrol_id, g.nozzles["1 PG+"], line_amt),
    ).lastrowid
    line2_amt = (20_000 * 2_700_000 + 50_000) // 100_000  # 540,000
    g.ticket2_id = db.execute(
        "INSERT INTO credit_sales (uuid, customer_id, business_date,"
        " gross_amount_paisa, paid_now_paisa, receipt_no)"
        " VALUES (?, ?, ?, ?, 0, 'CR-2')",
        (uuid.uuid4().hex, g.customer_id, day, line2_amt),
    ).lastrowid
    g.payment_id = db.execute(
        "INSERT INTO customer_payments (uuid, customer_id, business_date,"
        " amount_paisa, method, receipt_no)"
        " VALUES (?, ?, ?, 500000, 'cash', 'PAY-1')",
        (uuid.uuid4().hex, g.customer_id, day),
    ).lastrowid
    g.alloc_id = db.execute(
        "INSERT INTO payment_allocations (payment_id, credit_sale_id,"
        " amount_paisa, business_date) VALUES (?, ?, 300000, ?)",
        (g.payment_id, g.ticket1_id, day),
    ).lastrowid
    g.alloc_reversal_id = db.execute(
        "INSERT INTO payment_allocation_reversals (uuid, allocation_id,"
        " payment_id, credit_sale_id, amount_paisa, business_date, reason)"
        " VALUES (?, ?, ?, ?, 100000, ?, 'wrong ticket')",
        (uuid.uuid4().hex, g.alloc_id, g.payment_id, g.ticket1_id, day),
    ).lastrowid
    g.cust_adj_id = db.execute(
        "INSERT INTO customer_opening_adjustments (uuid, customer_id,"
        " business_date, adjustment_paisa, reason)"
        " VALUES (?, ?, ?, 50000, 'opening balance correction')",
        (uuid.uuid4().hex, g.customer_id, day),
    ).lastrowid
    cash_sales_cat = db.execute(
        "SELECT id FROM cash_categories WHERE name = 'cash_sales'"
    ).fetchone()[0]
    expense_cat = db.execute(
        "SELECT id FROM cash_categories WHERE name = 'expense'"
    ).fetchone()[0]
    g.cash_receipt_id = db.execute(
        "INSERT INTO cash_receipts (uuid, business_date, category_id,"
        " amount_paisa, method) VALUES (?, ?, ?, 1000000, 'cash')",
        (uuid.uuid4().hex, day, cash_sales_cat),
    ).lastrowid
    g.cash_payment_id = db.execute(
        "INSERT INTO cash_payments (uuid, business_date, category_id,"
        " amount_paisa, method) VALUES (?, ?, ?, 200000, 'cash')",
        (uuid.uuid4().hex, day, expense_cat),
    ).lastrowid
    g.settlement_id = db.execute(
        "INSERT INTO sales_settlements (uuid, business_date, fuel_product_id,"
        " category, amount_paisa, batch_uuid)"
        " VALUES (?, ?, ?, 'cash', 5000000, ?)",
        (uuid.uuid4().hex, day, g.petrol_id, uuid.uuid4().hex),
    ).lastrowid
    g.expense_id = db.execute(
        "INSERT INTO expenses (uuid, business_date, category_id,"
        " amount_paisa, method) VALUES (?, ?, 2, 150000, 'cash')",
        (uuid.uuid4().hex, day),
    ).lastrowid
    g.supplier_payment_id = db.execute(
        "INSERT INTO supplier_payments (uuid, supplier_id, delivery_id,"
        " business_date, amount_paisa, method)"
        " VALUES (?, ?, ?, ?, 100000, 'cash')",
        (uuid.uuid4().hex, g.supplier_id, g.delivery_id, day),
    ).lastrowid
    g.product_id = db.execute(
        "INSERT INTO other_products (name, unit)"
        " VALUES ('Engine Oil 1L', 'bottle')"
    ).lastrowid
    g.other_txn_id = db.execute(
        "INSERT INTO other_product_transactions (uuid, business_date,"
        " product_id, txn_type, quantity_units, unit_price_paisa, total_paisa,"
        " method) VALUES (?, ?, ?, 'sale', 2, 85000, 170000, 'cash')",
        (uuid.uuid4().hex, day, g.product_id),
    ).lastrowid
    g.tank_reading_id = db.execute(
        "INSERT INTO tank_readings (tank_id, business_date, quantity_ml,"
        " measured_by) VALUES (?, ?, 4500000, 'manager')",
        (g.tank_p, day),
    ).lastrowid
    g.closing_id = db.execute(
        "INSERT INTO business_day_closings (business_date, closing_revision,"
        " opening_cash_paisa, closing_cash_paisa, summary_json)"
        " VALUES (?, 1, 2000000, 2100000, '{}')",
        (day,),
    ).lastrowid
    db.execute(
        "INSERT INTO stock_opening_values (business_date, fuel_product_id,"
        " value_paisa) VALUES (?, ?, 1300000000)",
        (day, g.petrol_id),
    )
    g.event_id = db.execute(
        "SELECT id FROM business_day_events WHERE business_date = ?"
        " AND event_type = 'open'",
        (day,),
    ).fetchone()[0]
    db.commit()
    g.day = day
    return g


@pytest.fixture
def pop_a(db):
    """Seed A: open day with posted rows (raw SQL, FK-respecting)."""
    return _seed_open_day(db, DAY)


# ---------------------------------------------------------------------------
# 1. Posted-row immutability — UPDATE and DELETE on every append-only table
# ---------------------------------------------------------------------------

#: (table, business column to tamper, tampered value). The seed id is looked
#: up from the ``pop_a`` namespace by table name; ``stock_opening_values``
#: has no ``id`` column and is keyed by (business_date, fuel_product_id).
IMMUTABLE_TABLES = [
    ("nozzle_readings", "reading_ml", 999_999_999),
    ("fuel_deliveries", "other_cost_paisa", 12_345),
    ("inventory_movements", "reason", "tampered"),
    ("inventory_adjustments", "reason", "tampered"),
    ("credit_sales", "gross_amount_paisa", 1),
    ("credit_sale_lines", "line_amount_paisa", 1),
    ("customer_payments", "amount_paisa", 1),
    ("payment_allocations", "amount_paisa", 1),
    ("payment_allocation_reversals", "reason", "tampered"),
    ("customer_opening_adjustments", "reason", "tampered"),
    ("cash_receipts", "amount_paisa", 1),
    ("cash_payments", "amount_paisa", 1),
    ("sales_settlements", "amount_paisa", 1),
    ("expenses", "amount_paisa", 1),
    ("supplier_payments", "amount_paisa", 1),
    ("business_day_events", "reason", "tampered"),
    ("business_day_closings", "closing_cash_paisa", 1),
    ("opening_balances", "amount_paisa", 1),
    ("stock_opening_values", "value_paisa", 1),
    ("price_history", "unit_price_10k", 1),
    ("other_product_transactions", "total_paisa", 1),
]

_SEED_ID_ATTR = {
    "nozzle_readings": "reading_ids",
    "fuel_deliveries": "delivery_id",
    "inventory_movements": "movement_sale_id",
    "inventory_adjustments": "adjustment_id",
    "credit_sales": "ticket1_id",
    "credit_sale_lines": "line1_id",
    "customer_payments": "payment_id",
    "payment_allocations": "alloc_id",
    "payment_allocation_reversals": "alloc_reversal_id",
    "customer_opening_adjustments": "cust_adj_id",
    "cash_receipts": "cash_receipt_id",
    "cash_payments": "cash_payment_id",
    "sales_settlements": "settlement_id",
    "expenses": "expense_id",
    "supplier_payments": "supplier_payment_id",
    "business_day_events": "event_id",
    "business_day_closings": "closing_id",
    "opening_balances": "opening_cash_id",
    "stock_opening_values": None,  # keyed by (business_date, fuel_product_id)
    "price_history": "price_petrol_id",
    "other_product_transactions": "other_txn_id",
}


def _seed_where(pop_a, table: str) -> tuple[str, tuple]:
    if table == "stock_opening_values":
        return (
            "business_date = ? AND fuel_product_id = ?",
            (pop_a.day, pop_a.petrol_id),
        )
    attr = _SEED_ID_ATTR[table]
    row_id = getattr(pop_a, attr)
    if isinstance(row_id, list):
        row_id = row_id[0]
    return _by_id(table, int(row_id))


@pytest.mark.parametrize(
    "table,column,tampered", IMMUTABLE_TABLES, ids=[t[0] for t in IMMUTABLE_TABLES]
)
def test_posted_row_update_rejected(pop_a, atk, table, column, tampered):
    """C1: raw-SQL UPDATE of a posted row is rejected; nothing changes."""
    where, params = _seed_where(pop_a, table)
    _attack_rejected_update(atk, table, where, params, column, tampered)


@pytest.mark.parametrize(
    "table,column,_", IMMUTABLE_TABLES, ids=[t[0] for t in IMMUTABLE_TABLES]
)
def test_posted_row_delete_rejected(pop_a, atk, table, column, _):
    """C2: raw-SQL DELETE of a posted row is rejected; nothing changes."""
    where, params = _seed_where(pop_a, table)
    _attack_rejected_delete(atk, table, where, params)


# ---------------------------------------------------------------------------
# 7. created_at_utc tampering (A1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table",
    [
        "nozzle_readings",
        "fuel_deliveries",
        "inventory_movements",
        "credit_sales",
        "cash_receipts",
        "expenses",
    ],
)
def test_created_at_utc_update_rejected(pop_a, atk, table):
    """A1: raw-SQL UPDATE of created_at_utc aborts; the value is unchanged."""
    where, params = _seed_where(pop_a, table)
    _attack_rejected_update(
        atk, table, where, params, "created_at_utc", "2000-01-01T00:00:00.000Z"
    )


# ---------------------------------------------------------------------------
# 3. Duplicates
# ---------------------------------------------------------------------------


def test_duplicate_closing_reading_rejected(pop_a, atk):
    """E1: a second 'closing' reading for the same nozzle+date is rejected."""
    nozzle_id = pop_a.nozzles["1 PG+"]
    _attack_rejected_insert(
        atk,
        "nozzle_readings",
        "INSERT INTO nozzle_readings (nozzle_id, business_date,"
        " reading_kind, reading_ml) VALUES (?, ?, 'closing', 999999999)",
        (nozzle_id, pop_a.day),
    )


def test_duplicate_delivery_invoice_rejected(pop_a, atk):
    """H4: a second delivery with the same (supplier, invoice no) is rejected."""
    total = (1_000_000 * 2_600_000 + 50_000) // 100_000
    _attack_rejected_insert(
        atk,
        "fuel_deliveries",
        "INSERT INTO fuel_deliveries (uuid, supplier_id, fuel_product_id,"
        " tank_id, business_date, quantity_ml, unit_cost_10k, total_cost_paisa,"
        " other_cost_paisa, supplier_invoice_no, amount_paid_paisa,"
        " payment_status) VALUES (?, ?, ?, ?, ?, 1000000, 2600000, ?, 0,"
        " 'INV-001', 0, 'unpaid')",
        (
            uuid.uuid4().hex,
            pop_a.supplier_id,
            pop_a.petrol_id,
            pop_a.tank_p,
            pop_a.day,
            total,
        ),
    )


def test_duplicate_cash_receipt_uuid_rejected(pop_a, atk):
    """H5: re-posting a cash receipt with an already-used uuid is rejected."""
    existing_uuid = atk.execute(
        "SELECT uuid FROM cash_receipts WHERE id = ?", (pop_a.cash_receipt_id,)
    ).fetchone()[0]
    category_id = atk.execute(
        "SELECT category_id FROM cash_receipts WHERE id = ?",
        (pop_a.cash_receipt_id,),
    ).fetchone()[0]
    _attack_rejected_insert(
        atk,
        "cash_receipts",
        "INSERT INTO cash_receipts (uuid, business_date, category_id,"
        " amount_paisa, method) VALUES (?, ?, ?, 1000000, 'cash')",
        (existing_uuid, pop_a.day, category_id),
    )


def test_duplicate_settlement_batch_leg_rejected(pop_a, atk):
    """Re-posting the same settlement batch leg is rejected (unique leg key).

    Same ``batch_uuid`` and leg key (day, product, category, NULL source)
    with a fresh row uuid — the partial unique index
    ``uq_settlement_manager_leg`` rejects the double posting.
    """
    batch_uuid = atk.execute(
        "SELECT batch_uuid FROM sales_settlements WHERE id = ?",
        (pop_a.settlement_id,),
    ).fetchone()[0]
    _attack_rejected_insert(
        atk,
        "sales_settlements",
        "INSERT INTO sales_settlements (uuid, business_date, fuel_product_id,"
        " category, amount_paisa, batch_uuid)"
        " VALUES (?, ?, ?, 'cash', 5000000, ?)",
        (uuid.uuid4().hex, pop_a.day, pop_a.petrol_id, batch_uuid),
    )


# ---------------------------------------------------------------------------
# 4. Over-allocation (G1 aggregate)
# ---------------------------------------------------------------------------


def test_over_allocation_rejected_atomically(pop_a, atk):
    """G1: allocating past the payment total is rejected; existing
    allocations are unchanged (count and amounts identical)."""
    before = atk.execute(
        "SELECT COUNT(*), COALESCE(SUM(amount_paisa), 0)"
        " FROM payment_allocations WHERE payment_id = ?",
        (pop_a.payment_id,),
    ).fetchone()
    assert tuple(before) == (1, 300_000)
    # 300,000 allocated of a 500,000 payment, with a 100,000 audited
    # reversal on that allocation (effective 200,000); 350,000 more would
    # push the effective total to 550,000 > 500,000 (ticket 2's unpaid
    # balance of 540,000 is not the limiter).
    _attack_rejected_insert(
        atk,
        "payment_allocations",
        "INSERT INTO payment_allocations (payment_id, credit_sale_id,"
        " amount_paisa, business_date) VALUES (?, ?, 350000, ?)",
        (pop_a.payment_id, pop_a.ticket2_id, pop_a.day),
    )
    after = atk.execute(
        "SELECT COUNT(*), COALESCE(SUM(amount_paisa), 0)"
        " FROM payment_allocations WHERE payment_id = ?",
        (pop_a.payment_id,),
    ).fetchone()
    assert tuple(after) == (1, 300_000)


# ---------------------------------------------------------------------------
# 5. Backdated price edit
# ---------------------------------------------------------------------------


def test_backdated_price_edit_rejected_and_sales_unchanged(pop_a, atk):
    """price_history is immutable: UPDATE is rejected and the old sale keeps
    its original priced values."""
    sale_before = _full_row(
        atk, "credit_sale_lines", *_by_id("credit_sale_lines", pop_a.line1_id)
    )
    assert sale_before is not None
    where, params = _by_id("price_history", pop_a.price_petrol_id)
    _attack_rejected_update(
        atk, "price_history", where, params, "unit_price_10k", 9_999_999
    )
    sale_after = _full_row(
        atk, "credit_sale_lines", *_by_id("credit_sale_lines", pop_a.line1_id)
    )
    assert sale_after == sale_before
    # the priced columns specifically are untouched
    cols = [d[1] for d in atk.execute("PRAGMA table_info(credit_sale_lines)")]
    assert sale_after[cols.index("unit_price_10k")] == 2_700_000
    assert sale_after[cols.index("line_amount_paisa")] == 1_080_000


# ---------------------------------------------------------------------------
# 6. audit_log tampering
# ---------------------------------------------------------------------------


def test_audit_log_update_rejected(pop_a, atk):
    """UPDATE on audit_log is rejected; the row is unchanged."""
    row_id = atk.execute("SELECT MIN(id) FROM audit_log").fetchone()[0]
    where, params = _by_id("audit_log", int(row_id))
    _attack_rejected_update(atk, "audit_log", where, params, "actor", "mallory")


def test_audit_log_delete_rejected(pop_a, atk):
    """DELETE on audit_log is rejected; the row survives."""
    row_id = atk.execute("SELECT MIN(id) FROM audit_log").fetchone()[0]
    where, params = _by_id("audit_log", int(row_id))
    _attack_rejected_delete(atk, "audit_log", where, params)


def test_audit_log_manual_insert_observed(pop_a, atk):
    """OBSERVED BEHAVIOR (no trigger installed — out of scope): a manual
    INSERT into audit_log SUCCEEDS. Documented here so the gap is visible;
    UPDATE/DELETE remain rejected (tests above)."""
    before = _count(atk, "audit_log")
    atk.execute(
        "INSERT INTO audit_log (table_name, record_id, action, actor, reason)"
        " VALUES ('nozzle_readings', 1, 'INSERT', 'attacker',"
        " 'forged audit row')"
    )
    atk.commit()
    after = _count(atk, "audit_log")
    assert after == before + 1, (
        "expected the manual audit_log INSERT to succeed (no guard installed)"
    )
    forged = atk.execute(
        "SELECT actor, reason FROM audit_log WHERE actor = 'attacker'"
    ).fetchone()
    assert tuple(forged) == ("attacker", "forged audit row")


# ---------------------------------------------------------------------------
# Seed B — day built through the SERVICE layer, closed via the service layer
# ---------------------------------------------------------------------------


@pytest.fixture
def closed(phase5_services, db):
    """Build a full business day through services, then close it via
    ``BusinessDayService.close_day``. Returns ids needed by the attacks."""
    svc = phase5_services
    g = SimpleNamespace(day=DAY)
    close_prior_day(
        svc,
        PREV,
        {code: "100" for code in NOZZLES},
        opening_stock_litres="5000",
        physical_cash_paisa=2_000_000,
    )
    seed_phase3_day(svc, DAY)
    for code in NOZZLES:
        svc.nozzles.record_reading(DAY, code, "200")  # 100 L known intervals
    g.delivery_petrol = svc.deliveries.record_delivery(
        supplier="PSO Depot",
        fuel_product="PETROL",
        tank="TANK-PETROL",
        business_date=DAY,
        quantity_litres=Decimal("2000"),
        unit_cost_pkr=Decimal("260"),
        supplier_invoice_no="INV-200",
        occurred_at_utc=pkt_clock_utc_iso(DAY, 8),
    )
    g.delivery_diesel = svc.deliveries.record_delivery(
        supplier="PSO Depot",
        fuel_product="DIESEL",
        tank="TANK-DIESEL",
        business_date=DAY,
        quantity_litres=Decimal("2000"),
        unit_cost_pkr=Decimal("260"),
        supplier_invoice_no="INV-201",
        occurred_at_utc=pkt_clock_utc_iso(DAY, 8),
    )
    svc.sales.post_day_sales(DAY)
    svc.credit.create_customer("Attack Customer")  # -> CUST-0001
    g.ticket1 = svc.credit.issue_ticket(
        "CUST-0001",
        DAY,
        [
            {
                "fuel_product_code": "PETROL",
                "nozzle_code": "1 PG+",
                "quantity_litres": Decimal("40"),
            }
        ],
        occurred_at_utc=pkt_clock_utc_iso(DAY, 10),
    )
    g.ticket2 = svc.credit.issue_ticket(
        "CUST-0001",
        DAY,
        [
            {
                "fuel_product_code": "PETROL",
                "nozzle_code": "2 PG+",
                "quantity_litres": Decimal("10"),
            }
        ],
        occurred_at_utc=pkt_clock_utc_iso(DAY, 11),
    )
    g.payment = svc.payments.record_payment(
        "CUST-0001",
        DAY,
        500_000,
        auto_allocate=False,
        occurred_at_utc=pkt_clock_utc_iso(DAY, 12),
    )
    svc.payments.allocate(
        g.payment.id,
        [{"ticket_uuid": g.ticket1.uuid, "amount_paisa": 300_000}],
        occurred_at_utc=pkt_clock_utc_iso(DAY, 12),
    )
    g.cash_receipt = svc.cash.record_receipt(
        business_date=DAY,
        category="other_receipt",
        amount_paisa=1_000_000,
        occurred_at_utc=pkt_clock_utc_iso(DAY, 13),
    )
    g.cash_payment = svc.cash.record_payment(
        business_date=DAY,
        category="other_payment",
        amount_paisa=200_000,
        payee="tester",
        occurred_at_utc=pkt_clock_utc_iso(DAY, 14),
    )
    g.expense = svc.expenses.record_expense(
        business_date=DAY,
        category="Salaries",
        amount_paisa=150_000,
        method="cash",
        occurred_at_utc=pkt_clock_utc_iso(DAY, 15),
    )
    g.supplier_payment = svc.suppliers.record_supplier_payment(
        supplier="PSO Depot",
        business_date=DAY,
        amount_paisa=100_000,
        method="cash",
        delivery_id=g.delivery_petrol,
        occurred_at_utc=pkt_clock_utc_iso(DAY, 16),
    )
    g.product_id = svc.materials.add_product("Engine Oil 1L", "bottle")
    svc.materials.record_purchase(
        product=g.product_id,
        business_date=DAY,
        quantity_units=5,
        unit_price_paisa=70_000,
        method="cash",
        occurred_at_utc=pkt_clock_utc_iso(DAY, 17),
    )
    g.other_txn = svc.materials.record_sale(
        product=g.product_id,
        business_date=DAY,
        quantity_units=2,
        unit_price_paisa=85_000,
        method="cash",
        occurred_at_utc=pkt_clock_utc_iso(DAY, 18),
    )
    svc.inventory.record_adjustment(
        fuel_product="PETROL",
        tank="TANK-PETROL",
        business_date=DAY,
        quantity_litres_signed=Decimal("50"),
        reason="dip variance approved",
        authorized_by="manager",
    )
    # Tables with no service writer in this phase: raw SQL on the open day.
    g.customer_id = db.execute(
        "SELECT id FROM customers WHERE customer_code = 'CUST-0001'"
    ).fetchone()[0]
    tank_p = db.execute(
        "SELECT id FROM tanks WHERE code = 'TANK-PETROL'"
    ).fetchone()[0]
    tank_d = db.execute(
        "SELECT id FROM tanks WHERE code = 'TANK-DIESEL'"
    ).fetchone()[0]
    g.tank_reading_id = db.execute(
        "INSERT INTO tank_readings (tank_id, business_date, quantity_ml,"
        " measured_by) VALUES (?, ?, 4500000, 'manager')",
        (tank_p, DAY),
    ).lastrowid
    db.execute(
        "INSERT INTO tank_readings (tank_id, business_date, quantity_ml,"
        " measured_by) VALUES (?, ?, 4600000, 'manager')",
        (tank_d, DAY),
    )
    alloc_id = db.execute(
        "SELECT id FROM payment_allocations WHERE payment_id = ?",
        (dto_field(g.payment, "id"),),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO payment_allocation_reversals (uuid, allocation_id,"
        " payment_id, credit_sale_id, amount_paisa, business_date, reason)"
        " VALUES (?, ?, ?, ?, 100000, ?, 'wrong ticket')",
        (
            uuid.uuid4().hex,
            alloc_id,
            dto_field(g.payment, "id"),
            dto_field(g.ticket1, "id"),
            DAY,
        ),
    )
    db.execute(
        "INSERT INTO customer_opening_adjustments (uuid, customer_id,"
        " business_date, adjustment_paisa, reason)"
        " VALUES (?, ?, ?, 50000, 'opening balance correction')",
        (uuid.uuid4().hex, g.customer_id, DAY),
    )
    db.commit()

    svc.days.close_day(DAY, physical_cash_paisa=2_100_000)
    assert (
        db.execute(
            "SELECT status FROM business_days WHERE business_date = ?", (DAY,)
        ).fetchone()[0]
        == "closed"
    )
    return g


def _closed_id(atk, table: str, day: str, extra: str = "", params: tuple = ()):
    row = atk.execute(
        f"SELECT id FROM {table} WHERE business_date = ?{extra} ORDER BY id"
        f" LIMIT 1",
        (day, *params),
    ).fetchone()
    assert row is not None, f"no {table} row for {day}"
    return int(row[0])


# ---------------------------------------------------------------------------
# 2. Closed-day guards — INSERT / UPDATE / DELETE on the closed date
# ---------------------------------------------------------------------------


def test_closed_day_nozzle_readings(closed, atk):
    """D1/D2/D3 on nozzle_readings: insert, update and delete rejected."""
    day = closed.day
    nozzle_id = atk.execute(
        "SELECT id FROM nozzles WHERE code = '1 PG+'"
    ).fetchone()[0]
    rid = _closed_id(atk, "nozzle_readings", day, " AND nozzle_id = ?", (nozzle_id,))
    _attack_rejected_insert(
        atk,
        "nozzle_readings",
        "INSERT INTO nozzle_readings (nozzle_id, business_date, reading_kind,"
        " reading_ml) VALUES (?, ?, 'segment', 250000)",
        (nozzle_id, day),
    )
    where, params = _by_id("nozzle_readings", rid)
    _attack_rejected_update(atk, "nozzle_readings", where, params, "reading_ml", 1)
    _attack_rejected_delete(atk, "nozzle_readings", where, params)


def test_closed_day_fuel_deliveries(closed, atk):
    """D1/D2/D3 on fuel_deliveries: insert, update and delete rejected."""
    day = closed.day
    sup = atk.execute("SELECT id FROM suppliers WHERE name = 'PSO Depot'").fetchone()[0]
    petrol = atk.execute(
        "SELECT id FROM fuel_products WHERE code = 'PETROL'"
    ).fetchone()[0]
    tank = atk.execute(
        "SELECT id FROM tanks WHERE code = 'TANK-PETROL'"
    ).fetchone()[0]
    total = (1_000_000 * 2_600_000 + 50_000) // 100_000
    _attack_rejected_insert(
        atk,
        "fuel_deliveries",
        "INSERT INTO fuel_deliveries (uuid, supplier_id, fuel_product_id,"
        " tank_id, business_date, quantity_ml, unit_cost_10k, total_cost_paisa,"
        " other_cost_paisa, supplier_invoice_no, amount_paid_paisa,"
        " payment_status) VALUES (?, ?, ?, ?, ?, 1000000, 2600000, ?, 0,"
        " 'INV-ATTACK', 0, 'unpaid')",
        (uuid.uuid4().hex, sup, petrol, tank, day, total),
    )
    where, params = _by_id("fuel_deliveries", int(closed.delivery_petrol))
    _attack_rejected_update(
        atk, "fuel_deliveries", where, params, "other_cost_paisa", 1
    )
    _attack_rejected_delete(atk, "fuel_deliveries", where, params)


def test_closed_day_inventory_movements(closed, atk):
    """D1/D2/D3 on inventory_movements: insert, update and delete rejected."""
    day = closed.day
    petrol = atk.execute(
        "SELECT id FROM fuel_products WHERE code = 'PETROL'"
    ).fetchone()[0]
    tank = atk.execute(
        "SELECT id FROM tanks WHERE code = 'TANK-PETROL'"
    ).fetchone()[0]
    _attack_rejected_insert(
        atk,
        "inventory_movements",
        "INSERT INTO inventory_movements (uuid, business_date, fuel_product_id,"
        " tank_id, movement_type, quantity_ml, source_table, source_uuid)"
        " VALUES (?, ?, ?, ?, 'adjustment', 1000, 'manual', ?)",
        (uuid.uuid4().hex, day, petrol, tank, uuid.uuid4().hex),
    )
    mid = _closed_id(atk, "inventory_movements", day)
    where, params = _by_id("inventory_movements", mid)
    _attack_rejected_update(atk, "inventory_movements", where, params, "reason", "x")
    _attack_rejected_delete(atk, "inventory_movements", where, params)


def test_closed_day_credit_sales(closed, atk):
    """D1/D2/D3 on credit_sales: insert, update and delete rejected."""
    day = closed.day
    _attack_rejected_insert(
        atk,
        "credit_sales",
        "INSERT INTO credit_sales (uuid, customer_id, business_date,"
        " gross_amount_paisa, paid_now_paisa, receipt_no)"
        " VALUES (?, ?, ?, 100000, 0, 'CR-ATTACK')",
        (uuid.uuid4().hex, closed.customer_id, day),
    )
    where, params = _by_id("credit_sales", dto_field(closed.ticket1, "id"))
    _attack_rejected_update(
        atk, "credit_sales", where, params, "gross_amount_paisa", 1
    )
    _attack_rejected_delete(atk, "credit_sales", where, params)


def test_closed_day_customer_payments(closed, atk):
    """D1/D2/D3 on customer_payments: insert, update and delete rejected."""
    day = closed.day
    _attack_rejected_insert(
        atk,
        "customer_payments",
        "INSERT INTO customer_payments (uuid, customer_id, business_date,"
        " amount_paisa, method, receipt_no)"
        " VALUES (?, ?, ?, 50000, 'cash', 'PAY-ATTACK')",
        (uuid.uuid4().hex, closed.customer_id, day),
    )
    where, params = _by_id("customer_payments", dto_field(closed.payment, "id"))
    _attack_rejected_update(
        atk, "customer_payments", where, params, "amount_paisa", 1
    )
    _attack_rejected_delete(atk, "customer_payments", where, params)


def test_closed_day_payment_allocations(closed, atk):
    """D1/D2/D3 on payment_allocations: insert, update and delete rejected.

    The INSERT is fully G1-valid (posted payment/ticket, same customer,
    within both the ticket's unpaid balance and the payment total), so the
    closed-day guard is the only possible rejector.
    """
    day = closed.day
    _attack_rejected_insert(
        atk,
        "payment_allocations",
        "INSERT INTO payment_allocations (payment_id, credit_sale_id,"
        " amount_paisa, business_date) VALUES (?, ?, 100000, ?)",
        (
            dto_field(closed.payment, "id"),
            dto_field(closed.ticket2, "id"),
            day,
        ),
    )
    aid = _closed_id(atk, "payment_allocations", day)
    where, params = _by_id("payment_allocations", aid)
    _attack_rejected_update(
        atk, "payment_allocations", where, params, "amount_paisa", 1
    )
    _attack_rejected_delete(atk, "payment_allocations", where, params)


def test_closed_day_cash_receipts(closed, atk):
    """D1/D2/D3 on cash_receipts: insert, update and delete rejected."""
    day = closed.day
    cat = atk.execute(
        "SELECT id FROM cash_categories WHERE name = 'other_receipt'"
    ).fetchone()[0]
    _attack_rejected_insert(
        atk,
        "cash_receipts",
        "INSERT INTO cash_receipts (uuid, business_date, category_id,"
        " amount_paisa, method) VALUES (?, ?, ?, 10000, 'cash')",
        (uuid.uuid4().hex, day, cat),
    )
    where, params = _by_id("cash_receipts", dto_field(closed.cash_receipt, "id"))
    _attack_rejected_update(atk, "cash_receipts", where, params, "amount_paisa", 1)
    _attack_rejected_delete(atk, "cash_receipts", where, params)


def test_closed_day_cash_payments(closed, atk):
    """D1/D2/D3 on cash_payments: insert, update and delete rejected."""
    day = closed.day
    cat = atk.execute(
        "SELECT id FROM cash_categories WHERE name = 'other_payment'"
    ).fetchone()[0]
    _attack_rejected_insert(
        atk,
        "cash_payments",
        "INSERT INTO cash_payments (uuid, business_date, category_id,"
        " amount_paisa, method) VALUES (?, ?, ?, 10000, 'cash')",
        (uuid.uuid4().hex, day, cat),
    )
    where, params = _by_id("cash_payments", dto_field(closed.cash_payment, "id"))
    _attack_rejected_update(atk, "cash_payments", where, params, "amount_paisa", 1)
    _attack_rejected_delete(atk, "cash_payments", where, params)


def test_closed_day_sales_settlements(closed, atk):
    """D1/D2/D3 on sales_settlements: insert, update and delete rejected."""
    day = closed.day
    diesel = atk.execute(
        "SELECT id FROM fuel_products WHERE code = 'DIESEL'"
    ).fetchone()[0]
    _attack_rejected_insert(
        atk,
        "sales_settlements",
        "INSERT INTO sales_settlements (uuid, business_date, fuel_product_id,"
        " category, amount_paisa, batch_uuid)"
        " VALUES (?, ?, ?, 'other', 10000, ?)",
        (uuid.uuid4().hex, day, diesel, uuid.uuid4().hex),
    )
    sid = _closed_id(atk, "sales_settlements", day)
    where, params = _by_id("sales_settlements", sid)
    _attack_rejected_update(atk, "sales_settlements", where, params, "amount_paisa", 1)
    _attack_rejected_delete(atk, "sales_settlements", where, params)


def test_closed_day_expenses(closed, atk):
    """D1/D2/D3 on expenses: insert, update and delete rejected."""
    day = closed.day
    _attack_rejected_insert(
        atk,
        "expenses",
        "INSERT INTO expenses (uuid, business_date, category_id,"
        " amount_paisa, method) VALUES (?, ?, 2, 10000, 'cash')",
        (uuid.uuid4().hex, day),
    )
    where, params = _by_id("expenses", dto_field(closed.expense, "id"))
    _attack_rejected_update(atk, "expenses", where, params, "amount_paisa", 1)
    _attack_rejected_delete(atk, "expenses", where, params)


def test_closed_day_supplier_payments(closed, atk):
    """D1/D2/D3 on supplier_payments: insert, update and delete rejected."""
    day = closed.day
    sup = atk.execute("SELECT id FROM suppliers WHERE name = 'PSO Depot'").fetchone()[0]
    _attack_rejected_insert(
        atk,
        "supplier_payments",
        "INSERT INTO supplier_payments (uuid, supplier_id, business_date,"
        " amount_paisa, method) VALUES (?, ?, ?, 10000, 'cash')",
        (uuid.uuid4().hex, sup, day),
    )
    where, params = _by_id(
        "supplier_payments", dto_field(closed.supplier_payment, "id")
    )
    _attack_rejected_update(
        atk, "supplier_payments", where, params, "amount_paisa", 1
    )
    _attack_rejected_delete(atk, "supplier_payments", where, params)


def test_closed_day_other_product_transactions(closed, atk):
    """D1/D2/D3 on other_product_transactions: insert, update, delete rejected."""
    day = closed.day
    _attack_rejected_insert(
        atk,
        "other_product_transactions",
        "INSERT INTO other_product_transactions (uuid, business_date,"
        " product_id, txn_type, quantity_units, unit_price_paisa, total_paisa,"
        " method) VALUES (?, ?, ?, 'sale', 1, 10000, 10000, 'cash')",
        (uuid.uuid4().hex, day, closed.product_id),
    )
    where, params = _by_id(
        "other_product_transactions", dto_field(closed.other_txn, "id")
    )
    _attack_rejected_update(
        atk, "other_product_transactions", where, params, "total_paisa", 1
    )
    _attack_rejected_delete(atk, "other_product_transactions", where, params)


def test_closed_day_tank_readings_insert_rejected(closed, atk):
    """D1 on tank_readings: INSERT on a closed day is rejected."""
    day = closed.day
    tank = atk.execute(
        "SELECT id FROM tanks WHERE code = 'TANK-PETROL'"
    ).fetchone()[0]
    _attack_rejected_insert(
        atk,
        "tank_readings",
        "INSERT INTO tank_readings (tank_id, business_date, quantity_ml,"
        " measured_by) VALUES (?, ?, 1000, 'attacker')",
        (tank, day),
    )


def test_closed_day_tank_readings_update_delete_gap(closed, atk):
    """Closed-day UPDATE/DELETE on tank_readings are BLOCKED by the D2/D3
    guards added in migration 014 (the gap pinned here previously: D1
    existed but UPDATE/DELETE succeeded). On OPEN days tank_readings stay
    mutable + audited by deliberate design (dip-typo corrections)."""
    rid = closed.tank_reading_id
    where, params = _by_id("tank_readings", rid)
    _attack_rejected_update(
        atk, "tank_readings", where, params, "quantity_ml", 1
    )
    _attack_rejected_delete(atk, "tank_readings", where, params)
