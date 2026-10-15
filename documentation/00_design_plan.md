# Design Plan — Offline Petrol Pump Management System
**Version:** 1.0 (Phase 1 contract) · **Date:** 2026-10-15 · **Status:** Approved by product owner (user) — build proceeds per this document.

This document is the **contract for all later phases**. It answers master-prompt §30 items 1–10 and records every unresolved accounting assumption with the safe default chosen. Implementation must not contradict it; changes require an explicit amendment entry at the bottom.

## 0. Decisions already locked

| Area | Decision |
|---|---|
| Language / UI | Python 3.12 + PySide6 (Qt for Python), desktop only |
| Database | SQLite, accessed via Python `sqlite3`, repository + service layers |
| Migrations | Numbered, versioned SQL migration scripts, applied at startup |
| Money math | Python `Decimal`, round-half-up, integer paisa storage |
| Quantity math | Integer milliliters |
| Unit prices | Scaled-integer (0.0001 PKR resolution) |
| Reports | ReportLab (PDF), openpyxl (XLSX), CSV |
| Tests | pytest, temporary databases only |
| Packaging | PyInstaller + Inno Setup |
| Backup | SQLite online backup API → validated snapshot → local + Google Drive for desktop folder |
| Locales | English + Urdu (RTL), persistent setting, independent of stored data |
| Live DB location | `%LOCALAPPDATA%\PetrolPumpManager\data\petrol_pump.sqlite3` (Windows); XDG `~/.local/share/petrol-pump-manager/` on Linux (dev/test) — never inside a cloud-synced folder, never in `Program Files` |
| Timezone | Business logic in `Asia/Karachi`; machine timestamps stored UTC |

**Guiding principle:** the database enforces invariants (it must reject bad writes even from raw SQL); tested Python services perform the business calculations inside atomic transactions. Triggers are guards, not the accounting engine.

---

## 1. Proposed architecture

```
┌─────────────────────────────────────────────────────────────┐
│ UI layer (PySide6) — app/ui/                                │
│  Forms, tables, dashboards, print previews.                 │
│  RULE: no authoritative calculations here. Calls services.   │
├─────────────────────────────────────────────────────────────┤
│ Service layer — app/services/                               │
│  business_day_service · nozzle_service · sales_service ·     │
│  inventory_service · customer_credit_service ·               │
│  payment_service · cashbook_service · accounting_service ·   │
│  reporting_service · backup_service · audit_service          │
│  RULE: validates input, runs Decimal math, owns the         │
│  transaction boundary (BEGIN … COMMIT / ROLLBACK).           │
├─────────────────────────────────────────────────────────────┤
│ Repository layer — app/repositories/                        │
│  Thin, parameterized SQL per aggregate. No business logic.   │
│  Never handed a live connection by UI code.                  │
├─────────────────────────────────────────────────────────────┤
│ Database — app/database/                                    │
│  connection.py (pragma-verified connections)                │
│  migrations/NNN_*.sql (only way schema changes)             │
│  views/*.sql · integrity_checks.py                          │
│  RULE: constraints + triggers reject invalid writes from    │
│  ANY connection, including direct SQL.                      │
└─────────────────────────────────────────────────────────────┘
```

### Module responsibilities

- `app/main.py` — entry point; parses args, starts Qt app, hands off to `bootstrap`.
- `app/bootstrap.py` — resolves data directories, runs migrations, integrity checks, clock-skew warning, creates `MainWindow`.
- `app/config.py` — paths, app constants, settings keys. No business data.
- `app/ui/` — one package per sidebar section (dashboard, daily_register, nozzles, inventory, customers, payments, suppliers, expenses, reports, settings) + `dialogs/`. Each view is a passive form: reads via services, writes via services, shows service-raised `ValidationError`s.
- `app/services/*` — one service per domain concept (see diagram). Each public mutating method: validate → compute (Decimal) → repository writes inside one explicit transaction → return result DTO. Raises `BusinessRuleError` / `ValidationError`; never shows a success message before commit.
- `app/repositories/*` — CRUD + queries with `?` placeholders only. Returns rows/dataclasses.
- `app/database/connection.py` — single place that opens SQLite connections; sets and **verifies** pragmas; WAL; clean shutdown.
- `app/database/migrations/` — `001_…sql`, `002_…sql`, … plus a `schema_migrations` ledger. Startup applies missing migrations in order, inside transactions where supported, then verifies mandatory objects exist.
- `app/database/views/` — reporting views (daily register, customer ledger, stock) kept as versioned SQL.
- `app/database/integrity_checks.py` — `integrity_check`, `foreign_key_check`, object-existence check, schema-version compatibility check.
- `app/models/` — dataclasses/DTOs (BusinessDay, NozzleReading, CreditTicket, …). No SQL.
- `app/calculations/` — pure money/quantity functions (paisa↔Decimal, ml↔liters, price scaling, round-half-up, amount formulas). 100% unit-testable, zero DB imports.
- `app/localization/` — `en.json`, `ur.json`; a `tr(key)` helper; RTL layout switch. Language never alters stored values.
- `app/resources/` — icons, Urdu-capable fonts (e.g. Noto Nastaliq Urdu / Noto Naskh Arabic), report templates.
- `app/tests/` — pytest suite; fixtures create **temporary** databases; never touch the live DB.
- `installer/` — PyInstaller spec + Inno Setup script.
- `documentation/` — this file, business-rules doc, user guides, test report.

### Key architectural rules (non-negotiable)

1. UI never imports `sqlite3` and never receives a raw connection.
2. Every financial write path is service → repository → one explicit transaction.
3. Critical rules live in **both** service validation and DB triggers/constraints.
4. Schema changes only via numbered migrations; startup fails safe (clear error, no data loss) on incompatible versions.
5. `Decimal` for all money math; integers (paisa / milliliters / scaled price) at rest.

## 2. Database entity-relationship design

Notation: `*` PK · `→` FK · `U` unique · `CK` check · `NN` not null.

