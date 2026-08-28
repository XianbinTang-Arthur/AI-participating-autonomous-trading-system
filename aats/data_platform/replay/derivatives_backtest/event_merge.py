"""Streaming deterministic merge and internal liquidation barriers."""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, TypeAlias

from aats.data_platform.governance.typed_json_identity import typed_json_sha256

from .contracts import DerivativesBacktestContractError
from .events import (
    DERIVATIVES_EVENT_ORDERING_POLICY_ID,
    DERIVATIVES_MAX_SOURCE_SEQUENCE,
    EVENT_PHASE_PRIORITY_V1,
    BarCloseEventV1,
    ContractTierEffectiveEventV1,
    DerivativeEventKindV1,
    DerivativeReplayEventV1,
    FundingSettlementEventV1,
    IndexPriceEventV1,
    MarkPriceEventV1,
    TradableEventV1,
    event_order_key,
    parse_derivative_replay_event,
)
from .snapshot_refs import DerivativesSnapshotRefsV1
from .wire import (
    canonical_utc_timestamp,
    require_exact_int,
    require_sha256,
    require_utc_datetime,
)


DERIVATIVES_ENGINE_BARRIER_SCHEMA = "derivatives-engine-barrier/v1"


_EVENT_CLASS_BY_KIND = {
    DerivativeEventKindV1.CONTRACT_TIER_EFFECTIVE: ContractTierEffectiveEventV1,
    DerivativeEventKindV1.INDEX_PRICE: IndexPriceEventV1,
    DerivativeEventKindV1.MARK_PRICE: MarkPriceEventV1,
    DerivativeEventKindV1.FUNDING_SETTLEMENT: FundingSettlementEventV1,
    DerivativeEventKindV1.TRADABLE: TradableEventV1,
    DerivativeEventKindV1.BAR_CLOSE: BarCloseEventV1,
}
_SINGLETON_EVENT_KINDS_PER_TIMESTAMP = frozenset(
    {
        DerivativeEventKindV1.CONTRACT_TIER_EFFECTIVE,
        DerivativeEventKindV1.FUNDING_SETTLEMENT,
        DerivativeEventKindV1.TRADABLE,
        DerivativeEventKindV1.BAR_CLOSE,
    }
)
_EVENT_KIND_BY_CLASS = {event_class: kind for kind, event_class in _EVENT_CLASS_BY_KIND.items()}


class EngineBarrierKindV1(StrEnum):
    PRE_FILL_LIQUIDATION = "pre_fill_liquidation"
    POST_FILL_LIQUIDATION = "post_fill_liquidation"


_BARRIER_PHASE = MappingProxyType({
    EngineBarrierKindV1.PRE_FILL_LIQUIDATION: 40,
    EngineBarrierKindV1.POST_FILL_LIQUIDATION: 55,
})


@dataclass(frozen=True, slots=True)
class DerivedLiquidationBarrierV1:
    kind: EngineBarrierKindV1
    ts: datetime
    trigger_source_key: tuple[datetime, int, int, str]
    snapshot_set_fingerprint: str
    barrier_id: str

    def __post_init__(self) -> None:
        if type(self.kind) is not EngineBarrierKindV1:
            raise DerivativesBacktestContractError("barrier_kind_invalid")
        require_utc_datetime(self.ts, "barrier_ts")
        if (
            type(self.trigger_source_key) is not tuple
            or len(self.trigger_source_key) != 4
            or type(self.trigger_source_key[0]) is not datetime
            or self.trigger_source_key[0] != self.ts
        ):
            raise DerivativesBacktestContractError("barrier_trigger_key_invalid")
        trigger_phase = require_exact_int(
            self.trigger_source_key[1],
            "barrier_trigger_phase",
            minimum=0,
            maximum=100,
        )
        if trigger_phase not in set(EVENT_PHASE_PRIORITY_V1.values()):
            raise DerivativesBacktestContractError("barrier_trigger_phase_invalid")
        require_exact_int(
            self.trigger_source_key[2],
            "barrier_trigger_source_sequence",
            minimum=0,
            maximum=DERIVATIVES_MAX_SOURCE_SEQUENCE,
        )
        if (
            self.kind is EngineBarrierKindV1.POST_FILL_LIQUIDATION
            and trigger_phase
            != EVENT_PHASE_PRIORITY_V1[DerivativeEventKindV1.TRADABLE]
        ):
            raise DerivativesBacktestContractError(
                "post_fill_barrier_trigger_phase_invalid"
            )
        require_sha256(self.trigger_source_key[3], "trigger_event_id")
        require_sha256(
            self.snapshot_set_fingerprint,
            "snapshot_set_fingerprint",
        )
        require_sha256(self.barrier_id, "barrier_id")
        if self.barrier_id != _derive_barrier_id(
            self.kind,
            self.ts,
            self.trigger_source_key,
            self.snapshot_set_fingerprint,
        ):
            raise DerivativesBacktestContractError("barrier_id_mismatch")

    @property
    def phase_priority(self) -> int:
        return _BARRIER_PHASE[self.kind]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DERIVATIVES_ENGINE_BARRIER_SCHEMA,
            "ordering_policy_id": DERIVATIVES_EVENT_ORDERING_POLICY_ID,
            "barrier_type": self.kind.value,
            "ts": canonical_utc_timestamp(self.ts, "barrier_ts"),
            "phase_priority": self.phase_priority,
            "trigger_source_key": _source_key_payload(self.trigger_source_key),
            "snapshot_set_fingerprint": self.snapshot_set_fingerprint,
            "barrier_id": self.barrier_id,
        }


