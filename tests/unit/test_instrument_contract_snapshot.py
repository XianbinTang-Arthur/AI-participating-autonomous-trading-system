from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

import pytest

from aats.domain.instrument_contract import InstrumentContractError
from aats.domain.instrument_contract_snapshot import (
    InstrumentContractSnapshot,
    instrument_contract_observation_window_from_metadata,
    instrument_contract_snapshot_from_metadata,
)
from aats.data_platform.data_governance.instrument_lineage import (
    instrument_contract_snapshot_source_key,
)
from aats.schemas.exchange import InstrumentMetadata


UTC = timezone.utc
OBSERVED = datetime(2026, 8, 27, 12, tzinfo=UTC)


def _metadata(**overrides) -> InstrumentMetadata:
    values = {
        "instrument_id": "BTC-USDT-SWAP",
        "symbol": "BTC-USDT-SWAP",
        "base_currency": "BTC",
        "quote_currency": "USDT",
        "lot_size": Decimal("1.0"),
        "tick_size": Decimal("0.10"),
        "min_size": Decimal("1.00"),
        "contract_value": Decimal("0.0100"),
        "contract_multiplier": Decimal("1.0"),
        "contract_type": "linear",
        "instrument_type": "SWAP",
        "instrument_family": "BTC-USDT",
        "underlying": "BTC-USDT",
        "settle_currency": "USDT",
        "contract_value_currency": "BTC",
        "state": "live",
    }
    values.update(overrides)
    return InstrumentMetadata(**values)


def _snapshot(**overrides) -> InstrumentContractSnapshot:
    values = {
        "instrument": _metadata(),
        "venue": "OKX",
        "observed_at": OBSERVED,
        "source_locator": "/api/v5/public/instruments",
        "source_schema": "okx-public-instruments-v5",
    }
    values.update(overrides)
    return instrument_contract_snapshot_from_metadata(**values)


def test_snapshot_round_trip_is_canonical_and_decimal_scale_independent() -> None:
    first = _snapshot()
    second = _snapshot(
        instrument=_metadata(
            contract_value=Decimal("0.01"),
            contract_multiplier=Decimal("1"),
            lot_size=Decimal("1"),
            min_size=Decimal("1"),
            tick_size=Decimal("0.1"),
        )
    )

    assert first.digest == second.digest
    assert first.to_dict()["instrument"]["contract_value"] == "0.01"
    assert InstrumentContractSnapshot.from_dict(first.to_dict()) == first


def test_snapshot_identity_is_independent_from_decimal_context_precision() -> None:
    with localcontext() as context:
        context.prec = 4
        low_precision = _snapshot(
            instrument=_metadata(
                contract_value=Decimal("123.456789000"),
                tick_size=Decimal("0.000123456789"),
            )
        )
    with localcontext() as context:
        context.prec = 28
        normal_precision = _snapshot(
            instrument=_metadata(
                contract_value=Decimal("123.456789"),
                tick_size=Decimal("0.0001234567890"),
            )
        )

    assert low_precision.to_dict()["instrument"]["contract_value"] == "123.456789"
    assert low_precision.to_dict()["instrument"]["tick_size"] == "0.000123456789"
    assert low_precision.digest == normal_precision.digest
    assert (
        instrument_contract_snapshot_source_key(low_precision)
        == instrument_contract_snapshot_source_key(normal_precision)
    )


@pytest.mark.parametrize("value", [Decimal("1E+100000"), Decimal("1E-100000")])
def test_snapshot_rejects_decimal_expansion_denial_of_service(value: Decimal) -> None:
    with pytest.raises(
        InstrumentContractError,
        match="instrument_snapshot_decimal_invalid",
    ):
        _snapshot(instrument=_metadata(contract_value=value))


def test_snapshot_detects_payload_tampering() -> None:
    payload = _snapshot().to_dict()
    payload["instrument"]["contract_value"] = "1"

    with pytest.raises(
        InstrumentContractError,
        match="instrument_snapshot_digest_mismatch",
    ):
        InstrumentContractSnapshot.from_dict(payload)


def test_current_observation_cannot_claim_past_effective_window() -> None:
    with pytest.raises(
        InstrumentContractError,
        match="instrument_snapshot_window_unproven",
    ):
        _snapshot(effective_from=OBSERVED - timedelta(days=1))


def test_observation_window_proves_only_the_captured_half_open_interval() -> None:
    snapshot = instrument_contract_observation_window_from_metadata(
        _metadata(),
        venue="OKX",
        first_observed_at=OBSERVED - timedelta(days=1),
        last_observed_at=OBSERVED,
        observation_evidence_sha256="e" * 64,
        source_locator="immutable://test/instrument-observation-window",
    )

    snapshot.validate_window(
        symbol="BTC-USDT-SWAP",
        start=OBSERVED - timedelta(days=1),
        end=OBSERVED,
    )
    with pytest.raises(
        InstrumentContractError,
        match="instrument_snapshot_window_unproven",
    ):
        snapshot.validate_window(
            symbol="BTC-USDT-SWAP",
            start=OBSERVED - timedelta(days=1),
            end=OBSERVED + timedelta(microseconds=1),
        )


def test_authoritative_history_label_is_not_itself_temporal_proof() -> None:
    snapshot = _snapshot(
        evidence_kind="authoritative_history",
        effective_from=OBSERVED - timedelta(days=30),
        effective_to=OBSERVED + timedelta(days=1),
    )

    assert snapshot.evidence_kind == "authoritative_history"


def test_snapshot_rejects_symbol_mismatch_and_unknown_extra_fields() -> None:
    snapshot = _snapshot()
    with pytest.raises(
        InstrumentContractError,
        match="instrument_snapshot_symbol_mismatch",
    ):
        snapshot.validate_window(
            symbol="ETH-USDT-SWAP",
            start=OBSERVED,
            end=OBSERVED + timedelta(seconds=1),
        )

    payload = snapshot.to_dict()
    payload["unexpected"] = True
    with pytest.raises(
        InstrumentContractError,
        match="instrument_snapshot_shape_invalid",
    ):
        InstrumentContractSnapshot.from_dict(payload)
