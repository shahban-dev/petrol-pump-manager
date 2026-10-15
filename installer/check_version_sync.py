#!/usr/bin/env python3
"""Fail unless app/config.py :: APP_VERSION matches the version hardcoded
in installer/petrol_pump_manager.iss (#define MyAppVersion).

The .iss script deliberately does not read the Python file (Inno Setup
compiles before any Python runs on the build machine), so the two values
must be kept in sync by hand. Run this before every build; the GitHub
Actions workflow (build-installer.yml) runs it automatically.

Usage:  python installer/check_version_sync.py   (from the repo root)
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

app_py = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
iss = (ROOT / "installer" / "petrol_pump_manager.iss").read_text(encoding="utf-8")

app_version = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', app_py).group(1)
iss_version = re.search(r'#define\s+MyAppVersion\s+"([^"]+)"', iss).group(1)

print(f"app/config.py APP_VERSION : {app_version}")
print(f".iss MyAppVersion         : {iss_version}")

if app_version != iss_version:
    print(
        "VERSION MISMATCH: update #define MyAppVersion in "
        "installer/petrol_pump_manager.iss to match app/config.py :: APP_VERSION.",
        file=sys.stderr,
    )
    sys.exit(1)

print("Versions are in sync.")
