-- Migration 014: closed-day guards (group D2/D3) for tank_readings.
--
-- Gap found by Phase-7 attack testing (test_phase7_attack.py): tank_readings
-- had the D1 INSERT guard but no D2/D3 guards, so raw-SQL UPDATE/DELETE on a
-- CLOSED business day succeeded (audited, but not blocked). Design plan §4-D
-- requires D2/D3 on every business-date-scoped table.
--
-- Deliberate scope: on OPEN days tank_readings stay mutable + audited (a dip
-- reading typo corrected the same day is legitimate; the au_audit trigger
-- records old/new values, so it is never silent). Only closed-day writes are
-- blocked here; use the authorized reopen workflow for corrections.
-- Tables whose rows are fully immutable use C-group triggers instead; that
-- stricter rule was NOT applied to tank_readings.

CREATE TRIGGER trg_tank_readings_bu_closed_day
BEFORE UPDATE ON tank_readings
FOR EACH ROW
WHEN (SELECT status FROM business_days
       WHERE business_date = OLD.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;

CREATE TRIGGER trg_tank_readings_bd_closed_day
BEFORE DELETE ON tank_readings
FOR EACH ROW
WHEN (SELECT status FROM business_days
       WHERE business_date = OLD.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;
