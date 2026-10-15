-- 010_phase5.sql — Phase 5: cash book and accounting.
--
-- New tables:
--   cash_categories            configurable receipt/payment categories (manager-editable)
--   sales_settlements          the ledger connecting meter-derived daily revenue to
--                              cash / bank / paid_now_on_ticket / receivable / other
--   cash_receipts              physical-cash receipts (cash method only, §11.14)
--   cash_payments              physical-cash payments  (cash method only, §11.14)
--   expense_categories         configurable expense categories (manager-editable)
--   expenses                   dated expenses; cash ones post exactly one cash row
--   supplier_payments          partial/full settlements against delivery invoices
--   other_products             engine oil / lubricants / similar (master data)
--   other_product_transactions purchase | sale | adjustment (immutable facts)
--
-- New views:
--   v_effective_settlements    settlement legs net of reversals (signed paisa)
--   v_cashbook                 chronological cash receipts/payments (signed paisa)
--   v_supplier_balances        invoiced − paid-at-delivery − payments, per supplier
--   v_other_product_stock      on-hand units per other product
--   v_daily_cash_position      opening + receipts − payments = expected, per day
--
-- Trigger groups installed here: A (timestamps), B (audit), C (posted-ledger
-- immutability with reversal whitelists), D (closed business day), H (unique
-- source references — each source posts its cash row exactly once), I
-- (amount guards + supplier-payment allocation cap).
--
-- Money: INTEGER paisa. Quantities: fuel INTEGER milliliters; other products
-- INTEGER smallest units (their `unit` column names the granularity).
-- Timestamps: UTC 'YYYY-MM-DDTHH:MM:SS.SSSZ' via strftime; business_date is
-- the Asia/Karachi operating date. Non-negotiable rules from the master
-- prompt: no float/REAL anywhere for money or quantities; every rule is
-- enforced by the database, not just the service layer.

-- ===========================================================================
-- cash_categories — configurable receipt/payment categories
-- ===========================================================================
CREATE TABLE cash_categories (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN ('receipt', 'payment')),
    name           TEXT NOT NULL,          -- machine key, e.g. 'cash_sales'
    name_en        TEXT NOT NULL,
    name_ur        TEXT,
    is_active      INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_by     TEXT NOT NULL DEFAULT 'system',
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (kind, name)
);

-- Seeded categories. The manager may add/deactivate more via the service;
-- seeded rows are configuration, never hard-coded business logic.
INSERT INTO cash_categories (kind, name, name_en, name_ur, created_by) VALUES
    ('receipt', 'cash_sales',         'Cash sales',            'نقد فروخت',          'system'),
    ('receipt', 'paid_now_on_ticket', 'Paid at sale (credit)', 'ادائیگی بوقت فروخت', 'system'),
    ('receipt', 'credit_recovery',    'Old debt recovery',     'پرانے قرض کی وصولی', 'system'),
    ('receipt', 'other_sales',        'Other product sales',   'دیگر اشیاء کی فروخت', 'system'),
    ('receipt', 'other_receipt',      'Other receipt',         'دیگر وصولی',          'system'),
    ('payment', 'supplier_payment',       'Supplier payment',      'سپلائر کو ادائیگی', 'system'),
    ('payment', 'expense',                'Expense',               'اخراجات',          'system'),
    ('payment', 'other_materials_purchase','Other materials purchase','دیگر اشیاء کی خرید','system'),
    ('payment', 'other_payment',          'Other payment',         'دیگر ادائیگی',     'system');

CREATE INDEX idx_cash_categories_kind ON cash_categories (kind);

-- ===========================================================================
-- sales_settlements — meter revenue = settled + new receivables (§6)
-- ===========================================================================
-- One row per (day, product, category) for manager-entered legs, or one row
-- per credit-sale LINE for ticket-derived legs. The service enforces the
-- identity per product:
--   meter_revenue = cash + bank + paid_now_on_ticket + receivable + other
-- 'receivable' legs are a classification of the ticket (gross − paid_now);
-- they never create cash or revenue on their own. Old-debt recovery never
-- appears here — it is a cash_receipts row, not revenue.
CREATE TABLE sales_settlements (
    id                     INTEGER PRIMARY KEY,
    uuid                   TEXT NOT NULL UNIQUE,
    business_date          TEXT NOT NULL REFERENCES business_days (business_date),
    fuel_product_id        INTEGER NOT NULL REFERENCES fuel_products (id),
    category               TEXT NOT NULL CHECK (category IN
                               ('cash', 'bank', 'paid_now_on_ticket',
                                'receivable', 'other')),
    amount_paisa           INTEGER NOT NULL CHECK (amount_paisa >= 0),
    source_table           TEXT,     -- 'credit_sale_lines' for ticket-derived legs
    source_id              INTEGER,  -- credit_sale_lines.id; NULL for manager legs
    batch_uuid             TEXT NOT NULL,
    is_reversal            INTEGER NOT NULL DEFAULT 0 CHECK (is_reversal IN (0, 1)),
    reverses_settlement_id INTEGER REFERENCES sales_settlements (id),
    status                 TEXT NOT NULL DEFAULT 'posted'
                           CHECK (status IN ('posted', 'reversed')),
    reversed_by_batch_uuid TEXT,
    notes                  TEXT,
    occurred_at_utc        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    created_by             TEXT NOT NULL DEFAULT 'system',
    created_at_utc         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (is_reversal = 0 OR reverses_settlement_id IS NOT NULL),
    CHECK (status = 'posted' OR reversed_by_batch_uuid IS NOT NULL)
);
-- A ticket line settles each of its legs exactly once per active batch …
CREATE UNIQUE INDEX uq_settlement_ticket_leg ON sales_settlements
    (business_date, fuel_product_id, category, source_id)
    WHERE status = 'posted' AND is_reversal = 0 AND source_id IS NOT NULL;
-- … and a manager-entered leg exists at most once per (day, product, category).
CREATE UNIQUE INDEX uq_settlement_manager_leg ON sales_settlements
    (business_date, fuel_product_id, category)
    WHERE status = 'posted' AND is_reversal = 0 AND source_id IS NULL;
CREATE INDEX idx_settlements_date ON sales_settlements (business_date);
CREATE INDEX idx_settlements_batch ON sales_settlements (batch_uuid);

