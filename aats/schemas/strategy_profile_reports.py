from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from aats.schemas.common import SchemaBase, new_id, utc_now


class StrategyProfileOptimizationCandidate(SchemaBase):
    profile_id: str
    profile_label: str
    risk_level: str
    market_intent: str
    base_score: float = 0.0
    shadow_adjustment: float = 0.0
    replay_adjustment: float = 0.0
    stability_adjustment: float = 0.0
    composite_score: float = 0.0
    recommendation_strength: float = 0.0
    offline_replay_score: float = 0.0
    offline_replay_breakdown: dict[str, Any] = Field(default_factory=dict)
    selection_eligible: bool = True
    selection_blocked_reasons: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    evaluation_refs: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


class StrategyProfileOptimizationReport(SchemaBase):
    report_id: str = Field(default_factory=lambda: new_id("strp_opt"))
    generated_at: datetime = Field(default_factory=utc_now)
    version: int = 1
    parent_report_id: str | None = None
    scope: dict[str, Any] = Field(default_factory=dict)
    product_type: str | None = None
    margin_mode: str | None = None
    allowed_symbols: tuple[str, ...] = Field(default_factory=tuple)
    ranking_method: str = "replay_shadow_offline_v1"
    context_snapshot_id: str | None = None
    active_profile_id: str | None = None
    recommended_profile_id: str | None = None
    recommended_by: str = "winner_engine"
    score_delta_vs_active: float = 0.0
    replay_summary: dict[str, Any] = Field(default_factory=dict)
    offline_replay_pipeline: dict[str, Any] = Field(default_factory=dict)
    ai_performance_summary: dict[str, Any] = Field(default_factory=dict)
    control_summary: dict[str, Any] = Field(default_factory=dict)
    winner_selection_policy: dict[str, Any] = Field(default_factory=dict)
    version_experiments: list[dict[str, Any]] = Field(default_factory=list)
    candidates: list[StrategyProfileOptimizationCandidate] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class StrategyProfileSelectionDecision(SchemaBase):
    selection_decision_id: str = Field(default_factory=lambda: new_id("strp_sel"))
    created_at: datetime = Field(default_factory=utc_now)
    version: int = 1
    report_id: str
    parent_decision_id: str | None = None
    product_type: str | None = None
    margin_mode: str | None = None
    allowed_symbols: tuple[str, ...] = Field(default_factory=tuple)
    context_snapshot_id: str | None = None
    active_profile_id: str | None = None
    candidate_profile_id: str | None = None
    rollback_profile_id: str | None = None
    candidate_source: str = "winner_engine"
    activation_decision_source: str = "activation_gate"
    transition_class: str | None = None
    transition_risk_direction: str | None = None
    decision_status: str = "pending_review"
    execution_state: str = "not_executed"
    recommended_action: str | None = None
    fast_track_eligible: bool = False
    fast_track_applied: bool = False
    operator_summary: str | None = None
    gating_state: dict[str, Any] = Field(default_factory=dict)
    blocked_reasons: list[str] = Field(default_factory=list)
    rationale: list[str] = Field(default_factory=list)
    replay_guard: dict[str, Any] = Field(default_factory=dict)
    shadow_guard: dict[str, Any] = Field(default_factory=dict)
    execution_outcome: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class StrategyProfileActivationPolicyConfig(SchemaBase):
    policy_id: str = Field(default_factory=lambda: new_id("strp_activation_policy"))
    created_at: datetime = Field(default_factory=utc_now)
    product_type: str | None = None
    margin_mode: str | None = None
    allowed_symbols: tuple[str, ...] = Field(default_factory=tuple)
    previous_policy_id: str | None = None
    policy_status: str = "approved"
    effective: bool = True
    enabled: bool = False
    min_composite_score: float = 0.0
    min_offline_replay_score: float = -10.0
    min_recommendation_strength: float = 0.0
    require_positive_replay_consensus: bool = False
    disallow_when_shadow_review_required: bool = False
    matrix_allowed_symbols: tuple[str, ...] = Field(default_factory=tuple)
    matrix_allowed_regimes: tuple[str, ...] = Field(default_factory=tuple)
    matrix_allowed_profiles: tuple[str, ...] = Field(default_factory=tuple)
    approved_by: str | None = None
    approved_at: datetime | None = None
    frozen: bool = False
    frozen_by: str | None = None
    frozen_at: datetime | None = None
    freeze_reason: str | None = None
    updated_by: str | None = None
    update_reason: str | None = None
