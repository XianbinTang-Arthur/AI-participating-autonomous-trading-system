from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import SchemaBase, new_id, utc_now


StrategyRiskLevel = Literal["conservative", "normal", "aggressive"]
StrategyMarketIntent = Literal["trend", "range", "high_volatility", "execution_degraded"]
StrategyProfileStatus = Literal["draft", "pending", "active", "superseded", "rejected", "rolled_back"]
StrategyActivationMode = Literal["manual", "auto", "rollback", "staged"]
StrategyProfileAxisLevel = Literal["relaxed", "balanced", "strict", "defensive"]
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
    "strategy_entry_min_signal_edge_bps",
    "strategy_entry_alpha_min",
    "strategy_entry_confidence_min",
    "strategy_short_entry_allowed_regimes",
    "strategy_short_entry_min_signal_edge_bps",
    "strategy_short_entry_alpha_min",
    "strategy_short_entry_confidence_min",
    "strategy_scale_in_min_signal_edge_bps",
    "strategy_scale_in_alpha_min",
    "strategy_scale_in_confidence_min",
    "strategy_short_scale_in_min_signal_edge_bps",
    "strategy_short_scale_in_alpha_min",
    "strategy_short_scale_in_confidence_min",
    "strategy_reversal_min_signal_edge_bps",
    "strategy_reversal_alpha_min",
    "strategy_reversal_confidence_min",
    "strategy_short_reversal_min_signal_edge_bps",
    "strategy_short_reversal_alpha_min",
    "strategy_short_reversal_confidence_min",
    "strategy_min_hold_seconds",
    "strategy_post_close_cooldown_seconds",
    "strategy_max_fee_drag_ratio",
    "strategy_max_churn_ratio",
    "strategy_low_edge_threshold_bps",
    "strategy_low_edge_streak_limit",
    "strategy_low_edge_cooldown_seconds",
    "strategy_transient_close_retry_cooldown_seconds",
    # Independent hedge family — profile 管辖面扩展 (2026-04-19):
    # 切 profile 时这 9 个字段跟着变, 让 operator/AI 自动切档对 independent
    # family 真正生效 (之前 profile 只覆盖 mainline directional 字段, 独立双书
    # 参数在档位切换时不动)。
    # 范围限定原则:
    #   - 只纳入"档位风格"字段 (entry/close/scale_in/持仓/确认严格度)
    #   - 不纳入 family enable 开关, rollout/metrics, catastrophic buffer,
    #     执行模式 (*_execution_mode), de_risk / liquidity 开关, ai_operating_mode
    #     (后者是独立的 AI 参与度机制, 与 profile 切档解耦)
    # 字段本身 independent 专属 (只有 independent engine 读), 但 "切档机制"
    # 共用: 一次 apply_strategy_profile_payload 同时改 directional+independent 字段。
    "strategy_hedge_independent_long_entry_threshold",
    "strategy_hedge_independent_short_entry_threshold",
    "strategy_hedge_independent_long_close_threshold",
    "strategy_hedge_independent_short_close_threshold",
    "strategy_hedge_independent_long_scale_in_threshold",
    "strategy_hedge_independent_short_scale_in_threshold",
    "strategy_hedge_independent_long_min_hold_seconds",
    "strategy_hedge_independent_min_confirm_ticks",
    "strategy_hedge_independent_min_score_stability_bps",
)

STRATEGY_PROFILE_SHORT_FIELD_PAIRS: tuple[tuple[str, str], ...] = (
    ("strategy_short_entry_allowed_regimes", "strategy_entry_allowed_regimes"),
    ("strategy_short_entry_min_signal_edge_bps", "strategy_entry_min_signal_edge_bps"),
    ("strategy_short_entry_alpha_min", "strategy_entry_alpha_min"),
    ("strategy_short_entry_confidence_min", "strategy_entry_confidence_min"),
    ("strategy_short_scale_in_min_signal_edge_bps", "strategy_scale_in_min_signal_edge_bps"),
    ("strategy_short_scale_in_alpha_min", "strategy_scale_in_alpha_min"),
    ("strategy_short_scale_in_confidence_min", "strategy_scale_in_confidence_min"),
    ("strategy_short_reversal_min_signal_edge_bps", "strategy_reversal_min_signal_edge_bps"),
    ("strategy_short_reversal_alpha_min", "strategy_reversal_alpha_min"),
    ("strategy_short_reversal_confidence_min", "strategy_reversal_confidence_min"),
)