-- ===========================================================================
-- cash_receipts / cash_payments — the physical cash book (cash only, §11.14)
-- ===========================================================================
-- method is CHECK-constrained to 'cash': bank/other receipts never touch
-- physical cash. Non-cash sales are tracked in sales_settlements (bank).
CREATE TABLE cash_receipts (
    id               INTEGER PRIMARY KEY,
    uuid             TEXT NOT NULL UNIQUE,
    business_date    TEXT NOT NULL REFERENCES business_days (business_date),
    category_id      INTEGER NOT NULL REFERENCES cash_categories (id),
    amount_paisa     INTEGER NOT NULL CHECK (amount_paisa > 0),
    source_table     TEXT,     -- e.g. 'customer_payments', 'credit_sales',
                              -- 'sales_settlements', 'other_product_transactions'
    source_id        INTEGER,  -- NULL for manually entered receipts
    customer_id      INTEGER REFERENCES customers (id),
    method           TEXT NOT NULL DEFAULT 'cash' CHECK (method = 'cash'),
    remarks          TEXT,
    status           TEXT NOT NULL DEFAULT 'posted'
                     CHECK (status IN ('posted', 'reversed')),
    reverses_uuid    TEXT REFERENCES cash_receipts (uuid),
    reversed_by_uuid TEXT REFERENCES cash_receipts (uuid),
    occurred_at_utc  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    created_by       TEXT NOT NULL DEFAULT 'system',
    created_at_utc   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (status = 'posted' OR reversed_by_uuid IS NOT NULL),
    CHECK (reverses_uuid IS NULL OR status = 'posted')
);
-- Each source posts its cash receipt exactly once (no double counting when
-- a payment is entered via both the payment screen and the cash book).
CREATE UNIQUE INDEX uq_cash_receipt_source ON cash_receipts (source_table, source_id)
    WHERE source_table IS NOT NULL AND reverses_uuid IS NULL
      AND status = 'posted';
CREATE INDEX idx_cash_receipts_date ON cash_receipts (business_date);
CREATE INDEX idx_cash_receipts_customer ON cash_receipts (customer_id);

CREATE TABLE cash_payments (
    id               INTEGER PRIMARY KEY,
    uuid             TEXT NOT NULL UNIQUE,
    business_date    TEXT NOT NULL REFERENCES business_days (business_date),
    category_id      INTEGER NOT NULL REFERENCES cash_categories (id),
    amount_paisa     INTEGER NOT NULL CHECK (amount_paisa > 0),
    payee            TEXT,
    source_table     TEXT,     -- e.g. 'expenses', 'supplier_payments',
                              -- 'other_product_transactions'
    source_id        INTEGER,  -- NULL for manually entered payments
    method           TEXT NOT NULL DEFAULT 'cash' CHECK (method = 'cash'),
    remarks          TEXT,
    status           TEXT NOT NULL DEFAULT 'posted'
                     CHECK (status IN ('posted', 'reversed')),
    reverses_uuid    TEXT REFERENCES cash_payments (uuid),
    reversed_by_uuid TEXT REFERENCES cash_payments (uuid),
    occurred_at_utc  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    created_by       TEXT NOT NULL DEFAULT 'system',
    created_at_utc   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (status = 'posted' OR reversed_by_uuid IS NOT NULL),
    CHECK (reverses_uuid IS NULL OR status = 'posted')
);
CREATE UNIQUE INDEX uq_cash_payment_source ON cash_payments (source_table, source_id)
    WHERE source_table IS NOT NULL AND reverses_uuid IS NULL
      AND status = 'posted';
CREATE INDEX idx_cash_payments_date ON cash_payments (business_date);

