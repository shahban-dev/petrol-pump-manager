-- ============================================================================
-- Migration 013 — Phase 7 gap-closing: timestamp/audit/immutability triggers
-- for backup_history and meter_events; tamper guard for schema_migrations.
--
-- Gaps closed (found by inspecting the live schema at version 12):
--   1. backup_history had ZERO triggers: no audit trail, no immutability,
--      and no created_at_utc/updated_at_utc (design plan §4 conventions /
--      §5 timestamp rules). It now gets both timestamp columns, Group A1/A2
--      triggers, Group B audit (ai/au/bd), and Group C2
--      trg_backup_history_bd_no_delete.
--   2. meter_events lacked created_at_utc/updated_at_utc. It now gets both
--      columns plus Group A1/A2 triggers.
--   3. schema_migrations had no tamper guard. It now gets
--      trg_schema_migrations_bu_no_modify / _bd_no_modify (BEFORE
--      UPDATE/DELETE abort unconditionally; INSERTs stay allowed because
--      the migrator records versions with INSERT only).
--
-- CONTRACT — backup_history updates stay allowed (deliberate design):
--   NO bu_no_update trigger is installed. Phase 8's backup pipeline
--   performs audited status UPDATEs on the single backup row
--   (requested → snapshot_created → validated → copied_to_drive →
--   sync_confirmed); those UPDATEs are audited (au trigger) and bump
--   updated_at_utc (A2 touch). DELETEs are forbidden (C2).
--
-- CONTRACT — meter_events updates stay allowed (deliberate design):
--   updates remain permitted and audited (e.g. wrap_value_ml may be
--   recorded after the event row is created; pinned by
--   app/tests/test_phase3_acceptance.py which UPDATEs it directly).
--
-- IMPLEMENTATION NOTE — table rebuild instead of ADD COLUMN:
--   SQLite forbids ALTER TABLE ADD COLUMN with a non-constant DEFAULT
--   ("Cannot add a column with non-constant default"), so the naive
--   ADD COLUMN ... DEFAULT (strftime(...)) fails. Both tables are rebuilt
--   with the official 12-step procedure to reach the intended end state:
--   NOT NULL columns with strftime('%Y-%m-%dT%H:%M:%fZ','now') defaults.
--   Column order is preserved (new columns appended last), so positional
--   SELECTs are unaffected. Existing rows are backfilled from the most
--   truthful domain timestamp available: meter_events.created_at_utc /
--   updated_at_utc from occurred_at_utc, backup_history.* from
--   requested_at_utc.
--   meter_events has one dependent trigger, trg_nozzle_readings_bi_validate
--   (006), which SQLite will not let a DROP TABLE outlive; it is dropped
--   first and recreated byte-identical afterwards (same DROP+recreate
--   pattern 006 itself used). backup_history has no dependents (no
--   triggers, indexes, views, or inbound foreign keys).
--
-- meter_events au_audit recreation: 003's trg_meter_events_au_audit had no
--   WHEN guard because meter_events then had no A2 touch trigger. The new
--   A2 touch performs an inner UPDATE that changes only updated_at_utc, so
--   the trigger is recreated here with the same business-column WHEN guard
--   as the touch trigger (design plan §4: a touch-only inner update writes
--   no audit row; one business UPDATE yields exactly one audit row). The
--   json snapshots are kept byte-identical to 003 (minimal change).
-- ============================================================================

