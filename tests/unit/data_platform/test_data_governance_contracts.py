from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from aats.data_platform.data_governance.contracts import (
    DataSourceRecord,
    DatasetBundleContract,
    SourceKind,
    TruthTier,
    bundle_fingerprint,
    canonical_json_bytes,
    source_fingerprint,
)
from aats.data_platform.data_governance.eligibility import evaluate_historical_bundle
from aats.data_platform.data_governance.instrument_lineage import (
    evaluate_instrument_contract_binding,
)
from aats.domain.instrument_contract_snapshot import (
    instrument_contract_observation_window_from_metadata,
)
from aats.schemas.exchange import InstrumentMetadata


UTC = timezone.utc


def _source(
    key: str = "okx-bulk:l2:v1",
    *,
    kind: SourceKind = SourceKind.OKX_BULK,
    start: datetime | None = None,
    end: datetime | None = None,
    raw_partition_sha256: str = "b" * 64,
    git_commit: str = "a" * 40,
    instrument_contract_snapshot=None,
) -> DataSourceRecord:
    coverage_start = start or datetime(2026, 8, 1, tzinfo=UTC)
    coverage_end = end or coverage_start + timedelta(days=1)
    return DataSourceRecord(
        source_key=key,
        source_kind=kind,
        provider="OKX",
        source_locator="official historical data file",
        retrieved_at=datetime(2026, 8, 26, tzinfo=UTC),
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        timestamp_semantics="exchange event time",
        schema_version="v1",
        dataset_version="2026-08-26",
        transform_version="causal-resample-v1",
        git_commit=git_commit,
        raw_sha256=hashlib.sha256(
            canonical_json_bytes((raw_partition_sha256,))
        ).hexdigest(),
        row_count=100,
        gap_manifest={
            "gap_count": 0,
            "unclassified_gap_count": 0,
            "raw_partition_count": 1,
            "raw_partition_sha256": (raw_partition_sha256,),
        },
        license_usage_note="official public historical data",
        truth_tier=(
            TruthTier.PROXY
            if kind == SourceKind.PROXY
            else TruthTier.AUTHORITATIVE_EXTERNAL
        ),
        instrument_contract_snapshot=instrument_contract_snapshot,
    )


def _historical_snapshot(*, contract_value: str = "0.01"):
    metadata = InstrumentMetadata(
            instrument_id="BTC-USDT-SWAP",
            symbol="BTC-USDT-SWAP",
            base_currency="BTC",
            quote_currency="USDT",
            lot_size="1",
            tick_size="0.1",
            min_size="1",
            contract_value=contract_value,
            contract_multiplier="1",
            contract_type="linear",
            instrument_type="SWAP",
            underlying="BTC-USDT",
            settle_currency="USDT",
            contract_value_currency="BTC",
            state="live",
        )
    return instrument_contract_observation_window_from_metadata(
        metadata,
        venue="OKX",
        first_observed_at=datetime(2026, 8, 1, tzinfo=UTC),
        last_observed_at=datetime(2026, 8, 2, tzinfo=UTC),
        observation_evidence_sha256="e" * 64,
        source_locator="immutable://test/instrument-observation-window",
    )


def test_bundle_fingerprint_is_order_independent_but_source_sensitive() -> None:
    first = _source()
    second = _source("okx-rest:trades:v5", kind=SourceKind.OKX_REST)
    start = first.coverage_start
    end = first.coverage_end
    one = DatasetBundleContract(
        "bundle-v1", "v1", "microstructure_research", "historical_research",
        start, end, [first, second],
    )
    two = DatasetBundleContract(
        "bundle-v1", "v1", "microstructure_research", "historical_research",
        start, end, [second, first],
    )

    assert bundle_fingerprint(one) == bundle_fingerprint(two)
    changed = _source(raw_partition_sha256="c" * 64)
    changed_bundle = DatasetBundleContract(
        "bundle-v1", "v1", "microstructure_research", "historical_research",
        start, end, [changed, second],
    )
    assert bundle_fingerprint(one) != bundle_fingerprint(changed_bundle)


def test_source_fingerprint_ignores_retrieval_time_for_exact_retry() -> None:
    source = _source()
    retried = DataSourceRecord(
        **{
            **source.__dict__,
            "retrieved_at": source.retrieved_at + timedelta(hours=1),
        }
    )

    assert source_fingerprint(source) == source_fingerprint(retried)


