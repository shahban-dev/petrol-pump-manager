"""Backups page: run backups, browse history, restore, retention, Drive folder.

The page programs against the ``app.services.backup_service.BackupService``
contract (Phase 8): ``run_backup(trigger)``, ``list_history(limit)``,
``get_drive_folder()``, ``set_drive_folder(path)``, ``drive_available()``,
``validate_backup_file(path)``, ``restore_backup(backup_id, confirmed,
actor)``, ``get_retention()`` / ``set_retention(a, b)``, ``prune_backups()``,
``scheduled_backup_due()``, ``mark_scheduled_backup()`` and
``status_text_key(record)``.

Backup status wording is deliberately honest: a backup sitting in the
Google Drive folder is shown as unconfirmed until the service reports the
cloud sync itself succeeded. The UI never claims a backup (or a cloud
copy) succeeded before the service call returns, and restore requires
typing the exact backup file name — the confirmation protects against
accidentally restoring over the live database. The page never does
accounting math; timestamps are display-only conversions to
Asia/Karachi.
"""

from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.calculations.time import KARACHI_TZ
from app.localization import get_language, tr
from app.repositories.business_day_repository import BusinessDayRepository
from app.ui import _helpers as h

#: app_settings key used to persist the scheduled-backup interval when the
#: service does not expose its own getter/setter for it.
_INTERVAL_SETTING = "backup_interval_minutes"

#: trigger values -> localization keys.
_TRIGGER_KEYS = {
    "manual": "backups.trigger.manual",
    "scheduled": "backups.trigger.scheduled",
}

#: destination values -> localization keys.
_DESTINATION_KEYS = {
    "local": "backups.dest.local",
    "gdrive": "backups.dest.gdrive",
}

#: Table columns: (localization key, stretch factor).
_COLUMNS = (
    ("backups.col_time", 1),
    ("backups.col_file", 2),
    ("backups.col_destination", 1),
    ("backups.col_status", 2),
    ("backups.col_size", 0),
    ("backups.col_trigger", 1),
    ("backups.col_error", 2),
    ("backups.col_action", 0),
)


