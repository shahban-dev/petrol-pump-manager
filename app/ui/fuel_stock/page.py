"""Fuel Stock page: per-tank stock positions, counts, prices, snapshots.

Stock positions, day sales and prices are computed by services; the UI
only parses inputs (litres, PKR, datetimes) and formats the results.
The mid-day price-change workflow lives here: set a new price and record
the meter snapshot readings each active nozzle needs first.
"""

from __future__ import annotations

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.calculations import time as timecalc
from app.localization import tr
from app.services import errors
from app.ui import _helpers as h


class FuelStockPage(QWidget):
    """Per-tank stock positions, physical counts, adjustments and prices."""

    def __init__(
        self,
        inventory_service,
        price_service,
        sales_service,
        nozzle_service,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._inventory = inventory_service
        self._prices = price_service
        self._sales = sales_service
        self._nozzles = nozzle_service
        self._tanks: list = []
        self._products: list = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self._title = QLabel(self)
        self._title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(self._title)

        # -- date row ------------------------------------------------------
        date_row = QHBoxLayout()
        self._date_label = QLabel(self)
        self._date_edit = QDateEdit(self)
        self._date_edit.setDisplayFormat("dd-MM-yyyy")
        self._date_edit.setDate(QDate.currentDate())
        self._date_edit.setCalendarPopup(True)
        self._date_edit.dateChanged.connect(self.refresh)
        self._refresh_btn = QPushButton(self)
        self._refresh_btn.clicked.connect(self.refresh)
        date_row.addWidget(self._date_label)
        date_row.addWidget(self._date_edit)
        date_row.addWidget(self._refresh_btn)
        date_row.addStretch(1)
        layout.addLayout(date_row)

        # -- stock positions -----------------------------------------------
        self._positions_group = QGroupBox(self)
        positions_layout = QVBoxLayout(self._positions_group)
        self._table = QTableWidget(self)
        self._table.setColumnCount(9)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setMinimumHeight(180)
        positions_layout.addWidget(self._table)
        self._sales_label = QLabel(self)
        self._sales_label.setWordWrap(True)
        positions_layout.addWidget(self._sales_label)
        layout.addWidget(self._positions_group)

        # -- action buttons -------------------------------------------------
        buttons = QHBoxLayout()
        self._physical_btn = QPushButton(self)
        self._physical_btn.clicked.connect(self._on_physical_count)
        self._correct_btn = QPushButton(self)
        self._correct_btn.clicked.connect(self._on_correct_physical)
        self._adjust_btn = QPushButton(self)
        self._adjust_btn.clicked.connect(self._on_adjustment)
        self._opening_btn = QPushButton(self)
        self._opening_btn.clicked.connect(self._on_opening_stock)
        self._post_btn = QPushButton(self)
        self._post_btn.clicked.connect(self._on_post_sales)
        buttons.addWidget(self._physical_btn)
        buttons.addWidget(self._correct_btn)
        buttons.addWidget(self._adjust_btn)
        buttons.addWidget(self._opening_btn)
        buttons.addWidget(self._post_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        # -- prices ----------------------------------------------------------
        self._price_group = QGroupBox(self)
        price_layout = QVBoxLayout(self._price_group)
        price_top = QHBoxLayout()
        self._product_label = QLabel(self)
        self._product_combo = QComboBox(self)
        self._product_combo.currentIndexChanged.connect(self._refresh_prices)
        self._set_price_btn = QPushButton(self)
        self._set_price_btn.clicked.connect(self._on_set_price)
        price_top.addWidget(self._product_label)
        price_top.addWidget(self._product_combo, 1)
        price_top.addWidget(self._set_price_btn)
        price_layout.addLayout(price_top)
        self._price_table = QTableWidget(self)
        self._price_table.setColumnCount(2)
        self._price_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._price_table.horizontalHeader().setStretchLastSection(True)
        self._price_table.setMinimumHeight(110)
        price_layout.addWidget(self._price_table)
        layout.addWidget(self._price_group)

        # -- meter snapshot (for mid-day price changes) ---------------------
        self._snapshot_group = QGroupBox(self)
        snapshot_layout = QVBoxLayout(self._snapshot_group)
        self._snapshot_hint = QLabel(self)
        self._snapshot_hint.setWordWrap(True)
        self._snapshot_hint.setStyleSheet("color: gray;")
        snapshot_layout.addWidget(self._snapshot_hint)
        snapshot_row = QHBoxLayout()
        self._nozzle_label = QLabel(self)
        self._nozzle_combo = QComboBox(self)
        self._snap_qty_label = QLabel(self)
        self._snap_qty_edit = h.make_integer_edit(self)
        self._snap_qty_edit.setAlignment(Qt.AlignRight)
        self._snap_time_label = QLabel(self)
        self._snap_time_edit = QDateTimeEdit(self)
        self._snap_time_edit.setDisplayFormat("dd-MM-yyyy HH:mm")
        self._snap_time_edit.setCalendarPopup(True)
        self._snapshot_btn = QPushButton(self)
        self._snapshot_btn.clicked.connect(self._on_snapshot)
        snapshot_row.addWidget(self._nozzle_label)
        snapshot_row.addWidget(self._nozzle_combo, 1)
        snapshot_row.addWidget(self._snap_qty_label)
        snapshot_row.addWidget(self._snap_qty_edit)
        snapshot_row.addWidget(self._snap_time_label)
        snapshot_row.addWidget(self._snap_time_edit)
        snapshot_row.addWidget(self._snapshot_btn)
        snapshot_layout.addLayout(snapshot_row)
        layout.addWidget(self._snapshot_group)

        layout.addStretch(1)
        self.retranslate()
        self.refresh()

    # ------------------------------------------------------------------
    def _business_date(self) -> str:
        return self._date_edit.date().toString("yyyy-MM-dd")

    def retranslate(self) -> None:
        """Re-apply all static texts (called on language change)."""
        self._title.setText(tr("stock.title"))
        self._date_label.setText(tr("stock.date") + ":")
        self._refresh_btn.setText(tr("stock.refresh"))
        self._positions_group.setTitle(tr("stock.positions_section"))
        self._table.setHorizontalHeaderLabels(
            [
                tr("stock.th_tank"),
                tr("stock.th_product"),
                tr("stock.th_opening_l"),
                tr("stock.th_received_l"),
                tr("stock.th_sold_l"),
                tr("stock.th_adjust_l"),
                tr("stock.th_theoretical_l"),
                tr("stock.th_physical_l"),
                tr("stock.th_variance_l"),
            ]
        )
        self._physical_btn.setText(tr("stock.btn_physical"))
        self._correct_btn.setText(tr("stock.btn_correct_physical"))
        self._adjust_btn.setText(tr("stock.btn_adjustment"))
        self._opening_btn.setText(tr("stock.btn_opening_stock"))
        self._post_btn.setText(tr("stock.btn_post_sales"))
        self._price_group.setTitle(tr("stock.price_section"))
        self._product_label.setText(tr("stock.product_label") + ":")
        self._set_price_btn.setText(tr("stock.btn_set_price"))
        self._price_table.setHorizontalHeaderLabels(
            [tr("stock.th_effective_from"), tr("stock.th_price_pkr_l")]
        )
        self._snapshot_group.setTitle(tr("stock.snapshot_section"))
        self._snapshot_hint.setText(tr("stock.snapshot_hint"))
        self._nozzle_label.setText(tr("stock.nozzle_label") + ":")
        self._snap_qty_label.setText(tr("stock.qty_l_label") + ":")
        self._snap_time_label.setText(tr("stock.snapshot_time_label") + ":")
        self._snapshot_btn.setText(tr("stock.btn_snapshot"))
        self._snap_time_edit.setDateTime(h.karachi_now_qdatetime())
        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Reload positions, sales summary, products, prices and nozzles."""
        self._refresh_tanks_products()
        self._refresh_positions()
        self._refresh_sales_summary()
        self._refresh_prices()

    def _refresh_tanks_products(self) -> None:
        try:
            self._tanks = self._inventory.list_tanks()
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        try:
            self._products = self._prices.list_fuel_products()
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        current = self._product_combo.currentData()
        self._product_combo.blockSignals(True)
        self._product_combo.clear()
        for product in self._products:
            label, product_id = h.product_combo_entry(product)
            self._product_combo.addItem(label, product_id)
        if current is not None:
            index = self._product_combo.findData(current)
            if index >= 0:
                self._product_combo.setCurrentIndex(index)
        self._product_combo.blockSignals(False)
        self._refresh_snapshot_nozzles()

    def _refresh_positions(self) -> None:
        try:
            positions = self._inventory.stock_positions(self._business_date())
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._table.setRowCount(len(positions))
        for row, pos in enumerate(positions):
            values = [
                pos.tank_code,
                pos.fuel_product_code,
                h.format_litres(pos.opening_ml),
                h.format_litres(pos.received_ml),
                h.format_litres(pos.sold_ml),
                h.format_litres(pos.adjustments_ml),
                h.format_litres(pos.theoretical_ml),
                h.format_litres(pos.physical_ml),
                h.format_litres(pos.variance_ml),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col >= 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._table.setItem(row, col, item)
        self._table.resizeColumnsToContents()

    def _refresh_sales_summary(self) -> None:
        try:
            summary = self._sales.day_sales(self._business_date())
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        liters, revenue = self._sales_maps(summary)
        parts = []
        for code in sorted(liters):
            parts.append(
                f"{code}: {h.format_litres(liters[code])}"
                f" {tr('common.litres')} (PKR {h.format_pkr(revenue.get(code))})"
            )
        self._sales_label.setText(
            tr("stock.sales_summary").format(detail="; ".join(parts) or "—")
        )

    @staticmethod
    def _sales_maps(summary) -> tuple[dict, dict]:
        """{product code: ml}, {product code: paisa} from day_sales output.

        Primary shape is the DaySales DTO from the service contract; a dict
        ({"products": [...]} or {code: row}) or a list of per-product rows is
        accepted as a fallback.
        """
        if hasattr(summary, "liters_by_product_ml"):
            return (
                dict(summary.liters_by_product_ml or {}),
                dict(summary.revenue_by_product_paisa or {}),
            )
        rows = (
            summary.get("products", summary.get("lines", summary))
            if isinstance(summary, dict)
            else summary
        )
        items = (
            rows.items()
            if isinstance(rows, dict)
            else [
                (
                    h.dto_field(
                        row, "fuel_product_code", "product_code", "code"
                    ),
                    row,
                )
                for row in rows
            ]
        )
        liters: dict = {}
        revenue: dict = {}
        for code, row in items:
            if isinstance(row, int) and not isinstance(row, bool):
                revenue[code] = row
                continue
            if isinstance(row, dict):
                liters[code] = row.get("liters_ml", row.get("quantity_ml"))
                revenue[code] = row.get(
                    "revenue_paisa", row.get("total_revenue_paisa")
                )
            else:
                liters[code] = h.dto_field(
                    row,
                    "liters_ml",
                    "quantity_ml",
                    "liters_by_product_ml",
                    default=None,
                )
                revenue[code] = h.dto_field(
                    row,
                    "revenue_paisa",
                    "total_revenue_paisa",
                    "amount_paisa",
                    default=None,
                )
        return liters, revenue

    def _refresh_prices(self) -> None:
        product_id = self._product_combo.currentData()
        if product_id is None:
            self._price_table.setRowCount(0)
            return
        try:
            prices = self._prices.list_prices(product_id)
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._price_table.setRowCount(len(prices))
        for row, price in enumerate(prices):
            effective = QTableWidgetItem(
                timecalc.format_display_datetime(price.effective_from_utc)
            )
            value = QTableWidgetItem(h.format_unit_price(price.unit_price_10k))
            value.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._price_table.setItem(row, 0, effective)
            self._price_table.setItem(row, 1, value)
        self._price_table.resizeColumnsToContents()
        self._refresh_snapshot_nozzles()

    def _refresh_snapshot_nozzles(self) -> None:
        """Nozzle combo follows the price section's selected product."""
        product_id = self._product_combo.currentData()
        try:
            nozzles = self._nozzles.list_nozzles()
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        current = self._nozzle_combo.currentData()
        self._nozzle_combo.clear()
        for nozzle in nozzles:
            if product_id is not None and nozzle.fuel_product_id != product_id:
                continue
            self._nozzle_combo.addItem(
                f"{nozzle.code} ({nozzle.fuel_product_code})", nozzle.id
            )
        if current is not None:
            index = self._nozzle_combo.findData(current)
            if index >= 0:
                self._nozzle_combo.setCurrentIndex(index)

    # ------------------------------------------------------------------
    # dialogs
    # ------------------------------------------------------------------
    def _tank_combo(self, dialog) -> QComboBox:
        combo = QComboBox(dialog)
        for tank in self._tanks:
            combo.addItem(f"{tank.code} ({tank.fuel_product_code})", tank.id)
        return combo

    def _accept_buttons(self, dialog) -> QDialogButtonBox:
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dialog
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        return buttons

    def _on_physical_count(self) -> None:
        """Dialog: record a dip/physical tank count for the date."""
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("stock.physical_title"))
        form = QFormLayout(dialog)
        tank_combo = self._tank_combo(dialog)
        if tank_combo.count() == 0:
            h.show_error(
                self, errors.NotFoundError("No tanks configured.")
            )
            return
        qty_edit = h.make_integer_edit(dialog)
        measured_edit = QLineEdit(dialog)
        note_edit = QLineEdit(dialog)
        form.addRow(QLabel(tr("stock.tank_label"), dialog), tank_combo)
        form.addRow(QLabel(tr("stock.qty_l_label"), dialog), qty_edit)
        form.addRow(QLabel(tr("stock.measured_by_label"), dialog), measured_edit)
        form.addRow(QLabel(tr("stock.note_label"), dialog), note_edit)
        form.addRow(self._accept_buttons(dialog))
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            self._inventory.record_physical_count(
                tank_combo.currentData(),
                self._business_date(),
                h.parse_litres(qty_edit.text()),
                measured_by=measured_edit.text().strip() or None,
                note=note_edit.text().strip() or None,
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        h.show_info(self, tr("stock.physical_saved_ok"))
        self.refresh()

    def _on_correct_physical(self) -> None:
        """Dialog: correct a physical count (original is preserved)."""
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("stock.correct_physical_title"))
        form = QFormLayout(dialog)
        tank_combo = self._tank_combo(dialog)
        if tank_combo.count() == 0:
            h.show_error(
                self, errors.NotFoundError("No tanks configured.")
            )
            return
        qty_edit = h.make_integer_edit(dialog)
        reason_edit = QTextEdit(dialog)
        reason_edit.setMinimumSize(340, 70)
        form.addRow(QLabel(tr("stock.tank_label"), dialog), tank_combo)
        form.addRow(QLabel(tr("stock.qty_l_label"), dialog), qty_edit)
        form.addRow(QLabel(tr("stock.reason_label"), dialog), reason_edit)
        form.addRow(self._accept_buttons(dialog))
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            self._inventory.correct_physical_count(
                tank_combo.currentData(),
                self._business_date(),
                h.parse_litres(qty_edit.text()),
                reason=reason_edit.toPlainText(),
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        h.show_info(self, tr("stock.physical_corrected_ok"))
        self.refresh()

    def _on_adjustment(self) -> None:
        """Dialog: record a signed stock adjustment with a reason."""
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("stock.adjustment_title"))
        form = QFormLayout(dialog)
        tank_combo = self._tank_combo(dialog)
        if tank_combo.count() == 0:
            h.show_error(
                self, errors.NotFoundError("No tanks configured.")
            )
            return
        qty_edit = h.make_signed_integer_edit(dialog)
        product_combo = QComboBox(dialog)
        for product in self._products:
            label, product_id = h.product_combo_entry(product)
            product_combo.addItem(label, product_id)
        reason_edit = QTextEdit(dialog)
        reason_edit.setMinimumSize(340, 70)
        authorized_edit = QLineEdit(dialog)
        form.addRow(QLabel(tr("stock.tank_label"), dialog), tank_combo)
        form.addRow(QLabel(tr("stock.product_label"), dialog), product_combo)
        form.addRow(QLabel(tr("stock.qty_signed_l_label"), dialog), qty_edit)
        form.addRow(QLabel(tr("stock.reason_label"), dialog), reason_edit)
        form.addRow(
            QLabel(tr("stock.authorized_by_label"), dialog), authorized_edit
        )
        form.addRow(self._accept_buttons(dialog))
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            self._inventory.record_adjustment(
                fuel_product=product_combo.currentData(),
                tank=tank_combo.currentData(),
                business_date=self._business_date(),
                quantity_litres_signed=h.parse_signed_litres(qty_edit.text()),
                reason=reason_edit.toPlainText(),
                authorized_by=authorized_edit.text().strip() or None,
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        h.show_info(self, tr("stock.adjustment_saved_ok"))
        self.refresh()

    def _on_opening_stock(self) -> None:
        """Dialog: set the opening stock of a fuel product for the date."""
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("stock.opening_stock_title"))
        form = QFormLayout(dialog)
        product_combo = QComboBox(dialog)
        for product in self._products:
            label, product_id = h.product_combo_entry(product)
            product_combo.addItem(label, product_id)
        if product_combo.count() == 0:
            h.show_error(
                self, errors.NotFoundError("No fuel products configured.")
            )
            return
        qty_edit = h.make_integer_edit(dialog)
        form.addRow(QLabel(tr("stock.product_label"), dialog), product_combo)
        form.addRow(QLabel(tr("stock.qty_l_label"), dialog), qty_edit)
        form.addRow(self._accept_buttons(dialog))
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            self._inventory.set_opening_stock(
                product_combo.currentData(),
                h.parse_litres(qty_edit.text()),
                self._business_date(),
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        h.show_info(self, tr("stock.opening_stock_saved_ok"))
        self.refresh()

    def _on_post_sales(self) -> None:
        """Post the day's sales; report posted/already-posted/skipped counts."""
        try:
            result = self._sales.post_day_sales(self._business_date())
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        h.show_info(
            self,
            tr("stock.sales_posted").format(
                posted=result.get("posted", 0),
                already=result.get("already_posted", 0),
                corrected=result.get("corrected", 0),
                skipped=result.get("skipped_unknown_qty", 0),
            ),
        )
        self.refresh()

    def _on_set_price(self) -> None:
        """Dialog: set a new fuel price effective at a chosen date/time."""
        product_id = self._product_combo.currentData()
        if product_id is None:
            h.show_error(
                self,
                errors.BusinessRuleError("Select a fuel product first."),
            )
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("stock.set_price_title"))
        form = QFormLayout(dialog)
        price_edit = h.make_decimal_edit(dialog, decimals=4)
        effective_edit = QDateTimeEdit(dialog)
        effective_edit.setDisplayFormat("dd-MM-yyyy HH:mm")
        effective_edit.setCalendarPopup(True)
        effective_edit.setDateTime(h.karachi_now_qdatetime())
        hint = QLabel(tr("stock.snapshot_hint"), dialog)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        form.addRow(QLabel(tr("stock.price_pkr_l_label"), dialog), price_edit)
        form.addRow(
            QLabel(tr("stock.effective_from_label"), dialog), effective_edit
        )
        form.addRow(hint)
        form.addRow(self._accept_buttons(dialog))
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            price_pkr = h.parse_pkr(price_edit.text())
            effective_utc = h.qdatetime_karachi_to_utc_iso(
                effective_edit.dateTime()
            )
            self._prices.set_price(
                product_id,
                price_pkr,
                effective_utc,
                self._business_date(),
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        h.show_info(self, tr("stock.price_saved_ok"))
        self.refresh()

    def _on_snapshot(self) -> None:
        """Record a segment snapshot reading on one nozzle."""
        nozzle_id = self._nozzle_combo.currentData()
        if nozzle_id is None:
            h.show_error(
                self,
                errors.BusinessRuleError("No nozzle selected."),
            )
            return
        try:
            litres = h.parse_litres(self._snap_qty_edit.text())
            occurred_at_utc = h.qdatetime_karachi_to_utc_iso(
                self._snap_time_edit.dateTime()
            )
            self._nozzles.record_segment_reading(
                self._business_date(),
                nozzle_id,
                litres,
                occurred_at_utc=occurred_at_utc,
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._snap_qty_edit.clear()
        h.show_info(self, tr("stock.snapshot_saved_ok"))
        self.refresh()
