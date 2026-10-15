# Phase 6 Build Notes — Reports & Localization

**Date:** 2026-10-15 · **Schema:** v12 (no new migrations — every report reuses existing views/services) · **Tests:** 338 passed (245 prior intact + 93 new), full suite green

## Stability note (2026-10-15)

`/tmp` is a 512 MB tmpfs; parallel pytest runs filled it to 100%, causing
transient `sqlite3.OperationalError: database or disk is full` failures in
full-suite runs (3 tests one run, 2 sidebar-count order-dependent UI failures
in another). Stale `/tmp/pytest-of-root` dirs were cleaned; reruns are fully
green. If "database or disk is full" reappears, rerun with a roomier `TMPDIR`
— it is an environment artifact, not a code defect. (Same class of one-off
hiccup as the Phase 5 note.)

## What was built

| Area | Files |
|---|---|
| Report contract | `app/models/report.py` — `ReportResult`/`ReportColumn`/`ReportFilter`/`ReportSpec`, `REPORT_IDS` (21 ids). Rows carry raw integers (paisa/ml/scaled price); formatting happens at render. |
| Reporting service | `app/services/reporting_service.py` (~2,400 lines) — `list_reports()` + `generate_report(...)` for all 21 §22 reports, read-only, committed records only |
| Export engine | `app/services/report_export.py` — PDF (ReportLab, A4), XLSX (openpyxl), CSV; temp-write + atomic rename; `ValidationError` on bad input |
| Fonts | `app/resources/fonts/` — Noto Sans Arabic Regular/Bold (static TTF, OFL 1.1), `OFL.txt`, `FONTS.md` provenance/coverage notes. No system-font reliance (Windows target) |
| Reports UI | `app/ui/reports/page.py` — picker, dynamic filters from `ReportSpec`, register-style HTML preview, print (QPrintDialog), working PDF/XLSX/CSV export buttons; wired into sidebar; bilingual + RTL |
| Real-register fixture | `app/tests/fixtures/register_day_2026_10_06.json` + `documentation/register_transcription_notes.md` (user's paper register, 2026-10-06, Zeishan Petroleum Service PSO) |
| Localization | `errors.py` message keys (`message_key` + `params`, `str(exc)` stays English); `localize_error()`; 308 raise sites keyed across 16 service files; `en.json`/`ur.json` 966 keys exact parity; RTL via `apply_language_direction`; close-checklist items keyed |
| Deps | `requirements.txt`: reportlab, openpyxl, arabic-reshaper, python-bidi promoted to active runtime deps; `requirements-dev.txt`: pypdf (test-only) |
| Tests | `test_phase6_reporting.py` (39), `test_phase6_exports.py` (17), `test_phase6_localization.py` (13), `test_phase6_register_fixture.py` (7), `test_ui_phase6_reports.py` (17) |

## The 21 reports

daily_register (paper-mirrored sections) · nozzle_readings · fuel_sales ·
cashbook · sales_reconciliation (§6 identity + unresolved-difference warning) ·
customer_credit_register · customer_statement (running balance = §7 formula) ·
payment_receipt (uuid → receipt_no fallback) · outstanding_balances (as-of
cutoff) · fuel_stock · tank_reconciliation · deliveries · supplier_balances ·
other_materials · expenses · pnl_daily (frozen closing snapshots) ·
pnl_monthly · pnl_annual · cash_difference (never written off) ·
audit_history (5,000-row cap, disclosed) · backup_history.

Every report carries generated_at_utc, period labels EN+UR, and the latest
closing revision for single-day reports (reopened days get a bilingual
revision note).

## Paper-mirror fix (user priority)

The first `daily_register` build emitted Payments detail rows before Receipts
(order of `ORDER BY kind`: `'payment' < 'receipt'`) — diverging from the
paper. Fixed: rows regrouped Receipts (details + total) then Payments
(details + total). Mandated section order now enforced in service and UI
preview: nozzle readings → fuel sales → stock → receipts → payments →
customer credit → daily balances. Pinned by
`test_section_order_with_cash_detail_rows` and
`test_daily_register_section_order_matches_paper_register`.

## §11 interpretations recorded this phase

1. **Reversal display:** flipped originals keep posted contribution; reversal
   rows negate (corrected 009/011 rule) — applied in
   `customer_credit_register` and `expenses`.
2. **Unresolved §6 difference** is a bilingual warning note, never an error;
   pre-close reports may show it nonzero.
3. **Single-day default** = the open business day (noted EN+UR); empty ranges
   are valid empty reports.
4. **`outstanding_balances` as-of** uses `v_customer_ledger` with an
   `occurred_at_utc` cutoff; customers created after the cutoff are excluded.
5. **`audit_history`** filters on audit `occurred_at_utc` (machine UTC),
   newest-first, capped 5,000 rows with truncation disclosed.
6. **`payment_receipt`** reference resolves uuid first, then `receipt_no`.
7. **P&L monthly/annual** sum per-day `daily_pnl` snapshots; closed days are
   frozen, missing days flagged estimated.
8. **Urdu period labels** use a fixed EN/UR month table (`اکتوبر 2026` style).
9. **Diesel variance:** system formula physical − theoretical_close = +200 L
   (3,260 − 3,060); the paper's فرق 35 L = dip − pre-sale book total
   (3,260 − 3,225). Both asserted against the same committed inputs; the
   system formula was NOT forced to 35. Difference documented in the test.
10. **Localization:** Western digits in both languages (matches the paper);
    amounts `12,345.67`; dates DD-MM-YYYY; technical parse details
    (e.g. Decimal errors) stay English inside translated sentences — honest,
    not machine-garbled.
11. **Real-register fixture:** official 2026-10-06 notified rates
    (petrol 393.64, diesel 397.76) seed price_history; LOW-certainty cash
    scribbles (198,800 / 66,163 / 265,963) are asserted to appear nowhere;
    petrol opening stock (~14 L, uncertain) is not asserted.

## Urdu rendering verification

PDF content stream decoded through the embedded font's ToUnicode map:
painted glyph sequence is byte-exact bidi visual order; pypdf extraction of
the Urdu PDF yields shaped Urdu (e.g. روزنامچہ) with no tofu; `/FontFile2`
embedded. XLSX money cells are numeric Decimals with PKR formats (never
float-derived); Urdu sheets set `rightToLeft`.

## Deliberate limitations

- No bank ledger (§11.14) — bank settlements tracked, excluded from cash.
- Audit-history cap at 5,000 rows (disclosed); raise if the manager needs more.
- `ticket_integrity` returns English strings; `credit_liters_guard` keeps
  English `"warnings"` with parallel `"warning_segments"` (test-pinned).

## Readiness for Phase 7

- `audit_history` report surfaces `audit_log` for verifying Phase 7's
  attack-style SQL tests.
- All financial tables already carry immutability/closed-day triggers from
  earlier phases; Phase 7 completes the audit/immutability/reopen coverage,
  migration tests, and restore workflows.
- No blockers. No applied migrations were edited; schema stays v12.
