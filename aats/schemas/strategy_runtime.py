from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field

from aats.schemas.common import SchemaBase, new_id, utc_now
from aats.schemas.execution import LegOrderAction, PositionMode, PositionSide
from aats.schemas.system import MarginModelType, ProductType


StrategyFamily = Literal[
    "directional",
    "smart_arbitrage",
    "spot_grid",
    "dca",
    "protective",
    "opportunistic",
    "independent",
]
StrategyFamilyAction = Literal[
    "hold_family",
    "blocked",
    "protect",
    "rebalance_protection",
    "close_protection_leg",
    "open_opportunity_leg",
    "close_opportunity_leg",
    "open_independent_book",
    "scale_independent_book",
    "rebalance_independent_books",
    "de_risk_independent_book",
    "close_failed_thesis_independent_book",
    "close_stale_thesis_independent_book",
    "close_independent_book",
]
StrategyCandidateState = Literal[
    "ready",
    "inactive",
    "disabled",
    "incompatible",
    "advisory_only",
    "candidate",
    "blocked",
    "opening",
    "active",
    "rebalancing",
    "unwinding",
    "recovery",
]
StrategyRouteAction = Literal["override_target", "hold_current", "advisory_only", "protective_fallback"]
StrategySleeveExecutionControlMode = Literal[
    "approved",
    "permission_denied",
    "budget_zero_suppressed",
    "protective_override",
]
StrategySleeveExecutionBehavior = Literal[
    "execute_target",
    "hold_current",
    "advisory_only",
    "suppressed_after_approval",
    "protective_execute",
]
StrategyExecutionBundleStatus = Literal[
    "blocked",
    "planned",
    "submitted",
    "partial_fill_recovery",
    "review_required",
    "recovered",
]
StrategySleeveStatus = Literal["active", "inactive", "paused", "retired"]
StrategyInventoryPolicy = Literal["account_net_inventory", "paired_inventory", "inventory_accumulation"]
StrategySleeveAutomationState = Literal["active", "contracted", "paused", "protective_only", "disabled"]
AllocatorHedgePriorityClass = Literal["standard", "inventory", "hedge", "critical_hedge"]


class StrategySleeveAutomationDecision(SchemaBase):
    family: StrategyFamily
    strategy_sleeve_id: str
    automatic_enabled: bool = True
    runtime_supported: bool = True
    approved_for_execution: bool = True
    permission_mode: str = "approved"
    execution_control_mode: StrategySleeveExecutionControlMode | None = None
    execution_behavior: StrategySleeveExecutionBehavior | None = None
    automation_state: StrategySleeveAutomationState = "active"
    budget_multiplier: Decimal = Decimal("1")
    effective_scale: Decimal = Decimal("1")
    allocator_weight: Decimal = Decimal("1")
    recent_net_pnl: Decimal = Decimal("0")
    current_inventory_notional: Decimal = Decimal("0")
    requested_delta_position_qty: Decimal = Decimal("0")
    composed_delta_position_qty: Decimal = Decimal("0")
    composed_route_action: StrategyRouteAction = "hold_current"
    protective_intent: bool = False
    budget_zero_suppressed: bool = False
    reason_codes: list[str] = Field(default_factory=list)
    permission_reason_codes: list[str] = Field(default_factory=list)
    budget_reason_codes: list[str] = Field(default_factory=list)
    composition_reason_codes: list[str] = Field(default_factory=list)
    scale_trace: list[str] = Field(default_factory=list)
    operator_summary: str | None = None


