"""Agent 0 - DB Scan (deterministic, no LLM).

Reads the stock position straight out of SQL and works out which SKUs are
actually short. Everything here is arithmetic: the numbers it produces are the
facts the LLM agents reason over, and the floor the reorder agents are held to.
"""

from __future__ import annotations

from typing import Any

from db.repository import fetch_inventory, fetch_offers, resolve_warehouse
from schemas import DbScan, SkuCandidate

from . import calc
from .base import AgentRuntime, trace_message

ROLE = "db_scan"

URGENCY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def build_node(rt: AgentRuntime):
    def node(state: dict[str, Any]) -> dict[str, Any]:
        settings = rt.settings
        warehouse = resolve_warehouse(rt.engine, state.get("warehouse_code"))
        inventory = fetch_inventory(rt.engine, warehouse["id"])

        product_ids = [row["product_id"] for row in inventory]
        offers = fetch_offers(rt.engine, product_ids)

        lead_by_product: dict[int, int] = {}
        offers_by_product: dict[int, list[dict[str, Any]]] = {}
        for offer in offers:
            pid = offer["product_id"]
            offers_by_product.setdefault(pid, []).append(offer)
            lead = int(offer.get("lead_time_days") or 0)
            current = lead_by_product.get(pid)
            if current is None or lead < current:
                lead_by_product[pid] = lead

        candidates: list[SkuCandidate] = []
        for row in inventory:
            pid = row["product_id"]
            lead = lead_by_product.get(pid, 14)
            forecast = (
                row.get("forecast_daily_30d")
                or row.get("avg_demand_30d")
                or row.get("daily_demand")
                or 0.0
            )
            cover = calc.days_of_cover(row, forecast)
            horizon = lead + int(row.get("safety_days") or 0)
            projected = int(row.get("on_hand") or 0) + int(row.get("on_order") or 0)
            shortfall = calc.shortfall(row, lead, forecast)

            sku_offers = offers_by_product.get(pid, [])
            cheapest = (
                min(float(o["unit_price"]) for o in sku_offers) if sku_offers else 0.0
            )

            below_reorder = projected <= int(row["reorder_point"])
            stockout_risk = cover <= horizon
            if not ((below_reorder or stockout_risk) and shortfall > 0):
                continue

            candidates.append(
                SkuCandidate(
                    product_id=pid,
                    sku=row["sku"],
                    product_name=row["product_name"],
                    category=row.get("category", ""),
                    unit=row.get("unit", "pcs"),
                    on_hand=int(row["on_hand"]),
                    reorder_point=int(row["reorder_point"]),
                    on_order=int(row["on_order"]),
                    forecast_daily_demand=round(float(forecast), 2),
                    demand_trend_pct=float(row.get("demand_trend_pct") or 0.0),
                    demand_stdev_30d=float(row.get("demand_stdev_30d") or 0.0),
                    lead_time_days=lead,
                    safety_days=int(row.get("safety_days") or 0),
                    days_of_cover=cover,
                    projected_stock_at_arrival=projected,
                    min_required_qty=shortfall,
                    rule_urgency=calc.urgency_for(cover, lead),
                    supplier_count=len(sku_offers),
                    cheapest_unit_price=round(cheapest, 4),
                )
            )

        focus = {s.strip().upper() for s in (state.get("focus_skus") or []) if s.strip()}
        if focus:
            candidates = [c for c in candidates if c.sku.upper() in focus]

        candidates.sort(
            key=lambda c: (URGENCY_RANK.get(c.rule_urgency, 4), -c.min_required_qty)
        )

        max_items = int(state.get("max_items") or settings.max_items)
        truncated = max(0, len(candidates) - max_items)
        selected = candidates[:max_items]

        scan = DbScan(
            warehouse_code=warehouse["code"],
            warehouse_name=warehouse["name"],
            candidates=selected,
            skipped_count=len(inventory) - len(selected) - truncated,
            notes=(
                f"{len(selected)} SKU(s) need reordering in {warehouse['code']}; "
                f"{len(inventory) - len(selected) - truncated} healthy line(s) skipped"
                + (f"; {truncated} lower-priority line(s) deferred" if truncated else "")
                + "."
            ),
        )

        summary = (
            f"{len(selected)} SKU(s) need reordering"
            + (f"; {scan.skipped_count} healthy" if scan.skipped_count else "")
            + (f"; {truncated} deferred" if truncated else "")
            + "."
        )
        return {
            "scan": scan.model_dump(),
            "db_rows": {"warehouse": warehouse},
            "messages": [trace_message(scan.notes)],
            "audit": [
                {
                    "role": ROLE,
                    "provider": "local",
                    "model": "deterministic",
                    "summary": summary,
                    "skus": [c.sku for c in selected],
                }
            ],
        }

    return node
