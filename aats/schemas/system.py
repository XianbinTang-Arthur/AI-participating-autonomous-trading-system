from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field

from aats.schemas.common import SchemaBase


HealthStatus = Literal["ok", "warn", "blocked"]
RuntimeProfileName = Literal[
    "paper_local",
    "exchange_simulated",
    "exchange_simulated_spot",
    "exchange_simulated_derivatives",
    "exchange_live_spot",
    "exchange_live_derivatives",
]
ProductType = Literal["spot", "derivatives"]
PositionDirectionality = Literal["long_only", "bi_directional"]
LeverageSupport = Literal["none", "supported"]
MarginModelType = Literal["cash", "cross", "isolated"]
OperatingState = Literal[
    "local_demo",
    "real_market_paper",
    "guarded_simulated_submit_dry_run",
    "guarded_simulated_submit_enabled",
    "guarded_simulated_submit_spot_dry_run",
    "guarded_simulated_submit_spot_enabled",
    "guarded_simulated_submit_derivatives_dry_run",
    "guarded_simulated_submit_derivatives_enabled",
    "guarded_live_blocked",
    "guarded_live_enabled",
]


class ComponentHealth(SchemaBase):
    component: str
    status: HealthStatus
    connected: bool
    fresh: bool
    last_update_ts: datetime | None = None
    detail: str | None = None
    blockers: list[str] = Field(default_factory=list)


class HealthSnapshot(SchemaBase):
    decision_id: str | None = None
    mode: str
    operating_state: OperatingState
    status: HealthStatus
    halted: bool
    blockers: list[str] = Field(default_factory=list)
    components: list[ComponentHealth] = Field(default_factory=list)


class SystemHealthSnapshot(HealthSnapshot):
    pass


class RuntimeProfileState(SchemaBase):
    name: RuntimeProfileName
    description: str
    product_type: ProductType
    exchange_coupled: bool
    exchange_submission_capable: bool
    persistent_storage_required: bool
    account_synchronization_meaningful: bool
    rebaseline_meaningful: bool
    live_trading_blocked: bool
    shorting_supported: bool
    leverage_supported: bool


class EnvironmentCapabilitiesState(SchemaBase):
    product_type: ProductType
    market_data_source_kind: str
    account_state_source_kind: str
    execution_adapter_kind: str
    execution_route: str
    exchange_submission_target: str
    exchange_submission_possible: bool
    exchange_submission_enabled: bool
    persistent_storage_required: bool
    exchange_coupled: bool
    local_only: bool
    position_directionality: PositionDirectionality
    leverage_support: LeverageSupport
    margin_model: MarginModelType


class PolicyProfileState(SchemaBase):
    name: str
    product_type: ProductType
    exchange_submission_allowed_in_principle: bool
    dry_run_only: bool
    requires_human_approval: bool
    enforce_health_blockers: bool
    blocks_on_account_freshness: bool
    blocks_on_reconciliation_freshness: bool
    blocks_on_review_required: bool
    balance_checks_required: bool
    real_money_submission_structurally_blocked: bool
    shorting_allowed: bool
    leverage_allowed: bool
    max_target_leverage: float


class RecoveryPolicyState(SchemaBase):
    name: str
    product_type: ProductType
    startup_baseline_import_supported: bool
    operator_rebaseline_supported: bool
    account_snapshot_required: bool
    review_required_blocks_resume: bool
    reconciliation_required_for_execution_state: bool
    exchange_portfolio_comparison_enabled: bool
    derivatives_position_comparison_enabled: bool


class RuntimeModeState(SchemaBase):
    mode: str
    config_profile: str
    startup_profile: ProductType | None = None
    operating_state: OperatingState
    runtime_profile: RuntimeProfileState
    environment_capabilities: EnvironmentCapabilitiesState
    policy_profile: PolicyProfileState
    recovery_policy: RecoveryPolicyState
    market_data_source: str
    account_read_source: str
    market_data_backend: str | None = None
    account_backend: str | None = None
    execution_backend: str | None = None
    ai_operating_mode: str | None = None
    execution_route: str
    exchange_submit_target: str
    exchange_submit_allowed: bool
    submit_blocked: bool = False
    submit_blocked_reasons: list[str] = Field(default_factory=list)
    execution_blocked: bool = False
    blocked_reason: str | None = None
    live_submit_enabled: bool
    guarded_execution_dry_run: bool
    okx_simulated_trading: bool = False
    trading_product_type: ProductType = "spot"
    margin_mode: MarginModelType = "cash"
    max_target_leverage: float = 1.0
    halted: bool
    profile_source: str = "env_only"
    active_profile_revision_id: str | None = None
    pending_profile_revision_id: str | None = None
    restart_required: bool = False


