"""Daily Sheet page: the client's paper workflow in one scrolling sheet.

Section order follows the paper register: CASH → SALES → STOCK → NOZZLES.
Every figure is read from services; entry dialogs call service commands.
The UI performs no accounting math: every total shown is a service-computed
value, except the "total cash of sale" row which sums the service's
per-product revenue figures for display only.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.database.connection import DatabaseManager
from app.localization import get_language, localize_error, tr
from app.repositories.business_day_repository import BusinessDayRepository
from app.services import errors
from app.services.business_day_service import BusinessDayService
from app.services.cashbook_service import CashbookService
from app.services.expense_service import ExpenseService
from app.services.inventory_service import InventoryService
from app.services.nozzle_service import NozzleService
from app.services.price_service import PriceService
from app.services.sales_service import SalesService
from app.ui import _helpers as h

#: Section tints: (background, border, title color).
_SECTION_STYLE = {
    "cash": ("#eaf7ec", "#43a047", "#1b5e20"),
    "sales": ("#e8f1fb", "#1e88e5", "#0d47a1"),
    "stock": ("#fdf6e3", "#f9a825", "#7c5a00"),
    "nozzles": ("#f3eafa", "#8e24aa", "#4a148c"),
}

#: Fuel display order on the sheet (client lists diesel first).
_FUEL_ORDER = {"DIESEL": 0, "PETROL": 1}
_FUEL_LABEL = {"DIESEL": "sheet.diesel", "PETROL": "sheet.petrol"}


class DailySheetPage(QWidget):
    """One-page daily workflow: cash, sales, stock, nozzles."""

    def __init__(
        self,
        db_manager: DatabaseManager,
        day_service: BusinessDayService,
        cashbook_service: CashbookService,
        expense_service: ExpenseService,
        sales_service: SalesService,
        inventory_service: InventoryService,
        nozzle_service: NozzleService,
        price_service: PriceService,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._db = db_manager
        self._days = day_service
        self._cashbook = cashbook_service
        self._expenses = expense_service
        self._sales = sales_service
        self._inventory = inventory_service
        self._nozzles = nozzle_service
        self._prices = price_service

        # Dynamic widget registries, rebuilt on every refresh().
        self._dip_edits: dict[int, QLineEdit] = {}
        self._nozzle_edits: dict[int, QLineEdit] = {}
        self._nozzle_previews: dict[int, QLabel] = {}
        self._nozzle_status: dict[int, QLabel] = {}
        self._nozzle_rows: list = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        # -- header ------------------------------------------------------
        header = QHBoxLayout()
        self._station_label = QLabel(self)
        self._station_label.setStyleSheet(
            "font-size: 20px; font-weight: bold; color: #12365f;"
        )
        header.addWidget(self._station_label)
        header.addStretch(1)
        self._date_label = QLabel(self)
        self._date_label.setStyleSheet("font-size: 14px; color: #444;")
        header.addWidget(self._date_label)
        self._status_label = QLabel(self)
        self._status_label.setStyleSheet("font-size: 14px; color: #444;")
        header.addWidget(self._status_label)
        self._refresh_btn = QPushButton(self)
        self._refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self._refresh_btn)
        layout.addLayout(header)

        self._no_day_label = QLabel(self)
        self._no_day_label.setWordWrap(True)
        self._no_day_label.setStyleSheet("color: #b71c1c; font-size: 14px;")
        self._no_day_label.hide()
        layout.addWidget(self._no_day_label)

        # -- sections ----------------------------------------------------
        self._cash_group = self._make_section("cash", layout)
        self._cash_grid = QGridLayout()
        self._cash_grid.setColumnStretch(0, 1)
        self._cash_grid.setColumnStretch(1, 0)
        cash_inner = QVBoxLayout(self._cash_group)
        cash_inner.addLayout(self._cash_grid)
        cash_btns = QHBoxLayout()
        self._add_receipt_btn = QPushButton(self)
        self._add_receipt_btn.clicked.connect(self._on_add_receipt)
        self._add_expense_btn = QPushButton(self)
        self._add_expense_btn.clicked.connect(self._on_add_expense)
        cash_btns.addWidget(self._add_receipt_btn)
        cash_btns.addWidget(self._add_expense_btn)
        cash_btns.addStretch(1)
        cash_inner.addLayout(cash_btns)

        self._sales_group = self._make_section("sales", layout)
        self._sales_grid = QGridLayout()
        self._sales_grid.setColumnStretch(0, 1)
        sales_inner = QVBoxLayout(self._sales_group)
        sales_inner.addLayout(self._sales_grid)

        self._stock_group = self._make_section("stock", layout)
        self._stock_body = QVBoxLayout(self._stock_group)

        self._nozzle_group = self._make_section("nozzles", layout)
        nozzle_inner = QVBoxLayout(self._nozzle_group)
        self._nozzle_grid = QGridLayout()
        self._nozzle_grid.setColumnStretch(0, 0)
        nozzle_inner.addLayout(self._nozzle_grid)
        nozzle_btns = QHBoxLayout()
        self._save_readings_btn = QPushButton(self)
        self._save_readings_btn.clicked.connect(self._on_save_readings)
        nozzle_btns.addWidget(self._save_readings_btn)
        nozzle_btns.addStretch(1)
        nozzle_inner.addLayout(nozzle_btns)

        layout.addStretch(1)

        self.retranslate()
        self.refresh()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _make_section(self, key: str, layout: QVBoxLayout) -> QGroupBox:
        bg, border, _ = _SECTION_STYLE[key]
        group = QGroupBox(self)
        group.setProperty("sheet_section", key)
        group.setStyleSheet(
            f"QGroupBox {{ font-size: 16px; font-weight: bold; "
            f"border: 2px solid {border}; border-radius: 8px; "
            f"background: {bg}; margin-top: 12px; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin; "
            f"left: 12px; padding: 0 6px; }}"
        )
        layout.addWidget(group)
        return group

    def _section_title_color(self, key: str) -> str:
        return _SECTION_STYLE[key][2]

    def _station_name(self) -> str:
        try:
            conn = self._db.connect()
            try:
                name = BusinessDayRepository.get_setting(
                    conn, "station_name"
                ) or BusinessDayRepository.get_setting(conn, "operator_name")
            finally:
                conn.close()
            if name and str(name).strip():
                return str(name).strip()
        except Exception:  # noqa: BLE001 — fall back to generic title
            pass
        return tr("sheet.default_station")

    def _business_date(self) -> str | None:
        try:
            day = self._days.get_open_day()
        except Exception:  # noqa: BLE001
            return None
        return day.business_date if day is not None else None

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())

    def _value_label(self, bold: bool = False) -> QLabel:
        label = QLabel(self)
        label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        if bold:
            label.setStyleSheet("font-weight: bold; font-size: 14px;")
        return label

    # ------------------------------------------------------------------
    # translation
    # ------------------------------------------------------------------
    def retranslate(self) -> None:
        """Re-apply all static texts (called on language change)."""
        self._station_label.setText(self._station_name())
        self._refresh_btn.setText(tr("sheet.refresh"))
        self._no_day_label.setText(tr("sheet.no_open_day"))
        self._cash_group.setTitle(tr("sheet.cash"))
        self._sales_group.setTitle(tr("sheet.sales"))
        self._stock_group.setTitle(tr("sheet.stock"))
        self._nozzle_group.setTitle(tr("sheet.nozzles"))
        for key in ("cash", "sales", "stock", "nozzles"):
            group = {
                "cash": self._cash_group,
                "sales": self._sales_group,
                "stock": self._stock_group,
                "nozzles": self._nozzle_group,
            }[key]
            group.setStyleSheet(
                f"QGroupBox {{ font-size: 16px; font-weight: bold; "
                f"border: 2px solid {_SECTION_STYLE[key][1]}; "
                f"border-radius: 8px; background: {_SECTION_STYLE[key][0]}; "
                f"margin-top: 12px; color: {_SECTION_STYLE[key][2]}; }}"
                f"QGroupBox::title {{ subcontrol-origin: margin; "
                f"left: 12px; padding: 0 6px; }}"
            )
        self._add_receipt_btn.setText(tr("sheet.add_receipt"))
        self._add_expense_btn.setText(tr("sheet.add_expense"))
        self._save_readings_btn.setText(tr("sheet.save_readings"))
        self.refresh()

    # ------------------------------------------------------------------
    # refresh
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Reload every section from the services."""
        business_date = self._business_date()
        if business_date is None:
            self._no_day_label.show()
            return
        self._no_day_label.hide()
        try:
            day = self._days.get_open_day()
            self._date_label.setText(f"{tr('sheet.date')}: {business_date}")
            self._status_label.setText(
                f"{tr('sheet.day_status')}: {day.status if day else '—'}"
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        try:
            self._refresh_cash(business_date)
            self._refresh_sales(business_date)
            self._refresh_stock(business_date)
            self._refresh_nozzles(business_date)
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)

    # ------------------------------------------------------------------
    # CASH section
    # ------------------------------------------------------------------
    def _refresh_cash(self, business_date: str) -> None:
        self._clear_layout(self._cash_grid)
        pos = self._cashbook.cash_position(business_date)
        other = self._cashbook.other_income_total(business_date)
        expenses = self._expenses.expense_totals(business_date)

        row = 0

        def add_row(label_key: str, paisa: int, bold: bool = False):
            nonlocal row
            label = QLabel(tr(label_key), self)
            if bold:
                label.setStyleSheet("font-weight: bold; font-size: 14px;")
            value = self._value_label(bold=bold)
            value.setText(h.format_pkr(paisa))
            self._cash_grid.addWidget(label, row, 0)
            self._cash_grid.addWidget(value, row, 1)
            row += 1

        add_row("sheet.prev_day_cash", pos.opening_cash_paisa)
        add_row("sheet.today_receipts", pos.receipts_paisa)
        add_row("sheet.other_income", other["total_paisa"])
        # Debt-recovery subtotal + who paid.
        rec_label = QLabel("↳ " + tr("sheet.debt_recovered"), self)
        rec_label.setStyleSheet("color: #2e7d32;")
        rec_value = self._value_label()
        rec_value.setText(h.format_pkr(other["credit_recovery_paisa"]))
        rec_value.setStyleSheet("color: #2e7d32;")
        self._cash_grid.addWidget(rec_label, row, 0)
        self._cash_grid.addWidget(rec_value, row, 1)
        row += 1
        for rec in other["recoveries"]:
            who = rec.get("customer_name") or tr("sheet.unknown_customer")
            who_label = QLabel(f"    • {who}", self)
            who_label.setStyleSheet("color: #555;")
            who_value = self._value_label()
            who_value.setText(h.format_pkr(rec["amount_paisa"]))
            who_value.setStyleSheet("color: #555;")
            self._cash_grid.addWidget(who_label, row, 0)
            self._cash_grid.addWidget(who_value, row, 1)
            row += 1
        add_row("sheet.today_expenses", expenses["total_paisa"])
        for cat in expenses.get("by_category", []):
            cat_label = QLabel(f"    • {cat['category_en']}", self)
            cat_label.setStyleSheet("color: #555;")
            cat_value = self._value_label()
            cat_value.setText(h.format_pkr(cat["total_paisa"]))
            cat_value.setStyleSheet("color: #555;")
            self._cash_grid.addWidget(cat_label, row, 0)
            self._cash_grid.addWidget(cat_value, row, 1)
            row += 1
        add_row("sheet.cash_total", pos.expected_paisa, bold=True)

    def _on_add_receipt(self) -> None:
        business_date = self._business_date()
        if business_date is None:
            return
        try:
            categories = self._cashbook.list_categories("receipt")
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("sheet.receipt_title"))
        form = QFormLayout(dialog)
        cat_combo = QComboBox(dialog)
        for cat in categories:
            label = (
                cat.name_ur
                if get_language() == "ur" and cat.name_ur
                else cat.name_en
            )
            cat_combo.addItem(label, cat.name)
        amount_edit = h.make_integer_edit(dialog)
        amount_edit.setPlaceholderText("0")
        remarks_edit = QLineEdit(dialog)
        form.addRow(QLabel(tr("sheet.category"), dialog), cat_combo)
        form.addRow(QLabel(tr("sheet.amount_pkr"), dialog), amount_edit)
        form.addRow(QLabel(tr("sheet.remarks"), dialog), remarks_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dialog
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            paisa = h.parse_pkr_to_paisa(amount_edit.text())
            self._cashbook.record_receipt(
                business_date=business_date,
                category=cat_combo.currentData(),
                amount_paisa=paisa,
                remarks=remarks_edit.text().strip() or None,
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        h.show_info(self, tr("sheet.receipt_saved"))
        self.refresh()

    def _on_add_expense(self) -> None:
        business_date = self._business_date()
        if business_date is None:
            return
        try:
            categories = self._expenses.list_categories()
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("sheet.expense_title"))
        form = QFormLayout(dialog)
        cat_combo = QComboBox(dialog)
        for cat in categories:
            label = (
                cat.name_ur
                if get_language() == "ur" and cat.name_ur
                else cat.name_en
            )
            cat_combo.addItem(label, cat.id)
        amount_edit = h.make_integer_edit(dialog)
        amount_edit.setPlaceholderText("0")
        payee_edit = QLineEdit(dialog)
        form.addRow(QLabel(tr("sheet.category"), dialog), cat_combo)
        form.addRow(QLabel(tr("sheet.amount_pkr"), dialog), amount_edit)
        form.addRow(QLabel(tr("sheet.payee"), dialog), payee_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dialog
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            paisa = h.parse_pkr_to_paisa(amount_edit.text())
            self._expenses.record_expense(
                business_date=business_date,
                category=cat_combo.currentData(),
                amount_paisa=paisa,
                method="cash",
                payee=payee_edit.text().strip() or None,
            )
        except Exception as exc:  # noqa: BLE001
            h.show_error(self, exc)
            return
        h.show_info(self, tr("sheet.expense_saved"))
        self.refresh()

    # ------------------------------------------------------------------
    # SALES section (read-only)
    # ------------------------------------------------------------------
    def _refresh_sales(self, business_date: str) -> None:
        self._clear_layout(self._sales_grid)
        sales = self._sales.day_sales(business_date)

        headers = ["", tr("sheet.litres"), tr("sheet.rate"), tr("sheet.amount")]
        for col, text in enumerate(headers):
            head = QLabel(text, self)
            head.setStyleSheet("font-weight: bold; color: #0d47a1;")
            if col:
                head.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._sales_grid.addWidget(head, 0, col)

        total_paisa = 0
        row = 1
        for code in ("PETROL", "DIESEL"):
            litres_ml = sales.liters_by_product_ml.get(code, 0)
            revenue = sales.revenue_by_product_paisa.get(code, 0)
            total_paisa += revenue
            try:
                price = self._prices.price_effective_at_day_end(code, business_date)
                rate = (
                    h.format_unit_price(price.unit_price_10k)
                    if price is not None
                    else tr("common.not_entered")
                )
            except Exception:  # noqa: BLE001 — price display is best-effort
                rate = tr("common.not_entered")
            name = QLabel(tr(_FUEL_LABEL[code]), self)
            litres = self._value_label()
            litres.setText(h.format_litres(litres_ml))
            rate_label = self._value_label()
            rate_label.setText(rate)
            amount = self._value_label()
            amount.setText(h.format_pkr(revenue))
            self._sales_grid.addWidget(name, row, 0)
            self._sales_grid.addWidget(litres, row, 1)
            self._sales_grid.addWidget(rate_label, row, 2)
            self._sales_grid.addWidget(amount, row, 3)
            row += 1

        total_label = QLabel(tr("sheet.total_cash_of_sale"), self)
        total_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        total_value = self._value_label(bold=True)
        # Display-only sum of the service-computed per-product revenues.
        total_value.setText(h.format_pkr(total_paisa))
        self._sales_grid.addWidget(total_label, row, 0)
        self._sales_grid.addWidget(total_value, row, 3)

    # ------------------------------------------------------------------
    # STOCK section
    # ------------------------------------------------------------------
    def _refresh_stock(self, business_date: str) -> None:
        self._clear_layout(self._stock_body)
        self._dip_edits = {}
        summary = self._inventory.fuel_stock_summary(business_date)
        tanks = self._inventory.list_tanks()
        readings = {
            r.tank_id: r for r in self._inventory.tank_readings(business_date)
        }
        tanks_by_fuel: dict[str, list] = {}
        for tank in tanks:
            if not tank.is_active:
                continue
            tanks_by_fuel.setdefault(tank.fuel_product_code, []).append(tank)

        summary.sort(key=lambda s: _FUEL_ORDER.get(s["fuel_product_code"], 99))
        for fuel in summary:
            code = fuel["fuel_product_code"]
            block = QWidget(self)
            grid = QGridLayout(block)
            grid.setColumnStretch(0, 1)
            header = QLabel(tr(_FUEL_LABEL.get(code, "sheet.fuel")), self)
            header.setStyleSheet(
                "font-weight: bold; font-size: 15px; color: #7c5a00;"
            )
            grid.addWidget(header, 0, 0, 1, 2)

            rows = [
                ("sheet.total_stock", fuel["opening_ml"]),
                ("sheet.sold_today", fuel["sold_ml"]),
                ("sheet.new_stock", fuel["received_ml"]),
                ("sheet.total_incl_new", fuel["theoretical_ml"]),
            ]
            r = 1
            for key, ml in rows:
                grid.addWidget(QLabel(tr(key), self), r, 0)
                value = self._value_label()
                value.setText(h.format_litres(ml))
                grid.addWidget(value, r, 1)
                r += 1

            # Dip entry per tank (editable only where no reading exists yet).
            for tank in tanks_by_fuel.get(code, []):
                dip_label = QLabel(
                    f"{tr('sheet.dip')} — {tr('sheet.tank')} {tank.code}", self
                )
                grid.addWidget(dip_label, r, 0)
                existing = readings.get(tank.id)
                if existing is not None:
                    value = self._value_label()
                    value.setText(h.format_litres(existing.quantity_ml))
                    grid.addWidget(value, r, 1)
                else:
                    edit = h.make_integer_edit(self)
                    edit.setPlaceholderText(tr("sheet.dip_placeholder"))
                    edit.setAlignment(Qt.AlignRight)
                    self._dip_edits[tank.id] = edit
                    grid.addWidget(edit, r, 1)
                r += 1

            grid.addWidget(QLabel(tr("sheet.stock_as_per_dip"), self), r, 0)
            physical = self._value_label()
            physical.setText(h.format_litres(fuel["physical_ml"]))
            grid.addWidget(physical, r, 1)
            r += 1

            grid.addWidget(QLabel(tr("sheet.difference"), self), r, 0)
            variance = self._value_label(bold=True)
            variance.setText(h.format_litres(fuel["variance_ml"]))
            if fuel["variance_ml"] is not None and fuel["variance_ml"] < 0:
                variance.setStyleSheet(
                    "font-weight: bold; font-size: 14px; color: #c62828;"
                )
            elif fuel["variance_ml"] is not None and fuel["variance_ml"] > 0:
                variance.setStyleSheet(
                    "font-weight: bold; font-size: 14px; color: #2e7d32;"
                )
            grid.addWidget(variance, r, 1)

            self._stock_body.addWidget(block)

        if self._dip_edits:
            save_btn = QPushButton(tr("sheet.save_dips"), self)
            save_btn.clicked.connect(self._on_save_dips)
            self._stock_body.addWidget(save_btn)

    def _on_save_dips(self) -> None:
        business_date = self._business_date()
        if business_date is None:
            return
        saved = 0
        for tank_id, edit in self._dip_edits.items():
            text = edit.text().strip()
            if not text:
                continue
            try:
                litres = h.parse_litres(text)
                self._inventory.record_physical_count(
                    tank_id, business_date, litres
                )
                saved += 1
            except Exception as exc:  # noqa: BLE001
                h.show_error(self, exc)
                return
        self.refresh()
        if saved:
            h.show_info(self, tr("sheet.dips_saved"))

    # ------------------------------------------------------------------
    # NOZZLES section
    # ------------------------------------------------------------------
    def _refresh_nozzles(self, business_date: str) -> None:
        self._clear_layout(self._nozzle_grid)
        self._nozzle_edits = {}
        self._nozzle_previews = {}
        self._nozzle_status = {}
        self._nozzle_rows = self._nozzles.day_readings(business_date)

        headers = [
            tr("sheet.nozzle"),
            tr("sheet.fuel"),
            tr("sheet.prev_reading"),
            tr("sheet.today_reading"),
            tr("sheet.sale_l"),
            tr("sheet.status"),
        ]
        for col, text in enumerate(headers):
            head = QLabel(text, self)
            head.setStyleSheet("font-weight: bold; color: #4a148c;")
            self._nozzle_grid.addWidget(head, 0, col)

        for idx, reading in enumerate(self._nozzle_rows, start=1):
            code_label = QLabel(reading.nozzle_code, self)
            code_label.setStyleSheet("font-weight: bold;")
            fuel_label = QLabel(reading.fuel_product_code, self)
            prev_label = self._value_label()
            prev_label.setText(h.format_litres(reading.previous_ml))

            edit = h.make_integer_edit(self)
            edit.setAlignment(Qt.AlignRight)
            if reading.current_ml is not None:
                edit.setPlaceholderText(h.format_litres(reading.current_ml))
            edit.textChanged.connect(
                lambda _t, rid=reading.nozzle_id: self._update_preview(rid)
            )
            self._nozzle_edits[reading.nozzle_id] = edit

            preview = self._value_label()
            preview.setText(h.format_litres(reading.liters_sold_ml))
            self._nozzle_previews[reading.nozzle_id] = preview

            status = QLabel(
                tr("sheet.status_recorded")
                if reading.has_reading
                else tr("sheet.status_missing"),
                self,
            )
            self._nozzle_status[reading.nozzle_id] = status

            self._nozzle_grid.addWidget(code_label, idx, 0)
            self._nozzle_grid.addWidget(fuel_label, idx, 1)
            self._nozzle_grid.addWidget(prev_label, idx, 2)
            self._nozzle_grid.addWidget(edit, idx, 3)
            self._nozzle_grid.addWidget(preview, idx, 4)
            self._nozzle_grid.addWidget(status, idx, 5)

    def _update_preview(self, nozzle_id: int) -> None:
        """Live sale preview as the operator types (display only).

        The authoritative sale figure always comes from the service after
        the reading is recorded; this is a typing aid.
        """
        reading = next(
            (r for r in self._nozzle_rows if r.nozzle_id == nozzle_id), None
        )
        preview = self._nozzle_previews.get(nozzle_id)
        edit = self._nozzle_edits.get(nozzle_id)
        if reading is None or preview is None or edit is None:
            return
        text = edit.text().strip()
        if not text or reading.previous_ml is None:
            preview.setText(
                h.format_litres(reading.liters_sold_ml)
                if reading.has_reading
                else tr("common.not_entered")
            )
            return
        try:
            # Decimal-only math: liters -> milliliters, no float anywhere.
            current_ml = int(h.parse_litres(text) * 1000)
        except ValueError:
            preview.setText(tr("common.not_entered"))
            return
        if current_ml >= reading.previous_ml:
            preview.setText(h.format_litres(current_ml - reading.previous_ml))
        else:
            preview.setText(tr("common.not_entered"))

    def _on_save_readings(self) -> None:
        business_date = self._business_date()
        if business_date is None:
            return
        saved = 0
        for reading in self._nozzle_rows:
            edit = self._nozzle_edits.get(reading.nozzle_id)
            status = self._nozzle_status.get(reading.nozzle_id)
            text = edit.text().strip() if edit is not None else ""
            if not text:
                continue
            try:
                litres = h.parse_litres(text)
                self._nozzles.record_reading(
                    business_date, reading.nozzle_id, litres
                )
                saved += 1
                if status is not None:
                    status.setText(tr("sheet.status_recorded"))
            except errors.BusinessRuleError as exc:
                if status is not None:
                    status.setText(
                        f"{tr('sheet.status_error')}: {localize_error(exc)}"
                    )
            except Exception:  # noqa: BLE001
                if status is not None:
                    status.setText(tr("sheet.status_error"))
        self.refresh()
        if saved:
            h.show_info(self, tr("sheet.readings_saved").format(n=saved))
