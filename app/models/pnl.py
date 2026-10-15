"""Phase-5 P&L models (pure dataclasses, no SQL).

Profit follows design §8 (periodic weighted-average costing) with a
**costing period of one business day**: each day's average cost is computed
from that day's opening value/quantity plus that day's deliveries, and the
resulting COGS is snapshotted into the closing record so old P&L never
changes when later purchases are entered.

Figures are labelled **estimated** whenever a cost basis is missing
(unknown opening valuation, no purchases to average from); profit is never
derived from drawer cash and debt recovery is never revenue.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProductPnl:
    """One product's revenue / COGS / gross profit for a period."""

    fuel_product_code: str | None  # None for the "other products" rollup line
    product_name: str
    revenue_paisa: int
    cogs_paisa: int | None
    gross_profit_paisa: int | None

    def to_dict(self) -> dict:
        """JSON-serializable form (for the closing snapshot)."""
        return {
            "fuel_product_code": self.fuel_product_code,
            "product_name": self.product_name,
            "revenue_paisa": self.revenue_paisa,
            "cogs_paisa": self.cogs_paisa,
            "gross_profit_paisa": self.gross_profit_paisa,
        }


@dataclass
class PnlReport:
    """A profit-and-loss report for one day or a date range."""

    period_label: str
    start_date: str
    end_date: str
    lines: list[ProductPnl] = field(default_factory=list)
    other_revenue_paisa: int = 0
    other_cogs_paisa: int | None = 0
    total_revenue_paisa: int = 0
    total_cogs_paisa: int | None = 0
    gross_profit_paisa: int | None = 0
    operating_expenses_paisa: int = 0
    net_profit_paisa: int | None = 0
    receivables_created_paisa: int = 0
    estimated: bool = False
    estimate_reasons: list[str] = field(default_factory=list)
    source: str = "live"  # 'live' | 'closing_snapshot'

    def to_dict(self) -> dict:
        """JSON-serializable form (stored in the closing record's summary)."""
        return {
            "period_label": self.period_label,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "lines": [line.to_dict() for line in self.lines],
            "other_revenue_paisa": self.other_revenue_paisa,
            "other_cogs_paisa": self.other_cogs_paisa,
            "total_revenue_paisa": self.total_revenue_paisa,
            "total_cogs_paisa": self.total_cogs_paisa,
            "gross_profit_paisa": self.gross_profit_paisa,
            "operating_expenses_paisa": self.operating_expenses_paisa,
            "net_profit_paisa": self.net_profit_paisa,
            "receivables_created_paisa": self.receivables_created_paisa,
            "estimated": self.estimated,
            "estimate_reasons": list(self.estimate_reasons),
            "source": self.source,
        }

    @staticmethod
    def from_dict(data: dict) -> "PnlReport":
        """Rebuild a report from a closing snapshot (immutable history)."""
        return PnlReport(
            period_label=data["period_label"],
            start_date=data["start_date"],
            end_date=data["end_date"],
            lines=[
                ProductPnl(
                    fuel_product_code=line.get("fuel_product_code"),
                    product_name=line["product_name"],
                    revenue_paisa=line["revenue_paisa"],
                    cogs_paisa=line.get("cogs_paisa"),
                    gross_profit_paisa=line.get("gross_profit_paisa"),
                )
                for line in data.get("lines", [])
            ],
            other_revenue_paisa=data.get("other_revenue_paisa", 0),
            other_cogs_paisa=data.get("other_cogs_paisa"),
            total_revenue_paisa=data.get("total_revenue_paisa", 0),
            total_cogs_paisa=data.get("total_cogs_paisa"),
            gross_profit_paisa=data.get("gross_profit_paisa"),
            operating_expenses_paisa=data.get("operating_expenses_paisa", 0),
            net_profit_paisa=data.get("net_profit_paisa"),
            receivables_created_paisa=data.get("receivables_created_paisa", 0),
            estimated=bool(data.get("estimated", False)),
            estimate_reasons=list(data.get("estimate_reasons", [])),
            source=data.get("source", "closing_snapshot"),
        )
