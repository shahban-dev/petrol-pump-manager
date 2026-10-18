"""Offscreen UI interaction tests (Phase 6): the Reports page.

Drives the real ReportsPage through its actual controls. Two layers:

* Stub services implementing the published Phase-6 contracts
  (``ReportingService.list_reports`` / ``generate_report`` and the
  ``ReportSpec``/``ReportFilter``/``ReportResult``/``ReportColumn``
  dataclasses from ``app.models.report``) — these run NOW and prove the
  page wires every control to a service call.
* The REAL ``ReportingService`` on a temp DB (bootstrap) — proving the
  page works against the actual reporting API once the parallel
  reporting-services agent lands.

Export/print paths are exercised through the real buttons with a
monkeypatched ``QFileDialog.getSaveFileName`` and a monkeypatched
``QPrintDialog``. Requires Qt; skips cleanly when Qt cannot initialize.
"""

from __future__ import annotations

import importlib.util
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
QtCore = pytest.importorskip("PySide6.QtCore", exc_type=ImportError)
pytest.importorskip("PySide6.QtPrintSupport", exc_type=ImportError)

from app.localization import get_language, set_language, tr
from app.models.report import (
    ReportColumn,
    ReportFilter,
    ReportResult,
    ReportSpec,
)
from app.ui import _helpers as h
from app.ui.reports import page as reports_page_module
from app.ui.reports.page import ReportsPage

QApplication = QtWidgets.QApplication
QMessageBox = QtWidgets.QMessageBox
QDialog = QtWidgets.QDialog
QFileDialog = QtWidgets.QFileDialog

DAY = "2026-10-12"

#: The real Phase-6 reporting service / export engine present?
_REPORTING_AVAILABLE = (
    importlib.util.find_spec("app.services.reporting_service") is not None
)
_EXPORT_AVAILABLE = (
    importlib.util.find_spec("app.services.report_export") is not None
)
needs_reporting = pytest.mark.skipif(
    not _REPORTING_AVAILABLE, reason="reporting_service not landed yet"
)
needs_export = pytest.mark.skipif(
    not _EXPORT_AVAILABLE, reason="report_export not landed yet"
)


# ---------------------------------------------------------------------------
# Stub services (in-memory, contract-shaped)
# ---------------------------------------------------------------------------
class StubDayService:
    def get_open_day(self):
        return SimpleNamespace(business_date=DAY)


class StubNoDayService:
    def get_open_day(self):
        return None


class StubCustomerService:
    def list_customers(self, active_only=True):
        return [
            SimpleNamespace(
                id=1, customer_code="CUST-0001", name="Alpha Traders",
            ),
            SimpleNamespace(
                id=2, customer_code="CUST-0002", name="Beta Goods",
            ),
        ]


class StubPriceService:
    def list_fuel_products(self, active_only=True):
        return [
            {"id": 1, "code": "PETROL", "name_en": "Petrol"},
            {"id": 2, "code": "DIESEL", "name_en": "Diesel"},
        ]


def _register_columns():
    return [
        ReportColumn("nozzle", "Nozzle", "نوزل"),
        ReportColumn("previous_l", "Previous (L)", "پچھلی (لٹر)", kind="quantity"),
        ReportColumn("current_l", "Current (L)", "موجودہ (لٹر)", kind="quantity"),
        ReportColumn("liters", "Liters sold", "فروخت شدہ لٹر", kind="quantity"),
        ReportColumn("unit_price", "Unit price", "فی لٹر قیمت", kind="price"),
        ReportColumn("amount", "Amount (PKR)", "رقم (روپے)", kind="money"),
    ]


