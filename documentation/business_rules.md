# Business Rules — Petrol Pump Manager
**Auditor's reference: every financial formula in the system, stated exactly.**
Version 1.0 · 2026-10-15 · Normative companion to `00_design_plan.md` (sections 5, 6, 7, 8, 11).
Implementation: `app/calculations/money.py` (primitives), `app/services/` (formulas), SQLite triggers (guards).

All amounts are stored and summed as **integer paisa**, all quantities as **integer milliliters**.
No Python `float` and no SQLite `REAL` appear anywhere in money, quantity, or price paths.

---

## 1. Storage standards

| Concept | Stored as | Column convention | Example |
|---|---|---|---|
| Money | `INTEGER` paisa; 1 PKR = 100 paisa | `*_paisa` | PKR 12,500.26 → `1250026` |
| Fuel quantity / meter reading | `INTEGER` milliliters; 1 L = 1,000 mL | `*_ml`, `reading_ml` | 47.000 L → `47000` |
| Fuel unit price | `INTEGER` in 0.0001 PKR per litre | `*_10k` | PKR 393.6400/L → `3936400` |
| Timestamps | UTC text `YYYY-MM-DDTHH:MM:SS.SSSZ` (ms precision) | `*_utc` | `2026-10-15T13:54:44.123Z` |
| Business date | `TEXT 'YYYY-MM-DD'`, computed from `Asia/Karachi` wall clock; never derived by slicing a UTC timestamp | `business_date` | `2026-10-06` |

## 2. Calculation policy

**BR-1 — One rounding, at the boundary.** All math in Python `Decimal`; round **half-up**
(`ROUND_HALF_UP`) exactly once per transaction to produce the stored integer. Intermediate
values are never rounded. Report totals sum stored integer paisa; reports never re-round
from decimals.

**BR-2 — Canonical sale amount.** For a sale of `Q` mL at price `P` (in 0.0001-PKR units):

```
amount_paisa = round_half_up(Q × P / 100_000)            [paisa]
```

because amount_PKR = (Q/1000) × (P/10,000), ×100 for paisa = Q × P / 10⁵.
`amount_paisa_for(Q: int, P: int) -> int`; both inputs must be non-negative ints;
a `float` anywhere in the chain raises `TypeError` (fail loud).

**BR-3 — Unit conversions.**
`to_paisa(PKR)` = `round_half_up(PKR × 100)` [paisa];
`to_ml(litres)` = `round_half_up(litres × 1000)` [mL];
`to_price_10k(PKR)` = `round_half_up(PKR × 10_000)` [0.0001-PKR units].
Reverse functions (`from_paisa`, `from_ml`, `from_price_10k`) are exact to 2/3/4 dp and
are display-only — never re-fed into arithmetic.

## 3. Nozzle liters

**BR-4 — Interval quantity.** For one nozzle, one validated reading interval
`[prev_reading_ml, curr_reading_ml]`:

```
liters_sold_ml = curr_reading_ml − prev_reading_ml        [mL, ≥ 0]
```

Normal case. A backwards interval (curr < prev) is **rejected** unless a `meter_events`
row of type `rollover` with a documented `wrap_value_ml` covers the pair, in which case:

```
liters_sold_ml = (wrap_value_ml − prev_reading_ml) + curr_reading_ml    [mL]
```

A `reset`/`replacement` event makes the interval quantity **unknown (NULL)** — never a
negative sale; the day-close checklist raises a warning. Duplicate `closing` readings
per nozzle per day are rejected (partial unique index, E1).

**BR-5 — Product totals.** For business date `d` and fuel product `p`:

```
liters_sold(d, p) = Σ liters_sold_ml over all nozzles of p with quantity ≠ NULL   [mL]
```

## 4. Revenue

**BR-6 — Priced interval.** Each interval is priced by the `price_history` row with the
greatest `effective_from_utc` **strictly before** the interval's `occurred_at_utc`
(mid-day price changes take effect only after an authorizing `segment` snapshot reading;
the interval ending at the change moment keeps the old price — never retrospective).

**BR-7 — Revenue.** Per interval, `revenue_paisa = amount_paisa_for(quantity_ml, unit_price_10k)`
(BR-2 — rounding once, at the interval). Per product per day:

```
revenue(d, p) = Σ revenue_paisa over priced intervals of p          [paisa]
```

Revenue rows are immutable once posted. A price change never reprices old intervals;
old `price_history` rows are never edited.

## 5. Settlement identity

**BR-8 — Every closed day must satisfy:**

```
meter_revenue(d) = settled_cash(d) + settled_bank(d) + settled_other(d)
                 + paid_now_on_ticket(d) + new_receivables(d)          [paisa]
```

where `settled_*` come from `sales_settlements` (categories `cash`, `bank`, `other`,
`paid_now_on_ticket`), and

```
new_receivables(d) = Σ credit_sales.gross_amount_paisa(d) − Σ credit_sales.paid_now_paisa(d)
```

`paid_now_paisa` is cash taken at the moment of the credit ticket and pins to that
ticket only. **Old-debt recovery is not revenue**: a `customer_payments` row allocated
to an earlier ticket creates `cash_receipts(category='credit_recovery')` and reduces
the receivable; it never touches `sales_settlements` or revenue. The unresolved
difference must be 0 before a day can close.

## 6. Stock

**BR-9 — Theoretical close.** Per product (per tank where tracked), for business date `d`:

```
theoretical_close(d) = opening(d) + Σ deliveries(d) − Σ sales(d) + Σ adjustments(d)   [mL]
```

`Σ sales` sums one `inventory_movements` row per validated reading interval (idempotent
via unique `source_uuid`; re-posting is rejected). Sales movements originate only from
meter-derived intervals — never typed totals.

