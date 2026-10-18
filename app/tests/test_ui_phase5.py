"""Offscreen UI interaction tests (Phase 5): cash book, expenses,
suppliers, other materials.

Drives the real pages through their actual buttons/dialogs. Two layers:

* Stub services implementing the published Phase-5 contracts — these run
  NOW and prove the pages wire every control to a service call.
* The REAL services on a temp DB (bootstrap) — proving the pages work
  against the actual service APIs.

Modal dialogs are auto-driven via a monkeypatched QDialog.exec that fills
fields and clicks the dialog's own buttons, so the offscreen run never
blocks. Requires Qt; skips cleanly when Qt cannot initialize here.
"""

from __future__ import annotations

import importlib.util
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
QtCore = pytest.importorskip("PySide6.QtCore", exc_type=ImportError)

from app.localization import set_language, tr
from app.ui import _helpers as h
from app.ui.cashbook.page import (
    CashBookPage,
    _SettleDialog,
    _CashMoveDialog,
)
from app.ui.expenses.page import ExpensePage
from app.ui.other_materials.page import OtherMaterialsPage, _TxnDialog
from app.ui.suppliers.page import SupplierPage, _RecordPaymentDialog

QApplication = QtWidgets.QApplication
QMessageBox = QtWidgets.QMessageBox
QDialog = QtWidgets.QDialog
QPushButton = QtWidgets.QPushButton

DAY = "2026-10-12"

#: Real Phase-5 services present?
_REAL_SERVICES = (
    importlib.util.find_spec("app.services.cashbook_service") is not None
    and importlib.util.find_spec("app.services.expense_service") is not None
    and importlib.util.find_spec("app.services.supplier_service") is not None
    and importlib.util.find_spec("app.services.accounting_service") is not None
    and importlib.util.find_spec("app.services.other_materials_service")
    is not None
)
needs_real = pytest.mark.skipif(
    not _REAL_SERVICES, reason="Phase-5 services not landed yet"
)


# ---------------------------------------------------------------------------
# Stub DTOs
# ---------------------------------------------------------------------------
@dataclass
class StubCashPosition:
    business_date: str
    opening_cash_paisa: int
    receipts_paisa: int
    payments_paisa: int
    expected_paisa: int
    physical_cash_paisa: int | None = None
    difference_paisa: int | None = None
    shortage_paisa: int = 0
    surplus_paisa: int = 0


# ---------------------------------------------------------------------------
# Stub services (in-memory, contract-shaped)
# ---------------------------------------------------------------------------
class StubDayService:
    def get_open_day(self):
        return SimpleNamespace(business_date=DAY)


class StubNoDayService:
    def get_open_day(self):
        return None


