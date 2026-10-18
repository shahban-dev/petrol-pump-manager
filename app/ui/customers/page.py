"""Customer credit page: customers, credit tickets, ledger/statement.

All figures come from CustomerCreditService; the UI parses inputs and
formats service-computed values — it does no accounting math of its own.
Nozzle/product choices come from the optional NozzleService (passed by
the main window); without it the ticket dialog still works with typed
codes validated by the service.
"""

from __future__ import annotations

from decimal import Decimal

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox,
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
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.calculations import time as timecalc
from app.localization import tr
from app.services import errors
from app.ui import _helpers as h

#: TicketInfo.status -> tr() keys.
_TICKET_STATUS_KEYS = {
    "posted": "ticket.status_posted",
    "reversed": "ticket.status_reversed",
}

#: LedgerEntry.entry_type -> tr() keys (unknown types show as-is).
_LEDGER_TYPE_KEYS = {
    "opening": "ledger.type_opening",
    "ticket": "ledger.type_ticket",
    "ticket_reversal": "ledger.type_reversal",
    "paid_now": "ledger.type_paid_now",
    "payment": "ledger.type_payment",
    "adjustment": "ledger.type_adjustment",
    "reversal": "ledger.type_reversal",
}


def _ledger_type_text(entry_type) -> str:
    key = _LEDGER_TYPE_KEYS.get(str(entry_type or "").lower())
    return tr(key) if key else str(entry_type or "—")


class _CustomerDialog(QDialog):
    """Add / edit customer. Service call happens on OK; the dialog stays
    open (preserving input) when validation or the service fails."""

    def __init__(self, parent, credit_service, existing=None) -> None:
        super().__init__(parent)
        self._credit = credit_service
        self._existing = existing
        self.setWindowTitle(
            tr("cust.dialog_edit_title") if existing else tr("cust.dialog_add_title")
        )
        self.setMinimumWidth(380)

        form = QFormLayout(self)
        self._name_edit = QLineEdit(self)
        self._business_edit = QLineEdit(self)
        self._phone_edit = QLineEdit(self)
        self._address_edit = QLineEdit(self)
        self._opening_edit = h.make_integer_edit(self)
        self._opening_edit.setAlignment(Qt.AlignRight)
        self._limit_edit = h.make_integer_edit(self)
        self._limit_edit.setAlignment(Qt.AlignRight)
        self._no_limit_check = QCheckBox(tr("cust.clear_limit"), self)
        self._status_combo = QComboBox(self)
        self._notes_edit = QLineEdit(self)

        form.addRow(QLabel(tr("cust.name"), self), self._name_edit)
        form.addRow(QLabel(tr("cust.business_name"), self), self._business_edit)
        form.addRow(QLabel(tr("cust.phone"), self), self._phone_edit)
        form.addRow(QLabel(tr("cust.address"), self), self._address_edit)
        if existing is None:
            form.addRow(
                QLabel(tr("cust.opening_receivable"), self), self._opening_edit
            )
        else:
            opening = h.dto_field(
                existing, "opening_receivable_paisa", default=None
            )
            self._opening_label = QLabel(h.format_pkr(opening), self)
            form.addRow(
                QLabel(tr("cust.opening_receivable"), self), self._opening_label
            )
            hint = QLabel(tr("cust.opening_readonly_hint"), self)
            hint.setWordWrap(True)
            hint.setStyleSheet("color: gray;")
            form.addRow(hint)
        form.addRow(QLabel(tr("cust.credit_limit"), self), self._limit_edit)
        form.addRow(self._no_limit_check)
        if existing is not None:
            self._status_combo.addItem(tr("cust.status_active"), "active")
            self._status_combo.addItem(tr("cust.status_inactive"), "inactive")
            form.addRow(QLabel(tr("cust.status"), self), self._status_combo)
        form.addRow(QLabel(tr("cust.notes"), self), self._notes_edit)

        if existing is not None:
            self._name_edit.setText(h.dto_field(existing, "name", default="") or "")
            self._business_edit.setText(
                h.dto_field(existing, "business_name", default="") or ""
            )
            self._phone_edit.setText(h.dto_field(existing, "phone", default="") or "")
            self._address_edit.setText(
                h.dto_field(existing, "address", default="") or ""
            )
            limit = h.dto_field(existing, "credit_limit_paisa", default=None)
            if limit is None:
                self._no_limit_check.setChecked(True)
            else:
                self._limit_edit.setText(f"{Decimal(limit) / 100:.2f}")
            status = h.dto_field(existing, "status", default="active")
            index = self._status_combo.findData(status)
            if index >= 0:
                self._status_combo.setCurrentIndex(index)
            self._notes_edit.setText(h.dto_field(existing, "notes", default="") or "")

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    # ------------------------------------------------------------------
    def _on_accept(self) -> None:
        name = self._name_edit.text().strip()
        try:
            if self._existing is None:
                opening_text = self._opening_edit.text().strip()
                opening = (
                    h.parse_pkr_to_paisa(opening_text) if opening_text else 0
                )
                limit_text = self._limit_edit.text().strip()
                limit = (
                    h.parse_pkr_to_paisa(limit_text) if limit_text else None
                )
                # The UI parses to integer paisa (never float); the service
                # takes paisa ints for all money inputs.
                self._credit.create_customer(
                    name,
                    business_name=self._business_edit.text().strip() or None,
                    phone=self._phone_edit.text().strip() or None,
                    address=self._address_edit.text().strip() or None,
                    opening_receivable_paisa=opening,
                    credit_limit_paisa=limit,
                    notes=self._notes_edit.text().strip() or None,
                )
            else:
                customer_id = h.dto_field(
                    self._existing, "id", "customer_id", default=None
                )
                kwargs: dict = {}
                limit_text = self._limit_edit.text().strip()
                if self._no_limit_check.isChecked():
                    kwargs["clear_credit_limit"] = True
                elif limit_text:
                    kwargs["credit_limit_paisa"] = h.parse_pkr_to_paisa(limit_text)
                self._credit.update_customer(
                    customer_id,
                    name=name,
                    business_name=self._business_edit.text().strip() or None,
                    phone=self._phone_edit.text().strip() or None,
                    address=self._address_edit.text().strip() or None,
                    status=self._status_combo.currentData(),
                    notes=self._notes_edit.text().strip() or None,
                    **kwargs,
                )
        except Exception as exc:  # noqa: BLE001 — keep dialog open, keep input
            h.show_error(self, exc)
            return
        h.show_info(self, tr("cust.saved_ok"))
        self.accept()