-- ==================== backup_history: table rebuild =========================
CREATE TABLE backup_history_new (
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
    error            TEXT,
    created_at_utc   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at_utc   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

INSERT INTO backup_history_new
    (id, requested_at_utc, filename, destination, status,
     size_bytes, sha256, verified_at_utc, error,
     created_at_utc, updated_at_utc)
SELECT id, requested_at_utc, filename, destination, status,
       size_bytes, sha256, verified_at_utc, error,
       requested_at_utc, requested_at_utc
  FROM backup_history;

DROP TABLE backup_history;

ALTER TABLE backup_history_new RENAME TO backup_history;

-- ===================== meter_events: table rebuild =========================
-- The sole dependent trigger is dropped first and recreated byte-identical
-- below (SQLite refuses to DROP a table a live trigger references).
DROP TRIGGER trg_nozzle_readings_bi_validate;

CREATE TABLE meter_events_new (
    id                INTEGER PRIMARY KEY,
    nozzle_id         INTEGER NOT NULL REFERENCES nozzles (id),
    event_type        TEXT NOT NULL
                      CHECK (event_type IN ('reset', 'rollover', 'replacement', 'correction')),
    reading_before_ml INTEGER,
    reading_after_ml  INTEGER,
    occurred_at_utc   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    reason            TEXT NOT NULL,
    authorized_by     TEXT NOT NULL DEFAULT 'system',
    wrap_value_ml     INTEGER
                      CHECK (wrap_value_ml IS NULL OR wrap_value_ml > 0),
    created_at_utc    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at_utc    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

INSERT INTO meter_events_new
    (id, nozzle_id, event_type, reading_before_ml, reading_after_ml,
     occurred_at_utc, reason, authorized_by, wrap_value_ml,
     created_at_utc, updated_at_utc)
SELECT id, nozzle_id, event_type, reading_before_ml, reading_after_ml,
       occurred_at_utc, reason, authorized_by, wrap_value_ml,
       occurred_at_utc, occurred_at_utc
  FROM meter_events;

-- Dropping the table also drops its three 003 audit triggers and its index;
-- all are recreated below.
DROP TABLE meter_events;

ALTER TABLE meter_events_new RENAME TO meter_events;

CREATE INDEX idx_meter_events_nozzle_id ON meter_events (nozzle_id);

-- Byte-identical recreation of migration 006's
-- trg_nozzle_readings_bi_validate (dropped above for the rebuild).
CREATE TRIGGER trg_nozzle_readings_bi_validate
BEFORE INSERT ON nozzle_readings
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'nozzle is unknown or inactive')
     WHERE NOT EXISTS (SELECT 1 FROM nozzles
                        WHERE id = NEW.nozzle_id AND is_active = 1);

    SELECT RAISE(ABORT, 'business date does not exist')
     WHERE NOT EXISTS (SELECT 1 FROM business_days
                        WHERE business_date = NEW.business_date);

    SELECT RAISE(ABORT, 'reading_ml must be >= 0')
     WHERE NEW.reading_ml < 0;

    -- Previous *effective* reading: the latest closing-kind row on an
    -- earlier date that no correction has superseded.
    SELECT RAISE(ABORT, 'reading is lower than the previous closing reading; record a meter event (reset/rollover/replacement/correction) first')
     WHERE NEW.reading_ml < (SELECT r.reading_ml
                               FROM nozzle_readings r
                              WHERE r.nozzle_id = NEW.nozzle_id
                                AND r.reading_kind = 'closing'
                                AND r.business_date < NEW.business_date
                                AND NOT EXISTS (SELECT 1
                                                  FROM nozzle_readings s
                                                 WHERE s.supersedes_reading_id = r.id)
                              ORDER BY r.business_date DESC, r.id DESC
                              LIMIT 1)
       AND NOT EXISTS (SELECT 1 FROM meter_events
                        WHERE nozzle_id = NEW.nozzle_id);
END;

-- ==================== backup_history: Group A timestamps ====================
CREATE TRIGGER trg_backup_history_bu_guard_created
BEFORE UPDATE ON backup_history
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_backup_history_bu_touch_updated
BEFORE UPDATE ON backup_history
FOR EACH ROW
WHEN (OLD.requested_at_utc IS NOT NEW.requested_at_utc
      OR OLD.filename IS NOT NEW.filename
      OR OLD.destination IS NOT NEW.destination
      OR OLD.status IS NOT NEW.status
      OR OLD.size_bytes IS NOT NEW.size_bytes
      OR OLD.sha256 IS NOT NEW.sha256
      OR OLD.verified_at_utc IS NOT NEW.verified_at_utc
      OR OLD.error IS NOT NEW.error)
BEGIN
    UPDATE backup_history
       SET updated_at_utc = strftime('%Y-%m-%dT%H:%M:%fZ','now')
     WHERE id = NEW.id;
END;

-- ===================== meter_events: Group A timestamps =====================
CREATE TRIGGER trg_meter_events_bu_guard_created
BEFORE UPDATE ON meter_events
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_meter_events_bu_touch_updated
BEFORE UPDATE ON meter_events
FOR EACH ROW
WHEN (OLD.nozzle_id IS NOT NEW.nozzle_id
      OR OLD.event_type IS NOT NEW.event_type
      OR OLD.reading_before_ml IS NOT NEW.reading_before_ml
      OR OLD.reading_after_ml IS NOT NEW.reading_after_ml
      OR OLD.occurred_at_utc IS NOT NEW.occurred_at_utc
      OR OLD.reason IS NOT NEW.reason
      OR OLD.authorized_by IS NOT NEW.authorized_by
      OR OLD.wrap_value_ml IS NOT NEW.wrap_value_ml)
BEGIN
    UPDATE meter_events
       SET updated_at_utc = strftime('%Y-%m-%dT%H:%M:%fZ','now')
     WHERE id = NEW.id;
END;

-- ==================== backup_history: Group B audit =========================
CREATE TRIGGER trg_backup_history_ai_audit
AFTER INSERT ON backup_history
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('backup_history', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'requested_at_utc', NEW.requested_at_utc,
                        'filename', NEW.filename,
                        'destination', NEW.destination,
                        'status', NEW.status,
                        'size_bytes', NEW.size_bytes,
                        'sha256', NEW.sha256,
                        'verified_at_utc', NEW.verified_at_utc,
                        'error', NEW.error,
                        'created_at_utc', NEW.created_at_utc,
                        'updated_at_utc', NEW.updated_at_utc),
            NULL, 'system');
