# Phase 8 notes — Backup & packaging

Phase 8 implements design plan §9 (backup/restore) and the installer. This
file records build decisions per agent-owned area.

---

## Backup service (backup-service builder, 2026-10-15)

Modules: `app/services/backup_service.py`, `app/repositories/backup_repository.py`,
`app/models/backup.py`, migration `015_phase8_backup_trigger.sql`
(`CURRENT_SCHEMA_VERSION = 15`). Error classes appended to
`app/services/errors.py`: `BackupError(BusinessRuleError)`,
`RestoreConfirmationRequired(BackupError)`, `BackupValidationError(BackupError)`.
`DatabaseManager.close_all()` added (closes the context-manager connection;
documented that all other connections are caller-owned and must already be
finished before a restore).

### Pipeline decisions

- **Snapshot method:** the SQLite online backup API (`src.backup(dst)`) to a
  temp file in `config.BACKUP_DIR` — never a raw file copy of the WAL-mode
  live DB (recovery.md §1). The temp file is closed cleanly, validated, then
  atomically renamed with `os.replace` to
  `PetrolPump_Backup_YYYY-MM-DD_HHMMSS.sqlite`; safety backups use the
  `PetrolPump_Safety_` prefix. Local PKT time (`Asia/Karachi`, with a fixed
  UTC+5 fallback if the tz database is missing) appears in the **filename
  only**; all stored timestamps are UTC (§5.1).
- **History on every path:** each step updates `backup_history`
  (`requested → snapshot_created → validated → copied_to_drive`); failure
  paths write `failed` with error text, and temp files are cleaned up in a
  `finally`. Status UPDATEs are deliberate and audited per the migration-013
  contract (no `bu_no_update` trigger on `backup_history`).
- **Validation (read-only open):** `PRAGMA integrity_check == 'ok'`,
  `foreign_key_check` empty, `business_days`/`audit_log` present, file size
  > 0, and row-count sanity over `business_days`, `business_day_closings`,
  `nozzle_readings`, `customers`, `cash_receipts`, `audit_log`.
- **Sizes:** integer bytes everywhere; `hashlib.sha256` chunked (no float,
  no full-file read).
- **Settings keys** (`app_settings`): `backup.drive_folder`,
  `backup.keep_local` (default 30), `backup.keep_drive` (default 14),
  `backup.on_day_close` ('1'), `backup.on_shutdown` ('0'),
  `backup.on_upgrade` ('1'), `backup.periodic_minutes` ('0' = disabled),
  `backup.last_scheduled_at_utc`. `scheduled_backup_due()` /
  `mark_scheduled_backup()` drive the periodic schedule.

### Honest cloud-status rules (§11.15)

- The Drive leg copies the validated file with `shutil.copy2` and verifies
  existence + size match before recording `copied_to_drive` + sha256.
- `sync_confirmed` is written **only** if `_drive_sync_confirmed()` returns
  True; it checks for a real OS signal (in-progress upload marker) and
  **defaults to False** — Drive for desktop exposes no reliable per-file
  sync signal and §9 forbids depending on the Google API. The normal honest
  state is `copied_to_drive`.