class SleeveBudgetProfile(SchemaBase):
    budget_profile_id: str = Field(default_factory=lambda: new_id("budget"))
    family: StrategyFamily
    product_type: ProductType
    margin_mode: MarginModelType
    symbol_scope: tuple[str, ...] = Field(default_factory=tuple)
    quote_budget_limit: Decimal | None = None
    margin_budget_limit: Decimal | None = None
    notional_cap: Decimal | None = None
    max_symbol_notional: Decimal | None = None
    max_drawdown_usdt: Decimal | None = None
    allocator_base_weight: Decimal = Decimal("1")
    hedge_priority_class: AllocatorHedgePriorityClass = "standard"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SleeveBudgetAssignment(SchemaBase):
    assignment_id: str = Field(default_factory=lambda: new_id("budgetassign"))
    budget_profile_id: str
    strategy_sleeve_id: str
    family: StrategyFamily
    symbol: str
    product_type: ProductType
    margin_mode: MarginModelType
    active_budget_multiplier: Decimal = Decimal("1")
    allocator_base_weight: Decimal = Decimal("1")
    effective_quote_budget_limit: Decimal | None = None
    effective_margin_budget_limit: Decimal | None = None
    effective_notional_cap: Decimal | None = None
    effective_max_symbol_notional: Decimal | None = None
    hedge_priority_class: AllocatorHedgePriorityClass = "standard"
    reason_codes: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


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
    family_action: StrategyFamilyAction = "hold_family"
    headline: str | None = None
    selectable: bool = False
    execution_compatible: bool = False
    current_position_qty: Decimal = Decimal("0")
    target_position_qty: Decimal = Decimal("0")
    delta_position_qty: Decimal = Decimal("0")
    account_current_position_qty: Decimal | None = None
    account_target_position_qty: Decimal | None = None
    target_notional: Decimal | None = None
    requested_target_position_qty: Decimal | None = None
    requested_delta_position_qty: Decimal = Decimal("0")
    priority_score: float = 0.0
    reason_codes: list[str] = Field(default_factory=list)
    automatic_enabled: bool = True
    approved_for_execution: bool = True
    permission_mode: str = "approved"
    execution_control_mode: StrategySleeveExecutionControlMode | None = None
    execution_behavior: StrategySleeveExecutionBehavior | None = None
    budget_zero_suppressed: bool = False
    budget_multiplier: Decimal = Decimal("1")
    allocator_weight: Decimal = Decimal("1")
    control_reason_codes: list[str] = Field(default_factory=list)
    control_summary: str | None = None
    control_trace: dict[str, Any] = Field(default_factory=dict)
    pair_id: str | None = None
    opportunity_kind: str | None = None
    execution_mode: str | None = None
    state_phase: str | None = None
    blocking_reasons: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    legs: list["StrategyLegIntent"] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class AllocatorBudgetSnapshot(SchemaBase):
    budget_snapshot_id: str = Field(default_factory=lambda: new_id("budgetsnap"))
    allocation_id: str
    strategy_sleeve_id: str
    family: StrategyFamily
    symbol: str
    product_type: ProductType
    margin_mode: MarginModelType
    requested_notional: Decimal = Decimal("0")
    approved_notional: Decimal = Decimal("0")
    requested_delta_qty: Decimal = Decimal("0")
    approved_delta_qty: Decimal = Decimal("0")
    budget_multiplier: Decimal = Decimal("1")
    allocator_weight: Decimal = Decimal("1")
    quote_budget_limit: Decimal | None = None
    margin_budget_limit: Decimal | None = None
    notional_cap: Decimal | None = None
    max_symbol_notional: Decimal | None = None
    hedge_priority_class: AllocatorHedgePriorityClass = "standard"
    priority_rank: int = 0
    portfolio_requested_notional: Decimal = Decimal("0")
    portfolio_approved_notional: Decimal = Decimal("0")
    portfolio_budget_cut_notional: Decimal = Decimal("0")
    clamped: bool = False
    reason_codes: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class AllocatorConflictResolution(SchemaBase):
    conflict_resolution_id: str = Field(default_factory=lambda: new_id("conflict"))
    allocation_id: str
    symbol: str
    product_type: ProductType
    margin_mode: MarginModelType
    conflict_type: str
    resolution_action: str
    input_sleeve_ids: list[str] = Field(default_factory=list)
    approved_sleeve_ids: list[str] = Field(default_factory=list)
    gross_requested_qty: Decimal = Decimal("0")
    net_approved_qty: Decimal = Decimal("0")
    blocked_qty: Decimal = Decimal("0")
    protected_notional: Decimal = Decimal("0")
    reduced_notional: Decimal = Decimal("0")
    reason_codes: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class AllocatorNettingDecision(SchemaBase):
    netting_decision_id: str = Field(default_factory=lambda: new_id("netting"))
    allocation_id: str
    symbol: str
    product_type: ProductType
    margin_mode: MarginModelType
    gross_buy_qty: Decimal = Decimal("0")
    gross_sell_qty: Decimal = Decimal("0")
    net_approved_qty: Decimal = Decimal("0")
    participating_sleeve_ids: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class PortfolioAllocationDecision(SchemaBase):
    allocation_id: str = Field(default_factory=lambda: new_id("alloc"))
    decision_id: str
    symbol: str
    product_type: ProductType
    margin_mode: MarginModelType
    allocator_version: str = "task74_allocator_v2_phase2"
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
    approved_notional_by_sleeve: dict[str, Decimal] = Field(default_factory=dict)
    portfolio_requested_notional: Decimal = Decimal("0")
    portfolio_approved_notional: Decimal = Decimal("0")
    portfolio_budget_cut_notional: Decimal = Decimal("0")
    budget_cut_reason_codes: list[str] = Field(default_factory=list)
    budget_snapshot_ids: list[str] = Field(default_factory=list)
    expected_edge_bps: Decimal | None = None
    expected_cost_bps: Decimal | None = None
    budget_assignments: list[SleeveBudgetAssignment] = Field(default_factory=list)
    budget_snapshots: list[AllocatorBudgetSnapshot] = Field(default_factory=list)
    conflict_resolutions: list[AllocatorConflictResolution] = Field(default_factory=list)
    netting_decisions: list[AllocatorNettingDecision] = Field(default_factory=list)
    hedge_protected_notional: Decimal = Decimal("0")
    directional_reduced_notional: Decimal = Decimal("0")
    portfolio_risk_budget_state: str | None = None
    sleeve_intents: list[StrategySleeveIntent] = Field(default_factory=list)
    execution_legs: list["StrategyLegIntent"] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class StrategyLegIntent(SchemaBase):
    symbol: str
    execution_chain_id: str | None = None
    product_type: ProductType
    side: Literal["buy", "sell"]
    position_mode: PositionMode | None = None
    pos_side: PositionSide | None = None
    action: LegOrderAction | None = None
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
    pair_id: str | None = None
    opportunity_kind: str | None = None
    execution_mode: str | None = None
    state_phase: str | None = None
    overlay_mode: Literal["protective", "opportunistic", "independent"] | None = None
    hedge_ratio: Decimal | None = None
    trigger_reason_codes: list[str] = Field(default_factory=list)
    note: str | None = None
    execution_style_preference: str | None = None
    order_type_preference: Literal["market", "limit"] | None = None
    time_in_force_preference: str | None = None
    limit_offset_bps_preference: Decimal | None = None
    execution_preference_reason_codes: list[str] = Field(default_factory=list)
    book_action: str | None = None
    close_reason: str | None = None
    policy_reason: str | None = None
    execution_policy_urgency: Literal["low", "medium", "high"] | None = None
    expected_leg_cost_bps: float | None = None
    expected_net_edge_bps: float | None = None
    liquidity_quality_score: float | None = None
    execution_health_state: str | None = None
    max_acceptable_cost_bps: float | None = None


