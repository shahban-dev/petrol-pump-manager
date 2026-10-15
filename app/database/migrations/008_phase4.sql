-- ============================================================================
-- Migration 008 — Phase 4: customer credit (customers, tickets, payments,
-- allocations)
-- Design plan: documentation/00_design_plan.md §4 (trigger groups A/B/C/D/G/
-- H/I), §5 (numeric standards), §7 (customer-credit model), §11 (assumptions
-- #4 FIFO allocation, #7 credit limits warning-only, #8 no auto-writeoffs).
--
-- New tables:
--   * customers                    — mutable master data (Group A1/A2, B).
--   * credit_sales                 — ticket HEADER: customer, day, totals,
--                                    paid-now, status posted|reversed.
--                                    Immutable (Group C) except the whitelisted
--                                    posted → reversed transition, which flips
--                                    the original row and is always paired with
--                                    a reversal ROW (reverses_uuid) posted on a
--                                    writable day. The original row and its
--                                    audit trail are never erased.
--   * credit_sale_lines            — per-fuel-product ticket lines (the actual
--                                    liters/prices; the header gross must cover
--                                    them). Immutable (Group C).
--   * customer_payments            — one row per money received. Immutable
--                                    except the posted → reversed transition.
--   * payment_allocations          — payment → ticket links. Immutable (C);
--                                    changes go through payment_allocation_
--                                    reversals + new rows (audited, reversible).
--   * payment_allocation_reversals — audited cancellation (full or partial) of
--                                    one allocation row. Immutable (C).
--   * customer_opening_adjustments — audited fixes to a customer's opening
--                                    receivable (signed paisa; the computed
--                                    ledger balance itself is never editable).
--                                    Immutable (C).
--
-- Accounting model (§7):
--   outstanding(customer)
--     = opening_receivable
--     + Σ posted ticket (gross − paid_now)          [reversal rows negate]
--     − Σ effective allocations to the customer's tickets
--     + Σ opening adjustments                       [signed]
--   A credit ticket NEVER creates an inventory movement and NEVER creates a
--   second revenue entry: its liters are a classification of the meter-derived
--   sales (§6). The close checklist's credit_liters_guard warns when ticket
--   liters exceed the day's metered liters.
--   paid_now_paisa is cash taken AT the sale moment; it allocates to THIS
--   ticket only — never to older debt (§11.4). Old-debt recovery is a
--   customer_payments row + allocations; it is a receipt, not revenue (§6).
--
-- Trigger message strings consumed by app/services/_base.py translation:
--   'business day is closed; reopen it first'      → ClosedDayError
--   'credit_sales is immutable; ...'               → BusinessRuleError
--   'customer_payments is immutable; ...'          → BusinessRuleError
--   'payment_allocations is immutable; ...'        → BusinessRuleError
--   'allocation exceeds the ticket''s unpaid balance'
--   'total allocations exceed the payment amount'
--   'cannot allocate a reversed or missing payment'
--   'cannot allocate to a reversed or missing credit ticket'
--   'payment and ticket belong to different customers'
--   'credit ticket line amount does not match quantity × unit price'
--   'nozzle fuel product does not match the line fuel product'
--   'cannot add lines to a reversed credit ticket'
--   'credit ticket lines exceed the ticket gross amount'
--   'reversal ticket must ...' / 'reversal payment must ...'
--   'allocation reversal exceeds the remaining allocated amount'
--   'credit ticket customer is unknown or inactive'
--
-- Numeric standards (§5): money INTEGER paisa (*_paisa), quantities INTEGER
-- milliliters (*_ml), fuel prices INTEGER 0.0001 PKR/litre (*_10k). The
-- line-amount trigger uses the canonical formula
--   amount_paisa = round_half_up(quantity_ml * unit_price_10k / 100000)
-- SQLite integer division truncates, which equals round-half-up for
-- non-negative values, so (q * p + 50000) / 100000.
-- ============================================================================

-- --- Customers (mutable master data) ------------------------------------------
CREATE TABLE customers (
    id                      INTEGER PRIMARY KEY,
    customer_code           TEXT NOT NULL UNIQUE,
    name                    TEXT NOT NULL,
    business_name           TEXT,
    phone                   TEXT,
    address                 TEXT,
    opening_receivable_paisa INTEGER NOT NULL DEFAULT 0
                            CHECK (opening_receivable_paisa >= 0),
    credit_limit_paisa      INTEGER
                            CHECK (credit_limit_paisa IS NULL
                                   OR credit_limit_paisa >= 0),
    status                  TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'inactive')),
    notes                   TEXT,
    created_by              TEXT NOT NULL DEFAULT 'system',
    created_at_utc          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at_utc          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_customers_status ON customers (status);

-- --- Credit sale tickets (header; immutable posted facts) ---------------------
CREATE TABLE credit_sales (
    id                 INTEGER PRIMARY KEY,
    uuid               TEXT NOT NULL UNIQUE,
    customer_id        INTEGER NOT NULL REFERENCES customers (id),
    business_date      TEXT NOT NULL REFERENCES business_days (business_date),
    gross_amount_paisa INTEGER NOT NULL CHECK (gross_amount_paisa >= 0),
    paid_now_paisa     INTEGER NOT NULL DEFAULT 0 CHECK (paid_now_paisa >= 0),
    receipt_no         TEXT UNIQUE,
    status             TEXT NOT NULL DEFAULT 'posted'
                       CHECK (status IN ('posted', 'reversed')),
    reverses_uuid      TEXT REFERENCES credit_sales (uuid),
    reversed_by_uuid   TEXT REFERENCES credit_sales (uuid),
    occurred_at_utc    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    created_by         TEXT NOT NULL DEFAULT 'system',
    created_at_utc     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    notes              TEXT,
    CHECK (paid_now_paisa <= gross_amount_paisa),
    CHECK (reverses_uuid IS NULL OR paid_now_paisa = 0)
);
CREATE INDEX idx_credit_sales_customer_date ON credit_sales (customer_id, business_date);
CREATE INDEX idx_credit_sales_date ON credit_sales (business_date);

