"""Dashboard page: read-only overview of the current business day.

Every figure is read from the database through services on each refresh;
nothing here is computed in the UI beyond formatting for display.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.calculations import time as timecalc
from app.localization import tr
from app.services.business_day_service import BusinessDayService
from app.services.nozzle_service import NozzleService
from app.ui import _helpers as h


class DashboardPage(QWidget):
    """Read-only day overview with navigation shortcuts."""

    def __init__(
        self,
        day_service: BusinessDayService,
        nozzle_service: NozzleService,
        navigate,
        parent=None,
        *,
        cashbook_service=None,
        accounting_service=None,
    ) -> None:
        super().__init__(parent)
        self._days = day_service
        self._nozzles = nozzle_service
        self._navigate = navigate
        self._cashbook = cashbook_service
        self._accounting = accounting_service

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self._title = QLabel(self)
        self._title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(self._title)

        # -- current day card -------------------------------------------------
        self._day_group = QGroupBox(self)
        day_form = QFormLayout(self._day_group)
        self._day_value = QLabel(self)
        self._status_value = QLabel(self)
        self._opening_cash_value = QLabel(self)
        self._physical_cash_value = QLabel(self)
        self._revision_value = QLabel(self)
        self._day_label = QLabel(self)
        self._status_label = QLabel(self)
        self._opening_cash_label = QLabel(self)
        self._physical_cash_label = QLabel(self)
        self._revision_label = QLabel(self)
        day_form.addRow(self._day_label, self._day_value)
        day_form.addRow(self._status_label, self._status_value)
        day_form.addRow(self._opening_cash_label, self._opening_cash_value)
        day_form.addRow(self._physical_cash_label, self._physical_cash_value)
        day_form.addRow(self._revision_label, self._revision_value)
        layout.addWidget(self._day_group)

        # -- cash & P&L card ---------------------------------------------------
        self._cash_group = QGroupBox(self)
        cash_form = QFormLayout(self._cash_group)
        self._cash_expected_label = QLabel(self)
        self._cash_expected_value = QLabel(self)
        self._cash_physical_label = QLabel(self)
        self._cash_physical_value = QLabel(self)
        self._cash_diff_label = QLabel(self)
        self._cash_diff_value = QLabel(self)
        self._cash_shortage_label = QLabel(self)
        self._cash_shortage_value = QLabel(self)
        self._cash_surplus_label = QLabel(self)
        self._cash_surplus_value = QLabel(self)
        self._cash_revenue_label = QLabel(self)
        self._cash_revenue_value = QLabel(self)
        self._cash_gross_label = QLabel(self)
        self._cash_gross_value = QLabel(self)
        self._cash_net_label = QLabel(self)
        self._cash_net_value = QLabel(self)
        self._cash_recv_label = QLabel(self)
        self._cash_recv_value = QLabel(self)
        cash_form.addRow(self._cash_expected_label, self._cash_expected_value)
        cash_form.addRow(self._cash_physical_label, self._cash_physical_value)
        cash_form.addRow(self._cash_diff_label, self._cash_diff_value)
        cash_form.addRow(self._cash_shortage_label, self._cash_shortage_value)
        cash_form.addRow(self._cash_surplus_label, self._cash_surplus_value)
        cash_form.addRow(self._cash_revenue_label, self._cash_revenue_value)
        cash_form.addRow(self._cash_gross_label, self._cash_gross_value)
        cash_form.addRow(self._cash_net_label, self._cash_net_value)
        cash_form.addRow(self._cash_recv_label, self._cash_recv_value)
        layout.addWidget(self._cash_group)

        # -- readings table ---------------------------------------------------
        self._readings_group = QGroupBox(self)
        readings_layout = QVBoxLayout(self._readings_group)
        self._table = QTableWidget(self)
        self._table.setColumnCount(5)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.horizontalHeader().setStretchLastSection(True)
        readings_layout.addWidget(self._table)
        self._liters_label = QLabel(self)
        readings_layout.addWidget(self._liters_label)
        layout.addWidget(self._readings_group)

        # -- actions -----------------------------------------------------------
        actions = QHBoxLayout()
        self._refresh_btn = QPushButton(self)
        self._refresh_btn.clicked.connect(self.refresh)
        self._go_readings_btn = QPushButton(self)
        self._go_readings_btn.clicked.connect(lambda: self._navigate(2))
        self._go_register_btn = QPushButton(self)
        self._go_register_btn.clicked.connect(lambda: self._navigate(1))
        self._go_cashbook_btn = QPushButton(self)
        self._go_cashbook_btn.clicked.connect(lambda: self._navigate(7))
        self._go_suppliers_btn = QPushButton(self)
        self._go_suppliers_btn.clicked.connect(lambda: self._navigate(8))
        self._go_materials_btn = QPushButton(self)
        self._go_materials_btn.clicked.connect(lambda: self._navigate(9))
        self._go_expenses_btn = QPushButton(self)
        self._go_expenses_btn.clicked.connect(lambda: self._navigate(10))
        actions.addWidget(self._refresh_btn)
        actions.addWidget(self._go_readings_btn)
        actions.addWidget(self._go_register_btn)
        actions.addWidget(self._go_cashbook_btn)
        actions.addWidget(self._go_suppliers_btn)
        actions.addWidget(self._go_materials_btn)
        actions.addWidget(self._go_expenses_btn)
        actions.addStretch(1)
        self._last_refresh = QLabel(self)
        self._last_refresh.setStyleSheet("color: gray;")
        actions.addWidget(self._last_refresh)
        layout.addLayout(actions)

        layout.addStretch(1)
        self.retranslate()
        self.refresh()

    # ------------------------------------------------------------------
    def retranslate(self) -> None:
        """Re-apply all static texts (called on language change)."""
        self._title.setText(tr("dash.title"))
        self._day_group.setTitle(tr("dash.current_day"))
        self._day_label.setText(tr("day.date") + ":")
        self._status_label.setText(tr("dash.status_label") + ":")
        self._opening_cash_label.setText(tr("dash.opening_cash") + ":")
        self._physical_cash_label.setText(tr("dash.physical_cash") + ":")
        self._revision_label.setText(tr("dash.closing_revision") + ":")
        self._readings_group.setTitle(tr("dash.readings_today"))
        self._table.setHorizontalHeaderLabels(
            [
                tr("dash.th_nozzle"),
                tr("dash.th_fuel"),
                tr("dash.th_previous"),
                tr("dash.th_current"),
                tr("dash.th_liters"),
            ]
        )
        self._refresh_btn.setText(tr("dash.refresh"))
        self._go_readings_btn.setText(tr("dash.go_readings"))
        self._go_register_btn.setText(tr("dash.go_register"))
        self._go_cashbook_btn.setText(tr("dash.go_cashbook"))
        self._go_suppliers_btn.setText(tr("dash.go_suppliers"))
        self._go_materials_btn.setText(tr("dash.go_other_materials"))
        self._go_expenses_btn.setText(tr("dash.go_expenses"))
        self._cash_group.setTitle(tr("dash.cash_pnl"))
        self._cash_expected_label.setText(tr("dash.expected_cash") + ":")
        self._cash_physical_label.setText(tr("dash.physical_cash") + ":")
        self._cash_diff_label.setText(tr("dash.cash_difference") + ":")
        self._cash_shortage_label.setText(tr("dash.shortage") + ":")
        self._cash_surplus_label.setText(tr("dash.surplus") + ":")
        self._cash_revenue_label.setText(tr("dash.revenue") + ":")
        self._cash_gross_label.setText(tr("dash.gross_profit") + ":")
        self._cash_net_label.setText(tr("dash.net_profit") + ":")
        self._cash_recv_label.setText(tr("dash.new_receivables") + ":")

    def refresh(self) -> None:
        """Reload every figure from the database."""
        try:
            day = self._days.get_open_day()
        except Exception as exc:  # noqa: BLE001 — show, don't crash
            h.show_error(self, exc)
            return
        if day is None:
            self._day_value.setText(tr("dash.no_open_day"))
            self._status_value.setText("—")
            self._opening_cash_value.setText("—")
            self._physical_cash_value.setText("—")
            self._revision_value.setText("—")
            self._table.setRowCount(0)
            self._liters_label.setText(tr("dash.open_day_hint"))
        else:
            self._day_value.setText(timecalc.format_display_date(day.business_date))
            self._status_value.setText(day.status)
            self._opening_cash_value.setText(h.format_pkr(day.opening_cash_paisa))
            self._physical_cash_value.setText(h.format_pkr(day.physical_cash_paisa))
            self._revision_value.setText(
                str(day.closing_revision) if day.closing_revision else "—"
            )
            try:
                readings = self._nozzles.day_readings(day.business_date)
            except Exception as exc:  # noqa: BLE001
                h.show_error(self, exc)
                return
            self._table.setRowCount(len(readings))
            liters: dict[str, int] = {}
            for row, reading in enumerate(readings):
                self._table.setItem(row, 0, QTableWidgetItem(reading.nozzle_code))
                self._table.setItem(
                    row, 1, QTableWidgetItem(reading.fuel_product_code)
                )
                prev = QTableWidgetItem(h.format_litres(reading.previous_ml))
                curr = QTableWidgetItem(h.format_litres(reading.current_ml))
                sold = QTableWidgetItem(h.format_litres(reading.liters_sold_ml))
                for item in (prev, curr, sold):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._table.setItem(row, 2, prev)
                self._table.setItem(row, 3, curr)
                self._table.setItem(row, 4, sold)
                if reading.liters_sold_ml is not None:
                    liters[reading.fuel_product_code] = (
                        liters.get(reading.fuel_product_code, 0)
                        + reading.liters_sold_ml
                    )
            summary = ", ".join(
                f"{product}: {h.format_litres(ml)} {tr('common.litres')}"
                for product, ml in sorted(liters.items())
            )
            self._liters_label.setText(
                f"{tr('dash.liters_by_product')}: {summary or '—'}"
            )
        self._refresh_cash_pnl(day)
        self._last_refresh.setText(
            f"{tr('dash.last_refresh')}: "
            f"{timecalc.format_display_datetime(timecalc.utc_now_iso())}"
        )

    # ------------------------------------------------------------------
    def _refresh_cash_pnl(self, day) -> None:
        """Fill the cash & P&L card from CashbookService / AccountingService.

        Every figure is service-computed; shortage/surplus are shown
        separately and new receivables are shown separately (never as
        revenue). Estimates are labelled, never presented as fact.
        """
        blanks = (
            self._cash_expected_value,
            self._cash_physical_value,
            self._cash_diff_value,
            self._cash_shortage_value,
            self._cash_surplus_value,
            self._cash_revenue_value,
            self._cash_gross_value,
            self._cash_net_value,
            self._cash_recv_value,
        )
        business_date = None
        if day is not None:
            business_date = day.business_date
        if (
            business_date is None
            or self._cashbook is None
            or self._accounting is None
        ):
            for widget in blanks:
                widget.setText("—")
            return
        try:
            position = self._cashbook.cash_position(business_date)
        except Exception as exc:  # noqa: BLE001 — show, don't crash
            h.show_error(self, exc)
            return
        self._cash_expected_value.setText(
            h.format_pkr(h.dto_field(position, "expected_paisa", default=None))
        )
        self._cash_physical_value.setText(
            h.format_pkr(
                h.dto_field(position, "physical_cash_paisa", default=None)
            )
        )
        self._cash_diff_value.setText(
            h.format_pkr(h.dto_field(position, "difference_paisa", default=None))
        )
        shortage = h.dto_field(position, "shortage_paisa", default=None)
        self._cash_shortage_value.setText(
            h.format_pkr(shortage) if shortage else "—"
        )
        surplus = h.dto_field(position, "surplus_paisa", default=None)
        self._cash_surplus_value.setText(
            h.format_pkr(surplus) if surplus else "—"
        )
        try:
            pnl = self._accounting.daily_pnl(business_date)
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        estimated = bool(h.dto_field(pnl, "estimated", default=False))
        tag = f" {tr('dash.estimated_tag')}" if estimated else ""
        self._cash_revenue_value.setText(
            h.format_pkr(h.dto_field(pnl, "total_revenue_paisa", default=None))
        )
        gross = h.dto_field(pnl, "gross_profit_paisa", default=None)
        self._cash_gross_value.setText(h.format_pkr(gross) + tag)
        net = h.dto_field(pnl, "net_profit_paisa", default=None)
        self._cash_net_value.setText(h.format_pkr(net) + tag)
        self._cash_recv_value.setText(
            h.format_pkr(
                h.dto_field(pnl, "receivables_created_paisa", default=None)
            )
        )
