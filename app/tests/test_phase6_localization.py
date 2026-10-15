"""Phase 6: bilingual validation/service error messages.

Every user-visible service error now carries a stable ``message_key`` +
``params``; the UI renders ``tr(key)`` in the current language while
``str(exc)`` stays English. This module pins:

(a) en.json / ur.json key parity (exact same key sets),
(b) every message/detail key referenced in code exists in both JSON files,
(c) representative errors raised by real services render in Urdu,
(d) switching language never alters stored data,
(e) the RTL layout switch follows the language (Qt-dependent).

Temp databases only; the language is always restored to English.
"""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from app.localization import (
    get_language,
    localize_check_detail,
    localize_error,
    set_language,
    tr,
)
from app.services import errors

from .conftest import insert_business_day, insert_reading

LOC_DIR = Path(__file__).resolve().parent.parent / "localization"
SRC_DIRS = [
    Path(__file__).resolve().parent.parent / "services",
    Path(__file__).resolve().parent.parent / "ui",
]


def _load_jsons():
    en = json.loads((LOC_DIR / "en.json").read_text(encoding="utf-8"))
    ur = json.loads((LOC_DIR / "ur.json").read_text(encoding="utf-8"))
    return en, ur


@pytest.fixture
def language():
    """Run the test in the requested language; always restore English."""
    set_language("en")
    yield
    set_language("en")


def _use_language(code: str):
    set_language(code)
    assert get_language() == code


# ---------------------------------------------------------------------------
# (a) key parity
# ---------------------------------------------------------------------------


def test_en_ur_key_parity():
    en, ur = _load_jsons()
    missing_in_ur = sorted(set(en) - set(ur))
    extra_in_ur = sorted(set(ur) - set(en))
    assert not missing_in_ur, f"keys missing from ur.json: {missing_in_ur}"
    assert not extra_in_ur, f"extra keys in ur.json: {extra_in_ur}"
    assert len(en) == len(ur) > 0


# ---------------------------------------------------------------------------
# (b) every key referenced in code exists in both JSON files
# ---------------------------------------------------------------------------

_KEY_RE = re.compile(r'"((?:err|close\.detail|parse|common|cust)\.[A-Za-z0-9_]+)"')


def _code_keys() -> set[str]:
    found: set[str] = set()
    for src_dir in SRC_DIRS:
        for path in src_dir.rglob("*.py"):
            found |= set(_KEY_RE.findall(path.read_text(encoding="utf-8")))
    # Only the localization namespaces this phase owns; static UI keys
    # (tr("...")) are covered by the parity test above.
    return {
        k
        for k in found
        if k.startswith(("err.", "close.detail.", "parse."))
    }


def test_every_code_key_exists_in_both_languages():
    en, ur = _load_jsons()
    keys = _code_keys()
    assert keys, "no message keys found in code — scanner broken?"
    missing_en = sorted(k for k in keys if k not in en)
    missing_ur = sorted(k for k in keys if k not in ur)
    assert not missing_en, f"keys missing from en.json: {missing_en}"
    assert not missing_ur, f"keys missing from ur.json: {missing_ur}"


def test_no_untranslated_fallback_for_keyed_templates():
    """Every keyed template must render without leaking the key itself."""
    en, ur = _load_jsons()
    for key in _code_keys():
        assert en[key] != key, f"en.json template is the bare key: {key}"
        assert ur[key] != key, f"ur.json template is the bare key: {key}"
        assert en[key].strip(), f"empty en template: {key}"
        assert ur[key].strip(), f"empty ur template: {key}"


# ---------------------------------------------------------------------------
# (c) representative errors render in Urdu through real services
# ---------------------------------------------------------------------------


def test_invalid_business_date_urdu(phase5_services, language):
    svc = phase5_services
    with pytest.raises(errors.ValidationError) as exc_info:
        svc.days.open_day("not-a-date", opening_cash_paisa=100)
    exc = exc_info.value
    assert exc.message_key == "err.invalid_business_date_format"
    # str() stays English for tests/fallbacks.
    assert "Invalid business date" in str(exc)

    _use_language("ur")
    rendered = localize_error(exc)
    assert "غلط کاروباری تاریخ" in rendered
    assert "Invalid business date" not in rendered
    # The raw value passes through (Western digits convention).
    assert "not-a-date" in rendered


