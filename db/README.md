# The database

Everything about the database lives here. The SQL is plain `.sql` files, and
`setup.py` applies them for you.

| File | What it is |
|---|---|
| [`setup.py`](setup.py) | **The setup tool.** Creates the schema and loads the demo data against whatever `DATABASE_URL` points at. |
| [`../database.sql`](../database.sql) | Complete SQLite build (schema + data) in one file. |
| [`../database.postgres.sql`](../database.postgres.sql) | Complete Postgres / Supabase build in one file. |
| [`schema.sql`](schema.sql) | SQLite DDL: tables, keys, indexes. |
| [`seed.sql`](seed.sql) | SQLite demo data, incl. 120 days of demand history. |
| [`schema.postgres.sql`](schema.postgres.sql) | Postgres / Supabase DDL. |
| [`seed.postgres.sql`](seed.postgres.sql) | Postgres / Supabase demo data. |
| [`supabase_rls.sql`](supabase_rls.sql) | Optional Row Level Security policies for Supabase. |
| [`repository.py`](repository.py) | The queries the agents use. Returns plain dicts. |

## Set it up

```bash
python db/setup.py
```

That is the whole thing. It picks the SQL flavour from `DATABASE_URL`, creates
the tables, loads the demo data, and prints the row counts.

### Against local SQLite (the default)

```bash
python db/setup.py
```

### Against Supabase

Put the connection string from the Supabase dashboard
(**Project Settings → Database → Connection string**) into `.env`:

```dotenv
DATABASE_URL=postgresql+psycopg://postgres:<PASSWORD>@db.<PROJECT>.supabase.co:5432/postgres
```

Then:

```bash
python db/setup.py
```

The tool uses `db/schema.postgres.sql` and `db/seed.postgres.sql` automatically.
No pasting SQL into the dashboard.

### Options

| Command | What it does |
|---|---|
| `python db/setup.py` | schema + demo data (idempotent) |
| `python db/setup.py --schema-only` | just the tables |
| `python db/setup.py --seed-only` | just the data |
| `python db/setup.py --reset` | drop the tables first, then rebuild |
| `python db/setup.py --url "postgresql+psycopg://..."` | use a different database for this run |
| `python db/setup.py --emit-sql --dialect postgres` | regenerate `database.postgres.sql` |

### With raw SQL instead

```bash
sqlite3 stock_reorder.db < database.sql
psql "$DATABASE_URL" -f database.postgres.sql
```

Or paste `database.postgres.sql` into the Supabase **SQL Editor**.

## Supabase and Row Level Security

`schema.postgres.sql` does **not** enable RLS. If you also read the data
through the Supabase client SDK or REST API, apply the policies:

```bash
psql "$DATABASE_URL" -f db/supabase_rls.sql
```

That grants read access to signed-in users only. The `anon` role is
deliberately left out - procurement data and supplier pricing should not be
world-readable.

The pipeline itself connects with the Postgres owner role, which bypasses RLS,
so applying the policies does not break the app.

## The tables

| Table | Purpose |
|---|---|
| `warehouses` | Stocking locations |
| `products` | SKUs, unit of measure, safety days |
| `inventory` | On hand, reorder point, on order, daily demand |
| `suppliers` | Reliability, payment terms, currency |
| `supplier_offers` | Price, MOQ, price break, lead time, defect rate, contract expiry |
| `demand_history` | Daily units sold - drives the demand forecast |
| `negotiation_history` | Prices previously agreed - shows list prices are negotiable |
| `purchase_orders` | Draft POs |

## After editing the SQL

Regenerate the combined build scripts so they never drift:

```bash
python db/setup.py --emit-sql --dialect sqlite
python db/setup.py --emit-sql --dialect postgres
```

`tests/test_db.py` fails if either combined file is out of date, checks that
`database.sql` builds a working database on its own, and parses every `.sql`
file with the parser for its dialect - so a Postgres syntax error is caught
without a Postgres server.

## Adding another database engine

1. Add `db/schema.<dialect>.sql` and `db/seed.<dialect>.sql`.
2. Add a branch to `sql_files()` and `dialect_of()` in `setup.py`.
3. Add a combined-output target in `combined_path()`.

The demand statistics are aggregated in Python rather than SQL, so the query
layer stays dialect-free.