def _register_rows():
    return [
        {
            "section": "Nozzle readings",
            "nozzle": "1 PG+",
            "previous_l": 84_938_000,
            "current_l": 84_985_000,
            "liters": 47_000,
            "unit_price": 2_700_000,
            "amount": 1_269_000,
        },
        {
            "section": "Nozzle readings",
            "nozzle": "2 PG+",
            "previous_l": 81_234_000,
            "current_l": 81_274_000,
            "liters": 40_000,
            "unit_price": 2_700_000,
            "amount": 1_080_000,
        },
        {
            "section": "Fuel sales",
            "nozzle": "PETROL",
            "previous_l": None,
            "current_l": None,
            "liters": 87_000,
            "unit_price": 2_700_000,
            "amount": 2_349_000,
        },
        {
            "section": "Stock",
            "nozzle": "PETROL",
            "previous_l": None,
            "current_l": None,
            "liters": 5_000_000,
            "unit_price": None,
            "amount": None,
        },
        {
            "section": "Receipts",
            "nozzle": "Cash sales",
            "previous_l": None,
            "current_l": None,
            "liters": None,
            "unit_price": None,
            "amount": 2_049_000,
        },
        {
            "section": "Payments",
            "nozzle": "Expenses",
            "previous_l": None,
            "current_l": None,
            "liters": None,
            "unit_price": None,
            "amount": 120_000,
        },
        {
            "section": "Customer credit",
            "nozzle": "Alpha Traders",
            "previous_l": None,
            "current_l": None,
            "liters": 40_000,
            "unit_price": 2_700_000,
            "amount": 1_080_000,
        },
        {
            "section": "Daily balances",
            "nozzle": "Expected cash",
            "previous_l": None,
            "current_l": None,
            "liters": None,
            "unit_price": None,
            "amount": 1_929_000,
        },
    ]


class StubReportingService:
    """Test double for ReportingService.

    Records every ``generate_report`` call (kwargs included) and returns
    canned ``ReportResult`` values whose raw figures the tests assert on
    in the rendered preview HTML.
    """

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def list_reports(self):
        return [
            ReportSpec(
                report_id="daily_register",
                title_en="Daily Register",
                title_ur="روزنامچہ",
                filters=(
                    ReportFilter(
                        name="business_date",
                        label_en="Business date",
                        label_ur="کاروباری تاریخ",
                        kind="date",
                        required=True,
                    ),
                ),
            ),
            ReportSpec(
                report_id="customer_statement",
                title_en="Customer Statement",
                title_ur="کسٹمر اسٹیٹمنٹ",
                filters=(
                    ReportFilter(
                        name="customer_id",
                        label_en="Customer",
                        label_ur="کسٹمر",
                        kind="customer",
                        required=True,
                    ),
                    ReportFilter(
                        name="date_from",
                        label_en="From",
                        label_ur="از",
                        kind="date",
                    ),
                    ReportFilter(
                        name="date_to",
                        label_en="To",
                        label_ur="تک",
                        kind="date",
                    ),
                ),
            ),
            ReportSpec(
                report_id="fuel_sales",
                title_en="Fuel Sales",
                title_ur="ایندھن کی فروخت",
                filters=(
                    ReportFilter(
                        name="date_from",
                        label_en="From",
                        label_ur="از",
                        kind="date",
                    ),
                    ReportFilter(
                        name="date_to",
                        label_en="To",
                        label_ur="تک",
                        kind="date",
                    ),
                    ReportFilter(
                        name="fuel_product_id",
                        label_en="Fuel product",
                        label_ur="ایندھن",
                        kind="fuel_product",
                    ),
                    ReportFilter(
                        name="category",
                        label_en="Category",
                        label_ur="قسم",
                        kind="category",
                    ),
                    ReportFilter(
                        name="reference",
                        label_en="Reference",
                        label_ur="حوالہ",
                        kind="text",
                    ),
                ),
            ),
        ]

    def generate_report(self, report_id, **kwargs):
        self.calls.append((report_id, dict(kwargs)))
        if report_id == "daily_register":
            return ReportResult(
                report_id="daily_register",
                title_en="Daily Register",
                title_ur="روزنامچہ",
                generated_at_utc="2026-10-12T11:00:00.000Z",
                business_date=kwargs.get("business_date"),
                closing_revision=2,
                period_label_en="12-10-2026",
                period_label_ur="12-10-2026",
                columns=_register_columns(),
                rows=_register_rows(),
                totals={"liters": 87_000, "amount": 2_349_000},
                notes_en=["Register-style preview test."],
                notes_ur=["رجسٹر طرز پیش نظارہ ٹیسٹ۔"],
            )
        if report_id == "customer_statement":
            return ReportResult(
                report_id="customer_statement",
                title_en="Customer Statement",
                title_ur="کسٹمر اسٹیٹمنٹ",
                generated_at_utc="2026-10-12T11:00:00.000Z",
                date_from=kwargs.get("date_from"),
                date_to=kwargs.get("date_to"),
                period_label_en="01-10-2026 to 12-10-2026",
                period_label_ur="01-10-2026 تا 12-10-2026",
                columns=[
                    ReportColumn("date", "Date", "تاریخ", kind="date"),
                    ReportColumn("description", "Description", "تفصیل"),
                    ReportColumn("debit", "Debit (PKR)", "ڈیبٹ", kind="money"),
                    ReportColumn("credit", "Credit (PKR)", "کریڈٹ", kind="money"),
                    ReportColumn("balance", "Balance (PKR)", "بیلنس", kind="money"),
                ],
                rows=[
                    {
                        "date": "2026-10-12",
                        "description": "Ticket T-1",
                        "debit": 1_080_000,
                        "credit": 0,
                        "balance": 1_080_000,
                    },
                    {
                        "date": "2026-10-12",
                        "description": "Payment P-1",
                        "debit": 0,
                        "credit": 500_000,
                        "balance": 580_000,
                    },
                ],
                totals={"debit": 1_080_000, "credit": 500_000},
            )
        if report_id == "fuel_sales":
            return ReportResult(
                report_id="fuel_sales",
                title_en="Fuel Sales",
                title_ur="ایندھن کی فروخت",
                generated_at_utc="2026-10-12T11:00:00.000Z",
                date_from=kwargs.get("date_from"),
                date_to=kwargs.get("date_to"),
                columns=[
                    ReportColumn("product", "Product", "ایندھن"),
                    ReportColumn("liters", "Liters", "لٹر", kind="quantity"),
                    ReportColumn("amount", "Amount (PKR)", "رقم", kind="money"),
                ],
                rows=[
                    {
                        "product": "PETROL",
                        "liters": 87_000,
                        "amount": 2_349_000,
                    }
                ],
                totals={"liters": 87_000, "amount": 2_349_000},
            )
        raise ValueError(f"unknown report: {report_id}")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.quit()