-- --- Credit sale lines (per-product detail; immutable) ------------------------
CREATE TABLE credit_sale_lines (
    id                INTEGER PRIMARY KEY,
    credit_sale_id    INTEGER NOT NULL REFERENCES credit_sales (id),
    seq_no            INTEGER NOT NULL,
    fuel_product_id   INTEGER NOT NULL REFERENCES fuel_products (id),
    nozzle_id         INTEGER REFERENCES nozzles (id),
    quantity_ml       INTEGER NOT NULL CHECK (quantity_ml > 0),
    unit_price_10k    INTEGER NOT NULL CHECK (unit_price_10k > 0),
    line_amount_paisa INTEGER NOT NULL CHECK (line_amount_paisa >= 0),
    occurred_at_utc   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    created_by        TEXT NOT NULL DEFAULT 'system',
    created_at_utc    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (credit_sale_id, seq_no)
);
CREATE INDEX idx_credit_sale_lines_ticket ON credit_sale_lines (credit_sale_id);

-- --- Customer payments (immutable posted facts) -------------------------------
CREATE TABLE customer_payments (
    id               INTEGER PRIMARY KEY,
    uuid             TEXT NOT NULL UNIQUE,
    customer_id      INTEGER NOT NULL REFERENCES customers (id),
    business_date    TEXT NOT NULL REFERENCES business_days (business_date),
    amount_paisa     INTEGER NOT NULL CHECK (amount_paisa > 0),
    method           TEXT NOT NULL CHECK (method IN ('cash', 'bank', 'other')),
    receipt_no       TEXT UNIQUE,
    status           TEXT NOT NULL DEFAULT 'posted'
                     CHECK (status IN ('posted', 'reversed')),
    reverses_uuid    TEXT REFERENCES customer_payments (uuid),
    reversed_by_uuid TEXT REFERENCES customer_payments (uuid),
    occurred_at_utc  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    created_by       TEXT NOT NULL DEFAULT 'system',
    created_at_utc   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    remarks          TEXT
);
CREATE INDEX idx_customer_payments_customer_date
    ON customer_payments (customer_id, business_date);
CREATE INDEX idx_customer_payments_date ON customer_payments (business_date);

-- --- Payment allocations (immutable; changed via reversals + new rows) --------
CREATE TABLE payment_allocations (
    id             INTEGER PRIMARY KEY,
    payment_id     INTEGER NOT NULL REFERENCES customer_payments (id),
    credit_sale_id INTEGER NOT NULL REFERENCES credit_sales (id),
    amount_paisa   INTEGER NOT NULL CHECK (amount_paisa > 0),
    business_date  TEXT NOT NULL REFERENCES business_days (business_date),
    occurred_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    created_by     TEXT NOT NULL DEFAULT 'system',
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (payment_id, credit_sale_id)
);
CREATE INDEX idx_payment_allocations_payment ON payment_allocations (payment_id);
CREATE INDEX idx_payment_allocations_ticket ON payment_allocations (credit_sale_id);

