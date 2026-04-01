from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from aats.schemas.common import SchemaBase, new_id, utc_now
from aats.schemas.decision import OverlayParentExposureAudit
from aats.schemas.strategy_runtime import StrategyExpectedVsRealizedSummary
from aats.schemas.system import MarginModelType, OperatingState, ProductType


RuntimeState = Literal["healthy", "degraded", "blocked", "halted"]
OperatorRole = Literal["anonymous", "viewer", "operator", "admin"]
AuthSource = Literal["anonymous", "session", "api_key", "local_config"]


class BlockerSnapshotRecord(SchemaBase):
    blocker_snapshot_id: str = Field(default_factory=lambda: new_id("blockers"))
    source: str
    runtime_state: RuntimeState
    operating_state: OperatingState
    mode: str
    halted: bool
    execution_blocked: bool
    submit_blocked: bool
    blockers: list[dict[str, Any]] = Field(default_factory=list)


class ExecutionErrorSummary(SchemaBase):
    error_id: str = Field(default_factory=lambda: new_id("execerr"))
    subsystem: str
    severity: Literal["warning", "error"]
    message: str
    decision_id: str | None = None
    intent_id: str | None = None
    order_id: str | None = None
    status: str | None = None
    observed_at: datetime


class ProcessingFailureRecord(SchemaBase):
    failure_id: str = Field(default_factory=lambda: new_id("procfail"))
    subsystem: str
    stage: str
    severity: Literal["warning", "error"]
    message: str
    decision_id: str | None = None
    intent_id: str | None = None
    order_id: str | None = None
    fill_id: str | None = None
    reconciliation_id: str | None = None
    symbol: str | None = None
    product_type: ProductType | None = None
    margin_mode: MarginModelType | None = None
    retriable: bool = False
    observed_at: datetime
    details: dict[str, Any] = Field(default_factory=dict)


class ReplayValidationSummary(SchemaBase):
    validation_id: str = Field(default_factory=lambda: new_id("replay"))
    validated_at: datetime
    decision_id: str | None = None
    symbol: str | None = None
    regime: str | None = None
    active_profile_id: str | None = None
    product_type: ProductType | None = None
    margin_mode: MarginModelType | None = None
    allowed_symbols: tuple[str, ...] = Field(default_factory=tuple)
    replayed_event_count: int
    stored_snapshot_count: int
    divergence_count: int
    portfolio_issues: list[str] = Field(default_factory=list)
    portfolio_issue_count: int = 0
    decision_chain_issues: list[str] = Field(default_factory=list)
    decision_chain_issue_count: int = 0
    execution_chain_issues: list[str] = Field(default_factory=list)
    execution_chain_issue_count: int = 0
    audit_issues: list[str] = Field(default_factory=list)
    audit_issue_count: int = 0
    baseline_switch_count: int = 0
    baseline_switch_issues: list[str] = Field(default_factory=list)
    baseline_switch_issue_count: int = 0
    incremental_window_start_at: datetime | None = None
    baseline_generation_id: str | None = None
    exchange_ack_watermark_id: str | None = None
    replay_offset_id: str | None = None
    divergence_density: float = 0.0
    chain_health_score: float = 0.0
    healthy: bool
    independent_expected_vs_realized_summary: StrategyExpectedVsRealizedSummary | None = None
    independent_adaptive_summary: dict[str, Any] | None = None
    overlay_parent_exposure_summary: OverlayParentExposureAudit | None = None


class ReconciliationValidationSummary(SchemaBase):
    validation_id: str = Field(default_factory=lambda: new_id("reconval"))
    trigger: str
    reconciliation_id: str
    decision_id: str | None = None
    severity: str
    halt_required: bool
    exchange_comparison_enabled: bool
    mismatch_reasons: list[str] = Field(default_factory=list)
    safety_impacts: list[str] = Field(default_factory=list)
    validated_at: datetime


class OperatorUserRecord(SchemaBase):
    user_id: str = Field(default_factory=lambda: new_id("opuser"))
    username: str
    password_hash: str
    role: Literal["viewer", "operator", "admin"]
    enabled: bool = True
    session_version: int = 1
    updated_at: datetime = Field(default_factory=utc_now)
    last_login_at: datetime | None = None
    last_failed_login_at: datetime | None = None
    failed_login_attempts: int = 0
    locked_until: datetime | None = None


class OperatorActionRecord(SchemaBase):
    action_id: str = Field(default_factory=lambda: new_id("opact"))
    action: Literal[
        "halt",
        "resume",
        "mode_change",
        "reconciliation_validate",
        "rebaseline",
        "cancel_order",
        "resolve_stuck_submission",
        "login",
        "user_create",
        "user_update",
        "user_delete",
        "runtime_profile_create",
        "runtime_profile_update",
        "runtime_profile_stage",
        "runtime_profile_stage_rejected",
        "runtime_profile_cancel_pending",
        "runtime_profile_restart_request",
        "runtime_profile_activation",
        "runtime_profile_activation_failed",
        "runtime_profile_supervisor_restart",
        "strategy_profile_evaluate",
        "strategy_profile_accept",
        "strategy_profile_reject",
        "strategy_profile_activate_pending",
        "strategy_profile_manual_activate",
        "strategy_profile_pause_auto",
        "strategy_profile_restore_auto",
        "strategy_profile_rollback",
        "strategy_profile_activation_policy",
        "ai_shadow_evaluate",
        "ai_operating_mode_select",
        "ai_review_restore",
        "ai_review_degrade_to_baseline",
        "phase1_shadow_review",
        "refresh_exchange_state",
        "capital_scale_review",
        "trial_review_snapshot",
        "trial_guard_manual_reset",
    ]
    actor_role: OperatorRole
    actor_identity: str | None = None
    auth_source: AuthSource = "anonymous"
    reason: str
    status: str
    decision_id: str | None = None
    order_id: str | None = None
    recovery_state_before: str | None = None
    recovery_state_after: str | None = None
    baseline_event_ref: str | None = None
    reconciliation_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