class StrategyBookExpectancyEntry(SchemaBase):
    leg: Literal["long", "short"]
    expected_gross_edge_bps: float = 0.0
    expected_signal_edge_bps: float = 0.0
    expected_slippage_bps: float = 0.0
    expected_cost_bps: float = 0.0
    expected_net_edge_bps: float = 0.0
    required_safe_net_edge_bps: float | None = None
    max_acceptable_cost_bps: float | None = None
    weak_edge_execution_mode: str | None = None
    weak_edge_report_only: bool = False
    passive_first_required: bool = False
    book_action: str | None = None
    close_reason: str | None = None
    policy_reason: str | None = None
    execution_policy_urgency: Literal["low", "medium", "high"] | None = None
    execution_style_preference: str | None = None
    order_type_preference: Literal["market", "limit"] | None = None
    time_in_force_preference: str | None = None
    limit_offset_bps_preference: Decimal | None = None
    expected_leg_cost_bps: float | None = None
    liquidity_quality_score: float | None = None
    execution_health_state: str | None = None
    score_raw: float | None = None
    score_adjusted: float | None = None
    size_multiplier: float | None = None
    capital_multiplier: float | None = None
    health_state: str | None = None
    book_state: str | None = None
    guard_state: str | None = None
    holding_phase: str | None = None
    edge_strength: Literal["weak", "medium", "strong"] | None = None


