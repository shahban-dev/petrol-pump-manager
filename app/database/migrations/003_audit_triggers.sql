-- ============================================================================
-- Migration 003 — Trigger group B: audit history
-- Design plan §4, group B.
--
-- One trigger set per audited Phase-1 business table:
--   trg_<table>_ai_audit  AFTER INSERT
--   trg_<table>_au_audit  AFTER UPDATE
--   trg_<table>_bd_audit  BEFORE DELETE
-- Each inserts a row into audit_log with json_object() snapshots of the
-- table's columns (old on UPDATE/DELETE, new on INSERT/UPDATE).
--   * business_date = the row's business_date where the table has one
--   * actor = NEW.created_by / OLD.created_by where the column exists,
--             otherwise 'system'
-- Recursion guard: audit_log has NO audit triggers of its own; audit triggers
-- only INSERT into audit_log and never UPDATE the source row. Additionally,
-- trg_audit_log_bu/bd_no_modify abort any UPDATE/DELETE on audit_log itself.
-- ============================================================================

-- ============================ app_settings ==================================
CREATE TRIGGER trg_app_settings_ai_audit
AFTER INSERT ON app_settings
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('app_settings', 0, 'INSERT', NULL,
            json_object('key', NEW.key,
                        'value', NEW.value,
                        'created_at_utc', NEW.created_at_utc,
                        'updated_at_utc', NEW.updated_at_utc),
            NULL, 'system');
END;

CREATE TRIGGER trg_app_settings_au_audit
AFTER UPDATE ON app_settings
FOR EACH ROW
-- Skip the timestamp-touch inner update (group A): it changes only
-- updated_at_utc, which is maintenance, not a business change. The same
-- business-column list as trg_app_settings_bu_touch_updated.
WHEN (OLD.key IS NOT NEW.key
      OR OLD.value IS NOT NEW.value)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('app_settings', 0, 'UPDATE',
            json_object('key', OLD.key,
                        'value', OLD.value,
                        'created_at_utc', OLD.created_at_utc,
                        'updated_at_utc', OLD.updated_at_utc),
            json_object('key', NEW.key,
                        'value', NEW.value,
                        'created_at_utc', NEW.created_at_utc,
                        'updated_at_utc', NEW.updated_at_utc),
            NULL, 'system');
END;

CREATE TRIGGER trg_app_settings_bd_audit
BEFORE DELETE ON app_settings
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('app_settings', 0, 'DELETE',
            json_object('key', OLD.key,
                        'value', OLD.value,
                        'created_at_utc', OLD.created_at_utc,
                        'updated_at_utc', OLD.updated_at_utc),
            NULL, NULL, 'system');
END;

-- ============================ business_days =================================
CREATE TRIGGER trg_business_days_ai_audit
AFTER INSERT ON business_days
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('business_days', 0, 'INSERT', NULL,
            json_object('business_date', NEW.business_date,
                        'status', NEW.status,
                        'opening_cash_paisa', NEW.opening_cash_paisa,
                        'prev_closing_date', NEW.prev_closing_date,
                        'opened_at_utc', NEW.opened_at_utc,
                        'closed_at_utc', NEW.closed_at_utc,
                        'closing_revision', NEW.closing_revision,
                        'physical_cash_paisa', NEW.physical_cash_paisa,
                        'created_at_utc', NEW.created_at_utc,
                        'updated_at_utc', NEW.updated_at_utc),
            NEW.business_date, 'system');
END;

CREATE TRIGGER trg_business_days_au_audit
AFTER UPDATE ON business_days
FOR EACH ROW
-- Skip the timestamp-touch inner update (group A); see app_settings note.
WHEN (OLD.status IS NOT NEW.status
      OR OLD.opening_cash_paisa IS NOT NEW.opening_cash_paisa
      OR OLD.prev_closing_date IS NOT NEW.prev_closing_date
      OR OLD.opened_at_utc IS NOT NEW.opened_at_utc
      OR OLD.closed_at_utc IS NOT NEW.closed_at_utc
      OR OLD.closing_revision IS NOT NEW.closing_revision
      OR OLD.physical_cash_paisa IS NOT NEW.physical_cash_paisa)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('business_days', 0, 'UPDATE',
            json_object('business_date', OLD.business_date,
                        'status', OLD.status,
                        'opening_cash_paisa', OLD.opening_cash_paisa,
                        'prev_closing_date', OLD.prev_closing_date,
                        'opened_at_utc', OLD.opened_at_utc,
                        'closed_at_utc', OLD.closed_at_utc,
                        'closing_revision', OLD.closing_revision,
                        'physical_cash_paisa', OLD.physical_cash_paisa,
                        'created_at_utc', OLD.created_at_utc,
                        'updated_at_utc', OLD.updated_at_utc),
            json_object('business_date', NEW.business_date,
                        'status', NEW.status,
                        'opening_cash_paisa', NEW.opening_cash_paisa,
                        'prev_closing_date', NEW.prev_closing_date,
                        'opened_at_utc', NEW.opened_at_utc,
                        'closed_at_utc', NEW.closed_at_utc,
                        'closing_revision', NEW.closing_revision,
                        'physical_cash_paisa', NEW.physical_cash_paisa,
                        'created_at_utc', NEW.created_at_utc,
                        'updated_at_utc', NEW.updated_at_utc),
            NEW.business_date, 'system');
