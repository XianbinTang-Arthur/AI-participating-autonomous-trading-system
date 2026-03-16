from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field
from pydantic import BaseModel

from aats.schemas.common import SchemaBase
from aats.schemas.system import MarginModelType, ProductType


AIOperatingMode = Literal["baseline_only", "ai_advisory", "ai_blended", "ai_primary"]


class DecisionContext(SchemaBase):
    decision_id: str
    symbol: str
    timeframe: Literal["15m", "1h"]
    as_of_ts: datetime
    market_snapshot_ref: str
    feature_snapshot_ref: str
    portfolio_snapshot_ref: str
    health_snapshot_ref: str
    mode: str
    policy_flags: list[str] = Field(default_factory=list)
    risk_budget_state: dict[str, float] = Field(default_factory=dict)
    current_position_qty: float
    current_open_orders: list[str] = Field(default_factory=list)
    product_type: ProductType = "spot"
    current_exposure_side: Literal["long", "short", "flat"] = "flat"
    current_target_leverage: float = 1.0

class BaselineAssessment(SchemaBase):
    decision_id: str
    symbol: str
    regime: str
    direction_bias: Literal["long", "short", "flat"]
    trend_strength: float
    volatility_state: str
    confidence: float
    composite_alpha_score: float = 0.0
    suggested_position_scale: float = 0.0
    volatility_target_scale: float = 1.0
    factor_scores: dict[str, float] = Field(default_factory=dict)
    holding_horizon: str
    invalidation_conditions: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    engine_version: str


class AIMarketAssessment(SchemaBase):
    decision_id: str
    symbol: str
    regime: str
    directional_edge: float
    expected_volatility: float
    confidence: float
    uncertainty: float
    expected_holding_horizon: str
    invalidation_conditions: list[str] = Field(default_factory=list)
    risk_tags: list[str] = Field(default_factory=list)
    rationale_summary: str
    operating_mode: AIOperatingMode = "baseline_only"
    provider_name: str = "baseline_fallback"
    provider_request_id: str | None = None
    provider_latency_ms: float | None = None
    output_valid: bool = True
    fallback_used: bool = False
    fallback_reason: str | None = None
    degraded: bool = False
    calibrated_confidence: float = 0.0
    evaluation_tags: list[str] = Field(default_factory=list)
    model_name: str
    model_version: str
    prompt_version: str


class AIProviderAssessmentOutput(BaseModel):
    regime: str
    directional_edge: float
    expected_volatility: float
    confidence: float
    uncertainty: float
    expected_holding_horizon: str
    invalidation_conditions: list[str] = Field(default_factory=list)
    risk_tags: list[str] = Field(default_factory=list)
    rationale_summary: str


class AIDecisionEvaluation(SchemaBase):
    decision_id: str
    operating_mode: AIOperatingMode
    provider_name: str
    output_valid: bool
    calibrated_confidence: float
    fallback_used: bool = False
    fallback_reason: str | None = None
    degraded: bool = False
    portfolio_snapshot_ref: str | None = None
    reconciliation_ref: str | None = None
    reconciliation_severity: str | None = None
    observed_total_equity: float | None = None


class AIActionProposal(SchemaBase):
    decision_id: str
    symbol: str
    suggested_target_exposure: float
    suggested_entry_style: str
    suggested_reduce_conditions: list[str] = Field(default_factory=list)
    suggested_exit_conditions: list[str] = Field(default_factory=list)
    urgency: Literal["low", "medium", "high"]
    ttl_seconds: int


class PositionTarget(SchemaBase):
    decision_id: str
    symbol: str
    current_position_qty: float
    target_position_qty: float
    delta_position_qty: float
    current_notional: float
    target_notional: float
    rebalance_reason: str
    urgency: Literal["low", "medium", "high"]
    max_slippage_tolerance_bps: int
    source_mix: dict[str, float]
    decision_expiry_ts: datetime
    product_type: ProductType = "spot"
    current_exposure_side: Literal["long", "short", "flat"] = "flat"
    target_exposure_side: Literal["long", "short", "flat"] = "flat"
    position_intent: Literal[
        "hold",
        "open_long",
        "reduce_long",
        "close_long",
        "open_short",
        "reduce_short",
        "close_short",
        "reverse_to_long",
        "reverse_to_short",
    ] = "hold"
    target_leverage: float = 1.0
    margin_mode: MarginModelType = "cash"
    leverage_bias: float = 1.0