class StubCashbookService:
    """Test double for CashbookService (all sums computed here, in the
    double — the UI under test does none)."""

    def __init__(self):
        self._receipts: list[dict] = []
        self._payments: list[dict] = []
        self._next = 1
        self._recorded = False
        self._per_product: list[dict] = [
            {
                "fuel_product_code": "PETROL",
                "meter_revenue_paisa": 2_000_000,  # 20,000 PKR
                "cash_paisa": 0,
                "bank_paisa": 0,
                "paid_now_paisa": 0,
                "receivable_paisa": 0,
                "other_paisa": 0,
                "settled_paisa": 0,
                "new_receivables_paisa": 0,
                "identity_ok": False,
                "difference_paisa": 2_000_000,
            }
        ]

    # -- categories -----------------------------------------------------
    def list_categories(self, kind):
        if kind == "receipt":
            return [
                SimpleNamespace(
                    id=1, kind="receipt", name="cash_sales",
                    name_en="Cash sales", name_ur="نقد فروخت",
                    is_active=True,
                ),
                SimpleNamespace(
                    id=2, kind="receipt", name="other_receipt",
                    name_en="Other receipt", name_ur="دیگر وصولی",
                    is_active=True,
                ),
            ]
        return [
            SimpleNamespace(
                id=3, kind="payment", name="expense",
                name_en="Expense", name_ur="اخراجات", is_active=True,
            ),
            SimpleNamespace(
                id=4, kind="payment", name="other_payment",
                name_en="Other payment", name_ur="دیگر ادائیگی",
                is_active=True,
            ),
        ]

    def add_category(self, kind, name, name_en, name_ur=None, *, actor=None):
        raise NotImplementedError

    def set_category_active(self, category_id, active, *, actor=None):
        raise NotImplementedError

    # -- settlements ------------------------------------------------------
    def settlement_status(self, business_date):
        return {
            "recorded": self._recorded,
            "batch_uuid": "batch-1" if self._recorded else None,
            "per_product": [dict(p) for p in self._per_product],
            "identity_ok": all(
                p["identity_ok"] for p in self._per_product
            ),
            "note": None,
        }

    def record_settlements(
        self, business_date, *, cash_by_product, bank_by_product=None,
        other_by_product=None, notes=None, occurred_at_utc=None, actor=None,
    ):
        if self._recorded:
            from app.services import errors

            raise errors.DuplicateError("already recorded; reverse first")
        for product in self._per_product:
            code = product["fuel_product_code"]
            cash = (cash_by_product or {}).get(code, 0)
            bank = (bank_by_product or {}).get(code, 0)
            other = (other_by_product or {}).get(code, 0)
            settled = cash + bank + product["paid_now_paisa"] + other
            product.update(
                {
                    "cash_paisa": cash,
                    "bank_paisa": bank,
                    "other_paisa": other,
                    "settled_paisa": settled,
                    "identity_ok": settled + product["new_receivables_paisa"]
                    == product["meter_revenue_paisa"],
                }
            )
            product["difference_paisa"] = (
                product["meter_revenue_paisa"]
                - settled
                - product["new_receivables_paisa"]
            )
        if not all(p["identity_ok"] for p in self._per_product):
            from app.services import errors

            raise errors.ValidationError("Settlement identity broken")
        self._recorded = True
        return "batch-1"

    def reverse_settlements(self, business_date, *, reason, actor=None):
        if not (reason or "").strip():
            raise ValueError("A reason is required")
        if not self._recorded:
            raise ValueError("No active settlement batch")
        self._recorded = False
        return "batch-2"

    # -- receipts / payments ----------------------------------------------
    def _row(self, kind, category, amount, **extra):
        n = self._next
        self._next += 1
        row = {
            "id": n,
            "uuid": f"{kind}-{n}",
            "business_date": DAY,
            "category": category,
            "category_en": category,
            "amount_paisa": amount,
            "effective_paisa": amount,
            "source_table": None,
            "source_id": None,
            "customer_id": None,
            "customer_name": None,
            "payee": None,
            "remarks": None,
            "status": "posted",
            "occurred_at_utc": f"{DAY}T10:00:00.000Z",
            "created_by": "test",
        }
        row.update(extra)
        return SimpleNamespace(**row)

    def record_receipt(
        self, *, business_date, category, amount_paisa, customer_id=None,
        remarks=None, occurred_at_utc=None, actor=None,
    ):
        if amount_paisa <= 0:
            raise ValueError("Receipt amount must be positive.")
        row = self._row(
            "receipt", category, amount_paisa, customer_id=customer_id,
            remarks=remarks,
        )
        self._receipts.append(row)
        return row

    def record_payment(
        self, *, business_date, category, amount_paisa, payee=None,
        remarks=None, occurred_at_utc=None, actor=None,
    ):
        if amount_paisa <= 0:
            raise ValueError("Payment amount must be positive.")
        row = self._row(
            "payment", category, amount_paisa, payee=payee, remarks=remarks,
        )
        self._payments.append(row)
        return row

    def reverse_cash_receipt(self, receipt_uuid, *, reason, actor=None):
        if not (reason or "").strip():
            raise ValueError("A reason is required")
        for row in self._receipts:
            if row.uuid == receipt_uuid:
                if row.status != "posted":
                    raise ValueError("not posted")
                row.status = "reversed"
                return row
        raise ValueError("unknown receipt")

    def reverse_cash_payment(self, payment_uuid, *, reason, actor=None):
        if not (reason or "").strip():
            raise ValueError("A reason is required")
        for row in self._payments:
            if row.uuid == payment_uuid:
                if row.status != "posted":
                    raise ValueError("not posted")
                row.status = "reversed"
                return row
        raise ValueError("unknown payment")

    def list_receipts(self, business_date):
        return [r for r in self._receipts if r.business_date == business_date]

    def list_payments(self, business_date):
        return [p for p in self._payments if p.business_date == business_date]

    def cashbook_entries(self, business_date):
        entries = []
        for row in self._receipts + self._payments:
            if row.business_date != business_date:
                continue
            kind = "receipt" if row.uuid.startswith("receipt") else "payment"
            signed = row.amount_paisa if kind == "receipt" else -row.amount_paisa
            entries.append(
                {
                    "seq": len(entries),
                    "business_date": row.business_date,
                    "occurred_at_utc": row.occurred_at_utc,
                    "kind": kind,
                    "category": row.category,
                    "category_en": row.category_en,
                    "customer_id": row.customer_id,
                    "payee": row.payee,
                    "signed_paisa": signed,
                    "remarks": row.remarks,
                    "uuid": row.uuid,
                }
            )
        entries.sort(key=lambda e: e["seq"])
        return [
            {k: v for k, v in e.items() if k != "seq"} for e in entries
        ]

    def missing_cash_postings(self, business_date):
        return []

    # -- cash position ------------------------------------------------------
    def cash_position(self, business_date):
        receipts = sum(
            r.amount_paisa for r in self._receipts if r.status == "posted"
        )
        payments = sum(
            p.amount_paisa for p in self._payments if p.status == "posted"
        )
        opening = 100_000
        expected = opening + receipts - payments
        return StubCashPosition(
            business_date=business_date,
            opening_cash_paisa=opening,
            receipts_paisa=receipts,
            payments_paisa=payments,
            expected_paisa=expected,
            physical_cash_paisa=None,
            difference_paisa=None,
            shortage_paisa=0,
            surplus_paisa=0,
        )


