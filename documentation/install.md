# Petrol Pump Manager — Installation & Maintenance Guide

Offline desktop application for petrol station bookkeeping
(Windows installer build).

Version this guide covers: **1.0.0** (keep in sync with
`app/config.py :: APP_VERSION`).

Related documents: `documentation/recovery.md` (disaster procedures —
read it before upgrading or restoring), `installer/petrol_pump_manager.iss`
(the installer definition), `installer/petrol_pump_manager.spec` (the
PyInstaller build definition).

---

## 0. Getting the installer without a Windows build machine

The installer `.exe` can only be compiled on Windows (PyInstaller + Inno
Setup). If you don't want to install those build tools on a Windows PC
yourself, the repo ships a GitHub Actions workflow
(`.github/workflows/build-installer.yml`) that builds the installer on
GitHub's Windows runners:

1. Create a free GitHub account and push this repository to it (a private
   repository is fine).
2. On github.com, open the repository → **Actions** → **Build Windows
   installer** → **Run workflow**.
3. When the run finishes (roughly 10–20 minutes), download the
   **PetrolPumpManager-Setup** artifact from the run page. It contains
   `PetrolPumpManager-Setup-<version>.exe` — the same installer the manual
   build below produces.

The workflow runs the full test suite first; the installer is only built
when all tests pass. The resulting build is unsigned, so expect the
SmartScreen note in §2 below.

---

## 1. System requirements

- **OS:** Windows 10 (version 1809 or later) or Windows 11, **64-bit**.
- **Disk:** ~300 MB free for the install, plus room for the data folder.
- **No internet connection required** — the app is fully offline.
- **No administrator rights required** — the installer is a per-user
  install and the app never writes outside its own install folder and the
  current user's `%LOCALAPPDATA%`.

## 2. Installing

1. Copy `PetrolPumpManager-Setup-<version>.exe` to the shop PC (USB drive
   is fine — the app needs no network).
2. Double-click the setup file. Windows SmartScreen may show a
   *"Windows protected your PC / Unknown publisher"* warning — this is
   expected because the build is **not code-signed** (see §7 below). To
   proceed: click *More info*, then *Run anyway*. Only run the installer
   if you obtained it from the person who built it.
3. Accept the default folder (`C:\Users\<you>\AppData\Local\Programs\PetrolPumpManager`).
4. Optionally tick **Create a desktop icon**.
5. Launch from the Start Menu entry *Petrol Pump Manager*.

The installer places the program under per-user Program Files; **your
business data is NOT there** — it lives in
`%LOCALAPPDATA%\PetrolPumpManager` (see §3).

## 3. First launch

On first start the app:

1. Creates the data folder structure under
   `%LOCALAPPDATA%\PetrolPumpManager`:
   - `data\` — the live SQLite database (`petrol_pump.sqlite3`)
   - `backups\` — verified backup snapshots
   - `exports\` — PDF / XLSX / CSV report exports
   - `logs\` — application log
   - `config\` — app settings
2. Creates the database and applies all schema migrations automatically.
3. Asks for the opening of the **first business day**. The manager must
   enter:
   - **Opening cash in drawer** (required on the very first day; later
     days carry it forward from the previous closing automatically).
   - **Opening stock per product** (petrol / diesel tank levels, and any
     other tracked products).
   - **Opening meter readings per nozzle.**
   - The day's notified fuel prices.

Subsequent days pull previous meter readings, stock and cash forward from
the last closing — the register workflow is designed to mirror the paper
register the station already keeps.

## 4. Upgrading to a new version

Upgrades are installed **in place** over the existing installation. Your
database is in `%LOCALAPPDATA%` — the installer never touches it — so
re-running the setup for a new version keeps all business data. After the
upgrade, the next app start runs any pending schema migrations inside
per-migration transactions (`documentation/recovery.md` §3).

> ⚠️ **Mandatory before upgrading: take a safety backup first.** The app
> also takes an **automatic** pre-upgrade safety backup at startup before
> applying any pending migration (`recovery.md` §3, honors the
> `backup.on_upgrade` setting), but do not rely on it alone: take your own
> backup from the Backups page (§5.1) before installing a new version. If
> the upgrade fails, `bootstrap()` refuses to start and the database is
> left at its previous version; you then roll back by restoring your
> safety backup with the §5 restore procedure.

**Rollback procedure (if a migration fails):**
1. Close the app.
2. Restore the safety backup you took before upgrading (restore steps in
   §5 below).
3. Reinstall the previous version's setup file.
4. Start the app — it will open the restored database normally.

## 5. Backup and restore

The live database must never be copied while the app is running (it is a
WAL-mode SQLite file; a raw copy is not a consistent snapshot). The only
supported snapshot method is the SQLite **online backup API**
(`sqlite3.Connection.backup()`), as described in `recovery.md` §1.

### 5.1 Backing up

The app ships a **Backups** page (sidebar) backed by
`app/services/backup_service.py`:

- **Backup now** — runs the full §9 pipeline: consistent snapshot via the
  SQLite online backup API to a temp file, integrity validation, atomic
  rename to `PetrolPump_Backup_YYYY-MM-DD_HHMMSS.sqlite`, copy to the
  configured Google Drive folder (if set), and a `backup_history` row with
  filename, size and sha256. Statuses are honest: *"In Google Drive folder
  — cloud sync unconfirmed"* is the normal state; the app never claims
  cloud sync without a real OS signal.
- Automatic backups run **after each day close** and **before upgrades**
  (both configurable in Settings; failures are logged, never fatal).
- **Restore** — pick a backup from history, confirm explicitly, and the
  app takes a safety backup of the current database first, validates the
  candidate, swaps it in atomically, re-verifies, and asks for a restart.

**Manual fallback** (if the app cannot start): quit the app, then on the
shop PC run the script below (paths in PowerShell style). It uses the same
online-backup-API + validation steps the service performs:

```powershell
# Adjust the paths to your machine and Python installation.
$env:XDG_DATA_HOME = ""   # not used on Windows; shown for completeness
python - <<'EOF'
import sqlite3, hashlib, shutil, tempfile, os
from pathlib import Path
from datetime import datetime, timezone, timedelta

