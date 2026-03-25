from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from aats.schemas.common import SchemaBase, new_id, utc_now


BlockerCategory = Literal["system_execution", "submission_mode", "ai_decision", "profile_control", "external"]
BlockerResolutionMode = Literal["manual_only", "auto_only", "manual_or_auto", "external_only"]
BlockerActionKind = Literal["api", "client", "external"]
BlockerActionTone = Literal["primary", "secondary", "warning", "danger", "ghost"]
BlockerLifecycleState = Literal["open", "acknowledged", "in_progress", "resolved", "auto_cleared", "superseded"]
BlockerTaskKind = Literal["resolve_blocker", "review_reconciliation", "resume", "observe", "refresh_state", "healthy"]


class BlockerActionDefinition(SchemaBase):
    action_id: str
    label: str
    kind: BlockerActionKind = "api"
    tone: BlockerActionTone = "secondary"
    endpoint: str | None = None
    method: Literal["GET", "POST", "CLIENT"] = "POST"
    client_action: str | None = None
    value: str | None = None
    enabled: bool = True
    disabled_reason: str | None = None
    requires_confirmation: bool = False
    confirmation_title: str | None = None
    confirmation_copy: str | None = None
    expected_effect: str | None = None


class BlockerControlItem(SchemaBase):
    blocker_instance_id: str = Field(default_factory=lambda: new_id("blk"))
    blocker: str
    category: BlockerCategory
    subsystem: str
    priority: int
    lifecycle_state: BlockerLifecycleState = "open"
    resolution_mode: BlockerResolutionMode = "manual_only"
    title: str
    description: str
    impact: str
    recommended_next_step: str
    root_cause: bool = False
    derived_from: list[str] = Field(default_factory=list)
    affects_execution: bool = True
    submit_only: bool = False
    actions: list[BlockerActionDefinition] = Field(default_factory=list)


class BlockerControlTask(SchemaBase):
    kind: BlockerTaskKind
    title: str
    summary: str
    reason: str
    completion_outcome: str
    source_blocker: str | None = None
    secondary_blocker_count: int = 0
    actions: list[BlockerActionDefinition] = Field(default_factory=list)


class BlockerControlSnapshot(SchemaBase):
    panel_version: str
    generated_at: datetime = Field(default_factory=utc_now)
    halted: bool
    review_required: bool
    resume_eligible: bool
    safe_to_trade: bool
    primary_blocker: BlockerControlItem | None = None
    secondary_blockers: list[BlockerControlItem] = Field(default_factory=list)
    blockers: list[BlockerControlItem] = Field(default_factory=list)
    primary_task: BlockerControlTask
    next_step_summary: str


class BlockerActionExecutionRequest(SchemaBase):
    panel_version: str | None = None
    blocker: str | None = None
    reason: str | None = None


class BlockerActionExecutionResult(SchemaBase):
    action_id: str
    status: str
    message: str
    blocker_control: BlockerControlSnapshot