class StubExpenseService:
    def __init__(self):
        self._categories = [
            SimpleNamespace(
                id=1, name_en="Fuel purchase", name_ur="ایندھن کی خرید",
                is_active=True,
            ),
            SimpleNamespace(
                id=2, name_en="Salaries", name_ur="تنخواہیں",
                is_active=True,
            ),
        ]
        self._expenses: list = []
        self._next = 1

    def list_categories(self, active_only=True):
        return [
            c for c in self._categories if c.is_active or not active_only
        ]

    def add_category(self, name_en, name_ur=None, *, actor=None):
        clean = (name_en or "").strip()
        if not clean:
            raise ValueError("Category name must not be empty.")
        if any(c.name_en == clean for c in self._categories):
            from app.services import errors

            raise errors.DuplicateError(f"already exists: {clean!r}")
        cid = max(c.id for c in self._categories) + 1
        self._categories.append(
            SimpleNamespace(
                id=cid, name_en=clean, name_ur=name_ur, is_active=True,
            )
        )
        return cid

    def set_category_active(self, category_id, active, *, actor=None):
        for c in self._categories:
            if c.id == category_id:
                c.is_active = bool(active)
                return
        raise ValueError("Unknown expense category")

    def record_expense(
        self, *, business_date, category, amount_paisa, method="cash",
        payee=None, reference=None, occurred_at_utc=None, actor=None,
    ):
        if amount_paisa <= 0:
            raise ValueError("Expense amount must be positive.")
        if method not in ("cash", "bank", "other"):
            raise ValueError("Unknown payment method")
        n = self._next
        self._next += 1
        cat = next(c for c in self._categories if c.id == category)
        row = SimpleNamespace(
            id=n,
            uuid=f"expense-{n}",
            business_date=business_date,
            category_id=cat.id,
            category_en=cat.name_en,
            amount_paisa=amount_paisa,
            effective_paisa=amount_paisa,
            method=method,
            payee=payee,
            reference=reference,
            status="posted",
            occurred_at_utc=f"{business_date}T10:00:00.000Z",
            created_by="test",
        )
        self._expenses.append(row)
        return row

    def reverse_expense(self, expense_uuid, *, reason, actor=None):
        if not (reason or "").strip():
            raise ValueError("A reason is required")
        for row in self._expenses:
            if row.uuid == expense_uuid:
                if row.status != "posted":
                    raise ValueError("not posted")
                row.status = "reversed"
                row.effective_paisa = 0
                return row
        raise ValueError("unknown expense")

    def list_expenses(self, *, business_date=None, category_id=None):
        return [
            e
            for e in self._expenses
            if (business_date is None or e.business_date == business_date)
            and (category_id is None or e.category_id == category_id)
        ]

    def expense_totals(self, business_date):
        by_category: dict[str, int] = {}
        for e in self._expenses:
            if e.business_date == business_date and e.status == "posted":
                by_category[e.category_en] = (
                    by_category.get(e.category_en, 0) + e.amount_paisa
                )
        return {
            "business_date": business_date,
            "by_category": [
                {"category_en": k, "total_paisa": v}
                for k, v in by_category.items()
            ],
            "total_paisa": sum(by_category.values()),
        }


class StubDeliveryService:
    def __init__(self):
        self._suppliers = [
            SimpleNamespace(
                id=1, name="Test Supplier", phone=None, address=None,
                is_active=True,
            )
        ]
        self._next = 2

    def list_suppliers(self, active_only=True):
        return [
            s for s in self._suppliers if s.is_active or not active_only
        ]

    def add_supplier(self, name, phone=None, address=None, *, actor=None):
        clean = (name or "").strip()
        if not clean:
            raise ValueError("Supplier name must not be empty.")
        sid = self._next
        self._next += 1
        self._suppliers.append(
            SimpleNamespace(
                id=sid, name=clean, phone=phone, address=address,
                is_active=True,
            )
        )
        return sid


class StubSupplierService:
    def __init__(self, deliveries: StubDeliveryService):
        self._deliveries = deliveries
        self._payments: list = []
        self._next = 1

    def record_supplier_payment(
        self, *, supplier, business_date, amount_paisa, method="cash",
        delivery_id=None, occurred_at_utc=None, actor=None,
    ):
        if amount_paisa <= 0:
            raise ValueError("Payment amount must be positive.")
        suppliers = {s.id: s for s in self._deliveries._suppliers}
        if supplier not in suppliers:
            raise ValueError("Unknown supplier")
        n = self._next
        self._next += 1
        row = SimpleNamespace(
            id=n,
            uuid=f"suppay-{n}",
            supplier_id=supplier,
            supplier_name=suppliers[supplier].name,
            delivery_id=delivery_id,
            business_date=business_date,
            amount_paisa=amount_paisa,
            effective_paisa=amount_paisa,
            method=method,
            status="posted",
            occurred_at_utc=f"{business_date}T10:00:00.000Z",
            created_by="test",
        )
        self._payments.append(row)
        return row

    def reverse_supplier_payment(self, payment_uuid, *, reason, actor=None):
        if not (reason or "").strip():
            raise ValueError("A reason is required")
        for row in self._payments:
            if row.uuid == payment_uuid:
                if row.status != "posted":
                    raise ValueError("not posted")
                row.status = "reversed"
                row.effective_paisa = 0
                return row
        raise ValueError("unknown payment")

    def supplier_balances(self):
        return [
            SimpleNamespace(
                supplier_id=s.id,
                supplier_name=s.name,
                invoiced_paisa=50_000,
                paid_at_delivery_paisa=0,
                paid_paisa=sum(
                    p.amount_paisa
                    for p in self._payments
                    if p.supplier_id == s.id and p.status == "posted"
                ),
                outstanding_paisa=50_000
                - sum(
                    p.amount_paisa
                    for p in self._payments
                    if p.supplier_id == s.id and p.status == "posted"
                ),
            )
            for s in self._deliveries._suppliers
        ]

    def delivery_outstanding(self, delivery_id):
        return 50_000

    def list_payments(self, *, supplier_id=None, business_date=None):
        return [
            p
            for p in self._payments
            if (supplier_id is None or p.supplier_id == supplier_id)
            and (business_date is None or p.business_date == business_date)
        ]

    def deliveries_with_outstanding(self, supplier_id=None):
        return [
            {
                "id": 7,
                "uuid": "delivery-7",
                "supplier_id": 1,
                "supplier_name": "Test Supplier",
                "fuel_product_code": "PETROL",
                "tank_code": "T1",
                "business_date": DAY,
                "quantity_ml": 1_000_000,
                "unit_cost_10k": 2_600_000,
                "total_cost_paisa": 50_000,
                "other_cost_paisa": 0,
                "supplier_invoice_no": "INV-7",
                "amount_paid_paisa": 0,
                "payment_status": "unpaid",
                "outstanding_paisa": 50_000,
            }
        ]