class StrategyBookExpectancySummary(SchemaBase):
    source: str = "independent_book"
    books: list[StrategyBookExpectancyEntry] = Field(default_factory=list)


class StrategyAdaptiveThresholdSnapshot(SchemaBase):
    leg: Literal["long", "short"]
    shadow_only: bool = True
    rollout_enabled: bool = False
    live_applied: bool = False
    health_enforcement_enabled: bool = False
    size_down_entry_enabled: bool = False
    long_short_asymmetry_enabled: bool = False
    entry_threshold: float | None = None
    close_threshold: float | None = None
    scale_in_threshold: float | None = None
    thesis_age_seconds: float | None = None
    de_risk_net_edge_bps: float | None = None
    score_drawdown_bps: float | None = None
    adaptive_entry_threshold: float | None = None
    adaptive_close_threshold: float | None = None
    adaptive_scale_in_threshold: float | None = None
    adaptive_thesis_age_seconds: float | None = None
    adaptive_de_risk_net_edge_bps: float | None = None
    effective_entry_threshold: float | None = None
    effective_close_threshold: float | None = None
    effective_scale_in_threshold: float | None = None
    effective_thesis_age_seconds: float | None = None
    effective_de_risk_net_edge_bps: float | None = None
    effective_score_drawdown_bps: float | None = None
    capital_multiplier: float | None = None
    confidence_multiplier: float | None = None
    volatility_multiplier: float | None = None
    liquidity_multiplier: float | None = None
    health_multiplier: float | None = None
    direction_bias_multiplier: float | None = None
    reason_codes: list[str] = Field(default_factory=list)


class StrategyIndependentLegHealthSummary(SchemaBase):
    leg: Literal["long", "short"]
    health_state: str | None = None
    halt_openings: bool = False
    only_reduce: bool = False
    suspended: bool = False
    warnings: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


class StrategyBookRuntimeState(SchemaBase):
    leg: Literal["long", "short"]
    execution_chain_id: str | None = None
    current_qty: Decimal = Decimal("0")
    target_qty: Decimal = Decimal("0")
    state: str = "inactive"
    score: float | None = None
    score_raw: float | None = None
    score_adjusted: float | None = None
    size_multiplier: float | None = None
    capital_multiplier: float | None = None
    book_state: str | None = None
    guard_state: str | None = None
    holding_phase: str | None = None
    health_state: str | None = None
    eligibility_state: str | None = None
    book_action: str | None = None
    close_reason: str | None = None
    policy_reason: str | None = None
    thesis_started_at: datetime | None = None
    thesis_age_seconds: float | None = None
    current_scale_in_count: int = 0
    current_de_risk_count: int = 0
    prior_book_state: str | None = None
    prior_guard_state: str | None = None
    last_transition_at: datetime | None = None
    last_transition_reason: str | None = None
    suspended_until: datetime | None = None
    state_version: int = 2
    expected_signal_edge_bps: float | None = None
    expected_cost_bps: float | None = None
    expected_net_edge_bps: float | None = None
    liquidity_quality_score: float | None = None
    execution_health_state: str | None = None
    cooldown_until: datetime | None = None
    min_hold_remaining_seconds: float | None = None
    rebalance_cooldown_remaining_seconds: float | None = None
    execution_policy_urgency: Literal["low", "medium", "high"] | None = None
    edge_strength: Literal["weak", "medium", "strong"] | None = None
    threshold_snapshot: StrategyAdaptiveThresholdSnapshot | None = None
    leg_health_summary: StrategyIndependentLegHealthSummary | None = None
    reason_codes: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    transition_valid: bool = True
    transition_violation_reason: str | None = None