STRATEGY_PROFILE_SHORT_FIELDS: tuple[str, ...] = tuple(
    short_field for short_field, _legacy_field in STRATEGY_PROFILE_SHORT_FIELD_PAIRS
)


def normalize_strategy_profile_payload_for_product_type(
    payload: "StrategyProfilePayload | dict[str, Any]",
    *,
    product_type: str,
) -> "StrategyProfilePayload":
    raw = payload.model_dump(mode="python") if isinstance(payload, StrategyProfilePayload) else dict(payload)
    normalized = dict(raw)
    if product_type == "spot":
        for short_field, shared_field in STRATEGY_PROFILE_SHORT_FIELD_PAIRS:
            normalized[short_field] = normalized.get(shared_field)
    return StrategyProfilePayload.model_validate(normalized)


def _strategy_profile_summary_fields(*, product_type: str | None) -> tuple[str, ...]:
    if product_type == "spot":
        return tuple(field for field in STRATEGY_PROFILE_MANAGED_FIELDS if field not in STRATEGY_PROFILE_SHORT_FIELDS)
    return STRATEGY_PROFILE_MANAGED_FIELDS


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
    strategy_entry_min_signal_edge_bps: float
    strategy_entry_alpha_min: float
    strategy_entry_confidence_min: float
    strategy_short_entry_allowed_regimes: tuple[str, ...]
    strategy_short_entry_min_signal_edge_bps: float
    strategy_short_entry_alpha_min: float
    strategy_short_entry_confidence_min: float
    strategy_scale_in_min_signal_edge_bps: float
    strategy_scale_in_alpha_min: float
    strategy_scale_in_confidence_min: float
    strategy_short_scale_in_min_signal_edge_bps: float
    strategy_short_scale_in_alpha_min: float
    strategy_short_scale_in_confidence_min: float
    strategy_reversal_min_signal_edge_bps: float
    strategy_reversal_alpha_min: float
    strategy_reversal_confidence_min: float
    strategy_short_reversal_min_signal_edge_bps: float
    strategy_short_reversal_alpha_min: float
    strategy_short_reversal_confidence_min: float
    strategy_min_hold_seconds: float
    strategy_post_close_cooldown_seconds: float
    strategy_max_fee_drag_ratio: float
    strategy_max_churn_ratio: float
    strategy_low_edge_threshold_bps: float
    strategy_low_edge_streak_limit: int
    strategy_low_edge_cooldown_seconds: float
    strategy_transient_close_retry_cooldown_seconds: float
    # Independent hedge family — profile 管辖面扩展 (2026-04-19):
    # 默认值与 AATSSettings 同字段对齐 (settings.py:531-548), 保证历史 profile
    # payload (DB 里存的 StrategyProfileRevision) 缺这些字段时能无损反序列化,
    # 下一次 strategy_profile_payload_from_settings 时从 settings 读真实值覆盖。
    strategy_hedge_independent_long_entry_threshold: float = 0.66
    strategy_hedge_independent_short_entry_threshold: float = 0.66
    strategy_hedge_independent_long_close_threshold: float = 0.66
    strategy_hedge_independent_short_close_threshold: float = 0.66
    strategy_hedge_independent_long_scale_in_threshold: float = 0.70
    strategy_hedge_independent_short_scale_in_threshold: float = 0.70
    strategy_hedge_independent_long_min_hold_seconds: float = 300.0
    strategy_hedge_independent_min_confirm_ticks: int = 2
    strategy_hedge_independent_min_score_stability_bps: float = 2.0

    @model_validator(mode="before")
    @classmethod
    def backfill_legacy_short_thresholds(cls, raw: Any) -> Any:
        if not isinstance(raw, dict):
            return raw
        payload = dict(raw)
        fallback_pairs = (
            ("strategy_short_entry_allowed_regimes", "strategy_entry_allowed_regimes"),
            ("strategy_short_entry_min_signal_edge_bps", "strategy_entry_min_signal_edge_bps"),
            ("strategy_short_entry_alpha_min", "strategy_entry_alpha_min"),
            ("strategy_short_entry_confidence_min", "strategy_entry_confidence_min"),
            ("strategy_short_scale_in_min_signal_edge_bps", "strategy_scale_in_min_signal_edge_bps"),
            ("strategy_short_scale_in_alpha_min", "strategy_scale_in_alpha_min"),
            ("strategy_short_scale_in_confidence_min", "strategy_scale_in_confidence_min"),
            ("strategy_short_reversal_min_signal_edge_bps", "strategy_reversal_min_signal_edge_bps"),
            ("strategy_short_reversal_alpha_min", "strategy_reversal_alpha_min"),
            ("strategy_short_reversal_confidence_min", "strategy_reversal_confidence_min"),
        )
        for short_field, legacy_field in fallback_pairs:
            value = payload.get(short_field)
            if value is None and legacy_field in payload:
                payload[short_field] = payload.get(legacy_field)
        return payload