base = Path(os.environ["LOCALAPPDATA"]) / "PetrolPumpManager"
src = base / "data" / "petrol_pump.sqlite3"
pkt = timezone(timedelta(hours=5))
stamp = datetime.now(pkt).strftime("%Y-%m-%d_%H%M%S")
name = f"PetrolPump_Backup_{stamp}.sqlite"          # filename convention, PKT local time

tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite")
tmp.close()
with sqlite3.connect(src) as live, sqlite3.connect(tmp.name) as snap:
    live.backup(snap)                               # consistent snapshot of the live WAL DB
    chk = snap.execute("PRAGMA integrity_check").fetchone()[0]
    fk  = snap.execute("PRAGMA foreign_key_check").fetchall()
assert chk == "ok" and not fk, "snapshot failed validation — do NOT use it"

dest = base / "backups" / name
dest.parent.mkdir(parents=True, exist_ok=True)
shutil.move(tmp.name, dest)
sha = hashlib.sha256(dest.read_bytes()).hexdigest()
print("backup written:", dest)
print("sha256:", sha)
EOF
```

Record-keeping: each backup is meant to be logged in the `backup_history`
table (`status='validated'`, sha256 recorded) — the design for the full
pipeline is in `00_design_plan.md` §9. In the manual flow, keep the
console output (filename + sha256) with your records.

### 5.2 Google Drive folder setup (off-site copy)

Google Drive for desktop is used **only as a synchronized folder
destination** — the app has no Google API integration and needs no
internet to operate (design §9).

1. On the shop PC, install *Google Drive for desktop* and sign in with the
   station's Google account.
2. In Drive settings → Preferences → Folders from Drive → choose a local
   folder (e.g. `G:\My Drive\PetrolPumpBackups\`) and note the path. The
   Drive app mirrors this folder to the cloud.
3. After taking a backup (§5.1), **copy the validated
   `PetrolPump_Backup_<date>_<time>.sqlite` file into that Drive folder**.
   Verify the file exists there and is plausibly sized (compare byte
   counts).
4. Drive syncs it when online. **Honest status:** the file sitting in the
   Drive folder means *“in the Google Drive folder — cloud sync
   pending/unconfirmed”*, never *“backed up to cloud”* (design §9 step 7).
   Open drive.google.com occasionally and confirm the file is there.

**Never put the live database inside a synced folder** — a synced live DB
corrupts. Only validated backup copies go to Drive.

### 5.3 Restore from backup

Follow `documentation/recovery.md` §4 exactly. The key points:

1. **Safety backup of the current DB first** — even if it looks broken,
   snapshot it with the §5.1 script before touching anything. A restore
   never destroys the thing it replaces.
2. **Validate the candidate backup** on a copy: `PRAGMA integrity_check`
   must return `"ok"`, `PRAGMA foreign_key_check` must return no rows,
   then spot-check business truth (business-day dates, nozzle/customer
   counts, the latest closing's cash and liter figures, a tail of
   `audit_log`) against the printed register reports.
3. **Confirm explicitly** — the design requires typed confirmation in the
   UI when it exists; manually: say the filename and date out loud and
   check it twice before it replaces the live DB.
4. Replace the live DB, then start the app — `bootstrap()` re-runs
   migrations, schema verification and both integrity checks.

### 5.4 Retention

Pruning removes **old validated backups only**, and always keeps the
newest known-good backup plus the pre-restore safety backup. Never delete
the last valid backup. There is no automatic retention pruning in the app
yet — delete old validated files by hand using this rule.

## 6. Uninstalling

Uninstall via *Settings → Apps → Petrol Pump Manager → Uninstall* (or the
*Uninstall Petrol Pump Manager* Start Menu entry).

**Your business data is preserved.** The uninstaller removes the program
files only. The data directory `%LOCALAPPDATA%\PetrolPumpManager`
(database, backups, exports, logs, config) is **deliberately left
untouched** — uninstalling must never destroy the station's books (this is
enforced in `installer/petrol_pump_manager.iss`: no uninstall-delete rules
target the data folder).

**Before uninstalling, take a final backup** (§5.1) and copy it to the
Drive folder (§5.2) or a USB drive, especially if the PC is being retired.
If you want the data folder removed after that, delete
`%LOCALAPPDATA%\PetrolPumpManager` manually in File Explorer.

## 7. Antivirus & code signing

**Be aware of the following — this is stated plainly, not hidden:**

- The installer and program are **NOT code-signed**. Code signing requires
  buying a certificate from a certificate authority, and no certificate
  has been purchased for this build. **Do not claim it is signed.**
- On first install, **Windows SmartScreen will show an “Unknown
  publisher” warning** — this is expected for unsigned software. The
  person installing should only proceed with a setup file received from a
  trusted source (the builder).
- Some antivirus products flag unsigned installers on first sight. The
  build uses the **one-dir layout** (`installer/petrol_pump_manager.spec`):
  every file the app runs is a plain, inspectable file in the install
  folder, so an AV scanner — or the station owner — can inspect or
  quarantine individual files instead of facing one opaque blob.
- If the builder later obtains a code-signing certificate, the build
  scripts should sign `PetrolPumpManager.exe` and the setup file with
  `signtool` before distribution; the SmartScreen reputation then builds
  up over time.

## 8. Rebuilding from source

On a Windows build machine (64-bit), from the repository root:

```bat
REM 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate

