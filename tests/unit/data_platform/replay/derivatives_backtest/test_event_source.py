from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import aats.data_platform.replay.derivatives_backtest.event_source as source_module
from aats.data_platform.governance.typed_json_identity import (
    canonical_typed_json_bytes,
    typed_json_sha256,
)
from aats.data_platform.replay.derivatives_backtest.contracts import (
    DerivativesBacktestContractError,
)
from aats.data_platform.replay.derivatives_backtest.event_set import (
    DerivativesEventSetManifestV1,
    DerivativesEventSetRefV1,
    DerivativesEventStreamCursorV1,
    DerivativesEventStreamRefV1,
    EventStreamBoundaryKeyV1,
    EventStreamIntegritySummaryV1,
    SnapshotSetCatalogEntryV1,
    event_stream_semantic_seed,
    update_event_stream_semantic_digest,
)
from aats.data_platform.replay.derivatives_backtest.event_source import (
    preflight_non_promotable_derivatives_event_source,
)
from aats.data_platform.replay.derivatives_backtest.events import (
    EXPECTED_EVENT_STREAM_ID_V1,
    BarCloseEventV1,
    DerivativeEventKindV1,
    IndexPriceEventV1,
    MarkPriceEventV1,
    SourceRecordRefV1,
    TradableEventV1,
)
from aats.data_platform.replay.derivatives_backtest.snapshot_loader import (
    DERIVATIVES_SNAPSHOT_ENVELOPE_SCHEMA,
)
from aats.data_platform.replay.derivatives_backtest.snapshot_refs import (
    DerivativesSnapshotRefsV1,
    ImmutableSnapshotRefV1,
    SnapshotKindV1,
)
from aats.domain.instrument_contract import InstrumentContract
from aats.domain.instrument_contract_snapshot import InstrumentContractSnapshot


BASE_TS = datetime(2026, 1, 2, 0, 15, tzinfo=timezone.utc)
EVALUATION_TS = BASE_TS + timedelta(minutes=15)
END_TS = BASE_TS + timedelta(hours=1)
EVENT_SET_ID = "00000000-0000-4000-8000-000000000401"


def _source_ref(kind: DerivativeEventKindV1) -> SourceRecordRefV1:
    return SourceRecordRefV1(
        stream_id=EXPECTED_EVENT_STREAM_ID_V1[kind],
        source_registry_id="00000000-0000-4000-8000-000000000099",
        parent_artifact_sha256="a" * 64,
        source_record_sha256="b" * 64,
    )


def _instrument_snapshot(source_schema: str) -> InstrumentContractSnapshot:
    return InstrumentContractSnapshot(
        venue="OKX",
        contract=InstrumentContract(
            symbol="BTC-USDT-SWAP",
            instrument_type="SWAP",
            contract_type="linear",
            base_currency="BTC",
            quote_currency="USDT",
            settle_currency="USDT",
            contract_value=Decimal("0.01"),
            contract_multiplier=Decimal("1"),
            contract_value_currency="BTC",
            lot_size=Decimal("1"),
            min_size=Decimal("1"),
            tick_size=Decimal("0.1"),
        ),
        observed_at=BASE_TS,
        effective_from=BASE_TS,
        effective_to=None,
        evidence_kind="authoritative_history",
        source_locator="synthetic://instrument",
        source_schema=source_schema,
        source_payload_sha256="f" * 64,
    )


def _snapshot_payload(
    kind: SnapshotKindV1,
    source_schema: str,
    *,
    funding_cadence_seconds: int,
) -> dict:
    if kind is SnapshotKindV1.INSTRUMENT:
        return _instrument_snapshot(source_schema).to_dict()
    if kind is SnapshotKindV1.POSITION_TIER:
        return {
            "tier_id": 1,
            "minimum_notional_inclusive": "0",
            "maximum_notional_inclusive": "1e6",
            "maximum_leverage": "1e2",
            "maintenance_margin_rate": "5e-3",
            "maintenance_margin_deduction": "0",
            "liquidation_fee_rate": "25e-4",
        }
    if kind is SnapshotKindV1.EXECUTION_FEE:
        return {
            "account_fee_tier_id": "okx-regular-lv1",
            "maker_fee_rate": "-2e-4",
            "taker_fee_rate": "5e-4",
            "fee_asset": "USDT",
        }
    return {
        "minimum_rate_inclusive": "-1e-2",
        "maximum_rate_inclusive": "1e-2",
        "schedule_id": "00000000-0000-4000-8000-000000000004",
        "cadence_seconds": funding_cadence_seconds,
        "settlement_anchor_ts": "2026-01-02T00:00:00.000000Z",
    }


