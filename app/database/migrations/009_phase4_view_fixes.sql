-- ============================================================================
-- Migration 009 — Phase-4 view corrections (v_outstanding_receivables,
-- v_customer_ledger).
--
-- Corrects two accounting defects in the 008 view definitions. Migration
-- 008 itself is NOT edited (numbered migrations are append-only); the views
-- are redefined here, which is the versioned mechanism the design plan §1
-- prescribes for schema changes.
--
-- Defect 1 — v_customer_ledger double-subtracted paid_now_paisa.
--   The 008 ticket entry debited (gross_amount_paisa - paid_now_paisa) AND a
--   separate 'paid_now' credit entry existed, so the ledger's running
--   balance sat exactly paid_now BELOW v_outstanding_receivables (which
--   implements the design §7 formula). The 008 header comment documents the
--   intent as "ticket charges (gross as debit, paid-now as credit)": the
--   ticket entry now debits the full gross.
--
-- Defect 2 — both views negated reversed tickets TWICE.
--   The 008 CASE mapped status='reversed' to 0 while the reversal ROW
--   contributed -(gross - paid_now). But the reversal workflow flips the
--   original to 'reversed' AND posts the reversal row, so voiding a
--   PKR 10,800 ticket left -10,800 outstanding instead of 0. The corrected
--   rule: a reversed original keeps its posted contribution (the status flip
--   is a marker; the row and its audit trail are never erased) and the
--   reversal row negates it. Voiding a ticket with paid_now P now leaves -P
--   (the cash taken at the sale is still owed back / held as customer
--   credit) instead of -gross.
--
-- Authoritative formula (design plan §7):
--   outstanding = opening_receivable
--               + Σ posted ticket gross − Σ posted ticket paid_now
--               − Σ allocations to posted tickets ± Σ adjustments.
-- After this migration the ledger's running balance and
-- v_outstanding_receivables agree on every scenario (verified by the
-- Phase-4 service tests).
-- ============================================================================

DROP VIEW v_customer_ledger;
DROP VIEW v_outstanding_receivables;

-- Ledger-derived outstanding receivable per customer (design §7). This view —
-- never an editable column — is the authoritative balance.
CREATE VIEW v_outstanding_receivables AS
SELECT c.id AS customer_id,
       c.customer_code,
       c.name,
       c.credit_limit_paisa,
       c.opening_receivable_paisa
       + COALESCE((SELECT SUM(CASE
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
           CASE WHEN cs.reverses_uuid IS NOT NULL
                THEN 0 ELSE cs.gross_amount_paisa END,
           CASE WHEN cs.reverses_uuid IS NOT NULL
                THEN (cs.gross_amount_paisa - cs.paid_now_paisa)
                ELSE 0 END
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
