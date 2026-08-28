from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from aats.data_platform.replay.derivatives_backtest.contracts import (
    DerivativesBacktestContractError,
)
from aats.data_platform.replay.derivatives_backtest.snapshot_refs import (
    DERIVATIVES_SNAPSHOT_MAX_BYTES,
    DerivativesSnapshotRefsV1,
    ImmutableSnapshotRefV1,
    SnapshotKindV1,
    validate_snapshot_transition,
)
from tests.unit.data_platform.replay.derivatives_backtest._event_helpers import (
    BASE_TS,
    snapshot_ref,
    snapshot_refs,
)


def test_snapshot_ref_round_trip_preserves_exact_identity() -> None:
    original = snapshot_ref(SnapshotKindV1.INSTRUMENT, ordinal=1)

    restored = ImmutableSnapshotRefV1.from_dict(original.to_dict())

    assert restored == original
    assert restored.fingerprint == original.fingerprint


def test_snapshot_set_round_trip_and_window_preflight() -> None:
    original = snapshot_refs()

    restored = DerivativesSnapshotRefsV1.from_dict(original.to_dict())
    restored.validate_window(start=BASE_TS, end=BASE_TS + timedelta(days=1))

    assert restored == original
    assert restored.fingerprint == original.fingerprint


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("snapshot_id", "{00000000-0000-4000-8000-000000000001}", "uuid_non_canonical"),
        ("raw_sha256", "A" * 64, "sha256_non_canonical"),
        ("semantic_sha256", "sha256:" + "a" * 64, "sha256_non_canonical"),
        ("size_bytes", True, "integer_out_of_bounds"),
        ("size_bytes", DERIVATIVES_SNAPSHOT_MAX_BYTES + 1, "integer_out_of_bounds"),
        ("relative_path", "../snapshot.json", "artifact_relative_path_invalid"),
        ("relative_path", "C:/snapshot.json", "artifact_relative_path_invalid"),
        ("relative_path", "snapshots\\instrument.json", "artifact_relative_path_invalid"),
        ("relative_path", "snapshots/CON.json", "artifact_relative_path_invalid"),
        ("relative_path", "snapshots/lpt1", "artifact_relative_path_invalid"),
    ],
)
def test_snapshot_ref_rejects_noncanonical_identity_fields(
    field: str,
    value: object,
    code: str,
) -> None:
    payload = snapshot_ref(SnapshotKindV1.INSTRUMENT, ordinal=1).to_dict()
    payload[field] = value

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        ImmutableSnapshotRefV1.from_dict(payload)

    assert exc_info.value.code == code


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-01-02T00:15:00Z",
        "2026-01-02T00:15:00.000000+00:00",
        "2026-01-01T19:15:00.000000-05:00",
    ],
)
def test_snapshot_ref_rejects_noncanonical_time_wire(timestamp: str) -> None:
    payload = snapshot_ref(SnapshotKindV1.INSTRUMENT, ordinal=1).to_dict()
    payload["effective_window"]["start"] = timestamp

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        ImmutableSnapshotRefV1.from_dict(payload)

    assert exc_info.value.code == "timestamp_non_canonical"


def test_snapshot_ref_rejects_unknown_key() -> None:
    payload = snapshot_ref(SnapshotKindV1.INSTRUMENT, ordinal=1).to_dict()
    payload["verified"] = True

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        ImmutableSnapshotRefV1.from_dict(payload)

    assert exc_info.value.code == "snapshot_ref_shape_invalid"


def test_snapshot_window_is_half_open_and_must_be_fully_proven() -> None:
    ref = snapshot_ref(SnapshotKindV1.INSTRUMENT, ordinal=1)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        ref.validate_window(
            start=BASE_TS - timedelta(microseconds=1),
            end=BASE_TS + timedelta(hours=1),
        )

    assert exc_info.value.code == "snapshot_effective_window_unproven"


