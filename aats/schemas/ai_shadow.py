from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from aats.schemas.common import SchemaBase, new_id, utc_now


ShadowActionType = Literal[
    "same_as_baseline",
    "hold_instead",
    "entry_override",
    "exit_override",
    "reverse_override",
]


class AITakeoverDecision(SchemaBase):
    takeover_id: str = Field(default_factory=lambda: new_id("ai_takeover"))
    decision_id: str
    symbol: str
    timeframe: str
    ai_takeover_allowed: bool = False
    ai_takeover_applied: bool = False
    ai_takeover_blockers: list[str] = Field(default_factory=list)
    baseline_direction: str
    ai_direction: str
    final_direction: str
    created_at: datetime = Field(default_factory=utc_now)


class AIShadowDecision(SchemaBase):
    shadow_decision_id: str = Field(default_factory=lambda: new_id("ai_shadow"))
    decision_id: str
    symbol: str
    timeframe: str

    baseline_target_qty: float
    baseline_action: str

    ai_shadow_target_qty: float
    ai_shadow_action: str

    would_override_baseline: bool
    shadow_action_type: ShadowActionType
    reason_codes: list[str] = Field(default_factory=list)

    ai_assessment_ref: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class AIShadowEvaluation(SchemaBase):
    evaluation_id: str = Field(default_factory=lambda: new_id("ai_shadow_eval"))
    window_start: datetime
    window_end: datetime

    symbol: str
    timeframe: str
    decision_ids: list[str] = Field(default_factory=list)

    baseline_trade_count: int
    shadow_trade_count: int
    override_count: int
    agreement_count: int
    disagreement_count: int
    fallback_count: int

    baseline_gross_pnl: float | None = None
    baseline_net_pnl: float | None = None
    baseline_fee_total: float | None = None
    baseline_fee_ratio: float | None = None
    baseline_churn_ratio: float | None = None

    shadow_gross_pnl: float | None = None
    shadow_net_pnl: float | None = None
    shadow_fee_total: float | None = None
    shadow_fee_ratio: float | None = None
    shadow_churn_ratio: float | None = None

    shadow_outperformed: bool | None = None
    summary: dict = Field(default_factory=dict)


class AIDegradationEvent(SchemaBase):
    degradation_id: str = Field(default_factory=lambda: new_id("ai_degradation"))
    symbol: str
    timeframe: str
    product_type: str
    margin_mode: str
    allowed_symbols: tuple[str, ...] = Field(default_factory=tuple)

    configured_operating_mode: str
    effective_operating_mode: str
    degraded: bool = True
    auto_downgrade_active: bool = False

    reason_code: str
    consecutive_failures: int
    consecutive_successes: int
    recovery_probe_after: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
