"""Repository for customers, credit tickets, payments and allocations.

Thin, parameterized SQL only — no business logic, no transaction control.
``credit_sales`` / ``customer_payments`` / ``payment_allocations`` /
``payment_allocation_reversals`` / ``customer_opening_adjustments`` are
posted facts (immutable via Group C triggers); ``customers`` is mutable
master data. Every method is a ``@staticmethod`` taking the connection
first.
"""

from __future__ import annotations

import sqlite3

#: Select fragments with the joined codes the services' DTOs need.
_CUSTOMER_SELECT = (
    "SELECT c.*, v.outstanding_paisa"
    " FROM customers c"
    " JOIN v_outstanding_receivables v ON v.customer_id = c.id"
)
_TICKET_SELECT = (
    "SELECT cs.*, c.customer_code"
    " FROM credit_sales cs"
    " JOIN customers c ON c.id = cs.customer_id"
)
_LINE_SELECT = (
    "SELECT l.*, p.code AS fuel_product_code, n.code AS nozzle_code"
    " FROM credit_sale_lines l"
    " JOIN fuel_products p ON p.id = l.fuel_product_id"
    " LEFT JOIN nozzles n ON n.id = l.nozzle_id"
)
_PAYMENT_SELECT = (
    "SELECT cp.*, c.customer_code"
    " FROM customer_payments cp"
    " JOIN customers c ON c.id = cp.customer_id"
)
_ALLOCATION_SELECT = (
    "SELECT va.allocation_id, va.payment_id, va.credit_sale_id,"
    "       va.business_date, va.effective_paisa,"
    "       pa.amount_paisa, pa.occurred_at_utc,"
    "       cs.uuid AS ticket_uuid, cs.receipt_no AS ticket_receipt_no"
    " FROM v_effective_allocations va"
    " JOIN payment_allocations pa ON pa.id = va.allocation_id"
    " JOIN credit_sales cs ON cs.id = va.credit_sale_id"
)

#: Whitelisted columns for ``update_customer`` (opening_receivable_paisa is
#: deliberately absent: balance errors are fixed by audited opening-balance
#: adjustments, never by editing the field — design plan §11.5).
_CUSTOMER_UPDATABLE = {
    "customer_code",
    "name",
    "business_name",
    "phone",
    "address",
    "credit_limit_paisa",
    "status",
    "notes",
}


