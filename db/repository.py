"""SQL access layer for the stock-reorder agents.

Everything the agents need from the database is exposed as plain read-only
query functions that return plain dicts, so agent code never touches SQLAlchemy
or connection handling directly.

Demand statistics are aggregated in Python rather than SQL so the same code
works on SQLite, Postgres and MySQL without dialect-specific functions.
"""

from __future__ import annotations

import os
import statistics
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import Engine, bindparam, create_engine, text

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "stock_reorder.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# Load .env here as well as in config.settings, so DATABASE_URL is picked up
# no matter which module happens to be imported first. load_dotenv never
# overrides a value that is already set in the environment.
load_dotenv(ROOT / ".env")


def database_url(db_path: Path | str | None = None) -> str:
    """Resolve the database URL: explicit arg > DATABASE_URL env > local SQLite.

    A relative SQLite path in DATABASE_URL is resolved against the project
    root, not the current working directory, so the app finds the same
    database whether it is launched from VS Code or anywhere else.
    """
    if db_path is not None:
        return f"sqlite:///{Path(db_path).resolve()}"

    env = os.getenv("DATABASE_URL")
    if env:
        if env.startswith("sqlite:///") and not env.startswith("sqlite:////"):
            raw = env[len("sqlite:///") :]
            if raw and not Path(raw).is_absolute():
                return f"sqlite:///{(ROOT / raw).resolve()}"
        return env

    return f"sqlite:///{DEFAULT_DB_PATH}"


def get_engine(db_path: Path | str | None = None) -> Engine:
    url = database_url(db_path)
    kwargs: dict[str, Any] = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    try:
        return create_engine(url, **kwargs)
    except ModuleNotFoundError as exc:
        raise RuntimeError(_missing_driver_help(url, exc)) from exc


# Drivers each database URL needs, and how to install them.
_DRIVER_HELP = {
    "postgresql+psycopg": ("psycopg", 'pip install "psycopg[binary]"'),
    "postgresql+psycopg2": ("psycopg2", "pip install psycopg2-binary"),
    "postgresql": ("psycopg2", "pip install psycopg2-binary"),
    "mysql+pymysql": ("pymysql", "pip install pymysql"),
    "mysql+mysqlconnector": ("mysql.connector", "pip install mysql-connector-python"),
}


def _missing_driver_help(url: str, exc: Exception) -> str:
    """Explain a missing DB driver instead of letting the traceback show."""
    missing = getattr(exc, "name", None) or str(exc)
    install = ""
    for prefix, (package, command) in _DRIVER_HELP.items():
        if url.startswith(prefix):
            install = f"  {command}"
            missing = package
            break

    venv = ROOT / ".venv" / "Scripts" / "python.exe"
    use_venv = (
        "  .venv\\Scripts\\python.exe db\\setup.py --check\n"
        if venv.exists()
        else ""
    )

    return (
        f"The database driver '{missing}' is not installed for this interpreter.\n\n"
        f"Fix it by either:\n"
        f"  1. installing the driver:{install or '  pip install <driver>'}\n"
        f"  2. using the project virtual environment (recommended):\n"
        f"{use_venv}"
        f"\nCheck which interpreter you are using with:  python -c \"import sys; print(sys.executable)\""
    )


def init_schema(engine: Engine) -> None:
    """Create tables if they do not exist (idempotent)."""
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    with engine.begin() as conn:
        for statement in ddl.split(";"):
            stmt = statement.strip()
            if stmt:
                conn.exec_driver_sql(stmt)


# ---------------------------------------------------------------------------
# Warehouses
# ---------------------------------------------------------------------------
def list_warehouses(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, code, name, region FROM warehouses ORDER BY code")
        ).mappings()
        return [dict(r) for r in rows]