def test_source_contract_rejects_unknown_git_commit() -> None:
    with pytest.raises(ValueError, match="git_commit_must_be_full_lowercase_hex"):
        _source(git_commit="unknown")


def test_historical_bundle_passes_without_fake_live_heartbeat() -> None:
    source = _source()
    bundle = DatasetBundleContract(
        "l2-day", "v1", "l2_replay", "historical_research",
        source.coverage_start, source.coverage_end, [source],
    )

    report = evaluate_historical_bundle(
        bundle,
        component_roles={source.source_key: "l2_event_history"},
        coverage_ratios={source.source_key: 1.0},
        causal_time_checks={source.source_key: True},
    )

    assert report.eligible is True
    assert report.reason_codes == ()


def test_historical_bundle_fails_closed_on_unclassified_gap() -> None:
    source = DataSourceRecord(
        **{
            **_source().__dict__,
            "gap_manifest": {
                **_source().gap_manifest,
                "gap_count": 1,
                "unclassified_gap_count": 1,
            },
        }
    )
    bundle = DatasetBundleContract(
        "l2-day-unclassified-gap",
        "v1",
        "l2_replay",
        "historical_research",
        source.coverage_start,
        source.coverage_end,
        [source],
    )

    report = evaluate_historical_bundle(
        bundle,
        component_roles={source.source_key: "l2_event_history"},
        coverage_ratios={source.source_key: 1.0},
        causal_time_checks={source.source_key: True},
    )

    assert report.eligible is False
    assert report.reason_codes == (f"unclassified_gaps:{source.source_key}",)


def test_proxy_cannot_satisfy_tick_role_and_missing_causality_fails_closed() -> None:
    source = _source("okx-rest:mark-price", kind=SourceKind.PROXY)
    bundle = DatasetBundleContract(
        "mark-proxy", "v1", "microstructure_research", "historical_research",
        source.coverage_start, source.coverage_end, [source],
    )

    report = evaluate_historical_bundle(
        bundle,
        component_roles={source.source_key: "mark_tick"},
        coverage_ratios={source.source_key: 1.0},
        causal_time_checks={source.source_key: False},
    )

    assert report.eligible is False
    assert any(reason.startswith("proxy_role_disallowed") for reason in report.reason_codes)
    assert any(reason.startswith("causal_time_check_failed") for reason in report.reason_codes)


def test_source_contract_rejects_naive_time_or_invalid_sha() -> None:
    with pytest.raises(ValueError, match="retrieved_at_must_be_timezone_aware"):
        DataSourceRecord(**{**_source().__dict__, "retrieved_at": datetime(2026, 8, 26)})
    with pytest.raises(ValueError, match="raw_sha256_must_be_hex"):
        DataSourceRecord(**{**_source().__dict__, "raw_sha256": "z" * 64})
    with pytest.raises(ValueError, match="source_truth_tier_incompatible"):
        DataSourceRecord(
            **{
                **_source().__dict__,
                "truth_tier": TruthTier.PROXY,
            }
        )
    with pytest.raises(ValueError, match="source_raw_sha256_aggregate_mismatch"):
        DataSourceRecord(
            **{
                **_source().__dict__,
                "raw_sha256": "c" * 64,
            }
        )


def test_bundle_contract_rejects_naive_window() -> None:
    source = _source()
    with pytest.raises(ValueError, match="coverage_start_must_be_timezone_aware"):
        DatasetBundleContract(
            "bundle-v1",
            "v1",
            "research",
            "historical_research",
            datetime(2026, 8, 1),
            source.coverage_end,
            [source],
        )


def test_snapshot_digest_changes_source_and_bundle_fingerprints() -> None:
    first = _source(instrument_contract_snapshot=_historical_snapshot())
    changed = _source(
        instrument_contract_snapshot=_historical_snapshot(contract_value="0.02")
    )
    assert source_fingerprint(first) != source_fingerprint(changed)
    first_bundle = DatasetBundleContract(
        "bound-l2",
        "v1",
        "l2_replay",
        "historical_research",
        first.coverage_start,
        first.coverage_end,
        [first],
    )
    changed_bundle = DatasetBundleContract(
        "bound-l2",
        "v1",
        "l2_replay",
        "historical_research",
        changed.coverage_start,
        changed.coverage_end,
        [changed],
    )
    assert bundle_fingerprint(first_bundle) != bundle_fingerprint(changed_bundle)