@pytest.fixture
def dialogs(qapp, monkeypatch):
    """Suppress modal dialogs; the offscreen run never blocks."""
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.Yes,
    )
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.Accepted)
    set_language("en")
    yield
    set_language("en")


@pytest.fixture
def reporting_stub():
    return StubReportingService()


@pytest.fixture
def day_stub():
    return StubDayService()


@pytest.fixture
def no_day_stub():
    return StubNoDayService()


@pytest.fixture
def customers_stub():
    return StubCustomerService()


@pytest.fixture
def prices_stub():
    return StubPriceService()


def _make_page(reporting, days, customers=None, prices=None, db_manager=None):
    page = ReportsPage(
        reporting,
        days,
        customer_service=customers,
        price_service=prices,
        db_manager=db_manager,
    )
    page.show()
    return page


@pytest.fixture
def reports_page(qapp, dialogs, reporting_stub, day_stub, customers_stub,
                 prices_stub):
    page = _make_page(
        reporting_stub, day_stub, customers_stub, prices_stub,
    )
    yield page
    page.close()


def _select_report(page, report_id):
    for index in range(page._report_combo.count()):
        spec = page._report_combo.itemData(index)
        if h.dto_field(spec, "report_id", default=None) == report_id:
            page._report_combo.setCurrentIndex(index)
            return
    raise AssertionError(f"report {report_id!r} not in picker")


# ---------------------------------------------------------------------------
# Stub-service tests: every control wires to a service call
# ---------------------------------------------------------------------------
def test_picker_lists_all_reports(qapp, dialogs, reports_page):
    combo = reports_page._report_combo
    assert combo.count() == 3
    assert combo.itemText(0) == "Daily Register"
    assert combo.itemText(1) == "Customer Statement"
    assert combo.itemText(2) == "Fuel Sales"
    # The current spec drives the filter area.
    assert "business_date" in reports_page._filter_widgets


