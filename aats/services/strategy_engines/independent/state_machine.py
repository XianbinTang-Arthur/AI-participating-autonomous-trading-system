from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal

from .models import IndependentBookAction, IndependentBookDecision, IndependentExecutionHealthState, IndependentLeg

IndependentBookState = Literal[
    "flat",
    "probing",
    "building",
    "holding",
    "de_risking",
    "forced_exit",
    "cooldown",
    "suspended",
]
IndependentHoldingPhase = Literal["entry", "scale_in", "steady", "reduce", "exit"] | None


@dataclass(frozen=True, slots=True)
class IndependentBookStateSnapshot:
    leg: IndependentLeg
    current_qty: Decimal
    target_qty: Decimal
    legacy_state: str
    book_action: IndependentBookAction
    book_state: IndependentBookState | None = None
    holding_phase: IndependentHoldingPhase = None
    health_state: IndependentExecutionHealthState | None = None
    eligibility_state: str | None = None
    current_scale_in_count: int = 0
    current_de_risk_count: int = 0
    thesis_started_at: datetime | None = None
    thesis_age_seconds: float | None = None
    last_transition_at: datetime | None = None
    last_transition_reason: str | None = None
    suspended_until: datetime | None = None
    cooldown_until: datetime | None = None
    state_version: int = 1
    close_reason: str | None = None
    blocked_reasons: tuple[str, ...] = ()
    execution_health_state: IndependentExecutionHealthState | None = None


IndependentStateSnapshot = IndependentBookStateSnapshot


@dataclass(frozen=True, slots=True)
class IndependentStateTransition:
    prior_state: IndependentBookState
    next_state: IndependentBookState
    holding_phase: IndependentHoldingPhase
    transition_reason: str | None = None


def derive_book_state(*, snapshot: IndependentStateSnapshot) -> IndependentBookState:
    current_qty = max(to_decimal(snapshot.current_qty), Decimal("0"))
    target_qty = max(to_decimal(snapshot.target_qty), Decimal("0"))
    if snapshot.book_action in {"close_failed_thesis", "close_stale_thesis"}:
        return "forced_exit"
    if snapshot.book_action == "de_risk":
        return "de_risking"
    if snapshot.book_action == "scale_in":
        return "building"
    if snapshot.book_action == "open":
        return "probing" if current_qty <= EPSILON_DECIMAL_12 else "building"
    if snapshot.book_action == "blocked":
        if any("trial_guard" in reason for reason in snapshot.blocked_reasons):
            return "suspended"
        if current_qty > EPSILON_DECIMAL_12:
            return "holding"
        return "cooldown"
    if current_qty <= EPSILON_DECIMAL_12 and target_qty <= EPSILON_DECIMAL_12:
        return "flat"
    return "holding"


def derive_holding_phase(*, snapshot: IndependentStateSnapshot, book_state: IndependentBookState) -> IndependentHoldingPhase:
    if book_state == "probing":
        return "entry"
    if book_state == "building":
        return "scale_in" if snapshot.book_action == "scale_in" else "entry"
    if book_state == "holding":
        return "steady"
    if book_state == "de_risking":
        return "reduce"
    if book_state == "forced_exit":
        return "exit"
    return None


def transition_book_state(
    *,
    prior_state: IndependentBookState,
    snapshot: IndependentStateSnapshot,
) -> IndependentStateTransition:
    next_state = derive_book_state(snapshot=snapshot)
    holding_phase = derive_holding_phase(snapshot=snapshot, book_state=next_state)
    transition_reason = snapshot.close_reason or snapshot.book_action
    return IndependentStateTransition(
        prior_state=prior_state,
        next_state=next_state,
        holding_phase=holding_phase,
        transition_reason=transition_reason,
    )


def snapshot_from_decision(*, decision: IndependentBookDecision) -> IndependentStateSnapshot:
    scale_in_count = 1 if decision.book_action == "scale_in" else 0
    de_risk_count = 1 if decision.book_action == "de_risk" else 0
    state_version = 1 + scale_in_count + de_risk_count
    return IndependentStateSnapshot(
        leg=decision.leg,
        current_qty=decision.current_qty,
        target_qty=decision.target_qty,
        legacy_state=decision.state,
        book_action=decision.book_action,
        book_state=decision.book_state,  # type: ignore[arg-type]
        holding_phase=decision.holding_phase,  # type: ignore[arg-type]
        health_state=decision.health_state,  # type: ignore[arg-type]
        eligibility_state=(
            None
            if decision.eligibility is None
            else "eligible"
            if decision.eligibility.eligible
            else "blocked"
        ),
        current_scale_in_count=scale_in_count,
        current_de_risk_count=de_risk_count,
        thesis_age_seconds=decision.thesis_age_seconds,
        last_transition_reason=decision.close_reason or decision.book_action,
        state_version=state_version,
        close_reason=decision.close_reason,
        blocked_reasons=tuple(decision.blocked_reasons),
        execution_health_state=decision.execution_health_state,
    )
