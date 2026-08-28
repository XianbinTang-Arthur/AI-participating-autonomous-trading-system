from __future__ import annotations

import copy
import hashlib
from dataclasses import replace
from datetime import timedelta

import pytest

import aats.data_platform.replay.derivatives_backtest.event_set as event_set_module
from aats.data_platform.replay.derivatives_backtest.contracts import (
    DerivativesBacktestContractError,
)
from aats.data_platform.replay.derivatives_backtest.event_set import (
    DERIVATIVES_EVENT_SET_MAX_EVENTS,
    DERIVATIVES_EVENT_STREAM_INTEGRITY_POLICY_FINGERPRINT,
    DERIVATIVES_EVENT_STREAM_MAX_EVENTS,
    DERIVATIVES_MAX_SNAPSHOT_CATALOG_ENTRIES,
    DerivativesEventSetManifestV1,
    DerivativesEventSetRefV1,
    DerivativesEventStreamCursorV1,
    DerivativesEventStreamRefV1,
    EventStreamBoundaryKeyV1,
    EventStreamIntegritySummaryV1,
    SnapshotSetCatalogEntryV1,
    derive_raw_artifact_set_fingerprint,
    event_stream_semantic_seed,
    update_event_stream_semantic_digest,
)
from aats.data_platform.replay.derivatives_backtest.events import (
    EXPECTED_EVENT_STREAM_ID_V1,
    SINGLETON_EVENT_KINDS_PER_TIMESTAMP_V1,
    ContractTierEffectiveEventV1,
    DerivativeEventKindV1,
)
from aats.data_platform.governance.typed_json_identity import (
    canonical_typed_json_bytes,
)
from aats.data_platform.replay.derivatives_backtest.snapshot_refs import (
    DerivativesSnapshotRefsV1,
)
from tests.unit.data_platform.replay.derivatives_backtest._event_helpers import (
    BASE_TS,
    all_events,
    snapshot_refs,
    source_ref,
)


END_TS = BASE_TS + timedelta(hours=1)
EVALUATION_START_TS = BASE_TS + timedelta(minutes=15)
EVENT_SET_ID = "00000000-0000-4000-8000-000000000301"
TRANSFORM_FINGERPRINT = "9" * 64
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _event_by_kind():
    events = all_events(BASE_TS)
    return {
        DerivativeEventKindV1.CONTRACT_TIER_EFFECTIVE: events["contract"],
        DerivativeEventKindV1.INDEX_PRICE: events["index"],
        DerivativeEventKindV1.MARK_PRICE: events["mark"],
        DerivativeEventKindV1.FUNDING_SETTLEMENT: events["funding"],
        DerivativeEventKindV1.TRADABLE: events["tradable"],
        DerivativeEventKindV1.BAR_CLOSE: events["bar"],
    }


def _stream_ref(kind: DerivativeEventKindV1) -> DerivativesEventStreamRefV1:
    event = _event_by_kind()[kind]
    boundary = EventStreamBoundaryKeyV1(
        ts=event.header.ts,
        source_sequence=event.header.source_sequence,
        event_id=event.header.event_id,
    )
    digest = update_event_stream_semantic_digest(
        event_stream_semantic_seed(kind),
        event_id=event.header.event_id,
    )
    ordinal = tuple(DerivativeEventKindV1).index(kind) + 1
    return DerivativesEventStreamRefV1(
        kind=kind,
        stream_id=EXPECTED_EVENT_STREAM_ID_V1[kind],
        relative_path=f"events/{kind.value}.jsonl",
        size_bytes=100 + ordinal,
        raw_sha256=f"{ordinal:x}" * 64,
        event_count=1,
        semantic_event_digest=digest,
        integrity=EventStreamIntegritySummaryV1.create(
            kind=kind,
            coverage_start_ts=BASE_TS,
            coverage_end_ts=END_TS,
            checked_event_count=1,
            semantic_event_digest=digest,
        ),
        first_key=boundary,
        last_key=boundary,
        coverage_start_ts=BASE_TS,
        coverage_end_ts=END_TS,
        source_registry_ids=(event.header.source_ref.source_registry_id,),
        parent_raw_partition_sha256s=(
            event.header.source_ref.parent_artifact_sha256,
        ),
    )


