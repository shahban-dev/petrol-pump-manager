"""Phase-4 service tests: customers, credit tickets, payments, FIFO allocations.

Covers the task contract's CustomerCreditService / PaymentService surface::

    CustomerCreditService.create_customer / update_customer / get_customer /
        adjust_opening_balance / issue_ticket / reverse_ticket /
        ticket_details / day_tickets / outstanding / outstanding_all /
        customer_ledger / statement / ticket_integrity / allocation_order
    PaymentService.record_payment / allocate / reallocate / reverse_payment /
        payment_details / day_payments / customer_payments / receipt_data /
        ticket_unpaid / allocation_order / set_allocation_order

plus the Group C/D/G trigger backstops (raw SQL through ``db2``), the
close-checklist credit items, the metered-vs-credit liters guard, and the
spec §7 worked example (Tests 5–7: 40 L @ PKR 270 → 10,800 gross, paid-now
3,000 → 7,800 receivable; 2,000 recovery → 5,800).

Design plan: 00_design_plan.md §5 (numeric standards), §6 (reconciliation),
§7 (customer-credit model), §11 assumptions 4/5/7/8/13, acceptance tests
5–7 and 12, trigger groups C/D/G.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from app.services import errors

from .conftest import (
    close_prior_day,
    phase3_services,  # noqa: F401  (fixture)
    phase4_services,  # noqa: F401  (fixture)
    pkt_clock_utc_iso,
    pkt_midnight_utc_iso,
    record_reset_interval,
    seed_credit_day,
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

PRICE_270 = Decimal("270")  # PKR/L — the spec's worked-example price

#: 40 L of petrol through nozzle "1 PG+".
LINE_40L = {
    "fuel_product_code": "PETROL",
    "nozzle_code": "1 PG+",
    "quantity_litres": Decimal("40"),
}
#: 20 L of petrol, no nozzle attribution.
LINE_20L = {
    "fuel_product_code": "PETROL",
    "nozzle_code": None,
    "quantity_litres": Decimal("20"),
}

GROSS_40L = 1_080_000  # 40 L @ 270 = PKR 10,800.00
GROSS_20L = 540_000  # 20 L @ 270 = PKR 5,400.00


def _issue(svc, day=DAY, **overrides):
    """Issue the standard 40 L ticket, overridable per test."""
    customer = overrides.pop("customer", "CUST-0001")
    lines = overrides.pop("lines", [dict(LINE_40L)])
    occurred = overrides.pop("occurred_at_utc", pkt_clock_utc_iso(day, 10))
    return svc.credit.issue_ticket(
        customer, day, lines, occurred_at_utc=occurred, **overrides
    )


def _pay(svc, amount_paisa, day=DAY, **overrides):
    """Record a payment for CUST-0001."""
    occurred = overrides.pop("occurred_at_utc", pkt_clock_utc_iso(day, 12))
    return svc.payments.record_payment(
        "CUST-0001", day, amount_paisa, occurred_at_utc=occurred, **overrides
    )


# ---------------------------------------------------------------------------
# customers
# ---------------------------------------------------------------------------


def test_create_customer_autocodes(phase4_services):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)  # creates CUST-0001
    first = svc.credit.create_customer("Alpha Traders")
    second = svc.credit.create_customer(
        "Beta Goods", opening_receivable_paisa=150_050
    )
    assert first.customer_code == "CUST-0002"
    assert second.customer_code == "CUST-0003"
    assert first.outstanding_paisa == 0
    assert second.opening_receivable_paisa == 150_050
    assert second.outstanding_paisa == 150_050


def test_create_customer_explicit_code_and_duplicate(phase4_services):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    svc.credit.create_customer("Named", customer_code="ACME-1")
    with pytest.raises(errors.DuplicateError):
        svc.credit.create_customer("Clone", customer_code="ACME-1")
    # A non-matching existing code does not disturb the CUST- sequence
    # (seed_credit_day already took CUST-0001).
    nxt = svc.credit.create_customer("Next")
    assert nxt.customer_code == "CUST-0002"


def test_create_customer_validation(phase4_services):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    with pytest.raises(errors.ValidationError):
        svc.credit.create_customer("   ")
    with pytest.raises(errors.ValidationError):
        svc.credit.create_customer("Neg", opening_receivable_paisa=-1)


def test_update_customer(phase4_services):
    svc = phase4_services
    cid = seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    updated = svc.credit.update_customer(
        cid,
        phone="0300-1234567",
        credit_limit_paisa=5_000_000,
        notes="good payer",
    )
    assert updated.phone == "0300-1234567"
    assert updated.credit_limit_paisa == 5_000_000
    # None = no change.
    again = svc.credit.update_customer(cid)
    assert again.phone == "0300-1234567"
    assert again.name == "Test Customer"
    cleared = svc.credit.update_customer(cid, clear_credit_limit=True)
    assert cleared.credit_limit_paisa is None
    with pytest.raises(errors.ValidationError):
        svc.credit.update_customer(cid, status="archived")
    inactive = svc.credit.update_customer(cid, status="inactive")
    assert inactive.status == "inactive"
    with pytest.raises(errors.NotFoundError):
        svc.credit.update_customer(9999, phone="x")


def test_adjust_opening_balance(phase4_services, db):
    svc = phase4_services
    cid = seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    adj = svc.credit.adjust_opening_balance(
        cid, 500_000, "opening understated at setup", DAY,
        authorized_by="manager",
        occurred_at_utc=pkt_clock_utc_iso(DAY, 10),
    )
    assert adj.adjustment_paisa == 500_000
    assert svc.credit.outstanding(cid) == 500_000
    svc.credit.adjust_opening_balance(
        cid, -200_050, "double-counted invoice", DAY,
        occurred_at_utc=pkt_clock_utc_iso(DAY, 11),
    )
    assert svc.credit.outstanding(cid) == 299_950
    with pytest.raises(errors.ValidationError):
        svc.credit.adjust_opening_balance(cid, 0, "no-op", DAY)
    with pytest.raises(errors.ValidationError):
        svc.credit.adjust_opening_balance(cid, 1000, "  ", DAY)
    rows = db.execute(
        "SELECT COUNT(*) FROM audit_log"
        " WHERE table_name = 'customer_opening_adjustments'"
    ).fetchone()
    assert int(rows[0]) == 2  # both adjustments audited


# ---------------------------------------------------------------------------
# ticket issuing
# ---------------------------------------------------------------------------


def test_issue_ticket_math(phase4_services):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    ticket = _issue(svc)
    assert ticket.gross_amount_paisa == GROSS_40L
    assert ticket.paid_now_paisa == 0
    assert ticket.unpaid_paisa == GROSS_40L
    assert ticket.receipt_no == "T-20261015-001"
    assert ticket.status == "posted"
    assert ticket.warnings == ()
    (line,) = ticket.lines
    assert line.quantity_ml == 40_000
    assert line.unit_price_10k == 2_700_000
    assert line.line_amount_paisa == GROSS_40L
    assert line.nozzle_code == "1 PG+"
    assert line.fuel_product_code == "PETROL"


def test_issue_ticket_receipt_sequencing(phase4_services):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    first = _issue(svc)
    second = _issue(
        svc, occurred_at_utc=pkt_clock_utc_iso(DAY, 11), lines=[dict(LINE_20L)]
    )
    assert first.receipt_no == "T-20261015-001"
    assert second.receipt_no == "T-20261015-002"
    assert second.gross_amount_paisa == GROSS_20L


def test_issue_ticket_paid_now(phase4_services):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    ticket = _issue(svc, paid_now_paisa=300_000)
    assert ticket.paid_now_paisa == 300_000
    assert ticket.unpaid_paisa == GROSS_40L - 300_000
    assert svc.credit.outstanding("CUST-0001") == GROSS_40L - 300_000
    with pytest.raises(errors.ValidationError):
        _issue(svc, paid_now_paisa=2_000_000)  # > gross


def test_issue_ticket_needs_price(phase4_services):
    svc = phase4_services
    # A day with no price history at all.
    svc.days.open_day("2026-10-16", opening_cash_paisa=2_000_000)
    svc.credit.create_customer("Priceless")
    with pytest.raises(errors.NotFoundError):
        svc.credit.issue_ticket(
            "CUST-0001", "2026-10-16", [dict(LINE_40L)],
            occurred_at_utc=pkt_clock_utc_iso("2026-10-16", 10),
            )


def test_issue_ticket_line_validation(phase4_services):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    stamp = pkt_clock_utc_iso(DAY, 10)
    # Nozzle dispenses a different product than the line.
    with pytest.raises(errors.ValidationError):
        svc.credit.issue_ticket(
            customer="CUST-0001",
            business_date=DAY,
            lines=[{
                "fuel_product_code": "DIESEL",
                "nozzle_code": "1 PG+",
                "quantity_litres": Decimal("10"),
            }],
            occurred_at_utc=stamp,
        )
    with pytest.raises(errors.NotFoundError):
        svc.credit.issue_ticket(
            customer="CUST-0001",
            business_date=DAY,
            lines=[{
                "fuel_product_code": "PETROL",
                "nozzle_code": "9 XYZ",
                "quantity_litres": Decimal("10"),
            }],
            occurred_at_utc=stamp,
        )
    with pytest.raises(errors.ValidationError):
        svc.credit.issue_ticket(
            customer="CUST-0001",
            business_date=DAY,
            lines=[{
                "fuel_product_code": "PETROL",
                "nozzle_code": None,
                "quantity_litres": Decimal("0"),
            }],
            occurred_at_utc=stamp,
        )
    with pytest.raises(errors.NotFoundError):
        svc.credit.issue_ticket(
            "GHOST", DAY, [dict(LINE_40L)],
            occurred_at_utc=stamp,
            )
    with pytest.raises(errors.ValidationError):
        _issue(svc, lines=[])


def test_issue_ticket_inactive_customer(phase4_services):
    svc = phase4_services
    cid = seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    svc.credit.update_customer(cid, status="inactive")
    with pytest.raises(errors.BusinessRuleError):
        _issue(svc)


def test_credit_limit_warns_not_blocks(phase4_services):
    svc = phase4_services
    cid = seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    svc.credit.update_customer(cid, credit_limit_paisa=500_000)
    ticket = _issue(svc)  # 10,800 > 5,000 limit — must still issue
    assert len(ticket.warnings) == 1
    assert "Credit limit exceeded" in ticket.warnings[0]
    assert ticket.gross_amount_paisa == GROSS_40L
    assert svc.credit.outstanding(cid) == GROSS_40L


def test_issue_ticket_writes_no_stock(phase4_services, db):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    before = db.execute("SELECT COUNT(*) FROM inventory_movements").fetchone()
    _issue(svc)
    after = db.execute("SELECT COUNT(*) FROM inventory_movements").fetchone()
    assert int(after[0]) == int(before[0])  # a ticket classifies metered
    # sales; it never moves stock.


# ---------------------------------------------------------------------------
# the spec §7 worked example (acceptance Tests 5–7)
# ---------------------------------------------------------------------------


def test_outstanding_worked_example(phase4_services):
    """40 L @ 270 → 10,800 gross; paid-now 3,000 → 7,800 receivable;
    2,000 recovery → 5,800. Revenue is untouched by the recovery."""
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    ticket = _issue(svc, paid_now_paisa=300_000)
    assert svc.credit.outstanding("CUST-0001") == 780_000
    payment = _pay(svc, 200_000)  # auto-allocates FIFO to the ticket
    assert payment.unallocated_paisa == 0
    assert svc.credit.outstanding("CUST-0001") == 580_000
    assert svc.payments.ticket_unpaid(ticket.uuid) == 580_000
    # The recovery is a receipt, not revenue: the ticket gross is unchanged.
    assert svc.credit.ticket_details(ticket.id).gross_amount_paisa == GROSS_40L


# ---------------------------------------------------------------------------
# payments & FIFO allocation
# ---------------------------------------------------------------------------


def test_record_payment_receipt_and_validation(phase4_services):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    first = _pay(svc, 200_000)
    second = _pay(svc, 100_000, method="bank")
    assert first.receipt_no == "R-20261015-001"
    assert second.receipt_no == "R-20261015-002"
    assert first.method == "cash"
    assert second.unallocated_paisa == 100_000  # no tickets: nothing to take
    with pytest.raises(errors.ValidationError):
        _pay(svc, 0)
    with pytest.raises(errors.ValidationError):
        _pay(svc, -5)
    with pytest.raises(errors.ValidationError):
        _pay(svc, 100, method="cheque")
    with pytest.raises(errors.ValidationError):
        _pay(svc, 100.5)  # float paisa is never accepted


def test_fifo_allocation_oldest_first(phase4_services):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    older = _issue(svc, occurred_at_utc=pkt_clock_utc_iso(DAY, 9))
    newer = _issue(
        svc,
        occurred_at_utc=pkt_clock_utc_iso(DAY, 11),
        lines=[dict(LINE_20L)],
    )
    payment = _pay(svc, 1_200_000)  # 12,000 covers older fully, newer partly
    allocs = {a.ticket_uuid: a.effective_paisa for a in payment.allocations}
    assert allocs[older.uuid] == GROSS_40L
    assert allocs[newer.uuid] == 1_200_000 - GROSS_40L
    assert payment.unallocated_paisa == 0
    assert svc.payments.ticket_unpaid(older.uuid) == 0
    assert svc.payments.ticket_unpaid(newer.uuid) == (
        GROSS_20L - (1_200_000 - GROSS_40L)
    )


def test_paid_now_stays_on_its_ticket(phase4_services):
    """§11.4: money handed over at the sale settles that ticket only —
    FIFO recovery never re-touches the paid-now portion."""
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    first = _issue(svc, paid_now_paisa=300_000)  # unpaid 780,000
    second = _issue(
        svc,
        occurred_at_utc=pkt_clock_utc_iso(DAY, 11),
        lines=[dict(LINE_20L)],
    )  # unpaid 540,000
    payment = _pay(svc, 1_000_000)
    allocs = {a.ticket_uuid: a.effective_paisa for a in payment.allocations}
    assert allocs[first.uuid] == 780_000
    assert allocs[second.uuid] == 220_000
    assert svc.payments.ticket_unpaid(first.uuid) == 0


def test_manual_allocation_order(phase4_services):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    assert svc.payments.allocation_order() == "fifo"  # the §11.4 default
    svc.payments.set_allocation_order("manual")
    assert svc.payments.allocation_order() == "manual"
    _issue(svc)
    payment = _pay(svc, 200_000, auto_allocate=True)
    assert payment.allocations == ()  # manual: the manager picks
    assert payment.unallocated_paisa == 200_000
    with pytest.raises(errors.ValidationError):
        svc.payments.set_allocation_order("lifo")


def test_allocate_none_means_fifo(phase4_services):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    svc.payments.set_allocation_order("manual")  # explicit None still → FIFO
    older = _issue(svc, occurred_at_utc=pkt_clock_utc_iso(DAY, 9))
    _issue(svc, occurred_at_utc=pkt_clock_utc_iso(DAY, 11),
           lines=[dict(LINE_20L)])
    payment = _pay(svc, 1_200_000, auto_allocate=False)
    done = svc.payments.allocate(
        payment.id, None, occurred_at_utc=pkt_clock_utc_iso(DAY, 13)
    )
    allocs = {a.ticket_uuid: a.effective_paisa for a in done.allocations}
    assert allocs[older.uuid] == GROSS_40L  # oldest first even in manual mode
    assert done.unallocated_paisa == 0


def test_explicit_splits_and_atomic_over_allocation(phase4_services):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    svc.payments.set_allocation_order("manual")
    first = _issue(svc)
    second = _issue(
        svc,
        occurred_at_utc=pkt_clock_utc_iso(DAY, 11),
        lines=[dict(LINE_20L)],
    )
    payment = _pay(svc, 500_000, auto_allocate=False)
    # Over-allocating aborts atomically: nothing is persisted.
    with pytest.raises(errors.ValidationError):
        svc.payments.allocate(
            payment.id,
            [
                {"ticket_uuid": first.uuid, "amount_paisa": 400_000},
                {"ticket_uuid": second.uuid, "amount_paisa": 200_000},
            ],
            occurred_at_utc=pkt_clock_utc_iso(DAY, 13),
        )
    fresh = svc.payments.payment_details(payment.id)
    assert fresh.allocations == ()
    assert fresh.unallocated_paisa == 500_000
    # A valid split plan commits.
    done = svc.payments.allocate(
        payment.id,
        [
            {"ticket_uuid": first.uuid, "amount_paisa": 400_000},
            {"ticket_uuid": second.uuid, "amount_paisa": 100_000},
        ],
        occurred_at_utc=pkt_clock_utc_iso(DAY, 13),
    )
    assert done.unallocated_paisa == 0
    assert svc.payments.ticket_unpaid(first.uuid) == GROSS_40L - 400_000


def test_allocate_validates_splits(phase4_services):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    other = svc.credit.create_customer("Other")  # different customer
    ticket = _issue(svc)
    other_ticket = svc.credit.issue_ticket(
        other.customer_code, DAY, [dict(LINE_40L)],
        occurred_at_utc=pkt_clock_utc_iso(DAY, 10),
        )
    payment = _pay(svc, 500_000, auto_allocate=False)
    with pytest.raises(errors.ValidationError):  # other customer's ticket
        svc.payments.allocate(
            payment.id,
            [{"ticket_uuid": other_ticket.uuid, "amount_paisa": 100_000}],
            occurred_at_utc=pkt_clock_utc_iso(DAY, 13),
        )
    with pytest.raises(errors.NotFoundError):  # unknown ticket
        svc.payments.allocate(
            payment.id,
            [{"ticket_uuid": "nope", "amount_paisa": 100_000}],
            occurred_at_utc=pkt_clock_utc_iso(DAY, 13),
        )
    with pytest.raises(errors.ValidationError):  # non-positive amount
        svc.payments.allocate(
            payment.id,
            [{"ticket_uuid": ticket.uuid, "amount_paisa": 0}],
            occurred_at_utc=pkt_clock_utc_iso(DAY, 13),
        )
    with pytest.raises(errors.ValidationError):  # duplicate ticket
        svc.payments.allocate(
            payment.id,
            [
                {"ticket_uuid": ticket.uuid, "amount_paisa": 100_000},
                {"ticket_uuid": ticket.uuid, "amount_paisa": 100_000},
            ],
            occurred_at_utc=pkt_clock_utc_iso(DAY, 13),
        )


def test_overallocation_trigger_backstop(phase4_services, db2):
    """Raw SQL that over-allocates is rejected by the Group G trigger —
    the database guards even writers that bypass the service."""
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    ticket = _issue(svc)
    payment = _pay(svc, 500_000, auto_allocate=False)
    pay_id = payment.id
    with pytest.raises(sqlite3.IntegrityError, match="payment amount"):
        db2.execute(
            "INSERT INTO payment_allocations"
            " (payment_id, credit_sale_id, amount_paisa, business_date)"
            " VALUES (?, ?, ?, ?)",
            (pay_id, ticket.id, 600_000, DAY),
        )
    with pytest.raises(sqlite3.IntegrityError, match="unpaid balance"):
        db2.execute(
            "INSERT INTO payment_allocations"
            " (payment_id, credit_sale_id, amount_paisa, business_date)"
            " VALUES (?, ?, ?, ?)",
            (pay_id, ticket.id, GROSS_40L + 1, DAY),
        )


def test_reallocate(phase4_services, db):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    first = _issue(svc)
    second = _issue(
        svc,
        occurred_at_utc=pkt_clock_utc_iso(DAY, 11),
        lines=[dict(LINE_20L)],
    )
    payment = _pay(svc, 500_000)  # FIFO: all 500k to the first ticket
    (alloc,) = payment.allocations
    assert alloc.ticket_uuid == first.uuid
    moved = svc.payments.reallocate(
        alloc.id,
        [{"ticket_uuid": second.uuid, "amount_paisa": 200_000}],
        "manager correction",
        DAY,
    )
    eff = {a.ticket_uuid: a.effective_paisa for a in moved.allocations}
    assert eff[first.uuid] == 0  # fully reversed
    assert eff[second.uuid] == 200_000
    assert moved.unallocated_paisa == 300_000
    assert svc.payments.ticket_unpaid(first.uuid) == GROSS_40L
    assert svc.payments.ticket_unpaid(second.uuid) == GROSS_20L - 200_000
    reversals = db.execute(
        "SELECT COUNT(*) FROM payment_allocation_reversals"
    ).fetchone()
    assert int(reversals[0]) == 1
    with pytest.raises(errors.ValidationError):
        svc.payments.reallocate(alloc.id, [], "  ", DAY)  # reason required


def test_reverse_ticket(phase4_services):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    ticket = _issue(svc, paid_now_paisa=300_000)
    payment = _pay(svc, 200_000)  # allocated to the ticket
    assert svc.credit.outstanding("CUST-0001") == 580_000
    reversal = svc.credit.reverse_ticket(
        ticket.uuid, "wrong customer billed", DAY
    )
    assert reversal.reverses_uuid == ticket.uuid
    assert reversal.paid_now_paisa == 0
    assert len(reversal.lines) == 1
    assert reversal.lines[0].line_amount_paisa == GROSS_40L
    original = svc.credit.ticket_details(ticket.uuid)
    assert original.status == "reversed"
    assert original.unpaid_paisa == 0
    # The sale is void: the 300,000 paid-now cash stays with the station as
    # customer credit, and the 200,000 payment is unallocated again.
    assert svc.credit.outstanding("CUST-0001") == -300_000
    assert svc.payments.payment_details(payment.uuid).unallocated_paisa == (
        200_000
    )
    assert svc.credit.ticket_integrity(reversal.id) == []
    with pytest.raises(errors.BusinessRuleError):
        svc.credit.reverse_ticket(ticket.uuid, "again", DAY)


def test_reverse_ticket_void_without_cash(phase4_services):
    """Voiding a ticket with no paid-now and no payments zeroes the
    receivable exactly."""
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    ticket = _issue(svc)
    assert svc.credit.outstanding("CUST-0001") == GROSS_40L
    svc.credit.reverse_ticket(ticket.uuid, "entered twice", DAY)
    assert svc.credit.outstanding("CUST-0001") == 0
    entries = svc.credit.customer_ledger("CUST-0001")
    assert entries[-1].running_paisa == 0


def test_reverse_ticket_closed_day_rules(phase4_services):
    svc = phase4_services
    close_prior_day(
        svc, PREV, PREV_READINGS, opening_stock_litres=Decimal("5000")
    )
    seed_phase3_day(svc, DAY)
    svc.credit.create_customer("Day Customer")
    ticket = svc.credit.issue_ticket(
        "CUST-0001", DAY, [dict(LINE_40L)],
        occurred_at_utc=pkt_clock_utc_iso(DAY, 10),
        )
    svc.nozzles.record_reading(DAY, "1 PG+", "84985")  # 47 L sale
    record_reset_interval(svc, DAY, "2 PG+", PREV_READINGS["2 PG+"])
    record_reset_interval(svc, DAY, "3 HSD", PREV_READINGS["3 HSD"])
    record_reset_interval(svc, DAY, "4 HSD", PREV_READINGS["4 HSD"])
    svc.sales.post_day_sales(DAY)
    svc.days.close_day(DAY, physical_cash_paisa=2_000_000)
    seed_phase3_day(svc, "2026-10-16")
    # Reversal onto an open day, but the ORIGINAL day is closed → blocked.
    with pytest.raises(errors.ClosedDayError):
        svc.credit.reverse_ticket(ticket.uuid, "late fix", "2026-10-16")
    # Reversal onto the closed day itself → blocked too.
    with pytest.raises(errors.ClosedDayError):
        svc.credit.reverse_ticket(ticket.uuid, "late fix", DAY)


def test_reverse_payment(phase4_services):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    ticket = _issue(svc)
    payment = _pay(svc, 500_000)
    assert svc.credit.outstanding("CUST-0001") == GROSS_40L - 500_000
    reversal = svc.payments.reverse_payment(
        payment.uuid, "cashier error", DAY
    )
    assert reversal.reverses_uuid == payment.uuid
    assert reversal.amount_paisa == 500_000
    original = svc.payments.payment_details(payment.uuid)
    assert original.status == "reversed"
    # The allocation was reversed first: the ticket is unpaid again.
    assert svc.credit.outstanding("CUST-0001") == GROSS_40L
    assert svc.payments.ticket_unpaid(ticket.uuid) == GROSS_40L
    with pytest.raises(errors.BusinessRuleError):
        svc.payments.reverse_payment(payment.uuid, "again", DAY)


def test_allocate_to_reversed_ticket_blocked(phase4_services):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    ticket = _issue(svc)
    svc.credit.reverse_ticket(ticket.uuid, "void", DAY)
    payment = _pay(svc, 100_000, auto_allocate=False)
    with pytest.raises(errors.BusinessRuleError):
        svc.payments.allocate(
            payment.id,
            [{"ticket_uuid": ticket.uuid, "amount_paisa": 50_000}],
            occurred_at_utc=pkt_clock_utc_iso(DAY, 13),
        )


# ---------------------------------------------------------------------------
# ledger, statements, integrity
# ---------------------------------------------------------------------------


def test_ticket_integrity_clean(phase4_services):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    ticket = _issue(svc, paid_now_paisa=100_000)
    assert svc.credit.ticket_integrity(ticket.id) == []
    assert svc.credit.ticket_integrity(ticket.uuid) == []


def test_customer_ledger_running_balance(phase4_services):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    _issue(svc, paid_now_paisa=300_000)
    _pay(svc, 200_000)
    entries = svc.credit.customer_ledger("CUST-0001")
    # The opening entry sorts by the customer's creation time, so only the
    # multiset is asserted — but the final running balance must agree with
    # the authoritative outstanding balance.
    assert sorted(e.entry_type for e in entries) == [
        "opening",
        "paid_now",
        "payment",
        "ticket",
    ]
    assert entries[-1].running_paisa == 580_000
    assert entries[-1].running_paisa == svc.credit.outstanding("CUST-0001")


def test_statement_date_filter(phase4_services):
    svc = phase4_services
    day2 = "2026-10-16"
    close_prior_day(
        svc, PREV, PREV_READINGS, opening_stock_litres=Decimal("5000"),
        price_pkr=PRICE_270,
    )
    seed_phase3_day(svc, DAY)
    svc.credit.create_customer("Stmt")
    _issue(svc, paid_now_paisa=300_000)  # day 1: unpaid 780,000
    svc.nozzles.record_reading(DAY, "1 PG+", "84985")  # 47 L sale
    record_reset_interval(svc, DAY, "2 PG+", PREV_READINGS["2 PG+"])
    record_reset_interval(svc, DAY, "3 HSD", PREV_READINGS["3 HSD"])
    record_reset_interval(svc, DAY, "4 HSD", PREV_READINGS["4 HSD"])
    svc.sales.post_day_sales(DAY)
    svc.days.close_day(DAY, physical_cash_paisa=2_000_000)
    seed_phase3_day(svc, day2)
    _issue(svc, day=day2, occurred_at_utc=pkt_clock_utc_iso(day2, 10))
    full = svc.credit.statement("CUST-0001")
    assert full.closing_paisa == 780_000 + GROSS_40L
    day2_only = svc.credit.statement("CUST-0001", date_from=day2, date_to=day2)
    assert day2_only.opening_paisa == 780_000
    assert day2_only.debit_paisa == GROSS_40L
    assert day2_only.closing_paisa == 780_000 + GROSS_40L
    with pytest.raises(errors.ValidationError):
        svc.credit.statement("CUST-0001", date_from="not-a-date")


def test_receipt_data(phase4_services):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    ticket = _issue(svc)
    payment = _pay(svc, 500_000)
    receipt = svc.payments.receipt_data(payment.id)
    assert receipt["payment"]["receipt_no"] == "R-20261015-001"
    assert receipt["customer"]["customer_code"] == "CUST-0001"
    assert receipt["customer"]["outstanding_paisa"] == GROSS_40L - 500_000
    assert receipt["allocations"][0]["ticket_uuid"] == ticket.uuid
    assert receipt["allocations"][0]["effective_paisa"] == 500_000
    assert receipt["unallocated_paisa"] == 0
    assert receipt["generated_at_utc"].endswith("Z")


def test_outstanding_all(phase4_services):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    svc.credit.create_customer("Second")
    _issue(svc)
    balances = {
        c.customer_code: c.outstanding_paisa
        for c in svc.credit.outstanding_all()
    }
    assert balances["CUST-0001"] == GROSS_40L
    assert balances["CUST-0002"] == 0


def test_immutability_triggers_raw_sql(phase4_services, db2):
    """Posted rows cannot be edited or deleted, even by direct SQL —
    corrections go through reversal rows."""
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    ticket = _issue(svc)
    payment = _pay(svc, 100_000, auto_allocate=False)
    alloc_id = svc.payments.allocate(
        payment.id,
        [{"ticket_uuid": ticket.uuid, "amount_paisa": 100_000}],
        occurred_at_utc=pkt_clock_utc_iso(DAY, 13),
    ).allocations[0].id
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db2.execute(
            "UPDATE credit_sales SET notes = 'edited' WHERE id = ?",
            (ticket.id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db2.execute("DELETE FROM credit_sales WHERE id = ?", (ticket.id,))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db2.execute(
            "UPDATE payment_allocations SET amount_paisa = 1 WHERE id = ?",
            (alloc_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db2.execute(
            "DELETE FROM payment_allocations WHERE id = ?", (alloc_id,)
        )


# ---------------------------------------------------------------------------
# close checklist, guard, reconciliation
# ---------------------------------------------------------------------------


def _check_item(report, key):
    return [i for i in report.items if i.key == key]


def test_close_checklist_credit_items(phase4_services):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    _issue(svc, paid_now_paisa=300_000)
    _pay(svc, 200_000)
    report = svc.days.validate_close(DAY)
    tickets = _check_item(report, "credit_tickets_saved")
    assert any(i.status == "pass" for i in tickets)
    # 40 L of credit against no metered sales: a warning, never a block.
    warnings = [i for i in tickets if i.status == "warning"]
    assert warnings and "PETROL" in warnings[0].detail
    allocs = _check_item(report, "allocations_valid")
    assert any(i.status == "pass" for i in allocs)
    assert not [i for i in allocs if i.status == "fail"]
    # The Phase-4 checks are no longer "not applicable".
    assert "credit_tickets_saved" not in [
        i.key for i in report.items if i.status == "na"
    ]
    assert "allocations_valid" not in [
        i.key for i in report.items if i.status == "na"
    ]


def test_credit_liters_guard_and_reconcile(phase4_services):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    _issue(svc, paid_now_paisa=300_000)
    guard = svc.sales.credit_liters_guard(DAY)
    assert guard["checked"] is True
    assert len(guard["warnings"]) == 1
    assert "PETROL" in guard["warnings"][0]
    recon = svc.sales.reconcile_day(DAY)
    assert recon["new_receivables_paisa"] == GROSS_40L - 300_000
    assert "reversal" in recon["new_receivables_note"]


def test_audit_trail_for_ticket_and_reversal(phase4_services, db):
    svc = phase4_services
    seed_credit_day(svc, DAY, price_pkr=PRICE_270)
    ticket = _issue(svc)
    before = db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    svc.credit.reverse_ticket(ticket.uuid, "void", DAY)
    rows = db.execute(
        "SELECT table_name, action FROM audit_log WHERE id > ? ORDER BY id",
        (before,),
    ).fetchall()
    tables = {(r[0], r[1]) for r in rows}
    assert ("credit_sales", "INSERT") in tables  # the reversal row
    assert ("credit_sales", "UPDATE") in tables  # the posted → reversed flip
    assert ("credit_sale_lines", "INSERT") in tables  # mirrored lines