def _parse_utc_iso(value: str | None) -> datetime | None:
    """Parse a stored UTC timestamp (``YYYY-MM-DDTHH:MM:SS.SSSZ``)."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        iso = text[:-1] + "+00:00" if text.endswith("Z") else text
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_pkt(iso: str | None) -> str:
    """Stored UTC timestamp -> ``DD-MM-YYYY HH:MM`` in Asia/Karachi."""
    parsed = _parse_utc_iso(iso)
    if parsed is None:
        return tr("common.not_entered")
    return parsed.astimezone(KARACHI_TZ).strftime("%d-%m-%Y %H:%M")


def _format_bytes(size) -> str:
    """Byte count -> '1.5 MB'. Display-only; None -> '—'."""
    if size is None:
        return tr("common.not_entered")
    try:
        value = float(size)
    except (TypeError, ValueError):
        return tr("common.not_entered")
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value):,} {unit}"
            return f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} GB"


class BackupsPage(QWidget):
    """Backup management. Every control performs a real service call."""

    def __init__(self, db_manager, parent=None, backup_service=None) -> None:
        super().__init__(parent)
        self._db = db_manager
        self._backup = backup_service
        self._repo = BusinessDayRepository()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self._title = QLabel(self)
        self._title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(self._title)

        # -- actions -----------------------------------------------------
        actions = QHBoxLayout()
        self._backup_now_btn = QPushButton(self)
        self._backup_now_btn.clicked.connect(self._on_backup_now)
        actions.addWidget(self._backup_now_btn)
        self._status_hint = QLabel(self)
        self._status_hint.setWordWrap(True)
        self._status_hint.setStyleSheet("color: gray;")
        actions.addWidget(self._status_hint, 1)
        layout.addLayout(actions)

        # -- history -----------------------------------------------------
        self._history_group = QGroupBox(self)
        history_layout = QVBoxLayout(self._history_group)
        self._history_table = QTableWidget(self)
        self._history_table.setColumnCount(len(_COLUMNS))
        self._history_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self._history_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._history_table.verticalHeader().setVisible(False)
        self._history_table.horizontalHeader().setStretchLastSection(False)
        history_layout.addWidget(self._history_table)
        self._empty_label = QLabel(self)
        self._empty_label.setStyleSheet("color: gray;")
        history_layout.addWidget(self._empty_label)
        layout.addWidget(self._history_group, 1)

        # -- drive folder --------------------------------------------------
        self._drive_group = QGroupBox(self)
        drive_layout = QHBoxLayout(self._drive_group)
        self._drive_folder_label = QLabel(self)
        self._drive_folder_edit = QLineEdit(self)
        self._drive_folder_edit.setReadOnly(True)
        self._choose_folder_btn = QPushButton(self)
        self._choose_folder_btn.clicked.connect(self._on_choose_folder)
        drive_layout.addWidget(self._drive_folder_label)
        drive_layout.addWidget(self._drive_folder_edit, 1)
        drive_layout.addWidget(self._choose_folder_btn)
        layout.addWidget(self._drive_group)

        # -- retention -----------------------------------------------------
        self._retention_group = QGroupBox(self)
        retention_layout = QHBoxLayout(self._retention_group)
        self._keep_local_label = QLabel(self)
        self._keep_local_spin = QSpinBox(self)
        self._keep_local_spin.setRange(1, 365)
        self._keep_drive_label = QLabel(self)
        self._keep_drive_spin = QSpinBox(self)
        self._keep_drive_spin.setRange(1, 365)
        self._save_retention_btn = QPushButton(self)
        self._save_retention_btn.clicked.connect(self._on_save_retention)
        self._prune_btn = QPushButton(self)
        self._prune_btn.clicked.connect(self._on_prune)
        retention_layout.addWidget(self._keep_local_label)
        retention_layout.addWidget(self._keep_local_spin)
        retention_layout.addWidget(self._keep_drive_label)
        retention_layout.addWidget(self._keep_drive_spin)
        retention_layout.addWidget(self._save_retention_btn)
        retention_layout.addWidget(self._prune_btn)
        retention_layout.addStretch(1)
        layout.addWidget(self._retention_group)

        # -- scheduled backups ---------------------------------------------
        self._schedule_group = QGroupBox(self)
        schedule_layout = QHBoxLayout(self._schedule_group)
        self._interval_label = QLabel(self)
        self._interval_spin = QSpinBox(self)
        self._interval_spin.setRange(0, 1440)
        self._save_interval_btn = QPushButton(self)
        self._save_interval_btn.clicked.connect(self._on_save_interval)
        schedule_layout.addWidget(self._interval_label)
        schedule_layout.addWidget(self._interval_spin)
        schedule_layout.addWidget(self._save_interval_btn)
        schedule_layout.addStretch(1)
        layout.addWidget(self._schedule_group)

        self.retranslate()
        self.refresh()

    # ------------------------------------------------------------------
    # backend helpers
    # ------------------------------------------------------------------
    def _actor(self) -> str | None:
        """Current operator name for audit attribution (None when unset)."""
        try:
            conn = self._db.connect()
            try:
                return (
                    self._repo.get_setting(conn, "operator_name") or None
                )
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — attribution is best-effort
            return None

    def _get_interval_minutes(self) -> int:
        """Scheduled-backup interval in minutes (0 = disabled)."""
        svc = self._backup
        getter = getattr(svc, "get_scheduled_interval", None)
        if callable(getter):
            try:
                return int(getter() or 0)
            except Exception:  # noqa: BLE001 — fall through to settings
                pass
        try:
            conn = self._db.connect()
            try:
                raw = self._repo.get_setting(conn, _INTERVAL_SETTING)
            finally:
                conn.close()
            return int(raw or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _set_interval_minutes(self, minutes: int) -> None:
        """Persist the scheduled-backup interval via the service's settings."""
        svc = self._backup
        setter = getattr(svc, "set_scheduled_interval", None)
        if callable(setter):
            setter(minutes)
            return
        conn = self._db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._repo.set_setting(
                conn, _INTERVAL_SETTING, str(int(minutes))
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _status_text(self, record) -> str:
        """Honest localized status wording via the service's status key."""
        svc = self._backup
        try:
            key = svc.status_text_key(record)
        except Exception:  # noqa: BLE001
            key = None
        if key:
            text = tr(str(key))
            if text != str(key):  # a real translation exists
                return text
        status = str(h.dto_field(record, "status", default="") or "")
        key = f"backup.status.{status}"
        text = tr(key)
        return text if text != key else status or "—"

    def _destination_text(self, record) -> str:
        destination = str(h.dto_field(record, "destination", default="") or "")
        key = _DESTINATION_KEYS.get(destination)
        return tr(key) if key else (destination or "—")

    def _trigger_text(self, record) -> str:
        trigger = str(h.dto_field(record, "trigger", default="") or "")
        key = _TRIGGER_KEYS.get(trigger)
        return tr(key) if key else (trigger or "—")

    # ------------------------------------------------------------------
    # data loading
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Reload history, Drive folder, retention and schedule."""
        self._load_history()
        self._load_drive_folder()
        self._load_retention()
        self._load_interval()

    def _load_history(self) -> None:
        self._history_table.setRowCount(0)
        records: list = []
        if self._backup is not None:
            try:
                records = list(self._backup.list_history(limit=100) or [])
            except Exception as exc:  # noqa: BLE001 — show, don't crash
                h.show_error(self, exc)
        self._empty_label.setVisible(not records)
        for row, record in enumerate(records):
            self._history_table.insertRow(row)
            self._set_cell(row, 0, _format_pkt(h.dto_field(record, "requested_at_utc")))
            self._set_cell(
                row,
                1,
                str(h.dto_field(record, "filename", default="") or ""),
            )
            self._set_cell(row, 2, self._destination_text(record))
            status = self._status_text(record)
            self._set_cell(row, 3, status)
            self._set_cell(
                row,
                4,
                _format_bytes(h.dto_field(record, "size_bytes")),
            )
            self._set_cell(row, 5, self._trigger_text(record))
            error = h.dto_field(record, "error", default=None)
            self._set_cell(row, 6, "" if error is None else str(error))
            restore_btn = QPushButton(tr("backups.restore"), self)
            restore_btn.clicked.connect(
                lambda _checked=False, rec=record: self._on_restore(rec)
            )
            self._history_table.setCellWidget(row, 7, restore_btn)
        self._history_table.resizeColumnsToContents()

    def _set_cell(self, row: int, column: int, text: str) -> None:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        self._history_table.setItem(row, column, item)

    def _load_drive_folder(self) -> None:
        if self._backup is None:
            self._drive_folder_edit.setText(tr("backups.unavailable"))
            return
        try:
            folder = self._backup.get_drive_folder()
        except Exception as exc:  # noqa: BLE001 — show, don't crash
            h.show_error(self, exc)
            return
        if folder:
            self._drive_folder_edit.setText(str(folder))
            return
        try:
            available = self._backup.drive_available()
        except Exception:  # noqa: BLE001
            available = False
        self._drive_folder_edit.setText(
            tr("backups.not_set") if available else tr("backups.unavailable")
        )

    def _load_retention(self) -> None:
        if self._backup is None:
            return
        try:
            keep_local, keep_drive = self._backup.get_retention()
        except Exception as exc:  # noqa: BLE001 — show, don't crash
            h.show_error(self, exc)
            return
        try:
            self._keep_local_spin.setValue(int(keep_local))
            self._keep_drive_spin.setValue(int(keep_drive))
        except (TypeError, ValueError):
            pass

    def _load_interval(self) -> None:
        try:
            self._interval_spin.setValue(max(0, self._get_interval_minutes()))
        except Exception:  # noqa: BLE001
            self._interval_spin.setValue(0)

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def _on_backup_now(self) -> None:
        if self._backup is None:
            h.show_error(
                self, ValueError(tr("backups.backend_unavailable"))
            )
            return
        self._backup_now_btn.setEnabled(False)
        try:
            record = self._backup.run_backup("manual")
        except Exception as exc:  # noqa: BLE001 — surface, don't crash
            h.show_error(self, exc)
            return
        finally:
            self._backup_now_btn.setEnabled(True)
        # Only after the service call returns do we report the outcome.
        self.refresh()
        status = str(h.dto_field(record, "status", default="") or "")
        if status == "failed":
            error = h.dto_field(record, "error", default=None)
            h.show_error(
                self,
                ValueError(
                    f"{tr('backup.status.failed')}"
                    + (f": {error}" if error else "")
                ),
            )
        else:
            h.show_info(self, tr("backups.backup_ok"))

    def _on_restore(self, record) -> None:
        if self._backup is None:
            h.show_error(
                self, ValueError(tr("backups.backend_unavailable"))
            )
            return
        record_id = h.dto_field(record, "id", default=None)
        filename = str(h.dto_field(record, "filename", default="") or "")
        if record_id is None or not filename:
            h.show_error(self, ValueError(tr("backups.backend_unavailable")))
            return
        prompt = (
            tr("backups.restore_warn")
            + "\n\n"
            + tr("backups.restore_prompt").format(filename=filename)
        )
        typed, ok = QInputDialog.getText(
            self, tr("backups.restore_title"), prompt
        )
        if not ok:
            return
        if typed.strip() != filename:
            h.show_error(self, ValueError(tr("backups.restore_mismatch")))
            return
        try:
            try:
                self._backup.restore_backup(
                    record_id, confirmed=True, actor=self._actor()
                )
            except TypeError:
                # Older/alternate signature without the actor keyword.
                self._backup.restore_backup(record_id, confirmed=True)
        except Exception as exc:  # noqa: BLE001 — surface, don't crash
            h.show_error(self, exc)
            return
        # Restore succeeded: the live database file was replaced, so the app
        # MUST quit now. There is deliberately no "No" option — any open
        # SQLite connection still points at the pre-restore file, and
        # continuing to work would write to the old database (silent
        # split-brain / data loss).
        QMessageBox.information(
            self,
            tr("common.info_title"),
            tr("backups.restore_ok") + "\n" + tr("backups.restart_now"),
        )
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _on_choose_folder(self) -> None:
        if self._backup is None:
            h.show_error(
                self, ValueError(tr("backups.backend_unavailable"))
            )
            return
        try:
            current = self._backup.get_drive_folder()
        except Exception:  # noqa: BLE001
            current = None
        chosen = QFileDialog.getExistingDirectory(
            self,
            tr("backups.choose_folder"),
            str(current) if current else "",
        )
        if not chosen:
            return
        try:
            self._backup.set_drive_folder(chosen)
        except Exception as exc:  # noqa: BLE001 — validation shown plainly
            h.show_error(self, exc)
            return
        self._load_drive_folder()
        h.show_info(self, tr("backups.folder_saved"))

    def _on_save_retention(self) -> None:
        if self._backup is None:
            h.show_error(
                self, ValueError(tr("backups.backend_unavailable"))
            )
            return
        try:
            self._backup.set_retention(
                self._keep_local_spin.value(), self._keep_drive_spin.value()
            )
        except Exception as exc:  # noqa: BLE001 — surface, don't crash
            h.show_error(self, exc)
            return
        h.show_info(self, tr("backups.saved_ok"))

    def _on_prune(self) -> None:
        if self._backup is None:
            h.show_error(
                self, ValueError(tr("backups.backend_unavailable"))
            )
            return
        # Backups are the last line of defense — never delete them on a
        # single accidental click.
        reply = QMessageBox.question(
            self,
            tr("backups.prune_title"),
            tr("backups.prune_confirm"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            report = self._backup.prune_backups()
        except Exception as exc:  # noqa: BLE001 — surface, don't crash
            h.show_error(self, exc)
            return
        self.refresh()
        kept_local = h.dto_field(report, "kept_local", "local_kept", default=None)
        kept_drive = h.dto_field(report, "kept_drive", "drive_kept", default=None)
        if kept_local is None or kept_drive is None:
            h.show_info(self, tr("backups.prune_done"))
        else:
            h.show_info(
                self,
                tr("backups.prune_ok").format(
                    local=kept_local, drive=kept_drive
                ),
            )

    def _on_save_interval(self) -> None:
        if self._backup is None:
            h.show_error(
                self, ValueError(tr("backups.backend_unavailable"))
            )
            return
        try:
            self._set_interval_minutes(self._interval_spin.value())
        except Exception as exc:  # noqa: BLE001 — surface, don't crash
            h.show_error(self, exc)
            return
        h.show_info(self, tr("backups.interval_saved"))

    # ------------------------------------------------------------------
    # i18n
    # ------------------------------------------------------------------
    def retranslate(self) -> None:
        """Re-apply all static texts (called on language change)."""
        self.setLayoutDirection(
            Qt.RightToLeft if get_language() == "ur" else Qt.LeftToRight
        )
        self._title.setText(tr("backups.title"))
        self._backup_now_btn.setText(tr("backups.backup_now"))
        self._status_hint.setText(tr("backups.status_hint"))
        self._history_group.setTitle(tr("backups.history_section"))
        self._history_table.setHorizontalHeaderLabels(
            [tr(key) for key, _ in _COLUMNS]
        )
        self._empty_label.setText(tr("backups.no_history"))
        self._drive_group.setTitle(tr("backups.drive_section"))
        self._drive_folder_label.setText(tr("backups.drive_folder"))
        self._choose_folder_btn.setText(tr("backups.choose_folder"))
        self._retention_group.setTitle(tr("backups.retention_section"))
        self._keep_local_label.setText(tr("backups.keep_local"))
        self._keep_drive_label.setText(tr("backups.keep_drive"))
        self._save_retention_btn.setText(tr("backups.save_retention"))
        self._prune_btn.setText(tr("backups.prune_now"))
        self._schedule_group.setTitle(tr("backups.schedule_section"))
        self._interval_label.setText(tr("backups.interval_minutes"))
        self._save_interval_btn.setText(tr("backups.save_interval"))
        # Status/destination/trigger wording follows the new language.
        self._load_history()
        self._load_drive_folder()
