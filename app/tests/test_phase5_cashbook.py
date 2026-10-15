"""Phase-5 service tests: cash book, settlements, expenses, supplier payments.

Covers the task contract's Phase-5 surface::

    CashbookService.record_settlements / reverse_settlements /
        settlement_status / record_receipt / record_payment /
        reverse_cash_receipt / reverse_cash_payment / cash_position /
        list_receipts / list_payments / cashbook_entries /
        missing_cash_postings
    ExpenseService.record_expense / reverse_expense / expense_totals
    SupplierService.record_supplier_payment / reverse_supplier_payment /
        supplier_balances / delivery_outstanding / list_payments

plus the spec §6 settlement identity and the acceptance tests 8–9, the
migration-011 effective-amount rule (reversed originals keep their posted
contribution; the reversal row negates; net = 0), and the closed-day /
immutability trigger backstops on the new tables (raw SQL through ``db2``).

Design plan: 00_design_plan.md §5 (numeric standards), §6
(reconciliation), §11 assumptions 8–10/13/14, acceptance tests 8–9,
trigger groups C/D.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

import pytest

from app.services import errors

from .conftest import (
    close_prior_day,
    new_uuid,
    phase4_services,  # noqa: F401  (fixture)
    phase5_services,  # noqa: F401  (fixture)
    pkt_clock_utc_iso,
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
PAISA_PER_ML_250 = 25

NOZZLES = ("1 PG+", "2 PG+", "3 HSD", "4 HSD")


def _open_meter_day(
    svc,
    day: str = DAY,
    *,
    sale_litres: str = "200",
    nozzle: str = "1 PG+",
    price_pkr: Decimal = PRICE_250,
    opening_stock_litres: Decimal | None = Decimal("10000"),
) -> int:
    """Close the prior day, open *day*, post a metered sale on one nozzle.

    Returns the meter revenue in paisa (single product, single nozzle;
    250 PKR/L gives exact paisa/ml so the caller can use integers).
    """
    close_prior_day(
        svc,
        PREV,
        PREV_READINGS,
        price_pkr=price_pkr,
        opening_stock_litres=opening_stock_litres,
    )
    seed_phase3_day(svc, day)
    prev = Decimal(PREV_READINGS[nozzle])
    svc.nozzles.record_reading(day, nozzle, str(prev + Decimal(sale_litres)))
    for code in NOZZLES:
        if code != nozzle:
            record_reset_interval(svc, day, code, PREV_READINGS[code])
    svc.sales.post_day_sales(day)
    return int(Decimal(sale_litres) * 1000 * PAISA_PER_ML_250)


def _petrol_row(status: dict) -> dict:
    (row,) = [
        p for p in status["per_product"] if p["fuel_product_code"] == "PETROL"
    ]
    return row


# ---------------------------------------------------------------------------
# Test 9 — the spec's daily-cash worked example
# ---------------------------------------------------------------------------


def test_daily_cash_worked_example(phase5_services):
    """20,000 + 50,000 + 3,000 − 5,000 − 8,000 = 60,000 expected; every
    receipt/payment is counted exactly once, and re-run paths never
    duplicate (unique source refs; idempotent identical retries)."""
    svc = phase5_services
    assert _open_meter_day(svc, sale_litres="200") == 5_000_000

    # Full-cash settlement → the cash_sales receipt posts inline.
    batch = svc.cash.record_settlements(
        DAY,
        cash_by_product={"PETROL": 5_000_000},
        occurred_at_utc=pkt_clock_utc_iso(DAY, 18),
    )
    # Identical retry is idempotent: same batch back, no second receipt.
    assert (
        svc.cash.record_settlements(
            DAY,
            cash_by_product={"PETROL": 5_000_000},
            occurred_at_utc=pkt_clock_utc_iso(DAY, 18, 30),
        )
        == batch
    )

    # Old-debt recovery: an opening receivable paid in cash on DAY.
    svc.credit.create_customer("Old Debtor")
    svc.credit.adjust_opening_balance(
        1, 300_000, "opening old debt", DAY, authorized_by="manager"
    )
    svc.payments.record_payment(
        "CUST-0001", DAY, 300_000, occurred_at_utc=pkt_clock_utc_iso(DAY, 19)
    )

    # Cash expense (posts its cash_payments row inline) + manual payment.
    svc.expenses.record_expense(
        business_date=DAY,
        category="Miscellaneous",
        amount_paisa=500_000,
        method="cash",
        occurred_at_utc=pkt_clock_utc_iso(DAY, 20),
    )
    svc.cash.record_payment(
        business_date=DAY,
        category="other_payment",
        amount_paisa=800_000,
        payee="Misc vendor",
        occurred_at_utc=pkt_clock_utc_iso(DAY, 21),
    )

    svc.days.close_day(DAY, physical_cash_paisa=6_000_000)

    pos = svc.cash.cash_position(DAY)
    assert pos.opening_cash_paisa == 2_000_000
    assert pos.receipts_paisa == 5_000_000 + 300_000
    assert pos.payments_paisa == 500_000 + 800_000
    assert pos.expected_paisa == 6_000_000
    assert pos.difference_paisa == 0

    receipts = svc.cash.list_receipts(DAY)
    payments = svc.cash.list_payments(DAY)
    assert sorted(r.amount_paisa for r in receipts) == [300_000, 5_000_000]
    assert sorted(p.amount_paisa for p in payments) == [500_000, 800_000]
    # Exactly one row per source: the unique (source_table, source_id)
    # index makes double-posting impossible, retries included.
    sources = {(r.source_table, r.source_id) for r in receipts}
    assert len(sources) == 2
    assert all(table is not None and sid is not None for table, sid in sources)
    # The signed chronological cash book agrees with the position.
    signed = sum(e["signed_paisa"] for e in svc.cash.cashbook_entries(DAY))
    assert signed == pos.receipts_paisa - pos.payments_paisa


# ---------------------------------------------------------------------------
# Test 8 — settlement identity; old-debt recovery is never revenue
# ---------------------------------------------------------------------------


def test_settlement_identity_and_old_recovery_exclusion(phase5_services):
    """meter revenue == settled + new receivables; a later cash recovery
    leaves revenue unchanged, drops the receivable, posts a receipt, and
    the identity still holds."""
    svc = phase5_services
    assert _open_meter_day(svc, sale_litres="200") == 5_000_000
    svc.credit.create_customer("Ticket Customer")
    svc.credit.issue_ticket(
        "CUST-0001",
        DAY,
        [{
            "fuel_product_code": "PETROL",
            "nozzle_code": None,
            "quantity_litres": Decimal("40"),
        }],
        paid_now_paisa=300_000,  # 40 L @ 250 → 1,000,000; unpaid 700,000
        occurred_at_utc=pkt_clock_utc_iso(DAY, 10),
    )
    svc.cash.record_settlements(
        DAY,
        cash_by_product={"PETROL": 4_000_000},
        occurred_at_utc=pkt_clock_utc_iso(DAY, 18),
    )

    status = svc.cash.settlement_status(DAY)
    row = _petrol_row(status)
    assert row["meter_revenue_paisa"] == 5_000_000
    assert row["cash_paisa"] == 4_000_000
    assert row["paid_now_paisa"] == 300_000
    assert row["receivable_paisa"] == 700_000
    assert row["new_receivables_paisa"] == 700_000
    assert row["identity_ok"] is True
    assert status["identity_ok"] is True
    recon = svc.sales.reconcile_day(DAY)
    assert recon["new_receivables_paisa"] == 700_000
    assert recon["settlement_identity_ok"] is True

    # Old-debt recovery: cash in against the ticket's receivable.
    svc.payments.record_payment(
        "CUST-0001", DAY, 200_000, occurred_at_utc=pkt_clock_utc_iso(DAY, 19)
    )
    svc.days.close_day(DAY, physical_cash_paisa=6_500_000)

    # Revenue untouched; receivable down; the recovery is a cash receipt.
    assert svc.credit.outstanding("CUST-0001") == 500_000
    pnl = svc.accounting.daily_pnl(DAY)
    assert pnl.total_revenue_paisa == 5_000_000
    assert pnl.receivables_created_paisa == 700_000
    receipts = svc.cash.list_receipts(DAY)
    recovery = [r for r in receipts if r.category == "credit_recovery"]
    assert [r.amount_paisa for r in recovery] == [200_000]
    # And the §6 identity still balances after the recovery.
    assert svc.cash.settlement_status(DAY)["identity_ok"] is True
    assert svc.sales.reconcile_day(DAY)["settlement_identity_ok"] is True
    pos = svc.cash.cash_position(DAY)
    assert pos.receipts_paisa == 4_000_000 + 300_000 + 200_000
    assert pos.expected_paisa == 6_500_000


# ---------------------------------------------------------------------------
# duplicate source postings
# ---------------------------------------------------------------------------


def test_duplicate_receipt_posting_rejected(phase5_services, db, db2):
    """The same source can never post two cash receipts: the service
    refuses a different settlement split for an already-settled day
    (DuplicateError), and the unique (source_table, source_id) index
    backstops raw SQL."""
    svc = phase5_services
    _open_meter_day(svc, sale_litres="200")
    batch = svc.cash.record_settlements(
        DAY,
        cash_by_product={"PETROL": 5_000_000},
        occurred_at_utc=pkt_clock_utc_iso(DAY, 18),
    )
    (receipt,) = [
        r for r in svc.cash.list_receipts(DAY) if r.category == "cash_sales"
    ]

    # Identical retry → idempotent. Different split → DuplicateError.
    assert (
        svc.cash.record_settlements(
            DAY,
            cash_by_product={"PETROL": 5_000_000},
            occurred_at_utc=pkt_clock_utc_iso(DAY, 18, 30),
        )
        == batch
    )
    with pytest.raises(errors.DuplicateError, match="already recorded"):
        svc.cash.record_settlements(
            DAY,
            cash_by_product={"PETROL": 4_000_000},
            bank_by_product={"PETROL": 1_000_000},
            occurred_at_utc=pkt_clock_utc_iso(DAY, 19),
        )

    # Raw SQL with the same (source_table, source_id) → rejected.
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint"):
        db2.execute(
            "INSERT INTO cash_receipts"
            " (uuid, business_date, category_id, amount_paisa,"
            "  source_table, source_id, occurred_at_utc, created_by)"
            " SELECT ?, business_date, category_id, amount_paisa,"
            "        source_table, source_id, occurred_at_utc, created_by"
            " FROM cash_receipts WHERE uuid = ?",
            (new_uuid(), receipt.uuid),
        )
    db2.rollback()
    assert db.execute(
        "SELECT COUNT(*) FROM cash_receipts WHERE business_date = ?", (DAY,)
    ).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# settlement reversal (migration 011) + re-record
# ---------------------------------------------------------------------------


def test_settlement_reversal_nets_to_zero_and_rerecord_works(
    phase5_services, db
):
    """Record → reverse → every view nets exactly 0 (the 011 fix, pinned
    numerically) → re-record a different split → identity holds again."""
    svc = phase5_services
    _open_meter_day(svc, sale_litres="200")
    batch = svc.cash.record_settlements(
        DAY,
        cash_by_product={"PETROL": 5_000_000},
        occurred_at_utc=pkt_clock_utc_iso(DAY, 18),
    )
    reversal_batch = svc.cash.reverse_settlements(
        DAY, reason="wrong split entered", actor="manager"
    )
    assert reversal_batch != batch

    # The 011 rule: the flipped original keeps its posted contribution and
    # the reversal row negates it — net effect of voiding is exactly 0.
    net = db.execute(
        "SELECT COALESCE(SUM(effective_paisa), 0)"
        " FROM v_effective_settlements WHERE business_date = ?",
        (DAY,),
    ).fetchone()[0]
    assert int(net) == 0
    rows = db.execute(
        "SELECT status, is_reversal, amount_paisa FROM sales_settlements"
        " WHERE business_date = ? ORDER BY id",
        (DAY,),
    ).fetchall()
    assert [(r["status"], r["is_reversal"], r["amount_paisa"]) for r in rows] == [
        ("reversed", 0, 5_000_000),
        ("posted", 1, 5_000_000),
    ]
    # The derived cash_sales receipt nets to 0 as well.
    cash_net = db.execute(
        "SELECT COALESCE(SUM(signed_paisa), 0) FROM v_cashbook"
        " WHERE business_date = ? AND category = 'cash_sales'",
        (DAY,),
    ).fetchone()[0]
    assert int(cash_net) == 0
    assert svc.cash.settlement_status(DAY)["recorded"] is False

    # Re-record with a different split: the day is closable again.
    svc.cash.record_settlements(
        DAY,
        cash_by_product={"PETROL": 3_000_000},
        bank_by_product={"PETROL": 2_000_000},
        occurred_at_utc=pkt_clock_utc_iso(DAY, 19),
    )
    status = svc.cash.settlement_status(DAY)
    assert status["recorded"] is True
    assert status["identity_ok"] is True
    row = _petrol_row(status)
    assert row["cash_paisa"] == 3_000_000
    assert row["bank_paisa"] == 2_000_000
    net_after = db.execute(
        "SELECT COALESCE(SUM(effective_paisa), 0)"
        " FROM v_effective_settlements WHERE business_date = ?",
        (DAY,),
    ).fetchone()[0]
    assert int(net_after) == 5_000_000
    svc.days.close_day(DAY, physical_cash_paisa=5_000_000)


# ---------------------------------------------------------------------------
# paid-now derived receipt; bank payments never touch cash
# ---------------------------------------------------------------------------


def test_paid_now_cash_receipt_auto(phase5_services):
    """A ticket's paid-now posts its cash receipt via the close backfill;
    a bank-method customer payment posts none."""
    svc = phase5_services
    assert _open_meter_day(svc, sale_litres="40") == 1_000_000
    svc.credit.create_customer("Ticket Customer")
    svc.credit.issue_ticket(
        "CUST-0001",
        DAY,
        [{
            "fuel_product_code": "PETROL",
            "nozzle_code": None,
            "quantity_litres": Decimal("40"),
        }],
        paid_now_paisa=300_000,  # 1,000,000 gross; 700,000 receivable
        occurred_at_utc=pkt_clock_utc_iso(DAY, 10),
    )
    # Bank recovery: old debt, no physical cash moves.
    svc.payments.record_payment(
        "CUST-0001",
        DAY,
        100_000,
        method="bank",
        occurred_at_utc=pkt_clock_utc_iso(DAY, 11),
    )
    gaps = svc.cash.missing_cash_postings(DAY)
    assert "paid_now_on_ticket" in {g["category"] for g in gaps}

    # Close auto-records the settlement (cash residual = 0) and backfills.
    svc.days.close_day(DAY, physical_cash_paisa=2_300_000)

    receipts = svc.cash.list_receipts(DAY)
    paid_now = [r for r in receipts if r.category == "paid_now_on_ticket"]
    assert [r.amount_paisa for r in paid_now] == [300_000]
    assert len(paid_now) == 1  # posted exactly once
    assert [r for r in receipts if r.category == "credit_recovery"] == []
    assert svc.cash.settlement_status(DAY)["identity_ok"] is True
    pos = svc.cash.cash_position(DAY)
    assert pos.receipts_paisa == 300_000
    assert pos.expected_paisa == 2_300_000
    assert pos.difference_paisa == 0


# ---------------------------------------------------------------------------
# trigger backstops: closed day + immutability
# ---------------------------------------------------------------------------


def _category_id(db, kind, name):
    return db.execute(
        "SELECT id FROM cash_categories WHERE kind = ? AND name = ?",
        (kind, name),
    ).fetchone()["id"]


def test_closed_day_trigger_backstops(phase5_services, db, db2):
    """Raw SQL inserts into the Phase-5 tables on a closed day are
    rejected with 'business day is closed' — no back door."""
    svc = phase5_services
    _open_meter_day(svc, sale_litres="40")
    svc.cash.record_settlements(
        DAY,
        cash_by_product={"PETROL": 1_000_000},
        occurred_at_utc=pkt_clock_utc_iso(DAY, 18),
    )
    svc.days.close_day(DAY, physical_cash_paisa=3_000_000)

    expense_cat = db.execute(
        "SELECT id FROM expense_categories WHERE name_en = 'Salaries'"
    ).fetchone()["id"]
    petrol_id = db.execute(
        "SELECT id FROM fuel_products WHERE code = 'PETROL'"
    ).fetchone()["id"]
    supplier_id = db.execute(
        "SELECT id FROM suppliers WHERE name = 'PSO Depot'"
    ).fetchone()["id"]
    stamp = pkt_clock_utc_iso(DAY, 20)

    attempts = [
        (
            "INSERT INTO expenses (uuid, business_date, category_id,"
            " amount_paisa, method, occurred_at_utc, created_by)"
            " VALUES (?, ?, ?, 100, 'cash', ?, 'test')",
            (new_uuid(), DAY, expense_cat, stamp),
        ),
        (
            "INSERT INTO cash_receipts (uuid, business_date, category_id,"
            " amount_paisa, occurred_at_utc, created_by)"
            " VALUES (?, ?, ?, 100, ?, 'test')",
            (new_uuid(), DAY, _category_id(db, "receipt", "other_receipt"), stamp),
        ),
        (
            "INSERT INTO cash_payments (uuid, business_date, category_id,"
            " amount_paisa, occurred_at_utc, created_by)"
            " VALUES (?, ?, ?, 100, ?, 'test')",
            (new_uuid(), DAY, _category_id(db, "payment", "other_payment"), stamp),
        ),
        (
            "INSERT INTO sales_settlements (uuid, business_date,"
            " fuel_product_id, category, amount_paisa, batch_uuid,"
            " occurred_at_utc, created_by)"
            " VALUES (?, ?, ?, 'cash', 100, ?, ?, 'test')",
            (new_uuid(), DAY, petrol_id, new_uuid(), stamp),
        ),
        (
            "INSERT INTO supplier_payments (uuid, supplier_id, business_date,"
            " amount_paisa, method, occurred_at_utc, created_by)"
            " VALUES (?, ?, ?, 100, 'cash', ?, 'test')",
            (new_uuid(), supplier_id, DAY, stamp),
        ),
    ]
    for sql, params in attempts:
        with pytest.raises(
            sqlite3.IntegrityError, match="business day is closed"
        ):
            db2.execute(sql, params)
        db2.rollback()
    # And the service layer refuses too.
    with pytest.raises(errors.ClosedDayError):
        svc.expenses.record_expense(
            business_date=DAY,
            category="Salaries",
            amount_paisa=100,
            occurred_at_utc=stamp,
        )


def test_posted_rows_immutable_via_sql(phase5_services, db, db2):
    """Posted rows cannot be edited or deleted, even by direct SQL; the
    whitelisted posted→reversed flip only ever runs with a reversal row."""
    svc = phase5_services
    _open_meter_day(svc, sale_litres="40")
    svc.cash.record_settlements(
        DAY,
        cash_by_product={"PETROL": 1_000_000},
        occurred_at_utc=pkt_clock_utc_iso(DAY, 18),
    )
    expense = svc.expenses.record_expense(
        business_date=DAY,
        category="Salaries",
        amount_paisa=100_000,
        method="cash",
        occurred_at_utc=pkt_clock_utc_iso(DAY, 19),
    )
    (receipt,) = [
        r for r in svc.cash.list_receipts(DAY) if r.category == "cash_sales"
    ]
    leg_id = db.execute(
        "SELECT id FROM sales_settlements WHERE business_date = ?", (DAY,)
    ).fetchone()["id"]

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db2.execute(
            "UPDATE expenses SET amount_paisa = 1 WHERE uuid = ?",
            (expense.uuid,),
        )
    db2.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db2.execute("DELETE FROM expenses WHERE uuid = ?", (expense.uuid,))
    db2.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db2.execute(
            "UPDATE cash_receipts SET amount_paisa = 1 WHERE uuid = ?",
            (receipt.uuid,),
        )
    db2.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db2.execute(
            "DELETE FROM cash_receipts WHERE uuid = ?", (receipt.uuid,)
        )
    db2.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db2.execute(
            "UPDATE sales_settlements SET notes = 'edited' WHERE id = ?",
            (leg_id,),
        )
    db2.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db2.execute("DELETE FROM sales_settlements WHERE id = ?", (leg_id,))
    db2.rollback()

    # A bare posted→reversed flip with no reversal row is rejected too.
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db2.execute(
            "UPDATE cash_receipts SET status = 'reversed' WHERE uuid = ?",
            (receipt.uuid,),
        )
    db2.rollback()

    # The supported path — a service reversal — flips alongside its row.
    svc.cash.reverse_cash_receipt(
        receipt.uuid, reason="duplicate entry", actor="manager"
    )
    rows = db.execute(
        "SELECT status FROM cash_receipts"
        " WHERE uuid = ? OR reverses_uuid = ?",
        (receipt.uuid, receipt.uuid),
    ).fetchall()
    assert sorted(r["status"] for r in rows) == ["posted", "reversed"]


# ---------------------------------------------------------------------------
# cash difference is never auto-written-off
# ---------------------------------------------------------------------------


def test_cash_difference_never_auto_written_off(phase5_services, db):
    """physical ≠ expected → the shortage is shown separately; the closing
    record keeps both figures and no adjustment row is auto-created."""
    svc = phase5_services
    _open_meter_day(svc, sale_litres="40")
    svc.cash.record_settlements(
        DAY,
        cash_by_product={"PETROL": 1_000_000},
        occurred_at_utc=pkt_clock_utc_iso(DAY, 18),
    )
    before_r = db.execute(
        "SELECT COUNT(*) FROM cash_receipts WHERE business_date = ?", (DAY,)
    ).fetchone()[0]
    before_p = db.execute(
        "SELECT COUNT(*) FROM cash_payments WHERE business_date = ?", (DAY,)
    ).fetchone()[0]

    # The physical count is 5,000 short of the 3,000,000 expected.
    svc.days.close_day(DAY, physical_cash_paisa=2_995_000)

    pos = svc.cash.cash_position(DAY)
    assert pos.expected_paisa == 3_000_000
    assert pos.physical_cash_paisa == 2_995_000
    assert pos.difference_paisa == -5_000
    assert pos.shortage_paisa == 5_000
    assert pos.surplus_paisa == 0
    # Closing created no adjustment: receipt/payment counts are unchanged.
    assert (
        db.execute(
            "SELECT COUNT(*) FROM cash_receipts WHERE business_date = ?",
            (DAY,),
        ).fetchone()[0]
        == before_r
    )
    assert (
        db.execute(
            "SELECT COUNT(*) FROM cash_payments WHERE business_date = ?",
            (DAY,),
        ).fetchone()[0]
        == before_p
    )
    # The closing record froze the day's P&L, cost and settlement snapshots.
    summary = json.loads(
        db.execute(
            "SELECT summary_json FROM business_day_closings"
            " WHERE business_date = ? ORDER BY closing_revision DESC LIMIT 1",
            (DAY,),
        ).fetchone()["summary_json"]
    )
    assert {"cost_snapshots", "pnl", "settlements"} <= set(summary)
    assert summary["settlements"]["identity_ok"] is True


# ---------------------------------------------------------------------------
# bank-method expenses
# ---------------------------------------------------------------------------


def test_bank_expense_never_touches_cash(phase5_services):
    """A bank-method expense posts no cash_payments row but still counts in
    expense totals and P&L operating expenses."""
    svc = phase5_services
    svc.days.open_day(DAY, opening_cash_paisa=2_000_000)
    expense = svc.expenses.record_expense(
        business_date=DAY,
        category="Utilities",
        amount_paisa=250_000,
        method="bank",
        occurred_at_utc=pkt_clock_utc_iso(DAY, 12),
    )
    assert expense.method == "bank"
    assert svc.cash.list_payments(DAY) == []
    totals = svc.expenses.expense_totals(DAY)
    assert totals["total_paisa"] == 250_000
    assert any(
        item["category_en"] == "Utilities" and item["total_paisa"] == 250_000
        for item in totals["by_category"]
    )
    pnl = svc.accounting.daily_pnl(DAY)
    assert pnl.operating_expenses_paisa == 250_000
    assert pnl.total_revenue_paisa == 0


# ---------------------------------------------------------------------------
# supplier payments
# ---------------------------------------------------------------------------


def _record_2000l_delivery(svc, day=DAY):
    """A 2,000 L petrol delivery at PKR 265.75/L → 53,150,000 paisa invoice."""
    return svc.deliveries.record_delivery(
        supplier="PSO Depot",
        fuel_product="PETROL",
        tank="TANK-PETROL",
        business_date=day,
        quantity_litres=Decimal("2000"),
        unit_cost_pkr=Decimal("265.75"),
        supplier_invoice_no="INV-001",
        occurred_at_utc=pkt_clock_utc_iso(day, 9),
    )


def test_supplier_payment_flows(phase5_services, db, db2):
    """Partial payment drops the invoice outstanding; overpaying is refused
    by the service and the trigger; supplier-level payments are allowed;
    reversal restores the outstanding."""
    svc = phase5_services
    _open_meter_day(svc, sale_litres="40")  # seeds the "PSO Depot" supplier
    delivery_id = _record_2000l_delivery(svc)
    invoice = 53_150_000
    assert svc.suppliers.delivery_outstanding(delivery_id) == invoice

    # Partial payment against the invoice → outstanding drops; one cash row.
    payment = svc.suppliers.record_supplier_payment(
        supplier="PSO Depot",
        business_date=DAY,
        amount_paisa=13_150_000,
        method="cash",
        delivery_id=delivery_id,
        occurred_at_utc=pkt_clock_utc_iso(DAY, 10),
    )
    assert svc.suppliers.delivery_outstanding(delivery_id) == 40_000_000
    cash_rows = [
        p
        for p in svc.cash.list_payments(DAY)
        if p.category == "supplier_payment"
    ]
    assert [p.amount_paisa for p in cash_rows] == [13_150_000]

    # Overpaying the invoice: the service refuses first…
    with pytest.raises(errors.BusinessRuleError, match="exceeds"):
        svc.suppliers.record_supplier_payment(
            supplier="PSO Depot",
            business_date=DAY,
            amount_paisa=40_000_001,
            method="cash",
            delivery_id=delivery_id,
            occurred_at_utc=pkt_clock_utc_iso(DAY, 11),
        )
    # …and the trigger backstops raw SQL.
    supplier_id = db.execute(
        "SELECT id FROM suppliers WHERE name = 'PSO Depot'"
    ).fetchone()["id"]
    with pytest.raises(
        sqlite3.IntegrityError, match="exceeds the delivery outstanding"
    ):
        db2.execute(
            "INSERT INTO supplier_payments"
            " (uuid, supplier_id, delivery_id, business_date,"
            "  amount_paisa, method, occurred_at_utc, created_by)"
            " VALUES (?, ?, ?, ?, 40000001, 'cash', ?, 'test')",
            (
                new_uuid(),
                supplier_id,
                delivery_id,
                DAY,
                pkt_clock_utc_iso(DAY, 11),
            ),
        )
    db2.rollback()

    # A supplier-level (unallocated) payment is allowed and does not touch
    # the invoice outstanding.
    svc.suppliers.record_supplier_payment(
        supplier="PSO Depot",
        business_date=DAY,
        amount_paisa=1_000_000,
        method="cash",
        occurred_at_utc=pkt_clock_utc_iso(DAY, 12),
    )
    assert svc.suppliers.delivery_outstanding(delivery_id) == 40_000_000
    # (The day setup seeds "PSO Depot" twice; key by the delivery's own
    # supplier id so the right balance row is asserted.)
    supplier_id = db.execute(
        "SELECT supplier_id FROM fuel_deliveries WHERE id = ?",
        (delivery_id,),
    ).fetchone()["supplier_id"]
    balances = {b.supplier_id: b for b in svc.suppliers.supplier_balances()}
    assert (
        balances[supplier_id].outstanding_paisa
        == invoice - 13_150_000 - 1_000_000
    )

    # Reversing the invoice payment restores its outstanding (and reverses
    # the derived cash row — the supplier-level payment's cash stands).
    svc.suppliers.reverse_supplier_payment(
        payment.uuid, reason="entered twice", actor="manager"
    )
    assert svc.suppliers.delivery_outstanding(delivery_id) == invoice
    cash_net = db.execute(
        "SELECT COALESCE(SUM(signed_paisa), 0) FROM v_cashbook"
        " WHERE business_date = ? AND category = 'supplier_payment'",
        (DAY,),
    ).fetchone()[0]
    # Payments are negative in the book: the reversed invoice payment nets to
    # 0 and only the supplier-level payment's −1,000,000 stands.
    assert int(cash_net) == -1_000_000
