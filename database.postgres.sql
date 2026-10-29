-- ===========================================================================
-- Stock reorder - COMPLETE database build (schema + demo data)
--
-- This is schema.postgres.sql and seed.postgres.sql in one file, so you can paste the
-- whole thing into any SQL client: DB Browser for SQLite, DBeaver, MySQL
-- Workbench, pgAdmin, or the sqlite3 CLI.
--
--   psql "$DATABASE_URL" -f database.postgres.sql
--
-- The two halves are also kept separately in db/ for reference:
--   schema.postgres.sql  -> tables, keys, indexes
--   seed.postgres.sql    -> demo data (incl. 120 days of demand history)
--
-- Regenerate this file after editing either of them:
--   python db/setup.py --emit-sql --dialect postgres
-- ===========================================================================

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


-- ===========================================================================
-- Stock reorder - PostgreSQL / Supabase demo data
--
-- Run after schema.postgres.sql:
--   psql "$DATABASE_URL" -f db/seed.postgres.sql
--
-- The demand history is generated with a CTE over generate_series, so no
-- Python and no data export is needed. It is fully deterministic: the same
-- rows every time.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- Clear existing data (children first, so the FK constraints are happy)
-- ---------------------------------------------------------------------------
TRUNCATE TABLE public.purchase_orders       RESTART IDENTITY CASCADE;
TRUNCATE TABLE public.negotiation_history   RESTART IDENTITY CASCADE;
TRUNCATE TABLE public.demand_history        RESTART IDENTITY CASCADE;
TRUNCATE TABLE public.supplier_offers       RESTART IDENTITY CASCADE;
TRUNCATE TABLE public.inventory             RESTART IDENTITY CASCADE;
TRUNCATE TABLE public.products              RESTART IDENTITY CASCADE;
TRUNCATE TABLE public.suppliers             RESTART IDENTITY CASCADE;
TRUNCATE TABLE public.warehouses            RESTART IDENTITY CASCADE;

-- ---------------------------------------------------------------------------
-- Warehouses
-- ---------------------------------------------------------------------------
INSERT INTO public.warehouses (code, name, region) VALUES
  ('WH-NORTH',   'North Distribution Centre',    'EU-North'),
  ('WH-WEST',    'West Coast Fulfillment',       'US-West'),
  ('WH-CENTRAL', 'Central Plant Warehouse',      'EU-Central');

-- ---------------------------------------------------------------------------
-- Products
--   safety_days = extra cover held on top of the supplier lead time
-- ---------------------------------------------------------------------------
INSERT INTO public.products (sku, name, category, unit, safety_days) VALUES
  ('SKU-1001', 'Lithium Cell 18650 3000mAh',  'Batteries',   'pcs',  10),
  ('SKU-1002', 'MCU Controller STM32F407',    'Electronics', 'pcs',  14),
  ('SKU-1003', 'Copper Wire 2.5mm PVC',       'Electrical',  'm',     7),
  ('SKU-1004', 'Deep Groove Bearing 6205-2RS','Mechanical',  'pcs',  10),
  ('SKU-1005', '4-Layer PCB FR4 80x60mm',     'Electronics', 'pcs',  21),
  ('SKU-1006', 'Steel Bracket M8 Galvanised', 'Mechanical',  'pcs',   5),
  ('SKU-1007', 'Heatsink Aluminium 40x40x20','Thermal',     'pcs',   7),
  ('SKU-1008', 'PSU 24V 5A DIN Rail',         'Power',       'pcs',  12),
  ('SKU-1009', 'Silicone Sealant 310ml',      'Consumables', 'tube',  5),
  ('SKU-1010', 'Temp Sensor TMP36 TO-92',     'Electronics', 'pcs',  14);

-- ---------------------------------------------------------------------------
-- Suppliers
--   reliability_score: 0..1 historical on-time + quality
-- ---------------------------------------------------------------------------
INSERT INTO public.suppliers (code, name, country, currency, reliability_score, payment_terms, years_active) VALUES
  ('SUP-001', 'Dongguan Precision Components', 'CN', 'USD', 0.91, 'Net 45',  7),
  ('SUP-002', 'Osaka Denshi K.K.',              'JP', 'USD', 0.96, 'Net 30', 11),
  ('SUP-003', 'Chennai Circuit Works',          'IN', 'USD', 0.74, 'Net 60',  3),
  ('SUP-004', 'Rheinwerk Elektronik GmbH',      'DE', 'EUR', 0.94, 'Net 30',  9),
  ('SUP-005', 'Monterrey Metals SA',            'MX', 'USD', 0.86, 'Net 30',  5),
  ('SUP-006', 'Bangkok Assembly Supply',        'TH', 'USD', 0.79, 'Net 45',  4);

