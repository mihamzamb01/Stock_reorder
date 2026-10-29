"""SQL execution machinery for the stock-reorder project.

Kept separate from `db/setup.py` so the command-line tool stays a thin shell
around reusable pieces:

* statement splitting that respects comments and string literals
* running a `.sql` file against any supported dialect
* dialect detection and the per-dialect SQL file pair
* the combined build script
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import Engine

from config.settings import PROVIDER_KEY_ENV  # noqa: F401  (kept for parity)

DB_DIR = Path(__file__).resolve().parent
ROOT = DB_DIR.parent

SQLITE_SCHEMA = DB_DIR / "schema.sql"
SQLITE_SEED = DB_DIR / "seed.sql"
POSTGRES_SCHEMA = DB_DIR / "schema.postgres.sql"
POSTGRES_SEED = DB_DIR / "seed.postgres.sql"

SQLITE_COMBINED = ROOT / "database.sql"
POSTGRES_COMBINED = ROOT / "database.postgres.sql"

DIALECTS = ("sqlite", "postgres")

_COMBINED_HEADER = """\
-- ===========================================================================
-- Stock reorder - COMPLETE database build (schema + demo data)
--
-- This is {schema_file} and {seed_file} in one file, so you can paste the
-- whole thing into any SQL client: DB Browser for SQLite, DBeaver, MySQL
-- Workbench, pgAdmin, or the sqlite3 CLI.
--
--   {cli_command}
--
-- The two halves are also kept separately in db/ for reference:
--   {schema_file}  -> tables, keys, indexes
--   {seed_file}    -> demo data (incl. 120 days of demand history)
--
-- Regenerate this file after editing either of them:
--   python db/setup.py --emit-sql --dialect {dialect}
-- ===========================================================================

"""


# ---------------------------------------------------------------------------
# Dialect handling
# ---------------------------------------------------------------------------
def dialect_of(engine: Engine) -> str:
    """Map a SQLAlchemy dialect onto one of our two SQL flavours."""
    name = engine.dialect.name
    if name in ("postgresql", "postgres"):
        return "postgres"
    if name == "sqlite":
        return "sqlite"
    raise ValueError(
        f"Unsupported database '{name}'. Supported: sqlite, postgres. "
        "Add a matching .sql pair in db/ for other engines."
    )


def sql_files(dialect: str) -> tuple[Path, Path]:
    """The (schema, seed) SQL files for a dialect."""
    if dialect == "postgres":
        return POSTGRES_SCHEMA, POSTGRES_SEED
    return SQLITE_SCHEMA, SQLITE_SEED


def combined_path(dialect: str) -> Path:
    """Where the combined build script for a dialect is written."""
    return POSTGRES_COMBINED if dialect == "postgres" else SQLITE_COMBINED


# ---------------------------------------------------------------------------
# Statement splitting
# ---------------------------------------------------------------------------
def split_statements(sql: str) -> list[str]:
    """Split a script into individual statements.

    Comments and semicolons inside string literals are handled, so a CTE or a
    function body is not cut in half.
    """
    statements: list[str] = []
    buffer: list[str] = []
    in_string = False
    in_line_comment = False
    in_block_comment = False
    index = 0

    while index < len(sql):
        char = sql[index]
        pair = sql[index : index + 2]

        if in_line_comment:
            if char == "\n":
                in_line_comment = False
                buffer.append(char)
        elif in_block_comment:
            if pair == "*/":
                in_block_comment = False
                index += 2
                continue
        elif in_string:
            buffer.append(char)
            if char == "'":
                # '' is an escaped quote inside a string literal.
                if index + 1 < len(sql) and sql[index + 1] == "'":
                    buffer.append("'")
                    index += 2
                    continue
                in_string = False
        else:
            if pair == "--":
                in_line_comment = True
                index += 2
                continue
            if pair == "/*":
                in_block_comment = True
                index += 2
                continue
            if char == "'":
                in_string = True
                buffer.append(char)
            elif char == ";":
                statements.append("".join(buffer))
                buffer = []
            else:
                buffer.append(char)
        index += 1

    statements.append("".join(buffer))
    return [s.strip() for s in statements if s.strip()]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def run_sql_file(engine: Engine, path: Path) -> int:
    """Execute a `.sql` file against the database. Returns the statement count."""
    sql = path.read_text(encoding="utf-8")

    if engine.dialect.name == "sqlite":
        # Use the driver's native executescript so the file runs exactly as
        # written - no parsing, no risk of mangling the recursive CTE.
        with engine.begin() as conn:
            raw = conn.connection.driver_connection
            raw.executescript(sql)
        return len(re.findall(r";", sql))

    count = 0
    with engine.begin() as conn:
        for statement in split_statements(sql):
            # psycopg treats % as a parameter placeholder, so a literal % in the
            # SQL (a modulo operator, a LIKE pattern) breaks the query. Doubling
            # it is how psycopg is told "this is a literal %".
            conn.exec_driver_sql(statement.replace("%", "%%"))
            count += 1
    return count


# ---------------------------------------------------------------------------
# Combined build script
# ---------------------------------------------------------------------------
def combined_sql(dialect: str) -> str:
    """The full database build as one self-contained script."""
    schema_path, seed_path = sql_files(dialect)
    if dialect == "postgres":
        cli = 'psql "$DATABASE_URL" -f database.postgres.sql'
    else:
        cli = "sqlite3 stock_reorder.db < database.sql"
    header = _COMBINED_HEADER.format(
        schema_file=schema_path.name,
        seed_file=seed_path.name,
        cli_command=cli,
        dialect=dialect,
    )
    return (
        header
        + schema_path.read_text(encoding="utf-8")
        + "\n\n"
        + seed_path.read_text(encoding="utf-8")
    )


def emit_combined(dialect: str = "sqlite", path: Path | None = None) -> Path:
    """Write the combined build script for a dialect."""
    target = path or combined_path(dialect)
    target.write_text(combined_sql(dialect), encoding="utf-8")
    return target
