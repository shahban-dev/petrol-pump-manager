"""Daily Register page: open day, guided close, authorized reopen.

Every button performs a real service call; validation failures are shown
and nothing is committed before the service transaction succeeds.
"""

from __future__ import annotations

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.calculations import time as timecalc
from app.localization import localize_check_detail, tr
from app.services import errors
from app.services.business_day_service import BusinessDayService
from app.ui import _helpers as h

_STATUS_COLORS = {
    "pass": "#1a7f37",
    "fail": "#c0392b",
    "warning": "#b7791f",
    "na": "#6e7781",
}


class DailyRegisterPage(QWidget):
    """Day lifecycle controls and the closing checklist."""

    def __init__(self, day_service: BusinessDayService, parent=None) -> None:
        super().__init__(parent)
        self._days = day_service
        self._current_report = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self._title = QLabel(self)
        self._title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(self._title)

        # -- date selector ----------------------------------------------------
        date_row = QHBoxLayout()
        self._date_label = QLabel(self)
        self._date_edit = QDateEdit(self)
        self._date_edit.setDisplayFormat("dd-MM-yyyy")
        self._date_edit.setDate(QDate.currentDate())
        self._date_edit.setCalendarPopup(True)
        self._date_edit.dateChanged.connect(self._load_day)
        date_row.addWidget(self._date_label)
        date_row.addWidget(self._date_edit)
        date_row.addStretch(1)
        layout.addLayout(date_row)

        # -- status ------------------------------------------------------------
        self._status_group = QGroupBox(self)
        status_form = QFormLayout(self._status_group)
        self._st_status_label = QLabel(self)
        self._st_status = QLabel(self)
        self._st_cash_label = QLabel(self)
        self._st_cash = QLabel(self)
        self._st_src_label = QLabel(self)
        self._st_src = QLabel(self)
        self._st_prev_label = QLabel(self)
        self._st_prev = QLabel(self)
        status_form.addRow(self._st_status_label, self._st_status)
        status_form.addRow(self._st_cash_label, self._st_cash)
        status_form.addRow(self._st_src_label, self._st_src)
        status_form.addRow(self._st_prev_label, self._st_prev)
        layout.addWidget(self._status_group)

        # -- open ---------------------------------------------------------------
        self._open_group = QGroupBox(self)
        open_form = QFormLayout(self._open_group)
        self._open_cash = h.make_integer_edit(self)
        self._open_cash.setPlaceholderText("0")
        self._gap_reason = QLineEdit(self)
        self._open_cash_label = QLabel(self)
        self._gap_label = QLabel(self)
        open_form.addRow(self._open_cash_label, self._open_cash)
        open_form.addRow(self._gap_label, self._gap_reason)
        self._first_day_hint = QLabel(self)
        self._first_day_hint.setWordWrap(True)
        self._first_day_hint.setStyleSheet("color: gray;")
        open_form.addRow(self._first_day_hint)
        open_buttons = QHBoxLayout()
        self._open_btn = QPushButton(self)
        self._open_btn.clicked.connect(self._on_open_day)
        self._open_nonop_btn = QPushButton(self)
        self._open_nonop_btn.clicked.connect(self._on_open_non_operating)
        open_buttons.addWidget(self._open_btn)
        open_buttons.addWidget(self._open_nonop_btn)
        open_buttons.addStretch(1)
        open_form.addRow(open_buttons)
        layout.addWidget(self._open_group)

        # -- close ---------------------------------------------------------------
        self._close_group = QGroupBox(self)
        close_layout = QVBoxLayout(self._close_group)
        cash_row = QHBoxLayout()
        self._cash_label = QLabel(self)
        self._physical_cash = h.make_integer_edit(self)
        self._physical_cash.setPlaceholderText("0")
        cash_row.addWidget(self._cash_label)
        cash_row.addWidget(self._physical_cash)
        cash_row.addStretch(1)
        close_layout.addLayout(cash_row)

        self._checklist_title = QLabel(self)
        self._checklist_title.setStyleSheet("font-weight: bold;")
        close_layout.addWidget(self._checklist_title)
        self._checklist = QTableWidget(self)
        self._checklist.setColumnCount(3)
        self._checklist.setEditTriggers(QTableWidget.NoEditTriggers)
        self._checklist.horizontalHeader().setStretchLastSection(True)
        self._checklist.setMinimumHeight(220)
        close_layout.addWidget(self._checklist)

        close_buttons = QHBoxLayout()
        self._check_btn = QPushButton(self)
        self._check_btn.clicked.connect(self._on_check_readiness)
        self._close_btn = QPushButton(self)
        self._close_btn.clicked.connect(self._on_close_day)
        self._reopen_btn = QPushButton(self)
        self._reopen_btn.clicked.connect(self._on_reopen_day)
        close_buttons.addWidget(self._check_btn)
        close_buttons.addWidget(self._close_btn)
        close_buttons.addWidget(self._reopen_btn)
        close_buttons.addStretch(1)
        close_layout.addLayout(close_buttons)

        self._reopen_hint = QLabel(self)
        self._reopen_hint.setWordWrap(True)
        self._reopen_hint.setStyleSheet("color: gray;")
        close_layout.addWidget(self._reopen_hint)
        layout.addWidget(self._close_group)

        layout.addStretch(1)
        self.retranslate()
        self._load_day()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _business_date(self) -> str:
        return self._date_edit.date().toString("yyyy-MM-dd")

    def _load_day(self) -> None:
        """Refresh the status panel for the selected date."""
        business_date = self._business_date()
        self._current_report = None
        self._checklist.setRowCount(0)
        try:
            day = self._days.get_day(business_date)
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        if day is None:
            self._st_status.setText(tr("day.no_day_loaded"))
            self._st_cash.setText("—")
            self._st_src.setText("—")
            self._st_prev.setText("—")
            self._open_group.setVisible(True)
            self._close_group.setVisible(False)
            return
        self._st_status.setText(
            f"{day.status} • {timecalc.format_display_date(day.business_date)}"
        )
        self._st_cash.setText(h.format_pkr(day.opening_cash_paisa))
        self._st_prev.setText(
            timecalc.format_display_date(day.prev_closing_date)
            if day.prev_closing_date
            else "—"
        )
        try:
            balances = self._days.opening_balances(business_date)
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        cash_src = "—"
        for balance in balances:
            if balance.balance_type != "cash":
                continue
            if balance.source_business_date is None:
                cash_src = tr("day.src_initial")
            elif balance.source_closing_revision is not None:
                cash_src = tr("day.src_closing").format(
                    date=timecalc.format_display_date(
                        balance.source_business_date
                    ),
                    revision=balance.source_closing_revision,
                )
            else:
                cash_src = tr("day.src_day_row").format(
                    date=timecalc.format_display_date(
                        balance.source_business_date
                    )
                )
        self._st_src.setText(cash_src)

        is_open = day.status in ("open", "reopened")
        is_closed = day.status == "closed"
        self._open_group.setVisible(not is_open and not is_closed)
        self._close_group.setVisible(True)
        self._check_btn.setVisible(is_open)
        self._close_btn.setVisible(is_open)
        self._reopen_btn.setVisible(is_closed)
        self._reopen_hint.setVisible(is_closed)
        if day.physical_cash_paisa is not None and not self._physical_cash.text():
            self._physical_cash.setText(
                f"{day.physical_cash_paisa / 100:.2f}"
            )

    def _parse_optional_pkr(self, widget: QLineEdit) -> int | None:
        text = widget.text().strip()
        if not text:
            return None
        try:
            return h.parse_pkr_to_paisa(text)
        except ValueError as exc:
            raise errors.ValidationError(str(exc)) from exc

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def _on_open_day(self) -> None:
        self._do_open(non_operating=False)

    def _on_open_non_operating(self) -> None:
        self._do_open(non_operating=True)

    def _do_open(self, *, non_operating: bool) -> None:
        business_date = self._business_date()
        try:
            opening_cash = self._parse_optional_pkr(self._open_cash)
            gap_reason = self._gap_reason.text().strip() or None
            self._days.open_day(
                business_date,
                opening_cash_paisa=opening_cash,
                non_operating=non_operating,
                gap_reason=gap_reason,
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        h.show_info(
            self, tr("day.opened_ok").format(date=business_date)
        )
        self._open_cash.clear()
        self._gap_reason.clear()
        self._load_day()

    def _on_check_readiness(self) -> None:
        business_date = self._business_date()
        try:
            physical_cash = self._parse_optional_pkr(self._physical_cash)
            report = self._days.validate_close(business_date, physical_cash)
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._current_report = report
        self._render_checklist(report)

    def _render_checklist(self, report) -> None:
        self._checklist.setRowCount(len(report.items))
        status_text = {
            "pass": tr("day.st_pass"),
            "fail": tr("day.st_fail"),
            "warning": tr("day.st_warning"),
            "na": tr("day.st_na"),
        }
        from PySide6.QtGui import QColor

        for row, item in enumerate(report.items):
            check = QTableWidgetItem(tr(item.label_key))
            status = QTableWidgetItem(status_text[item.status])
            status.setForeground(QColor(_STATUS_COLORS.get(item.status, "#000000")))
            detail_text = localize_check_detail(item)
            detail = QTableWidgetItem(detail_text)
            detail.setToolTip(detail_text)
            self._checklist.setItem(row, 0, check)
            self._checklist.setItem(row, 1, status)
            self._checklist.setItem(row, 2, detail)
        self._checklist.resizeColumnsToContents()

    def _on_close_day(self) -> None:
        business_date = self._business_date()
        try:
            physical_cash = self._parse_optional_pkr(self._physical_cash)
            if physical_cash is None:
                raise errors.ValidationError(
                    "Enter the counted physical cash before closing the day.",
                    key="err.physical_cash_required",
                )
            report = self._days.validate_close(business_date, physical_cash)
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._current_report = report
        self._render_checklist(report)

        answer = QMessageBox.question(
            self,
            tr("day.confirm_close_title"),
            tr("day.confirm_close_text").format(
                date=timecalc.format_display_date(business_date),
                cash=f"{physical_cash / 100:,.2f}",
                blocking=len(report.blocking_errors),
                warnings=len(report.warnings),
            ),
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Ok:
            return
        try:
            closing = self._days.close_day(
                business_date, physical_cash_paisa=physical_cash
            )
        except errors.CloseBlockedError as exc:
            self._render_checklist(exc.report)
            h.show_error(self, exc)
            return
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        h.show_info(
            self,
            tr("day.closed_ok").format(
                date=timecalc.format_display_date(business_date),
                revision=closing.closing_revision,
            ),
        )
        self._physical_cash.clear()
        self._load_day()

    def _on_reopen_day(self) -> None:
        business_date = self._business_date()
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("day.reopen_title"))
        dialog_layout = QVBoxLayout(dialog)
        label = QLabel(tr("day.reopen_label"), dialog)
        reason_edit = QTextEdit(dialog)
        reason_edit.setPlaceholderText(tr("day.reopen_hint"))
        reason_edit.setMinimumSize(380, 90)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dialog
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        dialog_layout.addWidget(label)
        dialog_layout.addWidget(reason_edit)
        dialog_layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            self._days.reopen_day(
                business_date, reason=reason_edit.toPlainText()
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        h.show_info(
            self,
            tr("day.reopened_ok").format(
                date=timecalc.format_display_date(business_date)
            ),
        )
        self._load_day()

    # ------------------------------------------------------------------
    def retranslate(self) -> None:
        """Re-apply all static texts (called on language change)."""
        self._title.setText(tr("day.title"))
        self._date_label.setText(tr("day.date") + ":")
        self._status_group.setTitle(tr("day.status_section"))
        self._st_status_label.setText(tr("day.status_label") + ":")
        self._st_cash_label.setText(tr("dash.opening_cash") + ":")
        self._st_src_label.setText(tr("day.opening_cash_src") + ":")
        self._st_prev_label.setText(tr("day.prev_closing") + ":")
        self._open_group.setTitle(tr("day.open_section"))
        self._open_cash_label.setText(tr("day.opening_cash_pkr") + ":")
        self._gap_label.setText(tr("day.gap_reason") + ":")
        self._gap_reason.setPlaceholderText(tr("day.gap_reason_hint"))
        self._first_day_hint.setText(tr("day.first_day_hint"))
        self._open_btn.setText(tr("day.open_day"))
        self._open_nonop_btn.setText(tr("day.open_non_operating"))
        self._close_group.setTitle(tr("day.close_section"))
        self._cash_label.setText(tr("day.physical_cash_pkr") + ":")
        self._checklist_title.setText(tr("day.checklist_title"))
        self._checklist.setHorizontalHeaderLabels(
            [tr("day.th_check"), tr("day.th_status"), tr("day.th_detail")]
        )
        self._check_btn.setText(tr("day.check_readiness"))
        self._close_btn.setText(tr("day.close_day"))
        self._reopen_btn.setText(tr("day.reopen_day"))
        self._reopen_hint.setText(tr("day.reopen_hint"))
        # Re-render a visible checklist with the new language.
        if self._current_report is not None:
            self._render_checklist(self._current_report)
