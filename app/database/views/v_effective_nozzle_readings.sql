-- v_effective_nozzle_readings — canonical view definition.
-- Installed by migration 006_phase2_readings.sql (the migration file is the
-- installed copy; this file is the versioned source for reference).
--
-- The effective closing reading for a nozzle+date is the closing-kind row
-- that no correction row supersedes. Corrections are linked through
-- nozzle_readings.supersedes_reading_id (migration 006); the original row
-- is never edited or deleted.

CREATE VIEW v_effective_nozzle_readings AS
SELECT r.*
  FROM nozzle_readings r
 WHERE r.reading_kind = 'closing'
   AND NOT EXISTS (SELECT 1
                     FROM nozzle_readings s
                    WHERE s.supersedes_reading_id = r.id);
