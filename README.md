# Petrol Pump Manager

Offline desktop application for a petrol station in Pakistan: daily Urdu-register workflows
(nozzle readings, fuel sales, stock, cash book, customer credit, suppliers, expenses),
bilingual English/Urdu UI, strict accounting rules, verified backups.

**Stack:** Python 3.12 + PySide6 + SQLite. No web server, no cloud backend, no internet required.

## Layout

- `app/` — application package (`main.py`, `bootstrap.py`, `config.py`, `ui/`, `services/`,
  `repositories/`, `database/`, `models/`, `calculations/`, `localization/`, `resources/`, `tests/`)
- `documentation/` — design plan (the build contract), business rules, guides
- `installer/` — PyInstaller spec + Inno Setup script (Phase 8)

## Quick start (development)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest app/tests -q
python -m app.main
```

Live database location: `%LOCALAPPDATA%\PetrolPumpManager\data\petrol_pump.sqlite3` on Windows,
`~/.local/share/petrol-pump-manager/` on Linux. Tests always use temporary databases.

## Build contract

`documentation/00_design_plan.md` is authoritative for architecture, schema, triggers,
accounting models, and test plan. Do not contradict it without an amendment entry.
