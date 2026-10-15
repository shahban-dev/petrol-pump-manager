-- ============================================================================
-- Migration 004 — Integrity triggers: groups C, D, H, E2 (Phase-1 scope)
-- Design plan §4. All rejections use RAISE(ABORT, ...) so the enclosing
-- transaction rolls back. These triggers live in the database itself, so they
-- reject bad writes from ANY connection, including raw sqlite3 SQL.
-- ============================================================================

-- ================= Group C — posted-ledger immutability ======================
-- nozzle_readings: posted meter facts — no UPDATE, no DELETE. Corrections go
-- through new meter_events rows + replacement readings (E3), never edits.
CREATE TRIGGER trg_nozzle_readings_bu_no_update
BEFORE UPDATE ON nozzle_readings
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'nozzle_readings is immutable; post a correction via meter_events instead');
END;

CREATE TRIGGER trg_nozzle_readings_bd_no_delete
BEFORE DELETE ON nozzle_readings
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'nozzle_readings is immutable; rows cannot be deleted');
END;

-- business_day_events: the day lifecycle log is append-only.
CREATE TRIGGER trg_business_day_events_bu_no_update
BEFORE UPDATE ON business_day_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'business_day_events is append-only; rows cannot be updated');
END;

CREATE TRIGGER trg_business_day_events_bd_no_delete
BEFORE DELETE ON business_day_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'business_day_events is append-only; rows cannot be deleted');
END;

-- business_days: days are never deleted (history must survive).
CREATE TRIGGER trg_business_days_bd_no_delete
BEFORE DELETE ON business_days
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'business_days rows cannot be deleted');
END;

-- ================= Group D — closed business day =============================
-- D1: no new readings may be posted to a closed day. Status 'reopened'
-- (transient during an authorized correction) permits writes; the reopen
-- workflow itself is service-level and audited via business_day_events.
CREATE TRIGGER trg_nozzle_readings_bi_closed_day
BEFORE INSERT ON nozzle_readings
FOR EACH ROW
WHEN (SELECT status FROM business_days
       WHERE business_date = NEW.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;
-- NOTE: D2/D3 (UPDATE/DELETE guards on business_date) are not needed in
-- Phase 1: UPDATE/DELETE on nozzle_readings and business_day_events are
-- already blocked unconditionally by Group C above.

-- ================= Group H — business dates and uniqueness ===================
-- H2: at most one business day may be open (or transiently reopened) at a time.
CREATE TRIGGER trg_business_days_bi_one_open
BEFORE INSERT ON business_days
FOR EACH ROW
WHEN NEW.status IN ('open', 'reopened')
 AND EXISTS (SELECT 1 FROM business_days WHERE status IN ('open', 'reopened'))
BEGIN
    SELECT RAISE(ABORT, 'only one business day may be open at a time');
END;

CREATE TRIGGER trg_business_days_bu_one_open
BEFORE UPDATE ON business_days
FOR EACH ROW
WHEN NEW.status IN ('open', 'reopened')
 AND EXISTS (SELECT 1 FROM business_days
              WHERE status IN ('open', 'reopened')
                AND business_date <> OLD.business_date)
BEGIN
    SELECT RAISE(ABORT, 'only one business day may be open at a time');
END;

-- I3/status flow (§4-I3): business_days.status transitions are restricted to
-- open → closed → reopened → closed, plus open → non_operating.
CREATE TRIGGER trg_business_days_bu_status_flow
BEFORE UPDATE ON business_days
FOR EACH ROW
WHEN OLD.status IS NOT NEW.status
 AND NOT ((OLD.status = 'open'      AND NEW.status = 'closed')
       OR (OLD.status = 'closed'    AND NEW.status = 'reopened')
       OR (OLD.status = 'reopened'  AND NEW.status = 'closed')
       OR (OLD.status = 'open'      AND NEW.status = 'non_operating'))
BEGIN
    SELECT RAISE(ABORT, 'illegal business day status transition');
END;

-- ================= Group E2 — nozzle reading validation =====================
-- BEFORE INSERT validation for nozzle_readings. The closed-day rule is
-- enforced by the D1 trigger above (created earlier in this file, so it fires
-- first); this trigger covers the remaining checks:
--   1. nozzle must exist and be active
--   2. business date must exist
--   3. reading_ml >= 0 (also enforced by the CHECK constraint)
--   4. a reading lower than the nozzle's previous closing reading is rejected
--      unless a meter_events row exists for the nozzle (any type: reset /
--      rollover / replacement / correction). A meter reset is NEVER
--      interpreted as a negative sale.
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

    SELECT RAISE(ABORT, 'reading is lower than the previous closing reading; record a meter event (reset/rollover/replacement/correction) first')
     WHERE NEW.reading_ml < (SELECT reading_ml
                               FROM nozzle_readings
                              WHERE nozzle_id = NEW.nozzle_id
                                AND reading_kind = 'closing'
                                AND business_date <= NEW.business_date
                              ORDER BY business_date DESC
                              LIMIT 1)
       AND NOT EXISTS (SELECT 1 FROM meter_events
                        WHERE nozzle_id = NEW.nozzle_id);
END;
