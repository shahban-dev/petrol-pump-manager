"""Phase-8 acceptance tests (Tests 14–15 and the §9 acceptance paths).

Acceptance criteria for the verified backup/restore system (design plan
§9). Every test uses a FRESH temporary database plus temporary
backup/drive folders — the live database is never touched.

This module complements ``app/tests/test_phase8_backup.py`` (unit-level
pipeline coverage: 23 tests). Nothing here duplicates those tests; these
drive the full acceptance paths:

* Test 14 — backup while the DB is in WAL mode with an open writer
  connection holding committed data;
* Test 15 — restore round-trip with computed balances
  (``v_outstanding_receivables``, ``v_stock_position``, cash position)
  identical to pre-mutation values;
* retention guarantees (oldest pruned, newest kept, last valid backup
  never deleted even with keep counts of 0);
* honest cloud statuses across the full pipeline (``sync_confirmed``
  never appears without a real sync signal);
* Drive-folder-unavailable behaviour (local backup stands, app keeps
  working);
* the pre-upgrade safety backup on a v14 database (regression pin for
  the fixed bootstrap deadlock), then migration 015 applies;
* the day-close auto-backup hook (enabled → ``day_close`` row, disabled
  → none) through the real ``BusinessDayService.close_day``;
* restore confirmation (``confirmed=False`` changes nothing);
* ``en.json`` / ``ur.json`` key parity.

Money is integer paisa everywhere; no float is used.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from app import config
from app.database.connection import DatabaseManager
from app.repositories.backup_repository import BackupRepository
from app.services import errors
from app.services.backup_service import BackupService
from app.tests.conftest import (
    close_prior_day,
    pkt_clock_utc_iso,
    record_reset_interval,
)


# ---------------------------------------------------------------------------
# fixtures & helpers
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _temp_backup_dir(monkeypatch, tmp_path: Path):
    """Redirect the *default* backup dir into tmp for every test here.

    ``BackupService`` falls back to ``config.BACKUP_DIR`` when no explicit
    ``backup_dir`` is given — the day-close hook constructs its service
    exactly that way, so without this redirect the hook's files would land
    in the real backup folder. The database itself is always the temp DB
    (the hook is bound to the service's own ``DatabaseManager``).
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
    return svc, tmp_path


def _backup_dir_of(tmp_path: Path) -> Path:
    return tmp_path / "backups"


#: Operational tables excluded from business-data equality checks: the
#: snapshot is taken mid-pipeline (before the final status UPDATEs), and a
#: restore always takes a safety backup first, so these rows legitimately
#: differ — the *business* data must be identical.
_NON_BUSINESS_TABLES = frozenset(
    {"audit_log", "backup_history", "schema_migrations", "sqlite_sequence"}
)


def _business_snapshot(db_file: Path) -> dict:
    """Deterministic dump of every business table (name → sorted rows)."""
    conn = sqlite3.connect(str(db_file))
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                " ORDER BY name"
            ).fetchall()
        ]
        snapshot = {}
        for table in tables:
            if table in _NON_BUSINESS_TABLES:
                continue
            columns = [
                col[1] for col in conn.execute(f"PRAGMA table_info({table})")
            ]
            rows = conn.execute(
                f"SELECT {', '.join(columns)} FROM {table} ORDER BY rowid"
            ).fetchall()
            snapshot[table] = [tuple(row) for row in rows]
        return snapshot
    finally:
        conn.close()


