"""Agent 3 - Cost Optimizer.

Takes the screened supplier offers and produces the cheapest purchase plan that
still meets the requirement: one order line per SKU (or a split across
suppliers when a price break justifies it).
"""

from __future__ import annotations

from typing import Any

from llm import structured_call
from schemas import CostPlan

from . import calc
from .base import AgentRuntime, as_messages, trace_message

ROLE = "cost_optimizer"

SYSTEM_PROMPT = """You are the Cost Optimizer for a global procurement team.

You receive, per SKU, the required quantity and the viable supplier offers with
their economics already computed. You must produce the purchase plan that
minimises total landed cost while meeting the required quantity.

Rules:
- For every SKU the total ordered quantity MUST equal the required quantity.
- `unit_price` is the price actually payable: use the price-break price when
  the quantity reaches the break, otherwise the list price.
- `shipping_cost` is shipping_per_unit multiplied by the quantity.
- `line_total` = unit_price * order_qty + shipping_cost.
- `landed_unit_cost` = line_total / order_qty.
- A volume price break is often worth ordering more than strictly needed -
  only do that if the extra spend is genuinely small relative to the unit price
  saving, and say so in the rationale.
- Respect the MOQ: never order less than the supplier's `moq`.
- A single supplier per SKU is simplest and usually best for freight and
  administration. Split only when a price break or a lead-time constraint makes
  it clearly better.
- Prefer shorter lead times when two offers cost about the same.
- If a budget cap is provided in the feedback field, keep the total under it.
  If that is impossible, get as close as possible and explain the trade-off in
  `notes`.

Return only the structured result."""


def build_node(rt: AgentRuntime):
    def node(state: dict[str, Any]) -> dict[str, Any]:
        spec = rt.spec(ROLE)
        llm = rt.llm(ROLE)

        shortlist = state.get("shortlist") or {}
        entries = shortlist.get("entries") or []
        if not entries:
            summary = "Nothing to optimise - no shortlist."
            return {
                "cost_plan": {
                    "allocations": [],
                    "total_spend": 0.0,
                    "baseline_spend": 0.0,
                    "projected_savings": 0.0,
                    "notes": "No SKUs to purchase.",
                },
                "cost_status": "no_plan",
                "messages": [trace_message(summary)],
                "audit": [
                    {
                        "role": ROLE,
                        "provider": spec.provider,
                        "model": spec.model,
                        "summary": summary,
                    }
                ],
            }

        required_by_sku: dict[str, int] = {}
        for entry in entries:
            required_by_sku[entry["sku"]] = int(entry.get("required_qty") or 0)

        baseline = calc.baseline_spend(shortlist, required_by_sku)
        budget_cap = state.get("budget_cap")
        feedback = state.get("cost_feedback") or ""

        payload = {
            "skus": [
                {
                    "sku": e["sku"],
                    "product_name": e.get("product_name", ""),
                    "required_qty": required_by_sku.get(e["sku"], 0),
                    "offers": [
                        {
                            "offer_id": o["offer_id"],
                            "supplier_id": o.get("supplier_id"),
                            "supplier_name": o.get("supplier_name", ""),
                            "country": o.get("country", ""),
                            "list_unit_price": o.get("list_unit_price", o.get("unit_price", 0.0)),
                            "effective_unit_price": o.get("effective_unit_price", 0.0),
                            "landed_unit_price": o.get("landed_unit_price", 0.0),
                            "moq": o.get("moq", 1),
                            "price_break_qty": o.get("price_break_qty", 0),
                            "price_break_unit": o.get("price_break_unit"),
                            "shipping_per_unit": o.get("shipping_per_unit", 0.0),
                            "lead_time_days": o.get("lead_time_days", 0),
                            "reliability_score": o.get("reliability_score", 0.0),
                            "viable": o.get("viable", True),
                        }
                        for o in (e.get("offers") or [])
                        if o.get("viable", True)
                    ],
                }
                for e in entries
            ],
            "baseline_spend_cheapest_supplier": baseline,
            "budget_cap": budget_cap,
            "feedback_from_previous_attempt": feedback,
        }

        messages = as_messages(SYSTEM_PROMPT, payload)
        plan: CostPlan = structured_call(
            llm,
            CostPlan,
            messages,
            obs=rt.obs,
            spec=spec,
            stage=f"{ROLE}/attempt{int(state.get('cost_attempt') or 0) + 1}",
        )

        reconciled = calc.reconcile_cost_plan(
            plan.model_dump(), shortlist, required_by_sku
        )
        if budget_cap:
            reconciled["notes"] = (
                f"{reconciled.get('notes', '')} "
                f"(budget cap {budget_cap}, spend {reconciled['total_spend']})"
            ).strip()

        status = "ok" if reconciled["allocations"] else "no_plan"
        summary = (
            f"Planned {len(reconciled['allocations'])} order line(s) totalling "
            f"{reconciled['total_spend']} {reconciled.get('currency', 'USD')} "
            f"(baseline {reconciled['baseline_spend']})."
        )
        return {
            "cost_plan": reconciled,
            "cost_status": status,
            "messages": [trace_message(summary)],
            "audit": [
                {
                    "role": ROLE,
                    "provider": spec.provider,
                    "model": spec.model,
                    "summary": summary,
                    "total_spend": reconciled["total_spend"],
                    "attempt": int(state.get("cost_attempt") or 0) + 1,
                }
            ],
        }

    return node
