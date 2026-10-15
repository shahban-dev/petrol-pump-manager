-- ============================================================================
-- Migration 007 — Phase 3: prices, tanks, suppliers, deliveries, inventory
-- Design plan: documentation/00_design_plan.md §4 (trigger groups A/B/C/D/F/I),
-- §5 (numeric standards), §11 (assumptions).
--
-- New tables:
--   * price_history         — immutable price timeline per fuel product
--                             (old rows are NEVER edited; old sales keep
--                             their original values forever).
--   * tanks                 — physical storage tanks (master data, mutable).
--   * suppliers             — fuel suppliers (master data, mutable).
--   * fuel_deliveries       — posted delivery facts (immutable, Group C).
--   * inventory_movements   — every stock change: delivery/sale/adjustment/
--                             opening (immutable, Group C).
--   * inventory_adjustments — authorized stock corrections (immutable, Group C;
--                             a wrong adjustment is fixed by a compensating
--                             row, never an edit).
--   * tank_readings         — dip/stick readings per tank per day (mutable).
--   * meter_events          — gains one nullable column: wrap_value_ml, the
--                             Phase-3 implementation of the rollover wrap-value
--                             math deferred in Phase 2 notes (§11.1).
--
-- Numeric standards (§5): money as INTEGER paisa (*_paisa), quantities as
-- INTEGER milliliters (*_ml), fuel prices as INTEGER 0.0001 PKR per litre
-- (*_10k). The delivery-total trigger below uses the canonical §5.2 formula
-- amount_paisa = round_half_up(quantity_ml * unit_cost_10k / 100000);
-- SQLite integer division truncates, which equals round-half-up for
-- non-negative values, so (q * c + 50000) / 100000.
--
-- Ordering note — seeds stay out of audit_log: the B (audit) triggers for the
-- new tables are created AFTER the tank seed rows are inserted, and the
-- nozzles tank-wiring UPDATE runs while trg_nozzles_au_audit is temporarily
-- dropped (recreated verbatim afterwards). Migration-time data fixes are not
-- business changes; test_audit.py pins exact audit counts after migration,
-- so the audit log must still be empty when this migration finishes.
--
-- Segment-reading validation note: NO new trigger is created for
-- reading_kind='segment'. The E2 trigger rewritten in migration 006
-- (trg_nozzle_readings_bi_validate) has no reading_kind restriction: it
-- already validates nozzle active, business date exists, reading_ml >= 0,
-- and "lower than previous effective closing reading ⇒ meter_events row
-- required" for EVERY insert including segment rows. The closed-day rule
-- comes from trg_nozzle_readings_bi_closed_day (D1, 004) and the
-- non-operating rule from trg_nozzle_readings_bi_non_operating (006).
-- A separate segment trigger would duplicate those checks.
-- ============================================================================

-- --- Price history (immutable timeline) ---------------------------------------
CREATE TABLE price_history (
    id                INTEGER PRIMARY KEY,
    fuel_product_id   INTEGER NOT NULL REFERENCES fuel_products (id),
    effective_from_utc TEXT NOT NULL,
    unit_price_10k    INTEGER NOT NULL CHECK (unit_price_10k > 0),
    business_date     TEXT NOT NULL REFERENCES business_days (business_date),
    created_by        TEXT NOT NULL DEFAULT 'system',
    created_at_utc    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (fuel_product_id, effective_from_utc)
);
CREATE INDEX idx_price_history_product_effective
    ON price_history (fuel_product_id, effective_from_utc);

