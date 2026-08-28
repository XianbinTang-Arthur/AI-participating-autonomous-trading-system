from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from aats.data_platform.governance.typed_json_identity import (
    canonical_typed_json_bytes,
    typed_json_sha256,
)
from aats.data_platform.replay.derivatives_backtest.contracts import (
    DerivativesBacktestContractError,
)
from aats.data_platform.replay.derivatives_backtest.events import (
    FundingSettlementEventV1,
)
from aats.data_platform.replay.derivatives_backtest.funding import (
    MAX_EXPECTED_FUNDING_SETTLEMENTS_V1,
    build_funding_continuity_plan,
    validate_funding_settlement_events,
)
from aats.data_platform.replay.derivatives_backtest.snapshot_loader import (
    LoadedDerivativesSnapshotSetV1,
    load_non_promotable_derivatives_snapshot_set,
)
from aats.data_platform.replay.derivatives_backtest.snapshot_refs import (
    SnapshotKindV1,
)
from aats.data_platform.replay.derivatives_backtest.wire import (
    canonical_utc_timestamp,
)
from tests.unit.data_platform.replay.derivatives_backtest._event_helpers import (
    BASE_TS,
    source_ref,
)
from tests.unit.data_platform.replay.derivatives_backtest.test_snapshot_loader import (
    build_snapshot_set,
    replace_ref,
)


SWITCH_TS = datetime(2026, 1, 2, 2, 0, tzinfo=timezone.utc)
END_TS = datetime(2026, 1, 2, 3, 0, tzinfo=timezone.utc)
ANCHOR_TS = datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc)


def _loaded_schedule(
    root: Path,
    *,
    snapshot_id: str,
    effective_from: datetime,
    effective_to: datetime | None,
    load_start: datetime,
    load_end: datetime,
    cadence_seconds: int,
    anchor: datetime,
    minimum_rate: str,
    maximum_rate: str,
) -> LoadedDerivativesSnapshotSetV1:
    refs, decoded = build_snapshot_set(root)
    kind = SnapshotKindV1.FUNDING_SCHEDULE
    original_ref = refs.funding_schedule
    envelope = decoded[kind]
    envelope["snapshot_id"] = snapshot_id
    envelope["effective_window"] = {
        "start": canonical_utc_timestamp(effective_from),
        "end": (
            None if effective_to is None else canonical_utc_timestamp(effective_to)
        ),
    }
    envelope["payload"] = {
        "minimum_rate_inclusive": minimum_rate,
        "maximum_rate_inclusive": maximum_rate,
        "schedule_id": snapshot_id,
        "cadence_seconds": cadence_seconds,
        "settlement_anchor_ts": canonical_utc_timestamp(anchor),
    }
    raw = canonical_typed_json_bytes(envelope)
    (root / original_ref.relative_path).write_bytes(raw)
    updated_ref = replace(
        original_ref,
        snapshot_id=snapshot_id,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        semantic_sha256=typed_json_sha256(envelope),
        effective_from=effective_from,
        effective_to=effective_to,
    )
    refs = replace_ref(refs, kind, updated_ref)
    return load_non_promotable_derivatives_snapshot_set(
        refs,
        snapshot_root=root,
        start_ts=load_start,
        end_ts=load_end,
    )


def _two_schedule_timeline(
    root: Path,
    *,
    first_end: datetime = SWITCH_TS,
    second_start: datetime = SWITCH_TS,
) -> tuple[LoadedDerivativesSnapshotSetV1, LoadedDerivativesSnapshotSetV1]:
    first = _loaded_schedule(
        root / "first",
        snapshot_id="00000000-0000-4000-8000-000000000104",
        effective_from=BASE_TS,
        effective_to=first_end,
        load_start=BASE_TS,
        load_end=first_end,
        cadence_seconds=3600,
        anchor=ANCHOR_TS,
        minimum_rate="-1e-3",
        maximum_rate="1e-3",
    )
    second = _loaded_schedule(
        root / "second",
        snapshot_id="00000000-0000-4000-8000-000000000204",
        effective_from=second_start,
        effective_to=END_TS,
        load_start=second_start,
        load_end=END_TS,
        cadence_seconds=1800,
        anchor=SWITCH_TS,
        minimum_rate="-1e-2",
        maximum_rate="1e-2",
    )
    return first, second


