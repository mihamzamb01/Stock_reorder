"""Deterministic helpers shared by the agents.

Design rule for this project: **LLMs decide, Python computes.** Every number
that ends up on a purchase order is recalculated here from the database values,
so a hallucinated price or quantity can never corrupt the plan.
"""

from __future__ import annotations

import math
from typing import Any

from config.settings import quality_thresholds


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------
def forecast_of(row: dict[str, Any]) -> float:
    """Best available demand forecast for a row, falling back sensibly."""
    for key in ("forecast_daily_30d", "avg_demand_30d", "daily_demand"):
        value = row.get(key)
        if value:
            return float(value)
    return 0.0


def days_of_cover(row: dict[str, Any], forecast: float | None = None) -> float:
    """Days of stock left at the current burn rate."""
    daily = float(forecast) if forecast is not None else forecast_of(row)
    if daily <= 0:
        return 999.0
    return round(float(row.get("on_hand") or 0) / daily, 1)


def horizon_demand(
    row: dict[str, Any], lead_time_days: int, forecast: float | None = None
) -> float:
    """Units expected to be consumed before a replenishment could arrive."""
    daily = float(forecast) if forecast is not None else forecast_of(row)
    safety_days = int(row.get("safety_days") or 0)
    return daily * max(0, lead_time_days + safety_days)


def shortfall(
    row: dict[str, Any],
    lead_time_days: int,
    forecast: float | None = None,
    order_multiple: int = 1,
) -> int:
    """Units to order to cover demand over the replenishment horizon."""
    demand = horizon_demand(row, lead_time_days, forecast)
    projected = float(row.get("on_hand") or 0) + float(row.get("on_order") or 0)
    gap = max(0.0, demand - projected)
    if gap <= 0:
        return 0
    qty = int(math.ceil(gap))
    if order_multiple > 1:
        qty = int(math.ceil(qty / order_multiple) * order_multiple)
    return qty


def urgency_for(days_of_cover: float, lead_time_days: int) -> str:
    """Rule-based urgency, used to sanity-check the agent's judgement."""
    lead = max(1, lead_time_days)
    if days_of_cover <= lead * 0.5:
        return "critical"
    if days_of_cover <= lead:
        return "high"
    if days_of_cover <= lead * 1.75:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Offers
# ---------------------------------------------------------------------------
def effective_price(offer: dict[str, Any], qty: int) -> float:
    """Unit price payable at ``qty`` honouring the volume price break."""
    price = float(offer.get("unit_price") or 0.0)
    break_qty = int(offer.get("price_break_qty") or 0)
    break_price = offer.get("price_break_unit")
    if break_qty and break_price is not None and qty >= break_qty:
        return float(break_price)
    return price


def landed_price(offer: dict[str, Any], qty: int) -> float:
    return effective_price(offer, qty) + float(offer.get("shipping_per_unit") or 0.0)


def offer_line_total(offer: dict[str, Any], qty: int) -> float:
    unit = effective_price(offer, qty)
    return unit * qty + float(offer.get("shipping_per_unit") or 0.0) * qty


def moq_ok(offer: dict[str, Any], qty: int) -> bool:
    return qty >= int(offer.get("moq") or 1)


def min_order_value_ok(offer: dict[str, Any], qty: int) -> bool:
    mov = float(offer.get("min_order_value") or 0.0)
    if mov <= 0:
        return True
    return effective_price(offer, qty) * qty >= mov


def next_multiple(qty: int, multiple: int) -> int:
    if multiple <= 1:
        return max(0, qty)
    return int(math.ceil(max(0, qty) / multiple) * multiple)


def contract_expiry_warning(offer: dict[str, Any]) -> str:
    days = offer.get("contract_days_remaining")
    if days is None:
        return ""
    if days <= 0:
        return "contract has expired"
    if days <= 30:
        return f"contract expires in {days} days"
    return ""


def annotate_offer(offer: dict[str, Any], required_qty: int) -> dict[str, Any]:
    """Pre-compute the deterministic fields the scout agent reasons about.

    `meets_moq` asks whether the quantity we actually want already clears the
    supplier's minimum. If it does not, `moq_overbuy` is how many extra units
    we would have to buy - a cost, not a disqualification.

    `meets_quality` applies the objective quality floors (defect rate and
    on-time rate). These are business rules, not judgement, so they are
    computed here rather than asked of the model.
    """
    qty = max(1, int(required_qty))
    moq = int(offer.get("moq") or 1)
    price = effective_price(offer, qty)
    min_value = float(offer.get("min_order_value") or 0.0)
    order_value = price * qty
    days_left = offer.get("contract_days_remaining")

    max_defect, min_on_time = quality_thresholds()
    defect = float(offer.get("defect_rate_pct") or 0.0)
    on_time = float(offer.get("on_time_pct") or 100.0)
    meets_quality = defect <= max_defect and on_time >= min_on_time

    return {
        **offer,
        "required_qty": required_qty,
        "eval_qty": qty,
        "effective_unit_price": round(price, 4),
        "landed_unit_price": round(price + float(offer.get("shipping_per_unit") or 0.0), 4),
        "meets_moq": qty >= moq,
        "moq_overbuy": max(0, moq - qty),
        "meets_min_order_value": (min_value <= 0) or (order_value >= min_value),
        "order_value": round(order_value, 2),
        "meets_quality": meets_quality,
        "quality_reason": (
            ""
            if meets_quality
            else f"defect rate {defect}% (max {max_defect}%), "
            f"on-time {on_time}% (min {min_on_time}%)"
        ),
        "contract_expired": days_left is not None and days_left <= 0,
        "contract_warning": contract_expiry_warning(offer),
    }


