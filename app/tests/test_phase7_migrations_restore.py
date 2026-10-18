"""Phase-7 migration safety + backup/restore round-trip tests.

Covers the migration contract (numbered, tested, history-preserving,
version bump only after success — design plan §1/§4) and the restore
workflow that Phase 8's backup service will build on:

* fresh install reaches ``CURRENT_SCHEMA_VERSION`` with a clean
  ``verify_schema()`` and every expected object present;
* re-running ``migrate()`` on a populated DB is a no-op preserving data;
* upgrading an older database (previous version → current) preserves
  business rows and installs the new triggers, functionally spot-checked;
* a database newer than this build is refused without touching data —
  both at the ``migrate()`` level and through the ``bootstrap()`` startup
  path;
* a mid-migration failure rolls back cleanly: version not bumped, broken
  objects absent, prior data and triggers working;
* a WAL database snapshotted with the sqlite3 online backup API restores
  bit-faithfully: integrity clean, FKs clean, dates/closings/readings,
  customer ledger, stock position, cash position and audit history all
  match.

Every test uses a FRESH temporary database (pytest ``tmp_path`` honours
``TMPDIR``); the live database is never touched. The schema version is
never hard-coded — ``migrator.CURRENT_SCHEMA_VERSION`` is the single
source of truth so these tests survive future migrations.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import astuple
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config
from app.bootstrap import bootstrap
from app.config import AppStartupError
from app.database import migrator
from app.database.connection import DatabaseManager
from app.database.migrator import (
    CURRENT_SCHEMA_VERSION,
    EXPECTED_INDEXES,
    EXPECTED_TABLES,
    EXPECTED_TRIGGERS,
    EXPECTED_VIEWS,
    MigrationError,
    get_schema_version,
    migrate,
    verify_schema,
)

from .conftest import (
    close_prior_day,
    insert_business_day,
    insert_reading,
    pkt_clock_utc_iso,
    record_reset_interval,
    seed_phase3_day,
)


@pytest.fixture(autouse=True)
def _temp_backup_dir(monkeypatch, tmp_path):
    """Redirect the *default* backup dir into tmp.

    The day-close auto-backup hook constructs its ``BackupService``
    without an explicit ``backup_dir``; the database it backs up is
    already the temp DB, and this keeps the hook's *files* in tmp too.
    """
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")

DAY = "2026-10-15"
PREV = "2026-10-14"
PREV_READINGS = {
    "1 PG+": "84938",
    "2 PG+": "81234",
    "3 HSD": "60000",
    "4 HSD": "70000",
}
PRICE_250 = Decimal("250")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _phase5_services(manager: DatabaseManager) -> SimpleNamespace:
    """All Phase-5 services bound to one DatabaseManager (lazy imports, like
    conftest, so this module stays importable on its own)."""
    from app.services.accounting_service import AccountingService
    from app.services.business_day_service import BusinessDayService
    from app.services.cashbook_service import CashbookService
    from app.services.customer_credit_service import CustomerCreditService
    from app.services.delivery_service import DeliveryService
    from app.services.expense_service import ExpenseService
    from app.services.inventory_service import InventoryService
    from app.services.nozzle_service import NozzleService
    from app.services.payment_service import PaymentService
    from app.services.price_service import PriceService
    from app.services.sales_service import SalesService

    return SimpleNamespace(
        days=BusinessDayService(manager),
        nozzles=NozzleService(manager),
        prices=PriceService(manager),
        deliveries=DeliveryService(manager),
        inventory=InventoryService(manager),
        sales=SalesService(manager),
        credit=CustomerCreditService(manager),
        payments=PaymentService(manager),
        expenses=ExpenseService(manager),
        cash=CashbookService(manager),
        accounting=AccountingService(manager),
    )


def _migrated_db(db_file: Path) -> DatabaseManager:
    """Fresh file → pragma-verified connection → fully migrated."""
    manager = DatabaseManager(db_file)
    conn = manager.connect()
    try:
        migrate(conn)
    finally:
        conn.close()
    return manager


def _full_business_day(svc: SimpleNamespace) -> dict:
    """Populate one realistic business day through the service layer.

    Prior day closed; DAY gets a fuel delivery, a 47 L metered petrol sale,
    a 40 L credit ticket with a partial payment, a cash expense, posted
    sales and a completed close. Returns key ids/amounts for assertions.
    """
    close_prior_day(
        svc,
        PREV,
        PREV_READINGS,
        price_pkr=PRICE_250,
        opening_stock_litres=Decimal("10000"),
    )
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
    svc.credit.create_customer("Day Customer")
    ticket = svc.credit.issue_ticket(
        "CUST-0001",
        DAY,
        [
            {
                "fuel_product_code": "PETROL",
                "nozzle_code": None,
                "quantity_litres": Decimal("40"),
            }
        ],
        occurred_at_utc=pkt_clock_utc_iso(DAY, 10),
    )
    payment = svc.payments.record_payment(
        "CUST-0001", DAY, 500_000, occurred_at_utc=pkt_clock_utc_iso(DAY, 12)
    )
    expense = svc.expenses.record_expense(
        business_date=DAY,
        category="Salaries",
        amount_paisa=200_000,
        method="cash",
        occurred_at_utc=pkt_clock_utc_iso(DAY, 14),
    )
    svc.nozzles.record_reading(DAY, "1 PG+", "84985")  # 47 L metered sale
    for code in ("2 PG+", "3 HSD", "4 HSD"):
        record_reset_interval(svc, DAY, code, PREV_READINGS[code])
    svc.sales.post_day_sales(DAY)
    # close_day backfills derived cash postings + the settlement batch, then
    # runs the checklist inside one transaction.
    closing = svc.days.close_day(DAY, physical_cash_paisa=2_000_000)
    return {
        "delivery_id": int(delivery_id),
        "ticket_id": int(ticket.id),
        "ticket_gross_paisa": int(ticket.gross_amount_paisa),
        "payment_id": int(payment.id),
        "expense_id": int(expense.id),
        "closing_revision": int(closing.closing_revision),
    }


def _business_snapshot(manager: DatabaseManager, conn: sqlite3.Connection) -> dict:
    """Service-level business facts used to compare source vs restored DBs."""
    svc = _phase5_services(manager)
    return {
        "outstanding_paisa": int(svc.credit.outstanding("CUST-0001")),
        "stock": [astuple(p) for p in svc.inventory.stock_positions(DAY)],
        "cash_position": astuple(svc.cash.cash_position(DAY)),
        "closings": [astuple(c) for c in svc.days.closings(DAY)],
        "audit_count": int(
            conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        ),
    }


def _table_dump(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """Every user table's rows as tuples, ordered by rowid — the strictest
    possible source-vs-restored comparison."""
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
    ]
    dump: dict[str, list[tuple]] = {}
    for table in tables:
        rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
        dump[table] = [tuple(row) for row in rows]
    return dump


def _copy_migrations_excluding(dest: Path, exclude_prefix: str) -> None:
    """Copy the migrations directory minus one file (upgrade-path tests)."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(migrator.MIGRATIONS_DIR, dest)
    for path in dest.glob(f"{exclude_prefix}*.sql"):
        path.unlink()


