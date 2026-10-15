"""Offscreen UI interaction tests (Phase 4): customer credit + payments.

Drives the real pages through their actual buttons/dialogs. Two layers:

* Stub services implementing the published Phase-4 contracts — these run
  NOW and prove the pages wire every control to a service call.
* The REAL services on a temp DB (bootstrap) — skipped until the parallel
  services agent lands ``customer_credit_service`` / ``payment_service``.

Modal dialogs are auto-driven via a monkeypatched QDialog.exec that fills
fields and clicks the dialog's own buttons, so the offscreen run never
blocks. Requires Qt; skips cleanly when Qt cannot initialize here.
"""

from __future__ import annotations

import importlib.util
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
QtCore = pytest.importorskip("PySide6.QtCore", exc_type=ImportError)

from app.calculations import money as calc
from app.localization import set_language, tr
from app.ui import _helpers as h
from app.ui.customers.page import (
    CustomerCreditPage,
    _CustomerDialog,
    _IssueTicketDialog,
)
from app.ui.payments.page import (
    CustomerPaymentsPage,
    _AllocateDialog,
    _ReallocateDialog,
    _ReceiptDialog,
    _RecordPaymentDialog,
)

QApplication = QtWidgets.QApplication
QMessageBox = QtWidgets.QMessageBox
QDialog = QtWidgets.QDialog
QPushButton = QtWidgets.QPushButton
QDate = QtCore.QDate

DAY = "2026-10-12"

#: Real Phase-4 services present? (parallel agent)
_REAL_SERVICES = (
    importlib.util.find_spec("app.services.customer_credit_service") is not None
    and importlib.util.find_spec("app.services.payment_service") is not None
)
needs_real = pytest.mark.skipif(
    not _REAL_SERVICES, reason="Phase-4 services not landed yet"
)


# ---------------------------------------------------------------------------
# Stub DTOs (exact contract field names)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CustomerInfo:
    id: int
    customer_code: str
    name: str
    business_name: str | None = None
    phone: str | None = None
    address: str | None = None
    opening_receivable_paisa: int = 0
    credit_limit_paisa: int | None = None
    status: str = "active"
    notes: str | None = None
    outstanding_paisa: int = 0


@dataclass(frozen=True)
class TicketLineInfo:
    fuel_product_code: str
    nozzle_code: str | None
    quantity_ml: int
    unit_price_10k: int
    line_amount_paisa: int


@dataclass(frozen=True)
class TicketInfo:
    id: int
    uuid: str
    customer_id: int
    customer_code: str
    business_date: str
    gross_amount_paisa: int
    paid_now_paisa: int
    unpaid_paisa: int
    receipt_no: str | None
    status: str
    lines: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


@dataclass(frozen=True)
class AllocationInfo:
    id: int
    ticket_uuid: str
    ticket_receipt_no: str | None
    amount_paisa: int
    effective_paisa: int = 0


@dataclass(frozen=True)
class PaymentInfo:
    id: int
    uuid: str
    customer_id: int
    customer_code: str
    business_date: str
    amount_paisa: int
    method: str
    receipt_no: str | None
    status: str
    allocations: list = field(default_factory=list)
    unallocated_paisa: int = 0


@dataclass(frozen=True)
class LedgerEntry:
    occurred_at_utc: str
    business_date: str | None
    entry_type: str
    description: str
    debit_paisa: int
    credit_paisa: int
    running_paisa: int


@dataclass(frozen=True)
class CustomerStatement:
    customer: object
    entries: list
    total_debit_paisa: int
    total_credit_paisa: int
    closing_paisa: int


@dataclass(frozen=True)
class NozzleStub:
    code: str
    fuel_product_code: str