class StubAccountingService:
    def daily_pnl(self, business_date):
        return SimpleNamespace(
            period_label=business_date,
            total_revenue_paisa=2_000_000,
            gross_profit_paisa=150_000,
            net_profit_paisa=100_000,
            receivables_created_paisa=300_000,
            estimated=True,
            estimate_reasons=["no cost basis"],
            source="live",
        )

    def period_pnl(self, start_date, end_date):
        return self.daily_pnl(start_date)


class StubOtherMaterialsService:
    def __init__(self):
        self._products: list = []
        self._txns: list = []
        self._next_product = 1
        self._next_txn = 1

    def list_products(self, active_only=True):
        return [
            p for p in self._products if p.is_active or not active_only
        ]

    def add_product(self, name, unit="piece", opening_value_paisa=0, *,
                    actor=None):
        clean = (name or "").strip()
        if not clean:
            raise ValueError("Product name must not be empty.")
        pid = self._next_product
        self._next_product += 1
        self._products.append(
            SimpleNamespace(
                id=pid, name=clean, unit=(unit or "piece").strip(),
                opening_value_paisa=opening_value_paisa,
                is_active=True, on_hand_units=0,
            )
        )
        return pid

    def set_product_active(self, product_id, active, *, actor=None):
        for p in self._products:
            if p.id == product_id:
                p.is_active = bool(active)
                return
        raise ValueError("Unknown product")

    def _txn(self, txn_type, product_id, quantity, unit_price, method,
             reference, reason=None):
        product = next(p for p in self._products if p.id == product_id)
        n = self._next_txn
        self._next_txn += 1
        total = quantity * unit_price if txn_type != "adjustment" else 0
        txn = SimpleNamespace(
            id=n,
            uuid=f"om-{n}",
            business_date=DAY,
            product_id=product_id,
            product_name=product.name,
            txn_type=txn_type,
            quantity_units=quantity,
            unit_price_paisa=unit_price,
            total_paisa=total,
            method=method,
            reference=reference,
            reason=reason,
            occurred_at_utc=f"{DAY}T10:00:00.000Z",
            created_by="test",
        )
        self._txns.append(txn)
        if txn_type == "purchase":
            product.on_hand_units += quantity
        elif txn_type == "sale":
            product.on_hand_units -= quantity
        else:
            product.on_hand_units += quantity
        return txn

    def record_purchase(
        self, *, product, business_date, quantity_units, unit_price_paisa,
        method="cash", reference=None, occurred_at_utc=None, actor=None,
    ):
        if quantity_units <= 0 or unit_price_paisa <= 0:
            raise ValueError("quantity and price must be positive")
        return self._txn(
            "purchase", product, quantity_units, unit_price_paisa, method,
            reference,
        )

    def record_sale(
        self, *, product, business_date, quantity_units, unit_price_paisa,
        method="cash", reference=None, occurred_at_utc=None, actor=None,
    ):
        if quantity_units <= 0 or unit_price_paisa <= 0:
            raise ValueError("quantity and price must be positive")
        return self._txn(
            "sale", product, quantity_units, unit_price_paisa, method,
            reference,
        )

    def record_adjustment(
        self, *, product, business_date, quantity_units, reason,
        reference=None, occurred_at_utc=None, actor=None,
    ):
        if quantity_units == 0:
            raise ValueError("adjustment cannot be zero")
        if not (reason or "").strip():
            raise ValueError("A reason is required")
        return self._txn(
            "adjustment", product, quantity_units, 0, "other", reference,
            reason=reason,
        )

    def stock(self, product_id=None):
        products = (
            [p for p in self._products if p.id == product_id]
            if product_id is not None
            else list(self._products)
        )
        return [SimpleNamespace(**vars(p)) for p in products]

    def list_transactions(self, *, product_id=None, business_date=None):
        return [
            t
            for t in self._txns
            if (product_id is None or t.product_id == product_id)
            and (business_date is None or t.business_date == business_date)
        ]


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
    """Auto-drive modal dialogs and message boxes.

    Fills text fields, clicks each dialog's own action buttons, then
    presses OK — exactly like a fast operator would.
    """
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *args, **kwargs: QMessageBox.Yes,
    )

    def fake_exec(self):
        composite = (
            QtWidgets.QDateEdit,
            QtWidgets.QDateTimeEdit,
            QtWidgets.QComboBox,
            QtWidgets.QAbstractSpinBox,
        )

        def is_internal_editor(widget):
            """A QLineEdit owned by a composite widget (date edit, editable
            combo, spin box) — filling those would corrupt the control."""
            ancestor = widget.parent()
            while ancestor is not None and ancestor is not self:
                if isinstance(ancestor, composite):
                    return True
                ancestor = ancestor.parent()
            return False

        for child in self.findChildren(QtWidgets.QLineEdit):
            if not is_internal_editor(child) and not child.text():
                child.setText("100")
        for child in self.findChildren(QtWidgets.QTextEdit):
            if not child.toPlainText():
                child.setPlainText("ui test reason")
        add_supplier = self.findChild(QPushButton, "add_supplier_btn")
        if add_supplier is not None:
            add_supplier.click()
        box = self.findChild(QtWidgets.QDialogButtonBox)
        save_btn = self.findChild(QPushButton, "save_btn")
        if save_btn is not None:
            save_btn.click()
        elif box is not None:
            ok = box.button(QtWidgets.QDialogButtonBox.Ok)
            if ok is not None:
                ok.click()
        return QDialog.Accepted

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    set_language("en")
    yield
    set_language("en")


