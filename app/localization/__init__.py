"""Bilingual UI strings (English + Urdu).

Language affects the UI only; stored data is never altered by a language
switch (design plan §0, §5.1). Persisted via QSettings under the app's
org/app name.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_LANGUAGES = ("en", "ur")
_current: str | None = None
_cache: dict[str, dict[str, str]] = {}

_SETTINGS_ORG = "PetrolPumpManager"
_SETTINGS_APP = "PetrolPumpManager"
_SETTINGS_KEY = "language"


def _qsettings():
    """Return a QSettings instance, or None if Qt is unavailable.

    Kept lazy so that pure-Python consumers (e.g. bootstrap's tr() calls)
    never hard-require PySide6 at import time.
    """
    try:
        from PySide6.QtCore import QSettings
    except ImportError:
        return None
    return QSettings(_SETTINGS_ORG, _SETTINGS_APP)


def _load(code: str) -> dict[str, str]:
    if code not in _cache:
        path = Path(__file__).with_name(f"{code}.json")
        with path.open("r", encoding="utf-8") as fh:
            _cache[code] = json.load(fh)
    return _cache[code]


def set_language(code: str) -> None:
    """Select the UI language and persist it. Unknown codes fall back to 'en'."""
    global _current
    code = code if code in _LANGUAGES else "en"
    _current = code
    settings = _qsettings()
    if settings is not None:
        settings.setValue(_SETTINGS_KEY, code)


def get_language() -> str:
    """Return the current UI language ('en' default)."""
    if _current is not None:
        return _current
    settings = _qsettings()
    if settings is not None:
        stored = settings.value(_SETTINGS_KEY, "en")
        return stored if stored in _LANGUAGES else "en"
    return "en"


def tr(key: str) -> str:
    """Translate *key* for the current language; fall back to the key itself."""
    lang = get_language()
    strings = _load(lang)
    if key in strings:
        return strings[key]
    if lang != "en":
        en = _load("en")
        if key in en:
            return en[key]
    return key


# ----------------------------------------------------------------------
# Display-time rendering for service errors and checklist details
# ----------------------------------------------------------------------
#
# Services raise ``BusinessRuleError`` with ``message_key`` + ``params``
# (raw domain values — never pre-formatted text). Formatting happens here,
# at display time, so the same raw value renders correctly in both
# languages (design plan §5.1/§11):
#
# - a param named ``*_paisa`` holds integer paisa  -> '12,345.67'
# - a param named ``*_ml`` holds integer milliliters -> '1,234.567'
# - a param named ``*_date`` holds 'YYYY-MM-DD'     -> 'DD-MM-YYYY'
# - any other param is passed through untouched (templates may use
#   ``{x!r}`` or format specs on it); None renders as '—'.
#
# Western digits are used in both languages, matching the paper register.
# Language switches never touch stored data — these functions only format
# for display.


def _format_error_param(name: str, value):
    """Format one raw template param for display."""
    if value is None:
        return "—"
    if name.endswith("_paisa"):
        from app.calculations import money as _money

        return _money.format_pkr(int(value))
    if name.endswith("_ml"):
        from decimal import Decimal

        return f"{Decimal(int(value)) / 1000:,.3f}"
    if name.endswith("_date") and isinstance(value, str):
        # Stored business dates are 'YYYY-MM-DD' (§5.1 display: DD-MM-YYYY).
        # Only convert real date shapes; anything else passes through.
        if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            year, month, day = value.split("-")
            return f"{day}-{month}-{year}"
        return value
    return value


def _render_template(key: str, params: dict | None) -> str:
    """``tr(key).format(**params)`` with display-time param formatting."""
    return tr(key).format(**_format_params(params))


def _format_params(params: dict | None) -> dict:
    """Apply display-time formatting to every template param."""
    params = dict(params or {})
    return {
        name: _format_error_param(name, value)
        for name, value in params.items()
    }


def render_en(key: str, params: dict | None = None) -> str:
    """Render *key* in English regardless of the current language.

    Used when an English string must be produced deterministically (e.g.
    the public ``ticket_integrity`` API whose output existing callers
    compare); UI display should use :func:`localize_error` instead.
    """
    template = _load("en").get(key, key)
    try:
        return template.format(**_format_params(params))
    except (KeyError, IndexError, ValueError):
        return key


def localize_error(exc: BaseException) -> str:
    """Render a user-facing message for *exc* in the current language.

    Exceptions carrying ``message_key``/``params`` (see
    ``app.services.errors.BusinessRuleError``) are translated; anything
    else falls back to ``str(exc)``. A broken template also falls back to
    ``str(exc)`` rather than showing a key or a traceback.
    """
    key = getattr(exc, "message_key", None)
    if not key:
        return str(exc)
    try:
        return _render_template(key, getattr(exc, "params", None))
    except (KeyError, IndexError, ValueError):
        return str(exc)


def localize_check_detail(item) -> str:
    """Render a close-checklist item's detail in the current language.

    Items carry ``detail_key`` + ``detail_params``; ``detail_params`` may
    hold an ``issues`` list of ``(key, params)`` segments that are each
    translated and joined with " | ". Falls back to the item's English
    ``detail`` when no key is present or a template breaks.
    """
    fallback = getattr(item, "detail", "") or ""
    key = getattr(item, "detail_key", "") or ""
    if not key:
        return fallback
    try:
        params = dict(getattr(item, "detail_params", None) or {})
        issues = params.pop("issues", None)
        if issues:
            params["issues"] = " | ".join(
                _render_template(seg_key, seg_params)
                for seg_key, seg_params in issues
            )
        return _render_template(key, params)
    except (KeyError, IndexError, ValueError):
        return fallback