class StrategyProfileAxes(SchemaBase):
    frequency: StrategyProfileAxisLevel
    entry_threshold: StrategyProfileAxisLevel
    scale_in_threshold: StrategyProfileAxisLevel
    reversal_threshold: StrategyProfileAxisLevel
    cost_protection: StrategyProfileAxisLevel
    cooldown_fuse: StrategyProfileAxisLevel


class StrategyProfileComparisonRow(SchemaBase):
    profile_id: str
    profile_label: str
    risk_level: StrategyRiskLevel
    market_intent: StrategyMarketIntent
    axes: StrategyProfileAxes
    evaluation_count: int = 0
    total_trade_count: int = 0
    avg_net_realized_pnl: float = 0.0
    avg_fee_to_gross_pnl_ratio: float = 0.0
    avg_small_pnl_churn_ratio: float = 0.0
    avg_win_rate: float = 0.0
    latest_status: StrategyEvaluationStatus | None = None
    active: bool = False
    pending: bool = False
    score: float = 0.0
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    expected_behavior: list[str] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class StrategyProfileComparisonReport(SchemaBase):
    report_id: str = Field(default_factory=lambda: new_id("strp_cmp"))
    scope: dict[str, Any] = Field(default_factory=dict)
    generated_at: datetime = Field(default_factory=utc_now)
    ranking_method: str = "historical_eval_plus_shadow_guard"
    shadow_summary: dict[str, Any] = Field(default_factory=dict)
    active_profile_id: str | None = None
    rows: list[StrategyProfileComparisonRow] = Field(default_factory=list)


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


class StrategyProfileEvaluationContextSnapshot(SchemaBase):
    snapshot_id: str = Field(default_factory=lambda: new_id("strp_ctx"))
    snapshot_ts: datetime = Field(default_factory=utc_now)
    scope: dict[str, Any] = Field(default_factory=dict)
    baseline: dict[str, Any] | None = None
    features: dict[str, Any] | None = None
    portfolio: dict[str, Any] | None = None
    safety_state: dict[str, Any] = Field(default_factory=dict)
    execution_health: dict[str, Any] = Field(default_factory=dict)
    performance: dict[str, Any] = Field(default_factory=dict)
    current_profile_id: str | None = None
    profile_selection_policy: dict[str, Any] = Field(default_factory=dict)
    candidate_profiles: list[dict[str, Any]] = Field(default_factory=list)


class StrategyProfileAIAdvice(SchemaBase):
    provider: str = "rule_fallback"
    model_name: str | None = None
    preferred_profile_id: str | None = None
    confidence: float = 0.0
    agreement_with_candidate: bool = False
    confidence_adjustment: float = 0.0
    market_regime_assessment: StrategyProfileMarketRegimeAssessment | None = None
    reason_codes: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    summary: str | None = None
    fallback_reason_code: str | None = None
    fallback_reason_detail: str | None = None
    used_fallback: bool = False


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
    selection_source: str = "winner_engine"
    context_snapshot_id: str | None = None
    ai_advice: StrategyProfileAIAdvice | None = None
    fallback_reason_code: str | None = None
    fallback_reason_detail: str | None = None
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
    return normalize_strategy_profile_payload_for_product_type(
        selected,
        product_type=settings.trading_product_type,
    )