@pytest.fixture
def day_stub():
    return StubDayService()


@pytest.fixture
def no_day_stub():
    return StubNoDayService()


@pytest.fixture
def cash_stub():
    return StubCashbookService()


@pytest.fixture
def expense_stub():
    return StubExpenseService()


@pytest.fixture
def deliveries_stub():
    return StubDeliveryService()


@pytest.fixture
def supplier_stub(deliveries_stub):
    return StubSupplierService(deliveries_stub)


@pytest.fixture
def materials_stub():
    return StubOtherMaterialsService()


@pytest.fixture
def accounting_stub():
    return StubAccountingService()


@pytest.fixture
def cash_page(qapp, dialogs, cash_stub, day_stub):
    page = CashBookPage(cash_stub, day_stub)
    page.show()
    yield page
    page.close()


@pytest.fixture
def expense_page(qapp, dialogs, expense_stub, day_stub):
    page = ExpensePage(expense_stub, day_stub)
    page.show()
    yield page
    page.close()


@pytest.fixture
def supplier_page(qapp, dialogs, supplier_stub, deliveries_stub, day_stub):
    page = SupplierPage(supplier_stub, deliveries_stub, day_stub)
    page.show()
    yield page
    page.close()


@pytest.fixture
def materials_page(qapp, dialogs, materials_stub, day_stub):
    page = OtherMaterialsPage(materials_stub, day_stub)
    page.show()
    yield page
    page.close()


# ---------------------------------------------------------------------------
# Stub-service chains
# ---------------------------------------------------------------------------
def test_cashbook_chain_receipt_payment_reverse(
    qapp, dialogs, cash_page, cash_stub
):
    # 1. record a receipt through the dialog (category = cash_sales)
    cash_page._record_receipt_btn.click()
    receipts = cash_stub.list_receipts(DAY)
    assert len(receipts) == 1
    assert receipts[0].category == "cash_sales"
    assert receipts[0].amount_paisa == 10_000
    # The fixture typed "100" into the customer-id field — the stub
    # accepted customer 100 verbatim; the dialog passes it through.
    assert receipts[0].customer_id == 100
    assert cash_page._receipts_table.rowCount() == 1
    assert cash_page._receipts_table.item(0, 2).text() == "100.00"
    assert cash_page._receipts_table.item(0, 6).text() == tr("cb.status_posted")

    # 2. record a payment through the dialog
    cash_page._record_payment_btn.click()
    payments = cash_stub.list_payments(DAY)
    assert len(payments) == 1
    assert payments[0].category == "expense"
    assert payments[0].amount_paisa == 10_000
    assert cash_page._payments_table.rowCount() == 1

    # 3. reverse the receipt (reason auto-filled by the fixture)
    cash_page.refresh()
    cash_page._receipts_table.setCurrentCell(0, 0)
    cash_page._reverse_receipt_btn.click()
    assert receipts[0].status == "reversed"
    cash_page.refresh()
    assert cash_page._receipts_table.item(0, 6).text() == tr(
        "cb.status_reversed"
    )

    # 4. the chronological entries show the signed audit trail
    cash_page.refresh()
    assert cash_page._entries_table.rowCount() == 2
    # receipt first (+100), payment second (−100)
    assert cash_page._entries_table.item(0, 3).text() == "100.00"
    assert cash_page._entries_table.item(1, 3).text() == "-100.00"
    # cash position from the stub double (reversal nets out)
    assert cash_page._opening_value.text() == "1,000.00"
    assert cash_page._receipts_value.text() == "0.00"
    assert cash_page._expected_value.text() == "900.00"


