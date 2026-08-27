from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

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


UTC = timezone.utc


def _source(
    key: str = "okx-bulk:l2:v1",
    *,
    kind: SourceKind = SourceKind.OKX_BULK,
    start: datetime | None = None,
    end: datetime | None = None,
    raw_partition_sha256: str = "b" * 64,
    git_commit: str = "a" * 40,
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
