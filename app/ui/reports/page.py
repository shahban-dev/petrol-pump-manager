"""Reports page: report picker, dynamic filters, register-style HTML preview,
print and PDF/XLSX/CSV export.

The page programs against the two Phase-6 contracts built by parallel
agents (``app/models/report.py`` is the frozen shared contract):

* ``app.services.reporting_service.ReportingService`` — ``list_reports()``
  and ``generate_report(...)`` returning ``ReportResult``.
* ``app.services.report_export`` — ``export_report_pdf`` /
  ``export_report_xlsx`` / ``export_report_csv`` each taking
  ``(result, dest_path, *, lang)``.

Both imports are guarded so this module stays importable before those
agents land; the page shows a clear UI error when an unavailable backend
is actually needed. Raw values (integer paisa, integer milliliters) are
formatted at the render boundary only — the UI never does accounting
math, and a language switch never alters stored data.

Daily-register convention: the reporting service tags each
``daily_register`` row with a ``section`` label (e.g. "Nozzle readings",
"Fuel sales", "Stock", "Receipts", "Payments", "Customer credit",
"Daily balances", mirroring the paper register's sections). Rows whose
section matches a known label are grouped under a localized heading;
any other section label is used verbatim as the heading, and rows
without a section tag fall back to the generic table.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QDate, Qt

try:  # printing is a nice-to-have; the page works without it
    from PySide6.QtGui import QTextDocument
    from PySide6.QtPrintSupport import QPrintDialog

    _PRINT_AVAILABLE = True
except ImportError:  # noqa: BLE001
    QTextDocument = None  # type: ignore[assignment]
    QPrintDialog = None  # type: ignore[assignment]
    _PRINT_AVAILABLE = False

from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.localization import get_language, tr
from app.ui import _helpers as h

try:  # Phase-6 reporting service lands from the parallel services agent.
    from app.services.reporting_service import ReportingService
except ImportError:  # pragma: no cover — interim state before it lands
    ReportingService = None  # type: ignore[assignment,misc]

try:  # Phase-6 export engine lands from the parallel export agent.
    from app.services.report_export import (
        export_report_csv,
        export_report_pdf,
        export_report_xlsx,
    )
except ImportError:  # pragma: no cover — interim state before it lands
    export_report_pdf = None  # type: ignore[assignment]
    export_report_xlsx = None  # type: ignore[assignment]
    export_report_csv = None  # type: ignore[assignment]

#: daily_register section labels (as produced by the reporting service)
#: -> localization keys, in paper-register order.
_REGISTER_SECTIONS = (
    ("Nozzle readings", "reports.section_nozzle_readings"),
    ("Fuel sales", "reports.section_fuel_sales"),
    ("Stock", "reports.section_stock"),
    ("Receipts", "reports.section_receipts"),
    ("Payments", "reports.section_payments"),
    ("Customer credit", "reports.section_customer_credit"),
    ("Daily balances", "reports.section_daily_balances"),
)

#: Column kinds that are always right-aligned (money and quantities).
_NUMERIC_KINDS = ("money", "quantity", "price", "number")


def _escape(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _format_cell(column, value) -> str:
    """Format one raw report value according to its column kind.

    Money is integer paisa -> PKR, quantity is integer milliliters ->
    liters, price is integer 0.0001-PKR units. Never float.
    """
    kind = h.dto_field(column, "kind", default="text")
    if value is None:
        return tr("common.not_entered")
    try:
        if kind == "money":
            return h.format_pkr(int(value))
        if kind == "quantity":
            return h.format_litres(int(value))
        if kind == "price":
            return h.format_unit_price(int(value))
        if kind == "number":
            return f"{int(value):,}"
        if kind == "date":
            try:
                return datetime.strptime(str(value), "%Y-%m-%d").strftime(
                    "%d-%m-%Y"
                )
            except ValueError:
                return _escape(value)
    except (TypeError, ValueError):
        return _escape(value)
    return _escape(value)


def _column_align(column) -> str:
    kind = h.dto_field(column, "kind", default="text")
    if kind in _NUMERIC_KINDS:
        return "right"
    align = h.dto_field(column, "align", default="left")
    return align if align in ("left", "right", "center") else "left"


class ReportsPage(QWidget):
    """Report picker, filters, register-style preview, print and export."""

    def __init__(
        self,
        reporting_service,
        day_service,
        parent=None,
        customer_service=None,
        price_service=None,
        db_manager=None,
    ) -> None:
        super().__init__(parent)
        self._reporting = reporting_service
        self._days = day_service
        self._customers = customer_service
        self._prices = price_service
        self._db = db_manager
        self._specs: list = []
        self._filter_widgets: dict[str, tuple[QWidget, object]] = {}
        self._last_result = None
        self._last_html: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self._title = QLabel(self)
        self._title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(self._title)

        # -- report picker ------------------------------------------------
        picker_row = QHBoxLayout()
        self._picker_label = QLabel(self)
        self._report_combo = QComboBox(self)
        self._report_combo.setMinimumWidth(320)
        self._report_combo.currentIndexChanged.connect(self._on_report_changed)
        picker_row.addWidget(self._picker_label)
        picker_row.addWidget(self._report_combo)
        picker_row.addStretch(1)
        layout.addLayout(picker_row)

        # -- dynamic filters -----------------------------------------------
        self._filters_group = QGroupBox(self)
        self._filters_form = QFormLayout(self._filters_group)
        layout.addWidget(self._filters_group)

        # -- actions --------------------------------------------------------
        actions = QHBoxLayout()
        self._generate_btn = QPushButton(self)
        self._generate_btn.clicked.connect(self._on_generate)
        self._print_btn = QPushButton(self)
        self._print_btn.clicked.connect(self._on_print)
        self._pdf_btn = QPushButton(self)
        self._pdf_btn.clicked.connect(lambda: self._on_export("pdf"))
        self._xlsx_btn = QPushButton(self)
        self._xlsx_btn.clicked.connect(lambda: self._on_export("xlsx"))
        self._csv_btn = QPushButton(self)
        self._csv_btn.clicked.connect(lambda: self._on_export("csv"))
        for btn in (
            self._generate_btn,
            self._print_btn,
            self._pdf_btn,
            self._xlsx_btn,
            self._csv_btn,
        ):
            actions.addWidget(btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        # -- preview ---------------------------------------------------------
        self._preview_group = QGroupBox(self)
        preview_layout = QVBoxLayout(self._preview_group)
        self._preview = QTextBrowser(self)
        self._preview.setOpenExternalLinks(False)
        preview_layout.addWidget(self._preview)
        layout.addWidget(self._preview_group, 1)

        self._load_specs()
        self.retranslate()
        self._apply_direction()

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------
    def _apply_direction(self) -> None:
        self.setLayoutDirection(
            Qt.RightToLeft if get_language() == "ur" else Qt.LeftToRight
        )

    def _open_business_date(self) -> str | None:
        try:
            day = self._days.get_open_day()
        except Exception:  # noqa: BLE001
            day = None
        if day is None:
            return None
        return str(h.dto_field(day, "business_date", default="") or "") or None

    def _station_name(self) -> str:
        if self._db is not None:
            try:
                from app.repositories.business_day_repository import (
                    BusinessDayRepository,
                )

                conn = self._db.connect()
                try:
                    name = BusinessDayRepository.get_setting(
                        conn, "station_name"
                    ) or BusinessDayRepository.get_setting(
                        conn, "operator_name"
                    )
                finally:
                    conn.close()
                if name and str(name).strip():
                    return str(name).strip()
            except Exception:  # noqa: BLE001 — fall back to default
                pass
        return tr("reports.default_station")

    def _load_specs(self) -> None:
        self._specs = []
        if self._reporting is None:
            return
        try:
            self._specs = list(self._reporting.list_reports() or [])
        except Exception as exc:  # noqa: BLE001 — show, don't crash
            h.show_error(self, exc)

    def _current_spec(self):
        return self._report_combo.currentData()

    # ------------------------------------------------------------------
    # i18n
    # ------------------------------------------------------------------
    def retranslate(self) -> None:
        """Re-apply all static texts (called on language change)."""
        self._apply_direction()
        self._title.setText(tr("reports.title"))
        self._picker_label.setText(tr("reports.select_report") + ":")
        self._filters_group.setTitle(tr("reports.filters_section"))
        self._preview_group.setTitle(tr("reports.preview_section"))
        self._generate_btn.setText(tr("reports.generate"))
        self._print_btn.setText(tr("reports.print"))
        self._pdf_btn.setText(tr("reports.export_pdf"))
        self._xlsx_btn.setText(tr("reports.export_xlsx"))
        self._csv_btn.setText(tr("reports.export_csv"))
        # Rebuild the report list and the filters so bilingual titles and
        # labels follow the new language; preserve the selection.
        current_id = None
        current = self._current_spec()
        if current is not None:
            current_id = h.dto_field(current, "report_id", default=None)
        self._report_combo.blockSignals(True)
        try:
            self._report_combo.clear()
            rtl = get_language() == "ur"
            for spec in self._specs:
                title = (
                    h.dto_field(spec, "title_ur", default=None)
                    if rtl
                    else None
                ) or h.dto_field(spec, "title_en", default="")
                self._report_combo.addItem(str(title), spec)
            if current_id is not None:
                for index, spec in enumerate(self._specs):
                    if h.dto_field(spec, "report_id", default=None) == current_id:
                        self._report_combo.setCurrentIndex(index)
                        break
        finally:
            self._report_combo.blockSignals(False)
        self._build_filter_controls()
        if self._last_result is not None:
            self._last_html = self._render_html(self._last_result)
            self._preview.setHtml(self._last_html)
        else:
            self._preview.setHtml(
                f"<p><i>{_escape(tr('reports.no_report'))}</i></p>"
            )
        self._set_actions_enabled(self._last_result is not None)

    def refresh(self) -> None:
        """Reload report list and filter lookups (combos may have changed)."""
        self._load_specs()
        self.retranslate()

    def _set_actions_enabled(self, enabled: bool) -> None:
        self._print_btn.setEnabled(enabled)
        self._pdf_btn.setEnabled(enabled)
        self._xlsx_btn.setEnabled(enabled)
        self._csv_btn.setEnabled(enabled)

    # ------------------------------------------------------------------
    # dynamic filters
    # ------------------------------------------------------------------
    def _on_report_changed(self, _index: int) -> None:
        self._build_filter_controls()
        self._last_result = None
        self._last_html = None
        self._preview.setHtml(
            f"<p><i>{_escape(tr('reports.no_report'))}</i></p>"
        )
        self._set_actions_enabled(False)

    def _default_date(self) -> QDate:
        business_date = self._open_business_date()
        if business_date:
            parsed = QDate.fromString(business_date, "yyyy-MM-dd")
            if parsed.isValid():
                return parsed
        return QDate.currentDate()

    def _clear_filters(self) -> None:
        while self._filters_form.rowCount():
            self._filters_form.removeRow(0)
        self._filter_widgets = {}

    def _customer_entries(self) -> list[tuple[str, object]]:
        entries: list[tuple[str, object]] = []
        if self._customers is None:
            return entries
        try:
            customers = self._customers.list_customers() or []
        except Exception:  # noqa: BLE001
            customers = []
        for customer in customers:
            code = h.dto_field(customer, "customer_code", default=None)
            name = h.dto_field(customer, "name", default=None) or code or "—"
            label = f"{name} ({code})" if code else str(name)
            entries.append(
                (label, h.dto_field(customer, "id", default=None))
            )
        return entries

    def _fuel_product_entries(self) -> list[tuple[str, object]]:
        entries: list[tuple[str, object]] = []
        if self._prices is None:
            return entries
        try:
            products = self._prices.list_fuel_products() or []
        except Exception:  # noqa: BLE001
            products = []
        for product in products:
            code = h.dto_field(product, "code", default=None)
            name = h.dto_field(product, "name_en", default=None) or code or "—"
            label = f"{code} — {name}" if code else str(name)
            entries.append((label, h.dto_field(product, "id", default=None)))
        return entries

    def _build_filter_controls(self) -> None:
        self._clear_filters()
        spec = self._current_spec()
        if spec is None:
            return
        rtl = get_language() == "ur"
        filters = h.dto_field(spec, "filters", default=()) or ()
        for report_filter in filters:
            name = str(h.dto_field(report_filter, "name", default=""))
            kind = str(h.dto_field(report_filter, "kind", default="text"))
            required = bool(h.dto_field(report_filter, "required", default=False))
            label_text = (
                h.dto_field(report_filter, "label_ur", default=None)
                if rtl
                else None
            ) or h.dto_field(report_filter, "label_en", default=name)
            label = QLabel(f"{label_text}:", self)
            widget: QWidget
            if kind == "date":
                edit = QDateEdit(self)
                edit.setCalendarPopup(True)
                edit.setDisplayFormat("dd-MM-yyyy")
                edit.setDate(self._default_date())
                widget = edit
            elif kind in ("customer", "fuel_product"):
                combo = QComboBox(self)
                combo.setMinimumWidth(220)
                entries = (
                    self._customer_entries()
                    if kind == "customer"
                    else self._fuel_product_entries()
                )
                if not required:
                    combo.addItem(tr("reports.all_option"), None)
                for entry_label, entry_id in entries:
                    combo.addItem(entry_label, entry_id)
                widget = combo
            elif kind == "category":
                combo = QComboBox(self)
                combo.setEditable(True)
                combo.setMinimumWidth(220)
                if not required:
                    combo.addItem(tr("reports.all_option"), None)
                widget = combo
            else:  # "text" and any future free-text kind
                widget = QLineEdit(self)
            self._filters_form.addRow(label, widget)
            self._filter_widgets[name] = (widget, report_filter)

    def _filter_label(self, report_filter) -> str:
        rtl = get_language() == "ur"
        return str(
            (h.dto_field(report_filter, "label_ur", default=None) if rtl else None)
            or h.dto_field(report_filter, "label_en", default="")
        )

    def _collect_filter_kwargs(self) -> dict:
        """Read the filter controls into generate_report kwargs.

        Raises ValueError when a required filter has no value.
        """
        kwargs: dict = {}
        for name, (widget, report_filter) in self._filter_widgets.items():
            kind = str(h.dto_field(report_filter, "kind", default="text"))
            required = bool(h.dto_field(report_filter, "required", default=False))
            value = None
            if kind == "date" and isinstance(widget, QDateEdit):
                value = widget.date().toString("yyyy-MM-dd")
            elif kind in ("customer", "fuel_product") and isinstance(
                widget, QComboBox
            ):
                value = widget.currentData()
            elif kind == "category" and isinstance(widget, QComboBox):
                text = widget.currentText().strip()
                if not text or text == tr("reports.all_option"):
                    value = None
                else:
                    value = text
            elif isinstance(widget, QLineEdit):
                text = widget.text().strip()
                value = text or None
            if required and value in (None, ""):
                raise ValueError(
                    tr("reports.required_missing").format(
                        label=self._filter_label(report_filter)
                    )
                )
            kwargs[name] = value
        return kwargs

    # ------------------------------------------------------------------
    # generate + preview
    # ------------------------------------------------------------------
    def _on_generate(self) -> None:
        if self._reporting is None:
            h.show_error(self, ValueError(tr("reports.reporting_unavailable")))
            return
        spec = self._current_spec()
        if spec is None:
            return
        try:
            kwargs = self._collect_filter_kwargs()
        except ValueError as exc:
            h.show_error(self, exc)
            return
        try:
            result = self._reporting.generate_report(
                h.dto_field(spec, "report_id", default=""), **kwargs
            )
        except Exception as exc:  # noqa: BLE001 — surface, don't crash
            h.show_error(self, exc)
            return
        self._last_result = result
        self._last_html = self._render_html(result)
        self._preview.setHtml(self._last_html)
        self._set_actions_enabled(True)

    def _render_html(self, result) -> str:
        rtl = get_language() == "ur"
        direction = "rtl" if rtl else "ltr"
        title = (
            h.dto_field(result, "title_ur", default=None) if rtl else None
        ) or h.dto_field(result, "title_en", default="")
        period = (
            h.dto_field(result, "period_label_ur", default=None) if rtl else None
        ) or h.dto_field(result, "period_label_en", default="")
        business_date = h.dto_field(result, "business_date", default=None)
        revision = h.dto_field(result, "closing_revision", default=None)
        generated = h.dto_field(result, "generated_at_utc", default="") or ""

        meta_bits = []
        if period:
            meta_bits.append(
                f"{_escape(tr('reports.period'))}: {_escape(period)}"
            )
        if business_date:
            bit = (
                f"{_escape(tr('reports.business_date'))}: "
                f"{_escape(self._format_iso_date(str(business_date)))}"
            )
            if revision is not None:
                bit += (
                    ' <span class="badge">'
                    f"{_escape(tr('reports.revision_badge').format(n=revision))}"
                    "</span>"
                )
            meta_bits.append(bit)
        meta_bits.append(
            f"{_escape(tr('reports.generated_at'))}: "
            f"{_escape(self._format_timestamp(str(generated)))}"
        )

        parts = [
            "<!DOCTYPE html>",
            f'<html><head><meta charset="utf-8"><style>',
            "body{font-family:sans-serif;margin:12px;}",
            ".header{text-align:center;border-bottom:3px double #333;"
            "padding-bottom:8px;margin-bottom:12px;}",
            ".station{font-size:20px;font-weight:bold;}",
            ".title{font-size:16px;font-weight:bold;margin-top:4px;}",
            ".meta{font-size:12px;color:#333;margin-top:4px;}",
            ".badge{display:inline-block;border:1px solid #333;"
            "border-radius:4px;padding:1px 8px;margin:0 4px;font-size:11px;}",
            "h2.section{font-size:14px;border-bottom:1px solid #999;"
            "padding-bottom:2px;margin:14px 0 6px;}",
            "table{border-collapse:collapse;width:100%;margin:6px 0;"
            "font-size:12px;}",
            "th,td{border:1px solid #999;padding:4px 6px;}",
            "th{background:#eee;}",
            "td.num,th.num{text-align:right;}",
            "td.ctr,th.ctr{text-align:center;}",
            "tr.totals td{font-weight:bold;background:#f7f7f7;}",
            ".notes{font-size:12px;color:#333;}",
            "</style></head>",
            f'<body dir="{direction}">',
            '<div class="header">',
            f'<div class="station">{_escape(self._station_name())}</div>',
            f'<div class="title">{_escape(title)}</div>',
            "".join(f'<div class="meta">{bit}</div>' for bit in meta_bits),
            "</div>",
        ]
        if h.dto_field(result, "report_id", default="") == "daily_register":
            parts.append(self._render_register(result, rtl))
        else:
            parts.append(self._render_table(result, rtl))
        notes = (
            h.dto_field(result, "notes_ur", default=None) if rtl else None
        ) or h.dto_field(result, "notes_en", default=[]) or []
        if notes:
            parts.append(
                f'<h2 class="section">{_escape(tr("reports.notes"))}</h2>'
                '<ul class="notes">'
                + "".join(f"<li>{_escape(note)}</li>" for note in notes)
                + "</ul>"
            )
        parts.append("</body></html>")
        return "".join(parts)

    @staticmethod
    def _format_iso_date(value: str) -> str:
        try:
            return datetime.strptime(value, "%Y-%m-%d").strftime("%d-%m-%Y")
        except ValueError:
            return value

    @staticmethod
    def _format_timestamp(value: str) -> str:
        text = value.strip()
        if not text:
            return "—"
        # 'YYYY-MM-DDTHH:MM:SS.SSSZ' -> 'YYYY-MM-DD HH:MM UTC'
        text = text.replace("T", " ")
        if text.endswith("Z"):
            text = text[:-1].rstrip() + " UTC"
        return text[:22]

    def _table_html(self, columns, rows, totals, rtl: bool) -> str:
        ordered = list(reversed(columns)) if rtl else list(columns)
        header_cells = []
        for column in ordered:
            label = (
                h.dto_field(column, "label_ur", default=None) if rtl else None
            ) or h.dto_field(column, "label_en", default="")
            align = _column_align(column)
            css = "num" if align == "right" else ("ctr" if align == "center" else "")
            header_cells.append(
                f'<th class="{css}">{_escape(label)}</th>' if css
                else f"<th>{_escape(label)}</th>"
            )
        body_rows = []
        for row in rows:
            cells = []
            for column in ordered:
                key = h.dto_field(column, "key", default="")
                align = _column_align(column)
                css = "num" if align == "right" else ("ctr" if align == "center" else "")
                value = row.get(key) if isinstance(row, dict) else h.dto_field(row, key, default=None)
                cell = _format_cell(column, value)
                cells.append(
                    f'<td class="{css}">{cell}</td>' if css
                    else f"<td>{cell}</td>"
                )
            body_rows.append(f"<tr>{''.join(cells)}</tr>")
        totals_row = ""
        if totals:
            cells = []
            first = True
            for column in ordered:
                key = h.dto_field(column, "key", default="")
                align = _column_align(column)
                css = "num" if align == "right" else ("ctr" if align == "center" else "")
                if first:
                    text = _escape(tr("reports.totals"))
                    first = False
                elif key in totals:
                    text = _format_cell(column, totals.get(key))
                else:
                    text = ""
                cells.append(
                    f'<td class="{css}">{text}</td>' if css
                    else f"<td>{text}</td>"
                )
            totals_row = f'<tr class="totals">{"".join(cells)}</tr>'
        return (
            "<table><thead><tr>" + "".join(header_cells) + "</tr></thead>"
            "<tbody>" + "".join(body_rows) + "</tbody>"
            + (f"<tfoot>{totals_row}</tfoot>" if totals_row else "")
            + "</table>"
        )

    def _render_table(self, result, rtl: bool) -> str:
        return self._table_html(
            h.dto_field(result, "columns", default=[]) or [],
            h.dto_field(result, "rows", default=[]) or [],
            h.dto_field(result, "totals", default={}) or {},
            rtl,
        )

    def _render_register(self, result, rtl: bool) -> str:
        """Register-style layout mirroring the paper register.

        Rows are grouped by their ``section`` label (matched
        case-insensitively against the known paper-register sections) and
        rendered in exactly the paper register's sequence: nozzle readings
        -> fuel sales -> stock -> receipts -> payments -> customer credit ->
        daily balances. Unknown section labels render after the known ones
        (in first-seen order); untagged rows fall back to the generic table.
        """
        columns = h.dto_field(result, "columns", default=[]) or []
        rows = h.dto_field(result, "rows", default=[]) or []
        totals = h.dto_field(result, "totals", default={}) or {}
        known = {
            label.strip().lower(): (label, loc_key)
            for label, loc_key in _REGISTER_SECTIONS
        }
        grouped: dict[str, list] = {}
        first_seen: list[str] = []
        untagged: list = []
        for row in rows:
            raw = row.get("section") if isinstance(row, dict) else None
            label = str(raw).strip() if raw else ""
            if not label:
                untagged.append(row)
                continue
            canonical = known.get(label.lower())
            display = canonical[0] if canonical else label
            if display not in grouped:
                grouped[display] = []
                first_seen.append(display)
            grouped[display].append(row)
        parts = []
        body_columns = [
            c for c in columns if h.dto_field(c, "key", default="") != "section"
        ]
        for label, loc_key in _REGISTER_SECTIONS:
            if label not in grouped:
                continue
            parts.append(f'<h2 class="section">{_escape(tr(loc_key))}</h2>')
            parts.append(
                self._table_html(body_columns, grouped[label], {}, rtl)
            )
        for label in first_seen:
            if any(label == known_label for known_label, _ in _REGISTER_SECTIONS):
                continue
            parts.append(f'<h2 class="section">{_escape(label)}</h2>')
            parts.append(
                self._table_html(body_columns, grouped[label], {}, rtl)
            )
        if untagged or not parts:
            parts.append(self._table_html(columns, untagged or rows, totals, rtl))
        elif totals:
            # Totals still shown when everything was sectioned (the section
            # tag column carries no totals of its own).
            parts.append(
                self._table_html(body_columns, [], totals, rtl)
            )
        return "".join(parts)

    # ------------------------------------------------------------------
    # print + export
    # ------------------------------------------------------------------
    def _on_print(self) -> None:
        if self._last_html is None:
            h.show_error(self, ValueError(tr("reports.no_report")))
            return
        if not _PRINT_AVAILABLE:
            h.show_error(self, ValueError(tr("reports.print_unavailable")))
            return
        document = QTextDocument(self)
        document.setHtml(self._last_html)
        if get_language() == "ur":
            document.setLayoutDirection(Qt.RightToLeft)
        dialog = QPrintDialog(self)
        dialog.setWindowTitle(tr("reports.print_title"))
        if dialog.exec() == QDialog.Accepted:
            try:
                document.print_(dialog.printer())
            except Exception as exc:  # noqa: BLE001 — surface, don't crash
                h.show_error(self, exc)

    def _on_export(self, fmt: str) -> None:
        if self._last_result is None:
            h.show_error(self, ValueError(tr("reports.no_report")))
            return
        exporters = {
            "pdf": (export_report_pdf, tr("reports.pdf_files"), ".pdf"),
            "xlsx": (export_report_xlsx, tr("reports.excel_files"), ".xlsx"),
            "csv": (export_report_csv, tr("reports.csv_files"), ".csv"),
        }
        exporter, file_filter, suffix = exporters[fmt]
        if exporter is None:
            h.show_error(self, ValueError(tr("reports.export_unavailable")))
            return
        business_date = h.dto_field(self._last_result, "business_date", default=None)
        report_id = h.dto_field(self._last_result, "report_id", default="report")
        default_name = f"{report_id}_{business_date or 'report'}{suffix}"
        path, _ = QFileDialog.getSaveFileName(
            self, tr("reports.export_title"), default_name, file_filter
        )
        if not path:
            return
        try:
            produced = exporter(self._last_result, path, lang=get_language())
        except Exception as exc:  # noqa: BLE001 — surface, don't crash
            h.show_error(self, exc)
            return
        produced_path = Path(str(produced)) if produced else None
        if (
            produced_path is not None
            and produced_path.exists()
            and produced_path.stat().st_size > 0
        ):
            h.show_info(self, tr("reports.exported_ok"))
        else:
            h.show_error(self, ValueError(tr("reports.export_failed")))