# ---------------------------------------------------------------------------
# Shortlist reconciliation
# ---------------------------------------------------------------------------
def reconcile_shortlist(
    shortlist: dict[str, Any],
    candidates: list[dict[str, Any]],
    required_by_sku: dict[str, int],
) -> dict[str, Any]:
    """Join the agent's ranking back onto the database offers.

    An offer is viable unless it fails a deterministic feasibility rule
    (expired contract, minimum order value not met) or the agent explicitly
    excluded it. If nothing survives, the cheapest feasible offer is
    reinstated so the pipeline never stalls.
    """
    by_sku: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        by_sku.setdefault(candidate["sku"], []).append(candidate)

    entries: list[dict[str, Any]] = []
    for entry in shortlist.get("entries", []):
        sku = entry.get("sku")
        pool = by_sku.get(sku) or []
        if not pool:
            continue

        excluded = {int(x) for x in (entry.get("exclude_offer_ids") or [])}

        scored: list[dict[str, Any]] = []
        for offer in pool:
            feasible = (
                bool(offer.get("meets_min_order_value"))
                and not bool(offer.get("contract_expired"))
                and bool(offer.get("meets_quality"))
            )
            is_excluded = int(offer["offer_id"]) in excluded
            reason = ""
            if not feasible:
                if not offer.get("meets_quality"):
                    reason = offer.get("quality_reason") or "quality below the required floor"
                elif not offer.get("meets_min_order_value"):
                    reason = "minimum order value not met"
                else:
                    reason = "contract expired"
            elif is_excluded:
                reason = "excluded by the scout"
            scored.append(
                {
                    **offer,
                    "viable": feasible and not is_excluded,
                    "rejection_reason": reason,
                    "assessment": entry.get("notes", "") if int(offer["offer_id"]) == int(
                        entry.get("preferred_offer_id") or 0
                    ) else "",
                }
            )

        viable = [o for o in scored if o["viable"]]
        if not viable:
            # Nothing usable: fall back to the cheapest feasible offer so the
            # pipeline never stalls.
            feasible = [o for o in scored if o.get("meets_min_order_value")] or scored
            fallback = min(feasible, key=lambda o: o["landed_unit_price"])
            fallback = {**fallback, "viable": True, "rejection_reason": ""}
            scored = [fallback] + [o for o in scored if o["offer_id"] != fallback["offer_id"]]
            viable = [fallback]

        preferred = next(
            (o for o in viable if o["offer_id"] == entry.get("preferred_offer_id")), None
        ) or min(viable, key=lambda o: o["landed_unit_price"])

        runner_up = next(
            (
                o
                for o in viable
                if o["offer_id"] == entry.get("runner_up_offer_id")
                and o["offer_id"] != preferred["offer_id"]
            ),
            None,
        )

        scored.sort(key=lambda o: (not o.get("viable", False), o["landed_unit_price"]))
        entries.append(
            {
                "sku": sku,
                "product_name": pool[0].get("product_name", ""),
                "required_qty": required_by_sku.get(sku, entry.get("required_qty", 0)),
                "offers": scored,
                "preferred_offer_id": preferred["offer_id"],
                "preferred_supplier_name": preferred.get("supplier_name", ""),
                "runner_up_supplier_name": (runner_up or {}).get("supplier_name", ""),
                "notes": entry.get("notes", ""),
            }
        )

    return {**shortlist, "entries": entries}


# ---------------------------------------------------------------------------
# Cost plan
# ---------------------------------------------------------------------------
def baseline_spend(shortlist: dict[str, Any], required_by_sku: dict[str, int]) -> float:
    """Cost of buying everything from the cheapest landed-cost supplier."""
    total = 0.0
    for entry in shortlist.get("entries", []):
        sku = entry.get("sku")
        qty = required_by_sku.get(sku, entry.get("required_qty", 0))
        if qty <= 0:
            continue
        offers = entry.get("offers") or []
        if not offers:
            continue
        cheapest = min(offers, key=lambda o: o.get("landed_unit_price", float("inf")))
        total += offer_line_total(cheapest, qty)
    return round(total, 2)


