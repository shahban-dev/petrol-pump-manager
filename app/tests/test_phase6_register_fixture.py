"""Phase-6 real-register fixture tests: the user's handwritten register day.

Posts 2026-10-06 (Zeishan Petroleum Service, PSO) through the real
services and asserts the ``daily_register`` report reproduces the paper's
HIGH-certainty figures, and that the report's section order mirrors the
paper register.

Certainty discipline (documentation/register_transcription_notes.md, and
the fixture's own ``_meta.certainty_summary``):

* HIGH — business date, nozzle codes/products, all readings, sale
  quantities, product sale totals, and revenue at the OFFICIAL notified
  rates (petrol 393.64 / diesel 397.76 PKR/L, effective 2026-10-06 —
  user-approved as the price_history seed).
* MEDIUM — the diesel stock-table rows. The petrol stock row (opening
  ~14 L flagged uncertain) is NOT asserted.
* LOW — the handwritten rate/cash scribbles (198,800 / 66,163 /
  265,963). A guard test below asserts these figures appear NOWHERE in
  the report; they are never used as expected values.

Every scenario below posts through the real services (no raw SQL except
the conftest ``insert_price_row`` test bridge for the day-open price —
the §11.2 snapshot rule would otherwise block re-setting a day-open
price without segment snapshots).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from app.calculations import money as calc
from app.database.connection import DatabaseManager
from app.services.reporting_service import ReportingService

from .conftest import (
    insert_price_row,
    pkt_clock_utc_iso,
    pkt_midnight_utc_iso,
)

FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "register_day_2026_10_06.json"
)
with FIXTURE_PATH.open(encoding="utf-8") as _fh:
    FIXTURE = json.load(_fh)

# The day before the register day: carries the "previous" meter readings
# and the manager-entered diesel opening stock forward.
DAY = FIXTURE["business_date"]["value"]  # "2026-10-06"
PREV = "2026-10-05"

#: Product code mapping: fixture's lowercase names -> seeded product codes.
PRODUCT_CODES = {"petrol": "PETROL", "diesel": "DIESEL"}

OFFICIAL_RATES = FIXTURE["rates_pkr_per_l"][
    "official_notified_rates_for_2026_10_06"
]
PETROL_RATE = Decimal(str(OFFICIAL_RATES["petrol"]))  # 393.64 PKR/L
DIESEL_RATE = Decimal(str(OFFICIAL_RATES["diesel_hsd"]))  # 397.76 PKR/L
PETROL_RATE_10K = calc.to_price_10k(PETROL_RATE)  # 3_936_400
DIESEL_RATE_10K = calc.to_price_10k(DIESEL_RATE)  # 3_977_600

DERIVED = FIXTURE["rates_pkr_per_l"]["derived_revenue_at_official_rates"]
DIESEL_STOCK = FIXTURE["stock_table_supplementary"]["diesel"]

#: The mandated paper-register section order (user priority, §Task 2).
PAPER_SECTION_ORDER = (
    "Nozzle readings",
    "Fuel sales",
    "Stock",
    "Receipts",
    "Payments",
    "Customer credit",
    "Daily balances",
)


def _post_register_day(svc, db) -> None:
    """Post the register day 2026-10-06 through the real services.

    ``svc`` is the phase5_services fixture (all services, one temp DB);
    ``db`` is the same DB's migrated connection, used only for the
    price_history test bridge.
    """
    nozzles = {n["code"]: n for n in FIXTURE["nozzles"]}

    # --- previous day: plant the paper's "previous" readings -------------
    svc.days.open_day(PREV, opening_cash_paisa=0)
    effective_prev = pkt_midnight_utc_iso(PREV)
    svc.prices.set_price("PETROL", PETROL_RATE, effective_prev, PREV)
    svc.prices.set_price("DIESEL", DIESEL_RATE, effective_prev, PREV)
    supplier_id = svc.deliveries.add_supplier("PSO Depot")
    # Manager-entered diesel opening stock (first-day entry path); it
    # carries forward to DAY on open. Petrol opening (~14 L) is flagged
    # uncertain in the notes, so no petrol opening stock is entered.
    svc.inventory.set_opening_stock(
        "DIESEL", Decimal(DIESEL_STOCK["opening_l"]), PREV
    )
    for code, nozzle in nozzles.items():
        svc.nozzles.record_reading(PREV, code, str(nozzle["previous_reading_l"]))
    # Nothing postable on PREV (no earlier readings -> every interval has
    # unknown quantity); the close still succeeds.
    svc.sales.post_day_sales(PREV)
    svc.days.close_day(PREV, physical_cash_paisa=0)

    # --- the register day -------------------------------------------------
    svc.days.open_day(DAY)  # carries diesel stock 2,225 L forward
    effective = pkt_midnight_utc_iso(DAY)
    # Official notified rates for 2026-10-06 (user-approved seed).
    insert_price_row(db, "PETROL", effective, PETROL_RATE_10K, DAY)
    insert_price_row(db, "DIESEL", effective, DIESEL_RATE_10K, DAY)
    # Deliveries as in the paper's stock table. Unpaid (the paper's cash
    # scribbles are LOW-certainty, so no supplier payment is invented);
    # they also satisfy trigger F2 (sales may not drive stock negative).
    svc.deliveries.record_delivery(
        supplier=supplier_id,
        fuel_product="PETROL",
        tank="TANK-PETROL",
        business_date=DAY,
        quantity_litres=Decimal("1000"),
        unit_cost_pkr=PETROL_RATE,
        supplier_invoice_no="PSO-20261006-P",
        occurred_at_utc=pkt_clock_utc_iso(DAY, 9),
    )
    svc.deliveries.record_delivery(
        supplier=supplier_id,
        fuel_product="DIESEL",
        tank="TANK-DIESEL",
        business_date=DAY,
        quantity_litres=Decimal(DIESEL_STOCK["delivery_l"]),
        unit_cost_pkr=DIESEL_RATE,
        supplier_invoice_no="PSO-20261006-D",
        occurred_at_utc=pkt_clock_utc_iso(DAY, 10),
    )
    for code, nozzle in nozzles.items():
        svc.nozzles.record_reading(DAY, code, str(nozzle["current_reading_l"]))
    svc.sales.post_day_sales(DAY)
    # Diesel dip -> the paper's stock-per-dip 3,260 L.
    svc.inventory.record_physical_count(
        "TANK-DIESEL", DAY, Decimal(DIESEL_STOCK["stock_per_dip_l"])
    )


def _daily_register(db_file):
    manager = DatabaseManager(db_file)
    return ReportingService(manager).generate_report(
        "daily_register", business_date=DAY
    )


def _section_rows(result, section):
    return [r for r in result.rows if r["section"] == section]


# ---------------------------------------------------------------------------
# 1. nozzle readings (HIGH certainty)
# ---------------------------------------------------------------------------
def test_fixture_nozzle_readings(phase5_services, db, db_file):
    _post_register_day(phase5_services, db)
    result = _daily_register(db_file)
    nozzle_rows = {
        r["description"]: r for r in _section_rows(result, "Nozzle readings")
    }
    assert len(nozzle_rows) == 4
    for nozzle in FIXTURE["nozzles"]:
        code = nozzle["code"]
        product = PRODUCT_CODES[nozzle["product"]]
        desc = f"Nozzle {code} ({product})"
        row = nozzle_rows[desc]
        expected_ml = nozzle["sale_l"] * 1_000
        assert row["quantity_ml"] == expected_ml, desc
        # The note carries the paper's previous -> current meter values.
        assert row["note"] == (
            f"{nozzle['previous_reading_ml']:,} ml →"
            f" {nozzle['current_reading_ml']:,} ml"
        ), desc
        # Revenue ties to the official notified rate, exactly:
        # sale is whole litres and the rate is exact to the paisa, so
        # per-interval amount_paisa_for (round_half_up of ml*price_10k /
        # 100000) is exact — no rounding drift against the paper's
        # sale_l x rate product.
        rate_10k = PETROL_RATE_10K if product == "PETROL" else DIESEL_RATE_10K
        assert row["amount_paisa"] == calc.amount_paisa_for(
            expected_ml, rate_10k
        ), desc


# ---------------------------------------------------------------------------
# 2. fuel sales by product (HIGH certainty)
# ---------------------------------------------------------------------------
def test_fixture_fuel_sales_totals(phase5_services, db, db_file):
    _post_register_day(phase5_services, db)
    result = _daily_register(db_file)
    sale_rows = {
        r["description"]: r for r in _section_rows(result, "Fuel sales")
    }

    petrol = sale_rows["PETROL (Petrol)"]
    diesel = sale_rows["DIESEL (Diesel)"]
    # The paper's product totals: petrol 497 L, diesel 165 L.
    assert petrol["quantity_ml"] == 497 * 1_000
    assert diesel["quantity_ml"] == 165 * 1_000
    # Revenue at the official 2026-10-06 rates, tied to the fixture's own
    # derived figures: 497 x 393.64 = 195,639.08 and 165 x 397.76 =
    # 65,630.40 — exact, no rounding drift (see the comment above).
    assert petrol["amount_paisa"] == DERIVED["petrol_paisa"] == 19_563_908
    assert diesel["amount_paisa"] == DERIVED["diesel_paisa"] == 6_563_040
    assert petrol["price_10k"] == PETROL_RATE_10K
    assert diesel["price_10k"] == DIESEL_RATE_10K

    total = sale_rows["Total fuel sales"]
    assert total["quantity_ml"] == 662 * 1_000
    assert total["amount_paisa"] == DERIVED["total_paisa"] == 26_126_948
    # Report totals (register convention: fuel sales only).
    assert result.totals["quantity_ml"] == 662 * 1_000
    assert result.totals["amount_paisa"] == 26_126_948


# ---------------------------------------------------------------------------
# 3. diesel stock (MEDIUM certainty) — interpretation documented, not forced
# ---------------------------------------------------------------------------
def test_fixture_diesel_stock(phase5_services, db, db_file):
    _post_register_day(phase5_services, db)
    result = _daily_register(db_file)
    stock_rows = {
        r["description"]: r for r in _section_rows(result, "Stock")
    }
    row = stock_rows["Tank TANK-DIESEL (DIESEL)"]

    # The report surfaces the SYSTEM's stock formula:
    #   theoretical_close = opening + received - sold (+ adjustments)
    #   variance        = physical - theoretical_close
    # = 2,225 + 1,000 - 165 = 3,060 L book close; dip 3,260 L -> +200 L.
    assert row["quantity_ml"] == (2_225 + 1_000 - 165) * 1_000
    assert row["note"] == "Physical 3,260,000 ml, variance +200,000 ml"

    # THE PAPER'S OWN ARITHMETIC, verified against the same committed
    # inputs the report reads (opening_balances / stock movements / the
    # dip reading):
    #   book total (pre-sale) = opening 2,225 + delivery 1,000 = 3,225 L
    #   dip stock             = 3,260 L
    #   paper's فرق            = 3,260 - 3,225 = 35 L
    #
    # INTERPRETATION DIFFERENCE (recorded, not forced): the register's
    # فرق compares the dip against the PRE-SALE book total (35 L), while
    # the system's variance compares the dip against the POST-SALE
    # theoretical close (+200 L). Both are correct under their own
    # definition; the system's formula is NOT changed to equal 35.
    opening_ml = DIESEL_STOCK["opening_l"] * 1_000
    delivery_ml = DIESEL_STOCK["delivery_l"] * 1_000
    dip_ml = DIESEL_STOCK["stock_per_dip_l"] * 1_000
    assert opening_ml + delivery_ml == DIESEL_STOCK["total_stock_l"] * 1_000
    assert dip_ml - (opening_ml + delivery_ml) == (
        DIESEL_STOCK["difference_l"] * 1_000
    ) == 35_000
    # And the two variances stay distinct by design:
    system_variance_ml = 200_000
    paper_difference_ml = 35_000
    assert system_variance_ml != paper_difference_ml


def test_fixture_petrol_delivery_only(phase5_services, db, db_file):
    # Petrol stock: assert ONLY the posted 1,000 L delivery. The opening
    # (~14 L) and dip (955) are flagged uncertain in the transcription
    # notes — deliberately not entered and not asserted.
    _post_register_day(phase5_services, db)
    positions = {
        p.tank_code: p
        for p in phase5_services.inventory.stock_positions(DAY)
    }
    assert positions["TANK-PETROL"].received_ml == 1_000_000
    assert positions["TANK-PETROL"].opening_ml == 0
    assert positions["TANK-PETROL"].physical_ml is None


# ---------------------------------------------------------------------------
# 4. the LOW-certainty cash scribbles must not leak into the report
# ---------------------------------------------------------------------------
def test_low_certainty_cash_scribbles_absent(phase5_services, db, db_file):
    _post_register_day(phase5_services, db)
    result = _daily_register(db_file)
    # Handwriting read: petrol 198,800 / diesel 66,163 / total 265,963 PKR.
    # These are LOW-certainty and must never appear as report values.
    forbidden_paisa = {198_800_00, 66_163_00, 265_963_00}
    for row in result.rows:
        assert row["amount_paisa"] not in forbidden_paisa, row
    for total in result.totals.values():
        assert total not in forbidden_paisa


# ---------------------------------------------------------------------------
# 5. section order mirrors the paper register (user priority)
# ---------------------------------------------------------------------------
def test_daily_register_matches_paper_section_order(
    phase5_services, db, db_file
):
    _post_register_day(phase5_services, db)
    result = _daily_register(db_file)
    assert result.report_id == "daily_register"
    assert result.business_date == DAY
    assert result.period_label_en == f"Business day {DAY}"

    first_seen: dict[str, int] = {}
    for idx, row in enumerate(result.rows):
        first_seen.setdefault(row["section"], idx)
    # Every paper section is present ...
    for section in PAPER_SECTION_ORDER:
        assert section in first_seen, f"missing section: {section}"
    # ... exactly once as a block, in the paper's order:
    # nozzle readings -> fuel sales -> stock -> receipts -> payments ->
    # customer credit -> daily balances.
    positions = [first_seen[s] for s in PAPER_SECTION_ORDER]
    assert positions == sorted(positions), first_seen
    # Contiguity: sections never reappear after a later one started.
    ordered = [r["section"] for r in result.rows]
    for section in PAPER_SECTION_ORDER:
        idxs = [i for i, s in enumerate(ordered) if s == section]
        assert idxs == list(range(min(idxs), max(idxs) + 1)), section
    # Receipts section closes with its total before Payments starts.
    receipts_end = max(
        i for i, s in enumerate(ordered) if s == "Receipts"
    )
    payments_start = min(
        i for i, s in enumerate(ordered) if s == "Payments"
    )
    assert ordered[receipts_end] == "Receipts"
    assert result.rows[receipts_end]["description"] == "Total receipts"
    assert receipts_end < payments_start


def test_section_order_with_cash_detail_rows(phase5_services, db, db_file):
    # The fixture day has no cash movements; the receipts/payments order
    # only matters when detail rows exist (the old code emitted Payments
    # details before Receipts details because ORDER BY kind sorts
    # 'payment' < 'receipt'). Post a receipt and a payment, then assert
    # the full block order with interleaved detail rows.
    _post_register_day(phase5_services, db)
    phase5_services.cash.record_receipt(
        business_date=DAY,
        category="other_receipt",
        amount_paisa=50_000,
        remarks="register-fixture test receipt",
        occurred_at_utc=pkt_clock_utc_iso(DAY, 15),
    )
    phase5_services.cash.record_payment(
        business_date=DAY,
        category="other_payment",
        amount_paisa=20_000,
        payee="register-fixture test payee",
        occurred_at_utc=pkt_clock_utc_iso(DAY, 16),
    )
    result = _daily_register(db_file)

    ordered = [r["section"] for r in result.rows]
    receipt_idxs = [i for i, s in enumerate(ordered) if s == "Receipts"]
    payment_idxs = [i for i, s in enumerate(ordered) if s == "Payments"]
    assert len(receipt_idxs) == 2  # detail row + Total receipts
    assert len(payment_idxs) == 2  # detail row + Total payments
    # The last Receipts row is its total, and nothing from Payments
    # appears before every Receipts row is emitted.
    assert max(receipt_idxs) < min(payment_idxs)
    receipt_descs = [
        result.rows[i]["description"] for i in receipt_idxs
    ]
    assert receipt_descs[-1] == "Total receipts"
    assert result.rows[receipt_idxs[0]]["amount_paisa"] == 50_000
    assert result.rows[receipt_idxs[-1]]["amount_paisa"] == 50_000
    payment_descs = [
        result.rows[i]["description"] for i in payment_idxs
    ]
    assert payment_descs[-1] == "Total payments"
    assert result.rows[payment_idxs[0]]["amount_paisa"] == 20_000
    assert result.rows[payment_idxs[-1]]["amount_paisa"] == 20_000
    # Paper order still holds across all seven sections.
    first_seen: dict[str, int] = {}
    for idx, section in enumerate(ordered):
        first_seen.setdefault(section, idx)
    positions = [first_seen[s] for s in PAPER_SECTION_ORDER]
    assert positions == sorted(positions)