-- ===========================================================================
-- expense_categories / expenses
-- ===========================================================================
CREATE TABLE expense_categories (
    id             INTEGER PRIMARY KEY,
    name_en        TEXT NOT NULL UNIQUE,
    name_ur        TEXT,
    is_active      INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_by     TEXT NOT NULL DEFAULT 'system',
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

INSERT INTO expense_categories (name_en, name_ur, created_by) VALUES
    ('Fuel purchase',        'ایندھن کی خرید',      'system'),
    ('Salaries',             'تنخواہیں',            'system'),
    ('Utilities',            'بجلی / پانی / گیس',   'system'),
    ('Maintenance',          'مرمت',                'system'),
    ('Stationery',           'اسٹیشنری',            'system'),
    ('Miscellaneous',        'متفرق',               'system');

CREATE TABLE expenses (
    id               INTEGER PRIMARY KEY,
    uuid             TEXT NOT NULL UNIQUE,
    business_date    TEXT NOT NULL REFERENCES business_days (business_date),
    category_id      INTEGER NOT NULL REFERENCES expense_categories (id),
    amount_paisa     INTEGER NOT NULL CHECK (amount_paisa > 0),
    method           TEXT NOT NULL CHECK (method IN ('cash', 'bank', 'other')),
    payee            TEXT,
    reference        TEXT,
    status           TEXT NOT NULL DEFAULT 'posted'
                     CHECK (status IN ('posted', 'reversed')),
    reverses_uuid    TEXT REFERENCES expenses (uuid),
    reversed_by_uuid TEXT REFERENCES expenses (uuid),
    occurred_at_utc  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    created_by       TEXT NOT NULL DEFAULT 'system',
    created_at_utc   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (status = 'posted' OR reversed_by_uuid IS NOT NULL),
    CHECK (reverses_uuid IS NULL OR status = 'posted')
);
CREATE INDEX idx_expenses_date ON expenses (business_date);
CREATE INDEX idx_expenses_category ON expenses (category_id);

-- ===========================================================================
-- supplier_payments — settlements against delivery invoices
-- ===========================================================================
-- Outstanding liability is COMPUTED (Σ invoice totals − Σ payments), never
-- typed. An unpaid invoice never reduces physical cash: only method='cash'
-- rows create a cash_payments row, via the service in the same transaction.
CREATE TABLE supplier_payments (
    id               INTEGER PRIMARY KEY,
    uuid             TEXT NOT NULL UNIQUE,
    supplier_id      INTEGER NOT NULL REFERENCES suppliers (id),
    delivery_id      INTEGER REFERENCES fuel_deliveries (id),
    business_date    TEXT NOT NULL REFERENCES business_days (business_date),
    amount_paisa     INTEGER NOT NULL CHECK (amount_paisa > 0),
    method           TEXT NOT NULL CHECK (method IN ('cash', 'bank', 'other')),
    status           TEXT NOT NULL DEFAULT 'posted'
                     CHECK (status IN ('posted', 'reversed')),
    reverses_uuid    TEXT REFERENCES supplier_payments (uuid),
    reversed_by_uuid TEXT REFERENCES supplier_payments (uuid),
    occurred_at_utc  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    created_by       TEXT NOT NULL DEFAULT 'system',
    created_at_utc   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (status = 'posted' OR reversed_by_uuid IS NOT NULL),
    CHECK (reverses_uuid IS NULL OR status = 'posted')
);
CREATE INDEX idx_supplier_payments_supplier ON supplier_payments (supplier_id);
CREATE INDEX idx_supplier_payments_delivery ON supplier_payments (delivery_id);
CREATE INDEX idx_supplier_payments_date ON supplier_payments (business_date);

-- ===========================================================================
-- other_products / other_product_transactions — engine oil, lubricants, …
-- ===========================================================================
CREATE TABLE other_products (
    id                  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    unit                TEXT NOT NULL DEFAULT 'piece',
    opening_value_paisa INTEGER NOT NULL DEFAULT 0 CHECK (opening_value_paisa >= 0),
    is_active           INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_by          TEXT NOT NULL DEFAULT 'system',
    created_at_utc      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at_utc      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (name)
);

-- quantity_units is INTEGER smallest units (bottles, litres…); signed only
-- for 'adjustment'. total_paisa = quantity × unit price (unsigned for
-- purchase/sale). Cash purchases/sales post exactly one cash row each.
CREATE TABLE other_product_transactions (
    id               INTEGER PRIMARY KEY,
    uuid             TEXT NOT NULL UNIQUE,
    business_date    TEXT NOT NULL REFERENCES business_days (business_date),
    product_id       INTEGER NOT NULL REFERENCES other_products (id),
    txn_type         TEXT NOT NULL CHECK (txn_type IN ('purchase', 'sale', 'adjustment')),
    quantity_units   INTEGER NOT NULL CHECK (quantity_units <> 0),
    unit_price_paisa INTEGER NOT NULL CHECK (unit_price_paisa >= 0),
    total_paisa      INTEGER NOT NULL CHECK (total_paisa >= 0),
    method           TEXT NOT NULL DEFAULT 'cash' CHECK (method IN ('cash', 'bank', 'other')),
    reference        TEXT,
    occurred_at_utc  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    created_by       TEXT NOT NULL DEFAULT 'system',
    created_at_utc   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (txn_type = 'adjustment' OR quantity_units > 0)
);
CREATE UNIQUE INDEX uq_other_txn_source ON other_product_transactions (uuid);
CREATE INDEX idx_other_txn_product_date ON other_product_transactions (product_id, business_date);

-- ===========================================================================
-- VIEWS
-- ===========================================================================

-- Settlement legs net of reversals. A reversed original contributes 0 (the
-- flip is only a marker — the row is never erased); the reversal row
-- negates it. Mirrors the Phase-4 §11.1 fixed rule.
CREATE VIEW v_effective_settlements AS
SELECT
    s.id,
    s.uuid,
    s.business_date,
    s.fuel_product_id,
    p.code AS fuel_product_code,
    s.category,
    s.batch_uuid,
    s.source_table,
    s.source_id,
    CASE
        WHEN s.is_reversal = 1 THEN -s.amount_paisa
        WHEN s.status = 'reversed' THEN 0
        ELSE s.amount_paisa
    END AS effective_paisa,
    s.status,
    s.is_reversal,
    s.notes,
    s.occurred_at_utc,
    s.created_by
FROM sales_settlements s
JOIN fuel_products p ON p.id = s.fuel_product_id;

-- The chronological cash book: receipts positive, payments negative, both
-- net of reversals.
CREATE VIEW v_cashbook AS
SELECT
    r.business_date,
    r.occurred_at_utc,
    'receipt' AS kind,
    c.name AS category,
    c.name_en AS category_en,
    r.customer_id,
    NULL AS payee,
    CASE
        WHEN r.reverses_uuid IS NOT NULL THEN -r.amount_paisa
        WHEN r.status = 'reversed' THEN 0
        ELSE r.amount_paisa
    END AS signed_paisa,
    r.remarks AS remarks,
    r.uuid,
    r.status
FROM cash_receipts r
JOIN cash_categories c ON c.id = r.category_id
UNION ALL
SELECT
    p.business_date,
    p.occurred_at_utc,
    'payment' AS kind,
    c.name AS category,
    c.name_en AS category_en,
    NULL AS customer_id,
    p.payee AS payee,
    -CASE
        WHEN p.reverses_uuid IS NOT NULL THEN -p.amount_paisa
        WHEN p.status = 'reversed' THEN 0
        ELSE p.amount_paisa
    END AS signed_paisa,
    p.remarks AS remarks,
    p.uuid,
    p.status
FROM cash_payments p
JOIN cash_categories c ON c.id = p.category_id;

-- Supplier balances: invoiced − paid at delivery − later payments, computed.
CREATE VIEW v_supplier_balances AS
SELECT
    s.id AS supplier_id,
    s.name AS supplier_name,
    COALESCE((
        SELECT SUM(d.total_cost_paisa + COALESCE(d.other_cost_paisa, 0))
        FROM fuel_deliveries d WHERE d.supplier_id = s.id
    ), 0) AS invoiced_paisa,
    COALESCE((
        SELECT SUM(COALESCE(d.amount_paid_paisa, 0))
        FROM fuel_deliveries d WHERE d.supplier_id = s.id
    ), 0) AS paid_at_delivery_paisa,
    COALESCE((
        SELECT SUM(CASE
                WHEN sp.reverses_uuid IS NOT NULL THEN -sp.amount_paisa
                WHEN sp.status = 'reversed' THEN 0
                ELSE sp.amount_paisa
            END)
        FROM supplier_payments sp WHERE sp.supplier_id = s.id
    ), 0) AS paid_paisa,
    COALESCE((
        SELECT SUM(d.total_cost_paisa + COALESCE(d.other_cost_paisa, 0)
                   - COALESCE(d.amount_paid_paisa, 0))
        FROM fuel_deliveries d WHERE d.supplier_id = s.id
    ), 0)
    - COALESCE((
        SELECT SUM(CASE
                WHEN sp.reverses_uuid IS NOT NULL THEN -sp.amount_paisa
                WHEN sp.status = 'reversed' THEN 0
                ELSE sp.amount_paisa
            END)
        FROM supplier_payments sp WHERE sp.supplier_id = s.id
    ), 0) AS outstanding_paisa
FROM suppliers s;

-- On-hand units per other product: purchases − sales ± adjustments.
CREATE VIEW v_other_product_stock AS
SELECT
    p.id AS product_id,
    p.name AS product_name,
    p.unit AS unit,
    COALESCE(SUM(CASE
        WHEN t.txn_type = 'purchase' THEN t.quantity_units
        WHEN t.txn_type = 'sale' THEN -t.quantity_units
        ELSE t.quantity_units
    END), 0) AS on_hand_units
FROM other_products p
LEFT JOIN other_product_transactions t ON t.product_id = p.id
GROUP BY p.id;

-- Daily cash position: opening + receipts − payments = expected; the
-- physical count and the never-auto-written-off difference come along.
CREATE VIEW v_daily_cash_position AS
SELECT
    b.business_date,
    b.opening_cash_paisa,
    COALESCE((
        SELECT SUM(CASE
                WHEN r.reverses_uuid IS NOT NULL THEN -r.amount_paisa
                WHEN r.status = 'reversed' THEN 0
                ELSE r.amount_paisa
            END)
        FROM cash_receipts r WHERE r.business_date = b.business_date
    ), 0) AS receipts_paisa,
    COALESCE((
        SELECT SUM(CASE
                WHEN p.reverses_uuid IS NOT NULL THEN -p.amount_paisa
                WHEN p.status = 'reversed' THEN 0
                ELSE p.amount_paisa
            END)
        FROM cash_payments p WHERE p.business_date = b.business_date
    ), 0) AS payments_paisa,
    b.opening_cash_paisa
    + COALESCE((
        SELECT SUM(CASE
                WHEN r.reverses_uuid IS NOT NULL THEN -r.amount_paisa
                WHEN r.status = 'reversed' THEN 0
                ELSE r.amount_paisa
            END)
        FROM cash_receipts r WHERE r.business_date = b.business_date
    ), 0)
    - COALESCE((
        SELECT SUM(CASE
                WHEN p.reverses_uuid IS NOT NULL THEN -p.amount_paisa
                WHEN p.status = 'reversed' THEN 0
                ELSE p.amount_paisa
            END)
        FROM cash_payments p WHERE p.business_date = b.business_date
    ), 0) AS expected_paisa,
    b.physical_cash_paisa,
    CASE WHEN b.physical_cash_paisa IS NOT NULL
         THEN b.physical_cash_paisa - (
            b.opening_cash_paisa
            + COALESCE((
                SELECT SUM(CASE
                        WHEN r.reverses_uuid IS NOT NULL THEN -r.amount_paisa
                        WHEN r.status = 'reversed' THEN 0
                        ELSE r.amount_paisa
                    END)
                FROM cash_receipts r WHERE r.business_date = b.business_date
            ), 0)
            - COALESCE((
                SELECT SUM(CASE
                        WHEN p.reverses_uuid IS NOT NULL THEN -p.amount_paisa
                        WHEN p.status = 'reversed' THEN 0
                        ELSE p.amount_paisa
                    END)
                FROM cash_payments p WHERE p.business_date = b.business_date
            ), 0))
         ELSE NULL
    END AS difference_paisa
FROM business_days b;

-- ===========================================================================
-- TRIGGERS — Group A: automatic timestamps
-- ===========================================================================

-- A1: created_at_utc is immutable (one trigger per table).
CREATE TRIGGER trg_cash_categories_bu_guard_created
BEFORE UPDATE ON cash_categories
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_sales_settlements_bu_guard_created
BEFORE UPDATE ON sales_settlements
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_cash_receipts_bu_guard_created
BEFORE UPDATE ON cash_receipts
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_cash_payments_bu_guard_created
BEFORE UPDATE ON cash_payments
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_expense_categories_bu_guard_created
BEFORE UPDATE ON expense_categories
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_expenses_bu_guard_created
BEFORE UPDATE ON expenses
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_supplier_payments_bu_guard_created
BEFORE UPDATE ON supplier_payments
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_other_products_bu_guard_created
BEFORE UPDATE ON other_products
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_other_product_transactions_bu_guard_created
BEFORE UPDATE ON other_product_transactions
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

-- A2: updated_at_utc maintained on mutable master tables (business-column
-- guard so the timestamp touch writes no audit row and never recurses).
CREATE TRIGGER trg_cash_categories_bu_touch_updated
BEFORE UPDATE ON cash_categories
FOR EACH ROW
WHEN (OLD.kind IS NOT NEW.kind
      OR OLD.name IS NOT NEW.name
      OR OLD.name_en IS NOT NEW.name_en
      OR OLD.name_ur IS NOT NEW.name_ur
      OR OLD.is_active IS NOT NEW.is_active
      OR OLD.created_by IS NOT NEW.created_by)
BEGIN
    UPDATE cash_categories
    SET updated_at_utc = strftime('%Y-%m-%dT%H:%M:%fZ','now')
    WHERE id = NEW.id;
END;

CREATE TRIGGER trg_expense_categories_bu_touch_updated
BEFORE UPDATE ON expense_categories
FOR EACH ROW
WHEN (OLD.name_en IS NOT NEW.name_en
      OR OLD.name_ur IS NOT NEW.name_ur
      OR OLD.is_active IS NOT NEW.is_active
      OR OLD.created_by IS NOT NEW.created_by)
BEGIN
    UPDATE expense_categories
    SET updated_at_utc = strftime('%Y-%m-%dT%H:%M:%fZ','now')
    WHERE id = NEW.id;
END;

CREATE TRIGGER trg_other_products_bu_touch_updated
BEFORE UPDATE ON other_products
FOR EACH ROW
WHEN (OLD.name IS NOT NEW.name
      OR OLD.unit IS NOT NEW.unit
      OR OLD.opening_value_paisa IS NOT NEW.opening_value_paisa
      OR OLD.is_active IS NOT NEW.is_active
      OR OLD.created_by IS NOT NEW.created_by)
BEGIN
    UPDATE other_products
    SET updated_at_utc = strftime('%Y-%m-%dT%H:%M:%fZ','now')
    WHERE id = NEW.id;
END;

-- ===========================================================================
-- TRIGGERS — Group B: audit history
-- ===========================================================================

CREATE TRIGGER trg_cash_categories_ai_audit
AFTER INSERT ON cash_categories
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('cash_categories', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'kind', NEW.kind,
                        'name', NEW.name,
                        'name_en', NEW.name_en,
                        'is_active', NEW.is_active,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc),
            NULL, NEW.created_by);
