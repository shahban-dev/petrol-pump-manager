"""Application bootstrap: safe, ordered startup of the local database.

Startup order (design plan §1, §10 "fail safe"):
    ensure_dirs() -> connect (pragma-verified) -> migrate() ->
    verify_schema() -> integrity check -> clock-skew check -> return.

On ANY failure an AppStartupError is raised with a user-friendly message.
The database is never deleted, overwritten, or "repaired" here.

NOTE ON PARALLEL DEVELOPMENT: the database layer (app.database.connection,
app.database.migrator, app.database.integrity_checks) is being built by a
sister agent in parallel. The canonical names below are imported first; if
they are not importable yet, minimal local shims are used SO THAT PHASE 1
CAN BE SMOKE-TESTED. This fallback is TEMPORARY and must be deleted once
the real database layer lands — see _USING_SHIM and the report to the
build coordinator.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from app import config
from app.config import AppStartupError
from app.localization import tr

# ---------------------------------------------------------------------------
# Database-layer imports (canonical contract). Fall back to minimal shims
# only if the database-layer agent has not landed its modules yet.
# ---------------------------------------------------------------------------
_USING_SHIM = False

try:  # canonical imports -------------------------------------------------
    from app.database.connection import DatabaseManager, DatabaseError
    from app.database.migrator import (
        CURRENT_SCHEMA_VERSION,
        MigrationError,
        get_schema_version,
        migrate,
        verify_schema,
    )
    from app.database import integrity_checks
except ImportError:  # TEMPORARY minimal shim -------------------------------
    _USING_SHIM = True

    class DatabaseError(Exception):
        """Shim for app.database.connection.DatabaseError."""

    class MigrationError(Exception):
        """Shim for app.database.migrator.MigrationError."""

    CURRENT_SCHEMA_VERSION = 0

    class DatabaseManager:
        """Shim DatabaseManager: pragma-verified sqlite3 connections."""

        def __init__(self, db_path: Path) -> None:
            self.db_path = Path(db_path)

        def connect(self) -> sqlite3.Connection:
            try:
                conn = sqlite3.connect(str(self.db_path), timeout=30.0)
            except sqlite3.Error as exc:
                raise DatabaseError(f"Cannot open database: {exc}") from exc
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            jm = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            fk = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
            if jm.lower() != "wal" or fk != 1:
                conn.close()
                raise DatabaseError(
                    f"Pragma verification failed (journal_mode={jm}, "
                    f"foreign_keys={fk})."
                )
            return conn

        def __enter__(self) -> sqlite3.Connection:
            self._conn = self.connect()
            return self._conn

        def __exit__(self, *args: Any) -> None:
            conn = getattr(self, "_conn", None)
            if conn is not None:
                conn.close()

    def migrate(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, "
            "applied_at_utc TEXT NOT NULL, "
            "description TEXT)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations "
            "(version, applied_at_utc, description) VALUES (0, "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'shim baseline')"
        )
        conn.commit()

    def get_schema_version(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def verify_schema(conn: sqlite3.Connection) -> list:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = [t for t in ("schema_migrations",) if t not in tables]
        return [f"missing table: {t}" for t in missing]

    class _IntegrityChecks:
        @staticmethod
        def check_integrity(conn: sqlite3.Connection) -> str:
            rows = conn.execute("PRAGMA integrity_check;").fetchall()
            if len(rows) == 1 and rows[0][0] == "ok":
                return "ok"
            return "; ".join(r[0] for r in rows)

        @staticmethod
        def check_foreign_keys(conn: sqlite3.Connection) -> list:
            return conn.execute("PRAGMA foreign_key_check;").fetchall()

    integrity_checks = _IntegrityChecks()


# ---------------------------------------------------------------------------
# Bootstrap result
# ---------------------------------------------------------------------------
@dataclass
class BootstrapResult:
    db_manager: Any
    db_path: Path
    schema_version: int
    pragmas: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Logging (file only; never logs financial row contents)
# ---------------------------------------------------------------------------
_LOGGER = logging.getLogger("petrol_pump_manager")
_LOG_CONFIGURED = False


def _configure_logging() -> None:
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        return
    _LOGGER.setLevel(logging.INFO)
    _LOGGER.propagate = False
    handler = RotatingFileHandler(
        config.LOG_DIR / config.LOG_FILENAME,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    _LOGGER.addHandler(handler)
    _LOG_CONFIGURED = True


# ---------------------------------------------------------------------------
# Clock-skew check (design plan §5.1, §11: warn, never rewrite history)
# ---------------------------------------------------------------------------
# (table, timestamp column) pairs holding the newest recorded machine time.
_TIMESTAMP_TABLES = (
    ("business_days", "created_at_utc"),
    ("nozzle_readings", "occurred_at_utc"),
    ("audit_log", "occurred_at_utc"),
)
_CLOCK_SKEW_TOLERANCE = timedelta(seconds=60)
_TS_FORMATS = ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(ts: str) -> datetime | None:
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def _newest_recorded_timestamp(conn: sqlite3.Connection) -> datetime | None:
    """Return the newest machine timestamp across the business tables, or None."""
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    newest: datetime | None = None
    for table, column in _TIMESTAMP_TABLES:
        if table not in existing:
            continue
        try:
            row = conn.execute(f"SELECT MAX({column}) FROM {table}").fetchone()
        except sqlite3.Error:
            continue
        if row and row[0]:
            parsed = _parse_utc(row[0])
            if parsed and (newest is None or parsed > newest):
                newest = parsed
    return newest


# ---------------------------------------------------------------------------
# Pre-migration safety backup (recovery.md §3: "Safety-backup-first rule")
# ---------------------------------------------------------------------------
def _maybe_safety_backup(
    conn: sqlite3.Connection, target: Path, db_existed: bool
) -> None:
    """Take an automatic pre-upgrade safety backup when migrations pending.

    ``db_existed`` is whether ``target`` was an existing, non-empty database
    file *before* ``bootstrap()`` connected (connecting initializes the
    file header, so the check must be pre-connect). On a fresh/empty DB it
    returns silently. Pending migrations are detected read-only via
    ``migrator.get_schema_version`` — migrator.py is NOT modified here.

    Respects the ``backup.on_upgrade`` setting (default enabled; a missing
    key counts as enabled), read with ``BackupRepository.get_setting`` —
    the repository is the canonical ``app_settings`` reader and works
    directly on the startup connection; no ``BackupService`` instance
    exists yet at this point in startup (it is constructed below, after
    the setting is read). Post-startup code can use the
    ``BackupService.get_setting`` convenience accessor instead. Fail safe:
    if the safety backup itself fails,
    raises ``AppStartupError`` — a database with real data is never
    migrated without the safety net. On success the backup result
    (filename/reference from the backup service) is logged.
    """
    if not db_existed:
        return  # fresh install: nothing to protect, skip silently
    try:
        current_version = int(get_schema_version(conn))
    except Exception as exc:
        _LOGGER.error("Could not read schema version: %s", exc)
        raise AppStartupError(
            f"Could not determine the database schema version: {exc}. "
            + _SUPPORT_FOOTER
        ) from exc
    if current_version >= CURRENT_SCHEMA_VERSION:
        return  # nothing pending: no backup needed
    try:
        from app.repositories.backup_repository import BackupRepository
        from app.services.backup_service import BackupService
    except ImportError as exc:
        raise AppStartupError(
            "A database upgrade is available, but the automatic safety "
            f"backup could not run (backup support unavailable: {exc}). "
            "The upgrade was NOT started. " + _SUPPORT_FOOTER
        ) from exc
    try:
        try:
            setting = BackupRepository.get_setting(conn, "backup.on_upgrade")
        except Exception as exc:
            # A database too old to have app_settings cannot report the
            # setting; the default (enabled) applies and the backup is
            # attempted — its own failure still fails safe below.
            _LOGGER.warning(
                "Could not read the 'backup.on_upgrade' setting (%s); "
                "assuming enabled.", exc
            )
            setting = None
        # Missing key counts as the default: enabled ('1').
        if (setting if setting is not None else "1") == "0":
            _LOGGER.warning(
                "Pre-upgrade safety backup is disabled by the "
                "'backup.on_upgrade' setting; migrating schema v%s -> v%s "
                "WITHOUT a safety net.",
                current_version,
                CURRENT_SCHEMA_VERSION,
            )
            return
        result = BackupService(actor="system").run_backup("safety")
    except AppStartupError:
        raise
    except Exception as exc:
        raise AppStartupError(
            f"Could not take the automatic pre-upgrade safety backup: {exc}. "
            "The upgrade was NOT started. " + _SUPPORT_FOOTER
        ) from exc
    _LOGGER.info(
        "Pre-upgrade safety backup taken before migrating schema v%s -> "
        "v%s: %s",
        current_version,
        CURRENT_SCHEMA_VERSION,
        result,
    )


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
_SUPPORT_FOOTER = (
    "The database has NOT been deleted or modified. "
    "Please restore from a backup or contact support."
)


def bootstrap(db_path: Path | None = None) -> BootstrapResult:
    """Run the ordered, fail-safe application startup sequence.

    Args:
        db_path: database file to open; defaults to config.db_path().

    Returns:
        BootstrapResult with the database manager, schema version, active
        pragmas, and any non-fatal startup warnings.

    Raises:
        AppStartupError: on ANY failure, with a user-friendly message.
    """
    target = Path(db_path) if db_path is not None else config.db_path()

    # 1. Directories first: logging needs LOG_DIR.
    try:
        config.ensure_dirs()
    except AppStartupError:
        raise
    _configure_logging()
    _LOGGER.info(
        "Starting %s v%s (database layer: %s)",
        config.APP_DISPLAY_NAME,
        config.APP_VERSION,
        "shim-fallback" if _USING_SHIM else "canonical",
    )
    if _USING_SHIM:
        _LOGGER.warning(
            "Database-layer modules not yet available; using TEMPORARY "
            "local shims for connection/migration/integrity. "
            "This must be replaced by the real database layer."
        )

    manager = DatabaseManager(target)
    conn: sqlite3.Connection | None = None
    try:
        # 2. Connect (pragma-verified).
        # Capture pre-connect existence: connect() initializes the file
        # header, so a post-connect size check cannot tell a fresh install
        # from a real database.
        db_existed = target.exists() and target.stat().st_size > 0
        try:
            conn = manager.connect()
        except Exception as exc:
            _LOGGER.error("Database connection failed for %s: %s", target, exc)
            raise AppStartupError(
                f"Could not open the database at '{target}': {exc}. "
                + _SUPPORT_FOOTER
            ) from exc
        _LOGGER.info("Database opened at %s", target)

        # 2b. Pre-migration safety backup (recovery.md §3). Only when a real
        # database already exists with pending migrations; never for a fresh
        # install. Fail safe: a failed safety backup raises AppStartupError
        # here, BEFORE migrate() can touch the data.
        _maybe_safety_backup(conn, target, db_existed)

        # 3. Migrations.
        try:
            migrate(conn)
        except Exception as exc:
            _LOGGER.error("Migration failed: %s", exc)
            raise AppStartupError(
                f"Database upgrade failed: {exc}. " + _SUPPORT_FOOTER
            ) from exc

        # 4. Schema verification.
        problems = verify_schema(conn)
        if problems:
            _LOGGER.error("Schema verification failed: %s", problems)
            raise AppStartupError(
                "The database schema is incomplete or incompatible: "
                + "; ".join(problems)
                + ". "
                + _SUPPORT_FOOTER
            )
        try:
            schema_version = int(get_schema_version(conn))
        except Exception as exc:
            _LOGGER.error("Could not read schema version: %s", exc)
            raise AppStartupError(
                f"Could not determine the database schema version: {exc}. "
                + _SUPPORT_FOOTER
            ) from exc
        _LOGGER.info("Schema version %s verified", schema_version)

        # 5. Integrity check.
        integrity = integrity_checks.check_integrity(conn)
        if integrity != "ok":
            _LOGGER.error("Integrity check failed: %s", integrity)
            raise AppStartupError(
                f"Database integrity check failed: {integrity}. "
                + _SUPPORT_FOOTER
            )
        fk_violations = integrity_checks.check_foreign_keys(conn)
        if fk_violations:
            _LOGGER.error("Foreign-key violations found: %d", len(fk_violations))
            raise AppStartupError(
                "Database foreign-key check found "
                f"{len(fk_violations)} violation(s). " + _SUPPORT_FOOTER
            )
        _LOGGER.info("Integrity check: ok; foreign keys: ok")

        pragmas = {
            "foreign_keys": str(conn.execute("PRAGMA foreign_keys;").fetchone()[0]),
            "journal_mode": str(conn.execute("PRAGMA journal_mode;").fetchone()[0]),
            "synchronous": str(conn.execute("PRAGMA synchronous;").fetchone()[0]),
        }

        # 6. Clock-skew check: warn, never alter data.
        warnings: list[str] = []
        now_utc = datetime.now(timezone.utc)
        newest = _newest_recorded_timestamp(conn)
        if newest is not None and now_utc < newest - _CLOCK_SKEW_TOLERANCE:
            warning = tr("warning.clock_skew").format(
                max_ts=newest.strftime("%Y-%m-%dT%H:%M:%SZ")
            )
            warnings.append(warning)
            _LOGGER.warning("Clock skew detected: %s", warning)

        _LOGGER.info("Startup complete: schema v%s", schema_version)
        return BootstrapResult(
            db_manager=manager,
            db_path=target,
            schema_version=schema_version,
            pragmas=pragmas,
            warnings=warnings,
        )
    finally:
        # The manager holds no open connection between calls; close ours.
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