```mermaid
erDiagram
    schema_migrations ||--o{ app_settings : "versioned by"
    schema_migrations {
        INTEGER version PK
        TEXT applied_at_utc
        TEXT description
    }
    app_settings {
        TEXT key PK
        TEXT value NN
        TEXT updated_at_utc
    }
    business_days ||--o{ business_day_events : "has"
    business_days ||--o{ nozzle_readings : "contains"
    business_days ||--o{ credit_sales : "contains"
    business_days ||--o{ customer_payments : "contains"
    business_days ||--o{ cash_receipts : "contains"
    business_days ||--o{ cash_payments : "contains"
    business_days ||--o{ fuel_deliveries : "contains"
    business_days ||--o{ inventory_movements : "contains"
    business_days ||--o{ expenses : "contains"
    business_days {
        TEXT business_date PK "YYYY-MM-DD"
        TEXT status NN "CK: open|closed|reopened|non_operating"
        INTEGER opening_cash_paisa NN
        TEXT prev_closing_date "→ business_days, nullable"
        TEXT opened_at_utc
        TEXT closed_at_utc
        INTEGER closing_revision NN "default 0"
        TEXT created_at_utc
        TEXT updated_at_utc
    }
    business_day_events {
        INTEGER id PK
        TEXT business_date NN "→ business_days"
        TEXT event_type NN "open|close|reopen|adjust_note"
        TEXT occurred_at_utc NN
        TEXT actor NN
        TEXT reason "required for reopen"
        TEXT payload_json
    }
    fuel_products ||--o{ nozzles : "dispensed by"
    fuel_products ||--o{ price_history : "priced by"
    fuel_products ||--o{ inventory_movements : "moves"
    fuel_products {
        INTEGER id PK
        TEXT code U NN "e.g. PETROL, DIESEL"
        TEXT name_en NN
        TEXT name_ur
        INTEGER is_active NN
    }
    tanks ||--o{ inventory_movements : "stored in"
    tanks {
        INTEGER id PK
        INTEGER fuel_product_id NN "→ fuel_products"
        TEXT code U NN
        INTEGER capacity_ml
        INTEGER is_active NN
    }
    nozzles ||--o{ nozzle_readings : "measured by"
    nozzles {
        INTEGER id PK
        TEXT code U NN "e.g. 1 PG+"
        INTEGER fuel_product_id NN "→ fuel_products"
        INTEGER tank_id "→ tanks, nullable"
        TEXT label_en
        TEXT label_ur
        INTEGER is_active NN
        INTEGER sort_order NN
    }
    price_history {
        INTEGER id PK
        INTEGER fuel_product_id NN "→ fuel_products"
        TEXT effective_from_utc NN
        INTEGER unit_price_10k NN "0.0001 PKR per litre"
        TEXT business_date NN
        TEXT created_by NN
        U "fuel_product_id + effective_from_utc"
    }
    nozzle_readings ||--o{ meter_events : "explained by"
    nozzle_readings {
        INTEGER id PK
        INTEGER nozzle_id NN "→ nozzles"
        TEXT business_date NN "→ business_days"
        TEXT reading_kind NN "CK: closing|segment|opening_snapshot"
        INTEGER reading_ml NN "CK >= 0"
        TEXT occurred_at_utc NN
        TEXT created_by NN
        U "partial: nozzle_id + business_date WHERE reading_kind='closing'"
    }
    meter_events {
        INTEGER id PK
        INTEGER nozzle_id NN "→ nozzles"
        TEXT event_type NN "reset|rollover|replacement|correction"
        INTEGER reading_before_ml
        INTEGER reading_after_ml
        TEXT occurred_at_utc NN
        TEXT reason NN
        TEXT authorized_by NN
    }
    suppliers ||--o{ fuel_deliveries : "delivers"
    suppliers {
        INTEGER id PK
        TEXT name NN
        TEXT phone
        TEXT address
        INTEGER is_active NN
    }
    fuel_deliveries {
        INTEGER id PK
        TEXT uuid U NN
        INTEGER supplier_id NN "→ suppliers"
        INTEGER fuel_product_id NN "→ fuel_products"
        INTEGER tank_id "→ tanks"
        TEXT business_date NN "→ business_days"
        INTEGER quantity_ml NN "CK > 0"
        INTEGER unit_cost_10k NN
        INTEGER total_cost_paisa NN
        INTEGER other_cost_paisa
        TEXT supplier_invoice_no
        INTEGER amount_paid_paisa
        TEXT payment_status NN "unpaid|partial|paid"
        TEXT occurred_at_utc NN
        U "supplier_id + supplier_invoice_no (when not null)"
    }
    supplier_payments {
        INTEGER id PK
        TEXT uuid U NN
        INTEGER supplier_id NN "→ suppliers"
        INTEGER delivery_id "→ fuel_deliveries"
        TEXT business_date NN
        INTEGER amount_paisa NN "CK > 0"
        TEXT method NN
        TEXT occurred_at_utc NN
    }
    inventory_movements {
        INTEGER id PK
        TEXT uuid U NN
        TEXT business_date NN "→ business_days"
        INTEGER fuel_product_id NN "→ fuel_products"
        INTEGER tank_id "→ tanks"
        TEXT movement_type NN "delivery|sale|adjustment|opening"
        INTEGER quantity_ml NN "signed; CK: rule per type"
        TEXT source_table NN
        INTEGER source_id NN
        TEXT source_uuid U NN "prevents double posting"
        TEXT occurred_at_utc NN
        TEXT reason
    }
    customers ||--o{ credit_sales : "buys"
    customers {
        INTEGER id PK
        TEXT customer_code U NN
        TEXT name NN
        TEXT business_name
        TEXT phone
        TEXT address
        INTEGER opening_receivable_paisa NN
        INTEGER credit_limit_paisa "nullable"
        TEXT status NN "active|inactive"
        TEXT notes
    }
    credit_sales {
        INTEGER id PK
        TEXT uuid U NN
        INTEGER customer_id NN "→ customers"
        TEXT business_date NN "→ business_days"
        INTEGER fuel_product_id NN "→ fuel_products"
        INTEGER nozzle_id "→ nozzles"
        INTEGER quantity_ml NN "CK > 0"
        INTEGER unit_price_10k NN
        INTEGER gross_amount_paisa NN
        INTEGER paid_now_paisa NN
        TEXT receipt_no U
        TEXT status NN "posted|reversed"
        TEXT occurred_at_utc NN
        TEXT notes
    }
    customer_payments {
        INTEGER id PK
        TEXT uuid U NN
        INTEGER customer_id NN "→ customers"
        TEXT business_date NN
        INTEGER amount_paisa NN "CK > 0"
        TEXT method NN "cash|bank|other"
        TEXT receipt_no U
        TEXT status NN "posted|reversed"
        TEXT occurred_at_utc NN
    }
    payment_allocations {
        INTEGER id PK
        INTEGER payment_id NN "→ customer_payments"
        INTEGER credit_sale_id NN "→ credit_sales"
        INTEGER amount_paisa NN "CK > 0"
        U "payment_id + credit_sale_id"
    }
    sales_settlements {
        INTEGER id PK
        TEXT business_date NN "→ business_days"
        INTEGER fuel_product_id NN "→ fuel_products"
        TEXT category NN "cash|bank|receivable|paid_now_on_ticket|other"
        INTEGER amount_paisa NN "CK >= 0"
        TEXT source_table
        INTEGER source_id
        TEXT notes
    }
    cash_receipts {
        INTEGER id PK
        TEXT uuid U NN
        TEXT business_date NN "→ business_days"
        TEXT category NN
        INTEGER amount_paisa NN "CK > 0"
        TEXT source_table
        INTEGER source_id
        INTEGER customer_id "→ customers"
        TEXT method NN "cash only here"
        TEXT remarks
        TEXT occurred_at_utc NN
    }
    cash_payments {
        INTEGER id PK
        TEXT uuid U NN
        TEXT business_date NN "→ business_days"
        TEXT category NN
        INTEGER amount_paisa NN "CK > 0"
        TEXT payee
        TEXT source_table
        INTEGER source_id
        TEXT remarks
        TEXT occurred_at_utc NN
    }
    expense_categories ||--o{ expenses : "classifies"
    expense_categories {
        INTEGER id PK
        TEXT name_en U NN
        TEXT name_ur
        INTEGER is_active NN
    }
    expenses {
        INTEGER id PK
        TEXT uuid U NN
        TEXT business_date NN "→ business_days"
        INTEGER category_id NN "→ expense_categories"
        INTEGER amount_paisa NN "CK > 0"
        TEXT method NN
        TEXT payee
        TEXT reference
        TEXT occurred_at_utc NN
    }
    other_products ||--o{ other_product_transactions : "traded as"
    other_products {
        INTEGER id PK
        TEXT name NN
        TEXT unit NN
        INTEGER is_active NN
    }
    other_product_transactions {
        INTEGER id PK
        TEXT uuid U NN
        TEXT business_date NN
        INTEGER product_id NN "→ other_products"
        TEXT txn_type NN "purchase|sale|adjustment"
        INTEGER quantity
        INTEGER unit_price_paisa
        INTEGER total_paisa NN
    }
    audit_log {
        INTEGER id PK
        TEXT occurred_at_utc NN
        TEXT table_name NN
        INTEGER record_id NN
        TEXT action NN "INSERT|UPDATE|DELETE"
        TEXT old_values_json
        TEXT new_values_json
        TEXT business_date
        TEXT actor
        TEXT reason
    }
    backup_history {
        INTEGER id PK
        TEXT requested_at_utc NN
        TEXT filename NN
        TEXT destination NN "local|gdrive"
        TEXT status NN
        INTEGER size_bytes
        TEXT sha256
        TEXT verified_at_utc
        TEXT error
    }
```