def _empty_stream(kind: DerivativeEventKindV1) -> DerivativesEventStreamRefV1:
    event = _event_by_kind()[kind]
    digest = event_stream_semantic_seed(kind)
    return DerivativesEventStreamRefV1(
        kind=kind,
        stream_id=EXPECTED_EVENT_STREAM_ID_V1[kind],
        relative_path=f"events/{kind.value}.jsonl",
        size_bytes=0,
        raw_sha256=EMPTY_SHA256,
        event_count=0,
        semantic_event_digest=digest,
        integrity=EventStreamIntegritySummaryV1.create(
            kind=kind,
            coverage_start_ts=BASE_TS,
            coverage_end_ts=END_TS,
            checked_event_count=0,
            semantic_event_digest=digest,
        ),
        first_key=None,
        last_key=None,
        coverage_start_ts=BASE_TS,
        coverage_end_ts=END_TS,
        source_registry_ids=(event.header.source_ref.source_registry_id,),
        parent_raw_partition_sha256s=(
            event.header.source_ref.parent_artifact_sha256,
        ),
    )


def _with_declared_count(
    stream: DerivativesEventStreamRefV1,
    count: int,
) -> DerivativesEventStreamRefV1:
    assert stream.first_key is not None
    digest = stream.semantic_event_digest
    last = stream.first_key
    if count > 1:
        last = EventStreamBoundaryKeyV1(
            ts=min(EVALUATION_START_TS, END_TS - timedelta(microseconds=1)),
            source_sequence=stream.first_key.source_sequence + 1,
            event_id="f" * 64,
        )
        digest = update_event_stream_semantic_digest(
            digest,
            event_id=last.event_id,
        )
    return replace(
        stream,
        size_bytes=max(stream.size_bytes, 256),
        raw_sha256="d" * 64,
        event_count=count,
        semantic_event_digest=digest,
        integrity=EventStreamIntegritySummaryV1.create(
            kind=stream.kind,
            coverage_start_ts=stream.coverage_start_ts,
            coverage_end_ts=stream.coverage_end_ts,
            checked_event_count=count,
            semantic_event_digest=digest,
        ),
        last_key=last,
    )


def _streams() -> tuple[DerivativesEventStreamRefV1, ...]:
    streams = []
    for kind in DerivativeEventKindV1:
        if kind is DerivativeEventKindV1.CONTRACT_TIER_EFFECTIVE:
            streams.append(_empty_stream(kind))
            continue
        stream = _stream_ref(kind)
        if kind is DerivativeEventKindV1.BAR_CLOSE:
            stream = _with_declared_count(stream, 2)
        streams.append(stream)
    return tuple(streams)


def _catalog(
    refs: DerivativesSnapshotRefsV1 | None = None,
) -> tuple[SnapshotSetCatalogEntryV1, ...]:
    return (
        SnapshotSetCatalogEntryV1(
            activation_ts=BASE_TS,
            refs=snapshot_refs() if refs is None else refs,
        ),
    )


def _manifest(
    *,
    streams: tuple[DerivativesEventStreamRefV1, ...] | None = None,
    snapshot_catalog: tuple[SnapshotSetCatalogEntryV1, ...] | None = None,
) -> DerivativesEventSetManifestV1:
    return DerivativesEventSetManifestV1.create(
        event_set_id=EVENT_SET_ID,
        warmup_start_ts=BASE_TS,
        evaluation_start_ts=EVALUATION_START_TS,
        end_ts=END_TS,
        dataset_version="synthetic-v1",
        transform_policy_id="rdp-transform",
        transform_policy_version="v1",
        transform_policy_fingerprint=TRANSFORM_FINGERPRINT,
        snapshot_catalog=_catalog() if snapshot_catalog is None else snapshot_catalog,
        streams=_streams() if streams is None else streams,
    )


def test_event_stream_digest_is_domain_separated_and_order_sensitive() -> None:
    first_id = "a" * 64
    second_id = "b" * 64
    mark_seed = event_stream_semantic_seed(DerivativeEventKindV1.MARK_PRICE)
    index_seed = event_stream_semantic_seed(DerivativeEventKindV1.INDEX_PRICE)

    forward = update_event_stream_semantic_digest(
        update_event_stream_semantic_digest(mark_seed, event_id=first_id),
        event_id=second_id,
    )
    reverse = update_event_stream_semantic_digest(
        update_event_stream_semantic_digest(mark_seed, event_id=second_id),
        event_id=first_id,
    )

    assert mark_seed != index_seed
    assert forward != reverse


