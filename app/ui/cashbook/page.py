"""Cash Book page: cash position, daily settlements, receipts, payments.

All figures come from CashbookService; the UI parses inputs and formats
service-computed values — it does no accounting math of its own.
Shortage/surplus are shown separately and are never auto-written-off.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
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
    QComboBox,
)

from app.localization import get_language, tr
from app.services import errors
from app.ui import _helpers as h


def _confirm(parent: QWidget, title: str, text: str) -> bool:
    """A yes/no confirmation for consequential writes."""
    from PySide6.QtWidgets import QMessageBox

    return (
        QMessageBox.question(
            parent,
            title,
            text,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        == QMessageBox.Yes
    )


def _status_text(status) -> str:
    status = str(status or "").lower()
    if status == "posted":
        return tr("cb.status_posted")
    if status == "reversed":
        return tr("cb.status_reversed")
    return str(status or "—")


def _category_label(category) -> str:
    """Localized display name for a cash category DTO/dict."""
    if get_language() == "ur":
        name_ur = h.dto_field(category, "name_ur", default=None)
        if name_ur:
            return str(name_ur)
    return str(
        h.dto_field(category, "name_en", "category_en", "name", default="—")
    )


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


class _SettleDialog(QDialog):
    """Record the day's settlement batch.

    Per-product rows come from ``settlement_status`` (meter revenue,
    paid-now and receivables are ledger truth, shown read-only); the
    manager enters only the cash/bank/other split. The service call
    happens on OK; the dialog stays open (preserving input) on failure.
    """

    def __init__(self, parent, cashbook, business_date) -> None:
        super().__init__(parent)
        self._cashbook = cashbook
        self._business_date = business_date
        self.setWindowTitle(tr("cb.settle_dialog_title"))
        self.setMinimumWidth(720)

        layout = QVBoxLayout(self)
        hint = QLabel(tr("cb.settle_hint"), self)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._rows: list[dict] = []  # {"code", "cash_edit", "bank_edit", "other_edit"}
        try:
            status = cashbook.settlement_status(business_date) or {}
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            status = {}
        grid = QFormLayout()
        grid.setSpacing(8)
        header_row = QHBoxLayout()
        for label in (
            tr("cb.th_product"),
            tr("cb.th_meter"),
            tr("cb.th_paid_now"),
            tr("cb.th_new_receivables"),
            tr("cb.th_cash"),
            tr("cb.th_bank"),
            tr("cb.th_other"),
        ):
            cell = QLabel(label, self)
            cell.setStyleSheet("font-weight: bold;")
            cell.setMinimumWidth(96)
            header_row.addWidget(cell)
        layout.addLayout(header_row)
        for product in status.get("per_product", []) or []:
            code = str(product.get("fuel_product_code", "—"))
            cash_edit = h.make_integer_edit(self)
            bank_edit = h.make_integer_edit(self)
            other_edit = h.make_integer_edit(self)
            for edit in (cash_edit, bank_edit, other_edit):
                edit.setAlignment(Qt.AlignRight)
                edit.setPlaceholderText("0")
            row = QHBoxLayout()
            row.addWidget(QLabel(code, self), 0)
            info = QLabel(
                f"{h.format_pkr(product.get('meter_revenue_paisa'))}", self
            )
            row.addWidget(info, 0)
            paid_now = QLabel(
                f"{h.format_pkr(product.get('paid_now_paisa'))}", self
            )
            row.addWidget(paid_now, 0)
            new_recv = QLabel(
                f"{h.format_pkr(product.get('new_receivables_paisa'))}", self
            )
            row.addWidget(new_recv, 0)
            for widget in (info, paid_now, new_recv):
                widget.setMinimumWidth(96)
                widget.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row.addWidget(cash_edit)
            row.addWidget(bank_edit)
            row.addWidget(other_edit)
            layout.addLayout(row)
            self._rows.append(
                {
                    "code": code,
                    "cash_edit": cash_edit,
                    "bank_edit": bank_edit,
                    "other_edit": other_edit,
                }
            )
        if not self._rows:
            layout.addWidget(QLabel(tr("cb.not_recorded"), self))

        form = QFormLayout()
        self._notes_edit = QLineEdit(self)
        form.addRow(QLabel(tr("cb.notes"), self), self._notes_edit)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------
    def _parse_split(self, edit: QLineEdit, product_code: str) -> int:
        try:
            return h.parse_pkr_to_paisa(edit.text())
        except ValueError as exc:
            raise ValueError(
                f"{product_code}: {exc}"
            ) from None

    def _on_accept(self) -> None:
        cash: dict[str, int] = {}
        bank: dict[str, int] = {}
        other: dict[str, int] = {}
        for entry in self._rows:
            code = entry["code"]
            try:
                cash[code] = self._parse_split(entry["cash_edit"], code)
                bank[code] = self._parse_split(entry["bank_edit"], code)
                other[code] = self._parse_split(entry["other_edit"], code)
            except ValueError as exc:
                h.show_error(self, exc)
                return
        if not _confirm(
            self,
            self.windowTitle(),
            tr("cb.confirm_settlements").format(date=self._business_date),
        ):
            return
        try:
            self._cashbook.record_settlements(
                self._business_date,
                cash_by_product=cash,
                bank_by_product=bank,
                other_by_product=other,
                notes=self._notes_edit.text().strip() or None,
            )
        except Exception as exc:  # noqa: BLE001 — keep dialog open
            h.show_error(self, exc)
            return
        h.show_info(self, tr("cb.settled_ok"))
        self.accept()


class _CashMoveDialog(QDialog):
    """Record a manual cash receipt or payment (kind 'receipt' | 'payment').

    The service call happens on OK; the dialog stays open on failure.
    """

    def __init__(self, parent, cashbook, business_date, kind: str) -> None:
        super().__init__(parent)
        self._cashbook = cashbook
        self._business_date = business_date
        self._kind = kind
        self.setWindowTitle(
            tr("cb.receipt_dialog_title")
            if kind == "receipt"
            else tr("cb.payment_dialog_title")
        )
        self.setMinimumWidth(420)

        form = QFormLayout(self)
        self._category_combo = QComboBox(self)
        try:
            categories = cashbook.list_categories(kind) or []
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            categories = []
        for category in categories:
            self._category_combo.addItem(_category_label(category), category)
        form.addRow(
            QLabel(
                tr("cb.receipt_category")
                if kind == "receipt"
                else tr("cb.payment_category"),
                self,
            ),
            self._category_combo,
        )

        self._amount_edit = h.make_integer_edit(self)
        self._amount_edit.setAlignment(Qt.AlignRight)
        form.addRow(QLabel(tr("cb.amount_pkr"), self), self._amount_edit)

        self._party_edit = QLineEdit(self)
        if kind == "receipt":
            form.addRow(
                QLabel(tr("cb.customer_id_optional"), self), self._party_edit
            )
            self._party_edit.setPlaceholderText("—")
        else:
            form.addRow(QLabel(tr("cb.payee_optional"), self), self._party_edit)

        self._remarks_edit = QLineEdit(self)
        form.addRow(QLabel(tr("cb.remarks_optional"), self), self._remarks_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    # ------------------------------------------------------------------
    def _on_accept(self) -> None:
        category = self._category_combo.currentData()
        if category is None:
            h.show_error(self, ValueError(tr("cb.th_category")))
            return
        category_key = str(h.dto_field(category, "name", default=""))
        try:
            amount = h.parse_pkr_to_paisa(self._amount_edit.text())
        except ValueError as exc:
            h.show_error(self, exc)
            return
        if amount <= 0:
            h.show_error(self, ValueError(tr("cb.amount_pkr")))
            return
        party = self._party_edit.text().strip()
        remarks = self._remarks_edit.text().strip() or None
        try:
            if self._kind == "receipt":
                customer_id = int(party) if party else None
                self._cashbook.record_receipt(
                    business_date=self._business_date,
                    category=category_key,
                    amount_paisa=amount,
                    customer_id=customer_id,
                    remarks=remarks,
                )
            else:
                self._cashbook.record_payment(
                    business_date=self._business_date,
                    category=category_key,
                    amount_paisa=amount,
                    payee=party or None,
                    remarks=remarks,
                )
        except ValueError as exc:  # e.g. non-numeric customer id
            h.show_error(self, exc)
            return
        except Exception as exc:  # noqa: BLE001 — keep dialog open
            h.show_error(self, exc)
            return
        h.show_info(self, tr("cb.recorded_ok"))
        self.accept()


class CashBookPage(QWidget):
    """Cash position, settlements, receipts, payments and the audit trail."""

    def __init__(self, cashbook_service, day_service, parent=None) -> None:
        super().__init__(parent)
        self._cashbook = cashbook_service
        self._days = day_service
        self._receipts_list: list = []
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

        # -- cash position --------------------------------------------------
        self._position_group = QGroupBox(self)
        position_form = QFormLayout(self._position_group)
        self._opening_label = QLabel(self)
        self._opening_value = QLabel(self)
        self._receipts_label = QLabel(self)
        self._receipts_value = QLabel(self)
        self._payments_label = QLabel(self)
        self._payments_value = QLabel(self)
        self._expected_label = QLabel(self)
        self._expected_value = QLabel(self)
        self._physical_label = QLabel(self)
        self._physical_value = QLabel(self)
        self._difference_label = QLabel(self)
        self._difference_value = QLabel(self)
        self._shortage_label = QLabel(self)
        self._shortage_value = QLabel(self)
        self._surplus_label = QLabel(self)
        self._surplus_value = QLabel(self)
        position_form.addRow(self._opening_label, self._opening_value)
        position_form.addRow(self._receipts_label, self._receipts_value)
        position_form.addRow(self._payments_label, self._payments_value)
        position_form.addRow(self._expected_label, self._expected_value)
        position_form.addRow(self._physical_label, self._physical_value)
        position_form.addRow(self._difference_label, self._difference_value)
        position_form.addRow(self._shortage_label, self._shortage_value)
        position_form.addRow(self._surplus_label, self._surplus_value)
        layout.addWidget(self._position_group)

        # -- settlements ----------------------------------------------------
        self._settle_group = QGroupBox(self)
        settle_layout = QVBoxLayout(self._settle_group)
        self._settle_banner = QLabel(self)
        self._settle_banner.setWordWrap(True)
        settle_layout.addWidget(self._settle_banner)
        self._settle_table = QTableWidget(self)
        self._settle_table.setColumnCount(9)
        self._settle_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._settle_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._settle_table.horizontalHeader().setStretchLastSection(True)
        settle_layout.addWidget(self._settle_table)
        settle_btns = QHBoxLayout()
        self._record_settle_btn = QPushButton(self)
        self._record_settle_btn.clicked.connect(self._on_record_settlements)
        self._reverse_settle_btn = QPushButton(self)
        self._reverse_settle_btn.clicked.connect(self._on_reverse_settlements)
        settle_btns.addWidget(self._record_settle_btn)
        settle_btns.addWidget(self._reverse_settle_btn)
        settle_btns.addStretch(1)
        settle_layout.addLayout(settle_btns)
        layout.addWidget(self._settle_group)

        # -- receipts --------------------------------------------------------
        self._receipts_group = QGroupBox(self)
        receipts_layout = QVBoxLayout(self._receipts_group)
        self._receipts_table = QTableWidget(self)
        self._receipts_table.setColumnCount(7)
        self._receipts_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._receipts_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._receipts_table.horizontalHeader().setStretchLastSection(True)
        receipts_layout.addWidget(self._receipts_table)
        receipt_btns = QHBoxLayout()
        self._record_receipt_btn = QPushButton(self)
        self._record_receipt_btn.clicked.connect(self._on_record_receipt)
        self._reverse_receipt_btn = QPushButton(self)
        self._reverse_receipt_btn.clicked.connect(self._on_reverse_receipt)
        receipt_btns.addWidget(self._record_receipt_btn)
        receipt_btns.addWidget(self._reverse_receipt_btn)
        receipt_btns.addStretch(1)
        receipts_layout.addLayout(receipt_btns)
        layout.addWidget(self._receipts_group)

        # -- payments ---------------------------------------------------------
        self._payments_group = QGroupBox(self)
        payments_layout = QVBoxLayout(self._payments_group)
        self._payments_table = QTableWidget(self)
        self._payments_table.setColumnCount(7)
        self._payments_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._payments_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._payments_table.horizontalHeader().setStretchLastSection(True)
        payments_layout.addWidget(self._payments_table)
        payment_btns = QHBoxLayout()
        self._record_payment_btn = QPushButton(self)
        self._record_payment_btn.clicked.connect(self._on_record_payment)
        self._reverse_payment_btn = QPushButton(self)
        self._reverse_payment_btn.clicked.connect(self._on_reverse_payment)
        payment_btns.addWidget(self._record_payment_btn)
        payment_btns.addWidget(self._reverse_payment_btn)
        payment_btns.addStretch(1)
        payments_layout.addLayout(payment_btns)
        layout.addWidget(self._payments_group)

        # -- chronological entries --------------------------------------------
        self._entries_group = QGroupBox(self)
        entries_layout = QVBoxLayout(self._entries_group)
        self._entries_table = QTableWidget(self)
        self._entries_table.setColumnCount(5)
        self._entries_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._entries_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._entries_table.horizontalHeader().setStretchLastSection(True)
        entries_layout.addWidget(self._entries_table)
        layout.addWidget(self._entries_group)

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
        self._title.setText(tr("cb.title"))
        self._date_label.setText(tr("cb.date") + ":")
        self._refresh_btn.setText(tr("cb.refresh"))
        self._position_group.setTitle(tr("cb.position_section"))
        self._opening_label.setText(tr("cb.opening") + ":")
        self._receipts_label.setText(tr("cb.receipts") + ":")
        self._payments_label.setText(tr("cb.payments") + ":")
        self._expected_label.setText(tr("cb.expected") + ":")
        self._physical_label.setText(tr("cb.physical") + ":")
        self._difference_label.setText(tr("cb.difference") + ":")
        self._shortage_label.setText(tr("cb.shortage") + ":")
        self._surplus_label.setText(tr("cb.surplus") + ":")
        self._settle_group.setTitle(tr("cb.settlements_section"))
        self._settle_table.setHorizontalHeaderLabels(
            [
                tr("cb.th_product"),
                tr("cb.th_meter"),
                tr("cb.th_cash"),
                tr("cb.th_bank"),
                tr("cb.th_paid_now"),
                tr("cb.th_receivable"),
                tr("cb.th_other"),
                tr("cb.th_settled"),
                tr("cb.th_identity"),
            ]
        )
        self._record_settle_btn.setText(tr("cb.record_settlements"))
        self._reverse_settle_btn.setText(tr("cb.reverse_settlements"))
        self._receipts_group.setTitle(tr("cb.receipts_section"))
        self._receipts_table.setHorizontalHeaderLabels(
            [
                tr("cb.th_time"),
                tr("cb.th_category"),
                tr("cb.th_amount"),
                tr("cb.th_customer"),
                tr("cb.th_source"),
                tr("cb.th_remarks"),
                tr("cb.th_status"),
            ]
        )
        self._record_receipt_btn.setText(tr("cb.record_receipt"))
        self._reverse_receipt_btn.setText(tr("cb.reverse_receipt"))
        self._payments_group.setTitle(tr("cb.payments_section"))
        self._payments_table.setHorizontalHeaderLabels(
            [
                tr("cb.th_time"),
                tr("cb.th_category"),
                tr("cb.th_amount"),
                tr("cb.th_payee"),
                tr("cb.th_source"),
                tr("cb.th_remarks"),
                tr("cb.th_status"),
            ]
        )
        self._record_payment_btn.setText(tr("cb.record_payment"))
        self._reverse_payment_btn.setText(tr("cb.reverse_payment"))
        self._entries_group.setTitle(tr("cb.entries_section"))
        self._entries_table.setHorizontalHeaderLabels(
            [
                tr("cb.th_time"),
                tr("cb.th_direction"),
                tr("cb.th_category"),
                tr("cb.th_amount"),
                tr("cb.th_remarks"),
            ]
        )
        self.refresh()

    # ------------------------------------------------------------------
    def _set_writes_enabled(self, enabled: bool) -> None:
        for btn in (
            self._record_settle_btn,
            self._reverse_settle_btn,
            self._record_receipt_btn,
            self._reverse_receipt_btn,
            self._record_payment_btn,
            self._reverse_payment_btn,
        ):
            btn.setEnabled(enabled)

    def refresh(self) -> None:
        """Reload every figure from the database."""
        business_date = self._business_date()
        if not business_date:
            self._date_value.setText(tr("dash.no_open_day"))
            for widget in (
                self._opening_value,
                self._receipts_value,
                self._payments_value,
                self._expected_value,
                self._physical_value,
                self._difference_value,
                self._shortage_value,
                self._surplus_value,
            ):
                widget.setText("—")
            self._settle_banner.setText(tr("dash.open_day_hint"))
            self._settle_table.setRowCount(0)
            self._receipts_table.setRowCount(0)
            self._payments_table.setRowCount(0)
            self._entries_table.setRowCount(0)
            self._set_writes_enabled(False)
            return
        self._set_writes_enabled(True)
        self._date_value.setText(business_date)
        try:
            position = self._cashbook.cash_position(business_date)
        except Exception as exc:  # noqa: BLE001 — show, don't crash
            h.show_error(self, exc)
            return
        self._opening_value.setText(
            h.format_pkr(h.dto_field(position, "opening_cash_paisa", default=None))
        )
        self._receipts_value.setText(
            h.format_pkr(h.dto_field(position, "receipts_paisa", default=None))
        )
        self._payments_value.setText(
            h.format_pkr(h.dto_field(position, "payments_paisa", default=None))
        )
        self._expected_value.setText(
            h.format_pkr(h.dto_field(position, "expected_paisa", default=None))
        )
        self._physical_value.setText(
            h.format_pkr(
                h.dto_field(position, "physical_cash_paisa", default=None)
            )
        )
        self._difference_value.setText(
            h.format_pkr(h.dto_field(position, "difference_paisa", default=None))
        )
        # Shortage and surplus are shown separately — never netted or written
        # off. A zero leg displays as "—".
        shortage = h.dto_field(position, "shortage_paisa", default=None)
        self._shortage_value.setText(
            h.format_pkr(shortage) if shortage else "—"
        )
        surplus = h.dto_field(position, "surplus_paisa", default=None)
        self._surplus_value.setText(
            h.format_pkr(surplus) if surplus else "—"
        )
        try:
            status = self._cashbook.settlement_status(business_date) or {}
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        per_product = status.get("per_product", []) or []
        if not status.get("recorded"):
            self._settle_banner.setText(tr("cb.not_recorded"))
        elif status.get("identity_ok"):
            self._settle_banner.setText(tr("cb.identity_banner_ok"))
        else:
            broken = [
                p for p in per_product if not p.get("identity_ok", True)
            ]
            self._settle_banner.setText(
                tr("cb.identity_banner_bad").format(
                    n=len(broken),
                    amount=h.format_pkr(
                        sum(
                            abs(int(p.get("difference_paisa", 0) or 0))
                            for p in broken
                        )
                    ),
                )
            )
        self._settle_table.setRowCount(len(per_product))
        for row, product in enumerate(per_product):
            values = [
                str(product.get("fuel_product_code", "—")),
                h.format_pkr(product.get("meter_revenue_paisa")),
                h.format_pkr(product.get("cash_paisa")),
                h.format_pkr(product.get("bank_paisa")),
                h.format_pkr(product.get("paid_now_paisa")),
                h.format_pkr(product.get("receivable_paisa")),
                h.format_pkr(product.get("other_paisa")),
                h.format_pkr(product.get("settled_paisa")),
                tr("cb.identity_ok")
                if product.get("identity_ok")
                else tr("cb.identity_break"),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col in (1, 2, 3, 4, 5, 6, 7):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._settle_table.setItem(row, col, item)
        self._settle_table.resizeColumnsToContents()
        self._refresh_receipts(business_date)
        self._refresh_payments(business_date)
        try:
            entries = self._cashbook.cashbook_entries(business_date) or []
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._entries_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            signed = h.dto_field(entry, "signed_paisa", default=None)
            values = [
                str(h.dto_field(entry, "occurred_at_utc", default="—")),
                tr("cb.direction_in")
                if signed is not None and signed >= 0
                else tr("cb.direction_out"),
                str(h.dto_field(entry, "category_en", "category", default="—")),
                h.format_pkr(signed),
                str(h.dto_field(entry, "remarks", default=None) or "—"),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col == 3:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._entries_table.setItem(row, col, item)
        self._entries_table.resizeColumnsToContents()

    # ------------------------------------------------------------------
    def _refresh_receipts(self, business_date: str) -> None:
        try:
            self._receipts_list = (
                self._cashbook.list_receipts(business_date) or []
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._receipts_table.setRowCount(len(self._receipts_list))
        for row, receipt in enumerate(self._receipts_list):
            source_table = h.dto_field(receipt, "source_table", default=None)
            values = [
                str(h.dto_field(receipt, "occurred_at_utc", default="—")),
                str(
                    h.dto_field(
                        receipt, "category_en", "category", default="—"
                    )
                ),
                h.format_pkr(
                    h.dto_field(receipt, "effective_paisa", "amount_paisa",
                                default=None)
                ),
                str(h.dto_field(receipt, "customer_name", default=None) or "—"),
                tr("cb.source_auto") if source_table else tr("cb.source_manual"),
                str(h.dto_field(receipt, "remarks", default=None) or "—"),
                _status_text(h.dto_field(receipt, "status", default=None)),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col == 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._receipts_table.setItem(row, col, item)
        self._receipts_table.resizeColumnsToContents()

    def _refresh_payments(self, business_date: str) -> None:
        try:
            self._payments_list = (
                self._cashbook.list_payments(business_date) or []
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._payments_table.setRowCount(len(self._payments_list))
        for row, payment in enumerate(self._payments_list):
            source_table = h.dto_field(payment, "source_table", default=None)
            values = [
                str(h.dto_field(payment, "occurred_at_utc", default="—")),
                str(
                    h.dto_field(
                        payment, "category_en", "category", default="—"
                    )
                ),
                h.format_pkr(
                    h.dto_field(payment, "effective_paisa", "amount_paisa",
                                default=None)
                ),
                str(h.dto_field(payment, "payee", default=None) or "—"),
                tr("cb.source_auto") if source_table else tr("cb.source_manual"),
                str(h.dto_field(payment, "remarks", default=None) or "—"),
                _status_text(h.dto_field(payment, "status", default=None)),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col == 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._payments_table.setItem(row, col, item)
        self._payments_table.resizeColumnsToContents()

    # ------------------------------------------------------------------
    def _on_record_settlements(self) -> None:
        _SettleDialog(self, self._cashbook, self._business_date()).exec()
        self.refresh()

    def _on_reverse_settlements(self) -> None:
        business_date = self._business_date()
        confirm = _ReasonDialog(
            self,
            tr("cb.reverse_settlements"),
            tr("cb.confirm_reverse_settlements").format(date=business_date)
            + "\n"
            + tr("cb.reverse_reason"),
        )
        if confirm.exec() != QDialog.Accepted:
            return
        try:
            self._cashbook.reverse_settlements(business_date, reason=confirm.reason)
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        h.show_info(self, tr("cb.settlements_reversed_ok"))
        self.refresh()

    def _on_record_receipt(self) -> None:
        _CashMoveDialog(self, self._cashbook, self._business_date(), "receipt").exec()
        self.refresh()

    def _on_record_payment(self) -> None:
        _CashMoveDialog(self, self._cashbook, self._business_date(), "payment").exec()
        self.refresh()

    def _on_reverse_receipt(self) -> None:
        row = self._receipts_table.currentRow()
        if not (0 <= row < len(self._receipts_list)):
            h.show_error(
                self, errors.BusinessRuleError(tr("cb.select_receipt_first"))
            )
            return
        receipt = self._receipts_list[row]
        if str(h.dto_field(receipt, "status", default="")) != "posted":
            h.show_error(
                self, errors.BusinessRuleError(tr("cb.status_reversed"))
            )
            return
        amount = h.dto_field(receipt, "effective_paisa", "amount_paisa",
                             default=None)
        confirm = _ReasonDialog(
            self,
            tr("cb.reverse_receipt"),
            tr("cb.confirm_reverse_receipt").format(
                amount=h.format_pkr(amount)
            )
            + "\n"
            + tr("cb.reverse_reason"),
        )
        if confirm.exec() != QDialog.Accepted:
            return
        try:
            self._cashbook.reverse_cash_receipt(
                h.dto_field(receipt, "uuid", default=None),
                reason=confirm.reason,
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        h.show_info(self, tr("cb.reversed_ok"))
        self.refresh()

    def _on_reverse_payment(self) -> None:
        row = self._payments_table.currentRow()
        if not (0 <= row < len(self._payments_list)):
            h.show_error(
                self, errors.BusinessRuleError(tr("cb.select_payment_first"))
            )
            return
        payment = self._payments_list[row]
        if str(h.dto_field(payment, "status", default="")) != "posted":
            h.show_error(
                self, errors.BusinessRuleError(tr("cb.status_reversed"))
            )
            return
        amount = h.dto_field(payment, "effective_paisa", "amount_paisa",
                             default=None)
        confirm = _ReasonDialog(
            self,
            tr("cb.reverse_payment"),
            tr("cb.confirm_reverse_payment").format(
                amount=h.format_pkr(amount)
            )
            + "\n"
            + tr("cb.reverse_reason"),
        )
        if confirm.exec() != QDialog.Accepted:
            return
        try:
            self._cashbook.reverse_cash_payment(
                h.dto_field(payment, "uuid", default=None),
                reason=confirm.reason,
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        h.show_info(self, tr("cb.reversed_ok"))
        self.refresh()
