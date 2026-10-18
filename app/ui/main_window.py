"""Main window: sidebar navigation over the Phase-5 pages.

Dashboard, Daily Register, Nozzle Readings, Fuel Stock, Fuel Deliveries,
Customer Credit, Customer Payments, Cash Book, Suppliers, Other Materials,
Expenses, Reports, Backups, Settings — every control on every page performs
a real service call. There are no mock screens and no dead buttons: a
control that cannot work in this phase is not shown.

A one-minute timer checks the backup service's scheduled-backup policy
and runs a 'scheduled' backup when due; failures are logged, never shown
to the operator as success.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QStackedWidget,
    QWidget,
)

from app import config
from app.localization import get_language, tr
from app.services.business_day_service import BusinessDayService
from app.services.nozzle_service import NozzleService
from app.ui.customers.page import CustomerCreditPage
from app.ui.daily_register.page import DailyRegisterPage
from app.ui.daily_sheet.page import DailySheetPage
from app.ui.dashboard.page import DashboardPage
from app.ui.fuel_deliveries.page import FuelDeliveriesPage
from app.ui.fuel_stock.page import FuelStockPage
from app.ui.nozzles.page import NozzleReadingsPage
from app.ui.payments.page import CustomerPaymentsPage
from app.ui.settings.page import SettingsPage

from app.services.delivery_service import DeliveryService
from app.services.inventory_service import InventoryService
from app.services.price_service import PriceService
from app.services.sales_service import SalesService
from app.services.cashbook_service import CashbookService
from app.services.expense_service import ExpenseService
from app.services.supplier_service import SupplierService
from app.services.accounting_service import AccountingService
from app.services.other_materials_service import OtherMaterialsService
from app.ui.cashbook.page import CashBookPage
from app.ui.expenses.page import ExpensePage
from app.ui.other_materials.page import OtherMaterialsPage
from app.ui.reports.page import ReportsPage
from app.ui.suppliers.page import SupplierPage

try:  # Phase-6 reporting service lands from the parallel services agent;
    # the Reports page is added to the sidebar only when it is importable
    # so the app keeps working in the interim.
    from app.services.reporting_service import ReportingService
except ImportError:  # pragma: no cover — interim state before Phase-6 lands
    ReportingService = None  # type: ignore[assignment]

_REPORTS_AVAILABLE = ReportingService is not None

try:  # Phase-4 services land from the parallel services agent; the pages
    # are added to the sidebar only when both are importable so the app
    # keeps working in the interim.
    from app.services.customer_credit_service import CustomerCreditService
    from app.services.payment_service import PaymentService
except ImportError:  # pragma: no cover — interim state before Phase-4 lands
    CustomerCreditService = None  # type: ignore[assignment]
    PaymentService = None  # type: ignore[assignment]

_PHASE4_AVAILABLE = CustomerCreditService is not None and PaymentService is not None

try:  # Phase-8 backup service lands from the parallel backups agent; the
    # Backups page is added to the sidebar only when it is importable so
    # the app keeps working in the interim.
    from app.services.backup_service import BackupService
except ImportError:  # pragma: no cover — interim state before Phase-8 lands
    BackupService = None  # type: ignore[assignment]

try:  # The Backups page guards its own service import, so this import is
    # safe even before the service lands; the sidebar still waits for the
    # service itself.
    from app.ui.backups.page import BackupsPage
except ImportError:  # pragma: no cover — interim state before Phase-8 lands
    BackupsPage = None  # type: ignore[assignment]

_BACKUPS_AVAILABLE = BackupService is not None and BackupsPage is not None

#: Sidebar order — the Daily Sheet is the daily workflow, so it comes first.
#: DashboardPage's navigation buttons use these indexes.
#: Conditional pages (Reports, Backups) occupy their slot only when their
#: service landed; these are the maximum-index positions.
_PAGE_DAILY_SHEET = 0
_PAGE_DASHBOARD = 1
_PAGE_REGISTER = 2
_PAGE_READINGS = 3
_PAGE_FUEL_STOCK = 4
_PAGE_FUEL_DELIVERIES = 5
_PAGE_CUSTOMERS = 6
_PAGE_PAYMENTS = 7
_PAGE_CASHBOOK = 8
_PAGE_SUPPLIERS = 9
_PAGE_OTHER_MATERIALS = 10
_PAGE_EXPENSES = 11
_PAGE_REPORTS = 12
_PAGE_BACKUPS = 13
_PAGE_SETTINGS = 14

_logger = logging.getLogger(__name__)

#: How often the scheduled-backup policy is re-checked (milliseconds).
_SCHEDULED_BACKUP_CHECK_MS = 60_000


def apply_language_direction(widget=None) -> None:
    """Apply the current language's layout direction (Phase 6 RTL fix).

    Sets the application-wide default direction *and* the given widget's
    direction when one is passed. Dialogs and message boxes inherit the
    direction from their parent widget at construction time — the app-wide
    default additionally covers any parentless dialog. Called at startup
    and on every language switch (via ``MainWindow._apply_direction``),
    so Urdu always renders right-to-left, including error dialogs.
    """
    direction = Qt.RightToLeft if get_language() == "ur" else Qt.LeftToRight
    if widget is not None:
        widget.setLayoutDirection(direction)
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
    except ImportError:
        app = None
    if app is not None:
        app.setLayoutDirection(direction)


class MainWindow(QMainWindow):
    """Sidebar + stacked pages around the bootstrapped database."""

    def __init__(self, bootstrap_result, actor: str | None = None) -> None:
        super().__init__()
        self._result = bootstrap_result
        self._actor = actor
        db_manager = bootstrap_result.db_manager
        self._day_service = BusinessDayService(db_manager, actor=actor)
        self._nozzle_service = NozzleService(db_manager, actor=actor)
        self._price_service = PriceService(db_manager, actor=actor)
        self._delivery_service = DeliveryService(db_manager, actor=actor)
        self._inventory_service = InventoryService(db_manager, actor=actor)
        self._sales_service = SalesService(db_manager, actor=actor)
        self._credit_service = (
            CustomerCreditService(db_manager, actor=actor)
            if _PHASE4_AVAILABLE
            else None
        )
        self._payment_service = (
            PaymentService(db_manager, actor=actor) if _PHASE4_AVAILABLE else None
        )
        self._cashbook_service = CashbookService(db_manager, actor=actor)
        self._expense_service = ExpenseService(db_manager, actor=actor)
        self._supplier_service = SupplierService(db_manager, actor=actor)
        self._accounting_service = AccountingService(db_manager, actor=actor)
        self._other_materials_service = OtherMaterialsService(
            db_manager, actor=actor
        )
        self._reporting_service = (
            ReportingService(db_manager, actor=actor)
            if _REPORTS_AVAILABLE
            else None
        )
        self._backup_service = self._make_backup_service(db_manager)

        self.setWindowTitle(tr("app.title"))
        self.resize(1024, 720)
        self._apply_direction()

        self._build_menu()
        self._build_body(db_manager)
        self._build_status_bar()
        self._start_scheduled_backup_timer()

    # ------------------------------------------------------------------
    @staticmethod
    def _make_backup_service(db_manager):
        """Build the backup service, tolerating the contract's constructor.

        The published contract is ``BackupService(actor=...)``; the real
        service may also accept the database manager. Both forms are
        tried — the real module wins over the contract text.
        """
        if BackupService is None:
            return None
        try:
            return BackupService(actor=None, db_manager=db_manager)
        except TypeError:
            pass
        try:
            return BackupService(actor=None)
        except TypeError:
            pass
        try:
            return BackupService(db_manager)
        except Exception:  # noqa: BLE001
            _logger.warning("BackupService could not be constructed")
            return None

    def _start_scheduled_backup_timer(self) -> None:
        self._backup_timer = QTimer(self)
        self._backup_timer.setInterval(_SCHEDULED_BACKUP_CHECK_MS)
        self._backup_timer.timeout.connect(self._maybe_run_scheduled_backup)
        self._backup_timer.start()

    def _maybe_run_scheduled_backup(self) -> None:
        """Run a scheduled backup when the service says one is due.

        Failures are logged only — a background backup must never pop up
        dialogs or claim success it did not achieve.
        """
        service = self._backup_service
        if service is None:
            return
        try:
            if not service.scheduled_backup_due():
                return
            service.run_backup("scheduled")
            service.mark_scheduled_backup()
        except Exception:  # noqa: BLE001
            _logger.exception("Scheduled backup failed")

    # ------------------------------------------------------------------
    def _apply_direction(self) -> None:
        apply_language_direction(self)

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu(tr("menu.file"))
        exit_action = file_menu.addAction(tr("menu.exit"))
        exit_action.triggered.connect(self.close)
        help_menu = menu_bar.addMenu(tr("menu.help"))
        about_action = help_menu.addAction(tr("menu.about"))
        about_action.triggered.connect(self._show_about)

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            tr("about.title"),
            tr("about.text").format(version=config.APP_VERSION),
        )

    def _build_body(self, db_manager) -> None:
        central = QWidget(self)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._sidebar = QListWidget(self)
        self._sidebar.setObjectName("sidebar")
        self._sidebar.setMaximumWidth(200)
        self._sidebar.setMinimumWidth(160)
        self._pages = QStackedWidget(self)

        self._daily_sheet = DailySheetPage(
            db_manager,
            self._day_service,
            self._cashbook_service,
            self._expense_service,
            self._sales_service,
            self._inventory_service,
            self._nozzle_service,
            self._price_service,
            self,
        )
        self._dashboard = DashboardPage(
            self._day_service,
            self._nozzle_service,
            self.navigate_to,
            self,
            cashbook_service=self._cashbook_service,
            accounting_service=self._accounting_service,
            sales_service=self._sales_service,
            inventory_service=self._inventory_service,
            credit_service=self._credit_service,
        )
        self._register = DailyRegisterPage(self._day_service, self)
        self._readings = NozzleReadingsPage(self._nozzle_service, self)
        self._page_widgets: list = [
            self._daily_sheet,
            self._dashboard,
            self._register,
            self._readings,
        ]
        self._sidebar_keys = [
            "nav.daily_sheet",
            "nav.dashboard",
            "nav.daily_register",
            "nav.nozzle_readings",
        ]
        self._fuel_stock = FuelStockPage(
            self._inventory_service,
            self._price_service,
            self._sales_service,
            self._nozzle_service,
            self,
        )
        self._fuel_deliveries = FuelDeliveriesPage(
            self._delivery_service,
            self._inventory_service,
            self._price_service,
            self,
        )
        self._page_widgets += [self._fuel_stock, self._fuel_deliveries]
        self._sidebar_keys += ["nav.fuel_stock", "nav.fuel_deliveries"]
        if _PHASE4_AVAILABLE:
            self._customers = CustomerCreditPage(
                self._credit_service,
                self._day_service,
                self,
                nozzle_service=self._nozzle_service,
            )
            self._payments = CustomerPaymentsPage(
                self._payment_service,
                self._credit_service,
                self._day_service,
                self,
            )
            self._page_widgets += [self._customers, self._payments]
            self._sidebar_keys += ["nav.customers", "nav.payments"]
        self._cashbook = CashBookPage(
            self._cashbook_service, self._day_service, self
        )
        self._suppliers = SupplierPage(
            self._supplier_service,
            self._delivery_service,
            self._day_service,
            self,
        )
        self._other_materials = OtherMaterialsPage(
            self._other_materials_service, self._day_service, self
        )
        self._expenses = ExpensePage(
            self._expense_service, self._day_service, self
        )
        self._page_widgets += [
            self._cashbook,
            self._suppliers,
            self._other_materials,
            self._expenses,
        ]
        self._sidebar_keys += [
            "nav.cashbook",
            "nav.suppliers",
            "nav.other_materials",
            "nav.expenses",
        ]
        if _REPORTS_AVAILABLE:
            self._reports = ReportsPage(
                self._reporting_service,
                self._day_service,
                self,
                customer_service=self._credit_service,
                price_service=self._price_service,
                db_manager=db_manager,
            )
            self._page_widgets.append(self._reports)
            self._sidebar_keys.append("nav.reports")
        if _BACKUPS_AVAILABLE:
            self._backups = BackupsPage(
                db_manager,
                self,
                backup_service=self._backup_service,
            )
            self._page_widgets.append(self._backups)
            self._sidebar_keys.append("nav.backups")
        self._settings = SettingsPage(
            db_manager, self._on_language_changed, self
        )
        self._page_widgets.append(self._settings)
        self._sidebar_keys.append("nav.settings")
        for widget in self._page_widgets:
            # Scroll fix: every page scrolls vertically inside the stacked
            # widget so nothing is half-cut on small (e.g. 1366x768) screens.
            scroll = QScrollArea(self)
            scroll.setWidgetResizable(True)
            scroll.setWidget(widget)
            self._pages.addWidget(scroll)

        self._sidebar_items: list[QListWidgetItem] = []
        for key in self._sidebar_keys:
            item = QListWidgetItem(tr(key), self._sidebar)
            self._sidebar_items.append(item)
        # Page-key -> sidebar index, for string navigation (e.g. "daily_sheet").
        self._page_key_index = {
            key: idx for idx, key in enumerate(self._sidebar_keys)
        }
        self._sidebar.setCurrentRow(_PAGE_DAILY_SHEET)
        self._sidebar.currentRowChanged.connect(self._on_sidebar_changed)

        layout.addWidget(self._sidebar)
        layout.addWidget(self._pages, 1)
        self.setCentralWidget(central)

    # ------------------------------------------------------------------
    def _on_sidebar_changed(self, row: int) -> None:
        self._pages.setCurrentIndex(row)
        # Refresh the page being shown so figures are never stale.
        widget = self._page_widgets[row]
        refresh = getattr(widget, "refresh", None)
        if callable(refresh):
            refresh()

    def navigate_to(self, index: int | str) -> None:
        """Switch pages (used by dashboard shortcut buttons).

        Accepts a sidebar index or a page key such as ``"daily_sheet"``
        (resolved against the sidebar keys; falls back to the daily
        register when the key is not registered).
        """
        if isinstance(index, str):
            key = f"nav.{index}"
            target = self._page_key_index.get(key)
            if target is None:
                target = _PAGE_REGISTER
            index = target
        self._sidebar.setCurrentRow(index)

    def _on_language_changed(self) -> None:
        """Apply a language switch live: direction, menu, all pages."""
        self._apply_direction()
        self.setWindowTitle(tr("app.title"))
        self.menuBar().clear()
        self._build_menu()
        for item, key in zip(self._sidebar_items, self._sidebar_keys):
            item.setText(tr(key))
        for widget in self._page_widgets:
            retranslate = getattr(widget, "retranslate", None)
            if callable(retranslate):
                retranslate()

    # ------------------------------------------------------------------
    def _build_status_bar(self) -> None:
        self.statusBar().showMessage(
            f"{tr('statusbar.db_connected')} \u2022 "
            f"{tr('statusbar.schema').format(version=self._result.schema_version)}"
        )
