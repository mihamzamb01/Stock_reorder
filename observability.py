"""Observability: logs, metrics, traces, alerts and the supervision dashboard.

This module implements the observability concepts the pipeline's course slides
describe, mapped onto the stock-reorder project:

* **Logs**      - every agent step (tool/model called, duration, outcome) is
  written to ``runs/<correlation_id>.json``.
* **Metrics**   - token counts per role, an estimated LLM cost per run, and the
  run summary compiled by :func:`summarize`.
* **Traces**    - each execution gets a **correlation ID** (``REQ-2026-1026-1432-4ac2``)
  stamped on every step so concurrent runs can be told apart end to end.
* **Alerts**    - three documented alerts, evaluated after every run:
  latency, failure rate and cost. Thresholds are set in ``.env``.
* **Never-log** - :func:`redact` removes emails, card numbers, key-shaped
  strings and any live secret value before anything is stored on disk.
* **Dashboard** - :func:`build_dashboard` turns the executions table into a
  self-contained HTML page (and ``runs/executions.csv`` opens in Google Sheets).
"""

from __future__ import annotations

import csv
import html
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from config.settings import ROOT

# ---------------------------------------------------------------------------
# Configuration (all overridable in .env)
# ---------------------------------------------------------------------------
RUNS_DIR = ROOT / os.getenv("RUNS_DIR", "runs")


def _runs_dir() -> Path:
    """The runs directory, late-bound so tests can monkeypatch RUNS_DIR."""
    return RUNS_DIR


def _executions_csv() -> Path:
    """The executions table, always in sync with the active runs directory."""
    return _runs_dir() / "executions.csv"


# Backwards-compatible constants (resolve once at import).
EXECUTIONS_CSV = RUNS_DIR / "executions.csv"

ALERT_LATENCY_MS = int(os.getenv("ALERT_LATENCY_MS", "30000"))
ALERT_FAILURE_RATE_PCT = float(os.getenv("ALERT_FAILURE_RATE_PCT", "20"))
ALERT_COST_LIMIT_USD = float(os.getenv("ALERT_COST_LIMIT_USD", "10"))

# Estimated $ per million tokens (input / output). Free pools: leave 0.
COST_INPUT_PER_MTOK = float(os.getenv("COST_INPUT_PER_MTOK", "0"))
COST_OUTPUT_PER_MTOK = float(os.getenv("COST_OUTPUT_PER_MTOK", "0"))

CSV_FIELDS = [
    "correlation_id",
    "started_at",
    "warehouse",
    "status",
    "steps",
    "failed",
    "duration_ms",
    "input_tokens",
    "output_tokens",
    "est_cost_usd",
    "alerts",
]

# ---------------------------------------------------------------------------
# Redaction - the "never log" rule
# ---------------------------------------------------------------------------
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 13-19 digit runs with optional separators: card-shaped numbers.
_CARD = re.compile(r"\b(?:\d[ \-]?){13,19}\b")
# Key-shaped strings: lrsk-, sk-or-v1-, sk-ant-, nvapi-, sk-, ...
_KEY = re.compile(
    r"(?:lrsk|sk-or-v1|sk-ant|nvapi|sk)[-_][A-Za-z0-9_\-]{6,}", re.IGNORECASE
)
_SECRET_NAMES = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def _live_secret_values() -> list[str]:
    """The current values of any environment variable that looks like a secret."""
    values: list[str] = []
    for name, value in os.environ.items():
        if len(value) >= 12 and any(marker in name.upper() for marker in _SECRET_NAMES):
            values.append(value)
    return values


def redact(text: Any, extra_secrets: list[str] | None = None) -> str:
    """Blank out anything that must never reach a log file.

    * emails                    -> ``[email]``
    * card-shaped digit runs    -> ``[card]``
    * key-shaped strings        -> ``[key]``
    * the live values of any ``*KEY*`` / ``*PASSWORD*`` env var -> ``[secret]``
    """
    text = str(text)
    secrets_list = _live_secret_values() if extra_secrets is None else extra_secrets
    for value in secrets_list:
        if value and value in text:
            text = text.replace(value, "[secret]")
    text = _KEY.sub("[key]", text)
    text = _EMAIL.sub("[email]", text)
    text = _CARD.sub("[card]", text)
    return text


# ---------------------------------------------------------------------------
# Correlation ID - one per execution, stamped on every step
# ---------------------------------------------------------------------------
def new_correlation_id() -> str:
    """``REQ-20261026-1432-4ac2`` - date, time, four random characters."""
    now = datetime.now()
    unique = secrets.token_hex(2)
    return f"REQ-{now:%Y%m%d-%H%M}-{unique}"


