"""Tests for the React dashboard's API layer (observability_server).

No HTTP: these exercise the payload functions directly against fixture data
written into a temporary runs directory.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import observability_server as server  # noqa: E402
from observability import RunRecorder  # noqa: E402


def _make_history(runs_dir: Path) -> str:
    """One ok run + one failed-with-alert run; returns the ok trace id."""
    ok = RunRecorder(runs_dir=runs_dir, correlation_id="REQ-OK-1")
    ok.meta = {"warehouse": "WH-NORTH"}
    with ok.step(stage="reorder/SKU-1001", role="reorder_analyst",
                 provider="p", model="m"):
        pass
    ok.finish("ok")

    bad = RunRecorder(runs_dir=runs_dir, correlation_id="REQ-BAD-1")
    bad.meta = {"warehouse": "WH-WEST"}
    with bad.step(stage="scout", role="supplier_scout", provider="p", model="m"):
        pass
    bad.steps[-1].duration_ms = 99_999  # fires the latency alert
    bad.steps[-1].status = "failed"
    bad.finish("failed")
    return "REQ-OK-1"


def test_summary_payload(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    _make_history(runs)
    monkeypatch.setattr("observability.RUNS_DIR", runs)
    observation = server.summary_payload()
    assert observation["runs"] == 2
    assert observation["successful_runs"] == 1
    assert observation["success_rate_pct"] == 50.0
    assert observation["alerting_runs"] == 1
    assert observation["thresholds"]["latency_ms"] > 0


def test_executions_payload(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    _make_history(runs)
    monkeypatch.setattr("observability.RUNS_DIR", runs)
    rows = server.executions_payload()
    assert [r["correlation_id"] for r in rows] == ["REQ-BAD-1", "REQ-OK-1"]
    assert set(rows[0]["alerts"]) == {"LATENCY", "FAILURE_RATE"}  # slow AND failed
    assert rows[1]["alerts"] == []
    assert rows[0]["duration_ms"] == 99_999


def test_trace_payload(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    corr = _make_history(runs)
    monkeypatch.setattr("observability.RUNS_DIR", runs)

    trace = server.trace_payload(corr)
    assert trace["correlation_id"] == corr
    assert trace["steps_detail"][0]["stage"] == "reorder/SKU-1001"

    assert server.trace_payload("REQ-DOES-NOT-EXIST") is None


def test_traces_file_is_valid_json_round_trip(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    corr = _make_history(runs)
    monkeypatch.setattr("observability.RUNS_DIR", runs)
    raw = (runs / f"{corr}.json").read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert set(payload) >= {
        "correlation_id", "status", "steps_detail", "usage_by_role", "alerts"
    }