def _build_snapshot_set(
    root: Path,
    *,
    funding_cadence_seconds: int = 28_800,
) -> DerivativesSnapshotRefsV1:
    refs: list[ImmutableSnapshotRefV1] = []
    for ordinal, kind in enumerate(SnapshotKindV1, start=1):
        source_schema = f"aats.synthetic.{kind.value}.v1"
        snapshot_id = f"00000000-0000-4000-8000-{ordinal:012d}"
        envelope = {
            "schema": DERIVATIVES_SNAPSHOT_ENVELOPE_SCHEMA,
            "kind": kind.value,
            "payload_schema": (
                f"derivatives-{kind.value.replace('_', '-')}-snapshot-payload/v1"
            ),
            "venue": "OKX",
            "symbol": "BTC-USDT-SWAP",
            "instrument_type": "SWAP",
            "contract_type": "linear",
            "settle_currency": "USDT",
            "margin_mode": "isolated",
            "position_mode": "single_position",
            "snapshot_id": snapshot_id,
            "source_registry_id": (
                f"00000000-0000-4000-8000-{ordinal + 10:012d}"
            ),
            "source_seal_fingerprint": f"{ordinal + 8:x}" * 64,
            "source_schema": source_schema,
            "effective_window": {
                "start": "2026-01-02T00:15:00.000000Z",
                "end": None,
            },
            "authority_status": "synthetic_test_only",
            "payload": _snapshot_payload(
                kind,
                source_schema,
                funding_cadence_seconds=funding_cadence_seconds,
            ),
        }
        raw = canonical_typed_json_bytes(envelope)
        relative_path = f"snapshots/{kind.value}.json"
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        refs.append(
            ImmutableSnapshotRefV1(
                kind=kind,
                snapshot_id=snapshot_id,
                relative_path=relative_path,
                raw_sha256=hashlib.sha256(raw).hexdigest(),
                size_bytes=len(raw),
                semantic_sha256=typed_json_sha256(envelope),
                source_registry_id=envelope["source_registry_id"],
                source_seal_fingerprint=envelope["source_seal_fingerprint"],
                source_schema=source_schema,
                effective_from=BASE_TS,
                effective_to=None,
            )
        )
    return DerivativesSnapshotRefsV1(
        instrument=refs[0],
        position_tier=refs[1],
        execution_fee=refs[2],
        funding_schedule=refs[3],
    )


def _events(refs: DerivativesSnapshotRefsV1) -> dict[DerivativeEventKindV1, tuple]:
    bars = tuple(
        BarCloseEventV1.create(
            ts=ts,
            source_sequence=ordinal,
            source_ref=_source_ref(DerivativeEventKindV1.BAR_CLOSE),
            bar_start_ts=ts - timedelta(minutes=15),
            bar_end_ts=ts,
            open_price=Decimal("50000"),
            high_price=Decimal("50010"),
            low_price=Decimal("49990"),
            close_price=Decimal("50001"),
            volume_contracts=Decimal("100"),
            feature_ref=SourceRecordRefV1(
                stream_id="bar-features",
                source_registry_id="00000000-0000-4000-8000-000000000098",
                parent_artifact_sha256="c" * 64,
                source_record_sha256=f"{ordinal:x}" * 64,
            ),
        )
        for ordinal, ts in enumerate(
            (BASE_TS + timedelta(minutes=15 * index) for index in range(1, 4)),
            start=1,
        )
    )
    return {
        DerivativeEventKindV1.CONTRACT_TIER_EFFECTIVE: (),
        DerivativeEventKindV1.INDEX_PRICE: (
            IndexPriceEventV1.create(
                ts=BASE_TS,
                source_sequence=1,
                source_ref=_source_ref(DerivativeEventKindV1.INDEX_PRICE),
                price=Decimal("50000"),
            ),
        ),
        DerivativeEventKindV1.MARK_PRICE: (
            MarkPriceEventV1.create(
                ts=BASE_TS,
                source_sequence=1,
                source_ref=_source_ref(DerivativeEventKindV1.MARK_PRICE),
                price=Decimal("50001"),
            ),
        ),
        DerivativeEventKindV1.FUNDING_SETTLEMENT: (),
        DerivativeEventKindV1.TRADABLE: (
            TradableEventV1.create(
                ts=BASE_TS,
                source_sequence=1,
                source_ref=_source_ref(DerivativeEventKindV1.TRADABLE),
                reference_price=Decimal("50002"),
                available_contracts=Decimal("2"),
            ),
        ),
        DerivativeEventKindV1.BAR_CLOSE: bars,
    }