def test_event_stream_integrity_policy_fingerprint_is_frozen() -> None:
    assert SINGLETON_EVENT_KINDS_PER_TIMESTAMP_V1 == {
        DerivativeEventKindV1.CONTRACT_TIER_EFFECTIVE,
        DerivativeEventKindV1.FUNDING_SETTLEMENT,
        DerivativeEventKindV1.TRADABLE,
        DerivativeEventKindV1.BAR_CLOSE,
    }
    assert DERIVATIVES_EVENT_STREAM_INTEGRITY_POLICY_FINGERPRINT == (
        "cfe051b5f8763e11420a93e2ded821acfa2b6ca34040a85ab441045220d8fb17"
    )


def test_empty_stream_has_one_canonical_identity() -> None:
    event = _event_by_kind()[DerivativeEventKindV1.INDEX_PRICE]
    stream = DerivativesEventStreamRefV1(
        kind=DerivativeEventKindV1.INDEX_PRICE,
        stream_id=EXPECTED_EVENT_STREAM_ID_V1[
            DerivativeEventKindV1.INDEX_PRICE
        ],
        relative_path="events/index-price.jsonl",
        size_bytes=0,
        raw_sha256=EMPTY_SHA256,
        event_count=0,
        semantic_event_digest=event_stream_semantic_seed(
            DerivativeEventKindV1.INDEX_PRICE
        ),
        integrity=EventStreamIntegritySummaryV1.create(
            kind=DerivativeEventKindV1.INDEX_PRICE,
            coverage_start_ts=BASE_TS,
            coverage_end_ts=END_TS,
            checked_event_count=0,
            semantic_event_digest=event_stream_semantic_seed(
                DerivativeEventKindV1.INDEX_PRICE
            ),
        ),
        first_key=None,
        last_key=None,
        coverage_start_ts=BASE_TS,
        coverage_end_ts=END_TS,
        source_registry_ids=(event.header.source_ref.source_registry_id,),
        parent_raw_partition_sha256s=(
            event.header.source_ref.parent_artifact_sha256,
        ),
    )

    assert DerivativesEventStreamRefV1.from_dict(stream.to_dict()) == stream

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        replace(stream, raw_sha256="f" * 64)

    assert exc_info.value.code == "empty_event_stream_identity_invalid"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("size_bytes", True, "integer_out_of_bounds"),
        ("event_count", True, "integer_out_of_bounds"),
        ("raw_sha256", "A" * 64, "sha256_non_canonical"),
        ("relative_path", "../events.jsonl", "artifact_relative_path_invalid"),
    ],
)
def test_event_stream_rejects_noncanonical_wire_fields(
    field: str,
    value: object,
    code: str,
) -> None:
    payload = _stream_ref(DerivativeEventKindV1.MARK_PRICE).to_dict()
    payload[field] = value

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        DerivativesEventStreamRefV1.from_dict(payload)

    assert exc_info.value.code == code


def test_event_stream_rejects_boundary_outside_half_open_coverage() -> None:
    stream = _stream_ref(DerivativeEventKindV1.MARK_PRICE)
    outside = replace(stream.first_key, ts=END_TS)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        replace(
            stream,
            first_key=outside,
            last_key=outside,
        )

    assert exc_info.value.code == "event_stream_boundary_outside_coverage"


def test_event_stream_integrity_is_hash_bound_and_must_report_zero_failures() -> None:
    stream = _stream_ref(DerivativeEventKindV1.MARK_PRICE)
    restored = EventStreamIntegritySummaryV1.from_dict(
        stream.integrity.to_dict()
    )

    assert restored == stream.integrity
    assert (
        stream.semantic_identity_dict()["integrity"]["evidence_digest"]
        == stream.integrity.evidence_digest
    )

    payload = stream.to_dict()
    payload["integrity"]["results"]["gap_count"] = 1
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        DerivativesEventStreamRefV1.from_dict(payload)

    assert exc_info.value.code == "event_stream_integrity_failed"


def test_event_stream_wire_requires_json_arrays_for_lineage() -> None:
    payload = _stream_ref(DerivativeEventKindV1.INDEX_PRICE).to_dict()
    payload["source_registry_ids"] = tuple(payload["source_registry_ids"])

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        DerivativesEventStreamRefV1.from_dict(payload)

    assert exc_info.value.code == "event_stream_lineage_wire_invalid"


