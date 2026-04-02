from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from aats.schemas.common import utc_now
from aats.schemas.strategy_runtime import StrategyBookRuntimeState
from aats.services.portfolio_service.decimals import to_decimal

from .state_machine import (
    IndependentStateSnapshot,
    derive_book_state,
    derive_guard_state,
    transition_book_state,
)

_LIFECYCLE_BOOK_STATES = frozenset({"flat", "probing", "building", "holding", "de_risking", "forced_exit"})


def normalize_independent_runtime_state(
    *,
    runtime_state: StrategyBookRuntimeState | None,
    as_of_ts: datetime | None = None,
) -> StrategyBookRuntimeState | None:
    if runtime_state is None:
        return None
    effective_as_of_ts = _effective_as_of_ts(as_of_ts=as_of_ts)
    normalized_book_state = _normalized_book_state(runtime_state=runtime_state)
    normalized_guard_state = derive_guard_state(
        snapshot=_runtime_state_machine_snapshot(runtime_state=runtime_state),
        as_of_ts=effective_as_of_ts,
    )
    normalized_prior_book_state, normalized_prior_guard_state = _normalized_prior_states(
        runtime_state=runtime_state,
        as_of_ts=effective_as_of_ts,
    )
    return runtime_state.model_copy(
        update={
            "book_state": normalized_book_state,
            "guard_state": normalized_guard_state,
            "prior_book_state": normalized_prior_book_state,
            "prior_guard_state": normalized_prior_guard_state,
        }
    )


def normalize_independent_runtime_state_payload(
    *,
    runtime_state: Mapping[str, Any] | StrategyBookRuntimeState | None,
    as_of_ts: datetime | None = None,
) -> dict[str, Any] | None:
    normalized = normalize_independent_runtime_state(
        runtime_state=_coerce_runtime_state(runtime_state),
        as_of_ts=as_of_ts,
    )
    return None if normalized is None else normalized.model_dump(mode="json")