def _assert_meter_events_preserved(
    before_rows: list[tuple], after_rows: list[tuple]
) -> None:
    """meter_events rows survive the upgrade.

    Migration 013 rebuilt this table, appending ``created_at_utc`` /
    ``updated_at_utc`` backfilled from ``occurred_at_utc`` (its documented
    rule); the business columns keep their order. Any later migration must
    leave the rows byte-identical.
    """
    assert len(after_rows) == len(before_rows)
    for old, new in zip(before_rows, after_rows):
        assert old[:9] == new[:9]  # business columns, order preserved
        if len(new) == 11 and len(old) == 9:
            occurred_at_utc = old[5]
            assert new[9] == occurred_at_utc
            assert new[10] == occurred_at_utc
        else:
            assert old == new


def _assert_upgrade_preserved(
    before: dict[str, list[tuple]], after: dict[str, list[tuple]]
) -> None:
    """Business rows survive the upgrade.

    Three tables can legitimately change under the newest migrations and
    are asserted precisely instead of byte-compared:

    * ``schema_migrations`` gains exactly the new version row;
    * ``meter_events`` — see ``_assert_meter_events_preserved``;
    * ``backup_history`` gains the nullable ``trigger`` column (migration
      015, appended last); pre-015 rows come back with ``trigger`` NULL —
      the day-close auto-backup hook writes rows here, so this table is
      no longer guaranteed empty;
    * ``users`` is created empty by migration 016 (owner auth) — it did
      not exist before the upgrade.
    """
    expected_new = {"users"}
    assert set(after) == set(before) | expected_new
    for table in expected_new:
        assert after[table] == [], f"table {table} must be empty after upgrade"
    before_versions = [row[0] for row in before["schema_migrations"]]
    after_versions = [row[0] for row in after["schema_migrations"]]
    assert after_versions == before_versions + [CURRENT_SCHEMA_VERSION]

    for table, rows in before.items():
        if table == "schema_migrations":
            continue
        if table == "meter_events":
            _assert_meter_events_preserved(rows, after[table])
            continue
        if table == "backup_history":
            after_rows = after[table]
            assert len(after_rows) == len(rows)
            for after_row, before_row in zip(after_rows, rows):
                if len(after_row) == len(before_row):
                    # Both sides already carry the trigger column
                    # (upgrade from 015+): rows must be byte-identical.
                    assert after_row == before_row
                else:
                    # Pre-015 columns unchanged; the appended trigger column
                    # reads back NULL for rows written before 015.
                    assert after_row[:-1] == before_row
                    assert after_row[-1] is None
            continue
        assert after[table] == rows, f"table {table} changed by upgrade"