def apply_strategy_profile_payload(settings: AATSSettings, payload: StrategyProfilePayload | dict[str, Any]) -> None:
    raw = normalize_strategy_profile_payload_for_product_type(
        payload,
        product_type=settings.trading_product_type,
    ).model_dump(mode="python")
    for field in STRATEGY_PROFILE_MANAGED_FIELDS:
        if field in raw:
            setattr(settings, field, raw[field])


def summarize_strategy_profile_payload(
    payload: StrategyProfilePayload | dict[str, Any],
    *,
    product_type: str | None = None,
) -> dict[str, Any]:
    raw = normalize_strategy_profile_payload_for_product_type(
        payload,
        product_type=product_type or "derivatives",
    ).model_dump(mode="python")
    summary = {
        "axes": strategy_profile_axes_from_payload(raw, product_type=product_type).model_dump(mode="json"),
        "decision_min_interval_seconds_15m": raw.get("decision_min_interval_seconds_15m"),
        "max_decisions_per_minute": raw.get("max_decisions_per_minute"),
        "decision_min_price_move_bps": raw.get("decision_min_price_move_bps"),
        "decision_min_momentum_delta": raw.get("decision_min_momentum_delta"),
        "strategy_min_net_edge_bps": raw.get("strategy_min_net_edge_bps"),
        "strategy_entry_allowed_regimes": raw.get("strategy_entry_allowed_regimes"),
        "strategy_entry_min_signal_edge_bps": raw.get("strategy_entry_min_signal_edge_bps"),
        "strategy_entry_alpha_min": raw.get("strategy_entry_alpha_min"),
        "strategy_entry_confidence_min": raw.get("strategy_entry_confidence_min"),
        "strategy_short_entry_allowed_regimes": raw.get("strategy_short_entry_allowed_regimes"),
        "strategy_short_entry_min_signal_edge_bps": raw.get("strategy_short_entry_min_signal_edge_bps"),
        "strategy_short_entry_alpha_min": raw.get("strategy_short_entry_alpha_min"),
        "strategy_short_entry_confidence_min": raw.get("strategy_short_entry_confidence_min"),
        "strategy_scale_in_min_signal_edge_bps": raw.get("strategy_scale_in_min_signal_edge_bps"),
        "strategy_scale_in_alpha_min": raw.get("strategy_scale_in_alpha_min"),
        "strategy_scale_in_confidence_min": raw.get("strategy_scale_in_confidence_min"),
        "strategy_short_scale_in_min_signal_edge_bps": raw.get("strategy_short_scale_in_min_signal_edge_bps"),
        "strategy_short_scale_in_alpha_min": raw.get("strategy_short_scale_in_alpha_min"),
        "strategy_short_scale_in_confidence_min": raw.get("strategy_short_scale_in_confidence_min"),
        "strategy_reversal_min_signal_edge_bps": raw.get("strategy_reversal_min_signal_edge_bps"),
        "strategy_reversal_alpha_min": raw.get("strategy_reversal_alpha_min"),
        "strategy_reversal_confidence_min": raw.get("strategy_reversal_confidence_min"),
        "strategy_short_reversal_min_signal_edge_bps": raw.get("strategy_short_reversal_min_signal_edge_bps"),
        "strategy_short_reversal_alpha_min": raw.get("strategy_short_reversal_alpha_min"),
        "strategy_short_reversal_confidence_min": raw.get("strategy_short_reversal_confidence_min"),
        "strategy_min_hold_seconds": raw.get("strategy_min_hold_seconds"),
        "strategy_post_close_cooldown_seconds": raw.get("strategy_post_close_cooldown_seconds"),
        "strategy_max_fee_drag_ratio": raw.get("strategy_max_fee_drag_ratio"),
        "strategy_max_churn_ratio": raw.get("strategy_max_churn_ratio"),
        "strategy_low_edge_threshold_bps": raw.get("strategy_low_edge_threshold_bps"),
        "strategy_low_edge_streak_limit": raw.get("strategy_low_edge_streak_limit"),
        "strategy_low_edge_cooldown_seconds": raw.get("strategy_low_edge_cooldown_seconds"),
    }
    if product_type == "spot":
        for field in STRATEGY_PROFILE_SHORT_FIELDS:
            summary.pop(field, None)
    return summary