DerivativeEngineStepV1: TypeAlias = (
    DerivativeReplayEventV1 | DerivedLiquidationBarrierV1
)


def _source_key_payload(key: tuple[datetime, int, int, str]) -> dict[str, Any]:
    return {
        "ts": canonical_utc_timestamp(key[0], "event_ts"),
        "phase_priority": key[1],
        "source_sequence": key[2],
        "event_id": key[3],
    }


def _derive_barrier_id(
    kind: EngineBarrierKindV1,
    ts: datetime,
    trigger_source_key: tuple[datetime, int, int, str],
    snapshot_set_fingerprint: str,
) -> str:
    require_sha256(snapshot_set_fingerprint, "snapshot_set_fingerprint")
    return typed_json_sha256(
        {
            "schema": DERIVATIVES_ENGINE_BARRIER_SCHEMA,
            "ordering_policy_id": DERIVATIVES_EVENT_ORDERING_POLICY_ID,
            "barrier_type": kind.value,
            "ts": canonical_utc_timestamp(ts, "barrier_ts"),
            "trigger_source_key": _source_key_payload(trigger_source_key),
            "snapshot_set_fingerprint": snapshot_set_fingerprint,
        }
    )


def _barrier(
    kind: EngineBarrierKindV1,
    event: DerivativeReplayEventV1,
    *,
    snapshot_set_fingerprint: str,
) -> DerivedLiquidationBarrierV1:
    key = event_order_key(event)
    return DerivedLiquidationBarrierV1(
        kind=kind,
        ts=event.header.ts,
        trigger_source_key=key,
        snapshot_set_fingerprint=snapshot_set_fingerprint,
        barrier_id=_derive_barrier_id(
            kind,
            event.header.ts,
            key,
            snapshot_set_fingerprint,
        ),
    )


def _validate_snapshot_transition(
    active: DerivativesSnapshotRefsV1,
    incoming: DerivativesSnapshotRefsV1,
    *,
    ts: datetime,
) -> None:
    if active.fingerprint == incoming.fingerprint:
        raise DerivativesBacktestContractError("snapshot_activation_noop")
    changed = False
    for previous, replacement in zip(
        (
            active.instrument,
            active.position_tier,
            active.execution_fee,
            active.funding_schedule,
        ),
        (
            incoming.instrument,
            incoming.position_tier,
            incoming.execution_fee,
            incoming.funding_schedule,
        ),
        strict=True,
    ):
        if previous.fingerprint == replacement.fingerprint:
            continue
        changed = True
        if previous.snapshot_id == replacement.snapshot_id:
            raise DerivativesBacktestContractError("snapshot_identity_conflict")
        if previous.effective_to != ts or replacement.effective_from != ts:
            raise DerivativesBacktestContractError(
                "snapshot_transition_window_invalid",
                field=replacement.kind.value,
            )
    if not changed:  # pragma: no cover - set fingerprint already proves this
        raise DerivativesBacktestContractError("snapshot_activation_noop")