def test_event_stream_boundary_cardinality_uses_ts_and_source_sequence() -> None:
    stream = _stream_ref(DerivativeEventKindV1.INDEX_PRICE)
    assert stream.first_key is not None
    later = replace(
        stream.first_key,
        source_sequence=stream.first_key.source_sequence + 1,
        event_id="e" * 64,
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        replace(stream, last_key=later)

    assert exc_info.value.code == "event_stream_boundary_order_invalid"

    same_source_key = replace(stream.first_key, event_id="f" * 64)
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        replace(
            stream,
            event_count=2,
            integrity=EventStreamIntegritySummaryV1.create(
                kind=stream.kind,
                coverage_start_ts=stream.coverage_start_ts,
                coverage_end_ts=stream.coverage_end_ts,
                checked_event_count=2,
                semantic_event_digest=stream.semantic_event_digest,
            ),
            last_key=same_source_key,
        )

    assert exc_info.value.code == "event_stream_boundary_order_invalid"


def test_manifest_round_trip_preserves_fingerprint_dag() -> None:
    manifest = _manifest()
    restored = DerivativesEventSetManifestV1.from_dict(manifest.to_dict())
    manifest_bytes = canonical_typed_json_bytes(manifest.to_dict())
    ref = DerivativesEventSetRefV1(
        event_set_id=manifest.event_set_id,
        manifest_relative_path="event-sets/manifest.json",
        manifest_size_bytes=len(manifest_bytes),
        manifest_raw_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        raw_artifact_set_fingerprint=manifest.raw_artifact_set_fingerprint,
        semantic_event_set_fingerprint=manifest.semantic_event_set_fingerprint,
    )

    ref.validate_manifest(
        restored,
        observed_relative_path="event-sets/manifest.json",
        manifest_bytes=manifest_bytes,
    )

    assert restored == manifest
    assert derive_raw_artifact_set_fingerprint(manifest.streams) == (
        manifest.raw_artifact_set_fingerprint
    )
    assert restored.semantic_identity_dict() == manifest.semantic_identity_dict()


def test_event_set_ref_rejects_false_raw_manifest_identity() -> None:
    manifest = _manifest()
    manifest_bytes = canonical_typed_json_bytes(manifest.to_dict())
    ref = DerivativesEventSetRefV1(
        event_set_id=manifest.event_set_id,
        manifest_relative_path="event-sets/manifest.json",
        manifest_size_bytes=1,
        manifest_raw_sha256="0" * 64,
        raw_artifact_set_fingerprint=manifest.raw_artifact_set_fingerprint,
        semantic_event_set_fingerprint=manifest.semantic_event_set_fingerprint,
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        ref.validate_manifest(
            manifest,
            observed_relative_path="event-sets/manifest.json",
            manifest_bytes=manifest_bytes,
        )

    assert exc_info.value.code == "event_set_manifest_raw_mismatch"


def test_event_set_ref_rejects_noncanonical_bytes_and_stream_path_collision() -> None:
    manifest = _manifest()
    canonical_bytes = canonical_typed_json_bytes(manifest.to_dict())
    noncanonical_bytes = canonical_bytes + b"\n"
    ref = DerivativesEventSetRefV1(
        event_set_id=manifest.event_set_id,
        manifest_relative_path="event-sets/manifest.json",
        manifest_size_bytes=len(noncanonical_bytes),
        manifest_raw_sha256=hashlib.sha256(noncanonical_bytes).hexdigest(),
        raw_artifact_set_fingerprint=manifest.raw_artifact_set_fingerprint,
        semantic_event_set_fingerprint=manifest.semantic_event_set_fingerprint,
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        ref.validate_manifest(
            manifest,
            observed_relative_path="event-sets/manifest.json",
            manifest_bytes=noncanonical_bytes,
        )

    assert exc_info.value.code == "event_set_manifest_bytes_noncanonical"

    collision_path = manifest.streams[1].relative_path
    collision_ref = replace(
        ref,
        manifest_relative_path=collision_path,
        manifest_size_bytes=len(canonical_bytes),
        manifest_raw_sha256=hashlib.sha256(canonical_bytes).hexdigest(),
    )
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        collision_ref.validate_manifest(
            manifest,
            observed_relative_path=collision_path,
            manifest_bytes=canonical_bytes,
        )

    assert exc_info.value.code == "event_set_manifest_stream_path_collision"


def test_manifest_semantic_identity_excludes_stream_locator_and_raw_bytes() -> None:
    original = _manifest()
    relocated_streams = tuple(
        replace(
            stream,
            relative_path=f"alternate/{stream.kind.value}.jsonl",
            raw_sha256=("a" * 64 if index == 1 else stream.raw_sha256),
        )
        for index, stream in enumerate(original.streams)
    )
    relocated = _manifest(streams=relocated_streams)

    assert relocated.raw_artifact_set_fingerprint != (
        original.raw_artifact_set_fingerprint
    )
    assert relocated.semantic_event_set_fingerprint == (
        original.semantic_event_set_fingerprint
    )


def test_manifest_rejects_missing_stream_and_casefold_path_collision() -> None:
    payload = _manifest().to_dict()
    payload["streams"].pop(DerivativeEventKindV1.INDEX_PRICE.value)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        DerivativesEventSetManifestV1.from_dict(payload)

    assert exc_info.value.code == "event_stream_set_invalid"

    streams = list(_streams())
    streams[1] = replace(
        streams[1],
        relative_path=streams[0].relative_path.upper(),
    )
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        _manifest(streams=tuple(streams))

    assert exc_info.value.code == "event_stream_path_collision"


def test_manifest_rejects_wrong_stream_order_and_total_event_limit() -> None:
    streams = list(_streams())
    streams[0], streams[1] = streams[1], streams[0]

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        _manifest(streams=tuple(streams))

    assert exc_info.value.code == "event_stream_set_kind_mismatch"

    streams = list(_streams())
    streams[1] = _with_declared_count(
        streams[1],
        DERIVATIVES_EVENT_STREAM_MAX_EVENTS,
    )
    streams[2] = _with_declared_count(
        streams[2],
        DERIVATIVES_EVENT_STREAM_MAX_EVENTS,
    )
    streams[3] = _with_declared_count(streams[3], 1)
    assert sum(stream.event_count for stream in streams) > (
        DERIVATIVES_EVENT_SET_MAX_EVENTS
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        _manifest(streams=tuple(streams))

    assert exc_info.value.code == "event_set_event_limit_exceeded"


def test_manifest_closes_opening_catalog_against_phase05_stream() -> None:
    streams = list(_streams())
    streams[0] = _stream_ref(
        DerivativeEventKindV1.CONTRACT_TIER_EFFECTIVE
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        _manifest(streams=tuple(streams))

    assert exc_info.value.code == "snapshot_catalog_event_count_mismatch"


def test_manifest_requires_at_least_one_evaluation_bar() -> None:
    streams = list(_streams())
    streams[-1] = _stream_ref(DerivativeEventKindV1.BAR_CLOSE)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        _manifest(streams=tuple(streams))

    assert exc_info.value.code == "event_set_bar_stream_empty"


def test_manifest_rejects_scope_authority_and_unknown_key_drift() -> None:
    for mutate, expected_code in (
        (
            lambda payload: payload["scope"].__setitem__(
                "symbol",
                "ETH-USDT-SWAP",
            ),
            "event_set_scope_out_of_v1",
        ),
        (
            lambda payload: payload.__setitem__("capital_promotion_eligible", True),
            "synthetic_event_set_cannot_be_promotable",
        ),
        (
            lambda payload: payload.__setitem__("verified", True),
            "event_set_manifest_shape_invalid",
        ),
    ):
        payload = copy.deepcopy(_manifest().to_dict())
        mutate(payload)

        with pytest.raises(DerivativesBacktestContractError) as exc_info:
            DerivativesEventSetManifestV1.from_dict(payload)

        assert exc_info.value.code == expected_code


def test_manifest_requires_all_window_boundaries_to_be_utc_15m_aligned() -> None:
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        DerivativesEventSetManifestV1.create(
            event_set_id=EVENT_SET_ID,
            warmup_start_ts=BASE_TS + timedelta(minutes=1),
            evaluation_start_ts=EVALUATION_START_TS,
            end_ts=END_TS,
            dataset_version="synthetic-v1",
            transform_policy_id="rdp-transform",
            transform_policy_version="v1",
            transform_policy_fingerprint=TRANSFORM_FINGERPRINT,
            snapshot_catalog=_catalog(),
            streams=_streams(),
        )

    assert exc_info.value.code == "event_set_window_alignment_invalid"
    assert exc_info.value.field == "warmup_start_ts"


def test_manifest_create_normalizes_wrong_collection_types_to_contract_errors() -> None:
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        DerivativesEventSetManifestV1.create(
            event_set_id=EVENT_SET_ID,
            warmup_start_ts=BASE_TS,
            evaluation_start_ts=EVALUATION_START_TS,
            end_ts=END_TS,
            dataset_version="synthetic-v1",
            transform_policy_id="rdp-transform",
            transform_policy_version="v1",
            transform_policy_fingerprint=TRANSFORM_FINGERPRINT,
            snapshot_catalog=_catalog(),
            streams=list(_streams()),  # type: ignore[arg-type]
        )

    assert exc_info.value.code == "event_stream_set_invalid"


def test_manifest_rejects_oversized_snapshot_catalog_before_parsing_entries() -> None:
    payload = _manifest().to_dict()
    payload["snapshot_sets"] = [payload["snapshot_sets"][0]] * (
        DERIVATIVES_MAX_SNAPSHOT_CATALOG_ENTRIES + 1
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        DerivativesEventSetManifestV1.from_dict(payload)

    assert exc_info.value.code == "snapshot_catalog_invalid"


def test_manifest_component_and_final_byte_gates_are_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streams = _streams()
    catalog = _catalog()
    component_size = sum(
        len(canonical_typed_json_bytes(item.to_dict()))
        for item in (*catalog, *streams)
    )

    monkeypatch.setattr(
        event_set_module,
        "DERIVATIVES_MANIFEST_MAX_BYTES",
        component_size
        + event_set_module.DERIVATIVES_MANIFEST_ENVELOPE_RESERVE_BYTES
        - 1,
    )
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        _manifest(streams=streams, snapshot_catalog=catalog)
    assert exc_info.value.code == "event_set_manifest_size_exceeded"

    monkeypatch.setattr(
        event_set_module,
        "DERIVATIVES_MANIFEST_ENVELOPE_RESERVE_BYTES",
        0,
    )
    monkeypatch.setattr(
        event_set_module,
        "DERIVATIVES_MANIFEST_MAX_BYTES",
        component_size + 1,
    )
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        _manifest(streams=streams, snapshot_catalog=catalog)
    assert exc_info.value.code == "event_set_manifest_size_exceeded"


def test_snapshot_catalog_requires_exact_atomic_half_open_transition() -> None:
    switch_ts = BASE_TS + timedelta(minutes=30)
    opening = snapshot_refs()
    closing = replace(
        opening,
        instrument=replace(opening.instrument, effective_to=switch_ts),
    )
    incoming = replace(
        closing,
        instrument=replace(
            closing.instrument,
            snapshot_id="00000000-0000-4000-8000-000000000401",
            relative_path="snapshots/instrument-401.json",
            raw_sha256="a" * 64,
            semantic_sha256="b" * 64,
            source_seal_fingerprint="c" * 64,
            effective_from=switch_ts,
            effective_to=None,
        ),
    )
    valid_catalog = (
        SnapshotSetCatalogEntryV1(activation_ts=BASE_TS, refs=closing),
        SnapshotSetCatalogEntryV1(activation_ts=switch_ts, refs=incoming),
    )
    activation = ContractTierEffectiveEventV1.create(
        ts=switch_ts,
        source_sequence=1,
        source_ref=source_ref("contract"),
        snapshot_refs=incoming,
    )
    activation_boundary = EventStreamBoundaryKeyV1(
        ts=switch_ts,
        source_sequence=activation.header.source_sequence,
        event_id=activation.header.event_id,
    )
    activation_digest = update_event_stream_semantic_digest(
        event_stream_semantic_seed(
            DerivativeEventKindV1.CONTRACT_TIER_EFFECTIVE
        ),
        event_id=activation.header.event_id,
    )
    contract_stream = replace(
        _stream_ref(DerivativeEventKindV1.CONTRACT_TIER_EFFECTIVE),
        semantic_event_digest=activation_digest,
        integrity=EventStreamIntegritySummaryV1.create(
            kind=DerivativeEventKindV1.CONTRACT_TIER_EFFECTIVE,
            coverage_start_ts=BASE_TS,
            coverage_end_ts=END_TS,
            checked_event_count=1,
            semantic_event_digest=activation_digest,
        ),
        first_key=activation_boundary,
        last_key=activation_boundary,
    )
    transition_streams = list(_streams())
    transition_streams[0] = contract_stream

    assert _manifest(
        streams=tuple(transition_streams),
        snapshot_catalog=valid_catalog,
    ).snapshot_catalog == valid_catalog

    invalid_closing = replace(
        closing,
        instrument=replace(
            closing.instrument,
            effective_to=switch_ts + timedelta(microseconds=1),
        ),
    )
    invalid_catalog = (
        SnapshotSetCatalogEntryV1(activation_ts=BASE_TS, refs=invalid_closing),
        SnapshotSetCatalogEntryV1(activation_ts=switch_ts, refs=incoming),
    )
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        _manifest(
            streams=tuple(transition_streams),
            snapshot_catalog=invalid_catalog,
        )

    assert exc_info.value.code == "snapshot_transition_window_invalid"

    colliding_incoming = replace(
        incoming,
        instrument=replace(
            incoming.instrument,
            relative_path=closing.instrument.relative_path,
        ),
    )
    colliding_catalog = (
        SnapshotSetCatalogEntryV1(activation_ts=BASE_TS, refs=closing),
        SnapshotSetCatalogEntryV1(
            activation_ts=switch_ts,
            refs=colliding_incoming,
        ),
    )
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        _manifest(
            streams=tuple(transition_streams),
            snapshot_catalog=colliding_catalog,
        )

    assert exc_info.value.code == "snapshot_catalog_path_collision"


def test_stream_cursor_validates_empty_partial_and_completed_boundaries() -> None:
    original = _stream_ref(DerivativeEventKindV1.MARK_PRICE)
    first = original.first_key
    assert first is not None
    second = EventStreamBoundaryKeyV1(
        ts=first.ts + timedelta(minutes=1),
        source_sequence=first.source_sequence + 1,
        event_id="e" * 64,
    )
    first_digest = update_event_stream_semantic_digest(
        event_stream_semantic_seed(original.kind),
        event_id=first.event_id,
    )
    complete_digest = update_event_stream_semantic_digest(
        first_digest,
        event_id=second.event_id,
    )
    stream = replace(
        original,
        size_bytes=256,
        raw_sha256="d" * 64,
        event_count=2,
        semantic_event_digest=complete_digest,
        integrity=EventStreamIntegritySummaryV1.create(
            kind=original.kind,
            coverage_start_ts=original.coverage_start_ts,
            coverage_end_ts=original.coverage_end_ts,
            checked_event_count=2,
            semantic_event_digest=complete_digest,
        ),
        last_key=second,
    )

    empty = DerivativesEventStreamCursorV1.empty(stream)
    empty.validate_against(stream)

    partial = DerivativesEventStreamCursorV1(
        stream_fingerprint=stream.fingerprint,
        next_byte_offset=128,
        committed_event_count=1,
        raw_prefix_sha256="c" * 64,
        semantic_prefix_sha256=first_digest,
        last_committed_key=first,
    )
    partial.validate_against(stream)

    completed = DerivativesEventStreamCursorV1(
        stream_fingerprint=stream.fingerprint,
        next_byte_offset=stream.size_bytes,
        committed_event_count=stream.event_count,
        raw_prefix_sha256=stream.raw_sha256,
        semantic_prefix_sha256=stream.semantic_event_digest,
        last_committed_key=stream.last_key,
    )
    completed.validate_against(stream)
    assert DerivativesEventStreamCursorV1.from_dict(completed.to_dict()) == completed

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        replace(partial, next_byte_offset=stream.size_bytes).validate_against(stream)

    assert exc_info.value.code == "partial_event_stream_cursor_mismatch"


def test_stream_cursor_rejects_wrong_empty_and_completed_digests() -> None:
    stream = _stream_ref(DerivativeEventKindV1.TRADABLE)
    empty = DerivativesEventStreamCursorV1.empty(stream)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        replace(empty, semantic_prefix_sha256="f" * 64).validate_against(stream)

    assert exc_info.value.code == "empty_stream_cursor_mismatch"

    completed = DerivativesEventStreamCursorV1(
        stream_fingerprint=stream.fingerprint,
        next_byte_offset=stream.size_bytes,
        committed_event_count=stream.event_count,
        raw_prefix_sha256="f" * 64,
        semantic_prefix_sha256=stream.semantic_event_digest,
        last_committed_key=stream.last_key,
    )
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        completed.validate_against(stream)

    assert exc_info.value.code == "completed_event_stream_cursor_mismatch"