-- --- Allocation reversals (audited cancellation of allocation rows) -----------
CREATE TABLE payment_allocation_reversals (
    id             INTEGER PRIMARY KEY,
    uuid           TEXT NOT NULL UNIQUE,
    allocation_id  INTEGER NOT NULL REFERENCES payment_allocations (id),
    payment_id     INTEGER NOT NULL REFERENCES customer_payments (id),
    credit_sale_id INTEGER NOT NULL REFERENCES credit_sales (id),
    amount_paisa   INTEGER NOT NULL CHECK (amount_paisa > 0),
    business_date  TEXT NOT NULL REFERENCES business_days (business_date),
    reason         TEXT NOT NULL,
    occurred_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    created_by     TEXT NOT NULL DEFAULT 'system',
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_allocation_reversals_allocation
    ON payment_allocation_reversals (allocation_id);

-- --- Customer opening-balance adjustments (signed, audited) -------------------
CREATE TABLE customer_opening_adjustments (
    id               INTEGER PRIMARY KEY,
    uuid             TEXT NOT NULL UNIQUE,
    customer_id      INTEGER NOT NULL REFERENCES customers (id),
    business_date    TEXT NOT NULL REFERENCES business_days (business_date),
    adjustment_paisa INTEGER NOT NULL CHECK (adjustment_paisa <> 0),
    reason           TEXT NOT NULL,
    authorized_by    TEXT NOT NULL DEFAULT 'system',
    occurred_at_utc  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    created_by       TEXT NOT NULL DEFAULT 'system',
    created_at_utc   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_opening_adjustments_customer
    ON customer_opening_adjustments (customer_id);

-- --- Default allocation order: FIFO (§11.4) -----------------------------------
-- NOTE: the allocation_order default ('fifo') is intentionally NOT seeded as an
-- app_settings row here. app_settings carries audit triggers (003), and
-- migration-time inserts would leave audit rows behind (the suite pins an
-- empty audit log after migration). PaymentService treats a missing
-- allocation_order setting as 'fifo' — the configured default.

-- ================= Group A1 — created_at_utc immutability =====================
CREATE TRIGGER trg_customers_bu_guard_created
BEFORE UPDATE ON customers
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_credit_sales_bu_guard_created
BEFORE UPDATE ON credit_sales
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_credit_sale_lines_bu_guard_created
BEFORE UPDATE ON credit_sale_lines
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_customer_payments_bu_guard_created
BEFORE UPDATE ON customer_payments
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_payment_allocations_bu_guard_created
BEFORE UPDATE ON payment_allocations
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_payment_allocation_reversals_bu_guard_created
BEFORE UPDATE ON payment_allocation_reversals
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_customer_opening_adjustments_bu_guard_created
BEFORE UPDATE ON customer_opening_adjustments
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

-- ================= Group A2 — updated_at_utc touch (customers only) ===========
-- Fires only when a business column changed; the inner UPDATE re-fires these
-- triggers but matches nothing, so there is no loop (see 002 header).
CREATE TRIGGER trg_customers_bu_touch_updated
BEFORE UPDATE ON customers
FOR EACH ROW
WHEN (OLD.customer_code IS NOT NEW.customer_code
      OR OLD.name IS NOT NEW.name
      OR OLD.business_name IS NOT NEW.business_name
      OR OLD.phone IS NOT NEW.phone
      OR OLD.address IS NOT NEW.address
      OR OLD.opening_receivable_paisa IS NOT NEW.opening_receivable_paisa
      OR OLD.credit_limit_paisa IS NOT NEW.credit_limit_paisa
      OR OLD.status IS NOT NEW.status
      OR OLD.notes IS NOT NEW.notes
      OR OLD.created_by IS NOT NEW.created_by)
BEGIN
    UPDATE customers
       SET updated_at_utc = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
     WHERE id = NEW.id;
END;

-- ================= Group B — audit history ====================================
-- One trigger set per table: AFTER INSERT, AFTER UPDATE (business-column
-- WHEN guard so the A2 timestamp touch writes no audit row), BEFORE DELETE.
-- audit_log itself has no audit triggers (see 003); the application exposes
-- no UPDATE/DELETE path for audit_log and 003's trg_audit_log_no_modify
-- aborts any attempt.

CREATE TRIGGER trg_customers_ai_audit
AFTER INSERT ON customers
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('customers', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'customer_code', NEW.customer_code,
                        'name', NEW.name,
                        'business_name', NEW.business_name,
                        'phone', NEW.phone,
                        'address', NEW.address,
                        'opening_receivable_paisa', NEW.opening_receivable_paisa,
                        'credit_limit_paisa', NEW.credit_limit_paisa,
                        'status', NEW.status,
                        'notes', NEW.notes,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc,
                        'updated_at_utc', NEW.updated_at_utc),
            NULL, NEW.created_by);
END;

CREATE TRIGGER trg_customers_au_audit
AFTER UPDATE ON customers
FOR EACH ROW
WHEN (OLD.customer_code IS NOT NEW.customer_code
      OR OLD.name IS NOT NEW.name
      OR OLD.business_name IS NOT NEW.business_name
      OR OLD.phone IS NOT NEW.phone
      OR OLD.address IS NOT NEW.address
      OR OLD.opening_receivable_paisa IS NOT NEW.opening_receivable_paisa
      OR OLD.credit_limit_paisa IS NOT NEW.credit_limit_paisa
      OR OLD.status IS NOT NEW.status
      OR OLD.notes IS NOT NEW.notes
      OR OLD.created_by IS NOT NEW.created_by)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('customers', NEW.id, 'UPDATE',
            json_object('id', OLD.id,
                        'customer_code', OLD.customer_code,
                        'name', OLD.name,
                        'business_name', OLD.business_name,
                        'phone', OLD.phone,
                        'address', OLD.address,
                        'opening_receivable_paisa', OLD.opening_receivable_paisa,
                        'credit_limit_paisa', OLD.credit_limit_paisa,
                        'status', OLD.status,
                        'notes', OLD.notes,
                        'created_by', OLD.created_by,
                        'created_at_utc', OLD.created_at_utc,
                        'updated_at_utc', OLD.updated_at_utc),
            json_object('id', NEW.id,
                        'customer_code', NEW.customer_code,
                        'name', NEW.name,
                        'business_name', NEW.business_name,
                        'phone', NEW.phone,
                        'address', NEW.address,
                        'opening_receivable_paisa', NEW.opening_receivable_paisa,
                        'credit_limit_paisa', NEW.credit_limit_paisa,
                        'status', NEW.status,
                        'notes', NEW.notes,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc,
                        'updated_at_utc', NEW.updated_at_utc),
            NULL, NEW.created_by);
END;

CREATE TRIGGER trg_customers_bd_audit
BEFORE DELETE ON customers
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('customers', OLD.id, 'DELETE',
            json_object('id', OLD.id,
                        'customer_code', OLD.customer_code,
                        'name', OLD.name,
                        'business_name', OLD.business_name,
                        'phone', OLD.phone,
                        'address', OLD.address,
                        'opening_receivable_paisa', OLD.opening_receivable_paisa,
                        'credit_limit_paisa', OLD.credit_limit_paisa,
                        'status', OLD.status,
                        'notes', OLD.notes,
                        'created_by', OLD.created_by,
                        'created_at_utc', OLD.created_at_utc,
                        'updated_at_utc', OLD.updated_at_utc),
            NULL, NULL, OLD.created_by);
END;

CREATE TRIGGER trg_credit_sales_ai_audit
AFTER INSERT ON credit_sales
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('credit_sales', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'uuid', NEW.uuid,
                        'customer_id', NEW.customer_id,
                        'business_date', NEW.business_date,
                        'gross_amount_paisa', NEW.gross_amount_paisa,
                        'paid_now_paisa', NEW.paid_now_paisa,
                        'receipt_no', NEW.receipt_no,
                        'status', NEW.status,
                        'reverses_uuid', NEW.reverses_uuid,
                        'reversed_by_uuid', NEW.reversed_by_uuid,
                        'occurred_at_utc', NEW.occurred_at_utc,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc,
                        'notes', NEW.notes),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_credit_sales_au_audit
AFTER UPDATE ON credit_sales
FOR EACH ROW
-- The only permitted UPDATE is the posted → reversed transition; audit it.
WHEN (OLD.status IS NOT NEW.status
      OR OLD.reversed_by_uuid IS NOT NEW.reversed_by_uuid)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor, reason)
    VALUES ('credit_sales', NEW.id, 'UPDATE',
            json_object('id', OLD.id,
                        'uuid', OLD.uuid,
                        'status', OLD.status,
                        'reversed_by_uuid', OLD.reversed_by_uuid),
            json_object('id', NEW.id,
                        'uuid', NEW.uuid,
                        'status', NEW.status,
                        'reversed_by_uuid', NEW.reversed_by_uuid),
            NEW.business_date, NEW.created_by,
            'status transition ' || OLD.status || ' → ' || NEW.status);
END;

