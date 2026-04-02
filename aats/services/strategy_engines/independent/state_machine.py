from __future__ import annotations

from dataclasses import dataclass, replace
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
]
IndependentGuardState = Literal["cooldown", "suspended"]
IndependentHoldingPhase = Literal["entry", "scale_in", "steady", "reduce", "exit"] | None


@dataclass(frozen=True, slots=True)
class IndependentBookStateSnapshot:
    leg: IndependentLeg
    current_qty: Decimal
    target_qty: Decimal
    legacy_state: str
    book_action: IndependentBookAction
    book_state: IndependentBookState | None = None
    guard_state: IndependentGuardState | None = None
    holding_phase: IndependentHoldingPhase = None
    health_state: IndependentExecutionHealthState | None = None
    eligibility_state: str | None = None
    current_scale_in_count: int = 0
    current_de_risk_count: int = 0
    prior_book_state: IndependentBookState | None = None
    prior_guard_state: IndependentGuardState | None = None
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
    transition_valid: bool = True
    transition_violation_reason: str | None = None


IndependentStateSnapshot = IndependentBookStateSnapshot


@dataclass(frozen=True, slots=True)
class IndependentStateTransition:
    prior_state: IndependentBookState
    next_state: IndependentBookState
    prior_guard_state: IndependentGuardState | None
    next_guard_state: IndependentGuardState | None
    holding_phase: IndependentHoldingPhase
    transition_reason: str | None = None
    valid_transition: bool = True
    violation_reason: str | None = None


_GUARD_STATES = frozenset({"cooldown", "suspended"})
_GUARD_BLOCKED_NEXT_STATES = frozenset({"probing", "building"})
_ALLOWED_TRANSITIONS: dict[IndependentBookState, frozenset[IndependentBookState]] = {
    "flat": frozenset({"flat", "probing"}),
    "probing": frozenset({"probing", "building", "holding", "flat", "forced_exit"}),
    "building": frozenset({"building", "holding", "flat", "forced_exit"}),
    "holding": frozenset({"holding", "building", "de_risking", "forced_exit", "flat"}),
    "de_risking": frozenset({"de_risking", "holding", "flat", "forced_exit"}),
    "forced_exit": frozenset({"forced_exit", "flat"}),
}


def _has_suspension_blocker(*, snapshot: IndependentStateSnapshot) -> bool:
    return any("trial_guard" in reason for reason in snapshot.blocked_reasons)


def _has_cooldown_blocker(*, snapshot: IndependentStateSnapshot) -> bool:
    return any("cooldown_active" in reason for reason in snapshot.blocked_reasons)


def _has_effective_suspension(
    *,
    snapshot: IndependentStateSnapshot,
    as_of_ts: datetime | None = None,
) -> bool:
    if _has_suspension_blocker(snapshot=snapshot):
        return True
    if snapshot.suspended_until is None:
        return False
    if as_of_ts is None:
        return True
    return snapshot.suspended_until > as_of_ts


def _has_effective_cooldown(
    *,
    snapshot: IndependentStateSnapshot,
    as_of_ts: datetime | None = None,
) -> bool:
    if _has_cooldown_blocker(snapshot=snapshot):
        return True
    if snapshot.cooldown_until is None:
        return False
    if as_of_ts is None:
        return True
    return snapshot.cooldown_until > as_of_ts


def _inventory_backed_prior_state(*, snapshot: IndependentStateSnapshot) -> IndependentBookState:
    current_qty = max(to_decimal(snapshot.current_qty), Decimal("0"))
    return "holding" if current_qty > EPSILON_DECIMAL_12 else "flat"


def _normalized_prior_book_state(
    *,
    prior_state: IndependentBookState | IndependentGuardState,
    snapshot: IndependentStateSnapshot,
) -> IndependentBookState:
    if prior_state in _GUARD_STATES:
        return _inventory_backed_prior_state(snapshot=snapshot)
    return prior_state


def _legacy_prior_guard_state(
    *,
    prior_state: IndependentBookState | IndependentGuardState,
) -> IndependentGuardState | None:
    if prior_state in _GUARD_STATES:
        return prior_state
    return None


