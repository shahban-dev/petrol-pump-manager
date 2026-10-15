"""Phase-8 backup/restore tests (acceptance tests 14–15).

Every test uses a FRESH temporary database plus temporary backup/drive
folders — the live database is never touched. Filenames carry PKT time
(``Asia/Karachi``); all stored timestamps are UTC.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from app import config
from app.database.connection import DatabaseManager
from app.models.backup import BackupRecord
from app.repositories.backup_repository import BackupRepository
from app.services import errors
from app.services.backup_service import BackupService


@pytest.fixture(autouse=True)
def _temp_backup_dir(monkeypatch, tmp_path: Path):
    """Redirect the *default* backup dir into tmp for every test here.

    The day-close hook constructs its ``BackupService`` without an
    explicit ``backup_dir`` (falling back to ``config.BACKUP_DIR``); the
    database it backs up is already the temp DB, and this keeps the
    hook's *files* in tmp too — the real backup folder stays untouched.
    """
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")


@pytest.fixture
def backup_svc(db, db_file: Path, tmp_path: Path):
    """BackupService bound to the migrated temp DB with temp folders."""
    manager = DatabaseManager(db_file)
    svc = BackupService(
        manager,
        actor="system",
        backup_dir=tmp_path / "backups",
    )
    svc._drive_dir = tmp_path / "drive"  # placeholder, unused directly
    return svc, tmp_path


def _fresh_conn(db_file: Path) -> sqlite3.Connection:
    """A brand-new read connection (sees the current on-disk state)."""
    manager = DatabaseManager(db_file)
    conn = manager.connect()
    conn.execute("SELECT 1")
    return conn


def _seed_closed_day(svc_ns, business_date: str) -> None:
    """Close a day through the real services (closings ledger gets a row)."""
    from app.tests.conftest import close_prior_day

    close_prior_day(
        svc_ns,
        business_date,
        {"1 PG+": "100", "2 PG+": "100", "3 HSD": "100", "4 HSD": "100"},
    )


def _table_counts(db_file: Path, tables: tuple | None = None) -> dict:
    conn = _fresh_conn(db_file)
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                tables
                or (
                    "business_days",
                    "business_day_closings",
                    "nozzle_readings",
                    "business_day_events",
                    "audit_log",
                )
            )
        }
    finally:
        conn.close()


#: Business tables for restore-equality checks. ``audit_log`` and
#: ``backup_history`` are deliberately excluded: the snapshot is taken
#: mid-pipeline (before the final status UPDATEs), and a restore always
#: takes a safety backup first, so operational rows legitimately differ —
#: the *business* data must be identical.
_BUSINESS_TABLES = (
    "business_days",
    "business_day_closings",
    "nozzle_readings",
    "business_day_events",
)


# ---------------------------------------------------------------------------
# backup pipeline
# ---------------------------------------------------------------------------
def test_run_backup_manual_validated(backup_svc, phase3_services, db_file):
    svc, tmp_path = backup_svc
    _seed_closed_day(phase3_services, "2026-10-14")
    phase3_services.days.open_day("2026-10-15", opening_cash_paisa=2_000_000)

    record = svc.run_backup("manual")

    assert isinstance(record, BackupRecord)
    assert record.status == "validated"
    assert record.trigger == "manual"
    assert record.destination == "local"
    assert record.size_bytes and record.size_bytes > 0
    assert record.sha256 and len(record.sha256) == 64
    assert record.verified_at_utc
    assert record.filename.startswith("PetrolPump_Backup_")
    assert record.filename.endswith(".sqlite")
    assert (tmp_path / "backups" / record.filename).is_file()

    # The pipeline left a full audit trail of statuses.
    statuses = [r.status for r in svc.list_history()]
    assert "validated" in statuses


def test_run_backup_unknown_trigger_rejected(backup_svc):
    svc, _ = backup_svc
    with pytest.raises(errors.ValidationError):
        svc.run_backup("sometimes")


def test_run_backup_filename_carries_pkt_time(backup_svc):
    svc, tmp_path = backup_svc
    record = svc.run_backup("manual")
    # YYYY-MM-DD_HHMMSS in the filename (PKT, filename only — §9).
    stem = Path(record.filename).stem
    date_part, time_part = stem.rsplit("_", 2)[1], stem.rsplit("_", 2)[2]
    assert len(date_part) == 10 and date_part[4] == "-" and date_part[7] == "-"
    assert len(time_part) == 6 and time_part.isdigit()


def test_safety_backup_uses_safety_prefix(backup_svc):
    svc, tmp_path = backup_svc
    record = svc.run_backup("safety")
    assert record.filename.startswith("PetrolPump_Safety_")
    assert record.trigger == "safety"
    assert record.status == "validated"


def test_validate_backup_file_rejects_corrupt(backup_svc):
    svc, tmp_path = backup_svc
    bad = tmp_path / "backups" / "corrupt.sqlite"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"this is not a sqlite database" * 100)
    report = svc.validate_backup_file(bad)
    assert not report.ok
    assert report.error
    assert any(not c["ok"] for c in report.checks)


def test_validate_backup_file_missing(backup_svc):
    svc, tmp_path = backup_svc
    report = svc.validate_backup_file(tmp_path / "nope.sqlite")
    assert not report.ok
    assert "not found" in report.error


# ---------------------------------------------------------------------------
# Drive leg + honest cloud wording
# ---------------------------------------------------------------------------
def test_drive_copy_and_honest_status(backup_svc):
    svc, tmp_path = backup_svc
    drive = tmp_path / "drive"
    drive.mkdir()
    svc.set_drive_folder(str(drive))

    record = svc.run_backup("manual")

    copied = drive / record.filename
    assert copied.is_file()
    assert copied.stat().st_size == record.size_bytes

    gdrive_rows = svc.list_history(destination="gdrive")
    assert len(gdrive_rows) == 1
    assert gdrive_rows[0].status == "copied_to_drive"
    assert gdrive_rows[0].filename == record.filename
    # Honest wording: copied to the Drive folder, sync unconfirmed (§11.15).
    assert (
        BackupService.status_text_key(gdrive_rows[0])
        == "backup.status.copied_to_drive"
    )


def test_drive_folder_refuses_app_data_dir(backup_svc):
    svc, _ = backup_svc
    with pytest.raises(errors.ValidationError):
        svc.set_drive_folder(str(config.BASE_DIR))
    with pytest.raises(errors.ValidationError):
        # The app's real backup folder (BASE_DIR/"backups") — spelled out
        # instead of config.BACKUP_DIR because the module autouse fixture
        # redirects config.BACKUP_DIR into tmp.
        svc.set_drive_folder(str(config.BASE_DIR / "backups"))


def test_drive_folder_must_exist_and_be_dir(backup_svc):
    svc, tmp_path = backup_svc
    with pytest.raises(errors.ValidationError):
        svc.set_drive_folder(str(tmp_path / "does-not-exist"))
    a_file = tmp_path / "afile.txt"
    a_file.write_text("x")
    with pytest.raises(errors.ValidationError):
        svc.set_drive_folder(str(a_file))


def test_drive_unavailable_records_cloud_unavailable(backup_svc):
    svc, tmp_path = backup_svc
    drive = tmp_path / "drive"
    drive.mkdir()
    svc.set_drive_folder(str(drive))
    # Simulate Drive going offline: remove the folder after configuring.
    drive.rmdir()
    assert not svc.drive_available()

    record = svc.run_backup("manual")
    assert record.status == "validated"  # local backup still completes

    gdrive_rows = svc.list_history(destination="gdrive")
    assert len(gdrive_rows) == 1
    assert gdrive_rows[0].status == "cloud_unavailable"
    assert BackupService.status_text_key(gdrive_rows[0]) == (
        "backup.status.cloud_unavailable"
    )


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------
def test_restore_requires_confirmation(backup_svc):
    svc, _ = backup_svc
    record = svc.run_backup("manual")
    with pytest.raises(errors.RestoreConfirmationRequired):
        svc.restore_backup(record.id, confirmed=False)


def test_restore_round_trip(backup_svc, phase3_services, db_file):
    svc, tmp_path = backup_svc
    _seed_closed_day(phase3_services, "2026-10-14")
    phase3_services.days.open_day("2026-10-15", opening_cash_paisa=2_000_000)

    candidate = svc.run_backup("manual")
    counts_before = _table_counts(db_file, _BUSINESS_TABLES)

    # Mutate the live DB after the backup: a note event on the open day.
    conn = _fresh_conn(db_file)
    conn.execute(
        "INSERT INTO business_day_events (business_date, event_type, actor, reason)"
        " VALUES ('2026-10-15', 'note', 'system', 'post-backup mutation')"
    )
    conn.commit()
    conn.close()
    assert _table_counts(db_file, _BUSINESS_TABLES)["business_day_events"] == (
        counts_before["business_day_events"] + 1
    )

    known_ids = {r.id for r in svc.list_history()}
    report = svc.restore_backup(candidate.id, confirmed=True, actor="system")

    assert report.verified
    assert report.restart_required
    assert report.safety_backup_id != candidate.id
    # The safety backup was taken during the restore (after the candidate).
    assert report.safety_backup_id not in known_ids
    # Its history row lived in the replaced DB, but the FILE always
    # survives in the backup folder and must itself validate.
    safety_path = tmp_path / "backups" / report.safety_backup_filename
    assert report.safety_backup_filename.startswith("PetrolPump_Safety_")
    assert safety_path.is_file()
    assert svc.validate_backup_file(safety_path).ok
    # The post-backup mutation is gone; business data is identical.
    # (audit_log/backup_history legitimately differ: the snapshot is taken
    # mid-pipeline and the restore takes a safety backup first.)
    assert _table_counts(db_file, _BUSINESS_TABLES) == counts_before


def test_restore_rejects_invalid_candidate(backup_svc, db_file):
    svc, tmp_path = backup_svc
    candidate = svc.run_backup("manual")
    counts_before = _table_counts(db_file, _BUSINESS_TABLES)

    # Corrupt the backup file on disk.
    target = tmp_path / "backups" / candidate.filename
    target.write_bytes(b"garbage" * 500)

    with pytest.raises(errors.BackupValidationError):
        svc.restore_backup(candidate.id, confirmed=True)

    # The live DB was not touched (business data; the rejected restore
    # still took its mandatory safety backup first, which adds
    # operational history/audit rows by design).
    assert _table_counts(db_file, _BUSINESS_TABLES) == counts_before


def test_restore_unknown_id(backup_svc):
    svc, _ = backup_svc
    with pytest.raises(errors.NotFoundError):
        svc.restore_backup(999_999, confirmed=True)


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------
def test_retention_defaults_and_set(backup_svc):
    svc, _ = backup_svc
    assert svc.get_retention() == (30, 14)
    svc.set_retention(7, 3)
    assert svc.get_retention() == (7, 3)
    with pytest.raises(errors.ValidationError):
        svc.set_retention(0, 3)
    with pytest.raises(errors.ValidationError):
        svc.set_retention(3, -1)


def test_prune_keeps_newest_and_respects_keep_count(backup_svc):
    svc, tmp_path = backup_svc
    made = [svc.run_backup("manual") for _ in range(4)]
    svc.set_retention(2, 14)

    report = svc.prune_backups()

    remaining = sorted(
        p.name for p in (tmp_path / "backups").glob("PetrolPump_Backup_*.sqlite")
    )
    assert len(remaining) == 2
    assert report.kept_local == 2
    assert len(report.deleted_local) == 2
    # The newest known-good backup is never deleted.
    newest = max(made, key=lambda r: r.id)
    assert newest.filename in remaining
    # History rows survive pruning (audit trail).
    assert len(svc.list_history()) >= 4


def test_prune_never_deletes_recent_safety_backup(backup_svc):
    svc, tmp_path = backup_svc
    old_manual = svc.run_backup("manual")
    safety = svc.run_backup("safety")
    newest_manual = svc.run_backup("manual")
    svc.set_retention(1, 14)

    report = svc.prune_backups()

    backup_dir = tmp_path / "backups"
    # Newest manual kept by the keep-count; safety kept by the 7-day rule;
    # the older manual is pruned.
    assert (backup_dir / newest_manual.filename).is_file()
    assert (backup_dir / safety.filename).is_file()
    assert not (backup_dir / old_manual.filename).exists()
    assert old_manual.filename in report.deleted_local
    assert safety.filename not in report.deleted_local


# ---------------------------------------------------------------------------
# scheduling helpers
# ---------------------------------------------------------------------------
def test_scheduled_backup_due_disabled_by_default(backup_svc):
    svc, _ = backup_svc
    assert svc.scheduled_backup_due() is False


def test_scheduled_backup_due_and_mark(backup_svc):
    svc, _ = backup_svc
    with svc.transaction() as conn:
        BackupRepository.set_setting(conn, "backup.periodic_minutes", "60")
    assert svc.scheduled_backup_due() is True  # never ran

    svc.mark_scheduled_backup()
    assert svc.scheduled_backup_due() is False

    # Backdate the last run beyond the interval -> due again.
    from datetime import datetime, timedelta, timezone

    old = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    with svc.transaction() as conn:
        BackupRepository.set_setting(conn, "backup.last_scheduled_at_utc", old)
    assert svc.scheduled_backup_due() is True


def test_get_setting_raw_accessor(backup_svc):
    svc, _ = backup_svc
    # Unset key -> None (the day-close hook treats "not '0'" as enabled).
    assert svc.get_setting("backup.on_day_close") is None
    with svc.transaction() as conn:
        BackupRepository.set_setting(conn, "backup.on_day_close", "0")
    assert svc.get_setting("backup.on_day_close") == "0"


def test_run_backup_succeeds_on_pre_015_database(backup_svc, db):
    """Pre-upgrade safety-backup deadlock regression test.

    The pre-upgrade safety backup runs in bootstrap() BEFORE migrate()
    applies 015, so on a v14 database ``backup_history.trigger`` does not
    exist. The history INSERT must be schema-tolerant: omit the column
    when absent instead of raising OperationalError (which used to make
    the upgrade impossible).
    """
    svc, tmp_path = backup_svc
    db.execute("ALTER TABLE backup_history DROP COLUMN trigger")
    db.commit()

    record = svc.run_backup("safety")

    assert record.status == "validated"
    assert record.trigger is None  # column absent -> not recorded
    assert (tmp_path / "backups" / record.filename).is_file()
    # (The trigger-is-recorded path is covered by every other test, which
    # run against the fully migrated schema.)


def test_set_setting_contract_parity(backup_svc):
    """Published contract: svc.get_setting(key) / svc.set_setting(key, value)."""
    svc, _ = backup_svc
    assert svc.get_setting("backup.periodic_minutes") is None
    svc.set_setting("backup.periodic_minutes", "45")
    assert svc.get_setting("backup.periodic_minutes") == "45"


def test_status_text_keys_cover_all_statuses():
    for status, key in {
        "requested": "backup.status.requested",
        "snapshot_created": "backup.status.snapshot_created",
        "validated": "backup.status.validated",
        "copied_to_drive": "backup.status.copied_to_drive",
        "sync_confirmed": "backup.status.sync_confirmed",
        "cloud_unavailable": "backup.status.cloud_unavailable",
        "failed": "backup.status.failed",
    }.items():
        record = BackupRecord(
            id=1,
            requested_at_utc="2026-10-15T00:00:00.000Z",
            filename="x.sqlite",
            destination="local",
            status=status,
            size_bytes=None,
            sha256=None,
            verified_at_utc=None,
            error=None,
            trigger="manual",
        )
        assert BackupService.status_text_key(record) == key
