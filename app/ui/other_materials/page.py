"""Other Materials page: engine oil / lubricants style products.

All figures come from OtherMaterialsService; the UI parses inputs and
formats service-computed values — it does no accounting math of its own.
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
    QVBoxLayout,
    QWidget,
)

from app.localization import tr
from app.services import errors
from app.ui import _helpers as h

_METHOD_KEYS = {
    "cash": "om.method_cash",
    "bank": "om.method_bank",
    "other": "om.method_other",
}

_TXN_TYPE_KEYS = {
    "purchase": "om.type_purchase",
    "sale": "om.type_sale",
    "adjustment": "om.type_adjustment",
}


def _method_text(method) -> str:
    key = _METHOD_KEYS.get(str(method or "").lower())
    return tr(key) if key else str(method or "—")


def _txn_type_text(txn_type) -> str:
    key = _TXN_TYPE_KEYS.get(str(txn_type or "").lower())
    return tr(key) if key else str(txn_type or "—")


class _AddProductDialog(QDialog):
    """Add an other product (name mandatory, unique)."""

    def __init__(self, parent, materials_service) -> None:
        super().__init__(parent)
        self._materials = materials_service
        self.setWindowTitle(tr("om.product_dialog_title"))
        self.setMinimumWidth(400)

        form = QFormLayout(self)
        self._name_edit = QLineEdit(self)
        form.addRow(QLabel(tr("om.product_name"), self), self._name_edit)
        self._unit_edit = QLineEdit(self)
        self._unit_edit.setPlaceholderText("piece")
        form.addRow(QLabel(tr("om.product_unit"), self), self._unit_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    # ------------------------------------------------------------------
    def _on_accept(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            h.show_error(self, ValueError(tr("om.product_name")))
            return
        unit = self._unit_edit.text().strip() or "piece"
        try:
            self._materials.add_product(name, unit=unit)
        except Exception as exc:  # noqa: BLE001 — keep dialog open
            h.show_error(self, exc)
            return
        h.show_info(self, tr("om.saved_ok"))
        self.accept()


class _TxnDialog(QDialog):
    """Record a purchase / sale / adjustment for one other product.

    The service call happens on OK; the dialog stays open (preserving
    input) when validation fails.
    """

    def __init__(self, parent, materials_service, business_date, txn_type: str) -> None:
        super().__init__(parent)
        self._materials = materials_service
        self._business_date = business_date
        self._txn_type = txn_type
        title_key = {
            "purchase": "om.purchase_title",
            "sale": "om.sale_title",
            "adjustment": "om.adjustment_title",
        }[txn_type]
        self.setWindowTitle(tr(title_key))
        self.setMinimumWidth(420)

        form = QFormLayout(self)
        self._product_combo = QComboBox(self)
        self._product_combo.setMinimumWidth(220)
        try:
            products = materials_service.list_products(active_only=True) or []
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            products = []
        for product in products:
            unit = h.dto_field(product, "unit", default="")
            name = h.dto_field(product, "name", default="—")
            self._product_combo.addItem(f"{name} ({unit})", product)
        form.addRow(QLabel(tr("om.product"), self), self._product_combo)

        self._qty_edit = QLineEdit(self)
        self._qty_edit.setAlignment(Qt.AlignRight)
        if txn_type == "adjustment":
            form.addRow(
                QLabel(tr("om.signed_quantity"), self), self._qty_edit
            )
        else:
            form.addRow(
                QLabel(tr("om.quantity_units"), self), self._qty_edit
            )

        if txn_type == "adjustment":
            self._price_edit = None
            self._method_combo = None
            self._reason_edit = QLineEdit(self)
            form.addRow(QLabel(tr("om.reason"), self), self._reason_edit)
            self._reference_edit = QLineEdit(self)
            form.addRow(
                QLabel(tr("om.reference_optional"), self), self._reference_edit
            )
        else:
            self._price_edit = QLineEdit(self)
            self._price_edit.setAlignment(Qt.AlignRight)
            form.addRow(
                QLabel(tr("om.unit_price_pkr"), self), self._price_edit
            )
            self._method_combo = QComboBox(self)
            for code in ("cash", "bank", "other"):
                self._method_combo.addItem(tr(_METHOD_KEYS[code]), code)
            form.addRow(QLabel(tr("om.method"), self), self._method_combo)
            method_hint = QLabel(tr("om.method_hint"), self)
            method_hint.setWordWrap(True)
            method_hint.setStyleSheet("color: gray;")
            form.addRow(method_hint)
            self._reason_edit = None
            self._reference_edit = QLineEdit(self)
            form.addRow(
                QLabel(tr("om.reference_optional"), self), self._reference_edit
            )

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    # ------------------------------------------------------------------
    def _parse_units(self, signed: bool) -> int:
        text = (self._qty_edit.text() or "").strip().replace(",", "")
        if not text:
            raise ValueError(tr("om.quantity_units"))
        try:
            value = int(text)
        except ValueError:
            raise ValueError(tr("om.quantity_units")) from None
        if not signed and value <= 0:
            raise ValueError(tr("om.quantity_units"))
        if signed and value == 0:
            raise ValueError(tr("om.signed_quantity"))
        return value

    def _on_accept(self) -> None:
        product = self._product_combo.currentData()
        if product is None:
            h.show_error(self, ValueError(tr("om.product")))
            return
        product_id = h.dto_field(product, "id", default=None)
        reference = (
            self._reference_edit.text().strip() or None
            if self._reference_edit is not None
            else None
        )
        try:
            if self._txn_type == "adjustment":
                quantity = self._parse_units(signed=True)
                reason = (self._reason_edit.text() or "").strip()
                if not reason:
                    h.show_error(self, ValueError(tr("om.reason")))
                    return
                self._materials.record_adjustment(
                    product=product_id,
                    business_date=self._business_date,
                    quantity_units=quantity,
                    reason=reason,
                    reference=reference,
                )
            else:
                quantity = self._parse_units(signed=False)
                unit_price = h.parse_pkr_to_paisa(
                    self._price_edit.text()
                )
                if unit_price <= 0:
                    h.show_error(self, ValueError(tr("om.unit_price_pkr")))
                    return
                record = (
                    self._materials.record_purchase
                    if self._txn_type == "purchase"
                    else self._materials.record_sale
                )
                record(
                    product=product_id,
                    business_date=self._business_date,
                    quantity_units=quantity,
                    unit_price_paisa=unit_price,
                    method=self._method_combo.currentData(),
                    reference=reference,
                )
        except ValueError as exc:  # input-level failures
            h.show_error(self, exc)
            return
        except Exception as exc:  # noqa: BLE001 — keep dialog open
            h.show_error(self, exc)
            return
        h.show_info(self, tr("om.recorded_ok"))
        self.accept()


class OtherMaterialsPage(QWidget):
    """Other-material products, stock on hand and transactions."""

    def __init__(self, materials_service, day_service, parent=None) -> None:
        super().__init__(parent)
        self._materials = materials_service
        self._days = day_service
        self._products_list: list = []
        self._txns_list: list = []

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

        # -- products -------------------------------------------------------
        self._products_group = QGroupBox(self)
        products_layout = QVBoxLayout(self._products_group)
        self._products_table = QTableWidget(self)
        self._products_table.setColumnCount(4)
        self._products_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._products_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._products_table.horizontalHeader().setStretchLastSection(True)
        products_layout.addWidget(self._products_table)
        product_btns = QHBoxLayout()
        self._add_product_btn = QPushButton(self)
        self._add_product_btn.clicked.connect(self._on_add_product)
        self._toggle_product_btn = QPushButton(self)
        self._toggle_product_btn.clicked.connect(self._on_toggle_product)
        product_btns.addWidget(self._add_product_btn)
        product_btns.addWidget(self._toggle_product_btn)
        product_btns.addStretch(1)
        products_layout.addLayout(product_btns)
        layout.addWidget(self._products_group)

        # -- transactions ---------------------------------------------------
        self._txns_group = QGroupBox(self)
        txns_layout = QVBoxLayout(self._txns_group)
        self._txns_table = QTableWidget(self)
        self._txns_table.setColumnCount(7)
        self._txns_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._txns_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._txns_table.horizontalHeader().setStretchLastSection(True)
        txns_layout.addWidget(self._txns_table)
        txn_btns = QHBoxLayout()
        self._purchase_btn = QPushButton(self)
        self._purchase_btn.clicked.connect(lambda: self._on_txn("purchase"))
        self._sale_btn = QPushButton(self)
        self._sale_btn.clicked.connect(lambda: self._on_txn("sale"))
        self._adjustment_btn = QPushButton(self)
        self._adjustment_btn.clicked.connect(lambda: self._on_txn("adjustment"))
        txn_btns.addWidget(self._purchase_btn)
        txn_btns.addWidget(self._sale_btn)
        txn_btns.addWidget(self._adjustment_btn)
        txn_btns.addStretch(1)
        txns_layout.addLayout(txn_btns)
        layout.addWidget(self._txns_group, 1)

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
        self._title.setText(tr("om.title"))
        self._date_label.setText(tr("om.date") + ":")
        self._refresh_btn.setText(tr("om.refresh"))
        self._products_group.setTitle(tr("om.products_section"))
        self._products_table.setHorizontalHeaderLabels(
            [
                tr("om.th_product"),
                tr("om.th_unit"),
                tr("om.th_on_hand"),
                tr("om.th_active"),
            ]
        )
        self._add_product_btn.setText(tr("om.add_product"))
        # The toggle label depends on selection; refresh() sets it.
        self._txns_group.setTitle(tr("om.txns_section"))
        self._txns_table.setHorizontalHeaderLabels(
            [
                tr("om.th_product"),
                tr("om.th_type"),
                tr("om.th_quantity"),
                tr("om.th_unit_price"),
                tr("om.th_total"),
                tr("om.th_method"),
                tr("om.th_reference"),
            ]
        )
        self._purchase_btn.setText(tr("om.purchase"))
        self._sale_btn.setText(tr("om.sale"))
        self._adjustment_btn.setText(tr("om.adjustment"))
        self.refresh()

    # ------------------------------------------------------------------
    def _set_writes_enabled(self, enabled: bool) -> None:
        for btn in (
            self._add_product_btn,
            self._toggle_product_btn,
            self._purchase_btn,
            self._sale_btn,
            self._adjustment_btn,
        ):
            btn.setEnabled(enabled)

    def refresh(self) -> None:
        """Reload products (with on-hand stock) and the day's transactions."""
        business_date = self._business_date()
        if not business_date:
            self._date_value.setText(tr("dash.no_open_day"))
            self._products_table.setRowCount(0)
            self._txns_table.setRowCount(0)
            self._set_writes_enabled(False)
            return
        self._set_writes_enabled(True)
        self._date_value.setText(business_date)
        # stock() returns master rows with the computed on-hand units —
        # the on-hand figure is service-computed, never derived here.
        try:
            self._products_list = self._materials.stock() or []
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._products_table.setRowCount(len(self._products_list))
        for row, product in enumerate(self._products_list):
            is_active = h.dto_field(product, "is_active", default=True)
            on_hand = h.dto_field(product, "on_hand_units", default=None)
            values = [
                str(h.dto_field(product, "name", default="—")),
                str(h.dto_field(product, "unit", default="—")),
                str(on_hand) if on_hand is not None else "—",
                tr("om.active_yes") if is_active else tr("om.active_no"),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col == 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._products_table.setItem(row, col, item)
        self._products_table.resizeColumnsToContents()
        self._update_toggle_label()
        try:
            self._txns_list = (
                self._materials.list_transactions(business_date=business_date)
                or []
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._txns_table.setRowCount(len(self._txns_list))
        for row, txn in enumerate(self._txns_list):
            values = [
                str(h.dto_field(txn, "product_name", default="—")),
                _txn_type_text(h.dto_field(txn, "txn_type", default=None)),
                str(h.dto_field(txn, "quantity_units", default="—")),
                h.format_pkr(h.dto_field(txn, "unit_price_paisa", default=None)),
                h.format_pkr(h.dto_field(txn, "total_paisa", default=None)),
                _method_text(h.dto_field(txn, "method", default=None)),
                str(h.dto_field(txn, "reference", default=None) or "—"),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col in (2, 3, 4):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._txns_table.setItem(row, col, item)
        self._txns_table.resizeColumnsToContents()

    def _update_toggle_label(self) -> None:
        row = self._products_table.currentRow()
        if 0 <= row < len(self._products_list):
            is_active = h.dto_field(
                self._products_list[row], "is_active", default=True
            )
            self._toggle_product_btn.setText(
                tr("om.deactivate") if is_active else tr("om.activate")
            )
        else:
            self._toggle_product_btn.setText(tr("om.deactivate"))

    # ------------------------------------------------------------------
    def _on_add_product(self) -> None:
        _AddProductDialog(self, self._materials).exec()
        self.refresh()

    def _on_toggle_product(self) -> None:
        row = self._products_table.currentRow()
        if not (0 <= row < len(self._products_list)):
            h.show_error(
                self, errors.BusinessRuleError(tr("om.select_product_first"))
            )
            return
        product = self._products_list[row]
        product_id = h.dto_field(product, "id", default=None)
        is_active = h.dto_field(product, "is_active", default=True)
        try:
            self._materials.set_product_active(product_id, not is_active)
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self.refresh()

    def _on_txn(self, txn_type: str) -> None:
        _TxnDialog(self, self._materials, self._business_date(), txn_type).exec()
        self.refresh()
