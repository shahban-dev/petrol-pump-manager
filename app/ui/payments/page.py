"""Customer payments page: record payments, allocate to tickets,
re-allocate, reverse, and print receipts.

All figures come from PaymentService / CustomerCreditService; the UI
parses inputs and formats service-computed values — it does no
accounting math of its own.
"""

from __future__ import annotations

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
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.calculations import time as timecalc
from app.localization import tr
from app.services import errors
from app.ui import _helpers as h

try:  # printing is a nice-to-have; the page works without it
    from PySide6.QtPrintSupport import QPrintDialog
    from PySide6.QtGui import QTextDocument

    _PRINT_AVAILABLE = True
except ImportError:  # noqa: BLE001
    _PRINT_AVAILABLE = False

#: PaymentInfo.method -> tr() keys.
_METHOD_KEYS = {
    "cash": "pay.method_cash",
    "bank": "pay.method_bank",
    "other": "pay.method_other",
}

#: PaymentInfo.status -> tr() keys.
_PAYMENT_STATUS_KEYS = {
    "posted": "pay.status_posted",
    "reversed": "pay.status_reversed",
}


def _method_text(method) -> str:
    key = _METHOD_KEYS.get(str(method or "").lower())
    return tr(key) if key else str(method or "—")


def _customer_display(payment_or_ticket, customer_names: dict) -> str:
    """Customer name for a payment/ticket DTO.

    The real DTOs carry only ``customer_id`` (plus ``customer_code``); the
    name is resolved through a lookup map, with graceful fallbacks.
    """
    name = h.dto_field(payment_or_ticket, "customer_name", default=None)
    if name:
        return str(name)
    customer_id = h.dto_field(payment_or_ticket, "customer_id", default=None)
    if customer_id is not None and customer_id in customer_names:
        return customer_names[customer_id]
    code = h.dto_field(payment_or_ticket, "customer_code", default=None)
    return str(code or "—")


def _same_customer(ticket, payment) -> bool:
    """True when the ticket belongs to the payment's customer (id-first)."""
    ticket_id = h.dto_field(ticket, "customer_id", default=None)
    payment_id = h.dto_field(payment, "customer_id", default=None)
    if ticket_id is not None and payment_id is not None:
        return ticket_id == payment_id
    ticket_name = h.dto_field(ticket, "customer_name", default=None)
    payment_name = h.dto_field(payment, "customer_name", default=None)
    if ticket_name and payment_name:
        return ticket_name == payment_name
    return True  # cannot tell: leave the final check to the service


class _RecordPaymentDialog(QDialog):
    """Record a customer payment. The service call happens on OK; the
    dialog stays open (preserving input) when validation fails."""

    def __init__(self, parent, payment_service, credit_service, business_date) -> None:
        super().__init__(parent)
        self._payments = payment_service
        self._business_date = business_date
        self.setWindowTitle(tr("pay.dialog_title"))
        self.setMinimumWidth(400)

        form = QFormLayout(self)
        self._customer_combo = QComboBox(self)
        try:
            customers = credit_service.list_customers(active_only=True) or []
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            customers = []
        for customer in customers:
            code = h.dto_field(customer, "customer_code", "code", default="?")
            name = h.dto_field(customer, "name", default="")
            self._customer_combo.addItem(f"{code} — {name}", customer)
        form.addRow(QLabel(tr("pay.customer"), self), self._customer_combo)

        self._amount_edit = h.make_integer_edit(self)
        self._amount_edit.setAlignment(Qt.AlignRight)
        form.addRow(QLabel(tr("pay.amount_pkr"), self), self._amount_edit)

        self._method_combo = QComboBox(self)
        for code in ("cash", "bank", "other"):
            self._method_combo.addItem(tr(_METHOD_KEYS[code]), code)
        form.addRow(QLabel(tr("pay.method"), self), self._method_combo)

        self._remarks_edit = QLineEdit(self)
        form.addRow(QLabel(tr("pay.remarks"), self), self._remarks_edit)

        self._auto_check = QCheckBox(tr("pay.auto_allocate"), self)
        self._auto_check.setObjectName("auto_allocate_check")
        self._auto_check.setChecked(True)
        form.addRow(self._auto_check)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    # ------------------------------------------------------------------
    def _on_accept(self) -> None:
        customer = self._customer_combo.currentData()
        if customer is None:
            h.show_error(self, ValueError(tr("cust.select_customer_first")))
            return
        customer_id = h.dto_field(customer, "id", "customer_id", default=None)
        try:
            amount = h.parse_pkr_to_paisa(self._amount_edit.text())
        except ValueError as exc:
            h.show_error(self, exc)
            return
        if amount <= 0:
            h.show_error(self, ValueError(tr("pay.amount_pkr")))
            return
        try:
            payment = self._payments.record_payment(
                customer_id,
                self._business_date,
                amount,
                self._method_combo.currentData(),
                remarks=self._remarks_edit.text().strip() or None,
                auto_allocate=self._auto_check.isChecked(),
            )
        except Exception as exc:  # noqa: BLE001 — keep dialog open, keep input
            h.show_error(self, exc)
            return
        receipt = h.dto_field(payment, "receipt_no", default="—")
        message = tr("pay.recorded_ok").format(receipt=receipt)
        unallocated = h.dto_field(payment, "unallocated_paisa", default=None)
        if unallocated:
            message += (
                "\n" + tr("pay.unallocated_label") + ": " + h.format_pkr(unallocated)
            )
        h.show_info(self, message)
        self.accept()


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


