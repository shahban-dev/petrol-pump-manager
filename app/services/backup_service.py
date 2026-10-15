"""Verified backup, retention and restore service (Phase 8).

Implements the §9 backup/restore design:

* the live database is snapshotted with the SQLite **online backup API**
  (``sqlite3.Connection.backup()``) — never a raw file copy of a WAL-mode
  database;
* every pipeline step is recorded in ``backup_history`` (statuses
  ``requested → snapshot_created → validated → copied_to_drive``), and
  **every** write happens even on failure paths (terminal ``failed`` rows
  keep their error text);
* the Drive leg copies the validated file into the configured Google Drive
  for desktop folder — no Google API, no internet required — and the cloud
  wording stays honest (§11.15): a copy is only ever reported as
  "copied to Google Drive folder — sync unconfirmed" unless a real OS sync
  signal says otherwise.

Restore: a restore never destroys what it replaces — an automatic safety
backup of the current live DB is taken first, the candidate is validated,
explicit confirmation is required, and the live DB is re-verified after
replacement.

Sizes are integer byte counts; no float is used anywhere here.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import config
from app.database import integrity_checks
from app.database.connection import DatabaseManager
from app.database.migrator import CURRENT_SCHEMA_VERSION, get_schema_version
from app.models.backup import (
    BackupRecord,
    PruneReport,
    RestoreReport,
    ValidationReport,
)
from app.repositories.backup_repository import BackupRepository
from app.services._base import ServiceBase
from app.services import errors


logger = logging.getLogger(__name__)

#: Triggers recorded in ``backup_history.trigger`` (migration 015).
VALID_TRIGGERS = ("manual", "day_close", "shutdown", "scheduled", "safety")

_BACKUP_PREFIX = "PetrolPump_Backup"
_SAFETY_PREFIX = "PetrolPump_Safety"
_BACKUP_SUFFIX = ".sqlite"
#: Only files we created are ever pruned or restored from the Drive folder.
_FILENAME_RE = re.compile(
    r"^PetrolPump_(?:Backup|Safety)_\d{4}-\d{2}-\d{2}_\d{6}(?:-\d+)?\.sqlite$"
)
#: Tables that must open and yield sane counts for a snapshot to validate.
_SPOT_CHECK_TABLES = (
    "business_days",
    "business_day_closings",
    "nozzle_readings",
    "customers",
    "cash_receipts",
    "audit_log",
)

#: i18n keys for backup statuses — the UI localizes these. The Drive copy
#: key is deliberately honest: "In Google Drive folder — cloud sync
#: unconfirmed", never "backed up to cloud" (design plan §11.15).
STATUS_TEXT_KEYS = {
    "requested": "backup.status.requested",
    "snapshot_created": "backup.status.snapshot_created",
    "validated": "backup.status.validated",
    "copied_to_drive": "backup.status.copied_to_drive",
    "sync_confirmed": "backup.status.sync_confirmed",
    "cloud_unavailable": "backup.status.cloud_unavailable",
    "failed": "backup.status.failed",
}


def _utc_now_iso() -> str:
    """Machine time as UTC text (design plan §5.1)."""
    return (
        datetime.now(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z"
    )


def _pkt_now() -> datetime:
    """Current local time in Asia/Karachi — filename use only."""
    try:
        tz = ZoneInfo("Asia/Karachi")
    except ZoneInfoNotFoundError:
        # PKT is UTC+5 year-round (no DST); a fixed offset is equivalent
        # when the tz database is unavailable.
        tz = timezone(timedelta(hours=5))
    return datetime.now(timezone.utc).astimezone(tz)


def _parse_utc(text: str) -> datetime:
    """Parse a UTC ISO timestamp written by this service / the DB default."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"unparseable UTC timestamp: {text!r}")


