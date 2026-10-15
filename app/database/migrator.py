"""Versioned SQL migrations.

Numbered ``NNN_*.sql`` files in ``app/database/migrations/`` are the *only*
way the schema changes. Each migration is applied inside its own transaction
(``BEGIN IMMEDIATE`` … ``COMMIT``); on failure the transaction is rolled back
and a :class:`MigrationError` naming the file is raised. The applied version
is recorded in ``schema_migrations`` *inside the same transaction*, so a
migration can never be half-applied or applied-without-record.

Design plan: documentation/00_design_plan.md §1 / §4.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path


MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

#: Highest schema version this build understands. Bump when a new migration
#: file is added.
CURRENT_SCHEMA_VERSION = 15

_FILENAME_RE = re.compile(r"^(\d+)_([A-Za-z0-9_]+)\.sql$")


class MigrationError(Exception):
    """Raised when migrations cannot be applied safely."""


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------
def _migration_files() -> list[tuple[int, Path]]:
    """Return ``(version, path)`` for every ``NNN_*.sql`` file, in order."""
    found: list[tuple[int, Path]] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = _FILENAME_RE.match(path.name)
        if not match:
            continue  # not a migration file; ignore
        found.append((int(match.group(1)), path))
    versions = [version for version, _ in found]
    if len(set(versions)) != len(versions):
        raise MigrationError("duplicate migration version numbers in migrations/")
    return sorted(found)


def _describe(path: Path) -> str:
    """Human-readable description derived from the file name."""
    return path.stem.split("_", 1)[1].replace("_", " ")


# ---------------------------------------------------------------------------
# version bookkeeping
# ---------------------------------------------------------------------------
def get_schema_version(conn: sqlite3.Connection) -> int:
    """Return ``MAX(version)`` from ``schema_migrations``; 0 if not migrated."""
    row = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if row is None:
        return 0
    version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    return int(version) if version is not None else 0


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------
def migrate(conn: sqlite3.Connection) -> int:
    """Apply every pending migration in filename order.

    Returns the number of migrations applied (0 when already up to date —
    re-running is a safe no-op).

    Raises:
        MigrationError: if the database is newer than this build (fail safe:
            never overwrite data), if a migration file fails, or if the
            connection already has an open transaction.
    """
    if conn.in_transaction:
        raise MigrationError("migrate() requires a connection with no open transaction")

    current = get_schema_version(conn)
    if current > CURRENT_SCHEMA_VERSION:
        raise MigrationError(
            f"incompatible newer database version (found schema version {current}, "
            f"this build supports up to {CURRENT_SCHEMA_VERSION}); refusing to open"
        )

    applied = 0
    for version, path in _migration_files():
        if version <= current:
            continue
        _apply_one(conn, version, path)
        applied += 1
    return applied


def _apply_one(conn: sqlite3.Connection, version: int, path: Path) -> None:
    """Apply a single migration file atomically, recording its version."""
    sql = path.read_text(encoding="utf-8")
    # The version record is part of the same transaction as the DDL, so a
    # migration is never half-applied or applied-without-record. `version` is
    # an int parsed from the file name and the description is single-quote
    # escaped; migration files themselves are static, developer-owned SQL.
    safe_description = _describe(path).replace("'", "''")
    script = (
        "BEGIN IMMEDIATE;\n"
        + sql.rstrip()
        + "\n;\n"
        + "INSERT INTO schema_migrations(version, description) "
        + f"VALUES ({version}, '{safe_description}');\n"
        + "COMMIT;"
    )
    try:
        # executescript() commits any *pending* transaction first, but
        # migrate() guarantees there is none, so the script's own
        # BEGIN IMMEDIATE … COMMIT is the migration's transaction.
        conn.executescript(script)
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        raise MigrationError(f"migration {path.name} failed: {exc}") from exc


# ---------------------------------------------------------------------------
# schema verification
# ---------------------------------------------------------------------------
_MUTABLE_TABLES = (
    "app_settings",
    "business_days",
    "fuel_products",
    "nozzles",
    "nozzle_readings",
)

_AUDITED_TABLES = (
    "app_settings",
    "business_days",
    "business_day_events",
    "fuel_products",
    "nozzles",
    "nozzle_readings",
    "meter_events",
)

EXPECTED_TABLES: tuple[str, ...] = (
    "schema_migrations",
    "app_settings",
    "business_days",
    "business_day_events",
    "fuel_products",
    "nozzles",
    "nozzle_readings",
    "meter_events",
    "audit_log",
    "backup_history",
    # Phase 2 (migration 005)
    "business_day_closings",
    "opening_balances",
    # Phase 3 (migration 007)
    "price_history",
    "tanks",
    "suppliers",
    "fuel_deliveries",
    "inventory_movements",
    "inventory_adjustments",
    "tank_readings",
    # Phase 5 (migration 010)
    "cash_categories",
    "sales_settlements",
    "cash_receipts",
    "cash_payments",
    "expense_categories",
    "expenses",
    "supplier_payments",
    "other_products",
    "other_product_transactions",
    # Phase 5 (migration 012)
    "stock_opening_values",
)

EXPECTED_INDEXES: tuple[str, ...] = (
    "uq_nozzle_readings_closing",
    "idx_business_day_events_business_date",
    "idx_nozzle_readings_nozzle_id",
    "idx_nozzle_readings_business_date",
    "idx_meter_events_nozzle_id",
    "idx_nozzles_fuel_product_id",
    "idx_nozzles_tank_id",
    "idx_business_days_prev_closing_date",
    "idx_audit_log_table_record",
    "idx_audit_log_occurred_at_utc",
    # Phase 2 (migrations 005-006)
    "idx_business_day_closings_business_date",
    "idx_business_day_closings_prev_closing_id",
    "uq_opening_balances_cash",
    "uq_opening_balances_stock",
    "idx_opening_balances_business_date",
    "idx_nozzle_readings_supersedes",
    # Phase 3 (migration 007)
    "idx_price_history_product_effective",
    "idx_fuel_deliveries_business_date",
    "idx_fuel_deliveries_supplier",
    "idx_inventory_movements_business_date",
    "idx_inventory_movements_fuel_product",
    "idx_inventory_movements_tank",
    "idx_inventory_movements_source_uuid",
    "idx_inventory_adjustments_business_date",
    # Phase 5 (migration 010)
    "idx_cash_categories_kind",
    "uq_settlement_ticket_leg",
    "uq_settlement_manager_leg",
    "idx_settlements_date",
    "idx_settlements_batch",
    "uq_cash_receipt_source",
    "idx_cash_receipts_date",
    "idx_cash_receipts_customer",
    "uq_cash_payment_source",
    "idx_cash_payments_date",
    "idx_expenses_date",
    "idx_expenses_category",
    "idx_supplier_payments_supplier",
    "idx_supplier_payments_delivery",
    "idx_supplier_payments_date",
    "uq_other_txn_source",
    "idx_other_txn_product_date",
    # Phase 5 (migration 012)
    "idx_stock_opening_values_product",
)

EXPECTED_VIEWS: tuple[str, ...] = (
    # Phase 2 (migration 006)
    "v_effective_nozzle_readings",
    # Phase 3 (migration 007)
    "v_stock_position",
    "v_physical_stock",
    # Phase 5 (migration 010)
    "v_effective_settlements",
    "v_cashbook",
    "v_supplier_balances",
    "v_other_product_stock",
    "v_daily_cash_position",
)

EXPECTED_TRIGGERS: tuple[str, ...] = tuple(
    # Group A1 — created_at_utc immutability guards
    [f"trg_{t}_bu_guard_created" for t in _MUTABLE_TABLES]
    # Group A2 — updated_at_utc touches
    + [f"trg_{t}_bu_touch_updated" for t in _MUTABLE_TABLES]
    # Group B — audit history (INSERT/UPDATE/DELETE per table)
    + [f"trg_{t}_ai_audit" for t in _AUDITED_TABLES]
    + [f"trg_{t}_au_audit" for t in _AUDITED_TABLES]
    + [f"trg_{t}_bd_audit" for t in _AUDITED_TABLES]
    # Group B — audit_log self-protection (one logical guard, two triggers:
    # SQLite has no multi-event trigger syntax)
    + ["trg_audit_log_bu_no_modify", "trg_audit_log_bd_no_modify"]
    # Group C — posted-ledger immutability
    + [
        "trg_nozzle_readings_bu_no_update",
        "trg_nozzle_readings_bd_no_delete",
        "trg_business_day_events_bu_no_update",
        "trg_business_day_events_bd_no_delete",
        "trg_business_days_bd_no_delete",
    ]
    # Group D — closed business day
    + ["trg_nozzle_readings_bi_closed_day"]
    # Group H — one open day + status flow
    + [
        "trg_business_days_bi_one_open",
        "trg_business_days_bu_one_open",
        "trg_business_days_bu_status_flow",
    ]
    # Group E2 — reading validation
    + ["trg_nozzle_readings_bi_validate"]
    # Phase 2 (migration 005): closings ledger + opening balances —
    # A1 guards, B audit, C immutability, D1 closed-day
    + [
        "trg_business_day_closings_bu_guard_created",
        "trg_business_day_closings_ai_audit",
        "trg_business_day_closings_bu_no_update",
        "trg_business_day_closings_bd_no_delete",
        "trg_business_day_closings_bi_closed_day",
        "trg_opening_balances_bu_guard_created",
        "trg_opening_balances_ai_audit",
        "trg_opening_balances_bu_no_update",
        "trg_opening_balances_bd_no_delete",
        "trg_opening_balances_bi_closed_day",
    ]
    # Phase 2 (migration 006): non-operating day guard
    + ["trg_nozzle_readings_bi_non_operating"]
    # Phase 3 (migration 007): prices, tanks, suppliers, deliveries,
    # inventory movements/adjustments, tank readings —
    # A1 guards, A2 touches (mutable tables only), B audit, C immutability,
    # D1 closed-day, F2 no-negative-stock, I amount constraints
    + [
        # Group A1 — created_at_utc immutability guards
        "trg_tanks_bu_guard_created",
        "trg_suppliers_bu_guard_created",
        "trg_tank_readings_bu_guard_created",
        "trg_price_history_bu_guard_created",
        "trg_fuel_deliveries_bu_guard_created",
        "trg_inventory_adjustments_bu_guard_created",
        "trg_inventory_movements_bu_guard_created",
        # Group A2 — updated_at_utc touches (mutable tables only)
        "trg_tanks_bu_touch_updated",
        "trg_suppliers_bu_touch_updated",
        "trg_tank_readings_bu_touch_updated",
        # Group B — audit history (INSERT/UPDATE/DELETE per table)
        "trg_tanks_ai_audit",
        "trg_suppliers_ai_audit",
        "trg_tank_readings_ai_audit",
        "trg_price_history_ai_audit",
        "trg_fuel_deliveries_ai_audit",
        "trg_inventory_adjustments_ai_audit",
        "trg_inventory_movements_ai_audit",
        "trg_tanks_au_audit",
        "trg_suppliers_au_audit",
        "trg_tank_readings_au_audit",
        "trg_price_history_au_audit",
        "trg_fuel_deliveries_au_audit",
        "trg_inventory_adjustments_au_audit",
        "trg_inventory_movements_au_audit",
        "trg_tanks_bd_audit",
        "trg_suppliers_bd_audit",
        "trg_tank_readings_bd_audit",
        "trg_price_history_bd_audit",
        "trg_fuel_deliveries_bd_audit",
        "trg_inventory_adjustments_bd_audit",
        "trg_inventory_movements_bd_audit",
        # Group C — posted-ledger immutability
        "trg_price_history_bu_no_update",
        "trg_price_history_bd_no_delete",
        "trg_fuel_deliveries_bu_no_update",
        "trg_fuel_deliveries_bd_no_delete",
        "trg_inventory_adjustments_bu_no_update",
        "trg_inventory_adjustments_bd_no_delete",
        "trg_inventory_movements_bu_no_update",
        "trg_inventory_movements_bd_no_delete",
        # Group D — closed business day
        "trg_price_history_bi_closed_day",
        "trg_fuel_deliveries_bi_closed_day",
        "trg_inventory_adjustments_bi_closed_day",
        "trg_inventory_movements_bi_closed_day",
        "trg_tank_readings_bi_closed_day",
        # Group F2 — sale must not drive tracked stock negative
        "trg_inventory_movements_bi_no_negative",
        # Group I — delivery amount constraints
        "trg_fuel_deliveries_bi_total_check",
        "trg_fuel_deliveries_bi_paid_cap",
        # Phase 5 (migration 010): cash book, settlements, expenses,
        # supplier payments, other materials —
        # A1 guards, A2 touches (mutable master tables only), B audit,
        # C immutability (with reversal whitelists), D1 closed-day,
        # I supplier-payment allocation guard + reversal mirror checks
        "trg_cash_categories_bu_guard_created",
        "trg_sales_settlements_bu_guard_created",
        "trg_cash_receipts_bu_guard_created",
        "trg_cash_payments_bu_guard_created",
        "trg_expense_categories_bu_guard_created",
        "trg_expenses_bu_guard_created",
        "trg_supplier_payments_bu_guard_created",
        "trg_other_products_bu_guard_created",
        "trg_other_product_transactions_bu_guard_created",
        "trg_cash_categories_bu_touch_updated",
        "trg_expense_categories_bu_touch_updated",
        "trg_other_products_bu_touch_updated",
        "trg_cash_categories_ai_audit",
        "trg_cash_categories_au_audit",
        "trg_cash_categories_bd_audit",
        "trg_sales_settlements_ai_audit",
        "trg_sales_settlements_au_audit",
        "trg_sales_settlements_bd_audit",
        "trg_cash_receipts_ai_audit",
        "trg_cash_receipts_au_audit",
        "trg_cash_receipts_bd_audit",
        "trg_cash_payments_ai_audit",
        "trg_cash_payments_au_audit",
        "trg_cash_payments_bd_audit",
        "trg_expense_categories_ai_audit",
        "trg_expense_categories_au_audit",
        "trg_expense_categories_bd_audit",
        "trg_expenses_ai_audit",
        "trg_expenses_au_audit",
        "trg_expenses_bd_audit",
        "trg_supplier_payments_ai_audit",
        "trg_supplier_payments_au_audit",
        "trg_supplier_payments_bd_audit",
        "trg_other_products_ai_audit",
        "trg_other_products_au_audit",
        "trg_other_products_bd_audit",
        "trg_other_product_transactions_ai_audit",
        "trg_other_product_transactions_au_audit",
        "trg_other_product_transactions_bd_audit",
        "trg_sales_settlements_bu_no_update",
        "trg_sales_settlements_bd_no_delete",
        "trg_cash_receipts_bu_no_update",
        "trg_cash_receipts_bd_no_delete",
        "trg_cash_payments_bu_no_update",
        "trg_cash_payments_bd_no_delete",
        "trg_expenses_bu_no_update",
        "trg_expenses_bd_no_delete",
        "trg_supplier_payments_bu_no_update",
        "trg_supplier_payments_bd_no_delete",
        "trg_other_product_transactions_bu_no_update",
        "trg_other_product_transactions_bd_no_delete",
        "trg_sales_settlements_bi_closed_day",
        "trg_cash_receipts_bi_closed_day",
        "trg_cash_payments_bi_closed_day",
        "trg_expenses_bi_closed_day",
        "trg_supplier_payments_bi_closed_day",
        "trg_other_product_transactions_bi_closed_day",
        "trg_supplier_payments_bi_check",
        "trg_supplier_payments_bi_reversal_check",
        "trg_cash_receipts_bi_reversal_check",
        "trg_cash_payments_bi_reversal_check",
        "trg_expenses_bi_reversal_check",
        # Phase 5 (migration 012): stock opening valuations —
        # A1 guard, B audit, C immutability, D1 closed-day
        "trg_stock_opening_values_bu_guard_created",
        "trg_stock_opening_values_ai_audit",
        "trg_stock_opening_values_bd_audit",
        "trg_stock_opening_values_bu_no_update",
        "trg_stock_opening_values_bd_no_delete",
        "trg_stock_opening_values_bi_closed_day",
        # Phase 7 (migration 013): gap-closing triggers —
        # backup_history: A1 guard, A2 touch, B audit, C2 no-delete
        #   (no bu_no_update by design: Phase 8's backup pipeline performs
        #   audited status UPDATEs on the single backup row);
        # meter_events: A1 guard, A2 touch (its ai/au/bd audit triggers
        #   already existed from 003; the au trigger was recreated in 013
        #   with the business-column WHEN guard);
        # schema_migrations: tamper guards (UPDATE/DELETE abort, INSERT
        #   stays allowed for the migrator).
        "trg_backup_history_bu_guard_created",
        "trg_backup_history_bu_touch_updated",
        "trg_backup_history_ai_audit",
        "trg_backup_history_au_audit",
        "trg_backup_history_bd_audit",
        "trg_backup_history_bd_no_delete",
        "trg_meter_events_bu_guard_created",
        "trg_meter_events_bu_touch_updated",
        "trg_schema_migrations_bu_no_modify",
        "trg_schema_migrations_bd_no_modify",
        # Phase 7 (migration 014): D2/D3 closed-day guards for
        # tank_readings (the D1 INSERT guard already existed; UPDATE/DELETE
        # on closed days were the gap found by attack testing).
        "trg_tank_readings_bu_closed_day",
        "trg_tank_readings_bd_closed_day",
    ]
)


def verify_schema(conn: sqlite3.Connection) -> list[str]:
    """Return names of required schema objects missing from the database.

    An empty list means the schema matches ``CURRENT_SCHEMA_VERSION``.
    Entries look like ``"table:app_settings"``, ``"index:..."``,
    ``"trigger:..."``.
    """
    missing: list[str] = []

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    for name in EXPECTED_TABLES:
        if name not in tables:
            missing.append(f"table:{name}")

    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    for name in EXPECTED_INDEXES:
        if name not in indexes:
            missing.append(f"index:{name}")

    triggers = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
    }
    for name in EXPECTED_TRIGGERS:
        if name not in triggers:
            missing.append(f"trigger:{name}")

    views = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'view'"
        ).fetchall()
    }
    for name in EXPECTED_VIEWS:
        if name not in views:
            missing.append(f"view:{name}")

    return missing
