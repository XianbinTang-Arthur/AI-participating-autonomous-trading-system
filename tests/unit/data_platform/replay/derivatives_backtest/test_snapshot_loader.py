from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from aats.data_platform.governance.typed_json_identity import (
    canonical_typed_json_bytes,
    typed_json_sha256,
)
from aats.data_platform.replay.derivatives_backtest.contracts import (
    DerivativesBacktestContractError,
    ExecutionFeeScheduleV1,
)
from aats.data_platform.replay.derivatives_backtest.snapshot_loader import (
    DERIVATIVES_SNAPSHOT_ENVELOPE_SCHEMA,
    load_non_promotable_derivatives_snapshot_set,
)
from aats.data_platform.replay.derivatives_backtest.snapshot_refs import (
    DerivativesSnapshotRefsV1,
    ImmutableSnapshotRefV1,
    SnapshotKindV1,
)
from aats.domain.instrument_contract import InstrumentContract
from aats.domain.instrument_contract_snapshot import InstrumentContractSnapshot
from tests.unit.data_platform.replay.derivatives_backtest._event_helpers import (
    BASE_TS,
)


def instrument_snapshot(
    source_schema: str,
    *,
    evidence_kind: str = "authoritative_history",
) -> InstrumentContractSnapshot:
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
        evidence_kind=evidence_kind,  # type: ignore[arg-type]
        source_locator="synthetic://instrument",
        source_schema=source_schema,
        source_payload_sha256="f" * 64,
    )


def snapshot_payload(kind: SnapshotKindV1, source_schema: str):
    if kind is SnapshotKindV1.INSTRUMENT:
        return instrument_snapshot(source_schema).to_dict()
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
        "cadence_seconds": 28800,
        "settlement_anchor_ts": "2026-01-02T00:00:00.000000Z",
    }


