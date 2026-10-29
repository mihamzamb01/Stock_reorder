"""Pydantic contracts exchanged between the agents.

Every agent returns a validated structure, so downstream stages never have to
parse prose.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Urgency = Literal["critical", "high", "medium", "low"]


# ---------------------------------------------------------------------------
# DB scan (deterministic) - the raw picture of one SKU
# ---------------------------------------------------------------------------
class SkuCandidate(BaseModel):
    """Everything the agents need to know about one SKU, read from the DB."""

    product_id: int = Field(ge=1)
    sku: str
    product_name: str = ""
    category: str = ""
    unit: str = "pcs"
    on_hand: int = Field(ge=0)
    reorder_point: int = Field(ge=0)
    on_order: int = Field(ge=0)
    forecast_daily_demand: float = Field(ge=0, description="Projected units per day")
    demand_trend_pct: float = Field(description="Change vs the previous 30 days")
    demand_stdev_30d: float = Field(ge=0)
    lead_time_days: int = Field(ge=0, description="Shortest available supplier lead time")
    safety_days: int = Field(ge=0)
    days_of_cover: float = Field(ge=0)
    projected_stock_at_arrival: int = Field(
        description="on_hand + on_order, i.e. what is left when a resupply lands"
    )
    min_required_qty: int = Field(
        ge=0, description="Floor computed from demand over the replenishment horizon"
    )
    rule_urgency: Urgency = Field(description="Urgency derived from the hard rules")
    supplier_count: int = Field(ge=0, description="Suppliers offering this SKU")
    cheapest_unit_price: float = Field(ge=0)


class DbScan(BaseModel):
    warehouse_code: str = ""
    warehouse_name: str = ""
    candidates: list[SkuCandidate] = Field(default_factory=list)
    skipped_count: int = Field(ge=0)
    notes: str = ""


# ---------------------------------------------------------------------------
# Reorder analyst (one agent per SKU)
# ---------------------------------------------------------------------------
class ReorderDecision(BaseModel):
    """The reorder decision for a single SKU."""

    sku: str
    product_name: str = ""
    urgency: Urgency
    order_qty: int = Field(ge=0)
    days_of_cover: float = Field(ge=0)
    stockout_in_days: float = Field(
        description="Days until the stock runs out at the current burn rate"
    )
    rationale: str = Field(description="One or two sentences justifying the quantity")


class ReorderPlan(BaseModel):
    decisions: list[ReorderDecision] = Field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------------------
# Supplier scout - the DB checking agent
#
# The agent ranks and excludes; it does not re-state the commercial facts.
# Every price, MOQ and lead time comes from the database and is joined back
# afterwards, so the reply stays small (it has to fit the model's token
# budget) and a mistranscribed price can never reach the plan.
#
# Asking for a yes/no verdict on every offer made the model reject whole
# supplier lists on hallucinated grounds, so the burden is inverted here: an
# offer is viable unless the model actively excludes it, or it fails a
# deterministic feasibility rule.
# ---------------------------------------------------------------------------
class ShortlistEntry(BaseModel):
    sku: str
    required_qty: int = Field(ge=0)
    preferred_offer_id: int = Field(
        description="The best offer for this SKU, chosen from the ones not excluded"
    )
    runner_up_offer_id: int | None = Field(
        default=None, description="Second choice, if there is a clear one"
    )
    exclude_offer_ids: list[int] = Field(
        default_factory=list,
        description="Offers that must not be used, e.g. an unacceptable quality risk",
    )
    notes: str = Field(description="Why the preferred offer was chosen, and any risk")


class SupplierShortlist(BaseModel):
    entries: list[ShortlistEntry] = Field(default_factory=list)
    notes: str = Field(description="Cross-cutting observations about the supplier base")


# ---------------------------------------------------------------------------
# Cost optimizer
# ---------------------------------------------------------------------------
class Allocation(BaseModel):
    sku: str
    product_name: str = ""
    offer_id: int
    supplier_id: int
    supplier_name: str
    order_qty: int = Field(ge=0)
    unit_price: float = Field(ge=0, description="Price actually payable per unit")
    shipping_cost: float = Field(ge=0)
    line_total: float = Field(ge=0, description="unit_price * order_qty + shipping_cost")
    landed_unit_cost: float = Field(ge=0, description="line_total / order_qty")
    lead_time_days: int = Field(ge=0)
    rationale: str = Field(description="Why this supplier and quantity")


class CostPlan(BaseModel):
    allocations: list[Allocation] = Field(default_factory=list)
    total_spend: float = Field(ge=0)
    baseline_spend: float = Field(
        ge=0, description="Cost of buying everything from the cheapest landed-cost supplier"
    )
    projected_savings: float = Field(description="baseline_spend - total_spend")
    currency: str = "USD"
    notes: str = Field(description="Trade-offs made and risks accepted")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
class ReportLine(BaseModel):
    sku: str
    product_name: str = ""
    supplier_name: str = ""
    order_qty: int = 0
    unit_price: float = 0.0
    landed_unit_cost: float = 0.0
    line_total: float = 0.0
    days_of_cover: float = 0.0
    stockout_in_days: float = 0.0
    urgency: str = ""
    rationale: str = ""
    alternatives: int = 0


class ReorderReport(BaseModel):
    warehouse: str = ""
    warehouse_name: str = ""
    generated_at: str = ""
    currency: str = "USD"
    lines: list[ReportLine] = Field(default_factory=list)
    total_spend: float = 0.0
    baseline_spend: float = 0.0
    projected_savings: float = 0.0
    savings_pct: float = 0.0
    summary: str = ""
    warnings: list[str] = Field(default_factory=list)