END;

CREATE TRIGGER trg_business_days_bd_audit
BEFORE DELETE ON business_days
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('business_days', 0, 'DELETE',
            json_object('business_date', OLD.business_date,
                        'status', OLD.status,
                        'opening_cash_paisa', OLD.opening_cash_paisa,
                        'prev_closing_date', OLD.prev_closing_date,
                        'opened_at_utc', OLD.opened_at_utc,
                        'closed_at_utc', OLD.closed_at_utc,
                        'closing_revision', OLD.closing_revision,
                        'physical_cash_paisa', OLD.physical_cash_paisa,
                        'created_at_utc', OLD.created_at_utc,
                        'updated_at_utc', OLD.updated_at_utc),
            NULL, OLD.business_date, 'system');
END;

-- ========================= business_day_events ===============================
CREATE TRIGGER trg_business_day_events_ai_audit
AFTER INSERT ON business_day_events
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('business_day_events', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'business_date', NEW.business_date,
                        'event_type', NEW.event_type,
                        'occurred_at_utc', NEW.occurred_at_utc,
                        'actor', NEW.actor,
                        'reason', NEW.reason,
                        'payload_json', NEW.payload_json),
            NEW.business_date, 'system');
END;

CREATE TRIGGER trg_business_day_events_au_audit
AFTER UPDATE ON business_day_events
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('business_day_events', NEW.id, 'UPDATE',
            json_object('id', OLD.id,
                        'business_date', OLD.business_date,
                        'event_type', OLD.event_type,
                        'occurred_at_utc', OLD.occurred_at_utc,
                        'actor', OLD.actor,
                        'reason', OLD.reason,
                        'payload_json', OLD.payload_json),
            json_object('id', NEW.id,
                        'business_date', NEW.business_date,
                        'event_type', NEW.event_type,
                        'occurred_at_utc', NEW.occurred_at_utc,
                        'actor', NEW.actor,
                        'reason', NEW.reason,
                        'payload_json', NEW.payload_json),
            NEW.business_date, 'system');
END;

CREATE TRIGGER trg_business_day_events_bd_audit
BEFORE DELETE ON business_day_events
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('business_day_events', OLD.id, 'DELETE',
            json_object('id', OLD.id,
                        'business_date', OLD.business_date,
                        'event_type', OLD.event_type,
                        'occurred_at_utc', OLD.occurred_at_utc,
                        'actor', OLD.actor,
                        'reason', OLD.reason,
                        'payload_json', OLD.payload_json),
            NULL, OLD.business_date, 'system');
END;

-- ============================ fuel_products =================================
CREATE TRIGGER trg_fuel_products_ai_audit
AFTER INSERT ON fuel_products
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('fuel_products', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'code', NEW.code,
                        'name_en', NEW.name_en,
                        'name_ur', NEW.name_ur,
                        'is_active', NEW.is_active,
                        'created_at_utc', NEW.created_at_utc,
                        'updated_at_utc', NEW.updated_at_utc),
            NULL, 'system');
END;

CREATE TRIGGER trg_fuel_products_au_audit
AFTER UPDATE ON fuel_products
FOR EACH ROW
-- Skip the timestamp-touch inner update (group A); see app_settings note.
WHEN (OLD.code IS NOT NEW.code
      OR OLD.name_en IS NOT NEW.name_en
      OR OLD.name_ur IS NOT NEW.name_ur
      OR OLD.is_active IS NOT NEW.is_active)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('fuel_products', NEW.id, 'UPDATE',
            json_object('id', OLD.id,
                        'code', OLD.code,
                        'name_en', OLD.name_en,
                        'name_ur', OLD.name_ur,
                        'is_active', OLD.is_active,
                        'created_at_utc', OLD.created_at_utc,
                        'updated_at_utc', OLD.updated_at_utc),
            json_object('id', NEW.id,
                        'code', NEW.code,
                        'name_en', NEW.name_en,
                        'name_ur', NEW.name_ur,
                        'is_active', NEW.is_active,
                        'created_at_utc', NEW.created_at_utc,
                        'updated_at_utc', NEW.updated_at_utc),
            NULL, 'system');
