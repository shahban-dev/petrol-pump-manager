-- ============================================================================
-- Migration 002 — Trigger group A: automatic timestamps
-- Design plan §4, group A. Applies to every Phase-1 mutable table
-- (app_settings, business_days, fuel_products, nozzles, nozzle_readings).
--
-- A1 (…_bu_guard_created): BEFORE UPDATE aborts any change to created_at_utc.
--     `IS NOT` is used so NULL handling is exact.
-- A2 (…_bu_touch_updated): BEFORE UPDATE refreshes updated_at_utc, but ONLY
--     when a business column actually changed (explicit per-table column
--     comparison). A no-op UPDATE therefore neither bumps the timestamp nor
--     loops. SQLite triggers cannot assign to NEW.*, so the touch is performed
--     as an inner UPDATE of the same row. Notes on the mechanics (verified
--     empirically on SQLite 3.45.1):
--     * The inner UPDATE re-fires the BEFORE UPDATE triggers, but their WHEN
--       clauses only match business-column changes, so the touch does not
--       re-touch: no infinite loop. (PRAGMA recursive_triggers is irrelevant
--       here — it only suppresses a trigger firing *itself* recursively;
--       other triggers on the same table still fire.)
--     * The inner UPDATE fires the AFTER UPDATE audit trigger; migration 003
--       therefore guards its …_au_audit triggers with the same business-column
--       WHEN clause, so a touch-only inner update writes no audit row and each
--       real business update yields exactly one audit row.
--     * The touched updated_at_utc value survives the outer UPDATE (the outer
--       statement does not clobber it); pinned by test_timestamps.py.
-- ============================================================================

-- ============================ app_settings ==================================
CREATE TRIGGER trg_app_settings_bu_guard_created
BEFORE UPDATE ON app_settings
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_app_settings_bu_touch_updated
BEFORE UPDATE ON app_settings
FOR EACH ROW
WHEN (OLD.key IS NOT NEW.key
      OR OLD.value IS NOT NEW.value)
BEGIN
    UPDATE app_settings
       SET updated_at_utc = strftime('%Y-%m-%dT%H:%M:%fZ','now')
     WHERE key = NEW.key;
END;

-- ============================ business_days =================================
CREATE TRIGGER trg_business_days_bu_guard_created
BEFORE UPDATE ON business_days
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_business_days_bu_touch_updated
BEFORE UPDATE ON business_days
FOR EACH ROW
WHEN (OLD.status IS NOT NEW.status
      OR OLD.opening_cash_paisa IS NOT NEW.opening_cash_paisa
      OR OLD.prev_closing_date IS NOT NEW.prev_closing_date
      OR OLD.opened_at_utc IS NOT NEW.opened_at_utc
      OR OLD.closed_at_utc IS NOT NEW.closed_at_utc
      OR OLD.closing_revision IS NOT NEW.closing_revision
      OR OLD.physical_cash_paisa IS NOT NEW.physical_cash_paisa)
BEGIN
    UPDATE business_days
       SET updated_at_utc = strftime('%Y-%m-%dT%H:%M:%fZ','now')
     WHERE business_date = NEW.business_date;
END;

-- ============================ fuel_products =================================
CREATE TRIGGER trg_fuel_products_bu_guard_created
BEFORE UPDATE ON fuel_products
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_fuel_products_bu_touch_updated
BEFORE UPDATE ON fuel_products
FOR EACH ROW
WHEN (OLD.code IS NOT NEW.code
      OR OLD.name_en IS NOT NEW.name_en
      OR OLD.name_ur IS NOT NEW.name_ur
      OR OLD.is_active IS NOT NEW.is_active)
BEGIN
    UPDATE fuel_products
       SET updated_at_utc = strftime('%Y-%m-%dT%H:%M:%fZ','now')
     WHERE id = NEW.id;
END;

-- ================================ nozzles ===================================
CREATE TRIGGER trg_nozzles_bu_guard_created
BEFORE UPDATE ON nozzles
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_nozzles_bu_touch_updated
BEFORE UPDATE ON nozzles
FOR EACH ROW
WHEN (OLD.code IS NOT NEW.code
      OR OLD.fuel_product_id IS NOT NEW.fuel_product_id
      OR OLD.tank_id IS NOT NEW.tank_id
      OR OLD.label_en IS NOT NEW.label_en
      OR OLD.label_ur IS NOT NEW.label_ur
      OR OLD.is_active IS NOT NEW.is_active
      OR OLD.sort_order IS NOT NEW.sort_order)
BEGIN
    UPDATE nozzles
       SET updated_at_utc = strftime('%Y-%m-%dT%H:%M:%fZ','now')
     WHERE id = NEW.id;
END;

-- ============================ nozzle_readings ===============================
-- NOTE: Group C (migration 004) forbids all UPDATEs on nozzle_readings, so in
-- Phase 1 these triggers never successfully fire; they are installed anyway
-- because §4-A lists nozzle_readings as a Group-A table.
CREATE TRIGGER trg_nozzle_readings_bu_guard_created
BEFORE UPDATE ON nozzle_readings
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_nozzle_readings_bu_touch_updated
BEFORE UPDATE ON nozzle_readings
FOR EACH ROW
WHEN (OLD.nozzle_id IS NOT NEW.nozzle_id
      OR OLD.business_date IS NOT NEW.business_date
      OR OLD.reading_kind IS NOT NEW.reading_kind
      OR OLD.reading_ml IS NOT NEW.reading_ml
      OR OLD.occurred_at_utc IS NOT NEW.occurred_at_utc
      OR OLD.created_by IS NOT NEW.created_by)
BEGIN
    UPDATE nozzle_readings
       SET updated_at_utc = strftime('%Y-%m-%dT%H:%M:%fZ','now')
     WHERE id = NEW.id;
END;