-- ---------------------------------------------------------------------------
-- Stock position
-- ---------------------------------------------------------------------------
INSERT INTO public.inventory (product_id, warehouse_id, on_hand, reorder_point, on_order, daily_demand, unit) VALUES
  -- WH-NORTH : the stressed warehouse
  (1,  1,  620,  900,    0,  41.0, 'pcs'),
  (2,  1,  140,  320,    0,  18.0, 'pcs'),
  (3,  1, 2100, 2500,    0, 122.0, 'm'),
  (4,  1,  880,  700,  200,  34.0, 'pcs'),
  (5,  1,   95,  400,    0,  26.0, 'pcs'),
  (6,  1, 5400, 4000, 1000, 210.0, 'pcs'),
  (7,  1,  410,  750,    0,  46.0, 'pcs'),
  (8,  1,   60,  150,    0,  11.0, 'pcs'),
  (9,  1,  260,  400,    0,  29.0, 'tube'),
  (10, 1, 1300, 1100,    0,  52.0, 'pcs'),
  -- WH-WEST : the worst positioned warehouse
  (1,  2,  410,  600,    0,  28.0, 'pcs'),
  (2,  2,   75,  200,    0,  12.0, 'pcs'),
  (3,  2, 1500, 1800,  500,  88.0, 'm'),
  (4,  2,  420,  450,    0,  19.0, 'pcs'),
  (5,  2,   40,  260,    0,  17.0, 'pcs'),
  (6,  2, 3200, 2600,    0, 140.0, 'pcs'),
  (7,  2,  180,  480,    0,  30.0, 'pcs'),
  (8,  2,   22,   95,    0,   8.0, 'pcs'),
  (9,  2,  150,  260,    0,  19.0, 'tube'),
  (10, 2,  700,  800,    0,  33.0, 'pcs'),
  -- WH-CENTRAL : mostly healthy, a couple of lines still need attention
  (1,  3, 1500, 1200,    0,  52.0, 'pcs'),
  (2,  3,  600,  500,  300,  21.0, 'pcs'),
  (3,  3, 3200, 2800,    0, 140.0, 'm'),
  (4,  3, 1100,  900,    0,  42.0, 'pcs'),
  (5,  3,  480,  520,    0,  30.0, 'pcs'),
  (6,  3, 6100, 5000,    0, 250.0, 'pcs'),
  (7,  3,  900,  950,    0,  55.0, 'pcs'),
  (8,  3,  210,  200,    0,  14.0, 'pcs'),
  (9,  3,  520,  520,    0,  35.0, 'tube'),
  (10, 3, 1900, 1500,    0,  61.0, 'pcs');

-- ---------------------------------------------------------------------------
-- Supplier offers
--   unit_price, moq, break_qty, break_price, lead_days, shipping_per_unit,
--   min_order_value, defect_pct, on_time_pct, contract_days, incoterms
-- ---------------------------------------------------------------------------
INSERT INTO public.supplier_offers
  (product_id, supplier_id, unit_price, currency, moq, price_break_qty, price_break_unit,
   lead_time_days, shipping_per_unit, min_order_value, contract_expires_on,
   defect_rate_pct, on_time_pct, incoterms)