def _loaded_fee_segment(
    root: Path,
    *,
    snapshot_id: str,
    effective_from: datetime,
    effective_to: datetime,
    taker_fee_rate: str,
) -> LoadedDerivativesSnapshotSetV1:
    refs, decoded = build_snapshot_set(root)
    kind = SnapshotKindV1.EXECUTION_FEE
    original_ref = refs.execution_fee
    envelope = decoded[kind]
    envelope["snapshot_id"] = snapshot_id
    envelope["effective_window"] = {
        "start": canonical_utc_timestamp(effective_from),
        "end": canonical_utc_timestamp(effective_to),
    }
    envelope["payload"]["taker_fee_rate"] = taker_fee_rate
    raw = canonical_typed_json_bytes(envelope)
    (root / original_ref.relative_path).write_bytes(raw)
    updated_ref = replace(
        original_ref,
        snapshot_id=snapshot_id,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        semantic_sha256=typed_json_sha256(envelope),
        effective_from=effective_from,
        effective_to=effective_to,
    )
    refs = replace_ref(refs, kind, updated_ref)
    return load_non_promotable_derivatives_snapshot_set(
        refs,
        snapshot_root=root,
        start_ts=effective_from,
        end_ts=effective_to,
    )


def _event(
    loaded: LoadedDerivativesSnapshotSetV1,
    *,
    ts: datetime,
    sequence: int,
    rate: str,
    observed_at: datetime | None = None,
) -> FundingSettlementEventV1:
    return FundingSettlementEventV1.create(
        ts=ts,
        source_sequence=sequence,
        source_ref=source_ref("funding"),
        rate=Decimal(rate),
        schedule_ref=loaded.refs.funding_schedule,
        observed_at_ts=ts if observed_at is None else observed_at,
    )


def _valid_events(
    first: LoadedDerivativesSnapshotSetV1,
    second: LoadedDerivativesSnapshotSetV1,
) -> tuple[FundingSettlementEventV1, ...]:
    return (
        _event(
            first,
            ts=datetime(2026, 1, 2, 1, 0, tzinfo=timezone.utc),
            sequence=1,
            rate="5e-4",
            observed_at=datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc),
        ),
        _event(second, ts=SWITCH_TS, sequence=2, rate="5e-3"),
        _event(
            second,
            ts=datetime(2026, 1, 2, 2, 30, tzinfo=timezone.utc),
            sequence=3,
            rate="-1e-2",
        ),
    )


def _assert_code(code: str, operation) -> None:
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        operation()
    assert exc_info.value.code == code


def test_dynamic_switch_uses_new_half_open_schedule_and_exact_lattice(
    tmp_path: Path,
) -> None:
    first, second = _two_schedule_timeline(tmp_path)
    snapshots = (first, second)
    events = _valid_events(first, second)

    plan = validate_funding_settlement_events(
        snapshots,
        events,
        start_ts=BASE_TS,
        end_ts=END_TS,
    )

    assert plan.expected_timestamps == (
        datetime(2026, 1, 2, 1, 0, tzinfo=timezone.utc),
        SWITCH_TS,
        datetime(2026, 1, 2, 2, 30, tzinfo=timezone.utc),
    )
    assert plan.segment_at(SWITCH_TS).schedule_ref == second.refs.funding_schedule
    assert events[1].rate > first.funding_schedule.schedule.maximum_rate_inclusive


def test_plan_does_not_alias_loaded_ref_and_revalidates_expected_lattice(
    tmp_path: Path,
) -> None:
    first, second = _two_schedule_timeline(tmp_path)
    plan = build_funding_continuity_plan(
        (first, second),
        start_ts=BASE_TS,
        end_ts=END_TS,
    )
    original_raw_sha = plan.segments[0].schedule_ref.raw_sha256

    object.__setattr__(first.refs.funding_schedule, "raw_sha256", "0" * 64)

    assert plan.segments[0].schedule_ref.raw_sha256 == original_raw_sha
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        replace(plan, expected_timestamps=())
    assert exc_info.value.code == "funding_expected_timestamps_mismatch"