class BackupService(ServiceBase):
    """Verified backups, retention pruning and restore (Phase 8)."""

    def __init__(
        self,
        db_manager: DatabaseManager | None = None,
        actor: str | None = None,
        *,
        backup_dir: str | Path | None = None,
    ) -> None:
        super().__init__(
            db_manager if db_manager is not None else DatabaseManager(config.db_path()),
            actor,
        )
        self._backup_dir = Path(backup_dir) if backup_dir else config.BACKUP_DIR

    # ------------------------------------------------------------------
    # backup pipeline
    # ------------------------------------------------------------------
    def run_backup(self, trigger: str) -> BackupRecord:
        """Run the full §9 backup pipeline and return the final record.

        Steps: ``requested`` → online-backup snapshot to a temp file
        (``snapshot_created``) → integrity + FK + sanity validation
        (``validated`` or ``failed``) → atomic rename into place → sha256
        recorded → optional Drive copy (``copied_to_drive`` /
        ``sync_confirmed`` / ``cloud_unavailable`` / ``failed`` gdrive row).

        Raises:
            ValidationError: unknown trigger.
            BackupValidationError: the snapshot failed validation.
            BackupError: snapshot/rename/IO failed.
        """
        if trigger not in VALID_TRIGGERS:
            raise errors.ValidationError(
                f"Unknown backup trigger: {trigger!r}.",
                key="err.backup_unknown_trigger",
                params={"trigger": trigger},
            )
        backup_dir = self._ensure_backup_dir()
        filename = self._unique_filename(trigger, backup_dir)

        logger.info("backup requested (trigger=%s, file=%s)", trigger, filename)
        with self.transaction() as conn:
            backup_id = BackupRepository.insert_history_row(
                conn,
                filename=filename,
                destination="local",
                status="requested",
                trigger=trigger,
            )

        temp_path = backup_dir / f"{filename}.partial-{os.getpid()}"
        try:
            self._snapshot_live_db(temp_path)
            self._set_status(backup_id, "snapshot_created")

            report = self.validate_backup_file(temp_path)
            if not report.ok:
                self._set_status(backup_id, "failed", error=report.error)
                raise errors.BackupValidationError(
                    f"Backup snapshot failed validation: {report.error}",
                    key="err.backup_validation_failed",
                    params={"detail": report.error or ""},
                )
            self._set_status(
                backup_id, "validated", verified_at_utc=_utc_now_iso()
            )

            # Atomic rename into place (os.replace is atomic on one
            # filesystem; the file was already validated, so readers only
            # ever see complete backups).
            final_path = backup_dir / filename
            os.replace(temp_path, final_path)

            digest = self._sha256(final_path)
            size = final_path.stat().st_size
            self._set_status(
                backup_id, "validated", size_bytes=size, sha256=digest
            )
            logger.info(
                "backup validated (id=%s, file=%s, size_bytes=%d)",
                backup_id,
                filename,
                size,
            )

            self._drive_leg(backup_id, final_path, filename, digest, size, trigger)
        except errors.BusinessRuleError:
            raise
        except OSError as exc:
            self._set_status(backup_id, "failed", error=str(exc)[:500])
            raise errors.BackupError(
                f"Backup failed: {exc}",
                key="err.backup_failed",
                params={"detail": str(exc)[:200]},
            ) from exc
        finally:
            # Temp files are never left behind on failure (§9 contract) —
            # including any -shm/-wal/-journal sidecars the temp snapshot
            # may have grown (e.g. if validation opened it while it was
            # still a WAL-mode file).
            for sidecar in (
                temp_path,
                Path(str(temp_path) + "-shm"),
                Path(str(temp_path) + "-wal"),
                Path(str(temp_path) + "-journal"),
            ):
                try:
                    if sidecar.exists():
                        sidecar.unlink()
                except OSError:
                    logger.warning(
                        "could not remove temp backup file %s", sidecar.name
                    )
        return self._get_record_or_raise(backup_id)

    def _ensure_backup_dir(self) -> Path:
        """Create the backup directory; fail safe with a clear message."""
        try:
            self._backup_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise errors.BackupError(
                f"Cannot create the backup folder '{self._backup_dir}': {exc}."
                " No backup was taken.",
                key="err.backup_dir_unwritable",
                params={
                    "folder": str(self._backup_dir),
                    "detail": str(exc)[:200],
                },
            ) from exc
        return self._backup_dir

    def _unique_filename(self, trigger: str, backup_dir: Path) -> str:
        """Final backup filename (PKT time in the filename only, §9).

        A same-second collision gets a ``-2``/``-3`` suffix rather than
        ever overwriting an existing backup.
        """
        prefix = _SAFETY_PREFIX if trigger == "safety" else _BACKUP_PREFIX
        stamp = _pkt_now().strftime("%Y-%m-%d_%H%M%S")
        candidate = f"{prefix}_{stamp}{_BACKUP_SUFFIX}"
        counter = 2
        while (backup_dir / candidate).exists():
            candidate = f"{prefix}_{stamp}-{counter}{_BACKUP_SUFFIX}"
            counter += 1
        return candidate

    def _snapshot_live_db(self, temp_path: Path) -> None:
        """Copy the live DB with the SQLite online backup API.

        A raw file copy of a WAL-mode database is not a consistent
        snapshot (recovery.md §1); ``Connection.backup()`` is the only
        supported way to snapshot a live database.
        """
        src = self._db.connect()
        try:
            dst = sqlite3.connect(str(temp_path))
            try:
                src.backup(dst)
                # The online backup API copies the source's WAL-mode file
                # header, so the snapshot would be a WAL database: the
                # read-only validation open then grows -shm/-wal sidecars
                # next to the temp file, which the atomic os.replace would
                # orphan (§9: temp files are never left behind). A snapshot
                # is only ever opened read-only or staged for restore, so
                # plain rollback-journal mode is correct and portable.
                dst.execute("PRAGMA journal_mode=DELETE")
            finally:
                dst.close()
        except sqlite3.Error as exc:
            raise errors.BackupError(
                f"Could not snapshot the database: {exc}.",
                key="err.backup_snapshot_failed",
                params={"detail": str(exc)[:200]},
            ) from exc
        finally:
            src.close()

    @staticmethod
    def _sha256(path: Path) -> str:
        """Chunked SHA-256 of a file (memory-safe for large databases)."""
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _set_status(self, backup_id: int, status: str, **fields) -> None:
        """Record a pipeline step; best-effort so a broken DB never masks
        the original failure with a second exception."""
        try:
            with self.transaction() as conn:
                BackupRepository.update_status(
                    conn, backup_id, status, **fields
                )
        except Exception:
            logger.exception(
                "could not record backup status %r for row %s",
                status,
                backup_id,
            )

    def _get_record_or_raise(self, backup_id: int) -> BackupRecord:
        record = self.get_record(backup_id)
        if record is None:
            raise errors.BackupError(
                f"Backup row {backup_id} disappeared mid-run.",
                key="err.backup_row_missing",
                params={"backup_id": backup_id},
            )
        return record

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------
    def validate_backup_file(self, path: str | Path) -> ValidationReport:
        """Validate a backup file (snapshot or restore candidate).

        Opens the file read-only: ``PRAGMA integrity_check`` must be
        ``ok``, ``foreign_key_check`` must be empty, the core business
        tables must open with sane row counts, and the file size must be
        plausible (> 0). Never modifies the file.
        """
        checks: list = []

        def record(name: str, ok: bool, detail: str = "") -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        target = Path(path)
        record("file_present", target.is_file(), target.name)
        if not target.is_file():
            return ValidationReport(
                ok=False, checks=checks, error=f"Backup file not found: {target}"
            )
        size = target.stat().st_size
        record("size_plausible", size > 0, f"{size} bytes")
        if size <= 0:
            return ValidationReport(
                ok=False, checks=checks, error="Backup file is empty."
            )

        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            expected = ("business_days", "audit_log")
            missing = [t for t in expected if t not in tables]
            record(
                "expected_tables",
                not missing,
                f"missing: {', '.join(missing)}" if missing else "present",
            )
            if missing:
                return ValidationReport(
                    ok=False,
                    checks=checks,
                    error=f"Backup is missing tables: {', '.join(missing)}.",
                )

            ic_row = conn.execute("PRAGMA integrity_check").fetchone()
            ic_ok = ic_row is not None and str(ic_row[0]).lower() == "ok"
            record(
                "integrity_check",
                ic_ok,
                "ok" if ic_ok else str(ic_row[0]) if ic_row else "no result",
            )
            if not ic_ok:
                return ValidationReport(
                    ok=False,
                    checks=checks,
                    error=f"integrity_check failed: {ic_row[0] if ic_row else 'no result'}",
                )

            fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
            record(
                "foreign_key_check",
                not fk_rows,
                "no violations"
                if not fk_rows
                else f"{len(fk_rows)} violation(s)",
            )
            if fk_rows:
                return ValidationReport(
                    ok=False,
                    checks=checks,
                    error=f"foreign_key_check found {len(fk_rows)} violation(s).",
                )

            for check in self._business_spot_checks(conn):
                checks.append(check)
            if not all(c["ok"] for c in checks if c["name"].startswith("spot_")):
                return ValidationReport(
                    ok=False,
                    checks=checks,
                    error="Business data spot-checks failed.",
                )
        except sqlite3.DatabaseError as exc:
            record("open_readable", False, str(exc)[:200])
            return ValidationReport(
                ok=False,
                checks=checks,
                error=f"Cannot open backup file: {exc}",
            )
        finally:
            conn.close()

        return ValidationReport(ok=True, checks=checks, error=None)

    @staticmethod
    def _business_spot_checks(conn: sqlite3.Connection) -> list:
        """Row-count sanity checks over the core business tables.

        "Sane" means the table opens and ``COUNT(*)`` is a non-negative
        integer — a smoke check that the snapshot holds real business
        data, not a stronger claim than the file can prove.
        """
        out = []
        for table in _SPOT_CHECK_TABLES:
            try:
                count = conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                sane = isinstance(count, int) and count >= 0
                out.append(
                    {
                        "name": f"spot_{table}",
                        "ok": sane,
                        "detail": f"{count} row(s)",
                    }
                )
            except sqlite3.Error as exc:
                out.append(
                    {
                        "name": f"spot_{table}",
                        "ok": False,
                        "detail": f"cannot read table: {exc}",
                    }
                )
        return out

    # ------------------------------------------------------------------
    # Drive leg
    # ------------------------------------------------------------------
    def get_drive_folder(self) -> Path | None:
        """The configured Google Drive for desktop folder, or None."""
        with self.read_connection() as conn:
            raw = BackupRepository.get_setting(conn, "backup.drive_folder")
        return Path(raw) if raw else None

    def set_drive_folder(self, path: str) -> Path:
        """Configure the Drive folder destination.

        The folder must exist, be a directory and be writable, and it must
        NOT be inside the app's data directory (the live DB and the local
        backups must never sit in a cloud-synced folder — design plan §0).
        """
        candidate = Path(path).expanduser()
        if not candidate.exists() or not candidate.is_dir():
            raise errors.ValidationError(
                f"The Drive folder '{path}' does not exist or is not a"
                " folder. Choose the Google Drive for desktop folder.",
                key="err.backup_drive_invalid",
                params={"folder": str(path)},
            )
        resolved = candidate.resolve()
        app_data = config.BASE_DIR.resolve()
        if resolved == app_data or resolved.is_relative_to(app_data):
            raise errors.ValidationError(
                "The Drive folder must not be inside the app's data folder"
                f" ('{app_data}'). The live database and local backups must"
                " never sit in a cloud-synced folder.",
                key="err.backup_drive_inside_appdata",
                params={"folder": str(resolved)},
            )
        # Real writability probe — os.access alone can lie on Windows/ACLs.
        probe = resolved / f".ppm_write_test_{os.getpid()}"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise errors.ValidationError(
                f"The Drive folder '{resolved}' is not writable: {exc}.",
                key="err.backup_drive_unwritable",
                params={
                    "folder": str(resolved),
                    "detail": str(exc)[:200],
                },
            ) from exc
        with self.transaction() as conn:
            BackupRepository.set_setting(
                conn, "backup.drive_folder", str(resolved)
            )
        logger.info("drive folder configured: %s", resolved)
        return resolved

    def drive_available(self) -> bool:
        """Whether the configured Drive folder is reachable and writable."""
        folder = self.get_drive_folder()
        return bool(
            folder is not None
            and folder.is_dir()
            and os.access(folder, os.W_OK)
        )

    def _drive_leg(
        self,
        backup_id: int,
        final_path: Path,
        filename: str,
        digest: str,
        size: int,
        trigger: str,
    ) -> None:
        """Copy the validated snapshot into the Drive folder (§9 step 6).

        No Drive folder configured → local-only backup, nothing to do.
        Drive configured but unreachable → ``cloud_unavailable`` gdrive
        row; the app keeps working offline. A failed copy → ``failed``
        gdrive row; the completed local backup still stands.
        """
        folder = self.get_drive_folder()
        if folder is None:
            return
        now = _utc_now_iso()
        if not self.drive_available():
            logger.warning(
                "drive folder %s unavailable; local backup %s kept",
                folder,
                filename,
            )
            with self.transaction() as conn:
                BackupRepository.insert_history_row(
                    conn,
                    filename=filename,
                    destination="gdrive",
                    status="cloud_unavailable",
                    trigger=trigger,
                    error=(
                        "Google Drive folder was not reachable; the local"
                        " backup completed. The copy will be retried on the"
                        " next backup."
                    ),
                    size_bytes=size,
                    sha256=digest,
                    verified_at_utc=now,
                )
            return
        dest = folder / filename
        try:
            shutil.copy2(str(final_path), str(dest))
            copied_size = dest.stat().st_size
            if copied_size != size:
                raise OSError(
                    f"copied size {copied_size} does not match source {size}"
                )
            status = (
                "sync_confirmed"
                if self._drive_sync_confirmed(dest)
                else "copied_to_drive"
            )
            with self.transaction() as conn:
                BackupRepository.insert_history_row(
                    conn,
                    filename=filename,
                    destination="gdrive",
                    status=status,
                    trigger=trigger,
                    size_bytes=copied_size,
                    sha256=digest,
                    verified_at_utc=now,
                )
            logger.info(
                "drive copy complete (file=%s, status=%s)", filename, status
            )
        except OSError as exc:
            logger.warning("drive copy failed for %s: %s", filename, exc)
            with self.transaction() as conn:
                BackupRepository.insert_history_row(
                    conn,
                    filename=filename,
                    destination="gdrive",
                    status="failed",
                    trigger=trigger,
                    error=str(exc)[:500],
                    size_bytes=size,
                    sha256=digest,
                )

    def _drive_sync_confirmed(self, copied_path: Path) -> bool:
        """Whether the OS shows a *positive* signal of Drive sync.

        Google Drive for desktop exposes no reliable per-file sync signal
        to a plain folder destination, and §9 forbids depending on the
        Google API. The only observable signal is an in-progress upload
        marker, whose *presence* proves sync has NOT finished. Absence
        proves nothing — so this defaults to ``False`` and the copy is
        honestly reported as ``copied_to_drive`` ("in Google Drive folder
        — cloud sync unconfirmed") until a future integration provides a
        real confirmation hook.
        """
        marker = copied_path.with_name(copied_path.name + ".tmp.driveupload")
        if marker.exists():
            logger.info(
                "drive upload marker present for %s; sync not finished",
                copied_path.name,
            )
        return False

    # ------------------------------------------------------------------
    # restore
    # ------------------------------------------------------------------
    def restore_backup(
        self, backup_id: int, *, confirmed: bool, actor: str | None = None
    ) -> RestoreReport:
        """Restore the live database from a validated backup.

        Steps (recovery.md §4):
        (a) automatic safety backup of the CURRENT live DB first — a
            restore never destroys what it replaces;
        (b) validate the candidate file (must pass or raise);
        (c) close all pooled DB connections;
        (d) stage a copy of the candidate and atomically replace the live
            DB file (the backup file itself is never moved);
        (e) post-restore verification: integrity_check, foreign_key_check,
            schema-version compatibility, and business-data checks;
        (f) return the report — the application should be restarted so
            every layer re-opens the replaced database.

        Raises:
            RestoreConfirmationRequired: ``confirmed`` is falsy.
            NotFoundError: no such backup row.
            BackupValidationError: candidate file missing or invalid.
            BackupError: replacement or post-restore verification failed
                (the safety backup id is named in the message).
        """
        if not confirmed:
            raise errors.RestoreConfirmationRequired(
                "Restoring replaces the live database. Confirm explicitly"
                " that you want to restore this backup.",
                key="err.restore_confirmation_required",
                params={"backup_id": backup_id},
            )
        who = self.resolve_actor(actor)
        logger.info("restore requested (backup_id=%s, actor=%s)", backup_id, who)

        candidate = self.get_record(backup_id)
        if candidate is None:
            raise errors.NotFoundError(
                f"No backup with id {backup_id}.",
                key="err.backup_not_found",
                params={"backup_id": backup_id},
            )
        candidate_path = self._candidate_path(candidate)
        if not candidate_path.is_file():
            raise errors.BackupValidationError(
                f"The backup file '{candidate.filename}' is missing from"
                f" its {candidate.destination} folder; it cannot be"
                " restored.",
                key="err.backup_file_missing",
                params={
                    "filename": candidate.filename,
                    "destination": candidate.destination,
                },
            )

        # (a) Safety backup of the current live DB first.
        safety = self.run_backup("safety")
        logger.info(
            "pre-restore safety backup complete (id=%s)", safety.id
        )

        # (b) Validate the candidate before it becomes the live DB.
        report = self.validate_backup_file(candidate_path)
        if not report.ok:
            raise errors.BackupValidationError(
                f"Backup '{candidate.filename}' failed validation and was"
                f" NOT restored: {report.error} The current database is"
                " unchanged.",
                key="err.restore_candidate_invalid",
                params={
                    "filename": candidate.filename,
                    "detail": report.error or "",
                },
            )

        # (c) Close all pooled connections, then (d) replace the live file.
        self._db.close_all()
        live_path = Path(self._db.db_path)
        staging = self._backup_dir / f"{candidate.filename}.restore-{os.getpid()}"
        try:
            shutil.copy2(str(candidate_path), str(staging))
            # Drop stale WAL sidecars of the OLD database: after os.replace
            # they would otherwise attach to the NEW file and corrupt it.
            for suffix in ("-wal", "-shm", "-journal"):
                sidecar = Path(str(live_path) + suffix)
                try:
                    if sidecar.exists():
                        sidecar.unlink()
                except OSError:
                    logger.warning(
                        "could not remove stale sidecar %s", sidecar.name
                    )
            os.replace(staging, live_path)
            logger.info(
                "live database replaced from backup %s (actor=%s)",
                candidate.filename,
                who,
            )
        except OSError as exc:
            raise errors.BackupError(
                f"Could not put the backup in place: {exc}. The current"
                f" database was left as-is; safety backup id {safety.id}"
                " holds its state.",
                key="err.restore_replace_failed",
                params={
                    "detail": str(exc)[:200],
                    "safety_backup_id": safety.id,
                },
            ) from exc
        finally:
            try:
                if staging.exists():
                    staging.unlink()
            except OSError:
                logger.warning(
                    "could not remove restore staging file %s", staging.name
                )

        # (e) Post-restore verification on the new live DB.
        checks = self._post_restore_checks()
        failures = [c for c in checks if not c["ok"]]
        if failures:
            detail = "; ".join(
                f"{c['name']}: {c['detail']}" for c in failures
            )
            raise errors.BackupError(
                "The restored database failed post-restore verification"
                f" ({detail}). Restore the safety backup (id {safety.id})"
                " to get back to the pre-restore state.",
                key="err.restore_verification_failed",
                params={
                    "detail": detail[:300],
                    "safety_backup_id": safety.id,
                },
            )

        notes = (
            "The live database was replaced. Restart the application so"
            " every layer re-opens the restored database cleanly. The"
            f" pre-restore safety backup file '{safety.filename}' remains"
            " in the backup folder; its history row belonged to the"
            " replaced database."
        )
        return RestoreReport(
            restored_backup_id=backup_id,
            candidate_filename=candidate.filename,
            safety_backup_id=safety.id,
            safety_backup_filename=safety.filename,
            verified=True,
            checks=checks,
            restart_required=True,
            notes=notes,
        )

    def _candidate_path(self, record: BackupRecord) -> Path:
        """Filesystem location of a backup row's file."""
        folder = self.get_drive_folder()
        if record.destination == "gdrive" and folder is not None:
            return folder / record.filename
        return self._backup_dir / record.filename

    def _post_restore_checks(self) -> list:
        """Verify the replaced live DB (integrity, FK, schema, business).

        Reuses ``app.database.integrity_checks``; business checks mirror
        the restore contract: business-day dates present, the latest
        closing exists, and the core tables open with sane counts.
        """
        checks: list = []

        def record(name: str, ok: bool, detail: str = "") -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        conn = self._db.connect()
        try:
            ic = integrity_checks.check_integrity(conn)
            record("integrity_check", ic == "ok", ic)
            fk = integrity_checks.check_foreign_keys(conn)
            record(
                "foreign_key_check",
                not fk,
                "no violations" if not fk else f"{len(fk)} violation(s)",
            )
            missing = integrity_checks.check_required_objects(conn)
            record(
                "schema_objects",
                not missing,
                "all present"
                if not missing
                else f"missing: {', '.join(missing[:5])}",
            )
            version = get_schema_version(conn)
            record(
                "schema_version",
                version <= CURRENT_SCHEMA_VERSION,
                f"db={version}, build={CURRENT_SCHEMA_VERSION}",
            )

            day_row = conn.execute(
                "SELECT COUNT(*), MIN(business_date), MAX(business_date)"
                " FROM business_days"
            ).fetchone()
            day_count = int(day_row[0])
            record(
                "business_days_present",
                day_count >= 1,
                f"{day_count} day(s)"
                + (f", {day_row[1]}..{day_row[2]}" if day_count else ""),
            )
            close_row = conn.execute(
                "SELECT COUNT(*), MAX(business_date), MAX(closing_revision)"
                " FROM business_day_closings"
            ).fetchone()
            close_count = int(close_row[0])
            record(
                "latest_closing_exists",
                close_count >= 1,
                f"{close_count} closing(s)"
                + (
                    f", latest {close_row[1]} rev {close_row[2]}"
                    if close_count
                    else ""
                ),
            )
            for check in self._business_spot_checks(conn):
                checks.append(check)
        except sqlite3.Error as exc:
            record("post_restore_open", False, str(exc)[:200])
        finally:
            conn.close()
        return checks

    # ------------------------------------------------------------------
    # history reads
    # ------------------------------------------------------------------
    def list_history(
        self, limit: int = 100, destination: str | None = None
    ) -> list[BackupRecord]:
        """Newest-first backup history (both destinations by default)."""
        with self.read_connection() as conn:
            rows = BackupRepository.list_history(
                conn, limit=limit, destination=destination
            )
        return [BackupRecord.from_row(r) for r in rows]

    def get_record(self, backup_id: int) -> BackupRecord | None:
        """One backup history row, or None."""
        with self.read_connection() as conn:
            row = BackupRepository.get_by_id(conn, backup_id)
        return BackupRecord.from_row(row) if row else None

    def get_setting(self, key: str) -> str | None:
        """Raw ``app_settings`` value for *key*, or None when unset.

        Convenience accessor for integration points — e.g. the day-close
        auto-backup hook in ``BusinessDayService`` reads
        ``backup.on_day_close`` (unset means the default, enabled).
        Documented defaults live on
        :class:`app.repositories.backup_repository.BackupRepository`.
        """
        with self.read_connection() as conn:
            return BackupRepository.get_setting(conn, key)

    def set_setting(self, key: str, value: str) -> None:
        """Upsert an ``app_settings`` value (contract parity with the
        published ``BackupService`` API; delegates to the repository)."""
        with self.transaction() as conn:
            BackupRepository.set_setting(conn, key, value)

    @staticmethod
    def status_text_key(record: BackupRecord) -> str:
        """Stable i18n key for a record's status (honest cloud wording).

        The UI renders ``tr(key)``; ``backup.status.copied_to_drive`` is
        "In Google Drive folder — cloud sync unconfirmed" — never "backed
        up to cloud" (design plan §11.15).
        """
        return STATUS_TEXT_KEYS.get(record.status, "backup.status.unknown")

    # ------------------------------------------------------------------
    # retention
    # ------------------------------------------------------------------
    def get_retention(self) -> tuple[int, int]:
        """``(keep_local, keep_drive)`` — validated backups kept per side."""
        with self.read_connection() as conn:
            keep_local = BackupRepository.get_setting(conn, "backup.keep_local")
            keep_drive = BackupRepository.get_setting(conn, "backup.keep_drive")
        return (
            self._positive_int_or(keep_local, 30),
            self._positive_int_or(keep_drive, 14),
        )

    @staticmethod
    def _positive_int_or(raw: str | None, default: int) -> int:
        try:
            value = int(raw) if raw is not None else default
        except (TypeError, ValueError):
            return default
        return value if value >= 1 else default

    def set_retention(self, keep_local: int, keep_drive: int) -> None:
        """Set how many validated backups to keep per side (positive ints)."""
        for name, value in (
            ("keep_local", keep_local),
            ("keep_drive", keep_drive),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise errors.ValidationError(
                    f"Retention '{name}' must be a positive whole number"
                    f" (got {value!r}).",
                    key="err.backup_retention_invalid",
                    params={"name": name, "value": value},
                )
        with self.transaction() as conn:
            BackupRepository.set_setting(
                conn, "backup.keep_local", str(keep_local)
            )
            BackupRepository.set_setting(
                conn, "backup.keep_drive", str(keep_drive)
            )
        logger.info(
            "retention set: keep_local=%d keep_drive=%d", keep_local, keep_drive
        )

    def prune_backups(self) -> PruneReport:
        """Delete files of old validated backups beyond the keep counts.

        Oldest first; history rows are kept (audit trail). Never deletes
        the newest known-good backup per destination, and never deletes a
        safety backup younger than 7 days. Only files this app created
        (``PetrolPump_Backup_*`` / ``PetrolPump_Safety_*``) are touched.
        """
        keep_local, keep_drive = self.get_retention()
        deleted_local: list[str] = []
        deleted_drive: list[str] = []
        errors_out: list[str] = []

        def unlink(target: Path, bucket: list[str]) -> None:
            try:
                target.unlink()
                bucket.append(target.name)
                logger.info("pruned old backup file %s", target.name)
            except OSError as exc:
                errors_out.append(f"{target.name}: {exc}")
                logger.warning("could not prune %s: %s", target.name, exc)

        with self.read_connection() as conn:
            local_rows = [
                BackupRecord.from_row(r)
                for r in BackupRepository.known_good_rows(conn, "local")
            ]
        kept_local = 0
        for index, record in enumerate(local_rows):
            if index == 0:
                # The newest known-good backup is never deleted — even
                # when the keep count is 0. The last valid backup is the
                # final restore point and must survive retention.
                kept_local += 1
                continue
            if index < keep_local:
                kept_local += 1
                continue
            if self._recent_safety(record):
                kept_local += 1  # exempt — still counts as kept
                continue
            target = self._backup_dir / record.filename
            if target.is_file() and _FILENAME_RE.match(target.name):
                unlink(target, deleted_local)

        kept_drive = 0
        folder = self.get_drive_folder()
        if folder is not None and folder.is_dir():
            with self.read_connection() as conn:
                drive_rows = [
                    BackupRecord.from_row(r)
                    for r in BackupRepository.known_good_rows(conn, "gdrive")
                ]
            for index, record in enumerate(drive_rows):
                if index == 0:
                    # Newest known-good Drive copy is never deleted —
                    # same guarantee as the local side.
                    kept_drive += 1
                    continue
                if index < keep_drive:
                    kept_drive += 1
                    continue
                if self._recent_safety(record):
                    kept_drive += 1
                    continue
                target = folder / record.filename
                if target.is_file() and _FILENAME_RE.match(target.name):
                    unlink(target, deleted_drive)

        return PruneReport(
            deleted_local=deleted_local,
            deleted_drive=deleted_drive,
            kept_local=kept_local,
            kept_drive=kept_drive,
            errors=errors_out,
        )

    @staticmethod
    def _recent_safety(record: BackupRecord) -> bool:
        """A safety backup younger than 7 days is never pruned."""
        if record.trigger != "safety":
            return False
        try:
            age = datetime.now(timezone.utc) - _parse_utc(record.requested_at_utc)
        except ValueError:
            return True  # undatable — err on the side of keeping it
        return age < timedelta(days=7)

    # ------------------------------------------------------------------
    # scheduled backups
    # ------------------------------------------------------------------
    def scheduled_backup_due(self) -> bool:
        """Whether a periodic backup is due.

        ``backup.periodic_minutes`` of ``0`` (the default) disables the
        schedule; otherwise a run is due when the last scheduled run is at
        least the interval old (or never ran).
        """
        with self.read_connection() as conn:
            raw_minutes = BackupRepository.get_setting(
                conn, "backup.periodic_minutes"
            )
            raw_last = BackupRepository.get_setting(
                conn, "backup.last_scheduled_at_utc"
            )
        minutes = self._positive_int_or(raw_minutes, 0)
        if minutes <= 0:
            return False
        if not raw_last:
            return True
        try:
            last = _parse_utc(raw_last)
        except ValueError:
            return True
        return datetime.now(timezone.utc) - last >= timedelta(minutes=minutes)

    def mark_scheduled_backup(self) -> None:
        """Record that a scheduled backup just ran."""
        with self.transaction() as conn:
            BackupRepository.set_setting(
                conn, "backup.last_scheduled_at_utc", _utc_now_iso()
            )