CREATE TRIGGER trg_credit_sales_bd_audit
BEFORE DELETE ON credit_sales
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('credit_sales', OLD.id, 'DELETE',
            json_object('id', OLD.id,
                        'uuid', OLD.uuid,
                        'customer_id', OLD.customer_id,
                        'business_date', OLD.business_date,
                        'gross_amount_paisa', OLD.gross_amount_paisa,
                        'paid_now_paisa', OLD.paid_now_paisa,
                        'status', OLD.status),
            NULL, OLD.business_date, OLD.created_by);
END;

CREATE TRIGGER trg_credit_sale_lines_ai_audit
AFTER INSERT ON credit_sale_lines
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('credit_sale_lines', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'credit_sale_id', NEW.credit_sale_id,
                        'seq_no', NEW.seq_no,
                        'fuel_product_id', NEW.fuel_product_id,
                        'nozzle_id', NEW.nozzle_id,
                        'quantity_ml', NEW.quantity_ml,
                        'unit_price_10k', NEW.unit_price_10k,
                        'line_amount_paisa', NEW.line_amount_paisa,
                        'occurred_at_utc', NEW.occurred_at_utc,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc),
            (SELECT business_date FROM credit_sales
              WHERE id = NEW.credit_sale_id),
            NEW.created_by);
END;

CREATE TRIGGER trg_credit_sale_lines_au_audit
AFTER UPDATE ON credit_sale_lines
FOR EACH ROW
WHEN (OLD.credit_sale_id IS NOT NEW.credit_sale_id
      OR OLD.seq_no IS NOT NEW.seq_no
      OR OLD.fuel_product_id IS NOT NEW.fuel_product_id
      OR OLD.nozzle_id IS NOT NEW.nozzle_id
      OR OLD.quantity_ml IS NOT NEW.quantity_ml
      OR OLD.unit_price_10k IS NOT NEW.unit_price_10k
      OR OLD.line_amount_paisa IS NOT NEW.line_amount_paisa)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('credit_sale_lines', NEW.id, 'UPDATE',
            json_object('id', OLD.id,
                        'credit_sale_id', OLD.credit_sale_id,
                        'quantity_ml', OLD.quantity_ml,
                        'unit_price_10k', OLD.unit_price_10k,
                        'line_amount_paisa', OLD.line_amount_paisa),
            json_object('id', NEW.id,
                        'credit_sale_id', NEW.credit_sale_id,
                        'quantity_ml', NEW.quantity_ml,
                        'unit_price_10k', NEW.unit_price_10k,
                        'line_amount_paisa', NEW.line_amount_paisa),
            (SELECT business_date FROM credit_sales
              WHERE id = NEW.credit_sale_id),
            NEW.created_by);
END;

CREATE TRIGGER trg_credit_sale_lines_bd_audit
BEFORE DELETE ON credit_sale_lines
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('credit_sale_lines', OLD.id, 'DELETE',
            json_object('id', OLD.id,
                        'credit_sale_id', OLD.credit_sale_id,
                        'quantity_ml', OLD.quantity_ml,
                        'line_amount_paisa', OLD.line_amount_paisa),
            NULL,
            (SELECT business_date FROM credit_sales
              WHERE id = OLD.credit_sale_id),
            OLD.created_by);
END;

CREATE TRIGGER trg_customer_payments_ai_audit
AFTER INSERT ON customer_payments
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('customer_payments', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'uuid', NEW.uuid,
                        'customer_id', NEW.customer_id,
                        'business_date', NEW.business_date,
                        'amount_paisa', NEW.amount_paisa,
                        'method', NEW.method,
                        'receipt_no', NEW.receipt_no,
                        'status', NEW.status,
                        'reverses_uuid', NEW.reverses_uuid,
                        'reversed_by_uuid', NEW.reversed_by_uuid,
                        'occurred_at_utc', NEW.occurred_at_utc,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc,
                        'remarks', NEW.remarks),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_customer_payments_au_audit
AFTER UPDATE ON customer_payments
FOR EACH ROW
-- The only permitted UPDATE is the posted → reversed transition; audit it.
WHEN (OLD.status IS NOT NEW.status
      OR OLD.reversed_by_uuid IS NOT NEW.reversed_by_uuid)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor, reason)
    VALUES ('customer_payments', NEW.id, 'UPDATE',
            json_object('id', OLD.id,
                        'uuid', OLD.uuid,
                        'status', OLD.status,
                        'reversed_by_uuid', OLD.reversed_by_uuid),
            json_object('id', NEW.id,
                        'uuid', NEW.uuid,
                        'status', NEW.status,
                        'reversed_by_uuid', NEW.reversed_by_uuid),
            NEW.business_date, NEW.created_by,
            'status transition ' || OLD.status || ' → ' || NEW.status);
END;

CREATE TRIGGER trg_customer_payments_bd_audit
BEFORE DELETE ON customer_payments
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('customer_payments', OLD.id, 'DELETE',
            json_object('id', OLD.id,
                        'uuid', OLD.uuid,
                        'customer_id', OLD.customer_id,
                        'amount_paisa', OLD.amount_paisa,
                        'status', OLD.status),
            NULL, OLD.business_date, OLD.created_by);
END;

