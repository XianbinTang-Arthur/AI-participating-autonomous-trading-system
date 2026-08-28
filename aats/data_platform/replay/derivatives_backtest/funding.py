"""Fail-closed funding-schedule continuity for derivatives replay v1.

The snapshot loader proves a stable filesystem read.  This module deliberately
does not treat a private marker or a frozen in-memory object as an authorization
boundary: every consumed funding artifact is re-bound to its public raw bytes,
immutable reference, and derived schedule before a settlement timeline is
accepted.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from aats.data_platform.governance.research_artifact_contract import (
    decode_strict_json_artifact,
)
from aats.data_platform.governance.typed_json_identity import (
    canonical_typed_json_bytes,
    typed_json_sha256,
)

from .contracts import (
    DERIVATIVES_BACKTEST_SYMBOL,
    DerivativesBacktestContractError,
    FundingRateScheduleV1,
    parse_canonical_accounting_decimal,
)
from .events import FundingSettlementEventV1, parse_derivative_replay_event
from .snapshot_loader import (
    DERIVATIVES_SNAPSHOT_AUTHORITY_SYNTHETIC,
    DERIVATIVES_SNAPSHOT_ENVELOPE_SCHEMA,
    LoadedDerivativesSnapshotSetV1,
    LoadedSnapshotArtifactV1,
    ResolvedFundingScheduleV1,
)
from .snapshot_refs import (
    DerivativesSnapshotRefsV1,
    ImmutableSnapshotRefV1,
    SnapshotKindV1,
    validate_snapshot_transition,
)
from .wire import (
    require_canonical_utc_timestamp,
    require_canonical_uuid,
    require_exact_int,
    require_exact_mapping_keys,
    require_sha256,
    require_utc_datetime,
)


FUNDING_SCHEDULE_PAYLOAD_SCHEMA_V1 = "derivatives-funding-schedule-snapshot-payload/v1"
MAX_FUNDING_SCHEDULE_SEGMENTS_V1 = 100_000
MAX_EXPECTED_FUNDING_SETTLEMENTS_V1 = 1_000_000

_ENVELOPE_KEYS = frozenset(
    {
        "schema",
        "kind",
        "payload_schema",
        "venue",
        "symbol",
        "instrument_type",
        "contract_type",
        "settle_currency",
        "margin_mode",
        "position_mode",
        "snapshot_id",
        "source_registry_id",
        "source_seal_fingerprint",
        "source_schema",
        "effective_window",
        "authority_status",
        "payload",
    }
)
_FUNDING_PAYLOAD_KEYS = frozenset(
    {
        "minimum_rate_inclusive",
        "maximum_rate_inclusive",
        "schedule_id",
        "cadence_seconds",
        "settlement_anchor_ts",
    }
)


@dataclass(frozen=True, slots=True)
class FundingScheduleSegmentV1:
    """One clipped, half-open schedule segment within a replay window."""

    start_ts: datetime
    end_ts: datetime
    schedule_ref: ImmutableSnapshotRefV1
    schedule: FundingRateScheduleV1
    cadence: timedelta
    settlement_anchor_ts: datetime

    def __post_init__(self) -> None:
        start = require_utc_datetime(self.start_ts, "funding_segment_start_ts")
        end = require_utc_datetime(self.end_ts, "funding_segment_end_ts")
        if end <= start:
            raise DerivativesBacktestContractError(
                "funding_schedule_segment_window_invalid"
            )
        if (
            type(self.schedule_ref) is not ImmutableSnapshotRefV1
            or self.schedule_ref.kind is not SnapshotKindV1.FUNDING_SCHEDULE
        ):
            raise DerivativesBacktestContractError(
                "funding_schedule_segment_ref_invalid"
            )
        if type(self.schedule) is not FundingRateScheduleV1:
            raise DerivativesBacktestContractError(
                "funding_schedule_segment_contract_invalid"
            )
        try:
            validated_ref = ImmutableSnapshotRefV1.from_dict(
                self.schedule_ref.to_dict()
            )
        except DerivativesBacktestContractError:
            raise
        except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as exc:
            raise DerivativesBacktestContractError(
                "funding_schedule_segment_ref_revalidation_failed"
            ) from exc
        validated_schedule = FundingRateScheduleV1(
            minimum_rate_inclusive=self.schedule.minimum_rate_inclusive,
            maximum_rate_inclusive=self.schedule.maximum_rate_inclusive,
        )
        if (
            type(self.cadence) is not timedelta
            or self.cadence < timedelta(minutes=1)
            or self.cadence > timedelta(days=7)
            or self.cadence.microseconds != 0
        ):
            raise DerivativesBacktestContractError(
                "funding_schedule_segment_cadence_invalid"
            )
        require_utc_datetime(
            self.settlement_anchor_ts,
            "funding_segment_settlement_anchor_ts",
        )
        if validated_ref.effective_from > start or (
            validated_ref.effective_to is not None
            and end > validated_ref.effective_to
        ):
            raise DerivativesBacktestContractError(
                "funding_schedule_segment_outside_ref_window"
            )
        object.__setattr__(self, "schedule_ref", validated_ref)
        object.__setattr__(self, "schedule", validated_schedule)

    def contains(self, ts: datetime) -> bool:
        resolved = require_utc_datetime(ts, "funding_event_ts")
        return self.start_ts <= resolved < self.end_ts


@dataclass(frozen=True, slots=True)
class FundingContinuityPlanV1:
    """Derived timeline; event validation always rebuilds it from raw inputs."""

    start_ts: datetime
    end_ts: datetime
    segments: tuple[FundingScheduleSegmentV1, ...]
    expected_timestamps: tuple[datetime, ...]

    def __post_init__(self) -> None:
        start = require_utc_datetime(self.start_ts, "start_ts")
        end = require_utc_datetime(self.end_ts, "end_ts")
        if end <= start:
            raise DerivativesBacktestContractError("replay_window_invalid")
        if type(self.segments) is not tuple or not self.segments:
            raise DerivativesBacktestContractError("funding_schedule_segments_missing")
        if len(self.segments) > MAX_FUNDING_SCHEDULE_SEGMENTS_V1:
            raise DerivativesBacktestContractError(
                "funding_schedule_segment_limit_exceeded"
            )
        if type(self.expected_timestamps) is not tuple:
            raise DerivativesBacktestContractError(
                "funding_expected_timestamps_invalid"
            )
        if len(self.expected_timestamps) > MAX_EXPECTED_FUNDING_SETTLEMENTS_V1:
            raise DerivativesBacktestContractError(
                "funding_expected_event_limit_exceeded"
            )
        validated_segments: list[FundingScheduleSegmentV1] = []
        for segment in self.segments:
            if type(segment) is not FundingScheduleSegmentV1:
                raise DerivativesBacktestContractError(
                    "funding_schedule_segment_type_invalid"
                )
            validated_segments.append(
                FundingScheduleSegmentV1(
                    start_ts=segment.start_ts,
                    end_ts=segment.end_ts,
                    schedule_ref=segment.schedule_ref,
                    schedule=segment.schedule,
                    cadence=segment.cadence,
                    settlement_anchor_ts=segment.settlement_anchor_ts,
                )
            )
        segments = tuple(validated_segments)
        if segments[0].start_ts != start or segments[-1].end_ts != end:
            raise DerivativesBacktestContractError("funding_schedule_window_uncovered")
        for previous, current in zip(segments, segments[1:]):
            if current.start_ts < previous.end_ts:
                raise DerivativesBacktestContractError(
                    "funding_schedule_segment_overlap"
                )
            if current.start_ts > previous.end_ts:
                raise DerivativesBacktestContractError("funding_schedule_segment_gap")
        expected_index = 0
        for segment in segments:
            first, count = _segment_timestamp_span(segment)
            if first is None:
                continue
            for ordinal in range(count):
                if expected_index >= len(self.expected_timestamps):
                    raise DerivativesBacktestContractError(
                        "funding_expected_timestamps_mismatch"
                    )
                supplied_ts = require_utc_datetime(
                    self.expected_timestamps[expected_index],
                    "funding_expected_ts",
                )
                if supplied_ts != first + ordinal * segment.cadence:
                    raise DerivativesBacktestContractError(
                        "funding_expected_timestamps_mismatch"
                    )
                expected_index += 1
        if expected_index != len(self.expected_timestamps):
            raise DerivativesBacktestContractError(
                "funding_expected_timestamps_mismatch"
            )
        object.__setattr__(self, "segments", segments)

    def segment_at(self, ts: datetime) -> FundingScheduleSegmentV1:
        resolved = require_utc_datetime(ts, "funding_event_ts")
        lower = 0
        upper = len(self.segments)
        while lower < upper:
            middle = (lower + upper) // 2
            if self.segments[middle].start_ts <= resolved:
                lower = middle + 1
            else:
                upper = middle
        if lower:
            candidate = self.segments[lower - 1]
            if candidate.contains(resolved):
                return candidate
        raise DerivativesBacktestContractError(
            "funding_event_outside_schedule_segments"
        )


def _decode_funding_artifact(
    snapshot_set: LoadedDerivativesSnapshotSetV1,
) -> tuple[
    ImmutableSnapshotRefV1,
    FundingRateScheduleV1,
    timedelta,
    datetime,
]:
    """Independently bind public loaded values without consulting a sentinel."""

    if type(snapshot_set) is not LoadedDerivativesSnapshotSetV1:
        raise DerivativesBacktestContractError("funding_snapshot_set_invalid")
    if type(snapshot_set.refs) is not DerivativesSnapshotRefsV1:
        raise DerivativesBacktestContractError("funding_snapshot_refs_invalid")
    if (
        snapshot_set.authority_status != DERIVATIVES_SNAPSHOT_AUTHORITY_SYNTHETIC
        or snapshot_set.capital_promotion_eligible is not False
    ):
        raise DerivativesBacktestContractError("funding_snapshot_authority_invalid")
    if snapshot_set.snapshot_set_fingerprint != snapshot_set.refs.fingerprint:
        raise DerivativesBacktestContractError(
            "funding_snapshot_set_fingerprint_mismatch"
        )
    if (
        type(snapshot_set.artifacts) is not tuple
        or len(snapshot_set.artifacts) != 4
        or any(
            type(artifact) is not LoadedSnapshotArtifactV1
            for artifact in snapshot_set.artifacts
        )
        or tuple(artifact.ref for artifact in snapshot_set.artifacts)
        != (
            snapshot_set.refs.instrument,
            snapshot_set.refs.position_tier,
            snapshot_set.refs.execution_fee,
            snapshot_set.refs.funding_schedule,
        )
    ):
        raise DerivativesBacktestContractError("funding_snapshot_artifact_set_mismatch")

    ref = snapshot_set.refs.funding_schedule
    artifact = snapshot_set.artifacts[3]
    if (
        type(artifact) is not LoadedSnapshotArtifactV1
        or artifact.ref != ref
        or ref.kind is not SnapshotKindV1.FUNDING_SCHEDULE
    ):
        raise DerivativesBacktestContractError("funding_snapshot_artifact_ref_mismatch")
    raw = artifact.raw_bytes
    if type(raw) is not bytes or not raw:
        raise DerivativesBacktestContractError("funding_snapshot_raw_invalid")
    if len(raw) != ref.size_bytes or hashlib.sha256(raw).hexdigest() != ref.raw_sha256:
        raise DerivativesBacktestContractError("funding_snapshot_raw_mismatch")
    try:
        decoded = decode_strict_json_artifact(raw, expected_type=dict)
        canonical = canonical_typed_json_bytes(decoded)
        semantic_sha256 = typed_json_sha256(decoded)
    except ValueError as exc:
        raise DerivativesBacktestContractError("funding_snapshot_json_invalid") from exc
    if raw != canonical:
        raise DerivativesBacktestContractError("funding_snapshot_bytes_non_canonical")
    if semantic_sha256 != ref.semantic_sha256:
        raise DerivativesBacktestContractError("funding_snapshot_semantic_mismatch")

    envelope = require_exact_mapping_keys(
        decoded,
        _ENVELOPE_KEYS,
        "funding_snapshot_envelope_shape_invalid",
    )
    if (
        envelope["schema"] != DERIVATIVES_SNAPSHOT_ENVELOPE_SCHEMA
        or envelope["kind"] != SnapshotKindV1.FUNDING_SCHEDULE.value
        or envelope["payload_schema"] != FUNDING_SCHEDULE_PAYLOAD_SCHEMA_V1
    ):
        raise DerivativesBacktestContractError(
            "funding_snapshot_envelope_schema_mismatch"
        )
    if (
        envelope["venue"] != "OKX"
        or envelope["symbol"] != DERIVATIVES_BACKTEST_SYMBOL
        or envelope["instrument_type"] != "SWAP"
        or envelope["contract_type"] != "linear"
        or envelope["settle_currency"] != "USDT"
        or envelope["margin_mode"] != "isolated"
        or envelope["position_mode"] != "single_position"
    ):
        raise DerivativesBacktestContractError("funding_snapshot_scope_mismatch")
    if envelope["authority_status"] != snapshot_set.authority_status:
        raise DerivativesBacktestContractError("funding_snapshot_authority_mismatch")

    effective_window = require_exact_mapping_keys(
        envelope["effective_window"],
        {"start", "end"},
        "funding_snapshot_effective_window_invalid",
    )
    effective_from = require_canonical_utc_timestamp(
        effective_window["start"],
        "funding_effective_from",
    )
    effective_to = (
        None
        if effective_window["end"] is None
        else require_canonical_utc_timestamp(
            effective_window["end"],
            "funding_effective_to",
        )
    )
    if (
        require_canonical_uuid(envelope["snapshot_id"], "funding_snapshot_id")
        != ref.snapshot_id
        or require_canonical_uuid(
            envelope["source_registry_id"],
            "funding_source_registry_id",
        )
        != ref.source_registry_id
        or require_sha256(
            envelope["source_seal_fingerprint"],
            "funding_source_seal_fingerprint",
        )
        != ref.source_seal_fingerprint
        or type(envelope["source_schema"]) is not str
        or envelope["source_schema"] != ref.source_schema
        or effective_from != ref.effective_from
        or effective_to != ref.effective_to
    ):
        raise DerivativesBacktestContractError("funding_snapshot_ref_mismatch")

    payload = require_exact_mapping_keys(
        envelope["payload"],
        _FUNDING_PAYLOAD_KEYS,
        "funding_snapshot_payload_shape_invalid",
    )
    if (
        require_canonical_uuid(payload["schedule_id"], "funding_schedule_id")
        != ref.snapshot_id
    ):
        raise DerivativesBacktestContractError("funding_schedule_id_mismatch")
    schedule = FundingRateScheduleV1(
        minimum_rate_inclusive=parse_canonical_accounting_decimal(
            payload["minimum_rate_inclusive"],
            "minimum_funding_rate",
        ),
        maximum_rate_inclusive=parse_canonical_accounting_decimal(
            payload["maximum_rate_inclusive"],
            "maximum_funding_rate",
        ),
    )
    cadence = timedelta(
        seconds=require_exact_int(
            payload["cadence_seconds"],
            "funding_cadence_seconds",
            minimum=60,
            maximum=7 * 24 * 60 * 60,
        )
    )
    anchor = require_canonical_utc_timestamp(
        payload["settlement_anchor_ts"],
        "funding_settlement_anchor_ts",
    )
    derived = snapshot_set.funding_schedule
    if (
        type(derived) is not ResolvedFundingScheduleV1
        or derived.schedule != schedule
        or derived.cadence != cadence
        or derived.settlement_anchor_ts != anchor
    ):
        raise DerivativesBacktestContractError("funding_schedule_derived_mismatch")
    return ref, schedule, cadence, anchor


def _revalidate_loaded_snapshot_set(
    snapshot_set: LoadedDerivativesSnapshotSetV1,
) -> LoadedDerivativesSnapshotSetV1:
    if type(snapshot_set) is not LoadedDerivativesSnapshotSetV1:
        raise DerivativesBacktestContractError("funding_snapshot_set_invalid")
    return LoadedDerivativesSnapshotSetV1(
        refs=snapshot_set.refs,
        replay_start_ts=snapshot_set.replay_start_ts,
        replay_end_ts=snapshot_set.replay_end_ts,
        instrument_contract=snapshot_set.instrument_contract,
        position_tier=snapshot_set.position_tier,
        execution_fee=snapshot_set.execution_fee,
        funding_schedule=snapshot_set.funding_schedule,
        artifacts=snapshot_set.artifacts,
        snapshot_set_fingerprint=snapshot_set.snapshot_set_fingerprint,
        authority_status=snapshot_set.authority_status,
        capital_promotion_eligible=snapshot_set.capital_promotion_eligible,
    )


def _validate_full_snapshot_transition(
    previous: LoadedDerivativesSnapshotSetV1,
    current: LoadedDerivativesSnapshotSetV1,
    *,
    switch_ts: datetime,
) -> None:
    validate_snapshot_transition(
        previous.refs,
        current.refs,
        switch_ts=switch_ts,
    )


def _revalidate_funding_event(
    event: FundingSettlementEventV1,
) -> FundingSettlementEventV1:
    if type(event) is not FundingSettlementEventV1:
        raise DerivativesBacktestContractError("funding_event_type_invalid")
    try:
        validated = parse_derivative_replay_event(event.to_dict())
    except DerivativesBacktestContractError:
        raise
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as exc:
        raise DerivativesBacktestContractError(
            "funding_event_revalidation_failed"
        ) from exc
    if type(validated) is not FundingSettlementEventV1 or validated != event:
        raise DerivativesBacktestContractError(
            "funding_event_revalidation_mismatch"
        )
    return validated


def _timedelta_microseconds(value: timedelta) -> int:
    return (
        value.days * 24 * 60 * 60 * 1_000_000
        + value.seconds * 1_000_000
        + value.microseconds
    )


def _segment_timestamp_span(
    segment: FundingScheduleSegmentV1,
) -> tuple[datetime | None, int]:
    cadence_us = _timedelta_microseconds(segment.cadence)
    offset_us = _timedelta_microseconds(segment.start_ts - segment.settlement_anchor_ts)
    first_multiplier = -((-offset_us) // cadence_us)
    try:
        first = segment.settlement_anchor_ts + first_multiplier * segment.cadence
    except OverflowError as exc:
        raise DerivativesBacktestContractError(
            "funding_schedule_timestamp_overflow"
        ) from exc
    if first < segment.start_ts:
        raise DerivativesBacktestContractError(
            "funding_schedule_timestamp_generation_invalid"
        )
    if first >= segment.end_ts:
        return None, 0
    remaining_us = _timedelta_microseconds(segment.end_ts - first)
    count = ((remaining_us - 1) // cadence_us) + 1
    if count > MAX_EXPECTED_FUNDING_SETTLEMENTS_V1:
        raise DerivativesBacktestContractError("funding_expected_event_limit_exceeded")
    return first, count


def build_funding_continuity_plan(
    snapshot_sets: tuple[LoadedDerivativesSnapshotSetV1, ...],
    *,
    start_ts: datetime,
    end_ts: datetime,
) -> FundingContinuityPlanV1:
    """Build an exact schedule/event lattice for the half-open replay window."""

    start = require_utc_datetime(start_ts, "start_ts")
    end = require_utc_datetime(end_ts, "end_ts")
    if end <= start:
        raise DerivativesBacktestContractError("replay_window_invalid")
    if type(snapshot_sets) is not tuple or not snapshot_sets:
        raise DerivativesBacktestContractError("funding_snapshot_sets_missing")
    if len(snapshot_sets) > MAX_FUNDING_SCHEDULE_SEGMENTS_V1:
        raise DerivativesBacktestContractError(
            "funding_schedule_segment_limit_exceeded"
        )

    segments: list[FundingScheduleSegmentV1] = []
    previous_set: LoadedDerivativesSnapshotSetV1 | None = None
    for raw_snapshot_set in snapshot_sets:
        snapshot_set = _revalidate_loaded_snapshot_set(raw_snapshot_set)
        segment_start = snapshot_set.replay_start_ts
        segment_end = snapshot_set.replay_end_ts
        if segment_start < start or segment_end > end or segment_end <= segment_start:
            raise DerivativesBacktestContractError(
                "funding_schedule_segment_outside_window"
            )
        if previous_set is None:
            if segment_start != start:
                raise DerivativesBacktestContractError(
                    "funding_schedule_window_uncovered"
                )
        else:
            if segment_start < previous_set.replay_end_ts:
                raise DerivativesBacktestContractError(
                    "funding_schedule_segment_overlap"
                )
            if segment_start > previous_set.replay_end_ts:
                raise DerivativesBacktestContractError(
                    "funding_schedule_segment_gap"
                )
            _validate_full_snapshot_transition(
                previous_set,
                snapshot_set,
                switch_ts=segment_start,
            )

        ref, schedule, cadence, anchor = _decode_funding_artifact(snapshot_set)
        if (
            segments
            and segments[-1].schedule_ref.fingerprint == ref.fingerprint
        ):
            previous_segment = segments[-1]
            if (
                previous_segment.schedule != schedule
                or previous_segment.cadence != cadence
                or previous_segment.settlement_anchor_ts != anchor
            ):
                raise DerivativesBacktestContractError(
                    "funding_schedule_semantic_identity_conflict"
                )
            segments[-1] = FundingScheduleSegmentV1(
                start_ts=previous_segment.start_ts,
                end_ts=segment_end,
                schedule_ref=previous_segment.schedule_ref,
                schedule=schedule,
                cadence=cadence,
                settlement_anchor_ts=anchor,
            )
        else:
            segments.append(
                FundingScheduleSegmentV1(
                    start_ts=segment_start,
                    end_ts=segment_end,
                    schedule_ref=ref,
                    schedule=schedule,
                    cadence=cadence,
                    settlement_anchor_ts=anchor,
                )
            )
        previous_set = snapshot_set

    if previous_set is None or previous_set.replay_end_ts != end:
        raise DerivativesBacktestContractError("funding_schedule_window_uncovered")

    expected: list[datetime] = []
    for segment in segments:
        first, count = _segment_timestamp_span(segment)
        if len(expected) + count > MAX_EXPECTED_FUNDING_SETTLEMENTS_V1:
            raise DerivativesBacktestContractError(
                "funding_expected_event_limit_exceeded"
            )
        if first is not None:
            expected.extend(
                first + ordinal * segment.cadence for ordinal in range(count)
            )
    return FundingContinuityPlanV1(
        start_ts=start,
        end_ts=end,
        segments=tuple(segments),
        expected_timestamps=tuple(expected),
    )


def validate_funding_settlement_events(
    snapshot_sets: tuple[LoadedDerivativesSnapshotSetV1, ...],
    events: tuple[FundingSettlementEventV1, ...],
    *,
    start_ts: datetime,
    end_ts: datetime,
) -> FundingContinuityPlanV1:
    """Validate exact funding completeness before any economic state mutation."""

    plan = build_funding_continuity_plan(
        snapshot_sets,
        start_ts=start_ts,
        end_ts=end_ts,
    )
    if type(events) is not tuple:
        raise DerivativesBacktestContractError("funding_event_sequence_invalid")
    if len(events) > MAX_EXPECTED_FUNDING_SETTLEMENTS_V1:
        raise DerivativesBacktestContractError("funding_event_limit_exceeded")

    previous_ts: datetime | None = None
    for event in events:
        validated = _revalidate_funding_event(event)
        event_ts = validated.header.ts
        if previous_ts is not None and event_ts == previous_ts:
            raise DerivativesBacktestContractError("funding_event_duplicate")
        if previous_ts is not None and event_ts < previous_ts:
            raise DerivativesBacktestContractError("funding_event_order_invalid")
        previous_ts = event_ts

    expected = plan.expected_timestamps
    if len(events) < len(expected):
        raise DerivativesBacktestContractError("funding_event_missing")
    if len(events) > len(expected):
        raise DerivativesBacktestContractError("funding_event_extra")

    segment_index = 0
    for event_index, event in enumerate(events):
        # Reconstructing rechecks header identity and all public event fields;
        # frozen dataclasses and private constructors are not safety boundaries.
        validated = _revalidate_funding_event(event)
        event_ts = validated.header.ts
        if event_ts != expected[event_index]:
            raise DerivativesBacktestContractError("funding_event_timestamp_mismatch")
        while (
            segment_index + 1 < len(plan.segments)
            and event_ts >= plan.segments[segment_index].end_ts
        ):
            segment_index += 1
        segment = plan.segments[segment_index]
        if not segment.contains(event_ts):
            raise DerivativesBacktestContractError(
                "funding_event_outside_schedule_segments"
            )
        if validated.schedule_ref.fingerprint != segment.schedule_ref.fingerprint:
            raise DerivativesBacktestContractError(
                "funding_event_schedule_ref_mismatch"
            )
        validated.schedule_ref.validate_at(event_ts)
        segment.schedule.validate_rate(validated.rate)
        if validated.observed_at_ts > validated.header.ts:
            raise DerivativesBacktestContractError("funding_observation_in_future")
        if validated.header.ts - validated.observed_at_ts > segment.cadence:
            raise DerivativesBacktestContractError("funding_event_stale")
    return plan


__all__ = [
    "FUNDING_SCHEDULE_PAYLOAD_SCHEMA_V1",
    "MAX_EXPECTED_FUNDING_SETTLEMENTS_V1",
    "MAX_FUNDING_SCHEDULE_SEGMENTS_V1",
    "FundingContinuityPlanV1",
    "FundingScheduleSegmentV1",
    "build_funding_continuity_plan",
    "validate_funding_settlement_events",
]