def test_contract_binding_distinguishes_missing_and_valid_snapshot() -> None:
    unbound = _source()
    missing = evaluate_instrument_contract_binding(
        unbound,
        symbol="BTC-USDT-SWAP",
        coverage_start=unbound.coverage_start,
        coverage_end=unbound.coverage_end,
    )
    bound_source = _source(instrument_contract_snapshot=_historical_snapshot())
    bound = evaluate_instrument_contract_binding(
        bound_source,
        symbol="BTC-USDT-SWAP",
        coverage_start=bound_source.coverage_start,
        coverage_end=bound_source.coverage_end,
    )

    assert missing.eligible is False
    assert missing.reason_codes == ("derivative_instrument_metadata_required",)
    assert bound.eligible is False
    assert bound.reason_codes == (
        "instrument_snapshot_observation_evidence_unverified",
    )
    assert bound.snapshot_digest == _historical_snapshot().digest


@pytest.mark.parametrize(
    "symbol",
    ("DOGE-USDT", "DOGE-USDT-SWAP", "BTC-USDT-240927"),
)
def test_contract_binding_keeps_unknown_instrument_scope_unproven(
    symbol: str,
) -> None:
    source = _source()

    report = evaluate_instrument_contract_binding(
        source,
        symbol=symbol,
        coverage_start=source.coverage_start,
        coverage_end=source.coverage_end,
    )

    assert report.required is False
    assert report.eligible is False
    assert report.reason_codes == ("instrument_scope_unsupported_or_unproven",)


def test_contract_binding_accepts_explicit_supported_spot_without_snapshot() -> None:
    source = _source()

    report = evaluate_instrument_contract_binding(
        source,
        symbol="ETH-USDT",
        coverage_start=source.coverage_start,
        coverage_end=source.coverage_end,
    )

    assert report.required is False
    assert report.eligible is True
    assert report.reason_codes == ()


def test_contract_binding_rejects_snapshot_from_a_different_instrument_scope() -> None:
    source = _source(instrument_contract_snapshot=_historical_snapshot())

    report = evaluate_instrument_contract_binding(
        source,
        symbol="BTC-USDT",
        coverage_start=source.coverage_start,
        coverage_end=source.coverage_end,
    )

    assert report.eligible is False
    assert "instrument_snapshot_symbol_mismatch" in report.reason_codes


def test_snapshot_contract_cannot_be_forged_with_spot_semantics() -> None:
    snapshot = _historical_snapshot()
    with pytest.raises(
        ValueError,
        match="instrument_identity_mismatch",
    ):
        replace(
            snapshot,
            contract=replace(
                snapshot.contract,
                instrument_type="SPOT",
                contract_type="spot",
                contract_value=Decimal("1"),
            ),
        )


def test_contract_binding_rejects_self_declared_authoritative_history() -> None:
    snapshot = replace(
        _historical_snapshot(),
        evidence_kind="authoritative_history",
    )
    source = _source(instrument_contract_snapshot=snapshot)

    report = evaluate_instrument_contract_binding(
        source,
        symbol="BTC-USDT-SWAP",
        coverage_start=source.coverage_start,
        coverage_end=source.coverage_end,
    )

    assert report.eligible is False
    assert report.reason_codes == (
        "instrument_snapshot_authoritative_history_unverified",
    )


def test_contract_binding_rejects_single_caller_observation_without_raw_verifier() -> None:
    observed_at = datetime(2026, 8, 1, tzinfo=UTC)
    snapshot = replace(
        _historical_snapshot(),
        observed_at=observed_at,
        effective_from=observed_at,
        effective_to=None,
        source_locator="/api/v5/public/instruments",
        source_schema="okx-public-instruments-v5",
        source_payload_sha256="f" * 64,
    )
    source = _source(
        start=observed_at,
        end=observed_at + timedelta(days=1),
        instrument_contract_snapshot=snapshot,
    )

    report = evaluate_instrument_contract_binding(
        source,
        symbol="BTC-USDT-SWAP",
        coverage_start=source.coverage_start,
        coverage_end=source.coverage_end,
    )

    assert report.eligible is False
    assert report.reason_codes == (
        "instrument_snapshot_observation_evidence_unverified",
    )