def _stream(
    kind: DerivativeEventKindV1,
    events: tuple,
    *,
    raw_override: bytes | None = None,
    registry_ids: tuple[str, ...] | None = None,
    parents: tuple[str, ...] | None = None,
) -> tuple[DerivativesEventStreamRefV1, bytes]:
    raw = (
        b"".join(canonical_typed_json_bytes(event.to_dict()) + b"\n" for event in events)
        if raw_override is None
        else raw_override
    )
    semantic = event_stream_semantic_seed(kind)
    for event in events:
        semantic = update_event_stream_semantic_digest(
            semantic,
            event_id=event.header.event_id,
        )
    first = (
        None
        if not events
        else EventStreamBoundaryKeyV1(
            ts=events[0].header.ts,
            source_sequence=events[0].header.source_sequence,
            event_id=events[0].header.event_id,
        )
    )
    last = (
        None
        if not events
        else EventStreamBoundaryKeyV1(
            ts=events[-1].header.ts,
            source_sequence=events[-1].header.source_sequence,
            event_id=events[-1].header.event_id,
        )
    )
    resolved_registries = (
        tuple(
            sorted(
                {event.header.source_ref.source_registry_id for event in events}
            )
        )
        if registry_ids is None
        else registry_ids
    )
    resolved_parents = (
        tuple(
            sorted(
                {event.header.source_ref.parent_artifact_sha256 for event in events}
            )
        )
        if parents is None
        else parents
    )
    return (
        DerivativesEventStreamRefV1(
            kind=kind,
            stream_id=EXPECTED_EVENT_STREAM_ID_V1[kind],
            relative_path=f"events/{kind.value}.jsonl",
            size_bytes=len(raw),
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            event_count=len(events),
            semantic_event_digest=semantic,
            integrity=EventStreamIntegritySummaryV1.create(
                kind=kind,
                coverage_start_ts=BASE_TS,
                coverage_end_ts=END_TS,
                checked_event_count=len(events),
                semantic_event_digest=semantic,
            ),
            first_key=first,
            last_key=last,
            coverage_start_ts=BASE_TS,
            coverage_end_ts=END_TS,
            source_registry_ids=resolved_registries,
            parent_raw_partition_sha256s=resolved_parents,
        ),
        raw,
    )


def _publish_event_set(
    root: Path,
    refs: DerivativesSnapshotRefsV1,
    events_by_kind: dict[DerivativeEventKindV1, tuple],
    *,
    stream_overrides: dict[
        DerivativeEventKindV1,
        tuple[DerivativesEventStreamRefV1, bytes],
    ]
    | None = None,
) -> tuple[DerivativesEventSetRefV1, DerivativesEventSetManifestV1]:
    stream_overrides = stream_overrides or {}
    streams: list[DerivativesEventStreamRefV1] = []
    for kind in DerivativeEventKindV1:
        stream, raw = stream_overrides.get(
            kind,
            _stream(kind, events_by_kind[kind]),
        )
        path = root / stream.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        streams.append(stream)
    manifest = DerivativesEventSetManifestV1.create(
        event_set_id=EVENT_SET_ID,
        warmup_start_ts=BASE_TS,
        evaluation_start_ts=EVALUATION_TS,
        end_ts=END_TS,
        dataset_version="synthetic-v1",
        transform_policy_id="rdp-transform",
        transform_policy_version="v1",
        transform_policy_fingerprint="9" * 64,
        snapshot_catalog=(
            SnapshotSetCatalogEntryV1(activation_ts=BASE_TS, refs=refs),
        ),
        streams=tuple(streams),
    )
    raw_manifest = canonical_typed_json_bytes(manifest.to_dict())
    manifest_path = root / "event-sets" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(raw_manifest)
    return (
        DerivativesEventSetRefV1(
            event_set_id=EVENT_SET_ID,
            manifest_relative_path="event-sets/manifest.json",
            manifest_size_bytes=len(raw_manifest),
            manifest_raw_sha256=hashlib.sha256(raw_manifest).hexdigest(),
            raw_artifact_set_fingerprint=manifest.raw_artifact_set_fingerprint,
            semantic_event_set_fingerprint=manifest.semantic_event_set_fingerprint,
        ),
        manifest,
    )


