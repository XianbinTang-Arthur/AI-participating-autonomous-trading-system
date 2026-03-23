from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from aats.schemas.common import SchemaBase


class PolicyDecision(SchemaBase):
    decision_id: str
    mode: str
    allowed: bool
    execution_allowed: bool = False
    submission_allowed: bool = False
    dry_run_only: bool = False
    requires_human_approval: bool
    allowed_symbols: list[str]
    allowed_execution_styles: list[str]
    max_notional_override: Decimal | None = None
    forced_degrade_mode: str | None = None
    rejection_reasons: list[str] = Field(default_factory=list)


class RiskDecision(SchemaBase):
    decision_id: str
    approved: bool
    modified: bool
    capped_target_position_qty: Decimal
    capped_target_notional: Decimal | None = None
    required_initial_margin: Decimal | None = None
    projected_margin_usage: Decimal | None = None
    projected_notional: Decimal | None = None
    current_open_order_count: int = 0
    risk_budget_multiplier: Decimal = Decimal("1")
    risk_budget_state: dict[str, object] = Field(default_factory=dict)
    execution_aggressiveness_multiplier: Decimal = Decimal("1")
    execution_aggressiveness_state: dict[str, object] = Field(default_factory=dict)
    constraints_applied: list[str] = Field(default_factory=list)
    risk_score: float
    flatten_required: bool = False
    halt_required: bool = False
    only_reduce_required: bool = False
    risk_limit_breached: bool = False
    liquidation_buffer_remaining: Decimal | None = None
    rejection_reasons: list[str] = Field(default_factory=list)