Views (reporting, read-only): `v_daily_register`, `v_customer_ledger`, `v_stock_position`, `v_nozzle_sales`, `v_cashbook`, `v_outstanding_receivables`, `v_supplier_balances`. Views live in `app/database/views/` and are installed by migrations; they never bypass the ledger tables.

---

## 3. Initial SQL schema

Conventions for every table: `TEXT` timestamps as defined in §5 (never `CURRENT_TIMESTAMP` literal mixing); money columns named `*_paisa INTEGER NOT NULL` with `CHECK(*_paisa >= 0)` unless a documented signed type; quantity columns `*_ml INTEGER NOT NULL`; prices `*_10k INTEGER NOT NULL` (0.0001 PKR resolution); `created_by TEXT NOT NULL DEFAULT 'system'`; `created_at_utc` default `strftime('%Y-%m-%dT%H:%M:%fZ','now')`.

**Migration 001 — core ledger tables (Phase 1 installs the rows marked ★; the rest arrive in their phase migrations but are specified here so later work cannot drift):**

★ `schema_migrations(version INTEGER PRIMARY KEY, applied_at_utc TEXT NOT NULL DEFAULT …, description TEXT)`
★ `app_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at_utc TEXT …)` — keys: `language`, `station_name`, `operator_name`, `backup.local_dir`, `backup.gdrive_dir`, `backup.retention_days`, `allocation_order` (`fifo`), `price_snapshot_policy`.
★ `business_days(business_date TEXT PRIMARY KEY CHECK(business_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'), status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed','reopened','non_operating')), opening_cash_paisa INTEGER NOT NULL DEFAULT 0 CHECK(opening_cash_paisa>=0), prev_closing_date TEXT REFERENCES business_days(business_date), opened_at_utc TEXT, closed_at_utc TEXT, closing_revision INTEGER NOT NULL DEFAULT 0, physical_cash_paisa INTEGER, created_at_utc …, updated_at_utc …)`
★ `business_day_events(id INTEGER PRIMARY KEY, business_date TEXT NOT NULL REFERENCES business_days(business_date), event_type TEXT NOT NULL CHECK(event_type IN ('open','close','reopen','note')), occurred_at_utc TEXT NOT NULL DEFAULT …, actor TEXT NOT NULL DEFAULT 'system', reason TEXT, payload_json TEXT)` + index on `(business_date)`.
★ `fuel_products(id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name_en TEXT NOT NULL, name_ur TEXT, is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)))`
★ `nozzles(id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, fuel_product_id INTEGER NOT NULL REFERENCES fuel_products(id), tank_id INTEGER REFERENCES tanks(id), label_en TEXT, label_ur TEXT, is_active INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0)`
★ `nozzle_readings(id INTEGER PRIMARY KEY, nozzle_id INTEGER NOT NULL REFERENCES nozzles(id), business_date TEXT NOT NULL REFERENCES business_days(business_date), reading_kind TEXT NOT NULL DEFAULT 'closing' CHECK(reading_kind IN ('closing','segment','opening_snapshot')), reading_ml INTEGER NOT NULL CHECK(reading_ml >= 0), occurred_at_utc TEXT NOT NULL DEFAULT …, created_by TEXT NOT NULL DEFAULT 'system', created_at_utc …, updated_at_utc …)` + **partial unique index** `UNIQUE(nozzle_id, business_date) WHERE reading_kind='closing'`.
★ `meter_events(id INTEGER PRIMARY KEY, nozzle_id INTEGER NOT NULL REFERENCES nozzles(id), event_type TEXT NOT NULL CHECK(event_type IN ('reset','rollover','replacement','correction')), reading_before_ml INTEGER, reading_after_ml INTEGER, occurred_at_utc TEXT NOT NULL DEFAULT …, reason TEXT NOT NULL, authorized_by TEXT NOT NULL DEFAULT 'system')`
★ `audit_log(id INTEGER PRIMARY KEY, occurred_at_utc TEXT NOT NULL DEFAULT …, table_name TEXT NOT NULL, record_id INTEGER NOT NULL, action TEXT NOT NULL CHECK(action IN ('INSERT','UPDATE','DELETE')), old_values_json TEXT, new_values_json TEXT, business_date TEXT, actor TEXT NOT NULL DEFAULT 'system', reason TEXT)` + indexes on `(table_name, record_id)` and `(occurred_at_utc)`.
★ `backup_history(id INTEGER PRIMARY KEY, requested_at_utc TEXT NOT NULL DEFAULT …, filename TEXT NOT NULL, destination TEXT NOT NULL CHECK(destination IN ('local','gdrive')), status TEXT NOT NULL CHECK(status IN ('requested','snapshot_created','validated','copied_to_drive','sync_confirmed','cloud_unavailable','failed')), size_bytes INTEGER, sha256 TEXT, verified_at_utc TEXT, error TEXT)`.

