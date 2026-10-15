"""Phase-5 other-products models (pure dataclasses, no SQL).

Other products (engine oil, lubricants, …) are tracked in whole units
(``quantity_units`` is an integer count of the product's ``unit``).
Transactions are immutable facts: ``purchase`` (+) and ``sale`` (−) move
stock and, for ``method='cash'``, post exactly one derived cash-book row;
``adjustment`` (±, reasoned) corrects stock with no cash movement.
On-hand stock is always computed (``v_other_product_stock``), never typed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OtherProductInfo:
    """An other-product master row with its computed on-hand units."""

    id: int
    name: str
    unit: str
    opening_value_paisa: int
    is_active: bool
    on_hand_units: int | None = None


@dataclass
class OtherTxnInfo:
    """One purchase / sale / adjustment transaction."""

    id: int
    uuid: str
    business_date: str
    product_id: int
    product_name: str
    txn_type: str  # purchase | sale | adjustment
    quantity_units: int
    unit_price_paisa: int
    total_paisa: int
    method: str  # cash | bank | other
    reference: str | None
    occurred_at_utc: str
    created_by: str