def test_filter_controls_follow_report_selection(
    qapp, dialogs, reports_page, customers_stub, prices_stub,
):
    _select_report(reports_page, "customer_statement")
    widgets = reports_page._filter_widgets
    assert set(widgets) == {"customer_id", "date_from", "date_to"}
    customer_combo = widgets["customer_id"][0]
    assert isinstance(customer_combo, QtWidgets.QComboBox)
    # "All" + 2 customers; required -> no "All" option.
    assert customer_combo.count() == 2
    assert customer_combo.itemText(0) == "Alpha Traders (CUST-0001)"
    assert isinstance(widgets["date_from"][0], QtWidgets.QDateEdit)

    _select_report(reports_page, "fuel_sales")
    widgets = reports_page._filter_widgets
    assert set(widgets) == {
        "date_from", "date_to", "fuel_product_id", "category", "reference",
    }
    product_combo = widgets["fuel_product_id"][0]
    assert isinstance(product_combo, QtWidgets.QComboBox)
    # Optional -> "All" option first.
    assert product_combo.count() == 3
    assert product_combo.itemData(0) is None
    assert product_combo.itemText(1) == "PETROL — Petrol"
    category_combo = widgets["category"][0]
    assert isinstance(category_combo, QtWidgets.QComboBox)
    assert category_combo.isEditable()
    assert isinstance(widgets["reference"][0], QtWidgets.QLineEdit)


def test_generate_daily_register_preview(
    qapp, dialogs, reports_page, reporting_stub,
):
    _select_report(reports_page, "daily_register")
    # The required business_date edit defaults to the open business day.
    date_edit = reports_page._filter_widgets["business_date"][0]
    assert date_edit.date().toString("yyyy-MM-dd") == DAY

    assert not reports_page._print_btn.isEnabled()
    reports_page._generate_btn.click()
    assert reports_page._print_btn.isEnabled()

    report_id, kwargs = reporting_stub.calls[-1]
    assert report_id == "daily_register"
    assert kwargs == {"business_date": DAY}

    html = reports_page._preview.toHtml()
    # Header: station default, bilingual title, period, revision badge.
    assert tr("reports.default_station") in html
    assert "Daily Register" in html
    assert "12-10-2026" in html
    assert "Closing revision 2" in html
    # Register sections mirror the paper register.
    for heading in (
        "Nozzle readings", "Fuel sales", "Stock", "Receipts", "Payments",
        "Customer credit", "Daily balances",
    ):
        assert heading in html, heading
    # Raw figures formatted at the render boundary: money as PKR,
    # milliliters as liters, never float.
    assert "12,690.00" in html      # 1,269,000 paisa nozzle amount
    assert "47.000" in html          # 47,000 ml nozzle liters
    assert "23,490.00" in html      # totals row: 2,349,000 paisa
    assert "87.000" in html         # totals row: 87,000 ml
    assert tr("reports.totals") in html
    assert "Register-style preview test." in html


def test_generate_customer_statement_filter_wiring(
    qapp, dialogs, reports_page, reporting_stub,
):
    _select_report(reports_page, "customer_statement")
    widgets = reports_page._filter_widgets
    widgets["customer_id"][0].setCurrentIndex(0)  # Alpha Traders, id 1
    widgets["date_from"][0].setDate(QtCore.QDate(2026, 10, 1))
    widgets["date_to"][0].setDate(QtCore.QDate(2026, 10, 12))

    reports_page._generate_btn.click()
    report_id, kwargs = reporting_stub.calls[-1]
    assert report_id == "customer_statement"
    assert kwargs == {
        "customer_id": 1,
        "date_from": "2026-10-01",
        "date_to": "2026-10-12",
    }
    html = reports_page._preview.toHtml()
    assert "10,800.00" in html   # ticket debit
    assert "5,000.00" in html    # payment credit
    assert "12-10-2026" in html  # DD-MM-YYYY date rendering
    assert "Customer Statement" in html


def test_generate_fuel_sales_all_filter_kinds(
    qapp, dialogs, reports_page, reporting_stub,
):
    _select_report(reports_page, "fuel_sales")
    widgets = reports_page._filter_widgets
    widgets["fuel_product_id"][0].setCurrentIndex(1)  # PETROL, id 1
    widgets["category"][0].setEditText("lubricants")
    widgets["reference"][0].setText("REF-1")

    reports_page._generate_btn.click()
    report_id, kwargs = reporting_stub.calls[-1]
    assert report_id == "fuel_sales"
    assert kwargs["fuel_product_id"] == 1
    assert kwargs["category"] == "lubricants"
    assert kwargs["reference"] == "REF-1"
    assert kwargs["date_from"] == DAY  # defaulted to the open day
    html = reports_page._preview.toHtml()
    assert "23,490.00" in html
    assert "87.000" in html


