"""Phase-5 expense models (pure dataclasses, no SQL).

Accounting rule: an expense is a dated, categorized cost. Only
``method='cash'`` expenses touch physical cash — via exactly one derived
``cash_payments`` row (category ``'expense'``) written in the *same*
transaction as the expense. ``bank``/``other`` expenses never touch the
cash book (design §11.14); they still count as operating expenses in P&L.

Reversals follow the standard pattern: a mirror row (``reverses_uuid``)
plus the posted → reversed flip; the derived cash row is reversed in the
same transaction.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ExpenseCategoryInfo:
    """A configurable expense category (master data)."""

    id: int
    name_en: str
    name_ur: str | None
    is_active: bool


@dataclass
class ExpenseInfo:
    """One posted expense with its effective (net-of-reversal) amount."""

    id: int
    uuid: str
    business_date: str
    category_id: int
    category_en: str
    amount_paisa: int
    effective_paisa: int
    method: str  # cash | bank | other
    payee: str | None
    reference: str | None
    status: str
    occurred_at_utc: str
    created_by: str
