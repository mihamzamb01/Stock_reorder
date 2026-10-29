"""CLI end-to-end full pipeline tests for GitLab CI.

Runs ``main.py`` as a complete pipeline with stubbed models - no API keys,
no network, fully offline. Exits non-zero on any failure.

Usage:
    python tests/ci_full_pipeline.py
"""

from __future__ import annotations

import sys
import tempfile
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_stubs():
    return {
        "reorder_analyst": StubLLM(reorder_handler),
        "supplier_scout": StubLLM(supplier_handler),
        "cost_optimizer": StubLLM(cost_handler),
    }


def install_stubs():
    app.build_llm = lambda spec, usage=None: make_stubs()[spec.role]


def run_pipeline(warehouse: str, max_items: int, budget: float | None = None) -> int:
    """Seed a temp DB and run the full pipeline for one warehouse."""
    tmp = tempfile.mkdtemp()
    try:
        db = Path(tmp) / "ci.db"
        seed_database(db)
        args = ["--db", str(db), "--warehouse", warehouse, "--max-items", str(max_items)]
        if budget is not None:
            args += ["--budget", str(budget)]
        return app.main(args)
    finally:
        # Best-effort cleanup; SQLite file locks on Windows may prevent deletion.
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def run_dashboard() -> int:
    """Build dashboard.html from seeded run history in a temp dir."""
    import shutil

    import observability as obs
    from observability import RunRecorder

    tmp = tempfile.mkdtemp()
    try:
        runs_dir = Path(tmp) / "runs"
        ok = RunRecorder(runs_dir=runs_dir, correlation_id="REQ-CI-OK")
        ok.meta = {"warehouse": "WH-NORTH"}
        with ok.step(stage="scan", role="db_scan", provider="local", model="deterministic"):
            pass
        ok.finish("ok")

        obs.RUNS_DIR = runs_dir
        obs.ROOT = Path(tmp)
        return app.main(["--dashboard-sheet"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [ok] {name}")
    else:
        msg = f"  [FAIL] {name}"
        if detail:
            msg += f" - {detail}"
        print(msg)
        failures.append(name)


def test_routing() -> None:
    print("[TEST] model routing")
    code = app.main(["--routing"])
    check("routing exits 0", code == 0, f"exit code {code}")


def test_wh_north() -> None:
    print("[TEST] full pipeline - WH-NORTH")
    install_stubs()
    code = run_pipeline("WH-NORTH", 5)
    check("WH-NORTH exits 0", code == 0, f"exit code {code}")


def test_wh_west() -> None:
    print("[TEST] full pipeline - WH-WEST")
    install_stubs()
    code = run_pipeline("WH-WEST", 5)
    check("WH-WEST exits 0", code == 0, f"exit code {code}")


def test_wh_central() -> None:
    print("[TEST] full pipeline - WH-CENTRAL")
    install_stubs()
    code = run_pipeline("WH-CENTRAL", 5)
    check("WH-CENTRAL exits 0", code == 0, f"exit code {code}")


def test_budget_retry() -> None:
    print("[TEST] budget over-cap retry")
    install_stubs()
    # Force the expensive cost handler to trigger over_budget.
    app.build_llm = lambda spec, usage=None: {
        "reorder_analyst": StubLLM(reorder_handler),
        "supplier_scout": StubLLM(supplier_handler),
        "cost_optimizer": StubLLM(cost_handler),
    }[spec.role]
    code = run_pipeline("WH-NORTH", 3, budget=1.0)
    check("over-budget exits 0", code == 0, f"exit code {code}")


def test_dashboard() -> None:
    print("[TEST] dashboard sheet build")
    code = run_dashboard()
    check("dashboard exits 0", code == 0, f"exit code {code}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 60)
    print("CI Full Pipeline Tests (stubbed models, offline)")
    print("=" * 60)

    test_routing()
    test_wh_north()
    test_wh_west()
    test_wh_central()
    test_budget_retry()
    test_dashboard()

    print()
    if failures:
        print(f"FAILED: {len(failures)} test(s): {', '.join(failures)}")
        return 1
    print("All CI full pipeline tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