# ---------------------------------------------------------------------------
# Stub services (in-memory, contract-shaped)
# ---------------------------------------------------------------------------
class StubCreditService:
    """Test double for CustomerCreditService (balance math lives here, in
    the double — the UI under test does none)."""

    PRICES_10K = {"PETROL": 2_659_630, "DIESEL": 2_577_400}

    def __init__(self):
        self._customers: dict[int, dict] = {}
        self._tickets: dict[str, dict] = {}
        self._adjustments: list[tuple[int, int]] = []
        self._next_customer = 1
        self._next_ticket = 1
        self.payment_stub = None  # wired by the fixture

    # -- internal ------------------------------------------------------
    def _info(self, cid: int) -> CustomerInfo:
        c = self._customers[cid]
        return CustomerInfo(
            id=cid,
            customer_code=c["code"],
            name=c["name"],
            business_name=c["business_name"],
            phone=c["phone"],
            address=c["address"],
            opening_receivable_paisa=c["opening"],
            credit_limit_paisa=c["limit"],
            status=c["status"],
            notes=c["notes"],
            outstanding_paisa=self.outstanding(cid),
        )

    def _ticket_info(self, t: dict) -> TicketInfo:
        return TicketInfo(
            id=t["n"],
            uuid=t["uuid"],
            customer_id=t["customer_id"],
            customer_code=self._customers[t["customer_id"]]["code"],
            business_date=t["business_date"],
            gross_amount_paisa=t["gross"],
            paid_now_paisa=t["paid_now"],
            unpaid_paisa=t["gross"] - t["paid_now"],
            receipt_no=t["receipt_no"],
            status=t["status"],
            lines=[
                TicketLineInfo(
                    fuel_product_code=ln["product"],
                    nozzle_code=ln["nozzle"],
                    quantity_ml=ln["ml"],
                    unit_price_10k=ln["price_10k"],
                    line_amount_paisa=ln["amount"],
                )
                for ln in t["lines"]
            ],
            warnings=list(t["warnings"]),
        )

    # -- customers ------------------------------------------------------
    def create_customer(
        self,
        name,
        *,
        business_name=None,
        phone=None,
        address=None,
        opening_receivable_paisa=0,
        credit_limit_paisa=None,
        notes=None,
        actor=None,
    ):
        if not (name or "").strip():
            raise ValueError("customer name is required")
        if opening_receivable_paisa < 0:
            raise ValueError("opening receivable cannot be negative")
        cid = self._next_customer
        self._next_customer += 1
        self._customers[cid] = {
            "code": f"C{cid:03d}",
            "name": name.strip(),
            "business_name": business_name,
            "phone": phone,
            "address": address,
            "opening": opening_receivable_paisa,
            "limit": credit_limit_paisa,
            "status": "active",
            "notes": notes,
        }
        return self._info(cid)

    def update_customer(
        self,
        customer_id,
        *,
        name=None,
        business_name=None,
        phone=None,
        address=None,
        credit_limit_paisa=None,
        status=None,
        notes=None,
        clear_credit_limit=False,
        actor=None,
    ):
        c = self._customers[customer_id]
        if name is not None:
            if not name.strip():
                raise ValueError("customer name is required")
            c["name"] = name.strip()
        if business_name is not None:
            c["business_name"] = business_name
        if phone is not None:
            c["phone"] = phone
        if address is not None:
            c["address"] = address
        if clear_credit_limit:
            c["limit"] = None
        elif credit_limit_paisa is not None:
            c["limit"] = credit_limit_paisa
        if status is not None:
            c["status"] = status
        if notes is not None:
            c["notes"] = notes
        return self._info(customer_id)

    def get_customer(self, customer_id):
        return self._info(customer_id)

    def list_customers(self, active_only=True):
        return [
            self._info(cid)
            for cid, c in sorted(self._customers.items())
            if c["status"] == "active" or not active_only
        ]

    def adjust_opening_balance(
        self, customer_id, adjustment_paisa, reason, business_date, *,
        authorized_by=None, actor=None,
    ):
        if not (reason or "").strip():
            raise ValueError("a reason is required")
        if adjustment_paisa == 0:
            raise ValueError("adjustment cannot be zero")
        # The computed balance is never edited: only the audited row is
        # appended (mirrors v_outstanding_receivables).
        self._adjustments.append((customer_id, adjustment_paisa))

    # -- tickets ---------------------------------------------------------
    def issue_ticket(
        self, customer_id, business_date, lines, *, paid_now_paisa=0,
        occurred_at_utc=None, receipt_no=None, notes=None, actor=None,
    ):
        customer = self._customers.get(customer_id)
        if customer is None or customer["status"] != "active":
            raise ValueError("customer is unknown or inactive")
        if not lines:
            raise ValueError("at least one line is required")
        built = []
        gross = 0
        for line in lines:
            qty = line["quantity_litres"]
            if qty <= 0:
                raise ValueError("quantity must be positive")
            product = line["fuel_product_code"]
            if product not in self.PRICES_10K:
                raise ValueError(f"unknown fuel product {product!r}")
            ml = calc.to_ml(qty)
            price = self.PRICES_10K[product]
            amount = calc.amount_paisa_for(ml, price)
            gross += amount
            built.append(
                {
                    "product": product,
                    "nozzle": line.get("nozzle_code"),
                    "ml": ml,
                    "price_10k": price,
                    "amount": amount,
                }
            )
        if paid_now_paisa > gross:
            raise ValueError("paid-now cannot exceed the ticket gross")
        n = self._next_ticket
        self._next_ticket += 1
        warnings = []
        limit = customer["limit"]
        if limit is not None and self.outstanding(customer_id) + (gross - paid_now_paisa) > limit:
            warnings.append("Credit limit exceeded")
        ticket = {
            "n": n,
            "uuid": f"ticket-{n}",
            "receipt_no": receipt_no or f"T-{n:04d}",
            "customer_id": customer_id,
            "business_date": business_date,
            "gross": gross,
            "paid_now": paid_now_paisa,
            "status": "posted",
            "is_reversal": False,
            "lines": built,
            "warnings": warnings,
            "notes": notes,
        }
        self._tickets[ticket["uuid"]] = ticket
        return self._ticket_info(ticket)

    def reverse_ticket(self, ticket_uuid, reason, business_date, *, actor=None):
        if not (reason or "").strip():
            raise ValueError("a reason is required")
        t = self._tickets[ticket_uuid]
        if t["status"] != "posted":
            raise ValueError("ticket is not posted")
        t["status"] = "reversed"
        n = self._next_ticket
        self._next_ticket += 1
        reversal = {
            "n": n,
            "uuid": f"ticket-{n}",
            "receipt_no": f"T-{n:04d}",
            "customer_id": t["customer_id"],
            "business_date": business_date,
            "gross": t["gross"],
            "paid_now": 0,
            "status": "posted",
            "is_reversal": True,
            "lines": [],
            "warnings": [],
            "notes": f"reversal of {t['receipt_no']}: {reason}",
        }
        self._tickets[reversal["uuid"]] = reversal

    def ticket_details(self, ticket_uuid):
        return self._ticket_info(self._tickets[ticket_uuid])

    def day_tickets(self, business_date):
        return [
            self._ticket_info(t)
            for t in self._tickets.values()
            if t["business_date"] == business_date
        ]

    # -- balances / ledger ----------------------------------------------
    def outstanding(self, customer_id):
        c = self._customers[customer_id]
        total = c["opening"] + sum(
            amt for cid, amt in self._adjustments if cid == customer_id
        )
        for t in self._tickets.values():
            if t["customer_id"] != customer_id or t["status"] != "posted":
                continue
            if t["is_reversal"]:
                total -= t["gross"] - t["paid_now"]
            else:
                total += t["gross"] - t["paid_now"]
        if self.payment_stub is not None:
            total -= self.payment_stub.allocated_to_customer(customer_id)
        return total

    def outstanding_all(self):
        # Mirrors the real contract: a plain list of CustomerInfo, each
        # carrying its own outstanding_paisa.
        return [self._info(cid) for cid in sorted(self._customers)]

    def customer_ledger(self, customer_id):
        return self.statement(customer_id).entries

    def statement(self, customer_id, date_from=None, date_to=None):
        c = self._customers[customer_id]
        entries: list[LedgerEntry] = []
        running = 0

        def add(business_date, entry_type, description, debit, credit):
            nonlocal running
            running += debit - credit
            entries.append(
                LedgerEntry(
                    occurred_at_utc=f"{business_date or DAY}T00:00:00.000Z",
                    business_date=business_date,
                    entry_type=entry_type,
                    description=description,
                    debit_paisa=debit,
                    credit_paisa=credit,
                    running_paisa=running,
                )
            )

        add(None, "opening", "Opening balance", c["opening"], 0)
        for t in sorted(self._tickets.values(), key=lambda t: t["receipt_no"]):
            if t["customer_id"] != customer_id or t["status"] != "posted":
                continue
            if t["is_reversal"]:
                add(t["business_date"], "ticket", f"Reversal {t['receipt_no']}",
                    0, t["gross"] - t["paid_now"])
            else:
                add(t["business_date"], "ticket", f"Ticket {t['receipt_no']}",
                    t["gross"], t["paid_now"])
        if self.payment_stub is not None:
            for alloc, pay in self.payment_stub.effective_allocations():
                t = self._tickets.get(alloc["ticket_uuid"])
                if t is None or t["customer_id"] != customer_id:
                    continue
                add(pay["business_date"], "payment",
                    f"Payment {pay['receipt_no']} → {t['receipt_no']}",
                    0, alloc["amount_paisa"])
        for cid, amt in self._adjustments:
            if cid == customer_id:
                add(DAY, "adjustment", "Opening adjustment",
                    max(amt, 0), max(-amt, 0))

        if date_from or date_to:
            def in_range(e):
                if e.business_date is None:
                    return True  # opening anchor always shown
                if date_from and e.business_date < date_from:
                    return False
                return not (date_to and e.business_date > date_to)

            entries = [e for e in entries if in_range(e)]
        total_debit = sum(e.debit_paisa for e in entries)
        total_credit = sum(e.credit_paisa for e in entries)
        closing = entries[-1].running_paisa if entries else 0
        return CustomerStatement(
            customer=self._info(customer_id),
            entries=entries,
            total_debit_paisa=total_debit,
            total_credit_paisa=total_credit,
            closing_paisa=closing,
        )


