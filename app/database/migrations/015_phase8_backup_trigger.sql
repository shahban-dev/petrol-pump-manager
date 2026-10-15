-- Migration 015: record what triggered each backup_history row.
--
-- The §9 backup pipeline records the trigger for every row:
--   'manual' | 'day_close' | 'shutdown' | 'scheduled' | 'safety'
-- (safety = the automatic pre-restore backup of the current live DB).
--
-- The column is NULLABLE so pre-Phase-8 rows (and rows written by older
-- code paths) stay valid; the application always sets it on new rows.
--
-- This is a plain nullable column with no default, so a simple
-- ALTER TABLE ... ADD COLUMN is legal here (the 12-step rebuild from
-- migration 013 was only needed there because of non-constant defaults).

ALTER TABLE backup_history ADD COLUMN trigger TEXT;