**Later-phase tables (specified now, migrated later):** `tanks`, `price_history` (Phase 3); `fuel_deliveries`, `suppliers`, `supplier_payments`, `inventory_movements` (Phase 3); `customers`, `credit_sales`, `customer_payments`, `payment_allocations`, `sales_settlements` (Phase 4); `cash_receipts`, `cash_payments`, `expenses`, `expense_categories` (Phase 5); `other_products`, `other_product_transactions` (Phase 5/6). Column lists are exactly those in the ERD above.

**Phase 1 seed data:** `fuel_products` gets two rows (`PETROL`, `DIESEL`) and `nozzles` gets the four configurable example nozzles from the register (`1 PG+`, `2 PG+` → petrol; `3 HSD`, `4 HSD` → diesel). These are *examples the manager may rename/deactivate* — they are seed configuration, not hard-coded business logic: no code may assume exactly four nozzles or these names.

**Indexes:** FK columns, `(business_date)` on every business table, `(customer_id, business_date)` on credit tables, `(supplier_id)` on delivery/payment tables, `(occurred_at_utc)` for time-range reports.

## 4. Mandatory SQLite triggers (groups A–I)

Every trigger below is installed by a numbered migration and therefore exists in the shipped database. Trigger names follow `trg_<table>_<timing>_<action>[_<purpose>]`. All timestamps inside triggers use `strftime('%Y-%m-%dT%H:%M:%fZ','now')`. `RAISE(ABORT, 'message')` is used for rejections so the enclosing transaction rolls back. Audit triggers use `json_object(...)` (SQLite JSON1 is available in the Python 3.12-bundled SQLite).

### Group A — automatic timestamps
Applies to every mutable business/master table (`app_settings`, `business_days`, `fuel_products`, `nozzles`, `nozzle_readings`, and all later-phase tables with `updated_at_utc`).
- **A1 `…_bu_guard_created` (BEFORE UPDATE):** `WHEN NEW.created_at_utc IS NOT OLD.created_at_utc` → `RAISE(ABORT, 'created_at_utc is immutable')`. `IS NOT` is used so NULL handling is exact.
- **A2 `…_bu_touch_updated` (BEFORE UPDATE):** `WHEN OLD.* IS NOT NEW.*` (row actually changed) → `SET NEW.updated_at_utc = strftime(…)`. Implemented as `WHEN (OLD.col1 IS NOT NEW.col1 OR …)` over the business columns so a no-op UPDATE does not bump the timestamp or recurse.
- **A3 insert defaults:** `created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))` on every table; `updated_at_utc` default same expression, maintained by A2.
- Tests: direct-SQL UPDATE of `created_at_utc` must abort; UPDATE of a business column must change `updated_at_utc` and keep `created_at_utc`; no-op UPDATE must not loop.

### Group B — audit history
One trigger set per audited table: `trg_<table>_ai_audit` (AFTER INSERT), `trg_<table>_au_audit` (AFTER UPDATE), `trg_<table>_bd_audit` (BEFORE DELETE). Each inserts into `audit_log(table_name, record_id, action, old_values_json, new_values_json, business_date, actor, reason)` where:
- `occurred_at_utc` = trigger-time UTC; `record_id` = `NEW.id`/`OLD.id`;
- snapshots = `json_object('col', NEW.col, …)` of the table's business columns (old on UPDATE/DELETE, new on INSERT/UPDATE);
- `business_date` = `NEW.business_date`/`OLD.business_date` when the table has one;
- `actor` = `NEW.created_by`/`OLD.created_by` where available else `'system'`.
- **Recursion guard:** `audit_log` has NO audit triggers; audit triggers are AFTER/BEFORE triggers that only INSERT into `audit_log`, never UPDATE the source row.
- The application exposes no UPDATE/DELETE path for `audit_log`; additionally `trg_audit_log_no_modify` (BEFORE UPDATE/DELETE on `audit_log`) aborts unconditionally.

### Group C — posted-ledger immutability
For every table whose rows become financial facts once posted (`nozzle_readings`, `fuel_deliveries`, `inventory_movements`, `credit_sales`, `customer_payments`, `payment_allocations`, `cash_receipts`, `cash_payments`, `supplier_payments`, `sales_settlements`, `expenses`, `business_day_events`):
- **C1 `…_bu_no_update` (BEFORE UPDATE):** abort unless the change is an explicitly whitelisted status transition (e.g. `posted → reversed` performed only by the reversal service, which additionally inserts the linked reversal row). For tables with no draft state (most), **all** UPDATEs abort: corrections happen via reversal/adjustment rows linked by `reverses_uuid`/`source_uuid`.
- **C2 `…_bd_no_delete` (BEFORE DELETE):** abort unconditionally.
- Draft lifecycle (only where the spec needs editing before posting, e.g. day-close drafts): rows carry `status='draft'`; C1 permits edits while `OLD.status='draft'`; the post operation flips to `posted` inside the same transaction that creates dependent rows. Once `posted`, C1/C2 apply.

### Group D — closed business day
For each business-date-scoped table (`nozzle_readings`, `fuel_deliveries`, `inventory_movements`, `credit_sales`, `customer_payments`, `payment_allocations`, `cash_receipts`, `cash_payments`, `sales_settlements`, `expenses`, `supplier_payments`, `other_product_transactions`):
- **D1 `…_bi_closed_day` (BEFORE INSERT):** `WHEN (SELECT status FROM business_days WHERE business_date = NEW.business_date) IN ('closed')` → abort with `'business day is closed; reopen it first'`. Status `'reopened'` (transient during correction) permits writes; `'non_operating'` permits only note-type rows.
- **D2/D3 `…_bu_closed_day`, `…_bd_closed_day`:** same guard on `OLD.business_date` for UPDATE/DELETE (UPDATEs are already blocked by C1 for posted rows; D2 covers draft rows and master data).
- **Reopening workflow (service-level, audited):** manager enters reason → service inserts `business_day_events(event_type='reopen', reason=…)` → sets `business_days.status='reopened'` → corrections are made as new audited rows → manager closes again → new `business_days.closing_revision = old + 1`, `closed_at_utc` updated, `event_type='close'` row. Old closing revisions are never overwritten; reports print the revision number.

