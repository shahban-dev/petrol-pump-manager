"""P&L and cost snapshots: daily periodic weighted-average costing (Phase 5).

**Costing period = one business day** (design §8 applied daily). For each
fuel product and each day::

    avg_cost = (opening_value_paisa + Σ delivery totals)
             / (opening_qty_ml + Σ delivery qty_ml)      [Decimal, 6 dp]
    COGS     = round_half_up(liters_sold_ml × avg_cost)  [rounded once]

The snapshot (and the day's P&L) is written into the closing record's
``summary_json`` by ``snapshot_for_close_conn``; a closed day's P&L is
thereafter read from that immutable snapshot, so old reports never change
when new purchases are entered.

Opening value precedence per product: (1) the previous closing's
``cost_snapshots`` entry; (2) the manager-entered ``opening_balances``
value (``amount_paisa``, when the extended ``set_opening_stock`` has stored
one); (3) unknown → the day is flagged ``estimated``.

Other products use a daily periodic average too: the day's purchases set
the unit cost; with no purchases that day the lifetime average is used
(flagged estimated); with no cost data at all COGS is unknown (None).

Figures are labelled **estimated** whenever a cost basis is missing.
Profit is never derived from drawer cash; debt recovery is never revenue.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from decimal import Decimal

from app.calculations import money as calc
from app.calculations import time as timecalc
from app.models.pnl import PnlReport, ProductPnl
from app.repositories.expense_repository import ExpenseRepository
from app.repositories.other_materials_repository import OtherMaterialsRepository
from app.repositories.price_repository import PriceRepository
from app.services import errors
from app.services._base import ServiceBase


class AccountingService(ServiceBase):
    """Computes cost snapshots and P&L reports (read-only, conn-safe)."""

    def __init__(self, db_manager, actor: str | None = None) -> None:
        super().__init__(db_manager, actor)
        self._prices = PriceRepository()
        self._expenses = ExpenseRepository()
        self._materials = OtherMaterialsRepository()

    # ------------------------------------------------------------------
    # cost snapshots
    # ------------------------------------------------------------------
    def cost_snapshot_conn(
        self, conn: sqlite3.Connection, business_date: str
    ) -> dict:
        """Per-product cost snapshot for one day (caller-owned connection).

        Returns ``{"business_date": ..., "products": [...]}`` where each
        product dict carries ``fuel_product_code, opening_qty_ml,
        opening_value_paisa, opening_value_estimated, delivery_qty_ml,
        delivery_cost_paisa, avg_cost_paisa_per_ml`` (Decimal as 6-dp
        string), ``liters_sold_ml, cogs_paisa|None, closing_qty_ml,
        closing_value_paisa|None, estimated, estimate_reasons``.
        All values are JSON-serializable (the close flow stores this).
        """
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        prev_values = self._previous_closing_values(conn, business_date)
        from app.services.sales_service import SalesService

        sales = SalesService(self._db, self._actor)._day_sales_conn(
            conn, business_date
        )
        products = []
        for product in self._prices.list_products(conn):
            code = product["code"]
            opening_qty_ml = self._opening_qty(conn, business_date, int(product["id"]))
            opening_value, opening_estimated, opening_reasons = (
                self._opening_value(conn, business_date, int(product["id"]),
                                    code, prev_values)
            )
            delivery = conn.execute(
                "SELECT COALESCE(SUM(quantity_ml), 0) AS qty,"
                " COALESCE(SUM(total_cost_paisa"
                "  + COALESCE(other_cost_paisa, 0)), 0) AS cost"
                " FROM fuel_deliveries"
                " WHERE business_date = ? AND fuel_product_id = ?",
                (business_date, int(product["id"])),
            ).fetchone()
            delivery_qty_ml = int(delivery["qty"])
            delivery_cost_paisa = int(delivery["cost"])
            liters_sold_ml = sales.liters_by_product_ml.get(code, 0)

            reasons = list(opening_reasons)
            denom = opening_qty_ml + delivery_qty_ml
            avg: Decimal | None = None
            avg_str: str | None = None
            if denom > 0:
                avg = (Decimal(opening_value) + Decimal(delivery_cost_paisa)) / Decimal(
                    denom
                )
                avg_str = f"{avg:.6f}"
            cogs_paisa: int | None
            closing_value_paisa: int | None
            if avg is not None:
                cogs_paisa = calc.round_half_up(Decimal(liters_sold_ml) * avg)
            elif liters_sold_ml == 0:
                cogs_paisa = 0
            else:
                cogs_paisa = None
                reasons.append(
                    f"{code}: no cost basis for {liters_sold_ml} ml sold"
                    " (no opening value and no deliveries)."
                )
            closing_qty_ml = opening_qty_ml + delivery_qty_ml - liters_sold_ml
            if avg is not None:
                closing_value_paisa = calc.round_half_up(
                    Decimal(closing_qty_ml) * avg
                )
            else:
                closing_value_paisa = None
            estimated = opening_estimated or (
                denom == 0 and liters_sold_ml > 0
            )
            products.append(
                {
                    "fuel_product_code": code,
                    "opening_qty_ml": opening_qty_ml,
                    "opening_value_paisa": opening_value,
                    "opening_value_estimated": opening_estimated,
                    "delivery_qty_ml": delivery_qty_ml,
                    "delivery_cost_paisa": delivery_cost_paisa,
                    "avg_cost_paisa_per_ml": avg_str,
                    "liters_sold_ml": liters_sold_ml,
                    "cogs_paisa": cogs_paisa,
                    "closing_qty_ml": closing_qty_ml,
                    "closing_value_paisa": closing_value_paisa,
                    "estimated": estimated,
                    "estimate_reasons": reasons,
                }
            )
        return {"business_date": business_date, "products": products}

    @staticmethod
    def _opening_qty(
        conn: sqlite3.Connection, business_date: str, fuel_product_id: int
    ) -> int:
        row = conn.execute(
            "SELECT quantity_ml FROM opening_balances"
            " WHERE business_date = ? AND balance_type = 'stock'"
            " AND fuel_product_id = ?",
            (business_date, fuel_product_id),
        ).fetchone()
        return int(row["quantity_ml"]) if row else 0

    def _previous_closing_values(
        self, conn: sqlite3.Connection, business_date: str
    ) -> dict[str, int]:
        """{product code: closing_value_paisa} from the latest earlier closing."""
        row = conn.execute(
            "SELECT summary_json FROM business_day_closings"
            " WHERE business_date < ?"
            " ORDER BY business_date DESC, closing_revision DESC LIMIT 1",
            (business_date,),
        ).fetchone()
        if row is None:
            return {}
        try:
            summary = json.loads(row["summary_json"])
        except (ValueError, TypeError):
            return {}
        out: dict[str, int] = {}
        snapshots = (summary.get("cost_snapshots") or {})
        items = (
            snapshots.get("products")
            if isinstance(snapshots, dict)
            else snapshots
        )
        for item in items or []:
            value = item.get("closing_value_paisa")
            if item.get("fuel_product_code") and value is not None:
                out[item["fuel_product_code"]] = int(value)
        return out

    def _opening_value(
        self,
        conn: sqlite3.Connection,
        business_date: str,
        fuel_product_id: int,
        code: str,
        prev_values: dict[str, int],
    ) -> tuple[int, bool, list[str]]:
        """(value paisa, estimated, reasons) for a product's opening stock."""
        if code in prev_values:
            return prev_values[code], False, []
        # Manager-entered first-day valuation (migration 012). The
        # migration-005 CHECK forces opening_balances.amount_paisa NULL for
        # stock rows, so the value lives in stock_opening_values.
        row = conn.execute(
            "SELECT value_paisa FROM stock_opening_values"
            " WHERE business_date = ? AND fuel_product_id = ?",
            (business_date, fuel_product_id),
        ).fetchone()
        if row is not None:
            return int(row["value_paisa"]), False, []
        return (
            0,
            True,
            [f"{code}: opening stock value unknown (no previous closing"
             " snapshot and no manager-entered value)."],
        )

    # ------------------------------------------------------------------
    # other-product COGS
    # ------------------------------------------------------------------
    def _other_cogs_conn(
        self, conn: sqlite3.Connection, business_date: str
    ) -> tuple[int | None, bool, list[str]]:
        """(cogs paisa|None, estimated, reasons) for other-product sales.

        Daily periodic average: the day's purchases set the unit cost; with
        no purchases that day the lifetime average is used (estimated); with
        no cost data at all COGS is unknown (None).
        """
        total = 0
        estimated = False
        reasons: list[str] = []
        for product in self._materials.list_products(conn, active_only=False):
            product_id = int(product["id"])
            sale_qty = self._materials.sale_quantity(
                conn, product_id, business_date
            )
            if sale_qty == 0:
                continue
            day = self._materials.purchase_totals(
                conn, product_id, business_date
            )
            if int(day["qty_units"]) > 0:
                avg = Decimal(int(day["total_paisa"])) / Decimal(
                    int(day["qty_units"])
                )
            else:
                lifetime = self._materials.purchase_totals(conn, product_id)
                if int(lifetime["qty_units"]) > 0:
                    avg = Decimal(int(lifetime["total_paisa"])) / Decimal(
                        int(lifetime["qty_units"])
                    )
                    estimated = True
                    reasons.append(
                        f"{product['name']}: no purchases on {business_date};"
                        " lifetime average unit cost used."
                    )
                else:
                    return (
                        None,
                        True,
                        [f"{product['name']}: sold {sale_qty} units with no"
                         " purchase cost data; COGS unknown."],
                    )
            total += calc.round_half_up(Decimal(sale_qty) * avg)
        return total, estimated, reasons

    # ------------------------------------------------------------------
    # daily P&L
    # ------------------------------------------------------------------
    def daily_pnl(self, business_date: str) -> PnlReport:
        """The day's P&L: closing snapshot when closed, live otherwise."""
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        with self.read_connection() as conn:
            return self._daily_pnl_conn(conn, business_date)

    def _daily_pnl_conn(
        self, conn: sqlite3.Connection, business_date: str
    ) -> PnlReport:
        day = conn.execute(
            "SELECT status FROM business_days WHERE business_date = ?",
            (business_date,),
        ).fetchone()
        if day is None:
            raise errors.NotFoundError(
                f"No business day {business_date}.",
                key="err.no_business_day_short",
                params={"day_date": business_date},
            )
        if day["status"] == "closed":
            snapshot = self._closing_pnl(conn, business_date)
            if snapshot is not None:
                return snapshot
        report = self._live_daily_pnl_conn(conn, business_date)
        if day["status"] == "closed":
            report.estimated = True
            report.estimate_reasons.append(
                "Day is closed but no P&L snapshot was found in the closing"
                " record; figures recomputed live."
            )
        return report

    @staticmethod
    def _closing_pnl(
        conn: sqlite3.Connection, business_date: str
    ) -> PnlReport | None:
        """The immutable P&L snapshot from the latest closing record."""
        row = conn.execute(
            "SELECT summary_json FROM business_day_closings"
            " WHERE business_date = ?"
            " ORDER BY closing_revision DESC LIMIT 1",
            (business_date,),
        ).fetchone()
        if row is None:
            return None
        try:
            summary = json.loads(row["summary_json"])
        except (ValueError, TypeError):
            return None
        pnl = summary.get("pnl")
        if not isinstance(pnl, dict):
            return None
        report = PnlReport.from_dict(pnl)
        report.source = "closing_snapshot"
        return report

    def _live_daily_pnl_conn(
        self, conn: sqlite3.Connection, business_date: str
    ) -> PnlReport:
        from app.services.sales_service import SalesService

        sales = SalesService(self._db, self._actor)._day_sales_conn(
            conn, business_date
        )
        snapshot = self.cost_snapshot_conn(conn, business_date)
        cogs_by_code = {
            item["fuel_product_code"]: item["cogs_paisa"]
            for item in snapshot["products"]
        }
        estimated = False
        reasons: list[str] = []
        lines: list[ProductPnl] = []
        total_revenue = 0
        total_cogs: int | None = 0
        for product in self._prices.list_products(conn):
            code = product["code"]
            revenue = sales.revenue_by_product_paisa.get(code, 0)
            cogs = cogs_by_code.get(code)
            gross = revenue - cogs if cogs is not None else None
            lines.append(
                ProductPnl(
                    fuel_product_code=code,
                    product_name=product["name_en"],
                    revenue_paisa=revenue,
                    cogs_paisa=cogs,
                    gross_profit_paisa=gross,
                )
            )
            total_revenue += revenue
            if cogs is None:
                total_cogs = None
            elif total_cogs is not None:
                total_cogs += cogs
            item = next(
                i for i in snapshot["products"]
                if i["fuel_product_code"] == code
            )
            if item["estimated"]:
                estimated = True
                reasons.extend(item["estimate_reasons"])
        other_revenue = self._materials.sale_revenue(conn, business_date)
        other_cogs, other_estimated, other_reasons = self._other_cogs_conn(
            conn, business_date
        )
        if other_estimated:
            estimated = True
            reasons.extend(other_reasons)
        total_revenue += other_revenue
        if total_cogs is None or other_cogs is None:
            total_cogs_value: int | None = None
        else:
            total_cogs_value = total_cogs + other_cogs
        gross_profit = (
            total_revenue - total_cogs_value
            if total_cogs_value is not None
            else None
        )
        operating_expenses = sum(
            int(row["total_paisa"] or 0)
            for row in self._expenses.effective_totals_by_category(
                conn, business_date
            )
        )
        net_profit = (
            gross_profit - operating_expenses
            if gross_profit is not None
            else None
        )
        receivables = self._ticket_receivables_conn(conn, business_date)
        return PnlReport(
            period_label=business_date,
            start_date=business_date,
            end_date=business_date,
            lines=lines,
            other_revenue_paisa=other_revenue,
            other_cogs_paisa=other_cogs,
            total_revenue_paisa=total_revenue,
            total_cogs_paisa=total_cogs_value,
            gross_profit_paisa=gross_profit,
            operating_expenses_paisa=operating_expenses,
            net_profit_paisa=net_profit,
            receivables_created_paisa=receivables,
            estimated=estimated,
            estimate_reasons=reasons,
            source="live",
        )

    @staticmethod
    def _ticket_receivables_conn(
        conn: sqlite3.Connection, business_date: str
    ) -> int:
        """Σ signed (gross − paid_now) over the day's tickets (reconcile formula).

        Corrected rule (migration 011, mirroring 009): a reversed original
        keeps its posted contribution; the reversal row negates it.
        """
        row = conn.execute(
            "SELECT COALESCE(SUM(CASE"
            "  WHEN reverses_uuid IS NOT NULL"
            "   THEN -(gross_amount_paisa - paid_now_paisa)"
            "  ELSE (gross_amount_paisa - paid_now_paisa)"
            " END), 0) AS total"
            " FROM credit_sales WHERE business_date = ?",
            (business_date,),
        ).fetchone()
        return int(row["total"])

    # ------------------------------------------------------------------
    # period P&L
    # ------------------------------------------------------------------
    def period_pnl(self, start_date: str, end_date: str) -> PnlReport:
        """P&L summed over a date range (each day via ``daily_pnl``).

        Closed days contribute their immutable snapshots; open or missing
        days are computed live and flagged estimated.
        """
        for value, name in ((start_date, "start_date"), (end_date, "end_date")):
            if not timecalc.is_valid_business_date(value):
                raise errors.ValidationError(
                    f"Invalid {name}: {value!r}.",
                    key="err.invalid_date_value",
                    params={"name": name, "value": value},
                )
        if start_date > end_date:
            raise errors.ValidationError(
                f"start_date {start_date} is after end_date {end_date}.",
                key="err.start_after_end",
                params={"start_date": start_date, "end_date": end_date},
            )
        with self.read_connection() as conn:
            days = self._date_range(start_date, end_date)
            per_product: dict[str, dict] = {}
            other_revenue = 0
            other_cogs: int | None = 0
            operating_expenses = 0
            receivables = 0
            estimated = False
            reasons: list[str] = []
            for day in days:
                day_row = conn.execute(
                    "SELECT status FROM business_days WHERE business_date = ?",
                    (day,),
                ).fetchone()
                if day_row is None:
                    estimated = True
                    reasons.append(f"{day}: no business day record.")
                    continue
                report = self._daily_pnl_conn(conn, day)
                if day_row["status"] != "closed":
                    # An open/reopened day's figures are live, not frozen:
                    # flag the period estimated even when the day's own
                    # inputs are fully valued.
                    estimated = True
                    reasons.append(
                        f"{day}: day is '{day_row['status']}', not closed;"
                        " figures are live, not a frozen snapshot."
                    )
                if report.estimated:
                    estimated = True
                    reasons.extend(report.estimate_reasons)
                for line in report.lines:
                    bucket = per_product.setdefault(
                        line.fuel_product_code or "",
                        {"name": line.product_name, "revenue": 0,
                         "cogs": 0, "cogs_known": True},
                    )
                    bucket["revenue"] += line.revenue_paisa
                    if line.cogs_paisa is None:
                        bucket["cogs_known"] = False
                    else:
                        bucket["cogs"] += line.cogs_paisa
                other_revenue += report.other_revenue_paisa
                if report.other_cogs_paisa is None:
                    other_cogs = None
                elif other_cogs is not None:
                    other_cogs += report.other_cogs_paisa
                operating_expenses += report.operating_expenses_paisa
                receivables += report.receivables_created_paisa
            lines = []
            total_revenue = other_revenue
            total_cogs_value: int | None = 0
            for code in sorted(per_product):
                bucket = per_product[code]
                cogs = bucket["cogs"] if bucket["cogs_known"] else None
                gross = bucket["revenue"] - cogs if cogs is not None else None
                lines.append(
                    ProductPnl(
                        fuel_product_code=code or None,
                        product_name=bucket["name"],
                        revenue_paisa=bucket["revenue"],
                        cogs_paisa=cogs,
                        gross_profit_paisa=gross,
                    )
                )
                total_revenue += bucket["revenue"]
                if cogs is None:
                    total_cogs_value = None
                elif total_cogs_value is not None:
                    total_cogs_value += cogs
            if total_cogs_value is None or other_cogs is None:
                total_cogs_value = None
            else:
                total_cogs_value += other_cogs
            gross_profit = (
                total_revenue - total_cogs_value
                if total_cogs_value is not None
                else None
            )
            net_profit = (
                gross_profit - operating_expenses
                if gross_profit is not None
                else None
            )
            return PnlReport(
                period_label=f"{start_date} to {end_date}",
                start_date=start_date,
                end_date=end_date,
                lines=lines,
                other_revenue_paisa=other_revenue,
                other_cogs_paisa=other_cogs,
                total_revenue_paisa=total_revenue,
                total_cogs_paisa=total_cogs_value,
                gross_profit_paisa=gross_profit,
                operating_expenses_paisa=operating_expenses,
                net_profit_paisa=net_profit,
                receivables_created_paisa=receivables,
                estimated=estimated,
                estimate_reasons=list(dict.fromkeys(reasons)),
                source="live",
            )

    @staticmethod
    def _date_range(start_date: str, end_date: str) -> list[str]:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        days = []
        current = start
        while current <= end:
            days.append(current.isoformat())
            current += timedelta(days=1)
        return days

    # ------------------------------------------------------------------
    # close-flow snapshot
    # ------------------------------------------------------------------
    def snapshot_for_close_conn(
        self, conn: sqlite3.Connection, business_date: str
    ) -> dict:
        """JSON-serializable ``{cost_snapshots, pnl}`` for the closing record.

        Called by the close flow inside its transaction (conn-safe: no
        nested transaction, no fresh connections). The day is still open at
        this point, so the P&L is computed live and then frozen.
        """
        snapshot = self.cost_snapshot_conn(conn, business_date)
        pnl = self._live_daily_pnl_conn(conn, business_date)
        return {
            "cost_snapshots": snapshot["products"],
            "pnl": pnl.to_dict(),
        }