END;

CREATE TRIGGER trg_backup_history_au_audit
AFTER UPDATE ON backup_history
FOR EACH ROW
-- Skip the timestamp-touch inner update (group A): it changes only
-- updated_at_utc, which is maintenance, not a business change. Same
-- business-column list as trg_backup_history_bu_touch_updated.
WHEN (OLD.requested_at_utc IS NOT NEW.requested_at_utc
      OR OLD.filename IS NOT NEW.filename
      OR OLD.destination IS NOT NEW.destination
      OR OLD.status IS NOT NEW.status
      OR OLD.size_bytes IS NOT NEW.size_bytes
      OR OLD.sha256 IS NOT NEW.sha256
      OR OLD.verified_at_utc IS NOT NEW.verified_at_utc
      OR OLD.error IS NOT NEW.error)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('backup_history', NEW.id, 'UPDATE',
            json_object('id', OLD.id,
                        'requested_at_utc', OLD.requested_at_utc,
                        'filename', OLD.filename,
                        'destination', OLD.destination,
                        'status', OLD.status,
                        'size_bytes', OLD.size_bytes,
                        'sha256', OLD.sha256,
                        'verified_at_utc', OLD.verified_at_utc,
                        'error', OLD.error,
                        'created_at_utc', OLD.created_at_utc,
                        'updated_at_utc', OLD.updated_at_utc),
            json_object('id', NEW.id,
                        'requested_at_utc', NEW.requested_at_utc,
                        'filename', NEW.filename,
                        'destination', NEW.destination,
                        'status', NEW.status,
                        'size_bytes', NEW.size_bytes,
                        'sha256', NEW.sha256,
                        'verified_at_utc', NEW.verified_at_utc,
                        'error', NEW.error,
                        'created_at_utc', NEW.created_at_utc,
                        'updated_at_utc', NEW.updated_at_utc),
            NULL, 'system');
END;

CREATE TRIGGER trg_backup_history_bd_audit
BEFORE DELETE ON backup_history
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('backup_history', OLD.id, 'DELETE',
            json_object('id', OLD.id,
                        'requested_at_utc', OLD.requested_at_utc,
                        'filename', OLD.filename,
                        'destination', OLD.destination,
                        'status', OLD.status,
                        'size_bytes', OLD.size_bytes,
                        'sha256', OLD.sha256,
                        'verified_at_utc', OLD.verified_at_utc,
                        'error', OLD.error,
                        'created_at_utc', OLD.created_at_utc,
                        'updated_at_utc', OLD.updated_at_utc),
            NULL, NULL, 'system');
END;

-- ================= backup_history: Group C2 no-delete ========================
CREATE TRIGGER trg_backup_history_bd_no_delete
BEFORE DELETE ON backup_history
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'backup_history rows cannot be deleted');
END;

-- ============ meter_events: Group B audit (recreated from 003) ==============
-- Snapshots are byte-identical to migration 003; the au trigger additionally
-- carries the business-column WHEN guard (see header note).
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
-- Skip the timestamp-touch inner update (group A); same business-column
-- list as trg_meter_events_bu_touch_updated.
WHEN (OLD.nozzle_id IS NOT NEW.nozzle_id
      OR OLD.event_type IS NOT NEW.event_type
      OR OLD.reading_before_ml IS NOT NEW.reading_before_ml
      OR OLD.reading_after_ml IS NOT NEW.reading_after_ml
      OR OLD.occurred_at_utc IS NOT NEW.occurred_at_utc
      OR OLD.reason IS NOT NEW.reason
      OR OLD.authorized_by IS NOT NEW.authorized_by
      OR OLD.wrap_value_ml IS NOT NEW.wrap_value_ml)
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

-- ============== schema_migrations: tamper guard (append-only) ===============
-- SQLite has no multi-event trigger syntax, so the single logical guard
-- "no UPDATE/DELETE on schema_migrations" is implemented as two triggers
-- (same pattern as trg_audit_log_bu/bd_no_modify in 003). INSERTs remain
-- allowed: the migrator records applied versions with INSERT only.
CREATE TRIGGER trg_schema_migrations_bu_no_modify
BEFORE UPDATE ON schema_migrations
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'schema_migrations is append-only; rows cannot be updated');
END;

CREATE TRIGGER trg_schema_migrations_bd_no_modify
BEFORE DELETE ON schema_migrations
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'schema_migrations is append-only; rows cannot be deleted');
END;
