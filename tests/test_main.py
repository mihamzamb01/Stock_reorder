"""Entry-point tests: `main.py` must render the plan and the routing table.

The models are stubbed, so no API keys are needed.
"""

from __future__ import annotations

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
    reorder_handler,
    supplier_handler,
)


def _install_stubs(monkeypatch, tmp_path: Path | None = None) -> None:
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
        # Keep stub-run traces out of the developer's real run history.
        monkeypatch.setattr(
            "observability.RUNS_DIR", tmp_path / "runs", raising=False
        )


def test_main_run(tmp_path: Path, monkeypatch, capsys) -> None:
    """`main.py` should render the agent trail and the reorder table."""
    _install_stubs(monkeypatch, tmp_path)

    db = tmp_path / "main.db"
    seed_database(db)

    exit_code = app.main(["--db", str(db), "--warehouse", "WH-NORTH", "--max-items", "3"])
    assert exit_code == 0, "the run should succeed"

    out = capsys.readouterr().out
    assert "Agent trail" in out
    assert "Reorder plan" in out
    assert "Total spend" in out
    print("  main.py renders the full report")


def test_main_run_with_budget(tmp_path: Path, monkeypatch, capsys) -> None:
    """An impossible budget must still finish and warn the user."""
    _install_stubs(monkeypatch, tmp_path)

    db = tmp_path / "main-budget.db"
    seed_database(db)

    exit_code = app.main(
        ["--db", str(db), "--warehouse", "WH-NORTH", "--max-items", "2", "--budget", "1"]
    )
    assert exit_code == 0

    out = capsys.readouterr().out
    assert "exceeds the budget cap" in out
    print("  budget warning is surfaced")


def test_main_writes_json(tmp_path: Path, monkeypatch, capsys) -> None:
    """The --json flag must write a valid report file."""
    import json

    _install_stubs(monkeypatch, tmp_path)

    db = tmp_path / "main-json.db"
    seed_database(db)
    out_file = tmp_path / "report.json"

    exit_code = app.main(
        ["--db", str(db), "--warehouse", "WH-NORTH", "--max-items", "2", "--json", str(out_file)]
    )
    assert exit_code == 0
    assert out_file.exists()

    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert "lines" in payload
    assert "total_spend" in payload
    print("  report is written to JSON")


def test_main_routing(capsys) -> None:
    """--routing lists the model for every agent role."""
    exit_code = app.main(["--routing"])
    assert exit_code == 0
    out = capsys.readouterr().out
    for role in ("reorder_analyst", "supplier_scout", "cost_optimizer"):
        assert role in out
    assert "negotiator" not in out, "the negotiator agent was removed"
    print("  model routing is listed")


def test_main_missing_keys(monkeypatch, capsys) -> None:
    """Without keys it must explain what to do instead of crashing."""
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
    assert exit_code == 2, "the run must stop before touching the database"
    out = capsys.readouterr().out
    assert "Missing API keys" in out
    print("  missing keys are reported clearly")


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