### Group E — nozzle readings
- **E1 uniqueness:** partial unique index `uq_reading_closing (nozzle_id, business_date) WHERE reading_kind='closing'` (duplicate closing reading → constraint violation).
- **E2 `trg_nozzle_readings_bi_validate` (BEFORE INSERT):** abort when: nozzle missing/inactive; business date missing/closed (delegates to D1); `reading_ml < 0`; `reading_ml < previous reading` **and** no authorizing `meter_events` row exists for that nozzle covering the interval (reset/rollover/replacement recorded by manager). A reset is never interpreted as a negative sale: quantity for the interval is computed by the service as `(reading_after_rollover) + (meter_max − reading_before)` only when a rollover event exists, else the insert is rejected.
- **E3 corrections:** no silent delete/reinsert; corrections are new `meter_events(event_type='correction')` + a replacement reading row linked to the original, both audited.

### Group F — inventory movement integrity
- **F1 `…_bi_source_unique`:** `source_uuid` UNIQUE — a delivery/sale/adjustment can post its movement exactly once; re-running a posting service is a no-op-or-error, never a double deduction.
- **F2 `…_bi_no_negative`:** before inserting a `sale` movement, check the product's current computed balance (sum of movements) minus the new quantity stays ≥ 0; else abort. (Tracked-balance check; default policy is *block*, see §11 assumption 3.)
- **F3:** `movement_type='sale'` requires `source_table='nozzle_reading_intervals'` semantics — i.e. sales movements originate only from validated reading intervals, never from typed-in totals; physical counts enter only via `adjustment` with reason.

### Group G — customer payment allocation
- **G1 `trg_payment_allocations_bi_check` (BEFORE INSERT):** abort if `NEW.amount_paisa <= 0`; abort if the referenced payment or credit sale does not exist or has `status='reversed'`/`'cancelled'`; abort if `SUM(amount_paisa)` over existing allocations for this payment plus `NEW.amount_paisa` exceeds the payment's `amount_paisa` (aggregate check in the trigger via subquery).
- **G2:** BEFORE UPDATE/DELETE on `payment_allocations` aborts (C-group immutability); allocation changes go through the audited re-allocation workflow (reverse allocation row + new allocation row).

### Group H — business dates and uniqueness
- **H1:** `business_days.business_date` PRIMARY KEY (unique by construction); `CHECK` on format.
- **H2 `trg_business_days_bi_one_open`:** abort INSERT/UPDATE that would leave more than one row with `status IN ('open','reopened')`.
- **H3:** closing uniqueness — `UNIQUE(business_date, closing_revision)` on a `business_day_closings` ledger (Phase 2) so no duplicate closing record per date+revision.
- **H4:** `fuel_deliveries`: `UNIQUE(supplier_id, supplier_invoice_no)` where invoice no is not null → duplicate invoice posting rejected at the DB.
- **H5:** receipt/transaction UUIDs: `uuid TEXT UNIQUE NOT NULL` on all financial tables; services generate UUIDv4.
- **H6:** FK integrity between nozzle ↔ fuel product ↔ reading (`nozzle_readings.nozzle_id → nozzles.id`, and reading's implied product must equal nozzle's product — enforced by service; trigger E2 re-verifies nozzle active/product match).

### Group I — financial amount constraints
- **I1:** `CHECK(amount_paisa >= 0)` / `CHECK(quantity_ml >= 0)` on all receipt/payment/ticket columns; signed columns (`inventory_movements.quantity_ml`, adjustments) carry explicit `movement_type`-conditional checks.
- **I2:** `NOT NULL` on every required reference (`customer_id`, `source_uuid`, `business_date`, `occurred_at_utc`).
- **I3:** status-transition triggers: e.g. `credit_sales.status` may go `posted → reversed` only; `business_days.status` transitions restricted to `open → closed → reopened → closed`, `open → non_operating` only via the business-day service (trigger `trg_business_days_bu_status_flow` aborts illegal jumps).
- **I4:** reversal linkage: a row with `status='reversed'` must reference its reversing row (`reversed_by_uuid NOT NULL`) — enforced by trigger on the transition.

**Cross-cutting rule:** no trigger performs multi-table accounting math. Triggers validate the single row + cheap aggregate guards; the service layer computes totals inside one transaction.

## 5. Timestamp and numeric storage standards

### 5.1 Timestamps
- **Machine time** is stored as UTC text: `YYYY-MM-DDTHH:MM:SS.SSSZ` (millisecond precision), e.g. `2026-10-15T08:58:36.125Z`. SQLite default expression: `strftime('%Y-%m-%dT%H:%M:%fZ','now')`. Never store local wall-clock as UTC; never mix naive/aware.
- **Business date** is a separate `TEXT 'YYYY-MM-DD'` column, the station's operating date in `Asia/Karachi`. Never derived by slicing a UTC timestamp; the service computes it from `datetime.now(Karachi)`.
- Every business/master table carries `created_at_utc` (immutable, §4-A1); mutable tables add `updated_at_utc` (trigger-maintained); event tables add `occurred_at_utc`; user-attributable tables add `created_by` (default `'system'`, never invented names).
- **Display:** convert UTC → `Asia/Karachi`; user-facing date format `DD-MM-YYYY`. Language switch never alters stored values.
- **Clock errors:** at startup compare `now` with `MAX(created_at_utc/occurred_at_utc)`; if the clock moved backwards materially, warn the manager and continue (never rewrite history).
- **Midnight/post-midnight:** a transaction at 00:20 PKT belongs to the *open business day*, which the manager controls via explicit open/close — the app never auto-rolls the business date from the wall clock alone.

### 5.2 Numeric storage and calculation policy
| Concept | Storage | Example |
|---|---|---|
| Money | `INTEGER` paisa (`*_paisa`); 1 PKR = 100 paisa | PKR 1,234.56 → `123456` |
| Fuel quantity | `INTEGER` milliliters (`*_ml`); 1 L = 1000 ml | 47.000 L → `47000` |
| Meter readings | `INTEGER` milliliters (`reading_ml`) | 84,985 L → `84985000` |
| Unit price (fuel) | `INTEGER` in 0.0001 PKR per litre (`*_10k`) | PKR 265.9630/L → `2659630` |
| Unit price (other goods) | `INTEGER` paisa per unit | — |

