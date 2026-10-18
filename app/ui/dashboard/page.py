"""Dashboard page: branded overview of the current business day.

Every figure is read from the database through services on each refresh;
nothing here is computed in the UI beyond formatting for display.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.calculations import time as timecalc
from app.localization import tr
from app.repositories.business_day_repository import BusinessDayRepository
from app.services.business_day_service import BusinessDayService
from app.services.nozzle_service import NozzleService
from app.ui import _helpers as h


def _station_name(day_service: BusinessDayService) -> str:
    """The configured pump name, or a plain fallback."""
    try:
        with day_service.read_connection() as conn:
            name = BusinessDayRepository.get_setting(conn, "station_name")
    except Exception:  # noqa: BLE001 — never block the dashboard
        name = None
    return (name or "").strip() or tr("dash.default_station_name")


class _Card(QFrame):
    """A colored summary card: title, big value, small sub-line."""

    def __init__(self, accent: str, parent=None) -> None:
        super().__init__(parent)
        self.setProperty("class", "card " + accent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)
        self.title = QLabel(self)
        self.title.setProperty("class", "cardTitle")
        self.value = QLabel(self)
        self.value.setProperty("class", "cardValue")
        self.value.setWordWrap(True)
        self.sub = QLabel(self)
        self.sub.setProperty("class", "cardSub")
        self.sub.setWordWrap(True)
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        layout.addWidget(self.sub)


class DashboardPage(QWidget):
    """Branded day overview: banner, summary cards, readings table."""

    def __init__(
        self,
        day_service: BusinessDayService,
        nozzle_service: NozzleService,
        navigate,
        parent=None,
        *,
        cashbook_service=None,
        accounting_service=None,
        sales_service=None,
        inventory_service=None,
        credit_service=None,
    ) -> None:
        super().__init__(parent)
        self._days = day_service
        self._nozzles = nozzle_service
        self._navigate = navigate
        self._cashbook = cashbook_service
        self._accounting = accounting_service
        self._sales = sales_service
        self._inventory = inventory_service
        self._credit = credit_service

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget(scroll)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        # -- branded banner -------------------------------------------------
        banner = QFrame(body)
        banner.setObjectName("banner")
        banner_layout = QHBoxLayout(banner)
        banner_layout.setContentsMargins(20, 16, 20, 16)
        banner_layout.setSpacing(12)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self._banner_title = QLabel(banner)
        self._banner_title.setObjectName("bannerTitle")
        self._banner_sub = QLabel(banner)
        self._banner_sub.setObjectName("bannerSub")
        title_box.addWidget(self._banner_title)
        title_box.addWidget(self._banner_sub)
        banner_layout.addLayout(title_box, 1)
        self._banner_status = QLabel(banner)
        self._banner_status.setObjectName("bannerStatus")
        banner_layout.addWidget(self._banner_status, 0, Qt.AlignVCenter)
        layout.addWidget(banner)

        # -- summary cards ---------------------------------------------------
        cards = QGridLayout()
        cards.setSpacing(12)
        self._card_cash = _Card("cardAccentGreen", body)
        self._card_petrol = _Card("cardAccentBlue", body)
        self._card_diesel = _Card("cardAccentAmber", body)
        self._card_stock = _Card("cardAccentRed", body)
        self._card_credit = _Card("cardAccentTeal", body)
        cards.addWidget(self._card_cash, 0, 0)
        cards.addWidget(self._card_petrol, 0, 1)
        cards.addWidget(self._card_diesel, 0, 2)
        cards.addWidget(self._card_stock, 1, 0)
        cards.addWidget(self._card_credit, 1, 1)
        layout.addLayout(cards)

        # -- actions -----------------------------------------------------------
        actions = QHBoxLayout()
        actions.setSpacing(10)
        self._open_sheet_btn = QPushButton(body)
        self._open_sheet_btn.setProperty("btn", "accent")
        self._open_sheet_btn.clicked.connect(
            lambda: self._navigate("daily_sheet")
        )
        self._refresh_btn = QPushButton(body)
        self._refresh_btn.clicked.connect(self.refresh)
        actions.addWidget(self._open_sheet_btn)
        actions.addWidget(self._refresh_btn)
        actions.addStretch(1)
        self._last_refresh = QLabel(body)
        self._last_refresh.setProperty("class", "hint")
        actions.addWidget(self._last_refresh)
        layout.addLayout(actions)

        # -- readings table ---------------------------------------------------
        self._readings_group_title = QLabel(body)
        self._readings_group_title.setStyleSheet(
            "font-size: 15px; font-weight: bold; color: #1a2f4b;"
        )
        layout.addWidget(self._readings_group_title)
        self._table = QTableWidget(body)
        self._table.setColumnCount(5)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setMinimumHeight(180)
        layout.addWidget(self._table)
        self._liters_label = QLabel(body)
        self._liters_label.setProperty("class", "hint")
        layout.addWidget(self._liters_label)

        layout.addStretch(1)
        self.retranslate()
        self.refresh()

    # ------------------------------------------------------------------
    def retranslate(self) -> None:
        """Re-apply all static texts (called on language change)."""
        self._banner_title.setText(_station_name(self._days))
        self._card_cash.title.setText(tr("dash2.cash_position"))
        self._card_petrol.title.setText(tr("dash2.petrol_today"))
        self._card_diesel.title.setText(tr("dash2.diesel_today"))
        self._card_stock.title.setText(tr("dash2.stock_alerts"))
        self._card_credit.title.setText(tr("dash2.borrowers_outstanding"))
        self._open_sheet_btn.setText(tr("dash2.open_daily_sheet"))
        self._refresh_btn.setText(tr("dash.refresh"))
        self._readings_group_title.setText(tr("dash.readings_today"))
        self._table.setHorizontalHeaderLabels(
            [
                tr("dash.th_nozzle"),
                tr("dash.th_fuel"),
                tr("dash.th_previous"),
                tr("dash.th_current"),
                tr("dash.th_liters"),
            ]
        )
        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Reload every figure from the database."""
        self._banner_title.setText(_station_name(self._days))
        try:
            day = self._days.get_open_day()
        except Exception as exc:  # noqa: BLE001 — show, don't crash
            h.show_error(self, exc)
            return
        if day is None:
            self._banner_sub.setText(tr("dash.no_open_day"))
            self._banner_status.setText(tr("dash2.status_closed"))
            self._banner_status.setProperty("status", "closed")
            self._banner_status.style().unpolish(self._banner_status)
            self._banner_status.style().polish(self._banner_status)
            for card in (
                self._card_cash,
                self._card_petrol,
                self._card_diesel,
                self._card_stock,
                self._card_credit,
            ):
                card.value.setText("—")
                card.sub.setText("")
            self._table.setRowCount(0)
            self._liters_label.setText(tr("dash.open_day_hint"))
        else:
            self._banner_sub.setText(
                timecalc.format_display_date(day.business_date)
            )
            self._banner_status.setText(tr("dash2.status_open"))
            self._banner_status.setProperty("status", "open")
            self._banner_status.style().unpolish(self._banner_status)
            self._banner_status.style().polish(self._banner_status)
            self._refresh_cards(day)
            self._refresh_readings(day)
        self._last_refresh.setText(
            f"{tr('dash.last_refresh')}: "
            f"{timecalc.format_display_datetime(timecalc.utc_now_iso())}"
        )

    # ------------------------------------------------------------------
    def _refresh_cards(self, day) -> None:
        business_date = day.business_date
        had_fault = False

        # -- cash position ------------------------------------------------
        if self._cashbook is not None:
            try:
                position = self._cashbook.cash_position(business_date)
            except Exception:  # noqa: BLE001
                position = None
                had_fault = True
            if position is not None:
                physical = h.dto_field(
                    position, "physical_cash_paisa", default=None
                )
                expected = h.dto_field(
                    position, "expected_paisa", default=None
                )
                self._card_cash.value.setText(h.format_pkr(physical))
                self._card_cash.sub.setText(
                    f"{tr('dash2.expected')}: {h.format_pkr(expected)}"
                )
            else:
                self._card_cash.value.setText("—")
                self._card_cash.sub.setText("")
        else:
            self._card_cash.value.setText("—")
            self._card_cash.sub.setText("")

        # -- petrol / diesel sold today ------------------------------------
        petrol_ml = petrol_rev = diesel_ml = diesel_rev = None
        if self._sales is not None:
            try:
                sales = self._sales.day_sales(business_date)
                petrol_ml = int(sales.liters_by_product_ml.get("PETROL", 0))
                petrol_rev = int(
                    sales.revenue_by_product_paisa.get("PETROL", 0)
                )
                diesel_ml = int(sales.liters_by_product_ml.get("DIESEL", 0))
                diesel_rev = int(
                    sales.revenue_by_product_paisa.get("DIESEL", 0)
                )
            except Exception:  # noqa: BLE001
                had_fault = True
                pass
        self._card_petrol.value.setText(
            h.format_pkr(petrol_rev) if petrol_rev is not None else "—"
        )
        self._card_petrol.sub.setText(
            f"{h.format_litres(petrol_ml)} {tr('common.litres')}"
            if petrol_ml is not None
            else ""
        )
        self._card_diesel.value.setText(
            h.format_pkr(diesel_rev) if diesel_rev is not None else "—"
        )
        self._card_diesel.sub.setText(
            f"{h.format_litres(diesel_ml)} {tr('common.litres')}"
            if diesel_ml is not None
            else ""
        )

        # -- stock alerts ---------------------------------------------------
        if self._inventory is not None:
            try:
                positions = self._inventory.stock_positions(business_date)
            except Exception:  # noqa: BLE001
                positions = None
                had_fault = True
            if positions is not None:
                alerts = [
                    p
                    for p in positions
                    if (p.variance_ml is not None and p.variance_ml != 0)
                    or p.physical_ml is None
                ]
                n = len(alerts)
                self._card_stock.value.setText(
                    f"{n} {tr('dash2.alerts')}" if n else tr("dash2.all_ok")
                )
                self._card_stock.sub.setText(
                    ", ".join(
                        f"{p.tank_code}"
                        for p in alerts[:3]
                    )
                    if alerts
                    else tr("dash2.dips_match")
                )
            else:
                self._card_stock.value.setText("—")
                self._card_stock.sub.setText("")
        else:
            self._card_stock.value.setText("—")
            self._card_stock.sub.setText("")

        # -- borrowers outstanding -------------------------------------------
        if self._credit is not None:
            try:
                customers = self._credit.outstanding_all()
            except Exception:  # noqa: BLE001
                customers = None
                had_fault = True
            if customers is not None:
                debtors = [c for c in customers if c.outstanding_paisa > 0]
                total = sum(c.outstanding_paisa for c in debtors)
                self._card_credit.value.setText(h.format_pkr(total))
                self._card_credit.sub.setText(
                    tr("dash2.borrowers_count").format(n=len(debtors))
                )
            else:
                self._card_credit.value.setText("—")
                self._card_credit.sub.setText("")
        else:
            self._card_credit.value.setText("—")
            self._card_credit.sub.setText("")

        # If any section failed, say so — "—" alone is indistinguishable
        # from "no data", and a locked/corrupt DB must not look normal.
        if had_fault:
            self._banner_sub.setText(
                f"{self._banner_sub.text()} • {tr('dash2.data_fault')}"
            )

    # ------------------------------------------------------------------
    def _refresh_readings(self, day) -> None:
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