VALUES
  -- SKU-1001 Lithium Cell
  (1, 1, 4.85, 'USD',  500, 2000, 4.35, 28, 0.12, 1500, CURRENT_DATE +  45, 1.8, 88, 'FOB'),
  (1, 2, 5.40, 'USD',  200, 1000, 5.05, 14, 0.45,  900, CURRENT_DATE + 120, 0.4, 97, 'DDP'),
  (1, 3, 4.10, 'USD', 1000, 5000, 3.75, 42, 0.08, 2500, CURRENT_DATE +  20, 4.2, 72, 'FOB'),
  (1, 6, 4.55, 'USD',  750, 3000, 4.20, 33, 0.10, 1800, CURRENT_DATE +  60, 2.6, 79, 'FOB'),
  -- SKU-1002 MCU Controller
  (2, 1, 6.20, 'USD',  300, 1200, 5.80, 30, 0.15, 2000, CURRENT_DATE +  75, 2.1, 85, 'FOB'),
  (2, 2, 7.90, 'USD',  100,  500, 7.40, 16, 0.55, 1200, CURRENT_DATE + 180, 0.3, 98, 'DDP'),
  (2, 3, 5.10, 'USD',  800, 4000, 4.60, 48, 0.09, 3000, CURRENT_DATE +  15, 5.0, 70, 'FOB'),
  (2, 4, 7.55, 'EUR',  150,  750, 7.10, 12, 0.60, 1500, CURRENT_DATE + 200, 0.6, 96, 'DDP'),
  -- SKU-1003 Copper Wire
  (3, 1, 2.35, 'USD', 1000, 5000, 2.05, 32, 0.05, 2000, CURRENT_DATE +  90, 3.1, 84, 'FOB'),
  (3, 5, 2.60, 'USD',  800, 4000, 2.30, 22, 0.09, 2500, CURRENT_DATE + 120, 1.4, 90, 'FOB'),
  (3, 4, 3.05, 'EUR',  400, 2000, 2.85, 10, 0.22, 1800, CURRENT_DATE + 150, 0.7, 97, 'DDP'),
  (3, 6, 2.20, 'USD', 1200, 6000, 1.95, 40, 0.04, 2200, CURRENT_DATE +  25, 4.4, 71, 'FOB'),
  -- SKU-1004 Bearing
  (4, 2, 3.80, 'USD',  400, 2000, 3.45, 18, 0.20, 1500, CURRENT_DATE + 150, 0.5, 97, 'DDP'),
  (4, 3, 2.95, 'USD',  900, 4500, 2.55, 50, 0.06, 2200, CURRENT_DATE +  20, 5.5, 68, 'FOB'),
  (4, 1, 3.35, 'USD',  700, 3500, 2.95, 34, 0.10, 1800, CURRENT_DATE +  70, 2.4, 83, 'FOB'),
  (4, 5, 3.10, 'USD',  600, 3000, 2.80, 26, 0.12, 1600, CURRENT_DATE + 100, 1.8, 89, 'FOB'),
  -- SKU-1005 PCB
  (5, 1, 8.90, 'USD',  200, 1000, 8.20, 35, 0.25, 2500, CURRENT_DATE +  60, 2.8, 82, 'FOB'),
  (5, 3, 7.40, 'USD',  600, 3000, 6.70, 52, 0.12, 3000, CURRENT_DATE +  18, 5.8, 69, 'FOB'),
  (5, 6, 8.10, 'USD',  500, 2500, 7.55, 38, 0.18, 2400, CURRENT_DATE +  55, 3.2, 78, 'FOB'),
  (5, 4, 11.20,'EUR',  100,  500,10.60,  9, 0.70, 1800, CURRENT_DATE + 210, 0.5, 96, 'DDP'),
  -- SKU-1006 Steel Bracket
  (6, 5, 1.45, 'USD', 1500, 6000, 1.28, 24, 0.03, 2000, CURRENT_DATE + 110, 1.6, 89, 'FOB'),
  (6, 1, 1.32, 'USD', 2000, 8000, 1.15, 36, 0.03, 2500, CURRENT_DATE +  65, 3.0, 82, 'FOB'),
  (6, 6, 1.18, 'USD', 2500,10000, 1.02, 45, 0.02, 2600, CURRENT_DATE +  30, 4.6, 73, 'FOB'),
  -- SKU-1007 Heatsink
  (7, 1, 2.60, 'USD',  600, 3000, 2.30, 33, 0.07, 1800, CURRENT_DATE +  70, 2.5, 84, 'FOB'),
  (7, 5, 2.95, 'USD',  500, 2500, 2.65, 21, 0.11, 1600, CURRENT_DATE + 130, 1.2, 91, 'FOB'),
  (7, 3, 2.20, 'USD', 1000, 5000, 1.95, 55, 0.05, 2400, CURRENT_DATE +  18, 5.6, 67, 'FOB'),
  -- SKU-1008 PSU
  (8, 2, 24.50,'USD',  100,  500,23.10, 20, 0.90, 2500, CURRENT_DATE + 160, 0.6, 97, 'DDP'),
  (8, 4, 26.80,'EUR',   80,  400,25.40, 11, 1.10, 2200, CURRENT_DATE + 190, 0.4, 95, 'DDP'),
  (8, 1, 21.90,'USD',  250, 1000,20.50, 34, 0.35, 2800, CURRENT_DATE +  80, 2.7, 81, 'FOB'),
  (8, 3, 18.40,'USD',  500, 2000,16.90, 60, 0.18, 3000, CURRENT_DATE +  15, 6.1, 65, 'FOB'),
  -- SKU-1009 Silicone Sealant
  (9, 6, 4.20, 'USD',  200, 1000, 3.70, 20, 0.35, 1000, CURRENT_DATE +  80, 1.9, 80, 'FOB'),
  (9, 1, 4.95, 'USD',  150,  750, 4.40, 30, 0.28, 1100, CURRENT_DATE + 120, 1.1, 88, 'FOB'),
  (9, 4, 5.80, 'EUR',  100,  500, 5.35,  9, 0.55,  900, CURRENT_DATE + 200, 0.3, 98, 'DDP'),
  -- SKU-1010 Temp Sensor
  (10, 2, 3.10,'USD',  500, 2500, 2.80, 17, 0.22, 1200, CURRENT_DATE + 170, 0.5, 97, 'DDP'),
  (10, 4, 3.65,'EUR',  300, 1500, 3.35, 10, 0.35, 1300, CURRENT_DATE + 220, 0.3, 96, 'DDP'),
  (10, 3, 2.45,'USD',  900, 4500, 2.20, 49, 0.09, 2200, CURRENT_DATE +  22, 5.2, 69, 'FOB'),
  (10, 1, 2.90,'USD',  700, 3500, 2.60, 33, 0.12, 1600, CURRENT_DATE +  95, 2.3, 85, 'FOB');

