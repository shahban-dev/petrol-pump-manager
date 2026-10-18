"""Shared UI helpers: input parsing and message boxes.

Parsing converts user text to the exact types services need (Decimal for
liters, integer paisa for money). The UI never does accounting math —
these are input conversions only; all calculations live in services.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from PySide6.QtGui import QDoubleValidator, QIntValidator
from PySide6.QtWidgets import QLabel, QLineEdit, QMessageBox, QWidget

from app.calculations import money as calc
from app.localization import localize_error, tr
from app.services import errors


def make_integer_edit(parent: QWidget | None = None) -> QLineEdit:
    """A QLineEdit that only accepts integer digits (meter readings, etc.).

    Letters and decimal points are rejected at keystroke level — the field
    can never hold non-numeric text.
    """
    edit = QLineEdit(parent)
    edit.setValidator(QIntValidator(0, 999_999_999, edit))
    return edit


def make_signed_integer_edit(parent: QWidget | None = None) -> QLineEdit:
    """A QLineEdit that accepts signed integers (adjustments, corrections).

    Same keystroke-level protection as :func:`make_integer_edit`, but the
    leading minus sign is allowed — required anywhere a shortage or
    reduction must be enterable (stock adjustments, opening-balance
    corrections).
    """
    edit = QLineEdit(parent)
    edit.setValidator(QIntValidator(-999_999_999, 999_999_999, edit))
    return edit


def make_decimal_edit(
    parent: QWidget | None = None, *, decimals: int = 2
) -> QLineEdit:
    """A QLineEdit that only accepts a decimal number (money, liters).

    Digits and a single decimal separator are allowed; letters are
    rejected at keystroke level. ``decimals`` caps the fraction digits.
    """
    edit = QLineEdit(parent)
    validator = QDoubleValidator(0.0, 999_999_999.0, decimals, edit)
    validator.setNotation(QDoubleValidator.StandardNotation)
    edit.setValidator(validator)
    return edit


def make_page_banner(
    parent: QWidget | None,
    title: str,
    subtitle: str = "",
    color: str = "navy",
) -> QWidget:
    """A bold colorful header banner for the top of a page.

    ``color`` is one of: green, blue, orange, purple, red, teal,
    indigo, navy, brown, slate. Each page uses its own color so the
    app feels vibrant everywhere, not just on the Daily Sheet.
    Returns the banner widget (already styled via theme.qss).
    """
    from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout

    banner = QFrame(parent)
    banner.setObjectName("pageBanner")
    banner.setProperty("pageBannerBg", color)
    layout = QHBoxLayout(banner)
    layout.setContentsMargins(18, 12, 18, 12)
    texts = QVBoxLayout()
    texts.setSpacing(2)
    title_label = QLabel(title, banner)
    title_label.setObjectName("pageBannerTitle")
    texts.addWidget(title_label)
    if subtitle:
        sub_label = QLabel(subtitle, banner)
        sub_label.setObjectName("pageBannerSub")
        sub_label.setWordWrap(True)
        texts.addWidget(sub_label)
    layout.addLayout(texts)
    layout.addStretch(1)
    # Re-apply dynamic property styling (needed when set after show).
    banner.style().unpolish(banner)
    banner.style().polish(banner)
    return banner


def parse_pkr_to_paisa(text: str) -> int:
    """'1,234.56' or '1234.56' -> integer paisa. Raises ValueError."""
    return calc.to_paisa(parse_pkr(text))


def parse_pkr(text: str) -> Decimal:
    """'1,234.56' or '1234.56' -> non-negative Decimal PKR. Raises ValueError."""
    cleaned = (text or "").strip().replace(",", "")
    if not cleaned:
        raise ValueError(tr("parse.empty_amount"))
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(tr("parse.not_a_number").format(value=text)) from None
    if value < 0:
        raise ValueError(tr("parse.amount_negative"))
    return value


def parse_signed_pkr(text: str) -> Decimal:
    """'1,234.56' or '-50' -> signed Decimal PKR. Raises ValueError."""
    cleaned = (text or "").strip().replace(",", "")
    if not cleaned:
        raise ValueError(tr("parse.empty_amount"))
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(tr("parse.not_a_number").format(value=text)) from None


def parse_signed_pkr_to_paisa(text: str) -> int:
    """Signed '1,234.56'/'-50' -> integer paisa (may be negative).

    Input conversion only — the signed adjustment itself is validated and
    applied by the service.
    """
    return calc.round_half_up(parse_signed_pkr(text) * calc.PAISA_PER_PKR)


def parse_litres(text: str) -> Decimal:
    """'47' or '47.5' -> Decimal liters. Raises ValueError."""
    cleaned = (text or "").strip().replace(",", "")
    if not cleaned:
        raise ValueError(tr("parse.empty_quantity"))
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(tr("parse.not_a_number").format(value=text)) from None
    if value < 0:
        raise ValueError(tr("parse.quantity_negative"))
    return value


def parse_signed_litres(text: str) -> Decimal:
    """'-12.5' or '47.5' -> Decimal liters (sign allowed). Raises ValueError."""
    cleaned = (text or "").strip().replace(",", "")
    if not cleaned:
        raise ValueError(tr("parse.empty_quantity"))
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(tr("parse.not_a_number").format(value=text)) from None


def format_litres(ml: int | None) -> str:
    """Integer milliliters -> '47.000'; None -> '—'."""
    if ml is None:
        return tr("common.not_entered")
    return f"{calc.from_ml(ml):,.3f}"


def format_pkr(paisa: int | None) -> str:
    """Integer paisa -> '1,234.56'; None -> '—'."""
    if paisa is None:
        return tr("common.not_entered")
    return calc.format_pkr(paisa)


def format_unit_price(unit_price_10k: int | None) -> str:
    """Integer 0.0001-PKR units -> '268.4500'; None -> '—'."""
    if unit_price_10k is None:
        return tr("common.not_entered")
    value = Decimal(unit_price_10k) / Decimal(10_000)
    return f"{value:,.4f}"


def qdatetime_karachi_to_utc_iso(qdt) -> str:
    """A QDateTime's wall time, interpreted as Asia/Karachi, -> UTC storage ISO.

    Used for service ``*_utc`` arguments coming from QDateTimeEdit widgets
    whose timezone is set to Asia/Karachi. Pure timestamp formatting —
    no accounting math.
    """
    from datetime import timezone

    from app.calculations import time as timecalc

    naive = qdt.toPython().replace(tzinfo=timecalc.KARACHI_TZ)
    utc = naive.astimezone(timezone.utc)
    return utc.strftime(timecalc.UTC_FORMAT)[:-3] + "Z"


def karachi_now_qdatetime():
    """Current time as a QDateTime pinned to the Asia/Karachi timezone."""
    from PySide6.QtCore import QDateTime, QTimeZone

    return QDateTime.currentDateTimeUtc().toTimeZone(QTimeZone(b"Asia/Karachi"))


def dto_field(obj, *names, default=None):
    """Read a field from a service DTO, dict, or row by trying *names* in order.

    Attribute access first, then mapping access. Returns *default* when none
    of the names is found — the UI prefers showing "—" over crashing when a
    service DTO carries a differently-named equivalent field.
    """
    for name in names:
        if isinstance(obj, dict):
            if name in obj:
                return obj[name]
            continue
        try:
            return getattr(obj, name)
        except AttributeError:
            pass
        try:
            return obj[name]
        except (KeyError, IndexError, TypeError):
            pass
    return default


def product_combo_entry(product) -> tuple[str, object]:
    """(label, id) for a fuel-product row that may be a dict or a DTO."""
    code = dto_field(product, "code")
    name = dto_field(product, "name_en", "name", default=None) or code
    return f"{code} — {name}", dto_field(product, "id")


def show_error(parent: QWidget, exc: BaseException) -> None:
    """Show a service/validation failure. Never shows a traceback.

    Service errors carrying a ``message_key`` are rendered bilingually
    for the current language (``localize_error``); anything else falls
    back to ``str(exc)``.
    """
    if isinstance(exc, errors.BusinessRuleError):
        detail = localize_error(exc)
    elif isinstance(exc, ValueError):
        detail = str(exc)
    else:  # unexpected: keep technical details out of the dialog
        detail = tr("common.unexpected_error")
    QMessageBox.warning(parent, tr("common.error"), detail)


def show_info(parent: QWidget, message: str) -> None:
    """Show an informational confirmation (only after a commit succeeded)."""
    QMessageBox.information(parent, tr("common.info_title"), message)