def test_fee_only_full_tuple_transition_coalesces_same_funding_schedule(
    tmp_path: Path,
) -> None:
    first = _loaded_fee_segment(
        tmp_path / "first",
        snapshot_id="00000000-0000-4000-8000-000000000103",
        effective_from=BASE_TS,
        effective_to=SWITCH_TS,
        taker_fee_rate="5e-4",
    )
    second = _loaded_fee_segment(
        tmp_path / "second",
        snapshot_id="00000000-0000-4000-8000-000000000203",
        effective_from=SWITCH_TS,
        effective_to=END_TS,
        taker_fee_rate="6e-4",
    )

    plan = build_funding_continuity_plan(
        (first, second),
        start_ts=BASE_TS,
        end_ts=END_TS,
    )

    assert len(plan.segments) == 1
    assert plan.segments[0].start_ts == BASE_TS
    assert plan.segments[0].end_ts == END_TS
    assert (
        plan.segments[0].schedule_ref.fingerprint
        == first.refs.funding_schedule.fingerprint
        == second.refs.funding_schedule.fingerprint
    )


@pytest.mark.parametrize(
    ("first_end", "second_start", "code"),
    [
        (
            SWITCH_TS - timedelta(minutes=1),
            SWITCH_TS,
            "funding_schedule_segment_gap",
        ),
        (
            SWITCH_TS + timedelta(minutes=1),
            SWITCH_TS,
            "funding_schedule_segment_overlap",
        ),
    ],
)
def test_schedule_segments_must_have_no_gap_or_overlap(
    tmp_path: Path,
    first_end: datetime,
    second_start: datetime,
    code: str,
) -> None:
    snapshots = _two_schedule_timeline(
        tmp_path,
        first_end=first_end,
        second_start=second_start,
    )

    _assert_code(
        code,
        lambda: build_funding_continuity_plan(
            snapshots,
            start_ts=BASE_TS,
            end_ts=END_TS,
        ),
    )


def test_raw_ref_and_derived_values_are_revalidated_after_object_mutation(
    tmp_path: Path,
) -> None:
    first, _second = _two_schedule_timeline(tmp_path)
    object.__setattr__(
        first,
        "funding_schedule",
        replace(first.funding_schedule, cadence=timedelta(hours=2)),
    )
    raw_tamper, _unused = _two_schedule_timeline(tmp_path / "raw-tamper")
    object.__setattr__(
        raw_tamper.artifacts[3],
        "raw_bytes",
        b"{}",
    )

    _assert_code(
        "snapshot_derived_contract_mismatch",
        lambda: build_funding_continuity_plan(
            (first,),
            start_ts=BASE_TS,
            end_ts=SWITCH_TS,
        ),
    )
    _assert_code(
        "snapshot_size_mismatch",
        lambda: build_funding_continuity_plan(
            (raw_tamper,),
            start_ts=BASE_TS,
            end_ts=SWITCH_TS,
        ),
    )


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("missing", "funding_event_missing"),
        ("duplicate", "funding_event_duplicate"),
        ("extra", "funding_event_extra"),
        ("wrong_order", "funding_event_order_invalid"),
        ("wrong_timestamp", "funding_event_timestamp_mismatch"),
    ],
)
def test_missing_duplicate_extra_and_wrong_event_sequences_fail_closed(
    tmp_path: Path,
    mutation: str,
    code: str,
) -> None:
    first, second = _two_schedule_timeline(tmp_path)
    valid = _valid_events(first, second)
    if mutation == "missing":
        events = valid[:-1]
    elif mutation == "duplicate":
        events = (valid[0], valid[1], valid[1], valid[2])
    elif mutation == "extra":
        events = (
            *valid,
            _event(
                second,
                ts=datetime(2026, 1, 2, 2, 45, tzinfo=timezone.utc),
                sequence=4,
                rate="0",
            ),
        )
    elif mutation == "wrong_order":
        events = (valid[1], valid[0], valid[2])
    else:
        events = (
            valid[0],
            _event(
                second,
                ts=datetime(2026, 1, 2, 2, 15, tzinfo=timezone.utc),
                sequence=2,
                rate="0",
            ),
            valid[2],
        )

    _assert_code(
        code,
        lambda: validate_funding_settlement_events(
            (first, second),
            events,
            start_ts=BASE_TS,
            end_ts=END_TS,
        ),
    )