def normalize_independent_runtime_state_payloads(
    *,
    runtime_states: Sequence[Mapping[str, Any] | StrategyBookRuntimeState] | None,
    as_of_ts: datetime | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(runtime_states, Sequence):
        return []
    effective_as_of_ts = _effective_as_of_ts(as_of_ts=as_of_ts)
    normalized: list[dict[str, Any]] = []
    for item in runtime_states:
        payload = normalize_independent_runtime_state_payload(
            runtime_state=item,
            as_of_ts=effective_as_of_ts,
        )
        if payload is not None:
            normalized.append(payload)
    return normalized


def normalize_independent_replay_snapshot_payload(
    *,
    replay_snapshot: Mapping[str, Any] | None,
    runtime_state: Mapping[str, Any] | StrategyBookRuntimeState | None = None,
    as_of_ts: datetime | None = None,
) -> dict[str, Any] | None:
    if not isinstance(replay_snapshot, Mapping):
        return None
    normalized = dict(replay_snapshot)
    effective_as_of_ts = _effective_as_of_ts(as_of_ts=as_of_ts)
    normalized_runtime_state = normalize_independent_runtime_state(
        runtime_state=_coerce_runtime_state(runtime_state),
        as_of_ts=effective_as_of_ts,
    )
    if normalized_runtime_state is None:
        return normalized
    normalized["book_state"] = normalized_runtime_state.book_state
    normalized["guard_state"] = normalized_runtime_state.guard_state
    if normalized_runtime_state.prior_book_state is not None or "prior_book_state" in normalized:
        normalized["prior_book_state"] = (
            normalized_runtime_state.prior_book_state
            if normalized_runtime_state.prior_book_state is not None
            else normalized.get("prior_book_state")
        )
    if normalized_runtime_state.prior_guard_state is not None or "prior_guard_state" in normalized:
        normalized["prior_guard_state"] = normalized_runtime_state.prior_guard_state
    return normalized


def normalize_independent_family_execution_summary(
    *,
    family_execution_summary: Mapping[str, Any] | None,
    as_of_ts: datetime | None = None,
) -> dict[str, Any] | None:
    if not isinstance(family_execution_summary, Mapping):
        return None
    normalized = dict(family_execution_summary)
    effective_as_of_ts = _effective_as_of_ts(as_of_ts=as_of_ts)
    runtime_states = normalize_independent_runtime_state_payloads(
        runtime_states=_runtime_state_items(family_execution_summary.get("book_runtime_states")),
        as_of_ts=effective_as_of_ts,
    )
    if runtime_states or "book_runtime_states" in normalized:
        normalized["book_runtime_states"] = runtime_states
    by_leg = {
        str(item.get("leg") or "").strip().lower(): item
        for item in runtime_states
        if str(item.get("leg") or "").strip().lower() in {"long", "short"}
    }
    for leg in ("long", "short"):
        key = f"{leg}_replay_snapshot"
        if key not in normalized:
            continue
        replay_snapshot = normalize_independent_replay_snapshot_payload(
            replay_snapshot=normalized.get(key),
            runtime_state=by_leg.get(leg),
            as_of_ts=effective_as_of_ts,
        )
        if replay_snapshot is not None:
            normalized[key] = replay_snapshot
    return normalized


def normalize_independent_payload(
    *,
    payload: Mapping[str, Any] | None,
    as_of_ts: datetime | None = None,
) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    normalized = dict(payload)
    effective_as_of_ts = _effective_as_of_ts(as_of_ts=as_of_ts)
    runtime_states = normalize_independent_runtime_state_payloads(
        runtime_states=_runtime_state_items(payload.get("book_runtime_states")),
        as_of_ts=effective_as_of_ts,
    )
    if runtime_states or "book_runtime_states" in normalized:
        normalized["book_runtime_states"] = runtime_states
    family_execution_summary = normalize_independent_family_execution_summary(
        family_execution_summary=payload.get("family_execution_summary"),
        as_of_ts=effective_as_of_ts,
    )
    if family_execution_summary is not None:
        normalized["family_execution_summary"] = family_execution_summary
    nested_outcome = payload.get("decision_outcome")
    if isinstance(nested_outcome, Mapping):
        normalized["decision_outcome"] = normalize_independent_payload(
            payload=nested_outcome,
            as_of_ts=effective_as_of_ts,
        )
    return normalized


def _effective_as_of_ts(*, as_of_ts: datetime | None) -> datetime:
    return utc_now() if as_of_ts is None else as_of_ts


def _coerce_runtime_state(
    runtime_state: Mapping[str, Any] | StrategyBookRuntimeState | None,
) -> StrategyBookRuntimeState | None:
    if runtime_state is None:
        return None
    if isinstance(runtime_state, StrategyBookRuntimeState):
        return runtime_state
    if not isinstance(runtime_state, Mapping):
        return None
    try:
        return StrategyBookRuntimeState.model_validate(dict(runtime_state))
    except Exception:
        return None


def _runtime_state_items(
    runtime_states: Any,
) -> Sequence[Mapping[str, Any] | StrategyBookRuntimeState]:
    if isinstance(runtime_states, Sequence) and not isinstance(runtime_states, (str, bytes, bytearray)):
        return runtime_states
    return ()


def _normalized_book_state(*, runtime_state: StrategyBookRuntimeState) -> str | None:
    explicit_book_state = str(runtime_state.book_state or "").strip()
    if explicit_book_state in _LIFECYCLE_BOOK_STATES and not str(runtime_state.book_action or "").strip():
        return explicit_book_state
    return derive_book_state(snapshot=_runtime_state_machine_snapshot(runtime_state=runtime_state))


def _normalized_prior_states(
    *,
    runtime_state: StrategyBookRuntimeState,
    as_of_ts: datetime | None,
) -> tuple[str | None, str | None]:
    prior_book_state = str(runtime_state.prior_book_state or "").strip() or None
    prior_guard_state = str(runtime_state.prior_guard_state or "").strip() or None
    if prior_book_state is None:
        return None, prior_guard_state
    transition = transition_book_state(
        prior_state=prior_book_state,
        prior_guard_state=prior_guard_state,
        snapshot=_runtime_state_machine_snapshot(runtime_state=runtime_state),
        as_of_ts=as_of_ts,
    )
    return transition.prior_state, transition.prior_guard_state


def _runtime_state_machine_snapshot(*, runtime_state: StrategyBookRuntimeState) -> IndependentStateSnapshot:
    return IndependentStateSnapshot(
        leg=runtime_state.leg,
        current_qty=to_decimal(runtime_state.current_qty),
        target_qty=to_decimal(runtime_state.target_qty),
        legacy_state=runtime_state.state,
        book_action=_runtime_book_action(runtime_state=runtime_state),
        book_state=None,
        guard_state=None,
        holding_phase=runtime_state.holding_phase,
        health_state=runtime_state.health_state,
        eligibility_state=runtime_state.eligibility_state,
        current_scale_in_count=int(runtime_state.current_scale_in_count or 0),
        current_de_risk_count=int(runtime_state.current_de_risk_count or 0),
        prior_book_state=None,
        prior_guard_state=None,
        thesis_started_at=runtime_state.thesis_started_at,
        thesis_age_seconds=runtime_state.thesis_age_seconds,
        last_transition_at=runtime_state.last_transition_at,
        last_transition_reason=runtime_state.last_transition_reason,
        suspended_until=runtime_state.suspended_until,
        cooldown_until=runtime_state.cooldown_until,
        state_version=max(int(runtime_state.state_version or 1), 1),
        close_reason=runtime_state.close_reason,
        blocked_reasons=tuple(
            str(item)
            for item in runtime_state.blocked_reasons
            if str(item or "").strip()
        ),
        execution_health_state=runtime_state.execution_health_state,
        transition_valid=bool(runtime_state.transition_valid),
        transition_violation_reason=runtime_state.transition_violation_reason,
    )


def _runtime_book_action(*, runtime_state: StrategyBookRuntimeState) -> str:
    value = str(runtime_state.book_action or "").strip()
    return value or "inactive"
