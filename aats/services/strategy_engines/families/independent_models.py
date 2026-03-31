from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True, slots=True)
class IndependentBookRuntimeState:
    side: Literal["long", "short"]
    current_qty: Decimal
    target_qty: Decimal
    state: str
    execution_chain_id: str | None = None
    thesis_started_at: datetime | None = None
    thesis_age_seconds: float | None = None
    last_transition_at: datetime | None = None
    last_transition_reason: str | None = None
    expected_signal_edge_bps: float | None = None
    expected_cost_bps: float | None = None
    expected_net_edge_bps: float | None = None
    liquidity_quality_score: float | None = None
    execution_health_state: str | None = None
    cooldown_until: datetime | None = None
    min_hold_remaining_seconds: float | None = None
    rebalance_cooldown_remaining_seconds: float | None = None
    score: float | None = None
    reason_codes: tuple[str, ...] = field(default_factory=tuple)
    blocked_reasons: tuple[str, ...] = field(default_factory=tuple)
    book_action: str | None = None
    close_reason: str | None = None
    policy_reason: str | None = None
    execution_policy_urgency: Literal["low", "medium", "high"] | None = None
    edge_strength: Literal["weak", "medium", "strong"] | None = None