def _revalidate_event(
    event: DerivativeReplayEventV1,
    *,
    expected_kind: DerivativeEventKindV1 | None = None,
) -> DerivativeReplayEventV1:
    event_kind = _EVENT_KIND_BY_CLASS.get(type(event))
    if event_kind is None or (
        expected_kind is not None and event_kind is not expected_kind
    ):
        raise DerivativesBacktestContractError("event_stream_type_mismatch")
    try:
        validated = parse_derivative_replay_event(event.to_dict())
    except DerivativesBacktestContractError:
        raise
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as exc:
        raise DerivativesBacktestContractError("event_revalidation_failed") from exc
    if type(validated) is not type(event) or validated != event:
        raise DerivativesBacktestContractError("event_revalidation_mismatch")
    return validated


def merge_derivative_event_streams(
    streams: Mapping[DerivativeEventKindV1, Iterable[DerivativeReplayEventV1]],
) -> Iterator[DerivativeReplayEventV1]:
    """Merge six already ordered streams without sorting or correcting a stream."""

    if type(streams) is not dict or set(streams) != set(DerivativeEventKindV1):
        raise DerivativesBacktestContractError("event_stream_set_invalid")
    if any(type(kind) is not DerivativeEventKindV1 for kind in streams):
        raise DerivativesBacktestContractError("event_stream_set_invalid")

    try:
        iterators: dict[DerivativeEventKindV1, Iterator[DerivativeReplayEventV1]] = {
            kind: iter(streams[kind]) for kind in DerivativeEventKindV1
        }
    except TypeError as exc:
        raise DerivativesBacktestContractError("event_stream_iterable_invalid") from exc
    last_local: dict[DerivativeEventKindV1, tuple[datetime, int]] = {}
    heap: list[
        tuple[
            tuple[datetime, int, int, str],
            str,
            DerivativeEventKindV1,
            DerivativeReplayEventV1,
        ]
    ] = []

    def read_next(kind: DerivativeEventKindV1) -> None:
        try:
            event = next(iterators[kind])
        except StopIteration:
            return
        if type(event) is not _EVENT_CLASS_BY_KIND[kind]:
            raise DerivativesBacktestContractError("event_stream_type_mismatch")
        event = _revalidate_event(event, expected_kind=kind)
        local_key = (event.header.ts, event.header.source_sequence)
        previous = last_local.get(kind)
        if previous is not None and local_key <= previous:
            raise DerivativesBacktestContractError(
                "event_stream_order_invalid",
                field=kind.value,
            )
        if (
            previous is not None
            and kind in _SINGLETON_EVENT_KINDS_PER_TIMESTAMP
            and local_key[0] == previous[0]
        ):
            raise DerivativesBacktestContractError(
                "event_timestamp_cardinality_invalid",
                field=kind.value,
            )
        last_local[kind] = local_key
        heapq.heappush(
            heap,
            (event_order_key(event), kind.value, kind, event),
        )

    for kind in DerivativeEventKindV1:
        read_next(kind)

    previous_global: tuple[datetime, int, int, str] | None = None
    while heap:
        key, _kind_name, kind, event = heapq.heappop(heap)
        if previous_global is not None and key <= previous_global:
            raise DerivativesBacktestContractError("event_global_order_invalid")
        previous_global = key
        yield event
        read_next(kind)


