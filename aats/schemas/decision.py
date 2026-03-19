from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field
from pydantic import BaseModel

from aats.schemas.common import SchemaBase
from aats.schemas.execution import AIExecutionParameterSuggestionEnvelope
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
    risk_budget_state: dict[str, Decimal] = Field(default_factory=dict)
    current_position_qty: Decimal
    current_open_orders: list[str] = Field(default_factory=list)
    product_type: ProductType = "spot"
    current_exposure_side: Literal["long", "short", "flat"] = "flat"
    current_target_leverage: float = 1.0
    current_position_opened_at: datetime | None = None
    last_position_closed_at: datetime | None = None
    latest_fill_timestamp: datetime | None = None
    recent_closed_trade_count: int = 0
    recent_win_rate: float = 0.0
    recent_fee_drag_ratio: float = 0.0
    recent_churn_ratio: float = 0.0
    recent_low_edge_trade_streak: int = 0
    recent_low_edge_trade_at: datetime | None = None
    strategy_guardrail_flags: list[str] = Field(default_factory=list)
    strategy_cooldowns: dict[str, float] = Field(default_factory=dict)

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
    baseline_override_recommended: bool = False
    override_reason_codes: list[str] = Field(default_factory=list)
    economically_actionable: bool = False
    estimated_edge_bps: float | None = None
    estimated_cost_bps: float | None = None
    estimated_net_edge_bps: float | None = None
    validation_flags: list[str] = Field(default_factory=list)
    rejection_flags: list[str] = Field(default_factory=list)
    source_mode: Literal["provider", "fallback"] = "fallback"
    execution_condition: str | None = None
    evaluation_tags: list[str] = Field(default_factory=list)
    ai_execution_parameter_suggestion: AIExecutionParameterSuggestionEnvelope | None = None
    model_name: str
    model_version: str
    prompt_version: str


class AIExecutionParameterSuggestionOutput(BaseModel):
    passive_bias: float | None = None
    maker_taker_bias: float | None = None
    max_cross_spread_bps: float | None = None
    slice_count: int | None = None
    max_participation_rate: float | None = None
    cancel_replace_patience_ms: int | None = None


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
    baseline_override_recommended: bool = False
    override_reason_codes: list[str] = Field(default_factory=list)


class AIProviderAssessmentWithExecutionSuggestionOutput(AIProviderAssessmentOutput):
    execution_parameter_suggestion: AIExecutionParameterSuggestionOutput | None = None


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
    observed_total_equity: Decimal | None = None


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
    current_position_qty: Decimal
    target_position_qty: Decimal
    delta_position_qty: Decimal
    current_notional: Decimal
    target_notional: Decimal
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
    ai_takeover_allowed: bool = False
    ai_takeover_applied: bool = False
    ai_takeover_blockers: list[str] = Field(default_factory=list)
    expected_signal_edge_bps: float = 0.0
    expected_cost_bps: float = 0.0
    expected_net_edge_bps: float = 0.0
    guardrail_flags: list[str] = Field(default_factory=list)
    ai_execution_parameter_suggestion: AIExecutionParameterSuggestionEnvelope | None = None