# ---------------------------------------------------------------------------
# 1. fresh install
# ---------------------------------------------------------------------------
def test_fresh_install_reaches_current_version(tmp_path):
    """Empty DB → migrate() applies everything: version is
    CURRENT_SCHEMA_VERSION and verify_schema() is clean."""
    db_file = tmp_path / "fresh.sqlite3"
    manager = DatabaseManager(db_file)
    conn = manager.connect()
    try:
        applied = migrate(conn)
        assert applied == CURRENT_SCHEMA_VERSION
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
        assert verify_schema(conn) == []
    finally:
        conn.close()


def test_fresh_install_has_all_expected_objects(tmp_path):
    """Every table, index, view and trigger the build expects is present
    after a fresh migrate — checked against the migrator's EXPECTED_*
    lists, not a hard-coded inventory."""
    db_file = tmp_path / "fresh.sqlite3"
    manager = DatabaseManager(db_file)
    conn = manager.connect()
    try:
        migrate(conn)
        objects = {
            (row["type"], row["name"])
            for row in conn.execute(
                "SELECT type, name FROM sqlite_master"
            ).fetchall()
        }
        for table in EXPECTED_TABLES:
            assert ("table", table) in objects, f"missing table {table}"
        for index in EXPECTED_INDEXES:
            assert ("index", index) in objects, f"missing index {index}"
        for view in EXPECTED_VIEWS:
            assert ("view", view) in objects, f"missing view {view}"
        for trigger in EXPECTED_TRIGGERS:
            assert ("trigger", trigger) in objects, f"missing trigger {trigger}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. idempotent re-run on a populated DB
# ---------------------------------------------------------------------------
def test_migrate_twice_on_populated_db_is_noop_and_preserves_data(db):
    """migrate() twice over user data: same version, 0 applied, no errors,
    every business row byte-identical afterwards."""
    insert_business_day(db, "2026-10-10", opening_cash_paisa=1_500_000)
    reading_id = insert_reading(db, "1 PG+", "2026-10-10", 84_938_000)
    db.commit()

    before = _table_dump(db)
    version_before = get_schema_version(db)

    assert migrate(db) == 0
    assert migrate(db) == 0
    assert get_schema_version(db) == version_before == CURRENT_SCHEMA_VERSION
    assert verify_schema(db) == []
    assert _table_dump(db) == before

    row = db.execute(
        "SELECT business_date, opening_cash_paisa FROM business_days "
        "WHERE business_date = '2026-10-10'"
    ).fetchone()
    assert tuple(row) == ("2026-10-10", 1_500_000)
    row = db.execute(
        "SELECT reading_ml FROM nozzle_readings WHERE id = ?", (reading_id,)
    ).fetchone()
    assert row[0] == 84_938_000