def test_cashbook_settlements_record_and_reverse(
    qapp, dialogs, cash_page, cash_stub
):
    cash_page.refresh()
    assert cash_page._settle_table.rowCount() == 1
    assert cash_page._settle_banner.text() == tr("cb.not_recorded")

    # Drive the settle dialog manually: the stub validates the identity
    # exactly like the real service (20,000 PKR meter revenue).
    dlg = _SettleDialog(cash_page, cash_stub, DAY)
    assert len(dlg._rows) == 1
    dlg._rows[0]["cash_edit"].setText("20000")
    dlg._rows[0]["bank_edit"].setText("0")
    dlg._rows[0]["other_edit"].setText("0")
    dlg._on_accept()  # the confirm question is auto-answered Yes
    assert cash_stub._recorded
    cash_page.refresh()
    assert cash_page._settle_banner.text() == tr("cb.identity_banner_ok")
    assert cash_page._settle_table.item(0, 2).text() == "20,000.00"
    assert cash_page._settle_table.item(0, 8).text() == tr("cb.identity_ok")
    dlg.close()

    # Duplicate recording raises — the page surfaces the service error and
    # keeps the existing batch.
    cash_page._record_settle_btn.click()
    assert cash_stub._recorded

    # Reverse the batch (reason auto-filled).
    cash_page._reverse_settle_btn.click()
    assert not cash_stub._recorded
    cash_page.refresh()
    assert cash_page._settle_banner.text() == tr("cb.not_recorded")


def test_cashbook_receipt_validation_keeps_dialog(
    qapp, dialogs, cash_page, cash_stub
):
    dlg = _CashMoveDialog(cash_page, cash_stub, DAY, "receipt")
    try:
        dlg._amount_edit.setText("")  # invalid amount
        dlg._on_accept()
        assert cash_stub.list_receipts(DAY) == []
        assert dlg.result() == 0  # dialog did not accept
    finally:
        dlg.close()


def test_expenses_chain(qapp, dialogs, expense_page, expense_stub):
    # 1. add a category through the dialog
    expense_page._add_category_btn.click()
    categories = expense_stub.list_categories(active_only=False)
    assert len(categories) == 3
    assert categories[-1].name_en == "100"
    assert expense_page._cat_table.rowCount() == 3

    # 2. deactivate it, then re-activate
    expense_page.refresh()
    expense_page._cat_table.setCurrentCell(2, 0)
    expense_page._toggle_category_btn.click()
    assert categories[-1].is_active is False
    expense_page.refresh()
    assert expense_page._toggle_category_btn.text() == tr("exp.activate")
    expense_page._toggle_category_btn.click()
    assert categories[-1].is_active is True

    # 3. record an expense (cash method; the hint text is displayed)
    expense_page._record_btn.click()
    expenses = expense_stub.list_expenses(business_date=DAY)
    assert len(expenses) == 1
    assert expenses[0].method == "cash"
    assert expenses[0].amount_paisa == 10_000
    expense_page.refresh()
    assert expense_page._exp_table.rowCount() == 1
    assert expense_page._exp_table.item(0, 1).text() == "100.00"
    # totals come from the service
    assert "100.00" in expense_page._totals_label.text()

    # 4. reverse it (reason auto-filled)
    expense_page._exp_table.setCurrentCell(0, 0)
    expense_page._reverse_btn.click()
    assert expenses[0].status == "reversed"
    expense_page.refresh()
    assert expense_page._exp_table.item(0, 5).text() == tr(
        "exp.status_reversed"
    )


def test_suppliers_chain(qapp, dialogs, supplier_page, supplier_stub):
    # Record a payment through the dialog: the fixture also clicks the
    # dialog's own "Add supplier" button (auto-filled name "100").
    supplier_page._record_btn.click()
    payments = supplier_stub.list_payments(business_date=DAY)
    assert len(payments) == 1
    assert payments[0].supplier_id == 1  # first supplier selected
    assert payments[0].amount_paisa == 10_000
    assert payments[0].method == "cash"
    # A new supplier was added through the dialog's add-supplier button.
    assert len(supplier_page._deliveries._suppliers) == 2
    supplier_page.refresh()
    assert supplier_page._payments_table.rowCount() == 1
    assert supplier_page._payments_table.item(0, 2).text() == "100.00"

    # The balances table shows the service-computed outstanding.
    assert supplier_page._balances_table.rowCount() == 2
    assert supplier_page._balances_table.item(0, 4).text() == "400.00"

    # Reverse the payment (reason auto-filled).
    supplier_page._payments_table.setCurrentCell(0, 0)
    supplier_page._reverse_btn.click()
    assert payments[0].status == "reversed"
    supplier_page.refresh()
    assert supplier_page._payments_table.item(0, 5).text() == tr(
        "sup.status_reversed"
    )


def test_supplier_payment_dialog_add_supplier(
    qapp, dialogs, supplier_page, supplier_stub, deliveries_stub
):
    before = len(deliveries_stub.list_suppliers())
    dlg = _RecordPaymentDialog(
        supplier_page, supplier_stub, deliveries_stub, DAY
    )
    try:
        assert dlg._supplier_combo.count() == before
        # The dialog's own add-supplier button opens a nested dialog; the
        # fixture fills name "100" and presses OK.
        dlg._add_supplier_btn.click()
        assert len(deliveries_stub.list_suppliers()) == before + 1
        assert dlg._supplier_combo.count() == before + 1
    finally:
        dlg.close()


