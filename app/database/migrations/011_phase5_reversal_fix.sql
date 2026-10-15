-- ============================================================================
-- Migration 011 — Phase-5 reversal-semantics correction.
--
-- Corrects the effective-amount rule in the four views introduced by
-- migration 010. Migration 010 mapped status='reversed' to 0 while the
-- reversal ROW contributed -amount; but the reversal workflow flips the
-- original to 'reversed' AND posts the reversal row, so voiding left a net
-- -amount instead of 0 (e.g. reversing a settlement batch of +A left -A,
-- and reversal → re-record netted to 0 instead of +A, making the day
-- unclosable). This is the exact defect class migration 009 fixed for the
-- Phase-4 views.
--
-- Corrected rule (design plan §4, migration 009 §11.1): a reversed original
-- KEEPS its posted contribution — the status flip is only a marker; the row
-- and its audit trail are never erased — and the reversal row negates it.
-- Net effect of a reversal: 0.
--
-- Migration 010 itself is NOT edited (numbered migrations are append-only);
-- the views are redefined here, the versioned mechanism the design plan §1
-- prescribes. No write paths change: inserts, flips and reversal rows are
-- identical; only the read-time CASE changes.
-- Authoritative check: reversing any posted row must leave its view net at 0.
-- ============================================================================

DROP VIEW v_daily_cash_position;
DROP VIEW v_cashbook;
DROP VIEW v_supplier_balances;
DROP VIEW v_effective_settlements;

-- Settlement legs net of reversals. A reversed original keeps its posted
-- contribution (the flip is a marker); the reversal row negates it.
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
-- net of reversals (reversed original keeps its entry; reversal offsets).
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
        ELSE p.amount_paisa
    END AS signed_paisa,
    p.remarks AS remarks,
    p.uuid,
    p.status
FROM cash_payments p
JOIN cash_categories c ON c.id = p.category_id;

-- Supplier balances: invoiced − paid at delivery − payments, computed.
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
                ELSE sp.amount_paisa
            END)
        FROM supplier_payments sp WHERE sp.supplier_id = s.id
    ), 0) AS outstanding_paisa
FROM suppliers s;

-- Daily cash position: opening + receipts − payments = expected, per day.
CREATE VIEW v_daily_cash_position AS
SELECT
    b.business_date,
    b.opening_cash_paisa,
    COALESCE((
        SELECT SUM(CASE
                WHEN r.reverses_uuid IS NOT NULL THEN -r.amount_paisa
                ELSE r.amount_paisa
            END)
        FROM cash_receipts r WHERE r.business_date = b.business_date
    ), 0) AS receipts_paisa,
    COALESCE((
        SELECT SUM(CASE
                WHEN p.reverses_uuid IS NOT NULL THEN -p.amount_paisa
                ELSE p.amount_paisa
            END)
        FROM cash_payments p WHERE p.business_date = b.business_date
    ), 0) AS payments_paisa,
    b.opening_cash_paisa
    + COALESCE((
        SELECT SUM(CASE
                WHEN r.reverses_uuid IS NOT NULL THEN -r.amount_paisa
                ELSE r.amount_paisa
            END)
        FROM cash_receipts r WHERE r.business_date = b.business_date
    ), 0)
    - COALESCE((
        SELECT SUM(CASE
                WHEN p.reverses_uuid IS NOT NULL THEN -p.amount_paisa
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
                        ELSE r.amount_paisa
                    END)
                FROM cash_receipts r WHERE r.business_date = b.business_date
            ), 0)
            - COALESCE((
                SELECT SUM(CASE
                        WHEN p.reverses_uuid IS NOT NULL THEN -p.amount_paisa
                        ELSE p.amount_paisa
                    END)
                FROM cash_payments p WHERE p.business_date = b.business_date
            ), 0))
         ELSE NULL
    END AS difference_paisa
FROM business_days b;
