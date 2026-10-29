"""LangGraph state for the stock-reorder pipeline."""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


def _merge_dicts(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Append-only reducer for audit and decision entries."""
    return left + right


def _replace(_left: Any, right: Any) -> Any:
    """Last writer wins."""
    return right


class ReorderState(TypedDict, total=False):
    """State shared by every node in the graph."""

    # --- run inputs -------------------------------------------------------
    run_id: str
    warehouse_code: str
    max_items: int
    budget_cap: float | None
    focus_skus: list[str]

    # --- stage outputs -----------------------------------------------------
    scan: dict[str, Any]  # deterministic DB read
    reorder_plan: dict[str, Any]  # merged analyst decisions
    shortlist: dict[str, Any]  # screened supplier offers
    cost_plan: dict[str, Any]  # cheapest landed-cost allocation
    report: dict[str, Any]  # final purchase plan

    # --- parallel fan-out collections -------------------------------------
    reorder_decisions: Annotated[list[dict[str, Any]], _merge_dicts]
    analyst_tasks: list[dict[str, Any]]

    # --- control flow -----------------------------------------------------
    cost_attempt: int
    max_cost_attempts: int
    cost_feedback: str
    cost_status: str  # "ok" | "over_budget" | "no_plan"

    # --- observability ----------------------------------------------------
    messages: Annotated[list[AnyMessage], add_messages]
    audit: Annotated[list[dict[str, Any]], _merge_dicts]
    db_rows: Annotated[dict[str, Any], _replace]