def test_required_filter_guard_shows_error(
    qapp, dialogs, reports_page, reporting_stub,
):
    _select_report(reports_page, "customer_statement")
    # customer_id is required but the stub spec has no "All" option, so
    # simulate an unusable state by clearing the combo selection.
    combo = reports_page._filter_widgets["customer_id"][0]
    combo.setCurrentIndex(-1)
    before = len(reporting_stub.calls)
    reports_page._generate_btn.click()  # must not crash
    assert len(reporting_stub.calls) == before  # service never called
    assert reports_page._last_result is None
    assert not reports_page._print_btn.isEnabled()


def test_no_open_day_defaults_and_generates(
    qapp, dialogs, reporting_stub, customers_stub, prices_stub, no_day_stub,
):
    page = _make_page(
        reporting_stub, no_day_stub, customers_stub, prices_stub,
    )
    try:
        page.show()
        _select_report(page, "daily_register")
        date_edit = page._filter_widgets["business_date"][0]
        # No open day -> today's date, still generates without crashing.
        assert date_edit.date() == QtCore.QDate.currentDate()
        page._generate_btn.click()
        report_id, kwargs = reporting_stub.calls[-1]
        assert report_id == "daily_register"
        assert kwargs["business_date"] == QtCore.QDate.currentDate().toString(
            "yyyy-MM-dd"
        )
        assert "Daily Register" in page._preview.toHtml()
    finally:
        page.close()


def test_service_error_surfaces_without_crash(
    qapp, dialogs, reports_page, reporting_stub,
):
    def boom(report_id, **kwargs):
        raise RuntimeError("simulated reporting failure")

    reporting_stub.generate_report = boom
    _select_report(reports_page, "daily_register")
    reports_page._generate_btn.click()  # warning suppressed by fixture
    assert reports_page._last_result is None
    assert not reports_page._print_btn.isEnabled()


def test_daily_register_section_order_matches_paper_register(
    qapp, dialogs, reporting_stub, day_stub,
):
    """The register preview renders sections in exactly the paper
    register's sequence — derived from the report data's section labels,
    never reordered by the UI.

    Required order: nozzle readings -> fuel sales -> stock -> receipts ->
    payments -> customer credit -> daily balances.
    """
    page = _make_page(reporting_stub, day_stub)
    try:
        # Deliberately shuffled, mixed-case labels, plus an unknown section.
        rows = [
            {"section": "payments", "description": "Expenses",
             "quantity_ml": None, "amount_paisa": 120_000},
            {"section": "DAILY BALANCES", "description": "Expected cash",
             "quantity_ml": None, "amount_paisa": 1_929_000},
            {"section": "Nozzle readings", "description": "1 PG+",
             "quantity_ml": 47_000, "amount_paisa": 1_269_000},
            {"section": "Mystery section", "description": "X",
             "quantity_ml": None, "amount_paisa": 1},
            {"section": "fuel sales", "description": "PETROL",
             "quantity_ml": 87_000, "amount_paisa": 2_349_000},
            {"section": "Stock", "description": "PETROL",
             "quantity_ml": 5_000_000, "amount_paisa": None},
            {"section": "Receipts", "description": "Cash sales",
             "quantity_ml": None, "amount_paisa": 2_049_000},
            {"section": "Customer Credit", "description": "Alpha Traders",
             "quantity_ml": 40_000, "amount_paisa": 1_080_000},
        ]
        result = ReportResult(
            report_id="daily_register",
            title_en="Daily Register",
            title_ur="روزانہ رجسٹر",
            generated_at_utc="2026-10-12T11:00:00.000Z",
            business_date=DAY,
            columns=[
                ReportColumn("section", "Section", "حصہ"),
                ReportColumn("description", "Description", "تفصیل"),
                ReportColumn("quantity_ml", "Quantity", "مقدار",
                             kind="quantity"),
                ReportColumn("amount_paisa", "Amount (PKR)", "رقم",
                             kind="money"),
            ],
            rows=rows,
            totals={"amount_paisa": 2_349_000},
        )
        html = page._render_html(result)
        headings = [
            "Nozzle readings",
            "Fuel sales",
            "Stock",
            "Receipts",
            "Payments",
            "Customer credit",
            "Daily balances",
            "Mystery section",  # unknown labels render after known ones
        ]
        positions = [html.index(f">{heading}</h2>") for heading in headings]
        assert positions == sorted(positions), headings
        # The section tag column itself is not rendered inside sections.
        assert ">حصہ</th>" not in html and ">Section</th>" not in html
        # Money/quantity formatting still applies inside sections.
        assert "12,690.00" in html
        assert "47.000" in html
    finally:
        page.close()


