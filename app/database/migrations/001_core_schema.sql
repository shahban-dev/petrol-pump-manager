-- ============================================================================
-- Migration 001 — Core schema (Phase 1)
-- Design plan: documentation/00_design_plan.md §3 (tables marked ★)
--
-- Conventions (per §3 / §5):
--   * money      → INTEGER paisa   (*_paisa),  CHECK >= 0 unless signed
--   * quantities → INTEGER milliliters (*_ml)
--   * prices     → INTEGER in 0.0001 PKR (*_10k)
--   * timestamps → TEXT UTC 'YYYY-MM-DDTHH:MM:SS.SSSZ' via
--                   strftime('%Y-%m-%dT%H:%M:%fZ','now'); never CURRENT_TIMESTAMP
--   * business_date → TEXT 'YYYY-MM-DD', the Asia/Karachi operating date
--   * created_at_utc is immutable (guarded by Group A triggers in 002)
--   * created_by defaults to 'system'; real operator names are never invented
-- ============================================================================

-- --- Ledger of applied migrations -------------------------------------------
CREATE TABLE schema_migrations (
    version        INTEGER PRIMARY KEY,
    applied_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    description    TEXT
);

-- --- Application settings (key/value store) ---------------------------------
CREATE TABLE app_settings (
    key            TEXT PRIMARY KEY,
    value          TEXT NOT NULL,
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- --- Business days -----------------------------------------------------------
CREATE TABLE business_days (
    business_date       TEXT PRIMARY KEY
                        CHECK (business_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    status              TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'closed', 'reopened', 'non_operating')),
    opening_cash_paisa  INTEGER NOT NULL DEFAULT 0 CHECK (opening_cash_paisa >= 0),
    prev_closing_date   TEXT REFERENCES business_days (business_date),
    opened_at_utc       TEXT,
    closed_at_utc       TEXT,
    closing_revision    INTEGER NOT NULL DEFAULT 0,
    physical_cash_paisa INTEGER,
    created_at_utc      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at_utc      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_business_days_prev_closing_date
    ON business_days (prev_closing_date);

-- --- Business day events (append-only lifecycle log) --------------------------
CREATE TABLE business_day_events (
    id              INTEGER PRIMARY KEY,
    business_date   TEXT NOT NULL REFERENCES business_days (business_date),
    event_type      TEXT NOT NULL CHECK (event_type IN ('open', 'close', 'reopen', 'note')),
    occurred_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    actor           TEXT NOT NULL DEFAULT 'system',
    reason          TEXT,
    payload_json    TEXT
);
CREATE INDEX idx_business_day_events_business_date
    ON business_day_events (business_date);

-- --- Fuel products (master data) ----------------------------------------------
CREATE TABLE fuel_products (
    id             INTEGER PRIMARY KEY,
    code           TEXT NOT NULL UNIQUE,
    name_en        TEXT NOT NULL,
    name_ur        TEXT,
    is_active      INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- --- Nozzles (master data; tank wiring arrives with the tanks table, Phase 3) -
-- NOTE: tank_id is a plain nullable column in Phase 1. The REFERENCES clause
-- for tanks(id) is deliberately deferred to the Phase-3 migration that
-- creates the tanks table: SQLite refuses to *prepare* any INSERT/UPDATE on
-- a child table whose parent table does not exist yet (foreign-key
-- mismatch), so declaring the FK now would make it impossible to insert
-- nozzles at all. The column is here so the wiring point exists from Phase 1.
CREATE TABLE nozzles (
    id              INTEGER PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,
    fuel_product_id INTEGER NOT NULL REFERENCES fuel_products (id),
    tank_id         INTEGER,  -- FK to tanks(id) added in Phase 3
    label_en        TEXT,
    label_ur        TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    sort_order      INTEGER NOT NULL DEFAULT 0,
    created_at_utc  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at_utc  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_nozzles_fuel_product_id ON nozzles (fuel_product_id);
CREATE INDEX idx_nozzles_tank_id ON nozzles (tank_id);

-- --- Nozzle meter readings (posted facts; immutable via Group C) ---------------
CREATE TABLE nozzle_readings (
    id              INTEGER PRIMARY KEY,
    nozzle_id       INTEGER NOT NULL REFERENCES nozzles (id),
    business_date   TEXT NOT NULL REFERENCES business_days (business_date),
    reading_kind    TEXT NOT NULL DEFAULT 'closing'
                    CHECK (reading_kind IN ('closing', 'segment', 'opening_snapshot')),
    reading_ml      INTEGER NOT NULL CHECK (reading_ml >= 0),
    occurred_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    created_by      TEXT NOT NULL DEFAULT 'system',
    created_at_utc  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at_utc  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
-- E1: at most one closing reading per nozzle per business date.
CREATE UNIQUE INDEX uq_nozzle_readings_closing
    ON nozzle_readings (nozzle_id, business_date)
    WHERE reading_kind = 'closing';
CREATE INDEX idx_nozzle_readings_nozzle_id ON nozzle_readings (nozzle_id);
CREATE INDEX idx_nozzle_readings_business_date ON nozzle_readings (business_date);

-- --- Meter events (resets / rollovers / replacements / corrections) ------------
CREATE TABLE meter_events (
    id                INTEGER PRIMARY KEY,
    nozzle_id         INTEGER NOT NULL REFERENCES nozzles (id),
    event_type        TEXT NOT NULL
                      CHECK (event_type IN ('reset', 'rollover', 'replacement', 'correction')),
    reading_before_ml INTEGER,
    reading_after_ml  INTEGER,
    occurred_at_utc   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    reason            TEXT NOT NULL,
    authorized_by     TEXT NOT NULL DEFAULT 'system'
);
CREATE INDEX idx_meter_events_nozzle_id ON meter_events (nozzle_id);

-- --- Audit log (append-only; protected by trg_audit_log_*_no_modify in 003) -----
CREATE TABLE audit_log (
    id               INTEGER PRIMARY KEY,
    occurred_at_utc  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    table_name       TEXT NOT NULL,
    record_id        INTEGER NOT NULL,
    action           TEXT NOT NULL CHECK (action IN ('INSERT', 'UPDATE', 'DELETE')),
    old_values_json  TEXT,
    new_values_json  TEXT,
    business_date    TEXT,
    actor            TEXT NOT NULL DEFAULT 'system',
    reason           TEXT
);
CREATE INDEX idx_audit_log_table_record ON audit_log (table_name, record_id);
CREATE INDEX idx_audit_log_occurred_at_utc ON audit_log (occurred_at_utc);

-- --- Backup history (operational log of the §9 backup pipeline) -----------------
CREATE TABLE backup_history (
    id               INTEGER PRIMARY KEY,
    requested_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    filename         TEXT NOT NULL,
    destination      TEXT NOT NULL CHECK (destination IN ('local', 'gdrive')),
    status           TEXT NOT NULL
                     CHECK (status IN ('requested', 'snapshot_created', 'validated',
                                       'copied_to_drive', 'sync_confirmed',
                                       'cloud_unavailable', 'failed')),
    size_bytes       INTEGER,
    sha256           TEXT,
    verified_at_utc  TEXT,
    error            TEXT
);

-- ============================================================================
-- Seed data (idempotent: INSERT OR IGNORE, safe to re-run)
--
-- CONFIGURABLE EXAMPLES — transcribed from the client's handwritten register
-- (4 nozzles: '1 PG+', '2 PG+' dispensing petrol; '3 HSD', '4 HSD' dispensing
-- diesel). These are seed *configuration* the manager may rename, deactivate
-- or extend. No code may assume exactly four nozzles or these names.
-- ============================================================================
INSERT OR IGNORE INTO fuel_products (code, name_en, name_ur, is_active) VALUES
    ('PETROL', 'Petrol', 'پٹرول', 1),
    ('DIESEL', 'Diesel', 'ڈیزل', 1);

INSERT OR IGNORE INTO nozzles (code, fuel_product_id, label_en, is_active, sort_order)
VALUES
    ('1 PG+', (SELECT id FROM fuel_products WHERE code = 'PETROL'), '1 PG+', 1, 1),
    ('2 PG+', (SELECT id FROM fuel_products WHERE code = 'PETROL'), '2 PG+', 1, 2),
    ('3 HSD', (SELECT id FROM fuel_products WHERE code = 'DIESEL'), '3 HSD', 1, 3),
    ('4 HSD', (SELECT id FROM fuel_products WHERE code = 'DIESEL'), '4 HSD', 1, 4);
