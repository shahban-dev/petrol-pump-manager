# Phase 4 Build Notes — Customer Credit

**Date:** 2026-10-15 · **Schema:** v9 (migrations 008 new, 009 view corrections) · **Tests:** 220 passed (171 Phase-1/2/3 intact + 49 new)

## What was built

| Area | Files |
|---|---|
| Migrations | `app/database/migrations/008_phase4.sql` (7 tables, 3 views, ~60 triggers); `009_phase4_view_fixes.sql` (redefines the two ledger views — see §11.1); `migrator.py` → v9 |
| Models | `app/models/credit.py` (CustomerInfo, TicketLineInfo, TicketInfo, PaymentInfo, AllocationInfo, LedgerEntry, CustomerStatement, AdjustmentInfo) |
| Repositories | `app/repositories/customer_repository.py` (all seven tables + FIFO query + view readers) |
| Services | `app/services/customer_credit_service.py`, `app/services/payment_service.py`; `_base.py` += IntegrityError translations for all new trigger messages |
| Close flow | `business_day_service.py`: real `_check_credit_tickets_saved` (ticket integrity + credit-liters-guard warnings) and `_check_allocations_valid`; both removed from `_NA_CHECKS` (settlements/cashbook remain `na` for Phase 5) |
| Sales | `sales_service.py`: `_credit_guard_conn` rewritten for the header+lines schema (signed liters, posted only); `reconcile_day.new_receivables_paisa` is now real (Σ signed `gross − paid_now`) |
| UI | `app/ui/customers/page.py` (Customer Credit), `app/ui/payments/page.py` (Customer Payments), sidebar entries + service wiring in `main_window.py`; `en.json`/`ur.json` 233 → 391 keys, exact parity |
| Tests | `app/tests/test_phase4_credit.py` (37), `app/tests/test_ui_phase4.py` (12); conftest += `phase4_services` fixture, `seed_credit_day` helper |

## Schema summary (008)

- `customers` — mutable master data (code unique, opening receivable entered once, optional warning-only credit limit, active/inactive).
- `credit_sales` — ticket header (uuid, customer, day, gross, paid_now, receipt_no, status posted|reversed, reverses/reversed_by linkage). Immutable except the whitelisted posted→reversed flip.
- `credit_sale_lines` — per-product lines (quantity ml, 10k price, exact line amount enforced by trigger). A ticket's lines may never exceed its gross.
- `customer_payments` — one row per money received (method cash|bank|other). Same reversal pattern as tickets.
- `payment_allocations` — immutable payment→ticket links; Group G trigger enforces: payment/ticket posted, same customer, ≤ ticket unpaid, Σ allocations ≤ payment amount — atomically.
- `payment_allocation_reversals` — audited full/partial cancellation of an allocation; effective allocation = posted − reversed.
- `customer_opening_adjustments` — signed, reasoned fixes to opening receivables; the computed balance is never editable.
- Views: `v_effective_allocations`, `v_outstanding_receivables` (the authoritative balance, design §7), `v_customer_ledger` (chronological entries + running balance).

Key rules enforced at the DB: closed-day D1/D2 (reversing a closed day's ticket requires reopen, §11.13), no double allocation, no allocation to reversed tickets, line-amount formula, nozzle↔product consistency, reversal rows must mirror originals.

## §11 assumptions that needed further interpretation

1. **View corrections (migration 009).** The 008 views had two defects, verified numerically before fixing: (a) `v_customer_ledger` double-subtracted `paid_now` (ticket debited `gross−paid_now` plus a separate `paid_now` credit); (b) both views negated reversals twice (`status='reversed' → 0` plus the reversal row's `−(gross−paid_now)`). Fixed rule: a reversed original keeps its posted contribution (the flip is a marker; the row is never erased) and the reversal row negates it; ticket entries debit the full gross with paid-now as a separate credit. Voiding a ticket with `paid_now = P` leaves `−P` (cash held as customer credit). Ledger running balance and outstanding now agree on all scenarios (pinned by tests). 008 itself untouched — append-only migrations.
2. **Strict `occurred_at_utc` validation.** Its Asia/Karachi calendar date must equal the business date. Tension with §5.1's post-midnight note (a 00:20 PKT ticket on the still-open day is rejected) — kept strict; relax only on product-owner decision.
3. **Money crosses service boundaries as integer paisa; quantities as Decimal litres** (→ ml). Matches the existing UI dialog signatures.
4. **Fully-reversed (effective 0) allocations are legitimate history**, not close-checklist failures.
5. **paid_now stays on the original ticket** on reversal (008 CHECK forces reversal `paid_now = 0`); a payment whose allocations were reversed remains visible as *unallocated* — per §7 only allocations reduce outstanding.
6. **Credit limits are warning-only** (§11.7): the ticket posts; the warning rides on `TicketInfo.warnings` and the UI shows it.
7. **Allocation order** defaults to FIFO via missing-setting fallback (`allocation_order` not seeded, to keep the post-migration audit log empty); configurable `fifo`|`manual`.

## Deliberate limitations (for Phase 5+)

- `sales_settlements` rows are still not written (Phase 5 cash book); `reconcile_day` now reports real `new_receivables_paisa` with settlements still pending.
- `paid_now` cash has no `cash_receipts` counterpart yet — Phase 5 will source it from `credit_sales` (`source_table='credit_sales'`).
- Receipt printing uses QTextDocument + QPrintDialog (simple, offline); PDF receipts arrive with Phase 6 reporting.

## Test maintenance note

No Phase-1/2/3 tests were weakened. `test_phase3_acceptance` expectations for `reconcile_day` were updated to the now-real `new_receivables_paisa` (honest values, not placeholders).

## Acceptance-test mapping

- **Test 5** — `test_issue_ticket_math`, `test_outstanding_worked_example`, `test_issue_ticket_writes_no_stock` (40 L @ 270 = 10,800; receivable += unpaid; no inventory movement).
- **Test 6** — `test_outstanding_worked_example` (3,000 on 10,800 → 7,800; ticket gross unchanged — receipt, not sale).
- **Test 7** — `test_outstanding_worked_example` (2,000 recovery → 5,800; revenue untouched).
- **Test 12** — `test_explicit_splits_and_atomic_over_allocation`, `test_overallocation_trigger_backstop` (6,000 on 5,000 fails atomically via service and via raw SQL).
- Plus: FIFO ordering, paid-now pinned to its ticket, reallocation audit workflow, statement/ledger correctness, credit-limit warning (not blocking), liters-guard warning, ticket/payment reversals, closed-day trigger backstops, immutability via raw SQL.

## Readiness for Phase 5

- `v_outstanding_receivables` and `v_customer_ledger` are the exact inputs for the cash-book settlement identity (`meter_revenue = settlements + new_receivables`); `reconcile_day` already exposes real receivables.
- `customer_payments` rows with `method='cash'` are the source for Phase-5 `cash_receipts(category='credit_recovery')`; `credit_sales.paid_now_paisa` is the source for `cash_receipts` at sale time.
- `sales_settlements` table + cash/bank categories, expense/supplier-payment modules, and P&L are the remaining Phase-5 scope.