# ---------------------------------------------------------------------------
# Export buttons: real files through the real buttons
# ---------------------------------------------------------------------------
def _fake_exporter(marker: bytes):
    def _export(result, dest_path, *, lang="en"):
        out = str(dest_path)
        with open(out, "wb") as fh:
            fh.write(marker + lang.encode("utf-8"))
        return out

    return _export


@pytest.fixture
def export_wiring(qapp, dialogs, monkeypatch, tmp_path, reports_page):
    """Wire the page module's export entry points to byte-writing fakes
    and point QFileDialog at temp paths."""
    monkeypatch.setattr(
        reports_page_module, "export_report_pdf",
        _fake_exporter(b"%PDF-1.4\n"), raising=False,
    )
    monkeypatch.setattr(
        reports_page_module, "export_report_xlsx",
        _fake_exporter(b"PK\x03\x04"), raising=False,
    )
    monkeypatch.setattr(
        reports_page_module, "export_report_csv",
        _fake_exporter(b"a,b\n"), raising=False,
    )
    chosen = {}

    def fake_get_save(parent, title, default_name, file_filter):
        suffix = default_name.rsplit(".", 1)[-1]
        target = tmp_path / f"report.{suffix}"
        chosen["path"] = str(target)
        chosen["default"] = default_name
        return str(target), file_filter

    monkeypatch.setattr(QFileDialog, "getSaveFileName", fake_get_save)
    return chosen


def test_export_buttons_produce_real_files(
    qapp, dialogs, export_wiring, reports_page, tmp_path,
):
    _select_report(reports_page, "daily_register")
    reports_page._generate_btn.click()
    assert "report.pdf" not in [p.name for p in tmp_path.iterdir()]

    reports_page._pdf_btn.click()
    pdf = tmp_path / "report.pdf"
    assert pdf.exists() and pdf.stat().st_size > 0
    assert pdf.read_bytes().startswith(b"%PDF-1.4")
    assert export_wiring["default"].startswith("daily_register_")
    assert export_wiring["default"].endswith(".pdf")

    reports_page._xlsx_btn.click()
    xlsx = tmp_path / "report.xlsx"
    assert xlsx.exists() and xlsx.stat().st_size > 0
    assert xlsx.read_bytes().startswith(b"PK\x03\x04")

    reports_page._csv_btn.click()
    csv = tmp_path / "report.csv"
    assert csv.exists() and csv.stat().st_size > 0


def test_export_guard_when_engine_missing(
    qapp, dialogs, monkeypatch, reports_page, tmp_path,
):
    """With no export engine, the buttons show an error and write nothing."""
    monkeypatch.setattr(
        reports_page_module, "export_report_pdf", None, raising=False,
    )
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        lambda *a, **k: (str(tmp_path / "report.pdf"), "*.pdf"),
    )
    _select_report(reports_page, "daily_register")
    reports_page._generate_btn.click()
    reports_page._pdf_btn.click()
    assert not (tmp_path / "report.pdf").exists()


def test_export_buttons_disabled_before_generate(
    qapp, dialogs, reports_page, monkeypatch, tmp_path,
):
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("dialog must not open")
        ),
    )
    # Fresh page: nothing generated yet -> buttons disabled, no dialog.
    assert not reports_page._pdf_btn.isEnabled()
    assert not reports_page._xlsx_btn.isEnabled()
    assert not reports_page._csv_btn.isEnabled()
    assert not reports_page._print_btn.isEnabled()
    reports_page._pdf_btn.click()  # disabled: no-op
    reports_page._print_btn.click()