END;

CREATE TRIGGER trg_fuel_products_bd_audit
BEFORE DELETE ON fuel_products
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('fuel_products', OLD.id, 'DELETE',
            json_object('id', OLD.id,
                        'code', OLD.code,
                        'name_en', OLD.name_en,
                        'name_ur', OLD.name_ur,
                        'is_active', OLD.is_active,
                        'created_at_utc', OLD.created_at_utc,
                        'updated_at_utc', OLD.updated_at_utc),
            NULL, NULL, 'system');
END;

-- ================================ nozzles ===================================
CREATE TRIGGER trg_nozzles_ai_audit
AFTER INSERT ON nozzles
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('nozzles', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'code', NEW.code,
                        'fuel_product_id', NEW.fuel_product_id,
                        'tank_id', NEW.tank_id,
                        'label_en', NEW.label_en,
                        'label_ur', NEW.label_ur,
                        'is_active', NEW.is_active,
                        'sort_order', NEW.sort_order,
                        'created_at_utc', NEW.created_at_utc,
                        'updated_at_utc', NEW.updated_at_utc),
            NULL, 'system');
END;

CREATE TRIGGER trg_nozzles_au_audit
AFTER UPDATE ON nozzles
FOR EACH ROW
-- Skip the timestamp-touch inner update (group A); see app_settings note.
WHEN (OLD.code IS NOT NEW.code
      OR OLD.fuel_product_id IS NOT NEW.fuel_product_id
      OR OLD.tank_id IS NOT NEW.tank_id
      OR OLD.label_en IS NOT NEW.label_en
      OR OLD.label_ur IS NOT NEW.label_ur
      OR OLD.is_active IS NOT NEW.is_active
      OR OLD.sort_order IS NOT NEW.sort_order)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('nozzles', NEW.id, 'UPDATE',
            json_object('id', OLD.id,
                        'code', OLD.code,
                        'fuel_product_id', OLD.fuel_product_id,
                        'tank_id', OLD.tank_id,
                        'label_en', OLD.label_en,
                        'label_ur', OLD.label_ur,
                        'is_active', OLD.is_active,
                        'sort_order', OLD.sort_order,
                        'created_at_utc', OLD.created_at_utc,
                        'updated_at_utc', OLD.updated_at_utc),
            json_object('id', NEW.id,
                        'code', NEW.code,
                        'fuel_product_id', NEW.fuel_product_id,
                        'tank_id', NEW.tank_id,
                        'label_en', NEW.label_en,
                        'label_ur', NEW.label_ur,
                        'is_active', NEW.is_active,
                        'sort_order', NEW.sort_order,
                        'created_at_utc', NEW.created_at_utc,
                        'updated_at_utc', NEW.updated_at_utc),
            NULL, 'system');
END;

CREATE TRIGGER trg_nozzles_bd_audit
BEFORE DELETE ON nozzles
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('nozzles', OLD.id, 'DELETE',
            json_object('id', OLD.id,
                        'code', OLD.code,
                        'fuel_product_id', OLD.fuel_product_id,
                        'tank_id', OLD.tank_id,
                        'label_en', OLD.label_en,
                        'label_ur', OLD.label_ur,
                        'is_active', OLD.is_active,
                        'sort_order', OLD.sort_order,
                        'created_at_utc', OLD.created_at_utc,
                        'updated_at_utc', OLD.updated_at_utc),
            NULL, NULL, 'system');
END;

-- ============================ nozzle_readings ===============================
CREATE TRIGGER trg_nozzle_readings_ai_audit
AFTER INSERT ON nozzle_readings
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('nozzle_readings', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'nozzle_id', NEW.nozzle_id,
                        'business_date', NEW.business_date,
                        'reading_kind', NEW.reading_kind,
                        'reading_ml', NEW.reading_ml,
                        'occurred_at_utc', NEW.occurred_at_utc,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc,
                        'updated_at_utc', NEW.updated_at_utc),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_nozzle_readings_au_audit