def test_other_materials_chain(qapp, dialogs, materials_page, materials_stub):
    # 1. add a product through the dialog
    materials_page._add_product_btn.click()
    products = materials_stub.list_products(active_only=False)
    assert len(products) == 1
    assert products[0].name == "100"
    materials_page.refresh()
    assert materials_page._products_table.rowCount() == 1

    # 2. purchase 100 units @ 100 PKR through the dialog
    materials_page._purchase_btn.click()
    txns = materials_stub.list_transactions(business_date=DAY)
    assert len(txns) == 1
    assert txns[0].txn_type == "purchase"
    assert txns[0].quantity_units == 100
    assert txns[0].total_paisa == 1_000_000
    materials_page.refresh()
    assert materials_page._products_table.item(0, 2).text() == "100"
    assert materials_page._txns_table.rowCount() == 1
    assert materials_page._txns_table.item(0, 1).text() == tr(
        "om.type_purchase"
    )
    assert materials_page._txns_table.item(0, 4).text() == "10,000.00"

    # 3. sell 100 units (stock back to zero)
    materials_page._sale_btn.click()
    materials_page.refresh()
    assert materials_page._products_table.item(0, 2).text() == "0"
    assert materials_page._txns_table.rowCount() == 2

    # 4. signed adjustment (+100) through the dialog — the fixture fills
    #    "100" into the signed quantity field.
    dlg = _TxnDialog(materials_page, materials_stub, DAY, "adjustment")
    dlg._qty_edit.setText("100")
    dlg._reason_edit.setText("count correction")
    dlg._on_accept()
    assert materials_stub.stock()[0].on_hand_units == 100
    materials_page.refresh()
    assert materials_page._txns_table.rowCount() == 3
    assert materials_page._txns_table.item(2, 1).text() == tr(
        "om.type_adjustment"
    )
    dlg.close()


def test_no_open_day_disables_writes(
    qapp, dialogs, cash_stub, expense_stub, supplier_stub, deliveries_stub,
    materials_stub, no_day_stub,
):
    cash = CashBookPage(cash_stub, no_day_stub)
    expenses = ExpensePage(expense_stub, no_day_stub)
    suppliers = SupplierPage(supplier_stub, deliveries_stub, no_day_stub)
    materials = OtherMaterialsPage(materials_stub, no_day_stub)
    try:
        for page in (cash, expenses, suppliers, materials):
            page.show()
            page.refresh()
        # Every write button is disabled and the hint is shown.
        for btn in (
            cash._record_settle_btn,
            cash._reverse_settle_btn,
            cash._record_receipt_btn,
            cash._reverse_receipt_btn,
            cash._record_payment_btn,
            cash._reverse_payment_btn,
            expenses._add_category_btn,
            expenses._toggle_category_btn,
            expenses._record_btn,
            expenses._reverse_btn,
            suppliers._record_btn,
            suppliers._reverse_btn,
            materials._add_product_btn,
            materials._toggle_product_btn,
            materials._purchase_btn,
            materials._sale_btn,
            materials._adjustment_btn,
        ):
            assert not btn.isEnabled()
        assert cash._settle_banner.text() == tr("dash.open_day_hint")
    finally:
        for page in (cash, expenses, suppliers, materials):
            page.close()


def test_language_switch_to_urdu(
    qapp,
    dialogs,
    cash_stub,
    expense_stub,
    supplier_stub,
    deliveries_stub,
    materials_stub,
    day_stub,
):
    set_language("ur")
    cash = CashBookPage(cash_stub, day_stub)
    expenses = ExpensePage(expense_stub, day_stub)
    suppliers = SupplierPage(supplier_stub, deliveries_stub, day_stub)
    materials = OtherMaterialsPage(materials_stub, day_stub)
    try:
        for page in (cash, expenses, suppliers, materials):
            page.show()
            page.refresh()
        assert cash._title.text() == "کیش بک"
        assert expenses._title.text() == "اخراجات"
        assert suppliers._title.text() == "سپلائرز"
        assert materials._title.text() == "دیگر اشیاء"
        assert cash._record_settle_btn.text() == "تصفیہ درج کریں"
        assert expenses._record_btn.text() == "خرچ درج کریں"
        assert suppliers._record_btn.text() == "ادائیگی درج کریں"
        assert materials._purchase_btn.text() == "خرید درج کریں"
    finally:
        for page in (cash, expenses, suppliers, materials):
            page.close()
        set_language("en")