CREATE TRIGGER trg_payment_allocations_ai_audit
AFTER INSERT ON payment_allocations
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('payment_allocations', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'payment_id', NEW.payment_id,
                        'credit_sale_id', NEW.credit_sale_id,
                        'amount_paisa', NEW.amount_paisa,
                        'business_date', NEW.business_date,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_payment_allocations_au_audit
AFTER UPDATE ON payment_allocations
FOR EACH ROW
-- UPDATEs are blocked by Group C, but if one ever passed it must be audited.
WHEN (OLD.payment_id IS NOT NEW.payment_id
      OR OLD.credit_sale_id IS NOT NEW.credit_sale_id
      OR OLD.amount_paisa IS NOT NEW.amount_paisa)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('payment_allocations', NEW.id, 'UPDATE',
            json_object('id', OLD.id,
                        'payment_id', OLD.payment_id,
                        'credit_sale_id', OLD.credit_sale_id,
                        'amount_paisa', OLD.amount_paisa),
            json_object('id', NEW.id,
                        'payment_id', NEW.payment_id,
                        'credit_sale_id', NEW.credit_sale_id,
                        'amount_paisa', NEW.amount_paisa),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_payment_allocations_bd_audit
BEFORE DELETE ON payment_allocations
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('payment_allocations', OLD.id, 'DELETE',
            json_object('id', OLD.id,
                        'payment_id', OLD.payment_id,
                        'credit_sale_id', OLD.credit_sale_id,
                        'amount_paisa', OLD.amount_paisa),
            NULL, OLD.business_date, OLD.created_by);
END;

CREATE TRIGGER trg_payment_allocation_reversals_ai_audit
AFTER INSERT ON payment_allocation_reversals
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor, reason)
    VALUES ('payment_allocation_reversals', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'uuid', NEW.uuid,
                        'allocation_id', NEW.allocation_id,
                        'payment_id', NEW.payment_id,
                        'credit_sale_id', NEW.credit_sale_id,
                        'amount_paisa', NEW.amount_paisa,
                        'reason', NEW.reason,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc),
            NEW.business_date, NEW.created_by, NEW.reason);
END;

CREATE TRIGGER trg_payment_allocation_reversals_bd_audit
BEFORE DELETE ON payment_allocation_reversals
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('payment_allocation_reversals', OLD.id, 'DELETE',
            json_object('id', OLD.id,
                        'uuid', OLD.uuid,
                        'allocation_id', OLD.allocation_id,
                        'amount_paisa', OLD.amount_paisa),
            NULL, OLD.business_date, OLD.created_by);
END;

CREATE TRIGGER trg_customer_opening_adjustments_ai_audit
AFTER INSERT ON customer_opening_adjustments
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor, reason)
    VALUES ('customer_opening_adjustments', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'uuid', NEW.uuid,
                        'customer_id', NEW.customer_id,
                        'adjustment_paisa', NEW.adjustment_paisa,
                        'reason', NEW.reason,
                        'authorized_by', NEW.authorized_by,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc),
            NEW.business_date, NEW.created_by, NEW.reason);
END;

CREATE TRIGGER trg_customer_opening_adjustments_bd_audit
BEFORE DELETE ON customer_opening_adjustments
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('customer_opening_adjustments', OLD.id, 'DELETE',
            json_object('id', OLD.id,
                        'uuid', OLD.uuid,
                        'customer_id', OLD.customer_id,
                        'adjustment_paisa', OLD.adjustment_paisa),
            NULL, OLD.business_date, OLD.created_by);
END;

-- ================= Group D — closed business day =============================
-- D1: no new credit/payment/allocation rows may be posted to a closed day.
-- Status 'reopened' (transient during an authorized correction) permits
-- writes; the reopen workflow itself is service-level and audited via
-- business_day_events. Same message as the Phase-1/2/3 D1 triggers so the
-- service error translation keeps working.
CREATE TRIGGER trg_credit_sales_bi_closed_day
BEFORE INSERT ON credit_sales
FOR EACH ROW
WHEN (SELECT status FROM business_days
       WHERE business_date = NEW.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;

CREATE TRIGGER trg_credit_sale_lines_bi_closed_day
BEFORE INSERT ON credit_sale_lines
FOR EACH ROW
WHEN (SELECT status FROM business_days WHERE business_date =
      (SELECT business_date FROM credit_sales
        WHERE id = NEW.credit_sale_id)) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;

CREATE TRIGGER trg_customer_payments_bi_closed_day
BEFORE INSERT ON customer_payments
FOR EACH ROW
WHEN (SELECT status FROM business_days
       WHERE business_date = NEW.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;

CREATE TRIGGER trg_payment_allocations_bi_closed_day
BEFORE INSERT ON payment_allocations
FOR EACH ROW
WHEN (SELECT status FROM business_days
       WHERE business_date = NEW.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;

CREATE TRIGGER trg_payment_allocation_reversals_bi_closed_day
BEFORE INSERT ON payment_allocation_reversals
FOR EACH ROW
WHEN (SELECT status FROM business_days
       WHERE business_date = NEW.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;

CREATE TRIGGER trg_customer_opening_adjustments_bi_closed_day
BEFORE INSERT ON customer_opening_adjustments
FOR EACH ROW
WHEN (SELECT status FROM business_days
       WHERE business_date = NEW.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;

-- D2: the reversal status flip touches the ORIGINAL row, which lives on the
-- original business date. A closed day's ticket/payment cannot be reversed
-- without the authorized reopen workflow (§11.13). Created BEFORE the Group C
-- triggers below so the closed-day rejection fires first and the service
-- maps it to ClosedDayError.
CREATE TRIGGER trg_credit_sales_bu_closed_day
BEFORE UPDATE ON credit_sales
FOR EACH ROW
WHEN (SELECT status FROM business_days
       WHERE business_date = OLD.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;

CREATE TRIGGER trg_customer_payments_bu_closed_day
BEFORE UPDATE ON customer_payments
FOR EACH ROW
WHEN (SELECT status FROM business_days
       WHERE business_date = OLD.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;

-- ================= Group C — posted-ledger immutability ======================
-- credit_sales / customer_payments: no UPDATE except the whitelisted
-- posted → reversed transition (which additionally requires reversed_by_uuid
-- and leaves every other column untouched); no DELETE ever. Corrections are
-- reversal rows linked by reverses_uuid / reversed_by_uuid — the original row
-- and its audit trail survive.
CREATE TRIGGER trg_credit_sales_bu_no_update
BEFORE UPDATE ON credit_sales
FOR EACH ROW
WHEN NOT (OLD.status = 'posted' AND NEW.status = 'reversed'
          AND OLD.reversed_by_uuid IS NULL
          AND NEW.reversed_by_uuid IS NOT NULL
          AND NEW.id IS OLD.id
          AND NEW.uuid IS OLD.uuid
          AND NEW.customer_id IS OLD.customer_id
          AND NEW.business_date IS OLD.business_date
          AND NEW.gross_amount_paisa IS OLD.gross_amount_paisa
          AND NEW.paid_now_paisa IS OLD.paid_now_paisa
          AND NEW.receipt_no IS OLD.receipt_no
          AND NEW.reverses_uuid IS OLD.reverses_uuid
          AND NEW.occurred_at_utc IS OLD.occurred_at_utc
          AND NEW.created_by IS OLD.created_by
          AND NEW.created_at_utc IS OLD.created_at_utc
          AND NEW.notes IS OLD.notes)
BEGIN
    SELECT RAISE(ABORT, 'credit_sales is immutable; reverse the ticket with a reversal row instead');
END;

CREATE TRIGGER trg_credit_sales_bd_no_delete
BEFORE DELETE ON credit_sales
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'credit_sales is immutable; rows cannot be deleted');
END;

CREATE TRIGGER trg_customer_payments_bu_no_update
BEFORE UPDATE ON customer_payments
FOR EACH ROW
WHEN NOT (OLD.status = 'posted' AND NEW.status = 'reversed'
          AND OLD.reversed_by_uuid IS NULL
          AND NEW.reversed_by_uuid IS NOT NULL
          AND NEW.id IS OLD.id
          AND NEW.uuid IS OLD.uuid
          AND NEW.customer_id IS OLD.customer_id
          AND NEW.business_date IS OLD.business_date
          AND NEW.amount_paisa IS OLD.amount_paisa
          AND NEW.method IS OLD.method
          AND NEW.receipt_no IS OLD.receipt_no
          AND NEW.reverses_uuid IS OLD.reverses_uuid
          AND NEW.occurred_at_utc IS OLD.occurred_at_utc
          AND NEW.created_by IS OLD.created_by
          AND NEW.created_at_utc IS OLD.created_at_utc
          AND NEW.remarks IS OLD.remarks)
BEGIN
    SELECT RAISE(ABORT, 'customer_payments is immutable; reverse the payment with a reversal row instead');
END;

CREATE TRIGGER trg_customer_payments_bd_no_delete
BEFORE DELETE ON customer_payments
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'customer_payments is immutable; rows cannot be deleted');
END;

-- credit_sale_lines / payment_allocations / reversals / opening adjustments:
-- no UPDATE, no DELETE. Allocation changes are new allocation rows paired with
-- payment_allocation_reversals rows (audited, reversible); a wrong opening
-- adjustment is fixed by a compensating row, never an edit.
CREATE TRIGGER trg_credit_sale_lines_bu_no_update
BEFORE UPDATE ON credit_sale_lines
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'credit_sale_lines is immutable; reverse the ticket instead');
END;

CREATE TRIGGER trg_credit_sale_lines_bd_no_delete
BEFORE DELETE ON credit_sale_lines
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'credit_sale_lines is immutable; rows cannot be deleted');
END;

CREATE TRIGGER trg_payment_allocations_bu_no_update
BEFORE UPDATE ON payment_allocations
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'payment_allocations is immutable; change allocations via the audited re-allocation workflow instead');
END;

CREATE TRIGGER trg_payment_allocations_bd_no_delete
BEFORE DELETE ON payment_allocations
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'payment_allocations is immutable; rows cannot be deleted');
END;

CREATE TRIGGER trg_payment_allocation_reversals_bu_no_update
BEFORE UPDATE ON payment_allocation_reversals
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'payment_allocation_reversals is immutable; post a compensating row instead');
END;

