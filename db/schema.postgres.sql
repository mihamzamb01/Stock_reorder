-- ===========================================================================
-- Stock reorder - PostgreSQL / Supabase schema
--
-- Build order:
--   1. schema.postgres.sql   (this file)  -> tables, keys, indexes
--   2. seed.postgres.sql                  -> demo rows
--
-- Or run the combined build:
--   psql "$DATABASE_URL" -f database.postgres.sql
--
-- Or let the project do it for you:
--   python db/setup.py
--
-- Notes for Supabase:
--   * tables live in the `public` schema
--   * Row Level Security is NOT enabled by this script. Run
--     db/supabase_rls.sql afterwards if you also expose the data through the
--     Supabase client SDK or REST API. The app itself connects as the table
--     owner, which bypasses RLS.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- Warehousing
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.warehouses (
    id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code     TEXT NOT NULL UNIQUE,
    name     TEXT NOT NULL,
    region   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS public.products (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,
    unit        TEXT NOT NULL,
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
CREATE TABLE IF NOT EXISTS public.inventory (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id    BIGINT NOT NULL REFERENCES public.products(id)  ON DELETE CASCADE,
    warehouse_id  BIGINT NOT NULL REFERENCES public.warehouses(id) ON DELETE CASCADE,
    on_hand       INTEGER NOT NULL DEFAULT 0,
    reorder_point INTEGER NOT NULL DEFAULT 0,
    on_order      INTEGER NOT NULL DEFAULT 0,
    daily_demand  DOUBLE PRECISION NOT NULL DEFAULT 0,
    unit          TEXT NOT NULL DEFAULT 'pcs',
    CONSTRAINT inventory_product_warehouse_key UNIQUE (product_id, warehouse_id)
);

-- ---------------------------------------------------------------------------
-- Suppliers and their offers
--   price_break_qty / price_break_unit  volume tier price
--   min_order_value                    order must be worth at least this
--   contract_expires_on                end of the current agreement
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.suppliers (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code              TEXT NOT NULL UNIQUE,
    name              TEXT NOT NULL,
    country           TEXT NOT NULL,
    currency          TEXT NOT NULL DEFAULT 'USD',
    reliability_score DOUBLE PRECISION NOT NULL,   -- 0..1 historical on-time + quality
    payment_terms     TEXT NOT NULL,               -- e.g. Net 30
    years_active      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS public.supplier_offers (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id           BIGINT NOT NULL REFERENCES public.products(id)   ON DELETE CASCADE,
    supplier_id          BIGINT NOT NULL REFERENCES public.suppliers(id) ON DELETE CASCADE,
    unit_price           DOUBLE PRECISION NOT NULL,
    currency             TEXT NOT NULL DEFAULT 'USD',
    moq                  INTEGER NOT NULL DEFAULT 1,   -- minimum order qty
    price_break_qty      INTEGER NOT NULL DEFAULT 0,   -- qty needed for tier price
    price_break_unit     DOUBLE PRECISION,             -- unit price at that tier
    lead_time_days       INTEGER NOT NULL DEFAULT 14,
    shipping_per_unit    DOUBLE PRECISION NOT NULL DEFAULT 0,
    min_order_value      DOUBLE PRECISION NOT NULL DEFAULT 0,
    contract_expires_on  DATE,
    defect_rate_pct      DOUBLE PRECISION NOT NULL DEFAULT 0,
    on_time_pct          DOUBLE PRECISION NOT NULL DEFAULT 95,
    incoterms            TEXT NOT NULL DEFAULT 'FOB',
    CONSTRAINT supplier_offers_product_supplier_key UNIQUE (product_id, supplier_id)
);

-- ---------------------------------------------------------------------------
-- Consumption history - drives the demand forecast
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.demand_history (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id   BIGINT NOT NULL REFERENCES public.products(id)   ON DELETE CASCADE,
    warehouse_id BIGINT NOT NULL REFERENCES public.warehouses(id) ON DELETE CASCADE,
    day          DATE NOT NULL,
    units_sold   INTEGER NOT NULL,
    CONSTRAINT demand_history_product_warehouse_day_key UNIQUE (product_id, warehouse_id, day)
);

-- ---------------------------------------------------------------------------
-- Price history - shows what we have previously agreed with a supplier.
-- Used as a signal that the current list price is negotiable.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.negotiation_history (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id   BIGINT NOT NULL REFERENCES public.products(id)   ON DELETE CASCADE,
    supplier_id  BIGINT NOT NULL REFERENCES public.suppliers(id) ON DELETE CASCADE,
    day          DATE NOT NULL,
    qty          INTEGER NOT NULL,
    agreed_price DOUBLE PRECISION NOT NULL,
    list_price   DOUBLE PRECISION NOT NULL,
    outcome      TEXT NOT NULL DEFAULT 'accepted'   -- accepted | rejected
);

-- ---------------------------------------------------------------------------
-- Draft purchase orders produced by the pipeline
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.purchase_orders (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku         TEXT NOT NULL,
    supplier_id BIGINT NOT NULL REFERENCES public.suppliers(id),
    qty         INTEGER NOT NULL,
    unit_price  DOUBLE PRECISION NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'USD',
    status      TEXT NOT NULL DEFAULT 'draft',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_inv_wh        ON public.inventory(warehouse_id);
CREATE INDEX IF NOT EXISTS idx_offers_prod   ON public.supplier_offers(product_id);
CREATE INDEX IF NOT EXISTS idx_offers_sup    ON public.supplier_offers(supplier_id);
CREATE INDEX IF NOT EXISTS idx_dem_prod      ON public.demand_history(product_id, warehouse_id);
CREATE INDEX IF NOT EXISTS idx_dem_day       ON public.demand_history(day);
CREATE INDEX IF NOT EXISTS idx_price_hist    ON public.negotiation_history(product_id, supplier_id);
