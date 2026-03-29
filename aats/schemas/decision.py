from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field
from pydantic import BaseModel

from aats.schemas.common import SchemaBase
from aats.schemas.execution import AIExecutionParameterSuggestionEnvelope
from aats.schemas.portfolio import InstrumentPositionState, PositionLegState
from aats.schemas.strategy_runtime import StrategyFamily, StrategyLegIntent, StrategyRouteAction
from aats.schemas.system import MarginModelType, ProductType


CanonicalAIOperatingMode = Literal[
    "baseline_only",
    "ai_assisted",
    "ai_decision_maker",
    "ai_decision_maker_with_profile_control",
]

LegacyAIOperatingMode = Literal["ai_advisory", "ai_blended", "ai_primary"]

AIOperatingMode = Literal[
    "baseline_only",
    "ai_assisted",
    "ai_decision_maker",
    "ai_decision_maker_with_profile_control",
    "ai_advisory",
    "ai_blended",
    "ai_primary",
]

AI_OPERATING_MODE_CANONICAL_MAP: dict[str, CanonicalAIOperatingMode] = {
    "baseline_only": "baseline_only",
    "ai_assisted": "ai_assisted",
    "ai_decision_maker": "ai_decision_maker",
    "ai_decision_maker_with_profile_control": "ai_decision_maker_with_profile_control",
    "ai_advisory": "ai_assisted",
    "ai_blended": "ai_assisted",
    "ai_primary": "ai_decision_maker",
}


def normalize_ai_operating_mode(mode: str | None) -> CanonicalAIOperatingMode:
    if mode is None:
        return "baseline_only"
    normalized = AI_OPERATING_MODE_CANONICAL_MAP.get(str(mode).strip())
    if normalized is not None:
        return normalized
    return "baseline_only"


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
    current_position_state: InstrumentPositionState | None = None
    current_position_legs: list[PositionLegState] = Field(default_factory=list)
    current_net_position_qty: Decimal = Decimal("0")
    current_gross_position_qty: Decimal = Decimal("0")
    current_long_position_qty: Decimal = Decimal("0")
    current_short_position_qty: Decimal = Decimal("0")
    current_net_position_notional: Decimal = Decimal("0")
    current_gross_position_notional: Decimal = Decimal("0")
    current_long_position_notional: Decimal = Decimal("0")
    current_short_position_notional: Decimal = Decimal("0")
    current_long_leg_opened_at: datetime | None = None
    current_short_leg_opened_at: datetime | None = None
    last_long_leg_closed_at: datetime | None = None
    last_short_leg_closed_at: datetime | None = None
    latest_long_leg_fill_timestamp: datetime | None = None
    latest_short_leg_fill_timestamp: datetime | None = None
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
    leg_strategy_health: dict[str, dict[str, object]] = Field(default_factory=dict)
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


class BaselineReference(SchemaBase):
    decision_id: str
    symbol: str
    timeframe: Literal["15m", "1h"] | None = None
    regime: str | None = None
    volatility_state: str | None = None
    direction_bias: Literal["long", "short", "flat"]
    confidence: float | None = None
    composite_alpha_score: float | None = None
    suggested_position_scale: float | None = None
    reason_codes: list[str] = Field(default_factory=list)
    raw_payload: dict[str, object] | None = None


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


class AIDecisionIntent(SchemaBase):
    decision_id: str
    symbol: str
    timeframe: Literal["15m", "1h"] | None = None
    direction: Literal["long", "short", "flat"]
    action: Literal["hold", "enter", "scale_in", "reduce", "exit", "reverse"]
    target_qty: Decimal
    confidence: float
    economically_actionable: bool = False
    reason_codes: list[str] = Field(default_factory=list)
    fallback_used: bool = False
    degraded: bool = False
    provider_name: str | None = None
    provider_request_id: str | None = None
    requested_profile_id: str | None = None
    requested_profile_reason_codes: list[str] = Field(default_factory=list)
    raw_assessment_ref: dict[str, object] | None = None


DecisionSource = Literal["baseline", "ai", "baseline_fallback", "admin_override"]
DecisionAuthority = Literal["reference_only", "advisory", "final_decision", "final_decision_with_profile_control"]
ProfileControlSource = Literal["env_default", "ai", "admin", "system"]


class ProfileControlDecision(SchemaBase):
    decision_id: str | None = None
    requested_by: Literal["ai", "admin", "system"]
    requested_profile_id: str
    current_profile_id: str | None = None
    applied: bool = False
    blocked_reasons: list[str] = Field(default_factory=list)
    frozen_by_admin_override: bool = False
    freeze_until: datetime | None = None
    decision_reason_codes: list[str] = Field(default_factory=list)
    activation_record_ref: str | None = None


