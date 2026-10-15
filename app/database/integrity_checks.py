"""Database integrity checks.

Thin wrappers over SQLite's own verification pragmas plus the schema-object
check from :mod:`migrator`, for use at startup (see ``app/bootstrap.py`` in a
later phase) and before/after backup and restore.

Design plan: documentation/00_design_plan.md §1, §9.
"""

from __future__ import annotations

import sqlite3

from . import migrator


def check_integrity(conn: sqlite3.Connection) -> str:
    """Run ``PRAGMA integrity_check``.

    Returns ``'ok'`` when the database is clean, otherwise the first error
    message reported by SQLite.
    """
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    if len(rows) == 1 and str(rows[0][0]).lower() == "ok":
        return "ok"
    if rows:
        return str(rows[0][0])
    return "integrity_check returned no rows"


def check_foreign_keys(conn: sqlite3.Connection) -> list[tuple]:
    """Run ``PRAGMA foreign_key_check``.

    Returns a list of violation rows ``(table, rowid, referenced_table,
    fk_index)``; an empty list means no violations.
    """
    return [tuple(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()]


def check_required_objects(conn: sqlite3.Connection) -> list[str]:
    """Return required schema objects missing from the database.

    Delegates to :func:`migrator.verify_schema`; an empty list means the
    schema is complete for the current version.
    """
    return migrator.verify_schema(conn)
