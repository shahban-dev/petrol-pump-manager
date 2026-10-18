"""Settings page: language, operator name, database info.

Language switches apply immediately (including right-to-left layout for
Urdu) and never alter stored data. The operator name feeds audit
attribution; it is stored in app_settings.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app import config
from app.database.connection import DatabaseManager
from app.localization import get_language, set_language, tr
from app.repositories.business_day_repository import BusinessDayRepository
from app.ui import _helpers as h


def _backup_service_or_none():
    """A BackupService for read-only display, or None before it lands.

    The backup service is built by a parallel agent; the constructor
    signature may differ from the published contract, so both the
    ``actor=`` keyword form and the no-argument form are tried.
    """
    try:
        from app.services.backup_service import BackupService
    except ImportError:
        return None
    try:
        return BackupService(actor=None)
    except TypeError:
        pass
    try:
        return BackupService()
    except Exception:  # noqa: BLE001
        return None


class SettingsPage(QWidget):
    """Application settings. Every control works."""

    def __init__(
        self, db_manager: DatabaseManager, on_language_changed, parent=None
    ) -> None:
        super().__init__(parent)
        self._db = db_manager
        self._on_language_changed = on_language_changed
        self._repo = BusinessDayRepository()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self._title = QLabel(self)
        self._title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(self._title)

        # -- general ----------------------------------------------------------
        self._general_group = QGroupBox(self)
        general_form = QFormLayout(self._general_group)
        self._language_label = QLabel(self)
        self._language_combo = QComboBox(self)
        self._language_combo.addItem(tr("settings.english"), "en")
        self._language_combo.addItem(tr("settings.urdu"), "ur")
        self._language_combo.setCurrentIndex(
            0 if get_language() == "en" else 1
        )
        self._language_hint = QLabel(self)
        self._language_hint.setWordWrap(True)
        self._language_hint.setStyleSheet("color: gray;")
        self._operator_label = QLabel(self)
        self._operator_edit = QLineEdit(self)
        self._station_label = QLabel(self)
        self._station_edit = QLineEdit(self)
        self._station_edit.setPlaceholderText(tr("settings.station_name_hint"))
        general_form.addRow(self._language_label, self._language_combo)
        general_form.addRow(self._language_hint)
        general_form.addRow(self._station_label, self._station_edit)
        general_form.addRow(self._operator_label, self._operator_edit)
        layout.addWidget(self._general_group)

        self._save_btn = QPushButton(self)
        self._save_btn.clicked.connect(self._on_save)
        layout.addWidget(self._save_btn)

        # -- database ----------------------------------------------------------
        self._db_group = QGroupBox(self)
        db_form = QFormLayout(self._db_group)
        self._db_path_label = QLabel(self)
        self._db_path_value = QLabel(self)
        self._db_path_value.setTextInteractionFlags(
            Qt.TextSelectableByMouse
        )
        self._db_path_value.setWordWrap(True)
        self._schema_label = QLabel(self)
        self._schema_value = QLabel(self)
        db_form.addRow(self._db_path_label, self._db_path_value)
        db_form.addRow(self._schema_label, self._schema_value)
        layout.addWidget(self._db_group)

        # -- storage locations -------------------------------------------------
        # Read-only: where the data lives — the live database, the local
        # backup folder, and the configured Google Drive folder.
        self._storage_group = QGroupBox(self)
        storage_form = QFormLayout(self._storage_group)
        self._storage_db_label = QLabel(self)
        self._storage_db_value = QLabel(self)
        self._storage_db_value.setTextInteractionFlags(
            Qt.TextSelectableByMouse
        )
        self._storage_db_value.setWordWrap(True)
        self._backup_dir_label = QLabel(self)
        self._backup_dir_value = QLabel(self)
        self._backup_dir_value.setTextInteractionFlags(
            Qt.TextSelectableByMouse
        )
        self._backup_dir_value.setWordWrap(True)
        self._drive_folder_label = QLabel(self)
        self._drive_folder_value = QLabel(self)
        self._drive_folder_value.setTextInteractionFlags(
            Qt.TextSelectableByMouse
        )
        self._drive_folder_value.setWordWrap(True)
        storage_form.addRow(self._storage_db_label, self._storage_db_value)
        storage_form.addRow(self._backup_dir_label, self._backup_dir_value)
        storage_form.addRow(self._drive_folder_label, self._drive_folder_value)
        layout.addWidget(self._storage_group)

        layout.addStretch(1)
        self.retranslate()
        self._load_values()

    # ------------------------------------------------------------------
    def _load_values(self) -> None:
        try:
            from app.database.migrator import get_schema_version

            conn = self._db.connect()
            try:
                operator = self._repo.get_setting(conn, "operator_name") or ""
                station = self._repo.get_setting(conn, "station_name") or ""
                version = get_schema_version(conn)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._operator_edit.setText(operator)
        self._station_edit.setText(station)
        self._db_path_value.setText(str(self._db.db_path))
        self._schema_value.setText(str(version))
        self._storage_db_value.setText(str(config.db_path()))
        self._backup_dir_value.setText(str(config.BACKUP_DIR))
        self._drive_folder_value.setText(self._drive_folder_display())

    def _on_save(self) -> None:
        new_language = self._language_combo.currentData()
        operator = self._operator_edit.text().strip()
        station = self._station_edit.text().strip()
        language_changed = new_language != get_language()
        try:
            conn = self._db.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._repo.set_setting(conn, "operator_name", operator)
                self._repo.set_setting(conn, "station_name", station)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        if language_changed:
            set_language(new_language)
            self._on_language_changed()
        h.show_info(self, tr("settings.saved_ok"))

    # ------------------------------------------------------------------
    def retranslate(self) -> None:
        """Re-apply all static texts (called on language change)."""
        # Rebuild combo labels without losing the selection.
        current = self._language_combo.currentData()
        self._language_combo.setItemText(0, tr("settings.english"))
        self._language_combo.setItemText(1, tr("settings.urdu"))
        self._language_combo.setCurrentIndex(0 if current == "en" else 1)
        self._title.setText(tr("settings.title"))
        self._general_group.setTitle(tr("settings.general_section"))
        self._language_label.setText(tr("settings.language") + ":")
        self._language_hint.setText(tr("settings.language_hint"))
        self._station_label.setText(tr("settings.station_name") + ":")
        self._station_edit.setPlaceholderText(tr("settings.station_name_hint"))
        self._operator_label.setText(tr("settings.operator_name") + ":")
        self._save_btn.setText(tr("settings.save"))
        self._db_group.setTitle(tr("settings.database_section"))
        self._db_path_label.setText(tr("settings.db_path") + ":")
        self._schema_label.setText(tr("settings.schema_version") + ":")
        self._db_path_value.setText(str(self._db.db_path))
        self._storage_group.setTitle(tr("settings.storage_section"))
        self._storage_db_label.setText(tr("settings.db_path") + ":")
        self._backup_dir_label.setText(tr("settings.backup_dir") + ":")
        self._drive_folder_label.setText(tr("settings.drive_folder") + ":")
        self._storage_db_value.setText(str(config.db_path()))
        self._backup_dir_value.setText(str(config.BACKUP_DIR))
        self._drive_folder_value.setText(self._drive_folder_display())

    @staticmethod
    def _drive_folder_display() -> str:
        """Configured Google Drive folder, or a plain not-set/unavailable note.

        The backup service may not have landed yet (parallel agent), so the
        import and every call are guarded — the setting shows an honest
        note rather than crashing.
        """
        service = _backup_service_or_none()
        if service is None:
            return tr("backups.unavailable")
        try:
            folder = service.get_drive_folder()
        except Exception:  # noqa: BLE001
            return tr("backups.unavailable")
        if folder:
            return str(folder)
        try:
            available = service.drive_available()
        except Exception:  # noqa: BLE001
            available = False
        return tr("backups.not_set") if available else tr("backups.unavailable")