def test_switch_timestamp_rejects_old_schedule_ref(tmp_path: Path) -> None:
    first, second = _two_schedule_timeline(tmp_path)
    valid = _valid_events(first, second)
    wrong_boundary_event = _event(
        first,
        ts=SWITCH_TS,
        sequence=2,
        rate="5e-4",
    )

    _assert_code(
        "funding_event_schedule_ref_mismatch",
        lambda: validate_funding_settlement_events(
            (first, second),
            (valid[0], wrong_boundary_event, valid[2]),
            start_ts=BASE_TS,
            end_ts=END_TS,
        ),
    )


def test_rate_cap_and_observation_age_are_enforced(tmp_path: Path) -> None:
    first, second = _two_schedule_timeline(tmp_path)
    valid = _valid_events(first, second)
    out_of_bounds = _event(second, ts=SWITCH_TS, sequence=2, rate="2e-2")
    stale = _event(
        first,
        ts=valid[0].header.ts,
        sequence=1,
        rate="0",
        observed_at=valid[0].header.ts - timedelta(hours=1, microseconds=1),
    )

    _assert_code(
        "funding_rate_out_of_schedule",
        lambda: validate_funding_settlement_events(
            (first, second),
            (valid[0], out_of_bounds, valid[2]),
            start_ts=BASE_TS,
            end_ts=END_TS,
        ),
    )
    _assert_code(
        "funding_event_stale",
        lambda: validate_funding_settlement_events(
            (first, second),
            (stale, valid[1], valid[2]),
            start_ts=BASE_TS,
            end_ts=END_TS,
        ),
    )


def test_mutated_future_observation_is_reconstructed_and_rejected(
    tmp_path: Path,
) -> None:
    first, second = _two_schedule_timeline(tmp_path)
    valid = list(_valid_events(first, second))
    object.__setattr__(
        valid[0],
        "observed_at_ts",
        valid[0].header.ts + timedelta(microseconds=1),
    )

    _assert_code(
        "event_id_mismatch",
        lambda: validate_funding_settlement_events(
            (first, second),
            tuple(valid),
            start_ts=BASE_TS,
            end_ts=END_TS,
        ),
    )


def test_mutated_header_type_maps_to_stable_funding_error(tmp_path: Path) -> None:
    first, second = _two_schedule_timeline(tmp_path)
    valid = list(_valid_events(first, second))
    object.__setattr__(valid[0].header, "ts", "bad")

    _assert_code(
        "timestamp_utc_required",
        lambda: validate_funding_settlement_events(
            (first, second),
            tuple(valid),
            start_ts=BASE_TS,
            end_ts=END_TS,
        ),
    )



@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("schedule_path", "artifact_relative_path_invalid"),
        ("source_digest", "sha256_non_canonical"),
    ],
)
def test_nested_funding_event_identity_is_strictly_reparsed(
    tmp_path: Path,
    mutation: str,
    code: str,
) -> None:
    first, second = _two_schedule_timeline(tmp_path)
    valid = list(_valid_events(first, second))
    if mutation == "schedule_path":
        copied_ref = replace(valid[0].schedule_ref)
        object.__setattr__(copied_ref, "relative_path", "../unsafe")
        object.__setattr__(valid[0], "schedule_ref", copied_ref)
    else:
        object.__setattr__(
            valid[0].header.source_ref,
            "parent_artifact_sha256",
            "BAD",
        )

    _assert_code(
        code,
        lambda: validate_funding_settlement_events(
            (first, second),
            tuple(valid),
            start_ts=BASE_TS,
            end_ts=END_TS,
        ),
    )


def test_expected_timestamp_generation_has_a_non_configurable_hard_limit(
    tmp_path: Path,
) -> None:
    end = BASE_TS + timedelta(minutes=MAX_EXPECTED_FUNDING_SETTLEMENTS_V1 + 1)
    loaded = _loaded_schedule(
        tmp_path,
        snapshot_id="00000000-0000-4000-8000-000000000304",
        effective_from=BASE_TS,
        effective_to=end,
        load_start=BASE_TS,
        load_end=end,
        cadence_seconds=60,
        anchor=BASE_TS,
        minimum_rate="-1e-2",
        maximum_rate="1e-2",
    )

    _assert_code(
        "funding_expected_event_limit_exceeded",
        lambda: build_funding_continuity_plan(
            (loaded,),
            start_ts=BASE_TS,
            end_ts=end,
        ),
    )