CREATE TRIGGER trg_payment_allocation_reversals_bd_no_delete
BEFORE DELETE ON payment_allocation_reversals
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'payment_allocation_reversals is immutable; rows cannot be deleted');
END;

CREATE TRIGGER trg_customer_opening_adjustments_bu_no_update
BEFORE UPDATE ON customer_opening_adjustments
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'customer_opening_adjustments is immutable; post a compensating adjustment instead');
END;

CREATE TRIGGER trg_customer_opening_adjustments_bd_no_delete
BEFORE DELETE ON customer_opening_adjustments
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'customer_opening_adjustments is immutable; rows cannot be deleted');
END;

-- ================= Group I — row validation ===================================
-- credit_sales: new tickets need an active customer; reversal rows must mirror
-- the original ticket (same customer, same gross, no paid-now on a reversal).
CREATE TRIGGER trg_credit_sales_bi_validate
BEFORE INSERT ON credit_sales
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'credit ticket customer is unknown or inactive')
     WHERE NEW.reverses_uuid IS NULL
       AND NOT EXISTS (SELECT 1 FROM customers
                        WHERE id = NEW.customer_id AND status = 'active');

    SELECT RAISE(ABORT, 'reversal ticket must reference a posted original ticket')
     WHERE NEW.reverses_uuid IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM credit_sales
                        WHERE uuid = NEW.reverses_uuid
                          AND status = 'posted'
                          AND reverses_uuid IS NULL);

    SELECT RAISE(ABORT, 'reversal ticket must match the original ticket')
     WHERE NEW.reverses_uuid IS NOT NULL
       AND (NEW.customer_id IS NOT (SELECT customer_id FROM credit_sales
                                    WHERE uuid = NEW.reverses_uuid)
            OR NEW.gross_amount_paisa IS NOT (SELECT gross_amount_paisa
                                               FROM credit_sales
                                               WHERE uuid = NEW.reverses_uuid)
            OR NEW.paid_now_paisa <> 0);
END;