def resolve_warehouse(engine: Engine, code: str | None) -> dict[str, Any]:
    """Resolve a warehouse by code; returns the first warehouse when code is None."""
    warehouses = list_warehouses(engine)
    if not warehouses:
        raise RuntimeError(
            "No warehouses in the database - run `python db/setup.py` first."
        )
    if not code:
        return warehouses[0]
    code = code.strip().upper()
    for w in warehouses:
        if w["code"].upper() == code:
            return w
    raise ValueError(
        f"Unknown warehouse '{code}'. Available: {', '.join(w['code'] for w in warehouses)}"
    )


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------
def _to_date(value: Any) -> date | None:
    """Coerce a date column to a `date`.

    SQLite stores dates as TEXT and returns strings; Postgres uses a real DATE
    column and returns `datetime.date`. This handles both.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text_value = str(value)
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text_value[: len(fmt) + 4], fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text_value[:10])
    except ValueError:
        return None


def fetch_inventory(engine: Engine, warehouse_id: int) -> list[dict[str, Any]]:
    """Inventory rows for a warehouse, enriched with 30/60-day demand statistics."""
    sql = text(
        """
        SELECT p.id            AS product_id,
               p.sku           AS sku,
               p.name          AS product_name,
               p.category      AS category,
               p.unit          AS unit,
               p.safety_days   AS safety_days,
               i.on_hand       AS on_hand,
               i.reorder_point AS reorder_point,
               i.on_order      AS on_order,
               i.daily_demand  AS daily_demand
        FROM inventory i
        JOIN products p ON p.id = i.product_id
        WHERE i.warehouse_id = :wh
        ORDER BY p.sku
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, {"wh": warehouse_id}).mappings()
        inventory = [dict(r) for r in rows]

    stats = _demand_stats(engine, warehouse_id)
    for row in inventory:
        row.update(stats.get(row["product_id"], _empty_demand_stats()))
    return inventory


def _empty_demand_stats() -> dict[str, Any]:
    return {
        "avg_demand_30d": 0.0,
        "avg_demand_60d": 0.0,
        "demand_trend_pct": 0.0,
        "demand_stdev_30d": 0.0,
        "forecast_daily_30d": 0.0,
    }