def _balances(db_file: Path) -> dict:
    """Computed balances: customer outstanding, stock, cash, closings."""
    conn = sqlite3.connect(str(db_file))
    try:

        def rows(sql: str) -> list:
            return [tuple(r) for r in conn.execute(sql).fetchall()]

        return {
            "outstanding": rows(
                "SELECT customer_code, outstanding_paisa"
                " FROM v_outstanding_receivables ORDER BY customer_code"
            ),
            "stock": rows(
                "SELECT t.code, fp.code, v.cumulative_ml"
                " FROM v_stock_position v"
                " JOIN tanks t ON t.id = v.tank_id"
                " JOIN fuel_products fp ON fp.id = v.fuel_product_id"
                " ORDER BY t.code, fp.code"
            ),
            "cash": rows(
                "SELECT business_date, opening_cash_paisa, receipts_paisa,"
                " payments_paisa, expected_paisa, physical_cash_paisa"
                " FROM v_daily_cash_position ORDER BY business_date"
            ),
            "closings": rows(
                "SELECT business_date, closing_revision, opening_cash_paisa,"
                " closing_cash_paisa, petrol_litres_ml, diesel_litres_ml"
                " FROM business_day_closings"
                " ORDER BY business_date, closing_revision"
            ),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Acceptance Test 14 — backup under WAL with an open writer
# ---------------------------------------------------------------------------
def test_acceptance_14_backup_under_wal_with_open_writer(
    backup_svc, phase3_services, db_file: Path
):
    """A backup taken while the WAL-mode DB has an open writer holding
    committed data must be a consistent, valid snapshot (the SQLite online
    backup API — never a raw file copy of a WAL database)."""
    svc, tmp_path = backup_svc
    close_prior_day(
        phase3_services,
        "2026-10-14",
        {"1 PG+": "100", "2 PG+": "100", "3 HSD": "100", "4 HSD": "100"},
    )
    phase3_services.days.open_day("2026-10-15", opening_cash_paisa=2_000_000)

    manager = DatabaseManager(db_file)
    writer = manager.connect()
    assert writer.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    # Committed (but un-checkpointed) data held by the still-open writer.
    writer.execute(
        "INSERT INTO business_day_events"
        " (business_date, event_type, actor, reason)"
        " VALUES ('2026-10-15', 'note', 'system',"
        " 'wal-writer committed note')"
    )
    writer.commit()
    # Live counts just before the backup runs.
    probe = manager.connect()
    try:
        days_before = probe.execute(
            "SELECT COUNT(*) FROM business_days"
        ).fetchone()[0]
        audit_before = probe.execute(
            "SELECT COUNT(*) FROM audit_log"
        ).fetchone()[0]
    finally:
        probe.close()
    try:
        record = svc.run_backup("manual")
    finally:
        writer.close()

    assert record.status == "validated"
    backup_path = _backup_dir_of(tmp_path) / record.filename
    assert backup_path.is_file()

    # The backup file opens read-only and is fully consistent.
    ro = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        assert ro.execute("PRAGMA integrity_check").fetchone()[0].lower() == "ok"
        assert ro.execute("PRAGMA foreign_key_check").fetchall() == []
        backup_days = ro.execute(
            "SELECT COUNT(*) FROM business_days"
        ).fetchone()[0]
        backup_audit = ro.execute(
            "SELECT COUNT(*) FROM audit_log"
        ).fetchone()[0]
        assert backup_days == days_before
        # The snapshot is taken mid-pipeline: it contains the pipeline's
        # own 'requested' history row (one audit row) but not the status
        # UPDATEs that follow the snapshot — so exactly pre_count + 1.
        assert backup_audit == audit_before + 1
        # The writer's committed row made it into the snapshot.
        note = ro.execute(
            "SELECT reason FROM business_day_events"
            " WHERE reason = 'wal-writer committed note'"
        ).fetchone()
        assert note is not None
    finally:
        ro.close()
    assert svc.validate_backup_file(backup_path).ok


# ---------------------------------------------------------------------------
# Acceptance Test 15 — restore round-trip, balances identical
# ---------------------------------------------------------------------------
def _seed_populated_two_days(svc_ns, db_file: Path, svc: BackupService) -> None:
    """Two populated days on the temp DB: closed day 1, open day 2 with a
    delivery, metered sales, a credit ticket and a partial payment.

    The day-close auto-backup hook is disabled for the seed so the
    pre-mutation snapshot is exactly the data below (the hook itself is
    covered by the dedicated day-close tests).
    """
    svc.set_setting("backup.on_day_close", "0")
    close_prior_day(
        svc_ns,
        "2026-10-14",
        {"1 PG+": "100", "2 PG+": "100", "3 HSD": "100", "4 HSD": "100"},
        opening_stock_litres="5000",
    )
    svc_ns.days.open_day("2026-10-15", opening_cash_paisa=2_000_000)
    # Stock in (day 1 set 5000 L petrol opening stock; diesel arrives here).
    svc_ns.deliveries.record_delivery(
        supplier="PSO Depot",
        fuel_product="PETROL",
        tank="TANK-PETROL",
        business_date="2026-10-15",
        quantity_litres=Decimal("2000"),
        unit_cost_pkr=Decimal("260"),
        amount_paid_pkr=Decimal("0"),
        occurred_at_utc=pkt_clock_utc_iso("2026-10-15", 9),
    )
    svc_ns.deliveries.record_delivery(
        supplier="PSO Depot",
        fuel_product="DIESEL",
        tank="TANK-DIESEL",
        business_date="2026-10-15",
        quantity_litres=Decimal("3000"),
        unit_cost_pkr=Decimal("250"),
        amount_paid_pkr=Decimal("0"),
        occurred_at_utc=pkt_clock_utc_iso("2026-10-15", 9),
    )
    # Metered sales: 20 L per nozzle.
    for code in ("1 PG+", "2 PG+", "3 HSD", "4 HSD"):
        svc_ns.nozzles.record_reading("2026-10-15", code, "120")
    # Credit ticket (40 L) + partial cash payment.
    customer = svc_ns.credit.create_customer("Acme Transport")
    svc_ns.credit.issue_ticket(
        customer.id,
        "2026-10-15",
        [
            {
                "fuel_product_code": "PETROL",
                "nozzle_code": None,
                "quantity_litres": Decimal("40"),
            }
        ],
        occurred_at_utc=pkt_clock_utc_iso("2026-10-15", 10),
    )
    svc_ns.payments.record_payment(
        customer.id,
        "2026-10-15",
        500_000,
        occurred_at_utc=pkt_clock_utc_iso("2026-10-15", 12),
    )
    svc_ns.sales.post_day_sales("2026-10-15")


def test_acceptance_15_restore_round_trip_balances(
    backup_svc, phase4_services, db_file: Path
):
    """Restore round-trip: back up a populated DB, mutate the live DB, then
    restore — key records and computed balances must be identical to the
    pre-mutation values, and a safety backup of the pre-restore DB must
    have been taken automatically."""
    svc, tmp_path = backup_svc
    _seed_populated_two_days(phase4_services, db_file, svc)

    candidate = svc.run_backup("manual")
    pre_balances = _balances(db_file)
    pre_snapshot = _business_snapshot(db_file)
    # The seed is non-trivial: outstanding, stock and closings all present.
    assert pre_balances["outstanding"]
    assert pre_balances["stock"]
    assert pre_balances["closings"]

    # --- mutate the live DB after the backup: every balance moves ---
    second = phase4_services.credit.create_customer("Beta Traders")
    phase4_services.credit.issue_ticket(
        second.id,
        "2026-10-15",
        [
            {
                "fuel_product_code": "DIESEL",
                "nozzle_code": None,
                "quantity_litres": Decimal("20"),
            }
        ],
        occurred_at_utc=pkt_clock_utc_iso("2026-10-15", 14),
    )
    phase4_services.deliveries.record_delivery(
        supplier="PSO Depot",
        fuel_product="PETROL",
        tank="TANK-PETROL",
        business_date="2026-10-15",
        quantity_litres=Decimal("500"),
        unit_cost_pkr=Decimal("260"),
        amount_paid_pkr=Decimal("0"),
        occurred_at_utc=pkt_clock_utc_iso("2026-10-15", 15),
    )
    from app.services.expense_service import ExpenseService

    ExpenseService(DatabaseManager(db_file)).record_expense(
        business_date="2026-10-15",
        category="Utilities",
        amount_paisa=50_000,
        occurred_at_utc=pkt_clock_utc_iso("2026-10-15", 16),
    )
    assert _balances(db_file) != pre_balances  # sanity: mutation moved them

    # --- restore ---
    known_ids = {r.id for r in svc.list_history()}
    report = svc.restore_backup(candidate.id, confirmed=True, actor="system")

    assert report.verified
    assert report.restart_required
    assert report.safety_backup_id != candidate.id
    # The safety backup was taken automatically during the restore.
    assert report.safety_backup_id not in known_ids
    assert report.safety_backup_filename.startswith("PetrolPump_Safety_")
    safety_path = _backup_dir_of(tmp_path) / report.safety_backup_filename
    assert safety_path.is_file()
    assert svc.validate_backup_file(safety_path).ok

    # Key records and computed balances are identical to pre-mutation.
    assert _balances(db_file) == pre_balances
    assert _business_snapshot(db_file) == pre_snapshot


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------
def test_retention_prune_oldest_newest_kept_and_zero_keep_safe(
    backup_svc, monkeypatch
):
    """Oldest backups are pruned beyond the keep counts, the newest are
    kept — and the last valid backup is NEVER deleted even when the keep
    counts are 0."""
    svc, tmp_path = backup_svc
    drive = tmp_path / "drive"
    drive.mkdir()
    svc.set_drive_folder(str(drive))
    for _ in range(3):
        svc.run_backup("manual")
    rows_before = len(svc.list_history())

    svc.set_retention(2, 1)
    report = svc.prune_backups()

    backup_dir = _backup_dir_of(tmp_path)
    local_files = sorted(
        p.name for p in backup_dir.glob("PetrolPump_Backup_*.sqlite")
    )
    drive_files = sorted(p.name for p in drive.glob("PetrolPump_Backup_*.sqlite"))
    assert len(local_files) == 2
    assert len(drive_files) == 1
    assert report.kept_local == 2
    assert report.kept_drive == 1
    assert len(report.deleted_local) == 1
    assert len(report.deleted_drive) == 2
    newest_local = max(
        svc.list_history(destination="local"), key=lambda r: r.id
    )
    assert newest_local.filename in local_files
    # History rows are never pruned (audit trail).
    assert len(svc.list_history()) == rows_before

    # Keep counts of 0: the last valid backup per destination still
    # survives. (set_retention rejects 0, so force the raw values the way
    # a hand-edited setting would.)
    monkeypatch.setattr(svc, "get_retention", lambda: (0, 0))
    report2 = svc.prune_backups()

    local_files2 = sorted(
        p.name for p in backup_dir.glob("PetrolPump_Backup_*.sqlite")
    )
    drive_files2 = sorted(p.name for p in drive.glob("PetrolPump_Backup_*.sqlite"))
    newest_drive = max(
        svc.list_history(destination="gdrive"), key=lambda r: r.id
    )
    assert local_files2 == [newest_local.filename]
    assert drive_files2 == [newest_drive.filename]
    assert report2.kept_local == 1
    assert report2.kept_drive == 1
    assert len(svc.list_history()) == rows_before


# ---------------------------------------------------------------------------
# Honest statuses across the full pipeline
# ---------------------------------------------------------------------------
def test_honest_statuses_full_pipeline_and_i18n_keys(backup_svc):
    """With a Drive folder configured, the pipeline records
    requested → snapshot_created → validated → copied_to_drive; the status
    is NEVER 'sync_confirmed' without a real sync signal, and the UI key
    resolves honestly in both languages."""
    svc, tmp_path = backup_svc
    drive = tmp_path / "drive"
    drive.mkdir()
    svc.set_drive_folder(str(drive))

    record = svc.run_backup("manual")
    assert record.status == "validated"

    rows = svc.list_history()
    by_destination = {}
    for row in rows:
        by_destination.setdefault(row.destination, []).append(row.status)
    # The local leg completed validation; the Drive leg copied honestly.
    assert "validated" in by_destination.get("local", [])
    gdrive_rows = svc.list_history(destination="gdrive")
    assert len(gdrive_rows) == 1
    assert gdrive_rows[0].status == "copied_to_drive"
    assert gdrive_rows[0].filename == record.filename
    assert gdrive_rows[0].sha256 == record.sha256
    # No sync signal exists (Drive for desktop exposes none to a plain
    # folder destination), so 'sync_confirmed' must never appear.
    assert all(r.status != "sync_confirmed" for r in rows)
    assert not svc._drive_sync_confirmed(drive / record.filename)
    # Even an in-progress upload marker proves sync has NOT finished —
    # still no confirmation.
    (drive / (record.filename + ".tmp.driveupload")).write_text("uploading")
    try:
        assert not svc._drive_sync_confirmed(drive / record.filename)
    finally:
        (drive / (record.filename + ".tmp.driveupload")).unlink()

    # The UI key resolves in both languages with honest wording (§11.15).
    key = BackupService.status_text_key(gdrive_rows[0])
    assert key == "backup.status.copied_to_drive"
    base = Path(config.__file__).resolve().parent / "localization"
    en = json.loads((base / "en.json").read_text(encoding="utf-8"))
    ur = json.loads((base / "ur.json").read_text(encoding="utf-8"))
    assert en[key] and ur[key]
    assert "unconfirmed" in en[key].lower()  # never "backed up to cloud"
    assert en[key] != ur[key]


# ---------------------------------------------------------------------------
# Drive folder unavailable
# ---------------------------------------------------------------------------
def test_drive_folder_nonexistent_path_local_backup_stands(backup_svc):
    """A Drive folder that does not exist (e.g. Drive signed out or the
    path removed) must not break backups: the local backup still validates,
    the gdrive leg is recorded as 'cloud_unavailable', and the app keeps
    working."""
    svc, tmp_path = backup_svc
    # Point at a path that never existed — bypassing set_drive_folder's
    # existence check, the way a stale saved setting would.
    bad = tmp_path / "no-such-drive-folder"
    with svc.transaction() as conn:
        BackupRepository.set_setting(conn, "backup.drive_folder", str(bad))
    assert not bad.exists()
    assert not svc.drive_available()

    record = svc.run_backup("manual")
    assert record.status == "validated"

    gdrive_rows = svc.list_history(destination="gdrive")
    assert len(gdrive_rows) == 1
    assert gdrive_rows[0].status == "cloud_unavailable"
    assert (
        BackupService.status_text_key(gdrive_rows[0])
        == "backup.status.cloud_unavailable"
    )

    # The app keeps working: settings read back, history is queryable, and
    # the next backup succeeds the same way.
    assert svc.get_drive_folder() == bad
    assert svc.get_setting("backup.drive_folder") == str(bad)
    second = svc.run_backup("manual")
    assert second.status == "validated"
    assert len(svc.list_history(destination="gdrive")) == 2
    assert all(
        r.status == "cloud_unavailable"
        for r in svc.list_history(destination="gdrive")
    )


# ---------------------------------------------------------------------------
# Pre-upgrade safety backup (bootstrap deadlock regression pin)
# ---------------------------------------------------------------------------
def test_pre_upgrade_safety_backup_then_migrate_015(tmp_path, monkeypatch):
    """On a v14 database (migrations 001–014 applied), the bootstrap
    pre-migration safety-backup path must succeed — the history INSERT is
    schema-tolerant of the missing ``backup_history.trigger`` column
    (migration 015) — and migration 015 must then apply.

    Regression pin for the fixed deadlock that used to make the upgrade
    impossible (OperationalError → AppStartupError before migrate()).
    """
    from app.bootstrap import bootstrap
    from app.database import migrator

    v14_file = tmp_path / "v14.sqlite3"
    raw = sqlite3.connect(str(v14_file))
    try:
        for version, path in migrator._migration_files():
            if version <= 14:
                migrator._apply_one(raw, version, path)
        raw.commit()
    finally:
        raw.close()

    check = sqlite3.connect(str(v14_file))
    try:
        assert migrator.get_schema_version(check) == 14
        columns = [
            col[1] for col in check.execute("PRAGMA table_info(backup_history)")
        ]
        assert "trigger" not in columns  # migration 015 not yet applied
    finally:
        check.close()

    # Redirect the safety backup (constructed with default paths inside
    # bootstrap) at the temp v14 database and a temp backup folder.
    backup_dir = tmp_path / "backups"
    temp_manager = DatabaseManager(v14_file)
    real_service = BackupService

    class _TempBackupService(real_service):
        def __init__(self, db_manager=None, actor=None, **kwargs):
            super().__init__(temp_manager, actor=actor, backup_dir=backup_dir)

    monkeypatch.setattr(
        "app.services.backup_service.BackupService", _TempBackupService
    )

    result = bootstrap(v14_file)

    assert result.schema_version == 15
    verify = sqlite3.connect(str(v14_file))
    try:
        assert migrator.get_schema_version(verify) == 15
    finally:
        verify.close()
    safeties = list(backup_dir.glob("PetrolPump_Safety_*.sqlite"))
    assert len(safeties) == 1
    # The v14 safety backup is itself a valid snapshot.
    verifier = real_service(temp_manager, actor="system", backup_dir=backup_dir)
    assert verifier.validate_backup_file(safeties[0]).ok


# ---------------------------------------------------------------------------
# Day-close hook
# ---------------------------------------------------------------------------
def _close_second_day(phase3_services) -> None:
    """Open and close a second day with no meter movement (meter resets
    keep every interval unknown, so nothing posts and no stock is needed)."""
    phase3_services.days.open_day("2026-10-15", opening_cash_paisa=2_000_000)
    for code in ("1 PG+", "2 PG+", "3 HSD", "4 HSD"):
        record_reset_interval(phase3_services, "2026-10-15", code, "100")
    phase3_services.days.close_day("2026-10-15", physical_cash_paisa=2_000_000)


def _day_close_rows(svc: BackupService) -> list:
    return [r for r in svc.list_history() if r.trigger == "day_close"]


def test_day_close_hook_backs_up_when_enabled(
    backup_svc, phase3_services, db_file: Path
):
    """With ``backup.on_day_close='1'``, the real ``close_day`` writes a
    ``day_close`` backup row (and file) after the commit."""
    svc, tmp_path = backup_svc
    close_prior_day(
        phase3_services,
        "2026-10-14",
        {"1 PG+": "100", "2 PG+": "100", "3 HSD": "100", "4 HSD": "100"},
    )
    svc.set_setting("backup.on_day_close", "1")
    before = _day_close_rows(svc)

    _close_second_day(phase3_services)

    after = _day_close_rows(svc)
    assert len(after) == len(before) + 1
    new_row = max(after, key=lambda r: r.id)
    assert new_row.status == "validated"
    assert new_row.destination == "local"
    assert (_backup_dir_of(tmp_path) / new_row.filename).is_file()


def test_day_close_hook_skipped_when_disabled(
    backup_svc, phase3_services, db_file: Path
):
    """With ``backup.on_day_close='0'``, closing a day writes no backup."""
    svc, tmp_path = backup_svc
    close_prior_day(
        phase3_services,
        "2026-10-14",
        {"1 PG+": "100", "2 PG+": "100", "3 HSD": "100", "4 HSD": "100"},
    )
    svc.set_setting("backup.on_day_close", "0")
    before = _day_close_rows(svc)

    _close_second_day(phase3_services)

    assert _day_close_rows(svc) == before
    # ...and the close itself still succeeded.
    day = phase3_services.days.get_day("2026-10-15")
    assert day is not None and day.status == "closed"


# ---------------------------------------------------------------------------
# Restore confirmation
# ---------------------------------------------------------------------------
def test_restore_confirmation_required_changes_nothing(
    backup_svc, phase3_services, db_file: Path
):
    """``restore_backup(confirmed=False)`` raises
    ``RestoreConfirmationRequired`` and changes nothing — no safety backup,
    no history rows, no business-data change."""
    svc, tmp_path = backup_svc
    close_prior_day(
        phase3_services,
        "2026-10-14",
        {"1 PG+": "100", "2 PG+": "100", "3 HSD": "100", "4 HSD": "100"},
    )
    candidate = svc.run_backup("manual")

    before = _business_snapshot(db_file)
    history_before = [(r.id, r.status, r.trigger) for r in svc.list_history()]

    with pytest.raises(errors.RestoreConfirmationRequired):
        svc.restore_backup(candidate.id, confirmed=False)

    assert _business_snapshot(db_file) == before
    assert [(r.id, r.status, r.trigger) for r in svc.list_history()] == (
        history_before
    )


# ---------------------------------------------------------------------------
# i18n parity
# ---------------------------------------------------------------------------
def test_i18n_key_parity():
    """``en.json`` and ``ur.json`` carry exactly the same keys (1039 each
    after the Phase-8 additions) — no untranslated lookup can KeyError."""
    from app import localization

    base = Path(localization.__file__).resolve().parent
    en = json.loads((base / "en.json").read_text(encoding="utf-8"))
    ur = json.loads((base / "ur.json").read_text(encoding="utf-8"))
    assert set(en) == set(ur)
    assert len(en) == 1039
    assert len(ur) == 1039


# ---------------------------------------------------------------------------
# temp-file hygiene (sidecar regression pin)
# ---------------------------------------------------------------------------
def test_backup_leaves_no_temp_files_or_sidecars(backup_svc):
    """A successful backup leaves no ``.partial-*`` temp file and no
    ``-shm``/``-wal``/``-journal`` sidecars behind in the backup folder."""
    svc, tmp_path = backup_svc
    record = svc.run_backup("manual")
    assert record.status == "validated"

    backup_dir = _backup_dir_of(tmp_path)
    leftovers = [
        p.name
        for p in backup_dir.iterdir()
        if ".partial-" in p.name
        or p.name.endswith(("-shm", "-wal", "-journal"))
    ]
    assert leftovers == []
    assert (backup_dir / record.filename).is_file()
    # The snapshot is a plain rollback-journal file: the read-only
    # validation open cannot grow WAL sidecars next to it.
    check = sqlite3.connect(str(backup_dir / record.filename))
    try:
        assert (
            check.execute("PRAGMA journal_mode").fetchone()[0].lower()
            == "delete"
        )
    finally:
        check.close()