def expand_engine_barriers(
    events: Iterable[DerivativeReplayEventV1],
    *,
    opening_snapshot_refs: DerivativesSnapshotRefsV1,
) -> Iterator[DerivativeEngineStepV1]:
    """Insert phases 40/55 with one source-event look-ahead.

    The caller must fully drain this function during preflight before using a
    separately verified second pass for economic state changes.
    """

    if type(opening_snapshot_refs) is not DerivativesSnapshotRefsV1:
        raise DerivativesBacktestContractError("opening_snapshot_set_invalid")
    try:
        active_snapshot_refs = DerivativesSnapshotRefsV1.from_dict(
            opening_snapshot_refs.to_dict()
        )
    except DerivativesBacktestContractError:
        raise
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as exc:
        raise DerivativesBacktestContractError(
            "opening_snapshot_set_revalidation_failed"
        ) from exc
    if active_snapshot_refs != opening_snapshot_refs:
        raise DerivativesBacktestContractError(
            "opening_snapshot_set_revalidation_mismatch"
        )
    active_snapshot_set_fingerprint = active_snapshot_refs.fingerprint
    iterator = iter(events)
    try:
        current = _revalidate_event(next(iterator))
    except StopIteration:
        return

    previous_key: tuple[datetime, int, int, str] | None = None
    pre_emitted_for_ts = False
    tradable_seen_for_ts = False
    while True:
        current_key = event_order_key(current)
        if previous_key is not None and current_key <= previous_key:
            raise DerivativesBacktestContractError("event_global_order_invalid")
        try:
            following = _revalidate_event(next(iterator))
        except StopIteration:
            following = None

        following_key = None if following is None else event_order_key(following)
        if following_key is not None and following_key <= current_key:
            raise DerivativesBacktestContractError("event_global_order_invalid")
        if (
            following is not None
            and following.header.ts == current.header.ts
            and type(following) is type(current)
            and _EVENT_KIND_BY_CLASS[type(current)]
            in _SINGLETON_EVENT_KINDS_PER_TIMESTAMP
        ):
            if type(current) is TradableEventV1:
                raise DerivativesBacktestContractError(
                    "multiple_tradable_events_per_timestamp"
                )
            raise DerivativesBacktestContractError(
                "event_timestamp_cardinality_invalid",
                field=_EVENT_KIND_BY_CLASS[type(current)].value,
            )

        if type(current) is ContractTierEffectiveEventV1:
            _validate_snapshot_transition(
                active_snapshot_refs,
                current.snapshot_refs,
                ts=current.header.ts,
            )
            current.snapshot_refs.validate_at(current.header.ts)
            active_snapshot_refs = DerivativesSnapshotRefsV1.from_dict(
                current.snapshot_refs.to_dict()
            )
            active_snapshot_set_fingerprint = active_snapshot_refs.fingerprint
        else:
            active_snapshot_refs.validate_at(current.header.ts)
        if type(current) is FundingSettlementEventV1:
            current.schedule_ref.validate_at(current.header.ts)
            if (
                current.schedule_ref.fingerprint
                != active_snapshot_refs.funding_schedule.fingerprint
            ):
                raise DerivativesBacktestContractError(
                    "funding_event_active_schedule_mismatch"
                )

        output_steps: list[DerivativeEngineStepV1] = []
        phase = current_key[1]
        if phase >= 50 and not pre_emitted_for_ts:
            output_steps.append(
                _barrier(
                    EngineBarrierKindV1.PRE_FILL_LIQUIDATION,
                    current,
                    snapshot_set_fingerprint=active_snapshot_set_fingerprint,
                )
            )
            pre_emitted_for_ts = True

        output_steps.append(current)
        if isinstance(current, TradableEventV1):
            if tradable_seen_for_ts:
                raise DerivativesBacktestContractError(
                    "multiple_tradable_events_per_timestamp"
            )
            tradable_seen_for_ts = True
            output_steps.append(
                _barrier(
                    EngineBarrierKindV1.POST_FILL_LIQUIDATION,
                    current,
                    snapshot_set_fingerprint=active_snapshot_set_fingerprint,
                )
            )

        leaving_timestamp = (
            following is None or following.header.ts != current.header.ts
        )
        if leaving_timestamp and not pre_emitted_for_ts:
            output_steps.append(
                _barrier(
                    EngineBarrierKindV1.PRE_FILL_LIQUIDATION,
                    current,
                    snapshot_set_fingerprint=active_snapshot_set_fingerprint,
                )
            )
            pre_emitted_for_ts = True

        previous_key = current_key
        finished = following is None
        if not finished and leaving_timestamp:
            pre_emitted_for_ts = False
            tradable_seen_for_ts = False
        if following is not None:
            current = following

        # All internal state and derived barriers are frozen before the first
        # externally visible yield, so caller mutation while paused cannot
        # change later barriers or the active snapshot cursor.
        yield from output_steps
        if finished:
            break


def engine_step_order_key(
    step: DerivativeEngineStepV1,
) -> tuple[datetime, int, int, str]:
    if type(step) is DerivedLiquidationBarrierV1:
        validated = DerivedLiquidationBarrierV1(
            kind=step.kind,
            ts=step.ts,
            trigger_source_key=step.trigger_source_key,
            snapshot_set_fingerprint=step.snapshot_set_fingerprint,
            barrier_id=step.barrier_id,
        )
        if validated != step:
            raise DerivativesBacktestContractError(
                "barrier_revalidation_mismatch"
            )
        return (
            validated.ts,
            validated.phase_priority,
            validated.trigger_source_key[2],
            validated.barrier_id,
        )
    return event_order_key(_revalidate_event(step))


__all__ = [
    "DERIVATIVES_ENGINE_BARRIER_SCHEMA",
    "DerivedLiquidationBarrierV1",
    "DerivativeEngineStepV1",
    "EngineBarrierKindV1",
    "engine_step_order_key",
    "expand_engine_barriers",
    "merge_derivative_event_streams",
]
