# Phase 9 — Manual Windows Acceptance Script

**Product:** Petrol Pump Manager 1.0.0 (schema v15)
**Time budget:** ~60–90 minutes, plus unattended build time
**Where:** a clean Windows 10 (1809+) or Windows 11 64-bit machine
**Tester:**

Date:

This script closes the single gap left by automated testing: the installer
was **defined but never compiled or run on a real Windows machine**, and the
build is **unsigned** (see `documentation/install.md` §9 — the five manual
steps remaining). Every item in §9 is exercised below. The 475 automated
tests all pass from source; this script validates the *built, installed*
product on Windows, by hand, against the real paper register.

> Convention: each step ends with a **PASS / FAIL** checkbox. Record actual
> values you observed, not what you expected — a failed step is data, not
> a disaster. Stop on any FAIL that corrupts data; otherwise continue and
> note it.

---

## A. Prerequisites

- [ ] Windows 10/11 64-bit machine (the "test machine"). Ideally a fresh
      install/VM with no Python and no prior Petrol Pump Manager install.
- [ ] A second machine (or the same VM after snapshotting) usable as the
      **build machine**: Python 3.12 64-bit installed, this repo checked
      out, network access for `pip install` of build deps.
- [ ] A **standard (non-admin) Windows user account** on the test machine
      (for §A4). Have its password.
- [ ] A USB drive (to carry the built installer; also proves offline
      transfer works).
- [ ] The station's paper register page for **06-10-2026** (or a photo of
      it) — the real day we re-enter: nozzle sales **47 / 450 / 108 / 57 L**
      (662 L total), notified rates **petrol PKR 393.64/L, diesel PKR 397.76/L**.
- [ ] A printer (or PDF printer) for the A4 Urdu PDF check.
- [ ] A Google Drive-synced folder path (optional but recommended) for the
      Drive-copy step.

---

## B. Build on the Windows build machine

Per `install.md` §8 ("Rebuilding from source") — run these **on Windows**,
since the bundle must be built on its target OS.