# ---------------------------------------------------------------------------
# 3. data preservation across an upgrade (v(N-1) -> current)
# ---------------------------------------------------------------------------
def test_upgrade_preserves_business_data(tmp_path, monkeypatch):
    """A database migrated to the previous version, populated through the
    service layer, then upgraded: every business row survives, the version
    lands on CURRENT_SCHEMA_VERSION, and a second migrate() is a no-op."""
    latest_file = sorted(migrator.MIGRATIONS_DIR.glob("*.sql"))[-1].name
    latest_version = int(latest_file.split("_")[0])
    assert latest_version == CURRENT_SCHEMA_VERSION

    prev_dir = tmp_path / "migrations_prev"
    _copy_migrations_excluding(prev_dir, f"{latest_version:03d}_")

    db_file = tmp_path / "upgrade.sqlite3"
    real_dir = migrator.MIGRATIONS_DIR
    monkeypatch.setattr(migrator, "MIGRATIONS_DIR", prev_dir)
    manager = DatabaseManager(db_file)
    conn = manager.connect()
    try:
        migrate(conn)
        base_version = get_schema_version(conn)
        assert 0 < base_version < CURRENT_SCHEMA_VERSION
    finally:
        conn.close()

    # Populate at the older version through the real service layer.
    svc = _phase5_services(manager)
    keys = _full_business_day(svc)
    assert keys["ticket_gross_paisa"] == 1_000_000  # 40 L @ 250 PKR/L

    dump_conn = manager.connect()
    try:
        source_dump = _table_dump(dump_conn)
    finally:
        dump_conn.close()

    # Upgrade to the current build.
    monkeypatch.setattr(migrator, "MIGRATIONS_DIR", real_dir)
    conn = manager.connect()
    try:
        applied = migrate(conn)
        assert applied == CURRENT_SCHEMA_VERSION - base_version
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
        assert verify_schema(conn) == []
        assert migrate(conn) == 0  # second run is a no-op
        _assert_upgrade_preserved(source_dump, _table_dump(conn))
    finally:
        conn.close()

    # Business facts still readable through the services after upgrade.
    svc2 = _phase5_services(manager)
    assert svc2.credit.outstanding("CUST-0001") == 500_000
    (closing,) = svc2.days.closings(DAY)
    assert closing.business_date == DAY
    assert closing.closing_revision == keys["closing_revision"]