# ---------------------------------------------------------------------------
# Print path
# ---------------------------------------------------------------------------
def test_print_path_does_not_crash(
    qapp, dialogs, monkeypatch, tmp_path, reports_page,
):
    from PySide6.QtPrintSupport import QPrinter

    calls = {}

    class FakePrintDialog:
        def __init__(self, parent=None):
            self._printer = QPrinter()
            self._printer.setOutputFormat(QPrinter.PdfFormat)
            self._printer.setOutputFileName(str(tmp_path / "print.pdf"))
            calls["created"] = True

        def setWindowTitle(self, title):
            pass

        def exec(self):
            calls["exec"] = True
            return QDialog.Accepted

        def printer(self):
            return self._printer

    monkeypatch.setattr(reports_page_module, "QPrintDialog", FakePrintDialog)
    _select_report(reports_page, "daily_register")
    reports_page._generate_btn.click()
    reports_page._print_btn.click()
    assert calls.get("exec") is True
    printed = tmp_path / "print.pdf"
    assert printed.exists() and printed.stat().st_size > 0


# ---------------------------------------------------------------------------
# Urdu / RTL
# ---------------------------------------------------------------------------
def test_urdu_mode_renders_rtl(
    qapp, dialogs, reporting_stub, day_stub, customers_stub, prices_stub,
):
    set_language("ur")
    page = _make_page(
        reporting_stub, day_stub, customers_stub, prices_stub,
    )
    try:
        page.show()
        assert page._report_combo.itemText(0) == "روزنامچہ"
        assert page.layoutDirection() == QtCore.Qt.RightToLeft
        _select_report(page, "daily_register")
        page._generate_btn.click()
        html = page._preview.toHtml()
        # QTextDocument normalizes the markup, so the raw generated HTML is
        # the source of truth for the dir attribute.
        assert 'dir="rtl"' in page._last_html
        assert "روزنامچہ" in html
        assert "کلوزنگ نظرثانی 2" in html
        assert "نوزل ریڈنگ" in html
        # Figures are language-independent.
        assert "23,490.00" in html
    finally:
        page.close()
        set_language("en")


# ---------------------------------------------------------------------------
# Sidebar wiring
# ---------------------------------------------------------------------------
def test_sidebar_wiring(qapp, dialogs, tmp_path):
    """Reports sits between Expenses and Backups when the reporting
    service is importable; otherwise it is absent (graceful interim)."""
    from app.bootstrap import bootstrap
    from app.ui.main_window import MainWindow, _REPORTS_AVAILABLE

    result = bootstrap(tmp_path / "reportsui.sqlite3")
    try:
        window = MainWindow(result)
        try:
            window.show()
            labels = [
                window._sidebar.item(i).text()
                for i in range(window._sidebar.count())
            ]
            if _REPORTS_AVAILABLE:
                assert tr("nav.reports") in labels
                reports_index = labels.index(tr("nav.reports"))
                assert labels[reports_index - 1] == tr("nav.expenses")
                assert labels[reports_index + 1] == tr("nav.backups")
                widget = window._pages.widget(reports_index)
                # Pages are wrapped in QScrollArea (v2 responsive fix).
                inner = widget.widget() if hasattr(widget, "widget") else widget
                assert isinstance(inner, ReportsPage)
            else:
                assert tr("nav.reports") not in labels
        finally:
            window.close()
    finally:
        result.db_manager.close()