-- --- Tanks (mutable master data) ----------------------------------------------
CREATE TABLE tanks (
    id              INTEGER PRIMARY KEY,
    fuel_product_id INTEGER NOT NULL REFERENCES fuel_products (id),
    code            TEXT NOT NULL UNIQUE,
    label_en        TEXT,
    capacity_ml     INTEGER CHECK (capacity_ml IS NULL OR capacity_ml >= 0),
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_by      TEXT NOT NULL DEFAULT 'system',
    created_at_utc  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at_utc  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- --- Suppliers (mutable master data) ------------------------------------------
CREATE TABLE suppliers (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    phone           TEXT,
    address         TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_by      TEXT NOT NULL DEFAULT 'system',
    created_at_utc  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at_utc  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- --- Fuel deliveries (posted facts; immutable via Group C) --------------------
CREATE TABLE fuel_deliveries (
    id                 INTEGER PRIMARY KEY,
    uuid               TEXT NOT NULL UNIQUE,
    supplier_id        INTEGER NOT NULL REFERENCES suppliers (id),
    fuel_product_id    INTEGER NOT NULL REFERENCES fuel_products (id),
    tank_id            INTEGER REFERENCES tanks (id),
    business_date      TEXT NOT NULL REFERENCES business_days (business_date),
    quantity_ml        INTEGER NOT NULL CHECK (quantity_ml > 0),
    unit_cost_10k      INTEGER NOT NULL CHECK (unit_cost_10k > 0),
    total_cost_paisa   INTEGER NOT NULL CHECK (total_cost_paisa >= 0),
    other_cost_paisa   INTEGER NOT NULL DEFAULT 0 CHECK (other_cost_paisa >= 0),
    supplier_invoice_no TEXT,
    amount_paid_paisa  INTEGER NOT NULL DEFAULT 0 CHECK (amount_paid_paisa >= 0),
    payment_status     TEXT NOT NULL DEFAULT 'unpaid'
                       CHECK (payment_status IN ('unpaid', 'partial', 'paid')),
    occurred_at_utc    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    created_by         TEXT NOT NULL DEFAULT 'system',
    created_at_utc     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    -- H4: reject a duplicate invoice number from the same supplier.
    -- SQLite treats NULLs as distinct in UNIQUE, so repeat NULL-invoice
    -- deliveries are still allowed.
    UNIQUE (supplier_id, supplier_invoice_no)
);
CREATE INDEX idx_fuel_deliveries_business_date
    ON fuel_deliveries (business_date);
CREATE INDEX idx_fuel_deliveries_supplier
    ON fuel_deliveries (supplier_id);

-- --- Inventory movements (posted facts; immutable via Group C) -----------------
-- Every stock change is a row here: deliveries add, sales subtract,
-- adjustments correct (either sign), 'opening' seeds a day's start.
CREATE TABLE inventory_movements (
    id              INTEGER PRIMARY KEY,
    uuid            TEXT NOT NULL UNIQUE,
    business_date   TEXT NOT NULL REFERENCES business_days (business_date),
    fuel_product_id INTEGER NOT NULL REFERENCES fuel_products (id),
    tank_id         INTEGER NOT NULL REFERENCES tanks (id),
    movement_type   TEXT NOT NULL
                    CHECK (movement_type IN ('delivery', 'sale', 'adjustment', 'opening')),
    quantity_ml     INTEGER NOT NULL,
    source_table    TEXT NOT NULL,
    source_id       INTEGER,
    source_uuid     TEXT NOT NULL UNIQUE,
    occurred_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    reason          TEXT,
    created_by      TEXT NOT NULL DEFAULT 'system',
    created_at_utc  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (
        (movement_type = 'delivery'   AND quantity_ml > 0) OR
        (movement_type = 'sale'       AND quantity_ml < 0) OR
        (movement_type = 'adjustment' AND quantity_ml != 0) OR
        (movement_type = 'opening'    AND quantity_ml >= 0)
    )
);
CREATE INDEX idx_inventory_movements_business_date
    ON inventory_movements (business_date);
CREATE INDEX idx_inventory_movements_fuel_product
    ON inventory_movements (fuel_product_id);
CREATE INDEX idx_inventory_movements_tank
    ON inventory_movements (tank_id);
-- source_uuid already has an autoindex from UNIQUE; the explicit index below
-- keeps the naming convention uniform for schema verification.
CREATE INDEX idx_inventory_movements_source_uuid
    ON inventory_movements (source_uuid);

-- --- Inventory adjustments (posted facts; immutable via Group C) ---------------
CREATE TABLE inventory_adjustments (
    id              INTEGER PRIMARY KEY,
    uuid            TEXT NOT NULL UNIQUE,
    fuel_product_id INTEGER NOT NULL REFERENCES fuel_products (id),
    tank_id         INTEGER NOT NULL REFERENCES tanks (id),
    business_date   TEXT NOT NULL REFERENCES business_days (business_date),
    quantity_ml     INTEGER NOT NULL CHECK (quantity_ml != 0),
    reason          TEXT NOT NULL,
    authorized_by   TEXT NOT NULL DEFAULT 'system',
    occurred_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    created_by      TEXT NOT NULL DEFAULT 'system',
    created_at_utc  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_inventory_adjustments_business_date
    ON inventory_adjustments (business_date);

-- --- Tank readings (mutable dip readings, one per tank per day) ----------------
CREATE TABLE tank_readings (
    id              INTEGER PRIMARY KEY,
    tank_id         INTEGER NOT NULL REFERENCES tanks (id),
    business_date   TEXT NOT NULL REFERENCES business_days (business_date),
    quantity_ml     INTEGER NOT NULL CHECK (quantity_ml >= 0),
    occurred_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    measured_by     TEXT NOT NULL DEFAULT 'system',
    note            TEXT,
    created_by      TEXT NOT NULL DEFAULT 'system',
    created_at_utc  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at_utc  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (tank_id, business_date)
);

-- --- Meter rollover wrap value (Phase-3 part of §11.1) --------------------------
-- NULL = unknown/not applicable; when set, the meter's wrap (rollover) point
-- in milliliters, so sale math can compute (wrap - before) + after.
ALTER TABLE meter_events
    ADD COLUMN wrap_value_ml INTEGER
        CHECK (wrap_value_ml IS NULL OR wrap_value_ml > 0);

-- ============================ Views ===========================================
-- v_stock_position: cumulative movements per tank+product. Informational
-- only, never authoritative: services add the day's opening balance (from
-- opening_balances, which carries the full prior history via carry-forward)
-- to get the tracked balance.
CREATE VIEW v_stock_position AS
SELECT tank_id,
       fuel_product_id,
       COALESCE(SUM(quantity_ml), 0) AS cumulative_ml
  FROM inventory_movements
 GROUP BY tank_id, fuel_product_id;

-- v_physical_stock: the dip-reading series per tank.
CREATE VIEW v_physical_stock AS
SELECT tank_id, business_date, quantity_ml, occurred_at_utc, measured_by
  FROM tank_readings;

-- ============================ Seed + wiring ===================================
-- Seeds run BEFORE the Group-B audit triggers are created (below), so the
-- migration leaves audit_log empty — pinned by test_audit.py.
-- Product ids are looked up by code, never hard-coded.
INSERT INTO tanks (fuel_product_id, code, label_en)
VALUES ((SELECT id FROM fuel_products WHERE code = 'PETROL'),
        'TANK-PETROL', 'Petrol Tank'),
       ((SELECT id FROM fuel_products WHERE code = 'DIESEL'),
        'TANK-DIESEL', 'Diesel Tank');

-- Wire the Phase-1 nozzles to the tank of their fuel product. The nozzles
-- au_audit trigger (003) is dropped for the duration of this wiring UPDATE
-- and recreated verbatim afterwards: the wiring is a migration-time data
-- fix, not a business change, and must not pollute audit_log.
DROP TRIGGER trg_nozzles_au_audit;
UPDATE nozzles
   SET tank_id = (SELECT t.id
                    FROM tanks t
                   WHERE t.fuel_product_id = nozzles.fuel_product_id)
 WHERE tank_id IS NULL;
CREATE TRIGGER trg_nozzles_au_audit
AFTER UPDATE ON nozzles
FOR EACH ROW
-- Skip the timestamp-touch inner update (group A); see app_settings note.
WHEN (OLD.code IS NOT NEW.code
      OR OLD.fuel_product_id IS NOT NEW.fuel_product_id
      OR OLD.tank_id IS NOT NEW.tank_id
      OR OLD.label_en IS NOT NEW.label_en
      OR OLD.label_ur IS NOT NEW.label_ur
      OR OLD.is_active IS NOT NEW.is_active
      OR OLD.sort_order IS NOT NEW.sort_order)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('nozzles', NEW.id, 'UPDATE',
            json_object('id', OLD.id,
                        'code', OLD.code,
                        'fuel_product_id', OLD.fuel_product_id,
                        'tank_id', OLD.tank_id,
                        'label_en', OLD.label_en,
                        'label_ur', OLD.label_ur,
                        'is_active', OLD.is_active,
                        'sort_order', OLD.sort_order,
                        'created_at_utc', OLD.created_at_utc,
                        'updated_at_utc', OLD.updated_at_utc),
            json_object('id', NEW.id,
                        'code', NEW.code,
                        'fuel_product_id', NEW.fuel_product_id,
                        'tank_id', NEW.tank_id,
                        'label_en', NEW.label_en,
                        'label_ur', NEW.label_ur,
                        'is_active', NEW.is_active,
                        'sort_order', NEW.sort_order,
                        'created_at_utc', NEW.created_at_utc,
                        'updated_at_utc', NEW.updated_at_utc),
            NULL, 'system');
END;

-- ================= Group A1 — created_at_utc immutability =====================
CREATE TRIGGER trg_tanks_bu_guard_created
BEFORE UPDATE ON tanks
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_suppliers_bu_guard_created
BEFORE UPDATE ON suppliers
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_tank_readings_bu_guard_created
BEFORE UPDATE ON tank_readings
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_price_history_bu_guard_created
BEFORE UPDATE ON price_history
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_fuel_deliveries_bu_guard_created
BEFORE UPDATE ON fuel_deliveries
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_inventory_adjustments_bu_guard_created
BEFORE UPDATE ON inventory_adjustments
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

CREATE TRIGGER trg_inventory_movements_bu_guard_created
BEFORE UPDATE ON inventory_movements
FOR EACH ROW
WHEN NEW.created_at_utc IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'created_at_utc is immutable');
END;

-- ================= Group A2 — updated_at_utc touches ==========================
-- Only the mutable tables (tanks, suppliers, tank_readings). The touch fires
-- only when a business column changed; the inner UPDATE re-fires these
-- triggers but matches nothing, so there is no loop (see 002 header).
CREATE TRIGGER trg_tanks_bu_touch_updated
BEFORE UPDATE ON tanks
FOR EACH ROW
WHEN (OLD.fuel_product_id IS NOT NEW.fuel_product_id
      OR OLD.code IS NOT NEW.code
      OR OLD.label_en IS NOT NEW.label_en
      OR OLD.capacity_ml IS NOT NEW.capacity_ml
      OR OLD.is_active IS NOT NEW.is_active)
BEGIN
    UPDATE tanks
       SET updated_at_utc = strftime('%Y-%m-%dT%H:%M:%fZ','now')
     WHERE id = NEW.id;
END;

CREATE TRIGGER trg_suppliers_bu_touch_updated
BEFORE UPDATE ON suppliers
FOR EACH ROW
WHEN (OLD.name IS NOT NEW.name
      OR OLD.phone IS NOT NEW.phone
      OR OLD.address IS NOT NEW.address
      OR OLD.is_active IS NOT NEW.is_active)
BEGIN
    UPDATE suppliers
       SET updated_at_utc = strftime('%Y-%m-%dT%H:%M:%fZ','now')
     WHERE id = NEW.id;
END;

CREATE TRIGGER trg_tank_readings_bu_touch_updated
BEFORE UPDATE ON tank_readings
FOR EACH ROW
WHEN (OLD.tank_id IS NOT NEW.tank_id
      OR OLD.business_date IS NOT NEW.business_date
      OR OLD.quantity_ml IS NOT NEW.quantity_ml
      OR OLD.occurred_at_utc IS NOT NEW.occurred_at_utc
      OR OLD.measured_by IS NOT NEW.measured_by
      OR OLD.note IS NOT NEW.note)
BEGIN
    UPDATE tank_readings
       SET updated_at_utc = strftime('%Y-%m-%dT%H:%M:%fZ','now')
     WHERE id = NEW.id;
END;

-- ================= Group B — audit history ====================================
-- ai/au/bd per table, json_object snapshots of business columns.
-- business_date = the row's business_date where the table has one.
-- actor = created_by (every Phase-3 table carries it).
-- au triggers on the mutable tables reuse the A2 business-column WHEN guard
-- so the timestamp-touch inner UPDATE writes no audit row (003 pattern).
-- au/bd on the immutable tables (price_history, fuel_deliveries,
-- inventory_adjustments, inventory_movements) can never fire — Group C
-- aborts the UPDATE/DELETE first — but are installed for uniformity.
-- audit_log itself has no triggers: no recursion.

-- --------------------------------- tanks --------------------------------------
CREATE TRIGGER trg_tanks_ai_audit
AFTER INSERT ON tanks
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('tanks', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'fuel_product_id', NEW.fuel_product_id,
                        'code', NEW.code,
                        'label_en', NEW.label_en,
                        'capacity_ml', NEW.capacity_ml,
                        'is_active', NEW.is_active,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc,
                        'updated_at_utc', NEW.updated_at_utc),
            NULL, NEW.created_by);
