"""Offscreen smoke test: bootstrap a temp database and instantiate the
Phase-1 shell window without a display.

Requires app/ui/main_window.py and app/bootstrap.py (both present — the
shell agent has finished them). Qt itself is optional: if the Qt platform
libraries cannot load in this environment (e.g. missing system libEGL),
the module skips cleanly instead of breaking collection.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

# Skips the whole module when Qt cannot initialize here (ImportError from a
# missing system library is caught by importorskip just like a missing
# package). QT_QPA_PLATFORM is set above, before any Qt import.
QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from app.bootstrap import bootstrap
from app.database.migrator import CURRENT_SCHEMA_VERSION
from app.ui.main_window import MainWindow

QApplication = QtWidgets.QApplication


@pytest.fixture(scope="module")
def qapp():
    """A single offscreen QApplication for the module."""
    app = QApplication.instance() or QApplication([])
    yield app
    app.quit()


def test_bootstrap_and_main_window_offscreen(tmp_path, qapp):
    """Bootstrap a tmp database, build MainWindow, and verify the window
    title/status bar report the schema version — then close cleanly."""
    db_path = tmp_path / "smoke.sqlite3"

    result = bootstrap(db_path)
    try:
        assert result.schema_version == CURRENT_SCHEMA_VERSION
        assert db_path.exists()

        window = MainWindow(result)
        try:
            window.show()  # offscreen: no visible display needed

            # Window title comes from the app title string.
            assert window.windowTitle(), "window title must not be empty"

            # Status bar must show the schema version.
            status = window.statusBar().currentMessage()
            assert str(result.schema_version) in status, (
                f"status bar {status!r} does not mention schema version"
                f" {result.schema_version}"
            )

            # The pragmas recorded at bootstrap are the verified ones.
            assert result.pragmas.get("journal_mode", "").lower() == "wal"
        finally:
            window.close()
    finally:
        result.db_manager.close()
