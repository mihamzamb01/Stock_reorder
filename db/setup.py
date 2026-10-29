"""Database setup for the stock-reorder project.

    python db/setup.py                     schema + demo data (idempotent)
    python db/setup.py --check             test the connection, verify the tables
    python db/setup.py --reset             reload the demo data
    python db/setup.py --drop              drop the tables, then rebuild
    python db/setup.py --schema-only       just the tables
    python db/setup.py --seed-only         just the data
    python db/setup.py --emit-sql          regenerate the combined .sql files
    python db/setup.py --emit-env-example  regenerate .env.example, secrets blanked
    python db/setup.py --supabase <ref> <password>   build a Supabase URL

The SQL machinery lives in `db/sql.py`; this module is the command-line shell
around it.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import Engine, text

if __package__ in (None, ""):
    # Allow `python db/setup.py` as well as `python -m db.setup`.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from config.settings import interpreter_hint
    from db.repository import get_engine
    from db.sql import (
        DIALECTS,
        dialect_of,
        emit_combined,
        run_sql_file,
        sql_files,
    )
else:
    from config.settings import interpreter_hint

    from .repository import get_engine
    from .sql import (
        DIALECTS,
        dialect_of,
        emit_combined,
        run_sql_file,
        sql_files,
    )

ROOT = Path(__file__).resolve().parent.parent

EXPECTED_TABLES = [
    "warehouses",
    "products",
    "suppliers",
    "inventory",
    "supplier_offers",
    "demand_history",
    "negotiation_history",
    "purchase_orders",
]


# ---------------------------------------------------------------------------
# Supabase URL
# ---------------------------------------------------------------------------
def build_supabase_url(
    project_ref: str,
    password: str,
    *,
    pooler: bool = False,
    region: str | None = None,
    port: int | None = None,
    database: str = "postgres",
) -> str:
    """Build a Supabase connection string from the pieces in the dashboard.

    The password is URL-encoded, because Supabase passwords often contain
    characters that would otherwise break the URL.

    The username differs by mode, and getting it wrong is the single most
    common cause of a failed connection:

    * pooler  -> ``postgres.<project_ref>``  (the pooler needs the tenant id,
      otherwise the server replies "no tenant identifier provided")
    * direct  -> ``postgres``

        >>> build_supabase_url("abcdefgh", "p@ss word")
        'postgresql+psycopg://postgres:p%40ss%20word@db.abcdefgh.supabase.co:5432/postgres'
        >>> build_supabase_url("abcdefgh", "p@ss word", pooler=True)
        'postgresql+psycopg://postgres.abcdefgh:p%40ss%20word@aws-0-us-east-1.pooler.supabase.com:6543/postgres'
    """
    from urllib.parse import quote

    encoded = quote(password, safe="")
    if pooler:
        # Session mode pooler - also required on IPv6-only networks.
        host = f"aws-0-{region or 'us-east-1'}.pooler.supabase.com"
        port = port or 6543
        user = f"postgres.{project_ref}"
    else:
        host = f"db.{project_ref}.supabase.co"
        port = port or 5432
        user = "postgres"
    return f"postgresql+psycopg://{user}:{encoded}@{host}:{port}/{database}"


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------
def init_schema(engine: Engine) -> None:
    """Create the tables if they do not exist (idempotent)."""
    dialect = dialect_of(engine)
    schema_path, _ = sql_files(dialect)
    run_sql_file(engine, schema_path)


def seed_database(db_path: Path | str | None = None) -> str:
    """Create the schema and load the demo data. Returns the target URL."""
    engine = get_engine(db_path)
    init_schema(engine)
    _, seed_path = sql_files(dialect_of(engine))
    run_sql_file(engine, seed_path)
    return engine.url.render_as_string(hide_password=True)


def reset_database(engine: Engine) -> None:
    """Drop every table so the next build starts from scratch.

    Only needed when the *schema* changes. The seed file truncates and
    re-inserts on its own, so a plain re-run is already a data reset - and
    because it happens inside one transaction, a failure rolls back and the
    previous data survives.
    """
    dialect = dialect_of(engine)
    tables = [
        "purchase_orders",
        "negotiation_history",
        "demand_history",
        "supplier_offers",
        "inventory",
        "products",
        "suppliers",
        "warehouses",
    ]
    with engine.begin() as conn:
        if dialect == "postgres":
            conn.exec_driver_sql(
                "DROP TABLE IF EXISTS " + ", ".join(f"public.{t}" for t in tables) + " CASCADE"
            )
        else:
            # SQLite's DROP TABLE takes one name at a time.
            for table in tables:
                conn.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")


def table_counts(engine: Engine) -> dict[str, int]:
    """Row count per table, for a quick sanity check after setup."""
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        for name in EXPECTED_TABLES:
            try:
                counts[name] = conn.execute(text(f"select count(*) from {name}")).scalar_one()
            except Exception:
                counts[name] = -1
    return counts


def check_database(engine: Engine) -> int:
    """Verify the connection and that the schema is in place. Changes nothing.

    Returns a process exit code: 0 when everything is ready, 1 otherwise.
    """
    dialect = dialect_of(engine)
    print(f"Dialect  : {dialect}")

    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("select 1")
    except Exception as exc:
        print("\nCould not connect.\n")
        print(_connection_help(dialect, exc))
        return 1

    print("Connected.")

    missing: list[str] = []
    with engine.connect() as conn:
        for name in EXPECTED_TABLES:
            try:
                count = conn.exec_driver_sql(f"select count(*) from {name}").scalar_one()
            except Exception:
                missing.append(name)
                continue
            print(f"  {name:22} {count:>6}")

    if missing:
        print(f"\nMissing tables: {', '.join(missing)}")
        print("Create them with:  python db/setup.py --schema-only")
        return 1

    print("\nEverything is in place.")
    print("Next:  python main.py")
    return 0


def _connection_help(dialect: str, exc: Exception) -> str:
    """Turn a connection failure into something actionable."""
    text = f"{type(exc).__name__}: {exc}"
    hints: list[str] = []
    lowered = text.lower()

    dns_markers = (
        "could not translate host name",
        "name or service not known",
        "failed to resolve host",
        "getaddrinfo",
        "nodename nor servname",
        "temporary failure in name resolution",
    )
    if any(marker in lowered for marker in dns_markers):
        hints.append(
            "The host name could not be resolved. Two common causes:\n"
            "  * your network is IPv6-only - use the pooler host instead:\n"
            "      aws-0-<region>.pooler.supabase.com:6543 (session mode)\n"
            "    try:  python db/setup.py --supabase <ref> <password> --pooler\n"
            "  * the project ref is wrong - copy it from Settings -> Database."
        )
    if "no tenant identifier" in lowered or "sni_hostname" in lowered:
        hints.append(
            "The pooler needs the tenant id in the username. Use\n"
            "  postgres.<PROJECT_REF>   as the username, not plain 'postgres'.\n"
            "  try:  python db/setup.py --supabase <ref> <password> --pooler --region <region>"
        )
    if "circuitbreaker" in lowered or "too many authentication failures" in lowered:
        hints.append(
            "The pooler has temporarily blocked this client after too many failed\n"
            "  logins. Wait a minute or so and try again. If it keeps happening, the\n"
            "  password is wrong."
        )
    if "password authentication failed" in lowered or "fe_sendauth: no password" in lowered:
        hints.append(
            "Authentication failed. The password is wrong, or it has changed since it\n"
            "  was copied. Reset it in Supabase (Settings -> Database), then rebuild the\n"
            "  URL so it is encoded correctly:\n"
            "  try:  python db/setup.py --supabase <ref> <new-password> --pooler --region <region>"
        )
    if "ssl" in lowered and ("required" in lowered or "verify" in lowered):
        hints.append("SSL is required. Append ?sslmode=require to the connection string.")
    if "timeout" in lowered or "timed out" in lowered or "no route to host" in lowered:
        hints.append(
            "Connection timed out. Check the host, the port and your firewall - the\n"
            "  Supabase direct connection is port 5432, the pooler is 6543."
        )
    if "does not exist" in lowered and "database" in lowered:
        hints.append(
            "The database name in the URL is wrong. For Supabase it is usually 'postgres'."
        )

    if dialect == "postgres":
        hints.append(
            "Expected form:\n"
            "  postgresql+psycopg://postgres.<PROJECT_REF>:<PASSWORD>@"
            "aws-0-<REGION>.pooler.supabase.com:6543/postgres"
        )
    else:
        hints.append("Expected form: sqlite:///./stock_reorder.db")

    if not hints:
        hints.append("Check the host, port, credentials and SSL settings in DATABASE_URL.")

    return text + "\n\n" + "\n\n".join(hints)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Set up the stock-reorder database (schema + demo data)"
    )
    parser.add_argument("--db", default=None, help="target .db file path (SQLite only)")
    parser.add_argument(
        "--dialect",
        choices=DIALECTS,
        default=None,
        help="SQL flavour to use (default: detect from DATABASE_URL)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="test the connection and verify the schema, without changing anything",
    )
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="only create the tables",
    )
    parser.add_argument("--seed-only", action="store_true", help="only load the demo data")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="reload the demo data (the seed truncates and re-inserts)",
    )
    parser.add_argument(
        "--drop",
        action="store_true",
        help="drop the tables first, then rebuild them (use when the schema changed)",
    )
    parser.add_argument(
        "--emit-sql",
        action="store_true",
        help="regenerate the combined .sql build script and exit",
    )
    parser.add_argument(
        "--emit-env-example",
        action="store_true",
        help="regenerate .env.example from .env, with every secret blanked",
    )
    parser.add_argument(
        "--supabase",
        nargs=2,
        metavar=("PROJECT_REF", "PASSWORD"),
        help="build a Supabase URL from the project ref and password, then print it",
    )
    parser.add_argument(
        "--pooler",
        action="store_true",
        help="with --supabase, use the pooler host (needed on IPv6-only networks)",
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="Supabase region for the pooler host, e.g. eu-west-1 (default: us-east-1)",
    )
    parser.add_argument("--url", default=None, help="override DATABASE_URL for this run")
    args = parser.parse_args(argv)

    if args.emit_sql:
        dialect = args.dialect or "sqlite"
        path = emit_combined(dialect)
        print(f"Wrote {path}")
        return 0

    if args.emit_env_example:
        if __package__ in (None, ""):
            from db.env_example import emit_env_example
        else:
            from .env_example import emit_env_example

        path = emit_env_example()
        print(f"Wrote {path} (secrets blanked)")
        return 0

    if args.supabase:
        url = build_supabase_url(
            args.supabase[0],
            args.supabase[1],
            pooler=args.pooler,
            region=args.region,
        )
        print(f"Built    : {url}")
        print()
        print("Put this in .env as DATABASE_URL, then run:")
        print("  python db/setup.py --check")
        print("  python main.py --test")
        return 0

    if args.url:
        os.environ["DATABASE_URL"] = args.url

    hint = interpreter_hint()
    if hint:
        print(f"Note: {hint}\n")

    try:
        engine = get_engine(args.db)
    except RuntimeError as exc:
        print("\nCould not start the database engine.\n")
        print(exc)
        return 1

    dialect = args.dialect or dialect_of(engine)
    schema_path, seed_path = sql_files(dialect)

    if dialect == "sqlite":
        target = engine.url.database or ":memory:"
    else:
        target = engine.url.render_as_string(hide_password=True)

    if args.check:
        print(f"Target   : {target}")
        return check_database(engine)

    print(f"Target   : {target}")
    print(f"Dialect  : {dialect}")

    if args.drop:
        print("Dropping the tables...")
        reset_database(engine)

    if not args.seed_only:
        print(f"Creating schema from {schema_path.name}...")
        run_sql_file(engine, schema_path)

    if not args.schema_only:
        print(f"Loading demo data from {seed_path.name}...")
        run_sql_file(engine, seed_path)

    print("\nRow counts:")
    for name, count in table_counts(engine).items():
        print(f"  {name:22} {count:>6}")

    print("\nDone. The database is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
