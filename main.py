"""Entry point for the stock-reorder multi-agent pipeline.

    python main.py --test --warehouse WH-NORTH   check everything, then run
    python main.py --warehouse WH-NORTH          run the pipeline
    python main.py --routing                     show the model routing
    python main.py --preflight                   check the models only

The settings block below is what you normally edit. Presentation lives in
`reporting.py`; the graph lives in `graph.py`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rich.panel import Panel  # noqa: E402
from rich.text import Text  # noqa: E402

from agents.base import AgentRuntime  # noqa: E402
from config.settings import (  # noqa: E402
    Settings,
    config_freshness,
    interpreter_hint,
    load_routing,
    missing_keys,
)
from db.repository import get_engine  # noqa: E402
from db.setup import check_database, seed_database  # noqa: E402
from graph import build_graph  # noqa: E402
from llm import build_llm  # noqa: E402
from observability import RunRecorder, build_dashboard  # noqa: E402
from reporting import (  # noqa: E402
    console,
    print_audit,
    print_banner,
    print_report,
    print_routing,
    print_steps,
)

# ===========================================================================
# Settings - edit these, or pass the matching command line flag
# ===========================================================================
WAREHOUSE = "WH-NORTH"  # WH-NORTH | WH-WEST | WH-CENTRAL
MAX_ITEMS = 5  # how many of the most urgent SKUs to plan for
BUDGET = None  # spend cap in USD, e.g. 25000. None means no cap
FOCUS_SKUS = []  # e.g. ["SKU-1005", "SKU-1008"] to plan for specific SKUs only
REBUILD_DATABASE = False  # True reseeds the demo database before running
WRITE_JSON = None  # e.g. "report.json" to also dump the result to a file


# ===========================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py", description="Multi-agent stock reorder pipeline (LangGraph)"
    )
    parser.add_argument("--warehouse", default=WAREHOUSE, help="warehouse code")
    parser.add_argument("--max-items", type=int, default=MAX_ITEMS, help="SKUs to plan for")
    parser.add_argument("--budget", type=float, default=BUDGET, help="spend cap in USD")
    parser.add_argument("--sku", action="append", default=[], help="limit to specific SKUs")
    parser.add_argument("--db", default=None, help="path to the SQLite database file")
    parser.add_argument("--rebuild-db", action="store_true", help="reseed the demo database")
    parser.add_argument("--json", default=None, help="write the report to this JSON file")
    parser.add_argument(
        "--routing", action="store_true", help="show which model handles which agent, then exit"
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="serve the React supervision dashboard (http://localhost:8642), then exit",
    )
    parser.add_argument(
        "--dashboard-sheet",
        action="store_true",
        help="rebuild dashboard.html (Google-Sheets-style static export) instead",
    )
    parser.add_argument(
        "--port", type=int, default=8642, help="port for --dashboard (default: 8642)"
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="call every model once to verify the keys and providers, then exit",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="check the database, check the models, then run the pipeline",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.routing:
        print_routing()
        return 0

    if args.dashboard:
        from observability_server import serve

        serve(port=args.port, open_browser=True)
        return 0

    if args.dashboard_sheet:
        return run_dashboard_sheet()

    if args.test:
        return run_test(args)

    if args.preflight:
        missing = missing_keys()
        if missing:
            console.print(
                Panel(
                    Text.from_markup(
                        "[bold]Missing API keys:[/bold] " + ", ".join(missing) + "\n\n"
                        "1. Open [.env] and paste your key into the line above\n"
                        "2. For LarpRouter the key starts with [cyan]lrsk-[/cyan] - create one\n"
                        "   in the LarpRouter dashboard and bind it to a DeepSeek or GLM pool\n"
                        "3. Run [cyan]python main.py --preflight[/cyan] to check it works"
                    ),
                    title="Cannot run",
                    border_style="red",
                )
            )
            return 2
        return run_preflight()

    hint = interpreter_hint()
    if hint:
        console.print(f"[yellow]Note:[/yellow] {hint}\n")

    stale = config_freshness()
    if stale:
        console.print(Panel(Text(stale), title="Stale config", border_style="yellow"))

    missing = missing_keys()
    if missing:
        console.print(
            Panel(
                Text.from_markup(
                    "[bold]Missing API keys:[/bold] " + ", ".join(missing) + "\n\n"
                    "1. Open [.env] and paste your key into the line above\n"
                    "2. For LarpRouter the key starts with [cyan]lrsk-[/cyan] - create one\n"
                    "   in the LarpRouter dashboard and bind it to a DeepSeek or GLM pool\n"
                    "3. Run [cyan]python main.py --preflight[/cyan] to check it works"
                ),
                title="Cannot run",
                border_style="red",
            )
        )
        return 2

    db_path = Path(args.db) if args.db else None
    if args.rebuild_db or REBUILD_DATABASE:
        console.print("[dim]Rebuilding the demo database...[/dim]")
        try:
            seed_database(db_path)
        except RuntimeError as exc:
            console.print(f"[red]Could not reach the database:[/red]\n\n{exc}")
            return 1

    routing = load_routing()
    settings = Settings(
        db_path=db_path,
        warehouse=args.warehouse,
        max_items=args.max_items,
        budget_cap=args.budget,
        max_cost_attempts=2,
    )

    # Observability: one recorder per run. It generates the correlation ID,
    # logs every agent step, counts tokens and evaluates the three alerts.
    recorder = RunRecorder()
    focus = args.sku or FOCUS_SKUS
    recorder.meta = {
        "warehouse": args.warehouse,
        "max_items": args.max_items,
        "budget_cap": args.budget,
        "focus_skus": list(focus),
    }
    console.print(f"[dim]Trace:[/dim] {recorder.correlation_id}")

    console.print("[dim]Building agent models...[/dim]")
    llms = {
        role: build_llm(spec, usage=recorder.usage_handler(role))
        for role, spec in routing.items()
    }

    try:
        engine = get_engine(db_path)
    except RuntimeError as exc:
        console.print(f"[red]Could not reach the database:[/red]\n\n{exc}")
        return 1

    runtime = AgentRuntime(
        engine=engine, llms=llms, routing=routing, settings=settings, obs=recorder
    )
    app = build_graph(runtime)
    initial: dict[str, Any] = {
        "run_id": uuid.uuid4().hex[:12],
        "warehouse_code": args.warehouse,
        "max_items": args.max_items,
        "budget_cap": args.budget,
        "focus_skus": list(focus),
        "cost_attempt": 0,
        "max_cost_attempts": settings.max_cost_attempts,
        "messages": [],
        "audit": [],
    }

    console.print(
        f"[bold]Running agents[/bold] for [cyan]{args.warehouse}[/cyan] "
        f"(max {args.max_items} SKUs"
        + (f", budget {args.budget}" if args.budget else "")
        + ")"
    )
    console.print()

    try:
        final = app.invoke(initial, config={"recursion_limit": 50})
    except Exception as exc:
        summary = recorder.finish("failed")
        console.print(f"[red]The pipeline failed:[/red] {_short_error(exc)}")
        _print_observability(summary)
        return 1

    print_audit(final.get("audit") or [])
    print_report(final.get("report") or {})

    summary = recorder.finish("ok")
    _print_observability(summary)

    out_path = args.json or WRITE_JSON
    if out_path:
        path = Path(out_path)
        path.write_text(
            json.dumps(final.get("report") or {}, indent=2, default=str), encoding="utf-8"
        )
        console.print(f"[dim]Report written to {path}[/dim]")

    return 0


# ===========================================================================
# Test runner
# ===========================================================================
def run_test(args: argparse.Namespace) -> int:
    """Full test: database, then models, then the pipeline.

    This is the one command to run when you want to know whether the whole
    project works. Each stage is checked before the next one starts, so a
    failure tells you exactly which layer broke.
    """
    steps: list[tuple[str, bool, str]] = []

    # --- 1. database -----------------------------------------------------
    console.print("[bold]Step 1/3 - database[/bold]")
    db_path = Path(args.db) if args.db else None
    try:
        engine = get_engine(db_path)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    db_ok = check_database(engine) == 0
    steps.append(("database", db_ok, "connected" if db_ok else "unreachable"))
    if not db_ok:
        print_steps(steps)
        return 1

    # --- 2. models -------------------------------------------------------
    console.print("\n[bold]Step 2/3 - models[/bold]")
    models_ok = run_preflight() == 0
    steps.append(("models", models_ok, "all reachable" if models_ok else "some failed"))
    if not models_ok:
        print_steps(steps)
        return 1

    # --- 3. pipeline -----------------------------------------------------
    console.print("\n[bold]Step 3/3 - pipeline[/bold]")
    result = main(
        [
            *([] if args.db is None else ["--db", args.db]),
            "--warehouse",
            args.warehouse,
            "--max-items",
            str(args.max_items),
        ]
    )
    steps.append(("pipeline", result == 0, "completed" if result == 0 else "failed"))
    print_steps(steps)
    return result


# ===========================================================================
# Pre-flight
# ===========================================================================
def run_preflight() -> int:
    """Call every configured model once, to check the keys and providers work.

    This is the cheapest possible end-to-end check: one tiny request per
    agent, so it costs almost nothing but catches bad keys, wrong model ids
    and provider outages before a full pipeline run.
    """
    routing = load_routing()
    console.print("[bold]Pre-flight check[/bold] - one request per agent\n")

    failures = 0
    for role, spec in routing.items():
        started = time.time()
        try:
            model = build_llm(spec)
            response = model.invoke("Reply with the single word: OK")
            text = str(getattr(response, "content", "")).strip()
            if not text:
                raise RuntimeError("the model returned an empty response")
            elapsed = time.time() - started
            console.print(
                f"  [green]OK[/green]    {role:18} {spec.provider}/{spec.model}  "
                f"[dim]({elapsed:.1f}s)[/dim]"
            )
        except Exception as exc:
            failures += 1
            elapsed = time.time() - started
            console.print(
                f"  [red]FAIL[/red]  {role:18} {spec.provider}/{spec.model}  "
                f"[dim]({elapsed:.1f}s)[/dim]"
            )
            console.print(f"         [red]{_explain_provider_error(exc)}[/red]")

    console.print()
    if failures:
        console.print(
            f"[red]{failures} of {len(routing)} model(s) failed.[/red]\n"
            "Fix the .env values, or pick different models:\n"
            "  python main.py --routing   (shows the current routing)\n"
            "See the README for a table of models that are known to work."
        )
        return 1

    console.print("[green]All models reachable.[/green] Ready to run:  python main.py")
    return 0


def _explain_provider_error(exc: Exception) -> str:
    """Turn a provider error into something actionable."""
    message = str(exc)
    lowered = message.lower()

    if "requires more credits" in lowered or "402" in message:
        return (
            "Not enough credit for this model's MAX_TOKENS. Either add credit, "
            "or lower MAX_TOKENS for the role, or pick a cheaper model."
        )
    if "not found for account" in lowered:
        return (
            "That NVIDIA model is not enabled for your account. "
            "Use a different NVIDIA model id (try nvidia/nemotron-3-ultra-550b-a55b)."
        )
    if "404" in message and "not found" in lowered:
        return (
            "The model id is not available on this provider/account. "
            "Try a different model for this role."
        )
    if "401" in message or "unauthorized" in lowered or "invalid api key" in lowered:
        return "The API key was rejected. Check it in .env."
    if "410" in message or "end of life" in lowered:
        return "That model has been retired by the provider. Pick another one."
    if "insufficient_credit" in lowered:
        return "The account has no credit left. Top it up and try again."
    return message.split("\n")[0][:200]


# ===========================================================================
# Observability helpers
# ===========================================================================
def run_dashboard() -> int:
    """Build dashboard.html from runs/executions.csv and print the summary."""
    path, summary = build_dashboard()
    console.print(f"[bold]Supervision dashboard[/bold] -> [cyan]{path}[/cyan]\n")
    console.print(
        f"  Runs           : {summary['runs']}\n"
        f"  Success rate   : {summary['success_rate_pct']}%\n"
        f"  Average latency: {summary['avg_latency_ms']} ms\n"
        f"  Total cost     : ${summary['total_cost_usd']}\n"
        f"  Alerting runs  : {summary['alerting_runs']}"
    )
    if summary["runs"] == 0:
        console.print(
            "\n[yellow]No runs recorded yet.[/yellow] Run the pipeline first:  python main.py"
        )
    console.print(
        "\n[dim]The executions table is runs/executions.csv - it opens directly in Google Sheets."
        "\nThe traces behind it are runs/<correlation_id>.json.[/dim]"
    )
    return 0


run_dashboard_sheet = run_dashboard  # legacy alias for --dashboard-sheet


def _print_observability(summary: dict[str, Any]) -> None:
    """Correlation ID, the token/cost metrics and any alert after a run."""
    console.print(
        f"\n[dim]Trace: {summary['correlation_id']}  |  "
        f"{summary['steps']} step(s), {summary['failed']} failed  |  "
        f"{summary['duration_ms']:,} ms  |  "
        f"{summary['input_tokens'] + summary['output_tokens']:,} tokens  |  "
        f"${summary['estimated_cost_usd']} est.  |  "
        "run log: runs/<correlation_id>.json[/dim]"
    )
    if summary["alerts"]:
        lines = "\n".join(
            f"[bold]{a['type']}[/bold] - {a['message']}" for a in summary["alerts"]
        )
        console.print(Panel(Text.from_markup(lines), title="Alerts", border_style="red"))


def _short_error(exc: Exception) -> str:
    """One-line, redacted error message."""
    from observability import redact

    return redact(str(exc)).split("\n")[0][:300]


if __name__ == "__main__":
    if len(sys.argv) == 1:
        print_banner()
        raise SystemExit(0)
    raise SystemExit(main())
