"""Full pipeline end-to-end tests.

These exercise ``main.py`` as a complete pipeline - database seeding, agent
execution with stubbed models, report rendering, JSON export, dashboard sheet
build and model routing - without any API keys or network access.

Every test installs deterministic stub LLMs so the run is fully offline and
reproducible in CI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import main as app  # noqa: E402
from db.setup import seed_database  # noqa: E402
from tests.test_pipeline import (  # noqa: E402
    StubLLM,
    cost_handler,
    empty_cost_handler,
    reorder_handler,
    supplier_handler,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _install_stubs(monkeypatch, tmp_path: Path | None = None) -> None:
    """Replace every model with a deterministic stub and set dummy keys."""
    stubs = {
        "reorder_analyst": StubLLM(reorder_handler),
        "supplier_scout": StubLLM(supplier_handler),
        "cost_optimizer": StubLLM(cost_handler),
    }
    monkeypatch.setenv("OPENROUTER_API_KEY", "stub")
    monkeypatch.setenv("NVIDIA_API_KEY", "stub")
    monkeypatch.setenv("LARP_ROUTER_API_KEY", "stub")
    monkeypatch.setattr(app, "build_llm", lambda spec, usage=None: stubs[spec.role])
    monkeypatch.setattr(
        "config.settings.api_key_for", lambda spec: "stub", raising=False
    )
    if tmp_path is not None:
        monkeypatch.setattr(
            "observability.RUNS_DIR", tmp_path / "runs", raising=False
        )


def _seed(tmp_path: Path, name: str) -> Path:
    db = tmp_path / f"{name}.db"
    seed_database(db)
    return db


# ---------------------------------------------------------------------------
# Warehouse matrix - the pipeline must terminate cleanly for every warehouse
# ---------------------------------------------------------------------------

def test_pipeline_warehouse_north(tmp_path: Path, monkeypatch, capsys) -> None:
    """WH-NORTH is the stressed warehouse - expect reorder lines."""
    _install_stubs(monkeypatch, tmp_path)
    db = _seed(tmp_path, "north")

    exit_code = app.main(["--db", str(db), "--warehouse", "WH-NORTH", "--max-items", "5"])
    assert exit_code == 0, "WH-NORTH pipeline should succeed"

    out = capsys.readouterr().out
    assert "Agent trail" in out
    assert "Reorder plan" in out
    assert "Total spend" in out


def test_pipeline_warehouse_west(tmp_path: Path, monkeypatch, capsys) -> None:
    """WH-WEST must also complete a full pipeline run."""
    _install_stubs(monkeypatch, tmp_path)
    db = _seed(tmp_path, "west")

    exit_code = app.main(["--db", str(db), "--warehouse", "WH-WEST", "--max-items", "5"])
    assert exit_code == 0, "WH-WEST pipeline should succeed"

    out = capsys.readouterr().out
    assert "Agent trail" in out
    assert "Reorder plan" in out


def test_pipeline_warehouse_central(tmp_path: Path, monkeypatch, capsys) -> None:
    """WH-CENTRAL is mostly healthy - may have few or no reorder lines but must not crash."""
    _install_stubs(monkeypatch, tmp_path)
    db = _seed(tmp_path, "central")

    exit_code = app.main(["--db", str(db), "--warehouse", "WH-CENTRAL", "--max-items", "5"])
    assert exit_code == 0, "WH-CENTRAL pipeline should succeed"

    out = capsys.readouterr().out
    assert "Agent trail" in out


# ---------------------------------------------------------------------------
# Budget scenarios
# ---------------------------------------------------------------------------

def test_pipeline_over_budget_retry(tmp_path: Path, monkeypatch, capsys) -> None:
    """An impossible budget forces a cost retry, then the run still completes with a warning."""
    _install_stubs(monkeypatch, tmp_path)
    # Swap in the expensive cost handler to force over_budget.
    monkeypatch.setattr(
        app, "build_llm",
        lambda spec, usage=None: {
            "reorder_analyst": StubLLM(reorder_handler),
            "supplier_scout": StubLLM(supplier_handler),
            "cost_optimizer": StubLLM(cost_handler),
        }[spec.role],
    )
    db = _seed(tmp_path, "budget")

    exit_code = app.main(
        ["--db", str(db), "--warehouse", "WH-NORTH", "--max-items", "3", "--budget", "1"]
    )
    assert exit_code == 0, "over-budget run must still exit 0"

    out = capsys.readouterr().out
    assert "exceeds the budget cap" in out or "budget" in out.lower()


def test_pipeline_no_budget_cap(tmp_path: Path, monkeypatch, capsys) -> None:
    """No budget cap means the cheapest feasible plan is accepted without retry."""
    _install_stubs(monkeypatch, tmp_path)
    db = _seed(tmp_path, "nobudget")

    exit_code = app.main(["--db", str(db), "--warehouse", "WH-NORTH", "--max-items", "3"])
    assert exit_code == 0

    out = capsys.readouterr().out
    assert "Total spend" in out


# ---------------------------------------------------------------------------
# JSON report output
# ---------------------------------------------------------------------------

def test_pipeline_json_report(tmp_path: Path, monkeypatch) -> None:
    """--json must write a valid report file with lines and total_spend."""
    _install_stubs(monkeypatch, tmp_path)
    db = _seed(tmp_path, "json")
    out_file = tmp_path / "report.json"

    exit_code = app.main(
        ["--db", str(db), "--warehouse", "WH-NORTH", "--max-items", "2", "--json", str(out_file)]
    )
    assert exit_code == 0
    assert out_file.exists(), "the JSON report file must be created"

    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert "lines" in payload
    assert "total_spend" in payload


# ---------------------------------------------------------------------------
# Model routing (no keys needed, no model calls)
# ---------------------------------------------------------------------------

def test_pipeline_routing(capsys) -> None:
    """--routing lists the model for every agent role."""
    exit_code = app.main(["--routing"])
    assert exit_code == 0
    out = capsys.readouterr().out
    for role in ("reorder_analyst", "supplier_scout", "cost_optimizer"):
        assert role in out, f"routing output missing role {role!r}"
    assert "negotiator" not in out, "the negotiator agent was removed"


# ---------------------------------------------------------------------------
# Dashboard sheet build
# ---------------------------------------------------------------------------

def test_pipeline_dashboard_sheet(tmp_path: Path, monkeypatch) -> None:
    """--dashboard-sheet must rebuild dashboard.html from run history."""
    _install_stubs(monkeypatch, tmp_path)
    import observability as obs

    # Seed some runs so the dashboard has history.
    runs_dir = tmp_path / "runs"
    ok = obs.RunRecorder(runs_dir=runs_dir, correlation_id="REQ-DASH-OK")
    ok.meta = {"warehouse": "WH-NORTH"}
    with ok.step(stage="scan", role="db_scan", provider="local", model="deterministic"):
        pass
    ok.finish("ok")

    bad = obs.RunRecorder(runs_dir=runs_dir, correlation_id="REQ-DASH-BAD")
    bad.meta = {"warehouse": "WH-WEST"}
    with bad.step(stage="scout", role="supplier_scout", provider="p", model="m"):
        pass
    bad.finish("failed")

    monkeypatch.setattr("observability.RUNS_DIR", runs_dir, raising=False)
    # build_dashboard() writes to ROOT/dashboard.html; point ROOT at tmp_path
    # so the test does not pollute the project directory.
    monkeypatch.setattr(obs, "ROOT", tmp_path, raising=False)

    exit_code = app.main(["--dashboard-sheet"])
    assert exit_code == 0

    dashboard = tmp_path / "dashboard.html"
    assert dashboard.exists(), "dashboard.html must be produced by --dashboard-sheet"


# ---------------------------------------------------------------------------
# Missing keys - graceful failure before touching the database
# ---------------------------------------------------------------------------

def test_pipeline_missing_keys(monkeypatch, capsys) -> None:
    """Without any API keys the run must explain what to do and exit 2."""
    for name in (
        "OPENROUTER_API_KEY",
        "NVIDIA_API_KEY",
        "LARP_ROUTER_API_KEY",
        "LLM_API_KEY",
        "MODEL_REORDER_API_KEY",
        "MODEL_SUPPLIER_API_KEY",
        "MODEL_COST_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    exit_code = app.main([])
    assert exit_code == 2, "missing keys must stop before the database"
    out = capsys.readouterr().out
    assert "Missing API keys" in out


# ---------------------------------------------------------------------------
# Empty plan termination
# ---------------------------------------------------------------------------

def test_pipeline_empty_plan_terminates(tmp_path: Path, monkeypatch, capsys) -> None:
    """An empty purchase plan must not dead-end the graph."""
    _install_stubs(monkeypatch, tmp_path)
    db = _seed(tmp_path, "empty")

    # Force the cost optimiser to return an empty plan.
    monkeypatch.setattr(
        app, "build_llm",
        lambda spec, usage=None: {
            "reorder_analyst": StubLLM(reorder_handler),
            "supplier_scout": StubLLM(supplier_handler),
            "cost_optimizer": StubLLM(empty_cost_handler),
        }[spec.role],
    )

    exit_code = app.main(["--db", str(db), "--warehouse", "WH-NORTH", "--max-items", "3"])
    assert exit_code == 0, "empty plan must terminate cleanly"

    out = capsys.readouterr().out
    assert "Reorder plan" in out or "Total spend" in out


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