def _normalized_prior_guard_state(
    *,
    prior_guard_state: IndependentGuardState | None,
    snapshot: IndependentStateSnapshot,
    as_of_ts: datetime | None = None,
) -> IndependentGuardState | None:
    if prior_guard_state == "cooldown":
        return "cooldown" if _has_effective_cooldown(snapshot=snapshot, as_of_ts=as_of_ts) else None
    if prior_guard_state == "suspended":
        return "suspended" if _has_effective_suspension(snapshot=snapshot, as_of_ts=as_of_ts) else None
    return None


def derive_guard_state(
    *,
    snapshot: IndependentStateSnapshot,
    as_of_ts: datetime | None = None,
) -> IndependentGuardState | None:
    if _has_effective_suspension(snapshot=snapshot, as_of_ts=as_of_ts):
        return "suspended"
    if _has_effective_cooldown(snapshot=snapshot, as_of_ts=as_of_ts):
        return "cooldown"
    return None


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
    prior_state: IndependentBookState | IndependentGuardState,
    snapshot: IndependentStateSnapshot,
    prior_guard_state: IndependentGuardState | None = None,
    as_of_ts: datetime | None = None,
) -> IndependentStateTransition:
    effective_prior_state = _normalized_prior_book_state(
        prior_state=prior_state,
        snapshot=snapshot,
    )
    effective_prior_guard_state = _normalized_prior_guard_state(
        prior_guard_state=(
            prior_guard_state
            if prior_guard_state is not None
            else _legacy_prior_guard_state(prior_state=prior_state)
        ),
        snapshot=snapshot,
        as_of_ts=as_of_ts,
    )
    next_state = derive_book_state(snapshot=snapshot)
    next_guard_state = derive_guard_state(snapshot=snapshot, as_of_ts=as_of_ts)
    holding_phase = derive_holding_phase(snapshot=snapshot, book_state=next_state)
    transition_reason = snapshot.close_reason or snapshot.book_action
    allowed_next_states = _ALLOWED_TRANSITIONS.get(effective_prior_state, frozenset())
    valid_transition = next_state in allowed_next_states
    violation_reason = None
    if not valid_transition:
        violation_reason = f"independent_transition_invalid:{effective_prior_state}->{next_state}"
    elif effective_prior_guard_state is not None and next_state in _GUARD_BLOCKED_NEXT_STATES:
        valid_transition = False
        violation_reason = f"independent_transition_invalid:{effective_prior_guard_state}->{next_state}"
    return IndependentStateTransition(
        prior_state=effective_prior_state,
        next_state=next_state,
        prior_guard_state=effective_prior_guard_state,
        next_guard_state=next_guard_state,
        holding_phase=holding_phase,
        transition_reason=transition_reason,
        valid_transition=valid_transition,
        violation_reason=violation_reason,
    )


