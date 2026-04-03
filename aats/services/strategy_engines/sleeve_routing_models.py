from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from aats.schemas.strategy_runtime import (
    StrategyLegIntent,
    StrategySleeveExecutionBehavior,
    StrategyRouteAction,
    StrategySleeveExecutionControlMode,
)


@dataclass(frozen=True)
class RawSleeveCandidateInputs:
    family: str
    strategy_sleeve_id: str
    symbol: str
    current_position_qty: Decimal
    target_position_qty: Decimal
    delta_position_qty: Decimal
    account_current_position_qty: Decimal | None
    target_notional: Decimal | None
    route_action: StrategyRouteAction
    requested_legs: tuple[StrategyLegIntent, ...]
    metrics: dict[str, Any]
    candidate_state: str
    candidate_enabled: bool
    candidate_selectable: bool
    candidate_execution_compatible: bool
    candidate_score: float
    candidate_confidence: float
    state_runtime_supported: bool
    active_inventory: bool
    current_inventory_notional: Decimal
    protective_intent: bool


@dataclass(frozen=True)
class ExecutionPermissionDecision:
    configured_auto_execution_enabled: bool
    state_runtime_supported: bool
    candidate_enabled: bool
    candidate_execution_compatible: bool
    protective_intent: bool
    approved_for_execution: bool
    blocks_non_protective_execution: bool
    permission_mode: str
    reason_codes: tuple[str, ...]
    human_summary: str | None = None

    @property
    def denied(self) -> bool:
        return not self.approved_for_execution

    @property
    def is_protective_override(self) -> bool:
        return self.permission_mode == "protective_override"


@dataclass(frozen=True)
class BudgetControlDecision:
    requested_delta_position_qty: Decimal
    requested_target_position_qty: Decimal | None
    base_scale: Decimal
    effective_scale: Decimal
    pnl_contraction_multiplier: Decimal
    reconciliation_contraction_multiplier: Decimal
    capacity_contraction_multiplier: Decimal
    custom_contraction_multiplier: Decimal
    scaled_delta_position_qty: Decimal
    scaled_target_position_qty: Decimal | None
    scaled_legs: tuple[StrategyLegIntent, ...]
    contraction_reason_codes: tuple[str, ...]
    scale_trace: tuple[str, ...]
    budget_zero_suppressed: bool = False

    @property
    def has_any_contraction(self) -> bool:
        return self.effective_scale < Decimal("1")


@dataclass(frozen=True)
class ComposedSleeveRoutingDecision:
    route_action: StrategyRouteAction
    approved_for_execution: bool
    execution_permission_mode: str
    execution_control_mode: StrategySleeveExecutionControlMode
    execution_behavior: StrategySleeveExecutionBehavior
    requested_delta_position_qty: Decimal
    composed_delta_position_qty: Decimal
    requested_target_position_qty: Decimal | None
    composed_target_position_qty: Decimal | None
    requested_legs: tuple[StrategyLegIntent, ...]
    composed_legs: tuple[StrategyLegIntent, ...]
    permission_reason_codes: tuple[str, ...]
    budget_reason_codes: tuple[str, ...]
    composition_reason_codes: tuple[str, ...]
    budget_zero_suppressed: bool = False

    @property
    def advisory_only(self) -> bool:
        return self.route_action == "advisory_only"
