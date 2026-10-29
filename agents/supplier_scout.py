"""Agent 2 - Supplier Scout (the database checking agent).

Reads every supplier offer out of SQL for the SKUs that need reordering,
pre-computes the economics, and asks the model for a verdict on each offer.

The model returns only a *judgement* per offer. Every commercial field is
joined back from the database afterwards. That keeps the reply small enough to
fit the model's token budget, and stops a mistranscribed price from leaking
into the plan.
"""

from __future__ import annotations

from typing import Any

from db.repository import fetch_offers
from llm import structured_call
from schemas import SupplierShortlist

from . import calc
from .base import AgentRuntime, as_messages, trace_message

ROLE = "supplier_scout"

SYSTEM_PROMPT = """You are the Supplier Scout for a global procurement team.

For each SKU you are given every supplier offer on file, read from the
procurement database with the economics already computed. Your job is to pick
the best supplier, and to rule out any that must not be used.

WHAT TO RETURN, per SKU:
- `preferred_offer_id` - the best offer for this SKU. It must be one of the
  `offer_id` values listed under that SKU.
- `runner_up_offer_id` - the second choice, if there is a clear one, else null.
- `exclude_offer_ids` - offers that must not be used. Only exclude an offer
  when there is a real reason:
    * its quality is unacceptable (very high `defect_pct` or very low
      `on_time_pct`) and it is not the only option
    * `meets_min_order_value` is false
    * `contract_warning` says the contract has expired
  Exclude as few offers as possible. If an offer is merely slower or dearer,
  that is a ranking matter, not an exclusion.
- `notes` - one or two sentences: why you chose the preferred offer, and any
  risk the buyer should know about.

HOW TO RANK, in this order:
  1. A lead time short enough to be useful given `days_of_cover`.
  2. Reliability and quality: `reliability`, `on_time_pct`, `defect_pct`.
  3. The lower `landed_price`.

Only the offers listed are still in contention - the quality floors, minimum
order values and contract dates have already been applied for you, so you do
not need to re-check them. Your job is to pick between what is left.

IMPORTANT - LEAD TIME:
If every offer for a SKU has a lead time longer than `days_of_cover`, that
tells you the SKU is already late. It says nothing about the suppliers, and
ruling them all out leaves nothing to choose from. Rank them instead and
prefer the fastest, then note the risk in `notes`.

A short lead time does not excuse a bad price on a critical line, and a cheap
price does not excuse a supplier that delivers 65% on time.

OTHER NOTES:
  * `moq_overbuy` is how many extra units you must buy to reach the supplier's
    minimum order quantity. It is a cost, not a reason to exclude.
  * `last_agreed_price` is what was previously agreed with this supplier. A
    previous agreement below the current list price means the list price is
    not the real market price - mention that in `notes`.

Keep every note under 30 words. Return only the structured result."""


def build_node(rt: AgentRuntime):
    def node(state: dict[str, Any]) -> dict[str, Any]:
        spec = rt.spec(ROLE)
        llm = rt.llm(ROLE)

        plan = state.get("reorder_plan") or {}
        decisions = plan.get("decisions") or []
        if not decisions:
            summary = "Nothing to source - no SKUs flagged for reorder."
            return {
                "shortlist": {"entries": [], "notes": "No SKUs to source."},
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
        product_ids: list[int] = []
        for decision in decisions:
            sku = decision["sku"]
            required_by_sku[sku] = int(decision.get("order_qty") or 0)
            pid = int(decision.get("product_id") or 0)
            if pid:
                product_ids.append(pid)

        product_ids = list(dict.fromkeys(product_ids))
        raw_offers = fetch_offers(rt.engine, product_ids)

        candidates = []
        for offer in raw_offers:
            qty = required_by_sku.get(offer["sku"], 0)
            if qty <= 0:
                continue
            candidates.append(calc.annotate_offer(offer, qty))

        # Compact payload, grouped per SKU so the offer ids are unambiguous.
        payload = {
            "skus": [
                {
                    "sku": d["sku"],
                    "required_qty": required_by_sku.get(d["sku"], 0),
                    "days_of_cover": d.get("days_of_cover"),
                    "urgency": d.get("urgency"),
                    "offers": [
                        {
                            "offer_id": c["offer_id"],
                            "supplier": c["supplier_name"],
                            "country": c["country"],
                            "list_price": c["unit_price"],
                            "landed_price": c["landed_unit_price"],
                            "moq": c["moq"],
                            "moq_overbuy": c["moq_overbuy"],
                            "lead_days": c["lead_time_days"],
                            "defect_pct": c["defect_rate_pct"],
                            "on_time_pct": c["on_time_pct"],
                            "reliability": c["reliability_score"],
                            "last_agreed_price": c.get("last_agreed_price"),
                        }
                        for c in candidates
                        if c["sku"] == d["sku"]
                        and c.get("meets_quality")
                        and c.get("meets_min_order_value")
                        and not c.get("contract_expired")
                    ],
                }
                for d in decisions
            ],
        }

        messages = as_messages(SYSTEM_PROMPT, payload)
        shortlist: SupplierShortlist = structured_call(
            llm, SupplierShortlist, messages, obs=rt.obs, spec=spec, stage=ROLE
        )

        reconciled = calc.reconcile_shortlist(
            shortlist.model_dump(), candidates, required_by_sku
        )

        viable_count = sum(
            1 for e in reconciled["entries"] for o in e["offers"] if o.get("viable")
        )
        summary = (
            f"Shortlisted {len(reconciled['entries'])} SKU(s) from "
            f"{len(candidates)} offer(s); {viable_count} viable."
        )
        return {
            "shortlist": reconciled,
            "messages": [trace_message(summary)],
            "audit": [
                {
                    "role": ROLE,
                    "provider": spec.provider,
                    "model": spec.model,
                    "summary": summary,
                    "preferred": {
                        e["sku"]: e.get("preferred_supplier_name") for e in reconciled["entries"]
                    },
                }
            ],
        }

    return node