- **Never** Python `float` or SQLite `REAL` for money, quantities, or prices — in code, SQL, or exports.
- **Calculation:** all money math in Python `Decimal`; **round-half-up** (`ROUND_HALF_UP`), applied **once at the transaction boundary** to produce the final integer paisa. No intermediate rounding.
- **Canonical amount formula** (exact-price sale): `amount_paisa = round_half_up(Decimal(quantity_ml) × Decimal(unit_price_10k) / 100000)` — because amount_PKR = (ml/1000) × (price_10k/10000), ×100 for paisa = ml × price_10k / 10⁵.
  - Test 1 check: 47 L = 47000 ml; at price 2659630 (PKR 265.9630) → 47000 × 2659630 / 100000 = 1,250,026.1 → **1,250,026 paisa = PKR 12,500.26**.
- `app/calculations/` holds the single implementation (`to_paisa`, `to_ml`, `amount_paisa_for`, `apply_rounding`); every service uses it; unit tests pin the formulas.

---

## 6. Daily fuel-sales reconciliation model

**Source of truth:** meter-derived sales. For each nozzle, each validated reading interval `[prev_reading, curr_reading]` yields `liters_sold_ml`; the interval is priced by the `price_history` row effective at the interval's `occurred_at_utc` (mid-day price changes require a snapshot reading — §11 assumption 2). Revenue per product = Σ intervals × price → `amount_paisa`.

**Settlement identity (must balance every closed day):**
```
meter_revenue_today  =  settled_against_today_sales  +  new_receivables_today
```
where `settled_against_today_sales` is recorded in `sales_settlements` by category (`cash`, `bank`, `paid_now_on_ticket`, `other`) and `new_receivables_today = Σ credit_sales.gross_amount_paisa − Σ paid_now_paisa` for today.

**What is NOT revenue:** old-debt recovery. A `customer_payments` row allocated (via `payment_allocations`) to an earlier ticket creates a `cash_receipts(category='credit_recovery')` row and reduces the receivable; it never touches `sales_settlements` or revenue. The register's "وصولی" (receipts) section maps to `cash_receipts`/`customer_payments`, never to sales.

**Worked example (spec Test 5–7):** ticket 40 L @ PKR 270 = **PKR 10,800** gross, paid_now PKR 3,000 → `sales_settlements(paid_now_on_ticket, 3000)` + `new_receivable 7,800`. Later PKR 2,000 recovery → `cash_receipts(credit_recovery, 2000)`, receivable 7,800 → 5,800, revenue unchanged. Any report showing PKR 13,800 sales here is a defect.

**Reconciliation report columns:** meter revenue by product → settlement by category → new receivables → *unresolved difference* (must be 0 to close; warnings vs blocking errors per §19).

**Double-count guards:** `sales_settlements` rows carry `source_uuid` unique per source; reading intervals are derived, not typed; re-saving a reading screen never creates a second interval (E1 uniqueness + idempotent posting keyed by `(nozzle_id, business_date, reading_kind)`).

---

## 7. Customer-credit accounting model

- **Ticket (`credit_sales`):** one row per credit issue. `gross_amount_paisa` from §5.2 formula; `paid_now_paisa` (cash taken at the moment of sale, allocated to *this* ticket immediately); `unpaid = gross − paid_now`. Status `posted` (immutable, §4-C); corrections via reversal ticket linked by `reverses_uuid`.
- **Payment (`customer_payments`):** one row per money received; `method` cash/bank/other.
- **Allocation (`payment_allocations`):** links payment → ticket(s); default order **FIFO** (oldest unpaid ticket first), configurable in `app_settings.allocation_order`. Guard G1 enforces `Σ allocations ≤ payment.amount` at the DB; the service computes the split inside one transaction — over-allocation fails atomically with nothing persisted (spec Test 12).
- **Balance (ledger-derived, never typed):**
```
outstanding = opening_receivable
            + Σ posted ticket gross
            − Σ posted ticket paid_now
            − Σ allocations to posted tickets
            − Σ authorized credit adjustments
```
  The UI shows this computed figure; an `opening_receivable` error is fixed by an audited opening-balance adjustment, never by editing the computed balance.