-- credit_sale_lines: exact line amounts, nozzle/product consistency, lines
-- covered by the header gross, no lines on reversed tickets.
CREATE TRIGGER trg_credit_sale_lines_bi_validate
BEFORE INSERT ON credit_sale_lines
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'cannot add lines to a reversed credit ticket')
     WHERE (SELECT status FROM credit_sales
             WHERE id = NEW.credit_sale_id) <> 'posted';

    SELECT RAISE(ABORT, 'credit ticket line amount does not match quantity × unit price')
     WHERE NEW.line_amount_paisa <> (NEW.quantity_ml * NEW.unit_price_10k + 50000) / 100000;

    SELECT RAISE(ABORT, 'nozzle fuel product does not match the line fuel product')
     WHERE NEW.nozzle_id IS NOT NULL
       AND (SELECT fuel_product_id FROM nozzles WHERE id = NEW.nozzle_id)
           IS NOT NEW.fuel_product_id;

    SELECT RAISE(ABORT, 'credit ticket lines exceed the ticket gross amount')
     WHERE (SELECT COALESCE(SUM(line_amount_paisa), 0) FROM credit_sale_lines
             WHERE credit_sale_id = NEW.credit_sale_id)
           + NEW.line_amount_paisa
           > (SELECT gross_amount_paisa FROM credit_sales
               WHERE id = NEW.credit_sale_id);
END;

-- customer_payments: reversal rows must mirror the original payment.
CREATE TRIGGER trg_customer_payments_bi_validate
BEFORE INSERT ON customer_payments
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'payment customer is unknown')
     WHERE NOT EXISTS (SELECT 1 FROM customers WHERE id = NEW.customer_id);

    SELECT RAISE(ABORT, 'reversal payment must reference a posted original payment')
     WHERE NEW.reverses_uuid IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM customer_payments
                        WHERE uuid = NEW.reverses_uuid
                          AND status = 'posted'
                          AND reverses_uuid IS NULL);

    SELECT RAISE(ABORT, 'reversal payment must match the original payment')
     WHERE NEW.reverses_uuid IS NOT NULL
       AND (NEW.customer_id IS NOT (SELECT customer_id FROM customer_payments
                                    WHERE uuid = NEW.reverses_uuid)
            OR NEW.amount_paisa IS NOT (SELECT amount_paisa FROM customer_payments
                                         WHERE uuid = NEW.reverses_uuid)
            OR NEW.method IS NOT (SELECT method FROM customer_payments
                                   WHERE uuid = NEW.reverses_uuid));
END;

-- payment_allocation_reversals: must stay within the allocation's remainder.
CREATE TRIGGER trg_payment_allocation_reversals_bi_validate
BEFORE INSERT ON payment_allocation_reversals
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'allocation reversal references a missing allocation')
     WHERE NOT EXISTS (SELECT 1 FROM payment_allocations
                        WHERE id = NEW.allocation_id);

    SELECT RAISE(ABORT, 'allocation reversal must reference the same payment and ticket')
     WHERE (SELECT payment_id FROM payment_allocations
             WHERE id = NEW.allocation_id) IS NOT NEW.payment_id
        OR (SELECT credit_sale_id FROM payment_allocations
             WHERE id = NEW.allocation_id) IS NOT NEW.credit_sale_id;

    SELECT RAISE(ABORT, 'allocation reversal exceeds the remaining allocated amount')
     WHERE NEW.amount_paisa >
           (SELECT pa.amount_paisa
                   - COALESCE((SELECT SUM(par.amount_paisa)
                               FROM payment_allocation_reversals par
                               WHERE par.allocation_id = pa.id), 0)
            FROM payment_allocations pa WHERE pa.id = NEW.allocation_id);
END;

-- ================= Group G — payment allocation guards =======================
-- G1: every new allocation is validated against the payment, the ticket and
-- their current effective (allocation − reversal) totals. The aggregate
-- checks run inside the trigger so they hold for ANY writer, including raw
-- SQL; the whole statement (and its transaction) aborts on violation, so a
-- failed allocation persists nothing (acceptance Test 12).
CREATE TRIGGER trg_payment_allocations_bi_check
BEFORE INSERT ON payment_allocations
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'cannot allocate a reversed or missing payment')
     WHERE NOT EXISTS (SELECT 1 FROM customer_payments
                        WHERE id = NEW.payment_id AND status = 'posted');

    SELECT RAISE(ABORT, 'cannot allocate to a reversed or missing credit ticket')
     WHERE NOT EXISTS (SELECT 1 FROM credit_sales
                        WHERE id = NEW.credit_sale_id
                          AND status = 'posted'
                          AND reverses_uuid IS NULL);

    SELECT RAISE(ABORT, 'payment and ticket belong to different customers')
     WHERE (SELECT customer_id FROM customer_payments
             WHERE id = NEW.payment_id)
           IS NOT (SELECT customer_id FROM credit_sales
                    WHERE id = NEW.credit_sale_id);

    SELECT RAISE(ABORT, 'allocation exceeds the ticket''s unpaid balance')
     WHERE NEW.amount_paisa >
           (SELECT (cs.gross_amount_paisa - cs.paid_now_paisa)
                   - COALESCE((SELECT SUM(pa2.amount_paisa)
                                     - COALESCE((SELECT SUM(par2.amount_paisa)
                                                 FROM payment_allocation_reversals par2
                                                 WHERE par2.allocation_id = pa2.id), 0)
                               FROM payment_allocations pa2
                               WHERE pa2.credit_sale_id = NEW.credit_sale_id), 0)
            FROM credit_sales cs WHERE cs.id = NEW.credit_sale_id);

    SELECT RAISE(ABORT, 'total allocations exceed the payment amount')
     WHERE (SELECT COALESCE(SUM(pa3.amount_paisa), 0)
              FROM payment_allocations pa3 WHERE pa3.payment_id = NEW.payment_id)
           - COALESCE((SELECT SUM(par3.amount_paisa)
                       FROM payment_allocation_reversals par3
                       JOIN payment_allocations pa4 ON pa4.id = par3.allocation_id
                       WHERE pa4.payment_id = NEW.payment_id), 0)
           + NEW.amount_paisa
           > (SELECT amount_paisa FROM customer_payments
               WHERE id = NEW.payment_id);
END;
-- G2: BEFORE UPDATE/DELETE on payment_allocations abort unconditionally (the
-- Group C triggers above). Allocation changes go through the audited
-- re-allocation workflow: reversal row(s) + new allocation row(s).

