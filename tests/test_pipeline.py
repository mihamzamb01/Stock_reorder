"""End-to-end pipeline test with stubbed models.

No API keys required: every agent's model is replaced by a deterministic stub
that derives its answer from the same prompt payload the real model receives.
This exercises the graph wiring, the SQL layer and all the reconciliation
guardrails.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langchain_core.runnables import RunnableLambda  # noqa: E402

from agents.base import AgentRuntime  # noqa: E402
from config.settings import ModelSpec, Settings  # noqa: E402
from db.repository import get_engine  # noqa: E402
from db.setup import seed_database  # noqa: E402
from graph import build_graph  # noqa: E402
from schemas import (  # noqa: E402
    CostPlan,
    ReorderDecision,
    SupplierShortlist,
)


# ---------------------------------------------------------------------------
# Stub model
# ---------------------------------------------------------------------------
def _payload(messages: list[Any]) -> dict[str, Any]:
    for message in reversed(messages):
        content = getattr(message, "content", "")
        if isinstance(content, str) and content.strip().startswith("{"):
            return json.loads(content)
    return {}


class StubLLM:
    """Minimal ChatOpenAI stand-in that returns schema-valid objects."""

    def __init__(self, handler):
        self.handler = handler

    def with_structured_output(self, schema, method=None, **kwargs):
        def _run(messages):
            return self.handler(schema, _payload(messages))

        return RunnableLambda(_run)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
def reorder_handler(schema, payload):
    """One reorder decision per dispatched SKU."""
    candidate = payload.get("sku") or payload.get("candidate") or {}
    return ReorderDecision(
        sku=candidate["sku"],
        product_name=candidate.get("product_name", ""),
        urgency=candidate.get("rule_urgency", "medium"),
        order_qty=candidate.get("min_required_qty", 0),
        days_of_cover=candidate.get("days_of_cover", 0.0),
        stockout_in_days=candidate.get("days_of_cover", 0.0),
        rationale="stub - ordering the computed floor",
    )


def supplier_handler(schema, payload):
    """Rank the offers per SKU, exactly as the real scout returns.

    Only the offers that passed the deterministic feasibility and quality
    filters reach the agent, so the stub simply ranks what it is given.
    """
    entries = []
    for spec in payload.get("skus", []):
        offers = spec.get("offers") or []
        ordered = sorted(offers, key=lambda o: o["landed_price"])
        entries.append(
            {
                "sku": spec["sku"],
                "required_qty": spec["required_qty"],
                "preferred_offer_id": ordered[0]["offer_id"] if ordered else 0,
                "runner_up_offer_id": ordered[1]["offer_id"] if len(ordered) > 1 else None,
                "exclude_offer_ids": [],
                "notes": "stub - cheapest feasible offer",
            }
        )
    return SupplierShortlist(entries=entries, notes="stub")


def cost_handler(schema, payload):
    """Always buys from the most expensive viable supplier (to exercise the budget retry)."""
    allocations = []
    for spec in payload.get("skus", []):
        offers = spec.get("offers") or []
        if not offers:
            continue
        chosen = max(offers, key=lambda o: o["landed_unit_price"])
        qty = spec["required_qty"]
        unit_price = chosen["effective_unit_price"]
        shipping = chosen["shipping_per_unit"] * qty
        allocations.append(
            {
                "sku": spec["sku"],
                "product_name": spec.get("product_name", ""),
                "offer_id": chosen["offer_id"],
                "supplier_id": chosen["supplier_id"],
                "supplier_name": chosen["supplier_name"],
                "order_qty": qty,
                "unit_price": unit_price,
                "shipping_cost": shipping,
                "line_total": unit_price * qty + shipping,
                "landed_unit_cost": unit_price + chosen["shipping_per_unit"],
                "lead_time_days": chosen["lead_time_days"],
                "rationale": "stub - intentionally expensive",
            }
        )
    total = sum(a["line_total"] for a in allocations)
    baseline = payload.get("baseline_spend_cheapest_supplier", 0.0)
    return CostPlan(
        allocations=allocations,
        total_spend=total,
        baseline_spend=baseline,
        projected_savings=baseline - total,
        currency="USD",
        notes="stub",
    )


def empty_cost_handler(schema, payload):
    """A cost optimiser that cannot find a plan at all."""
    return CostPlan(
        allocations=[],
        total_spend=0.0,
        baseline_spend=payload.get("baseline_spend_cheapest_supplier", 0.0),
        projected_savings=0.0,
        currency="USD",
        notes="no feasible plan",
    )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------
def make_runtime(tmp_db: Path, **settings_kwargs) -> AgentRuntime:
    routing = {
        role: ModelSpec(role=role, provider="openrouter", model="stub/model")
        for role in ("reorder_analyst", "supplier_scout", "cost_optimizer")
    }
    llms = {
        "reorder_analyst": StubLLM(reorder_handler),
        "supplier_scout": StubLLM(supplier_handler),
        "cost_optimizer": StubLLM(cost_handler),
    }
    settings = Settings(**settings_kwargs)
    return AgentRuntime(
        engine=get_engine(tmp_db), llms=llms, routing=routing, settings=settings
    )


def run_pipeline(rt: AgentRuntime, **overrides) -> dict[str, Any]:
    app = build_graph(rt)
    initial: dict[str, Any] = {
        "run_id": "test",
        "warehouse_code": "WH-NORTH",
        "max_items": 5,
        "budget_cap": None,
        "focus_skus": [],
        "cost_attempt": 0,
        "max_cost_attempts": 2,
        "messages": [],
        "audit": [],
    }
    initial.update(overrides)
    return app.invoke(initial, config={"recursion_limit": 50})


def test_pipeline(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    seed_database(db)
    rt = make_runtime(db, max_items=5, max_cost_attempts=2)

    result = run_pipeline(rt)

    report = result["report"]
    assert report["lines"], "expected at least one order line"
    assert report["total_spend"] > 0

    # Totals must be internally consistent.
    line_sum = round(sum(line["line_total"] for line in report["lines"]), 2)
    assert abs(line_sum - report["total_spend"]) < 0.01, (line_sum, report["total_spend"])

    # Every agent must have contributed to the trail.
    roles = [entry["role"] for entry in result["audit"]]
    for role in ("db_scan", "supplier_scout", "cost_optimizer", "report"):
        assert role in roles, f"missing audit entry for {role}: {roles}"
    # The reorder analysts fan out into one agent per short SKU.
    analysts = [r for r in roles if r.startswith("reorder/")]
    assert len(analysts) >= len(report["lines"]), analysts
    assert "reorder/merge" in roles

    # The DB scan must have found the stressed warehouse's SKUs.
    scan = result["scan"]
    assert scan["candidates"], "expected at least one candidate SKU"
    assert scan["warehouse_code"] == "WH-NORTH"

    # Quantities must match what the analysts asked for. The only permitted
    # deviation is a supplier MOQ forcing an over-buy.
    required = {
        d["sku"]: d["order_qty"] for d in result["reorder_plan"]["decisions"]
    }
    offer_index = {
        o["offer_id"]: o
        for entry in result["shortlist"]["entries"]
        for o in entry["offers"]
    }
    allocated: dict[str, int] = {}
    for allocation in result["cost_plan"]["allocations"]:
        allocated[allocation["sku"]] = allocated.get(allocation["sku"], 0) + allocation["order_qty"]
    for sku, qty in allocated.items():
        assert qty >= required[sku], f"{sku}: allocated {qty} < required {required[sku]}"
        moq = max(
            offer_index[a["offer_id"]]["moq"]
            for a in result["cost_plan"]["allocations"]
            if a["sku"] == sku
        )
        assert qty - required[sku] < moq, f"{sku}: over-buy {qty - required[sku]} is not MOQ-driven"

    # MOQ must be respected on every line.
    for allocation in result["cost_plan"]["allocations"]:
        offer = offer_index[allocation["offer_id"]]
        assert allocation["order_qty"] >= offer["moq"], allocation

    print(f"  lines={len(report['lines'])} spend={report['total_spend']} ok")
    rt.engine.dispose()


def test_budget_retry_loop(tmp_path: Path) -> None:
    """A budget below the cheapest plan forces a retry, then the run still completes."""
    db = tmp_path / "budget.db"
    seed_database(db)
    rt = make_runtime(db, max_items=3, max_cost_attempts=2)

    result = run_pipeline(rt, budget_cap=1.0)

    attempts = [e for e in result["audit"] if e["role"] == "cost_optimizer"]
    assert len(attempts) == 2, f"expected 2 cost attempts, got {len(attempts)}"
    assert result["cost_status"] == "over_budget"
    assert result["report"]["warnings"], "expected a budget warning in the report"
    print(f"  cost attempts={len(attempts)} status={result['cost_status']} ok")
    rt.engine.dispose()


def test_no_reorder_needed(tmp_path: Path) -> None:
    """WH-CENTRAL is mostly healthy; the pipeline must still terminate cleanly."""
    db = tmp_path / "healthy.db"
    seed_database(db)
    rt = make_runtime(db, max_items=5)

    result = run_pipeline(rt, warehouse_code="WH-CENTRAL")

    assert "report" in result
    assert result["scan"]["candidates"], "WH-CENTRAL still has lines to reorder"
    print(f"  lines={len(result['report']['lines'])} ok")
    rt.engine.dispose()


def test_empty_plan_terminates(tmp_path: Path) -> None:
    """An empty purchase plan must not dead-end the graph."""
    db = tmp_path / "empty.db"
    seed_database(db)
    rt = make_runtime(db, max_items=3)
    rt.llms["cost_optimizer"] = StubLLM(empty_cost_handler)

    result = run_pipeline(rt)

    assert "report" in result, "the graph must reach the report node"
    assert result["report"]["lines"] == []
    assert result["cost_status"] == "no_plan"
    print("  empty plan terminates cleanly")
    rt.engine.dispose()


if __name__ == "__main__":
    import tempfile

    failures = 0
    for test in (
        test_pipeline,
        test_budget_retry_loop,
        test_no_reorder_needed,
        test_empty_plan_terminates,
    ):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            print(f"[RUN] {test.__name__}")
            try:
                test(Path(tmp))
                print(f"[PASS] {test.__name__}")
            except AssertionError as exc:
                failures += 1
                print(f"[FAIL] {test.__name__}: {exc}")
            except Exception as exc:  # pragma: no cover
                failures += 1
                import traceback

                traceback.print_exc()
                print(f"[ERROR] {test.__name__}: {exc}")
    sys.exit(1 if failures else 0)
