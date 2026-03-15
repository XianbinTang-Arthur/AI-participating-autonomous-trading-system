from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from aats.schemas.common import SchemaBase


HealthStatus = Literal["ok", "warn", "blocked"]
OperatingState = Literal[
    "local_demo",
    "real_market_paper",
    "guarded_simulated_submit_dry_run",
    "guarded_simulated_submit_enabled",
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


class RuntimeModeState(SchemaBase):
    mode: str
    config_profile: str
    operating_state: OperatingState
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
    halted: bool


class RecoveryStatus(SchemaBase):
    status: str
    recovery_state: str = "normal_operation"
    recovered_order_count: int = 0
    recovered_fill_count: int = 0
    recovered_snapshot_available: bool = False
    rebuilt_snapshot_saved: bool = False
    recovered_reconciliation_available: bool = False
    latest_reconciliation_id: str | None = None
    latest_reconciliation_severity: str | None = None
    open_order_count: int = 0
    divergence_count: int = 0
    safe_startup: bool = True
    safe_to_trade: bool = False
    resume_eligible: bool = False
    review_required: bool = False
    rebaseline_available: bool = False
    halted: bool = False
    recovery_action: str | None = None
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