def _build(
    root: Path,
    *,
    funding_cadence_seconds: int = 28_800,
    stream_overrides: dict[
        DerivativeEventKindV1,
        tuple[DerivativesEventStreamRefV1, bytes],
    ]
    | None = None,
):
    refs = _build_snapshot_set(
        root,
        funding_cadence_seconds=funding_cadence_seconds,
    )
    events_by_kind = _events(refs)
    event_set_ref, manifest = _publish_event_set(
        root,
        refs,
        events_by_kind,
        stream_overrides=stream_overrides,
    )
    return event_set_ref, manifest, events_by_kind


def _preflight(root: Path, event_set_ref: DerivativesEventSetRefV1):
    return preflight_non_promotable_derivatives_event_source(
        event_set_ref,
        event_root=root,
        snapshot_root=root,
    )


def test_preflight_and_second_pass_recompute_all_stream_identities(
    tmp_path: Path,
) -> None:
    event_set_ref, manifest, events_by_kind = _build(tmp_path)

    source = _preflight(tmp_path, event_set_ref)
    read_pass = source.start_verification_pass()
    observed = {
        kind: tuple(record.event for record in read_pass.open_stream(kind))
        for kind in DerivativeEventKindV1
    }
    completed = read_pass.finish()

    assert source.manifest == manifest
    assert source.capital_promotion_eligible is False
    assert source.economic_mutation_allowed is False
    assert read_pass.economic_mutation_allowed is False
    assert observed == events_by_kind
    assert completed == source.preflight_cursors
    assert all(cursor.next_byte_offset == stream.size_bytes for cursor, stream in zip(completed, manifest.streams))


def test_restart_cursor_replays_prefix_and_yields_only_suffix(tmp_path: Path) -> None:
    event_set_ref, manifest, events_by_kind = _build(tmp_path)
    source = _preflight(tmp_path, event_set_ref)
    interrupted = source.start_verification_pass()
    bar_reader = interrupted.open_stream(DerivativeEventKindV1.BAR_CLOSE)
    first = next(bar_reader)
    bar_reader.close()  # type: ignore[attr-defined]
    cursors = {
        stream.kind: DerivativesEventStreamCursorV1.empty(stream)
        for stream in manifest.streams
    }
    cursors[DerivativeEventKindV1.BAR_CLOSE] = first.cursor_after

    resumed = source.start_verification_pass(cursors=cursors)
    observed = {
        kind: tuple(record.event for record in resumed.open_stream(kind))
        for kind in DerivativeEventKindV1
    }

    assert observed[DerivativeEventKindV1.BAR_CLOSE] == events_by_kind[
        DerivativeEventKindV1.BAR_CLOSE
    ][1:]
    assert resumed.finish() == source.preflight_cursors


def test_second_pass_rejects_same_byte_path_replacement(tmp_path: Path) -> None:
    event_set_ref, manifest, _events_by_kind = _build(tmp_path)
    source = _preflight(tmp_path, event_set_ref)
    mark = next(
        stream
        for stream in manifest.streams
        if stream.kind is DerivativeEventKindV1.MARK_PRICE
    )
    path = tmp_path / mark.relative_path
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(path.read_bytes())
    os.replace(replacement, path)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        source.start_verification_pass()

    assert exc_info.value.code == "event_set_identity_changed"