END;

CREATE TRIGGER trg_cash_categories_au_audit
AFTER UPDATE ON cash_categories
FOR EACH ROW
WHEN (OLD.kind IS NOT NEW.kind
      OR OLD.name IS NOT NEW.name
      OR OLD.name_en IS NOT NEW.name_en
      OR OLD.name_ur IS NOT NEW.name_ur
      OR OLD.is_active IS NOT NEW.is_active)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('cash_categories', NEW.id, 'UPDATE',
            json_object('kind', OLD.kind, 'name', OLD.name,
                        'name_en', OLD.name_en, 'is_active', OLD.is_active),
            json_object('kind', NEW.kind, 'name', NEW.name,
                        'name_en', NEW.name_en, 'is_active', NEW.is_active),
            NULL, NEW.created_by);
END;

CREATE TRIGGER trg_cash_categories_bd_audit
BEFORE DELETE ON cash_categories
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('cash_categories', OLD.id, 'DELETE',
            json_object('id', OLD.id, 'kind', OLD.kind, 'name', OLD.name,
                        'name_en', OLD.name_en),
            NULL, NULL, OLD.created_by);
END;

CREATE TRIGGER trg_sales_settlements_ai_audit
AFTER INSERT ON sales_settlements
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('sales_settlements', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'uuid', NEW.uuid,
                        'business_date', NEW.business_date,
                        'fuel_product_id', NEW.fuel_product_id,
                        'category', NEW.category,
                        'amount_paisa', NEW.amount_paisa,
                        'source_table', NEW.source_table,
                        'source_id', NEW.source_id,
                        'batch_uuid', NEW.batch_uuid,
                        'is_reversal', NEW.is_reversal,
                        'status', NEW.status,
                        'occurred_at_utc', NEW.occurred_at_utc,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc,
                        'notes', NEW.notes),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_sales_settlements_au_audit
