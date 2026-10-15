"""Suppliers page: balances, deliveries with outstanding, payments.

All figures come from SupplierService; the UI parses inputs and formats
service-computed values — it does no accounting math of its own.
Delivery-linked payments are capped at the invoice outstanding by the
service (and the DB trigger).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
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

from app.localization import tr
from app.services import errors
from app.ui import _helpers as h

_METHOD_KEYS = {
    "cash": "sup.method_cash",
    "bank": "sup.method_bank",
    "other": "sup.method_other",
}


def _method_text(method) -> str:
    key = _METHOD_KEYS.get(str(method or "").lower())
    return tr(key) if key else str(method or "—")


def _status_text(status) -> str:
    status = str(status or "").lower()
    if status == "posted":
        return tr("sup.status_posted")
    if status == "reversed":
        return tr("sup.status_reversed")
    return str(status or "—")


class _ReasonDialog(QDialog):
    """Single required-reason prompt."""

    def __init__(self, parent, title: str, label: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(360)
        layout = QVBoxLayout(self)
        message = QLabel(label, self)
        message.setWordWrap(True)
        layout.addWidget(message)
        self._reason_edit = QTextEdit(self)
        self._reason_edit.setMaximumHeight(90)
        layout.addWidget(self._reason_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def reason(self) -> str:
        return self._reason_edit.toPlainText().strip()

    def _on_accept(self) -> None:
        if not self.reason:
            h.show_error(self, ValueError(self.windowTitle()))
            return
        self.accept()


class _AddSupplierDialog(QDialog):
    """Add a supplier through DeliveryService (name mandatory)."""

    def __init__(self, parent, delivery_service) -> None:
        super().__init__(parent)
        self._deliveries = delivery_service
        self.setWindowTitle(tr("sup.supplier_dialog_title"))
        self.setMinimumWidth(400)

        form = QFormLayout(self)
        self._name_edit = QLineEdit(self)
        form.addRow(QLabel(tr("sup.supplier_name"), self), self._name_edit)
        self._phone_edit = QLineEdit(self)
        form.addRow(QLabel(tr("delivery.phone"), self), self._phone_edit)
        self._address_edit = QLineEdit(self)
        form.addRow(QLabel(tr("delivery.address"), self), self._address_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _on_accept(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            h.show_error(self, ValueError(tr("sup.supplier_name")))
            return
        try:
            self._deliveries.add_supplier(
                name,
                phone=self._phone_edit.text().strip() or None,
                address=self._address_edit.text().strip() or None,
            )
        except Exception as exc:  # noqa: BLE001 — keep dialog open
            h.show_error(self, exc)
            return
        h.show_info(self, tr("sup.supplier_saved_ok"))
        self.accept()


class _RecordPaymentDialog(QDialog):
    """Record a supplier payment (optionally against one invoice).

    The service call happens on OK; the dialog stays open (preserving
    input) when validation fails.
    """

    def __init__(self, parent, supplier_service, delivery_service, business_date) -> None:
        super().__init__(parent)
        self._suppliers = supplier_service
        self._deliveries = delivery_service
        self._business_date = business_date
        self.setWindowTitle(tr("sup.dialog_title"))
        self.setMinimumWidth(440)

        form = QFormLayout(self)
        supplier_row = QHBoxLayout()
        self._supplier_combo = QComboBox(self)
        self._supplier_combo.setMinimumWidth(220)
        supplier_row.addWidget(self._supplier_combo, 1)
        self._add_supplier_btn = QPushButton(tr("sup.add_supplier"), self)
        self._add_supplier_btn.setObjectName("add_supplier_btn")
        self._add_supplier_btn.clicked.connect(self._on_add_supplier)
        supplier_row.addWidget(self._add_supplier_btn)
        form.addRow(QLabel(tr("sup.supplier"), self), supplier_row)

        self._delivery_combo = QComboBox(self)
        self._delivery_combo.setMinimumWidth(240)
        self._delivery_combo.currentIndexChanged.connect(self._on_delivery_changed)
        form.addRow(QLabel(tr("sup.delivery"), self), self._delivery_combo)
        self._delivery_outstanding_label = QLabel(self)
        self._delivery_outstanding_label.setStyleSheet("color: gray;")
        form.addRow(self._delivery_outstanding_label)

        self._amount_edit = QLineEdit(self)
        self._amount_edit.setAlignment(Qt.AlignRight)
        form.addRow(QLabel(tr("sup.amount_pkr"), self), self._amount_edit)

        self._method_combo = QComboBox(self)
        for code in ("cash", "bank", "other"):
            self._method_combo.addItem(tr(_METHOD_KEYS[code]), code)
        form.addRow(QLabel(tr("sup.method"), self), self._method_combo)
        method_hint = QLabel(tr("sup.method_hint"), self)
        method_hint.setWordWrap(True)
        method_hint.setStyleSheet("color: gray;")
        form.addRow(method_hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self._supplier_combo.currentIndexChanged.connect(
            self._on_supplier_changed
        )
        self._load_suppliers()

    # ------------------------------------------------------------------
    def _load_suppliers(self) -> None:
        self._supplier_combo.blockSignals(True)
        self._supplier_combo.clear()
        try:
            suppliers = self._deliveries.list_suppliers(active_only=True) or []
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            suppliers = []
        for supplier in suppliers:
            self._supplier_combo.addItem(
                str(h.dto_field(supplier, "name", default="—")), supplier
            )
        self._supplier_combo.blockSignals(False)
        self._load_deliveries()

    def _on_add_supplier(self) -> None:
        _AddSupplierDialog(self, self._deliveries).exec()
        self._load_suppliers()

    def _current_supplier_id(self):
        supplier = self._supplier_combo.currentData()
        return h.dto_field(supplier, "id", "supplier_id", default=None)

    def _on_supplier_changed(self, _index: int) -> None:
        self._load_deliveries()

    def _load_deliveries(self) -> None:
        self._delivery_combo.blockSignals(True)
        self._delivery_combo.clear()
        self._delivery_combo.addItem(tr("sup.no_delivery"), None)
        supplier_id = self._current_supplier_id()
        if supplier_id is not None:
            try:
                deliveries = (
                    self._suppliers.deliveries_with_outstanding(supplier_id)
                    or []
                )
            except Exception as exc:  # noqa: BLE001
                h.show_error(self, exc)
                deliveries = []
            for delivery in deliveries:
                invoice = h.dto_field(
                    delivery, "supplier_invoice_no", default=None
                ) or f"#{h.dto_field(delivery, 'id', default='?')}"
                outstanding = h.dto_field(
                    delivery, "outstanding_paisa", default=0
                ) or 0
                product = h.dto_field(
                    delivery, "fuel_product_code", default="—"
                )
                self._delivery_combo.addItem(
                    f"{invoice} — {product} — {h.format_pkr(outstanding)}",
                    delivery,
                )
        self._delivery_combo.blockSignals(False)
        self._on_delivery_changed()

    def _on_delivery_changed(self, _index: int = 0) -> None:
        delivery = self._delivery_combo.currentData()
        if delivery is None:
            self._delivery_outstanding_label.setText("")
            return
        outstanding = h.dto_field(delivery, "outstanding_paisa", default=0) or 0
        self._delivery_outstanding_label.setText(
            tr("sup.delivery_outstanding").format(
                amount=h.format_pkr(outstanding)
            )
        )

    def _on_accept(self) -> None:
        supplier_id = self._current_supplier_id()
        if supplier_id is None:
            h.show_error(self, ValueError(tr("sup.supplier")))
            return
        try:
            amount = h.parse_pkr_to_paisa(self._amount_edit.text())
        except ValueError as exc:
            h.show_error(self, exc)
            return
        if amount <= 0:
            h.show_error(self, ValueError(tr("sup.amount_pkr")))
            return
        delivery = self._delivery_combo.currentData()
        delivery_id = (
            h.dto_field(delivery, "id", default=None)
            if delivery is not None
            else None
        )
        try:
            self._suppliers.record_supplier_payment(
                supplier=supplier_id,
                business_date=self._business_date,
                amount_paisa=amount,
                method=self._method_combo.currentData(),
                delivery_id=delivery_id,
            )
        except Exception as exc:  # noqa: BLE001 — keep dialog open
            h.show_error(self, exc)
            return
        h.show_info(self, tr("sup.recorded_ok"))
        self.accept()


class SupplierPage(QWidget):
    """Supplier balances, outstanding deliveries and supplier payments."""

    def __init__(
        self, supplier_service, delivery_service, day_service, parent=None
    ) -> None:
        super().__init__(parent)
        self._suppliers = supplier_service
        self._deliveries = delivery_service
        self._days = day_service
        self._balances_list: list = []
        self._deliveries_list: list = []
        self._payments_list: list = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self._title = QLabel(self)
        self._title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(self._title)

        # -- date row ------------------------------------------------------
        date_row = QHBoxLayout()
        self._date_label = QLabel(self)
        self._date_value = QLabel(self)
        self._date_value.setStyleSheet("font-weight: bold;")
        self._refresh_btn = QPushButton(self)
        self._refresh_btn.clicked.connect(self.refresh)
        date_row.addWidget(self._date_label)
        date_row.addWidget(self._date_value)
        date_row.addWidget(self._refresh_btn)
        date_row.addStretch(1)
        layout.addLayout(date_row)

        # -- balances ------------------------------------------------------
        self._balances_group = QGroupBox(self)
        balances_layout = QVBoxLayout(self._balances_group)
        self._balances_table = QTableWidget(self)
        self._balances_table.setColumnCount(5)
        self._balances_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._balances_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._balances_table.horizontalHeader().setStretchLastSection(True)
        balances_layout.addWidget(self._balances_table)
        layout.addWidget(self._balances_group)

        # -- deliveries with outstanding ------------------------------------
        self._deliveries_group = QGroupBox(self)
        deliveries_layout = QVBoxLayout(self._deliveries_group)
        self._deliveries_table = QTableWidget(self)
        self._deliveries_table.setColumnCount(5)
        self._deliveries_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._deliveries_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._deliveries_table.horizontalHeader().setStretchLastSection(True)
        deliveries_layout.addWidget(self._deliveries_table)
        layout.addWidget(self._deliveries_group)

        # -- payments --------------------------------------------------------
        self._payments_group = QGroupBox(self)
        payments_layout = QVBoxLayout(self._payments_group)
        self._payments_table = QTableWidget(self)
        self._payments_table.setColumnCount(6)
        self._payments_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._payments_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._payments_table.horizontalHeader().setStretchLastSection(True)
        payments_layout.addWidget(self._payments_table)
        payment_btns = QHBoxLayout()
        self._record_btn = QPushButton(self)
        self._record_btn.clicked.connect(self._on_record_payment)
        self._reverse_btn = QPushButton(self)
        self._reverse_btn.clicked.connect(self._on_reverse_payment)
        payment_btns.addWidget(self._record_btn)
        payment_btns.addWidget(self._reverse_btn)
        payment_btns.addStretch(1)
        payments_layout.addLayout(payment_btns)
        layout.addWidget(self._payments_group, 1)

        layout.addStretch(1)
        self.retranslate()
        self.refresh()

    # ------------------------------------------------------------------
    def _business_date(self) -> str | None:
        try:
            day = self._days.get_open_day()
        except Exception:  # noqa: BLE001
            day = None
        if day is None:
            return None
        return str(h.dto_field(day, "business_date", default=""))

    def retranslate(self) -> None:
        """Re-apply all static texts (called on language change)."""
        self._title.setText(tr("sup.title"))
        self._date_label.setText(tr("sup.date") + ":")
        self._refresh_btn.setText(tr("sup.refresh"))
        self._balances_group.setTitle(tr("sup.balances_section"))
        self._balances_table.setHorizontalHeaderLabels(
            [
                tr("sup.th_supplier"),
                tr("sup.th_invoiced"),
                tr("sup.th_paid_delivery"),
                tr("sup.th_paid"),
                tr("sup.th_outstanding"),
            ]
        )
        self._deliveries_group.setTitle(tr("sup.deliveries_section"))
        self._deliveries_table.setHorizontalHeaderLabels(
            [
                tr("sup.th_supplier"),
                tr("sup.th_invoice"),
                tr("sup.th_product"),
                tr("sup.th_qty_l"),
                tr("sup.th_outstanding"),
            ]
        )
        self._payments_group.setTitle(tr("sup.payments_section"))
        self._payments_table.setHorizontalHeaderLabels(
            [
                tr("sup.th_supplier"),
                tr("sup.th_delivery"),
                tr("sup.th_amount"),
                tr("sup.th_method"),
                tr("sup.th_date"),
                tr("sup.th_status"),
            ]
        )
        self._record_btn.setText(tr("sup.record"))
        self._reverse_btn.setText(tr("sup.reverse"))
        self.refresh()

    # ------------------------------------------------------------------
    def _set_writes_enabled(self, enabled: bool) -> None:
        for btn in (self._record_btn, self._reverse_btn):
            btn.setEnabled(enabled)

    def refresh(self) -> None:
        """Reload balances, outstanding deliveries and the day's payments."""
        business_date = self._business_date()
        if not business_date:
            self._date_value.setText(tr("dash.no_open_day"))
            self._balances_table.setRowCount(0)
            self._deliveries_table.setRowCount(0)
            self._payments_table.setRowCount(0)
            self._set_writes_enabled(False)
            return
        self._set_writes_enabled(True)
        self._date_value.setText(business_date)
        try:
            self._balances_list = self._suppliers.supplier_balances() or []
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._balances_table.setRowCount(len(self._balances_list))
        for row, balance in enumerate(self._balances_list):
            values = [
                str(h.dto_field(balance, "supplier_name", default="—")),
                h.format_pkr(h.dto_field(balance, "invoiced_paisa", default=None)),
                h.format_pkr(
                    h.dto_field(balance, "paid_at_delivery_paisa", default=None)
                ),
                h.format_pkr(h.dto_field(balance, "paid_paisa", default=None)),
                h.format_pkr(
                    h.dto_field(balance, "outstanding_paisa", default=None)
                ),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col > 0:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._balances_table.setItem(row, col, item)
        self._balances_table.resizeColumnsToContents()
        try:
            self._deliveries_list = (
                self._suppliers.deliveries_with_outstanding() or []
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._deliveries_table.setRowCount(len(self._deliveries_list))
        for row, delivery in enumerate(self._deliveries_list):
            invoice = h.dto_field(
                delivery, "supplier_invoice_no", default=None
            ) or f"#{h.dto_field(delivery, 'id', default='?')}"
            values = [
                str(h.dto_field(delivery, "supplier_name", default="—")),
                str(invoice),
                str(h.dto_field(delivery, "fuel_product_code", default="—")),
                h.format_litres(h.dto_field(delivery, "quantity_ml", default=None)),
                h.format_pkr(
                    h.dto_field(delivery, "outstanding_paisa", default=None)
                ),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col in (3, 4):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._deliveries_table.setItem(row, col, item)
        self._deliveries_table.resizeColumnsToContents()
        try:
            self._payments_list = (
                self._suppliers.list_payments(business_date=business_date) or []
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._payments_table.setRowCount(len(self._payments_list))
        for row, payment in enumerate(self._payments_list):
            delivery_id = h.dto_field(payment, "delivery_id", default=None)
            values = [
                str(h.dto_field(payment, "supplier_name", default="—")),
                f"#{delivery_id}" if delivery_id is not None else "—",
                h.format_pkr(
                    h.dto_field(
                        payment, "effective_paisa", "amount_paisa", default=None
                    )
                ),
                _method_text(h.dto_field(payment, "method", default=None)),
                str(h.dto_field(payment, "business_date", default="—")),
                _status_text(h.dto_field(payment, "status", default=None)),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col == 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._payments_table.setItem(row, col, item)
        self._payments_table.resizeColumnsToContents()

    # ------------------------------------------------------------------
    def _on_record_payment(self) -> None:
        _RecordPaymentDialog(
            self, self._suppliers, self._deliveries, self._business_date()
        ).exec()
        self.refresh()

    def _on_reverse_payment(self) -> None:
        row = self._payments_table.currentRow()
        if not (0 <= row < len(self._payments_list)):
            h.show_error(
                self, errors.BusinessRuleError(tr("sup.select_payment_first"))
            )
            return
        payment = self._payments_list[row]
        if str(h.dto_field(payment, "status", default="")) != "posted":
            h.show_error(
                self, errors.BusinessRuleError(tr("sup.status_reversed"))
            )
            return
        amount = h.dto_field(payment, "effective_paisa", "amount_paisa",
                             default=None)
        supplier = h.dto_field(payment, "supplier_name", default="—")
        confirm = _ReasonDialog(
            self,
            tr("sup.reverse_title"),
            tr("sup.confirm_reverse").format(
                amount=h.format_pkr(amount), supplier=supplier
            )
            + "\n"
            + tr("sup.reverse_reason"),
        )
        if confirm.exec() != QDialog.Accepted:
            return
        try:
            self._suppliers.reverse_supplier_payment(
                h.dto_field(payment, "uuid", default=None),
                reason=confirm.reason,
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        h.show_info(self, tr("sup.reversed_ok"))
        self.refresh()
