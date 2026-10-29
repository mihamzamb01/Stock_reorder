-- ===========================================================================
-- Stock reorder - Supabase Row Level Security (optional)
--
-- Run this AFTER db/schema.postgres.sql if you also read the data through the
-- Supabase client SDK or the REST API.
--
-- The pipeline itself connects with the Postgres owner role (the connection
-- string from the Supabase dashboard), which bypasses RLS - so applying this
-- file does NOT break the app.
--
-- By default only signed-in users (`authenticated`) can read. The `anon` role
-- is deliberately NOT granted access: procurement and supplier pricing should
-- not be world-readable. If you do want public read access, add the matching
-- policies for the `anon` role.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- Enable RLS on every table
-- ---------------------------------------------------------------------------
ALTER TABLE public.warehouses          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.products           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.inventory          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.suppliers          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.supplier_offers    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.demand_history     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.negotiation_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.purchase_orders    ENABLE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------------
-- Read access for signed-in users
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS "read warehouses"       ON public.warehouses;
CREATE POLICY "read warehouses"       ON public.warehouses        FOR SELECT TO authenticated USING (true);

DROP POLICY IF EXISTS "read products"        ON public.products;
CREATE POLICY "read products"        ON public.products         FOR SELECT TO authenticated USING (true);

DROP POLICY IF EXISTS "read inventory"       ON public.inventory;
CREATE POLICY "read inventory"       ON public.inventory        FOR SELECT TO authenticated USING (true);

DROP POLICY IF EXISTS "read suppliers"       ON public.suppliers;
CREATE POLICY "read suppliers"       ON public.suppliers        FOR SELECT TO authenticated USING (true);

DROP POLICY IF EXISTS "read supplier_offers" ON public.supplier_offers;
CREATE POLICY "read supplier_offers" ON public.supplier_offers  FOR SELECT TO authenticated USING (true);

DROP POLICY IF EXISTS "read demand_history"  ON public.demand_history;
CREATE POLICY "read demand_history"  ON public.demand_history   FOR SELECT TO authenticated USING (true);

DROP POLICY IF EXISTS "read negotiation_history" ON public.negotiation_history;
CREATE POLICY "read negotiation_history" ON public.negotiation_history FOR SELECT TO authenticated USING (true);

DROP POLICY IF EXISTS "read purchase_orders" ON public.purchase_orders;
CREATE POLICY "read purchase_orders" ON public.purchase_orders   FOR SELECT TO authenticated USING (true);

-- ---------------------------------------------------------------------------
-- Optional: let signed-in users write draft purchase orders
-- (comment out if only the pipeline should ever write them)
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS "insert purchase_orders" ON public.purchase_orders;
CREATE POLICY "insert purchase_orders" ON public.purchase_orders
    FOR INSERT TO authenticated WITH CHECK (true);

DROP POLICY IF EXISTS "update purchase_orders" ON public.purchase_orders;
CREATE POLICY "update purchase_orders" ON public.purchase_orders
    FOR UPDATE TO authenticated USING (true) WITH CHECK (true);

-- ---------------------------------------------------------------------------
-- Verify
--   SELECT tablename, rowsecurity FROM pg_tables WHERE schemaname = 'public';
-- ===========================================================================
