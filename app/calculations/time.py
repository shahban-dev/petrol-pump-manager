"""Business-date and timestamp helpers.

Single implementation for the project's timestamp policy
(design plan §5.1):

* machine timestamps are stored as UTC text ``YYYY-MM-DDTHH:MM:SS.SSSZ``
  (millisecond precision);
* the business date is a separate ``YYYY-MM-DD`` value — the station's
  operating date in ``Asia/Karachi`` — and is NEVER derived by slicing a
  UTC timestamp;
* a transaction recorded shortly after midnight in Pakistan belongs to
  the *open business day*; the app never auto-rolls the business date
  from the wall clock alone.

Pure functions, no database access: fully unit-testable.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

KARACHI_TZ = ZoneInfo("Asia/Karachi")

#: Storage format for machine timestamps (§5.1).
UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"  # + 'Z', trimmed to milliseconds
_DISPLAY_DATE_FORMAT = "%d-%m-%Y"
_DISPLAY_DATETIME_FORMAT = "%d-%m-%Y %H:%M"


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SS.SSSZ`` (millisecond precision)."""
    return utc_now().strftime(UTC_FORMAT)[:-3] + "Z"


def karachi_now() -> datetime:
    """Current time as a timezone-aware ``Asia/Karachi`` datetime."""
    return datetime.now(KARACHI_TZ)


def business_date_for(moment: datetime) -> str:
    """Operating date (``YYYY-MM-DD``) for an aware datetime in Asia/Karachi.

    A naive datetime is assumed to already be Pakistan local time; an aware
    one is converted. The date is the *local calendar date* of the moment —
    which business day a post-midnight transaction belongs to is decided by
    the open business day, not by this function.
    """
    if moment.tzinfo is None:
        local = moment.replace(tzinfo=KARACHI_TZ)
    else:
        local = moment.astimezone(KARACHI_TZ)
    return local.strftime("%Y-%m-%d")


def karachi_today() -> str:
    """Today's operating date in ``Asia/Karachi`` (``YYYY-MM-DD``)."""
    return business_date_for(karachi_now())


def parse_utc_iso(value: str) -> datetime:
    """Parse a stored ``YYYY-MM-DDTHH:MM:SS.SSSZ`` timestamp to aware UTC."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_display_date(business_date: str) -> str:
    """``YYYY-MM-DD`` -> user-facing ``DD-MM-YYYY`` (§5.1-D)."""
    year, month, day = business_date.split("-")
    return f"{day}-{month}-{year}"


def format_display_datetime(utc_iso: str) -> str:
    """Stored UTC timestamp -> ``DD-MM-YYYY HH:MM`` in Pakistan time."""
    local = parse_utc_iso(utc_iso).astimezone(KARACHI_TZ)
    return local.strftime(_DISPLAY_DATETIME_FORMAT)


def is_valid_business_date(value: str) -> bool:
    """True when *value* is a real calendar date in ``YYYY-MM-DD`` form."""
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except (ValueError, TypeError):
        return False
    return True


#: The exact §5.1 machine-timestamp shape. Lexicographic ordering of values
#: in this shape is chronological, which the price and reading queries rely on.
_UTC_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def require_utc_iso(value: str) -> str:
    """Validate a machine timestamp and return it in canonical storage form.

    Accepts only the exact §5.1 shape ``YYYY-MM-DDTHH:MM:SS.SSSZ`` (so
    string comparison stays chronological); raises ``ValueError`` for
    anything else, including impossible calendar dates. The returned value
    is normalized to that shape.
    """
    text = value.strip() if isinstance(value, str) else ""
    if not _UTC_ISO_RE.match(text):
        raise ValueError(
            f"Invalid UTC timestamp: {value!r}"
            " (expected YYYY-MM-DDTHH:MM:SS.SSSZ)."
        )
    parsed = parse_utc_iso(text)  # rejects impossible dates, e.g. month 13
    return parsed.strftime(UTC_FORMAT)[:-3] + "Z"


def karachi_end_of_day_utc(business_date: str) -> str:
    """The last millisecond of a business date in ``Asia/Karachi``, as a UTC
    storage timestamp (``23:59:59.999`` PKT → ``18:59:59.999Z``).

    Used to ask "what price was effective at the end of this business day".
    """
    day = datetime.strptime(business_date, "%Y-%m-%d").date()
    local = datetime(
        day.year, day.month, day.day, 23, 59, 59, 999000, tzinfo=KARACHI_TZ
    )
    return local.astimezone(timezone.utc).strftime(UTC_FORMAT)[:-3] + "Z"


def karachi_start_of_day_utc(business_date: str) -> str:
    """00:00 ``Asia/Karachi`` on a business date, as a UTC storage timestamp
    (midnight PKT → ``19:00Z`` of the previous UTC date).

    A price change effective exactly at the day boundary needs no intraday
    meter snapshot (§11.2): the previous day's closing readings already
    delimit the intervals.
    """
    day = datetime.strptime(business_date, "%Y-%m-%d").date()
    local = datetime(
        day.year, day.month, day.day, 0, 0, 0, 0, tzinfo=KARACHI_TZ
    )
    return local.astimezone(timezone.utc).strftime(UTC_FORMAT)[:-3] + "Z"


def utc_calendar_date_in_karachi(utc_iso: str) -> str:
    """The ``Asia/Karachi`` calendar date (``YYYY-MM-DD``) of a stored UTC
    timestamp."""
    return business_date_for(parse_utc_iso(utc_iso))
