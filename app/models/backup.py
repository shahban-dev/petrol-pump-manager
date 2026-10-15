"""Phase-8 data transfer objects: backup history, validation, restore, pruning.

DTOs only — no SQL, no business logic (design plan §1). Sizes are integer
byte counts; no float is used anywhere in the backup pipeline.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass(frozen=True)
class BackupRecord:
    """One row of ``backup_history`` — one leg of one backup run.

    A single :meth:`BackupService.run_backup` call produces one ``local``
    row; when a Drive folder is configured it additionally produces one
    ``gdrive`` row (status ``copied_to_drive`` / ``sync_confirmed`` /
    ``cloud_unavailable`` / ``failed``).
    """

    id: int
    requested_at_utc: str
    filename: str
    destination: str  # 'local' | 'gdrive'
    status: str  # 'requested' | 'snapshot_created' | 'validated' |
                 # 'copied_to_drive' | 'sync_confirmed' |
                 # 'cloud_unavailable' | 'failed'
    size_bytes: int | None
    sha256: str | None
    verified_at_utc: str | None
    error: str | None
    trigger: str | None  # 'manual' | 'day_close' | 'shutdown' |
                         # 'scheduled' | 'safety' (migration 015)

    @staticmethod
    def from_row(row: sqlite3.Row) -> "BackupRecord":
        """Build from a ``backup_history`` row (positional access avoided:
        column reads are by name so schema order never matters)."""
        return BackupRecord(
            id=int(row["id"]),
            requested_at_utc=row["requested_at_utc"],
            filename=row["filename"],
            destination=row["destination"],
            status=row["status"],
            size_bytes=(
                int(row["size_bytes"]) if row["size_bytes"] is not None else None
            ),
            sha256=row["sha256"],
            verified_at_utc=row["verified_at_utc"],
            error=row["error"],
            trigger=row["trigger"] if "trigger" in row.keys() else None,
        )


@dataclass(frozen=True)
class ValidationReport:
    """Result of validating a backup file (snapshot or restore candidate)."""

    ok: bool
    # Each entry: {"name": str, "ok": bool, "detail": str} — display-safe,
    # never contains file contents or personal data.
    checks: list = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class RestoreReport:
    """Result of :meth:`BackupService.restore_backup`."""

    restored_backup_id: int
    candidate_filename: str
    safety_backup_id: int
    # The pre-restore safety backup's history row lives in the *replaced*
    # database, so after a restore it is no longer queryable — but its
    # FILE always survives in the backup folder under this name.
    safety_backup_filename: str
    verified: bool
    checks: list = field(default_factory=list)
    restart_required: bool = True
    notes: str = ""


@dataclass(frozen=True)
class PruneReport:
    """Result of :meth:`BackupService.prune_backups`."""

    deleted_local: list = field(default_factory=list)
    deleted_drive: list = field(default_factory=list)
    kept_local: int = 0
    kept_drive: int = 0
    # Non-fatal per-file problems (a failed unlink never aborts pruning).
    errors: list = field(default_factory=list)