def test_unknown_nozzle_urdu(phase5_services, language):
    svc = phase5_services
    day = "2026-10-15"
    svc.days.open_day(day, opening_cash_paisa=1_000_000)
    with pytest.raises(errors.NotFoundError) as exc_info:
        svc.nozzles.record_reading(day, "NOPE-9", Decimal("10"))
    exc = exc_info.value
    assert exc.message_key == "err.unknown_nozzle"

    _use_language("ur")
    rendered = localize_error(exc)
    assert "نامعلوم نوزل" in rendered
    assert "Unknown nozzle" not in rendered


def test_trigger_rejection_key_urdu(phase5_services, db, language):
    """A lower reading rejected by the E2 trigger maps to a keyed error.

    The trigger compares against the previous closing reading on an
    *earlier* business date, so the fixture seeds two open days.
    """
    svc = phase5_services
    day1, day2 = "2026-10-14", "2026-10-15"
    # Day 1 is open while its 100 L closing reading is stored, then closed
    # via the allowed open → closed transition; day 2 is opened through
    # the service. A 50 L day-2 reading must be rejected by the E2 trigger
    # (lower than the previous closing reading).
    insert_business_day(db, day1, status="open", opening_cash_paisa=1_000_000)
    db.commit()
    insert_reading(db, "1 PG+", day1, 100_000)  # ml
    db.execute(
        "UPDATE business_days SET status = 'closed' WHERE business_date = ?",
        (day1,),
    )
    db.commit()
    svc.days.open_day(day2, opening_cash_paisa=1_000_000)
    with pytest.raises(errors.BusinessRuleError) as exc_info:
        svc.nozzles.record_reading(day2, "1 PG+", Decimal("50"))
    exc = exc_info.value
    assert exc.message_key == "err.trg_reading_lower"
    assert "lower than the previous" in str(exc)

    _use_language("ur")
    rendered = localize_error(exc)
    assert "میٹر ایونٹ" in rendered
    assert "lower than the previous" not in rendered


def test_duplicate_price_trigger_urdu(phase5_services, language):
    svc = phase5_services
    day = "2026-10-15"
    svc.days.open_day(day, opening_cash_paisa=1_000_000)
    stamp = "2026-10-15T00:00:00.000Z"
    svc.prices.set_price("PETROL", Decimal("270"), stamp, day)
    with pytest.raises(errors.DuplicateError) as exc_info:
        svc.prices.set_price("PETROL", Decimal("271"), stamp, day)
    exc = exc_info.value
    assert exc.message_key in (
        "err.trg_duplicate_price",
        "err.price_already_recorded",
    )

    _use_language("ur")
    rendered = localize_error(exc)
    assert "پہلے سے" in rendered
    assert "already" not in rendered


def test_english_rendering_formats_values(phase5_services, language):
    """Display-time formatting: paisa amounts and dates in templates."""
    svc = phase5_services
    with pytest.raises(errors.ValidationError) as exc_info:
        svc.days.open_day("2026-13-99", opening_cash_paisa=100)
    exc = exc_info.value
    _use_language("en")
    rendered = localize_error(exc)
    assert "Invalid business date" in rendered

    # A keyed error with a plain param renders in Urdu too.
    with pytest.raises(errors.ValidationError) as exc_info:
        svc.payments.record_payment(1, "2026-10-15", "not-an-int")
    exc = exc_info.value
    assert exc.message_key == "err.payment_amount_integer"
    _use_language("ur")
    rendered = localize_error(exc)
    assert "ادائیگی" in rendered
    assert "Payment amount" not in rendered


def test_close_checklist_details_urdu(phase5_services, language):
    """Checklist item details render in Urdu, not just the labels."""
    svc = phase5_services
    day = "2026-10-15"
    svc.days.open_day(day, opening_cash_paisa=1_000_000)
    report = svc.days.validate_close(day, 1_000_000)
    assert not report.can_close
    by_key = {item.key: item for item in report.items}
    item = by_key["readings_complete"]
    assert item.status == "fail"
    assert item.detail_key in (
        "close.detail.readings_none",
        "close.detail.readings_missing",
    )

    _use_language("ur")
    assert tr(item.label_key) != item.label_key  # label translated
    detail = localize_check_detail(item)
    assert "ریڈنگ" in detail or "نوزل" in detail
    assert "No active nozzles" not in detail
    assert "Missing final readings" not in detail

    # The English detail fallback is untouched (closing record stores it).
    assert item.detail != detail or get_language() == "en"


