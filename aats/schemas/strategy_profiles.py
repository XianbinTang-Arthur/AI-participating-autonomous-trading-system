from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, Field

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import SchemaBase, new_id, utc_now


StrategyRiskLevel = Literal["conservative", "normal", "aggressive"]
StrategyMarketIntent = Literal["trend", "range", "high_volatility", "execution_degraded"]
StrategyProfileStatus = Literal["draft", "pending", "active", "superseded", "rejected", "rolled_back"]
StrategyActivationMode = Literal["manual", "auto", "rollback", "staged"]
StrategyActivationResult = Literal[
    "none",
    "activation_succeeded",
    "activation_failed",
    "activation_rejected",
    "rollback_succeeded",
    "rollback_failed",
]
StrategyTriggerType = Literal["manual", "ai_auto", "rollback", "system_guard"]
StrategyRecommendationDecision = Literal["pending", "accepted", "rejected", "expired"]
StrategyEvaluationStatus = Literal["observing", "healthy", "degraded", "rollback_recommended", "rollback_executed"]

STRATEGY_PROFILE_MANAGED_FIELDS: tuple[str, ...] = (
    "decision_min_interval_seconds_15m",
    "decision_min_interval_seconds_1h",
    "max_decisions_per_minute",
    "decision_min_price_move_bps",
    "decision_min_momentum_delta",
    "strategy_flat_signal_hold_enabled",
    "strategy_flat_exit_microstructure_threshold",
    "strategy_flat_exit_factor_threshold",
    "strategy_flat_exit_ai_edge_threshold",
    "strategy_cost_guard_enabled",
    "strategy_alpha_edge_bps_scale",
    "strategy_expected_slippage_bps_fraction",
    "strategy_min_net_edge_bps",
    "strategy_entry_allowed_regimes",
    "strategy_entry_alpha_min",
    "strategy_entry_confidence_min",
    "strategy_scale_in_alpha_min",
    "strategy_scale_in_confidence_min",
    "strategy_reversal_alpha_min",
    "strategy_reversal_confidence_min",
    "strategy_transient_close_retry_cooldown_seconds",
)


class StrategyProfilePayload(SchemaBase):
    decision_min_interval_seconds_15m: float
    decision_min_interval_seconds_1h: float
    max_decisions_per_minute: int
    decision_min_price_move_bps: float
    decision_min_momentum_delta: float
    strategy_flat_signal_hold_enabled: bool
    strategy_flat_exit_microstructure_threshold: float
    strategy_flat_exit_factor_threshold: float
    strategy_flat_exit_ai_edge_threshold: float
    strategy_cost_guard_enabled: bool
    strategy_alpha_edge_bps_scale: float
    strategy_expected_slippage_bps_fraction: float
    strategy_min_net_edge_bps: float
    strategy_entry_allowed_regimes: tuple[str, ...]
    strategy_entry_alpha_min: float
    strategy_entry_confidence_min: float
    strategy_scale_in_alpha_min: float
    strategy_scale_in_confidence_min: float
    strategy_reversal_alpha_min: float
    strategy_reversal_confidence_min: float
    strategy_transient_close_retry_cooldown_seconds: float


class StrategyProfileGuardrails(SchemaBase):
    entry_scale_reversal_ordering_required: bool = True
    max_single_field_change_ratio: float = 0.15
    cannot_lower_cost_guard_below_bps: float = 6.0
    activation_min_holding_minutes: int = 120
    disallow_auto_apply_when_open_orders_present: bool = True
    disallow_auto_apply_when_review_required: bool = True
    disallow_auto_apply_when_reconciliation_not_clean: bool = True


class StrategyProfileRevision(SchemaBase):
    revision_id: str = Field(default_factory=lambda: new_id("strp_rev"))
    profile_family: str = "strategy_tuning"
    profile_id: str
    profile_label: str
    version: int = 1
    status: StrategyProfileStatus = "draft"
    risk_level: StrategyRiskLevel
    market_intent: StrategyMarketIntent
    product_type: str
    margin_mode: str
    allowed_symbols: tuple[str, ...]
    hot_safe_only: bool = True
    auto_switch_allowed: bool = False
    manual_approval_required: bool = True
    payload: StrategyProfilePayload
    guardrails: StrategyProfileGuardrails = Field(default_factory=StrategyProfileGuardrails)
    description: str | None = None
    expected_behavior: list[str] = Field(default_factory=list)
    created_by: str
    created_reason: str
    source_recommendation_id: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class StrategyProfileActivationState(SchemaBase):
    activation_id: str = "strategy_profile_activation"
    product_type: str
    margin_mode: str
    allowed_symbols: tuple[str, ...]
    active_revision_id: str | None = None
    pending_revision_id: str | None = None
    previous_active_revision_id: str | None = None
    active_profile_id: str | None = None
    pending_profile_id: str | None = None
    activation_mode: StrategyActivationMode = "manual"
    restart_required: bool = False
    last_activation_result: StrategyActivationResult = "none"
    last_activation_at: datetime | None = None
    last_activation_error: str | None = None
    last_switch_reason: str | None = None
    last_switch_actor: str | None = None
    cooldown_until: datetime | None = None
    frozen_until: datetime | None = None
    auto_switch_enabled: bool = False


class StrategyProfileMarketRegimeAssessment(BaseModel):
    regime: str
    volatility_state: str
    execution_condition: str


class StrategyProfileRecommendationOutput(BaseModel):
    recommended_profile_id: str
    fallback_profile_id: str | None = None
    confidence: float
    market_regime_assessment: StrategyProfileMarketRegimeAssessment
    reason_codes: list[str] = Field(default_factory=list)
    human_summary: str
    risk_notes: list[str] = Field(default_factory=list)
    valid_for_minutes: int


