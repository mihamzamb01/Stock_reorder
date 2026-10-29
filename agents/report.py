"""Final report node - deterministic assembly of the reorder plan.

No LLM here on purpose: the numbers come straight from the reconciled plan.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from schemas import ReorderReport

from .base import AgentRuntime, trace_message


def build_node(rt: AgentRuntime):
    def node(state: dict[str, Any]) -> dict[str, Any]:
        cost_plan = state.get("cost_plan") or {}
        plan = state.get("reorder_plan") or {}
        scan = state.get("scan") or {}
        warehouse = (state.get("db_rows") or {}).get("warehouse") or {}

        decision_by_sku = {d["sku"]: d for d in plan.get("decisions") or []}
        offers_by_sku: dict[str, list[dict[str, Any]]] = {}
        for entry in (state.get("shortlist") or {}).get("entries") or []:
            offers_by_sku[entry["sku"]] = entry.get("offers") or []

        lines: list[dict[str, Any]] = []
        for allocation in cost_plan.get("allocations") or []:
            sku = allocation.get("sku", "")
            decision = decision_by_sku.get(sku, {})
            offers = offers_by_sku.get(sku, [])
            alternatives = sum(1 for o in offers if o.get("offer_id") != allocation.get("offer_id"))
            lines.append(
                {
                    "sku": sku,
                    "product_name": allocation.get("product_name")
                    or decision.get("product_name", ""),
                    "supplier_name": allocation.get("supplier_name", ""),
                    "order_qty": int(allocation.get("order_qty") or 0),
                    "unit_price": float(allocation.get("unit_price") or 0.0),
                    "landed_unit_cost": float(allocation.get("landed_unit_cost") or 0.0),
                    "line_total": float(allocation.get("line_total") or 0.0),
                    "days_of_cover": float(decision.get("days_of_cover") or 0.0),
                    "stockout_in_days": float(decision.get("stockout_in_days") or 0.0),
                    "urgency": decision.get("urgency", ""),
                    "rationale": decision.get("rationale", ""),
                    "alternatives": alternatives,
                }
            )

        lines.sort(key=lambda line: line["line_total"], reverse=True)

        total_spend = round(sum(line["line_total"] for line in lines), 2)
        baseline = float(cost_plan.get("baseline_spend") or 0.0)
        savings = round(baseline - total_spend, 2)
        savings_pct = round(savings / baseline * 100.0, 2) if baseline > 0 else 0.0

        warnings: list[str] = []
        budget_cap = state.get("budget_cap")
        if budget_cap and total_spend > float(budget_cap):
            warnings.append(
                f"Total spend {total_spend} exceeds the budget cap {budget_cap} "
                "after all optimisation attempts."
            )
        for decision in plan.get("decisions") or []:
            if decision.get("urgency") == "critical":
                warnings.append(
                    f"{decision['sku']} is critical - it can stock out before resupply."
                )
        for allocation in cost_plan.get("allocations") or []:
            if allocation.get("overbuy_qty"):
                warnings.append(
                    f"{allocation.get('sku')}: {allocation.get('overbuy_qty')} extra unit(s) "
                    f"ordered because of {allocation.get('supplier_name')}'s MOQ."
                )
        if scan.get("skipped_count"):
            warnings.append(
                f"{scan['skipped_count']} line(s) were healthy or deferred and are not in this plan."
            )

        summary = (
            f"{len(lines)} order line(s) across "
            f"{len({line['supplier_name'] for line in lines})} supplier(s). "
            f"Planned spend {total_spend} against a baseline of {baseline} "
            f"({_savings_phrase(savings, savings_pct)})."
        )

        report = ReorderReport(
            warehouse=warehouse.get("code") or scan.get("warehouse_code") or "",
            warehouse_name=warehouse.get("name") or scan.get("warehouse_name") or "",
            generated_at=datetime.now().isoformat(timespec="seconds"),
            currency=cost_plan.get("currency", "USD"),
            lines=lines,
            total_spend=total_spend,
            baseline_spend=baseline,
            projected_savings=savings,
            savings_pct=savings_pct,
            summary=summary,
            warnings=warnings,
        ).model_dump()

        return {
            "report": report,
            "messages": [trace_message(summary)],
            "audit": [
                {
                    "role": "report",
                    "provider": "local",
                    "model": "deterministic",
                    "summary": summary,
                }
            ],
        }

    return node


def _savings_phrase(savings: float, pct: float) -> str:
    """Describe the delta against the baseline without claiming a saving when
    the plan actually costs more than buying from the cheapest supplier."""
    if savings >= 0:
        return f"{pct}% saving"
    return f"{abs(pct)}% premium over the cheapest-supplier baseline"