def strategy_profile_axes_from_payload(
    payload: StrategyProfilePayload | dict[str, Any],
    *,
    product_type: str | None = None,
) -> StrategyProfileAxes:
    raw = normalize_strategy_profile_payload_for_product_type(
        payload,
        product_type=product_type or "derivatives",
    ).model_dump(mode="python")

    def level(
        value: float,
        *,
        low: float,
        medium: float,
        high: float,
        invert: bool = False,
    ) -> StrategyProfileAxisLevel:
        compare = -float(value) if invert else float(value)
        threshold_low = -high if invert else low
        threshold_medium = -medium if invert else medium
        threshold_high = -low if invert else high
        if compare <= threshold_low:
            return "relaxed"
        if compare <= threshold_medium:
            return "balanced"
        if compare <= threshold_high:
            return "strict"
        return "defensive"

    frequency_signal = max(
        float(raw.get("decision_min_interval_seconds_15m", 0.0) or 0.0) / 60.0,
        float(raw.get("max_decisions_per_minute", 0) or 0),
    )
    cost_signal = max(
        float(raw.get("strategy_min_net_edge_bps", 0.0) or 0.0),
        float(raw.get("decision_min_price_move_bps", 0.0) or 0.0),
    )
    cooldown_signal = max(
        float(raw.get("strategy_transient_close_retry_cooldown_seconds", 0.0) or 0.0),
        float(raw.get("strategy_post_close_cooldown_seconds", 0.0) or 0.0),
        (float(raw.get("strategy_min_hold_seconds", 0.0) or 0.0) / 8.0),
        float(raw.get("strategy_low_edge_cooldown_seconds", 0.0) or 0.0) / 12.0,
    )
    entry_alpha_signal = max(
        float(raw.get("strategy_entry_alpha_min", 0.0) or 0.0),
        float(raw.get("strategy_short_entry_alpha_min", 0.0) or 0.0),
    )
    scale_in_alpha_signal = max(
        float(raw.get("strategy_scale_in_alpha_min", 0.0) or 0.0),
        float(raw.get("strategy_short_scale_in_alpha_min", 0.0) or 0.0),
    )
    reversal_alpha_signal = max(
        float(raw.get("strategy_reversal_alpha_min", 0.0) or 0.0),
        float(raw.get("strategy_short_reversal_alpha_min", 0.0) or 0.0),
    )
    return StrategyProfileAxes(
        frequency=level(frequency_signal, low=1.5, medium=2.5, high=4.0),
        entry_threshold=level(entry_alpha_signal, low=0.16, medium=0.22, high=0.28),
        scale_in_threshold=level(scale_in_alpha_signal, low=0.2, medium=0.28, high=0.34),
        reversal_threshold=level(reversal_alpha_signal, low=0.26, medium=0.34, high=0.4),
        cost_protection=level(cost_signal, low=5.0, medium=8.0, high=12.0),
        cooldown_fuse=level(cooldown_signal, low=90.0, medium=180.0, high=300.0),
    )


def diff_strategy_profile_payload(
    previous: StrategyProfilePayload | dict[str, Any],
    next_payload: StrategyProfilePayload | dict[str, Any],
    *,
    product_type: str | None = None,
) -> dict[str, Any]:
    normalized_product_type = product_type or "derivatives"
    previous_raw = normalize_strategy_profile_payload_for_product_type(
        previous,
        product_type=normalized_product_type,
    ).model_dump(mode="python")
    next_raw = normalize_strategy_profile_payload_for_product_type(
        next_payload,
        product_type=normalized_product_type,
    ).model_dump(mode="python")
    changed_fields = [
        field
        for field in _strategy_profile_summary_fields(product_type=product_type)
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
