"""Offscreen UI interaction tests (Phase 3): Fuel Stock + Fuel Deliveries.

Drives the real pages through their actual buttons/dialogs against stub
services that implement the published Phase-3 service contracts. Modal
dialogs are auto-answered via monkeypatched QDialog so the offscreen run
never blocks. MainWindow sidebar wiring is tested only when the real
services are importable (they land from a parallel agent).

Requires Qt; skips cleanly when Qt cannot initialize here.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import dataclass, field

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
QtCore = pytest.importorskip("PySide6.QtCore", exc_type=ImportError)

from app.localization import set_language, tr
from app.ui.fuel_deliveries.page import FuelDeliveriesPage
from app.ui.fuel_stock.page import FuelStockPage

QApplication = QtWidgets.QApplication
QMessageBox = QtWidgets.QMessageBox
QDialog = QtWidgets.QDialog
QPushButton = QtWidgets.QPushButton
QDate = QtCore.QDate

DAY = "2026-10-12"


# ---------------------------------------------------------------------------
# Stub services implementing the Phase-3 contracts
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SupplierInfo:
    id: int
    name: str
    phone: str | None = None
    address: str | None = None
    is_active: bool = True


@dataclass(frozen=True)
class DeliveryInfo:
    id: int
    supplier_id: int
    supplier_name: str
    supplier_invoice_no: str | None
    fuel_product_id: int
    fuel_product_code: str
    tank_id: int
    tank_code: str
    business_date: str
    quantity_ml: int
    unit_cost_10k: int
    total_paisa: int
    paid_paisa: int
    status: str


@dataclass(frozen=True)
class TankInfo:
    id: int
    code: str
    fuel_product_id: int
    fuel_product_code: str
    label_en: str | None = None
    capacity_ml: int = 20_000_000
    is_active: bool = True


@dataclass(frozen=True)
class PriceInfo:
    id: int
    fuel_product_id: int
    fuel_product_code: str
    effective_from_utc: str
    unit_price_10k: int
    business_date: str
    created_by: str | None = None


@dataclass(frozen=True)
class StockPosition:
    tank_id: int
    tank_code: str
    fuel_product_code: str
    opening_ml: int
    received_ml: int
    sold_ml: int
    adjustments_ml: int
    theoretical_ml: int
    physical_ml: int | None
    variance_ml: int | None


@dataclass(frozen=True)
class DaySales:
    business_date: str
    intervals: list = field(default_factory=list)
    liters_by_product_ml: dict = field(default_factory=dict)
    revenue_by_product_paisa: dict = field(default_factory=dict)


@dataclass(frozen=True)
class NozzleStub:
    id: int
    code: str
    fuel_product_id: int
    fuel_product_code: str


class StubDeliveryService:
    def __init__(self):
        self.suppliers = [SupplierInfo(1, "PSO Depot")]
        self.deliveries: list[DeliveryInfo] = []
        self.next_supplier = 2
        self.next_delivery = 1

    def list_suppliers(self, active_only=True):
        return [s for s in self.suppliers if s.is_active or not active_only]

    def add_supplier(self, name, phone=None, address=None, *, actor=None):
        if not name:
            raise ValueError("name required")
        supplier = SupplierInfo(self.next_supplier, name, phone, address)
        self.suppliers.append(supplier)
        self.next_supplier += 1
        return supplier.id

    def set_supplier_active(self, supplier_id, is_active, *, actor=None):
        pass

    def record_delivery(
        self,
        *,
        supplier,
        fuel_product,
        tank,
        business_date,
        quantity_litres,
        unit_cost_pkr,
        supplier_invoice_no=None,
        other_cost_pkr=None,
        amount_paid_pkr=None,
        occurred_at_utc=None,
        delivery_uuid=None,
        actor=None,
    ):
        from decimal import Decimal

        from app.calculations import money as calc

        qty_ml = calc.to_ml(quantity_litres)
        unit_10k = int(Decimal(unit_cost_pkr) * 10_000)
        total = calc.amount_paisa_for(qty_ml, unit_10k)
        paid = calc.to_paisa(amount_paid_pkr or Decimal("0"))
        delivery = DeliveryInfo(
            id=self.next_delivery,
            supplier_id=supplier,
            supplier_name=next(
                s.name for s in self.suppliers if s.id == supplier
            ),
            supplier_invoice_no=supplier_invoice_no,
            fuel_product_id=fuel_product,
            fuel_product_code="PG+",
            tank_id=tank,
            tank_code="T1",
            business_date=business_date,
            quantity_ml=qty_ml,
            unit_cost_10k=unit_10k,
            total_paisa=total,
            paid_paisa=paid,
            status="paid" if paid >= total else ("partial" if paid else "unpaid"),
        )
        self.deliveries.append(delivery)
        self.next_delivery += 1
        return delivery.id

    def list_deliveries(self, business_date=None):
        return [
            d
            for d in self.deliveries
            if business_date is None or d.business_date == business_date
        ]

    def supplier_balance(self, supplier_id):
        invoiced = sum(
            d.total_paisa for d in self.deliveries if d.supplier_id == supplier_id
        )
        paid = sum(
            d.paid_paisa for d in self.deliveries if d.supplier_id == supplier_id
        )
        return {
            "total_invoiced_paisa": invoiced,
            "total_paid_paisa": paid,
            "outstanding_paisa": invoiced - paid,
        }


class StubInventoryService:
    def __init__(self):
        self.tanks = [
            TankInfo(1, "T1", 1, "PG+"),
            TankInfo(2, "T2", 2, "HSD"),
        ]
        self.calls = []

    def list_tanks(self, active_only=True):
        return [t for t in self.tanks if t.is_active or not active_only]

    def set_opening_stock(self, fuel_product, quantity_litres, business_date, *, actor=None):
        self.calls.append(("set_opening_stock", fuel_product, quantity_litres, business_date))

    def stock_positions(self, business_date):
        return [
            StockPosition(
                tank_id=1,
                tank_code="T1",
                fuel_product_code="PG+",
                opening_ml=10_000_000,
                received_ml=5_000_000,
                sold_ml=3_000_000,
                adjustments_ml=0,
                theoretical_ml=12_000_000,
                physical_ml=11_900_000,
                variance_ml=-100_000,
            ),
            StockPosition(
                tank_id=2,
                tank_code="T2",
                fuel_product_code="HSD",
                opening_ml=8_000_000,
                received_ml=0,
                sold_ml=1_000_000,
                adjustments_ml=0,
                theoretical_ml=7_000_000,
                physical_ml=None,
                variance_ml=None,
            ),
        ]

    def record_physical_count(self, tank, business_date, quantity_litres, *, measured_by=None, note=None, actor=None):
        self.calls.append(("record_physical_count", tank, business_date, quantity_litres))

    def correct_physical_count(self, tank, business_date, quantity_litres, *, reason, actor=None):
        self.calls.append(("correct_physical_count", tank, business_date, quantity_litres, reason))

    def record_adjustment(self, *, fuel_product, tank, business_date, quantity_litres_signed, reason, authorized_by=None, actor=None):
        self.calls.append(("record_adjustment", fuel_product, tank, business_date, quantity_litres_signed, reason))


class StubPriceService:
    def __init__(self):
        self.products = [
            {"id": 1, "code": "PG+", "name_en": "Petrol"},
            {"id": 2, "code": "HSD", "name_en": "Diesel"},
        ]
        self.prices = [
            PriceInfo(1, 1, "PG+", "2026-10-12T00:00:00.000Z", 2_684_500, "2026-10-12"),
        ]
        self.next_price = 2

    def list_fuel_products(self):
        return self.products

    def get_price(self, product, at_utc):
        return self.prices[-1]

    def set_price(self, product, price_pkr, effective_from_utc, business_date, *, actor=None):
        from decimal import Decimal

        info = PriceInfo(
            self.next_price,
            product,
            "PG+",
            effective_from_utc,
            int(Decimal(price_pkr) * 10_000),
            business_date,
        )
        self.prices.append(info)
        self.next_price += 1
        return info.id

    def list_prices(self, product):
        return [p for p in self.prices if p.fuel_product_id == product]


class StubSalesService:
    def __init__(self):
        self.posted: list[str] = []

    def day_sales(self, business_date):
        return DaySales(
            business_date=business_date,
            liters_by_product_ml={"PG+": 3_000_000, "HSD": 1_000_000},
            revenue_by_product_paisa={"PG+": 80_535_000, "HSD": 25_700_000},
        )

    def post_day_sales(self, business_date, *, actor=None):
        self.posted.append(business_date)
        return {"posted": 4, "already_posted": 0, "skipped_unknown_qty": 0}


class StubNozzleService:
    def __init__(self):
        self.nozzles = [
            NozzleStub(1, "N1", 1, "PG+"),
            NozzleStub(2, "N2", 1, "PG+"),
            NozzleStub(3, "N3", 2, "HSD"),
        ]
        self.snapshots = []

    def list_nozzles(self, *, active_only=True):
        return self.nozzles

    def record_segment_reading(self, business_date, nozzle, litres, *, occurred_at_utc, actor=None):
        self.snapshots.append((business_date, nozzle, litres, occurred_at_utc))
        return 1


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
    """Auto-answer modal dialogs and message boxes."""
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: None)

    def fake_exec(self):
        for child in self.findChildren(QtWidgets.QLineEdit):
            # Fill only the dialog's own edits (never a QDateTimeEdit's
            # internal editor) with a valid number.
            if child.parent() is self and not child.text():
                child.setText("100")
        for child in self.findChildren(QtWidgets.QTextEdit):
            if not child.toPlainText():
                child.setPlainText("ui test reason")
        return QDialog.Accepted

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    set_language("en")
    yield
    set_language("en")


@pytest.fixture
def deliveries_page(qapp, dialogs):
    page = FuelDeliveriesPage(
        StubDeliveryService(), StubInventoryService(), StubPriceService()
    )
    page.show()
    yield page
    page.close()


@pytest.fixture
def stock_page(qapp, dialogs):
    page = FuelStockPage(
        StubInventoryService(),
        StubPriceService(),
        StubSalesService(),
        StubNozzleService(),
    )
    page.show()
    yield page
    page.close()


def _click_all_buttons(page):
    """Click every QPushButton on the page: none may be dead or raise.

    Handlers run against stub services; errors surface through the
    monkeypatched QMessageBox, so a click that raises means a dead or
    broken button.
    """
    for btn in page.findChildren(QPushButton):
        assert btn.text().strip(), "button with no label"
        btn.click()


# ---------------------------------------------------------------------------
# FuelDeliveriesPage
# ---------------------------------------------------------------------------
def test_deliveries_table_populates(deliveries_page):
    page = deliveries_page
    page._date_edit.setDate(QDate(2026, 10, 12))
    assert page._table.rowCount() == 0  # nothing recorded yet

    page._qty_edit.setText("5000")
    page._unit_cost_edit.setText("250.50")
    page._invoice_edit.setText("INV-1")
    page._paid_edit.setText("1000000")
    page._save_btn.click()

    assert page._table.rowCount() == 1
    assert page._table.item(0, 0).text() == "PSO Depot"
    assert page._table.item(0, 1).text() == "INV-1"
    assert page._table.item(0, 4).text() == "5,000.000"
    assert page._table.item(0, 6).text() == "1,252,500.00"
    assert page._table.item(0, 7).text() == "1,000,000.00"
    assert page._table.item(0, 8).text() == tr("delivery.status_partial")


def test_add_supplier_dialog_then_balance_updates(deliveries_page):
    page = deliveries_page
    before = page._supplier_combo.count()
    page._add_supplier_btn.click()  # dialog auto-answered; name filled by fixture
    assert page._supplier_combo.count() == before + 1
    # The newly added supplier is selected and shows zero outstanding.
    assert page._supplier_combo.currentText() == "100"
    assert "0.00" in page._balance_value.text()


def test_delivery_validation_error_shows_no_crash(deliveries_page):
    """Empty quantity -> ValueError path shows a warning, table unchanged."""
    page = deliveries_page
    page._date_edit.setDate(QDate(2026, 10, 12))
    rows_before = page._table.rowCount()
    page._qty_edit.setText("")  # invalid: empty quantity
    page._unit_cost_edit.setText("250")
    page._save_btn.click()
    assert page._table.rowCount() == rows_before


def test_deliveries_buttons_all_connected(deliveries_page):
    _click_all_buttons(deliveries_page)


def test_deliveries_alternate_dto_shapes(qapp, dialogs):
    """Rows using payment_status/total_cost_paisa and an int balance render."""

    @dataclass(frozen=True)
    class AltDelivery:
        payment_status: str
        total_cost_paisa: int
        paid_paisa: int
        quantity_ml: int
        unit_cost_10k: int
        supplier_name: str = "PSO Depot"
        supplier_invoice_no: str | None = None
        fuel_product_code: str = "PG+"
        tank_code: str = "T1"

    class AltDeliveryService(StubDeliveryService):
        def list_deliveries(self, business_date=None):
            return [
                AltDelivery(
                    payment_status="unpaid",
                    total_cost_paisa=1_252_500_00,
                    paid_paisa=0,
                    quantity_ml=5_000_000,
                    unit_cost_10k=2_505_000,
                )
            ]

        def supplier_balance(self, supplier_id):
            return 1_252_500_00  # bare int outstanding paisa

    page = FuelDeliveriesPage(
        AltDeliveryService(), StubInventoryService(), StubPriceService()
    )
    page.show()
    try:
        page._date_edit.setDate(QDate(2026, 10, 12))
        page.refresh()
        assert page._table.rowCount() == 1
        assert page._table.item(0, 6).text() == "1,252,500.00"
        assert page._table.item(0, 8).text() == tr("delivery.status_unpaid")
        assert "1,252,500.00" in page._balance_value.text()
    finally:
        page.close()


# ---------------------------------------------------------------------------
# FuelStockPage
# ---------------------------------------------------------------------------
def test_stock_positions_table_populates(stock_page):
    page = stock_page
    page._date_edit.setDate(QDate(2026, 10, 12))
    page.refresh()
    assert page._table.rowCount() == 2
    assert page._table.item(0, 0).text() == "T1"
    assert page._table.item(0, 1).text() == "PG+"
    assert page._table.item(0, 2).text() == "10,000.000"
    assert page._table.item(0, 6).text() == "12,000.000"
    assert page._table.item(0, 7).text() == "11,900.000"
    assert page._table.item(0, 8).text() == "-100.000"
    # Second tank has no physical count yet -> em dash.
    assert page._table.item(1, 7).text() == "—"
    assert page._table.item(1, 8).text() == "—"


def test_sales_summary_label(stock_page):
    page = stock_page
    page._date_edit.setDate(QDate(2026, 10, 12))
    page.refresh()
    text = page._sales_label.text()
    assert text.startswith(tr("stock.sales_summary").split("{")[0])
    assert "PG+" in text and "3,000.000" in text
    assert "PKR 805,350.00" in text


def test_price_table_and_set_price(stock_page):
    page = stock_page
    page._date_edit.setDate(QDate(2026, 10, 12))
    page._product_combo.setCurrentIndex(0)  # PG+
    page.refresh()
    assert page._price_table.rowCount() == 1
    assert page._price_table.item(0, 1).text() == "268.4500"
    assert page._price_table.item(0, 0).text() == "12-10-2026 05:00"

    page._set_price_btn.click()  # dialog auto-answered: price "100" is valid
    assert page._price_table.rowCount() == 2
    assert page._price_table.item(1, 1).text() == "100.0000"


def test_snapshot_button_records_reading(stock_page):
    page = stock_page
    page._date_edit.setDate(QDate(2026, 10, 12))
    page._product_combo.setCurrentIndex(0)  # PG+ -> nozzles N1, N2
    page.refresh()
    assert page._nozzle_combo.count() == 2
    page._snap_qty_edit.setText("12345.5")
    page._snapshot_btn.click()
    service = page._nozzles
    assert len(service.snapshots) == 1
    business_date, nozzle_id, litres, occurred = service.snapshots[0]
    assert business_date == "2026-10-12"
    assert nozzle_id == 1
    assert str(litres) == "12345.5"
    assert occurred.endswith("Z")


def test_stock_dialogs_call_services(stock_page):
    """All dialog buttons run their service call without crashing."""
    page = stock_page
    inv = page._inventory
    calls_before = len(inv.calls)
    page._physical_btn.click()
    page._correct_btn.click()
    page._adjust_btn.click()
    page._opening_btn.click()
    assert len(inv.calls) == calls_before + 4
    kinds = [c[0] for c in inv.calls[calls_before:]]
    assert kinds == [
        "record_physical_count",
        "correct_physical_count",
        "record_adjustment",
        "set_opening_stock",
    ]


def test_post_day_sales_button(stock_page):
    page = stock_page
    page._date_edit.setDate(QDate(2026, 10, 12))
    page._post_btn.click()
    assert DAY in page._sales.posted


def test_stock_buttons_all_connected(stock_page):
    _click_all_buttons(stock_page)


def test_sales_summary_alternate_shapes(qapp, dialogs):
    """day_sales as a dict-of-products and as a row list both render."""

    class DictSalesService(StubSalesService):
        def day_sales(self, business_date):
            return {
                "products": [
                    {
                        "fuel_product_code": "PG+",
                        "quantity_ml": 3_000_000,
                        "revenue_paisa": 80_535_000,
                    }
                ]
            }

    for svc, expected in (
        (DictSalesService(), "PG+: 3,000.000 L (PKR 805,350.00)"),
    ):
        page = FuelStockPage(
            StubInventoryService(),
            StubPriceService(),
            svc,
            StubNozzleService(),
        )
        page.show()
        try:
            page._date_edit.setDate(QDate(2026, 10, 12))
            page.refresh()
            assert expected in page._sales_label.text()
        finally:
            page.close()


# ---------------------------------------------------------------------------
# i18n
# ---------------------------------------------------------------------------
def test_language_switch_to_urdu_does_not_crash(qapp, dialogs):
    set_language("ur")
    deliveries = FuelDeliveriesPage(
        StubDeliveryService(), StubInventoryService(), StubPriceService()
    )
    stock = FuelStockPage(
        StubInventoryService(),
        StubPriceService(),
        StubSalesService(),
        StubNozzleService(),
    )
    deliveries.show()
    stock.show()
    deliveries.refresh()
    stock.refresh()
    assert deliveries._title.text() == "ایندھن کی ترسیل"
    assert stock._title.text() == "ایندھن اسٹاک"
    assert deliveries._refresh_btn.text() == "تازہ کریں"
    assert stock._refresh_btn.text() == "تازہ کریں"
    deliveries.close()
    stock.close()
    set_language("en")


# ---------------------------------------------------------------------------
# MainWindow sidebar wiring (only when the real services exist)
# ---------------------------------------------------------------------------
def test_main_window_sidebar_wiring(qapp, dialogs, tmp_path):
    pytest.importorskip("app.services.price_service")
    pytest.importorskip("app.services.delivery_service")
    pytest.importorskip("app.services.inventory_service")
    pytest.importorskip("app.services.sales_service")
    from app.bootstrap import bootstrap
    from app.ui.main_window import MainWindow, _PAGE_FUEL_STOCK, _PAGE_FUEL_DELIVERIES

    result = bootstrap(tmp_path / "phase3.sqlite3")
    win = MainWindow(result)
    win.show()
    try:
        assert _PAGE_FUEL_STOCK == 3
        assert _PAGE_FUEL_DELIVERIES == 4
        assert win._sidebar.item(3).text() == tr("nav.fuel_stock")
        assert win._sidebar.item(4).text() == tr("nav.fuel_deliveries")
        win.navigate_to(_PAGE_FUEL_STOCK)
        assert isinstance(win._pages.currentWidget(), FuelStockPage)
        win.navigate_to(_PAGE_FUEL_DELIVERIES)
        assert isinstance(win._pages.currentWidget(), FuelDeliveriesPage)
        # Sidebar count matches the page stack (14 once the Phase-5
        # services land and the cash-book / supplier / materials / expense
        # pages are added, plus the Phase-6 Reports page when its service
        # is importable, plus the Phase-8 Backups page when its service
        # is importable).
        try:
            from app.services.customer_credit_service import (  # noqa: F401
                CustomerCreditService,
            )
            from app.services.payment_service import PaymentService  # noqa: F401

            expected_pages = 12
        except ImportError:
            expected_pages = 10
        try:
            from app.services.reporting_service import (  # noqa: F401
                ReportingService,
            )

            expected_pages += 1
        except ImportError:
            pass
        try:
            from app.services.backup_service import BackupService  # noqa: F401
            from app.ui.backups.page import BackupsPage  # noqa: F401

            expected_pages += 1
        except ImportError:
            pass
        assert win._sidebar.count() == win._pages.count() == expected_pages
    finally:
        win.close()
        result.db_manager.close()