AFTER UPDATE ON sales_settlements
FOR EACH ROW
WHEN (OLD.status IS NOT NEW.status
      OR OLD.reversed_by_batch_uuid IS NOT NEW.reversed_by_batch_uuid)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('sales_settlements', NEW.id, 'UPDATE',
            json_object('status', OLD.status,
                        'reversed_by_batch_uuid', OLD.reversed_by_batch_uuid),
            json_object('status', NEW.status,
                        'reversed_by_batch_uuid', NEW.reversed_by_batch_uuid),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_sales_settlements_bd_audit
BEFORE DELETE ON sales_settlements
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('sales_settlements', OLD.id, 'DELETE',
            json_object('id', OLD.id, 'uuid', OLD.uuid,
                        'business_date', OLD.business_date,
                        'fuel_product_id', OLD.fuel_product_id,
                        'category', OLD.category,
                        'amount_paisa', OLD.amount_paisa),
            NULL, OLD.business_date, OLD.created_by);
END;

CREATE TRIGGER trg_cash_receipts_ai_audit
AFTER INSERT ON cash_receipts
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('cash_receipts', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'uuid', NEW.uuid,
                        'business_date', NEW.business_date,
                        'category_id', NEW.category_id,
                        'amount_paisa', NEW.amount_paisa,
                        'source_table', NEW.source_table,
                        'source_id', NEW.source_id,
                        'customer_id', NEW.customer_id,
                        'status', NEW.status,
                        'reverses_uuid', NEW.reverses_uuid,
                        'occurred_at_utc', NEW.occurred_at_utc,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc,
                        'remarks', NEW.remarks),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_cash_receipts_au_audit
AFTER UPDATE ON cash_receipts
FOR EACH ROW
WHEN (OLD.status IS NOT NEW.status
      OR OLD.reversed_by_uuid IS NOT NEW.reversed_by_uuid)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('cash_receipts', NEW.id, 'UPDATE',
            json_object('status', OLD.status,
                        'reversed_by_uuid', OLD.reversed_by_uuid),
            json_object('status', NEW.status,
                        'reversed_by_uuid', NEW.reversed_by_uuid),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_cash_receipts_bd_audit
BEFORE DELETE ON cash_receipts
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('cash_receipts', OLD.id, 'DELETE',
            json_object('id', OLD.id, 'uuid', OLD.uuid,
                        'business_date', OLD.business_date,
                        'amount_paisa', OLD.amount_paisa),
            NULL, OLD.business_date, OLD.created_by);
END;

CREATE TRIGGER trg_cash_payments_ai_audit
AFTER INSERT ON cash_payments
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('cash_payments', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'uuid', NEW.uuid,
                        'business_date', NEW.business_date,
                        'category_id', NEW.category_id,
                        'amount_paisa', NEW.amount_paisa,
                        'payee', NEW.payee,
                        'source_table', NEW.source_table,
                        'source_id', NEW.source_id,
                        'status', NEW.status,
                        'reverses_uuid', NEW.reverses_uuid,
                        'occurred_at_utc', NEW.occurred_at_utc,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc,
                        'remarks', NEW.remarks),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_cash_payments_au_audit
AFTER UPDATE ON cash_payments
FOR EACH ROW
WHEN (OLD.status IS NOT NEW.status
      OR OLD.reversed_by_uuid IS NOT NEW.reversed_by_uuid)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('cash_payments', NEW.id, 'UPDATE',
            json_object('status', OLD.status,
                        'reversed_by_uuid', OLD.reversed_by_uuid),
            json_object('status', NEW.status,
                        'reversed_by_uuid', NEW.reversed_by_uuid),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_cash_payments_bd_audit
BEFORE DELETE ON cash_payments
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('cash_payments', OLD.id, 'DELETE',
            json_object('id', OLD.id, 'uuid', OLD.uuid,
                        'business_date', OLD.business_date,
                        'amount_paisa', OLD.amount_paisa),
            NULL, OLD.business_date, OLD.created_by);
END;

CREATE TRIGGER trg_expense_categories_ai_audit
AFTER INSERT ON expense_categories
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('expense_categories', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'name_en', NEW.name_en,
                        'is_active', NEW.is_active,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc),
            NULL, NEW.created_by);
END;

CREATE TRIGGER trg_expense_categories_au_audit
AFTER UPDATE ON expense_categories
FOR EACH ROW
WHEN (OLD.name_en IS NOT NEW.name_en
      OR OLD.name_ur IS NOT NEW.name_ur
      OR OLD.is_active IS NOT NEW.is_active)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('expense_categories', NEW.id, 'UPDATE',
            json_object('name_en', OLD.name_en, 'is_active', OLD.is_active),
            json_object('name_en', NEW.name_en, 'is_active', NEW.is_active),
            NULL, NEW.created_by);
END;

CREATE TRIGGER trg_expense_categories_bd_audit
BEFORE DELETE ON expense_categories
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('expense_categories', OLD.id, 'DELETE',
            json_object('id', OLD.id, 'name_en', OLD.name_en),
            NULL, NULL, OLD.created_by);
END;

CREATE TRIGGER trg_expenses_ai_audit
AFTER INSERT ON expenses
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('expenses', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'uuid', NEW.uuid,
                        'business_date', NEW.business_date,
                        'category_id', NEW.category_id,
                        'amount_paisa', NEW.amount_paisa,
                        'method', NEW.method,
                        'payee', NEW.payee,
                        'reference', NEW.reference,
                        'status', NEW.status,
                        'reverses_uuid', NEW.reverses_uuid,
                        'occurred_at_utc', NEW.occurred_at_utc,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_expenses_au_audit
AFTER UPDATE ON expenses
FOR EACH ROW
WHEN (OLD.status IS NOT NEW.status
      OR OLD.reversed_by_uuid IS NOT NEW.reversed_by_uuid)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('expenses', NEW.id, 'UPDATE',
            json_object('status', OLD.status,
                        'reversed_by_uuid', OLD.reversed_by_uuid),
            json_object('status', NEW.status,
                        'reversed_by_uuid', NEW.reversed_by_uuid),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_expenses_bd_audit
BEFORE DELETE ON expenses
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('expenses', OLD.id, 'DELETE',
            json_object('id', OLD.id, 'uuid', OLD.uuid,
                        'business_date', OLD.business_date,
                        'amount_paisa', OLD.amount_paisa),
            NULL, OLD.business_date, OLD.created_by);
END;

CREATE TRIGGER trg_supplier_payments_ai_audit
AFTER INSERT ON supplier_payments
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('supplier_payments', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'uuid', NEW.uuid,
                        'supplier_id', NEW.supplier_id,
                        'delivery_id', NEW.delivery_id,
                        'business_date', NEW.business_date,
                        'amount_paisa', NEW.amount_paisa,
                        'method', NEW.method,
                        'status', NEW.status,
                        'reverses_uuid', NEW.reverses_uuid,
                        'occurred_at_utc', NEW.occurred_at_utc,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_supplier_payments_au_audit
AFTER UPDATE ON supplier_payments
FOR EACH ROW
WHEN (OLD.status IS NOT NEW.status
      OR OLD.reversed_by_uuid IS NOT NEW.reversed_by_uuid)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('supplier_payments', NEW.id, 'UPDATE',
            json_object('status', OLD.status,
                        'reversed_by_uuid', OLD.reversed_by_uuid),
            json_object('status', NEW.status,
                        'reversed_by_uuid', NEW.reversed_by_uuid),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_supplier_payments_bd_audit
BEFORE DELETE ON supplier_payments
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('supplier_payments', OLD.id, 'DELETE',
            json_object('id', OLD.id, 'uuid', OLD.uuid,
                        'supplier_id', OLD.supplier_id,
                        'business_date', OLD.business_date,
                        'amount_paisa', OLD.amount_paisa),
            NULL, OLD.business_date, OLD.created_by);
END;

CREATE TRIGGER trg_other_products_ai_audit
AFTER INSERT ON other_products
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('other_products', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'name', NEW.name,
                        'unit', NEW.unit,
                        'opening_value_paisa', NEW.opening_value_paisa,
                        'is_active', NEW.is_active,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc),
            NULL, NEW.created_by);
END;

CREATE TRIGGER trg_other_products_au_audit
AFTER UPDATE ON other_products
FOR EACH ROW
WHEN (OLD.name IS NOT NEW.name
      OR OLD.unit IS NOT NEW.unit
      OR OLD.opening_value_paisa IS NOT NEW.opening_value_paisa
      OR OLD.is_active IS NOT NEW.is_active)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('other_products', NEW.id, 'UPDATE',
            json_object('name', OLD.name, 'is_active', OLD.is_active),
            json_object('name', NEW.name, 'is_active', NEW.is_active),
            NULL, NEW.created_by);
END;

CREATE TRIGGER trg_other_products_bd_audit
BEFORE DELETE ON other_products
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('other_products', OLD.id, 'DELETE',
            json_object('id', OLD.id, 'name', OLD.name),
            NULL, NULL, OLD.created_by);
END;

CREATE TRIGGER trg_other_product_transactions_ai_audit
AFTER INSERT ON other_product_transactions
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('other_product_transactions', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'uuid', NEW.uuid,
                        'business_date', NEW.business_date,
                        'product_id', NEW.product_id,
                        'txn_type', NEW.txn_type,
                        'quantity_units', NEW.quantity_units,
                        'unit_price_paisa', NEW.unit_price_paisa,
                        'total_paisa', NEW.total_paisa,
                        'method', NEW.method,
                        'occurred_at_utc', NEW.occurred_at_utc,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_other_product_transactions_au_audit
AFTER UPDATE ON other_product_transactions
FOR EACH ROW
WHEN (OLD.quantity_units IS NOT NEW.quantity_units
      OR OLD.unit_price_paisa IS NOT NEW.unit_price_paisa
      OR OLD.total_paisa IS NOT NEW.total_paisa
      OR OLD.txn_type IS NOT NEW.txn_type)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('other_product_transactions', NEW.id, 'UPDATE',
            json_object('quantity_units', OLD.quantity_units,
                        'total_paisa', OLD.total_paisa),
            json_object('quantity_units', NEW.quantity_units,
                        'total_paisa', NEW.total_paisa),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_other_product_transactions_bd_audit
BEFORE DELETE ON other_product_transactions
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('other_product_transactions', OLD.id, 'DELETE',
            json_object('id', OLD.id, 'uuid', OLD.uuid,
                        'business_date', OLD.business_date,
                        'product_id', OLD.product_id,
                        'txn_type', OLD.txn_type,
                        'total_paisa', OLD.total_paisa),
            NULL, OLD.business_date, OLD.created_by);
END;

-- ===========================================================================
-- TRIGGERS — Group C: posted-ledger immutability
-- ===========================================================================

-- sales_settlements: only the posted → reversed flip is allowed, and only
-- when the reversal batch is named (all business columns must be unchanged).
CREATE TRIGGER trg_sales_settlements_bu_no_update
BEFORE UPDATE ON sales_settlements
FOR EACH ROW
WHEN NOT (OLD.status = 'posted' AND NEW.status = 'reversed'
          AND OLD.reversed_by_batch_uuid IS NULL
          AND NEW.reversed_by_batch_uuid IS NOT NULL
          AND NEW.id IS OLD.id
          AND NEW.uuid IS OLD.uuid
          AND NEW.business_date IS OLD.business_date
          AND NEW.fuel_product_id IS OLD.fuel_product_id
          AND NEW.category IS OLD.category
          AND NEW.amount_paisa IS OLD.amount_paisa
          AND NEW.source_table IS OLD.source_table
          AND NEW.source_id IS OLD.source_id
          AND NEW.batch_uuid IS OLD.batch_uuid
          AND NEW.is_reversal IS OLD.is_reversal
          AND NEW.reverses_settlement_id IS OLD.reverses_settlement_id
          AND NEW.notes IS OLD.notes
          AND NEW.occurred_at_utc IS OLD.occurred_at_utc
          AND NEW.created_by IS OLD.created_by
          AND NEW.created_at_utc IS OLD.created_at_utc)
BEGIN
    SELECT RAISE(ABORT, 'sales_settlements is immutable; reverse the batch with a reversal batch instead');
END;

CREATE TRIGGER trg_sales_settlements_bd_no_delete
BEFORE DELETE ON sales_settlements
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'sales_settlements is immutable; rows cannot be deleted');
END;

-- cash_receipts / cash_payments: reversal rows only; the flip is whitelisted.
CREATE TRIGGER trg_cash_receipts_bu_no_update
BEFORE UPDATE ON cash_receipts
FOR EACH ROW
WHEN NOT (OLD.status = 'posted' AND NEW.status = 'reversed'
          AND OLD.reversed_by_uuid IS NULL
          AND NEW.reversed_by_uuid IS NOT NULL
          AND NEW.id IS OLD.id
          AND NEW.uuid IS OLD.uuid
          AND NEW.business_date IS OLD.business_date
          AND NEW.category_id IS OLD.category_id
          AND NEW.amount_paisa IS OLD.amount_paisa
          AND NEW.source_table IS OLD.source_table
          AND NEW.source_id IS OLD.source_id
          AND NEW.customer_id IS OLD.customer_id
          AND NEW.method IS OLD.method
          AND NEW.remarks IS OLD.remarks
          AND NEW.reverses_uuid IS OLD.reverses_uuid
          AND NEW.occurred_at_utc IS OLD.occurred_at_utc
          AND NEW.created_by IS OLD.created_by
          AND NEW.created_at_utc IS OLD.created_at_utc)
BEGIN
    SELECT RAISE(ABORT, 'cash_receipts is immutable; reverse the receipt with a reversal row instead');
END;

CREATE TRIGGER trg_cash_receipts_bd_no_delete
BEFORE DELETE ON cash_receipts
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'cash_receipts is immutable; rows cannot be deleted');
END;

CREATE TRIGGER trg_cash_payments_bu_no_update
BEFORE UPDATE ON cash_payments
FOR EACH ROW
WHEN NOT (OLD.status = 'posted' AND NEW.status = 'reversed'
          AND OLD.reversed_by_uuid IS NULL
          AND NEW.reversed_by_uuid IS NOT NULL
          AND NEW.id IS OLD.id
          AND NEW.uuid IS OLD.uuid
          AND NEW.business_date IS OLD.business_date
          AND NEW.category_id IS OLD.category_id
          AND NEW.amount_paisa IS OLD.amount_paisa
          AND NEW.payee IS OLD.payee
          AND NEW.source_table IS OLD.source_table
          AND NEW.source_id IS OLD.source_id
          AND NEW.method IS OLD.method
          AND NEW.remarks IS OLD.remarks
          AND NEW.reverses_uuid IS OLD.reverses_uuid
          AND NEW.occurred_at_utc IS OLD.occurred_at_utc
          AND NEW.created_by IS OLD.created_by
          AND NEW.created_at_utc IS OLD.created_at_utc)
BEGIN
    SELECT RAISE(ABORT, 'cash_payments is immutable; reverse the payment with a reversal row instead');
END;

CREATE TRIGGER trg_cash_payments_bd_no_delete
BEFORE DELETE ON cash_payments
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'cash_payments is immutable; rows cannot be deleted');
END;

-- expenses / supplier_payments: fully immutable; corrections are reversal rows.
CREATE TRIGGER trg_expenses_bu_no_update
BEFORE UPDATE ON expenses
FOR EACH ROW
WHEN NOT (OLD.status = 'posted' AND NEW.status = 'reversed'
          AND OLD.reversed_by_uuid IS NULL
          AND NEW.reversed_by_uuid IS NOT NULL
          AND NEW.id IS OLD.id
          AND NEW.uuid IS OLD.uuid
          AND NEW.business_date IS OLD.business_date
          AND NEW.category_id IS OLD.category_id
          AND NEW.amount_paisa IS OLD.amount_paisa
          AND NEW.method IS OLD.method
          AND NEW.payee IS OLD.payee
          AND NEW.reference IS OLD.reference
          AND NEW.reverses_uuid IS OLD.reverses_uuid
          AND NEW.occurred_at_utc IS OLD.occurred_at_utc
          AND NEW.created_by IS OLD.created_by
          AND NEW.created_at_utc IS OLD.created_at_utc)
BEGIN
    SELECT RAISE(ABORT, 'expenses is immutable; reverse the expense with a reversal row instead');
END;

CREATE TRIGGER trg_expenses_bd_no_delete
BEFORE DELETE ON expenses
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'expenses is immutable; rows cannot be deleted');
END;

CREATE TRIGGER trg_supplier_payments_bu_no_update
BEFORE UPDATE ON supplier_payments
FOR EACH ROW
WHEN NOT (OLD.status = 'posted' AND NEW.status = 'reversed'
          AND OLD.reversed_by_uuid IS NULL
          AND NEW.reversed_by_uuid IS NOT NULL
          AND NEW.id IS OLD.id
          AND NEW.uuid IS OLD.uuid
          AND NEW.supplier_id IS OLD.supplier_id
          AND NEW.delivery_id IS OLD.delivery_id
          AND NEW.business_date IS OLD.business_date
          AND NEW.amount_paisa IS OLD.amount_paisa
          AND NEW.method IS OLD.method
          AND NEW.reverses_uuid IS OLD.reverses_uuid
          AND NEW.occurred_at_utc IS OLD.occurred_at_utc
          AND NEW.created_by IS OLD.created_by
          AND NEW.created_at_utc IS OLD.created_at_utc)
BEGIN
    SELECT RAISE(ABORT, 'supplier_payments is immutable; reverse the payment with a reversal row instead');
END;

CREATE TRIGGER trg_supplier_payments_bd_no_delete
BEFORE DELETE ON supplier_payments
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'supplier_payments is immutable; rows cannot be deleted');
END;

-- other_product_transactions: fully immutable; corrections are 'adjustment' rows.
CREATE TRIGGER trg_other_product_transactions_bu_no_update
BEFORE UPDATE ON other_product_transactions
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'other_product_transactions is immutable; post an adjustment transaction instead');
END;

CREATE TRIGGER trg_other_product_transactions_bd_no_delete
BEFORE DELETE ON other_product_transactions
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'other_product_transactions is immutable; rows cannot be deleted');
END;