REM 2. Install runtime + build dependencies
pip install -r requirements.txt
pip install pyinstaller pytest

REM 3. Run the test suite (must be green before packaging)
pytest app/tests -q

REM 4. Build the one-dir application bundle
pyinstaller installer\petrol_pump_manager.spec
REM -> produces dist\PetrolPumpManager\

REM 5. Compile the installer (Inno Setup 6.x, iscc.exe on PATH)
iscc installer\petrol_pump_manager.iss
REM -> produces installer\Output\PetrolPumpManager-Setup-<version>.exe
```

Notes:
- Keep `installer/petrol_pump_manager.iss`'s `MyAppVersion` in sync with
  `app/config.py :: APP_VERSION`.
- The Inno Setup `AppId` GUID must **never** change, or upgrades break
  (comment in the `.iss`).
- The `dist\` folder is build output — it is what the `.iss` packages;
  it is not committed to the repository.

## 9. MANUAL STEPS REMAINING (Phase-9 acceptance)

The following were **not** done while producing this Phase-8 packaging
work and must be performed manually in Phase 9. Nothing below should be
read as tested:

1. **Clean-Windows-machine install test.** No Windows VM or clean Windows
   machine is available in this build environment, so the installer has
   **not** been run on a fresh Windows 10/11 system. Phase 9 must install
   on a clean machine and verify the app starts, creates the database,
   and the first-day flow works.
2. **Install under a standard (non-admin) Windows user.** The script sets
   `PrivilegesRequired=lowest`, but this has not been exercised — verify
   that a non-admin user can install, launch, and write to the data
   folder without elevation prompts.
3. **SmartScreen behaviour on first run.** The expected “Unknown
   publisher” warning (§7) has not been observed in practice; confirm it
   appears as described and that *More info → Run anyway* completes the
   install.
4. **Full Phase-9 acceptance on the installed build.** The PyInstaller
   bundle and the Inno Setup installer have not been compiled or run here
   — Phase 9 must run the §8 rebuild commands on the Windows build
   machine, install the produced setup file, and carry out the acceptance
   workflow (register entries, reports, backup/restore, upgrade) against
   the installed build, not just the source tree.
5. **Code signing decision.** If the owner buys a certificate, sign the
   binaries and re-verify §7; until then the SmartScreen warning stands.
