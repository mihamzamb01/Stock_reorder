"""Observability tests: redaction, traces, metrics, alerts and the dashboard.

These use stub models and a temp directory - no API keys, no network, no real
runs in the project's own runs/ folder.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import observability as obs  # noqa: E402
from observability import RunRecorder, build_dashboard, new_correlation_id, redact  # noqa: E402


# ---------------------------------------------------------------------------
# Correlation IDs
# ---------------------------------------------------------------------------
def test_correlation_id_shape():
    """REQ-YYYYMMDD-HHMM-XXXX, unique per call."""
    first = new_correlation_id()
    second = new_correlation_id()
    assert first.startswith("REQ-")
    assert len(first.split("-")) == 4
    assert first != second


# ---------------------------------------------------------------------------
# Redaction - the "never log" rule
# ---------------------------------------------------------------------------
def test_redact_masks_emails_cards_and_keys():
    text = "contact ops@firm.com or call 4111 1111 1111 1111, key lrsk-abcdef123456"
    out = redact(text)
    assert "ops@firm.com" not in out
    assert "[email]" in out
    assert "4111" not in out
    assert "[card]" in out
    assert "lrsk-abcdef123456" not in out
    assert "[key]" in out


def test_redact_masks_live_secret_values(monkeypatch):
    monkeypatch.setenv("LARP_ROUTER_API_KEY", "lrsk-supersecretvalue99")
    out = redact("failed auth with key lrsk-supersecretvalue99")
    assert "lrsk-supersecretvalue99" not in out
    assert "[secret]" in out


def test_redact_leaves_normal_text():
    assert redact("SKU-1005 ordered 500 pcs") == "SKU-1005 ordered 500 pcs"


# ---------------------------------------------------------------------------
# Token usage
# ---------------------------------------------------------------------------
def test_usage_handler_counts_calls_thread_safely():
    import threading

    from langchain_core.messages import AIMessage
    from langchain_core.outputs import LLMResult
    from langchain_core.outputs.chat_generation import ChatGeneration

    handler = obs.UsageHandler("reorder_analyst")

    def make_result(prompt: int, completion: int) -> LLMResult:
        generation = ChatGeneration(
            message=AIMessage(
                content="x",
                usage_metadata={
                    "input_tokens": prompt,
                    "output_tokens": completion,
                    "total_tokens": prompt + completion,
                },
            )
        )
        return LLMResult(generations=[[generation]])

    def worker():
        for _ in range(50):
            handler.on_llm_end(make_result(10, 5))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snapshot = handler.snapshot()
    assert snapshot["calls"] == 200
    assert snapshot["input_tokens"] == 200 * 10
    assert snapshot["output_tokens"] == 200 * 5


# ---------------------------------------------------------------------------
# Traces - steps, failures, persistence
# ---------------------------------------------------------------------------
def test_recorder_records_ok_and_failed_steps(tmp_path):
    import pytest

    recorder = RunRecorder(runs_dir=tmp_path / "runs")

    with recorder.step(stage="reorder/SKU-1001", role="reorder_analyst",
                       provider="larprouter", model="glm-5.3-flash"):
        pass  # success

    with pytest.raises(ValueError):
        with recorder.step(stage="scout", role="supplier_scout",
                           provider="larprouter", model="deepseek-v4.1-flash"):
            raise ValueError("boom")  # failure

    summary = recorder.summarize("failed")
    assert summary["steps"] == 2
    assert summary["failed"] == 1
    assert summary["steps_detail"][1]["status"] == "failed"


def test_recorder_failure_step_keeps_redacted_error(tmp_path):
    import pytest

    recorder = RunRecorder(runs_dir=tmp_path / "runs")
    with pytest.raises(ValueError):
        with recorder.step(stage="scout", role="supplier_scout",
                           provider="p", model="m"):
            raise ValueError("auth failed lrsk-livevalue12345678")
    step = recorder.steps[-1]
    assert step.status == "failed"
    assert "lrsk-livevalue" not in step.error or "[key]" in step.error


def test_finish_writes_json_log_and_csv_row(tmp_path):
    recorder = RunRecorder(runs_dir=tmp_path / "runs")
    recorder.meta = {"warehouse": "WH-NORTH"}
    with recorder.step(stage="reorder/SKU-1001", role="reorder_analyst",
                       provider="larprouter", model="glm-5.3-flash"):
        pass
    recorder.finish("ok")

    log_path = tmp_path / "runs" / f"{recorder.correlation_id}.json"
    data = json.loads(log_path.read_text(encoding="utf-8"))
    assert data["correlation_id"] == recorder.correlation_id
    assert data["warehouse"] == "WH-NORTH"
    assert data["status"] == "ok"
    assert data["steps_detail"][0]["stage"] == "reorder/SKU-1001"

    rows = obs.read_executions(tmp_path / "runs" / "executions.csv")
    assert len(rows) == 1
    assert rows[0]["correlation_id"] == recorder.correlation_id
    assert rows[0]["warehouse"] == "WH-NORTH"
    assert rows[0]["status"] == "ok"


def test_recorder_writes_in_threaded_simulation(tmp_path):
    """Parallel analyst nodes all land in one ordered trace."""
    recorder = RunRecorder(runs_dir=tmp_path / "runs")

    def work(tag: str) -> None:
        with recorder.step(stage=f"reorder/{tag}", role="reorder_analyst",
                           provider="larprouter", model="m"):
            pass

    threads = [threading.Thread(target=work, args=(f"SKU-{i}",)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(recorder.steps) == 5
    assert recorder.summarize("ok")["steps"] == 5


# ---------------------------------------------------------------------------
# The 3 documented alerts
# ---------------------------------------------------------------------------
def test_latency_alert_fires(monkeypatch):
    monkeypatch.setattr(obs, "ALERT_LATENCY_MS", 100)
    recorder = RunRecorder(runs_dir=Path("/nonexistent"), redact_live_secrets=False)
    with recorder.step(stage="cost", role="cost_optimizer",
                       provider="p", model="m"):
        pass
    recorder.steps[-1].duration_ms = 400  # force a slow step
    types = {a["type"] for a in recorder.evaluate_alerts()}
    assert "LATENCY" in types


def test_failure_rate_alert_fires():
    import pytest

    recorder = RunRecorder(runs_dir=Path("/nonexistent"), redact_live_secrets=False)
    for index in range(4):
        if index < 2:
            with pytest.raises(ValueError):
                with recorder.step(stage=f"s{index}", role="r", provider="p", model="m"):
                    raise ValueError("fail")
        else:
            with recorder.step(stage=f"s{index}", role="r", provider="p", model="m"):
                pass
    types = {a["type"] for a in recorder.evaluate_alerts()}
    assert "FAILURE_RATE" in types


def test_cost_alert_fires(monkeypatch):
    monkeypatch.setattr(obs, "ALERT_COST_LIMIT_USD", 1.0)
    monkeypatch.setattr(obs, "COST_INPUT_PER_MTOK", 10.0)
    monkeypatch.setattr(obs, "COST_OUTPUT_PER_MTOK", 0.0)
    recorder = RunRecorder(runs_dir=Path("/nonexistent"), redact_live_secrets=False)

    class FakeHandler:
        def __init__(self) -> None:
            self.calls = 1
            self.input_tokens = 500_000
            self.output_tokens = 0
            self._lock = threading.Lock()

        def snapshot(self):
            return {
                "role": "cost_optimizer",
                "calls": self.calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
            }

    recorder.handlers["cost_optimizer"] = FakeHandler()  # type: ignore[assignment]
    types = {a["type"] for a in recorder.evaluate_alerts()}
    assert "COST" in types


def test_token_totals_derive_from_handlers():
    recorder = RunRecorder(runs_dir=Path("/nonexistent"), redact_live_secrets=False)
    handler = recorder.usage_handler("reorder_analyst")
    handler.calls = 3  # type: ignore[misc]
    handler.input_tokens = 120  # type: ignore[misc]
    handler.output_tokens = 60  # type: ignore[misc]

    assert recorder.token_totals() == {
        "calls": 3,
        "input_tokens": 120,
        "output_tokens": 60,
    }
    assert recorder.summarize("ok")["input_tokens"] == 120


# ---------------------------------------------------------------------------
# The dashboard
# ---------------------------------------------------------------------------
def test_dashboard_builds_from_csv(tmp_path):
    csv_path = tmp_path / "executions.csv"
    rows = [
        {
            "correlation_id": "REQ-20261026-1010-aaaa",
            "started_at": "2026-10-26T10:10:00+00:00",
            "warehouse": "WH-NORTH",
            "status": "ok",
            "steps": "6",
            "failed": "0",
            "duration_ms": "8000",
            "input_tokens": "1000",
            "output_tokens": "500",
            "est_cost_usd": "0.0",
            "alerts": "",
        },
        {
            "correlation_id": "REQ-20261026-1020-bbbb",
            "started_at": "2026-10-26T10:20:00+00:00",
            "warehouse": "WH-WEST",
            "status": "failed",
            "steps": "6",
            "failed": "2",
            "duration_ms": "40000",
            "input_tokens": "2000",
            "output_tokens": "800",
            "est_cost_usd": "12.5",
            "alerts": "LATENCY: step latency 40000 ms; COST: $12.50",
        },
    ]
    import csv as _csv

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = _csv.DictWriter(handle, fieldnames=obs.CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    out_path = tmp_path / "dashboard.html"
    path, summary = build_dashboard(out_path, executions_csv=csv_path)

    assert path == out_path
    assert summary["runs"] == 2
    assert summary["success_rate_pct"] == 50.0
    assert summary["avg_latency_ms"] == 24000
    assert summary["total_cost_usd"] == 12.5
    assert summary["alerting_runs"] == 1

    page = out_path.read_text(encoding="utf-8")
    assert "REQ-20261026-1010-aaaa" in page
    assert "REQ-20261026-1020-bbbb" in page
    assert "Alerte Latence" in page
    assert "Alerte Taux d'échec" in page
    assert "Alerte Coût" in page


def test_dashboard_handles_no_history(tmp_path):
    path, summary = build_dashboard(
        tmp_path / "dashboard.html", executions_csv=tmp_path / "executions.csv"
    )
    assert summary["runs"] == 0
    assert path.exists()