class CustomerRepository:
    """CRUD + queries for the customer-credit aggregate."""

    # ------------------------------------------------------------------
    # customers
    # ------------------------------------------------------------------
    @staticmethod
    def insert_customer(
        conn: sqlite3.Connection,
        customer_code: str,
        name: str,
        business_name: str | None,
        phone: str | None,
        address: str | None,
        opening_receivable_paisa: int,
        credit_limit_paisa: int | None,
        notes: str | None,
        created_by: str,
    ) -> int:
        """Insert a customer; returns its id."""
        cur = conn.execute(
            "INSERT INTO customers"
            " (customer_code, name, business_name, phone, address,"
            "  opening_receivable_paisa, credit_limit_paisa, notes, created_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                customer_code,
                name,
                business_name,
                phone,
                address,
                opening_receivable_paisa,
                credit_limit_paisa,
                notes,
                created_by,
            ),
        )
        return int(cur.lastrowid)

    @staticmethod
    def get_customer(
        conn: sqlite3.Connection, customer_id: int
    ) -> sqlite3.Row | None:
        """One customer with its outstanding balance, or None."""
        return conn.execute(
            _CUSTOMER_SELECT + " WHERE c.id = ?", (customer_id,)
        ).fetchone()

    @staticmethod
    def get_by_code(
        conn: sqlite3.Connection, customer_code: str
    ) -> sqlite3.Row | None:
        """One customer by exact code (case-sensitive), or None."""
        return conn.execute(
            _CUSTOMER_SELECT + " WHERE c.customer_code = ?",
            (customer_code,),
        ).fetchone()

    @staticmethod
    def list_customers(
        conn: sqlite3.Connection, active_only: bool = True
    ) -> list[sqlite3.Row]:
        """Customers with outstanding balances, code order, optionally
        active only."""
        sql = _CUSTOMER_SELECT
        if active_only:
            sql += " WHERE c.status = 'active'"
        sql += " ORDER BY c.customer_code"
        return conn.execute(sql).fetchall()

    @staticmethod
    def update_customer(
        conn: sqlite3.Connection, customer_id: int, fields: dict
    ) -> None:
        """Update whitelisted customer columns (audited by the au trigger).

        ``fields`` maps column -> new value; unknown or non-updatable
        columns raise ``ValueError`` (a programming error, not user input).
        """
        unknown = set(fields) - _CUSTOMER_UPDATABLE
        if unknown:
            raise ValueError(
                f"Customer columns not updatable: {sorted(unknown)}."
            )
        if not fields:
            return
        assignments = ", ".join(f"{col} = ?" for col in fields)
        params = list(fields.values()) + [customer_id]
        conn.execute(
            f"UPDATE customers SET {assignments} WHERE id = ?",
            params,
        )

    @staticmethod
    def next_customer_code(conn: sqlite3.Connection) -> str:
        """The next ``'CUST-%04d'`` code, from the max numeric suffix."""
        row = conn.execute(
            "SELECT COALESCE(MAX(CAST(substr(customer_code, 6) AS INTEGER)), 0)"
            " FROM customers"
            " WHERE customer_code GLOB 'CUST-[0-9]*'",
        ).fetchone()
        return f"CUST-{int(row[0]) + 1:04d}"

    # ------------------------------------------------------------------
    # credit_sales (ticket headers)
    # ------------------------------------------------------------------
    @staticmethod
    def insert_ticket(
        conn: sqlite3.Connection,
        uuid: str,
        customer_id: int,
        business_date: str,
        gross_amount_paisa: int,
        paid_now_paisa: int,
        receipt_no: str | None,
        reverses_uuid: str | None,
        occurred_at_utc: str | None,
        notes: str | None,
        created_by: str,
    ) -> int:
        """Insert a credit ticket header; returns its id.

        ``occurred_at_utc=None`` lets the database default apply; pass an
        explicit value to pin the event time.
        """
        columns = (
            "uuid, customer_id, business_date, gross_amount_paisa,"
            " paid_now_paisa, receipt_no, reverses_uuid, notes, created_by"
        )
        values: tuple = (
            uuid,
            customer_id,
            business_date,
            gross_amount_paisa,
            paid_now_paisa,
            receipt_no,
            reverses_uuid,
            notes,
            created_by,
        )
        if occurred_at_utc is not None:
            columns += ", occurred_at_utc"
            values += (occurred_at_utc,)
        placeholders = ", ".join(["?"] * len(values))
        cur = conn.execute(
            f"INSERT INTO credit_sales ({columns}) VALUES ({placeholders})",
            values,
        )
        return int(cur.lastrowid)

    @staticmethod
    def get_ticket(
        conn: sqlite3.Connection, ticket_id: int
    ) -> sqlite3.Row | None:
        """One ticket with the customer code, or None."""
        return conn.execute(
            _TICKET_SELECT + " WHERE cs.id = ?", (ticket_id,)
        ).fetchone()

    @staticmethod
    def get_ticket_by_uuid(
        conn: sqlite3.Connection, uuid: str
    ) -> sqlite3.Row | None:
        """One ticket by its uuid, or None."""
        return conn.execute(
            _TICKET_SELECT + " WHERE cs.uuid = ?", (uuid,)
        ).fetchone()

    @staticmethod
    def list_tickets_by_day(
        conn: sqlite3.Connection, business_date: str
    ) -> list[sqlite3.Row]:
        """The day's tickets, id order (includes reversal rows)."""
        return conn.execute(
            _TICKET_SELECT + " WHERE cs.business_date = ? ORDER BY cs.id",
            (business_date,),
        ).fetchall()

    @staticmethod
    def list_tickets_by_customer(
        conn: sqlite3.Connection, customer_id: int
    ) -> list[sqlite3.Row]:
        """A customer's tickets, id order."""
        return conn.execute(
            _TICKET_SELECT
            + " WHERE cs.customer_id = ? ORDER BY cs.id",
            (customer_id,),
        ).fetchall()

    @staticmethod
    def count_tickets_by_day(
        conn: sqlite3.Connection, business_date: str
    ) -> int:
        """Number of ticket rows posted on a day (receipt sequencing)."""
        row = conn.execute(
            "SELECT COUNT(*) FROM credit_sales WHERE business_date = ?",
            (business_date,),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def mark_ticket_reversed(
        conn: sqlite3.Connection, ticket_id: int, reversed_by_uuid: str
    ) -> None:
        """Flip a ticket ``posted → reversed`` (the only permitted UPDATE;
        enforced by the Group C trigger)."""
        conn.execute(
            "UPDATE credit_sales"
            " SET status = 'reversed', reversed_by_uuid = ?"
            " WHERE id = ?",
            (reversed_by_uuid, ticket_id),
        )

    # ------------------------------------------------------------------
    # credit_sale_lines
    # ------------------------------------------------------------------
    @staticmethod
    def insert_line(
        conn: sqlite3.Connection,
        credit_sale_id: int,
        seq_no: int,
        fuel_product_id: int,
        nozzle_id: int | None,
        quantity_ml: int,
        unit_price_10k: int,
        line_amount_paisa: int,
        occurred_at_utc: str | None,
        created_by: str,
    ) -> int:
        """Insert one ticket line; returns its id."""
        columns = (
            "credit_sale_id, seq_no, fuel_product_id, nozzle_id,"
            " quantity_ml, unit_price_10k, line_amount_paisa, created_by"
        )
        values: tuple = (
            credit_sale_id,
            seq_no,
            fuel_product_id,
            nozzle_id,
            quantity_ml,
            unit_price_10k,
            line_amount_paisa,
            created_by,
        )
        if occurred_at_utc is not None:
            columns += ", occurred_at_utc"
            values += (occurred_at_utc,)
        placeholders = ", ".join(["?"] * len(values))
        cur = conn.execute(
            f"INSERT INTO credit_sale_lines ({columns})"
            f" VALUES ({placeholders})",
            values,
        )
        return int(cur.lastrowid)

    @staticmethod
    def list_lines_for_ticket(
        conn: sqlite3.Connection, ticket_id: int
    ) -> list[sqlite3.Row]:
        """A ticket's lines with product/nozzle codes, seq order."""
        return conn.execute(
            _LINE_SELECT
            + " WHERE l.credit_sale_id = ? ORDER BY l.seq_no",
            (ticket_id,),
        ).fetchall()

    # ------------------------------------------------------------------
    # customer_payments
    # ------------------------------------------------------------------
    @staticmethod
    def insert_payment(
        conn: sqlite3.Connection,
        uuid: str,
        customer_id: int,
        business_date: str,
        amount_paisa: int,
        method: str,
        receipt_no: str | None,
        reverses_uuid: str | None,
        occurred_at_utc: str | None,
        remarks: str | None,
        created_by: str,
    ) -> int:
        """Insert a customer payment; returns its id."""
        columns = (
            "uuid, customer_id, business_date, amount_paisa, method,"
            " receipt_no, reverses_uuid, remarks, created_by"
        )
        values: tuple = (
            uuid,
            customer_id,
            business_date,
            amount_paisa,
            method,
            receipt_no,
            reverses_uuid,
            remarks,
            created_by,
        )
        if occurred_at_utc is not None:
            columns += ", occurred_at_utc"
            values += (occurred_at_utc,)
        placeholders = ", ".join(["?"] * len(values))
        cur = conn.execute(
            f"INSERT INTO customer_payments ({columns})"
            f" VALUES ({placeholders})",
            values,
        )
        return int(cur.lastrowid)

    @staticmethod
    def get_payment(
        conn: sqlite3.Connection, payment_id: int
    ) -> sqlite3.Row | None:
        """One payment with the customer code, or None."""
        return conn.execute(
            _PAYMENT_SELECT + " WHERE cp.id = ?", (payment_id,)
        ).fetchone()

    @staticmethod
    def get_payment_by_uuid(
        conn: sqlite3.Connection, uuid: str
    ) -> sqlite3.Row | None:
        """One payment by its uuid, or None."""
        return conn.execute(
            _PAYMENT_SELECT + " WHERE cp.uuid = ?", (uuid,)
        ).fetchone()

    @staticmethod
    def list_payments_by_day(
        conn: sqlite3.Connection, business_date: str
    ) -> list[sqlite3.Row]:
        """The day's payments, id order (includes reversal rows)."""
        return conn.execute(
            _PAYMENT_SELECT
            + " WHERE cp.business_date = ? ORDER BY cp.id",
            (business_date,),
        ).fetchall()

    @staticmethod
    def list_payments_by_customer(
        conn: sqlite3.Connection, customer_id: int
    ) -> list[sqlite3.Row]:
        """A customer's payments, id order."""
        return conn.execute(
            _PAYMENT_SELECT
            + " WHERE cp.customer_id = ? ORDER BY cp.id",
            (customer_id,),
        ).fetchall()

    @staticmethod
    def count_payments_by_day(
        conn: sqlite3.Connection, business_date: str
    ) -> int:
        """Number of payment rows posted on a day (receipt sequencing)."""
        row = conn.execute(
            "SELECT COUNT(*) FROM customer_payments WHERE business_date = ?",
            (business_date,),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def mark_payment_reversed(
        conn: sqlite3.Connection, payment_id: int, reversed_by_uuid: str
    ) -> None:
        """Flip a payment ``posted → reversed`` (the only permitted UPDATE;
        enforced by the Group C trigger)."""
        conn.execute(
            "UPDATE customer_payments"
            " SET status = 'reversed', reversed_by_uuid = ?"
            " WHERE id = ?",
            (reversed_by_uuid, payment_id),
        )

    # ------------------------------------------------------------------
    # payment_allocations
    # ------------------------------------------------------------------
    @staticmethod
    def insert_allocation(
        conn: sqlite3.Connection,
        payment_id: int,
        credit_sale_id: int,
        amount_paisa: int,
        business_date: str,
        occurred_at_utc: str | None,
        created_by: str,
    ) -> int:
        """Insert one payment → ticket allocation; returns its id.

        Group G trigger guards (payment/ticket posted, same customer,
        within ticket unpaid and payment amount) fire on this insert.
        """
        columns = (
            "payment_id, credit_sale_id, amount_paisa, business_date,"
            " created_by"
        )
        values: tuple = (
            payment_id,
            credit_sale_id,
            amount_paisa,
            business_date,
            created_by,
        )
        if occurred_at_utc is not None:
            columns += ", occurred_at_utc"
            values += (occurred_at_utc,)
        placeholders = ", ".join(["?"] * len(values))
        cur = conn.execute(
            f"INSERT INTO payment_allocations ({columns})"
            f" VALUES ({placeholders})",
            values,
        )
        return int(cur.lastrowid)

    @staticmethod
    def get_allocation(
        conn: sqlite3.Connection, allocation_id: int
    ) -> sqlite3.Row | None:
        """One allocation with its effective amount and ticket refs, or
        None."""
        return conn.execute(
            _ALLOCATION_SELECT + " WHERE va.allocation_id = ?",
            (allocation_id,),
        ).fetchone()

    @staticmethod
    def list_allocations_for_payment(
        conn: sqlite3.Connection, payment_id: int
    ) -> list[sqlite3.Row]:
        """A payment's allocations (with effective amounts), id order."""
        return conn.execute(
            _ALLOCATION_SELECT
            + " WHERE va.payment_id = ? ORDER BY va.allocation_id",
            (payment_id,),
        ).fetchall()

    @staticmethod
    def list_allocations_for_ticket(
        conn: sqlite3.Connection, ticket_id: int
    ) -> list[sqlite3.Row]:
        """A ticket's allocations (with effective amounts), id order."""
        return conn.execute(
            _ALLOCATION_SELECT
            + " WHERE va.credit_sale_id = ? ORDER BY va.allocation_id",
            (ticket_id,),
        ).fetchall()

    @staticmethod
    def allocation_integrity_rows(
        conn: sqlite3.Connection, business_date: str
    ) -> list[sqlite3.Row]:
        """All allocations of the day's *payments* with the ticket and
        payment facts the close checklist re-verifies (close check
        ``allocations_valid``)."""
        return conn.execute(
            "SELECT va.allocation_id, va.payment_id, va.credit_sale_id,"
            "       va.effective_paisa,"
            "       cp.amount_paisa AS payment_amount,"
            "       cp.customer_id AS payment_customer_id,"
            "       cp.status AS payment_status,"
            "       cs.status AS ticket_status,"
            "       cs.reverses_uuid AS ticket_reverses_uuid,"
            "       cs.customer_id AS ticket_customer_id"
            " FROM v_effective_allocations va"
            " JOIN customer_payments cp ON cp.id = va.payment_id"
            " JOIN credit_sales cs ON cs.id = va.credit_sale_id"
            " WHERE cp.business_date = ?"
            " ORDER BY va.allocation_id",
            (business_date,),
        ).fetchall()

    # ------------------------------------------------------------------
    # payment_allocation_reversals
    # ------------------------------------------------------------------
    @staticmethod
    def insert_reversal(
        conn: sqlite3.Connection,
        uuid: str,
        allocation_id: int,
        payment_id: int,
        credit_sale_id: int,
        amount_paisa: int,
        business_date: str,
        reason: str,
        occurred_at_utc: str | None,
        created_by: str,
    ) -> int:
        """Insert an audited allocation reversal; returns its id.

        The trigger backstops that the reversal stays within the
        allocation's remaining (unreversed) amount.
        """
        columns = (
            "uuid, allocation_id, payment_id, credit_sale_id, amount_paisa,"
            " business_date, reason, created_by"
        )
        values: tuple = (
            uuid,
            allocation_id,
            payment_id,
            credit_sale_id,
            amount_paisa,
            business_date,
            reason,
            created_by,
        )
        if occurred_at_utc is not None:
            columns += ", occurred_at_utc"
            values += (occurred_at_utc,)
        placeholders = ", ".join(["?"] * len(values))
        cur = conn.execute(
            "INSERT INTO payment_allocation_reversals"
            f" ({columns}) VALUES ({placeholders})",
            values,
        )
        return int(cur.lastrowid)

    @staticmethod
    def list_reversals_for_allocation(
        conn: sqlite3.Connection, allocation_id: int
    ) -> list[sqlite3.Row]:
        """An allocation's reversal rows, id order."""
        return conn.execute(
            "SELECT * FROM payment_allocation_reversals"
            " WHERE allocation_id = ? ORDER BY id",
            (allocation_id,),
        ).fetchall()

    @staticmethod
    def effective_for_allocation(
        conn: sqlite3.Connection, allocation_id: int
    ) -> int:
        """One allocation's effective (posted − reversed) amount."""
        row = conn.execute(
            "SELECT effective_paisa FROM v_effective_allocations"
            " WHERE allocation_id = ?",
            (allocation_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def effective_for_payment(
        conn: sqlite3.Connection, payment_id: int
    ) -> int:
        """A payment's total effective allocated amount."""
        row = conn.execute(
            "SELECT COALESCE(SUM(effective_paisa), 0)"
            " FROM v_effective_allocations WHERE payment_id = ?",
            (payment_id,),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def effective_for_ticket(
        conn: sqlite3.Connection, ticket_id: int
    ) -> int:
        """A ticket's total effective allocated amount (from posted,
        non-reversal payments only — the same scope as
        ``v_outstanding_receivables``)."""
        row = conn.execute(
            "SELECT COALESCE(SUM(va.effective_paisa), 0)"
            " FROM v_effective_allocations va"
            " JOIN customer_payments cp ON cp.id = va.payment_id"
            " WHERE va.credit_sale_id = ?"
            "   AND cp.status = 'posted' AND cp.reverses_uuid IS NULL",
            (ticket_id,),
        ).fetchone()
        return int(row[0])

    # ------------------------------------------------------------------
    # customer_opening_adjustments
    # ------------------------------------------------------------------
    @staticmethod
    def insert_adjustment(
        conn: sqlite3.Connection,
        uuid: str,
        customer_id: int,
        business_date: str,
        adjustment_paisa: int,
        reason: str,
        authorized_by: str,
        occurred_at_utc: str | None,
        created_by: str,
    ) -> int:
        """Insert an audited (signed) opening-balance adjustment; returns
        its id."""
        columns = (
            "uuid, customer_id, business_date, adjustment_paisa, reason,"
            " authorized_by, created_by"
        )
        values: tuple = (
            uuid,
            customer_id,
            business_date,
            adjustment_paisa,
            reason,
            authorized_by,
            created_by,
        )
        if occurred_at_utc is not None:
            columns += ", occurred_at_utc"
            values += (occurred_at_utc,)
        placeholders = ", ".join(["?"] * len(values))
        cur = conn.execute(
            "INSERT INTO customer_opening_adjustments"
            f" ({columns}) VALUES ({placeholders})",
            values,
        )
        return int(cur.lastrowid)

    @staticmethod
    def list_adjustments_for_customer(
        conn: sqlite3.Connection, customer_id: int
    ) -> list[sqlite3.Row]:
        """A customer's opening adjustments, id order."""
        return conn.execute(
            "SELECT * FROM customer_opening_adjustments"
            " WHERE customer_id = ? ORDER BY id",
            (customer_id,),
        ).fetchall()

    # ------------------------------------------------------------------
    # balance / FIFO queries
    # ------------------------------------------------------------------
    @staticmethod
    def outstanding_for_customer(
        conn: sqlite3.Connection, customer_id: int
    ) -> int | None:
        """Ledger-derived outstanding receivable (design plan §7), or None
        when the customer does not exist."""
        row = conn.execute(
            "SELECT outstanding_paisa FROM v_outstanding_receivables"
            " WHERE customer_id = ?",
            (customer_id,),
        ).fetchone()
        return int(row[0]) if row else None

    @staticmethod
    def outstanding_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
        """``v_outstanding_receivables`` for every customer, code order."""
        return conn.execute(
            "SELECT * FROM v_outstanding_receivables"
            " ORDER BY customer_code"
        ).fetchall()

    @staticmethod
    def outstanding_tickets_fifo(
        conn: sqlite3.Connection, customer_id: int
    ) -> list[sqlite3.Row]:
        """Posted, non-reversal tickets with effective unpaid > 0, oldest
        first by ``(occurred_at_utc, id)`` — the FIFO allocation order
        (§11.4)."""
        return conn.execute(
            "SELECT cs.id, cs.uuid, cs.receipt_no, cs.gross_amount_paisa,"
            "       cs.paid_now_paisa, cs.occurred_at_utc,"
            "       (cs.gross_amount_paisa - cs.paid_now_paisa)"
            "       - COALESCE(alloc.alloc_paisa, 0) AS unpaid_paisa"
            " FROM credit_sales cs"
            " LEFT JOIN ("
            "   SELECT va.credit_sale_id, SUM(va.effective_paisa) AS alloc_paisa"
            "   FROM v_effective_allocations va"
            "   JOIN customer_payments cp ON cp.id = va.payment_id"
            "   WHERE cp.status = 'posted' AND cp.reverses_uuid IS NULL"
            "   GROUP BY va.credit_sale_id"
            " ) alloc ON alloc.credit_sale_id = cs.id"
            " WHERE cs.customer_id = ?"
            "   AND cs.status = 'posted'"
            "   AND cs.reverses_uuid IS NULL"
            "   AND (cs.gross_amount_paisa - cs.paid_now_paisa)"
            "       - COALESCE(alloc.alloc_paisa, 0) > 0"
            " ORDER BY cs.occurred_at_utc, cs.id",
            (customer_id,),
        ).fetchall()

    @staticmethod
    def ticket_unpaid(
        conn: sqlite3.Connection, ticket_id: int
    ) -> int | None:
        """Effective unpaid balance of a posted, non-reversal ticket; None
        when the ticket is missing, reversed, or itself a reversal row."""
        row = conn.execute(
            "SELECT (cs.gross_amount_paisa - cs.paid_now_paisa)"
            "       - COALESCE(alloc.alloc_paisa, 0) AS unpaid_paisa"
            " FROM credit_sales cs"
            " LEFT JOIN ("
            "   SELECT va.credit_sale_id, SUM(va.effective_paisa) AS alloc_paisa"
            "   FROM v_effective_allocations va"
            "   JOIN customer_payments cp ON cp.id = va.payment_id"
            "   WHERE cp.status = 'posted' AND cp.reverses_uuid IS NULL"
            "   GROUP BY va.credit_sale_id"
            " ) alloc ON alloc.credit_sale_id = cs.id"
            " WHERE cs.id = ?"
            "   AND cs.status = 'posted'"
            "   AND cs.reverses_uuid IS NULL",
            (ticket_id,),
        ).fetchone()
        return int(row[0]) if row else None

    @staticmethod
    def payment_unallocated(
        conn: sqlite3.Connection, payment_id: int
    ) -> int | None:
        """A payment's unallocated remainder (amount − effective
        allocations); None when the payment does not exist."""
        row = conn.execute(
            "SELECT cp.amount_paisa"
            "       - COALESCE((SELECT SUM(va.effective_paisa)"
            "                   FROM v_effective_allocations va"
            "                   WHERE va.payment_id = cp.id), 0)"
            "       AS unallocated_paisa"
            " FROM customer_payments cp WHERE cp.id = ?",
            (payment_id,),
        ).fetchone()
        return int(row[0]) if row else None

    @staticmethod
    def ledger_for_customer(
        conn: sqlite3.Connection, customer_id: int
    ) -> list[sqlite3.Row]:
        """``v_customer_ledger`` entries for a customer, chronological."""
        return conn.execute(
            "SELECT * FROM v_customer_ledger WHERE customer_id = ?"
            " ORDER BY occurred_at_utc",
            (customer_id,),
        ).fetchall()