-- ---------------------------------------------------------------------------
-- Demand history - 120 days, generated in pure SQL
--
--   * weekday/weekend seasonality (weekends run at ~45%)
--   * a gentle upward trend of ~0.22% per day
--   * deterministic pseudo-random noise so the forecast has something to chew on
--
-- The noise term is a simple integer hash of (product, warehouse, day) so the
-- result is identical on every run.
-- ---------------------------------------------------------------------------
INSERT INTO public.demand_history (product_id, warehouse_id, day, units_sold)
WITH days AS (
    SELECT (CURRENT_DATE - g) AS day, g AS recency
    FROM generate_series(0, 119) AS g
),
grid AS (
    SELECT i.product_id                AS product_id,
           i.warehouse_id              AS warehouse_id,
           i.daily_demand              AS base,
           d.recency                   AS recency,
           d.day                       AS day,
           CASE WHEN EXTRACT(DOW FROM d.day) IN (0, 6)
                THEN 0.45 ELSE 1.0 END AS weekday_factor
    FROM days d
    CROSS JOIN public.inventory i
)
SELECT product_id,
       warehouse_id,
       day::date,
       GREATEST(0, CAST(ROUND(
              base * weekday_factor
                   * (1.0 + 0.0022 * (120 - recency))
                   * (1.0 + 0.16 * ((mod(product_id * 37 + warehouse_id * 11 + recency * 7, 23) - 11.0) / 11.0))
           ) AS INTEGER)) AS units_sold
FROM grid;

-- ---------------------------------------------------------------------------
-- Price history - what we have previously agreed with each supplier.
-- Shows that several current list prices are above what we have paid before.
-- ---------------------------------------------------------------------------
INSERT INTO public.negotiation_history (product_id, supplier_id, day, qty, agreed_price, list_price, outcome) VALUES
  (1,  1, CURRENT_DATE -  45,  2000, 4.42, 4.85, 'accepted'),
  (1,  1, CURRENT_DATE - 110,  1000, 4.55, 4.85, 'accepted'),
  (1,  2, CURRENT_DATE -  60,   500, 5.10, 5.40, 'accepted'),
  (2,  4, CURRENT_DATE -  45,   400, 7.10, 7.55, 'accepted'),
  (2,  2, CURRENT_DATE -  90,   250, 7.60, 7.90, 'accepted'),
  (3,  1, CURRENT_DATE -  55,  5000, 2.15, 2.35, 'accepted'),
  (3,  6, CURRENT_DATE - 120,  6000, 2.02, 2.20, 'accepted'),
  (4,  3, CURRENT_DATE -  70,  4500, 2.70, 2.95, 'accepted'),
  (4,  2, CURRENT_DATE -  40,  1000, 3.52, 3.80, 'accepted'),
  (5,  3, CURRENT_DATE -  35,  3000, 6.90, 7.40, 'accepted'),
  (5,  1, CURRENT_DATE -  85,  1000, 8.35, 8.90, 'accepted'),
  (6,  1, CURRENT_DATE -  50,  8000, 1.18, 1.32, 'accepted'),
  (6,  6, CURRENT_DATE -  95, 10000, 1.05, 1.18, 'accepted'),
  (7,  3, CURRENT_DATE -  65,  5000, 2.00, 2.20, 'accepted'),
  (7,  5, CURRENT_DATE -  30,  2500, 2.70, 2.95, 'accepted'),
  (8,  3, CURRENT_DATE -  80,  2000, 17.10, 18.40, 'accepted'),
  (8,  2, CURRENT_DATE -  25,   300, 23.80, 24.50, 'accepted'),
  (9,  1, CURRENT_DATE -  45,   750, 4.55, 4.95, 'accepted'),
  (9,  6, CURRENT_DATE - 100,  1000, 3.85, 4.20, 'accepted'),
  (10, 3, CURRENT_DATE -  70,  4500, 2.25, 2.45, 'accepted'),
  (10, 1, CURRENT_DATE -  40,  3500, 2.68, 2.90, 'accepted'),
  (3,  4, CURRENT_DATE - 120,  2000, 2.90, 3.05, 'rejected'),
  (5,  3, CURRENT_DATE - 140,  2500, 7.60, 7.40, 'rejected');