def test_second_pass_rejects_same_byte_snapshot_replacement(
    tmp_path: Path,
) -> None:
    event_set_ref, manifest, _events_by_kind = _build(tmp_path)
    source = _preflight(tmp_path, event_set_ref)
    relative_path = manifest.snapshot_catalog[0].refs.instrument.relative_path
    path = tmp_path / relative_path
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(path.read_bytes())
    os.replace(replacement, path)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        source.start_verification_pass()

    assert exc_info.value.code == "event_set_identity_changed"


def test_finish_rejects_same_byte_stream_replacement_after_drain(
    tmp_path: Path,
) -> None:
    event_set_ref, manifest, _events_by_kind = _build(tmp_path)
    source = _preflight(tmp_path, event_set_ref)
    read_pass = source.start_verification_pass()
    for kind in DerivativeEventKindV1:
        tuple(read_pass.open_stream(kind))
    mark = next(
        stream
        for stream in manifest.streams
        if stream.kind is DerivativeEventKindV1.MARK_PRICE
    )
    path = tmp_path / mark.relative_path
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(path.read_bytes())
    os.replace(replacement, path)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        read_pass.finish()

    assert exc_info.value.code == "event_set_identity_changed"


def test_finish_rejects_same_byte_snapshot_replacement_after_start(
    tmp_path: Path,
) -> None:
    event_set_ref, manifest, _events_by_kind = _build(tmp_path)
    source = _preflight(tmp_path, event_set_ref)
    read_pass = source.start_verification_pass()
    for kind in DerivativeEventKindV1:
        tuple(read_pass.open_stream(kind))
    relative_path = manifest.snapshot_catalog[0].refs.instrument.relative_path
    path = tmp_path / relative_path
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(path.read_bytes())
    os.replace(replacement, path)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        read_pass.finish()

    assert exc_info.value.code == "event_set_identity_changed"


