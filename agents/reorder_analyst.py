"""Agent 1 - Reorder Analyst (one agent per SKU, runs in parallel).

Each agent gets a single SKU's numbers and answers one question: how urgent is
this line, and how much should we order? The arithmetic floor is computed in
:mod:`agents.calc` - the agent explains and prioritises, it does not do maths.
"""

from __future__ import annotations

from typing import Any

from llm import structured_call
from schemas import ReorderDecision

from . import calc
from .base import AgentRuntime, as_messages, trace_message

ROLE = "reorder_analyst"
SHORT_ROLE = "reorder"  # compact label for the agent trail

SYSTEM_PROMPT = """You are a Reorder Analyst for a global procurement team.

You handle ONE SKU. Your job is to judge how urgent the replenishment is and
how much to order, then explain your reasoning in a sentence or two.

The numbers you are given have already been computed from the warehouse
database - stock on hand, what is already on order, a demand forecast built
from 120 days of sales history, and the shortest lead time any supplier can
offer.

Rules:
- `days_of_cover` is how many days of stock are left at the current burn rate.
  Compare it with `lead_time_days`: when cover is shorter than the lead time,
  the line stocks out before a resupply can arrive.
- `min_required_qty` is the hard floor: demand over the replenishment horizon
  minus what is already on hand or on order. NEVER order less than that - it
  guarantees a stockout.
- You MAY order more than the floor when the extra volume is genuinely
  useful: a volume price break that makes the unit price cheaper, or covering
  a rising trend (`demand_trend_pct` above zero) with extra safety margin.
  If you do, say so in the rationale.
- `stockout_in_days` is days of cover when you take the floor quantity. Report
  the real number.
- `urgency`:
    critical  - cover is at or below half the lead time, or stock is already zero
    high      - cover is below the lead time
    medium    - cover is below 1.75x the lead time
    low       - comfortable cover
- `min_required_qty` and the other fields are facts. Do not contradict them.

Return only the structured result."""


def build_line_node(rt: AgentRuntime):
    """One reorder analyst per SKU - these run concurrently."""

    def node(task: dict[str, Any]) -> dict[str, Any]:
        spec = rt.spec(ROLE)
        llm = rt.llm(ROLE)

        candidate = task.get("candidate") or task
        payload = {
            "sku": candidate,
            "instruction": (
                "Decide the urgency and the order quantity for this single SKU. "
                "Order at least min_required_qty."
            ),
        }

        messages = as_messages(SYSTEM_PROMPT, payload)
        decision: ReorderDecision = structured_call(
            llm,
            ReorderDecision,
            messages,
            obs=rt.obs,
            spec=spec,
            stage=f"{SHORT_ROLE}/{candidate.get('sku', '')}",
        )

        reconciled = _reconcile(decision.model_dump(), candidate)
        if reconciled is None:
            return {"reorder_decisions": []}

        return {
            "reorder_decisions": [reconciled],
            "messages": [
                trace_message(
                    f"{reconciled['sku']}: {reconciled['urgency']}, "
                    f"order {reconciled['order_qty']} {candidate.get('unit', 'pcs')}."
                )
            ],
            "audit": [
                {
                    "role": f"{SHORT_ROLE}/{reconciled['sku']}",
                    "provider": spec.provider,
                    "model": spec.model,
                    "summary": (
                        f"{reconciled['urgency']}, order {reconciled['order_qty']} "
                        f"(floor {candidate.get('min_required_qty')})."
                    ),
                }
            ],
        }

    return node


def build_merge_node(rt: AgentRuntime):
    """Collect the parallel decisions into one ordered reorder plan."""

    def node(state: dict[str, Any]) -> dict[str, Any]:
        scan = state.get("scan") or {}
        candidates = {c["sku"]: c for c in scan.get("candidates") or []}
        decisions = list(state.get("reorder_decisions") or [])

        plan_entries: list[dict[str, Any]] = []
        for decision in decisions:
            sku = decision.get("sku")
            candidate = candidates.get(sku)
            if candidate is None:
                continue
            merged = {**candidate, **decision}
            merged["order_qty"] = max(
                int(decision.get("order_qty") or 0), int(candidate.get("min_required_qty") or 0)
            )
            plan_entries.append(merged)

        rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        plan_entries.sort(
            key=lambda d: (rank.get(d.get("urgency"), 4), -int(d.get("order_qty") or 0))
        )

        total_units = sum(int(d.get("order_qty") or 0) for d in plan_entries)
        notes = (
            f"{len(plan_entries)} SKU(s) to reorder, {total_units} units in total."
            if plan_entries
            else "Nothing needs reordering."
        )

        return {
            "reorder_plan": {"decisions": plan_entries, "notes": notes},
            "messages": [trace_message(notes)],
            "audit": [
                {
                    "role": f"{SHORT_ROLE}/merge",
                    "provider": "local",
                    "model": "deterministic",
                    "summary": notes,
                }
            ],
        }

    return node


# ---------------------------------------------------------------------------
def _reconcile(
    decision: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any] | None:
    """Hold the agent to the deterministic floor and to the urgency rules."""
    sku = candidate.get("sku")
    if not sku:
        return None

    floor = int(candidate.get("min_required_qty") or 0)
    order_qty = max(int(decision.get("order_qty") or 0), floor)

    rule_urgency = candidate.get("rule_urgency")
    urgency = decision.get("urgency")
    if rule_urgency == "critical" and urgency not in ("critical", "high"):
        urgency = "critical"
    elif rule_urgency == "high" and urgency == "low":
        urgency = "high"

    cover = float(decision.get("days_of_cover") or candidate.get("days_of_cover") or 0.0)
    return {
        **decision,
        "sku": sku,
        "product_name": candidate.get("product_name", ""),
        "urgency": urgency,
        "order_qty": order_qty,
        "days_of_cover": cover,
        "stockout_in_days": round(
            float(decision.get("stockout_in_days") or candidate.get("days_of_cover") or 0.0), 1
        ),
        "min_required_qty": floor,
    }


def stockout_days(candidate: dict[str, Any]) -> float:
    """Days until the stock runs out if nothing is ordered."""
    return calc.days_of_cover(candidate, candidate.get("forecast_daily_demand") or 0.0)
