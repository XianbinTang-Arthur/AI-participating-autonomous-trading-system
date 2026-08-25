from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from aats.data_platform.research_factory.features.quality import (
    build_factor_input_quality_report,
)
from aats.data_platform.research_factory.specs import DatasetSpec, SegmentSpec
from aats.data_platform.research_factory.datasets.gold_bars import PreparedGoldBarDataset


START = datetime(2026, 5, 16, tzinfo=UTC)


def _prepared() -> PreparedGoldBarDataset:
    segments = (
        SegmentSpec(name="train", start=START, end=START + timedelta(hours=2), purpose="train"),
        SegmentSpec(
            name="valid",
            start=START + timedelta(hours=2),
            end=START + timedelta(hours=4),
            purpose="valid",
        ),
        SegmentSpec(
            name="test",
            start=START + timedelta(hours=4),
            end=START + timedelta(hours=6),
            purpose="sealed holdout",
        ),
    )
    spec = DatasetSpec(
        dataset_id="micro_quality",
        symbol="BTC-USDT-SWAP",
        timeframe="1h",
        dataset_version="v1",
        window_start=START,
        window_end=START + timedelta(hours=6),
        segments=segments,
        source_refs={"gold": "fixture"},
    )
    rows = {
        segment.name: tuple(
            {
                "ts": segment.start + timedelta(hours=index),
                "trade_flow_imbalance": 0.1,
                "oi_delta": 0.01,
            }
            for index in range(2)
        )
        for segment in segments
    }
    return PreparedGoldBarDataset(dataset_spec=spec, rows_by_segment=rows)


def test_factor_input_quality_passes_at_inclusive_threshold() -> None:
    prepared = _prepared()
    rows = dict(prepared.rows_by_segment)
    valid = [dict(row) for row in rows["valid"]]
    valid[0]["oi_delta"] = None
    rows["valid"] = tuple(valid)

    report = build_factor_input_quality_report(
        replace(prepared, rows_by_segment=rows),
        required_fields=("trade_flow_imbalance", "oi_delta"),
        max_missing_ratio=0.5,
        created_at=START,
    )

    assert report.passed is True
    assert report.missing_counts["oi_delta"] == 1
    assert report.segment_missing_ratios["valid"]["oi_delta"] == pytest.approx(0.5)


def test_factor_input_quality_fails_closed_per_segment() -> None:
    prepared = _prepared()
    rows = dict(prepared.rows_by_segment)
    test_rows = [dict(row) for row in rows["test"]]
    test_rows[0]["trade_flow_imbalance"] = None
    rows["test"] = tuple(test_rows)

    report = build_factor_input_quality_report(
        replace(prepared, rows_by_segment=rows),
        required_fields=("trade_flow_imbalance",),
        max_missing_ratio=0.01,
        created_at=START,
    )

    assert report.passed is False
    assert report.failures == (
        "test.trade_flow_imbalance_missing_ratio=0.500000 > max_missing_ratio=0.010000",
    )


def test_factor_input_quality_rejects_invalid_threshold() -> None:
    with pytest.raises(ValueError, match="between zero and one"):
        build_factor_input_quality_report(
            _prepared(),
            required_fields=("close",),
            max_missing_ratio=1.1,
        )
