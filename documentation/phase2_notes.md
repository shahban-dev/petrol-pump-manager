# Phase 2 Build Notes — Business Day & Register

**Date:** 2026-10-15 · **Schema:** v6 (migrations 005–006, new) · **Tests:** 138 passed (70 Phase-1 intact + 68 new)

## What was built

| Area | Files |
|---|---|
| Migrations | `app/database/migrations/005_phase2_closings.sql`, `006_phase2_readings.sql` |
| Migrator | `app/database/migrator.py` → `CURRENT_SCHEMA_VERSION = 6`, new expected tables/indexes/triggers/view |
| Time policy | `app/calculations/time.py` (UTC storage format, Asia/Karachi business dates, display formats) |
| Models | `app/models/business_day.py`, `app/models/nozzle.py` |
| Repositories | `app/repositories/business_day_repository.py`, `app/repositories/nozzle_repository.py` |
| Services | `app/services/_base.py`, `app/services/errors.py`, `app/services/business_day_service.py`, `app/services/nozzle_service.py` |
| UI | `app/ui/main_window.py` (sidebar), `app/ui/dashboard/page.py`, `app/ui/daily_register/page.py`, `app/ui/nozzles/page.py`, `app/ui/settings/page.py`, `app/ui/_helpers.py` |
| Localization | `app/localization/en.json`, `ur.json` (146 keys each, full parity, zero fallbacks) |
| View source | `app/database/views/v_effective_nozzle_readings.sql` (installed by 006) |
| Tests | `test_business_day_service.py`, `test_nozzle_service.py`, `test_close_reopen.py`, `test_phase2_acceptance.py`, `test_timecalc.py`, `test_ui_phase2.py`; `services` fixture added to `conftest.py` |

## §11 assumptions that needed further interpretation

1. **Meter reset / rollover (§11.1).** A backwards reading still requires a
   recorded `meter_events` row (unchanged trigger semantics). New: when the
   current reading is lower than the previous one, the interval quantity is
   **unknown from readings alone** — `liters_sold_ml` is `None`, never a
   negative number. The close checklist raises a *warning* (meter event
   recorded, excluded from product totals) instead of blocking. Rollover
   wrap-value arithmetic (meter_max) is deferred to Phase 3 — there is no
   wrap-value field yet, and inventing one would be guessing.
2. **Mid-day price change (§11.2).** Deferred to Phase 3 with `price_history`;
   `segment` readings are anticipated by the existing `reading_kind` values.
3. **Opening balances (§11.5).** First-day cash is manager-entered once;
   afterwards it is immutable and traceable via `opening_balances`
   (`source_business_date`, `source_closing_revision`). No typed-in ledger
   balances anywhere.
4. **Non-operating days (§11.6).** Terminal state — never "closed". New
   trigger `trg_nozzle_readings_bi_non_operating` rejects readings on them;
   cash/readings carry forward unchanged from the last closing record.
5. **Reversal vs. reopen (§11.13).** Open-day reading errors → correction
   rows (original preserved, linked by `supersedes_reading_id`). Closed-day
   errors → authorized reopen first; only the **most recent** day may be
   reopened (older days already feed newer opening balances).
6. **Date gaps.** Days open in date order; skipping dates requires an
   explicit, audited reason (the administrative recovery path), recorded in
   the `open` event payload.
7. **Close checklist honesty.** Items with no module yet (prices, deliveries,
   credit, settlements, cashbook, stock, tanks) report status `na` with
   "arrives with its module" — never a fake pass, never blocking.
8. **Correction storage.** The replacement row reuses `reading_kind='closing'`
   with `supersedes_reading_id` pointing at the original (no table rebuild,
   no new kind). E1's partial index excludes superseding rows; E2 validates
   against *effective* readings via `v_effective_nozzle_readings`.
9. **i18n scope.** All *static* UI strings are bilingual (EN+UR, RTL). Dynamic
   service/trigger validation messages stay in English in Phase 2; full
   validation-message translation is a Phase 6 concern per the phasing plan.

## Deliberate limitations (for Phase 3+)

- `business_day_closings` carries `petrol_litres_ml` / `diesel_litres_ml`
  convenience columns; any future product lives in `summary_json` until a
  general per-product closing-lines table exists.
- `validate_close` re-reads on its own connection inside `close_day`'s
  `BEGIN IMMEDIATE`; safe because the reserved lock blocks other writers
  between validation and commit.
- `app_settings` has no seed rows (seeding would pollute every fresh DB's
  audit log — pinned by `test_audit.py`); services treat missing keys as unset.

## Readiness for Phase 3

- `v_effective_nozzle_readings` is the exact input for meter-derived sales
  intervals (`sale` inventory movements keyed per nozzle+date).
- `opening_balances(balance_type='stock', fuel_product_id=…)` is ready for
  opening-stock carry-forward; `tanks` FK wiring point already exists on
  `nozzles.tank_id`.
- `reading_kind='segment'` / `'opening_snapshot'` values already allowed for
  the price-change snapshot workflow (§11.2).
