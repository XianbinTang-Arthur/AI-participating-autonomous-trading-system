from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from aats.schemas.common import SchemaBase


HealthStatus = Literal["ok", "warn", "blocked"]
OperatingState = Literal[
    "local_demo",
    "real_market_paper",
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
    operating_state: OperatingState
    live_submit_enabled: bool
    guarded_execution_dry_run: bool
    halted: bool
