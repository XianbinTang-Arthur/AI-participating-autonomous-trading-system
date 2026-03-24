from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field

from aats.schemas.common import SchemaBase, new_id, utc_now
from aats.schemas.system import MarginModelType, ProductType


StrategyFamily = Literal["directional", "smart_arbitrage", "spot_grid", "dca"]
StrategyCandidateState = Literal["ready", "inactive", "disabled", "incompatible", "advisory_only"]
StrategyRouteAction = Literal["override_target", "hold_current", "advisory_only", "protective_fallback"]
StrategyExecutionBundleStatus = Literal["blocked", "planned", "submitted", "partial_fill_recovery", "recovered"]
StrategySleeveStatus = Literal["active", "inactive", "paused", "retired"]
StrategyInventoryPolicy = Literal["account_net_inventory", "paired_inventory", "inventory_accumulation"]
StrategySleeveAutomationState = Literal["active", "contracted", "paused", "protective_only", "disabled"]


class StrategySleeveAutomationDecision(SchemaBase):
    family: StrategyFamily
    strategy_sleeve_id: str
    automatic_enabled: bool = True
    runtime_supported: bool = True
    approved_for_execution: bool = True
    automation_state: StrategySleeveAutomationState = "active"
    budget_multiplier: Decimal = Decimal("1")
    allocator_weight: Decimal = Decimal("1")
    recent_net_pnl: Decimal = Decimal("0")
    current_inventory_notional: Decimal = Decimal("0")
    reason_codes: list[str] = Field(default_factory=list)
    operator_summary: str | None = None


class StrategySleeveIntent(SchemaBase):
    sleeve_intent_id: str = Field(default_factory=lambda: new_id("sintent"))
    decision_id: str
    family: StrategyFamily
    strategy_sleeve_id: str
    state: StrategyCandidateState = "inactive"
    symbol: str
    product_type: ProductType
    margin_mode: MarginModelType
    inventory_policy: StrategyInventoryPolicy
    route_action: StrategyRouteAction = "hold_current"
    headline: str | None = None
    selectable: bool = False
    execution_compatible: bool = False
    current_position_qty: Decimal = Decimal("0")
    target_position_qty: Decimal = Decimal("0")
    delta_position_qty: Decimal = Decimal("0")
    account_current_position_qty: Decimal | None = None
    account_target_position_qty: Decimal | None = None
    target_notional: Decimal | None = None
    priority_score: float = 0.0
    reason_codes: list[str] = Field(default_factory=list)
    automatic_enabled: bool = True
    budget_multiplier: Decimal = Decimal("1")
    allocator_weight: Decimal = Decimal("1")
    control_reason_codes: list[str] = Field(default_factory=list)
    control_summary: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    legs: list["StrategyLegIntent"] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class PortfolioAllocationDecision(SchemaBase):
    allocation_id: str = Field(default_factory=lambda: new_id("alloc"))
    decision_id: str
    symbol: str
    product_type: ProductType
    margin_mode: MarginModelType
    allocator_version: str = "task73_allocator_v1"
    automatic_enabled: bool = True
    route_action: StrategyRouteAction = "hold_current"
    primary_family: StrategyFamily = "directional"
    primary_strategy_sleeve_id: str | None = None
    active_families: list[StrategyFamily] = Field(default_factory=list)
    approved_families: list[StrategyFamily] = Field(default_factory=list)
    blocked_reason_codes: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    operator_summary: str | None = None
    current_position_qty: Decimal = Decimal("0")
    target_position_qty: Decimal = Decimal("0")
    delta_position_qty: Decimal = Decimal("0")
    target_notional: Decimal | None = None
    approved_sleeve_weights: dict[str, Decimal] = Field(default_factory=dict)
    approved_sleeve_budget_multipliers: dict[str, Decimal] = Field(default_factory=dict)
    sleeve_intents: list[StrategySleeveIntent] = Field(default_factory=list)
    execution_legs: list["StrategyLegIntent"] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class StrategyLegIntent(SchemaBase):
    symbol: str
    product_type: ProductType
    side: Literal["buy", "sell"]
    family: StrategyFamily | None = None
    role: Literal["primary", "hedge", "inventory", "accumulation"] = "primary"
    strategy_sleeve_id: str | None = None
    allocation_id: str | None = None
    margin_mode: MarginModelType = "cash"
    target_leverage: float = 1.0
    current_position_qty: Decimal | None = None
    target_position_qty: Decimal | None = None
    delta_position_qty: Decimal | None = None
    reference_price: Decimal | None = None
    execution_compatible: bool = False
    policy_allowed: bool | None = None
    policy_rejection_reasons: list[str] = Field(default_factory=list)
    risk_approved: bool | None = None
    risk_rejection_reasons: list[str] = Field(default_factory=list)
    risk_constraints_applied: list[str] = Field(default_factory=list)
    execution_plan_ref: str | None = None
    order_intent_ref: str | None = None
    note: str | None = None


