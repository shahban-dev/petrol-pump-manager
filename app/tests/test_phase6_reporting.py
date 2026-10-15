"""Phase-6 reporting service tests.

Every test builds a KNOWN fixture scenario in a fresh temp database (via
the ``phase5_services`` fixture) and asserts each report's totals tie
exactly to the underlying ledger — cashbook vs ``v_daily_cash_position``,
fuel sales vs nozzle intervals, statement vs the §7 ledger formula, the §6
reconciliation identity, and frozen P&L snapshots. No floats may appear in
any report value.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app import config
from app.database.connection import DatabaseManager
from app.models.report import REPORT_IDS
from app.services import errors
from app.services.reporting_service import ReportingService

from .conftest import (
    DEFAULT_PRICE_PKR,
    fuel_product_id,
    pkt_clock_utc_iso,
    pkt_midnight_utc_iso,
)


@pytest.fixture(autouse=True)
def _temp_backup_dir(monkeypatch, tmp_path):
    """Redirect the *default* backup dir into tmp.

    The day-close auto-backup hook constructs its ``BackupService``
    without an explicit ``backup_dir``; the database it backs up is
    already the temp DB, and this keeps the hook's *files* in tmp too.
    """
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")

D0 = "2026-10-13"
D1 = "2026-10-14"
D2 = "2026-10-15"

PETROL_PRICE = Decimal("265.9630")
DIESEL_PRICE = Decimal("260.0000")

# Hand-computed fixture constants (see module docstring).
METER_PETROL_ML = 800_000
METER_DIESEL_ML = 300_000
METER_PETROL_PAISA = 21_277_040  # 800 L @ 265.9630
METER_DIESEL_PAISA = 7_800_000  # 300 L @ 260.0000
TICKET_GROSS_PAISA = 2_659_630  # 100 L @ 265.9630
TICKET_PAID_NOW_PAISA = 500_000
TICKET_NET_PAISA = 2_159_630
PAYMENT_PAISA = 1_000_000
TICKET_UNPAID_AFTER_PAYMENT = 1_159_630
EXPENSE_PAISA = 1_000_000
EXPECTED_CASH_PAISA = 27_627_410  # opening 1,000,000 + receipts − payments
PETROL_COGS_PAISA = 19_466_667
DIESEL_COGS_PAISA = 7_200_000

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _stamp(day: str, hour: int, minute: int = 0) -> str:
    return pkt_clock_utc_iso(day, hour, minute)


@pytest.fixture
def reporting(db, db_file):
    """ReportingService bound to the migrated temp database.

    Depends on ``db`` so the file is created and migrated first.
    """
    manager = DatabaseManager(db_file)
    return ReportingService(manager)


def _setup_day0(svc: SimpleNamespace) -> None:
    """D0: first day — open, prices, opening stock w/ values, readings, close."""
    svc.days.open_day(D0, opening_cash_paisa=1_000_000)
    effective = pkt_midnight_utc_iso(D0)
    svc.prices.set_price("PETROL", PETROL_PRICE, effective, D0)
    svc.prices.set_price("DIESEL", DIESEL_PRICE, effective, D0)
    svc.inventory.set_opening_stock(
        "PETROL", Decimal("10000"), D0, value_pkr=Decimal("2400000")
    )
    svc.inventory.set_opening_stock(
        "DIESEL", Decimal("8000"), D0, value_pkr=Decimal("1920000")
    )
    for code in ("1 PG+", "2 PG+", "3 HSD", "4 HSD"):
        svc.nozzles.record_reading(D0, code, "1000")
    svc.sales.post_day_sales(D0)
    svc.days.close_day(D0, physical_cash_paisa=1_000_000)


def _setup_day1_open(svc: SimpleNamespace) -> dict:
    """D1: deliveries, readings, sales, credit, payments, expenses, materials.

    Returns the interesting ids/uuids. The day stays open; callers close it.
    """
    svc.days.open_day(D1, opening_cash_paisa=1_000_000)
    supplier_id = svc.deliveries.add_supplier("PSO Depot")
    svc.deliveries.record_delivery(
        supplier=supplier_id,
        fuel_product="PETROL",
        tank="TANK-PETROL",
        business_date=D1,
        quantity_litres=Decimal("5000"),
        unit_cost_pkr=Decimal("250.0000"),
        supplier_invoice_no="INV-001",
        occurred_at_utc=_stamp(D1, 9),
    )
    for code, litres in (
        ("1 PG+", "1500"),
        ("2 PG+", "1300"),
        ("3 HSD", "1200"),
        ("4 HSD", "1100"),
    ):
        svc.nozzles.record_reading(D1, code, litres)
    svc.sales.post_day_sales(D1)
    customer_id = svc.credit.create_customer(
        "Ali Traders", customer_code="ALI-001"
    ).id
    ticket = svc.credit.issue_ticket(
        customer_id,
        D1,
        [
            {
                "fuel_product_code": "PETROL",
                "nozzle_code": "1 PG+",
                "quantity_litres": Decimal("100"),
            }
        ],
        paid_now_paisa=TICKET_PAID_NOW_PAISA,
        occurred_at_utc=_stamp(D1, 12),
    )
    payment = svc.payments.record_payment(
        customer_id, D1, PAYMENT_PAISA, method="cash",
        occurred_at_utc=_stamp(D1, 13),
    )
    svc.expenses.record_expense(
        business_date=D1,
        category="Salaries",
        amount_paisa=EXPENSE_PAISA,
        method="cash",
        payee="Staff",
        occurred_at_utc=_stamp(D1, 14),
    )
    svc.materials.add_product("Lubricant 1L")
    svc.materials.record_purchase(
        product="Lubricant 1L",
        business_date=D1,
        quantity_units=10,
        unit_price_paisa=50_000,
        method="cash",
        occurred_at_utc=_stamp(D1, 10),
    )
    svc.materials.record_sale(
        product="Lubricant 1L",
        business_date=D1,
        quantity_units=3,
        unit_price_paisa=70_000,
        method="cash",
        occurred_at_utc=_stamp(D1, 15),
    )
    return {
        "supplier_id": supplier_id,
        "customer_id": customer_id,
        "ticket_uuid": ticket.uuid,
        "ticket_receipt_no": ticket.receipt_no,
        "payment_uuid": payment.uuid,
        "payment_receipt_no": payment.receipt_no,
    }


def _close_day1_clean(svc: SimpleNamespace, **kwargs) -> dict:
    """Full two-day scenario; D1 closed with a balanced drawer by default."""
    physical = kwargs.pop("physical_cash_paisa", EXPECTED_CASH_PAISA)
    counts = kwargs.pop("physical_counts", None)
    assert not kwargs, f"unexpected kwargs: {kwargs}"
    _setup_day0(svc)
    refs = _setup_day1_open(svc)
    if counts:
        for tank, litres in counts.items():
            svc.inventory.record_physical_count(tank, D1, litres)
    svc.days.close_day(D1, physical_cash_paisa=physical)
    return refs


# ---------------------------------------------------------------------------
# catalog & validation
# ---------------------------------------------------------------------------
def test_list_reports_catalog(reporting):
    specs = reporting.list_reports()
    assert [s.report_id for s in specs] == list(REPORT_IDS)
    assert len(specs) == 21
    for spec in specs:
        assert spec.title_en.strip(), spec.report_id
        assert spec.title_ur.strip(), spec.report_id
        for filt in spec.filters:
            assert filt.name and filt.label_en and filt.label_ur and filt.kind


def test_unknown_report_id_raises(reporting):
    with pytest.raises(errors.ValidationError):
        reporting.generate_report("no_such_report")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"date_from": "2026-13-40"},
        {"date_to": "not-a-date"},
        {"business_date": "2026/10/01"},
        {"date_from": "2026-10-05", "date_to": "2026-10-01"},
    ],
)
def test_bad_dates_raise(reporting, kwargs):
    with pytest.raises(errors.ValidationError):
        reporting.generate_report("fuel_sales", **kwargs)


def test_unknown_filter_values_raise(reporting, phase5_services):
    with pytest.raises(errors.ValidationError):
        reporting.generate_report("fuel_sales", fuel_product_id=9999)
    with pytest.raises(errors.ValidationError):
        reporting.generate_report("customer_statement", customer_id=9999)
    with pytest.raises(errors.ValidationError):
        reporting.generate_report("expenses", category="No Such Category")
    with pytest.raises(errors.ValidationError):
        reporting.generate_report("customer_statement")  # customer required
    with pytest.raises(errors.ValidationError):
        reporting.generate_report("payment_receipt")  # reference required
    with pytest.raises(errors.NotFoundError):
        reporting.generate_report("payment_receipt", reference="nope")


def test_fuel_product_filter_accepts_code_and_id(
    reporting, phase5_services, db
):
    _close_day1_clean(phase5_services)
    by_id = reporting.generate_report(
        "fuel_sales",
        date_from=D1,
        date_to=D1,
        fuel_product_id=fuel_product_id(db, "PETROL"),
    )
    by_code = reporting.generate_report(
        "fuel_sales", date_from=D1, date_to=D1, fuel_product_id="PETROL"
    )
    assert {r["product"] for r in by_code.rows} == {"PETROL"}
    assert by_id.totals == by_code.totals
    assert by_code.totals["liters_ml"] == METER_PETROL_ML


def test_empty_result_is_valid_not_error(reporting):
    # Fresh DB: no business days at all; explicit range → valid empty report.
    result = reporting.generate_report(
        "fuel_sales", date_from="2026-10-01", date_to="2026-10-02"
    )
    assert result.rows == []
    assert result.totals["liters_ml"] == 0
    assert result.totals["revenue_paisa"] == 0
    assert TIMESTAMP_RE.match(result.generated_at_utc)


def test_generated_at_utc_format(reporting, phase5_services):
    _close_day1_clean(phase5_services)
    result = reporting.generate_report("cashbook", business_date=D1)
    assert TIMESTAMP_RE.match(result.generated_at_utc)
    # generated_at_utc must be "now"-ish (within the last hour)
    generated = datetime.strptime(result.generated_at_utc, "%Y-%m-%dT%H:%M:%S.%fZ")
    from datetime import timezone

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((now - generated).total_seconds()) < 3600


# ---------------------------------------------------------------------------
# register-style reports
# ---------------------------------------------------------------------------
def test_daily_register_sections_and_totals(reporting, phase5_services, db):
    _close_day1_clean(phase5_services)
    result = reporting.generate_report("daily_register", business_date=D1)
    assert result.closing_revision == 1
    sections = {r["section"] for r in result.rows}
    assert {
        "Nozzle readings",
        "Fuel sales",
        "Stock",
        "Receipts",
        "Payments",
        "Customer credit",
        "Daily balances",
    } <= sections
    # Fuel-sales total ties to the metered intervals.
    assert result.totals["quantity_ml"] == METER_PETROL_ML + METER_DIESEL_ML
    assert result.totals["amount_paisa"] == METER_PETROL_PAISA + METER_DIESEL_PAISA
    # Per-nozzle rows: 4 nozzles, quantities match readings.
    nozzle_rows = [
        r for r in result.rows if r["section"] == "Nozzle readings"
    ]
    assert len(nozzle_rows) == 4
    assert sum(r["quantity_ml"] or 0 for r in nozzle_rows) == 1_100_000
    # Receipts tie to v_cashbook; the drawer balances.
    receipt_total = next(
        r["amount_paisa"]
        for r in result.rows
        if r["description"] == "Total receipts"
    )
    cashbook_receipts = db.execute(
        "SELECT COALESCE(SUM(signed_paisa), 0) FROM v_cashbook"
        " WHERE business_date = ? AND kind = 'receipt'",
        (D1,),
    ).fetchone()[0]
    assert receipt_total == cashbook_receipts
    balances = {
        r["description"]: r["amount_paisa"]
        for r in result.rows
        if r["section"] == "Daily balances"
    }
    assert balances["Difference (physical − expected)"] == 0
    assert result.period_label_en == f"Business day {D1}"
    assert result.period_label_ur == f"کاروباری دن {D1}"


def test_daily_register_open_day_defaults_and_no_revision(
    reporting, phase5_services
):
    _setup_day0(phase5_services)
    _setup_day1_open(phase5_services)  # D1 open, not closed
    result = reporting.generate_report("daily_register")  # defaults to open day
    assert result.business_date == D1
    assert result.closing_revision is None
    assert any("open day" in n for n in result.notes_en)


def test_nozzle_readings(reporting, phase5_services):
    _close_day1_clean(phase5_services)
    result = reporting.generate_report("nozzle_readings", business_date=D1)
    assert len(result.rows) == 4
    by_nozzle = {r["nozzle"]: r for r in result.rows}
    assert by_nozzle["1 PG+"]["liters_sold_ml"] == 500_000
    assert by_nozzle["1 PG+"]["previous_ml"] == 1_000_000
    assert by_nozzle["1 PG+"]["current_ml"] == 1_500_000
    assert result.totals["liters_sold_ml"] == 1_100_000
    # fuel product filter
    petrol = reporting.generate_report(
        "nozzle_readings", business_date=D1, fuel_product_id="PETROL"
    )
    assert {r["product"] for r in petrol.rows} == {"PETROL"}
    assert petrol.totals["liters_sold_ml"] == METER_PETROL_ML


def test_fuel_sales_split_and_ties(reporting, phase5_services):
    _close_day1_clean(phase5_services)
    result = reporting.generate_report("fuel_sales", date_from=D0, date_to=D1)
    d1_rows = [r for r in result.rows if r["business_date"] == D1]
    assert {r["product"] for r in d1_rows} == {"PETROL", "DIESEL"}
    by_product = {r["product"]: r for r in d1_rows}
    assert by_product["PETROL"]["liters_ml"] == METER_PETROL_ML
    assert by_product["PETROL"]["revenue_paisa"] == METER_PETROL_PAISA
    assert by_product["DIESEL"]["liters_ml"] == METER_DIESEL_ML
    assert by_product["DIESEL"]["revenue_paisa"] == METER_DIESEL_PAISA
    # Ties to the sales service aggregates.
    sales = phase5_services.sales.day_sales(D1)
    assert result.totals["revenue_paisa"] == sum(
        sales.revenue_by_product_paisa.values()
    )
    assert result.totals["liters_ml"] == sum(
        sales.liters_by_product_ml.values()
    )


def test_cashbook_ties_to_daily_cash_position(reporting, phase5_services, db):
    _close_day1_clean(phase5_services)
    result = reporting.generate_report("cashbook", business_date=D1)
    assert result.rows, "expected cash-book entries"
    position = db.execute(
        "SELECT * FROM v_daily_cash_position WHERE business_date = ?", (D1,)
    ).fetchone()
    assert result.totals["receipt_paisa"] == position["receipts_paisa"]
    assert result.totals["payment_paisa"] == position["payments_paisa"]
    # Running balance starts at opening cash and ends at expected cash.
    first = result.rows[0]
    first_signed = (first["receipt_paisa"] or 0) - (first["payment_paisa"] or 0)
    assert first["balance_paisa"] - first_signed == 1_000_000
    assert result.rows[-1]["balance_paisa"] == position["expected_paisa"]
    # Every row nets correctly: receipts positive, payments positive column.
    for row in result.rows:
        assert (row["receipt_paisa"] or 0) >= 0
        assert (row["payment_paisa"] or 0) >= 0
        assert not (
            row["receipt_paisa"] is not None
            and row["payment_paisa"] is not None
        )


def test_cashbook_category_filter(reporting, phase5_services):
    _close_day1_clean(phase5_services)
    result = reporting.generate_report(
        "cashbook", business_date=D1, category="cash_sales"
    )
    assert result.rows
    assert {r["category"] for r in result.rows} == {"Cash sales"}


def test_sales_reconciliation_identity(reporting, phase5_services):
    _setup_day0(phase5_services)
    _setup_day1_open(phase5_services)
    # Before close: no settlements recorded → unresolved warns (note, not error).
    pre = reporting.generate_report("sales_reconciliation", business_date=D1)
    assert pre.totals["unresolved_paisa"] != 0
    assert any("WARNING" in n for n in pre.notes_en)
    assert any("تنبیہ" in n for n in pre.notes_ur)
    # After close the §6 identity holds exactly.
    phase5_services.days.close_day(D1, physical_cash_paisa=EXPECTED_CASH_PAISA)
    result = reporting.generate_report("sales_reconciliation", business_date=D1)
    totals = result.totals
    assert totals["unresolved_paisa"] == 0
    assert (
        totals["meter_revenue_paisa"]
        == totals["settled_paisa"] + totals["new_receivables_paisa"]
    )
    assert totals["meter_revenue_paisa"] == METER_PETROL_PAISA + METER_DIESEL_PAISA
    assert totals["new_receivables_paisa"] == TICKET_NET_PAISA
    assert not any("WARNING" in n for n in result.notes_en)
    # Old-debt recovery never appears in the reconciliation.
    assert all(
        "credit_recovery" not in (r["product"] or "") for r in result.rows
    )


# ---------------------------------------------------------------------------
# credit reports
# ---------------------------------------------------------------------------
def test_customer_credit_register(reporting, phase5_services):
    refs = _close_day1_clean(phase5_services)
    result = reporting.generate_report(
        "customer_credit_register", date_from=D0, date_to=D1
    )
    tickets = [r for r in result.rows if r["kind"] == "Ticket"]
    payments = [r for r in result.rows if r["kind"] == "Payment"]
    assert len(tickets) == 1
    assert tickets[0]["gross_paisa"] == TICKET_GROSS_PAISA
    assert tickets[0]["paid_now_paisa"] == TICKET_PAID_NOW_PAISA
    assert tickets[0]["receipt_no"] == refs["ticket_receipt_no"]
    assert len(payments) == 1
    assert payments[0]["payment_paisa"] == PAYMENT_PAISA
    assert result.totals["gross_paisa"] == TICKET_GROSS_PAISA
    assert result.totals["payment_paisa"] == PAYMENT_PAISA
    # customer filter
    filtered = reporting.generate_report(
        "customer_credit_register",
        date_from=D0,
        date_to=D1,
        customer_id=refs["customer_id"],
    )
    assert len(filtered.rows) == len(result.rows)


def test_credit_register_reversal_nets_to_zero(reporting, phase5_services):
    # Reversal on the open day (no close-flow interaction): the flipped
    # original keeps +gross, the reversal row carries −gross, net 0.
    _setup_day0(phase5_services)
    refs = _setup_day1_open(phase5_services)
    phase5_services.credit.reverse_ticket(refs["ticket_uuid"], "test", D1)
    result = reporting.generate_report(
        "customer_credit_register", business_date=D1
    )
    assert result.totals["gross_paisa"] == 0
    assert "Ticket reversal" in {r["kind"] for r in result.rows}


def test_customer_statement_running_balance(reporting, phase5_services):
    refs = _close_day1_clean(phase5_services)
    result = reporting.generate_report(
        "customer_statement", customer_id=refs["customer_id"]
    )
    assert result.rows[0]["description"] == "Opening balance"
    assert result.rows[0]["balance_paisa"] == 0
    # Running balance follows the §7 ledger formula step by step.
    running = 0
    for row in result.rows[1:]:
        running += (row["debit_paisa"] or 0) - (row["credit_paisa"] or 0)
        assert row["balance_paisa"] == running
    assert running == TICKET_UNPAID_AFTER_PAYMENT
    # Closing balance equals the service's outstanding figure.
    assert phase5_services.credit.outstanding(refs["customer_id"]) == running
    # Date-filtered statement: opening is the balance just before date_from.
    filtered = reporting.generate_report(
        "customer_statement",
        customer_id=refs["customer_id"],
        date_from=D1,
        date_to=D1,
    )
    assert filtered.rows[0]["balance_paisa"] == 0  # nothing before D1


def test_payment_receipt_by_uuid_and_receipt_no(reporting, phase5_services):
    refs = _close_day1_clean(phase5_services)
    by_uuid = reporting.generate_report(
        "payment_receipt", reference=refs["payment_uuid"]
    )
    by_no = reporting.generate_report(
        "payment_receipt", reference=refs["payment_receipt_no"]
    )
    for result in (by_uuid, by_no):
        assert result.totals["amount_paisa"] == PAYMENT_PAISA
        fields = {r["field"]: r for r in result.rows}
        assert fields["Amount received"]["amount_paisa"] == PAYMENT_PAISA
        assert fields["Unallocated"]["amount_paisa"] == 0
        allocs = [
            r for r in result.rows if r["field"] == "Allocated to ticket"
        ]
        assert len(allocs) == 1
        assert allocs[0]["amount_paisa"] == PAYMENT_PAISA
        assert allocs[0]["value"] == refs["ticket_receipt_no"]
        assert result.closing_revision == 1
        assert result.business_date == D1


def test_outstanding_balances_cross_check(reporting, phase5_services, db):
    refs = _close_day1_clean(phase5_services)
    result = reporting.generate_report("outstanding_balances")
    row = next(
        r for r in result.rows if r["customer_code"] == "ALI-001"
    )
    assert row["outstanding_paisa"] == TICKET_UNPAID_AFTER_PAYMENT
    # Cross-check 1: sum of report == sum of v_outstanding_receivables.
    view_total = db.execute(
        "SELECT COALESCE(SUM(outstanding_paisa), 0)"
        " FROM v_outstanding_receivables"
    ).fetchone()[0]
    assert result.totals["outstanding_paisa"] == view_total
    # Cross-check 2: sum of report == sum of per-customer statement closings.
    statement = reporting.generate_report(
        "customer_statement", customer_id=refs["customer_id"]
    )
    assert statement.rows[-1]["balance_paisa"] == row["outstanding_paisa"]
    # As-of a date before the customer existed: customer excluded.
    asof = reporting.generate_report("outstanding_balances", date_to=D0)
    assert all(r["customer_code"] != "ALI-001" for r in asof.rows)
    # As-of D1 matches the current figure (nothing changed after D1).
    asof_d1 = reporting.generate_report("outstanding_balances", date_to=D1)
    assert asof_d1.totals["outstanding_paisa"] == result.totals["outstanding_paisa"]


# ---------------------------------------------------------------------------
# stock / deliveries / suppliers / materials / expenses
# ---------------------------------------------------------------------------
def test_fuel_stock_theoretical(reporting, phase5_services):
    _close_day1_clean(phase5_services)
    result = reporting.generate_report("fuel_stock", business_date=D1)
    by_tank = {r["tank"]: r for r in result.rows}
    # PETROL: 10,000 L opening + 5,000 L delivery − 800 L sold.
    assert by_tank["TANK-PETROL"]["theoretical_ml"] == 14_200_000
    assert by_tank["TANK-PETROL"]["opening_ml"] == 10_000_000
    assert by_tank["TANK-PETROL"]["received_ml"] == 5_000_000
    assert by_tank["TANK-PETROL"]["sold_ml"] == 800_000
    # DIESEL: 8,000 L opening − 300 L sold.
    assert by_tank["TANK-DIESEL"]["theoretical_ml"] == 7_700_000
    assert result.closing_revision == 1


def test_tank_reconciliation_variance(reporting, phase5_services):
    _close_day1_clean(
        phase5_services,
        physical_counts={"TANK-PETROL": "14200", "TANK-DIESEL": "7690"},
    )
    result = reporting.generate_report("tank_reconciliation", business_date=D1)
    by_tank = {r["tank"]: r for r in result.rows}
    assert by_tank["TANK-PETROL"]["variance_ml"] == 0
    assert by_tank["TANK-PETROL"]["status"] == "OK"
    assert by_tank["TANK-DIESEL"]["variance_ml"] == -10_000
    assert by_tank["TANK-DIESEL"]["status"] == "Variance"
    assert result.totals["variance_ml"] == -10_000


def test_tank_reconciliation_missing_dip(reporting, phase5_services):
    _close_day1_clean(phase5_services)  # no dip readings
    result = reporting.generate_report("tank_reconciliation", business_date=D1)
    assert all(r["status"] == "No dip reading" for r in result.rows)
    assert all(r["variance_ml"] is None for r in result.rows)


def test_deliveries(reporting, phase5_services):
    _close_day1_clean(phase5_services)
    result = reporting.generate_report("deliveries", date_from=D0, date_to=D1)
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row["supplier"] == "PSO Depot"
    assert row["product"] == "PETROL"
    assert row["quantity_ml"] == 5_000_000
    assert row["unit_cost_10k"] == 2_500_000
    assert row["total_cost_paisa"] == 125_000_000
    assert row["paid_paisa"] == 0
    assert row["outstanding_paisa"] == 125_000_000
    assert row["invoice_no"] == "INV-001"
    assert result.totals["total_cost_paisa"] == 125_000_000
    # product filter with no matching rows → valid empty report
    empty = reporting.generate_report(
        "deliveries", date_from=D0, date_to=D1, fuel_product_id="DIESEL"
    )
    assert empty.rows == []
    assert empty.totals["total_cost_paisa"] == 0


def test_supplier_balances(reporting, phase5_services):
    _close_day1_clean(phase5_services)
    result = reporting.generate_report("supplier_balances")
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row["supplier"] == "PSO Depot"
    assert row["invoiced_paisa"] == 125_000_000
    assert row["paid_at_delivery_paisa"] == 0
    assert row["paid_paisa"] == 0
    assert row["outstanding_paisa"] == 125_000_000
    # Ties to the delivery service's outstanding figure.
    delivery_id = phase5_services.deliveries.list_deliveries(
        business_date=D1
    )[0].id
    assert (
        phase5_services.suppliers.delivery_outstanding(delivery_id)
        == row["outstanding_paisa"]
    )


def test_other_materials(reporting, phase5_services):
    _close_day1_clean(phase5_services)
    result = reporting.generate_report(
        "other_materials", date_from=D1, date_to=D1
    )
    by_type = {r["txn_type"]: r for r in result.rows}
    assert by_type["purchase"]["quantity_units"] == 10
    assert by_type["purchase"]["total_paisa"] == 500_000
    assert by_type["sale"]["quantity_units"] == -3
    assert by_type["sale"]["total_paisa"] == -210_000
    assert result.totals["total_paisa"] == 290_000
    assert any("Lubricant 1L: 7 units" in n for n in result.notes_en)
    # category filter = product name
    filtered = reporting.generate_report(
        "other_materials", date_from=D1, date_to=D1, category="Lubricant 1L"
    )
    assert len(filtered.rows) == len(result.rows)


def test_expenses_and_reversal(reporting, phase5_services):
    _setup_day0(phase5_services)
    _setup_day1_open(phase5_services)
    expense = phase5_services.expenses.list_expenses(business_date=D1)[0]
    result = reporting.generate_report("expenses", business_date=D1)
    assert result.totals["amount_paisa"] == EXPENSE_PAISA
    assert result.rows[0]["category"] == "Salaries"
    # Reversal nets to zero under the corrected (011) rule.
    phase5_services.expenses.reverse_expense(expense.uuid, reason="test")
    reversed_result = reporting.generate_report("expenses", business_date=D1)
    assert reversed_result.totals["amount_paisa"] == 0
    assert {r["status"] for r in reversed_result.rows} == {
        "reversed",
        "reversal",
    }
    # category filter
    filtered = reporting.generate_report(
        "expenses", business_date=D1, category="Salaries"
    )
    assert len(filtered.rows) == len(reversed_result.rows)


# ---------------------------------------------------------------------------
# P&L
# ---------------------------------------------------------------------------
def test_pnl_daily_frozen_snapshot(reporting, phase5_services):
    _close_day1_clean(phase5_services)
    result = reporting.generate_report("pnl_daily", business_date=D1)
    assert result.closing_revision == 1
    by_product = {r["product"]: r for r in result.rows}
    assert by_product["PETROL"]["revenue_paisa"] == METER_PETROL_PAISA
    assert by_product["PETROL"]["cogs_paisa"] == PETROL_COGS_PAISA
    assert by_product["DIESEL"]["revenue_paisa"] == METER_DIESEL_PAISA
    assert by_product["DIESEL"]["cogs_paisa"] == DIESEL_COGS_PAISA
    totals = result.totals
    assert totals["revenue_paisa"] == METER_PETROL_PAISA + METER_DIESEL_PAISA + 210_000
    assert totals["cogs_paisa"] == PETROL_COGS_PAISA + DIESEL_COGS_PAISA + 150_000
    assert totals["gross_profit_paisa"] == totals["revenue_paisa"] - totals["cogs_paisa"]
    net_row = next(r for r in result.rows if r["product"] == "Net profit")
    assert net_row["gross_profit_paisa"] == totals["net_profit_paisa"]
    assert totals["net_profit_paisa"] == totals["gross_profit_paisa"] - EXPENSE_PAISA
    # Frozen: the note says the closing snapshot is the source.
    assert any("snapshot" in n for n in result.notes_en)
    # Ties exactly to the accounting service.
    service_pnl = phase5_services.accounting.daily_pnl(D1)
    assert service_pnl.source == "closing_snapshot"
    assert totals["net_profit_paisa"] == service_pnl.net_profit_paisa


def test_pnl_daily_immutable_after_later_purchase(reporting, phase5_services):
    _close_day1_clean(phase5_services)
    before = reporting.generate_report("pnl_daily", business_date=D1).totals
    # A new delivery on D2 must not restate D1's frozen P&L.
    supplier_id = phase5_services.deliveries.list_suppliers()[0].id
    phase5_services.days.open_day(D2, opening_cash_paisa=1_000_000)
    phase5_services.deliveries.record_delivery(
        supplier=supplier_id,
        fuel_product="DIESEL",
        tank="TANK-DIESEL",
        business_date=D2,
        quantity_litres=Decimal("2000"),
        unit_cost_pkr=Decimal("300.0000"),
        supplier_invoice_no="INV-002",
    )
    after = reporting.generate_report("pnl_daily", business_date=D1).totals
    assert before == after


def test_pnl_monthly_sums_daily_snapshots(reporting, phase5_services):
    _close_day1_clean(phase5_services)
    result = reporting.generate_report("pnl_monthly", business_date=D1)
    assert result.period_label_en == "October 2026"
    day_rows = [r for r in result.rows if r["business_date"] != "Total"]
    days = {r["business_date"] for r in day_rows}
    assert D0 in days and D1 in days
    total = next(r for r in result.rows if r["business_date"] == "Total")
    daily = reporting.generate_report("pnl_daily", business_date=D1).totals
    assert total["revenue_paisa"] == daily["revenue_paisa"]  # D0 revenue is 0
    assert total["net_profit_paisa"] == daily["net_profit_paisa"]
    assert result.totals["revenue_paisa"] == total["revenue_paisa"]
    # Days with no business-day record are flagged estimated, not errors.
    assert any("no business day record" in n for n in result.notes_en)


def test_pnl_annual(reporting, phase5_services):
    _close_day1_clean(phase5_services)
    result = reporting.generate_report("pnl_annual", business_date=D1)
    assert result.period_label_en == "Year 2026"
    october = next(r for r in result.rows if r["month"] == "October 2026")
    monthly = reporting.generate_report("pnl_monthly", business_date=D1)
    assert october["revenue_paisa"] == monthly.totals["revenue_paisa"]
    assert october["net_profit_paisa"] == monthly.totals["net_profit_paisa"]
    total = next(r for r in result.rows if r["month"] == "Total")
    assert total["revenue_paisa"] == october["revenue_paisa"]
    assert result.totals["net_profit_paisa"] == total["net_profit_paisa"]


# ---------------------------------------------------------------------------
# cash difference / audit / backup
# ---------------------------------------------------------------------------
def test_cash_difference_shortage_never_written_off(reporting, phase5_services, db):
    _close_day1_clean(
        phase5_services, physical_cash_paisa=EXPECTED_CASH_PAISA - 2_500
    )
    result = reporting.generate_report("cash_difference", business_date=D1)
    assert len(result.rows) == 1
    row = result.rows[0]
    position = db.execute(
        "SELECT * FROM v_daily_cash_position WHERE business_date = ?", (D1,)
    ).fetchone()
    assert row["expected_paisa"] == position["expected_paisa"]
    assert row["expected_paisa"] == EXPECTED_CASH_PAISA
    assert row["physical_paisa"] == EXPECTED_CASH_PAISA - 2_500
    assert row["difference_paisa"] == -2_500
    assert row["shortage_paisa"] == 2_500
    assert row["surplus_paisa"] == 0
    assert any("never" in n and "auto-written-off" in n for n in result.notes_en)
    # The shortage is NOT auto-written-off: the next day still opens with
    # the counted physical cash, and the difference stays on the record.
    assert db.execute(
        "SELECT difference_paisa FROM v_daily_cash_position"
        " WHERE business_date = ?",
        (D1,),
    ).fetchone()[0] == -2_500


def test_cash_difference_range(reporting, phase5_services):
    _close_day1_clean(phase5_services)
    result = reporting.generate_report(
        "cash_difference", date_from=D0, date_to=D1
    )
    assert len(result.rows) == 2
    assert result.totals["difference_paisa"] == 0


def test_audit_history_newest_first_and_range(reporting, phase5_services):
    _close_day1_clean(phase5_services)
    result = reporting.generate_report("audit_history")
    assert result.rows, "expected audit entries from the fixture writes"
    ids = [r["id"] for r in result.rows]
    assert ids == sorted(ids, reverse=True)
    # The date range filters on the audit event time (machine UTC, the
    # log's own "occurred_at_utc") — all fixture writes happened "now".
    from app.calculations import time as timecalc

    today = timecalc.karachi_now().date().isoformat()
    today_only = reporting.generate_report(
        "audit_history", date_from=today, date_to=today
    )
    assert today_only.rows
    start = timecalc.karachi_start_of_day_utc(today)
    end = timecalc.karachi_end_of_day_utc(today)
    for row in today_only.rows:
        assert start <= row["occurred_at_utc"] <= end
    # A range in the past with no audit events → valid empty report.
    empty = reporting.generate_report(
        "audit_history", date_from="2020-01-01", date_to="2020-01-31"
    )
    assert empty.rows == []


def test_backup_history(reporting, phase5_services, db):
    _close_day1_clean(phase5_services)
    # The day-close auto-backup hook wrote one 'day_close' row per closed
    # day (D0 and D1) — the hook backs up the service's own database.
    hook_rows = db.execute(
        "SELECT filename, status FROM backup_history"
        " WHERE filename LIKE 'PetrolPump_Backup_%'"
    ).fetchall()
    assert len(hook_rows) == 2
    assert all(r["status"] == "validated" for r in hook_rows)
    db.execute(
        "INSERT INTO backup_history"
        " (filename, destination, status, size_bytes, sha256)"
        " VALUES (?, ?, ?, ?, ?)",
        ("backup-20261014.sqlite3", "local", "validated", 12345, "deadbeef"),
    )
    db.commit()
    result = reporting.generate_report("backup_history")
    assert len(result.rows) == 3
    row = next(r for r in result.rows if r["filename"] == "backup-20261014.sqlite3")
    assert row["status"] == "validated"
    assert row["size_bytes"] == 12345
    assert result.totals["id"] == 3
    # Range with no backups → valid empty report.
    empty = reporting.generate_report(
        "backup_history", date_from="2020-01-01", date_to="2020-01-31"
    )
    assert empty.rows == []


# ---------------------------------------------------------------------------
# closing revisions & float hygiene
# ---------------------------------------------------------------------------
def test_reopened_day_report_shows_latest_revision(reporting, phase5_services):
    _close_day1_clean(phase5_services)
    phase5_services.days.reopen_day(D1, reason="correction for test")
    phase5_services.expenses.record_expense(
        business_date=D1,
        category="Miscellaneous",
        amount_paisa=200_000,
        method="cash",
        payee="Correction",
        occurred_at_utc=_stamp(D1, 16),
    )
    phase5_services.days.close_day(
        D1, physical_cash_paisa=EXPECTED_CASH_PAISA - 200_000
    )
    result = reporting.generate_report("daily_register", business_date=D1)
    assert result.closing_revision == 2
    assert any("revision 2" in n for n in result.notes_en)
    assert any("نظرثانی 2" in n for n in result.notes_ur)
    # The corrected day's P&L reflects the new expense (new snapshot).
    pnl = reporting.generate_report("pnl_daily", business_date=D1)
    assert pnl.closing_revision == 2
    assert pnl.totals["net_profit_paisa"] == (
        pnl.totals["gross_profit_paisa"] - EXPENSE_PAISA - 200_000
    )
    # A single-day cash_difference also carries the revision.
    diff = reporting.generate_report("cash_difference", business_date=D1)
    assert diff.closing_revision == 2


def test_no_float_or_decimal_in_any_report(reporting, phase5_services):
    from decimal import Decimal as _Decimal

    _close_day1_clean(
        phase5_services,
        physical_cash_paisa=EXPECTED_CASH_PAISA - 2_500,
        physical_counts={"TANK-PETROL": "14200", "TANK-DIESEL": "7690"},
    )
    scenarios = [
        ("daily_register", {"business_date": D1}),
        ("nozzle_readings", {"business_date": D1}),
        ("fuel_sales", {"date_from": D0, "date_to": D1}),
        ("cashbook", {"business_date": D1}),
        ("sales_reconciliation", {"business_date": D1}),
        ("customer_credit_register", {"date_from": D0, "date_to": D1}),
        ("fuel_stock", {"business_date": D1}),
        ("tank_reconciliation", {"business_date": D1}),
        ("deliveries", {"date_from": D0, "date_to": D1}),
        ("supplier_balances", {}),
        ("other_materials", {"date_from": D0, "date_to": D1}),
        ("expenses", {"date_from": D0, "date_to": D1}),
        ("pnl_daily", {"business_date": D1}),
        ("pnl_monthly", {"business_date": D1}),
        ("pnl_annual", {"business_date": D1}),
        ("cash_difference", {"date_from": D0, "date_to": D1}),
        ("audit_history", {}),
        ("backup_history", {}),
        ("outstanding_balances", {}),
    ]
    # customer-scoped reports need ids from the fixture services
    customer_id = phase5_services.credit.list_customers()[0].id
    payment = phase5_services.payments.day_payments(D1)[0]
    scenarios += [
        ("customer_statement", {"customer_id": customer_id}),
        ("payment_receipt", {"reference": payment.uuid}),
    ]
    assert len(scenarios) == 21
    for report_id, kwargs in scenarios:
        result = reporting.generate_report(report_id, **kwargs)
        assert result.report_id == report_id
        assert TIMESTAMP_RE.match(result.generated_at_utc), report_id
        for row in result.rows:
            for key, value in row.items():
                assert type(value).__name__ in (
                    "int",
                    "str",
                    "NoneType",
                ), (report_id, key, value, type(value))
        for key, value in result.totals.items():
            assert type(value).__name__ in (
                "int",
                "str",
                "NoneType",
            ), (report_id, key, value, type(value))
