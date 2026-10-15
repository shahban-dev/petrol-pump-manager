"""Phase-2 data transfer objects: business days, closings, opening balances.

DTOs only — no SQL, no business logic (design plan §1).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BusinessDay:
    """One row of ``business_days``."""

    business_date: str
    status: str
    opening_cash_paisa: int
    prev_closing_date: str | None
    opened_at_utc: str | None
    closed_at_utc: str | None
    closing_revision: int
    physical_cash_paisa: int | None


@dataclass(frozen=True)
class OpeningBalance:
    """One row of ``opening_balances`` — a carry-forward value together with
    the source closing record it came from (``source_*`` is None only for
    manager-entered first-day balances)."""

    business_date: str
    balance_type: str  # 'cash' | 'stock'
    fuel_product_id: int | None
    amount_paisa: int | None
    quantity_ml: int | None
    source_business_date: str | None
    source_closing_revision: int | None


@dataclass(frozen=True)
class ClosingRecord:
    """One immutable row of ``business_day_closings``."""

    id: int
    business_date: str
    closing_revision: int
    closed_at_utc: str
    prev_closing_id: int | None
    actor: str
    opening_cash_paisa: int
    closing_cash_paisa: int
    petrol_litres_ml: int
    diesel_litres_ml: int
    summary: dict
    warnings: list[str]


@dataclass(frozen=True)
class CloseCheckItem:
    """One row of the guided-closing checklist (§19)."""

    key: str
    label_key: str  # localization key for the UI
    status: str  # 'pass' | 'fail' | 'warning' | 'na'
    detail: str = ""
    # Bilingual detail (Phase 6): stable key + raw params; the UI renders
    # ``tr(detail_key).format(**detail_params)`` at display time.
    # ``detail_params`` may hold an ``issues`` list of (key, params)
    # segments, each translated and joined with " | ". ``detail`` stays
    # the English fallback (and what the closing record stores).
    detail_key: str = ""
    detail_params: dict = field(default_factory=dict)


@dataclass
class CloseValidationReport:
    """Result of :meth:`BusinessDayService.validate_close` — the recoverable
    draft the closing screen shows before anything is committed."""

    business_date: str
    items: list[CloseCheckItem] = field(default_factory=list)
    liters_by_product_ml: dict[str, int] = field(default_factory=dict)
    physical_cash_paisa: int | None = None

    @property
    def blocking_errors(self) -> list[CloseCheckItem]:
        """Items that must be resolved before the day may close."""
        return [item for item in self.items if item.status == "fail"]

    @property
    def warnings(self) -> list[CloseCheckItem]:
        """Approved-exception style notes: visible, but not blocking."""
        return [item for item in self.items if item.status == "warning"]

    @property
    def can_close(self) -> bool:
        """True when zero blocking errors remain."""
        return not self.blocking_errors