def snapshot_from_decision(*, decision: IndependentBookDecision) -> IndependentStateSnapshot:
    existing_snapshot = decision.state_snapshot
    return IndependentStateSnapshot(
        leg=decision.leg,
        current_qty=decision.current_qty,
        target_qty=decision.target_qty,
        legacy_state=decision.state,
        book_action=decision.book_action,
        book_state=decision.book_state,  # type: ignore[arg-type]
        guard_state=decision.guard_state,  # type: ignore[arg-type]
        holding_phase=decision.holding_phase,  # type: ignore[arg-type]
        health_state=decision.health_state,  # type: ignore[arg-type]
        eligibility_state=(
            None
            if decision.eligibility is None
            else "eligible"
            if decision.eligibility.eligible
            else "blocked"
        ),
        current_scale_in_count=(
            decision.current_scale_in_count
            if existing_snapshot is None
            else int(existing_snapshot.current_scale_in_count)
        ),
        current_de_risk_count=(
            decision.current_de_risk_count
            if existing_snapshot is None
            else int(existing_snapshot.current_de_risk_count)
        ),
        prior_book_state=(
            decision.prior_book_state
            if existing_snapshot is None
            else existing_snapshot.prior_book_state
        ),
        prior_guard_state=(
            decision.prior_guard_state
            if existing_snapshot is None
            else existing_snapshot.prior_guard_state
        ),
        thesis_age_seconds=decision.thesis_age_seconds,
        last_transition_at=(
            decision.last_transition_at
            if existing_snapshot is None
            else existing_snapshot.last_transition_at
        ),
        last_transition_reason=(
            decision.last_transition_reason or decision.close_reason or decision.book_action
            if existing_snapshot is None
            else existing_snapshot.last_transition_reason
        ),
        suspended_until=(
            decision.suspended_until
            if existing_snapshot is None
            else existing_snapshot.suspended_until
        ),
        cooldown_until=(
            decision.cooldown_until
            if existing_snapshot is None
            else existing_snapshot.cooldown_until
        ),
        state_version=(
            max(int(decision.state_version or 1), 1)
            if existing_snapshot is None
            else max(int(existing_snapshot.state_version or 1), 1)
        ),
        close_reason=decision.close_reason,
        blocked_reasons=tuple(decision.blocked_reasons),
        execution_health_state=decision.execution_health_state,
        transition_valid=(
            True
            if existing_snapshot is None
            else bool(existing_snapshot.transition_valid)
        ),
        transition_violation_reason=(
            None
            if existing_snapshot is None
            else existing_snapshot.transition_violation_reason
        ),
    )


def advance_state_snapshot(
    *,
    decision: IndependentBookDecision,
    as_of_ts: datetime,
    book_state: IndependentBookState,
    holding_phase: IndependentHoldingPhase,
) -> IndependentStateSnapshot:
    seed_snapshot = snapshot_from_decision(decision=decision)
    prior_book_state = (
        decision.prior_book_state
        if decision.prior_book_state is not None
        else seed_snapshot.prior_book_state
    )
    prior_guard_state = (
        decision.prior_guard_state
        if decision.prior_guard_state is not None
        else seed_snapshot.prior_guard_state
    )
    derived_guard_state = derive_guard_state(
        snapshot=replace(
            seed_snapshot,
            book_state=book_state,
            holding_phase=holding_phase,
        ),
        as_of_ts=as_of_ts,
    )
    transition = (
        None
        if prior_book_state is None
        else transition_book_state(
            prior_state=prior_book_state,
            prior_guard_state=prior_guard_state,
            snapshot=replace(
                seed_snapshot,
                book_state=book_state,
                guard_state=derived_guard_state,
                holding_phase=holding_phase,
            ),
            as_of_ts=as_of_ts,
        )
    )
    transition_changed = (
        prior_book_state is not None
        and prior_book_state != book_state
    ) or (
        prior_guard_state != derived_guard_state
    ) or decision.book_action in {
        "open",
        "scale_in",
        "de_risk",
        "close_failed_thesis",
        "close_stale_thesis",
        "blocked",
    }
    next_scale_in_count = int(seed_snapshot.current_scale_in_count or 0) + (
        1 if decision.book_action == "scale_in" else 0
    )
    next_de_risk_count = int(seed_snapshot.current_de_risk_count or 0) + (
        1 if decision.book_action == "de_risk" else 0
    )
    next_state_version = max(int(seed_snapshot.state_version or 1), 1) + (1 if transition_changed else 0)
    next_transition_reason = (
        None
        if transition is None
        else transition.transition_reason
    ) or (
        decision.close_reason
        or decision.book_action
        if transition_changed
        else seed_snapshot.last_transition_reason
    )
    return replace(
        seed_snapshot,
        book_state=book_state,
        guard_state=derived_guard_state,
        holding_phase=holding_phase,
        prior_book_state=prior_book_state,
        prior_guard_state=prior_guard_state,
        current_scale_in_count=next_scale_in_count,
        current_de_risk_count=next_de_risk_count,
        last_transition_at=(as_of_ts if transition_changed else seed_snapshot.last_transition_at),
        last_transition_reason=next_transition_reason,
        state_version=next_state_version,
        transition_valid=True if transition is None else transition.valid_transition,
        transition_violation_reason=None if transition is None else transition.violation_reason,
    )
