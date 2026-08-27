from datetime import datetime, timezone

import pytest

from scripts.rdp_run_execution_realism import (
    _annotate_research_evidence_identity,
    _load_replay_params as load_execution_replay_params,
    _parse_utc_datetime,
)
from scripts.rdp_run_live_attribution import (
    _load_replay_params as load_attribution_replay_params,
)

UTC = timezone.utc
START = datetime(2026, 5, 2, 4, 48, tzinfo=UTC)
END = datetime(2026, 5, 2, 7, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    "loader",
    (load_execution_replay_params, load_attribution_replay_params),
)
def test_legacy_runners_use_directional_baseline_for_empty_and_partial_params(
    loader,
) -> None:
    baseline = loader(None, None, [], family="directional")
    partial = loader(
        None,
        None,
        ["min_confirm_ticks=3"],
        family="directional",
    )

    for params in (baseline, partial):
        assert params.entry_threshold == 0.45
        assert params.close_threshold == 0.20
        assert params.scale_in_threshold == 0.55
    assert partial.min_confirm_ticks == 3


def test_parse_utc_datetime_accepts_date_and_offset_timestamp() -> None:
    assert _parse_utc_datetime("2026-05-02") == datetime(2026, 5, 2, tzinfo=UTC)
    assert _parse_utc_datetime("2026-05-02T06:48:00+02:00") == START


def test_execution_summary_binds_exact_valid_evidence_identity() -> None:
    result = _annotate_research_evidence_identity(
        {"cost_adjusted_edge": {"mean": 1.5}},
        source_run_id="phase4-valid-1",
        symbol="BTC-USDT-SWAP",
        timeframe="1h",
        window_start=START,
        window_end=END,
        benchmark_segment="valid",
        dataset_fingerprint="rfds_" + "a" * 64,
        dataset_fingerprint_compatibility_reason=None,
    )

    assert result["schema_version"] == "execution_cost_summary_v1"
    assert result["benchmark_segment"] == "valid"
    assert result["window_start"] == START.isoformat()
    assert result["window_end"] == END.isoformat()
    assert result["dataset_fingerprint"] == "rfds_" + "a" * 64
    assert "dataset_fingerprint_compatibility" not in result


def test_execution_summary_can_record_explicit_compatibility_assertion() -> None:
    result = _annotate_research_evidence_identity(
        {},
        source_run_id="phase4-valid-2",
        symbol="BTC-USDT-SWAP",
        timeframe="1h",
        window_start=START,
        window_end=END,
        benchmark_segment="valid",
        dataset_fingerprint=None,
        dataset_fingerprint_compatibility_reason="reviewed source manifest matches",
    )

    assert result["dataset_fingerprint_compatibility"] == "compatible"
    assert result["compatibility_reason"] == "reviewed source manifest matches"


@pytest.mark.parametrize(
    ("benchmark_segment", "fingerprint", "reason", "message"),
    (
        ("valid", None, None, "requires an exact dataset fingerprint"),
        ("valid", "rfds_" + "a" * 64, "also compatible", "either dataset fingerprint"),
        (None, "rfds_" + "a" * 64, None, "dataset identity requires"),
        ("holdout", "rfds_" + "a" * 64, None, "train, valid, or test"),
    ),
)
def test_execution_summary_rejects_ambiguous_research_identity(
    benchmark_segment: str | None,
    fingerprint: str | None,
    reason: str | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _annotate_research_evidence_identity(
            {},
            source_run_id="phase4-invalid",
            symbol="BTC-USDT-SWAP",
            timeframe="1h",
            window_start=START,
            window_end=END,
            benchmark_segment=benchmark_segment,
            dataset_fingerprint=fingerprint,
            dataset_fingerprint_compatibility_reason=reason,
        )


def test_execution_summary_rejects_non_positive_window() -> None:
    with pytest.raises(ValueError, match="end must be after start"):
        _annotate_research_evidence_identity(
            {},
            source_run_id="phase4-invalid-window",
            symbol="BTC-USDT-SWAP",
            timeframe="1h",
            window_start=END,
            window_end=START,
            benchmark_segment=None,
            dataset_fingerprint=None,
            dataset_fingerprint_compatibility_reason=None,
        )
