"""Expenses page: category manager + expense recording/reversal.

All figures come from ExpenseService; the UI parses inputs and formats
service-computed values — it does no accounting math of its own.
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

from app.localization import get_language, tr
from app.services import errors
from app.ui import _helpers as h

_METHOD_KEYS = {
    "cash": "exp.method_cash",
    "bank": "exp.method_bank",
    "other": "exp.method_other",
}


def _method_text(method) -> str:
    key = _METHOD_KEYS.get(str(method or "").lower())
    return tr(key) if key else str(method or "—")


def _category_name(category) -> str:
    if get_language() == "ur":
        name_ur = h.dto_field(category, "name_ur", default=None)
        if name_ur:
            return str(name_ur)
    return str(h.dto_field(category, "name_en", "category_en", default="—"))


def _status_text(status) -> str:
    status = str(status or "").lower()
    if status == "posted":
        return tr("exp.status_posted")
    if status == "reversed":
        return tr("exp.status_reversed")
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


class _RecordExpenseDialog(QDialog):
    """Record an expense. The service call happens on OK; the dialog stays
    open (preserving input) when validation fails."""

    def __init__(self, parent, expense_service, business_date) -> None:
        super().__init__(parent)
        self._expenses = expense_service
        self._business_date = business_date
        self.setWindowTitle(tr("exp.dialog_title"))
        self.setMinimumWidth(420)

        form = QFormLayout(self)
        self._category_combo = QComboBox(self)
        try:
            categories = expense_service.list_categories(active_only=True) or []
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            categories = []
        for category in categories:
            self._category_combo.addItem(_category_name(category), category)
        form.addRow(QLabel(tr("exp.category"), self), self._category_combo)

        self._amount_edit = QLineEdit(self)
        self._amount_edit.setAlignment(Qt.AlignRight)
        form.addRow(QLabel(tr("exp.amount_pkr"), self), self._amount_edit)

        self._method_combo = QComboBox(self)
        for code in ("cash", "bank", "other"):
            self._method_combo.addItem(tr(_METHOD_KEYS[code]), code)
        form.addRow(QLabel(tr("exp.method"), self), self._method_combo)
        method_hint = QLabel(tr("exp.method_hint"), self)
        method_hint.setWordWrap(True)
        method_hint.setStyleSheet("color: gray;")
        form.addRow(method_hint)

        self._payee_edit = QLineEdit(self)
        form.addRow(QLabel(tr("exp.payee_optional"), self), self._payee_edit)

        self._reference_edit = QLineEdit(self)
        form.addRow(
            QLabel(tr("exp.reference_optional"), self), self._reference_edit
        )

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
            h.show_error(self, ValueError(tr("exp.category")))
            return
        category_id = h.dto_field(category, "id", default=None)
        try:
            amount = h.parse_pkr_to_paisa(self._amount_edit.text())
        except ValueError as exc:
            h.show_error(self, exc)
            return
        if amount <= 0:
            h.show_error(self, ValueError(tr("exp.amount_pkr")))
            return
        try:
            self._expenses.record_expense(
                business_date=self._business_date,
                category=category_id,
                amount_paisa=amount,
                method=self._method_combo.currentData(),
                payee=self._payee_edit.text().strip() or None,
                reference=self._reference_edit.text().strip() or None,
            )
        except Exception as exc:  # noqa: BLE001 — keep dialog open
            h.show_error(self, exc)
            return
        h.show_info(self, tr("exp.recorded_ok"))
        self.accept()


class _CategoryDialog(QDialog):
    """Add an expense category (English name mandatory, unique)."""

    def __init__(self, parent, expense_service) -> None:
        super().__init__(parent)
        self._expenses = expense_service
        self.setWindowTitle(tr("exp.category_dialog_title"))
        self.setMinimumWidth(400)

        form = QFormLayout(self)
        self._name_en_edit = QLineEdit(self)
        form.addRow(QLabel(tr("exp.name_en"), self), self._name_en_edit)
        self._name_ur_edit = QLineEdit(self)
        form.addRow(QLabel(tr("exp.name_ur"), self), self._name_ur_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    # ------------------------------------------------------------------
    def _on_accept(self) -> None:
        name_en = self._name_en_edit.text().strip()
        if not name_en:
            h.show_error(self, ValueError(tr("exp.name_en")))
            return
        try:
            self._expenses.add_category(
                name_en,
                self._name_ur_edit.text().strip() or None,
            )
        except Exception as exc:  # noqa: BLE001 — keep dialog open
            h.show_error(self, exc)
            return
        h.show_info(self, tr("exp.recorded_ok"))
        self.accept()


class ExpensePage(QWidget):
    """Expense categories and the open day's expenses."""

    def __init__(self, expense_service, day_service, parent=None) -> None:
        super().__init__(parent)
        self._expenses = expense_service
        self._days = day_service
        self._categories_list: list = []
        self._expenses_list: list = []

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

        # -- categories -----------------------------------------------------
        self._cat_group = QGroupBox(self)
        cat_layout = QVBoxLayout(self._cat_group)
        self._cat_table = QTableWidget(self)
        self._cat_table.setColumnCount(3)
        self._cat_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._cat_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._cat_table.horizontalHeader().setStretchLastSection(True)
        cat_layout.addWidget(self._cat_table)
        cat_btns = QHBoxLayout()
        self._add_category_btn = QPushButton(self)
        self._add_category_btn.clicked.connect(self._on_add_category)
        self._toggle_category_btn = QPushButton(self)
        self._toggle_category_btn.clicked.connect(self._on_toggle_category)
        cat_btns.addWidget(self._add_category_btn)
        cat_btns.addWidget(self._toggle_category_btn)
        cat_btns.addStretch(1)
        cat_layout.addLayout(cat_btns)
        layout.addWidget(self._cat_group)

        # -- expenses -------------------------------------------------------
        self._exp_group = QGroupBox(self)
        exp_layout = QVBoxLayout(self._exp_group)
        self._exp_table = QTableWidget(self)
        self._exp_table.setColumnCount(6)
        self._exp_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._exp_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._exp_table.horizontalHeader().setStretchLastSection(True)
        exp_layout.addWidget(self._exp_table)
        self._totals_label = QLabel(self)
        self._totals_label.setStyleSheet("font-weight: bold;")
        exp_layout.addWidget(self._totals_label)
        exp_btns = QHBoxLayout()
        self._record_btn = QPushButton(self)
        self._record_btn.clicked.connect(self._on_record_expense)
        self._reverse_btn = QPushButton(self)
        self._reverse_btn.clicked.connect(self._on_reverse_expense)
        exp_btns.addWidget(self._record_btn)
        exp_btns.addWidget(self._reverse_btn)
        exp_btns.addStretch(1)
        exp_layout.addLayout(exp_btns)
        layout.addWidget(self._exp_group, 1)

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
        self._title.setText(tr("exp.title"))
        self._date_label.setText(tr("exp.date") + ":")
        self._refresh_btn.setText(tr("exp.refresh"))
        self._cat_group.setTitle(tr("exp.categories_section"))
        self._cat_table.setHorizontalHeaderLabels(
            [
                tr("exp.th_category"),
                tr("exp.th_active"),
                tr("exp.th_status"),
            ]
        )
        self._add_category_btn.setText(tr("exp.add_category"))
        # The toggle label depends on selection; refresh() sets it.
        self._exp_group.setTitle(tr("exp.expenses_section"))
        self._exp_table.setHorizontalHeaderLabels(
            [
                tr("exp.th_category"),
                tr("exp.th_amount"),
                tr("exp.th_method"),
                tr("exp.th_payee"),
                tr("exp.th_reference"),
                tr("exp.th_status"),
            ]
        )
        self._record_btn.setText(tr("exp.record"))
        self._reverse_btn.setText(tr("exp.reverse"))
        self.refresh()

    # ------------------------------------------------------------------
    def _set_writes_enabled(self, enabled: bool) -> None:
        for btn in (
            self._add_category_btn,
            self._toggle_category_btn,
            self._record_btn,
            self._reverse_btn,
        ):
            btn.setEnabled(enabled)

    def refresh(self) -> None:
        """Reload categories and the day's expenses."""
        business_date = self._business_date()
        if not business_date:
            self._date_value.setText(tr("dash.no_open_day"))
            self._cat_table.setRowCount(0)
            self._exp_table.setRowCount(0)
            self._totals_label.setText(tr("dash.open_day_hint"))
            self._set_writes_enabled(False)
            return
        self._set_writes_enabled(True)
        self._date_value.setText(business_date)
        try:
            self._categories_list = (
                self._expenses.list_categories(active_only=False) or []
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._cat_table.setRowCount(len(self._categories_list))
        for row, category in enumerate(self._categories_list):
            is_active = h.dto_field(category, "is_active", default=True)
            values = [
                _category_name(category),
                tr("exp.active_yes") if is_active else tr("exp.active_no"),
                tr("exp.status_posted") if is_active else tr("exp.status_reversed"),
            ]
            for col, text in enumerate(values):
                self._cat_table.setItem(row, col, QTableWidgetItem(text))
        self._cat_table.resizeColumnsToContents()
        self._update_toggle_label()
        try:
            self._expenses_list = (
                self._expenses.list_expenses(business_date=business_date) or []
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self._exp_table.setRowCount(len(self._expenses_list))
        for row, expense in enumerate(self._expenses_list):
            values = [
                str(
                    h.dto_field(
                        expense, "category_en", "category", default="—"
                    )
                ),
                h.format_pkr(
                    h.dto_field(
                        expense, "effective_paisa", "amount_paisa", default=None
                    )
                ),
                _method_text(h.dto_field(expense, "method", default=None)),
                str(h.dto_field(expense, "payee", default=None) or "—"),
                str(h.dto_field(expense, "reference", default=None) or "—"),
                _status_text(h.dto_field(expense, "status", default=None)),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col == 1:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._exp_table.setItem(row, col, item)
        self._exp_table.resizeColumnsToContents()
        # The totals come from the service — never summed here.
        try:
            totals = self._expenses.expense_totals(business_date) or {}
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        total = totals.get("total_paisa", 0)
        self._totals_label.setText(
            f"{tr('exp.total_label')}: {h.format_pkr(total)}"
        )

    def _update_toggle_label(self) -> None:
        row = self._cat_table.currentRow()
        if 0 <= row < len(self._categories_list):
            is_active = h.dto_field(
                self._categories_list[row], "is_active", default=True
            )
            self._toggle_category_btn.setText(
                tr("exp.deactivate") if is_active else tr("exp.activate")
            )
        else:
            self._toggle_category_btn.setText(tr("exp.deactivate"))

    # ------------------------------------------------------------------
    def _on_add_category(self) -> None:
        _CategoryDialog(self, self._expenses).exec()
        self.refresh()

    def _on_toggle_category(self) -> None:
        row = self._cat_table.currentRow()
        if not (0 <= row < len(self._categories_list)):
            h.show_error(
                self, errors.BusinessRuleError(tr("exp.select_category_first"))
            )
            return
        category = self._categories_list[row]
        category_id = h.dto_field(category, "id", default=None)
        is_active = h.dto_field(category, "is_active", default=True)
        try:
            self._expenses.set_category_active(category_id, not is_active)
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        self.refresh()

    def _on_record_expense(self) -> None:
        _RecordExpenseDialog(
            self, self._expenses, self._business_date()
        ).exec()
        self.refresh()

    def _on_reverse_expense(self) -> None:
        row = self._exp_table.currentRow()
        if not (0 <= row < len(self._expenses_list)):
            h.show_error(
                self, errors.BusinessRuleError(tr("exp.select_expense_first"))
            )
            return
        expense = self._expenses_list[row]
        if str(h.dto_field(expense, "status", default="")) != "posted":
            h.show_error(
                self, errors.BusinessRuleError(tr("exp.status_reversed"))
            )
            return
        amount = h.dto_field(expense, "effective_paisa", "amount_paisa",
                             default=None)
        confirm = _ReasonDialog(
            self,
            tr("exp.reverse_title"),
            tr("exp.confirm_reverse").format(amount=h.format_pkr(amount))
            + "\n"
            + tr("exp.reverse_reason"),
        )
        if confirm.exec() != QDialog.Accepted:
            return
        try:
            self._expenses.reverse_expense(
                h.dto_field(expense, "uuid", default=None),
                reason=confirm.reason,
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        h.show_info(self, tr("exp.reversed_ok"))
        self.refresh()
