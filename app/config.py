"""Application configuration: paths, constants, settings keys.

No business data lives here — only filesystem locations and app metadata.
Derived from the design plan (00_design_plan.md, §0 "Live DB location").

The live database is NEVER inside a cloud-synced folder and NEVER in
Program Files: Windows -> %LOCALAPPDATA%/PetrolPumpManager,
Linux/macOS -> XDG data dir (dev/test).

NOTE: module-level directory constants are computed once at import time.
Tests must set $XDG_DATA_HOME (or pass an explicit db_path to bootstrap())
before importing this module.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# App metadata
# ---------------------------------------------------------------------------
APP_NAME = "PetrolPumpManager"          # also the QSettings app name
ORG_NAME = "PetrolPumpManager"          # QSettings organization name
APP_DISPLAY_NAME = "Petrol Pump Manager"
APP_VERSION = "1.0.0"
DB_FILENAME = "petrol_pump.sqlite3"
LOG_FILENAME = "petrol_pump_manager.log"


class AppStartupError(Exception):
    """Raised when the application cannot start safely.

    Carries a user-friendly message. Callers must surface it to the user and
    must NEVER attempt to delete or overwrite the database on this path.
    """


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def get_base_dir() -> Path:
    """Return the per-user application data directory.

    Windows: %LOCALAPPDATA%/PetrolPumpManager
    Linux/macOS: $XDG_DATA_HOME/petrol-pump-manager, or
    ~/.local/share/petrol-pump-manager when $XDG_DATA_HOME is unset/empty.
    """
    if sys.platform == "win32" or os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise AppStartupError(
                "Could not determine the Windows application-data folder "
                "(%LOCALAPPDATA% is not set). Please contact support; the "
                "database has not been touched."
            )
        return Path(local_app_data) / "PetrolPumpManager"
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if not xdg_data_home:
        xdg_data_home = str(Path.home() / ".local" / "share")
    return Path(xdg_data_home) / "petrol-pump-manager"


BASE_DIR: Path = get_base_dir()
DATA_DIR: Path = BASE_DIR / "data"
BACKUP_DIR: Path = BASE_DIR / "backups"
EXPORT_DIR: Path = BASE_DIR / "exports"
LOG_DIR: Path = BASE_DIR / "logs"
CONFIG_DIR: Path = BASE_DIR / "config"


def db_path() -> Path:
    """Path of the live SQLite database file."""
    return DATA_DIR / DB_FILENAME


def ensure_dirs() -> None:
    """Create all application directories (exist_ok=True).

    Raises:
        AppStartupError: on permission errors, missing drives, or any other
            OS-level failure, with a message safe to show the user.
    """
    for directory in (DATA_DIR, BACKUP_DIR, EXPORT_DIR, LOG_DIR, CONFIG_DIR):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            raise AppStartupError(
                f"Cannot create application folder '{directory}': permission "
                f"denied. Please check folder permissions or contact support. "
                f"The database has not been touched."
            ) from exc
        except OSError as exc:
            raise AppStartupError(
                f"Cannot create application folder '{directory}': {exc}. "
                f"Please check available disk space or contact support. "
                f"The database has not been touched."
            ) from exc
