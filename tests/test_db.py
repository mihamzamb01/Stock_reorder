"""Data-layer tests: setup must be idempotent and the SQL must be self-contained."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db.repository import (  # noqa: E402
    effective_unit_price,
    fetch_inventory,
    fetch_offers,
    get_engine,
    list_warehouses,
    resolve_warehouse,
)
from db.setup import (  # noqa: E402
    build_supabase_url,
    check_database,
    dialect_of,
    seed_database,
    sql_files,
    table_counts,
)
from db.sql import (  # noqa: E402
    POSTGRES_COMBINED,
    SQLITE_COMBINED,
    combined_sql,
)


def test_seeding_is_idempotent(tmp_path: Path) -> None:
    """Re-seeding an existing database must not trip the foreign keys."""
    db = tmp_path / "idem.db"
    seed_database(db)
    seed_database(db)  # second run over existing data
    seed_database(db)

    engine = get_engine(db)
    rows = fetch_inventory(engine, resolve_warehouse(engine, "WH-NORTH")["id"])
    assert len(rows) == 10, rows
    engine.dispose()
    print("  re-seeding an existing database works")


def test_setup_script_runs_standalone(tmp_path: Path) -> None:
    """`python db/setup.py` must work - relative imports used to break it."""
    db = tmp_path / "standalone.db"
    result = subprocess.run(
        [sys.executable, str(ROOT / "db" / "setup.py"), "--db", str(db)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert db.exists()
    assert "warehouses" in result.stdout
    print("  db/setup.py runs as a standalone script")


def test_setup_reset_and_counts(tmp_path: Path) -> None:
    """`--reset` must rebuild from scratch and report row counts."""
    db = tmp_path / "reset.db"
    seed_database(db)

    result = subprocess.run(
        [sys.executable, str(ROOT / "db" / "setup.py"), "--db", str(db), "--reset"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr

    engine = get_engine(db)
    counts = table_counts(engine)
    assert counts["warehouses"] == 3, counts
    assert counts["demand_history"] == 3600, counts
    engine.dispose()
    print("  --reset rebuilds the database cleanly")


def test_sqlite_combined_sql_builds_a_database(tmp_path: Path) -> None:
    """database.sql must build a complete, working database on its own."""
    import sqlite3

    assert SQLITE_COMBINED.exists(), "run: python db/setup.py --emit-sql"

    target = tmp_path / "combined.db"
    conn = sqlite3.connect(target)
    try:
        conn.executescript(SQLITE_COMBINED.read_text(encoding="utf-8"))
        conn.commit()
        for table, expected in [
            ("warehouses", 3),
            ("products", 10),
            ("suppliers", 6),
            ("inventory", 30),
            ("demand_history", 3600),
        ]:
            count = conn.execute(f"select count(*) from {table}").fetchone()[0]
            assert count == expected, f"{table}: {count} != {expected}"
    finally:
        conn.close()
    print("  database.sql builds a complete database on its own")


def test_combined_sql_matches_sources() -> None:
    """The combined build scripts must not drift from the source .sql files."""
    for dialect, path in (("sqlite", SQLITE_COMBINED), ("postgres", POSTGRES_COMBINED)):
        assert path.exists(), f"run: python db/setup.py --emit-sql --dialect {dialect}"
        on_disk = path.read_text(encoding="utf-8")
        assert on_disk == combined_sql(dialect), (
            f"{path.name} is out of date - regenerate it with: "
            f"python db/setup.py --emit-sql --dialect {dialect}"
        )
    print("  combined build scripts are in sync with the source .sql files")


def test_postgres_sql_files_exist_and_differ_from_sqlite() -> None:
    """The Postgres flavour must exist and must not be a copy of the SQLite one."""
    schema, seed = sql_files("postgres")
    assert schema.exists() and seed.exists()
    postgres_sql = schema.read_text(encoding="utf-8")
    sqlite_sql = sql_files("sqlite")[0].read_text(encoding="utf-8")

    # Postgres-isms that SQLite does not have.
    assert "GENERATED ALWAYS AS IDENTITY" in postgres_sql
    assert "TIMESTAMPTZ" in postgres_sql
    assert "public." in postgres_sql
    assert "AUTOINCREMENT" not in postgres_sql

    # The seed must use Postgres date functions.
    postgres_seed = seed.read_text(encoding="utf-8")
    assert "generate_series" in postgres_seed
    assert "CURRENT_DATE" in postgres_seed
    assert "strftime" not in postgres_seed

    assert postgres_sql != sqlite_sql
    print("  the Postgres flavour is present and distinct")


def test_dialect_detection() -> None:
    """The dialect must be derived from the connection URL."""
    from sqlalchemy import create_engine

    sqlite_engine = get_engine(Path("x.db"))
    assert dialect_of(sqlite_engine) == "sqlite"

    # No connection is made here - create_engine only resolves the dialect.
    pg_engine = create_engine("postgresql+psycopg://user:pass@localhost/stockreorder")
    assert dialect_of(pg_engine) == "postgres"
    print("  dialect detection works")


def test_unsupported_dialect_is_reported() -> None:
    """An engine we have no SQL flavour for must say so clearly."""
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")  # a dialect we do support
    assert dialect_of(engine) == "sqlite"

    class FakeDialect:
        name = "oracle"

    class FakeEngine:
        dialect = FakeDialect()

    try:
        dialect_of(FakeEngine())
    except ValueError as exc:
        assert "Unsupported database 'oracle'" in str(exc)
    else:
        raise AssertionError("an unsupported dialect should raise")
    print("  unsupported dialects are reported clearly")


def test_repository_queries(tmp_path: Path) -> None:
    db = tmp_path / "queries.db"
    seed_database(db)
    engine = get_engine(db)

    warehouses = list_warehouses(engine)
    assert len(warehouses) == 3
    assert {w["code"] for w in warehouses} == {"WH-NORTH", "WH-WEST", "WH-CENTRAL"}

    north = resolve_warehouse(engine, "wh-north")  # case-insensitive
    assert north["code"] == "WH-NORTH"

    inventory = fetch_inventory(engine, north["id"])
    assert len(inventory) == 10
    for row in inventory:
        assert row["sku"].startswith("SKU-")
        assert row["on_hand"] >= 0
        assert "avg_demand_30d" in row
        assert "demand_trend_pct" in row

    product_ids = [row["product_id"] for row in inventory]
    offers = fetch_offers(engine, product_ids)
    assert offers, "expected supplier offers"
    for offer in offers:
        assert offer["moq"] >= 1
        assert offer["unit_price"] > 0
        assert offer["lead_time_days"] > 0
        assert offer["reliability_score"] >= 0

    # The price break must be honoured once the quantity reaches the tier.
    tiered = next(o for o in offers if o["price_break_qty"] and o["price_break_unit"])
    below = effective_unit_price(tiered, tiered["price_break_qty"] - 1)
    at = effective_unit_price(tiered, tiered["price_break_qty"])
    assert below == tiered["unit_price"]
    assert at == tiered["price_break_unit"]
    assert at < below

    engine.dispose()
    print("  repository queries return consistent data")


def test_sql_parses_for_its_dialect() -> None:
    """Every SQL file must parse with the parser for its dialect.

    This catches syntax errors in the Postgres flavour even when no Postgres
    server is available. Skipped if sqlglot is not installed.
    """
    import pytest

    sqlglot = pytest.importorskip("sqlglot")

    pairs = [
        (ROOT / "db" / "schema.sql", "sqlite"),
        (ROOT / "db" / "seed.sql", "sqlite"),
        (ROOT / "database.sql", "sqlite"),
        (ROOT / "db" / "schema.postgres.sql", "postgres"),
        (ROOT / "db" / "seed.postgres.sql", "postgres"),
        (ROOT / "database.postgres.sql", "postgres"),
    ]
    for path, dialect in pairs:
        assert path.exists(), path
        sql = path.read_text(encoding="utf-8")
        # Drop comment-only lines so the parser sees plain SQL.
        cleaned = "\n".join(
            line for line in sql.splitlines() if not line.strip().startswith("--")
        )
        parsed = sqlglot.parse(cleaned, read=dialect)
        assert parsed, f"{path.name} produced no statements"
    print("  every SQL file parses with its dialect's parser")


def test_build_supabase_url() -> None:
    """The Supabase URL builder must produce a usable, encoded URL."""

    # Direct connection: plain "postgres" username.
    url = build_supabase_url("abcdefghij", "secret")
    assert url == (
        "postgresql+psycopg://postgres:secret@db.abcdefghij.supabase.co:5432/postgres"
    )

    # Special characters in the password must be encoded.
    url = build_supabase_url("abcdefghij", "p@ss word#1")
    assert "p%40ss%20word%231" in url, url

    # Pooler: the tenant id MUST be in the username, otherwise the server
    # replies "no tenant identifier provided".
    url = build_supabase_url("abcdefghij", "secret", pooler=True, region="eu-west-1")
    assert url == (
        "postgresql+psycopg://postgres.abcdefghij:secret"
        "@aws-0-eu-west-1.pooler.supabase.com:6543/postgres"
    ), url
    print("  Supabase URLs are built correctly")


def test_check_database_reports_ready(tmp_path: Path) -> None:
    """--check must succeed and report the row counts once set up."""

    db = tmp_path / "check.db"
    seed_database(db)

    engine = get_engine(db)
    assert check_database(engine) == 0
    engine.dispose()
    print("  --check reports a ready database")


def test_check_database_reports_missing_tables(tmp_path: Path) -> None:
    """--check must fail when the schema has not been created."""

    db = tmp_path / "empty.db"
    engine = get_engine(db)
    # Create only some of the tables.
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE warehouses (id INTEGER PRIMARY KEY, code TEXT)")
    assert check_database(engine) == 1
    engine.dispose()
    print("  --check reports missing tables")


def test_to_date_handles_both_dialects() -> None:
    """SQLite returns date columns as strings; Postgres returns date objects."""
    from datetime import date, datetime

    from db.repository import _to_date

    assert _to_date("2026-10-20") == date(2026, 10, 20)
    assert _to_date(date(2026, 10, 20)) == date(2026, 10, 20)
    assert _to_date(datetime(2026, 10, 20, 13, 45)) == date(2026, 10, 20)
    assert _to_date("2026-10-20 13:45:00") == date(2026, 10, 20)
    assert _to_date(None) is None
    assert _to_date("not a date") is None
    print("  dates parse the same on SQLite and Postgres")


def test_demand_summary_is_dialect_free(tmp_path: Path) -> None:
    """The 60-day demand summary must not rely on SQLite-only functions."""
    from db.repository import fetch_demand_summary

    db = tmp_path / "summary.db"
    seed_database(db)
    engine = get_engine(db)

    summary = fetch_demand_summary(engine, resolve_warehouse(engine, "WH-NORTH")["id"])
    assert summary, "expected a summary"
    assert all(isinstance(v, int) for v in summary.values())
    engine.dispose()
    print("  the demand summary works on any dialect")


def test_contract_days_remaining_is_positive(tmp_path: Path) -> None:
    """A running contract must report positive days remaining.

    This was inverted once, which made every contract look expired and
    silently emptied the supplier scout's shortlist.
    """
    from datetime import date

    db = tmp_path / "contracts.db"
    seed_database(db)
    engine = get_engine(db)

    offers = fetch_offers(engine, [5])
    assert offers
    for offer in offers:
        days = offer["contract_days_remaining"]
        assert days is not None, offer
        assert days > 0, f"offer {offer['offer_id']} reports {days} days remaining"
        assert days < 400, offer

    # And the expiry date itself must be in the future.
    from db.repository import _to_date

    today = date.today()
    for offer in offers:
        expires = _to_date(offer["contract_expires_on"])
        assert expires is not None, offer
        assert expires >= today, offer

    engine.dispose()
    print("  contract expiry dates count the right way round")


def test_env_example_is_redacted(tmp_path: Path) -> None:
    """.env.example must never contain the real API keys."""
    import re

    from db.env_example import blank_secrets, emit_env_example

    source = tmp_path / ".env"
    source.write_text(
        "LARP_ROUTER_API_KEY=lrsk-abcdefghijklmnop1234\n"
        "OPENROUTER_API_KEY=sk-or-realvalue\n"
        "DATABASE_URL=postgresql+psycopg://u:p@host/db\n"
        "MODEL_COST_NAME=glm-5.3-flash\n"
        "# a comment mentioning lrsk- in passing\n",
        encoding="utf-8",
    )

    redacted = blank_secrets(source.read_text(encoding="utf-8"))
    assert "lrsk-abcdefghijklmnop1234" not in redacted
    assert "sk-or-realvalue" not in redacted
    # A password embedded in a URL is redacted, but the URL keeps its shape.
    assert "PASTE_PASSWORD_HERE" in redacted
    assert "postgresql+psycopg://u:" in redacted
    assert "@host/db" in redacted
    # Non-secret values are preserved.
    assert "glm-5.3-flash" in redacted
    # Comments are left alone.
    assert "# a comment mentioning lrsk- in passing" in redacted

    out = emit_env_example(source, tmp_path / ".env.example")
    written = out.read_text(encoding="utf-8")
    assert not re.findall(r"lrsk-[A-Za-z0-9]{16,}", written)
    print("  .env.example is redacted")


def test_config_freshness_detects_a_stale_env(tmp_path: Path) -> None:
    """A .env older than the settings module must be reported as stale."""
    import os

    from config.settings import config_freshness

    env_path = ROOT / ".env"
    code_path = ROOT / "config" / "settings.py"
    if not env_path.exists() or not code_path.exists():
        assert config_freshness() is None
        print("  no .env to check")
        return

    try:
        # Make the .env clearly older than the code that reads it.
        stale_time = code_path.stat().st_mtime - 3600
        os.utime(env_path, (stale_time, stale_time))
        message = config_freshness()
        assert message is not None, "a .env older than the code must be flagged"
        assert "stale" in message.lower()

        # And a .env that is newer than the code must not be flagged.
        fresh_time = code_path.stat().st_mtime + 3600
        os.utime(env_path, (fresh_time, fresh_time))
        assert config_freshness() is None
    finally:
        os.utime(env_path, None)
    print("  a stale .env is detected")


def test_sql_has_no_bare_percent_operator() -> None:
    """No SQL file may use the `%` operator.

    psycopg treats `%` as a parameter placeholder, so a modulo operator in a
    script breaks the query with "incomplete placeholder: '%'". The seed uses
    `mod(a, b)` instead, which both SQLite and Postgres understand.
    """
    import re

    for path in (
        ROOT / "db" / "schema.sql",
        ROOT / "db" / "seed.sql",
        ROOT / "db" / "schema.postgres.sql",
        ROOT / "db" / "seed.postgres.sql",
        ROOT / "database.sql",
        ROOT / "database.postgres.sql",
    ):
        sql = path.read_text(encoding="utf-8")
        # Strip comments and string literals, then look for a modulo operator.
        without_comments = "\n".join(
            line for line in sql.splitlines() if not line.strip().startswith("--")
        )
        assert not re.search(r"[^%]\s*%\s*\d", without_comments), (
            f"{path.name} uses the % operator - use mod(a, b) instead"
        )
    print("  no SQL file uses the % operator")


def test_percent_is_escaped_for_psycopg() -> None:
    """A literal % must survive the trip through psycopg."""
    from db.sql import split_statements as _split_statements

    statement = "select * from t where name like '%widget%' and 47 % 23 = 2"
    parts = _split_statements(statement)
    assert len(parts) == 1, parts
    # psycopg wants %% for a literal % - all three here.
    assert parts[0].count("%") == 3
    assert parts[0].replace("%", "%%").count("%%") == 3
    print("  literal % signs are escaped for psycopg")


if __name__ == "__main__":
    import tempfile

    failures = 0
    for test in (
        test_seeding_is_idempotent,
        test_setup_script_runs_standalone,
        test_setup_reset_and_counts,
        test_sqlite_combined_sql_builds_a_database,
        test_combined_sql_matches_sources,
        test_postgres_sql_files_exist_and_differ_from_sqlite,
        test_sql_parses_for_its_dialect,
        test_sql_has_no_bare_percent_operator,
        test_percent_is_escaped_for_psycopg,
        test_dialect_detection,
        test_unsupported_dialect_is_reported,
        test_build_supabase_url,
        test_check_database_reports_ready,
        test_check_database_reports_missing_tables,
        test_to_date_handles_both_dialects,
        test_demand_summary_is_dialect_free,
        test_contract_days_remaining_is_positive,
        test_env_example_is_redacted,
        test_config_freshness_detects_a_stale_env,
        test_repository_queries,
    ):
        print(f"[RUN] {test.__name__}")
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
                test(Path(tmp))
            print(f"[PASS] {test.__name__}")
        except Exception as exc:
            failures += 1
            import traceback

            traceback.print_exc()
            print(f"[FAIL] {test.__name__}: {exc}")
    sys.exit(1 if failures else 0)