def _demand_stats(engine: Engine, warehouse_id: int) -> dict[int, dict[str, Any]]:
    """Aggregate demand history in Python (DB-agnostic)."""
    sql = text(
        """
        SELECT h.product_id AS product_id, h.day AS day, h.units_sold AS units_sold
        FROM demand_history h
        WHERE h.warehouse_id = :wh
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, {"wh": warehouse_id}).mappings()
        raw = [dict(r) for r in rows]

    today = date.today()
    by_product: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for r in raw:
        day = _to_date(r["day"])
        if day is None:
            continue
        days_ago = (today - day).days
        by_product[r["product_id"]].append((days_ago, r["units_sold"]))

    out: dict[int, dict[str, Any]] = {}
    for product_id, points in by_product.items():
        recent = [u for d, u in points if d < 30]
        prior = [u for d, u in points if 30 <= d < 60]
        last_60 = [u for d, u in points if d < 60]

        avg_30 = statistics.fmean(recent) if recent else 0.0
        avg_60 = statistics.fmean(last_60) if last_60 else 0.0
        stdev_30 = statistics.pstdev(recent) if len(recent) > 1 else 0.0

        trend_pct = 0.0
        if prior:
            avg_prior = statistics.fmean(prior)
            if avg_prior > 0:
                trend_pct = round((avg_30 - avg_prior) / avg_prior * 100.0, 1)

        # Linear projection of the recent window forward one month.
        projected = avg_30 * (1.0 + (trend_pct / 100.0)) if avg_30 else 0.0
        if projected > 0:
            projected += 0.5 * stdev_30  # safety half-spread

        out[product_id] = {
            "avg_demand_30d": round(avg_30, 2),
            "avg_demand_60d": round(avg_60, 2),
            "demand_trend_pct": trend_pct,
            "demand_stdev_30d": round(stdev_30, 2),
            "forecast_daily_30d": round(projected, 2),
        }
    return out


# ---------------------------------------------------------------------------
# Supplier offers
# ---------------------------------------------------------------------------
def fetch_offers(
    engine: Engine, product_ids: Iterable[int], as_of: date | None = None
) -> list[dict[str, Any]]:
    """All supplier offers for the given products, enriched with supplier info."""
    ids = list(product_ids)
    if not ids:
        return []
    as_of = as_of or date.today()

    sql = text(
        """
        SELECT o.id                  AS offer_id,
               p.id                  AS product_id,
               p.sku                 AS sku,
               p.name                AS product_name,
               p.unit                AS unit,
               s.id                  AS supplier_id,
               s.code                AS supplier_code,
               s.name                AS supplier_name,
               s.country             AS country,
               s.currency            AS supplier_currency,
               s.reliability_score   AS reliability_score,
               s.payment_terms       AS payment_terms,
               s.years_active        AS years_active,
               o.unit_price          AS unit_price,
               o.currency            AS currency,
               o.moq                 AS moq,
               o.price_break_qty     AS price_break_qty,
               o.price_break_unit    AS price_break_unit,
               o.lead_time_days      AS lead_time_days,
               o.shipping_per_unit   AS shipping_per_unit,
               o.min_order_value     AS min_order_value,
               o.contract_expires_on AS contract_expires_on,
               o.defect_rate_pct     AS defect_rate_pct,
               o.on_time_pct         AS on_time_pct,
               o.incoterms           AS incoterms
        FROM supplier_offers o
        JOIN products  p ON p.id = o.product_id
        JOIN suppliers s ON s.id = o.supplier_id
        WHERE o.product_id IN :ids
        ORDER BY p.sku, o.unit_price
        """
    ).bindparams(bindparam("ids", expanding=True))

    with engine.connect() as conn:
        rows = conn.execute(sql, {"ids": ids}).mappings()
        offers = [dict(r) for r in rows]

    history = _negotiation_history(engine, ids)
    for offer in offers:
        expires = _to_date(offer.get("contract_expires_on"))
        # Days until the contract ends - positive means still running.
        offer["contract_days_remaining"] = (
            (expires - as_of).days if expires else None
        )
        h = history.get((offer["product_id"], offer["supplier_id"]))
        if h:
            offer["past_negotiations"] = h["count"]
            offer["last_agreed_price"] = h["last_agreed_price"]
            offer["last_negotiation_day"] = h["last_day"]
            offer["best_historical_price"] = h["best_price"]
        else:
            offer["past_negotiations"] = 0
            offer["last_agreed_price"] = None
            offer["last_negotiation_day"] = None
            offer["best_historical_price"] = None
    return offers


def _negotiation_history(
    engine: Engine, product_ids: list[int]
) -> dict[tuple[int, int], dict[str, Any]]:
    """Past negotiation outcomes per (product, supplier) - the agent's leverage."""
    sql = text(
        """
        SELECT product_id, supplier_id, day, agreed_price, list_price, outcome
        FROM negotiation_history
        WHERE product_id IN :ids
        ORDER BY day
        """
    ).bindparams(bindparam("ids", expanding=True))
    with engine.connect() as conn:
        rows = conn.execute(sql, {"ids": product_ids}).mappings()
        records = [dict(r) for r in rows]

    grouped: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        grouped[(r["product_id"], r["supplier_id"])].append(r)

    out: dict[tuple[int, int], dict[str, Any]] = {}
    for key, items in grouped.items():
        accepted = [i for i in items if i["outcome"] == "accepted"]
        pool = accepted or items
        out[key] = {
            "count": len(items),
            "last_day": items[-1]["day"],
            "last_agreed_price": pool[-1]["agreed_price"],
            "best_price": min(i["agreed_price"] for i in pool),
        }
    return out


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------
def fetch_suppliers(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, code, name, country, currency, reliability_score, "
                "payment_terms, years_active FROM suppliers ORDER BY name"
            )
        ).mappings()
        return [dict(r) for r in rows]


def fetch_demand_summary(engine: Engine, warehouse_id: int) -> dict[str, int]:
    """Units sold per SKU over the last 60 days.

    The date filter is applied in Python so it works on every dialect.
    """
    sql = text(
        """
        SELECT p.sku AS sku,
               h.day AS day,
               h.units_sold AS units_sold
        FROM demand_history h
        JOIN products p ON p.id = h.product_id
        WHERE h.warehouse_id = :wh
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, {"wh": warehouse_id}).mappings()

    today = date.today()
    summary: dict[str, int] = defaultdict(int)
    for row in rows:
        day = _to_date(row["day"])
        if day is None or (today - day).days > 60:
            continue
        summary[row["sku"]] += int(row["units_sold"])
    return dict(sorted(summary.items()))


def effective_unit_price(offer: dict[str, Any], qty: int) -> float:
    """Unit price actually payable for `qty`, honouring the volume price break."""
    price = float(offer["unit_price"])
    break_qty = int(offer.get("price_break_qty") or 0)
    break_price = offer.get("price_break_unit")
    if break_qty and break_price and qty >= break_qty:
        return float(break_price)
    return price


def landed_unit_price(offer: dict[str, Any], qty: int) -> float:
    """Unit price including per-unit shipping."""
    return effective_unit_price(offer, qty) + float(offer.get("shipping_per_unit") or 0.0)
