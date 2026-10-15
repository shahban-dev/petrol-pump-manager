"""Exact numeric primitives. Single implementation used by every service.

Rules (see documentation/00_design_plan.md §5.2):
- Money is stored as integer paisa; quantities as integer milliliters;
  fuel unit prices as integer 0.0001-PKR units.
- All math in Decimal, ROUND_HALF_UP, rounded ONCE at the transaction boundary.
- float is never accepted: passing one raises TypeError (fail loud, not silent).
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

PAISA_PER_PKR = 100
ML_PER_LITRE = 1000
PRICE_10K_PER_PKR = 10_000  # unit_price_10k = price_PKR * 10_000


def _dec(value: Decimal | str | int, name: str) -> Decimal:
    if isinstance(value, float):
        raise TypeError(f"{name}: float is not allowed for money/quantity math; use Decimal or str")
    try:
        return Decimal(value)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{name}: cannot interpret {value!r} as Decimal") from exc


def round_half_up(value: Decimal) -> int:
    """Round a Decimal to the nearest integer, halves away from zero (ROUND_HALF_UP)."""
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_paisa(pkr: Decimal | str | int) -> int:
    """PKR amount -> integer paisa, rounded half-up."""
    d = _dec(pkr, "pkr")
    if d < 0:
        raise ValueError("paisa amounts must be non-negative; signed adjustments use explicit types")
    return round_half_up(d * PAISA_PER_PKR)


def from_paisa(paisa: int) -> Decimal:
    """Integer paisa -> Decimal PKR (exact, 2 dp)."""
    if not isinstance(paisa, int) or isinstance(paisa, bool):
        raise TypeError("paisa must be int")
    return (Decimal(paisa) / PAISA_PER_PKR).quantize(Decimal("0.01"))


def to_ml(litres: Decimal | str | int) -> int:
    """Litres -> integer milliliters, rounded half-up."""
    d = _dec(litres, "litres")
    if d < 0:
        raise ValueError("quantities must be non-negative")
    return round_half_up(d * ML_PER_LITRE)


def from_ml(ml: int) -> Decimal:
    """Integer milliliters -> Decimal litres (exact, 3 dp)."""
    if not isinstance(ml, int) or isinstance(ml, bool):
        raise TypeError("ml must be int")
    return (Decimal(ml) / ML_PER_LITRE).quantize(Decimal("0.001"))


def amount_paisa_for(quantity_ml: int, unit_price_10k: int) -> int:
    """Canonical exact-price sale amount.

    amount_PKR = (quantity_ml / 1000) * (unit_price_10k / 10_000)
    amount_paisa = round_half_up(quantity_ml * unit_price_10k / 100_000)
    """
    if not isinstance(quantity_ml, int) or isinstance(quantity_ml, bool):
        raise TypeError("quantity_ml must be int")
    if not isinstance(unit_price_10k, int) or isinstance(unit_price_10k, bool):
        raise TypeError("unit_price_10k must be int")
    if quantity_ml < 0 or unit_price_10k < 0:
        raise ValueError("quantity and price must be non-negative")
    return round_half_up(Decimal(quantity_ml) * Decimal(unit_price_10k) / Decimal(100_000))


def format_pkr(paisa: int) -> str:
    """Human display: 123456 -> '1,234.56'. Display only; never parsed back."""
    d = from_paisa(paisa)
    return f"{d:,.2f}"


def to_price_10k(pkr: Decimal | str | int) -> int:
    """PKR unit price -> integer 0.0001-PKR units, rounded half-up.

    Rejects float (fail loud, like every other converter here) and negative
    values. A zero price is allowed by the converter; services that need a
    strictly positive price (price_history, deliveries) reject zero
    themselves.
    """
    d = _dec(pkr, "price_pkr")
    if d < 0:
        raise ValueError(
            f"price_pkr: unit prices must be non-negative; got {pkr!r}"
        )
    return round_half_up(d * PRICE_10K_PER_PKR)


def from_price_10k(price_10k: int) -> Decimal:
    """Integer 0.0001-PKR units -> Decimal PKR (exact, 4 dp)."""
    if not isinstance(price_10k, int) or isinstance(price_10k, bool):
        raise TypeError("price_10k must be int")
    return (Decimal(price_10k) / PRICE_10K_PER_PKR).quantize(Decimal("0.0001"))