END;

CREATE TRIGGER trg_tanks_au_audit
AFTER UPDATE ON tanks
FOR EACH ROW
WHEN (OLD.fuel_product_id IS NOT NEW.fuel_product_id
      OR OLD.code IS NOT NEW.code
      OR OLD.label_en IS NOT NEW.label_en
      OR OLD.capacity_ml IS NOT NEW.capacity_ml
      OR OLD.is_active IS NOT NEW.is_active)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('tanks', NEW.id, 'UPDATE',
            json_object('id', OLD.id,
                        'fuel_product_id', OLD.fuel_product_id,
                        'code', OLD.code,
                        'label_en', OLD.label_en,
                        'capacity_ml', OLD.capacity_ml,
                        'is_active', OLD.is_active,
                        'created_by', OLD.created_by,
                        'created_at_utc', OLD.created_at_utc,
                        'updated_at_utc', OLD.updated_at_utc),
            json_object('id', NEW.id,
                        'fuel_product_id', NEW.fuel_product_id,
                        'code', NEW.code,
                        'label_en', NEW.label_en,
                        'capacity_ml', NEW.capacity_ml,
                        'is_active', NEW.is_active,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc,
                        'updated_at_utc', NEW.updated_at_utc),
            NULL, NEW.created_by);
END;

