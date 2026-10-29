"""Console rendering for the stock-reorder pipeline.

Kept separate from `main.py` so the entry point stays about wiring and
orchestration, and the presentation can change without touching the graph.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from config.settings import (
    ROLE_ENV_PREFIX,
    ROOT,
    config_freshness,
    load_routing,
    overridden_by_environment,
)

console = Console()


def shorten(text: str, limit: int) -> str:
    """Truncate to `limit` characters using ASCII (Windows consoles are not UTF-8)."""
    text = (text or "").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def print_banner() -> None:
    """Friendly landing screen, shown when no command is given."""
    console.print(
        Panel(
            Text.from_markup(
                "[bold]Stock reorder - multi-agent pipeline[/bold]\n"
                "LangGraph + LarpRouter + Supabase\n\n"
                "Nothing to do yet. Pick one:\n"
                "[cyan]python main.py --test[/cyan]                   check the db, the models, then run\n"
                "[cyan]python main.py --warehouse WH-NORTH[/cyan]   run the agent pipeline\n"
                "[cyan]python main.py --dashboard[/cyan]              supervision dashboard + execution table\n"
                "[cyan]python main.py --routing[/cyan]                show the model routing\n"
                "[cyan]python main.py --preflight[/cyan]             check the models only\n"
                "[cyan]python db/setup.py --check[/cyan]             check the database only\n\n"
                "[dim]python main.py --help[/dim]                    full command reference"
            ),
            title="stock-reorder",
            border_style="cyan",
        )
    )


def print_audit(audit: list[dict[str, Any]]) -> None:
    """The trail of agents, with the model each one used."""
    if not audit:
        return
    table = Table(title="Agent trail", header_style="bold cyan")
    table.add_column("Role", no_wrap=True)
    table.add_column("Provider", no_wrap=True)
    table.add_column("Model", no_wrap=True)
    table.add_column("Summary", overflow="fold")

    for entry in audit:
        table.add_row(
            shorten(entry.get("role", ""), 30),
            entry.get("provider", ""),
            shorten(entry.get("model", ""), 28),
            entry.get("summary", ""),
        )
    console.print(table)


def print_report(report: dict[str, Any]) -> None:
    """The final reorder plan."""
    if not report:
        console.print("[yellow]No report produced.[/yellow]")
        return

    narrow = console.width < 120
    table = Table(
        title=f"Reorder plan - {report.get('warehouse', '')} {report.get('warehouse_name', '')}",
        header_style="bold cyan",
        expand=False,
    )
    table.add_column("SKU", no_wrap=True)
    if not narrow:
        table.add_column("Product", overflow="crop", max_width=26)
    table.add_column("Supplier", overflow="crop", max_width=22)
    table.add_column("Qty", justify="right")
    table.add_column("Unit", justify="right")
    table.add_column("Landed", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Cover", justify="right")
    table.add_column("Urgency", no_wrap=True)

    for line in report.get("lines", []):
        row = [line.get("sku", "")]
        if not narrow:
            row.append(shorten(line.get("product_name", ""), 26))
        row += [
            shorten(line.get("supplier_name", ""), 22),
            f"{line.get('order_qty', 0):,}",
            f"{line.get('unit_price', 0):,.4f}".rstrip("0").rstrip("."),
            f"{line.get('landed_unit_cost', 0):,.4f}".rstrip("0").rstrip("."),
            f"{line.get('line_total', 0):,.2f}",
            f"{line.get('days_of_cover', 0):.1f}",
            line.get("urgency", ""),
        ]
        table.add_row(*row)

    console.print(table)

    currency = report.get("currency", "USD")
    savings = float(report.get("projected_savings", 0))
    label = "Saving" if savings >= 0 else "Premium"
    console.print(
        f"[bold]Total spend[/bold]: {report.get('total_spend', 0):,.2f} {currency}   "
        f"[bold]Baseline[/bold]: {report.get('baseline_spend', 0):,.2f} {currency}   "
        f"[bold]{label}[/bold]: {abs(savings):,.2f} {currency} "
        f"({abs(report.get('savings_pct', 0))}%)"
    )
    console.print(Panel(Text(report.get("summary", "")), title="Summary", border_style="green"))

    for warning in report.get("warnings", []):
        console.print(f"[yellow]![/yellow] {warning}")


def print_steps(steps: list[tuple[str, bool, str]]) -> None:
    """The pass/fail table at the end of a test run."""
    console.print()
    table = Table(title="Test result", header_style="bold cyan")
    table.add_column("Stage", no_wrap=True)
    table.add_column("Result", no_wrap=True)
    table.add_column("Detail", overflow="fold")
    for name, ok, detail in steps:
        table.add_row(name, "[green]PASS[/green]" if ok else "[red]FAIL[/red]", detail)
    console.print(table)


def print_routing() -> None:
    """Which model handles which agent, and where the config came from."""
    routing = load_routing()
    table = Table(title="Model routing", header_style="bold cyan", expand=False)
    table.add_column("Agent role", no_wrap=True)
    table.add_column("Provider", no_wrap=True)
    table.add_column("Model", no_wrap=True)
    table.add_column("Endpoint", overflow="fold")
    table.add_column("Key", no_wrap=True)
    for role, spec in routing.items():
        endpoint = (
            spec.base_url.replace("https://", "").replace("http://", "").rstrip("/")
        )
        if endpoint.endswith("/v1"):
            endpoint = endpoint[: -len("/v1")]
        key = "SET" if os.getenv(spec.key_env, "").strip() else "[red]MISSING[/red]"
        table.add_row(role, spec.provider, spec.model, endpoint, key)
    console.print(table)

    key_vars = sorted({spec.key_env for spec in routing.values()})
    console.print("[dim]Key variable(s):[/dim] " + ", ".join(key_vars))

    overridden = _overridden_config()
    if overridden:
        console.print(
            Panel(
                Text.from_markup(
                    "[bold]These values come from the terminal environment, "
                    "not from .env:[/bold]\n"
                    + "\n".join(f"  {name}" for name in overridden)
                    + "\n\nVS Code copies .env into the terminal when it starts "
                    "(`python.terminal.useEnvFile`),\n"
                    "and load_dotenv() never overrides an existing variable. "
                    "A terminal opened while .env\nwas stale keeps serving the old values.\n\n"
                    "[cyan]Fix:[/cyan] open a new terminal, or run "
                    "'Developer: Reload Window', then try again."
                ),
                title="Overridden by the terminal environment",
                border_style="yellow",
            )
        )

    env_path = ROOT / ".env"
    if env_path.exists():
        stamp = datetime.fromtimestamp(env_path.stat().st_mtime)
        console.print(
            f"[dim]Config file:[/dim] {env_path}  (last written {stamp:%Y-%m-%d %H:%M})"
        )
    else:
        console.print(f"[yellow]No .env found at {env_path}[/yellow]")

    stale = config_freshness()
    if stale:
        console.print(Panel(Text(stale), title="Warning", border_style="yellow"))


def _overridden_config() -> list[str]:
    """Config variables the terminal environment overrides (these beat .env)."""
    routing = load_routing()
    watched: set[str] = {spec.key_env for spec in routing.values()}
    for spec in routing.values():
        prefix = ROLE_ENV_PREFIX.get(spec.role, spec.role.upper())
        watched.update(
            {
                f"MODEL_{prefix}_PROVIDER",
                f"MODEL_{prefix}_NAME",
                f"MODEL_{prefix}_BASE_URL",
                f"MODEL_{prefix}_MAX_TOKENS",
                f"MODEL_{prefix}_REASONING",
            }
        )
    return overridden_by_environment(sorted(watched))