def test_upgrade_installs_new_triggers_functional(tmp_path, monkeypatch):
    """The upgrade's new triggers exist and actually fire.

    Spot-checks the newest migration's additions: the schema_migrations
    append-only tamper guard and the backup_history no-delete guard.
    """
    previous = sorted(migrator.MIGRATIONS_DIR.glob("*.sql"))[-1].name
    prev_version = int(previous.split("_")[0])
    assert prev_version == CURRENT_SCHEMA_VERSION

    prev_dir = tmp_path / "migrations_prev"
    _copy_migrations_excluding(prev_dir, f"{prev_version:03d}_")

    db_file = tmp_path / "upgrade_trg.sqlite3"
    real_dir = migrator.MIGRATIONS_DIR
    monkeypatch.setattr(migrator, "MIGRATIONS_DIR", prev_dir)
    manager = DatabaseManager(db_file)
    conn = manager.connect()
    migrate(conn)
    conn.close()

    monkeypatch.setattr(migrator, "MIGRATIONS_DIR", real_dir)
    conn = manager.connect()
    try:
        migrate(conn)
        triggers = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        for name in (
            "trg_schema_migrations_bu_no_modify",
            "trg_schema_migrations_bd_no_modify",
            "trg_backup_history_bd_no_delete",
            "trg_meter_events_bu_touch_updated",
        ):
            assert name in triggers, f"upgrade did not install {name}"

        # Tamper guard fires: UPDATE and DELETE on schema_migrations abort.
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE schema_migrations SET description = 'x'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM schema_migrations")
        conn.rollback()
        # The version record itself is untouched by the blocked writes.
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION

        # No-delete guard fires on backup_history; the row survives.
        conn.execute(
            "INSERT INTO backup_history (filename, destination, status)"
            " VALUES ('probe.bak', 'local', 'requested')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            conn.execute("DELETE FROM backup_history")
        conn.rollback()
        assert (
            conn.execute("SELECT COUNT(*) FROM backup_history").fetchone()[0]
            == 1
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. newer-version refusal at the migrate() level
# ---------------------------------------------------------------------------
def test_migrate_refuses_newer_version_without_touching_data(db_file):
    """A DB claiming a newer version than this build: migrate() raises
    MigrationError and not a single user row is modified or deleted."""
    manager = _migrated_db(db_file)
    conn = manager.connect()
    insert_business_day(conn, "2026-10-10", opening_cash_paisa=1_500_000)
    reading_id = insert_reading(conn, "1 PG+", "2026-10-10", 84_938_000)
    conn.commit()
    before = _table_dump(conn)

    # Simulate a database written by a NEWER app build.
    conn.execute(
        "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
        (CURRENT_SCHEMA_VERSION + 1, "hypothetical future migration"),
    )
    conn.commit()
    assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION + 1
    before = _table_dump(conn)  # baseline *includes* the simulated newer row

    with pytest.raises(MigrationError, match="newer"):
        migrate(conn)

    # Nothing was modified or deleted: every user table is byte-identical.
    after = _table_dump(conn)
    assert set(after) == set(before)
    for table, rows in before.items():
        assert after[table] == rows, f"table {table} changed by refused migrate"
    row = conn.execute(
        "SELECT reading_ml FROM nozzle_readings WHERE id = ?", (reading_id,)
    ).fetchone()
    assert row[0] == 84_938_000
    conn.close()


# ---------------------------------------------------------------------------
# 5. mid-migration failure: atomic, recoverable
# ---------------------------------------------------------------------------
def test_mid_migration_failure_leaves_clean_recoverable_state(
    tmp_path, monkeypatch
):
    """A broken migration file: MigrationError naming the file, version NOT
    bumped, the broken migration's objects absent, prior data intact, and
    the database still opens with working triggers."""
    db_file = tmp_path / "broken.sqlite3"
    manager = _migrated_db(db_file)
    conn = manager.connect()
    insert_business_day(conn, "2026-10-10", opening_cash_paisa=1_500_000)
    reading_id = insert_reading(conn, "1 PG+", "2026-10-10", 84_938_000)
    conn.commit()
    before = _table_dump(conn)
    versions_before = conn.execute(
        "SELECT COUNT(*) FROM schema_migrations"
    ).fetchone()[0]
    conn.close()

    broken_dir = tmp_path / "migrations_broken"
    if broken_dir.exists():
        shutil.rmtree(broken_dir)
    shutil.copytree(migrator.MIGRATIONS_DIR, broken_dir)
    broken_name = f"{CURRENT_SCHEMA_VERSION + 1:03d}_broken_probe.sql"
    (broken_dir / broken_name).write_text(
        "-- failure-injection probe: creates an object, then dies\n"
        "CREATE TABLE broken_probe (id INTEGER PRIMARY KEY);\n"
        "THIS IS NOT VALID SQL;\n",
        encoding="utf-8",
    )
    real_dir = migrator.MIGRATIONS_DIR
    monkeypatch.setattr(migrator, "MIGRATIONS_DIR", broken_dir)

    conn = manager.connect()
    try:
        with pytest.raises(MigrationError, match="broken_probe"):
            migrate(conn)
    finally:
        monkeypatch.setattr(migrator, "MIGRATIONS_DIR", real_dir)
        conn.close()

    # Recoverable state: version not bumped, no half-applied objects, prior
    # data intact, schema still verifies.
    conn = manager.connect()
    try:
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
        assert (
            conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            == versions_before
        )
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "broken_probe" not in tables
        assert verify_schema(conn) == []
        assert _table_dump(conn) == before
        row = conn.execute(
            "SELECT reading_ml FROM nozzle_readings WHERE id = ?",
            (reading_id,),
        ).fetchone()
        assert row[0] == 84_938_000
    finally:
        conn.close()

    # The database still opens through the pragma-verified manager and its
    # triggers still fire (posted readings stay immutable).
    conn = manager.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE nozzle_readings SET reading_ml = ? WHERE id = ?",
                (84_939_000, reading_id),
            )
        conn.rollback()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6. bootstrap refuses an incompatible (newer) database
# ---------------------------------------------------------------------------
def test_bootstrap_refuses_newer_database_without_touching_data(db_file):
    """bootstrap() on a newer-version DB raises AppStartupError with a clear
    message and does not delete, replace or modify any user data."""
    manager = _migrated_db(db_file)
    conn = manager.connect()
    insert_business_day(conn, "2026-10-10", opening_cash_paisa=1_500_000)
    reading_id = insert_reading(conn, "1 PG+", "2026-10-10", 84_938_000)
    conn.commit()
    conn.execute(
        "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
        (CURRENT_SCHEMA_VERSION + 1, "hypothetical future migration"),
    )
    conn.commit()
    conn.close()

    with pytest.raises(AppStartupError) as excinfo:
        bootstrap(db_path=db_file)
    message = str(excinfo.value)
    assert "upgrade failed" in message.lower()
    assert "newer" in message.lower()
    assert "NOT been deleted or modified" in message

    # The file was not deleted, replaced or modified: the business row is
    # intact and the version still claims the newer build.
    assert db_file.exists()
    conn = manager.connect()
    try:
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION + 1
        row = conn.execute(
            "SELECT business_date, opening_cash_paisa FROM business_days "
            "WHERE business_date = '2026-10-10'"
        ).fetchone()
        assert tuple(row) == ("2026-10-10", 1_500_000)
        row = conn.execute(
            "SELECT reading_ml FROM nozzle_readings WHERE id = ?",
            (reading_id,),
        ).fetchone()
        assert row[0] == 84_938_000
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 7. restore from backup via the sqlite3 online backup API
# ---------------------------------------------------------------------------
def test_backup_api_restore_roundtrip_preserves_everything(tmp_path):
    """Populate via the service layer (WAL active) → snapshot with
    ``source.backup(target)`` → restore into a fresh location → the restored
    database matches the source on every business fact."""
    source_file = tmp_path / "source.sqlite3"
    restore_file = tmp_path / "restore.sqlite3"

    manager = _migrated_db(source_file)
    svc = _phase5_services(manager)
    keys = _full_business_day(svc)
    assert keys["closing_revision"] == 1

    source_conn = manager.connect()
    try:
        assert (
            source_conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
            == "wal"
        ), "restore test requires WAL mode on the source database"
        source_dump = _table_dump(source_conn)
        source_facts = _business_snapshot(manager, source_conn)

        target_conn = sqlite3.connect(str(restore_file))
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
    finally:
        source_conn.close()

    # The restored database opens through the pragma-verified manager and is
    # structurally sound.
    restore_manager = DatabaseManager(restore_file)
    restored = restore_manager.connect()
    try:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restored.execute("PRAGMA foreign_key_check").fetchall() == []
        assert get_schema_version(restored) == CURRENT_SCHEMA_VERSION
        assert verify_schema(restored) == []

        # Every table, row for row.
        assert _table_dump(restored) == source_dump

        # Named business facts, spelled out for the failure message.
        src_days = [
            tuple(r)
            for r in source_dump["business_days"]
        ]
        assert [tuple(r) for r in _table_dump(restored)["business_days"]] == (
            src_days
        )
        assert (
            _table_dump(restored)["business_day_closings"]
            == source_dump["business_day_closings"]
        )
        assert (
            _table_dump(restored)["nozzle_readings"]
            == source_dump["nozzle_readings"]
        )

        # Service-level positions agree exactly.
        restored_facts = _business_snapshot(restore_manager, restored)
        assert restored_facts == source_facts
        assert restored_facts["outstanding_paisa"] == 500_000
        assert restored_facts["audit_count"] == source_facts["audit_count"] > 0
    finally:
        restored.close()
