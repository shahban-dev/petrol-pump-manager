"""Phase-3 data transfer objects: fuel prices and meter-derived sale intervals.

DTOs only — no SQL, no business logic (design plan §1).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PriceInfo:
    """One row of ``price_history`` with its fuel product.

    The timeline is immutable: a price change is a new row with a later
    ``effective_from_utc``; old rows (and the sales priced from them) are
    never touched (§11.2).
    """

    id: int
    fuel_product_id: int
    fuel_product_code: str
    effective_from_utc: str
    unit_price_10k: int
    business_date: str
    created_by: str


@dataclass(frozen=True)
class SaleInterval:
    """One priced nozzle sale interval for a business day.

    ``quantity_ml`` is ``end - start`` for a forward interval; for a
    backwards interval it is None unless a documented rollover wrap value
    covers the pair (§11.1 — a reset is never a negative sale).
    ``unit_price_10k``/``amount_paisa`` are None when no price was effective
    at the interval's ``occurred_at_utc`` (the pair-end timestamp).
    """

    nozzle_id: int
    nozzle_code: str
    fuel_product_code: str
    tank_id: int | None
    business_date: str
    seq: int
    start_reading_ml: int
    end_reading_ml: int
    occurred_at_utc: str
    quantity_ml: int | None
    unit_price_10k: int | None
    amount_paisa: int | None


@dataclass
class DaySales:
    """A day's meter-derived sales: intervals plus per-product aggregates.

    Only intervals with a known quantity feed the aggregates; only priced
    intervals feed revenue. Iterating yields one per-product row
    (``{"fuel_product_code", "liters_ml", "revenue_paisa"}``) so callers can
    treat the day sales as a product breakdown directly.
    """

    business_date: str
    intervals: list[SaleInterval] = field(default_factory=list)
    liters_by_product_ml: dict[str, int] = field(default_factory=dict)
    revenue_by_product_paisa: dict[str, int] = field(default_factory=dict)

    def __iter__(self):
        """Per-product rows: code, metered liters, priced revenue."""
        for code in self.revenue_by_product_paisa:
            yield {
                "fuel_product_code": code,
                "liters_ml": self.liters_by_product_ml.get(code, 0),
                "revenue_paisa": self.revenue_by_product_paisa[code],
            }