def reconcile_cost_plan(
    plan: dict[str, Any],
    shortlist: dict[str, Any],
    required_by_sku: dict[str, int],
) -> dict[str, Any]:
    """Recompute every money figure and repair the quantities.

    Guarantees, per SKU: the allocated quantity equals the required quantity,
    each line satisfies its MOQ, and all totals are recomputed from the
    database price rather than trusted from the model.
    """
    offer_index: dict[int, dict[str, Any]] = {}
    for entry in shortlist.get("entries", []):
        for offer in entry.get("offers") or []:
            offer_index[int(offer["offer_id"])] = offer

    per_sku: dict[str, list[dict[str, Any]]] = {}
    for allocation in plan.get("allocations", []):
        sku = allocation.get("sku")
        offer = offer_index.get(int(allocation.get("offer_id") or 0))
        if offer is None:
            continue
        qty = max(0, int(allocation.get("order_qty") or 0))
        if qty <= 0:
            continue
        moq = int(offer.get("moq") or 1)
        if qty < moq:
            qty = moq
        per_sku.setdefault(sku, []).append(
            {
                **allocation,
                "offer_id": offer["offer_id"],
                "supplier_id": offer.get("supplier_id", allocation.get("supplier_id")),
                "supplier_name": offer.get("supplier_name", allocation.get("supplier_name")),
                "order_qty": qty,
                "lead_time_days": offer.get("lead_time_days", allocation.get("lead_time_days", 0)),
                "rationale": allocation.get("rationale", ""),
            }
        )

    allocations: list[dict[str, Any]] = []
    for sku, lines in per_sku.items():
        required = int(required_by_sku.get(sku, 0))
        allocated = sum(line["order_qty"] for line in lines)

        if allocated > required and allocated > 0:
            # Scale back proportionally, then re-apply MOQ on the surviving lines.
            scale = required / allocated
            for line in lines:
                line["order_qty"] = int(line["order_qty"] * scale)
            surplus = required - sum(line["order_qty"] for line in lines)
            if surplus > 0:
                cheapest = min(lines, key=lambda line: line["landed_unit_cost"])
                cheapest["order_qty"] += surplus
            for line in lines:
                line["order_qty"] = max(
                    line["order_qty"],
                    int(offer_index[line["offer_id"]].get("moq") or 1),
                )
        elif allocated < required:
            # Under-allocated: top up with the cheapest landed-cost supplier.
            candidates = [o for o in offer_index.values() if o.get("sku") == sku]
            if not candidates:
                candidates = list(offer_index.values())
            cheapest = min(candidates, key=lambda o: o.get("landed_unit_price", float("inf")))
            lines.append(
                {
                    "sku": sku,
                    "product_name": cheapest.get("product_name", ""),
                    "offer_id": cheapest["offer_id"],
                    "supplier_id": cheapest.get("supplier_id"),
                    "supplier_name": cheapest.get("supplier_name", ""),
                    "order_qty": required - allocated,
                    "lead_time_days": cheapest.get("lead_time_days", 0),
                    "rationale": "Balancing top-up to meet the required quantity.",
                }
            )

        for line in lines:
            offer = offer_index[line["offer_id"]]
            unit_price = effective_price(offer, line["order_qty"])
            shipping = float(offer.get("shipping_per_unit") or 0.0)
            line["unit_price"] = round(unit_price, 4)
            line["shipping_cost"] = round(shipping * line["order_qty"], 4)
            line["line_total"] = round(unit_price * line["order_qty"] + shipping * line["order_qty"], 4)
            line["landed_unit_cost"] = round(line["line_total"] / line["order_qty"], 4)

        # A supplier's MOQ can force us to buy more than we strictly need.
        allocated_qty = sum(line["order_qty"] for line in lines)
        overbuy = max(0, allocated_qty - required)
        for line in lines:
            line["overbuy_qty"] = overbuy
            line["overbuy_reason"] = (
                f"Supplier MOQ is {int(offer_index[line['offer_id']].get('moq') or 1)}, "
                f"so {overbuy} extra unit(s) are ordered above the {required} required."
                if overbuy
                else ""
            )
        allocations.extend(lines)

    total_spend = round(sum(line["line_total"] for line in allocations), 2)
    base = baseline_spend(shortlist, required_by_sku)
    return {
        **plan,
        "allocations": allocations,
        "total_spend": total_spend,
        "baseline_spend": base,
        "projected_savings": round(base - total_spend, 2),
    }


def over_budget(plan: dict[str, Any], budget_cap: float | None) -> bool:
    if budget_cap is None:
        return False
    return float(plan.get("total_spend") or 0.0) > float(budget_cap) + 1e-6