- [ ] `py -3.12 -m venv .venv && .\.venv\Scripts\activate`
- [ ] `pip install -r requirements.txt` (app deps) and PyInstaller per §8
- [ ] `pyinstaller installer\petrol_pump_manager.spec --noconfirm`
      → produces the one-dir bundle under `dist\PetrolPumpManager\`
- [ ] Build the Inno Setup installer → `PetrolPumpManager-Setup-1.0.0.exe`
- [ ] Copy the setup `.exe` to the USB drive.
- [ ] Sanity check: `dist\PetrolPumpManager\PetrolPumpManager.exe` launches
      on the **build machine** and shows the dashboard.

**Expected:** §9 item 4 begins here — the acceptance is against this built
installer, not the source tree.
☐ PASS ☐ FAIL — notes:

---

## C. Clean install (covers §9 items 1 & 3: clean-Windows install + SmartScreen)

Use a **fresh** Windows VM/machine, logged in as a **normal admin-capable
account first** (we do the non-admin variant in §D). Record the machine
spec: ______________________________

- [ ] Copy the setup `.exe` from USB. Double-click.
- [ ] **SmartScreen:** confirm the *expected* unsigned-build warning appears:
      *"Windows protected your PC — Microsoft Defender SmartScreen prevented
      an unrecognized app from starting"* with **Unknown publisher**.
      ☐ Appeared as expected ☐ Did NOT appear (record what happened — if it
      doesn't appear, that's still a finding for §7/§J)
- [ ] Click **More info → Run anyway** and complete the install. Accept the
      default per-user folder
      (`C:\Users\<you>\AppData\Local\Programs\PetrolPumpManager`).
      No UAC elevation prompt should appear.
- [ ] Tick **Create a desktop icon**.
- [ ] Launch from the Start Menu entry *Petrol Pump Manager*.

**Expected:** app opens, no crash, no elevation prompt.
☐ PASS ☐ FAIL — notes:

## D. Install under a standard (non-admin) user (covers §9 item 2)

- [ ] Log in as the **standard (non-admin) user** created in §A.
- [ ] Re-run the same setup `.exe` from USB. Confirm:
  - [ ] No administrator password / UAC elevation prompt appears
        (`PrivilegesRequired=lowest` in the `.iss`).
  - [ ] Install, launch, and first-run complete under this user.
  - [ ] The data folder is writable: enter one reading, close the app,
        reopen — the reading is still there.
- [ ] Confirm the data folder lives at
      `%LOCALAPPDATA%\PetrolPumpManager` (e.g.
      `C:\Users\<standard-user>\AppData\Local\PetrolPumpManager`), **not**
      under Program Files. Run in a terminal:
      `dir "%LOCALAPPDATA%\PetrolPumpManager"` — expect `data\`, `backups\`,
      `exports\`, `logs\`, `config\`.

**Expected:** full workflow possible as a standard user; live DB at
`data\petrol_pump.sqlite3`, never in the install folder.
☐ PASS ☐ FAIL — notes:

---

## E. First-run setup

On first launch the app asks for the station's opening state:

- [ ] **Station name:** `Zeishan Petroleum Service` (PSO).
- [ ] **Nozzles:** 4 nozzles —
      `1 PG+` and `2 PG+` → **Petrol**;
      `3 HSD` and `4 HSD` → **Diesel**.
      (This is the seed layout; confirm it is configurable later, not
      hard-coded.)
- [ ] **Tanks:** one Petrol tank, one Diesel tank; nozzle→tank wiring shown.
- [ ] **Opening cash in drawer** (PKR, required on the very first day).
- [ ] **Opening stock:** enter the tank dip levels from the paper register.
- [ ] **Opening meter readings** per nozzle (from the paper: the "previous"
      readings).
- [ ] **Prices:** petrol **393.64**, diesel **397.76** (the official
      2026-10-06 notified rates the fixture uses).

**Expected:** dashboard shows the open day with opening cash/stock/readings;
no errors.
☐ PASS ☐ FAIL — notes:

## F. Data-directory check (live DB is NOT in Program Files)

- [ ] Open `%LOCALAPPDATA%\PetrolPumpManager\data\` — confirm
      `petrol_pump.sqlite3` exists and has a recent timestamp.
- [ ] Open the install folder
      (`%LOCALAPPDATA%\Programs\PetrolPumpManager\`) — confirm **no**
      `.sqlite3` file lives there.
- [ ] In Settings → Storage, confirm the shown database path matches the
      `%LOCALAPPDATA%` location.

**Expected:** one live DB, in the data dir, never in the install tree or
the Drive folder.
☐ PASS ☐ FAIL — notes:

---

## G. The real 06-10-2026 day workflow

Re-enter the photographed paper day. Business date: **2026-10-06**.
(If the test machine's date differs, enter it as a historical day — the
app supports back-dated opening per the gap rules; record what you did.)

1. [ ] **Open day** 2026-10-06. Opening cash carries from first-run setup.
2. [ ] **Nozzle readings** (Sidebar → Nozzle Readings). Enter the closing
       readings that produce sales of **47 / 450 / 108 / 57 L**
       (petrol total 497 L, diesel total 165 L, **662 L total** — matches the
       register's پیٹرول سیل / ڈیزل سیل).
       - [ ] Each row shows previous → current and computed litres sold.
       - [ ] Try entering a reading **lower** than the previous without a
             meter event → must be **rejected** (rollover/reset guard, §11.1).
       - [ ] Try entering a second closing reading for the same nozzle+day
             → must be **rejected** (duplicate, E1).
3. [ ] **Prices** (Sidebar → Fuel Stock → Set price): petrol **393.64**,
       diesel **397.76**. Confirm the app asks for/uses the notified rates
       and that any price *change* requires a meter snapshot (mid-day price
       rule, §11.2).
4. [ ] **Delivery:** record a fuel delivery (supplier, invoice no., product,
       quantity, unit cost). Then try the **same invoice no. twice** →
       must be rejected (duplicate invoice guard).
5. [ ] **Credit ticket** (Customer Credit): issue a ticket to a customer,
       e.g. 40 L petrol at the day rate. Confirm gross = qty × price and a
       receivable is created. Try a ticket line with **no price on file** →
       must be refused.
6. [ ] **Customer payment** (Customer Payments): record a partial payment
       against the ticket. Confirm outstanding drops by the payment amount
       and a receipt row (not a sale) is recorded.
7. [ ] **Expense** (Expenses): record a cash expense. Confirm it appears in
       the cashbook as a payment, not in fuel sales.
8. [ ] **Guided day-close** (Sidebar → Daily Register → Close day):
       - [ ] Run **Check readiness** — resolve any checklist items it flags.
       - [ ] Enter **physical cash counted**; confirm the app shows
             expected vs physical and the difference (shortage/surplus) —
             and does **not** auto-write off the difference.
       - [ ] Close the day.
       - [ ] Confirm an **automatic backup** ran after the close
             (`backup.on_day_close` default on): check the Backups page for a
             new `PetrolPump_Backup_2026-10-06_*.sqlite` row with a
             *validated* status, and confirm the **day still closed even if
             you imagine the backup failing** (failure must never break a
             close).

**Expected:** 662 L sold; rates 393.64/397.76 applied; all guards reject;
day closes; auto-backup row present and validated.
☐ PASS ☐ FAIL — notes:

## H. Daily register report vs the paper + A4 Urdu PDF

- [ ] Sidebar → **Reports** → **Daily Register** for 2026-10-06. Compare
      section order against the paper register, top to bottom:
      1. Nozzle readings → 2. Fuel sales → 3. Stock → 4. Receipts →
      5. Payments → 6. Customer credit → 7. Daily balances.
      ☐ Matches ☐ Mismatch (describe):
- [ ] Spot-check totals: 662 L sales; petrol/diesel revenue = litres × rate
      (no rounding drift vs hand calculation).
- [ ] **Export → PDF (Urdu).** Switch UI language to Urdu (Settings) and
      export the daily register.
      - [ ] Open the PDF: Urdu text renders (embedded font, no tofu boxes),
            layout is **right-to-left**, columns mirrored.
      - [ ] Print to A4 (or PDF printer): everything fits, header shows
            station name "Zeishan Petroleum Service".
- [ ] Export → **XLSX** and **CSV** once; open both and confirm money cells
      are real numbers (not text) and Urdu values survive in CSV.

**Expected:** report mirrors the paper's sections/order; Urdu PDF is correct
RTL A4 output.
☐ PASS ☐ FAIL — notes:

---

## I. Backup & restore

- [ ] Backups page → set **Drive folder** to a Google Drive-synced folder
      (or any folder). Try setting it *inside* the app's data dir →
      must be **rejected**.
- [ ] **Backup now.** Confirm: history row appears with trigger `manual`,
      filename `PetrolPump_Backup_YYYY-MM-DD_HHMMSS.sqlite`, size > 0,
      status honest (validated / "sync unconfirmed" until Drive confirms).
      Record the **sha256** shown: _________________________
- [ ] Note a balance (e.g. a customer's outstanding) → _______________
- [ ] **Restore:** select the backup → Restore. Confirm the app:
  1. takes an **automatic safety backup first**,
  2. demands **explicit typed confirmation** (no silent restore),
  3. replaces the live DB and runs **post-restore verification**.
- [ ] After restore: the noted balance is **identical**; app opens the day
      normally. Restore a **corrupt file** (copy any non-DB file and rename
      to `.sqlite`) → must be **rejected by validation**, live DB untouched.

**Expected:** safety backup + typed confirmation + verified restore;
corrupt candidates rejected.
☐ PASS ☐ FAIL — notes:

## J. Correction flow (reopen with reason)

- [ ] Pick the closed 2026-10-06 day → **Reopen**, entering a reason
      (e.g. "reading typo on nozzle 2"). Confirm reopen of a *non-latest*
      closed day is refused if a later day exists.
- [ ] Correct the reading; confirm the audit log records reopen + correction.
- [ ] **Close again** → confirm a **new closing revision** is created and the
      old revision is preserved (history shows revision 1 and revision 2).
- [ ] Try editing the closed day **without** reopening (via the UI) →
      must be blocked.

**Expected:** old revision preserved; correction fully audited.
☐ PASS ☐ FAIL — notes:

## K. Fully offline run

- [ ] **Disable all network** on the test machine (airplane mode + unplug
      Ethernet; disable Wi-Fi). Confirm no network icon is active.
- [ ] Repeat a full day: open day → readings → prices → delivery →
      credit ticket → payment → expense → backup → close day → export PDF.
- [ ] Confirm **zero** network errors, zero hangs waiting on timeouts,
      and the backup still lands in the local `backups\` folder.

**Expected:** everything works; the app never needs the network (Drive
copy is a plain folder copy; unconfirmed sync status is honest, not an
error).
☐ PASS ☐ FAIL — notes:

## L. Upgrade simulation

- [ ] With data from §§G–K present, note the schema version in
      Settings → Storage: _______________
- [ ] **Re-run the installer** (same version) over the existing install.
- [ ] Confirm: a **pre-upgrade safety backup** is taken automatically at
      startup (`backup.on_upgrade`), migrations run, version is unchanged
      or newer, and **all data is intact** (yesterday's closing, balances,
      readings).
- [ ] If a newer build is available, install it over the old one and repeat.

**Expected:** upgrade is fail-closed on safety-backup failure; no data loss.
☐ PASS ☐ FAIL — notes:

## M. Uninstall keeps data

- [ ] Windows Settings → Apps → uninstall *Petrol Pump Manager*.
- [ ] Confirm the prompt/behavior: the **data directory is excluded from
      uninstall** — `%LOCALAPPDATA%\PetrolPumpManager` (data, backups,
      exports) **still exists** afterwards.
- [ ] Reinstall → data is picked up again.

**Expected:** uninstall removes the program, keeps the business data.
☐ PASS ☐ FAIL — notes:

## N. Code-signing decision (§9 item 5)

- [ ] Record the SmartScreen behavior observed in §C (screenshot
      recommended).
- [ ] Owner decision: ☐ buy a certificate and sign future builds (then
      re-verify install.md §7) / ☐ defer — SmartScreen "Unknown publisher"
      + *More info → Run anyway* remains the documented flow.

---

## Sign-off

| Check | Result |
|---|---|
| §C Clean install on fresh Windows | ☐ PASS ☐ FAIL |
| §D Standard (non-admin) user install | ☐ PASS ☐ FAIL |
| §C SmartScreen "Unknown publisher" observed as expected | ☐ PASS ☐ FAIL |
| §E–F First-run setup + data dir correct | ☐ PASS ☐ FAIL |
| §G Real 06-10-2026 workflow (662 L, 393.64/397.76) | ☐ PASS ☐ FAIL |
| §H Register mirrors paper; Urdu A4 PDF correct | ☐ PASS ☐ FAIL |
| §I Backup/restore (+ safety backup, sha256 verified) | ☐ PASS ☐ FAIL |
| §J Reopen→correct→new revision, old preserved | ☐ PASS ☐ FAIL |
| §K Fully offline day works | ☐ PASS ☐ FAIL |
| §L Upgrade over existing data, no loss | ☐ PASS ☐ FAIL |
| §M Uninstall keeps `%LOCALAPPDATA%\PetrolPumpManager` | ☐ PASS ☐ FAIL |
| §N Code-signing decision recorded | ☐ DONE |

**Failed steps / findings** (describe each, with screenshots where useful):

1.
2.
3.

**Overall verdict:** ☐ ACCEPTED for deployment ☐ ACCEPTED with findings
above ☐ NOT ACCEPTED

Tester signature: ____________________  Date: __________
Owner sign-off: ____________________  Date: __________