class StrategyProfileRecommendation(SchemaBase):
    recommendation_id: str = Field(default_factory=lambda: new_id("strp_rec"))
    schema_version: str = "1.0"
    product_type: str
    margin_mode: str
    allowed_symbols: tuple[str, ...]
    active_profile_id: str | None = None
    recommended_profile_id: str
    fallback_profile_id: str | None = None
    confidence: float
    market_regime_assessment: StrategyProfileMarketRegimeAssessment
    reason_codes: list[str] = Field(default_factory=list)
    human_summary: str
    risk_notes: list[str] = Field(default_factory=list)
    valid_for_minutes: int
    generated_by: str
    model_name: str | None = None
    prompt_version: str | None = None
    input_digest: str
    input_snapshot: dict[str, Any] = Field(default_factory=dict)
    generated_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    decision_status: StrategyRecommendationDecision = "pending"
    decision_reason_code: str | None = None
    decision_reason_detail: str | None = None


class StrategyProfileActivationRecord(SchemaBase):
    activation_event_id: str = Field(default_factory=lambda: new_id("strp_act"))
    product_type: str
    margin_mode: str
    allowed_symbols: tuple[str, ...]
    from_revision_id: str | None = None
    to_revision_id: str | None = None
    from_profile_id: str | None = None
    to_profile_id: str | None = None
    trigger_type: StrategyTriggerType
    actor_identity: str | None = None
    actor_role: str
    auth_source: str
    recommendation_id: str | None = None
    result: str
    reason_code: str
    reason_detail: str | None = None
    hot_safe: bool
    restart_required: bool
    diff: dict[str, Any] = Field(default_factory=dict)
    executed_at: datetime = Field(default_factory=utc_now)


class StrategyProfileRejectionRecord(SchemaBase):
    rejection_id: str = Field(default_factory=lambda: new_id("strp_rej"))
    product_type: str
    margin_mode: str
    allowed_symbols: tuple[str, ...]
    recommendation_id: str
    recommended_profile_id: str
    rejection_source: str
    rejection_reason_code: str
    rejection_reason_detail: str | None = None
    actor_identity: str | None = None
    actor_role: str


class StrategyProfileEvaluationRecord(SchemaBase):
    evaluation_id: str = Field(default_factory=lambda: new_id("strp_eval"))
    revision_id: str
    profile_id: str
    product_type: str
    margin_mode: str
    allowed_symbols: tuple[str, ...]
    window_start: datetime
    window_end: datetime | None = None
    trade_count: int
    win_rate: float
    gross_realized_pnl: float
    net_realized_pnl: float
    fee_total: float
    fee_to_gross_pnl_ratio: float
    small_pnl_churn_ratio: float
    execution_error_rate: float
    reconciliation_issue_count: int
    status: StrategyEvaluationStatus
    summary: dict[str, Any] = Field(default_factory=dict)


def strategy_profile_payload_from_settings(settings: AATSSettings) -> StrategyProfilePayload:
    payload = settings.model_dump(mode="python")
    selected = {field: payload[field] for field in STRATEGY_PROFILE_MANAGED_FIELDS}
    return StrategyProfilePayload.model_validate(selected)


def apply_strategy_profile_payload(settings: AATSSettings, payload: StrategyProfilePayload | dict[str, Any]) -> None:
    raw = payload.model_dump(mode="python") if isinstance(payload, StrategyProfilePayload) else dict(payload)
    for field in STRATEGY_PROFILE_MANAGED_FIELDS:
        if field in raw:
            setattr(settings, field, raw[field])


def summarize_strategy_profile_payload(payload: StrategyProfilePayload | dict[str, Any]) -> dict[str, Any]:
    raw = payload.model_dump(mode="python") if isinstance(payload, StrategyProfilePayload) else dict(payload)
    return {
        "decision_min_interval_seconds_15m": raw.get("decision_min_interval_seconds_15m"),
        "max_decisions_per_minute": raw.get("max_decisions_per_minute"),
        "decision_min_price_move_bps": raw.get("decision_min_price_move_bps"),
        "decision_min_momentum_delta": raw.get("decision_min_momentum_delta"),
        "strategy_min_net_edge_bps": raw.get("strategy_min_net_edge_bps"),
        "strategy_entry_allowed_regimes": raw.get("strategy_entry_allowed_regimes"),
        "strategy_entry_alpha_min": raw.get("strategy_entry_alpha_min"),
        "strategy_scale_in_alpha_min": raw.get("strategy_scale_in_alpha_min"),
        "strategy_reversal_alpha_min": raw.get("strategy_reversal_alpha_min"),
    }


def diff_strategy_profile_payload(
    previous: StrategyProfilePayload | dict[str, Any],
    next_payload: StrategyProfilePayload | dict[str, Any],
) -> dict[str, Any]:
    previous_raw = previous.model_dump(mode="python") if isinstance(previous, StrategyProfilePayload) else dict(previous)
    next_raw = (
        next_payload.model_dump(mode="python")
        if isinstance(next_payload, StrategyProfilePayload)
        else dict(next_payload)
    )
    changed_fields = [
        field
        for field in STRATEGY_PROFILE_MANAGED_FIELDS
        if previous_raw.get(field) != next_raw.get(field)
    ]
    return {
        "changed_fields": changed_fields,
        "previous_values": {field: previous_raw.get(field) for field in changed_fields},
        "next_values": {field: next_raw.get(field) for field in changed_fields},
    }


def strategy_profile_scope_hash(*, product_type: str, margin_mode: str, allowed_symbols: tuple[str, ...]) -> str:
    joined = "|".join([product_type, margin_mode, *sorted(allowed_symbols)])
    return sha256(joined.encode("utf-8")).hexdigest()