def test_second_pass_detects_replacement_while_descriptor_is_open(
    tmp_path: Path,
) -> None:
    event_set_ref, manifest, _events_by_kind = _build(tmp_path)
    source = _preflight(tmp_path, event_set_ref)
    read_pass = source.start_verification_pass()
    reader = read_pass.open_stream(DerivativeEventKindV1.MARK_PRICE)
    next(reader)
    mark = next(
        stream
        for stream in manifest.streams
        if stream.kind is DerivativeEventKindV1.MARK_PRICE
    )
    path = tmp_path / mark.relative_path
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(path.read_bytes())
    try:
        os.replace(replacement, path)
    except PermissionError:
        reader.close()  # type: ignore[attr-defined]
        pytest.skip("Windows holds the source descriptor against replacement")

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        tuple(reader)

    assert exc_info.value.code == "event_set_identity_changed"


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (lambda raw: raw.replace(b"\n", b"\r\n"), "event_stream_jsonl_invalid"),
        (lambda raw: raw.removesuffix(b"\n"), "event_stream_final_lf_missing"),
        (lambda raw: b"\n" + raw, "event_stream_jsonl_invalid"),
        (lambda raw: b"\xef\xbb\xbf" + raw, "event_stream_jsonl_invalid"),
    ],
)
def test_preflight_rejects_noncanonical_jsonl_framing(
    tmp_path: Path,
    mutate,
    expected_code: str,
) -> None:
    refs = _build_snapshot_set(tmp_path)
    events_by_kind = _events(refs)
    mark_events = events_by_kind[DerivativeEventKindV1.MARK_PRICE]
    original, raw = _stream(DerivativeEventKindV1.MARK_PRICE, mark_events)
    invalid_raw = mutate(raw)
    invalid = replace(
        original,
        size_bytes=len(invalid_raw),
        raw_sha256=hashlib.sha256(invalid_raw).hexdigest(),
    )
    event_set_ref, _manifest = _publish_event_set(
        tmp_path,
        refs,
        events_by_kind,
        stream_overrides={
            DerivativeEventKindV1.MARK_PRICE: (invalid, invalid_raw),
        },
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        _preflight(tmp_path, event_set_ref)

    assert exc_info.value.code == expected_code


def test_preflight_rejects_duplicate_keys_even_when_raw_hash_matches(
    tmp_path: Path,
) -> None:
    refs = _build_snapshot_set(tmp_path)
    events_by_kind = _events(refs)
    mark_events = events_by_kind[DerivativeEventKindV1.MARK_PRICE]
    original, raw = _stream(DerivativeEventKindV1.MARK_PRICE, mark_events)
    invalid_raw = raw.replace(b'{"event_id":', b'{"event_id":"0","event_id":', 1)
    invalid = replace(
        original,
        size_bytes=len(invalid_raw),
        raw_sha256=hashlib.sha256(invalid_raw).hexdigest(),
    )
    event_set_ref, _manifest = _publish_event_set(
        tmp_path,
        refs,
        events_by_kind,
        stream_overrides={
            DerivativeEventKindV1.MARK_PRICE: (invalid, invalid_raw),
        },
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        _preflight(tmp_path, event_set_ref)

    assert exc_info.value.code == "event_stream_json_invalid"


def test_preflight_rejects_singleton_timestamp_cardinality(tmp_path: Path) -> None:
    refs = _build_snapshot_set(tmp_path)
    events_by_kind = _events(refs)
    second = TradableEventV1.create(
        ts=BASE_TS,
        source_sequence=2,
        source_ref=_source_ref(DerivativeEventKindV1.TRADABLE),
        reference_price=Decimal("50003"),
        available_contracts=Decimal("3"),
    )
    events_by_kind[DerivativeEventKindV1.TRADABLE] += (second,)
    event_set_ref, _manifest = _publish_event_set(
        tmp_path,
        refs,
        events_by_kind,
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        _preflight(tmp_path, event_set_ref)

    assert exc_info.value.code == "event_stream_singleton_timestamp_invalid"


def test_preflight_rejects_bar_lattice_gap(tmp_path: Path) -> None:
    refs = _build_snapshot_set(tmp_path)
    events_by_kind = _events(refs)
    bars = events_by_kind[DerivativeEventKindV1.BAR_CLOSE]
    events_by_kind[DerivativeEventKindV1.BAR_CLOSE] = (bars[0], bars[2])
    event_set_ref, _manifest = _publish_event_set(
        tmp_path,
        refs,
        events_by_kind,
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        _preflight(tmp_path, event_set_ref)

    assert exc_info.value.code == "event_stream_gap_detected"


def test_preflight_rejects_event_lineage_outside_declared_seal(
    tmp_path: Path,
) -> None:
    refs = _build_snapshot_set(tmp_path)
    events_by_kind = _events(refs)
    mark = _stream(
        DerivativeEventKindV1.MARK_PRICE,
        events_by_kind[DerivativeEventKindV1.MARK_PRICE],
        parents=("d" * 64,),
    )
    event_set_ref, _manifest = _publish_event_set(
        tmp_path,
        refs,
        events_by_kind,
        stream_overrides={DerivativeEventKindV1.MARK_PRICE: mark},
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        _preflight(tmp_path, event_set_ref)

    assert exc_info.value.code == "event_stream_lineage_mismatch"


@pytest.mark.parametrize("extra_kind", ["registry", "parent"])
def test_preflight_rejects_overdeclared_event_lineage(
    tmp_path: Path,
    extra_kind: str,
) -> None:
    refs = _build_snapshot_set(tmp_path)
    events_by_kind = _events(refs)
    original, raw = _stream(
        DerivativeEventKindV1.MARK_PRICE,
        events_by_kind[DerivativeEventKindV1.MARK_PRICE],
    )
    if extra_kind == "registry":
        updated = replace(
            original,
            source_registry_ids=tuple(
                sorted(
                    (
                        *original.source_registry_ids,
                        "00000000-0000-4000-8000-000000000098",
                    )
                )
            ),
        )
    else:
        updated = replace(
            original,
            parent_raw_partition_sha256s=tuple(
                sorted((*original.parent_raw_partition_sha256s, "d" * 64))
            ),
        )
    event_set_ref, _manifest = _publish_event_set(
        tmp_path,
        refs,
        events_by_kind,
        stream_overrides={
            DerivativeEventKindV1.MARK_PRICE: (updated, raw),
        },
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        _preflight(tmp_path, event_set_ref)

    assert exc_info.value.code == "event_stream_identity_mismatch"


def test_snapshot_aware_funding_preflight_rejects_missing_settlements(
    tmp_path: Path,
) -> None:
    event_set_ref, _manifest, _events_by_kind = _build(
        tmp_path,
        funding_cadence_seconds=900,
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        _preflight(tmp_path, event_set_ref)

    assert exc_info.value.code == "funding_event_missing"


def test_manifest_raw_size_gate_runs_before_json_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_set_ref, _manifest, _events_by_kind = _build(tmp_path)
    monkeypatch.setattr(source_module, "DERIVATIVES_MANIFEST_MAX_BYTES", 1)

    def forbidden_decode(*_args, **_kwargs):
        raise AssertionError("manifest bytes must be gated before decode")

    monkeypatch.setattr(source_module, "decode_strict_json_artifact", forbidden_decode)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        _preflight(tmp_path, event_set_ref)

    assert exc_info.value.code == "resource_limit_exceeded"


def test_unique_snapshot_materialization_has_an_aggregate_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_set_ref, _manifest, _events_by_kind = _build(tmp_path)
    monkeypatch.setattr(
        source_module,
        "DERIVATIVES_EVENT_SOURCE_MAX_UNIQUE_SNAPSHOT_BYTES",
        1,
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        _preflight(tmp_path, event_set_ref)

    assert exc_info.value.code == "resource_limit_exceeded"


def test_preflight_rejects_case_aliased_path(tmp_path: Path) -> None:
    event_set_ref, manifest, _events_by_kind = _build(tmp_path)
    mark_index = next(
        index
        for index, stream in enumerate(manifest.streams)
        if stream.kind is DerivativeEventKindV1.MARK_PRICE
    )
    streams = list(manifest.streams)
    streams[mark_index] = replace(
        streams[mark_index],
        relative_path="Events/mark_price.jsonl",
    )
    updated = DerivativesEventSetManifestV1.create(
        event_set_id=manifest.event_set_id,
        warmup_start_ts=manifest.warmup_start_ts,
        evaluation_start_ts=manifest.evaluation_start_ts,
        end_ts=manifest.end_ts,
        dataset_version=manifest.dataset_version,
        transform_policy_id=manifest.transform_policy_id,
        transform_policy_version=manifest.transform_policy_version,
        transform_policy_fingerprint=manifest.transform_policy_fingerprint,
        snapshot_catalog=manifest.snapshot_catalog,
        streams=tuple(streams),
    )
    raw = canonical_typed_json_bytes(updated.to_dict())
    (tmp_path / event_set_ref.manifest_relative_path).write_bytes(raw)
    updated_ref = replace(
        event_set_ref,
        manifest_size_bytes=len(raw),
        manifest_raw_sha256=hashlib.sha256(raw).hexdigest(),
        raw_artifact_set_fingerprint=updated.raw_artifact_set_fingerprint,
        semantic_event_set_fingerprint=updated.semantic_event_set_fingerprint,
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        _preflight(tmp_path, updated_ref)

    assert exc_info.value.code == "event_source_path_case_mismatch"


def test_partial_cursor_prefix_tamper_is_rejected(tmp_path: Path) -> None:
    event_set_ref, manifest, _events_by_kind = _build(tmp_path)
    source = _preflight(tmp_path, event_set_ref)
    first_pass = source.start_verification_pass()
    reader = first_pass.open_stream(DerivativeEventKindV1.BAR_CLOSE)
    first = next(reader)
    reader.close()  # type: ignore[attr-defined]
    cursors = {
        stream.kind: DerivativesEventStreamCursorV1.empty(stream)
        for stream in manifest.streams
    }
    cursors[DerivativeEventKindV1.BAR_CLOSE] = replace(
        first.cursor_after,
        raw_prefix_sha256="f" * 64,
    )
    resumed = source.start_verification_pass(cursors=cursors)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        tuple(resumed.open_stream(DerivativeEventKindV1.BAR_CLOSE))

    assert exc_info.value.code == "event_stream_cursor_mismatch"