class StrategyExpectedVsRealizedBookDiagnostics(SchemaBase):
    leg: Literal["long", "short"]
    sample_count: int = 0
    entry_count: int = 0
    scale_in_count: int = 0
    close_count: int = 0
    de_risk_count: int = 0
    avg_expected_net_edge_bps: float | None = None
    avg_realized_net_bps: float | None = None


class StrategyExecutionAttemptDiagnostics(SchemaBase):
    attempt_count: int = 0
    matched_attempt_count: int = 0
    unmatched_attempt_count: int = 0
    filled_attempt_count: int = 0
    multi_attempt_chain_count: int = 0
    avg_attempts_per_chain: float | None = None
    avg_realized_net_bps_per_attempt: float | None = None
    avg_realized_slippage_bps_per_attempt: float | None = None
    truth_source: str = "fill_outcomes_grouped_by_execution_attempt"


class StrategyExpectedVsRealizedSummary(SchemaBase):
    family: StrategyFamily = "independent"
    sample_count: int = 0
    expected_sample_count: int = 0
    realized_sample_count: int = 0
    overlap_sample_count: int = 0
    entry_count: int = 0
    scale_in_count: int = 0
    close_count: int = 0
    de_risk_count: int = 0
    weak_edge_entry_count: int = 0
    avg_expected_net_edge_bps: float | None = None
    avg_realized_gross_bps: float | None = None
    avg_realized_fee_bps: float | None = None
    avg_realized_slippage_bps: float | None = None
    avg_realized_net_bps: float | None = None
    fee_drag_ratio: float | None = None
    churn_ratio: float | None = None
    passive_first_usage_ratio: float | None = None
    expected_realized_net_gap_bps: float | None = None
    expected_realized_correlation: float | None = None
    close_reason_distribution: list[dict[str, Any]] = Field(default_factory=list)
    book_breakdown: list[StrategyExpectedVsRealizedBookDiagnostics] = Field(default_factory=list)
    attempt_diagnostics: StrategyExecutionAttemptDiagnostics | None = None
    emitted_metric_flags: dict[str, bool] = Field(default_factory=dict)
    truth_source: str = "position_targets_plus_fill_outcomes"


class StrategyCandidate(SchemaBase):
    family: StrategyFamily
    state: StrategyCandidateState
    enabled: bool = False
    selectable: bool = False
    execution_compatible: bool = False
    route_action: StrategyRouteAction = "hold_current"
    family_action: StrategyFamilyAction = "hold_family"
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
    pair_id: str | None = None
    opportunity_kind: str | None = None
    execution_mode: str | None = None
    state_phase: str | None = None
    blocking_reasons: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    book_expectancy_summary: StrategyBookExpectancySummary | None = None
    book_runtime_states: list[StrategyBookRuntimeState] = Field(default_factory=list)
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
    selected_family_action: StrategyFamilyAction = "hold_family"
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
    bundle_type: Literal["single_sleeve", "multi_sleeve", "hedge_protected"] = "single_sleeve"
    bundle_priority: str = "standard"
    status: StrategyExecutionBundleStatus = "planned"
    selected_symbol: str
    operator_summary: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    gross_requested_exposure: Decimal = Decimal("0")
    net_approved_exposure: Decimal = Decimal("0")
    expected_cost_bps: Decimal | None = None
    expected_edge_bps: Decimal | None = None
    budget_snapshot_ids: list[str] = Field(default_factory=list)
    allocation_snapshot_ref: str | None = None
    portfolio_risk_budget_state: str | None = None
    hedge_protected_notional: Decimal = Decimal("0")
    directional_reduced_notional: Decimal = Decimal("0")
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
