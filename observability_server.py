"""Local API + static server for the React supervision dashboard.

    python main.py --dashboard              serve http://localhost:8642
    python main.py --dashboard --port 9000  any port
    python -m observability_server          ditto

It exposes what the dashboard needs, straight from the observability layer:

* ``/api/summary``    - success rate, avg latency, total cost, tokens, alerts,
* ``/api/executions`` - one row per pipeline run (the executions table),
* ``/api/traces?correlation_id=REQ-…`` - the full trace of one run: every
  agent step, its duration and model, plus token usage per role and alerts,
* and it serves the built React app from ``dashboard/dist``.

Everything runs on the standard library - no extra dependency for the
recipient, and nothing to deploy: this is a local supervision tool.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import webbrowser
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from config.settings import ROOT

DIST = ROOT / "dashboard" / "dist"
RUNS_DIR_NAME = "runs"

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")

# ---------------------------------------------------------------------------
# Data helpers (kept as plain functions so they are testable)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _obs():
    """Import the observability module (deferred so tests can point RUNS_DIR)."""
    import observability

    return observability


def runs_dir() -> Path:
    return _obs().RUNS_DIR


def summary_payload() -> dict:
    """The KPI block: totals over every recorded run."""
    obs = _obs()
    rows = obs.read_executions()
    total = len(rows)
    ok = sum(1 for r in rows if r["status"] == "ok")
    latencies = [int(r["duration_ms"]) for r in rows if r.get("duration_ms")]
    total_cost = sum(float(r.get("est_cost_usd") or 0) for r in rows)
    total_tokens = sum(
        int(r.get("input_tokens") or 0) + int(r.get("output_tokens") or 0) for r in rows
    )
    alerting = sum(1 for r in rows if r.get("alerts"))
    return {
        "runs": total,
        "successful_runs": ok,
        "success_rate_pct": round((ok / total * 100) if total else 0.0, 1),
        "avg_latency_ms": int(sum(latencies) / len(latencies)) if latencies else 0,
        "total_cost_usd": round(total_cost, 2),
        "total_tokens": total_tokens,
        "alerting_runs": alerting,
        "thresholds": {
            "latency_ms": obs.ALERT_LATENCY_MS,
            "failure_rate_pct": obs.ALERT_FAILURE_RATE_PCT,
            "cost_limit_usd": obs.ALERT_COST_LIMIT_USD,
        },
        "cost_prices": {
            "input_per_mtok": obs.COST_INPUT_PER_MTOK,
            "output_per_mtok": obs.COST_OUTPUT_PER_MTOK,
        },
    }


def executions_payload() -> list[dict]:
    """The executions table, newest first, alerts parsed into lists."""
    obs = _obs()
    out: list[dict] = []
    for row in obs.read_executions():
        alerts_field = row.get("alerts") or ""
        out.append(
            {
                "correlation_id": row.get("correlation_id", ""),
                "started_at": row.get("started_at", ""),
                "warehouse": row.get("warehouse", ""),
                "status": row.get("status", ""),
                "steps": int(row.get("steps") or 0),
                "failed": int(row.get("failed") or 0),
                "duration_ms": int(row.get("duration_ms") or 0),
                "input_tokens": int(row.get("input_tokens") or 0),
                "output_tokens": int(row.get("output_tokens") or 0),
                "est_cost_usd": float(row.get("est_cost_usd") or 0),
                "alerts": [
                    p.strip().split(":", 1)[0]
                    for p in alerts_field.split(";")
                    if p.strip()
                ],
                "alerts_detail": alerts_field,
            }
        )
    return out


def trace_payload(correlation_id: str) -> dict | None:
    """One run's full trace JSON, or None when the run is unknown."""
    path = runs_dir() / f"{correlation_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------
class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "StockReorderDashboard/1.0"

    def log_message(self, fmt: str, *args) -> None:  # silence per-request logs
        pass

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, relative: str) -> None:
        target = (DIST / relative.lstrip("/")).resolve()
        try:
            target.relative_to(DIST.resolve())
        except ValueError:
            self._json(403, {"error": "forbidden"})
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.exists():
            # SPA fallback: unknown paths render the app.
            target = DIST / "index.html"
        if not target.exists():
            self._json(503, {"error": "dashboard not built; run npm run build in dashboard/"})
            return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if relative.startswith("/assets/"):
            # Vite hashes the file names, so these are safe to cache hard.
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            # index.html can never be cached: it references the hashed bundles.
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        route = parsed.path

        if route == "/api/summary":
            self._json(200, summary_payload())
        elif route == "/api/executions":
            self._json(200, executions_payload())
        elif route == "/api/traces":
            params = parse_qs(parsed.query)
            corr = (params.get("correlation_id") or [""])[0]
            trace = trace_payload(corr) if corr else None
            if trace is None:
                self._json(404, {"error": f"no trace for {corr!r}"})
            else:
                self._json(200, trace)
        elif route == "/api/health":
            self._json(200, {"ok": True})
        elif route.startswith("/api/"):
            self._json(404, {"error": f"unknown endpoint {route}"})
        else:
            self._static(route)


def serve(port: int = 8642, open_browser: bool = True) -> None:
    """Serve the dashboard and block until Ctrl+C."""
    if not (DIST / "index.html").exists():
        print(
            "\nThe React dashboard is not built yet.\n"
            "  cd dashboard && npm install && npm run build\n"
            "Then run `python main.py --dashboard` again.\n"
            "In the meantime you can open the tables directly:\n"
            "  runs/executions.csv  (opens in Google Sheets)\n"
        )
        _obs().build_dashboard()
        return

    httpd = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    url = f"http://localhost:{port}"
    print(f"\nSupervision dashboard: {url}")
    print("Reading:", runs_dir())
    print("Press Ctrl+C to stop.\n")
    if open_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Serve the React supervision dashboard")
    parser.add_argument("--port", type=int, default=8642)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    serve(port=args.port, open_browser=not args.no_browser)