- `status_text_key(record)` returns i18n keys (e.g.
  `backup.status.copied_to_drive` = "In Google Drive folder — cloud sync
  unconfirmed"). The UI must never say "backed up to cloud".
- Drive configured but unreachable → `cloud_unavailable` gdrive row; the
  local backup stands and the app keeps working offline. A failed copy →
  `failed` gdrive row; local backup unaffected.
- `set_drive_folder()` refuses: non-existent / non-directory / unwritable
  folders, and any folder **inside the app data dir** (the live DB and
  local backups must never sit in a cloud-synced folder).

### Restore flow (recovery.md §4)

`restore_backup(backup_id, *, confirmed, actor)`:
1. Without `confirmed` → raises `RestoreConfirmationRequired` (the UI
   collects typed/explicit confirmation).
2. **Safety backup first** via `run_backup('safety')` — a restore never
   destroys what it replaces (history rows carry `trigger='safety'`).
3. Candidate validated (must pass, else `BackupValidationError`); the
   backup file itself is never moved — a staging copy is made.
4. `close_all()` on the manager, stale WAL sidecars (`-wal`/`-shm`/
   `-journal`) of the old DB removed (they would otherwise attach to the
   new file), atomic `os.replace` of the staged file into the live path.
5. Post-restore verification reuses `app/database/integrity_checks.py`
   (`integrity_check`, `foreign_key_check`, `check_required_objects`) plus
   schema-version compatibility and business checks (business-day dates
   present, latest closing exists, core-table counts sane). On failure →
   `BackupError` naming the safety backup id to restore.
6. Returns `RestoreReport` with `restart_required=True` — the app must
   restart so every layer re-opens the replaced database.

Retention (`prune_backups`): deletes files of old *validated* backups beyond
`keep_local`/`keep_drive`, oldest first; **never** deletes the newest
known-good backup per destination, **never** deletes a safety backup younger
than 7 days, touches only `PetrolPump_(Backup|Safety)_*` files, and keeps
all history rows (audit trail).

### Key API signatures (for UI / integration agents)

- `BackupService(db_manager=None, actor=None, *, backup_dir=None)` —
  defaults: live DB from `config.db_path()`, backups in `config.BACKUP_DIR`.
- `run_backup(trigger: str) -> BackupRecord` — trigger ∈
  `{'manual','day_close','shutdown','scheduled','safety'}`.
- `list_history(limit=100, destination=None) -> list[BackupRecord]`,
  `get_record(backup_id) -> BackupRecord | None`.
- `validate_backup_file(path) -> ValidationReport` (`ok`, `checks`, `error`).
- `restore_backup(backup_id, *, confirmed, actor=None) -> RestoreReport`.
- `get_drive_folder() -> Path | None`, `set_drive_folder(path) -> Path`
  (raises `ValidationError`), `drive_available() -> bool`.
- `get_retention() -> (keep_local, keep_drive)`,
  `set_retention(keep_local, keep_drive)`, `prune_backups() -> PruneReport`.
- `scheduled_backup_due() -> bool`, `mark_scheduled_backup()`.
- `status_text_key(record) -> str` — i18n key for honest status wording.

Acceptance tests 14/15: `app/tests/test_phase8_backup.py` covers
snapshot-under-WAL validity, validation failure rejection, the restore
round-trip (balances identical), and retention rules — all in temp DBs.

### Handoff: localization keys (owned by the localization/UI agent)

`test_phase6_localization.py::test_every_code_key_exists_in_both_languages`
scans `app/services` for `"err.*"` keys and requires them in
`app/localization/en.json` + `ur.json`. The backup service introduces 16 —
`app/localization/*` is owned by another agent, so these are listed here
for them to add (English text = `str(exc)`; `{params}` match the `params`
dict shown):

| key | params |
|---|---|
| `err.backup_unknown_trigger` | `trigger` |
| `err.backup_failed` | `detail` |
| `err.backup_snapshot_failed` | `detail` |
| `err.backup_validation_failed` | `detail` |
| `err.backup_dir_unwritable` | `folder`, `detail` |
| `err.backup_row_missing` | `backup_id` |
| `err.backup_not_found` | `backup_id` |
| `err.backup_file_missing` | `filename`, `destination` |
| `err.restore_confirmation_required` | `backup_id` |
| `err.restore_candidate_invalid` | `filename`, `detail` |
| `err.restore_replace_failed` | `detail`, `safety_backup_id` |
| `err.restore_verification_failed` | `detail`, `safety_backup_id` |
| `err.backup_drive_invalid` | `folder` |
| `err.backup_drive_inside_appdata` | `folder` |
| `err.backup_drive_unwritable` | `folder`, `detail` |
| `err.backup_retention_invalid` | `name`, `value` |

The UI agent also owns the `backup.status.*` keys returned by
`status_text_key()` (`requested`, `snapshot_created`, `validated`,
`copied_to_drive`, `sync_confirmed`, `cloud_unavailable`, `failed`,
`unknown`). Required honest wording: `backup.status.copied_to_drive` =
"In Google Drive folder — cloud sync unconfirmed" (never "backed up to
cloud", §11.15); `backup.status.sync_confirmed` must only be used when a
real sync signal exists.

**Update 2026-10-15 (integration):** the follow-up explicitly authorized
landing these keys, so all 16 `err.*` keys plus `backup.status.unknown`
were added to both `en.json` and `ur.json` (1039 keys each, parity holds;
Urdu strings are the backup agent's translations — the localization owner
should review wording). The UI agent had already added the other seven
`backup.status.*` keys with the honest §11.15 wording.

### Blocker fixes (integration follow-up, 2026-10-15)

1. **Pre-upgrade safety-backup deadlock (critical).** The pre-upgrade safety
   backup runs in `bootstrap()` *before* `migrate()` applies 015, so on a
   v14 database `backup_history.trigger` does not exist and a static INSERT
   raised `OperationalError` → `AppStartupError`, making the upgrade
   impossible. Fix: `BackupRepository.insert_history_row` is now
   schema-tolerant — it reads `PRAGMA table_info(backup_history)` and omits
   the `trigger` column when absent (records everything else). Pinned by
   `test_phase8_backup.py::test_run_backup_succeeds_on_pre_015_database`
   (drops the column to simulate v14, then runs the safety backup).
2. **Contract parity.** Published contract had `svc.get_setting(key)` /
   `svc.set_setting(key, value)` on `BackupService`; added as thin instance
   methods delegating to `BackupRepository` (settings were previously only
   reachable via the repository). `get_setting` returns the raw value or
   None; the day-close hook treats "not `'0'`" as enabled.

### Test-agent fixes (acceptance pass, 2026-10-15)

3. **Snapshot journal mode.** The online backup API copies the source's
   WAL-mode file header, so the temp snapshot was a WAL database; the
   read-only validation open then grew `-shm`/`-wal` sidecars next to the
   `.partial-*` temp file, which the atomic `os.replace` orphaned (§9
   "temp files are never left behind" violated — leaked sidecars were
   found in the backup folder). Fix: `_snapshot_live_db` now sets
   `PRAGMA journal_mode=DELETE` on the snapshot after the backup copy
   (snapshots are only opened read-only or staged for restore), and the
   `run_backup` `finally` also removes `-shm`/`-wal`/`-journal` sidecars
   of the temp path. Pinned by
   `test_phase8_acceptance.py::test_backup_leaves_no_temp_files_or_sidecars`.
4. **Day-close hook binds the service's own DB.** `_auto_backup_after_day_close`
   constructed `BackupService(actor=who)` with default (live) paths, so
   whenever `BusinessDayService` managed any other database (tests,
   tooling) the hook snapshotted the *live* database — test runs were
   creating the live DB file and writing `day_close` rows into it. Fix:
   the hook now passes `self._db` explicitly (identical in production,
   where `self._db` already is the live database). Pinned by the day-close
   hook acceptance tests (`..._back_up_when_enabled` /
   `..._skipped_when_disabled`), which run the real `close_day` on a temp
   DB with `config.BACKUP_DIR` redirected to tmp.
5. **Retention newest-guard.** `prune_backups` now keeps the newest
   known-good backup per destination unconditionally (index 0), instead of
   relying on the keep count being ≥ 1 — the last valid backup is never
   deleted even if the keep counts are 0. Pinned by
   `test_phase8_acceptance.py::test_retention_prune_oldest_newest_kept_and_zero_keep_safe`.

### Integration note: day-close auto-backup hook

`BusinessDayService._auto_backup_after_day_close` (owned by the
business-day agent, not modified here) already calls
`BackupService(actor=who).get_setting("backup.on_day_close")` and
`run_backup("day_close")` after a committed close — best-effort and
log-only on failure. The `get_setting(key) -> str | None` accessor was
added to `BackupService` to satisfy that contract (unset = default
enabled). Note: after a restore, the pre-restore safety backup's
*history row* belonged to the replaced database and is no longer
queryable, but its *file* (`PetrolPump_Safety_*` in the backup folder)
always survives — `RestoreReport.safety_backup_filename` names it, and
the report notes say so.
