# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller build spec for Petrol Pump Manager (Windows installer target).

======================================================================
DECISION: ONE-DIR layout (not one-file).
======================================================================
1. Faster startup — one-file builds must unpack the whole bundle to a
   temp folder on every launch; on a shop PC this noticeably delays
   opening the register each morning. One-dir starts the EXE in place.
2. Easier antivirus inspection — everything the app runs is visible as
   ordinary files under the install folder, so an AV scanner (or the
   station owner) can inspect or quarantine individual files instead of
   flagging one opaque 100 MB blob. This matters because the build is
   NOT code-signed (see documentation/install.md, "Antivirus & code
   signing").
3. Simpler debugging — if support ever needs to inspect the bundled
   migrations (.sql) or localisation (.json) on the customer's machine,
   they are plain files next to the EXE.

``console=False`` — no console window opens on launch
(see app/main.py header: the shipped build must hide the console;
on Linux/macOS no console is created anyway).

IMPORTANT: business data is NOT in this bundle. The live database and
all backups/exports/logs live in the per-user data dir
``%LOCALAPPDATA%\\PetrolPumpManager`` (see app/config.py::
get_base_dir/ensure_dirs). The uninstaller must never delete it.

Data files below are placed so they mirror the ``app/`` package layout
under the COLLECT root, because the code locates them via ``__file__``
(app/database/migrator.py::MIGRATIONS_DIR, app/localization/__init__.py,
app/services/report_export.py::_FONT_DIR).

Build (run from the repo root on the Windows build machine):
    pyinstaller installer/petrol_pump_manager.spec
Output:
    dist/PetrolPumpManager/   -> folder consumed by installer/petrol_pump_manager.iss
"""

from PyInstaller.utils.hooks import collect_submodules

# Project root = parent of this file (installer/ lives inside the repo).
PROJECT_ROOT = SPECPATH.parent

# ---------------------------------------------------------------------------
# Bundled data files (source -> destination inside the bundle).
# ---------------------------------------------------------------------------
datas = [
    # Localisation strings (app/localization/__init__.py reads <code>.json
    # next to itself).
    (
        str(PROJECT_ROOT / "app" / "localization" / "en.json"),
        "app/localization",
    ),
    (
        str(PROJECT_ROOT / "app" / "localization" / "ur.json"),
        "app/localization",
    ),
    # Noto Sans Arabic fonts for Urdu PDF reports
    # (app/services/report_export.py::_FONT_DIR). Includes the OFL license.
    # Only the real font/license files are bundled — the *.ttf.b64 text
    # copies exist only so CI can restore the binaries (see
    # .github/workflows/build-installer.yml) and must not ship.
    (
        str(PROJECT_ROOT / "app" / "resources" / "fonts" / "NotoSansArabic-Regular.ttf"),
        "app/resources/fonts",
    ),
    (
        str(PROJECT_ROOT / "app" / "resources" / "fonts" / "NotoSansArabic-Bold.ttf"),
        "app/resources/fonts",
    ),
    (
        str(PROJECT_ROOT / "app" / "resources" / "fonts" / "OFL.txt"),
        "app/resources/fonts",
    ),
    (
        str(PROJECT_ROOT / "app" / "resources" / "fonts" / "FONTS.md"),
        "app/resources/fonts",
    ),
    # SQL migrations — required at every startup (app/database/migrator.py).
    (
        str(PROJECT_ROOT / "app" / "database" / "migrations"),
        "app/database/migrations",
    ),
    # Reference SQL view definitions (dev/documentation aid, not used at
    # runtime; bundled so the installed copy matches the source tree).
    (
        str(PROJECT_ROOT / "app" / "database" / "views"),
        "app/database/views",
    ),
]

# Hidden imports. Modulegraph follows the static imports from app/main.py,
# but force-include every submodule of the ``app`` package so nothing the
# lazily-imported UI/report code needs is dropped.
hiddenimports = collect_submodules("app")

a = Analysis(
    [str(PROJECT_ROOT / "app" / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

# exclude_binaries=True + COLLECT below = the ONE-DIR layout (see header).
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PetrolPumpManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # <-- hides the console window on Windows
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # TODO: point at an app icon when one is provided (Phase 9)
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PetrolPumpManager",
)
