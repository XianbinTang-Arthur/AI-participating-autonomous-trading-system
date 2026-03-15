from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from aats.schemas.common import SchemaBase, new_id
from aats.schemas.system import OperatingState


RuntimeState = Literal["healthy", "degraded", "blocked", "halted"]
OperatorRole = Literal["anonymous", "read", "write"]


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


class ReplayValidationSummary(SchemaBase):
    validation_id: str = Field(default_factory=lambda: new_id("replay"))
    validated_at: datetime
    decision_id: str | None = None
    replayed_event_count: int
    stored_snapshot_count: int
    divergence_count: int
    portfolio_issues: list[str] = Field(default_factory=list)
    decision_chain_issues: list[str] = Field(default_factory=list)
    execution_chain_issues: list[str] = Field(default_factory=list)
    audit_issues: list[str] = Field(default_factory=list)
    healthy: bool


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


class OperatorActionRecord(SchemaBase):
    action_id: str = Field(default_factory=lambda: new_id("opact"))
    action: Literal["halt", "resume", "mode_change", "reconciliation_validate"]
    actor_role: OperatorRole
    reason: str
    status: str
    decision_id: str | None = None
    order_id: str | None = None