# ---------------------------------------------------------------------------
# Token usage per role - attached to the chat model as a callback
# ---------------------------------------------------------------------------
def _extract_usage(llm_result: LLMResult) -> dict[str, int]:
    """Token counts from a result, in either provider vocabulary."""
    usage: dict[str, int] = {}
    llm_output = getattr(llm_result, "llm_output", None) or {}
    raw = llm_output.get("token_usage") if isinstance(llm_output, dict) else None
    if not raw:
        for generation_list in getattr(llm_result, "generations", None) or []:
            for generation in generation_list or []:
                metadata = getattr(getattr(generation, "message", None), "usage_metadata", None)
                if metadata:
                    raw = metadata
                    break
    if isinstance(raw, dict):
        usage["input"] = int(
            raw.get("prompt_tokens") or raw.get("input_tokens") or 0
        )
        usage["output"] = int(
            raw.get("completion_tokens") or raw.get("output_tokens") or 0
        )
    return usage


class UsageHandler(BaseCallbackHandler):
    """Counts LLM calls and tokens for one agent role.

    Attached to the ``ChatOpenAI`` instance at build time, so it fires on every
    call of that model - including the structured-output retries - no matter
    which node invoked it. Thread-safe: parallel analyst nodes share one model.
    """

    def __init__(self, role: str) -> None:
        self.role = role
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self._lock = threading.Lock()

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:  # noqa: ARG002
        usage = _extract_usage(response)
        with self._lock:
            self.calls += 1
            self.input_tokens += usage.get("input", 0)
            self.output_tokens += usage.get("output", 0)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "role": self.role,
                "calls": self.calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
            }


# ---------------------------------------------------------------------------
# Step records - the log and trace layer
# ---------------------------------------------------------------------------
@dataclass
class StepRecord:
    """One agent step: who ran, for how long, and what came of it."""

    stage: str
    role: str
    provider: str
    model: str
    started_at: str
    duration_ms: int = 0
    status: str = "ok"  # ok | failed
    error: str = ""