# ---------------------------------------------------------------------------
# Real services on a temp DB
# ---------------------------------------------------------------------------
@needs_real
def test_real_phase5_pages(qapp, dialogs, tmp_path):
    from app.bootstrap import bootstrap
    from app.services.accounting_service import AccountingService
    from app.services.business_day_service import BusinessDayService
    from app.services.cashbook_service import CashbookService
    from app.services.delivery_service import DeliveryService
    from app.services.expense_service import ExpenseService
    from app.services.other_materials_service import OtherMaterialsService
    from app.services.supplier_service import SupplierService
    from app.ui.dashboard.page import DashboardPage

    today = QtCore.QDate.currentDate().toString("yyyy-MM-dd")
    result = bootstrap(tmp_path / "phase5ui.sqlite3")
    try:
        dbm = result.db_manager
        days = BusinessDayService(dbm)
        days.open_day(today, opening_cash_paisa=50_000)
        cashbook = CashbookService(dbm)
        expenses = ExpenseService(dbm)
        suppliers = SupplierService(dbm)
        deliveries = DeliveryService(dbm)
        materials = OtherMaterialsService(dbm)
        accounting = AccountingService(dbm)

        # -- cash book: receipt + payment + reversal ---------------------
        receipt = cashbook.record_receipt(
            business_date=today, category="other_receipt",
            amount_paisa=5_000, remarks="test receipt",
        )
        assert h.dto_field(receipt, "amount_paisa") == 5_000
        cashbook.record_payment(
            business_date=today, category="other_payment",
            amount_paisa=2_000, payee="test payee",
        )
        cash_page = CashBookPage(cashbook, days)
        cash_page.show()
        try:
            cash_page.refresh()
            assert cash_page._receipts_table.rowCount() == 1
            assert cash_page._payments_table.rowCount() == 1
            assert cash_page._opening_value.text() == "500.00"
            assert cash_page._receipts_value.text() == "50.00"
            assert cash_page._payments_value.text() == "20.00"
            assert cash_page._expected_value.text() == "530.00"
            # Entries show the chronological audit trail.
            assert cash_page._entries_table.rowCount() == 2
            # Reverse the receipt through the page.
            cash_page._receipts_table.setCurrentCell(0, 0)
            cash_page._reverse_receipt_btn.click()
            cash_page.refresh()
            assert cash_page._receipts_table.item(0, 6).text() == tr(
                "cb.status_reversed"
            )
            assert cash_page._receipts_value.text() == "0.00"
        finally:
            cash_page.close()

        # -- settlements: the page renders the service's identity table --
        assert cashbook.settlement_status(today)["recorded"] is False

        # -- expenses: record + totals -----------------------------------
        expense = expenses.record_expense(
            business_date=today, category="Fuel purchase",
            amount_paisa=12_000, method="cash", payee=" PSO ".strip(),
        )
        assert h.dto_field(expense, "amount_paisa") == 12_000
        exp_page = ExpensePage(expenses, days)
        exp_page.show()
        try:
            exp_page.refresh()
            assert exp_page._exp_table.rowCount() == 1
            assert exp_page._exp_table.item(0, 0).text() == "Fuel purchase"
            assert "120.00" in exp_page._totals_label.text()
            exp_page._exp_table.setCurrentCell(0, 0)
            exp_page._reverse_btn.click()
            exp_page.refresh()
            assert exp_page._exp_table.item(0, 5).text() == tr(
                "exp.status_reversed"
            )
        finally:
            exp_page.close()

        # -- suppliers: payment + reverse --------------------------------
        supplier_id = deliveries.add_supplier("Real Supplier")
        suppliers.record_supplier_payment(
            supplier=supplier_id, business_date=today,
            amount_paisa=25_000, method="cash",
        )
        assert len(suppliers.list_payments(business_date=today)) == 1
        sup_page = SupplierPage(suppliers, deliveries, days)
        sup_page.show()
        try:
            sup_page.refresh()
            assert sup_page._payments_table.rowCount() == 1
            assert sup_page._payments_table.item(0, 0).text() == "Real Supplier"
            balances = suppliers.supplier_balances()
            assert any(
                h.dto_field(b, "supplier_name") == "Real Supplier"
                for b in balances
            )
            sup_page._payments_table.setCurrentCell(0, 0)
            sup_page._reverse_btn.click()
            assert (
                suppliers.list_payments(business_date=today)[0].status
                == "reversed"
            )
        finally:
            sup_page.close()

        # -- other materials: product + purchase + stock -----------------
        product_id = materials.add_product("Engine Oil", unit="bottle")
        materials.record_purchase(
            product=product_id, business_date=today, quantity_units=10,
            unit_price_paisa=10_000, method="cash",
        )
        stock = materials.stock(product_id)
        assert h.dto_field(stock[0], "on_hand_units") == 10
        mat_page = OtherMaterialsPage(materials, days)
        mat_page.show()
        try:
            mat_page.refresh()
            assert mat_page._products_table.rowCount() == 1
            assert mat_page._products_table.item(0, 2).text() == "10"
            assert mat_page._txns_table.rowCount() == 1
            assert mat_page._txns_table.item(0, 1).text() == tr(
                "om.type_purchase"
            )
        finally:
            mat_page.close()

        # -- dashboard: cash card from the real services -------------------
        from app.services.nozzle_service import NozzleService

        dash = DashboardPage(
            days,
            NozzleService(dbm),
            lambda index: None,
            cashbook_service=cashbook,
            accounting_service=accounting,
        )
        dash.show()
        try:
            dash.refresh()
            # Cash card shows physical cash as the value and the expected
            # figure in the sub-line. Expected = 500 opening + (50 − 50)
            # receipts − (20 + (120 − 120) + (250 − 250) + 1000) payments
            # = −520.00 (every reversed row nets to 0 in the effective
            # figures).
            assert tr("dash2.expected") in dash._card_cash.sub.text()
            assert "-520.00" in dash._card_cash.sub.text()
            assert dash._open_sheet_btn.text() == tr("dash2.open_daily_sheet")
        finally:
            dash.close()
    finally:
        result.db_manager.close()
