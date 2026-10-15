"""Phase-5 supplier-payment models (pure dataclasses, no SQL).

Accounting rule: a supplier's outstanding liability is *computed*
(Σ invoice totals − paid at delivery − Σ effective payments), never typed.
An unpaid or partially-paid invoice never reduces physical cash — only a
``method='cash'`` payment row creates a derived ``cash_payments`` row
(category ``'supplier_payment'``), in the same transaction. A
delivery-linked payment can never exceed that invoice's unpaid amount
(enforced by the service pre-check and the Group I trigger backstop).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SupplierPaymentInfo:
    """One payment against a supplier (optionally a delivery invoice)."""

    id: int
    uuid: str
    supplier_id: int
    supplier_name: str
    delivery_id: int | None
    business_date: str
    amount_paisa: int
    effective_paisa: int
    method: str  # cash | bank | other
    status: str
    occurred_at_utc: str
    created_by: str


@dataclass
class SupplierBalance:
    """A supplier's computed liability (invoiced − paid at delivery − paid)."""

    supplier_id: int
    supplier_name: str
    invoiced_paisa: int
    paid_at_delivery_paisa: int
    paid_paisa: int
    outstanding_paisa: int