class _AllocateDialog(QDialog):
    """View a payment's allocations and add manual ones.

    Candidate tickets are the customer's unpaid posted tickets for the
    chosen ticket date (service-computed unpaid amounts)."""

    def __init__(
        self, parent, payment_service, credit_service, payment_uuid, business_date
    ) -> None:
        super().__init__(parent)
        self._payments = payment_service
        self._credit = credit_service
        self._payment_uuid = payment_uuid
        self._business_date = business_date
        self._payment = None
        self._pending: list[dict] = []  # {"ticket_uuid","ticket_receipt","amount_paisa"}
        self.setWindowTitle(tr("pay.alloc_title"))
        self.setMinimumWidth(620)

        layout = QVBoxLayout(self)

        self._summary_label = QLabel(self)
        self._summary_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self._summary_label)

        self._alloc_group = QGroupBox(tr("pay.allocations_section"), self)
        alloc_layout = QVBoxLayout(self._alloc_group)
        self._alloc_table = QTableWidget(self)
        self._alloc_table.setColumnCount(2)
        self._alloc_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._alloc_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._alloc_table.horizontalHeader().setStretchLastSection(True)
        self._alloc_table.setMinimumHeight(110)
        alloc_layout.addWidget(self._alloc_table)
        realloc_row = QHBoxLayout()
        self._realloc_btn = QPushButton(tr("pay.reallocate"), self)
        self._realloc_btn.clicked.connect(self._on_reallocate)
        realloc_row.addWidget(self._realloc_btn)
        realloc_row.addStretch(1)
        alloc_layout.addLayout(realloc_row)
        layout.addWidget(self._alloc_group)

        add_group = QGroupBox(tr("pay.add_allocation"), self)
        add_layout = QHBoxLayout(add_group)
        self._ticket_date_edit = QDateEdit(self)
        self._ticket_date_edit.setDisplayFormat("dd-MM-yyyy")
        self._ticket_date_edit.setCalendarPopup(True)
        self._ticket_date_edit.setDate(
            QDate.fromString(business_date, "yyyy-MM-dd")
        )
        self._load_btn = QPushButton(tr("pay.load_tickets"), self)
        self._load_btn.clicked.connect(self._load_candidates)
        self._ticket_combo = QComboBox(self)
        self._ticket_combo.setMinimumWidth(200)
        self._split_amount_edit = h.make_integer_edit(self)
        self._split_amount_edit.setAlignment(Qt.AlignRight)
        self._split_amount_edit.setPlaceholderText(tr("pay.amount_pkr"))
        self._split_amount_edit.setMinimumWidth(110)
        self._add_alloc_btn = QPushButton(tr("pay.add_allocation"), self)
        self._add_alloc_btn.setObjectName("add_alloc_btn")
        self._add_alloc_btn.clicked.connect(self._on_add_split)
        add_layout.addWidget(QLabel(tr("pay.alloc_date"), self))
        add_layout.addWidget(self._ticket_date_edit)
        add_layout.addWidget(self._load_btn)
        add_layout.addWidget(QLabel(tr("pay.ticket_label"), self))
        add_layout.addWidget(self._ticket_combo, 1)
        add_layout.addWidget(self._split_amount_edit)
        add_layout.addWidget(self._add_alloc_btn)
        layout.addWidget(add_group)

        self._pending_table = QTableWidget(self)
        self._pending_table.setColumnCount(2)
        self._pending_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._pending_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._pending_table.horizontalHeader().setStretchLastSection(True)
        self._pending_table.setMinimumHeight(90)
        layout.addWidget(self._pending_table)
        pending_row = QHBoxLayout()
        self._remove_pending_btn = QPushButton(tr("pay.remove_allocation"), self)
        self._remove_pending_btn.clicked.connect(self._on_remove_pending)
        pending_row.addWidget(self._remove_pending_btn)
        pending_row.addStretch(1)
        layout.addLayout(pending_row)

        btn_row = QHBoxLayout()
        self._auto_btn = QPushButton(tr("pay.auto_allocate_btn"), self)
        self._auto_btn.clicked.connect(self._on_auto_allocate)
        self._save_btn = QPushButton(tr("pay.save_allocations"), self)
        self._save_btn.setObjectName("save_btn")
        self._save_btn.clicked.connect(self._on_save)
        self._close_btn = QPushButton(tr("common.ok"), self)
        self._close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._auto_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self._save_btn)
        btn_row.addWidget(self._close_btn)
        layout.addLayout(btn_row)

        self._reload()

    # ------------------------------------------------------------------
    def _payment_id(self):
        return h.dto_field(self._payment, "id", "payment_id", default=None) or self._payment_uuid

    def _reload(self) -> None:
        # Clear first: on failure the previous payment's details must not
        # linger as if they were current.
        self._payment = None
        self._summary_label.setText("—")
        self._alloc_table.setRowCount(0)
        try:
            self._payment = self._payments.payment_details(self._payment_uuid)
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        try:
            customers = self._credit.list_customers(active_only=False) or []
        except Exception:  # noqa: BLE001 — name lookup is nice-to-have
            customers = []
        self._customer_names = {
            h.dto_field(c, "id", "customer_id", default=None): str(
                h.dto_field(c, "name", default="—")
            )
            for c in customers
        }
        receipt = h.dto_field(self._payment, "receipt_no", default="—")
        customer = _customer_display(self._payment, self._customer_names)
        amount = h.dto_field(self._payment, "amount_paisa", default=None)
        unallocated = h.dto_field(self._payment, "unallocated_paisa", default=None)
        self._summary_label.setText(
            f"{tr('pay.th_receipt')}: {receipt} • "
            f"{tr('pay.customer')}: {customer} • "
            f"{tr('pay.th_amount')}: {h.format_pkr(amount)} • "
            f"{tr('pay.unallocated_label')}: {h.format_pkr(unallocated)}"
        )
        allocations = h.dto_field(self._payment, "allocations", default=None) or []
        self._alloc_table.setRowCount(len(allocations))
        for row, alloc in enumerate(allocations):
            values = [
                str(
                    h.dto_field(
                        alloc, "ticket_receipt_no", "ticket_receipt", default="—"
                    )
                ),
                h.format_pkr(
                    h.dto_field(alloc, "effective_paisa", "amount_paisa", default=None)
                ),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col == 1:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._alloc_table.setItem(row, col, item)
        self._alloc_table.setHorizontalHeaderLabels(
            [tr("pay.th_alloc_ticket"), tr("pay.th_alloc_amount")]
        )
        self._alloc_table.resizeColumnsToContents()
        self._load_candidates()
        self._refresh_pending_table()

    def _load_candidates(self) -> None:
        """Unpaid posted tickets of this payment's customer for the date."""
        ticket_date = self._ticket_date_edit.date().toString("yyyy-MM-dd")
        candidates = []
        try:
            tickets = self._credit.day_tickets(ticket_date) or []
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            tickets = []
        for ticket in tickets:
            if h.dto_field(ticket, "status", default=None) != "posted":
                continue
            if not _same_customer(ticket, self._payment):
                continue
            unpaid = h.dto_field(ticket, "unpaid_paisa", default=0) or 0
            if unpaid <= 0:
                continue
            candidates.append(ticket)
        self._ticket_combo.blockSignals(True)
        self._ticket_combo.clear()
        for ticket in candidates:
            receipt = h.dto_field(ticket, "receipt_no", default="?")
            unpaid = h.dto_field(ticket, "unpaid_paisa", default=0) or 0
            self._ticket_combo.addItem(
                f"{receipt} — {h.format_pkr(unpaid)}", ticket
            )
        self._ticket_combo.blockSignals(False)

    # ------------------------------------------------------------------
    def _on_add_split(self) -> None:
        ticket = self._ticket_combo.currentData()
        if ticket is None:
            h.show_error(self, ValueError(tr("pay.ticket_label")))
            return
        try:
            amount = h.parse_pkr_to_paisa(self._split_amount_edit.text())
        except ValueError as exc:
            h.show_error(self, exc)
            return
        if amount <= 0:
            h.show_error(self, ValueError(tr("pay.amount_pkr")))
            return
        self._pending.append(
            {
                "ticket_uuid": h.dto_field(ticket, "uuid", default=None),
                "ticket_receipt": h.dto_field(ticket, "receipt_no", default="—"),
                "amount_paisa": amount,
            }
        )
        self._split_amount_edit.clear()
        self._refresh_pending_table()

    def _on_remove_pending(self) -> None:
        row = self._pending_table.currentRow()
        if 0 <= row < len(self._pending):
            del self._pending[row]
            self._refresh_pending_table()

    def _refresh_pending_table(self) -> None:
        self._pending_table.setRowCount(len(self._pending))
        for row, split in enumerate(self._pending):
            values = [
                str(split["ticket_receipt"]),
                h.format_pkr(split["amount_paisa"]),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col == 1:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._pending_table.setItem(row, col, item)
        self._pending_table.setHorizontalHeaderLabels(
            [tr("pay.th_alloc_ticket"), tr("pay.th_alloc_amount")]
        )
        self._pending_table.resizeColumnsToContents()

    # ------------------------------------------------------------------
    def _on_save(self) -> None:
        if not self._pending:
            h.show_error(self, ValueError(tr("pay.allocations_section")))
            return
        splits = [
            {"ticket_uuid": s["ticket_uuid"], "amount_paisa": s["amount_paisa"]}
            for s in self._pending
        ]
        try:
            self._payments.allocate(
                self._payment_id(), splits, business_date=self._business_date
            )
        except Exception as exc:  # noqa: BLE001 — keep dialog open, keep splits
            h.show_error(self, exc)
            return
        self._pending = []
        h.show_info(self, tr("pay.alloc_saved_ok"))
        self._reload()

    def _on_auto_allocate(self) -> None:
        try:
            self._payments.allocate(
                self._payment_id(), None, business_date=self._business_date
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        h.show_info(self, tr("pay.alloc_saved_ok"))
        self._reload()

    def _on_reallocate(self) -> None:
        row = self._alloc_table.currentRow()
        allocations = h.dto_field(self._payment, "allocations", default=None) or []
        if not (0 <= row < len(allocations)):
            h.show_error(
                self, errors.BusinessRuleError(tr("pay.select_payment_first"))
            )
            return
        allocation = allocations[row]
        dlg = _ReallocateDialog(
            self,
            self._payments,
            self._credit,
            allocation,
            self._payment,
            self._business_date,
        )
        dlg.exec()
        self._reload()


class _ReallocateDialog(QDialog):
    """Replace one allocation with new splits (audited, reason required)."""

    def __init__(
        self, parent, payment_service, credit_service, allocation, payment, business_date
    ) -> None:
        super().__init__(parent)
        self._payments = payment_service
        self._credit = credit_service
        self._allocation = allocation
        self._payment = payment
        self._business_date = business_date
        self._pending: list[dict] = []
        self.setWindowTitle(tr("pay.realloc_title"))
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        current = QLabel(
            f"{tr('pay.current_allocation')}: "
            f"{h.dto_field(allocation, 'ticket_receipt_no', 'ticket_receipt', default='—')} — "
            f"{h.format_pkr(h.dto_field(allocation, 'effective_paisa', 'amount_paisa', default=None))}",
            self,
        )
        current.setStyleSheet("font-weight: bold;")
        layout.addWidget(current)

        add_row = QHBoxLayout()
        self._ticket_combo = QComboBox(self)
        self._ticket_combo.setMinimumWidth(220)
        self._split_amount_edit = h.make_integer_edit(self)
        self._split_amount_edit.setAlignment(Qt.AlignRight)
        self._split_amount_edit.setPlaceholderText(tr("pay.amount_pkr"))
        self._add_btn = QPushButton(tr("pay.add_allocation"), self)
        self._add_btn.setObjectName("add_alloc_btn")
        self._add_btn.clicked.connect(self._on_add_split)
        add_row.addWidget(QLabel(tr("pay.ticket_label"), self))
        add_row.addWidget(self._ticket_combo, 1)
        add_row.addWidget(self._split_amount_edit)
        add_row.addWidget(self._add_btn)
        layout.addLayout(add_row)

        self._pending_table = QTableWidget(self)
        self._pending_table.setColumnCount(2)
        self._pending_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._pending_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._pending_table.horizontalHeader().setStretchLastSection(True)
        self._pending_table.setMinimumHeight(90)
        layout.addWidget(self._pending_table)
        self._pending_table.setHorizontalHeaderLabels(
            [tr("pay.th_alloc_ticket"), tr("pay.th_alloc_amount")]
        )

        self._reason_edit = QTextEdit(self)
        self._reason_edit.setMaximumHeight(70)
        layout.addWidget(QLabel(tr("pay.realloc_reason"), self))
        layout.addWidget(self._reason_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._load_candidates()

    # ------------------------------------------------------------------
    def _load_candidates(self) -> None:
        candidates = []
        try:
            tickets = self._credit.day_tickets(self._business_date) or []
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            tickets = []
        for ticket in tickets:
            if h.dto_field(ticket, "status", default=None) != "posted":
                continue
            if not _same_customer(ticket, self._payment):
                continue
            candidates.append(ticket)
        # Always include the currently-allocated ticket so its remainder can
        # be re-split onto itself.
        current_uuid = h.dto_field(self._allocation, "ticket_uuid", default=None)
        if current_uuid and not any(
            h.dto_field(t, "uuid", default=None) == current_uuid for t in candidates
        ):
            try:
                details = self._credit.ticket_details(current_uuid)
                if details is not None:
                    candidates.append(details)
            except Exception:  # noqa: BLE001 — nice-to-have only
                pass
        for ticket in candidates:
            receipt = h.dto_field(ticket, "receipt_no", default="?")
            unpaid = h.dto_field(ticket, "unpaid_paisa", default=0) or 0
            self._ticket_combo.addItem(
                f"{receipt} — {h.format_pkr(unpaid)}", ticket
            )

    def _on_add_split(self) -> None:
        ticket = self._ticket_combo.currentData()
        if ticket is None:
            h.show_error(self, ValueError(tr("pay.ticket_label")))
            return
        try:
            amount = h.parse_pkr_to_paisa(self._split_amount_edit.text())
        except ValueError as exc:
            h.show_error(self, exc)
            return
        if amount <= 0:
            h.show_error(self, ValueError(tr("pay.amount_pkr")))
            return
        self._pending.append(
            {
                "ticket_uuid": h.dto_field(ticket, "uuid", default=None),
                "ticket_receipt": h.dto_field(ticket, "receipt_no", default="—"),
                "amount_paisa": amount,
            }
        )
        self._split_amount_edit.clear()
        self._refresh_pending_table()

    def _refresh_pending_table(self) -> None:
        self._pending_table.setRowCount(len(self._pending))
        for row, split in enumerate(self._pending):
            values = [
                str(split["ticket_receipt"]),
                h.format_pkr(split["amount_paisa"]),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col == 1:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._pending_table.setItem(row, col, item)
        self._pending_table.resizeColumnsToContents()

    def _on_accept(self) -> None:
        if not self._pending:
            h.show_error(self, ValueError(tr("pay.allocations_section")))
            return
        reason = self._reason_edit.toPlainText().strip()
        if not reason:
            h.show_error(self, ValueError(tr("pay.realloc_reason")))
            return
        new_splits = [
            {"ticket_uuid": s["ticket_uuid"], "amount_paisa": s["amount_paisa"]}
            for s in self._pending
        ]
        try:
            self._payments.reallocate(
                h.dto_field(self._allocation, "id", "allocation_id", default=None),
                new_splits,
                reason,
                business_date=self._business_date,
            )
        except Exception as exc:  # noqa: BLE001 — keep dialog open, keep input
            h.show_error(self, exc)
            return
        h.show_info(self, tr("pay.realloc_saved_ok"))
        self.accept()


class _ReceiptDialog(QDialog):
    """Printable payment receipt rendered from receipt_data()."""

    def __init__(self, parent, payment_service, payment_uuid) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("pay.receipt_title"))
        self.setMinimumWidth(480)
        self._html = ""

        layout = QVBoxLayout(self)
        self._browser = QTextBrowser(self)
        layout.addWidget(self._browser, 1)

        btn_row = QHBoxLayout()
        self._print_btn = QPushButton(tr("pay.print"), self)
        self._print_btn.setEnabled(_PRINT_AVAILABLE)
        self._print_btn.clicked.connect(self._on_print)
        self._close_btn = QPushButton(tr("common.ok"), self)
        self._close_btn.clicked.connect(self.accept)
        btn_row.addStretch(1)
        btn_row.addWidget(self._print_btn)
        btn_row.addWidget(self._close_btn)
        layout.addLayout(btn_row)

        try:
            data = payment_service.receipt_data(payment_uuid) or {}
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._html = self._render(data)
        self._browser.setHtml(self._html)

    # ------------------------------------------------------------------
    def _render(self, data: dict) -> str:
        def row(label, value):
            return f"<tr><td><b>{label}</b></td><td>{value}</td></tr>"

        # The service returns a nested mapping; tolerate a flat one too.
        if isinstance(data.get("payment"), dict):
            payment = data["payment"]
            customer = data.get("customer") or {}
            allocations = data.get("allocations") or []
            unallocated = data.get("unallocated_paisa")
        else:
            payment = data
            customer = data
            allocations = h.dto_field(data, "allocations", default=None) or []
            unallocated = h.dto_field(data, "unallocated_paisa", default=None)

        occurred = h.dto_field(payment, "occurred_at_utc", default="") or ""
        when = (
            timecalc.format_display_datetime(occurred)
            if occurred
            else str(h.dto_field(payment, "business_date", default="—"))
        )
        customer_text = str(
            h.dto_field(customer, "name", "customer_name", default="—")
        )
        customer_code = h.dto_field(customer, "customer_code", default=None)
        if customer_code:
            customer_text = f"{customer_code} — {customer_text}"
        parts = [
            f"<h2>{tr('pay.receipt_title')}</h2>",
            "<table cellpadding='4'>",
            row(
                tr("pay.receipt_no"),
                h.dto_field(payment, "receipt_no", default="—"),
            ),
            row(tr("pay.receipt_customer"), customer_text),
            row(
                tr("pay.receipt_date"),
                h.dto_field(payment, "business_date", default="—"),
            ),
            row(tr("pay.receipt_time"), when),
            row(
                tr("pay.receipt_amount"),
                h.format_pkr(h.dto_field(payment, "amount_paisa", default=None)),
            ),
            row(
                tr("pay.receipt_method"),
                _method_text(h.dto_field(payment, "method", default=None)),
            ),
            row(
                tr("pay.receipt_remarks"),
                str(h.dto_field(payment, "remarks", default=None) or "—"),
            ),
            "</table>",
            f"<h3>{tr('pay.receipt_allocations')}</h3>",
        ]
        if allocations:
            parts.append("<table cellpadding='4' border='1' cellspacing='0'>")
            parts.append(
                f"<tr><th>{tr('pay.th_alloc_ticket')}</th>"
                f"<th>{tr('pay.th_alloc_amount')}</th></tr>"
            )
            for alloc in allocations:
                parts.append(
                    "<tr><td>"
                    f"{h.dto_field(alloc, 'ticket_receipt_no', 'ticket_receipt', default='—')}"
                    "</td><td align='right'>"
                    f"{h.format_pkr(h.dto_field(alloc, 'effective_paisa', 'amount_paisa', default=None))}"
                    "</td></tr>"
                )
            parts.append("</table>")
        else:
            parts.append("<p>—</p>")
        parts.append(
            f"<p><b>{tr('pay.receipt_unallocated')}:</b> "
            f"{h.format_pkr(unallocated)}</p>"
        )
        return "\n".join(parts)

    def _on_print(self) -> None:
        if not _PRINT_AVAILABLE:
            return
        dialog = QPrintDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            document = QTextDocument()
            document.setHtml(self._html)
            document.print_(dialog.printer())
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)


class CustomerPaymentsPage(QWidget):
    """Record customer payments and allocate them to credit tickets."""

    def __init__(self, payment_service, credit_service, day_service, parent=None) -> None:
        super().__init__(parent)
        self._payments = payment_service
        self._credit = credit_service
        self._days = day_service
        self._payments_list: list = []
        self._customer_names: dict = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self._banner = h.make_page_banner(
            self, tr("pay.title"), color="blue"
        )
        layout.insertWidget(0, self._banner)

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

        # -- actions -------------------------------------------------------
        btn_row = QHBoxLayout()
        self._record_payment_btn = QPushButton(self)
        self._record_payment_btn.clicked.connect(self._on_record_payment)
        self._view_allocate_btn = QPushButton(self)
        self._view_allocate_btn.clicked.connect(self._on_view_allocate)
        self._reverse_payment_btn = QPushButton(self)
        self._reverse_payment_btn.clicked.connect(self._on_reverse_payment)
        self._receipt_btn = QPushButton(self)
        self._receipt_btn.clicked.connect(self._on_receipt)
        btn_row.addWidget(self._record_payment_btn)
        btn_row.addWidget(self._view_allocate_btn)
        btn_row.addWidget(self._reverse_payment_btn)
        btn_row.addWidget(self._receipt_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        # -- payments table ------------------------------------------------
        self._pay_group = QGroupBox(self)
        group_layout = QVBoxLayout(self._pay_group)
        self._pay_table = QTableWidget(self)
        self._pay_table.setColumnCount(7)
        self._pay_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._pay_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._pay_table.horizontalHeader().setStretchLastSection(True)
        group_layout.addWidget(self._pay_table)
        layout.addWidget(self._pay_group, 1)

        layout.addStretch(1)
        self.retranslate()
        self.refresh()

    # ------------------------------------------------------------------
    def _default_date(self) -> QDate:
        try:
            day = self._days.get_open_day()
        except Exception:  # noqa: BLE001
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

    def retranslate(self) -> None:
        """Re-apply all static texts (called on language change)."""
        self._title.setText(tr("pay.title"))
        banner_title = self._banner.findChild(QLabel, "pageBannerTitle")
        if banner_title is not None:
            banner_title.setText(tr("pay.title"))
        self._date_label.setText(tr("pay.date") + ":")
        self._refresh_btn.setText(tr("pay.refresh"))
        self._record_payment_btn.setText(tr("pay.record"))
        self._view_allocate_btn.setText(tr("pay.view_allocate"))
        self._reverse_payment_btn.setText(tr("pay.reverse"))
        self._receipt_btn.setText(tr("pay.receipt"))
        self._pay_group.setTitle(tr("pay.title"))
        self._pay_table.setHorizontalHeaderLabels(
            [
                tr("pay.th_receipt"),
                tr("pay.th_customer"),
                tr("pay.th_amount"),
                tr("pay.th_method"),
                tr("pay.th_allocated"),
                tr("pay.th_unallocated"),
                tr("pay.th_status"),
            ]
        )
        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Reload the day's payments."""
        # Clear first: on failure the old rows must not linger.
        self._payments_list = []
        self._pay_table.setRowCount(0)
        try:
            self._payments_list = (
                self._payments.day_payments(self._business_date()) or []
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        try:
            customers = self._credit.list_customers(active_only=False) or []
        except Exception:  # noqa: BLE001 — name lookup is nice-to-have
            customers = []
        self._customer_names = {
            h.dto_field(c, "id", "customer_id", default=None): str(
                h.dto_field(c, "name", default="—")
            )
            for c in customers
        }
        self._pay_table.setRowCount(len(self._payments_list))
        for row, payment in enumerate(self._payments_list):
            amount = h.dto_field(payment, "amount_paisa", default=None)
            unallocated = h.dto_field(payment, "unallocated_paisa", default=None)
            # Display-only derivation from two service-computed values.
            allocated = (
                (amount - unallocated)
                if isinstance(amount, int) and isinstance(unallocated, int)
                else None
            )
            method = h.dto_field(payment, "method", default=None)
            status = h.dto_field(payment, "status", default=None)
            status_key = _PAYMENT_STATUS_KEYS.get(status)
            values = [
                str(h.dto_field(payment, "receipt_no", default="—")),
                _customer_display(payment, self._customer_names),
                h.format_pkr(amount),
                _method_text(method),
                h.format_pkr(allocated),
                h.format_pkr(unallocated),
                tr(status_key) if status_key else str(status or "—"),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col in (2, 4, 5):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._pay_table.setItem(row, col, item)
        self._pay_table.resizeColumnsToContents()

    # ------------------------------------------------------------------
    def _selected_payment(self):
        row = self._pay_table.currentRow()
        if 0 <= row < len(self._payments_list):
            return self._payments_list[row]
        return None

    def _on_record_payment(self) -> None:
        _RecordPaymentDialog(
            self, self._payments, self._credit, self._business_date()
        ).exec()
        self.refresh()

    def _on_view_allocate(self) -> None:
        payment = self._selected_payment()
        if payment is None:
            h.show_error(
                self, errors.BusinessRuleError(tr("pay.select_payment_first"))
            )
            return
        _AllocateDialog(
            self,
            self._payments,
            self._credit,
            h.dto_field(payment, "uuid", default=None),
            self._business_date(),
        ).exec()
        self.refresh()

    def _on_reverse_payment(self) -> None:
        payment = self._selected_payment()
        if payment is None:
            h.show_error(
                self, errors.BusinessRuleError(tr("pay.select_payment_first"))
            )
            return
        if h.dto_field(payment, "status", default=None) != "posted":
            h.show_error(
                self, errors.BusinessRuleError(tr("pay.status_reversed"))
            )
            return
        receipt = h.dto_field(payment, "receipt_no", default="—")
        confirm = _ReasonDialog(
            self,
            tr("pay.reverse_title"),
            tr("pay.confirm_reverse").format(receipt=receipt)
            + "\n"
            + tr("pay.reverse_reason"),
        )
        if confirm.exec() != QDialog.Accepted:
            return
        try:
            self._payments.reverse_payment(
                h.dto_field(payment, "uuid", default=None),
                confirm.reason,
                self._business_date(),
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        h.show_info(self, tr("pay.reversed_ok"))
        self.refresh()

    def _on_receipt(self) -> None:
        payment = self._selected_payment()
        if payment is None:
            h.show_error(
                self, errors.BusinessRuleError(tr("pay.select_payment_first"))
            )
            return
        _ReceiptDialog(
            self,
            self._payments,
            h.dto_field(payment, "uuid", default=None),
        ).exec()
