# Phase 3 Build Notes — Fuel Sales & Stock

**Date:** 2026-10-15 · **Schema:** v7 (migration 007, new) · **Tests:** 171 passed (138 Phase-1/2 intact + 33 new)

## What was built

| Area | Files |
|---|---|
| Migration | `app/database/migrations/007_phase3.sql` (~52 KB, 7 tables, 2 views, 47 triggers); `migrator.py` → v7 |
| Price math | `app/calculations/money.py` += `to_price_10k` / `from_price_10k`; `app/calculations/time.py` += `require_utc_iso`, `karachi_{start,end}_of_day_utc`, `utc_calendar_date_in_karachi` |
| Models | `app/models/price.py` (PriceInfo, SaleInterval, DaySales), `delivery.py` (SupplierInfo, DeliveryInfo), `inventory.py` (TankInfo, MovementInfo, AdjustmentInfo, TankReadingInfo, StockPosition) |
| Repositories | `price_repository.py`, `delivery_repository.py`, `inventory_repository.py`; `nozzle_repository.py` += `list_segments`, `has_segment_before` |
| Services | `price_service.py`, `delivery_service.py`, `inventory_service.py`, `sales_service.py`; `nozzle_service.py` += `record_segment_reading`; `_base.py` += `require_writable_day` + 6 new IntegrityError translations |
| Close flow | `business_day_service.py`: stock carried forward at open; real `prices_recorded` / `deliveries_posted` / `stock_calculated` / `tank_readings` checks; `close_day` posts sales idempotently in-transaction, summary gains revenue + theoretical stock |
| UI | `app/ui/fuel_stock/page.py`, `app/ui/fuel_deliveries/page.py`, sidebar entries, `main_window.py` service wiring; `en.json`/`ur.json` 146 → 233 keys, exact parity |
| Tests | `test_phase3_acceptance.py` (5), `test_price_service.py` (6), `test_inventory_service.py` (7), `test_ui_phase3.py` (15); conftest += `phase3_services` fixture, `ensure_phase3_close_preconditions`, `pkt_*_utc_iso`, `seed_first_day`, `close_prior_day`, `insert_price_row` |

## §11 assumptions that needed further interpretation

1. **Mid-day price change (§11.2).** `set_price` with a prior price row for the product requires every active nozzle of that product to have a `segment` reading on that business date with `occurred_at_utc <= effective_from_utc`; otherwise `ValidationError` naming the nozzles. Day-open prices on a fresh product need no snapshot. Effective times are monotonic (no backdating); duplicate (product, effective) rejected.
2. **Interval pricing rule.** An interval is priced by the latest price row with `effective_from_utc` **strictly before** the interval-end timestamp **and** not after the business day's end — never retrospectively (§11.2). An interval ending exactly at a price-change instant keeps the old price.
3. **Rollover wrap math (§11.1, deferred from Phase 2).** `meter_events.wrap_value_ml` added (007). Earliest covering `rollover` event with `wrap_value_ml > start` wins: qty = `(wrap − start) + end`. Reset/replacement without wrap → qty `None` (unknown, warned, excluded from totals — never negative, never posted).
4. **Negative stock (§11.3).** Blocked at service + trigger F2. F2 balance = day's `opening_balances('stock')` + same-day movements + new movement (openings carry full prior history, so same-day scope is correct). No silent exception: manager posts an authorized `inventory_adjustments` row (reason + authorizer mandatory, audited) first.
5. **Zero-litre intervals.** Valid, priced, aggregated as 0 — but never posted (ledger CHECK forbids `'sale'` with `quantity_ml = 0`). Reported under `skipped_zero_qty`; they don't block `stock_calculated`.
6. **Tank-level stock (§11.11).** One tank per product seeded (`TANK-PETROL`, `TANK-DIESEL`); all seed nozzles wired. Movements require `tank_id`; product-level rollup stays consistent via `stock_positions`. Physical counts are per tank (`tank_readings`, mutable measurement with audit); variance = physical − theoretical, never auto-applied.
7. **Delivery posting.** Exactly one `inventory_movements` row per delivery (`source_uuid` = delivery uuid, F1). Unpaid/partial invoices record `amount_paid_paisa` + derived `payment_status` only — no cash rows (cash book is Phase 5). Duplicate (supplier, invoice) rejected (H4); NULL invoices may repeat. Delivery `total_cost_paisa` is re-verified by trigger against the canonical `(qty × cost + 50000) / 100000` formula.
8. **Sales posting & close.** `post_day_sales` is idempotent (`source_uuid = nozzle-sale:{date}:{nozzle_id}:{seq}`); re-running posts nothing new. `stock_calculated` fails until sales are posted — the manager posts via the Fuel Stock page (or service) before closing; `close_day` re-posts idempotently inside its transaction as a safety net. `credit_liters_guard` honestly reports `{checked: False}` until the Phase-4 credit module exists.
9. **Stock carry-forward.** `open_day` carries per-product theoretical stock into `opening_balances('stock')`, but only for products the previous day actually tracked (has a `'stock'` opening row) — otherwise first-day `set_opening_stock` could never succeed later and rows would be invented for untracked products.

## Deliberate limitations (for Phase 4+)

- `reconcile_day` returns per-product meter vs posted revenue + identity check, with honest 0-valued Phase-4/5 placeholders (settlements, receivables) — never fake numbers.
- `correct_physical_count` validates the reason but does not persist it (`tank_readings` has no reason column; the UPDATE is audited). Consider a `correction_reason` column later.
- `meter_events.wrap_value_ml` is not in the 003 audit snapshots (ALTER-only in 007).
- Weighted-average COGS snapshotting is Phase 5 (P&L); Phase 3 provides exact quantities + revenue.
- Supplier payments module is Phase 5; `supplier_balance` derives outstanding from deliveries only.

## Test maintenance note

29 Phase-1/2 close-day tests were updated (not weakened) for the new honest checklist: they now establish Phase-3 preconditions (prices; stock where the day sells fuel) via `ensure_phase3_close_preconditions`, and `test_validate_close_reports_blocking_items` asserts the new real statuses (`prices_recorded` fail, `stock_calculated` pass on first-reading days). One Phase-2 test now posts sales before `validate_close` on a sales day — the intended workflow.

## Readiness for Phase 4

- `sales_service.day_intervals` / `day_sales` are the exact metered-sales inputs for credit-ticket reconciliation; `credit_liters_guard` is wired to activate when `credit_sales` exists.
- `sales_settlements` table (design §3) is still to be migrated in Phase 4/5; `reconcile_day` placeholders mark the integration points.
- `customer_payments` / `payment_allocations` and trigger group G arrive with Phase 4.