-- ================= Reporting views ============================================
-- Effective allocation per allocation row: posted amount minus reversals.
CREATE VIEW v_effective_allocations AS
SELECT pa.id AS allocation_id,
       pa.payment_id,
       pa.credit_sale_id,
       pa.business_date,
       pa.amount_paisa
       - COALESCE((SELECT SUM(par.amount_paisa)
                   FROM payment_allocation_reversals par
                   WHERE par.allocation_id = pa.id), 0)
       AS effective_paisa
FROM payment_allocations pa;

-- Ledger-derived outstanding receivable per customer (design §7). This view —
-- never an editable column — is the authoritative balance.
CREATE VIEW v_outstanding_receivables AS
SELECT c.id AS customer_id,
       c.customer_code,
       c.name,
       c.credit_limit_paisa,
       c.opening_receivable_paisa
       + COALESCE((SELECT SUM(CASE
                               WHEN cs.status = 'reversed' THEN 0
                               WHEN cs.reverses_uuid IS NOT NULL
                               THEN -(cs.gross_amount_paisa - cs.paid_now_paisa)
                               ELSE (cs.gross_amount_paisa - cs.paid_now_paisa)
                              END)
                   FROM credit_sales cs WHERE cs.customer_id = c.id), 0)
       - COALESCE((SELECT SUM(va.effective_paisa)
                   FROM v_effective_allocations va
                   JOIN customer_payments cp ON cp.id = va.payment_id
                   WHERE cp.customer_id = c.id
                     AND cp.status = 'posted'
                     AND cp.reverses_uuid IS NULL), 0)
       + COALESCE((SELECT SUM(adj.adjustment_paisa)
                   FROM customer_opening_adjustments adj
                   WHERE adj.customer_id = c.id), 0)
       AS outstanding_paisa
FROM customers c;

-- Chronological customer ledger with running balance: opening, ticket charges
-- (gross as debit, paid-now as credit), payment allocations (credit),
-- opening adjustments (signed). Powers statements and receipts.
CREATE VIEW v_customer_ledger AS
WITH entries AS (
    SELECT c.id AS customer_id,
           c.created_at_utc AS occurred_at_utc,
           NULL AS business_date,
           0 AS sort_key,
           'opening' AS entry_type,
           NULL AS ref_uuid,
           'Opening balance' AS description,
           c.opening_receivable_paisa AS debit_paisa,
           0 AS credit_paisa
    FROM customers c
    UNION ALL
    SELECT cs.customer_id,
           cs.occurred_at_utc,
           cs.business_date,
           10,
           CASE WHEN cs.reverses_uuid IS NOT NULL
                THEN 'ticket_reversal' ELSE 'ticket' END,
           cs.uuid,
           'Credit ticket ' || COALESCE(cs.receipt_no, substr(cs.uuid, 1, 8)),
           CASE WHEN (CASE WHEN cs.status = 'reversed' THEN 0
                           WHEN cs.reverses_uuid IS NOT NULL
                           THEN -(cs.gross_amount_paisa - cs.paid_now_paisa)
                           ELSE (cs.gross_amount_paisa - cs.paid_now_paisa)
                      END) > 0
                THEN (CASE WHEN cs.status = 'reversed' THEN 0
                           WHEN cs.reverses_uuid IS NOT NULL
                           THEN -(cs.gross_amount_paisa - cs.paid_now_paisa)
                           ELSE (cs.gross_amount_paisa - cs.paid_now_paisa)
                      END) ELSE 0 END,
           CASE WHEN (CASE WHEN cs.status = 'reversed' THEN 0
                           WHEN cs.reverses_uuid IS NOT NULL
                           THEN -(cs.gross_amount_paisa - cs.paid_now_paisa)
                           ELSE (cs.gross_amount_paisa - cs.paid_now_paisa)
                      END) < 0
                THEN -(CASE WHEN cs.status = 'reversed' THEN 0
                            WHEN cs.reverses_uuid IS NOT NULL
                            THEN -(cs.gross_amount_paisa - cs.paid_now_paisa)
                            ELSE (cs.gross_amount_paisa - cs.paid_now_paisa)
                       END) ELSE 0 END
    FROM credit_sales cs
    UNION ALL
    SELECT cs.customer_id,
           cs.occurred_at_utc,
           cs.business_date,
           11,
           'paid_now',
           cs.uuid,
           'Paid at sale ' || COALESCE(cs.receipt_no, substr(cs.uuid, 1, 8)),
           0,
           cs.paid_now_paisa
    FROM credit_sales cs
    WHERE cs.paid_now_paisa > 0 AND cs.reverses_uuid IS NULL
    UNION ALL
    SELECT cp.customer_id,
           cp.occurred_at_utc,
           cp.business_date,
           20,
           'payment',
           cp.uuid,
           'Payment ' || COALESCE(cp.receipt_no, substr(cp.uuid, 1, 8))
               || ' → ticket ' || COALESCE(t.receipt_no, substr(t.uuid, 1, 8)),
           0,
           va.effective_paisa
    FROM v_effective_allocations va
    JOIN customer_payments cp ON cp.id = va.payment_id
    JOIN credit_sales t ON t.id = va.credit_sale_id
    WHERE va.effective_paisa > 0
      AND cp.status = 'posted'
      AND cp.reverses_uuid IS NULL
    UNION ALL
    SELECT adj.customer_id,
           adj.occurred_at_utc,
           adj.business_date,
           30,
           'adjustment',
           adj.uuid,
           'Opening adjustment: ' || adj.reason,
           CASE WHEN adj.adjustment_paisa > 0 THEN adj.adjustment_paisa
                ELSE 0 END,
           CASE WHEN adj.adjustment_paisa < 0 THEN -adj.adjustment_paisa
                ELSE 0 END
    FROM customer_opening_adjustments adj
)
SELECT customer_id,
       occurred_at_utc,
       business_date,
       entry_type,
       ref_uuid,
       description,
       debit_paisa,
       credit_paisa,
       SUM(debit_paisa - credit_paisa) OVER (
           PARTITION BY customer_id
           ORDER BY occurred_at_utc, sort_key, ref_uuid
           ROWS UNBOUNDED PRECEDING
       ) AS running_paisa
FROM entries;
