from __future__ import annotations

from pydantic import Field

from aats.schemas.common import SchemaBase


class DecisionAuditRecord(SchemaBase):
    decision_id: str
    decision_context_ref: str
    baseline_assessment_ref: str | None = None
    ai_decision_brief_ref: str | None = None
    ai_market_assessment_ref: str | None = None
    ai_action_proposal_ref: str | None = None
    ai_takeover_decision_ref: str | None = None
    ai_shadow_decision_refs: list[str] = Field(default_factory=list)
    ai_shadow_evaluation_refs: list[str] = Field(default_factory=list)
    position_target_ref: str | None = None
    policy_decision_ref: str | None = None
    risk_decision_ref: str | None = None
    execution_plan_ref: str | None = None
    order_intent_refs: list[str] = Field(default_factory=list)
    order_state_refs: list[str] = Field(default_factory=list)
    fill_event_refs: list[str] = Field(default_factory=list)
    portfolio_delta_ref: str | None = None
    reconciliation_refs: list[str] = Field(default_factory=list)