class StubPaymentService:
    """Test double for PaymentService, wired to a StubCreditService."""

    def __init__(self, credit_stub: StubCreditService):
        self._credit = credit_stub
        self._payments: dict[str, dict] = {}
        self._allocations: dict[int, dict] = {}
        self._next_payment = 1
        self._next_allocation = 1
        self.last_reallocate_reason: str | None = None

    # -- internal ------------------------------------------------------
    def _unpaid_tickets(self, customer_id):
        result = []
        for t in self._credit._tickets.values():
            if (
                t["customer_id"] == customer_id
                and t["status"] == "posted"
                and not t["is_reversal"]
            ):
                allocated = sum(
                    a["amount_paisa"]
                    for a in self._allocations.values()
                    if a["ticket_uuid"] == t["uuid"] and not a["reversed"]
                    and self._payments[a["payment_uuid"]]["status"] == "posted"
                )
                unpaid = (t["gross"] - t["paid_now"]) - allocated
                if unpaid > 0:
                    result.append((t, unpaid))
        result.sort(key=lambda tu: (tu[0]["business_date"], tu[0]["receipt_no"]))
        return result

    def _unallocated(self, payment_uuid):
        p = self._payments[payment_uuid]
        allocated = sum(
            a["amount_paisa"]
            for a in self._allocations.values()
            if a["payment_uuid"] == payment_uuid and not a["reversed"]
        )
        return p["amount_paisa"] - allocated

    def _info(self, payment_uuid):
        p = self._payments[payment_uuid]
        allocs = [
            AllocationInfo(
                id=aid,
                ticket_uuid=a["ticket_uuid"],
                ticket_receipt_no=self._credit._tickets[a["ticket_uuid"]]["receipt_no"],
                amount_paisa=a["amount_paisa"],
                effective_paisa=a["amount_paisa"],
            )
            for aid, a in sorted(self._allocations.items())
            if a["payment_uuid"] == payment_uuid and not a["reversed"]
        ]
        cust = self._credit._customers[p["customer_id"]]
        return PaymentInfo(
            id=p["id"],
            uuid=payment_uuid,
            customer_id=p["customer_id"],
            customer_code=cust["code"],
            business_date=p["business_date"],
            amount_paisa=p["amount_paisa"],
            method=p["method"],
            receipt_no=p["receipt_no"],
            status=p["status"],
            allocations=allocs,
            unallocated_paisa=self._unallocated(payment_uuid),
        )

    def allocated_to_customer(self, customer_id):
        total = 0
        for a in self._allocations.values():
            if a["reversed"]:
                continue
            pay = self._payments[a["payment_uuid"]]
            if pay["status"] != "posted":
                continue
            t = self._credit._tickets.get(a["ticket_uuid"])
            if t is not None and t["customer_id"] == customer_id:
                total += a["amount_paisa"]
        return total

    def effective_allocations(self):
        """(allocation, payment) pairs still in effect — for the ledger."""
        return [
            (a, self._payments[a["payment_uuid"]])
            for a in self._allocations.values()
            if not a["reversed"]
            and self._payments[a["payment_uuid"]]["status"] == "posted"
        ]

    # -- payments --------------------------------------------------------
    def record_payment(
        self, customer_id, business_date, amount_paisa, method="cash", *,
        remarks=None, receipt_no=None, occurred_at_utc=None,
        auto_allocate=True, actor=None,
    ):
        if amount_paisa <= 0:
            raise ValueError("payment amount must be positive")
        if method not in ("cash", "bank", "other"):
            raise ValueError(f"unknown method {method!r}")
        if customer_id not in self._credit._customers:
            raise ValueError("unknown customer")
        n = self._next_payment
        self._next_payment += 1
        uuid = f"payment-{n}"
        self._payments[uuid] = {
            "id": n,
            "receipt_no": receipt_no or f"P-{n:04d}",
            "customer_id": customer_id,
            "business_date": business_date,
            "amount_paisa": amount_paisa,
            "method": method,
            "status": "posted",
            "remarks": remarks,
        }
        if auto_allocate:
            self.allocate(n, None, business_date=business_date)
        return self._info(uuid)

    def _find_payment(self, payment_id):
        for uuid, p in self._payments.items():
            if p["id"] == payment_id or uuid == payment_id:
                return uuid
        raise ValueError("payment not found")

    def allocate(self, payment_id, splits=None, *, business_date=None, actor=None):
        uuid = self._find_payment(payment_id)
        p = self._payments[uuid]
        if p["status"] != "posted":
            raise ValueError("cannot allocate a reversed payment")
        if splits is None:
            # FIFO auto-allocation over the remaining unallocated amount.
            remaining = self._unallocated(uuid)
            new_splits = []
            for t, unpaid in self._unpaid_tickets(p["customer_id"]):
                if remaining <= 0:
                    break
                take = min(unpaid, remaining)
                new_splits.append(
                    {"ticket_uuid": t["uuid"], "amount_paisa": take}
                )
                remaining -= take
            splits = new_splits
        tickets = {t["uuid"]: (t, unpaid) for t, unpaid in self._unpaid_tickets(p["customer_id"])}
        total_new = sum(s["amount_paisa"] for s in splits)
        if total_new > self._unallocated(uuid):
            raise ValueError("total allocations exceed the payment amount")
        for split in splits:
            if split["ticket_uuid"] not in tickets:
                raise ValueError("ticket is not unpaid for this customer")
            _, unpaid = tickets[split["ticket_uuid"]]
            if split["amount_paisa"] <= 0 or split["amount_paisa"] > unpaid:
                raise ValueError("allocation exceeds the ticket's unpaid balance")
        for split in splits:
            aid = self._next_allocation
            self._next_allocation += 1
            self._allocations[aid] = {
                "payment_uuid": uuid,
                "ticket_uuid": split["ticket_uuid"],
                "amount_paisa": split["amount_paisa"],
                "reversed": False,
            }

    def reallocate(self, allocation_id, new_splits, reason, *, business_date, actor=None):
        if not (reason or "").strip():
            raise ValueError("a reason is required")
        old = self._allocations[allocation_id]
        if old["reversed"]:
            raise ValueError("allocation already reversed")
        old["reversed"] = True
        self.last_reallocate_reason = reason
        try:
            self.allocate(old["payment_uuid"], new_splits, business_date=business_date)
        except Exception:
            old["reversed"] = False  # roll back the test double
            raise

    def reverse_payment(self, payment_uuid, reason, business_date, *, actor=None):
        if not (reason or "").strip():
            raise ValueError("a reason is required")
        p = self._payments[payment_uuid]
        if p["status"] != "posted":
            raise ValueError("payment is not posted")
        p["status"] = "reversed"

    def payment_details(self, payment_uuid):
        return self._info(payment_uuid)

    def day_payments(self, business_date):
        return [
            self._info(uuid)
            for uuid, p in self._payments.items()
            if p["business_date"] == business_date
        ]

    def customer_payments(self, customer_id):
        return [
            self._info(uuid)
            for uuid, p in self._payments.items()
            if p["customer_id"] == customer_id
        ]

    def receipt_data(self, payment):
        payment_uuid = self._find_payment(payment)
        info = self._info(payment_uuid)
        p = self._payments[payment_uuid]
        cust = self._credit._customers[p["customer_id"]]
        return {
            "payment": {
                "uuid": info.uuid,
                "receipt_no": info.receipt_no,
                "business_date": info.business_date,
                "amount_paisa": info.amount_paisa,
                "method": info.method,
                "status": info.status,
                "occurred_at_utc": f"{info.business_date}T10:30:00.000Z",
                "remarks": p["remarks"],
            },
            "customer": {
                "customer_code": cust["code"],
                "name": cust["name"],
                "business_name": cust["business_name"],
                "phone": cust["phone"],
                "outstanding_paisa": self._credit.outstanding(p["customer_id"]),
            },
            "allocations": [
                {
                    "ticket_uuid": a.ticket_uuid,
                    "ticket_receipt_no": a.ticket_receipt_no,
                    "amount_paisa": a.amount_paisa,
                    "effective_paisa": a.effective_paisa,
                }
                for a in info.allocations
            ],
            "unallocated_paisa": info.unallocated_paisa,
            "generated_at_utc": f"{info.business_date}T10:35:00.000Z",
        }


