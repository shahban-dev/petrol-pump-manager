-- ============================================================================
-- Migration 012 — stock_opening_values: manager-entered opening stock
-- valuations (Phase 5).
--
-- Why a new table: migration 005's CHECK on opening_balances forces
-- amount_paisa IS NULL for balance_type='stock' rows, and numbered
-- migrations are append-only — the CHECK cannot be relaxed in place.
-- The opening carrying VALUE of a product's stock (the cost basis for
-- periodic weighted-average COGS) is therefore recorded here, one row per
-- (day, product), written by InventoryService.set_opening_stock when the
-- manager supplies value_pkr. Later days take their opening value from the
-- previous closing record's cost snapshot; this table is only the first-day
-- entry path. Values are immutable posted facts (corrections go through a
-- new row for a new day, or an audited adjustment); the table is audited
-- and closed-day protected like every other business table.
-- ============================================================================

CREATE TABLE stock_opening_values (
    business_date    TEXT NOT NULL REFERENCES business_days (business_date),
    fuel_product_id  INTEGER NOT NULL REFERENCES fuel_products (id),
    value_paisa      INTEGER NOT NULL CHECK (value_paisa >= 0),
    created_by       TEXT NOT NULL DEFAULT 'system',
    created_at_utc   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (business_date, fuel_product_id)
);
CREATE INDEX idx_stock_opening_values_product
    ON stock_opening_values (fuel_product_id);

-- Group A1: created_at_utc is immutable.
CREATE TRIGGER trg_stock_opening_values_bu_guard_created
BEFORE UPDATE ON stock_opening_values
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

-- Group B: audit history.
CREATE TRIGGER trg_stock_opening_values_ai_audit
AFTER INSERT ON stock_opening_values
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('stock_opening_values', NEW.fuel_product_id, 'INSERT', NULL,
            json_object('business_date', NEW.business_date,
                        'fuel_product_id', NEW.fuel_product_id,
                        'value_paisa', NEW.value_paisa,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_stock_opening_values_bd_audit
BEFORE DELETE ON stock_opening_values
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('stock_opening_values', OLD.fuel_product_id, 'DELETE',
            json_object('business_date', OLD.business_date,
                        'fuel_product_id', OLD.fuel_product_id,
                        'value_paisa', OLD.value_paisa),
            NULL, OLD.business_date, OLD.created_by);
END;

-- Group C: immutable posted facts.
CREATE TRIGGER trg_stock_opening_values_bu_no_update
BEFORE UPDATE ON stock_opening_values
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'stock_opening_values is immutable; post a correcting value for a new day instead');
END;

CREATE TRIGGER trg_stock_opening_values_bd_no_delete
BEFORE DELETE ON stock_opening_values
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'stock_opening_values is immutable; rows cannot be deleted');
END;

-- Group D: closed business day.
CREATE TRIGGER trg_stock_opening_values_bi_closed_day
BEFORE INSERT ON stock_opening_values
FOR EACH ROW
WHEN (SELECT status FROM business_days
      WHERE business_date = NEW.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;