def test_close_blocked_error_urdu(phase5_services, language):
    svc = phase5_services
    day = "2026-10-15"
    svc.days.open_day(day, opening_cash_paisa=1_000_000)
    with pytest.raises(errors.CloseBlockedError) as exc_info:
        svc.days.close_day(day, physical_cash_paisa=1_000_000)
    exc = exc_info.value
    assert exc.message_key == "err.close_blocked_count"
    assert exc.report is not None

    _use_language("ur")
    rendered = localize_error(exc)
    assert "بند نہیں ہو سکتا" in rendered
    assert "cannot be closed" not in rendered
    # The checklist riding along also renders in Urdu.
    details = [localize_check_detail(i) for i in exc.report.items]
    assert any("نوزل" in d or "ریڈنگ" in d for d in details)


def test_localize_error_falls_back_to_str():
    """Unkeyed exceptions (and broken templates) never leak a key."""
    exc = errors.ValidationError("plain English, no key")
    assert exc.message_key is None
    _use_language("ur")
    assert localize_error(exc) == "plain English, no key"
    assert localize_error(ValueError("v")) == "v"


# ---------------------------------------------------------------------------
# (d) language switches never alter stored data
# ---------------------------------------------------------------------------


def _snapshot(conn) -> dict:
    """Canonical dump of every table: {table: [rows]}. Read-only."""
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    snap = {}
    for table in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        order = ", ".join(cols) if cols else "rowid"
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
        snap[table] = [tuple(r) for r in rows]
    return snap


def test_language_switch_leaves_stored_data_untouched(
    phase5_services, db_file, language
):
    import sqlite3

    svc = phase5_services
    day = "2026-10-15"
    svc.days.open_day(day, opening_cash_paisa=2_000_000)
    svc.prices.set_price("PETROL", Decimal("270"), "2026-10-15T00:00:00.000Z", day)
    cust_id = svc.credit.create_customer("زبیر ٹریڈرز").id
    svc.expenses.record_expense(
        category="Fuel purchase",
        amount_paisa=50_000,
        method="cash",
        business_date=day,
        occurred_at_utc="2026-10-14T20:00:00.000Z",
        payee="چوکیدار",
    )
    assert svc.credit.get_customer(cust_id).name == "زبیر ٹریڈرز"

    conn = sqlite3.connect(str(db_file))
    try:
        before = _snapshot(conn)
    finally:
        conn.close()

    # Switch language, re-render everything user-visible, switch back.
    _use_language("ur")
    report = svc.days.validate_close(day, 2_000_000)
    for item in report.items:
        tr(item.label_key)
        localize_check_detail(item)
    # A keyed error raised while in Urdu still carries its English str().
    with pytest.raises(errors.ValidationError) as exc_info:
        svc.days.open_day("bad-date")
    assert "Invalid business date" in str(exc_info.value)
    try:
        svc.days.close_day(day, physical_cash_paisa=2_000_000)
    except errors.CloseBlockedError as exc:
        localize_error(exc)
    day_info = svc.days.get_day(day)
    assert day_info.opening_cash_paisa == 2_000_000
    _use_language("en")

    conn = sqlite3.connect(str(db_file))
    try:
        after = _snapshot(conn)
    finally:
        conn.close()
    assert before == after, "stored data changed across a language switch"

    # Raw service values are identical in both languages.
    _use_language("ur")
    ur_day = svc.days.get_day(day)
    _use_language("en")
    en_day = svc.days.get_day(day)
    assert ur_day.opening_cash_paisa == en_day.opening_cash_paisa == 2_000_000
    assert ur_day.business_date == en_day.business_date == day


# ---------------------------------------------------------------------------
# (e) RTL follows the language (Qt-dependent)
# ---------------------------------------------------------------------------


def test_rtl_layout_direction_follows_language(language):
    QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    QtCore = pytest.importorskip("PySide6.QtCore", exc_type=ImportError)
    from app.ui.main_window import apply_language_direction

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    _use_language("ur")
    parent = QtWidgets.QWidget()
    apply_language_direction(parent)
    assert parent.layoutDirection() == QtCore.Qt.RightToLeft
    # The app-wide default covers dialogs/message boxes too.
    assert app.layoutDirection() == QtCore.Qt.RightToLeft
    box = QtWidgets.QMessageBox(parent)
    try:
        assert box.layoutDirection() == QtCore.Qt.RightToLeft
    finally:
        box.deleteLater()
    parent.deleteLater()

    _use_language("en")
    parent_en = QtWidgets.QWidget()
    apply_language_direction(parent_en)
    try:
        assert parent_en.layoutDirection() == QtCore.Qt.LeftToRight
        assert app.layoutDirection() == QtCore.Qt.LeftToRight
    finally:
        parent_en.deleteLater()
