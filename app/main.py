"""Application entry point (design plan §1: "parses args, starts Qt app,
hands off to bootstrap").

On Windows the shipped build hides the console with the PyInstaller
``--noconsole`` / ``--windowed`` flag; on Linux/macOS no console window
is created in the first place.
"""

from __future__ import annotations

import logging
import os
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from app import config
from app.bootstrap import bootstrap
from app.config import AppStartupError
from app.localization import get_language, tr
from app.ui.main_window import MainWindow

_LOG = logging.getLogger("petrol_pump_manager")

# Smoke-test hook: when set, the event loop quits after N milliseconds so
# `python -m app.main` can be exercised headlessly (QT_QPA_PLATFORM=offscreen).
_SMOKE_QUIT_ENV = "PPM_SMOKE_QUIT_MS"


def _maybe_shutdown_backup() -> None:
    """Best-effort automatic backup on shutdown (recovery.md §9 design).

    Runs headless with no dialogs and never raises: a backup failure must
    never crash or delay shutdown — it is logged as a warning only.

    NOTE: the enable/disable setting is read with
    ``BackupRepository.get_setting`` — ``BackupService`` exposes no
    ``get_setting`` instance method. A missing key counts as the default
    (disabled for shutdown).
    """
    try:
        from app.database.connection import DatabaseManager
        from app.repositories.backup_repository import BackupRepository
        from app.services.backup_service import BackupService
    except ImportError as exc:
        _LOG.warning("Automatic shutdown backup skipped: backup support "
                     "unavailable (%s).", exc)
        return
    try:
        conn = DatabaseManager(config.db_path()).connect()
        try:
            enabled = BackupRepository.get_setting(
                conn, "backup.on_shutdown"
            )
        finally:
            conn.close()
        # Missing key counts as the default: disabled ('0') for shutdown.
        if (enabled if enabled is not None else "0") == "0":
            _LOG.info("Automatic shutdown backup skipped "
                      "(backup.on_shutdown disabled).")
            return
        result = BackupService(actor="system").run_backup("shutdown")
        _LOG.info("Automatic shutdown backup completed: %s", result)
    except Exception as exc:
        _LOG.warning("Automatic shutdown backup FAILED: %s. Shutdown is "
                     "unaffected.", exc)


def main() -> int:
    """Start the application. Returns the process exit code."""
    app = QApplication(sys.argv)
    app.setApplicationName(config.APP_NAME)
    app.setOrganizationName(config.ORG_NAME)
    app.setApplicationVersion(config.APP_VERSION)

    try:
        result = bootstrap()
    except AppStartupError as exc:
        QMessageBox.critical(None, tr("error.title"), str(exc))
        _LOG.error("Startup failed: %s", exc)
        return 1

    window = MainWindow(result)
    window.show()

    smoke_quit = os.environ.get(_SMOKE_QUIT_ENV)
    if smoke_quit:
        try:
            QTimer.singleShot(int(smoke_quit), app.quit)
        except ValueError:
            pass

    exit_code = app.exec()
    _LOG.info("Shutdown complete (exit code %s).", exit_code)
    # Automatic backup hook: headless, log-only on failure — shutdown
    # semantics (exit code, order) are unchanged.
    _maybe_shutdown_backup()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
