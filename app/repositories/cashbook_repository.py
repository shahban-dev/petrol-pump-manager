"""Repository for the cash book: categories, settlements, receipts, payments.

Thin, parameterized SQL only — no business logic, no transaction control.
Effective (net-of-reversal) amounts are computed by the v10 views; this
repository only reads them.
"""

from __future__ import annotations

import sqlite3

_RECEIPT_SELECT = (
    "SELECT r.*, c.name AS category, c.name_en AS category_en,"
    " cu.name AS customer_name"
    " FROM cash_receipts r"
    " JOIN cash_categories c ON c.id = r.category_id"
    " LEFT JOIN customers cu ON cu.id = r.customer_id"
)

_PAYMENT_SELECT = (
    "SELECT p.*, c.name AS category, c.name_en AS category_en"
    " FROM cash_payments p"
    " JOIN cash_categories c ON c.id = p.category_id"
)


class CashbookRepository:
    """CRUD + view readers for the Phase-5 cash book."""

    # ------------------------------------------------------------------
    # cash_categories
    # ------------------------------------------------------------------
    @staticmethod
    def get_category(
        conn: sqlite3.Connection, kind: str, name: str
    ) -> sqlite3.Row | None:
        """Category by (kind, machine name), or None."""
        return conn.execute(
            "SELECT * FROM cash_categories WHERE kind = ? AND name = ?",
            (kind, name),
        ).fetchone()

    @staticmethod
    def get_category_by_id(
        conn: sqlite3.Connection, category_id: int
    ) -> sqlite3.Row | None:
        """Category by id, or None."""
        return conn.execute(
            "SELECT * FROM cash_categories WHERE id = ?", (category_id,)
        ).fetchone()

    @staticmethod
    def list_categories(
        conn: sqlite3.Connection, kind: str, active_only: bool = True
    ) -> list[sqlite3.Row]:
        """Categories of a kind, optionally active only, in id order."""
        sql = "SELECT * FROM cash_categories WHERE kind = ?"
        if active_only:
            sql += " AND is_active = 1"
        sql += " ORDER BY id"
        return conn.execute(sql, (kind,)).fetchall()

    @staticmethod
    def insert_category(
        conn: sqlite3.Connection,
        kind: str,
        name: str,
        name_en: str,
        name_ur: str | None,
        created_by: str,
    ) -> int:
        """Insert a category; returns its id."""
        cur = conn.execute(
            "INSERT INTO cash_categories (kind, name, name_en, name_ur,"
            " created_by) VALUES (?, ?, ?, ?, ?)",
            (kind, name, name_en, name_ur, created_by),
        )
        return int(cur.lastrowid)

    @staticmethod
    def set_category_active(
        conn: sqlite3.Connection, category_id: int, active: bool
    ) -> None:
        """Flip a category's active flag (audited)."""
        conn.execute(
            "UPDATE cash_categories SET is_active = ? WHERE id = ?",
            (1 if active else 0, category_id),
        )

    @staticmethod
    def category_in_use(conn: sqlite3.Connection, category_id: int) -> bool:
        """True when any cash receipt or payment references the category."""
        row = conn.execute(
            "SELECT 1 FROM cash_receipts WHERE category_id = ?"
            " UNION ALL"
            " SELECT 1 FROM cash_payments WHERE category_id = ?"
            " LIMIT 1",
            (category_id, category_id),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # cash_receipts
    # ------------------------------------------------------------------
    @staticmethod
    def insert_receipt(
        conn: sqlite3.Connection,
        uuid: str,
        business_date: str,
        category_id: int,
        amount_paisa: int,
        source_table: str | None,
        source_id: int | None,
        customer_id: int | None,
        remarks: str | None,
        reverses_uuid: str | None,
        occurred_at_utc: str,
        created_by: str,
    ) -> int:
        """Insert a cash receipt; returns its id."""
        cur = conn.execute(
            "INSERT INTO cash_receipts (uuid, business_date, category_id,"
            " amount_paisa, source_table, source_id, customer_id, remarks,"
            " reverses_uuid, occurred_at_utc, created_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid,
                business_date,
                category_id,
                amount_paisa,
                source_table,
                source_id,
                customer_id,
                remarks,
                reverses_uuid,
                occurred_at_utc,
                created_by,
            ),
        )
        return int(cur.lastrowid)

    @staticmethod
    def get_receipt_by_uuid(
        conn: sqlite3.Connection, uuid: str
    ) -> sqlite3.Row | None:
        """One receipt with category/customer names, or None."""
        return conn.execute(
            _RECEIPT_SELECT + " WHERE r.uuid = ?", (uuid,)
        ).fetchone()

    @staticmethod
    def flip_receipt_reversed(
        conn: sqlite3.Connection, receipt_id: int, reversed_by_uuid: str
    ) -> None:
        """Whitelisted posted → reversed flip (Group C trigger)."""
        conn.execute(
            "UPDATE cash_receipts SET status = 'reversed',"
            " reversed_by_uuid = ? WHERE id = ?",
            (reversed_by_uuid, receipt_id),
        )

    @staticmethod
    def derived_receipt(
        conn: sqlite3.Connection, source_table: str, source_id: int
    ) -> sqlite3.Row | None:
        """The posted, non-reversal derived receipt for a source, or None."""
        return conn.execute(
            _RECEIPT_SELECT
            + " WHERE r.source_table = ? AND r.source_id = ?"
            " AND r.status = 'posted' AND r.reverses_uuid IS NULL",
            (source_table, source_id),
        ).fetchone()

    @staticmethod
    def list_receipts(
        conn: sqlite3.Connection, business_date: str
    ) -> list[sqlite3.Row]:
        """The day's receipts with category names, chronological."""
        return conn.execute(
            _RECEIPT_SELECT
            + " WHERE r.business_date = ?"
            " ORDER BY r.occurred_at_utc, r.id",
            (business_date,),
        ).fetchall()

    # ------------------------------------------------------------------
    # cash_payments
    # ------------------------------------------------------------------
    @staticmethod
    def insert_payment(
        conn: sqlite3.Connection,
        uuid: str,
        business_date: str,
        category_id: int,
        amount_paisa: int,
        payee: str | None,
        source_table: str | None,
        source_id: int | None,
        remarks: str | None,
        reverses_uuid: str | None,
        occurred_at_utc: str,
        created_by: str,
    ) -> int:
        """Insert a cash payment; returns its id."""
        cur = conn.execute(
            "INSERT INTO cash_payments (uuid, business_date, category_id,"
            " amount_paisa, payee, source_table, source_id, remarks,"
            " reverses_uuid, occurred_at_utc, created_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid,
                business_date,
                category_id,
                amount_paisa,
                payee,
                source_table,
                source_id,
                remarks,
                reverses_uuid,
                occurred_at_utc,
                created_by,
            ),
        )
        return int(cur.lastrowid)

    @staticmethod
    def get_payment_by_uuid(
        conn: sqlite3.Connection, uuid: str
    ) -> sqlite3.Row | None:
        """One payment with its category name, or None."""
        return conn.execute(
            _PAYMENT_SELECT + " WHERE p.uuid = ?", (uuid,)
        ).fetchone()

    @staticmethod
    def flip_payment_reversed(
        conn: sqlite3.Connection, payment_id: int, reversed_by_uuid: str
    ) -> None:
        """Whitelisted posted → reversed flip (Group C trigger)."""
        conn.execute(
            "UPDATE cash_payments SET status = 'reversed',"
            " reversed_by_uuid = ? WHERE id = ?",
            (reversed_by_uuid, payment_id),
        )

    @staticmethod
    def derived_payment(
        conn: sqlite3.Connection, source_table: str, source_id: int
    ) -> sqlite3.Row | None:
        """The posted, non-reversal derived payment for a source, or None."""
        return conn.execute(
            _PAYMENT_SELECT
            + " WHERE p.source_table = ? AND p.source_id = ?"
            " AND p.status = 'posted' AND p.reverses_uuid IS NULL",
            (source_table, source_id),
        ).fetchone()

    @staticmethod
    def list_payments(
        conn: sqlite3.Connection, business_date: str
    ) -> list[sqlite3.Row]:
        """The day's payments with category names, chronological."""
        return conn.execute(
            _PAYMENT_SELECT
            + " WHERE p.business_date = ?"
            " ORDER BY p.occurred_at_utc, p.id",
            (business_date,),
        ).fetchall()

    # ------------------------------------------------------------------
    # sales_settlements
    # ------------------------------------------------------------------
    @staticmethod
    def insert_settlement(
        conn: sqlite3.Connection,
        uuid: str,
        business_date: str,
        fuel_product_id: int,
        category: str,
        amount_paisa: int,
        source_table: str | None,
        source_id: int | None,
        batch_uuid: str,
        is_reversal: bool,
        reverses_settlement_id: int | None,
        notes: str | None,
        occurred_at_utc: str,
        created_by: str,
    ) -> int:
        """Insert one settlement leg; returns its id."""
        cur = conn.execute(
            "INSERT INTO sales_settlements (uuid, business_date,"
            " fuel_product_id, category, amount_paisa, source_table,"
            " source_id, batch_uuid, is_reversal, reverses_settlement_id,"
            " notes, occurred_at_utc, created_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid,
                business_date,
                fuel_product_id,
                category,
                amount_paisa,
                source_table,
                source_id,
                batch_uuid,
                1 if is_reversal else 0,
                reverses_settlement_id,
                notes,
                occurred_at_utc,
                created_by,
            ),
        )
        return int(cur.lastrowid)

    @staticmethod
    def flip_settlement_reversed(
        conn: sqlite3.Connection, settlement_id: int, reversed_by_batch_uuid: str
    ) -> None:
        """Whitelisted posted → reversed flip (Group C trigger)."""
        conn.execute(
            "UPDATE sales_settlements SET status = 'reversed',"
            " reversed_by_batch_uuid = ? WHERE id = ?",
            (reversed_by_batch_uuid, settlement_id),
        )

    @staticmethod
    def active_batch_uuid(
        conn: sqlite3.Connection, business_date: str
    ) -> str | None:
        """The day's active (non-reversed) settlement batch, or None."""
        row = conn.execute(
            "SELECT DISTINCT batch_uuid FROM sales_settlements"
            " WHERE business_date = ? AND is_reversal = 0"
            " AND status = 'posted'",
            (business_date,),
        ).fetchone()
        return row["batch_uuid"] if row else None

    @staticmethod
    def reversed_batch_uuids(
        conn: sqlite3.Connection, business_date: str
    ) -> list[str]:
        """Distinct reversal batch uuids recorded for the day."""
        return [
            row["batch_uuid"]
            for row in conn.execute(
                "SELECT DISTINCT batch_uuid FROM sales_settlements"
                " WHERE business_date = ? AND is_reversal = 1",
                (business_date,),
            ).fetchall()
        ]

    @staticmethod
    def settlement_rows_for_batch(
        conn: sqlite3.Connection, batch_uuid: str
    ) -> list[sqlite3.Row]:
        """Every leg of a batch (originals, for reversal)."""
        return conn.execute(
            "SELECT s.*, p.code AS fuel_product_code"
            " FROM sales_settlements s"
            " JOIN fuel_products p ON p.id = s.fuel_product_id"
            " WHERE s.batch_uuid = ? AND s.is_reversal = 0"
            " ORDER BY s.id",
            (batch_uuid,),
        ).fetchall()

    @staticmethod
    def list_settlements_by_date(
        conn: sqlite3.Connection, business_date: str
    ) -> list[sqlite3.Row]:
        """All settlement rows for a day (any status), in write order."""
        return conn.execute(
            "SELECT s.*, p.code AS fuel_product_code"
            " FROM sales_settlements s"
            " JOIN fuel_products p ON p.id = s.fuel_product_id"
            " WHERE s.business_date = ?"
            " ORDER BY s.id",
            (business_date,),
        ).fetchall()

    @staticmethod
    def effective_legs_by_product(
        conn: sqlite3.Connection, business_date: str
    ) -> list[sqlite3.Row]:
        """Net settlement legs per (product, category) from the view."""
        return conn.execute(
            "SELECT fuel_product_code, category,"
            " SUM(effective_paisa) AS total_paisa"
            " FROM v_effective_settlements"
            " WHERE business_date = ?"
            " GROUP BY fuel_product_code, category",
            (business_date,),
        ).fetchall()

    # ------------------------------------------------------------------
    # views
    # ------------------------------------------------------------------
    @staticmethod
    def cashbook_entries(
        conn: sqlite3.Connection, business_date: str
    ) -> list[sqlite3.Row]:
        """The day's cash-book rows (signed, net of reversals), chronological."""
        return conn.execute(
            "SELECT * FROM v_cashbook"
            " WHERE business_date = ?"
            " ORDER BY occurred_at_utc, kind",
            (business_date,),
        ).fetchall()

    @staticmethod
    def daily_cash_position(
        conn: sqlite3.Connection, business_date: str
    ) -> sqlite3.Row | None:
        """The day's cash position row (None when the day does not exist)."""
        return conn.execute(
            "SELECT * FROM v_daily_cash_position WHERE business_date = ?",
            (business_date,),
        ).fetchone()

    # ------------------------------------------------------------------
    # ticket lines (settlement input)
    # ------------------------------------------------------------------
    @staticmethod
    def posted_ticket_lines(
        conn: sqlite3.Connection, business_date: str
    ) -> list[sqlite3.Row]:
        """Credit-sale lines for the day's tickets (incl. reversals).

        Corrected rule (migration 011, mirroring 009): a ticket flipped to
        ``'reversed'`` keeps its posted contribution — the flip is only a
        marker — and its reversal ticket (``reverses_uuid IS NOT NULL``,
        ``paid_now = 0``) negates it. Net effect of voiding a ticket: 0.
        """
        return conn.execute(
            "SELECT l.id AS line_id, l.credit_sale_id, l.fuel_product_id,"
            " p.code AS fuel_product_code, l.line_amount_paisa,"
            " t.paid_now_paisa, t.reverses_uuid, t.customer_id"
            " FROM credit_sale_lines l"
            " JOIN credit_sales t ON t.id = l.credit_sale_id"
            " JOIN fuel_products p ON p.id = l.fuel_product_id"
            " WHERE t.business_date = ?"
            " ORDER BY t.id, l.seq_no",
            (business_date,),
        ).fetchall()

    @staticmethod
    def count_posted_tickets(
        conn: sqlite3.Connection, business_date: str
    ) -> int:
        """Number of posted credit tickets on the day (any kind)."""
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM credit_sales"
            " WHERE business_date = ? AND status = 'posted'",
            (business_date,),
        ).fetchone()
        return int(row["n"])