def build_snapshot_set(root: Path):
    refs: list[ImmutableSnapshotRefV1] = []
    decoded_by_kind: dict[SnapshotKindV1, dict] = {}
    for ordinal, kind in enumerate(SnapshotKindV1, start=1):
        source_schema = f"aats.synthetic.{kind.value}.v1"
        envelope = {
            "schema": DERIVATIVES_SNAPSHOT_ENVELOPE_SCHEMA,
            "kind": kind.value,
            "payload_schema": f"derivatives-{kind.value.replace('_', '-')}-snapshot-payload/v1",
            "venue": "OKX",
            "symbol": "BTC-USDT-SWAP",
            "instrument_type": "SWAP",
            "contract_type": "linear",
            "settle_currency": "USDT",
            "margin_mode": "isolated",
            "position_mode": "single_position",
            "snapshot_id": f"00000000-0000-4000-8000-{ordinal:012d}",
            "source_registry_id": f"00000000-0000-4000-8000-{ordinal + 10:012d}",
            "source_seal_fingerprint": f"{ordinal + 8:x}" * 64,
            "source_schema": source_schema,
            "effective_window": {
                "start": "2026-01-02T00:15:00.000000Z",
                "end": None,
            },
            "authority_status": "synthetic_test_only",
            "payload": snapshot_payload(kind, source_schema),
        }
        raw = canonical_typed_json_bytes(envelope)
        relative_path = f"snapshots/{kind.value}.json"
        path = root / "snapshots" / f"{kind.value}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        refs.append(
            ImmutableSnapshotRefV1(
                kind=kind,
                snapshot_id=envelope["snapshot_id"],
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
        decoded_by_kind[kind] = envelope
    return (
        DerivativesSnapshotRefsV1(
            instrument=refs[0],
            position_tier=refs[1],
            execution_fee=refs[2],
            funding_schedule=refs[3],
        ),
        decoded_by_kind,
    )


def replace_ref(
    refs: DerivativesSnapshotRefsV1,
    kind: SnapshotKindV1,
    updated: ImmutableSnapshotRefV1,
) -> DerivativesSnapshotRefsV1:
    values = {
        "instrument": refs.instrument,
        "position_tier": refs.position_tier,
        "execution_fee": refs.execution_fee,
        "funding_schedule": refs.funding_schedule,
    }
    values[kind.value] = updated
    return DerivativesSnapshotRefsV1(**values)


def load(root: Path, refs: DerivativesSnapshotRefsV1):
    return load_non_promotable_derivatives_snapshot_set(
        refs,
        snapshot_root=root,
        start_ts=BASE_TS,
        end_ts=BASE_TS + timedelta(hours=1),
    )


def rewrite_envelope(
    root: Path,
    refs: DerivativesSnapshotRefsV1,
    kind: SnapshotKindV1,
    envelope: dict,
) -> DerivativesSnapshotRefsV1:
    ref = getattr(refs, kind.value)
    raw = canonical_typed_json_bytes(envelope)
    (root / ref.relative_path).write_bytes(raw)
    return replace_ref(
        refs,
        kind,
        replace(
            ref,
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            semantic_sha256=typed_json_sha256(envelope),
        ),
    )


def test_loader_resolves_all_economic_contracts_but_never_promotion(tmp_path: Path) -> None:
    refs, _ = build_snapshot_set(tmp_path)

    loaded = load(tmp_path, refs)

    assert loaded.instrument_contract.face_value == Decimal("0.01")
    assert loaded.position_tier.maximum_leverage == Decimal("100")
    assert loaded.execution_fee.taker_fee_rate == Decimal("0.0005")
    assert loaded.funding_schedule.cadence == timedelta(hours=8)
    assert loaded.authority_status == "synthetic_test_only"
    assert loaded.capital_promotion_eligible is False


def test_loader_returns_defensive_ref_and_artifact_graph(tmp_path: Path) -> None:
    refs, _ = build_snapshot_set(tmp_path)
    original_raw_sha = refs.instrument.raw_sha256

    loaded = load(tmp_path, refs)

    assert loaded.refs is not refs
    assert loaded.artifacts[0].ref is not refs.instrument
    object.__setattr__(refs.instrument, "raw_sha256", "0" * 64)
    assert loaded.refs.instrument.raw_sha256 == original_raw_sha
    assert loaded.artifacts[0].ref.raw_sha256 == original_raw_sha
    assert loaded.snapshot_set_fingerprint == loaded.refs.fingerprint


def test_loader_revalidates_nested_ref_before_path_resolution(tmp_path: Path) -> None:
    refs, _ = build_snapshot_set(tmp_path)
    object.__setattr__(refs.instrument, "relative_path", 123)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        load(tmp_path, refs)

    assert exc_info.value.code == "artifact_relative_path_invalid"


def test_loaded_artifact_replace_revalidates_raw_binding(tmp_path: Path) -> None:
    refs, _ = build_snapshot_set(tmp_path)
    loaded = load(tmp_path, refs)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        replace(loaded.artifacts[0], raw_bytes=b"forged")

    assert exc_info.value.code == "snapshot_size_mismatch"


def test_loaded_set_replace_revalidates_derived_economics(tmp_path: Path) -> None:
    refs, _ = build_snapshot_set(tmp_path)
    loaded = load(tmp_path, refs)
    forged_fee = ExecutionFeeScheduleV1(
        maker_fee_rate=Decimal("0.1"),
        taker_fee_rate=Decimal("0.2"),
        fee_asset="USDT",
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        replace(loaded, execution_fee=forged_fee)

    assert exc_info.value.code == "snapshot_derived_contract_mismatch"


def test_loaded_objects_revalidate_mutated_nested_reference(tmp_path: Path) -> None:
    refs, _ = build_snapshot_set(tmp_path)
    loaded = load(tmp_path, refs)
    object.__setattr__(
        loaded.artifacts[0].ref,
        "relative_path",
        "../unsafe.json",
    )

    with pytest.raises(DerivativesBacktestContractError) as artifact_error:
        replace(loaded.artifacts[0])
    with pytest.raises(DerivativesBacktestContractError) as set_error:
        replace(loaded)

    assert artifact_error.value.code == "artifact_relative_path_invalid"
    assert set_error.value.code == "artifact_relative_path_invalid"


def test_loader_rejects_raw_tamper_before_semantic_parse(tmp_path: Path) -> None:
    refs, _ = build_snapshot_set(tmp_path)
    path = tmp_path / refs.execution_fee.relative_path
    path.write_bytes(path.read_bytes().replace(b"USDT", b"USDC"))

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        load(tmp_path, refs)

    assert exc_info.value.code == "snapshot_raw_digest_mismatch"


def test_loader_rejects_semantically_equal_noncanonical_bytes(tmp_path: Path) -> None:
    refs, _ = build_snapshot_set(tmp_path)
    ref = refs.position_tier
    path = tmp_path / ref.relative_path
    raw = b" " + path.read_bytes()
    path.write_bytes(raw)
    updated = replace(
        ref,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
    )
    refs = replace_ref(refs, SnapshotKindV1.POSITION_TIER, updated)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        load(tmp_path, refs)

    assert exc_info.value.code == "snapshot_bytes_non_canonical"


def test_loader_rejects_ref_payload_identity_drift(tmp_path: Path) -> None:
    refs, decoded = build_snapshot_set(tmp_path)
    ref = refs.execution_fee
    envelope = decoded[SnapshotKindV1.EXECUTION_FEE]
    envelope["source_seal_fingerprint"] = "e" * 64
    raw = canonical_typed_json_bytes(envelope)
    path = tmp_path / ref.relative_path
    path.write_bytes(raw)
    updated = replace(
        ref,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        semantic_sha256=typed_json_sha256(envelope),
    )
    refs = replace_ref(refs, SnapshotKindV1.EXECUTION_FEE, updated)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        load(tmp_path, refs)

    assert exc_info.value.code == "snapshot_ref_payload_mismatch"


def test_loader_rejects_caller_claimed_managed_authority(tmp_path: Path) -> None:
    refs, decoded = build_snapshot_set(tmp_path)
    kind = SnapshotKindV1.FUNDING_SCHEDULE
    ref = refs.funding_schedule
    envelope = decoded[kind]
    envelope["authority_status"] = "managed_source_sealed"
    raw = canonical_typed_json_bytes(envelope)
    (tmp_path / ref.relative_path).write_bytes(raw)
    updated = replace(
        ref,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        semantic_sha256=typed_json_sha256(envelope),
    )
    refs = replace_ref(refs, kind, updated)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        load(tmp_path, refs)

    assert exc_info.value.code == "snapshot_managed_source_seal_verifier_unavailable"


def test_loader_rejects_json_number_economic_field(tmp_path: Path) -> None:
    refs, decoded = build_snapshot_set(tmp_path)
    kind = SnapshotKindV1.EXECUTION_FEE
    ref = refs.execution_fee
    envelope = decoded[kind]
    envelope["payload"]["maker_fee_rate"] = -0.0002
    raw = canonical_typed_json_bytes(envelope)
    (tmp_path / ref.relative_path).write_bytes(raw)
    updated = replace(
        ref,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        semantic_sha256=typed_json_sha256(envelope),
    )
    refs = replace_ref(refs, kind, updated)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        load(tmp_path, refs)

    assert exc_info.value.code == "economic_decimal_wire_type_invalid"


def test_loader_rejects_relative_root(tmp_path: Path) -> None:
    refs, _ = build_snapshot_set(tmp_path)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        load(Path("relative-root"), refs)

    assert exc_info.value.code == "snapshot_root_invalid"


def test_loader_rejects_locator_case_that_would_drift_between_windows_and_wsl(
    tmp_path: Path,
) -> None:
    refs, _ = build_snapshot_set(tmp_path)
    ref = refs.instrument
    updated = replace(ref, relative_path="SNAPSHOTS/instrument.json")
    refs = replace_ref(refs, SnapshotKindV1.INSTRUMENT, updated)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        load(tmp_path, refs)

    assert exc_info.value.code == "snapshot_path_case_mismatch"


def test_observed_forward_snapshot_cannot_authorize_future_window(tmp_path: Path) -> None:
    refs, decoded = build_snapshot_set(tmp_path)
    kind = SnapshotKindV1.INSTRUMENT
    ref = refs.instrument
    envelope = decoded[kind]
    envelope["payload"] = instrument_snapshot(
        ref.source_schema,
        evidence_kind="observed_forward",
    ).to_dict()
    raw = canonical_typed_json_bytes(envelope)
    (tmp_path / ref.relative_path).write_bytes(raw)
    updated = replace(
        ref,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        semantic_sha256=typed_json_sha256(envelope),
    )
    refs = replace_ref(refs, kind, updated)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        load(tmp_path, refs)

    assert exc_info.value.code == "instrument_snapshot_effective_window_unproven"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("symbol", "ETH-USDT-SWAP"),
        ("venue", "BINANCE"),
        ("margin_mode", "cross"),
        ("payload_schema", "derivatives-execution-fee-snapshot-payload/v2"),
    ],
)
def test_loader_rejects_fixed_scope_drift(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    refs, decoded = build_snapshot_set(tmp_path)
    kind = SnapshotKindV1.EXECUTION_FEE
    envelope = decoded[kind]
    envelope[field] = value
    refs = rewrite_envelope(tmp_path, refs, kind, envelope)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        load(tmp_path, refs)

    assert exc_info.value.code == "snapshot_scope_out_of_v1_scope"


def test_loader_rejects_funding_schedule_identity_drift(tmp_path: Path) -> None:
    refs, decoded = build_snapshot_set(tmp_path)
    kind = SnapshotKindV1.FUNDING_SCHEDULE
    envelope = decoded[kind]
    envelope["payload"]["schedule_id"] = "00000000-0000-4000-8000-999999999999"
    refs = rewrite_envelope(tmp_path, refs, kind, envelope)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        load(tmp_path, refs)

    assert exc_info.value.code == "funding_schedule_id_mismatch"