# ---------------------------------------------------------------------------
# The recorder - one instance per pipeline run
# ---------------------------------------------------------------------------
class RunRecorder:
    """Collects the logs, metrics and alerts for one pipeline execution."""

    def __init__(
        self,
        runs_dir: Path | None = None,
        *,
        correlation_id: str | None = None,
        redact_live_secrets: bool = True,
    ) -> None:
        self.runs_dir = Path(runs_dir) if runs_dir else RUNS_DIR
        self.correlation_id = correlation_id or new_correlation_id()
        self.started_at = datetime.now(UTC).isoformat(timespec="seconds")
        self.steps: list[StepRecord] = []
        self.handlers: dict[str, UsageHandler] = {}
        self.meta: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._secrets = _live_secret_values() if redact_live_secrets else []

    # -- usage handlers (one per model role) --------------------------------
    def usage_handler(self, role: str) -> UsageHandler:
        with self._lock:
            if role not in self.handlers:
                self.handlers[role] = UsageHandler(role)
            return self.handlers[role]

    # -- the trace context manager -------------------------------------------
    @contextmanager
    def step(
        self, *, stage: str, role: str, provider: str, model: str
    ) -> Iterator[StepRecord]:
        """Record one step: log it, time it, and keep it even when it fails."""
        record = StepRecord(
            stage=redact(stage, self._secrets)[:120],
            role=role,
            provider=provider,
            model=redact(model, self._secrets),
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        started = time.perf_counter()
        try:
            yield record
        except Exception as exc:
            record.status = "failed"
            record.error = redact(exc, self._secrets)[:400]
            raise
        finally:
            record.duration_ms = int((time.perf_counter() - started) * 1000)
            with self._lock:
                self.steps.append(record)

    # -- metrics ------------------------------------------------------------
    def token_totals(self) -> dict[str, int]:
        totals = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
        for handler in self.handlers.values():
            with handler._lock:  # noqa: SLF001 - same module
                totals["calls"] += handler.calls
                totals["input_tokens"] += handler.input_tokens
                totals["output_tokens"] += handler.output_tokens
        return totals

    def estimated_cost(self) -> float:
        totals = self.token_totals()
        return round(
            (totals["input_tokens"] * COST_INPUT_PER_MTOK
             + totals["output_tokens"] * COST_OUTPUT_PER_MTOK)
            / 1_000_000,
            4,
        )

    def failed_count(self) -> int:
        return sum(1 for s in self.steps if s.status == "failed")

    def duration_ms(self) -> int:
        return sum(s.duration_ms for s in self.steps)

    # -- alerts -------------------------------------------------------------
    def evaluate_alerts(self) -> list[dict[str, str]]:
        """The three documented alerts: latency, failure rate, cost."""
        alerts: list[dict[str, str]] = []
        slowest = max((s.duration_ms for s in self.steps), default=0)
        if slowest > ALERT_LATENCY_MS:
            alerts.append(
                {
                    "type": "LATENCY",
                    "message": (
                        f"Step latency {slowest} ms exceeded the "
                        f"{ALERT_LATENCY_MS} ms threshold."
                    ),
                }
            )
        total = len(self.steps)
        failed = self.failed_count()
        rate = (failed / total * 100) if total else 0.0
        if rate > ALERT_FAILURE_RATE_PCT:
            alerts.append(
                {
                    "type": "FAILURE_RATE",
                    "message": (
                        f"Failure rate {rate:.0f}% ({failed}/{total} steps) "
                        f"exceeded the {ALERT_FAILURE_RATE_PCT:.0f}% threshold."
                    ),
                }
            )
        cost = self.estimated_cost()
        if cost > ALERT_COST_LIMIT_USD:
            alerts.append(
                {
                    "type": "COST",
                    "message": (
                        f"Estimated LLM cost ${cost:.2f} exceeded the "
                        f"${ALERT_COST_LIMIT_USD:.2f} threshold."
                    ),
                }
            )
        return alerts

    # -- persistence ----------------------------------------------------------
    def finish(self, status: str) -> dict[str, Any]:
        """Freeze the run, write the JSON log and the CSV row, and summarise."""
        summary = self.summarize(status)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.runs_dir / f"{self.correlation_id}.json"
        log_path.write_text(
            json.dumps(summary, indent=2, default=str), encoding="utf-8"
        )
        _append_csv(self.runs_dir / "executions.csv", summary)
        return summary

    def summarize(self, status: str) -> dict[str, Any]:
        alerts = self.evaluate_alerts()
        totals = self.token_totals()
        return {
            "correlation_id": self.correlation_id,
            "started_at": self.started_at,
            "warehouse": self.meta.get("warehouse", ""),
            "status": status,
            "steps": len(self.steps),
            "failed": self.failed_count(),
            "duration_ms": self.duration_ms(),
            "input_tokens": totals["input_tokens"],
            "output_tokens": totals["output_tokens"],
            "estimated_cost_usd": self.estimated_cost(),
            "alerts": alerts,
            "steps_detail": [asdict(s) for s in self.steps],
            "usage_by_role": [h.snapshot() for h in self.handlers.values()],
            "meta": self.meta,
        }


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def _append_csv(path: Path, summary: dict[str, Any]) -> None:
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow(
            {
                "correlation_id": summary["correlation_id"],
                "started_at": summary["started_at"],
                "warehouse": summary["warehouse"],
                "status": summary["status"],
                "steps": summary["steps"],
                "failed": summary["failed"],
                "duration_ms": summary["duration_ms"],
                "input_tokens": summary["input_tokens"],
                "output_tokens": summary["output_tokens"],
                "est_cost_usd": summary["estimated_cost_usd"],
                "alerts": " ; ".join(
                    f"{a['type']}: {a['message']}" for a in summary["alerts"]
                ),
            }
        )


def read_executions(path: Path | None = None) -> list[dict[str, str]]:
    """The executions table, newest first."""
    target = path or _executions_csv()
    if not target.exists():
        return []
    with target.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    rows.reverse()
    return rows


# ---------------------------------------------------------------------------
# The dashboard - a self-contained HTML supervision page
# ---------------------------------------------------------------------------
_CARD_STYLE = (
    "border:1px solid #d6dee8;border-radius:10px;padding:14px 18px;"
    "min-width:150px;text-align:center;background:#f8fafc"
)
_PAGE_STYLE = (
    "font-family:Segoe UI,Arial,sans-serif;max-width:1100px;margin:24px auto;"
    "color:#1c2b3a"
)


def build_dashboard(
    out_path: Path | None = None,
    *,
    executions_csv: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Render the supervision dashboard from ``runs/executions.csv``.

    Returns the path and the summary it displays (also handy for tests).
    """
    rows = read_executions(executions_csv)
    out_path = out_path or (ROOT / "dashboard.html")

    total = len(rows)
    ok = sum(1 for r in rows if r["status"] == "ok")
    success_rate = (ok / total * 100) if total else 0.0
    latencies = [int(r["duration_ms"]) for r in rows if r["duration_ms"]]
    avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
    total_cost = sum(float(r["est_cost_usd"] or 0) for r in rows)
    total_alerts = sum(1 for r in rows if r["alerts"])

    summary = {
        "runs": total,
        "success_rate_pct": round(success_rate, 1),
        "avg_latency_ms": int(avg_latency),
        "total_cost_usd": round(total_cost, 2),
        "alerting_runs": total_alerts,
    }

    def esc(value: Any) -> str:
        return html.escape(str(value))

    execution_rows = "".join(
        "<tr>"
        f"<td><code>{esc(r['correlation_id'])}</code></td>"
        f"<td><span style='color:{'#1a7f37' if r['status'] == 'ok' else '#c62828'};font-weight:600'>"
        f"{esc(r['status'])}</span></td>"
        f"<td>{esc(r['started_at'])}</td>"
        f"<td>{esc(r['warehouse'])}</td>"
        f"<td>{esc(r['steps'])}</td>"
        f"<td>{esc(r['failed'])}</td>"
        f"<td>{esc(r['duration_ms'])}</td>"
        f"<td>${esc(r['est_cost_usd'])}</td>"
        f"<td style='color:#c0392b'>{esc(r['alerts'] or '-')}</td>"
        "</tr>"
        for r in rows
    )

    alert_cards = "".join(
        f"<div style='{_CARD_STYLE}'><b>{name}</b><br><small>{desc}</small></div>"
        for name, desc in (
            ("Alerte Latence", f"one step &gt; {ALERT_LATENCY_MS} ms"),
            ("Alerte Taux d'échec", f"&gt; {ALERT_FAILURE_RATE_PCT:.0f}% of steps fail"),
            ("Alerte Coût", f"run &gt; ${ALERT_COST_LIMIT_USD:.2f}"),
        )
    )

    alert_lines = "".join(
        f"<li><code>{esc(r['correlation_id'])}</code> - {esc(r['alerts'])}</li>"
        for r in rows
        if r["alerts"]
    ) or "<li>No alert recorded.</li>"

    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Stock reorder - supervision dashboard</title></head>
<body style="{_PAGE_STYLE}">
<h1 style="margin-bottom:4px">Supervision dashboard</h1>
<p style="color:#64748b">Stock reorder multi-agent pipeline - built {esc(datetime.now().strftime("%Y-%m-%d %H:%M"))}
from {total} recorded run(s) in {esc(str(executions_csv or _executions_csv()))}.</p>

<div style="display:flex;gap:14px;flex-wrap:wrap;margin:18px 0">
  <div style="{_CARD_STYLE}">Success rate<br><b style="font-size:22px">{summary['success_rate_pct']}%</b></div>
  <div style="{_CARD_STYLE}">Average latency<br><b style="font-size:22px">{summary['avg_latency_ms']} ms</b></div>
  <div style="{_CARD_STYLE}">Total cost<br><b style="font-size:22px">${summary['total_cost_usd']}</b></div>
  <div style="{_CARD_STYLE}">Runs<br><b style="font-size:22px">{summary['runs']}</b></div>
  <div style="{_CARD_STYLE}">Alerts<br><b style="font-size:22px">{summary['alerting_runs']}</b></div>
</div>

<h2>The 3 documented alerts</h2>
<div style="display:flex;gap:12px;margin:10px 0 20px">{alert_cards}</div>

<h2>Executions (correlation_id)</h2>
<table border="0" cellpadding="6" cellspacing="0"
  style="border-collapse:collapse;width:100%;font-size:14px">
<tr style="background:#eef3f8;text-align:left">
  <th>Correlation ID</th><th>Status</th><th>Started</th><th>Warehouse</th>
  <th>Steps</th><th>Failed</th><th>Duration ms</th><th>Cost</th><th>Alerts</th>
</tr>
{execution_rows}
</table>

<h2>Last alerts</h2>
<ul>{alert_lines}</ul>

<p style="color:#64748b;font-size:12px">
Traces: every executed step of a run is in runs/&lt;correlation_id&gt;.json.
The raw table is runs/executions.csv (open it in Google Sheets).
</p>
</body></html>"""

    out_path.write_text(document, encoding="utf-8")
    return out_path, summary
