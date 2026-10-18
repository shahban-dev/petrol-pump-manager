-- ============================================================================
-- Migration 016 — Owner authentication (users table).
--
-- Adds the login layer the v1 single-user model lacked:
--   * users: owner account(s) with scrypt-hashed passwords (Python stdlib
--    hashlib.scrypt; the hash and salt are stored, never the password).
--   * Two security questions (answers hashed the same way) for the offline
--    password-reset flow — there is no email/SMS in this offline app.
--   * failed_attempts + locked_until_utc: 5 bad attempts lock the account
--    for 15 minutes (service-enforced).
--
-- DELIBERATE OMISSIONS (documented, not oversights):
--   * No audit_log triggers on this table: audit snapshots would capture
--    password/security-answer hashes. Auth events are recorded in the
--    users row itself (failed_attempts, locked_until_utc, last_login_at_utc).
--   * No DELETE of the last remaining user (trigger below) — the app must
--    never be left with no way to log in.
--   * Usernames are case-insensitive unique (COLLATE NOCASE).
-- ============================================================================

CREATE TABLE users (
    id                 INTEGER PRIMARY KEY,
    uuid               TEXT NOT NULL UNIQUE,
    username           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash      TEXT NOT NULL,   -- hex(scrypt(password, salt))
    password_salt      TEXT NOT NULL,   -- hex, 16 bytes
    role               TEXT NOT NULL DEFAULT 'owner'
                       CHECK (role IN ('owner')),
    security_q1        TEXT NOT NULL,
    security_a1_hash   TEXT NOT NULL,   -- hex(scrypt(normalized_answer, salt))
    security_a1_salt   TEXT NOT NULL,   -- hex, 16 bytes
    security_q2        TEXT NOT NULL,
    security_a2_hash   TEXT NOT NULL,
    security_a2_salt   TEXT NOT NULL,   -- hex, 16 bytes
    failed_attempts    INTEGER NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
    locked_until_utc   TEXT,            -- NULL = not locked
    last_login_at_utc  TEXT,
    created_by         TEXT NOT NULL DEFAULT 'system',
    created_at_utc     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at_utc     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (username <> ''),
    CHECK (security_q1 <> '' AND security_q2 <> ''),
    CHECK (security_q1 <> security_q2)
);

CREATE INDEX idx_users_username ON users (username);

-- Group A (timestamps), same pattern as migration 002 -----------------------
CREATE TRIGGER trg_users_bu_guard_created
BEFORE UPDATE ON users
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_users_bu_touch_updated
BEFORE UPDATE ON users
FOR EACH ROW
WHEN (OLD.username IS NOT NEW.username
      OR OLD.password_hash IS NOT NEW.password_hash
      OR OLD.password_salt IS NOT NEW.password_salt
      OR OLD.role IS NOT NEW.role
      OR OLD.security_q1 IS NOT NEW.security_q1
      OR OLD.security_a1_hash IS NOT NEW.security_a1_hash
      OR OLD.security_a1_salt IS NOT NEW.security_a1_salt
      OR OLD.security_q2 IS NOT NEW.security_q2
      OR OLD.security_a2_hash IS NOT NEW.security_a2_hash
      OR OLD.security_a2_salt IS NOT NEW.security_a2_salt
      OR OLD.failed_attempts IS NOT NEW.failed_attempts
      OR OLD.locked_until_utc IS NOT NEW.locked_until_utc
      OR OLD.last_login_at_utc IS NOT NEW.last_login_at_utc
      OR OLD.created_by IS NOT NEW.created_by)
BEGIN
    UPDATE users
       SET updated_at_utc = strftime('%Y-%m-%dT%H:%M:%fZ','now')
     WHERE id = OLD.id;
END;

-- The app must never be left without a login: deleting the last user aborts.
CREATE TRIGGER trg_users_bd_no_delete_last
BEFORE DELETE ON users
FOR EACH ROW
WHEN (SELECT COUNT(*) FROM users) <= 1
BEGIN
    SELECT RAISE(ABORT, 'cannot delete the last user account');
END;
