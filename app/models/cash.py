"""Phase-5 cash-book models (pure dataclasses, no SQL).

Accounting rules implemented by the services in
:mod:`app.services.cashbook_service`:

* **Effective-amount rule** (matches the v10 views): a posted row counts
  ``+amount``; a row flipped to ``status='reversed'`` counts ``0`` (the flip
  is only a marker — the row is never erased); a reversal row
  (``reverses_uuid IS NOT NULL`` / ``is_reversal=1``) counts ``-amount``.
* **Settlement identity** (design §6): per fuel product,
  ``meter_revenue = cash + bank + paid_now_on_ticket + receivable + other``.
  ``sales_settlements`` rows are a *classification* of meter revenue, not
  cash movements; old-debt recovery never appears there.
* The cash book (``cash_receipts`` / ``cash_payments``) is **cash-method
  only** (design §11.14): every row is physical cash. Bank/other activity
  never touches these tables.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CashCategoryInfo:
    """A configurable cash-book category (master data)."""

    id: int
    kind: str  # 'receipt' | 'payment'
    name: str  # machine key, e.g. 'cash_sales'
    name_en: str
    name_ur: str | None
    is_active: bool


@dataclass
class CashReceiptInfo:
    """One physical-cash receipt with its effective (net-of-reversal) amount."""

    id: int
    uuid: str
    business_date: str
    category: str
    category_en: str
    amount_paisa: int
    effective_paisa: int
    source_table: str | None
    source_id: int | None
    customer_id: int | None
    customer_name: str | None
    remarks: str | None
    status: str
    occurred_at_utc: str
    created_by: str


@dataclass
class CashPaymentInfo:
    """One physical-cash payment with its effective (net-of-reversal) amount."""

    id: int
    uuid: str
    business_date: str
    category: str
    category_en: str
    amount_paisa: int
    effective_paisa: int
    source_table: str | None
    source_id: int | None
    payee: str | None
    remarks: str | None
    status: str
    occurred_at_utc: str
    created_by: str


@dataclass
class SettlementLeg:
    """One ``sales_settlements`` row as written (signed only in views)."""

    fuel_product_code: str
    category: str  # cash | bank | paid_now_on_ticket | receivable | other
    amount_paisa: int
    source_table: str | None
    source_id: int | None
    batch_uuid: str
    is_reversal: bool
    status: str
    notes: str | None


@dataclass
class CashPosition:
    """The day's cash position: opening + receipts − payments = expected.

    The difference (physical − expected) is *reported*, never auto-written
    off (design §11.8); ``shortage_paisa`` / ``surplus_paisa`` split it for
    display.
    """

    business_date: str
    opening_cash_paisa: int
    receipts_paisa: int
    payments_paisa: int
    expected_paisa: int
    physical_cash_paisa: int | None
    difference_paisa: int | None
    shortage_paisa: int
    surplus_paisa: int
