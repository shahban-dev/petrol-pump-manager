# Petrol Pump Manager — Test Report

**Version:** 1.0.0 · **Schema:** v15 · **Date:** 2026-10-15 (PKT)
**Scope:** full automated suite, run once against the source tree at
`~/workspace/petrol_pump_manager/` (not the Windows installer build — see
§8 "Outstanding limitations").

## 1. Run result

Command:

```
cd ~/workspace/petrol_pump_manager && ./.venv/bin/python -m pytest app/tests -q
```

Result: **475 passed, 0 failed** in 187.03 s.

Breakdown: 472 pre-existing tests + 3 new tests in
`app/tests/test_offline.py` (acceptance Test 16, added this phase).
The suite was run exactly once for this report; collection confirmed 475
tests beforehand. No re-runs, no flakes observed on this run.

Warnings (informational only, 35 total): DeprecationWarnings from
`openpyxl` (uses `datetime.datetime.utcnow()` internally in packaging
core/writer). They come from the third-party library, not app code, and do
not affect test outcomes.

## 2. Environment

Verified inside the project venv (`.venv`) immediately before the run:

| Component | Version |
|---|---|
| Python | 3.12.3 (GCC 13.3.0) |
| SQLite | 3.45.1 |
| PySide6 | 6.12.0 |
| pytest | 9.1.1 |

All service/database tests run against temp SQLite databases; UI tests run
headless/offscreen (`test_smoke_offscreen.py`). No network access is used or
required — proven, not assumed (see Test 16).

## 3. Acceptance tests → pinning tests (spec §25 / design plan §10)

Every one of the 20 mandatory acceptance tests is pinned by a named
regression test. Function names are exact (file::function):

| # | Acceptance test (spec) | Pinned by |
|---|---|---|
| 1 | Nozzle calc 84,938→84,985 = 47 L, carry-forward | `test_nozzle_service.py::test_nozzle_calculation_and_carry_forward` (PREV_LITRES="84938", CURR_LITRES="84985", liters_sold_ml == 47_000) |
| 2 | Duplicate daily reading rejected (E1) | `test_nozzle_readings.py::test_duplicate_closing_reading_rejected` (+ `test_phase7_attack.py::test_duplicate_closing_reading_rejected` for the adversarial/raw-SQL angle) |
| 3 | Price change: old sales/tickets keep original values | `test_price_service.py::test_price_change_keeps_old_values` (+ `test_price_change_blocked_without_snapshot` for the §11.2 mid-day rule) |
| 4 | Stock 5000+2000−500=6500 L; no double posting | `test_inventory_service.py::test_automatic_stock_no_double_deduction` (asserts theoretical_ml == 6_500_000, repost is a no-op) |
| 5 | Credit sale 40 L × 270 = PKR 10,800 | `test_phase4_credit.py::test_issue_ticket_math` (gross_amount_paisa == 1_080_000) |
| 6 | Partial payment 3,000 → 7,800 remains | `test_phase4_credit.py::test_outstanding_worked_example` (paid-now 300_000 paisa → outstanding 780_000) |
| 7 | Old-debt recovery 2,000 → 5,800; revenue untouched | same `test_outstanding_worked_example` (2,000 recovery → 580_000; gross unchanged, "recovery is a receipt, not revenue") |
| 8 | Reconciliation: meter revenue = settlements + new receivables; old recovery excluded; duplicate settlement rejected | `test_phase5_cashbook.py::test_settlement_identity_and_old_recovery_exclusion` (+ `test_phase4_credit.py::test_credit_liters_guard_and_reconcile`) |
| 9 | Daily cash 20,000+50,000+3,000−5,000−8,000=60,000 | `test_phase5_cashbook.py::test_daily_cash_worked_example` (exact 60,000 expected; idempotency asserted) |
| 10 | Closed-day protection; authorized reopen works, audited | `test_closed_day.py::test_insert_on_closed_day_rejected` and `test_closed_day.py::test_reopen_workflow_allows_correction_and_is_audited` |
| 11 | Audit trail correctness; posted rows immutable | `test_audit.py::test_insert_creates_complete_audit_row`, `test_update_creates_old_and_new_snapshots`, `test_delete_attempt_on_deletable_table_is_audited`, `test_audit_log_cannot_be_updated`; immutability triggers in `test_phase4_credit.py::test_immutability_triggers_raw_sql`, `test_immutability.py` |
| 12 | Over-allocation 6,000 against 5,000 → atomic failure | `test_phase4_credit.py::test_explicit_splits_and_atomic_over_allocation` (+ DB backstop `test_phase4_credit.py::test_overallocation_trigger_backstop`) |
| 13 | Inventory underflow blocked | `test_inventory_service.py::test_negative_stock_blocked` (service + F2 trigger) |
| 14 | Backup consistency under WAL | `test_phase8_acceptance.py::test_acceptance_14_backup_under_wal_with_open_writer` (snapshot opens, passes integrity, contains expected rows) |
| 15 | Restore recovery, identical balances | `test_phase8_acceptance.py::test_acceptance_15_restore_round_trip_balances` |
| 16 | Fully offline operation | `test_offline.py::test_no_network_imports_in_app_code` (static AST scan: zero socket/ssl/urllib/http/requests/httpx/aiohttp/websockets/smtplib/ftplib/telnetlib imports in app code), `test_offline.py::test_no_qt_networking_references_in_app_code` (zero QNetwork*/QTcpSocket refs), `test_offline.py::test_full_daily_workflow_with_sockets_disabled` (full open→close workflow with `socket.socket` sabotaged) |
| 17 | Transaction rollback, zero partial writes | `test_rollback.py::test_failed_transaction_leaves_zero_partial_writes` (+ `test_failed_trigger_abort_rolls_back_statement`) |
| 18 | Timestamp boundaries: UTC storage, PKT business date, midnight rollover | `test_timecalc.py::test_business_date_for_midnight_boundary`, `test_business_date_for_karachi_late_night`, `test_business_date_for_karachi_evening` + `test_timestamps.py::test_created_at_utc_immutable_direct_sql` |
| 19 | Closing revisions: close→reopen→correct→close, both kept | `test_close_reopen.py::test_reopen_correct_reclose_creates_new_revision` (+ `test_audit_trail_of_full_lifecycle`, `test_phase7_reopen.py::test_reopen_non_latest_day_rejected`) |
| 20 | Financial calculations: precision, rounding, COGS, reversals | `test_calculations.py::test_spec_test1_quantity_math` + whole `test_calculations.py`; COGS/P&L in `test_phase5_pnl.py`; reversals in `test_phase4_credit.py::test_reverse_ticket`, `::test_reverse_payment`, `::test_reverse_ticket_closed_day_rules` |

