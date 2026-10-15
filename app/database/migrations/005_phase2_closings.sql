-- ============================================================================
-- Migration 005 — Phase 2: day-closing ledger + traceable opening balances
-- Design plan: documentation/00_design_plan.md §3 (business_day_closings
-- ledger, §4-H3), §4 groups A/B/C/D, §10 (carry-forward traceability).
--
-- Two new tables:
--   * business_day_closings — one IMMUTABLE row per closing revision.
--     Reopening a day and closing it again inserts a NEW row (revision+1);
--     the old row is never overwritten, so historical reports stay stable.
--   * opening_balances — every carry-forward value (day-N opening cash,
--     later: opening stock) recorded together with the source closing
--     record it came from. The computed ledger balance is never typed in.
--
-- Both tables are append-only (Group C), audited (Group B), timestamp-
-- guarded (Group A1) and closed-day guarded (Group D1).
-- ============================================================================

-- --- Immutable closing records ----------------------------------------------
CREATE TABLE business_day_closings (
    id                    INTEGER PRIMARY KEY,
    business_date         TEXT NOT NULL REFERENCES business_days (business_date),
    closing_revision      INTEGER NOT NULL CHECK (closing_revision >= 1),
    closed_at_utc         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    prev_closing_id       INTEGER REFERENCES business_day_closings (id),
    actor                 TEXT NOT NULL DEFAULT 'system',
    opening_cash_paisa    INTEGER NOT NULL CHECK (opening_cash_paisa >= 0),
    closing_cash_paisa    INTEGER NOT NULL CHECK (closing_cash_paisa >= 0),
    petrol_litres_ml      INTEGER NOT NULL DEFAULT 0 CHECK (petrol_litres_ml >= 0),
    diesel_litres_ml      INTEGER NOT NULL DEFAULT 0 CHECK (diesel_litres_ml >= 0),
    summary_json          TEXT NOT NULL,
    warnings_json         TEXT NOT NULL DEFAULT '[]',
    created_at_utc        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    created_by            TEXT NOT NULL DEFAULT 'system',
    -- H3: no duplicate closing record for the same date and revision.
    UNIQUE (business_date, closing_revision)
);
CREATE INDEX idx_business_day_closings_business_date
    ON business_day_closings (business_date);
CREATE INDEX idx_business_day_closings_prev_closing_id
    ON business_day_closings (prev_closing_id);

-- --- Traceable opening balances ----------------------------------------------
-- One row per carried-forward value. 'cash' rows carry opening_cash_paisa;
-- 'stock' rows (written from Phase 3 on) carry per-product opening stock.
-- The source_* columns identify the exact closing record the value came
-- from; NULL source means manager-entered initial balances (first day).
CREATE TABLE opening_balances (
    id                      INTEGER PRIMARY KEY,
    business_date           TEXT NOT NULL REFERENCES business_days (business_date),
    balance_type            TEXT NOT NULL CHECK (balance_type IN ('cash', 'stock')),
    fuel_product_id         INTEGER REFERENCES fuel_products (id),
    amount_paisa            INTEGER CHECK (amount_paisa >= 0),
    quantity_ml             INTEGER CHECK (quantity_ml >= 0),
    source_business_date    TEXT REFERENCES business_days (business_date),
    source_closing_revision INTEGER,
    created_at_utc          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    created_by              TEXT NOT NULL DEFAULT 'system',
    CHECK (
        (balance_type = 'cash'
             AND amount_paisa IS NOT NULL
             AND quantity_ml IS NULL
             AND fuel_product_id IS NULL)
        OR
        (balance_type = 'stock'
             AND quantity_ml IS NOT NULL
             AND amount_paisa IS NULL
             AND fuel_product_id IS NOT NULL)
    )
);
-- Exactly one cash opening balance per day; one stock row per product per day.
CREATE UNIQUE INDEX uq_opening_balances_cash
    ON opening_balances (business_date) WHERE balance_type = 'cash';
