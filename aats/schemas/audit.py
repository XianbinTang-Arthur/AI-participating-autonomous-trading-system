from __future__ import annotations

from pydantic import Field

from aats.schemas.common import SchemaBase


class DecisionAuditRecord(SchemaBase):
    decision_id: str
    selected_strategy_sleeve_id: str | None = None
    allocation_id: str | None = None
    decision_context_ref: str
    strategy_coordinator_snapshot_ref: str | None = None
    strategy_sleeve_intent_refs: list[str] = Field(default_factory=list)
    portfolio_allocation_decision_ref: str | None = None
    baseline_assessment_ref: str | None = None
    ai_decision_brief_ref: str | None = None
    ai_market_assessment_ref: str | None = None
    ai_action_proposal_ref: str | None = None
    ai_shadow_decision_refs: list[str] = Field(default_factory=list)
    ai_shadow_evaluation_refs: list[str] = Field(default_factory=list)
    position_target_ref: str | None = None
    decision_outcome_ref: str | None = None
    policy_decision_ref: str | None = None
    risk_decision_ref: str | None = None
    execution_plan_ref: str | None = None
    execution_plan_refs: list[str] = Field(default_factory=list)
    strategy_execution_bundle_ref: str | None = None
    order_intent_refs: list[str] = Field(default_factory=list)
    order_state_refs: list[str] = Field(default_factory=list)
    fill_event_refs: list[str] = Field(default_factory=list)
    portfolio_delta_ref: str | None = None
    portfolio_delta_refs: list[str] = Field(default_factory=list)
    reconciliation_refs: list[str] = Field(default_factory=list)
