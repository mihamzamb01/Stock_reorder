-- ===========================================================================
-- Stock reorder - multi-agent demo schema
--
-- Build order:
--   1. schema.sql   (this file)  -> tables, keys, indexes
--   2. seed.sql                  -> demo rows
--
-- Written for SQLite. The DDL is ANSI-ish and works on PostgreSQL / MySQL
-- with two small changes:
--   * INTEGER PRIMARY KEY AUTOINCREMENT  ->  SERIAL PRIMARY KEY  (Postgres)
--                                           INT AUTO_INCREMENT PRIMARY KEY (MySQL)
--   * REAL                             ->  DOUBLE PRECISION / FLOAT
-- ===========================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Warehousing
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS warehouses (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    code     TEXT    NOT NULL UNIQUE,
    name     TEXT    NOT NULL,
    region   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sku         TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL,
    category    TEXT    NOT NULL,
    unit        TEXT    NOT NULL,
    -- extra days of cover to hold on top of the supplier lead time
    safety_days INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- Stock position
--   on_hand       units physically in the warehouse
--   reorder_point level at which a replenishment should be triggered
--   on_order      units already inbound (so we do not double-order)
--   daily_demand  baseline consumption used when history is thin
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inventory (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id    INTEGER NOT NULL REFERENCES products(id)  ON DELETE CASCADE,
    warehouse_id  INTEGER NOT NULL REFERENCES warehouses(id) ON DELETE CASCADE,
    on_hand       INTEGER NOT NULL DEFAULT 0,
    reorder_point INTEGER NOT NULL DEFAULT 0,
    on_order      INTEGER NOT NULL DEFAULT 0,
    daily_demand  REAL    NOT NULL DEFAULT 0,
    unit          TEXT    NOT NULL DEFAULT 'pcs',
    UNIQUE (product_id, warehouse_id)
);

-- ---------------------------------------------------------------------------
-- Suppliers and their offers
--   price_break_qty / price_break_unit  volume tier price
--   min_order_value                    order must be worth at least this
--   contract_expires_on                end of the current agreement
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS suppliers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    code              TEXT    NOT NULL UNIQUE,
    name              TEXT    NOT NULL,
    country           TEXT    NOT NULL,
    currency          TEXT    NOT NULL DEFAULT 'USD',
    reliability_score REAL    NOT NULL,      -- 0..1 historical on-time + quality
    payment_terms     TEXT    NOT NULL,      -- e.g. Net 30
    years_active      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS supplier_offers (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id           INTEGER NOT NULL REFERENCES products(id)   ON DELETE CASCADE,
    supplier_id          INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    unit_price           REAL    NOT NULL,
    currency             TEXT    NOT NULL DEFAULT 'USD',
    moq                  INTEGER NOT NULL DEFAULT 1,      -- minimum order qty
    price_break_qty      INTEGER NOT NULL DEFAULT 0,      -- qty needed for tier price
    price_break_unit     REAL,                           -- unit price at that tier
    lead_time_days       INTEGER NOT NULL DEFAULT 14,
    shipping_per_unit    REAL    NOT NULL DEFAULT 0,
    min_order_value      REAL    NOT NULL DEFAULT 0,
    contract_expires_on  TEXT,                           -- ISO date
    defect_rate_pct      REAL    NOT NULL DEFAULT 0,
    on_time_pct          REAL    NOT NULL DEFAULT 95,
    incoterms            TEXT    NOT NULL DEFAULT 'FOB',
    UNIQUE (product_id, supplier_id)
);

-- ---------------------------------------------------------------------------
-- Consumption history - drives the demand forecast
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS demand_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   INTEGER NOT NULL REFERENCES products(id)   ON DELETE CASCADE,
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id) ON DELETE CASCADE,
    day          TEXT    NOT NULL,                          -- ISO date
    units_sold   INTEGER NOT NULL,
    UNIQUE (product_id, warehouse_id, day)
);

-- ---------------------------------------------------------------------------
-- Price history - shows what we have previously agreed with a supplier.
-- Used as a signal that the current list price is negotiable.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS negotiation_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   INTEGER NOT NULL REFERENCES products(id)   ON DELETE CASCADE,
    supplier_id  INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    day          TEXT    NOT NULL,
    qty          INTEGER NOT NULL,
    agreed_price REAL    NOT NULL,
    list_price   REAL    NOT NULL,
    outcome      TEXT    NOT NULL DEFAULT 'accepted'      -- accepted | rejected
);

-- ---------------------------------------------------------------------------
-- Draft purchase orders produced by the pipeline
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS purchase_orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sku         TEXT    NOT NULL,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    qty         INTEGER NOT NULL,
    unit_price  REAL    NOT NULL,
    currency    TEXT    NOT NULL DEFAULT 'USD',
    status      TEXT    NOT NULL DEFAULT 'draft',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_inv_wh      ON inventory(warehouse_id);
CREATE INDEX IF NOT EXISTS idx_offers_prod ON supplier_offers(product_id);
CREATE INDEX IF NOT EXISTS idx_offers_sup  ON supplier_offers(supplier_id);
CREATE INDEX IF NOT EXISTS idx_dem_prod    ON demand_history(product_id, warehouse_id);
CREATE INDEX IF NOT EXISTS idx_dem_day     ON demand_history(day);
CREATE INDEX IF NOT EXISTS idx_price_hist  ON negotiation_history(product_id, supplier_id);