Additional coverage worth noting (not in the 20): the real-register
fixture in `test_phase6_register_fixture.py`
(`test_fixture_nozzle_readings`, `test_fixture_fuel_sales_totals`,
`test_fixture_diesel_stock`, `test_daily_register_matches_paper_section_order`
— the daily register mirrors the paper's section order), the Urdu/English UI
parity in `test_phase6_localization.py` (dynamic validation-message coverage
complete), exports in `test_phase6_exports.py` (PDF with embedded Urdu font,
typed numeric XLSX cells, BOM CSV), and 23 backup tests in
`test_phase8_backup.py` plus 11 in `test_phase8_acceptance.py`.

## 4. Deliverables audit (§29 — all 19 items)

| # | Deliverable | Status | Location |
|---|---|---|---|
| 1 | Source code | ✅ Present | `app/` |
| 2 | PySide6 UI | ✅ Present | `app/ui/` (12 pages: dashboard, register, readings, stock, deliveries, credit, payments, suppliers, other materials, expenses, reports, settings, backups) |
| 3 | Schema + migrations | ✅ Present | `app/database/migrations/001_core_schema.sql` … `015_phase8_backup_trigger.sql` (15 migrations, schema v15) |
| 4 | Trigger SQL | ✅ Present | `002_timestamp_triggers.sql`, `003_audit_triggers.sql`, `004_integrity_triggers.sql`, `015_phase8_backup_trigger.sql` |
| 5 | Views + indexes | ✅ Present | `app/database/views/v_effective_nozzle_readings.sql`; 9 `CREATE INDEX` in `001_core_schema.sql` plus indexes in later migrations |
| 6 | Services + repositories | ✅ Present | `app/services/` (18 modules incl. business_day, nozzle, inventory, cashbook, customer_credit, payment, backup, reporting), `app/repositories/` (11 modules) |
| 7 | Tests | ✅ Present | `app/tests/` — 475 tests, all passing |
| 8 | Backup/restore tools | ✅ Present | `app/services/backup_service.py` (+ Backups page); covered by `test_phase8_backup.py` (23) + `test_phase8_acceptance.py` (11) |
| 9 | Daily/monthly/annual reports | ✅ Present | `reporting_service.py`: `daily_register`, `pnl_daily`, `pnl_monthly`, `pnl_annual` + 12 more report builders |
| 10 | PDF/XLSX/CSV exports | ✅ Present | `app/services/report_export.py`; pinned by `test_phase6_exports.py` |
| 11 | Urdu + English UI | ✅ Present | `app/localization/`; parity pinned by `test_phase6_localization.py`; Urdu PDF export pins font embedding + RTL |
| 12 | Windows installer | ⚠️ **Gap** | Defined but **never compiled or run**: `installer/petrol_pump_manager.spec` (PyInstaller one-dir) and `installer/petrol_pump_manager.iss` (Inno Setup, `PrivilegesRequired=lowest`, data dir excluded from uninstall). No Windows host was available in the build environment. NOT code-signed → Windows SmartScreen will show "Unknown publisher" on install. |
| 13 | Example config | ✅ Present (new this phase) | `config/example_config.yaml` — every `app_settings` key documented with defaults |
| 14 | English user guide | ✅ Present | `documentation/user_guide.md` |
| 15 | Urdu user guide | ✅ Present | `documentation/user_guide_ur.md` |
| 16 | Install/upgrade/backup/recovery docs | ✅ Present | `documentation/install.md` (§§2–8), `documentation/recovery.md` |
| 17 | Business-rules doc | ✅ Present | `documentation/00_design_plan.md` (accounting model, spec §§, §11 safe defaults, §25 acceptance list) |
| 18 | Test report | ✅ Present (this file) | `documentation/test_report.md` |
| 19 | Rebuild instructions | ✅ Present | `documentation/install.md` §8 "Rebuilding from source" |

**Audit result: 18/19 present, 1 explicit gap** — the Windows installer is
specified and scripted but has never been compiled or executed on a real
Windows machine, and the binaries are unsigned. This is the single
unresolved deliverable risk; it is the subject of the Phase 9 manual
acceptance script (`documentation/phase9_acceptance_script.md`).

## 5. Outstanding limitations (honest, explicit)

1. **Installer uncompiled and untested.** `installer/petrol_pump_manager.spec`
   and `installer/petrol_pump_manager.iss` exist, but no Windows build host
   was available: PyInstaller was never run, the Inno Setup `.exe` was never
   produced, and nothing has been installed on a real Windows 10/11 machine.
   Everything validated so far runs from the source tree on Linux.
2. **Not code-signed.** Expect Windows SmartScreen "Unknown publisher" on the
   unsigned build (documented in `install.md` §7); *More info → Run anyway*
   is the workaround until the owner buys a certificate.
3. **No bank ledger in v1.** Bank settlements are tracked (method="bank" on
   expenses/payments), but the physical-cash identity deliberately excludes
   them — there is no bank-account reconciliation ledger. `test_phase5_cashbook.py::test_bank_expense_never_touches_cash`
   pins this behavior.
4. **Duplicate supplier names allowed.** Suppliers are keyed by `id`; the app
   does not enforce name uniqueness (documented behavior).
5. **One transient 800-paisa flake, never reproduced.** During Phase 5 a
   single 800-paisa (PKR 8.00) discrepancy was observed once in a test run;
   it did not recur in 4+ subsequent full-suite reruns, including this
   report's clean 475/475 run. Root cause unidentified; flagged here rather
   than hidden.
6. **Dynamic validation-message Urdu coverage is complete** per
   `test_phase6_localization.py` — all `err.*` keys resolve in both
   languages; no gaps known.
7. **Paper-register fixture scope.** The real-register fixture
   (`test_phase6_register_fixture.py`) uses the transcribed 06-10-2026 day
   with official notified rates (petrol 393.64, diesel 397.76) as the price
   seed; low-certainty cash scribbles from the photo are deliberately
   excluded from the fixture (see `register_transcription_notes.md`).
