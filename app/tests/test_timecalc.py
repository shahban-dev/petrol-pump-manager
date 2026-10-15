"""Unit tests for app.calculations.time — the §5.1 timestamp policy.

Pure functions, no database. Business dates are Asia/Karachi calendar
dates; machine timestamps are UTC 'YYYY-MM-DDTHH:MM:SS.SSSZ'.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from app.calculations import time as timecalc

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
UTC = timezone.utc


def test_utc_now_iso_format():
    """Millisecond-precision UTC storage format."""
    assert TIMESTAMP_RE.match(timecalc.utc_now_iso())


def test_business_date_for_karachi_evening():
    """19:30 UTC = 00:30 PKT next day."""
    assert (
        timecalc.business_date_for(datetime(2026, 10, 14, 19, 30, tzinfo=UTC))
        == "2026-10-15"
    )


def test_business_date_for_karachi_late_night():
    """18:59 UTC = 23:59 PKT — still the same business date."""
    assert (
        timecalc.business_date_for(datetime(2026, 10, 14, 18, 59, tzinfo=UTC))
        == "2026-10-14"
    )


def test_business_date_for_midnight_boundary():
    """19:00 UTC = midnight PKT — the new local date."""
    assert (
        timecalc.business_date_for(datetime(2026, 10, 14, 19, 0, tzinfo=UTC))
        == "2026-10-15"
    )


def test_business_date_for_naive_assumes_local():
    """Naive datetimes are treated as Pakistan local time."""
    assert (
        timecalc.business_date_for(datetime(2026, 10, 15, 0, 30)) == "2026-10-15"
    )


def test_parse_utc_iso_roundtrip():
    stamp = "2026-10-15T08:58:36.125Z"
    parsed = timecalc.parse_utc_iso(stamp)
    assert parsed.tzinfo is not None
    assert parsed.strftime("%Y-%m-%dT%H:%M:%S") == "2026-10-15T08:58:36"
    assert parsed.microsecond == 125_000


def test_format_display_date():
    """User-facing date format is DD-MM-YYYY."""
    assert timecalc.format_display_date("2026-10-15") == "15-10-2026"


def test_format_display_datetime_converts_to_pkt():
    """19:30 UTC displays as 00:30 the next day in Pakistan time."""
    assert (
        timecalc.format_display_datetime("2026-10-14T19:30:00.000Z")
        == "15-10-2026 00:30"
    )


def test_is_valid_business_date():
    assert timecalc.is_valid_business_date("2026-10-15")
    assert not timecalc.is_valid_business_date("2026-13-01")
    assert not timecalc.is_valid_business_date("15-10-2026")
    assert not timecalc.is_valid_business_date("")
    assert not timecalc.is_valid_business_date(None)
