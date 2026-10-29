"""LangGraph wiring for the stock-reorder multi-agent pipeline.

    START
      -> db_scan            (deterministic)  read the DB, find the short SKUs
      -> reorder_dispatch                   one task per short SKU
           |  (Send -> parallel fan-out)
           +-> reorder_analyst  (NVIDIA, xN, in parallel)
           |       one agent per SKU: urgency + quantity
           +-> reorder_analyst
           |
      -> reorder_merge      (deterministic)  collect the parallel decisions
      -> supplier_scout     (OpenRouter)     check the DB for supplier offers
      -> cost_optimizer     (OpenRouter)     cheapest landed-cost plan
      -> cost_gate          (deterministic)  budget check
           ^                        |
           |  over budget, retry    |
           +------------------------+
      -> report              (deterministic)  final reorder plan
      -> END
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from agents import (
    cost_optimizer,
    db_scan,
    reorder_analyst,
    report,
    supplier_scout,
)
from agents.base import AgentRuntime, trace_message
from agents.calc import over_budget
from state import ReorderState


def build_graph(rt: AgentRuntime):
    """Compile the reorder graph for the given runtime."""
    graph = StateGraph(ReorderState)

    graph.add_node("db_scan", db_scan.build_node(rt))
    graph.add_node("reorder_dispatch", _dispatch_node(rt))
    graph.add_node("reorder_analyst", reorder_analyst.build_line_node(rt))
    graph.add_node("reorder_merge", reorder_analyst.build_merge_node(rt))
    graph.add_node("supplier_scout", supplier_scout.build_node(rt))
    graph.add_node("cost_optimizer", cost_optimizer.build_node(rt))
    graph.add_node("cost_gate", _cost_gate_node(rt))
    graph.add_node("report", report.build_node(rt))

    graph.add_edge(START, "db_scan")
    graph.add_edge("db_scan", "reorder_dispatch")

    # Fan out: one reorder analyst per short SKU, all in parallel.
    graph.add_conditional_edges(
        "reorder_dispatch",
        _fan_out_reorder,
        ["reorder_analyst", "reorder_merge"],
    )
    graph.add_edge("reorder_analyst", "reorder_merge")

    graph.add_edge("reorder_merge", "supplier_scout")
    graph.add_edge("supplier_scout", "cost_optimizer")
    graph.add_edge("cost_optimizer", "cost_gate")
    graph.add_conditional_edges(
        "cost_gate",
        _route_after_gate,
        {"retry": "cost_optimizer", "proceed": "report"},
    )
    graph.add_edge("report", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Parallel fan-out
# ---------------------------------------------------------------------------
def _dispatch_node(rt: AgentRuntime):
    """Turn the DB scan into one task per short SKU."""

    def node(state: dict[str, Any]) -> dict[str, Any]:
        scan = state.get("scan") or {}
        candidates = scan.get("candidates") or []
        tasks = [{"candidate": candidate} for candidate in candidates]
        summary = (
            f"Dispatching {len(tasks)} reorder analyst(s), one per SKU."
            if tasks
            else "No SKU needs reordering."
        )
        return {
            "analyst_tasks": tasks,
            "messages": [trace_message(summary)],
            "audit": [
                {
                    "role": "reorder_dispatch",
                    "provider": "local",
                    "model": "deterministic",
                    "summary": summary,
                }
            ],
        }

    return node


def _fan_out_reorder(state: dict[str, Any]) -> list[Send] | str:
    """Dispatch one reorder analyst per candidate SKU.

    Falls through to the merge node directly when there is nothing to send, so
    the graph can never dead-end.
    """
    tasks = state.get("analyst_tasks") or []
    sends = [Send("reorder_analyst", task) for task in tasks]
    if not sends:
        return "reorder_merge"
    return sends


# ---------------------------------------------------------------------------
# Deterministic budget gate
# ---------------------------------------------------------------------------
def _cost_gate_node(rt: AgentRuntime):
    """Check the plan against the budget cap and decide whether to retry."""

    def node(state: dict[str, Any]) -> dict[str, Any]:
        plan = state.get("cost_plan") or {}
        budget_cap = state.get("budget_cap")
        attempt = int(state.get("cost_attempt") or 0)

        if not (plan.get("allocations") or []):
            feedback = ""
            status = "no_plan"
        elif over_budget(plan, budget_cap):
            overshoot = round(float(plan.get("total_spend") or 0.0) - float(budget_cap), 2)
            feedback = (
                f"Attempt {attempt + 1} came in {overshoot} over the budget cap of "
                f"{budget_cap}. Reduce spend: prefer a lower landed unit price, "
                "drop volume bought purely for a price break, and consolidate onto "
                "fewer suppliers."
            )
            status = "over_budget"
        else:
            feedback = ""
            status = "ok"

        return {
            "cost_attempt": attempt + 1,
            "cost_feedback": feedback,
            "cost_status": status,
            "messages": [
                trace_message(
                    f"Budget gate: spend {plan.get('total_spend')} vs cap {budget_cap} -> {status}"
                )
            ],
        }

    return node


def _route_after_gate(state: dict[str, Any]) -> str:
    """Retry the cost optimiser while over budget, then move on."""
    status = state.get("cost_status")
    attempt = int(state.get("cost_attempt") or 0)
    max_attempts = int(state.get("max_cost_attempts") or 2)

    if status == "over_budget" and attempt < max_attempts:
        return "retry"
    return "proceed"
