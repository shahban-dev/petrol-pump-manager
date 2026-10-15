-- ============================================================================
-- Migration 006 — Phase 2: reading corrections + effective readings
-- Design plan: documentation/00_design_plan.md §4-E3, §11.12/13, §5.
--
-- Readings are immutable (Group C), so a wrong reading is NEVER edited or
-- deleted. The correction workflow is:
--   1. a meter_events row (event_type='correction') with before/after
--      values, a reason and an authorizer, then
--   2. a replacement nozzle_readings row whose supersedes_reading_id points
--      at the original row.
-- Both rows are audited; the original stays in history.
--
-- The replacement reuses reading_kind='closing' (no CHECK change needed).
-- The "effective" reading for a nozzle+date is the one no other row
-- supersedes; v_effective_nozzle_readings exposes exactly that.
-- ============================================================================

-- --- Correction linkage -------------------------------------------------------
ALTER TABLE nozzle_readings
    ADD COLUMN supersedes_reading_id INTEGER REFERENCES nozzle_readings (id);
CREATE INDEX idx_nozzle_readings_supersedes
    ON nozzle_readings (supersedes_reading_id);

-- --- E1: one *effective* closing reading per nozzle per date -------------------
-- The original partial unique index is dropped and recreated so that a
-- correction row (which carries supersedes_reading_id) does not collide
-- with the original row it replaces.
DROP INDEX uq_nozzle_readings_closing;
CREATE UNIQUE INDEX uq_nozzle_readings_closing
    ON nozzle_readings (nozzle_id, business_date)
    WHERE reading_kind = 'closing' AND supersedes_reading_id IS NULL;

-- --- Effective readings view ----------------------------------------------------
-- Canonical definition also kept at app/database/views/
-- v_effective_nozzle_readings.sql (this migration installs it).
CREATE VIEW v_effective_nozzle_readings AS
SELECT r.*
  FROM nozzle_readings r
 WHERE r.reading_kind = 'closing'
   AND NOT EXISTS (SELECT 1
                     FROM nozzle_readings s
                    WHERE s.supersedes_reading_id = r.id);

-- --- E2: rewrite previous-reading lookup to use effective readings ---------------
-- A correction row must not leave the *next* day's validation comparing
-- against the superseded (wrong) value. The meter-event authorization
-- clause is unchanged (any meter_events row for the nozzle still authorizes
-- a lower reading; tightening that to per-interval coverage is deferred).
DROP TRIGGER trg_nozzle_readings_bi_validate;
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

-- --- Non-operating days accept no readings ---------------------------------------
-- Design plan §11.6: a non_operating day carries cash/stock/readings forward
-- unchanged; no readings or sales may be posted to it.
CREATE TRIGGER trg_nozzle_readings_bi_non_operating
BEFORE INSERT ON nozzle_readings
FOR EACH ROW
WHEN (SELECT status FROM business_days
       WHERE business_date = NEW.business_date) = 'non_operating'
BEGIN
    SELECT RAISE(ABORT, 'business day is non-operating; readings cannot be posted');
END;
