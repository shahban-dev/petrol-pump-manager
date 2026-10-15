"""Group A timestamp triggers: immutable created_at_utc, maintained
updated_at_utc, no-op updates that neither bump nor loop.

Design plan §4 group A, §5.1 (UTC text 'YYYY-MM-DDTHH:MM:SS.SSSZ'), §10 test 18.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from .conftest import TIMESTAMP_RE, insert_business_day, insert_meter_event


def test_created_at_utc_format(db):
    """created_at_utc defaults match YYYY-MM-DDTHH:MM:SS.SSSZ (§5.1)."""
    insert_business_day(db, "2026-10-10")
    event_id = insert_meter_event(db, "1 PG+", "reset")
    db.execute("INSERT INTO app_settings (key, value) VALUES ('language', 'en')")

    for table, column, where in [
        ("business_days", "created_at_utc", "business_date = '2026-10-10'"),
        ("nozzles", "created_at_utc", "code = '1 PG+'"),
        ("fuel_products", "created_at_utc", "code = 'PETROL'"),
        # meter_events is an event table: its machine timestamp is
        # occurred_at_utc, same format.
        ("meter_events", "occurred_at_utc", f"id = {event_id}"),
        ("app_settings", "created_at_utc", "key = 'language'"),
    ]:
        value = db.execute(
            f"SELECT {column} FROM {table} WHERE {where}"
        ).fetchone()[0]
        assert TIMESTAMP_RE.match(value), f"{table}.{column} = {value!r}"


def test_occurred_at_utc_format(db):
    insert_business_day(db, "2026-10-10")
    row = db.execute(
        "INSERT INTO business_day_events (business_date, event_type)"
        " VALUES ('2026-10-10', 'open') RETURNING occurred_at_utc"
    ).fetchone()
    assert TIMESTAMP_RE.match(row[0]), row[0]


@pytest.mark.parametrize("table,update_sql", [
    ("nozzles", "UPDATE nozzles SET created_at_utc = '2020-01-01T00:00:00.000Z'"
                " WHERE code = '1 PG+'"),
    ("fuel_products", "UPDATE fuel_products SET created_at_utc = '2020-01-01T00:00:00.000Z'"
                      " WHERE code = 'PETROL'"),
    ("business_days", "UPDATE business_days SET created_at_utc = '2020-01-01T00:00:00.000Z'"
                      " WHERE business_date = '2026-10-10'"),
    ("app_settings", "UPDATE app_settings SET created_at_utc = '2020-01-01T00:00:00.000Z'"
                     " WHERE key = 'language'"),
])
def test_created_at_utc_immutable_direct_sql(db, table, update_sql):
    """A1: direct-SQL UPDATE of created_at_utc aborts (trigger, not app code)."""
    insert_business_day(db, "2026-10-10")
    db.execute("INSERT INTO app_settings (key, value) VALUES ('language', 'en')")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute(update_sql)


def test_business_update_touches_updated_at_utc(db):
    """A2: changing a business column refreshes updated_at_utc and leaves
    created_at_utc byte-identical."""
    db.execute(
        "INSERT INTO nozzles (code, fuel_product_id, label_en)"
        " VALUES ('9 TEST', (SELECT id FROM fuel_products WHERE code = 'PETROL'), 'x')"
    )
    before = db.execute(
        "SELECT created_at_utc, updated_at_utc FROM nozzles WHERE code = '9 TEST'"
    ).fetchone()
    time.sleep(0.05)  # millisecond-precision timestamps must differ

    db.execute("UPDATE nozzles SET label_en = 'changed' WHERE code = '9 TEST'")
    after = db.execute(
        "SELECT created_at_utc, updated_at_utc FROM nozzles WHERE code = '9 TEST'"
    ).fetchone()

    assert after[0] == before[0], "created_at_utc must not move on business update"
    assert after[1] != before[1], "updated_at_utc must be refreshed on business update"
    assert TIMESTAMP_RE.match(after[1])


def test_noop_update_does_not_bump_or_loop(db):
    """A2: a no-op UPDATE completes (no loop) and does not touch the
    timestamp."""
    before = db.execute(
        "SELECT created_at_utc, updated_at_utc FROM nozzles WHERE code = '1 PG+'"
    ).fetchone()
    time.sleep(0.05)

    # No-op: assigns every business column to itself.
    db.execute(
        "UPDATE nozzles SET label_en = label_en, is_active = is_active,"
        " sort_order = sort_order WHERE code = '1 PG+'"
    )
    db.commit()  # completes — no trigger loop

    after = db.execute(
        "SELECT created_at_utc, updated_at_utc FROM nozzles WHERE code = '1 PG+'"
    ).fetchone()
    assert after[0] == before[0]
    assert after[1] == before[1]