class _AdjustOpeningDialog(QDialog):
    """Signed opening-balance adjustment (audited, never an edit)."""

    def __init__(self, parent, credit_service, customer, business_date) -> None:
        super().__init__(parent)
        self._credit = credit_service
        self._customer = customer
        self._business_date = business_date
        self.setWindowTitle(tr("cust.adjust_title"))
        self.setMinimumWidth(380)

        form = QFormLayout(self)
        opening = h.dto_field(customer, "opening_receivable_paisa", default=None)
        form.addRow(
            QLabel(tr("cust.current_opening"), self),
            QLabel(h.format_pkr(opening), self),
        )
        self._amount_edit = h.make_signed_integer_edit(self)
        self._amount_edit.setAlignment(Qt.AlignRight)
        form.addRow(QLabel(tr("cust.adjust_amount"), self), self._amount_edit)
        self._reason_edit = QTextEdit(self)
        self._reason_edit.setMaximumHeight(70)
        form.addRow(QLabel(tr("cust.adjust_reason"), self), self._reason_edit)
        self._authorized_edit = QLineEdit(self)
        form.addRow(QLabel(tr("cust.authorized_by"), self), self._authorized_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _on_accept(self) -> None:
        customer_id = h.dto_field(self._customer, "id", "customer_id", default=None)
        try:
            adjustment = h.parse_signed_pkr_to_paisa(self._amount_edit.text())
            if adjustment == 0:
                raise ValueError(tr("cust.adjust_amount"))
            reason = self._reason_edit.toPlainText().strip()
            if not reason:
                raise errors.ValidationError(
                    tr("cust.reason_required"), key="cust.reason_required"
                )
            self._credit.adjust_opening_balance(
                customer_id,
                adjustment,
                reason,
                self._business_date,
                authorized_by=self._authorized_edit.text().strip() or None,
            )
        except Exception as exc:  # noqa: BLE001 — keep dialog open, keep input
            h.show_error(self, exc)
            return
        h.show_info(self, tr("cust.adjusted_ok"))
        self.accept()


class _IssueTicketDialog(QDialog):
    """Multi-line credit ticket. Lines are collected first; the ticket is
    issued once on OK. Product/nozzle choices come from NozzleService when
    available, otherwise typed codes validated by the service."""

    def __init__(
        self, parent, credit_service, business_date, nozzle_service=None
    ) -> None:
        super().__init__(parent)
        self._credit = credit_service
        self._business_date = business_date
        self._nozzle_service = nozzle_service
        self._lines: list[dict] = []
        self.setWindowTitle(tr("ticket.dialog_title"))
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._customer_combo = QComboBox(self)
        form.addRow(QLabel(tr("ticket.customer"), self), self._customer_combo)

        line_row = QHBoxLayout()
        self._product_combo = QComboBox(self)
        self._product_combo.currentIndexChanged.connect(self._refresh_nozzle_combo)
        self._nozzle_combo = QComboBox(self)
        self._nozzle_combo.setEditable(True)
        self._nozzle_combo.setMinimumWidth(110)
        self._qty_edit = h.make_integer_edit(self)
        self._qty_edit.setAlignment(Qt.AlignRight)
        self._qty_edit.setPlaceholderText(tr("ticket.quantity_l"))
        self._qty_edit.setMinimumWidth(110)
        self._add_line_btn = QPushButton(tr("ticket.add_line"), self)
        self._add_line_btn.setObjectName("add_line_btn")
        self._add_line_btn.clicked.connect(self._on_add_line)
        line_row.addWidget(QLabel(tr("ticket.product"), self))
        line_row.addWidget(self._product_combo, 1)
        line_row.addWidget(QLabel(tr("ticket.nozzle"), self))
        line_row.addWidget(self._nozzle_combo, 1)
        line_row.addWidget(self._qty_edit)
        line_row.addWidget(self._add_line_btn)
        layout.addLayout(line_row)
        nozzle_hint = QLabel(tr("ticket.nozzle_hint"), self)
        nozzle_hint.setStyleSheet("color: gray;")
        layout.addWidget(nozzle_hint)

        self._lines_group = QGroupBox(tr("ticket.lines_section"), self)
        lines_layout = QVBoxLayout(self._lines_group)
        self._lines_table = QTableWidget(self)
        self._lines_table.setColumnCount(3)
        self._lines_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._lines_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._lines_table.horizontalHeader().setStretchLastSection(True)
        self._lines_table.setMinimumHeight(110)
        lines_layout.addWidget(self._lines_table)
        remove_row = QHBoxLayout()
        self._remove_line_btn = QPushButton(tr("ticket.remove_line"), self)
        self._remove_line_btn.clicked.connect(self._on_remove_line)
        remove_row.addWidget(self._remove_line_btn)
        remove_row.addStretch(1)
        lines_layout.addLayout(remove_row)
        layout.addWidget(self._lines_group)

        form2 = QFormLayout()
        self._paid_edit = h.make_integer_edit(self)
        self._paid_edit.setAlignment(Qt.AlignRight)
        self._paid_edit.setPlaceholderText("0")
        form2.addRow(QLabel(tr("ticket.paid_now"), self), self._paid_edit)
        self._notes_edit = QLineEdit(self)
        form2.addRow(QLabel(tr("ticket.notes"), self), self._notes_edit)
        layout.addLayout(form2)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._load_customers()
        self._load_products()

    # ------------------------------------------------------------------
    def _load_customers(self) -> None:
        try:
            customers = self._credit.list_customers(active_only=True)
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._customer_combo.clear()
        for customer in customers:
            code = h.dto_field(customer, "customer_code", "code", default="?")
            name = h.dto_field(customer, "name", default="")
            self._customer_combo.addItem(f"{code} — {name}", customer)

    def _load_products(self) -> None:
        codes: list[str] = []
        self._nozzles: list = []
        if self._nozzle_service is not None:
            try:
                self._nozzles = self._nozzle_service.list_nozzles() or []
            except Exception as exc:  # noqa: BLE001
                h.show_error(self, exc)
                self._nozzles = []
            for nozzle in self._nozzles:
                code = h.dto_field(nozzle, "fuel_product_code", default=None)
                if code and code not in codes:
                    codes.append(code)
        # Union with products already used on tickets today, so the combo
        # stays useful even when a product has no nozzle listed.
        try:
            for ticket in self._credit.day_tickets(self._business_date) or []:
                for line in h.dto_field(ticket, "lines", default=None) or []:
                    code = h.dto_field(
                        line, "fuel_product_code", "product_code", default=None
                    )
                    if code and code not in codes:
                        codes.append(code)
        except Exception:  # noqa: BLE001 — nice-to-have only
            pass
        self._product_combo.blockSignals(True)
        self._product_combo.clear()
        for code in codes:
            self._product_combo.addItem(code, code)
        self._product_combo.setEditable(not codes)
        self._product_combo.blockSignals(False)
        self._refresh_nozzle_combo()

    def _refresh_nozzle_combo(self) -> None:
        if self._product_combo.isEditable():
            product = self._product_combo.currentText().strip() or None
        else:
            product = self._product_combo.currentData()
        current = self._nozzle_combo.currentText()
        self._nozzle_combo.blockSignals(True)
        self._nozzle_combo.clear()
        self._nozzle_combo.addItem("", None)
        for nozzle in getattr(self, "_nozzles", []):
            if product and h.dto_field(
                nozzle, "fuel_product_code", default=None
            ) != product:
                continue
            code = h.dto_field(nozzle, "code", default=None)
            if code:
                self._nozzle_combo.addItem(code, code)
        index = self._nozzle_combo.findText(current)
        if index >= 0:
            self._nozzle_combo.setCurrentIndex(index)
        self._nozzle_combo.blockSignals(False)

    # ------------------------------------------------------------------
    def _combo_value(self, combo) -> str | None:
        data = combo.currentData()
        if data:
            return str(data)
        text = combo.currentText().strip()
        return text or None

    def _on_add_line(self) -> None:
        product = self._combo_value(self._product_combo)
        if not product:
            h.show_error(self, ValueError(tr("ticket.product")))
            return
        nozzle = self._combo_value(self._nozzle_combo)
        try:
            quantity = h.parse_litres(self._qty_edit.text())
        except ValueError as exc:
            h.show_error(self, exc)
            return
        if quantity <= 0:
            h.show_error(self, ValueError(tr("ticket.quantity_l")))
            return
        self._lines.append(
            {"product": product, "nozzle": nozzle, "quantity": quantity}
        )
        self._refresh_lines_table()

    def _on_remove_line(self) -> None:
        row = self._lines_table.currentRow()
        if 0 <= row < len(self._lines):
            del self._lines[row]
            self._refresh_lines_table()

    def _refresh_lines_table(self) -> None:
        self._lines_table.setRowCount(len(self._lines))
        for row, line in enumerate(self._lines):
            values = [
                line["product"],
                line["nozzle"] or "—",
                f"{line['quantity']:,.3f} {tr('common.litres')}",
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col == 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._lines_table.setItem(row, col, item)
        self._lines_table.resizeColumnsToContents()

    # ------------------------------------------------------------------
    def _on_accept(self) -> None:
        if not self._lines:
            h.show_error(self, ValueError(tr("ticket.no_lines")))
            return
        customer = self._customer_combo.currentData()
        if customer is None:
            h.show_error(self, ValueError(tr("cust.select_customer_first")))
            return
        customer_id = h.dto_field(customer, "id", "customer_id", default=None)
        paid_text = self._paid_edit.text().strip()
        try:
            paid_now = h.parse_pkr_to_paisa(paid_text) if paid_text else 0
        except ValueError as exc:
            h.show_error(self, exc)
            return
        lines = [
            {
                "fuel_product_code": line["product"],
                "nozzle_code": line["nozzle"],
                "quantity_litres": line["quantity"],
            }
            for line in self._lines
        ]
        try:
            ticket = self._credit.issue_ticket(
                customer_id,
                self._business_date,
                lines,
                paid_now_paisa=paid_now,
                notes=self._notes_edit.text().strip() or None,
            )
        except Exception as exc:  # noqa: BLE001 — keep dialog open, keep input
            h.show_error(self, exc)
            return
        receipt = h.dto_field(ticket, "receipt_no", default="—")
        message = tr("ticket.issued_ok").format(receipt=receipt)
        warnings = h.dto_field(ticket, "warnings", default=None) or []
        if warnings:
            message += (
                "\n" + tr("ticket.warnings_label") + ":\n- "
                + "\n- ".join(str(w) for w in warnings)
            )
        h.show_info(self, message)
        self.accept()


class _ReasonDialog(QDialog):
    """Single required-reason prompt (reversals, re-allocations)."""

    def __init__(self, parent, title: str, label: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(360)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(label, self))
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


class _TicketDetailDialog(QDialog):
    """Read-only ticket view: lines, totals, warnings, reverse action."""

    def __init__(
        self, parent, credit_service, ticket, business_date, customer_text="—"
    ) -> None:
        super().__init__(parent)
        self._credit = credit_service
        self._business_date = business_date
        self._uuid = h.dto_field(ticket, "uuid", default=None)
        self.setWindowTitle(tr("ticket.detail_title"))
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        header = QFormLayout()
        header.addRow(
            QLabel(tr("ticket.th_receipt"), self),
            QLabel(str(h.dto_field(ticket, "receipt_no", default="—")), self),
        )
        header.addRow(
            QLabel(tr("ticket.customer"), self),
            QLabel(customer_text, self),
        )
        header.addRow(
            QLabel(tr("cust.date"), self),
            QLabel(str(h.dto_field(ticket, "business_date", default="—")), self),
        )
        status = h.dto_field(ticket, "status", default=None)
        status_key = _TICKET_STATUS_KEYS.get(status)
        header.addRow(
            QLabel(tr("ticket.th_status"), self),
            QLabel(tr(status_key) if status_key else str(status or "—"), self),
        )
        layout.addLayout(header)

        self._lines_table = QTableWidget(self)
        self._lines_table.setColumnCount(5)
        self._lines_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._lines_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self._lines_table)

        self._summary_label = QLabel(self)
        self._summary_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self._summary_label)
        self._warnings_label = QLabel(self)
        self._warnings_label.setWordWrap(True)
        self._warnings_label.setStyleSheet("color: darkred;")
        layout.addWidget(self._warnings_label)

        btn_row = QHBoxLayout()
        self._reverse_btn = QPushButton(tr("ticket.reverse"), self)
        self._reverse_btn.clicked.connect(self._on_reverse)
        self._close_btn = QPushButton(tr("common.ok"), self)
        self._close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._reverse_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self._close_btn)
        layout.addLayout(btn_row)

        self._fill(ticket)

    # ------------------------------------------------------------------
    def _fill(self, ticket) -> None:
        lines = h.dto_field(ticket, "lines", default=None) or []
        self._lines_table.setRowCount(len(lines))
        for row, line in enumerate(lines):
            quantity_ml = h.dto_field(line, "quantity_ml", default=None)
            values = [
                str(
                    h.dto_field(
                        line, "fuel_product_code", "product_code", default="—"
                    )
                ),
                str(h.dto_field(line, "nozzle_code", default=None) or "—"),
                h.format_litres(quantity_ml),
                h.format_unit_price(
                    h.dto_field(line, "unit_price_10k", default=None)
                ),
                h.format_pkr(h.dto_field(line, "line_amount_paisa", default=None)),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col >= 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._lines_table.setItem(row, col, item)
        self._lines_table.setHorizontalHeaderLabels(
            [
                tr("ticket.th_product"),
                tr("ticket.th_nozzle"),
                tr("ticket.th_qty_l"),
                tr("ticket.th_unit_price"),
                tr("ticket.th_line_amount"),
            ]
        )
        self._lines_table.resizeColumnsToContents()
        self._summary_label.setText(
            tr("ticket.summary").format(
                gross=h.format_pkr(h.dto_field(ticket, "gross_amount_paisa", default=None)),
                paid=h.format_pkr(h.dto_field(ticket, "paid_now_paisa", default=None)),
                unpaid=h.format_pkr(h.dto_field(ticket, "unpaid_paisa", default=None)),
            )
        )
        warnings = h.dto_field(ticket, "warnings", default=None) or []
        self._warnings_label.setText(
            (tr("ticket.warnings_label") + ":\n" + "\n".join(str(w) for w in warnings))
            if warnings
            else ""
        )
        status = h.dto_field(ticket, "status", default=None)
        self._reverse_btn.setEnabled(status == "posted")

    def _on_reverse(self) -> None:
        reason_dlg = _ReasonDialog(
            self, tr("ticket.reverse_title"), tr("ticket.reverse_reason")
        )
        if reason_dlg.exec() != QDialog.Accepted:
            return
        try:
            self._credit.reverse_ticket(
                self._uuid, reason_dlg.reason, self._business_date
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        h.show_info(self, tr("ticket.reversed_ok"))
        self.accept()


class CustomerCreditPage(QWidget):
    """Customers, their credit tickets for a business day, and statements."""

    def __init__(
        self, credit_service, day_service, parent=None, *, nozzle_service=None
    ) -> None:
        super().__init__(parent)
        self._credit = credit_service
        self._days = day_service
        self._nozzles = nozzle_service
        self._customers: list = []
        self._tickets: list = []
        self._customer_names: dict = {}
        self._outstanding_cache: dict = {}

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
        self._date_edit.setDate(self._default_date())
        self._date_edit.setCalendarPopup(True)
        self._date_edit.dateChanged.connect(self.refresh)
        self._refresh_btn = QPushButton(self)
        self._refresh_btn.clicked.connect(self.refresh)
        date_row.addWidget(self._date_label)
        date_row.addWidget(self._date_edit)
        date_row.addWidget(self._refresh_btn)
        date_row.addStretch(1)
        layout.addLayout(date_row)

        # -- tabs ----------------------------------------------------------
        self._tabs = QTabWidget(self)
        layout.addWidget(self._tabs, 1)

        self._customers_tab = QWidget(self)
        self._build_customers_tab(self._customers_tab)
        self._tabs.addTab(self._customers_tab, "")

        self._tickets_tab = QWidget(self)
        self._build_tickets_tab(self._tickets_tab)
        self._tabs.addTab(self._tickets_tab, "")

        self._ledger_tab = QWidget(self)
        self._build_ledger_tab(self._ledger_tab)
        self._tabs.addTab(self._ledger_tab, "")

        self.retranslate()
        self.refresh()

    # ------------------------------------------------------------------
    def _default_date(self) -> QDate:
        try:
            day = self._days.get_open_day()
        except Exception:  # noqa: BLE001 — fall back to today
            day = None
        if day is not None:
            parsed = QDate.fromString(
                h.dto_field(day, "business_date", default=""), "yyyy-MM-dd"
            )
            if parsed.isValid():
                return parsed
        return QDate.currentDate()

    def _business_date(self) -> str:
        return self._date_edit.date().toString("yyyy-MM-dd")

    # ------------------------------------------------------------------
    # tab construction
    # ------------------------------------------------------------------
    def _build_customers_tab(self, tab: QWidget) -> None:
        layout = QVBoxLayout(tab)

        btn_row = QHBoxLayout()
        self._add_customer_btn = QPushButton(tab)
        self._add_customer_btn.clicked.connect(self._on_add_customer)
        self._edit_customer_btn = QPushButton(tab)
        self._edit_customer_btn.clicked.connect(self._on_edit_customer)
        self._adjust_opening_btn = QPushButton(tab)
        self._adjust_opening_btn.clicked.connect(self._on_adjust_opening)
        self._show_inactive_check = QCheckBox(tab)
        self._show_inactive_check.toggled.connect(self.refresh)
        btn_row.addWidget(self._add_customer_btn)
        btn_row.addWidget(self._edit_customer_btn)
        btn_row.addWidget(self._adjust_opening_btn)
        btn_row.addWidget(self._show_inactive_check)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self._cust_table = QTableWidget(tab)
        self._cust_table.setColumnCount(6)
        self._cust_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._cust_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._cust_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self._cust_table, 1)

    def _build_tickets_tab(self, tab: QWidget) -> None:
        layout = QVBoxLayout(tab)

        btn_row = QHBoxLayout()
        self._issue_ticket_btn = QPushButton(tab)
        self._issue_ticket_btn.clicked.connect(self._on_issue_ticket)
        self._ticket_detail_btn = QPushButton(tab)
        self._ticket_detail_btn.clicked.connect(self._on_ticket_detail)
        self._reverse_ticket_btn = QPushButton(tab)
        self._reverse_ticket_btn.clicked.connect(self._on_reverse_ticket)
        btn_row.addWidget(self._issue_ticket_btn)
        btn_row.addWidget(self._ticket_detail_btn)
        btn_row.addWidget(self._reverse_ticket_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self._ticket_table = QTableWidget(tab)
        self._ticket_table.setColumnCount(7)
        self._ticket_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._ticket_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._ticket_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self._ticket_table, 1)

    def _build_ledger_tab(self, tab: QWidget) -> None:
        layout = QVBoxLayout(tab)

        form_row = QHBoxLayout()
        self._ledger_customer_label = QLabel(tab)
        self._ledger_customer_combo = QComboBox(tab)
        self._ledger_customer_combo.setMinimumWidth(220)
        self._ledger_from_label = QLabel(tab)
        self._ledger_from_edit = QDateEdit(tab)
        self._ledger_from_edit.setDisplayFormat("dd-MM-yyyy")
        self._ledger_from_edit.setCalendarPopup(True)
        self._ledger_to_label = QLabel(tab)
        self._ledger_to_edit = QDateEdit(tab)
        self._ledger_to_edit.setDisplayFormat("dd-MM-yyyy")
        self._ledger_to_edit.setCalendarPopup(True)
        self._ledger_view_btn = QPushButton(tab)
        self._ledger_view_btn.clicked.connect(self._on_view_statement)
        form_row.addWidget(self._ledger_customer_label)
        form_row.addWidget(self._ledger_customer_combo)
        form_row.addWidget(self._ledger_from_label)
        form_row.addWidget(self._ledger_from_edit)
        form_row.addWidget(self._ledger_to_label)
        form_row.addWidget(self._ledger_to_edit)
        form_row.addWidget(self._ledger_view_btn)
        form_row.addStretch(1)
        layout.addLayout(form_row)

        self._ledger_table = QTableWidget(tab)
        self._ledger_table.setColumnCount(6)
        self._ledger_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._ledger_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self._ledger_table, 1)

        self._ledger_totals_label = QLabel(tab)
        self._ledger_totals_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self._ledger_totals_label)

        # Default the statement range to the selected business date's month.
        selected = self._date_edit.date()
        self._ledger_from_edit.setDate(QDate(selected.year(), selected.month(), 1))
        self._ledger_to_edit.setDate(selected)

    # ------------------------------------------------------------------
    def retranslate(self) -> None:
        """Re-apply all static texts (called on language change)."""
        self._title.setText(tr("cust.title"))
        self._date_label.setText(tr("cust.date") + ":")
        self._refresh_btn.setText(tr("cust.refresh"))
        self._tabs.setTabText(0, tr("cust.tab_customers"))
        self._tabs.setTabText(1, tr("cust.tab_tickets"))
        self._tabs.setTabText(2, tr("cust.tab_ledger"))

        self._add_customer_btn.setText(tr("cust.add_customer"))
        self._edit_customer_btn.setText(tr("cust.edit_customer"))
        self._adjust_opening_btn.setText(tr("cust.adjust_opening"))
        self._show_inactive_check.setText(tr("cust.show_inactive"))
        self._cust_table.setHorizontalHeaderLabels(
            [
                tr("cust.th_code"),
                tr("cust.th_name"),
                tr("cust.th_phone"),
                tr("cust.th_outstanding"),
                tr("cust.th_credit_limit"),
                tr("cust.th_status"),
            ]
        )

        self._issue_ticket_btn.setText(tr("ticket.issue"))
        self._ticket_detail_btn.setText(tr("ticket.view"))
        self._reverse_ticket_btn.setText(tr("ticket.reverse"))
        self._ticket_table.setHorizontalHeaderLabels(
            [
                tr("ticket.th_receipt"),
                tr("ticket.th_customer"),
                tr("ticket.th_gross"),
                tr("ticket.th_paid_now"),
                tr("ticket.th_unpaid"),
                tr("ticket.th_status"),
                tr("ticket.th_warnings"),
            ]
        )

        self._ledger_customer_label.setText(tr("ledger.customer") + ":")
        self._ledger_from_label.setText(tr("ledger.from") + ":")
        self._ledger_to_label.setText(tr("ledger.to") + ":")
        self._ledger_view_btn.setText(tr("ledger.view"))
        self._ledger_table.setHorizontalHeaderLabels(
            [
                tr("ledger.th_date"),
                tr("ledger.th_type"),
                tr("ledger.th_description"),
                tr("ledger.th_debit"),
                tr("ledger.th_credit"),
                tr("ledger.th_balance"),
            ]
        )
        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Reload customers, the day's tickets and the ledger customer list."""
        self._refresh_customers()
        self._refresh_tickets()
        self._refresh_ledger_customers()

    # ------------------------------------------------------------------
    # customers tab
    # ------------------------------------------------------------------
    def _outstanding_map(self) -> dict:
        """customer_code -> outstanding paisa, from the service view.

        Tolerates both shapes: a list of {"customer", "outstanding_paisa"}
        mappings and a plain list of CustomerInfo (which carries
        outstanding_paisa itself).
        """
        try:
            rows = self._credit.outstanding_all() or []
        except Exception:  # noqa: BLE001 — CustomerInfo may carry it already
            return {}
        result = {}
        for row in rows:
            customer = h.dto_field(row, "customer")
            if customer is None and h.dto_field(
                row, "customer_code", "code", default=None
            ) is not None:
                customer = row
            code = h.dto_field(customer, "customer_code", "code", default=None)
            amount = h.dto_field(row, "outstanding_paisa", "outstanding", default=None)
            if code is not None:
                result[code] = amount
        return result

    def _ticket_customer_text(self, ticket) -> str:
        """Customer display name for a ticket (the DTO carries only the id)."""
        name = h.dto_field(ticket, "customer_name", default=None)
        if name:
            return str(name)
        customer_id = h.dto_field(ticket, "customer_id", default=None)
        if customer_id is not None and customer_id in self._customer_names:
            return self._customer_names[customer_id]
        code = h.dto_field(ticket, "customer_code", default=None)
        return str(code or "—")

    def _refresh_customers(self) -> None:
        # Clear first: on failure the old rows must not linger.
        self._customers = []
        self._cust_table.setRowCount(0)
        try:
            all_customers = self._credit.list_customers(active_only=False) or []
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        # Name lookup for tickets/statements (covers inactive customers too);
        # the table itself respects the "show inactive" checkbox.
        self._customer_names = {
            h.dto_field(c, "id", "customer_id", default=None): str(
                h.dto_field(c, "name", default="—")
            )
            for c in all_customers
        }
        if self._show_inactive_check.isChecked():
            self._customers = all_customers
        else:
            self._customers = [
                c
                for c in all_customers
                if h.dto_field(c, "status", default="active") == "active"
            ]
        self._outstanding_cache = self._outstanding_map()
        self._cust_table.setRowCount(len(self._customers))
        for row, customer in enumerate(self._customers):
            code = h.dto_field(customer, "customer_code", "code", default="—")
            outstanding = h.dto_field(customer, "outstanding_paisa", default=None)
            if outstanding is None:
                outstanding = self._outstanding_cache.get(code)
            limit = h.dto_field(customer, "credit_limit_paisa", default=None)
            status = h.dto_field(customer, "status", default=None)
            status_text = (
                tr("cust.status_active")
                if status == "active"
                else tr("cust.status_inactive")
                if status == "inactive"
                else str(status or "—")
            )
            values = [
                str(code),
                str(h.dto_field(customer, "name", default="—")),
                str(h.dto_field(customer, "phone", default=None) or "—"),
                h.format_pkr(outstanding),
                h.format_pkr(limit) if limit is not None else tr("cust.no_limit"),
                status_text,
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col in (3, 4):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._cust_table.setItem(row, col, item)
        self._cust_table.resizeColumnsToContents()

    def _selected_customer(self):
        row = self._cust_table.currentRow()
        if 0 <= row < len(self._customers):
            return self._customers[row]
        return None

    def _on_add_customer(self) -> None:
        _CustomerDialog(self, self._credit).exec()
        self._refresh_customers()

    def _on_edit_customer(self) -> None:
        customer = self._selected_customer()
        if customer is None:
            h.show_error(self, errors.BusinessRuleError(tr("cust.select_customer_first")))
            return
        _CustomerDialog(self, self._credit, existing=customer).exec()
        self._refresh_customers()

    def _on_adjust_opening(self) -> None:
        customer = self._selected_customer()
        if customer is None:
            h.show_error(self, errors.BusinessRuleError(tr("cust.select_customer_first")))
            return
        _AdjustOpeningDialog(
            self, self._credit, customer, self._business_date()
        ).exec()
        self._refresh_customers()

    # ------------------------------------------------------------------
    # tickets tab
    # ------------------------------------------------------------------
    def _refresh_tickets(self) -> None:
        # Clear first: on failure the old rows must not linger.
        self._tickets = []
        self._ticket_table.setRowCount(0)
        try:
            self._tickets = self._credit.day_tickets(self._business_date()) or []
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._ticket_table.setRowCount(len(self._tickets))
        for row, ticket in enumerate(self._tickets):
            status = h.dto_field(ticket, "status", default=None)
            status_key = _TICKET_STATUS_KEYS.get(status)
            warnings = h.dto_field(ticket, "warnings", default=None) or []
            values = [
                str(h.dto_field(ticket, "receipt_no", default="—")),
                self._ticket_customer_text(ticket),
                h.format_pkr(h.dto_field(ticket, "gross_amount_paisa", default=None)),
                h.format_pkr(h.dto_field(ticket, "paid_now_paisa", default=None)),
                h.format_pkr(h.dto_field(ticket, "unpaid_paisa", default=None)),
                tr(status_key) if status_key else str(status or "—"),
                "—" if not warnings else str(len(warnings)),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col in (2, 3, 4, 6):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if warnings and col == 6:
                    item.setToolTip("\n".join(str(w) for w in warnings))
                self._ticket_table.setItem(row, col, item)
        self._ticket_table.resizeColumnsToContents()

    def _selected_ticket(self):
        row = self._ticket_table.currentRow()
        if 0 <= row < len(self._tickets):
            return self._tickets[row]
        return None

    def _on_issue_ticket(self) -> None:
        _IssueTicketDialog(
            self, self._credit, self._business_date(), self._nozzles
        ).exec()
        self._refresh_tickets()
        self._refresh_customers()

    def _on_ticket_detail(self) -> None:
        ticket = self._selected_ticket()
        if ticket is None:
            h.show_error(self, errors.BusinessRuleError(tr("ticket.select_ticket_first")))
            return
        uuid = h.dto_field(ticket, "uuid", default=None)
        try:
            details = self._credit.ticket_details(uuid)
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        _TicketDetailDialog(
            self,
            self._credit,
            details,
            self._business_date(),
            self._ticket_customer_text(ticket),
        ).exec()
        self._refresh_tickets()
        self._refresh_customers()

    def _on_reverse_ticket(self) -> None:
        ticket = self._selected_ticket()
        if ticket is None:
            h.show_error(self, errors.BusinessRuleError(tr("ticket.select_ticket_first")))
            return
        if h.dto_field(ticket, "status", default=None) != "posted":
            h.show_error(
                self,
                errors.BusinessRuleError(tr("ticket.status_reversed")),
            )
            return
        receipt = h.dto_field(ticket, "receipt_no", default="—")
        confirm = _ReasonDialog(
            self,
            tr("ticket.reverse_title"),
            tr("ticket.confirm_reverse").format(receipt=receipt)
            + "\n"
            + tr("ticket.reverse_reason"),
        )
        if confirm.exec() != QDialog.Accepted:
            return
        try:
            self._credit.reverse_ticket(
                h.dto_field(ticket, "uuid", default=None),
                confirm.reason,
                self._business_date(),
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        h.show_info(self, tr("ticket.reversed_ok"))
        self._refresh_tickets()
        self._refresh_customers()

    # ------------------------------------------------------------------
    # ledger tab
    # ------------------------------------------------------------------
    def _refresh_ledger_customers(self) -> None:
        current = self._ledger_customer_combo.currentData()
        current_id = (
            h.dto_field(current, "id", "customer_id", default=None)
            if current is not None
            else None
        )
        # Clear first (signals blocked): on failure the old list must not
        # linger as if it were current.
        self._ledger_customer_combo.blockSignals(True)
        self._ledger_customer_combo.clear()
        self._ledger_customer_combo.blockSignals(False)
        try:
            customers = self._credit.list_customers(active_only=False) or []
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._ledger_customer_combo.blockSignals(True)
        self._ledger_customer_combo.clear()
        for customer in customers:
            code = h.dto_field(customer, "customer_code", "code", default="?")
            name = h.dto_field(customer, "name", default="")
            self._ledger_customer_combo.addItem(f"{code} — {name}", customer)
        if current_id is not None:
            for index in range(self._ledger_customer_combo.count()):
                candidate = self._ledger_customer_combo.itemData(index)
                if h.dto_field(candidate, "id", "customer_id", default=None) == current_id:
                    self._ledger_customer_combo.setCurrentIndex(index)
                    break
        self._ledger_customer_combo.blockSignals(False)

    def _on_view_statement(self) -> None:
        customer = self._ledger_customer_combo.currentData()
        if customer is None:
            h.show_error(
                self, errors.BusinessRuleError(tr("ledger.no_customer"))
            )
            return
        customer_id = h.dto_field(customer, "id", "customer_id", default=None)
        date_from = self._ledger_from_edit.date().toString("yyyy-MM-dd")
        date_to = self._ledger_to_edit.date().toString("yyyy-MM-dd")
        try:
            statement = self._credit.statement(
                customer_id, date_from=date_from, date_to=date_to
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        entries = h.dto_field(statement, "entries", default=None) or []
        self._ledger_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            occurred = h.dto_field(entry, "occurred_at_utc", default="") or ""
            date_text = (
                h.dto_field(entry, "business_date", default=None)
                or occurred[:10]
                or "—"
            )
            if len(date_text) >= 16 and "T" in occurred:
                date_text = timecalc.format_display_datetime(occurred)
            elif len(str(date_text)) == 10 and str(date_text)[4] == "-":
                date_text = timecalc.format_display_date(str(date_text))
            debit = h.dto_field(entry, "debit_paisa", default=0) or 0
            credit = h.dto_field(entry, "credit_paisa", default=0) or 0
            values = [
                str(date_text),
                _ledger_type_text(h.dto_field(entry, "entry_type", default=None)),
                str(h.dto_field(entry, "description", default="") or ""),
                "—" if not debit else h.format_pkr(debit),
                "—" if not credit else h.format_pkr(credit),
                h.format_pkr(h.dto_field(entry, "running_paisa", default=None)),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col >= 3:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._ledger_table.setItem(row, col, item)
        self._ledger_table.resizeColumnsToContents()
        self._ledger_totals_label.setText(
            tr("ledger.totals").format(
                debit=h.format_pkr(
                    h.dto_field(
                        statement, "total_debit_paisa", "debit_paisa", default=None
                    )
                ),
                credit=h.format_pkr(
                    h.dto_field(
                        statement, "total_credit_paisa", "credit_paisa", default=None
                    )
                ),
                closing=h.format_pkr(
                    h.dto_field(statement, "closing_paisa", default=None)
                ),
            )
        )
