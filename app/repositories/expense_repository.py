"""Repository for expense categories and posted expenses.

Thin, parameterized SQL only — no business logic, no transaction control.
Effective (net-of-reversal) amounts use the standard rule: posted → +amount,
reversal row (``reverses_uuid``) → −amount; a row flipped to
``status='reversed'`` keeps its posted contribution (migration 011).
"""

from __future__ import annotations

import sqlite3

_EXPENSE_SELECT = (
    "SELECT e.*, c.name_en AS category_en"
    " FROM expenses e"
    " JOIN expense_categories c ON c.id = e.category_id"
)


class ExpenseRepository:
    """CRUD + queries for the expense aggregate."""

    # ------------------------------------------------------------------
    # expense_categories
    # ------------------------------------------------------------------
    @staticmethod
    def list_categories(
        conn: sqlite3.Connection, active_only: bool = True
    ) -> list[sqlite3.Row]:
        """Categories, optionally active only, in id order."""
        sql = "SELECT * FROM expense_categories"
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY id"
        return conn.execute(sql).fetchall()

    @staticmethod
    def get_category(
        conn: sqlite3.Connection, category_id: int
    ) -> sqlite3.Row | None:
        """One category by id, or None."""
        return conn.execute(
            "SELECT * FROM expense_categories WHERE id = ?", (category_id,)
        ).fetchone()

    @staticmethod
    def get_category_by_name(
        conn: sqlite3.Connection, name_en: str
    ) -> sqlite3.Row | None:
        """One category by exact English name, or None."""
        return conn.execute(
            "SELECT * FROM expense_categories WHERE name_en = ?", (name_en,)
        ).fetchone()

    @staticmethod
    def insert_category(
        conn: sqlite3.Connection,
        name_en: str,
        name_ur: str | None,
        created_by: str,
    ) -> int:
        """Insert a category; returns its id."""
        cur = conn.execute(
            "INSERT INTO expense_categories (name_en, name_ur, created_by)"
            " VALUES (?, ?, ?)",
            (name_en, name_ur, created_by),
        )
        return int(cur.lastrowid)

    @staticmethod
    def set_category_active(
        conn: sqlite3.Connection, category_id: int, active: bool
    ) -> None:
        """Flip a category's active flag (audited)."""
        conn.execute(
            "UPDATE expense_categories SET is_active = ? WHERE id = ?",
            (1 if active else 0, category_id),
        )

    # ------------------------------------------------------------------
    # expenses
    # ------------------------------------------------------------------
    @staticmethod
    def insert_expense(
        conn: sqlite3.Connection,
        uuid: str,
        business_date: str,
        category_id: int,
        amount_paisa: int,
        method: str,
        payee: str | None,
        reference: str | None,
        reverses_uuid: str | None,
        occurred_at_utc: str,
        created_by: str,
    ) -> int:
        """Insert a posted expense; returns its id."""
        cur = conn.execute(
            "INSERT INTO expenses (uuid, business_date, category_id,"
            " amount_paisa, method, payee, reference, reverses_uuid,"
            " occurred_at_utc, created_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid,
                business_date,
                category_id,
                amount_paisa,
                method,
                payee,
                reference,
                reverses_uuid,
                occurred_at_utc,
                created_by,
            ),
        )
        return int(cur.lastrowid)

    @staticmethod
    def get_expense_by_uuid(
        conn: sqlite3.Connection, uuid: str
    ) -> sqlite3.Row | None:
        """One expense with its category name, or None."""
        return conn.execute(
            _EXPENSE_SELECT + " WHERE e.uuid = ?", (uuid,)
        ).fetchone()

    @staticmethod
    def flip_expense_reversed(
        conn: sqlite3.Connection, expense_id: int, reversed_by_uuid: str
    ) -> None:
        """Whitelisted posted → reversed flip (Group C trigger)."""
        conn.execute(
            "UPDATE expenses SET status = 'reversed',"
            " reversed_by_uuid = ? WHERE id = ?",
            (reversed_by_uuid, expense_id),
        )

    @staticmethod
    def list_expenses(
        conn: sqlite3.Connection,
        business_date: str | None = None,
        category_id: int | None = None,
    ) -> list[sqlite3.Row]:
        """Expenses, optionally filtered, chronological."""
        sql = _EXPENSE_SELECT
        clauses: list[str] = []
        params: list = []
        if business_date is not None:
            clauses.append("e.business_date = ?")
            params.append(business_date)
        if category_id is not None:
            clauses.append("e.category_id = ?")
            params.append(category_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY e.occurred_at_utc, e.id"
        return conn.execute(sql, params).fetchall()

    @staticmethod
    def effective_totals_by_category(
        conn: sqlite3.Connection, business_date: str
    ) -> list[sqlite3.Row]:
        """Net expense totals per category for a day (posted net of reversals)."""
        return conn.execute(
            "SELECT c.name_en AS category_en,"
            " SUM(CASE"
            "  WHEN e.reverses_uuid IS NOT NULL THEN -e.amount_paisa"
            "  ELSE e.amount_paisa END) AS total_paisa"
            " FROM expenses e"
            " JOIN expense_categories c ON c.id = e.category_id"
            " WHERE e.business_date = ?"
            " GROUP BY c.id, c.name_en"
            " ORDER BY c.name_en",
            (business_date,),
        ).fetchall()