class RecoveryBundleLegStatus(SchemaBase):
    client_order_id: str
    exchange_order_id: str | None = None
    execution_chain_id: str | None = None
    execution_attempt_id: str | None = None
    symbol: str
    product_type: ProductType
    margin_mode: MarginModelType
    side: str
    pos_side: str | None = None
    leg_action: str | None = None
    status: str
    strategy_family: str | None = None
    strategy_sleeve_id: str | None = None
    strategy_leg_role: str | None = None
    strategy_execution_mode: str | None = None
    requested_qty: float | Decimal
    filled_qty: float | Decimal
    remaining_qty: float | Decimal
    submitted_ts: datetime | None = None
    last_update_ts: datetime | None = None


class RecoveryBundleSummary(SchemaBase):
    bundle_id: str
    allocation_id: str | None = None
    participating_families: list[str] = Field(default_factory=list)
    strategy_sleeve_refs: list[str] = Field(default_factory=list)
    symbol_scope: list[str] = Field(default_factory=list)
    product_types: list[str] = Field(default_factory=list)
    margin_modes: list[str] = Field(default_factory=list)
    open_order_count: int = 0
    total_order_count: int = 0
    terminal_order_count: int = 0
    active_obligation_count: int = 0
    recovery_state: str = "structured_open_orders"
    recoverable: bool = True
    reason_codes: list[str] = Field(default_factory=list)
    operator_summary: str | None = None
    legs: list[RecoveryBundleLegStatus] = Field(default_factory=list)


class IndependentRecoverySnapshot(SchemaBase):
    decision_id: str | None = None
    allocation_id: str | None = None
    symbol: str
    strategy_sleeve_id: str | None = None
    leg: Literal["long", "short"]
    book_state: str | None = None
    holding_phase: str | None = None
    health_state: str | None = None
    current_qty: Decimal = Decimal("0")
    target_qty: Decimal = Decimal("0")
    current_scale_in_count: int = 0
    current_de_risk_count: int = 0
    expected_chain_ids: list[str] = Field(default_factory=list)
    active_execution_chain_ids: list[str] = Field(default_factory=list)
    unresolved_attempt_ids: list[str] = Field(default_factory=list)
    recovery_posture: str = "idle"
    recovery_blockers: list[str] = Field(default_factory=list)
    last_recovery_incident_at: datetime | None = None
    recovery_version: str = "independent_phase6_additive_v1"
    suspended_until: datetime | None = None
    cooldown_until: datetime | None = None
    state_version: int = 1
    threshold_snapshot: dict[str, Any] | None = None
    health_snapshot: dict[str, Any] | None = None
    replay_snapshot: dict[str, Any] | None = None
    decision_snapshot: dict[str, Any] | None = None


class RecoveryStatus(SchemaBase):
    status: str
    recovery_source: str | None = None
    recovery_state: str = "normal_operation"
    recovered_order_count: int = 0
    recovered_fill_count: int = 0
    recovered_snapshot_available: bool = False
    rebuilt_snapshot_saved: bool = False
    recovered_reconciliation_available: bool = False
    latest_reconciliation_id: str | None = None
    latest_reconciliation_severity: str | None = None
    reconciliation_classification: str | None = None
    open_order_count: int = 0
    pending_command_count: int = 0
    stuck_sent_submit_order_count: int = 0
    divergence_count: int = 0
    safe_startup: bool = True
    safe_to_trade: bool = False
    resume_eligible: bool = False
    review_required: bool = False
    only_reduce_required: bool = False
    only_reduce_reasons: list[str] = Field(default_factory=list)
    unknown_state_details: list[dict[str, Any]] = Field(default_factory=list)
    rebaseline_available: bool = False
    halted: bool = False
    recovery_action: str | None = None
    bundle_recovery_required: bool = False
    bundle_recovery_count: int = 0
    recoverable_bundle_count: int = 0
    unbundled_open_order_count: int = 0
    bundle_summaries: list[RecoveryBundleSummary] = Field(default_factory=list)
    independent_recovery_snapshots: list[IndependentRecoverySnapshot] = Field(default_factory=list)
    baseline_imported: bool = False
    baseline_status: str | None = None
    baseline_imported_at: datetime | None = None
    baseline_event_ref: str | None = None
    baseline_source: str | None = None
    baseline_safe_for_automatic_continuation: bool = False
    baseline_requires_operator_review: bool = False
    baseline_balance_count: int = 0
    baseline_position_count: int = 0
    baseline_open_order_count: int = 0
    baseline_fill_count: int = 0
    last_rebaseline_at: datetime | None = None
    last_rebaseline_event_ref: str | None = None
    last_rebaseline_action_ref: str | None = None
    last_resume_action_ref: str | None = None
    last_resume_status: str | None = None
    last_resume_reason: str | None = None
    resume_blocked_reasons: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