CREATE UNIQUE INDEX uq_opening_balances_stock
    ON opening_balances (business_date, fuel_product_id) WHERE balance_type = 'stock';
CREATE INDEX idx_opening_balances_business_date
    ON opening_balances (business_date);

-- --- Settings keys used by Phase 2 -----------------------------------------------
-- NOTE: no seed rows are inserted here. app_settings rows are audited, and
-- seeding defaults would pollute every fresh database's audit log with
-- migration-time rows (pinned by test_audit.py). The services treat a
-- missing 'operator_name'/'station_name' as unset (→ 'system'), and the
-- Settings page upserts them on save.

-- ================= Group A1 — created_at_utc immutability =====================
CREATE TRIGGER trg_business_day_closings_bu_guard_created
BEFORE UPDATE ON business_day_closings
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_opening_balances_bu_guard_created
BEFORE UPDATE ON opening_balances
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

-- ================= Group B — audit history (INSERT only; ==============
-- updates/deletes are blocked by Group C below, so no au/bd audit needed)
CREATE TRIGGER trg_business_day_closings_ai_audit
AFTER INSERT ON business_day_closings
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('business_day_closings', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'business_date', NEW.business_date,
                        'closing_revision', NEW.closing_revision,
                        'closed_at_utc', NEW.closed_at_utc,
                        'prev_closing_id', NEW.prev_closing_id,
                        'actor', NEW.actor,
                        'opening_cash_paisa', NEW.opening_cash_paisa,
                        'closing_cash_paisa', NEW.closing_cash_paisa,
                        'petrol_litres_ml', NEW.petrol_litres_ml,
                        'diesel_litres_ml', NEW.diesel_litres_ml,
                        'created_at_utc', NEW.created_at_utc,
                        'created_by', NEW.created_by),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_opening_balances_ai_audit
AFTER INSERT ON opening_balances
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('opening_balances', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'business_date', NEW.business_date,
                        'balance_type', NEW.balance_type,
                        'fuel_product_id', NEW.fuel_product_id,
                        'amount_paisa', NEW.amount_paisa,
                        'quantity_ml', NEW.quantity_ml,
                        'source_business_date', NEW.source_business_date,
                        'source_closing_revision', NEW.source_closing_revision,
                        'created_at_utc', NEW.created_at_utc,
                        'created_by', NEW.created_by),
            NEW.business_date, NEW.created_by);
END;

-- ================= Group C — ledger immutability ==============================
-- Closing records and opening balances are posted facts: no UPDATE, no DELETE.
-- A wrong opening balance is fixed by an authorized adjustment row in a later
-- phase, never by editing history.
CREATE TRIGGER trg_business_day_closings_bu_no_update
BEFORE UPDATE ON business_day_closings
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'business_day_closings is immutable; close the day again for a new revision');
END;

CREATE TRIGGER trg_business_day_closings_bd_no_delete
BEFORE DELETE ON business_day_closings
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'business_day_closings is immutable; rows cannot be deleted');
END;

CREATE TRIGGER trg_opening_balances_bu_no_update
BEFORE UPDATE ON opening_balances
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'opening_balances is immutable; post an authorized adjustment instead');
END;

CREATE TRIGGER trg_opening_balances_bd_no_delete
BEFORE DELETE ON opening_balances
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'opening_balances is immutable; rows cannot be deleted');
END;

-- ================= Group D1 — closed business day =============================
-- Belt-and-braces: the close/reopen workflow itself runs while the day is
-- 'open'/'reopened', so legitimate writes pass; anything else on a closed
-- day is rejected even from raw SQL.
CREATE TRIGGER trg_business_day_closings_bi_closed_day
BEFORE INSERT ON business_day_closings
FOR EACH ROW
WHEN (SELECT status FROM business_days
       WHERE business_date = NEW.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;

CREATE TRIGGER trg_opening_balances_bi_closed_day
BEFORE INSERT ON opening_balances
FOR EACH ROW
WHEN (SELECT status FROM business_days
       WHERE business_date = NEW.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;
