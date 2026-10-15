"""Phase-4 data transfer objects: customers, credit tickets, payments,
allocations, ledger entries and statements.

DTOs only — no SQL, no business logic (design plan §1). Money is integer
paisa, quantities integer milliliters, fuel prices integer 0.0001-PKR
units (§5.2); the dataclasses carry the stored integers, never floats.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CustomerInfo:
    """One row of ``customers`` plus the ledger-derived outstanding balance.

    ``outstanding_paisa`` comes from ``v_outstanding_receivables`` — the
    authoritative, computed balance (design plan §7) — never an editable
    column.
    """

    id: int
    customer_code: str
    name: str
    business_name: str | None
    phone: str | None
    address: str | None
    opening_receivable_paisa: int
    credit_limit_paisa: int | None  # None = no limit configured
    status: str  # 'active' | 'inactive'
    notes: str | None
    outstanding_paisa: int


@dataclass(frozen=True)
class TicketLineInfo:
    """One row of ``credit_sale_lines`` — the actual liters and prices."""

    seq_no: int
    fuel_product_id: int
    fuel_product_code: str
    nozzle_id: int | None
    nozzle_code: str | None
    quantity_ml: int
    unit_price_10k: int
    line_amount_paisa: int


@dataclass(frozen=True)
class TicketInfo:
    """A credit ticket header with its lines.

    ``unpaid_paisa`` = gross − paid_now − effective allocations (0 for
    reversed tickets and for reversal rows, which are negation rows rather
    than debts). ``warnings`` carries soft warnings recorded at issue time
    — e.g. a credit-limit exceedance, which never blocks the sale
    (§11.7).
    """

    id: int
    uuid: str
    customer_id: int
    customer_code: str
    business_date: str
    gross_amount_paisa: int
    paid_now_paisa: int
    unpaid_paisa: int
    receipt_no: str | None
    status: str  # 'posted' | 'reversed'
    reverses_uuid: str | None
    reversed_by_uuid: str | None
    occurred_at_utc: str
    notes: str | None
    created_by: str
    lines: tuple[TicketLineInfo, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class AllocationInfo:
    """One payment → ticket link with its *effective* (net of reversals)
    amount from ``v_effective_allocations``."""

    id: int
    payment_id: int
    credit_sale_id: int
    ticket_uuid: str
    ticket_receipt_no: str | None
    amount_paisa: int  # the posted allocation row
    effective_paisa: int  # posted minus allocation reversals
    business_date: str
    occurred_at_utc: str


@dataclass(frozen=True)
class PaymentInfo:
    """A customer payment with its allocations and remaining balance."""

    id: int
    uuid: str
    customer_id: int
    customer_code: str
    business_date: str
    amount_paisa: int
    method: str  # 'cash' | 'bank' | 'other'
    receipt_no: str | None
    status: str  # 'posted' | 'reversed'
    reverses_uuid: str | None
    reversed_by_uuid: str | None
    occurred_at_utc: str
    remarks: str | None
    created_by: str
    allocations: tuple[AllocationInfo, ...] = ()
    unallocated_paisa: int = 0


@dataclass(frozen=True)
class LedgerEntry:
    """One chronological row of ``v_customer_ledger``."""

    occurred_at_utc: str
    business_date: str | None
    entry_type: str  # opening|ticket|ticket_reversal|paid_now|payment|adjustment
    ref_uuid: str | None
    description: str
    debit_paisa: int
    credit_paisa: int
    running_paisa: int


@dataclass(frozen=True)
class CustomerStatement:
    """A date-filtered customer statement: entries plus totals."""

    customer: CustomerInfo
    date_from: str | None
    date_to: str | None
    entries: tuple[LedgerEntry, ...] = ()
    opening_paisa: int = 0
    debit_paisa: int = 0
    credit_paisa: int = 0
    closing_paisa: int = 0


@dataclass(frozen=True)
class AdjustmentInfo:
    """One audited opening-balance adjustment (signed paisa)."""

    id: int
    uuid: str
    customer_id: int
    business_date: str
    adjustment_paisa: int
    reason: str
    authorized_by: str
    occurred_at_utc: str
    created_by: str


# ----------------------------------------------------------------------
# Row converters (shared by the credit and payment services so both build
# identical DTOs from repository rows).
# ----------------------------------------------------------------------
def customer_info_from_row(row) -> CustomerInfo:
    """A ``customers`` row (joined with ``v_outstanding_receivables``)."""
    return CustomerInfo(
        id=int(row["id"]),
        customer_code=row["customer_code"],
        name=row["name"],
        business_name=row["business_name"],
        phone=row["phone"],
        address=row["address"],
        opening_receivable_paisa=int(row["opening_receivable_paisa"]),
        credit_limit_paisa=(
            int(row["credit_limit_paisa"])
            if row["credit_limit_paisa"] is not None
            else None
        ),
        status=row["status"],
        notes=row["notes"],
        outstanding_paisa=int(row["outstanding_paisa"]),
    )


def ticket_line_info_from_row(row) -> TicketLineInfo:
    """A ``credit_sale_lines`` row (joined with product/nozzle codes)."""
    return TicketLineInfo(
        seq_no=int(row["seq_no"]),
        fuel_product_id=int(row["fuel_product_id"]),
        fuel_product_code=row["fuel_product_code"],
        nozzle_id=(
            int(row["nozzle_id"]) if row["nozzle_id"] is not None else None
        ),
        nozzle_code=row["nozzle_code"],
        quantity_ml=int(row["quantity_ml"]),
        unit_price_10k=int(row["unit_price_10k"]),
        line_amount_paisa=int(row["line_amount_paisa"]),
    )


def ticket_info_from_row(
    row,
    *,
    lines: tuple[TicketLineInfo, ...] = (),
    warnings: tuple[str, ...] = (),
    unpaid_paisa: int = 0,
) -> TicketInfo:
    """A ``credit_sales`` row (joined with the customer code)."""
    return TicketInfo(
        id=int(row["id"]),
        uuid=row["uuid"],
        customer_id=int(row["customer_id"]),
        customer_code=row["customer_code"],
        business_date=row["business_date"],
        gross_amount_paisa=int(row["gross_amount_paisa"]),
        paid_now_paisa=int(row["paid_now_paisa"]),
        unpaid_paisa=unpaid_paisa,
        receipt_no=row["receipt_no"],
        status=row["status"],
        reverses_uuid=row["reverses_uuid"],
        reversed_by_uuid=row["reversed_by_uuid"],
        occurred_at_utc=row["occurred_at_utc"],
        notes=row["notes"],
        created_by=row["created_by"],
        lines=lines,
        warnings=warnings,
    )


def allocation_info_from_row(row) -> AllocationInfo:
    """A ``v_effective_allocations`` row (joined with ticket refs)."""
    return AllocationInfo(
        id=int(row["allocation_id"]),
        payment_id=int(row["payment_id"]),
        credit_sale_id=int(row["credit_sale_id"]),
        ticket_uuid=row["ticket_uuid"],
        ticket_receipt_no=row["ticket_receipt_no"],
        amount_paisa=int(row["amount_paisa"]),
        effective_paisa=int(row["effective_paisa"]),
        business_date=row["business_date"],
        occurred_at_utc=row["occurred_at_utc"],
    )


def payment_info_from_row(
    row,
    *,
    allocations: tuple[AllocationInfo, ...] = (),
    unallocated_paisa: int = 0,
) -> PaymentInfo:
    """A ``customer_payments`` row (joined with the customer code)."""
    return PaymentInfo(
        id=int(row["id"]),
        uuid=row["uuid"],
        customer_id=int(row["customer_id"]),
        customer_code=row["customer_code"],
        business_date=row["business_date"],
        amount_paisa=int(row["amount_paisa"]),
        method=row["method"],
        receipt_no=row["receipt_no"],
        status=row["status"],
        reverses_uuid=row["reverses_uuid"],
        reversed_by_uuid=row["reversed_by_uuid"],
        occurred_at_utc=row["occurred_at_utc"],
        remarks=row["remarks"],
        created_by=row["created_by"],
        allocations=allocations,
        unallocated_paisa=unallocated_paisa,
    )


def ledger_entry_from_row(row) -> LedgerEntry:
    """A ``v_customer_ledger`` row."""
    return LedgerEntry(
        occurred_at_utc=row["occurred_at_utc"],
        business_date=row["business_date"],
        entry_type=row["entry_type"],
        ref_uuid=row["ref_uuid"],
        description=row["description"],
        debit_paisa=int(row["debit_paisa"]),
        credit_paisa=int(row["credit_paisa"]),
        running_paisa=int(row["running_paisa"]),
    )


def adjustment_info_from_row(row) -> AdjustmentInfo:
    """A ``customer_opening_adjustments`` row."""
    return AdjustmentInfo(
        id=int(row["id"]),
        uuid=row["uuid"],
        customer_id=int(row["customer_id"]),
        business_date=row["business_date"],
        adjustment_paisa=int(row["adjustment_paisa"]),
        reason=row["reason"],
        authorized_by=row["authorized_by"],
        occurred_at_utc=row["occurred_at_utc"],
        created_by=row["created_by"],
    )
