"""Fuel Deliveries page: record supplier deliveries and watch balances.

Suppliers, products and tanks are loaded through services; deliveries for
the selected business date are listed with their payment status. The UI
parses inputs and formats service-computed values — it does no
accounting math of its own.
"""

from __future__ import annotations

from decimal import Decimal

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
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
    QVBoxLayout,
    QWidget,
)

from app.calculations import time as timecalc
from app.localization import tr
from app.services import errors
from app.ui import _helpers as h

#: DeliveryInfo.status values -> tr() keys.
_STATUS_KEYS = {
    "unpaid": "delivery.status_unpaid",
    "partial": "delivery.status_partial",
    "paid": "delivery.status_paid",
}


def _format_unit_cost(value) -> str:
    """Format a unit cost that may be int 0.0001-PKR units or Decimal PKR."""
    if value is None:
        return h.format_unit_price(None)
    if isinstance(value, int) and not isinstance(value, bool):
        return h.format_unit_price(value)
    try:
        return f"{Decimal(value):,.4f}"
    except Exception:  # noqa: BLE001 — show something rather than crash
        return str(value)


class FuelDeliveriesPage(QWidget):
    """Supplier fuel deliveries for one business day."""

    def __init__(
        self,
        delivery_service,
        inventory_service,
        price_service,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._deliveries = delivery_service
        self._inventory = inventory_service
        self._prices = price_service
        self._tanks: list = []

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

        # -- deliveries table ----------------------------------------------
        self._deliveries_group = QGroupBox(self)
        deliveries_layout = QVBoxLayout(self._deliveries_group)
        self._table = QTableWidget(self)
        self._table.setColumnCount(9)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setMinimumHeight(200)
        deliveries_layout.addWidget(self._table)
        layout.addWidget(self._deliveries_group)

        # -- record delivery form ------------------------------------------
        self._record_group = QGroupBox(self)
        form = QFormLayout(self._record_group)

        supplier_row = QHBoxLayout()
        self._supplier_combo = QComboBox(self)
        self._supplier_combo.currentIndexChanged.connect(self._update_balance)
        self._add_supplier_btn = QPushButton(self)
        self._add_supplier_btn.clicked.connect(self._on_add_supplier)
        supplier_row.addWidget(self._supplier_combo, 1)
        supplier_row.addWidget(self._add_supplier_btn)
        self._supplier_label = QLabel(self)
        form.addRow(self._supplier_label, supplier_row)

        self._product_label = QLabel(self)
        self._product_combo = QComboBox(self)
        self._product_combo.currentIndexChanged.connect(self._on_product_changed)
        form.addRow(self._product_label, self._product_combo)

        self._tank_label = QLabel(self)
        self._tank_combo = QComboBox(self)
        form.addRow(self._tank_label, self._tank_combo)

        self._qty_label = QLabel(self)
        self._qty_edit = QLineEdit(self)
        self._qty_edit.setAlignment(Qt.AlignRight)
        form.addRow(self._qty_label, self._qty_edit)

        self._unit_cost_label = QLabel(self)
        self._unit_cost_edit = QLineEdit(self)
        self._unit_cost_edit.setAlignment(Qt.AlignRight)
        form.addRow(self._unit_cost_label, self._unit_cost_edit)

        self._invoice_label = QLabel(self)
        self._invoice_edit = QLineEdit(self)
        form.addRow(self._invoice_label, self._invoice_edit)

        self._other_costs_label = QLabel(self)
        self._other_costs_edit = QLineEdit(self)
        self._other_costs_edit.setAlignment(Qt.AlignRight)
        self._other_costs_edit.setPlaceholderText("0")
        form.addRow(self._other_costs_label, self._other_costs_edit)

        self._paid_label = QLabel(self)
        self._paid_edit = QLineEdit(self)
        self._paid_edit.setAlignment(Qt.AlignRight)
        self._paid_edit.setPlaceholderText("0")
        form.addRow(self._paid_label, self._paid_edit)

        self._save_btn = QPushButton(self)
        self._save_btn.clicked.connect(self._on_save_delivery)
        form.addRow(self._save_btn)
        layout.addWidget(self._record_group)

        # -- supplier balance ----------------------------------------------
        self._balance_group = QGroupBox(self)
        balance_layout = QHBoxLayout(self._balance_group)
        self._balance_label = QLabel(self)
        self._balance_value = QLabel(self)
        self._balance_value.setStyleSheet("font-weight: bold;")
        balance_layout.addWidget(self._balance_label)
        balance_layout.addWidget(self._balance_value)
        balance_layout.addStretch(1)
        layout.addWidget(self._balance_group)

        layout.addStretch(1)
        self.retranslate()
        self.refresh()

    # ------------------------------------------------------------------
    def _business_date(self) -> str:
        return self._date_edit.date().toString("yyyy-MM-dd")

    def retranslate(self) -> None:
        """Re-apply all static texts (called on language change)."""
        self._title.setText(tr("delivery.title"))
        self._date_label.setText(tr("delivery.date") + ":")
        self._refresh_btn.setText(tr("delivery.refresh"))
        self._deliveries_group.setTitle(tr("delivery.deliveries_section"))
        self._record_group.setTitle(tr("delivery.record_section"))
        self._balance_group.setTitle(tr("delivery.supplier_section"))
        self._table.setHorizontalHeaderLabels(
            [
                tr("delivery.th_supplier"),
                tr("delivery.th_invoice"),
                tr("delivery.th_product"),
                tr("delivery.th_tank"),
                tr("delivery.th_qty_l"),
                tr("delivery.th_unit_cost"),
                tr("delivery.th_total"),
                tr("delivery.th_paid"),
                tr("delivery.th_status"),
            ]
        )
        self._supplier_label.setText(tr("delivery.supplier") + ":")
        self._add_supplier_btn.setText(tr("delivery.add_supplier"))
        self._product_label.setText(tr("delivery.product") + ":")
        self._tank_label.setText(tr("delivery.tank") + ":")
        self._qty_label.setText(tr("delivery.quantity_l") + ":")
        self._unit_cost_label.setText(tr("delivery.unit_cost_pkr_l") + ":")
        self._invoice_label.setText(tr("delivery.invoice_no") + ":")
        self._other_costs_label.setText(tr("delivery.other_costs_pkr") + ":")
        self._paid_label.setText(tr("delivery.amount_paid_pkr") + ":")
        self._save_btn.setText(tr("delivery.save_delivery"))
        self._balance_label.setText(tr("delivery.supplier") + ":")
        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Reload suppliers, products, tanks, deliveries and the balance."""
        self._refresh_suppliers()
        self._refresh_products()
        self._refresh_table()
        self._update_balance()

    def _refresh_suppliers(self) -> None:
        try:
            suppliers = self._deliveries.list_suppliers()
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        current = self._supplier_combo.currentData()
        self._supplier_combo.blockSignals(True)
        self._supplier_combo.clear()
        for supplier in suppliers:
            self._supplier_combo.addItem(supplier.name, supplier.id)
        if current is not None:
            index = self._supplier_combo.findData(current)
            if index >= 0:
                self._supplier_combo.setCurrentIndex(index)
        self._supplier_combo.blockSignals(False)
        self._update_balance()

    def _refresh_products(self) -> None:
        try:
            products = self._prices.list_fuel_products()
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        current = self._product_combo.currentData()
        self._product_combo.blockSignals(True)
        self._product_combo.clear()
        for product in products:
            label, product_id = h.product_combo_entry(product)
            self._product_combo.addItem(label, product_id)
        if current is not None:
            index = self._product_combo.findData(current)
            if index >= 0:
                self._product_combo.setCurrentIndex(index)
        self._product_combo.blockSignals(False)
        self._refresh_tanks()

    def _on_product_changed(self) -> None:
        self._refresh_tanks()

    def _refresh_tanks(self) -> None:
        product_id = self._product_combo.currentData()
        try:
            tanks = self._inventory.list_tanks()
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._tanks = [
            t for t in tanks if product_id is None or t.fuel_product_id == product_id
        ]
        self._tank_combo.clear()
        for tank in self._tanks:
            self._tank_combo.addItem(
                f"{tank.code} ({tank.fuel_product_code})", tank.id
            )

    def _refresh_table(self) -> None:
        try:
            rows = self._deliveries.list_deliveries(self._business_date())
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._table.setRowCount(len(rows))
        for row, delivery in enumerate(rows):
            status = h.dto_field(
                delivery, "status", "payment_status", default=None
            )
            status_key = _STATUS_KEYS.get(status)
            status_text = tr(status_key) if status_key else (status or "—")
            values = [
                h.dto_field(delivery, "supplier_name", "supplier", default="—"),
                h.dto_field(
                    delivery, "supplier_invoice_no", "invoice_no", default=None
                )
                or "—",
                h.dto_field(
                    delivery, "fuel_product_code", "product_code", default="—"
                ),
                h.dto_field(delivery, "tank_code", default=None) or "—",
                h.format_litres(
                    h.dto_field(delivery, "quantity_ml", default=None)
                ),
                _format_unit_cost(
                    h.dto_field(
                        delivery, "unit_cost_10k", "unit_cost_pkr", default=None
                    )
                ),
                h.format_pkr(
                    h.dto_field(
                        delivery,
                        "total_paisa",
                        "total_cost_paisa",
                        default=None,
                    )
                ),
                h.format_pkr(
                    h.dto_field(
                        delivery, "paid_paisa", "amount_paid_paisa", default=None
                    )
                ),
                status_text,
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col >= 4:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._table.setItem(row, col, item)
        self._table.resizeColumnsToContents()

    def _update_balance(self) -> None:
        supplier_id = self._supplier_combo.currentData()
        if supplier_id is None:
            self._balance_value.setText("—")
            return
        try:
            balance = self._deliveries.supplier_balance(supplier_id)
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        # Contract: dict with outstanding_paisa; also accept a bare int or a
        # DTO carrying an outstanding field.
        outstanding = None
        if isinstance(balance, dict):
            outstanding = balance.get(
                "outstanding_paisa", balance.get("outstanding")
            )
        elif isinstance(balance, int) and not isinstance(balance, bool):
            outstanding = balance
        else:
            outstanding = h.dto_field(
                balance, "outstanding_paisa", "outstanding", default=None
            )
        self._balance_value.setText(
            tr("delivery.outstanding").format(amount=h.format_pkr(outstanding))
        )

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def _on_add_supplier(self) -> None:
        """Dialog: add a supplier, then select them in the form."""
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("delivery.add_supplier_title"))
        form = QFormLayout(dialog)
        name_edit = QLineEdit(dialog)
        phone_edit = QLineEdit(dialog)
        address_edit = QLineEdit(dialog)
        form.addRow(QLabel(tr("delivery.supplier_name"), dialog), name_edit)
        form.addRow(QLabel(tr("delivery.phone"), dialog), phone_edit)
        form.addRow(QLabel(tr("delivery.address"), dialog), address_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dialog
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)

        if dialog.exec() != QDialog.Accepted:
            return
        try:
            supplier_id = self._deliveries.add_supplier(
                name_edit.text().strip(),
                phone=phone_edit.text().strip() or None,
                address=address_edit.text().strip() or None,
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        h.show_info(self, tr("delivery.supplier_saved_ok"))
        self._refresh_suppliers()
        index = self._supplier_combo.findData(supplier_id)
        if index >= 0:
            self._supplier_combo.setCurrentIndex(index)

    def _on_save_delivery(self) -> None:
        """Validate the form, record the delivery, refresh, clear inputs."""
        supplier_id = self._supplier_combo.currentData()
        product_id = self._product_combo.currentData()
        tank_id = self._tank_combo.currentData()
        if supplier_id is None or product_id is None or tank_id is None:
            h.show_error(
                self,
                errors.BusinessRuleError(
                    "Select a supplier, product and tank first."
                ),
            )
            return
        try:
            quantity = h.parse_litres(self._qty_edit.text())
            unit_cost = h.parse_pkr(self._unit_cost_edit.text())
            other_costs = (
                h.parse_pkr(self._other_costs_edit.text())
                if self._other_costs_edit.text().strip()
                else Decimal("0")
            )
            amount_paid = (
                h.parse_pkr(self._paid_edit.text())
                if self._paid_edit.text().strip()
                else Decimal("0")
            )
            self._deliveries.record_delivery(
                supplier=supplier_id,
                fuel_product=product_id,
                tank=tank_id,
                business_date=self._business_date(),
                quantity_litres=quantity,
                unit_cost_pkr=unit_cost,
                supplier_invoice_no=self._invoice_edit.text().strip() or None,
                other_cost_pkr=other_costs,
                amount_paid_pkr=amount_paid,
                occurred_at_utc=timecalc.utc_now_iso(),
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._qty_edit.clear()
        self._unit_cost_edit.clear()
        self._invoice_edit.clear()
        self._other_costs_edit.clear()
        self._paid_edit.clear()
        h.show_info(self, tr("delivery.saved_ok"))
        self.refresh()
