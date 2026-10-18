"""Nozzle Readings page: enter today's final readings, meter events, corrections.

The previous reading is shown automatically from the last approved final
reading — the manager types only today's final reading. Liters are computed
by the service and displayed; the UI does no accounting math.
"""

from __future__ import annotations

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QPushButton,
)

from app.localization import localize_error, tr
from app.services import errors
from app.services.nozzle_service import NozzleService
from app.ui import _helpers as h

_EVENT_TYPE_KEYS = {
    "reset": "reading.ev_reset",
    "rollover": "reading.ev_rollover",
    "replacement": "reading.ev_replacement",
    "correction": "reading.ev_correction",
}


class NozzleReadingsPage(QWidget):
    """Meter-reading entry for one business day."""

    def __init__(self, nozzle_service: NozzleService, parent=None) -> None:
        super().__init__(parent)
        self._nozzles = nozzle_service
        self._rows: list = []  # DayReading DTOs, aligned with table rows

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self._banner = h.make_page_banner(
            self, tr("reading.title"), color="purple"
        )
        layout.insertWidget(0, self._banner)

        self._title = QLabel(self)
        self._title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(self._title)

        date_row = QHBoxLayout()
        self._date_label = QLabel(self)
        self._date_edit = QDateEdit(self)
        self._date_edit.setDisplayFormat("dd-MM-yyyy")
        self._date_edit.setDate(QDate.currentDate())
        self._date_edit.setCalendarPopup(True)
        self._date_edit.dateChanged.connect(self.refresh)
        date_row.addWidget(self._date_label)
        date_row.addWidget(self._date_edit)
        date_row.addStretch(1)
        layout.addLayout(date_row)

        self._hint = QLabel(self)
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet("color: gray;")
        layout.addWidget(self._hint)

        self._table = QTableWidget(self)
        self._table.setColumnCount(6)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setMinimumHeight(260)
        layout.addWidget(self._table)

        buttons = QHBoxLayout()
        self._save_btn = QPushButton(self)
        self._save_btn.clicked.connect(self._on_save_readings)
        self._event_btn = QPushButton(self)
        self._event_btn.clicked.connect(self._on_record_event)
        self._correct_btn = QPushButton(self)
        self._correct_btn.clicked.connect(self._on_correct_reading)
        buttons.addWidget(self._save_btn)
        buttons.addWidget(self._event_btn)
        buttons.addWidget(self._correct_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addStretch(1)

        self.retranslate()
        self.refresh()

    # ------------------------------------------------------------------
    def _business_date(self) -> str:
        return self._date_edit.date().toString("yyyy-MM-dd")

    def retranslate(self) -> None:
        """Re-apply all static texts (called on language change)."""
        self._title.setText(tr("reading.title"))
        banner_title = self._banner.findChild(QLabel, "pageBannerTitle")
        if banner_title is not None:
            banner_title.setText(tr("reading.title"))
        self._date_label.setText(tr("reading.date") + ":")
        self._hint.setText(tr("reading.enter_reading_hint"))
        self._table.setHorizontalHeaderLabels(
            [
                tr("reading.th_nozzle"),
                tr("reading.th_fuel"),
                tr("reading.th_previous_l"),
                tr("reading.th_current_l"),
                tr("reading.th_liters_l"),
                tr("reading.th_status"),
            ]
        )
        self._save_btn.setText(tr("reading.save_readings"))
        self._event_btn.setText(tr("reading.record_event"))
        self._correct_btn.setText(tr("reading.correct_title"))
        self.refresh()

    def refresh(self) -> None:
        """Reload the table from the database."""
        business_date = self._business_date()
        try:
            self._rows = self._nozzles.day_readings(business_date)
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._table.setRowCount(len(self._rows))
        for row, reading in enumerate(self._rows):
            nozzle_item = QTableWidgetItem(reading.nozzle_code)
            nozzle_item.setFlags(nozzle_item.flags() & ~Qt.ItemIsEditable)
            fuel_item = QTableWidgetItem(reading.fuel_product_code)
            fuel_item.setFlags(fuel_item.flags() & ~Qt.ItemIsEditable)
            prev_text = (
                h.format_litres(reading.previous_ml)
                if reading.previous_ml is not None
                else tr("reading.no_previous")
            )
            prev_item = QTableWidgetItem(prev_text)
            prev_item.setFlags(prev_item.flags() & ~Qt.ItemIsEditable)
            prev_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

            current_edit = h.make_integer_edit(self._table)
            current_edit.setPlaceholderText(
                h.format_litres(reading.current_ml)
                if reading.current_ml is not None
                else ""
            )
            current_edit.setAlignment(Qt.AlignRight)

            liters_item = QTableWidgetItem(h.format_litres(reading.liters_sold_ml))
            liters_item.setFlags(liters_item.flags() & ~Qt.ItemIsEditable)
            liters_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

            status_item = QTableWidgetItem(
                tr("reading.status_recorded")
                if reading.has_reading
                else tr("reading.status_missing")
            )
            status_item.setFlags(status_item.flags() & ~Qt.ItemIsEditable)

            self._table.setItem(row, 0, nozzle_item)
            self._table.setItem(row, 1, fuel_item)
            self._table.setItem(row, 2, prev_item)
            self._table.setCellWidget(row, 3, current_edit)
            self._table.setItem(row, 4, liters_item)
            self._table.setItem(row, 5, status_item)
        self._table.resizeColumnsToContents()

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def _on_save_readings(self) -> None:
        """Record every entered current reading through the service."""
        business_date = self._business_date()
        saved = 0
        failures: list[str] = []
        for row, reading in enumerate(self._rows):
            edit = self._table.cellWidget(row, 3)
            text = edit.text().strip() if edit is not None else ""
            if not text:
                continue  # nothing entered for this nozzle
            status_item = self._table.item(row, 5)
            try:
                litres = h.parse_litres(text)
                self._nozzles.record_reading(
                    business_date, reading.nozzle_id, litres
                )
                saved += 1
                if status_item is not None:
                    status_item.setText(tr("reading.status_recorded"))
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, errors.BusinessRuleError):
                    message = localize_error(exc)
                else:
                    message = tr("reading.status_error")
                # The refresh() below rebuilds the table and wipes per-row
                # status text, so collect failures for a summary instead.
                failures.append(f"{reading.nozzle_code}: {message}")
                if status_item is not None:
                    status_item.setText(f"{tr('reading.status_error')}: {message}")
        self.refresh()
        if failures:
            h.show_error(
                self,
                ValueError(
                    tr("reading.saved_with_errors").format(
                        n=saved, errors="\n".join(failures)
                    )
                ),
            )
        elif saved:
            h.show_info(self, tr("reading.saved_ok").format(n=saved))

    def _on_record_event(self) -> None:
        """Dialog: record a meter reset / rollover / replacement."""
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("reading.event_title"))
        form = QFormLayout(dialog)

        nozzle_combo = QComboBox(dialog)
        try:
            nozzles = self._nozzles.list_nozzles()
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        for nozzle in nozzles:
            nozzle_combo.addItem(
                f"{nozzle.code} ({nozzle.fuel_product_code})", nozzle.id
            )
        type_combo = QComboBox(dialog)
        for event_type in ("reset", "rollover", "replacement"):
            type_combo.addItem(tr(_EVENT_TYPE_KEYS[event_type]), event_type)
        before_edit = h.make_integer_edit(dialog)
        after_edit = h.make_integer_edit(dialog)
        reason_edit = QTextEdit(dialog)
        reason_edit.setMinimumSize(340, 70)

        form.addRow(QLabel(tr("reading.th_nozzle"), dialog), nozzle_combo)
        form.addRow(QLabel(tr("reading.event_type"), dialog), type_combo)
        form.addRow(QLabel(tr("reading.event_before_l"), dialog), before_edit)
        form.addRow(QLabel(tr("reading.event_after_l"), dialog), after_edit)
        form.addRow(QLabel(tr("reading.event_reason"), dialog), reason_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dialog
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)

        if dialog.exec() != QDialog.Accepted:
            return
        try:
            before = (
                h.parse_litres(before_edit.text())
                if before_edit.text().strip()
                else None
            )
            after = (
                h.parse_litres(after_edit.text())
                if after_edit.text().strip()
                else None
            )
            self._nozzles.record_meter_event(
                nozzle_combo.currentData(),
                type_combo.currentData(),
                reason=reason_edit.toPlainText(),
                reading_before_litres=before,
                reading_after_litres=after,
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        h.show_info(self, tr("reading.event_saved"))
        self.refresh()

    def _on_correct_reading(self) -> None:
        """Dialog: correct an existing reading (original is preserved)."""
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("reading.correct_title"))
        form = QFormLayout(dialog)

        nozzle_combo = QComboBox(dialog)
        for reading in self._rows:
            if reading.has_reading:
                nozzle_combo.addItem(
                    f"{reading.nozzle_code}"
                    f" ({h.format_litres(reading.current_ml)}"
                    f" {tr('common.litres')})",
                    reading.nozzle_id,
                )
        if nozzle_combo.count() == 0:
            h.show_error(
                self,
                errors.NotFoundError(
                    "No recorded readings for this date to correct.",
                    key="err.no_readings_to_correct",
                ),
            )
            return
        new_edit = h.make_integer_edit(dialog)
        reason_edit = QTextEdit(dialog)
        reason_edit.setMinimumSize(340, 70)

        form.addRow(QLabel(tr("reading.th_nozzle"), dialog), nozzle_combo)
        form.addRow(QLabel(tr("reading.correct_new_l"), dialog), new_edit)
        form.addRow(QLabel(tr("reading.correct_reason"), dialog), reason_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dialog
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)

        if dialog.exec() != QDialog.Accepted:
            return
        try:
            litres = h.parse_litres(new_edit.text())
            self._nozzles.correct_reading(
                self._business_date(),
                nozzle_combo.currentData(),
                litres,
                reason=reason_edit.toPlainText(),
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        h.show_info(self, tr("reading.corrected_ok"))
        self.refresh()
