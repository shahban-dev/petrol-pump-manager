# Recovery and disaster procedures

Practical guide for the station manager (and support). It describes what the
code **actually does** — module and function names included — and where a step
is a design-plan procedure rather than an implemented module, it says so.

Related: design plan `00_design_plan.md` §1 (startup), §4 (trigger guards),
§9 (backup/restore design), §10 (fail-safe startup); acceptance tests 14–15.

---

## 1. Unexpected shutdown (power cut, crash, killed process)

**What happens on the next start.** The database runs in WAL mode
(`app/database/connection.py::DatabaseManager.connect` sets and *verifies*
`journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`,
`synchronous=FULL`, `recursive_triggers=OFF` on every connection; it refuses
to open if any pragma is not honoured). On open, SQLite automatically replays
the `-wal` file: every *committed* transaction survives, every uncommitted
one is rolled back. The app comes back at the last consistent state — there
is nothing to repair by hand.

**What `bootstrap()` does** (`app/bootstrap.py::bootstrap`, in order):
1. `config.ensure_dirs()` — creates data/backups/exports/logs/config dirs.
2. `DatabaseManager.connect()` — pragma-verified connection.
3. `migrator.migrate(conn)` — applies pending `NNN_*.sql` migrations.
4. `migrator.verify_schema(conn)` — required tables/views/triggers present.
5. `integrity_checks.check_integrity(conn)` (`PRAGMA integrity_check`) and
   `integrity_checks.check_foreign_keys(conn)` (`PRAGMA foreign_key_check`).
6. Clock-skew check (see §5) — warning only.
7. Returns `BootstrapResult`; on **any** failure it raises `AppStartupError`
   with a plain-language message. The database is **never deleted,
   overwritten, or "repaired"** here — the message footer says so:
   *"The database has NOT been deleted or modified. Please restore from a
   backup or contact support."*

**What NOT to do:**
- **Never delete the `-wal` / `-shm` files** to "fix" anything. They hold
  committed transactions not yet checkpointed into the main file — deleting
  them destroys data.
- **Never copy the live database file while the app is running.** A raw file
  copy of a WAL-mode database is not a consistent snapshot. Use the SQLite
  online backup API (`sqlite3.Connection.backup()`) to a temp file — that is
  the §9 backup pipeline's step 2, and the only supported way to snapshot a
  live database.

---

## 2. Damaged database (integrity / foreign-key failure)

**Failure path.** If `check_integrity()` returns anything but `"ok"`, or
`check_foreign_keys()` returns any violation rows, `bootstrap()` logs the
detail and raises `AppStartupError` — the app refuses to start rather than
run on a damaged database. It does not attempt repair and does not replace
the file.

**Quarantine and restore (manual, deliberate):**
1. Quit the app (make sure no process holds the database).
2. **Rename** the damaged file — e.g. `petrol_pump.sqlite3` →
   `petrol_pump.corrupt-<YYYYMMDD-HHMMSS>.sqlite3`. Never delete it, and
   never let the app auto-create an empty database in its place (it won't —
   `migrate()` on a fresh file creates schema, but quarantine means you
   restore instead; an empty DB is not a recovery).
3. Pick the latest **verified** backup from `config.BACKUP_DIR`
   (`%LOCALAPPDATA%\PetrolPumpManager\backups` on Windows;
   `~/.local/share/petrol-pump-manager/backups` on Linux).
   Prefer one whose `backup_history` row has `status='validated'`
   (the schema exists in migration `001`; `ReportingService` can list it).
4. Validate the candidate **before** it becomes the live DB: open a copy,
   run `integrity_checks.check_integrity()` and
   `integrity_checks.check_foreign_keys()`, and spot-check business data
   (row counts for `business_days`, `audit_log`; the latest closing's cash
   and liter figures against the printed register report).
5. Copy the validated backup into place as the live database file.
6. Start the app — `bootstrap()` re-runs the full integrity suite (§1).
   If it still fails, the backup is bad too: go one backup older.

---

## 3. Failed upgrade (schema migration failure)

**What `migrate()` guarantees** (`app/database/migrator.py::migrate`):
- Migrations are the numbered files in `app/database/migrations/` —
  currently up to version **15** (`CURRENT_SCHEMA_VERSION = 15`).
- Each migration runs inside its **own** `BEGIN IMMEDIATE … COMMIT`, and the
  version row in `schema_migrations` is written **in the same transaction**.
  A failed migration is rolled back completely and raises `MigrationError`
  naming the file — the version bump happens only after success, so the
  database is left at its prior version, never half-migrated.
- `bootstrap()` catches the failure and raises `AppStartupError`; the
  previous database is untouched.
- `migrate()` also refuses to open a database **newer** than the build
  (`"incompatible newer database version … refusing to open"`): you cannot
  downgrade the app past a database a newer build already migrated.

**Safety-backup-first rule (automatic).** Before `migrate()` applies
anything, `bootstrap()` calls `app/bootstrap.py::_maybe_safety_backup`,
which:
1. Checks whether the database file **already existed and was non-empty
   before** `bootstrap()` connected (the existence check is pre-connect,
   because opening the database initializes its file header) — on a fresh
   install it returns silently and nothing is backed up.