- **Ledger view (`v_customer_ledger`):** chronological charges / payments / adjustments with running balance; powers statements and receipts.
- **Meter reconciliation:** credit liters per product per day must be ≤ meter-derived liters sold (warning if exceeded; blocking if the ticket's nozzle/product is inconsistent).

---

## 8. Stock and profit methodology

### Stock (inventory movement ledger)
`inventory_movements` is the single source of truth. Movement types: `opening` (day-1 closing carry-forward), `delivery` (+), `sale` (−, one row per validated reading interval, keyed by unique `source_uuid`), `adjustment` (±, manager-approved with reason).
```
theoretical_close = opening + Σ deliveries − Σ sales + Σ adjustments
variance          = physical_count − theoretical_close
```
Physical counts never overwrite the theoretical figure; an approved variance becomes an `adjustment` movement. Nobody types into a "stock balance" field — the balance is always `SUM(quantity_ml)`.

### Profit (periodic weighted-average costing)
- Cost basis per product per period: `avg_cost = (opening_value_paisa + Σ delivery total_cost_paisa) / (opening_qty_ml + Σ delivery qty_ml)`, computed in Decimal at period close and **snapshotted** into the closing record so old P&L never changes when new purchases are entered.
- `COGS = Σ sales qty_ml × avg_cost` (rounded once per day per product).
- `gross_profit = revenue − COGS`; `net_profit = gross_profit − operating_expenses`.
- Reports label figures **estimated** when opening valuations or expenses are missing. Profit is never derived from drawer cash; debt recovery is never revenue.

---

## 9. Backup and restore design

**Live DB stays local** (`%LOCALAPPDATA%\PetrolPumpManager\data\`). Google Drive for desktop is used only as a synchronized *folder destination* — no Google API, no internet required to operate.

**Backup pipeline (every step recorded in `backup_history`):**
1. `status='requested'` — user action, day-close auto-backup, scheduled, or shutdown backup.
2. Consistent snapshot via the **SQLite online backup API** (`sqlite3.Connection.backup()`) to a temp file — never a raw file copy of a WAL-mode DB.
3. `status='snapshot_created'` — temp file closed cleanly.
4. Validate: open the snapshot read-only, run `PRAGMA integrity_check` + `foreign_key_check`, sanity row-counts (business_days, audit_log). On failure → `status='failed'`, keep going on local data, alert the user.
5. `status='validated'` → atomic rename to `PetrolPump_Backup_YYYY-MM-DD_HHMMSS.sqlite` (local time PKT in the *filename only*).
6. Copy the validated file into the configured Drive folder; `status='copied_to_drive'` after `os.path.exists` + plausible size check; sha256 recorded.
7. **Honest cloud status:** `status='sync_confirmed'` only with a reliable OS signal of Drive sync (rare) — otherwise the Drive copy stays `copied_to_drive` and the UI says *"in Google Drive folder — cloud sync pending/unconfirmed"*, never *"backed up to cloud"*. If Drive is unavailable: local backup proceeds, Drive leg marked `cloud_unavailable`, app keeps working offline.

**Restore:** before any restore, take an automatic local safety backup; validate the candidate backup (integrity + FK + spot-check business dates/nozzles/customers/cash/audit); require explicit typed confirmation; replace the live DB only after validation; re-verify afterwards. Never auto-delete the last valid backup. Retention setting prunes *old validated* backups only.

---

## 10. Implementation phases & acceptance-test plan

| Phase | Scope | Done when |
|---|---|---|
| 1 Foundation | Structure, PySide6 shell, paths, pragma-verified connections, versioned migrations, schema-version safety, trigger groups A–D/H for installed tables, integrity checks | App opens/closes DB safely; suite green |
| 2 Business day & register | Open/close/reopen workflow, opening cash/stock, previous→current readings, carry-forward traceability | Carry-forward tests green |
| 3 Fuel sales & stock | `price_history`, deliveries, `inventory_movements`, meter-derived sales, tank reconciliation | No double-count tests green |
| 4 Customer credit | Customers, tickets, payments, FIFO allocations, statements | Revenue/receipt separation tests green |
| 5 Cash book & accounting | Receipts/payments, expenses, supplier liabilities, settlement ledger, P&L | Reconciliation tests green |
| 6 Reports & localization | 21 reports, bilingual UI + Urdu PDF, XLSX/CSV | Report + RTL tests green |
| 7 Security & recovery | Full audit/immutability/closed-day triggers, reopen flow, migration tests | Attack-style SQL tests green |
| 8 Backup & packaging | Verified backups, Drive folder, retention, restore, installer | Backup/restore + clean-install tests green |
| 9 Acceptance | Full suite, real desktop workflows, offline run, clean-Windows install | Test report signed off |

**Never advance past a phase whose core rules fail tests.**

### Acceptance tests (all in temp DBs; map to spec §25)
1. **Nozzle calculation** — prev 84,938 L, curr 84,985 L → 47,000 ml; carry-forward to next day.
2. **Duplicate daily reading** — second `closing` reading for same nozzle+date rejected (E1).
3. **Price change** — price edited after day end; old sales/tickets keep original values (price_history immutable).
4. **Automatic stock** — 5,000 + 2,000 − 500 = 6,500 L theoretical; posting twice rejected (F1).
5. **Credit sale** — 40 L @ PKR 270 → PKR 10,800 gross; receivable += unpaid; liters stay inside meter sales.
6. **Partial payment** — pay 3,000 on 10,800 ticket → 7,800 remains; receipt recorded, not a sale.
7. **Old debt recovery** — later 2,000 → balance 5,800; cash +2,000, receivable −2,000, revenue unchanged.
8. **Daily sales reconciliation** — meter revenue = settlements + new receivables; old recovery excluded; duplicate settlement rejected.
9. **Daily cash** — 20,000 + 50,000 + 3,000 − 5,000 − 8,000 = **60,000** expected; each receipt/payment counted once.
10. **Closed-day protection** — direct SQL change on closed day rejected (D); authorized reopen → correction works, fully audited.
11. **Audit trail** — insert/update/delete attempts produce correct audit rows; posted rows immutable; timestamps behave per A.
12. **Payment over-allocation** — allocate 6,000 against 5,000 payment → whole operation fails, nothing persisted.
13. **Inventory underflow** — sale that would drive stock negative is blocked (default policy).
14. **Backup consistency** — backup under WAL; snapshot opens, passes integrity, contains expected rows.
15. **Restore recovery** — restore known-good backup to temp location; balances identical before/after.
16. **Offline test** — all core flows with no network (no code path requires it).
17. **Transaction rollback** — forced mid-transaction error → zero partial writes.
18. **Timestamp boundaries** — UTC storage, PKT business date, midnight rollover, rapid identical timestamps.
19. **Historical closing revisions** — close → reopen → correct → close; both revisions + audits retained.
20. **Financial calculations** — price/quantity precision, rounding, COGS, reversals across many transactions.

## 11. Unresolved accounting assumptions & chosen safe defaults

These were ambiguous in the requirements; each lists the safe default implemented unless the product owner overrides it (overrides go in the amendment log).

1. **Meter rollover / reset / replacement.** Default: a reading lower than the previous reading is **rejected** unless the manager first records a `meter_events` row (type `reset`/`rollover`/`replacement`) with a reason and authorizer. A reset is never treated as a negative sale; quantity is computed only from an explicit rollover event's documented wrap value. *(Why: prevents the classic "meter reset looks like −84,938 L sale" corruption.)*
2. **Mid-day price change.** Default: the price change takes effect only after the manager records a meter **snapshot reading** (`reading_kind='segment'`). Each reading interval is priced by the `price_history` row effective at its `occurred_at_utc`. Today's price is never applied retrospectively. If no snapshot exists, the service blocks the price change with an explanatory error.
3. **Sale that would drive tracked stock negative.** Default: **blocked** at the service layer and by trigger F2. There is no silent negative-stock exception in v1; the manager must post an authorized `adjustment` (e.g. missed delivery entry) first, which is audited. *(Why: negative stock hides theft/leakage; an explicit adjustment names the cause.)*
4. **Payment allocation order.** Default: **FIFO** — a payment settles the oldest unpaid ticket first. Configurable via `app_settings.allocation_order`. Money handed over *at the moment of a credit sale* (`paid_now_paisa`) always allocates to that ticket, never to older debt.
5. **Opening balances.** Entered once by the manager at setup; afterwards the computed ledger balance is read-only. Errors are fixed by audited opening-balance adjustments, never by editing the balance field.
6. **Non-operating (station closed) days.** A `business_days` row with `status='non_operating'` carries forward cash/stock/readings unchanged; no readings or sales may be posted to it; no figures are invented.
7. **Credit limits.** Default: optional and **warning-only** (soft). The manager may configure hard blocking later; v1 never blocks a sale on credit limit without an explicit setting.
8. **Cash shortage/surplus.** Never auto-written-off or auto-adjusted. Shown separately; resolution is a manager decision recorded as an audited note/expense.
9. **Rounding.** Round-half-up, applied once at the transaction boundary (§5.2). Reports sum stored integer paisa; they never re-round from decimals.
10. **Operator identity (v1).** Single-manager operation; `created_by` defaults to `'system'` and the UI passes the configured operator name when set. No invented usernames appear in the audit log.
11. **Tank-level stock.** `tanks` and tank FKs exist from Phase 1; tank-level movements are implemented in Phase 3. Product-level rollup is always the fallback and always consistent.
12. **Reading granularity.** Phase 1 enforces one `closing` reading per nozzle per date (E1). Intraday `segment` readings (for price changes) and `opening_snapshot` rows are Phase 3; the partial unique index already anticipates them.
13. **Reversal vs. reopen.** Correcting a *posted* ticket/reading on an *open* day uses a reversal/adjustment row (original preserved). Correcting anything on a *closed* day requires the authorized reopen workflow (§4-D) — there is no "quiet edit" path.
14. **Bank/non-cash sales.** Tracked in `sales_settlements` (category `bank`) and excluded from physical-cash arithmetic. Phase 5 adds a bank ledger if the owner needs one; v1 cash book counts cash only.
15. **Backup "cloud" claims.** The UI may only claim cloud backup on a positive sync signal; the normal honest state is *"copied to Google Drive folder — sync unconfirmed"* (§9).

---

## Amendment log

| Date | Author | Change |
|---|---|---|
| 2026-10-15 | Build coordinator | v1.0 initial contract. Phase 1 authorized to proceed. |
| 2026-10-15 | Build coordinator | Trigger refinement (pre-first-deployment; no live DB existed, migrations edited in place): corrected 002 header comment (recursive_triggers does not suppress *other* triggers fired by the A2 inner UPDATE); added business-column WHEN guards to the five …_au_audit triggers in 003 so the timestamp-touch writes no audit row — exactly one audit row per business UPDATE. Pinned by test_audit.py::test_timestamp_touch_writes_no_audit_row. |
| 2026-10-15 | Phase 2 build | Phase 2 schema additions in NEW migrations 005–006 (001–004 untouched): `business_day_closings` ledger (§3/H3 — immutable, one row per date+revision), `opening_balances` (every carry-forward value stored with its source closing record), `nozzle_readings.supersedes_reading_id` (correction workflow reuses `reading_kind='closing'` so no table rebuild; E1 partial index now predicates `supersedes_reading_id IS NULL`), `v_effective_nozzle_readings` view, E2 trigger rewritten to validate against *effective* readings, new `trg_nozzle_readings_bi_non_operating` (no readings on non-operating days, §11.6). Interpretations: post-reset/replacement interval quantity is *unknown* (None) with a close-checklist warning — never a negative sale; rollover wrap-value math deferred to Phase 3. Only the most recent day may be reopened (protects newer days' carry-forward). Date gaps need an audited reason. Checklist items without a module yet report `na`, never a fake pass. Full validation-message translation deferred to Phase 6; all static UI strings bilingual. |
| 2026-10-15 | Phase 7 build | Migration 013 (001–012 untouched): closed three trigger gaps — `backup_history` gained A1/A2/B/C2 triggers (`created_at_utc`/`updated_at_utc` added via the official 12-step table rebuild, since SQLite rejects ADD COLUMN with a non-constant default; status UPDATEs stay allowed+audited per the Phase-8 backup-pipeline contract), `meter_events` gained timestamp columns + A1/A2 (updates stay allowed+audited: `wrap_value_ml` may be recorded after the event), `schema_migrations` gained no-modify guards (INSERT-only). **Bug fix (no migration needed):** `sales_service._post_day_sales` was existence-only — a reading correction made AFTER sales were posted left the stale movement on the ledger (stock overstated). It is now correction-aware: net posted quantity (original sale + prior correction adjustments) is compared with the effective interval; divergences append a single `adjustment` movement for the delta (`nozzle-sale-corr:{date}:{nozzle_id}:{seq}:{n}`), unknown/zero intervals fully reverse the posted sale; original rows never edited. Pinned by `test_phase7_sale_correction.py` (4 tests). |
| 2026-10-15 | Phase 7 build | Migration 014 (001–013 untouched): D2/D3 closed-day guards for `tank_readings` (`trg_tank_readings_bu_closed_day`, `trg_tank_readings_bd_closed_day`) — attack testing proved raw-SQL UPDATE/DELETE on closed days succeeded (only D1 existed). Open-day dip corrections stay allowed + audited by deliberate design. `test_phase7_attack.py::test_closed_day_tank_readings_update_delete_gap` now asserts the block. |

| 2026-10-15 | Phase 8 build | Migration 015 (001–014 untouched): `backup_history` gains nullable `trigger` column ('manual','day_close','shutdown','scheduled','safety'). New `BackupService` implements the §9 pipeline exactly (online backup API → temp file → integrity/FK validation → atomic rename to `PetrolPump_Backup_YYYY-MM-DD_HHMMSS.sqlite` → Drive-folder copy → honest statuses; `sync_confirmed` only on a real OS signal, otherwise "in Google Drive folder — sync unconfirmed"; never a raw WAL file copy). Restore: automatic safety backup first, candidate validation, typed-confirmation, post-restore verification. Retention prunes old validated backups only — newest known-good per destination is never deleted. Lifecycle wiring: auto-backup after day-close (failure never breaks the close), headless shutdown backup (setting, default off), automatic pre-migration safety backup in `bootstrap()` (fail-closed: backup failure aborts the upgrade via `AppStartupError`); the safety-backup path is schema-tolerant of pre-015 databases. UI: working Backups page + Settings storage locations, EN/UR parity. Packaging: PyInstaller one-dir spec (windowed), Inno Setup script (data dir excluded from uninstall), `documentation/install.md` with honest unsigned-build/antivirus notes. Bug fixes found by testing: day-close hook now uses the service's own DB handle (was snapshotting the live DB from tests); backup temp-file `-shm`/`-wal` sidecars cleaned up; retention newest-guard. Pinned by `test_phase8_backup.py` (23) + `test_phase8_acceptance.py` (11, incl. acceptance Tests 14–15). Manual steps remaining: clean-Windows install test (Phase 9). |

*End of design plan.*
