# Stock Reorder - Multi-Agent Pipeline (LangGraph)

A multi-agent system that watches stock levels, decides what to reorder, checks
the database for who can supply it, and works out the **cheapest way to buy
it**.

Built with [LangGraph](https://langchain-ai.github.io/langgraph/), running
models through **OpenRouter** and **NVIDIA NIM**, reading everything from
**SQL**.

---

## The graph

```
START
  │
  ▼
┌────────────────────┐   deterministic   reads the DB: stock, demand history,
│      db_scan       │──────────────────▶ lead times, supplier prices.
└─────────┬──────────┘                  works out which SKUs are actually short
          ▼
┌────────────────────┐
│  reorder_dispatch  │
└─────────┬──────────┘
          │  Send -> parallel fan-out, one agent per short SKU
          ├──▶ reorder_analyst (NVIDIA)  urgency + order quantity
          ├──▶ reorder_analyst (NVIDIA)
          └──▶ reorder_analyst (NVIDIA)
          ▼
┌────────────────────┐   deterministic   collects the parallel decisions
│    reorder_merge   │──────────────────▶ holds every agent to the same floor
└─────────┬──────────┘
          ▼
┌────────────────────┐   OpenRouter   checks the DB for every supplier offer
│   supplier_scout   │────────────────▶ screens them, rejects MOQ / expiry / quality
└─────────┬──────────┘
          ▼
┌────────────────────┐   OpenRouter   cheapest landed cost, price breaks,
│   cost_optimizer   │────────────────▶ volume splits, MOQ compliance
└─────────┬──────────┘
          ▼
     ╔════════════╗
     ║ cost_gate  ║   deterministic   spend vs budget cap
     ╚═════╤══════╝
     over budget │ retry (max N)
           └──────────────┐
          ▼              │
┌────────────────────┐   │
│       report       │   │   deterministic   the final reorder plan
└─────────┬──────────┘   │
          ▼              │
         END ◀────────────┘
```

### The three agents

| Agent | Provider (default) | What it decides |
|---|---|---|
| `reorder_analyst` | NVIDIA `nvidia/nemotron-3-super-120b-a12b` | One agent per short SKU: how urgent, how much to order |
| `supplier_scout` | OpenRouter `google/gemini-3.5-flash` | Which suppliers can realistically serve each SKU |
| `cost_optimizer` | OpenRouter `openai/gpt-oss-120b` | The cheapest landed-cost purchase plan |

The `reorder_analyst` runs **one agent per SKU, in parallel**, using LangGraph's
`Send` API. Each agent only sees its own SKU, which keeps the prompt focused
and the reasoning sharp.

---

## Core design rule: **LLMs decide, Python computes**

Every agent returns a validated Pydantic object, but **no number that reaches a
purchase order is trusted from the model**. After each stage the plan is
reconciled against the database in `agents/calc.py`:

- quantities are raised to the deterministic floor (under-ordering → stockout)
- quantities are scaled back if the model over-ordered
- MOQ and minimum-order-value are re-enforced
- every unit price, line total and grand total is **recomputed** from the DB
- a supplier MOQ that forces an over-buy is recorded and reported as a warning

The model's job is judgement ("how urgent is this really?", "which supplier?").
Python's job is arithmetic. This is why the pipeline produces a correct purchase
order even when a model hallucinates.

---

## Setup

### 1. Create a virtual environment

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

> macOS / Linux: `.venv/bin/python`, and `source .venv/bin/activate`.

`.venv` is expected in the project root - VS Code is configured to use
`${workspaceFolder}\.venv\Scripts\python.exe`. If it is missing, the Python
extension cannot resolve an interpreter and the integrated terminal dies on
startup.

### 2. Add your API key

A ready-to-fill `.env` is already in the project. Open it and paste your key:

```dotenv
LARP_ROUTER_API_KEY=lrsk-...
```

LarpRouter keys start with `lrsk-` and are created in the
[LarpRouter dashboard](https://larprouter.com). **Bind the key to a DeepSeek or
GLM model pool** when you create it - the pool is fixed per key, the base URL
never changes.

The project is configured for LarpRouter's free-tier routes by default:
`deepseek-v4.1-flash` and `glm-5.3-flash`.

OpenRouter and NVIDIA are still supported - put a key in `OPENROUTER_API_KEY` or
`NVIDIA_API_KEY` and switch a role with `MODEL_<ROLE>_PROVIDER`.

### 3. Build the database

See [Building the database](#building-the-database) below - either run the
Python helper, or execute the two `.sql` files directly.

---

## Building the database

The database is defined entirely in SQL, and the project can build it for you -
no pasting SQL into a dashboard by hand.

```bash
python db/setup.py
```

That picks the SQL flavour from `DATABASE_URL`, creates the tables, loads the
demo data, and prints the row counts.

### The SQL files

| File | What it does |
|---|---|
| [`database.sql`](database.sql) | **Complete SQLite build** - schema + demo data in one file |
| [`database.postgres.sql`](database.postgres.sql) | **Complete Postgres / Supabase build** - same thing |
| [`db/schema.sql`](db/schema.sql) | SQLite DDL: tables, keys, indexes |
| [`db/schema.postgres.sql`](db/schema.postgres.sql) | Postgres / Supabase DDL |
| [`db/seed.sql`](db/seed.sql) | SQLite demo data, incl. 120 days of demand history |
| [`db/seed.postgres.sql`](db/seed.postgres.sql) | Postgres demo data |
| [`db/supabase_rls.sql`](db/supabase_rls.sql) | Optional Supabase Row Level Security policies |

### Local SQLite (default)

```bash
python db/setup.py
```

### Supabase / Postgres

Copy the connection string from **Project Settings → Database → Connection
string** → **URI** into `.env`:

```dotenv
DATABASE_URL=postgresql+psycopg://postgres.<PROJECT_REF>:<PASSWORD>@aws-0-<REGION>.pooler.supabase.com:6543/postgres
```

> **Two things that are easy to get wrong:**
>
> 1. The username is `postgres.<PROJECT_REF>` — with a **dot**. `postgres:<REF>`
>    makes the pooler reply *"no tenant identifier provided"*.
> 2. The scheme must be `postgresql+psycopg://` — SQLAlchemy needs it to
>    select the psycopg3 driver. The dashboard's plain `postgresql://` works
>    too, but then you need psycopg2 instead.
>
> If you would rather not assemble it by hand, let the tool do it — it
> URL-encodes the password as well:
>
> ```bash
> python db/setup.py --supabase <PROJECT_REF> <PASSWORD> --pooler --region eu-west-1
> ```
>
> It prints the ready-to-paste URL.

Then verify the connection before running anything else:

```bash
python db/setup.py --check
```

That connects, prints the row count of every table, and tells you exactly what
to fix if the connection fails (wrong password, missing tenant id, unresolvable
host, SSL, wrong database name). It changes nothing.

### Setup options

| Command | What it does |
|---|---|
| `python db/setup.py --check` | test the connection and verify the tables, changing nothing |
| `python db/setup.py` | schema + demo data (idempotent) |
| `python db/setup.py --reset` | reload the demo data (the seed truncates and re-inserts) |
| `python db/setup.py --drop` | drop the tables first, then rebuild - only for a schema change |
| `python db/setup.py --schema-only` | just the tables |
| `python db/setup.py --seed-only` | just the data |
| `python db/setup.py --url "postgresql+psycopg://..."` | use a different database for this run |
| `python db/setup.py --supabase <ref> <password>` | build a Supabase URL and print it (touches nothing) |
| `python db/setup.py --emit-sql --dialect postgres` | regenerate the combined `.sql` files |
| `python db/setup.py --emit-env-example` | regenerate `.env.example` from `.env`, secrets blanked |

> `--reset` is safe: the seed runs inside one transaction, so if it fails the
> previous data is rolled back rather than left empty. `--drop` is the one that
> can leave the database empty if the rebuild then fails - use it only when the
> schema itself changed.

From VS Code: press <kbd>F5</kbd> and choose *Check the database connection* or
*Set up the database*.

> **Use the project venv.** Running `python` in a terminal picks up the global
> interpreter, which may not have the drivers installed. Both tools tell you
> when that happens. To be safe:
>
> ```bash
> .venv\Scripts\python.exe db\setup.py --check
> ```
>
> or activate it once: `.venv\Scripts\Activate.ps1`

Once connected, load the schema and demo data:

```bash
python db/setup.py
```

The tool selects `schema.postgres.sql` + `seed.postgres.sql` automatically.
`psycopg` is already in `requirements.txt`.

Prefer raw SQL? Either of these work too:

```bash
psql "$DATABASE_URL" -f database.postgres.sql
```

or paste `database.postgres.sql` into the Supabase **SQL Editor**.

### What gets created

| Table | Purpose |
|---|---|
| `warehouses` | Stocking locations |
| `products` | SKUs, unit of measure, safety days |
| `inventory` | On hand, reorder point, on order, daily demand |
| `suppliers` | Reliability, payment terms, currency |
| `supplier_offers` | Price, MOQ, price break, lead time, defect rate, contract expiry |
| `demand_history` | Daily units sold - drives the demand forecast |
| `negotiation_history` | Prices previously agreed - a signal that list prices are negotiable |
| `purchase_orders` | Draft POs, ready for the pipeline to write into |

The `demand_history` rows are generated by a **recursive CTE inside the SQL**,
so no Python and no data export is needed. SQLite version:

```sql
WITH RECURSIVE days (recency, day) AS (
    SELECT 0, date('now')
    UNION ALL
    SELECT recency + 1, date('now', '-' || (recency + 1) || ' days')
    FROM days WHERE recency < 119
)
...
```

Postgres version uses `generate_series` instead. Both produce 3,600 rows with
weekday seasonality, an upward trend and deterministic noise - identical on
every run.

### Supabase and Row Level Security

`schema.postgres.sql` does not enable RLS. If you also read the data through
the Supabase client SDK or REST API, apply the policies:

```bash
psql "$DATABASE_URL" -f db/supabase_rls.sql
```

That grants read access to signed-in users only; the `anon` role is
deliberately excluded, because procurement data and supplier pricing should
not be world-readable. The pipeline connects as the Postgres owner, which
bypasses RLS, so the app keeps working.

### Adding another database engine

1. Add `db/schema.<dialect>.sql` and `db/seed.<dialect>.sql`.
2. Add a branch to `sql_files()` and `dialect_of()` in `db/setup.py`.

The demand statistics are aggregated in Python rather than SQL, so the query
layer stays dialect-free.

---

## Running it

The entry point is **`main.py`**. Press <kbd>F5</kbd> in VS Code and pick a
configuration, or run it from the terminal:

```bash
python main.py                                    # uses the settings in the file
python main.py --warehouse WH-WEST                # override the warehouse
python main.py --max-items 3 --budget 25000       # limit SKUs, cap the spend
python main.py --sku SKU-1005 --sku SKU-1008      # plan for specific SKUs
python main.py --rebuild-db                       # reseed the database first
python main.py --json report.json                 # also dump the result
python main.py --routing                          # show which model handles what
python main.py --dashboard                        # supervision dashboard + execution table
```

### Settings block in `main.py`

The defaults live at the top of the file so you can change them without
touching the command line:

```python
WAREHOUSE = "WH-NORTH"   # WH-NORTH | WH-WEST | WH-CENTRAL
MAX_ITEMS = 5            # how many of the most urgent SKUs to plan for
BUDGET = None            # spend cap in USD, e.g. 25000. None means no cap
FOCUS_SKUS = []          # e.g. ["SKU-1005", "SKU-1008"]
REBUILD_DATABASE = False # True reseeds the demo database before running
WRITE_JSON = None        # e.g. "report.json"
```

### Output

A reorder table (SKU, supplier, quantity, unit price, landed cost, cover,
urgency), an agent trail showing which model handled each stage, the spend
against the cheapest-supplier baseline, any warnings, and a trace line:

```
Trace: REQ-20261026-1204-6286  |  4 step(s), 0 failed  |  105,714 ms  |  8,900 tokens  |  $0.0 est.
```

---

## Observability

The pipeline is instrumented with the observability pattern used for
multi-agent systems: **logs, metrics, traces** around one **correlation ID**,
three **alerts**, and a supervision **dashboard** (`observability.py`).

| Concept | Where it lives |
|---|---|
| Correlation ID | `REQ-YYYYMMDD-HHMM-xxxx`, one per execution, stamped on every step |
| Logs (tools, durations, token cost) | `runs/<correlation_id>.json` - every agent step with model, duration, outcome |
| Metrics (calls, tokens, estimated $) | per-role token counts in the same JSON, totals in the trace line |
| Redaction (never log) | emails, card-shaped digits, key-shaped strings and live secret values are masked before anything is written |
| Alerts | latency / failure rate / cost, evaluated after every run, shown in a panel |
| Executions table | `runs/executions.csv` - opens directly in Google Sheets |
| Dashboard | `dashboard.html`, rebuilt with `python main.py --dashboard` |

### The 3 documented alerts

Set the thresholds in `.env`; a run that breaches one shows an alert panel:

| Alert | Env var | Default | Fires when |
|---|---|---|---|
| Latence | `ALERT_LATENCY_MS` | 30000 | any single agent step exceeds it |
| Taux d'échec | `ALERT_FAILURE_RATE_PCT` | 20 | more than this share of steps fail |
| Coût | `ALERT_COST_LIMIT_USD` | 10 | the estimated LLM cost exceeds it |

Cost estimates need token prices: set `COST_INPUT_PER_MTOK` and
`COST_OUTPUT_PER_MTOK` (per million tokens). On free pools leave them at 0 -
the cost column then reads `$0` and the cost alert stays quiet.

### Dashboard

The supervision dashboard is a **React + shadcn/ui app** (`dashboard/`) that
reads the observability data over a small **stdlib API server**
(`observability_server.py`) — no extra Python dependency, nothing to deploy.

```bash
python main.py --dashboard                 # serve http://localhost:8642
python main.py --dashboard --port 9000     # another port
```

What it shows:

* **KPI cards** - success rate, average latency, estimated cost, total tokens,
  alerting runs
* **LangGraph pipeline panel** - the eight compiled graph nodes
  (db_scan → dispatch → parallel analyst fan-out → merge → scout → optimizer →
  cost gate → report) with the model, duration and SKU chips of the selected
  run, plus the cost-gate retry loop
* **Token usage by role** - calls and prompt/completion tokens per agent
* **The 3 documented alerts** - with the thresholds this backend enforces
* **Executions table** - every run by correlation ID; click a row to inspect
  its full trace
* live refresh every 20 seconds

Rebuild the frontend after editing `dashboard/src`:

```bash
cd dashboard
npm install   # first time only
npm run dev   # dev server with hot reload (proxies /api to port 8642)
npm run build # production bundle into dashboard/dist
```

`python main.py --dashboard` serves the built app; the recipient's zip ships
with `dashboard/dist` pre-built, so **Node is only needed to change the
frontend**.

### Dashboard (CSV / Sheets fallback)

```bash
python main.py --dashboard-sheet
```

Rebuilds the static `dashboard.html` and prints the summary — same KPIs, plain
HTML, no Node. The executions table `runs/executions.csv` also opens directly
in Google Sheets, Grafana or Metabase.

---

## VS Code

The project ships with a `.vscode/` folder, so it works out of the box.

**Press <kbd>F5</kbd>** and pick a configuration:

| Configuration | What it does |
|---|---|
| `Run pipeline - WH-NORTH` | the full pipeline |
| `Run pipeline - WH-WEST (with budget cap)` | pipeline with a spend ceiling |
| `Rebuild demo database, then run` | reseed and run in one go |
| `Set up the database (schema + demo data)` | `db/setup.py` against `DATABASE_URL` |
| `Rebuild the combined .sql files` | regenerate `database.sql` / `database.postgres.sql` |
| `Show model routing` | which model handles which agent |
| `Run tests (pytest)` | the test suite |

**Or use the integrated terminal** (<kbd>Ctrl</kbd>+<kbd>`</kbd>):

```bash
python main.py
```

`.vscode/settings.json` enables persistent terminals, pins the interpreter to
`.venv`, and hides `__pycache__` so you never open a `.pyc` by accident.

---

## The supplier quality floors

Two objective rules decide whether a supplier is even eligible, before the
scout agent ranks what is left:

```dotenv
MAX_DEFECT_RATE_PCT=5.0    # a supplier above this defect rate is never used
MIN_ON_TIME_PCT=75.0       # a supplier below this on-time rate is never used
```

These are business rules, not judgement calls, so they are computed
deterministically in `agents/calc.py`. A supplier that misses either floor is
never offered to the model, however cheap it is.

Raise them for critical components, lower them for consumables. The
deterministic layer also rejects an offer whose contract has expired, or whose
order value falls below the supplier's minimum.

Everything above that is the model's call: which of the *eligible* suppliers to
use, and why.

---

## Testing it

### The one command

```bash
python main.py --test --warehouse WH-NORTH
```

Checks the database, checks every model, then runs the pipeline - and prints a
pass/fail table. Each stage is verified before the next starts, so a failure
tells you exactly which layer broke.

In VS Code: <kbd>F5</kbd> → *TEST EVERYTHING (db + models + pipeline)*.

### Or one stage at a time

```bash
python db/setup.py --check          # is the DB reachable?
python main.py --preflight          # are the API keys good?
python main.py --warehouse WH-NORTH --max-items 4
pytest                              # 31 tests, no keys needed
```

### Other ways to run it

```bash
python main.py --warehouse WH-WEST --max-items 3
python main.py --warehouse WH-NORTH --budget 25000      # exercise the budget gate
python main.py --sku SKU-1005 --sku SKU-1008           # plan for specific SKUs
python main.py --json report.json                      # also dump the result
python main.py --routing                               # which model handles what
```

### The automated tests

The models are stubbed, so this needs no API keys and no database.

| File | Covers |
|---|---|
| `tests/test_pipeline.py` | Agent order, parallel fan-out, reconciliation, budget retry, empty-plan termination |
| `tests/test_llm.py` | Provider endpoint/key wiring, env-var overrides, the `function_calling` → `json_schema` → `json_mode` fallback chain |
| `tests/test_db.py` | Setup is idempotent, `--reset` works, the combined `.sql` files build a working DB, every `.sql` file parses with its dialect's parser, contract dates count the right way round |
| `tests/test_main.py` | `main.py` renders the plan, writes JSON, and reports missing keys clearly |

---

## Changing the models

Every role is routed independently through environment variables - no code
changes needed.

```dotenv
MODEL_REORDER_PROVIDER=larprouter
MODEL_REORDER_NAME=deepseek-v4.1-flash
MODEL_REORDER_TEMPERATURE=0.1
MODEL_REORDER_MAX_TOKENS=2048

MODEL_SUPPLIER_PROVIDER=larprouter
MODEL_SUPPLIER_NAME=glm-5.3-flash
MODEL_SUPPLIER_TEMPERATURE=0.1
MODEL_SUPPLIER_MAX_TOKENS=8192

MODEL_COST_PROVIDER=larprouter
MODEL_COST_NAME=glm-5.3-flash
MODEL_COST_MAX_TOKENS=8192
```

The prefix is the short alias above (`MODEL_COST_*`). The full role name works
too - `MODEL_COST_OPTIMIZER_NAME` is equivalent to `MODEL_COST_NAME` - and the
alias wins if both are set.

### Pointing a role at any platform

Each role resolves its endpoint and its key on its own, so any OpenAI-compatible
platform can back any role:

| Variable | Purpose |
|---|---|
| `MODEL_<ROLE>_PROVIDER` | `openrouter` \| `nvidia` \| `larprouter` \| `custom` |
| `MODEL_<ROLE>_NAME` | the provider's own model id |
| `MODEL_<ROLE>_BASE_URL` | override the endpoint for this role |
| `MODEL_<ROLE>_KEY_ENV` | which env var holds this role's key |
| `MODEL_<ROLE>_API_KEY` | put the key here instead of in a shared variable |
| `PROVIDER_BASE_URL` | endpoint for all roles at once |
| `LLM_API_KEY` | key fallback for all roles |

Resolution order for the endpoint:
`MODEL_<ROLE>_BASE_URL` → `PROVIDER_BASE_URL` → the provider's default.
For the key: `MODEL_<ROLE>_API_KEY` → the role's key variable → `LLM_API_KEY`.

So switching platform is a two-line change in `.env` - no code edit.

### Check a model before you run the pipeline

```bash
python main.py --preflight
```

This sends one tiny request to each agent's model and reports which are
reachable. It costs almost nothing and catches bad keys, retired model ids and
credit problems before a full run.

### LarpRouter

Set up for [LarpRouter](https://larprouter.com) out of the box:

```dotenv
LARP_ROUTER_API_KEY=lrsk-...
```

Keys start with `lrsk-` and are created in the LarpRouter dashboard. **Bind the
key to a DeepSeek or GLM model pool** - the pool is fixed when the key is
created, and the base URL never changes.

The two **free-tier** routes are `deepseek-v4.1-flash` and `glm-5.3-flash`,
which is what the defaults use.

| Route | Input / 1M | Output / 1M | Notes |
|---|---|---|---|
| `deepseek-v4.1-flash` | $0.008 | $0.041 | free tier, 1M context |
| `glm-5.3-flash` | $0.011 | $0.032 | free tier, 1M context |
| `deepseek-v4.1-flash-fast` | $0.010 | $0.040 | faster variant |
| `glm-5.2` | $0.020 | $0.100 | 526K context |
| `glm-5.3` | $0.0325 | $0.100 | 1M context, more reasoning headroom |
| `kimi-k2.7` | $0.030 | $0.150 | 262K context |
| `kimi-k3` | $0.060 | $0.375 | 1M context |
| `claude-opus-4-6` | $0.110 | $0.520 | |

`GET https://larprouter.com/v1/models` with your key returns the catalogue
available to *that* key.

### The other providers

Both are still supported and still configured in `.env`:

| Provider | Endpoint | Key variable |
|---|---|---|
| LarpRouter | `https://larprouter.com/v1` | `LARP_ROUTER_API_KEY` |
| OpenRouter | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| NVIDIA NIM | `https://integrate.api.nvidia.com/v1` | `NVIDIA_API_KEY` |

Known-good models on each:

| Provider | Model | Max tokens | Notes |
|---|---|---|---|
| NVIDIA | `nvidia/nemotron-3-ultra-550b-a55b` | 8192 | strong and fast |
| NVIDIA | `nvidia/nemotron-3.5-lightning-30b-a3b` | 8192 | good, cheaper |
| NVIDIA | `openai/gpt-oss-20b` | 8192 | reasoning model |
| NVIDIA | `nvidia/nemotron-3-super-120b-a12b` | - | **404 on some accounts** |
| OpenRouter | `openai/gpt-oss-120b` | 8192 | reasoning model |
| OpenRouter | `openai/gpt-oss-20b` | 8192 | cheap |
| OpenRouter | `deepseek/deepseek-v4.1-flash` | 8192 | cheap |
| OpenRouter | `google/gemini-3.5-flash-lite` | 2048-4096 | very fast |
| OpenRouter | `google/gemini-3.5-flash` | 512 | needs a paid balance |

> **The OpenRouter credit limit.** OpenRouter rejects a request whose
> `MAX_TOKENS` is larger than your remaining credit can cover, and it checks
> the *worst case* - so a large `MAX_TOKENS` is refused even when the actual
> reply would be small. On a zero balance keep `MAX_TOKENS` modest and prefer
> the cheap models above.

### Rules

- `PROVIDER` is `openrouter`, `nvidia`, `larprouter` or `custom`
- `NAME` is the provider's own model id
- `TEMPERATURE` is optional (blank = provider default)
- `REASONING` is only for reasoning models (`low` / `medium` / `high`)
- `REASONING_BODY` is raw JSON and always wins over `REASONING`

The providers do not share a vocabulary for reasoning, so `REASONING` maps onto
each provider's own mechanism:

| Provider | `REASONING=medium` becomes |
|---|---|
| OpenRouter | `{"reasoning": {"effort": "medium"}}` |
| NVIDIA NIM | `{"chat_template_kwargs": {"enable_thinking": true}}` |
| LarpRouter | nothing is sent - the upstream decides |

If a route needs something else, set `REASONING_BODY` to raw JSON:

```dotenv
MODEL_COST_REASONING_BODY={"chat_template_kwargs":{"reasoning_budget":4096}}
```

> Reasoning tokens count against `MAX_TOKENS`. Keep `MAX_TOKENS` comfortably
> above the reasoning budget, otherwise the model can spend the whole budget
> thinking and return an empty answer.

---

## Project layout

```
main.py                   entry point - settings block at the top
reporting.py              console rendering (tables, panels, the agent trail)
graph.py                  graph wiring, Send fan-out, budget gate
state.py                  LangGraph state + reducers
schemas.py                Pydantic contracts for every stage
llm.py                    provider factory + structured-output fallback chain
observability.py          correlation IDs, run logs, token metrics, alerts
observability_server.py   API + static server for the React dashboard
dashboard/                React + shadcn/ui supervision dashboard (Vite)

agents/
  base.py                 runtime container, audit trail, prompt helpers
  calc.py                 all deterministic arithmetic and reconciliation
  db_scan.py              deterministic DB read -> the short SKUs
  reorder_analyst.py      one agent per SKU (parallel) + merge
  supplier_scout.py       the DB checking agent
  cost_optimizer.py       landed cost and volume splits
  report.py               deterministic final report

db/
  setup.py               the DB setup command (any dialect)
  sql.py                 SQL execution: splitting, dialects, combined builds
  env_example.py         redacts secrets when regenerating .env.example
  schema.sql             SQLite DDL
  schema.postgres.sql    Postgres / Supabase DDL
  seed.sql               SQLite demo data
  seed.postgres.sql      Postgres demo data
  supabase_rls.sql       optional Supabase Row Level Security policies
  repository.py          query functions (plain dicts in, plain dicts out)

database.sql             generated: complete SQLite build
database.postgres.sql    generated: complete Postgres / Supabase build

config/settings.py        env loading + per-role model routing

deliver.py                packages the project for sharing, secrets removed
scan_secrets.py           finds credentials in any file
.github/workflows/ci.yml  tests + lint + package build on every push
tests/                    stubbed-model tests (no API keys needed)
```

### How the pieces fit

The dependency graph is acyclic and layered:

```
config/settings.py   schemas.py          (leaves - no internal dependencies)
      │                   │
      ▼                   ▼
    llm.py ──────► agents/*  ──► graph.py  ──► main.py
                       │                        ▲
                       └── db/repository.py ─────┘
```

`main.py` is the composition root: it builds the models, compiles the graph and
renders the result. The agents know nothing about the CLI, and the graph knows
nothing about the database driver. That is why the whole pipeline is testable
without an API key.

---

## Delivering it

**Do not upload the project folder as it is.** It contains `.env` (your API key
and database password), the virtual environment and the local database.

Use the packaging script instead:

```bash
python deliver.py
```

It stages a clean copy, scans it for your live credentials, aborts if anything
is found, and writes `dist/stock-reorder-agents.zip` (about 100 KB, 43 files).

**In the archive**

- the source, the tests, the SQL files, `.env.example` (redacted)
- `.vscode/` settings are stripped so the recipient has their own editor config

**Not in the archive**

| Excluded | Why |
|---|---|
| `.env` | your API keys and database password |
| `.venv/` | the virtual environment - the recipient makes their own |
| `*.db` | the local database - regenerable with `python db/setup.py` |
| `__pycache__/`, caches | build artefacts |

The recipient then does:

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
copy .env.example .env        # then paste their own keys
.venv\Scripts\python.exe db\setup.py
.venv\Scripts\python.exe main.py --test
```

### Checking for secrets yourself

```bash
python scan_secrets.py            # the working tree
python scan_secrets.py some-folder
```

Findings are graded **CRITICAL** (a byte-for-byte copy of a live secret - must
never be delivered) and **WARNING** (something that looks like a credential -
usually a placeholder or a test fixture).

The CI workflow runs the scanner on every push and fails the build if `.env` is
ever tracked by git.

### If you use git

`.gitignore` already excludes `.env`, `.venv/`, `*.db` and `dist/`. Check
before your first commit:

```bash
git status --ignored | grep -E "\.env|\.db"
git ls-files | grep -E "^\.env$"     # must print nothing
```

---

## Tests

The test suite stubs every model, so **no API keys are required**:

```bash
pytest                                 # all suites
python tests/test_pipeline.py          # graph, SQL, reconciliation, budget retry
python tests/test_llm.py               # provider wiring, structured-output fallbacks
python tests/test_db.py                # seeding, pure-SQL build, repository queries
python tests/test_main.py              # the entry point and its output
```

| File | Covers |
|---|---|
| `tests/test_pipeline.py` | Agent order, parallel fan-out, reconciliation, budget retry, empty-plan termination |
| `tests/test_llm.py` | Provider endpoint/key wiring, env-var overrides, the `json_schema` → `function_calling` → `json_mode` fallback chain |
| `tests/test_db.py` | Setup is idempotent, `--reset` works, the combined `.sql` files build a working DB, every `.sql` file parses with its dialect's parser, the Postgres flavour is distinct from SQLite |
| `tests/test_main.py` | `main.py` renders the plan, writes JSON, and reports missing keys clearly |

---

## Notes on provider support

Different OpenAI-compatible backends support different structured-output
features, so `llm.py` degrades gracefully:

1. `function_calling` (tool-call based schema) - the default, works everywhere
2. `json_schema` (strict structured output)
3. `json_mode` (plain JSON, validated locally)

Each method gets one corrective retry if the response fails Pydantic
validation. If all three fail, a final raw-JSON request is attempted before
the run is aborted with a readable error.

`function_calling` is tried first because it is the only method every provider
here supports reliably, and it is markedly faster than strict `json_schema` on
several models (measured: deepseek-v4.1-flash 3.9s vs 32.2s, nemotron-ultra
13.8s vs 22.9s). The result is validated by Pydantic either way.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Default interpreter path '.\.venv\Scripts\python.exe' could not be resolved` | no `.venv` in the project root | `python -m venv .venv` then install the requirements |
| Terminal closes immediately / `exit code: -65536` | VS Code tried to activate the missing venv and the shell died | fixed once `.venv` exists |
| `Pylance client couldn't create connection to server` | Pylance has no interpreter to run on | fixed once `.venv` exists |
| Editor shows `__init__.cpython-314.pyc` | a compiled file was opened | `__pycache__` is hidden in the explorer |
| `ModuleNotFoundError: No module named 'config'` | running from the wrong folder | run from the project root |
| `ImportError: attempted relative import` | ran `db/seed.py` as a script | use `python -m db.seed` or the launch config |
| `FOREIGN KEY constraint failed` | re-seeding mid-way | fixed - `init-db` is idempotent |
| `Missing API keys` | no key in `.env` | paste it into `.env` |
| `403` / `401` from the provider | bad or expired key | re-check the key in `.env` |
| `the model is not enabled for your account` | the routing points at a model your provider does not serve | run `python main.py --routing` and check the model column |
| `not enough credit` / `402` | the request's `max_tokens` exceeds what your balance covers | lower `MODEL_<ROLE>_MAX_TOKENS` or use a cheaper model |
| **the wrong models are used, but `.env` looks right** | **a stale editor buffer overwrote `.env`** | see below |

### The stale `.env` trap

There are two ways this happens, and both leave the file *looking* correct
while the run uses old settings.

**1. A stale editor buffer.** VS Code's `files.autoSave` writes whatever is in
the editor buffer. If `.env` was open while the file was changed on disk (by a
script, or by someone else), the auto-save writes the **old** buffer back over
the new file - and because `.env` is gitignored there is no version history to
recover it from.

**2. A stale terminal environment.** `.vscode/settings.json` sets
`python.terminal.useEnvFile: true`, so VS Code copies `.env` into the terminal
**when the terminal starts**. `load_dotenv()` never replaces a variable that is
already set - so a terminal opened while `.env` was stale keeps serving the old
values for that whole session, no matter how many times you edit the file.

Two defences are built in:

1. `python main.py --routing` prints the **resolved** routing, where each key
   lives, whether the key is set, when `.env` was last written, and - crucially
   - **any variable the terminal environment is overriding**.
2. If `.env` is **older than `config/settings.py`**, the app prints a
   "Stale config" warning and tells you to revert the file.

To recover:

```bash
# 1. Open a NEW terminal (Ctrl+Shift+` or Terminal -> New Terminal), or
#    run 'Developer: Reload Window' from the Command Palette.
# 2. If .env was overwritten, in VS Code choose File -> Revert File
#    (or press Ctrl+Shift+P -> Developer: Reload Window),
#    then re-paste your API key into .env.
# 3. Check what is actually in effect:
python main.py --routing
```

You can also turn auto-save off for this file:

```jsonc
// .vscode/settings.json
"files.autoSave": "afterDelay",
"[.env]": { "files.autoSave": "off" }
```

> `.env` holds secrets, so it is gitignored. To regenerate the committed
> template from your real `.env` **without leaking the keys**, run:
>
> ```bash
> python db/setup.py --emit-env-example
> ```

---

## Where to go next

- **Human in the loop**: wrap `reorder_dispatch` with `interrupt_before` to let
  a buyer approve each line before the plan is committed.
- **Write the POs**: `purchase_orders` already has the target schema - a small
  node can insert the approved plan.
- **Live supplier prices**: replace `supplier_offers` with an ERP or supplier
  portal API in `db/repository.py`; nothing else changes.
- **Better forecasts**: `demand_history` is already the input - swap the
  30-day average in `db/repository.py` for a proper time-series model.