2. Compares the current schema version (read-only, via
   `migrator.get_schema_version`) with `CURRENT_SCHEMA_VERSION`. If
   nothing is pending, it returns — no backup is taken.
3. If migrations **are** pending, runs
   `BackupService(actor='system').run_backup('safety')` and logs the backup
   result (the safety backup filename). This respects the `backup.on_upgrade`
   app setting (default enabled; a missing key counts as enabled).
4. **Fail safe:** if the safety backup itself fails, startup raises
   `AppStartupError` *before* any migration runs — a database with real
   data is never migrated without the safety net. The message carries the
   standard footer: the database has NOT been modified.

**Manual fallback (if automatic backup is disabled or unavailable):**
1. Take a backup via the §9 pipeline (online backup API →
   `config.BACKUP_DIR`), named `PetrolPump_Backup_YYYY-MM-DD_HHMMSS.sqlite`
   (local PKT time in the *filename only*).
2. Install/upgrade. If startup fails with a migration error, **roll back**
   by restoring that safety backup using the §4 procedure (validate it,
   then put it in place as the live DB).

**Other automatic backups in the app lifecycle** (all log-only on failure
— a backup failure never breaks the operation it follows):
- **Day close:** after `BusinessDayService.close_day` commits its one
  transaction, `_auto_backup_after_day_close` runs
  `BackupService(actor=<closer>).run_backup('day_close')`, unless
  `backup.on_day_close` is `'0'`. A failure is logged as a warning and the
  close stands — transaction semantics are unchanged.
- **Shutdown:** after the Qt event loop exits in `app/main.py::main()`,
  `_maybe_shutdown_backup` runs
  `BackupService(actor='system').run_backup('shutdown')`, unless
  `backup.on_shutdown` is `'0'` (default). Headless, no dialogs; shutdown
  semantics (exit code, order) are unchanged.

---

## 4. Restore from backup — step by step

> Status: restore is implemented — `BackupService.restore_backup()`
> (`app/services/backup_service.py`) plus the **Backups** page in the UI.
> The steps below describe what the code does; the manual variant (using
> the named building blocks) remains valid if the app cannot start.

1. **Safety backup of the current DB first** — even if it looks broken, take
   an online-backup snapshot into `config.BACKUP_DIR` before touching
   anything. A restore never destroys the thing it replaces.
2. **Validate the candidate backup**: open a copy (never the original file),
   run `check_integrity()` → must return `"ok"`, and `check_foreign_keys()`
   → must return no rows. Then spot-check business truth: `business_days`
   dates, nozzle/tank/customer counts, the latest closing's cash and liters,
   and a tail of `audit_log` — against the printed register reports.
3. **Confirm explicitly** (the design requires typed confirmation in the UI;
   manually: say the filename and date out loud and check it twice). The
   candidate replaces the live DB only after validation passes.
4. **Replace, then re-verify**: put the validated file in place as the live
   DB and start the app — `bootstrap()` re-runs migrations, schema
   verification, and both integrity checks (§1).
5. **Never auto-delete the last valid backup.** Retention pruning removes
   *old validated* backups only, and always keeps the newest known-good one
   plus the pre-restore safety backup from step 1.

---

## 5. Clock-skew warning

**What triggers it.** At startup, `app/bootstrap.py` compares the system
clock against the newest machine timestamp recorded in the database — the
maximum of `business_days.created_at_utc`, `nozzle_readings.occurred_at_utc`,
and `audit_log.occurred_at_utc`. If the clock is more than **60 seconds
behind** that timestamp, startup adds a non-fatal warning
(`BootstrapResult.warnings`, message key `warning.clock_skew`):
*"System clock appears to be behind the newest database timestamp
({max_ts}). Data was left unchanged; please check the system clock."*

**What the manager should do.** Fix the machine clock (turn on automatic
date/time / NTP sync) and restart the app. The warning is **warn-only**:
no record is ever rewritten because of it (design §5.1/§11 — never rewrite
history). Do not "fix" timestamps by hand; a clock set backwards is an
environment problem, not a data problem. If the warning persists after the
clock is corrected, contact support rather than editing the database.

---

## 6. What the guards already prevent (so recovery is rarely needed)

For orientation — these are enforced by the database itself, not by
discipline:

- **Posted rows are immutable** (trigger group C, migration `004`): any
  `UPDATE`/`DELETE` on `nozzle_readings`, `business_day_closings`,
  `business_day_events`, deliveries, movements, tickets, payments, cash
  rows, settlements, and expenses is rejected from *any* connection.
  Corrections happen only as new audited rows (reversals, correction meter
  events, replacement readings).
- **Closed-day protection** (group D): no writes to a `closed` day's rows
  from any connection; corrections require the authorized
  `BusinessDayService.reopen_day` (manager reason, audited event) and the
  next close writes a **new** revision — old revisions are never
  overwritten.
- **The audit log is append-only** (`trg_audit_log_*_no_modify`): audit rows
  can never be updated or deleted, so the trail used in §§2–4 above is
  trustworthy.