class DecisionOutcome(SchemaBase):
    decision_id: str
    symbol: str
    ai_operating_mode: CanonicalAIOperatingMode = "baseline_only"
    finalized: bool = False
    decision_source: DecisionSource
    decision_authority: DecisionAuthority
    final_direction: Literal["long", "short", "flat"] | None = None
    final_action: Literal["hold", "enter", "scale_in", "reduce", "exit", "reverse"] | None = None
    final_target_qty: Decimal | None = None
    baseline_reference: dict[str, object] | None = None
    baseline_disagreement: dict[str, object] | None = None
    decision_blocked_reasons: list[str] = Field(default_factory=list)
    guardrail_flags: list[str] = Field(default_factory=list)
    policy_blocked: bool = False
    policy_blocked_reasons: list[str] = Field(default_factory=list)
    risk_capped: bool = False
    risk_capped_reasons: list[str] = Field(default_factory=list)
    risk_capped_target_qty: Decimal | None = None
    position_management_reason_codes: list[str] = Field(default_factory=list)
    exit_attribution: str | None = None
    selected_strategy_family: StrategyFamily = "directional"
    selected_strategy_sleeve_id: str | None = None
    selected_strategy_route_action: StrategyRouteAction = "override_target"
    allocation_id: str | None = None
    strategy_selection_reason_codes: list[str] = Field(default_factory=list)
    strategy_selection_headline: str | None = None
    active_profile_id: str | None = None
    profile_control_source: ProfileControlSource | None = None
    ai_fallback_used: bool = False
    ai_degraded: bool = False


HedgeOverlayMode = Literal["protective", "opportunistic", "independent"]
HedgeOverlayState = Literal["disabled", "inactive", "opening", "holding", "closing", "blocked"]


class HedgeOverlayDecision(SchemaBase):
    enabled: bool = False
    runtime_supported: bool = False
    configured_mode: HedgeOverlayMode = "protective"
    effective_mode: HedgeOverlayMode | None = None
    overlay_source: str | None = None
    active: bool = False
    state: HedgeOverlayState = "disabled"
    main_leg_signal: Literal["long", "short", "flat"] = "flat"
    hedge_leg_signal: Literal["long", "short", "flat"] = "flat"
    main_leg_current_qty: Decimal = Decimal("0")
    hedge_leg_current_qty: Decimal = Decimal("0")
    main_leg_target_qty: Decimal = Decimal("0")
    hedge_leg_target_qty: Decimal = Decimal("0")
    hedge_ratio: Decimal = Decimal("0")
    max_ratio: Decimal = Decimal("0")
    pressure_score: float = 0.0
    open_threshold: float = 0.0
    close_threshold: float = 0.0
    open_condition: str | None = None
    close_condition: str | None = None
    fee_drag_ratio: float = 0.0
    churn_ratio: float = 0.0
    long_leg_score: float = 0.0
    short_leg_score: float = 0.0
    long_leg_reason_codes: list[str] = Field(default_factory=list)
    short_leg_reason_codes: list[str] = Field(default_factory=list)
    long_leg_blocked_reasons: list[str] = Field(default_factory=list)
    short_leg_blocked_reasons: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    min_hold_remaining_seconds: float = 0.0
    rebalance_cooldown_remaining_seconds: float = 0.0
    rollout_stage: Literal["replay_only", "dry_run", "live"] | None = None
    runtime_rollout_stage: Literal["replay_only", "dry_run", "live"] | None = None


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
        "scale_in_long",
        "reduce_long",
        "close_long",
        "open_short",
        "scale_in_short",
        "reduce_short",
        "close_short",
        "reverse_to_long",
        "reverse_to_short",
    ] = "hold"
    target_leverage: float = 1.0
    margin_mode: MarginModelType = "cash"
    leverage_bias: float = 1.0
    expected_signal_edge_bps: float = 0.0
    expected_cost_bps: float = 0.0
    expected_net_edge_bps: float = 0.0
    strategy_family: StrategyFamily = "directional"
    strategy_sleeve_id: str | None = None
    strategy_route_action: StrategyRouteAction = "override_target"
    strategy_pair_id: str | None = None
    strategy_opportunity_kind: str | None = None
    strategy_execution_mode: str | None = None
    strategy_state_phase: str | None = None
    strategy_reason_codes: list[str] = Field(default_factory=list)
    strategy_blocking_reasons: list[str] = Field(default_factory=list)
    strategy_headline: str | None = None
    allocation_id: str | None = None
    strategy_bundle_id: str | None = None
    strategy_execution_legs: list[StrategyLegIntent] = Field(default_factory=list)
    hedge_overlay_decision: HedgeOverlayDecision | None = None
    guardrail_flags: list[str] = Field(default_factory=list)
    ai_execution_parameter_suggestion: AIExecutionParameterSuggestionEnvelope | None = None
    ai_decision_intent: AIDecisionIntent | None = None
    profile_control_decision: ProfileControlDecision | None = None
    decision_outcome: DecisionOutcome | None = None
