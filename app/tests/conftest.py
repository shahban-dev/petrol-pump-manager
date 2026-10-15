"""Shared pytest fixtures for the Phase-1 test suite.

Every test uses a FRESH temporary database (pytest ``tmp_path``) — the live
database is never touched. Two connection fixtures are provided:

* ``db``  — a pragma-verified, fully migrated connection via DatabaseManager.
* ``db2`` — a second, RAW ``sqlite3`` connection to the same file (no pragma
  setup at all). Tests that write through ``db2`` prove the schema's
  triggers/constraints reject bad writes from ANY connection, including
  direct SQL that bypasses the application layer.

Helpers use parameterized SQL. Tests themselves may use direct SQL literals —
proving the triggers beat raw SQL is the point of this suite.

Design plan: documentation/00_design_plan.md §4, §10 (acceptance tests).
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.database.connection import DatabaseManager
from app.database.migrator import migrate

#: Machine timestamps are UTC text 'YYYY-MM-DDTHH:MM:SS.SSSZ' (§5.1).
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    """Path of a fresh (not yet created) temporary database file."""
    return tmp_path / "test.sqlite3"


@pytest.fixture
def db(db_file: Path):
    """Pragma-verified, fully migrated connection to a fresh temp database."""
    manager = DatabaseManager(db_file)
    conn = manager.connect()
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def db2(db, db_file: Path):
    """A second, RAW sqlite3 connection to the same temp database.

    Deliberately created with plain ``sqlite3.connect`` — no
    DatabaseManager, no pragma setup — so trigger tests through ``db2``
    prove the database itself rejects bad writes from any client.
    Depends on ``db`` so the file is created and migrated first.
    """
    raw = sqlite3.connect(str(db_file))
    raw.row_factory = sqlite3.Row
    yield raw
    raw.close()


@pytest.fixture
def services(db, db_file: Path):
    """Phase-2 services bound to the migrated temporary database.

    Returns ``(BusinessDayService, NozzleService)`` sharing one
    DatabaseManager. Depends on ``db`` so the file is migrated first.
    Service methods open their own connections; keep the ``db``
    connection idle (committed) while services run.
    """
    from app.services.business_day_service import BusinessDayService
    from app.services.nozzle_service import NozzleService

    manager = DatabaseManager(db_file)
    return BusinessDayService(manager), NozzleService(manager)


# ---------------------------------------------------------------------------
# helpers (parameterized SQL only)
# ---------------------------------------------------------------------------
def nozzle_id(conn: sqlite3.Connection, code: str) -> int:
    """Primary key of a seed nozzle, looked up by code (never hard-coded)."""
    row = conn.execute("SELECT id FROM nozzles WHERE code = ?", (code,)).fetchone()
    assert row is not None, f"seed nozzle {code!r} missing — migration 001 seed failed?"
    return int(row["id"])


def insert_business_day(
    conn: sqlite3.Connection,
    business_date: str,
    *,
    status: str = "open",
    opening_cash_paisa: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO business_days (business_date, status, opening_cash_paisa)"
        " VALUES (?, ?, ?)",
        (business_date, status, opening_cash_paisa),
    )


def insert_reading(
    conn: sqlite3.Connection,
    nozzle_code: str,
    business_date: str,
    reading_ml: int,
    *,
    kind: str = "closing",
    created_by: str = "system",
) -> int:
    cur = conn.execute(
        "INSERT INTO nozzle_readings"
        " (nozzle_id, business_date, reading_kind, reading_ml, created_by)"
        " VALUES (?, ?, ?, ?, ?)",
        (nozzle_id(conn, nozzle_code), business_date, kind, reading_ml, created_by),
    )
    return int(cur.lastrowid)


def insert_meter_event(
    conn: sqlite3.Connection,
    nozzle_code: str,
    event_type: str,
    *,
    reason: str = "meter replaced by technician",
    reading_before_ml: int | None = None,
    reading_after_ml: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO meter_events"
        " (nozzle_id, event_type, reading_before_ml, reading_after_ml,"
        "  reason, authorized_by)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            nozzle_id(conn, nozzle_code),
            event_type,
            reading_before_ml,
            reading_after_ml,
            reason,
            "manager",
        ),
    )
    return int(cur.lastrowid)


def insert_day_event(
    conn: sqlite3.Connection,
    business_date: str,
    event_type: str,
    *,
    reason: str | None = None,
    actor: str = "system",
) -> int:
    cur = conn.execute(
        "INSERT INTO business_day_events (business_date, event_type, actor, reason)"
        " VALUES (?, ?, ?, ?)",
        (business_date, event_type, actor, reason),
    )
    return int(cur.lastrowid)


def audit_rows(conn: sqlite3.Connection, **filters) -> list[sqlite3.Row]:
    """All audit_log rows matching the given column=value filters."""
    sql = "SELECT * FROM audit_log"
    params: list = []
    if filters:
        sql += " WHERE " + " AND ".join(f"{col} = ?" for col in filters)
        params = list(filters.values())
    sql += " ORDER BY id"
    return conn.execute(sql, params).fetchall()


def audit_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# Phase-3 additions (appended by the Phase-3 test agent; everything above is
# untouched). Fixtures/helpers for the price, delivery, inventory and sales
# services. Service imports are LAZY (inside the fixture) so this module stays
# importable — and the rest of the suite keeps running — before the services
# agent lands the Phase-3 service modules. A test that needs a service that
# does not exist yet ERRORS at fixture setup with the ImportError: that is
# the honest signal, not a skip.
# ---------------------------------------------------------------------------

#: Default price used by the Phase-3 seed helpers (PKR per litre).
DEFAULT_PRICE_PKR = Decimal("265.9630")

#: Seed nozzle codes from migration 001 (looked up, never hard-coded ids).
PETROL_NOZZLES = ("1 PG+", "2 PG+")
DIESEL_NOZZLES = ("3 HSD", "4 HSD")


@pytest.fixture
def phase3_services(db, db_file):
    """All six services sharing one DatabaseManager (Phase-3 tests).

    Depends on ``db`` so the file is created and migrated first. Service
    methods open their own connections; keep the ``db`` connection idle
    (committed) while services run.
    """
    from app.services.business_day_service import BusinessDayService
    from app.services.delivery_service import DeliveryService
    from app.services.inventory_service import InventoryService
    from app.services.nozzle_service import NozzleService
    from app.services.price_service import PriceService
    from app.services.sales_service import SalesService

    manager = DatabaseManager(db_file)
    return SimpleNamespace(
        days=BusinessDayService(manager),
        nozzles=NozzleService(manager),
        prices=PriceService(manager),
        deliveries=DeliveryService(manager),
        inventory=InventoryService(manager),
        sales=SalesService(manager),
    )


def tank_id(conn: sqlite3.Connection, code: str) -> int:
    """Primary key of a seed tank, looked up by code (never hard-coded)."""
    row = conn.execute("SELECT id FROM tanks WHERE code = ?", (code,)).fetchone()
    assert row is not None, f"seed tank {code!r} missing — migration 007 seed failed?"
    return int(row["id"])


def fuel_product_id(conn: sqlite3.Connection, code: str) -> int:
    """Primary key of a fuel product, looked up by code (never hard-coded)."""
    row = conn.execute(
        "SELECT id FROM fuel_products WHERE code = ?", (code,)
    ).fetchone()
    assert row is not None, f"fuel product {code!r} missing"
    return int(row["id"])


def new_uuid() -> str:
    """A fresh UUID string for raw-SQL inserts that need unique uuids."""
    return uuid.uuid4().hex


def pkt_midnight_utc_iso(business_date: str) -> str:
    """``YYYY-MM-DDTHH:MM:SS.SSSZ`` for 00:00 Asia/Karachi on *business_date*.

    PKT is UTC+5 with no DST, so midnight PKT is 19:00 UTC the day before.
    """
    day = datetime.strptime(business_date, "%Y-%m-%d")
    return (day - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def pkt_clock_utc_iso(business_date: str, hour: int, minute: int = 0) -> str:
    """``YYYY-MM-DDTHH:MM:SS.SSSZ`` for HH:MM Asia/Karachi on *business_date*."""
    moment = datetime.strptime(business_date, "%Y-%m-%d") + timedelta(
        hours=hour, minutes=minute
    )
    return (moment - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def dto_field(obj, *names):
    """Read a field from a service DTO, dict, or sqlite3.Row.

    Tries each *name* in order via attribute access, then mapping access.
    Raises AssertionError naming the missing field and the object repr so a
    DTO-shape deviation is easy to adapt to.
    """
    for name in names:
        if isinstance(obj, dict):
            if name in obj:
                return obj[name]
            continue
        try:
            return getattr(obj, name)
        except AttributeError:
            pass
        try:
            return obj[name]
        except (KeyError, IndexError, TypeError):
            pass
    raise AssertionError(f"none of {names!r} found on {obj!r}")


def seed_first_day(
    svc: SimpleNamespace,
    business_date: str,
    *,
    price_pkr: Decimal | str = DEFAULT_PRICE_PKR,
    diesel_price_pkr: Decimal | str | None = None,
    opening_cash_paisa: int = 2_000_000,
    opening_stock_litres: Decimal | str | None = None,
    supplier_name: str = "PSO Depot",
) -> int:
    """Full setup for the FIRST business day: open, supplier, prices, stock.

    Sets PETROL and DIESEL prices effective at the day's 00:00 PKT and,
    when given, the manager-entered opening stock (the first-day entry
    path — later days carry stock forward automatically at open, so
    ``set_opening_stock`` must NOT be called again for them). Returns the
    supplier id. Pass ``Decimal``/``str`` for money — never float.
    """
    svc.days.open_day(business_date, opening_cash_paisa=opening_cash_paisa)
    supplier_id = svc.deliveries.add_supplier(supplier_name)
    effective = pkt_midnight_utc_iso(business_date)
    svc.prices.set_price("PETROL", Decimal(price_pkr), effective, business_date)
    svc.prices.set_price(
        "DIESEL",
        Decimal(diesel_price_pkr if diesel_price_pkr is not None else price_pkr),
        effective,
        business_date,
    )
    if opening_stock_litres is not None:
        svc.inventory.set_opening_stock(
            "PETROL", Decimal(opening_stock_litres), business_date
        )
    return int(supplier_id)


def seed_phase3_day(
    svc: SimpleNamespace,
    business_date: str,
    *,
    opening_cash_paisa: int = 2_000_000,
    supplier_name: str = "PSO Depot",
) -> int:
    """Setup for a SUBSEQUENT business day: open it and add a supplier.

    Prices and opening stock are NOT set here — they carry forward from the
    previous day by design (``price_effective_at_day_end`` /
    ``_carry_stock_forward``). Re-setting an unchanged day-open price would
    trip the service's §11.2 snapshot rule (deviation note in the Phase-3
    test modules); a real mid-day price change goes through
    ``record_segment_reading`` + ``set_price`` explicitly. Returns the
    supplier id.
    """
    svc.days.open_day(business_date, opening_cash_paisa=opening_cash_paisa)
    return int(svc.deliveries.add_supplier(supplier_name))


def close_prior_day(
    svc: SimpleNamespace,
    business_date: str,
    readings: dict[str, str],
    *,
    price_pkr: Decimal | str = DEFAULT_PRICE_PKR,
    opening_stock_litres: Decimal | str | None = None,
    physical_cash_paisa: int = 2_000_000,
) -> None:
    """Open a (first) day, set prices/stock, record closings, post, close it.

    Used to establish previous-day meter values for the day under test. On
    a first day there are no previous readings, so every interval is of
    unknown quantity and ``post_day_sales`` skips them — nothing is posted,
    and the close still succeeds.
    """
    seed_first_day(
        svc,
        business_date,
        price_pkr=price_pkr,
        opening_stock_litres=opening_stock_litres,
    )
    for code, litres in readings.items():
        svc.nozzles.record_reading(business_date, code, litres)
    svc.sales.post_day_sales(business_date)
    svc.days.close_day(business_date, physical_cash_paisa=physical_cash_paisa)


def ensure_phase3_close_preconditions(
    db_file: Path,
    business_date: str,
    *,
    opening_stock_litres: Decimal | str | None = None,
) -> None:
    """Phase-3 close preconditions for an already-open day (test bridge).

    Phase-1/2 tests close days without ever setting fuel prices; Phase 3's
    close checklist honestly requires applicable prices (and postable sales
    need stock). This sets PETROL+DIESEL prices effective at the day's
    00:00 PKT plus optional manager-entered opening stock for both products.
    Production behavior is unchanged — this is test maintenance for the new
    checklist contract.
    """
    from app.services.inventory_service import InventoryService
    from app.services.price_service import PriceService

    manager = DatabaseManager(db_file)
    prices = PriceService(manager)
    effective = pkt_midnight_utc_iso(business_date)
    prices.set_price("PETROL", Decimal(DEFAULT_PRICE_PKR), effective, business_date)
    prices.set_price("DIESEL", Decimal(DEFAULT_PRICE_PKR), effective, business_date)
    if opening_stock_litres is not None:
        inventory = InventoryService(manager)
        inventory.set_opening_stock(
            "PETROL", Decimal(opening_stock_litres), business_date
        )
        inventory.set_opening_stock(
            "DIESEL", Decimal(opening_stock_litres), business_date
        )


def insert_price_row(
    conn: sqlite3.Connection,
    product_code: str,
    effective_from_utc: str,
    unit_price_10k: int,
    business_date: str,
    *,
    created_by: str = "system",
) -> int:
    """Insert a ``price_history`` row with raw SQL (test setup only).

    Used where the service's §11.2 snapshot rule would wrongly block a
    day-open price on a non-first day (deviation note in the Phase-3 test
    modules): the row itself is exactly what ``set_price`` would have
    written. The day must be open (D1 trigger).
    """
    cur = conn.execute(
        "INSERT INTO price_history (fuel_product_id, effective_from_utc,"
        " unit_price_10k, business_date, created_by)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            fuel_product_id(conn, product_code),
            effective_from_utc,
            unit_price_10k,
            business_date,
            created_by,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def record_reset_interval(
    svc: SimpleNamespace,
    business_date: str,
    nozzle_code: str,
    prev_litres: str,
    *,
    new_litres: str = "100",
) -> None:
    """Record a meter reset + backwards reading for a nozzle.

    The interval quantity becomes unknown (``None``) — never a 0 L interval
    (which ``post_day_sales`` cannot post: a 0-quantity sale movement
    violates the ledger CHECK) and never a negative sale (§11.1). Used in
    tests to keep non-moving nozzles out of the posted sales while still
    satisfying the close checklist's ``readings_complete`` item.
    """
    svc.nozzles.record_meter_event(
        nozzle_code,
        "reset",
        reason="meter reset by technician (test setup)",
        reading_before_litres=Decimal(prev_litres),
        reading_after_litres=Decimal(new_litres),
    )
    svc.nozzles.record_reading(business_date, nozzle_code, new_litres)


# ---------------------------------------------------------------------------
# Phase-4 additions. Same lazy-import pattern as the Phase-3 block above:
# service imports stay inside the fixture so this module stays importable
# without the credit/payment services on disk.
# ---------------------------------------------------------------------------


@pytest.fixture
def phase4_services(phase3_services, db_file):
    """Phase-3 services plus customer credit and payment services.

    Builds on ``phase3_services`` (the same temp database) and adds
    ``credit`` (CustomerCreditService) and ``payments`` (PaymentService)
    to the namespace.
    """
    from app.database.connection import DatabaseManager
    from app.services.customer_credit_service import CustomerCreditService
    from app.services.payment_service import PaymentService

    manager = DatabaseManager(db_file)
    phase3_services.credit = CustomerCreditService(manager)
    phase3_services.payments = PaymentService(manager)
    return phase3_services


def seed_credit_day(
    svc,
    business_date: str,
    *,
    opening_cash_paisa: int = 2_000_000,
    price_pkr: Decimal | str = DEFAULT_PRICE_PKR,
    customer_name: str = "Test Customer",
) -> int:
    """Open a business day, set fuel prices, and create one customer.

    Returns the customer id. Prices are effective at the day's 00:00 PKT
    so credit tickets price without surprises.
    """
    svc.days.open_day(business_date, opening_cash_paisa=opening_cash_paisa)
    effective = pkt_midnight_utc_iso(business_date)
    svc.prices.set_price("PETROL", Decimal(price_pkr), effective, business_date)
    svc.prices.set_price("DIESEL", Decimal(price_pkr), effective, business_date)
    return svc.credit.create_customer(customer_name).id


# ---------------------------------------------------------------------------
# Phase-5 additions. Same lazy-import pattern as the Phase-3/4 blocks above:
# service imports stay inside the fixture so this module stays importable
# without the cashbook/expense/supplier/accounting/other-materials services
# on disk.
# ---------------------------------------------------------------------------


@pytest.fixture
def phase5_services(phase4_services, db_file):
    """Phase-4 services plus the cashbook, expense, supplier, accounting
    and other-materials services.

    Builds on ``phase4_services`` (the same temp database) and adds
    ``cash`` (CashbookService), ``expenses`` (ExpenseService), ``suppliers``
    (SupplierService), ``accounting`` (AccountingService) and ``materials``
    (OtherMaterialsService) to the namespace.
    """
    from app.database.connection import DatabaseManager
    from app.services.accounting_service import AccountingService
    from app.services.cashbook_service import CashbookService
    from app.services.expense_service import ExpenseService
    from app.services.other_materials_service import OtherMaterialsService
    from app.services.supplier_service import SupplierService

    manager = DatabaseManager(db_file)
    phase4_services.cash = CashbookService(manager)
    phase4_services.expenses = ExpenseService(manager)
    phase4_services.suppliers = SupplierService(manager)
    phase4_services.accounting = AccountingService(manager)
    phase4_services.materials = OtherMaterialsService(manager)
    return phase4_services