class StrategyCandidate(SchemaBase):
    family: StrategyFamily
    state: StrategyCandidateState
    enabled: bool = False
    selectable: bool = False
    execution_compatible: bool = False
    route_action: StrategyRouteAction = "hold_current"
    headline: str
    recommended_symbol: str | None = None
    target_position_qty: Decimal | None = None
    delta_position_qty: Decimal | None = None
    score: float = 0.0
    confidence: float = 0.0
    urgency: Literal["low", "medium", "high"] = "low"
    reason_codes: list[str] = Field(default_factory=list)
    automatic_enabled: bool = True
    budget_multiplier: Decimal = Decimal("1")
    allocator_weight: Decimal = Decimal("1")
    control_reason_codes: list[str] = Field(default_factory=list)
    control_summary: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    legs: list[StrategyLegIntent] = Field(default_factory=list)


class StrategyCoordinatorSnapshot(SchemaBase):
    snapshot_id: str = Field(default_factory=lambda: new_id("strategy"))
    decision_id: str
    symbol: str
    timeframe: Literal["15m", "1h"]
    product_type: ProductType
    margin_mode: MarginModelType
    allowed_symbols: tuple[str, ...] = Field(default_factory=tuple)
    active_family: StrategyFamily = "directional"
    selected_family: StrategyFamily = "directional"
    selected_state: StrategyCandidateState = "ready"
    selected_route_action: StrategyRouteAction = "override_target"
    selected_headline: str | None = None
    selection_reason_codes: list[str] = Field(default_factory=list)
    active_families: list[StrategyFamily] = Field(default_factory=list)
    approved_families: list[StrategyFamily] = Field(default_factory=list)
    automation_decisions: list[StrategySleeveAutomationDecision] = Field(default_factory=list)
    candidates: list[StrategyCandidate] = Field(default_factory=list)
    sleeve_intents: list[StrategySleeveIntent] = Field(default_factory=list)
    allocation_decision: PortfolioAllocationDecision | None = None
    created_at: datetime = Field(default_factory=utc_now)


class StrategyExecutionBundle(SchemaBase):
    bundle_id: str = Field(default_factory=lambda: new_id("bundle"))
    decision_id: str
    family: StrategyFamily
    participating_families: list[StrategyFamily] = Field(default_factory=list)
    strategy_sleeve_id: str | None = None
    strategy_sleeve_refs: list[str] = Field(default_factory=list)
    allocation_id: str | None = None
    product_type: ProductType
    margin_mode: MarginModelType
    allowed_symbols: tuple[str, ...] = Field(default_factory=tuple)
    route_action: StrategyRouteAction
    status: StrategyExecutionBundleStatus = "planned"
    selected_symbol: str
    operator_summary: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    legs: list[StrategyLegIntent] = Field(default_factory=list)
    execution_plan_refs: list[str] = Field(default_factory=list)
    order_intent_refs: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class StrategySleeveRecord(SchemaBase):
    sleeve_id: str
    family: StrategyFamily
    name: str
    product_scope: ProductType
    margin_scope: MarginModelType
    symbol_scope: tuple[str, ...] = Field(default_factory=tuple)
    automatic_enabled: bool = True
    inventory_policy: StrategyInventoryPolicy = "account_net_inventory"
    status: StrategySleeveStatus = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