# ---------------------------------------------------------------------------
# Real reporting service on a temp DB
# ---------------------------------------------------------------------------
@needs_reporting
def test_real_daily_register_cashbook_and_statement(
    qapp, dialogs, tmp_path,
):
    import json
    from pathlib import Path

    from app.bootstrap import bootstrap
    from app.repositories.business_day_repository import (
        BusinessDayRepository,
    )
    from app.services.business_day_service import BusinessDayService
    from app.services.customer_credit_service import CustomerCreditService
    from app.services.expense_service import ExpenseService
    from app.services.nozzle_service import NozzleService
    from app.services.payment_service import PaymentService
    from app.services.price_service import PriceService
    from app.services.reporting_service import ReportingService

    # Station identity comes from the real-register fixture.
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "register_day_2026_10_06.json"
        ).read_text(encoding="utf-8")
    )
    station_name = fixture["station"]["name"]

    result = bootstrap(tmp_path / "reportsreal.sqlite3")
    try:
        dbm = result.db_manager
        conn = dbm.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            BusinessDayRepository.set_setting(
                conn, "station_name", station_name
            )
            conn.commit()
        finally:
            conn.close()
        days = BusinessDayService(dbm)
        prices = PriceService(dbm)
        credit = CustomerCreditService(dbm)
        payments = PaymentService(dbm)
        expenses = ExpenseService(dbm)
        nozzles = NozzleService(dbm)

        # Post a small day: open, price, reading, credit ticket + payment,
        # expense.
        days.open_day(DAY, opening_cash_paisa=200_000)
        midnight_utc = "2026-10-11T19:00:00.000Z"  # DAY 00:00 Asia/Karachi
        prices.set_price("PETROL", Decimal("270"), midnight_utc, DAY)
        prices.set_price("DIESEL", Decimal("265"), midnight_utc, DAY)
        nozzles.record_reading(DAY, "1 PG+", Decimal("84985"))
        customer = credit.create_customer("Alpha Traders")
        ticket = credit.issue_ticket(
            customer.customer_code,
            DAY,
            [
                {
                    "fuel_product_code": "PETROL",
                    "nozzle_code": "1 PG+",
                    "quantity_litres": Decimal("40"),
                }
            ],
            occurred_at_utc="2026-10-12T05:00:00.000Z",
        )
        assert h.dto_field(ticket, "gross_amount_paisa") == 1_080_000
        payments.record_payment(
            customer.customer_code,
            DAY,
            500_000,
            occurred_at_utc="2026-10-12T07:00:00.000Z",
        )
        expenses.record_expense(
            business_date=DAY,
            category="Fuel purchase",
            amount_paisa=120_000,
            method="cash",
            occurred_at_utc="2026-10-12T06:00:00.000Z",
        )

        reporting = ReportingService(dbm)
        specs = {s.report_id: s for s in reporting.list_reports()}
        for wanted in ("daily_register", "cashbook", "customer_statement"):
            assert wanted in specs, f"{wanted} missing from list_reports()"

        page = ReportsPage(
            reporting, days, customer_service=credit, price_service=prices,
            db_manager=dbm,
        )
        page.show()
        try:
            # daily_register: the ticket gross shows in the preview and the
            # totals match the ledger figure.
            _select_report(page, "daily_register")
            page._generate_btn.click()
            html = page._preview.toHtml()
            assert "Daily Register" in html
            assert station_name in html  # from app_settings
            assert "10,800.00" in html
            assert "12-10-2026" in html

            # cashbook: the expense shows.
            _select_report(page, "cashbook")
            page._generate_btn.click()
            html = page._preview.toHtml()
            assert "1,200.00" in html

            # customer_statement: pick the customer, ticket + payment legs.
            _select_report(page, "customer_statement")
            widgets = page._filter_widgets
            assert "customer_id" in widgets
            combo = widgets["customer_id"][0]
            target = next(
                i for i in range(combo.count())
                if combo.itemData(i) == customer.id
            )
            combo.setCurrentIndex(target)
            page._generate_btn.click()
            html = page._preview.toHtml()
            assert "10,800.00" in html
            assert "5,000.00" in html
        finally:
            page.close()
    finally:
        result.db_manager.close()


@needs_reporting
@needs_export
def test_real_export_engine_produces_files(
    qapp, dialogs, tmp_path, monkeypatch,
):
    from app.bootstrap import bootstrap
    from app.services.business_day_service import BusinessDayService
    from app.services.reporting_service import ReportingService
    from app.services.report_export import (
        export_report_csv,
        export_report_pdf,
        export_report_xlsx,
    )

    result = bootstrap(tmp_path / "reportsexport.sqlite3")
    try:
        dbm = result.db_manager
        days = BusinessDayService(dbm)
        days.open_day(DAY, opening_cash_paisa=200_000)
        reporting = ReportingService(dbm)
        page = ReportsPage(reporting, days, db_manager=dbm)
        page.show()
        try:
            _select_report(page, "cashbook")
            page._generate_btn.click()
            assert page._last_result is not None

            targets = {
                "pdf": (tmp_path / "r.pdf", export_report_pdf),
                "xlsx": (tmp_path / "r.xlsx", export_report_xlsx),
                "csv": (tmp_path / "r.csv", export_report_csv),
            }
            for suffix, (target, exporter) in targets.items():
                out = exporter(
                    page._last_result, str(target), lang="en",
                )
                assert out is not None
                produced = tmp_path / f"r.{suffix}"
                assert produced.exists() and produced.stat().st_size > 0
        finally:
            page.close()
    finally:
        result.db_manager.close()
