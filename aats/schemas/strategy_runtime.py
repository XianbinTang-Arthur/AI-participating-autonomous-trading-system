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


class StrategyLegIntent(SchemaBase):
    symbol: str
    product_type: ProductType
    side: Literal["buy", "sell"]
    role: Literal["primary", "hedge", "inventory", "accumulation"] = "primary"
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
    candidates: list[StrategyCandidate] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class StrategyExecutionBundle(SchemaBase):
    bundle_id: str = Field(default_factory=lambda: new_id("bundle"))
    decision_id: str
    family: StrategyFamily
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
