"""Offscreen UI interaction tests (Phase 2).

Drives the real pages — Daily Register, Nozzle Readings, Settings — through
their actual buttons and verifies the database state afterwards. Modal
dialogs are auto-answered via monkeypatched QMessageBox/QDialog so the
offscreen run never blocks.

Requires Qt; skips cleanly when Qt cannot initialize here.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from decimal import Decimal

from .conftest import pkt_midnight_utc_iso

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
QtCore = pytest.importorskip("PySide6.QtCore", exc_type=ImportError)

from app.bootstrap import bootstrap
from app.localization import get_language, set_language
from app.ui.main_window import MainWindow

QApplication = QtWidgets.QApplication
QMessageBox = QtWidgets.QMessageBox
QDialog = QtWidgets.QDialog
QTextEdit = QtWidgets.QTextEdit
QDate = QtCore.QDate
Qt = QtCore.Qt

DAY = "2026-10-10"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.quit()


@pytest.fixture
def window(tmp_path, qapp, monkeypatch):
    """MainWindow on a temp database with dialogs auto-answered."""
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Ok
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: None)

    def fake_exec(self):
        for child in self.findChildren(QTextEdit):
            child.setPlainText("ui test reason")
        return QDialog.Accepted

    monkeypatch.setattr(QDialog, "exec", fake_exec)

    set_language("en")
    result = bootstrap(tmp_path / "ui.sqlite3")
    win = MainWindow(result)
    win.show()
    yield win
    set_language("en")
    win.close()
    result.db_manager.close()


def _register(window):
    return window._register


def _readings(window):
    return window._readings


def test_open_day_button_opens_day(window):
    """Daily Register → Open Day creates the day with the entered cash."""
    page = _register(window)
    page._date_edit.setDate(QDate(2026, 10, 10))
    page._open_cash.setText("20000.00")
    page._open_btn.click()

    day = window._day_service.get_day(DAY)
    assert day is not None
    assert day.status == "open"
    assert day.opening_cash_paisa == 2_000_000  # PKR 20,000.00 → paisa
    assert "open" in page._st_status.text()


def test_save_readings_button_records_readings(window):
    """Nozzle Readings → Save Readings posts one reading per entered row."""
    test_open_day_button_opens_day(window)  # idempotent: day already open
    page = _readings(window)
    page._date_edit.setDate(QDate(2026, 10, 10))

    values = ["84985", "31371", "56948", "37486"]
    for row in range(page._table.rowCount()):
        edit = page._table.cellWidget(row, 3)
        edit.setText(values[row])
    page._save_btn.click()

    readings = window._nozzle_service.day_readings(DAY)
    assert [r.current_ml for r in readings] == [
        84_985_000,
        31_371_000,
        56_948_000,
        37_486_000,
    ]
    # Liters for the first-ever readings are unknown (no previous).
    assert all(r.liters_sold_ml is None for r in readings)


def test_checklist_and_close_day_buttons(window):
    """Check Readiness renders the checklist; Close Day closes the day."""
    test_save_readings_button_records_readings(window)
    # Phase-3 close rule: applicable fuel prices must be recorded. In the
    # real UI the manager sets these on the Fuel Stock page; here we use
    # the same service the page calls.
    effective = pkt_midnight_utc_iso(DAY)
    window._price_service.set_price("PETROL", Decimal("265.9630"), effective, DAY)
    window._price_service.set_price("DIESEL", Decimal("265.9630"), effective, DAY)

    page = _register(window)
    page._date_edit.setDate(QDate(2026, 10, 10))

    page._check_btn.click()
    assert page._checklist.rowCount() > 0
    items = {
        page._checklist.item(row, 0).text(): page._checklist.item(row, 1).text()
        for row in range(page._checklist.rowCount())
    }
    # Physical cash not entered yet → that item fails; readings pass.
    assert items["Physical cash has been entered"] == "Fail"
    assert items["Each active nozzle has a final reading"] == "Pass"

    page._physical_cash.setText("25000")
    page._close_btn.click()  # question dialog auto-answered OK

    day = window._day_service.get_day(DAY)
    assert day.status == "closed"
    assert day.physical_cash_paisa == 2_500_000
    closings = window._day_service.closings(DAY)
    assert len(closings) == 1
    assert closings[0].closing_revision == 1


def test_reopen_day_button(window):
    """Reopen Day (reason dialog auto-filled) reopens the closed day."""
    test_checklist_and_close_day_buttons(window)
    page = _register(window)
    page._date_edit.setDate(QDate(2026, 10, 10))

    page._reopen_btn.click()
    day = window._day_service.get_day(DAY)
    assert day.status == "reopened"
    events = window._day_service.day_events(DAY)
    assert events[-1]["event_type"] == "reopen"
    assert events[-1]["reason"] == "ui test reason"


def test_language_switch_to_urdu(window):
    """Settings → اردو applies RTL layout and Urdu strings live."""
    page = window._settings
    page._language_combo.setCurrentIndex(1)  # ur
    page._operator_edit.setText("Test Operator")
    page._save_btn.click()

    assert get_language() == "ur"
    assert window.layoutDirection() == Qt.RightToLeft
    assert window.windowTitle() == "پیٹرول پمپ مینیجر"
    # Daily Sheet is the first sidebar item (v2 daily workflow order).
    assert window._sidebar.item(0).text() == "یومیہ شیٹ"
    assert window._sidebar.item(1).text() == "ڈیش بورڈ"

    # The operator name was persisted for audit attribution.
    conn = window._day_service._db.connect()
    try:
        value = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'operator_name'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert value == "Test Operator"
    set_language("en")
