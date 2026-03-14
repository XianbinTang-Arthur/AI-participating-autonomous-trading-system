from __future__ import annotations

from pydantic import Field

from aats.schemas.common import SchemaBase


class PolicyDecision(SchemaBase):
    decision_id: str
    mode: str
    allowed: bool
    requires_human_approval: bool
    allowed_symbols: list[str]
    allowed_execution_styles: list[str]
    max_notional_override: float | None = None
    forced_degrade_mode: str | None = None
    rejection_reasons: list[str] = Field(default_factory=list)


class RiskDecision(SchemaBase):
    decision_id: str
    approved: bool
    modified: bool
    capped_target_position_qty: float
    constraints_applied: list[str] = Field(default_factory=list)
    risk_score: float
    flatten_required: bool = False
    halt_required: bool = False
    rejection_reasons: list[str] = Field(default_factory=list)