**BR-10 — Variance.** After a physical dip/count:

```
variance_ml = physical_count_ml − theoretical_close(d)    [mL]
```

A variance never silently overwrites the theoretical figure. An approved variance is
posted as an explicit `adjustment` movement (manager reason required, audited); the
balance remains `SUM(quantity_ml)` at all times. A sale that would drive tracked stock
negative is blocked (service + trigger F2); the manager must post an audited adjustment first.

## 7. Cash position

**BR-11 — Expected closing.** For business date `d` (cash method only — bank activity
never enters the cash book):

```
expected_close(d) = opening_cash_paisa(d)
                  + Σ cash_receipts.amount_paisa(d)       [paisa]
                  − Σ cash_payments.amount_paisa(d)       [paisa]
```

Reversed rows are excluded; reversal rows negate the original. Each receipt/payment is
counted exactly once (`v_daily_cash_position`).

**BR-12 — Cash difference.**

```
cash_difference(d) = physical_cash_paisa(d) − expected_close(d)    [paisa]
```

The difference is **reported, never auto-written-off or auto-adjusted**. Resolution
is a manager decision recorded as an audited note or expense.

## 8. Credit ledger

**BR-13 — Closing receivable.** For customer `c`:

```
receivable(c) = opening_receivable_paisa(c)
              + Σ posted ticket gross_amount_paisa
              − Σ posted ticket paid_now_paisa
              − Σ allocations to posted tickets
              − Σ authorized credit adjustments           [paisa]
```

Ledger-derived, never typed. An opening-balance error is fixed by an audited
opening-balance adjustment, never by editing the computed figure.

**BR-14 — Allocation.** A payment splits across unpaid tickets in **FIFO** order
(oldest unpaid first; configurable via `app_settings.allocation_order`). `paid_now`
from a ticket pins to that ticket only, never to older debt. DB guard (G1) enforces:

```
Σ allocations(amount_paisa) ≤ payment.amount_paisa    for each payment
```

Over-allocation aborts the whole operation atomically — nothing is persisted.
Allocations are immutable; changes go through audited reverse-and-reallocate rows.

## 9. COGS and P&L

**BR-15 — Periodic weighted average.** Costing period = **one business day**, per fuel
product. Computed at day close in `Decimal` (avg kept to 6 dp, paisa-per-mL) and
**frozen** into the closing record's snapshot — old P&L never changes when new
purchases are entered:

```
avg_cost_paisa_per_ml = (opening_value_paisa + Σ delivery_total_cost_paisa)
                      / (opening_qty_ml + Σ delivery_qty_ml)          [Decimal]
COGS(d, p)  = round_half_up(liters_sold_ml × avg_cost_paisa_per_ml)  [paisa, rounded once]
closing_value_paisa = round_half_up(closing_qty_ml × avg_cost_paisa_per_ml)  [paisa]
```

Delivery cost includes `total_cost_paisa + other_cost_paisa`. Opening value
precedence: previous closing's cost snapshot → manager-entered opening value →
unknown (day flagged **estimated**). If no cost basis exists but liters were sold,
`COGS = NULL` and the product is flagged estimated.

**BR-16 — Profit.**

```
gross_profit(d, p) = revenue(d, p) − COGS(d, p)        [paisa; NULL if COGS unknown]
net_profit(d)      = Σ gross_profit − operating_expenses(d)   [paisa]
```

Figures are labeled **estimated** whenever the cost basis is missing. Profit is never
derived from drawer cash; debt recovery is never revenue.

## 10. Worked example — register day 2026-10-06

Nozzle closing readings (liters): PG+ 1 = 47 L, PG+ 2 = 450 L, HSD 3 = 108 L,
HSD 4 = 57 L. Seed prices effective 2026-10-06: petrol PKR 393.6400/L,
diesel PKR 397.7600/L.

**BR-4 → quantities (mL):**

```
petrol: 47 L + 450 L = 497 L = 497,000 mL
diesel: 108 L + 57 L = 165 L = 165,000 mL
```

**BR-7 → revenue (BR-2 per interval, one rounding each).**

Petrol: Q = 497,000 mL; P = 393.64 × 10,000 = 3,936,400:

```
497,000 × 3,936,400 / 100,000 = 19,563,908.0
→ 19,563,908 paisa = PKR 195,639.08
```

Diesel: Q = 165,000 mL; P = 397.76 × 10,000 = 3,977,600:

```
165,000 × 3,977,600 / 100,000 = 6,563,040.0
→ 6,563,040 paisa = PKR 65,630.40
```

**Day totals:**

```
meter revenue = 19,563,908 + 6,563,040 = 26,126,948 paisa = PKR 261,269.48
liters sold   = 497,000 + 165,000 = 662,000 mL = 662 L
```

BR-8 then requires: `26,126,948 paisa = settled_cash + settled_bank + settled_other
+ paid_now_on_ticket + new_receivables`, with unresolved difference 0 to close.

## 11. Corrections and closing revisions

**BR-17 — Reversals, not edits.** Posted rows are immutable (trigger group C blocks
UPDATE/DELETE). A correction posts a linked reversal row (`reverses_uuid`) plus the
new row; both are audited. Reading corrections after sales were posted append a
single `adjustment` inventory movement for the delta — original movement rows are
never edited.

**BR-18 — Reopen creates a new revision.** Correcting a closed day requires the
audited reopen workflow (manager reason → `reopen` event → `status='reopened'` →
correction rows → close again). The new close writes
`business_day_closings(business_date, closing_revision = old + 1)`; the old closing
record and its cost snapshot are never overwritten. Reports print the revision
number. Only the most recent day may be reopened.