def test_snapshot_set_rejects_casefold_path_collision() -> None:
    refs = snapshot_refs()
    duplicate_path = ImmutableSnapshotRefV1(
        kind=SnapshotKindV1.POSITION_TIER,
        snapshot_id=refs.position_tier.snapshot_id,
        relative_path="SNAPSHOTS/INSTRUMENT.JSON",
        raw_sha256=refs.position_tier.raw_sha256,
        size_bytes=refs.position_tier.size_bytes,
        semantic_sha256=refs.position_tier.semantic_sha256,
        source_registry_id=refs.position_tier.source_registry_id,
        source_seal_fingerprint=refs.position_tier.source_seal_fingerprint,
        source_schema=refs.position_tier.source_schema,
        effective_from=refs.position_tier.effective_from,
        effective_to=refs.position_tier.effective_to,
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        DerivativesSnapshotRefsV1(
            instrument=refs.instrument,
            position_tier=duplicate_path,
            execution_fee=refs.execution_fee,
            funding_schedule=refs.funding_schedule,
        )

    assert exc_info.value.code == "snapshot_set_identity_duplicate"


def test_snapshot_fingerprint_excludes_local_locator() -> None:
    original = snapshot_ref(SnapshotKindV1.INSTRUMENT, ordinal=1)
    relocated = ImmutableSnapshotRefV1(
        kind=original.kind,
        snapshot_id=original.snapshot_id,
        relative_path="alternate/instrument.json",
        raw_sha256=original.raw_sha256,
        size_bytes=original.size_bytes,
        semantic_sha256=original.semantic_sha256,
        source_registry_id=original.source_registry_id,
        source_seal_fingerprint=original.source_seal_fingerprint,
        source_schema=original.source_schema,
        effective_from=original.effective_from,
        effective_to=original.effective_to,
    )

    assert relocated.to_dict() != original.to_dict()
    assert relocated.fingerprint == original.fingerprint


def test_snapshot_transition_requires_exact_half_open_switch() -> None:
    switch_ts = BASE_TS + timedelta(hours=1)
    original = snapshot_refs()
    active = replace(
        original,
        instrument=replace(original.instrument, effective_to=switch_ts),
    )
    incoming = replace(
        active,
        instrument=replace(
            active.instrument,
            snapshot_id="00000000-0000-4000-8000-000000000101",
            raw_sha256="c" * 64,
            semantic_sha256="d" * 64,
            source_seal_fingerprint="e" * 64,
            effective_from=switch_ts,
            effective_to=None,
        ),
    )

    validated_active, validated_incoming = validate_snapshot_transition(
        active,
        incoming,
        switch_ts=switch_ts,
    )

    assert validated_active == active
    assert validated_incoming == incoming
    assert validated_active is not active
    assert validated_incoming is not incoming


def test_snapshot_transition_rejects_noop_and_window_gap() -> None:
    switch_ts = BASE_TS + timedelta(hours=1)
    active = snapshot_refs()

    with pytest.raises(DerivativesBacktestContractError) as noop_info:
        validate_snapshot_transition(active, active, switch_ts=switch_ts)
    assert noop_info.value.code == "snapshot_activation_noop"

    closed = replace(
        active,
        instrument=replace(active.instrument, effective_to=switch_ts),
    )
    gapped = replace(
        closed,
        instrument=replace(
            closed.instrument,
            snapshot_id="00000000-0000-4000-8000-000000000102",
            raw_sha256="c" * 64,
            semantic_sha256="d" * 64,
            source_seal_fingerprint="e" * 64,
            effective_from=switch_ts + timedelta(microseconds=1),
            effective_to=None,
        ),
    )
    with pytest.raises(DerivativesBacktestContractError) as gap_info:
        validate_snapshot_transition(closed, gapped, switch_ts=switch_ts)
    assert gap_info.value.code == "snapshot_transition_window_invalid"