CREATE TRIGGER trg_tanks_bd_audit
BEFORE DELETE ON tanks
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('tanks', OLD.id, 'DELETE',
            json_object('id', OLD.id,
                        'fuel_product_id', OLD.fuel_product_id,
                        'code', OLD.code,
                        'label_en', OLD.label_en,
                        'capacity_ml', OLD.capacity_ml,
                        'is_active', OLD.is_active,
                        'created_by', OLD.created_by,
                        'created_at_utc', OLD.created_at_utc,
                        'updated_at_utc', OLD.updated_at_utc),
            NULL, NULL, OLD.created_by);
END;

-- -------------------------------- suppliers -----------------------------------
CREATE TRIGGER trg_suppliers_ai_audit
AFTER INSERT ON suppliers
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('suppliers', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'name', NEW.name,
                        'phone', NEW.phone,
                        'address', NEW.address,
                        'is_active', NEW.is_active,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc,
                        'updated_at_utc', NEW.updated_at_utc),
            NULL, NEW.created_by);
END;

CREATE TRIGGER trg_suppliers_au_audit
AFTER UPDATE ON suppliers
FOR EACH ROW
WHEN (OLD.name IS NOT NEW.name
      OR OLD.phone IS NOT NEW.phone
      OR OLD.address IS NOT NEW.address
      OR OLD.is_active IS NOT NEW.is_active)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('suppliers', NEW.id, 'UPDATE',
            json_object('id', OLD.id,
                        'name', OLD.name,
                        'phone', OLD.phone,
                        'address', OLD.address,
                        'is_active', OLD.is_active,
                        'created_by', OLD.created_by,
                        'created_at_utc', OLD.created_at_utc,
                        'updated_at_utc', OLD.updated_at_utc),
            json_object('id', NEW.id,
                        'name', NEW.name,
                        'phone', NEW.phone,
                        'address', NEW.address,
                        'is_active', NEW.is_active,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc,
                        'updated_at_utc', NEW.updated_at_utc),
            NULL, NEW.created_by);