AFTER UPDATE ON nozzle_readings
FOR EACH ROW
-- Skip the timestamp-touch inner update (group A); see app_settings note.
WHEN (OLD.nozzle_id IS NOT NEW.nozzle_id
      OR OLD.business_date IS NOT NEW.business_date
      OR OLD.reading_kind IS NOT NEW.reading_kind
      OR OLD.reading_ml IS NOT NEW.reading_ml
      OR OLD.occurred_at_utc IS NOT NEW.occurred_at_utc
      OR OLD.created_by IS NOT NEW.created_by)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('nozzle_readings', NEW.id, 'UPDATE',
            json_object('id', OLD.id,
                        'nozzle_id', OLD.nozzle_id,
                        'business_date', OLD.business_date,
                        'reading_kind', OLD.reading_kind,
                        'reading_ml', OLD.reading_ml,
                        'occurred_at_utc', OLD.occurred_at_utc,
                        'created_by', OLD.created_by,
                        'created_at_utc', OLD.created_at_utc,
                        'updated_at_utc', OLD.updated_at_utc),
            json_object('id', NEW.id,
                        'nozzle_id', NEW.nozzle_id,
                        'business_date', NEW.business_date,
                        'reading_kind', NEW.reading_kind,
                        'reading_ml', NEW.reading_ml,
                        'occurred_at_utc', NEW.occurred_at_utc,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc,
                        'updated_at_utc', NEW.updated_at_utc),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_nozzle_readings_bd_audit
BEFORE DELETE ON nozzle_readings
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('nozzle_readings', OLD.id, 'DELETE',
            json_object('id', OLD.id,
                        'nozzle_id', OLD.nozzle_id,
                        'business_date', OLD.business_date,
                        'reading_kind', OLD.reading_kind,
                        'reading_ml', OLD.reading_ml,
                        'occurred_at_utc', OLD.occurred_at_utc,
                        'created_by', OLD.created_by,
                        'created_at_utc', OLD.created_at_utc,
                        'updated_at_utc', OLD.updated_at_utc),
            NULL, OLD.business_date, OLD.created_by);
END;

-- ============================== meter_events ================================
CREATE TRIGGER trg_meter_events_ai_audit
AFTER INSERT ON meter_events
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('meter_events', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'nozzle_id', NEW.nozzle_id,
                        'event_type', NEW.event_type,
                        'reading_before_ml', NEW.reading_before_ml,
                        'reading_after_ml', NEW.reading_after_ml,
                        'occurred_at_utc', NEW.occurred_at_utc,
                        'reason', NEW.reason,
                        'authorized_by', NEW.authorized_by),
            NULL, 'system');
END;

CREATE TRIGGER trg_meter_events_au_audit
AFTER UPDATE ON meter_events
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('meter_events', NEW.id, 'UPDATE',
            json_object('id', OLD.id,
                        'nozzle_id', OLD.nozzle_id,
                        'event_type', OLD.event_type,
                        'reading_before_ml', OLD.reading_before_ml,
                        'reading_after_ml', OLD.reading_after_ml,
                        'occurred_at_utc', OLD.occurred_at_utc,
                        'reason', OLD.reason,
                        'authorized_by', OLD.authorized_by),
            json_object('id', NEW.id,
                        'nozzle_id', NEW.nozzle_id,
                        'event_type', NEW.event_type,
                        'reading_before_ml', NEW.reading_before_ml,
                        'reading_after_ml', NEW.reading_after_ml,
                        'occurred_at_utc', NEW.occurred_at_utc,
                        'reason', NEW.reason,
                        'authorized_by', NEW.authorized_by),
            NULL, 'system');
END;

CREATE TRIGGER trg_meter_events_bd_audit
BEFORE DELETE ON meter_events
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('meter_events', OLD.id, 'DELETE',
            json_object('id', OLD.id,
                        'nozzle_id', OLD.nozzle_id,
                        'event_type', OLD.event_type,
                        'reading_before_ml', OLD.reading_before_ml,
                        'reading_after_ml', OLD.reading_after_ml,
                        'occurred_at_utc', OLD.occurred_at_utc,
                        'reason', OLD.reason,
                        'authorized_by', OLD.authorized_by),
            NULL, NULL, 'system');
END;

-- ====================== audit_log self-protection ============================
-- SQLite has no multi-event trigger syntax, so the single logical guard
-- "no UPDATE/DELETE on audit_log" is implemented as two triggers.
CREATE TRIGGER trg_audit_log_bu_no_modify
BEFORE UPDATE ON audit_log
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only; rows cannot be updated');
END;

CREATE TRIGGER trg_audit_log_bd_no_modify
BEFORE DELETE ON audit_log
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only; rows cannot be deleted');
END;