class StubDayService:
    def get_open_day(self):
        return SimpleNamespace(business_date=DAY)


class StubNozzleService:
    def list_nozzles(self, **kwargs):
        return [
            NozzleStub("1 PG+", "PETROL"),
            NozzleStub("2 PG+", "PETROL"),
            NozzleStub("3 HSD", "DIESEL"),
            NozzleStub("4 HSD", "DIESEL"),
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

    Fills text fields, clicks each dialog's own action buttons (add line,
    add allocation), unchecks auto-allocate on the record-payment dialog,
    then presses OK — exactly like a fast operator would.
    """
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: None)

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
        add_line = self.findChild(QPushButton, "add_line_btn")
        if add_line is not None:
            add_line.click()
        add_alloc = self.findChild(QPushButton, "add_alloc_btn")
        if add_alloc is not None:
            add_alloc.click()
        for check in self.findChildren(QtWidgets.QCheckBox):
            if check.objectName() == "auto_allocate_check":
                check.setChecked(False)
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
def credit_stub():
    return StubCreditService()


@pytest.fixture
def pay_stub(credit_stub):
    svc = StubPaymentService(credit_stub)
    credit_stub.payment_stub = svc
    return svc


@pytest.fixture
def day_stub():
    return StubDayService()


@pytest.fixture
def nozzle_stub():
    return StubNozzleService()


@pytest.fixture
def cust_page(qapp, dialogs, credit_stub, day_stub, nozzle_stub):
    page = CustomerCreditPage(credit_stub, day_stub, nozzle_service=nozzle_stub)
    page.show()
    yield page
    page.close()


@pytest.fixture
def pay_page(qapp, dialogs, pay_stub, credit_stub, day_stub):
    page = CustomerPaymentsPage(pay_stub, credit_stub, day_stub)
    page.show()
    yield page
    page.close()


def _seed_customer_with_ticket(credit_stub, pay_stub=None):
    """One active customer + one 100 L PETROL ticket on DAY."""
    customer = credit_stub.create_customer(
        "Test Customer",
        business_name="Test Business",
        phone="0300-1234567",
        address="Test Address",
        opening_receivable_paisa=0,
        credit_limit_paisa=None,
        notes=None,
    )
    ticket = credit_stub.issue_ticket(
        customer.id,
        DAY,
        [{"fuel_product_code": "PETROL", "nozzle_code": "1 PG+",
          "quantity_litres": Decimal("100")}],
        paid_now_paisa=10_000,
    )
    return customer, ticket


# ---------------------------------------------------------------------------
# Stub-service chain: add customer -> issue ticket -> record payment ->
# allocate, asserting service state after each step
# ---------------------------------------------------------------------------
def test_chain_add_customer_issue_ticket_pay_allocate(
    qapp, dialogs, cust_page, pay_page, credit_stub, pay_stub
):
    cust_page._date_edit.setDate(QDate(2026, 10, 12))
    pay_page._date_edit.setDate(QDate(2026, 10, 12))

    # 1. add customer through the dialog (name/opening/limit auto-filled)
    cust_page._add_customer_btn.click()
    customers = credit_stub.list_customers()
    assert len(customers) == 1
    assert customers[0].name == "100"
    assert cust_page._cust_table.rowCount() == 1
    assert cust_page._cust_table.item(0, 0).text() == "C001"
    assert cust_page._cust_table.item(0, 3).text() == "100.00"  # opening
    assert cust_page._cust_table.item(0, 4).text() == "100.00"  # limit
    customer_id = customers[0].id

    # 2. issue ticket through the dialog (100 L PETROL, 100 PKR paid now)
    cust_page._issue_ticket_btn.click()
    tickets = credit_stub.day_tickets(DAY)
    assert len(tickets) == 1
    ticket = tickets[0]
    assert ticket.customer_id == customer_id
    assert ticket.lines[0].fuel_product_code == "PETROL"
    assert ticket.lines[0].quantity_ml == 100_000
    assert ticket.paid_now_paisa == 10_000
    assert ticket.unpaid_paisa == 2_659_630 - 10_000
    # The 100 PKR credit limit is exceeded -> the service warned.
    assert ticket.warnings, "expected a credit-limit warning from the service"
    assert cust_page._ticket_table.rowCount() == 1
    assert cust_page._ticket_table.item(0, 0).text() == "T-0001"
    # The ticket row resolves the customer name through the page lookup.
    assert cust_page._ticket_table.item(0, 1).text() == "100"

    # 3. record payment through the dialog (auto-allocate OFF via fixture)
    pay_page._record_payment_btn.click()
    payments = pay_stub.day_payments(DAY)
    assert len(payments) == 1
    assert payments[0].method == "cash"
    details = pay_stub.payment_details(payments[0].uuid)
    assert details.amount_paisa == 10_000
    assert details.unallocated_paisa == 10_000
    assert pay_page._pay_table.rowCount() == 1
    assert pay_page._pay_table.item(0, 0).text() == "P-0001"
    assert pay_page._pay_table.item(0, 5).text() == "100.00"  # unallocated

    # 4. allocate through the View/Allocate dialog
    pay_page.refresh()
    pay_page._pay_table.setCurrentCell(0, 0)
    pay_page._view_allocate_btn.click()
    details = pay_stub.payment_details(payments[0].uuid)
    assert details.unallocated_paisa == 0
    assert len(details.allocations) == 1
    assert details.allocations[0].amount_paisa == 10_000
    assert details.allocations[0].ticket_uuid == ticket.uuid

    # Outstanding dropped by exactly the allocated amount.
    assert credit_stub.outstanding(customer_id) == 10_000 + (2_659_630 - 10_000) - 10_000


def test_issue_ticket_multiple_lines(qapp, dialogs, credit_stub, day_stub, nozzle_stub):
    credit_stub.create_customer(
        "ACME", business_name="b", phone="p", address="a", notes="n"
    )
    page = CustomerCreditPage(credit_stub, day_stub, nozzle_service=nozzle_stub)
    page.show()
    try:
        page._date_edit.setDate(QDate(2026, 10, 12))
        dlg = _IssueTicketDialog(page, credit_stub, DAY, nozzle_stub)
        dlg._customer_combo.setCurrentIndex(0)
        assert dlg._product_combo.currentData() == "PETROL"
        dlg._qty_edit.setText("40")
        dlg._on_add_line()
        dlg._qty_edit.setText("10")
        dlg._on_add_line()
        assert len(dlg._lines) == 2
        assert dlg._lines_table.rowCount() == 2
        assert dlg._lines_table.item(0, 2).text() == f"40.000 {tr('common.litres')}"
        dlg._paid_edit.setText("0")
        dlg._on_accept()  # service call happens here
        tickets = credit_stub.day_tickets(DAY)
        assert len(tickets) == 1
        assert len(tickets[0].lines) == 2
        ml_40 = calc.to_ml(Decimal("40"))
        ml_10 = calc.to_ml(Decimal("10"))
        expected = calc.amount_paisa_for(ml_40, 2_659_630) + calc.amount_paisa_for(
            ml_10, 2_659_630
        )
        assert tickets[0].gross_amount_paisa == expected
    finally:
        page.close()


def test_add_customer_validation_keeps_dialog(qapp, dialogs, credit_stub, day_stub, nozzle_stub):
    """Empty name -> service error shown, nothing saved, input preserved."""
    page = CustomerCreditPage(credit_stub, day_stub, nozzle_service=nozzle_stub)
    page.show()
    try:
        dlg = _CustomerDialog(page, credit_stub)
        dlg._phone_edit.setText("0300-1")
        dlg._on_accept()  # name empty -> ValueError path
        assert credit_stub.list_customers() == []
        assert dlg._phone_edit.text() == "0300-1"  # unsaved input preserved
        assert dlg.result() == 0  # dialog did not accept
    finally:
        page.close()


def test_edit_customer_and_adjust_opening(
    qapp, dialogs, cust_page, credit_stub
):
    customer, _ = _seed_customer_with_ticket(credit_stub)
    cust_page._date_edit.setDate(QDate(2026, 10, 12))
    cust_page.refresh()
    assert cust_page._cust_table.rowCount() == 1

    # Edit: change the phone number via the dialog, keep everything else.
    cust_page._cust_table.setCurrentCell(0, 0)
    dlg = _CustomerDialog(cust_page, credit_stub, existing=credit_stub.get_customer(customer.id))
    dlg._phone_edit.setText("0311-9999999")
    dlg._on_accept()
    assert credit_stub.get_customer(customer.id).phone == "0311-9999999"
    dlg.close()

    # Adjust opening balance: +100 PKR with a reason.
    cust_page.refresh()
    cust_page._cust_table.setCurrentCell(0, 0)
    cust_page._adjust_opening_btn.click()  # auto-filled: amount 100, reason set
    # The adjustment is an audited row: the authoritative outstanding grows
    # by exactly the adjustment, and the table shows the service value.
    expected = 10_000 + (2_659_630 - 10_000)
    assert credit_stub.outstanding(customer.id) == expected
    cust_page.refresh()
    assert cust_page._cust_table.item(0, 3).text() == h.format_pkr(expected)


def test_reverse_ticket(qapp, dialogs, cust_page, credit_stub):
    customer, ticket = _seed_customer_with_ticket(credit_stub)
    cust_page._date_edit.setDate(QDate(2026, 10, 12))
    cust_page.refresh()
    cust_page._ticket_table.setCurrentCell(0, 0)
    cust_page._reverse_ticket_btn.click()  # reason auto-filled by fixture
    details = credit_stub.ticket_details(ticket.uuid)
    assert details.status == "reversed"
    # A reversal row was posted; the original row survives.
    assert len(credit_stub.day_tickets(DAY)) == 2
    cust_page.refresh()
    assert cust_page._ticket_table.rowCount() == 2
    assert cust_page._ticket_table.item(0, 5).text() == tr("ticket.status_reversed")
    # The customer table shows the service-computed outstanding, whatever
    # the reversal economics are.
    assert cust_page._cust_table.item(0, 3).text() == h.format_pkr(
        credit_stub.outstanding(customer.id)
    )


def test_reallocate_dialog(qapp, dialogs, pay_page, credit_stub, pay_stub):
    customer, ticket = _seed_customer_with_ticket(credit_stub)
    payment = pay_stub.record_payment(
        customer.id, DAY, 10_000, "cash", auto_allocate=False
    )
    pay_stub.allocate(
        payment.id,
        [{"ticket_uuid": ticket.uuid, "amount_paisa": 6_000}],
        business_date=DAY,
    )
    details = pay_stub.payment_details(payment.uuid)
    allocation = details.allocations[0]
    assert allocation.amount_paisa == 6_000

    dlg = _ReallocateDialog(
        pay_page, pay_stub, credit_stub, allocation, details, DAY
    )
    assert dlg._ticket_combo.count() >= 1  # the ticket is a candidate
    dlg._split_amount_edit.setText("60")  # 60 PKR = 6000 paisa
    dlg._on_add_split()
    assert len(dlg._pending) == 1
    dlg._reason_edit.setPlainText("correct ticket split")
    dlg._on_accept()
    assert pay_stub.last_reallocate_reason == "correct ticket split"
    details = pay_stub.payment_details(payment.uuid)
    assert len(details.allocations) == 1
    assert details.allocations[0].amount_paisa == 6_000
    assert details.unallocated_paisa == 4_000
    dlg.close()


def test_receipt_preview_renders(qapp, dialogs, pay_page, credit_stub, pay_stub):
    customer, ticket = _seed_customer_with_ticket(credit_stub)
    payment = pay_stub.record_payment(
        customer.id, DAY, 10_000, "cash", remarks="test",
        auto_allocate=False,
    )
    pay_stub.allocate(
        payment.id,
        [{"ticket_uuid": ticket.uuid, "amount_paisa": 10_000}],
        business_date=DAY,
    )
    dlg = _ReceiptDialog(pay_page, pay_stub, payment.uuid)
    try:
        html = dlg._browser.toHtml()
        assert "P-0001" in html
        assert "Test Customer" in html
        assert "100.00" in html
        assert "T-0001" in html
        assert dlg._print_btn.isEnabled()  # real printing wired up
    finally:
        dlg.close()


def test_record_payment_validation_error(qapp, dialogs, pay_page, pay_stub, credit_stub):
    """Empty amount -> error shown, no payment recorded."""
    credit_stub.create_customer(
        "ACME", business_name="b", phone="p", address="a", notes="n"
    )
    dlg = _RecordPaymentDialog(pay_page, pay_stub, credit_stub, DAY)
    try:
        dlg._amount_edit.setText("")  # invalid
        dlg._on_accept()
        assert pay_stub.day_payments(DAY) == []
        assert dlg.result() == 0
    finally:
        dlg.close()


def test_ledger_statement_renders(qapp, dialogs, cust_page, credit_stub, pay_stub):
    customer, ticket = _seed_customer_with_ticket(credit_stub)
    payment = pay_stub.record_payment(
        customer.id, DAY, 10_000, "cash", auto_allocate=True
    )
    assert payment.unallocated_paisa == 0  # FIFO allocated to the ticket

    cust_page._date_edit.setDate(QDate(2026, 10, 12))
    cust_page.refresh()
    # Pick the customer in the ledger tab and view the statement.
    index = cust_page._ledger_customer_combo.findText("C001", QtCore.Qt.MatchStartsWith)
    assert index >= 0
    cust_page._ledger_customer_combo.setCurrentIndex(index)
    cust_page._ledger_view_btn.click()

    table = cust_page._ledger_table
    assert table.rowCount() >= 3  # opening + ticket + payment allocation
    assert table.item(0, 1).text() == tr("ledger.type_opening")
    totals = cust_page._ledger_totals_label.text()
    assert tr("ledger.totals").split("{")[0] in totals
    # Closing balance matches the service-computed outstanding.
    statement = credit_stub.statement(customer.id)
    assert f"{statement.closing_paisa / 100:,.2f}" in totals


def test_language_switch_to_urdu(qapp, dialogs, credit_stub, pay_stub, day_stub, nozzle_stub):
    set_language("ur")
    cust = CustomerCreditPage(credit_stub, day_stub, nozzle_service=nozzle_stub)
    pay = CustomerPaymentsPage(pay_stub, credit_stub, day_stub)
    cust.show()
    pay.show()
    try:
        cust.refresh()
        pay.refresh()
        assert cust._title.text() == "گاہکوں کا ادھار"
        assert pay._title.text() == "گاہکوں کی ادائیگیاں"
        assert cust._tabs.tabText(0) == "گاہک"
        assert cust._tabs.tabText(1) == "ٹکٹ"
        assert cust._tabs.tabText(2) == "کھاتہ / گوشوارہ"
        assert cust._add_customer_btn.text() == "گاہک شامل کریں"
        assert pay._record_payment_btn.text() == "ادائیگی درج کریں"
    finally:
        cust.close()
        pay.close()
        set_language("en")


# ---------------------------------------------------------------------------
# Real services on a temp DB (skipped until the parallel agent lands them)
# ---------------------------------------------------------------------------
@needs_real
def test_real_chain_add_customer_issue_ticket_pay_allocate(qapp, dialogs, tmp_path):
    from app.bootstrap import bootstrap
    from app.services.business_day_service import BusinessDayService
    from app.services.customer_credit_service import CustomerCreditService
    from app.services.nozzle_service import NozzleService
    from app.services.payment_service import PaymentService
    from app.services.price_service import PriceService

    # The real services pin the event timestamp to the business date's
    # Asia/Karachi calendar day, so the test day must be today.
    today = QDate.currentDate().toString("yyyy-MM-dd")

    result = bootstrap(tmp_path / "phase4.sqlite3")
    try:
        dbm = result.db_manager
        days = BusinessDayService(dbm)
        days.open_day(today, opening_cash_paisa=0)
        prices = PriceService(dbm)
        prices.set_price("PETROL", Decimal("265.9630"), f"{today}T00:00:00.000Z", today)
        credit = CustomerCreditService(dbm)
        payments = PaymentService(dbm)
        nozzles = NozzleService(dbm)

        cust_page = CustomerCreditPage(credit, days, nozzle_service=nozzles)
        cust_page.show()
        try:
            cust_page._date_edit.setDate(QDate.currentDate())

            # 1. add customer through the dialog
            cust_page._add_customer_btn.click()
            customers = credit.list_customers()
            assert len(customers) == 1
            assert h.dto_field(customers[0], "name") == "100"
            customer_id = h.dto_field(customers[0], "id", "customer_id")

            # 2. issue ticket through the dialog
            cust_page._issue_ticket_btn.click()
            tickets = credit.day_tickets(today)
            assert len(tickets) == 1
            ticket = tickets[0]
            unpaid = h.dto_field(ticket, "unpaid_paisa", default=0)
            assert unpaid > 0
            assert cust_page._ticket_table.rowCount() == 1

            # 3. record payment (auto-allocate OFF via fixture)
            pay_page = CustomerPaymentsPage(payments, credit, days)
            pay_page.show()
            try:
                pay_page._date_edit.setDate(QDate.currentDate())
                pay_page._record_payment_btn.click()
                pays = payments.day_payments(today)
                assert len(pays) == 1
                pay_uuid = h.dto_field(pays[0], "uuid")
                details = payments.payment_details(pay_uuid)
                assert h.dto_field(details, "unallocated_paisa") == 10_000

                # 4. allocate through the View/Allocate dialog
                pay_page.refresh()
                pay_page._pay_table.setCurrentCell(0, 0)
                pay_page._view_allocate_btn.click()
                details = payments.payment_details(pay_uuid)
                assert h.dto_field(details, "unallocated_paisa") == 0
                allocs = h.dto_field(details, "allocations", default=[]) or []
                assert len(allocs) == 1
                assert h.dto_field(allocs[0], "amount_paisa") == 10_000

                # Outstanding dropped by exactly the allocated amount.
                assert credit.outstanding(customer_id) == (10_000 + unpaid) - 10_000
            finally:
                pay_page.close()
        finally:
            cust_page.close()
    finally:
        result.db_manager.close()


@needs_real
def test_real_main_window_sidebar(qapp, dialogs, tmp_path):
    from app.bootstrap import bootstrap
    from app.ui.main_window import (
        CustomerCreditPage as _CCP,
        CustomerPaymentsPage as _CPP,
        CashBookPage as _CBP,
        ExpensePage as _EP,
        MainWindow,
        _PAGE_CUSTOMERS,
        _PAGE_PAYMENTS,
        _PAGE_CASHBOOK,
        _PAGE_SUPPLIERS,
        _PAGE_OTHER_MATERIALS,
        _PAGE_EXPENSES,
        _PAGE_REPORTS,
        _PAGE_BACKUPS,
        _PAGE_SETTINGS,
        _BACKUPS_AVAILABLE,
        _REPORTS_AVAILABLE,
    )
    from app.ui.cashbook.page import CashBookPage
    from app.ui.expenses.page import ExpensePage
    from app.ui.other_materials.page import OtherMaterialsPage
    from app.ui.suppliers.page import SupplierPage

    assert _CCP is CustomerCreditPage and _CPP is CustomerPaymentsPage
    assert _CBP is CashBookPage and _EP is ExpensePage
    result = bootstrap(tmp_path / "phase4win.sqlite3")
    win = MainWindow(result)
    win.show()
    try:
        assert _PAGE_CUSTOMERS == 5
        assert _PAGE_PAYMENTS == 6
        assert _PAGE_CASHBOOK == 7
        assert _PAGE_SUPPLIERS == 8
        assert _PAGE_OTHER_MATERIALS == 9
        assert _PAGE_EXPENSES == 10
        # Conditional pages keep their slots in order: Reports (Phase 6)
        # then Backups (Phase 8), then Settings last.
        next_index = 11
        if _REPORTS_AVAILABLE:
            assert _PAGE_REPORTS == 11
            next_index = 12
        if _BACKUPS_AVAILABLE:
            assert _PAGE_BACKUPS == next_index
            next_index += 1
        assert _PAGE_SETTINGS == next_index
        assert win._sidebar.item(5).text() == tr("nav.customers")
        assert win._sidebar.item(6).text() == tr("nav.payments")
        assert win._sidebar.item(7).text() == tr("nav.cashbook")
        assert win._sidebar.item(8).text() == tr("nav.suppliers")
        assert win._sidebar.item(9).text() == tr("nav.other_materials")
        assert win._sidebar.item(10).text() == tr("nav.expenses")
        sidebar_index = 11
        if _REPORTS_AVAILABLE:
            assert win._sidebar.item(11).text() == tr("nav.reports")
            sidebar_index = 12
            win.navigate_to(_PAGE_REPORTS)
            from app.ui.reports.page import ReportsPage

            assert isinstance(win._pages.currentWidget(), ReportsPage)
        if _BACKUPS_AVAILABLE:
            assert win._sidebar.item(sidebar_index).text() == tr("nav.backups")
            sidebar_index += 1
        assert win._sidebar.item(sidebar_index).text() == tr("nav.settings")
        win.navigate_to(_PAGE_CUSTOMERS)
        assert isinstance(win._pages.currentWidget(), CustomerCreditPage)
        win.navigate_to(_PAGE_PAYMENTS)
        assert isinstance(win._pages.currentWidget(), CustomerPaymentsPage)
        win.navigate_to(_PAGE_CASHBOOK)
        assert isinstance(win._pages.currentWidget(), CashBookPage)
        win.navigate_to(_PAGE_SUPPLIERS)
        assert isinstance(win._pages.currentWidget(), SupplierPage)
        win.navigate_to(_PAGE_OTHER_MATERIALS)
        assert isinstance(win._pages.currentWidget(), OtherMaterialsPage)
        win.navigate_to(_PAGE_EXPENSES)
        assert isinstance(win._pages.currentWidget(), ExpensePage)
        # Every Phase-5 page exposes refresh(); sidebar and stack stay in sync
        # (14 once the Phase-6 reporting service lands and the Reports page
        # is added, plus the Phase-8 Backups page).
        expected = 12
        if _REPORTS_AVAILABLE:
            expected += 1
        if _BACKUPS_AVAILABLE:
            expected += 1
        assert win._sidebar.count() == win._pages.count() == expected
        assert callable(win._cashbook.refresh)
        assert callable(win._suppliers.refresh)
        assert callable(win._other_materials.refresh)
        assert callable(win._expenses.refresh)
    finally:
        win.close()
        result.db_manager.close()