END;

CREATE TRIGGER trg_suppliers_bd_audit
BEFORE DELETE ON suppliers
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('suppliers', OLD.id, 'DELETE',
            json_object('id', OLD.id,
                        'name', OLD.name,
                        'phone', OLD.phone,
                        'address', OLD.address,
                        'is_active', OLD.is_active,
                        'created_by', OLD.created_by,
                        'created_at_utc', OLD.created_at_utc,
                        'updated_at_utc', OLD.updated_at_utc),
            NULL, NULL, OLD.created_by);
END;

-- ------------------------------ tank_readings --------------------------------
CREATE TRIGGER trg_tank_readings_ai_audit
AFTER INSERT ON tank_readings
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('tank_readings', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'tank_id', NEW.tank_id,
                        'business_date', NEW.business_date,
                        'quantity_ml', NEW.quantity_ml,
                        'occurred_at_utc', NEW.occurred_at_utc,
                        'measured_by', NEW.measured_by,
                        'note', NEW.note,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc,
                        'updated_at_utc', NEW.updated_at_utc),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_tank_readings_au_audit
AFTER UPDATE ON tank_readings
FOR EACH ROW
WHEN (OLD.tank_id IS NOT NEW.tank_id
      OR OLD.business_date IS NOT NEW.business_date
      OR OLD.quantity_ml IS NOT NEW.quantity_ml
      OR OLD.occurred_at_utc IS NOT NEW.occurred_at_utc
      OR OLD.measured_by IS NOT NEW.measured_by
      OR OLD.note IS NOT NEW.note)
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('tank_readings', NEW.id, 'UPDATE',
            json_object('id', OLD.id,
                        'tank_id', OLD.tank_id,
                        'business_date', OLD.business_date,
                        'quantity_ml', OLD.quantity_ml,
                        'occurred_at_utc', OLD.occurred_at_utc,
                        'measured_by', OLD.measured_by,
                        'note', OLD.note,
                        'created_by', OLD.created_by,
                        'created_at_utc', OLD.created_at_utc,
                        'updated_at_utc', OLD.updated_at_utc),
            json_object('id', NEW.id,
                        'tank_id', NEW.tank_id,
                        'business_date', NEW.business_date,
                        'quantity_ml', NEW.quantity_ml,
                        'occurred_at_utc', NEW.occurred_at_utc,
                        'measured_by', NEW.measured_by,
                        'note', NEW.note,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc,
                        'updated_at_utc', NEW.updated_at_utc),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_tank_readings_bd_audit
BEFORE DELETE ON tank_readings
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('tank_readings', OLD.id, 'DELETE',
            json_object('id', OLD.id,
                        'tank_id', OLD.tank_id,
                        'business_date', OLD.business_date,
                        'quantity_ml', OLD.quantity_ml,
                        'occurred_at_utc', OLD.occurred_at_utc,
                        'measured_by', OLD.measured_by,
                        'note', OLD.note,
                        'created_by', OLD.created_by,
                        'created_at_utc', OLD.created_at_utc,
                        'updated_at_utc', OLD.updated_at_utc),
            NULL, OLD.business_date, OLD.created_by);
END;

-- ------------------------------ price_history -------------------------------
CREATE TRIGGER trg_price_history_ai_audit
AFTER INSERT ON price_history
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('price_history', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'fuel_product_id', NEW.fuel_product_id,
                        'effective_from_utc', NEW.effective_from_utc,
                        'unit_price_10k', NEW.unit_price_10k,
                        'business_date', NEW.business_date,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_price_history_au_audit
AFTER UPDATE ON price_history
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('price_history', NEW.id, 'UPDATE',
            json_object('id', OLD.id,
                        'fuel_product_id', OLD.fuel_product_id,
                        'effective_from_utc', OLD.effective_from_utc,
                        'unit_price_10k', OLD.unit_price_10k,
                        'business_date', OLD.business_date,
                        'created_by', OLD.created_by,
                        'created_at_utc', OLD.created_at_utc),
            json_object('id', NEW.id,
                        'fuel_product_id', NEW.fuel_product_id,
                        'effective_from_utc', NEW.effective_from_utc,
                        'unit_price_10k', NEW.unit_price_10k,
                        'business_date', NEW.business_date,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_price_history_bd_audit
BEFORE DELETE ON price_history
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('price_history', OLD.id, 'DELETE',
            json_object('id', OLD.id,
                        'fuel_product_id', OLD.fuel_product_id,
                        'effective_from_utc', OLD.effective_from_utc,
                        'unit_price_10k', OLD.unit_price_10k,
                        'business_date', OLD.business_date,
                        'created_by', OLD.created_by,
                        'created_at_utc', OLD.created_at_utc),
            NULL, OLD.business_date, OLD.created_by);
END;

-- ----------------------------- fuel_deliveries ------------------------------
CREATE TRIGGER trg_fuel_deliveries_ai_audit
AFTER INSERT ON fuel_deliveries
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('fuel_deliveries', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'uuid', NEW.uuid,
                        'supplier_id', NEW.supplier_id,
                        'fuel_product_id', NEW.fuel_product_id,
                        'tank_id', NEW.tank_id,
                        'business_date', NEW.business_date,
                        'quantity_ml', NEW.quantity_ml,
                        'unit_cost_10k', NEW.unit_cost_10k,
                        'total_cost_paisa', NEW.total_cost_paisa,
                        'other_cost_paisa', NEW.other_cost_paisa,
                        'supplier_invoice_no', NEW.supplier_invoice_no,
                        'amount_paid_paisa', NEW.amount_paid_paisa,
                        'payment_status', NEW.payment_status,
                        'occurred_at_utc', NEW.occurred_at_utc,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_fuel_deliveries_au_audit
AFTER UPDATE ON fuel_deliveries
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('fuel_deliveries', NEW.id, 'UPDATE',
            json_object('id', OLD.id,
                        'uuid', OLD.uuid,
                        'supplier_id', OLD.supplier_id,
                        'fuel_product_id', OLD.fuel_product_id,
                        'tank_id', OLD.tank_id,
                        'business_date', OLD.business_date,
                        'quantity_ml', OLD.quantity_ml,
                        'unit_cost_10k', OLD.unit_cost_10k,
                        'total_cost_paisa', OLD.total_cost_paisa,
                        'other_cost_paisa', OLD.other_cost_paisa,
                        'supplier_invoice_no', OLD.supplier_invoice_no,
                        'amount_paid_paisa', OLD.amount_paid_paisa,
                        'payment_status', OLD.payment_status,
                        'occurred_at_utc', OLD.occurred_at_utc,
                        'created_by', OLD.created_by,
                        'created_at_utc', OLD.created_at_utc),
            json_object('id', NEW.id,
                        'uuid', NEW.uuid,
                        'supplier_id', NEW.supplier_id,
                        'fuel_product_id', NEW.fuel_product_id,
                        'tank_id', NEW.tank_id,
                        'business_date', NEW.business_date,
                        'quantity_ml', NEW.quantity_ml,
                        'unit_cost_10k', NEW.unit_cost_10k,
                        'total_cost_paisa', NEW.total_cost_paisa,
                        'other_cost_paisa', NEW.other_cost_paisa,
                        'supplier_invoice_no', NEW.supplier_invoice_no,
                        'amount_paid_paisa', NEW.amount_paid_paisa,
                        'payment_status', NEW.payment_status,
                        'occurred_at_utc', NEW.occurred_at_utc,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_fuel_deliveries_bd_audit
BEFORE DELETE ON fuel_deliveries
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('fuel_deliveries', OLD.id, 'DELETE',
            json_object('id', OLD.id,
                        'uuid', OLD.uuid,
                        'supplier_id', OLD.supplier_id,
                        'fuel_product_id', OLD.fuel_product_id,
                        'tank_id', OLD.tank_id,
                        'business_date', OLD.business_date,
                        'quantity_ml', OLD.quantity_ml,
                        'unit_cost_10k', OLD.unit_cost_10k,
                        'total_cost_paisa', OLD.total_cost_paisa,
                        'other_cost_paisa', OLD.other_cost_paisa,
                        'supplier_invoice_no', OLD.supplier_invoice_no,
                        'amount_paid_paisa', OLD.amount_paid_paisa,
                        'payment_status', OLD.payment_status,
                        'occurred_at_utc', OLD.occurred_at_utc,
                        'created_by', OLD.created_by,
                        'created_at_utc', OLD.created_at_utc),
            NULL, OLD.business_date, OLD.created_by);
END;

-- --------------------------- inventory_adjustments ---------------------------
CREATE TRIGGER trg_inventory_adjustments_ai_audit
AFTER INSERT ON inventory_adjustments
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('inventory_adjustments', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'uuid', NEW.uuid,
                        'fuel_product_id', NEW.fuel_product_id,
                        'tank_id', NEW.tank_id,
                        'business_date', NEW.business_date,
                        'quantity_ml', NEW.quantity_ml,
                        'reason', NEW.reason,
                        'authorized_by', NEW.authorized_by,
                        'occurred_at_utc', NEW.occurred_at_utc,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_inventory_adjustments_au_audit
AFTER UPDATE ON inventory_adjustments
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('inventory_adjustments', NEW.id, 'UPDATE',
            json_object('id', OLD.id,
                        'uuid', OLD.uuid,
                        'fuel_product_id', OLD.fuel_product_id,
                        'tank_id', OLD.tank_id,
                        'business_date', OLD.business_date,
                        'quantity_ml', OLD.quantity_ml,
                        'reason', OLD.reason,
                        'authorized_by', OLD.authorized_by,
                        'occurred_at_utc', OLD.occurred_at_utc,
                        'created_by', OLD.created_by,
                        'created_at_utc', OLD.created_at_utc),
            json_object('id', NEW.id,
                        'uuid', NEW.uuid,
                        'fuel_product_id', NEW.fuel_product_id,
                        'tank_id', NEW.tank_id,
                        'business_date', NEW.business_date,
                        'quantity_ml', NEW.quantity_ml,
                        'reason', NEW.reason,
                        'authorized_by', NEW.authorized_by,
                        'occurred_at_utc', NEW.occurred_at_utc,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_inventory_adjustments_bd_audit
BEFORE DELETE ON inventory_adjustments
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('inventory_adjustments', OLD.id, 'DELETE',
            json_object('id', OLD.id,
                        'uuid', OLD.uuid,
                        'fuel_product_id', OLD.fuel_product_id,
                        'tank_id', OLD.tank_id,
                        'business_date', OLD.business_date,
                        'quantity_ml', OLD.quantity_ml,
                        'reason', OLD.reason,
                        'authorized_by', OLD.authorized_by,
                        'occurred_at_utc', OLD.occurred_at_utc,
                        'created_by', OLD.created_by,
                        'created_at_utc', OLD.created_at_utc),
            NULL, OLD.business_date, OLD.created_by);
END;

-- ---------------------------- inventory_movements ----------------------------
CREATE TRIGGER trg_inventory_movements_ai_audit
AFTER INSERT ON inventory_movements
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('inventory_movements', NEW.id, 'INSERT', NULL,
            json_object('id', NEW.id,
                        'uuid', NEW.uuid,
                        'business_date', NEW.business_date,
                        'fuel_product_id', NEW.fuel_product_id,
                        'tank_id', NEW.tank_id,
                        'movement_type', NEW.movement_type,
                        'quantity_ml', NEW.quantity_ml,
                        'source_table', NEW.source_table,
                        'source_id', NEW.source_id,
                        'source_uuid', NEW.source_uuid,
                        'occurred_at_utc', NEW.occurred_at_utc,
                        'reason', NEW.reason,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_inventory_movements_au_audit
AFTER UPDATE ON inventory_movements
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('inventory_movements', NEW.id, 'UPDATE',
            json_object('id', OLD.id,
                        'uuid', OLD.uuid,
                        'business_date', OLD.business_date,
                        'fuel_product_id', OLD.fuel_product_id,
                        'tank_id', OLD.tank_id,
                        'movement_type', OLD.movement_type,
                        'quantity_ml', OLD.quantity_ml,
                        'source_table', OLD.source_table,
                        'source_id', OLD.source_id,
                        'source_uuid', OLD.source_uuid,
                        'occurred_at_utc', OLD.occurred_at_utc,
                        'reason', OLD.reason,
                        'created_by', OLD.created_by,
                        'created_at_utc', OLD.created_at_utc),
            json_object('id', NEW.id,
                        'uuid', NEW.uuid,
                        'business_date', NEW.business_date,
                        'fuel_product_id', NEW.fuel_product_id,
                        'tank_id', NEW.tank_id,
                        'movement_type', NEW.movement_type,
                        'quantity_ml', NEW.quantity_ml,
                        'source_table', NEW.source_table,
                        'source_id', NEW.source_id,
                        'source_uuid', NEW.source_uuid,
                        'occurred_at_utc', NEW.occurred_at_utc,
                        'reason', NEW.reason,
                        'created_by', NEW.created_by,
                        'created_at_utc', NEW.created_at_utc),
            NEW.business_date, NEW.created_by);
END;

CREATE TRIGGER trg_inventory_movements_bd_audit
BEFORE DELETE ON inventory_movements
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, old_values_json,
                           new_values_json, business_date, actor)
    VALUES ('inventory_movements', OLD.id, 'DELETE',
            json_object('id', OLD.id,
                        'uuid', OLD.uuid,
                        'business_date', OLD.business_date,
                        'fuel_product_id', OLD.fuel_product_id,
                        'tank_id', OLD.tank_id,
                        'movement_type', OLD.movement_type,
                        'quantity_ml', OLD.quantity_ml,
                        'source_table', OLD.source_table,
                        'source_id', OLD.source_id,
                        'source_uuid', OLD.source_uuid,
                        'occurred_at_utc', OLD.occurred_at_utc,
                        'reason', OLD.reason,
                        'created_by', OLD.created_by,
                        'created_at_utc', OLD.created_at_utc),
            NULL, OLD.business_date, OLD.created_by);
END;

-- ================= Group C — posted-ledger immutability ======================
-- price_history: old rows are never edited or deleted; a price change is a
-- new row with a later effective_from_utc.
CREATE TRIGGER trg_price_history_bu_no_update
BEFORE UPDATE ON price_history
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'price_history is immutable; post a new price row with a later effective_from_utc instead');
END;

CREATE TRIGGER trg_price_history_bd_no_delete
BEFORE DELETE ON price_history
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'price_history is immutable; rows cannot be deleted');
END;

-- fuel_deliveries: posted facts — no UPDATE, no DELETE.
CREATE TRIGGER trg_fuel_deliveries_bu_no_update
BEFORE UPDATE ON fuel_deliveries
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'fuel_deliveries is immutable; post a correcting delivery instead');
END;

CREATE TRIGGER trg_fuel_deliveries_bd_no_delete
BEFORE DELETE ON fuel_deliveries
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'fuel_deliveries is immutable; rows cannot be deleted');
END;

-- inventory_adjustments: a wrong adjustment is fixed by a compensating row.
CREATE TRIGGER trg_inventory_adjustments_bu_no_update
BEFORE UPDATE ON inventory_adjustments
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'inventory_adjustments is immutable; post a compensating adjustment instead');
END;

CREATE TRIGGER trg_inventory_adjustments_bd_no_delete
BEFORE DELETE ON inventory_adjustments
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'inventory_adjustments is immutable; rows cannot be deleted');
END;

-- inventory_movements: the stock ledger is append-only.
CREATE TRIGGER trg_inventory_movements_bu_no_update
BEFORE UPDATE ON inventory_movements
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'inventory_movements is immutable; post a compensating movement instead');
END;

CREATE TRIGGER trg_inventory_movements_bd_no_delete
BEFORE DELETE ON inventory_movements
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'inventory_movements is immutable; rows cannot be deleted');
END;

-- ================= Group D1 — closed business day =============================
-- Same message as the Phase-1/2 D1 triggers so service error translation
-- keeps working. Created before the F/I triggers below so the closed-day
-- rejection fires first.
CREATE TRIGGER trg_price_history_bi_closed_day
BEFORE INSERT ON price_history
FOR EACH ROW
WHEN (SELECT status FROM business_days
       WHERE business_date = NEW.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;

CREATE TRIGGER trg_fuel_deliveries_bi_closed_day
BEFORE INSERT ON fuel_deliveries
FOR EACH ROW
WHEN (SELECT status FROM business_days
       WHERE business_date = NEW.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;

CREATE TRIGGER trg_inventory_adjustments_bi_closed_day
BEFORE INSERT ON inventory_adjustments
FOR EACH ROW
WHEN (SELECT status FROM business_days
       WHERE business_date = NEW.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;

CREATE TRIGGER trg_inventory_movements_bi_closed_day
BEFORE INSERT ON inventory_movements
FOR EACH ROW
WHEN (SELECT status FROM business_days
       WHERE business_date = NEW.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;

CREATE TRIGGER trg_tank_readings_bi_closed_day
BEFORE INSERT ON tank_readings
FOR EACH ROW
WHEN (SELECT status FROM business_days
       WHERE business_date = NEW.business_date) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'business day is closed; reopen it first');
END;

-- ================= Group F2 — no negative tracked stock ======================
-- A sale movement must not drive the product's tracked balance negative.
-- opening_balances carries the full prior history via carry-forward, so the
-- tracked balance is: stock opening balance + today's movements + this row.
-- Default policy is block (§11 assumption 3): the manager must post an
-- authorized adjustment (e.g. a missed delivery) first.
CREATE TRIGGER trg_inventory_movements_bi_no_negative
BEFORE INSERT ON inventory_movements
FOR EACH ROW
WHEN NEW.movement_type = 'sale'
BEGIN
    SELECT RAISE(ABORT, 'sale would drive tracked stock negative')
     WHERE COALESCE((SELECT quantity_ml
                       FROM opening_balances
                      WHERE business_date = NEW.business_date
                        AND balance_type = 'stock'
                        AND fuel_product_id = NEW.fuel_product_id), 0)
         + COALESCE((SELECT SUM(quantity_ml)
                       FROM inventory_movements
                      WHERE fuel_product_id = NEW.fuel_product_id
                        AND business_date = NEW.business_date), 0)
         + NEW.quantity_ml < 0;
END;

-- ================= Group I — financial amount constraints ====================
-- Delivery total must equal quantity × unit cost, rounded half-up once at the
-- transaction boundary (canonical §5.2 formula). Updates are blocked by
-- Group C1, so BEFORE INSERT is enough.
CREATE TRIGGER trg_fuel_deliveries_bi_total_check
BEFORE INSERT ON fuel_deliveries
FOR EACH ROW
WHEN NEW.total_cost_paisa != (NEW.quantity_ml * NEW.unit_cost_10k + 50000) / 100000
BEGIN
    SELECT RAISE(ABORT, 'delivery total does not match quantity x unit cost');
END;

-- Amount paid on a delivery can never exceed total + other costs.
CREATE TRIGGER trg_fuel_deliveries_bi_paid_cap
BEFORE INSERT ON fuel_deliveries
FOR EACH ROW
WHEN NEW.amount_paid_paisa > NEW.total_cost_paisa + NEW.other_cost_paisa
BEGIN
    SELECT RAISE(ABORT, 'amount paid exceeds delivery total');
END;