-- ===========================================================================
-- TRIGGERS — Group D: closed business day
-- ===========================================================================

CREATE TRIGGER trg_sales_settlements_bi_closed_day
BEFORE INSERT ON sales_settlements
FOR EACH ROW
WHEN (SELECT status FROM business_days
      WHERE business_date = NEW.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;

CREATE TRIGGER trg_cash_receipts_bi_closed_day
BEFORE INSERT ON cash_receipts
FOR EACH ROW
WHEN (SELECT status FROM business_days
      WHERE business_date = NEW.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;

CREATE TRIGGER trg_cash_payments_bi_closed_day
BEFORE INSERT ON cash_payments
FOR EACH ROW
WHEN (SELECT status FROM business_days
      WHERE business_date = NEW.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;

CREATE TRIGGER trg_expenses_bi_closed_day
BEFORE INSERT ON expenses
FOR EACH ROW
WHEN (SELECT status FROM business_days
      WHERE business_date = NEW.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;

CREATE TRIGGER trg_supplier_payments_bi_closed_day
BEFORE INSERT ON supplier_payments
FOR EACH ROW
WHEN (SELECT status FROM business_days
      WHERE business_date = NEW.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;

CREATE TRIGGER trg_other_product_transactions_bi_closed_day
BEFORE INSERT ON other_product_transactions
FOR EACH ROW
WHEN (SELECT status FROM business_days
      WHERE business_date = NEW.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;

-- ===========================================================================
-- TRIGGERS — Group I: supplier-payment allocation guard
-- ===========================================================================

-- A delivery-linked payment must reference the delivery's own supplier and
-- must not exceed that invoice's unpaid amount
-- (total + other costs − paid at delivery − earlier effective payments).
-- Reversal rows carry negative effective amounts and are exempt.
CREATE TRIGGER trg_supplier_payments_bi_check
BEFORE INSERT ON supplier_payments
FOR EACH ROW
WHEN NEW.reverses_uuid IS NULL
BEGIN
    SELECT RAISE(ABORT, 'supplier is unknown or inactive')
    WHERE NOT EXISTS (SELECT 1 FROM suppliers
                      WHERE id = NEW.supplier_id AND is_active = 1);
    SELECT RAISE(ABORT, 'supplier payment references a delivery of a different supplier')
    WHERE NEW.delivery_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM fuel_deliveries d
                      WHERE d.id = NEW.delivery_id
                        AND d.supplier_id = NEW.supplier_id);
    SELECT RAISE(ABORT, 'supplier payment exceeds the delivery outstanding amount')
    WHERE NEW.delivery_id IS NOT NULL
      AND NEW.amount_paisa > (
          SELECT (d.total_cost_paisa + COALESCE(d.other_cost_paisa, 0))
                 - COALESCE(d.amount_paid_paisa, 0)
                 - COALESCE((
                       SELECT SUM(CASE
                               WHEN sp.reverses_uuid IS NOT NULL THEN -sp.amount_paisa
                               WHEN sp.status = 'reversed' THEN 0
                               ELSE sp.amount_paisa
                           END)
                       FROM supplier_payments sp
                       WHERE sp.delivery_id = NEW.delivery_id), 0)
          FROM fuel_deliveries d
          WHERE d.id = NEW.delivery_id);
END;

-- A reversal row must mirror its original (same supplier, delivery, amount,
-- method and business date); the original must still be posted.
CREATE TRIGGER trg_supplier_payments_bi_reversal_check
BEFORE INSERT ON supplier_payments
FOR EACH ROW
WHEN NEW.reverses_uuid IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'supplier payment reversal references a missing or already-reversed payment')
    WHERE NOT EXISTS (SELECT 1 FROM supplier_payments o
                      WHERE o.uuid = NEW.reverses_uuid
                        AND o.status = 'posted'
                        AND o.reverses_uuid IS NULL
                        AND o.reversed_by_uuid IS NULL);
    SELECT RAISE(ABORT, 'supplier payment reversal must match the original payment')
    WHERE NOT EXISTS (SELECT 1 FROM supplier_payments o
                      WHERE o.uuid = NEW.reverses_uuid
                        AND o.supplier_id IS NEW.supplier_id
                        AND o.delivery_id IS NEW.delivery_id
                        AND o.amount_paisa IS NEW.amount_paisa
                        AND o.method IS NEW.method
                        AND o.business_date IS NEW.business_date);
END;

-- Cash/expense/payment reversal rows must mirror their originals.
CREATE TRIGGER trg_cash_receipts_bi_reversal_check
BEFORE INSERT ON cash_receipts
FOR EACH ROW
WHEN NEW.reverses_uuid IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'cash receipt reversal references a missing or already-reversed receipt')
    WHERE NOT EXISTS (SELECT 1 FROM cash_receipts o
                      WHERE o.uuid = NEW.reverses_uuid
                        AND o.status = 'posted'
                        AND o.reverses_uuid IS NULL
                        AND o.reversed_by_uuid IS NULL);
    SELECT RAISE(ABORT, 'cash receipt reversal must match the original receipt')
    WHERE NOT EXISTS (SELECT 1 FROM cash_receipts o
                      WHERE o.uuid = NEW.reverses_uuid
                        AND o.category_id IS NEW.category_id
                        AND o.amount_paisa IS NEW.amount_paisa
                        AND o.business_date IS NEW.business_date);
END;

CREATE TRIGGER trg_cash_payments_bi_reversal_check
BEFORE INSERT ON cash_payments
FOR EACH ROW
WHEN NEW.reverses_uuid IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'cash payment reversal references a missing or already-reversed payment')
    WHERE NOT EXISTS (SELECT 1 FROM cash_payments o
                      WHERE o.uuid = NEW.reverses_uuid
                        AND o.status = 'posted'
                        AND o.reverses_uuid IS NULL
                        AND o.reversed_by_uuid IS NULL);
    SELECT RAISE(ABORT, 'cash payment reversal must match the original payment')
    WHERE NOT EXISTS (SELECT 1 FROM cash_payments o
                      WHERE o.uuid = NEW.reverses_uuid
                        AND o.category_id IS NEW.category_id
                        AND o.amount_paisa IS NEW.amount_paisa
                        AND o.business_date IS NEW.business_date);
END;

CREATE TRIGGER trg_expenses_bi_reversal_check
BEFORE INSERT ON expenses
FOR EACH ROW
WHEN NEW.reverses_uuid IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'expense reversal references a missing or already-reversed expense')
    WHERE NOT EXISTS (SELECT 1 FROM expenses o
                      WHERE o.uuid = NEW.reverses_uuid
                        AND o.status = 'posted'
                        AND o.reverses_uuid IS NULL
                        AND o.reversed_by_uuid IS NULL);
    SELECT RAISE(ABORT, 'expense reversal must match the original expense')
    WHERE NOT EXISTS (SELECT 1 FROM expenses o
                      WHERE o.uuid = NEW.reverses_uuid
                        AND o.category_id IS NEW.category_id
                        AND o.amount_paisa IS NEW.amount_paisa
                        AND o.method IS NEW.method
                        AND o.business_date IS NEW.business_date);
END;
