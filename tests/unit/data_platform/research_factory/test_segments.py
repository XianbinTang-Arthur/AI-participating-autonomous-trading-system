from datetime import datetime, timezone

import pytest

from aats.data_platform.research_factory.datasets.segments import (
    assert_no_leakage,
    build_time_segments,
    segment_for_timestamp,
)
from aats.data_platform.research_factory.specs import SegmentSpec


UTC = timezone.utc


def ts(hour: int) -> datetime:
    return datetime(2026, 5, 16, hour, tzinfo=UTC)


def segment(name: str, start_hour: int, end_hour: int) -> SegmentSpec:
    return SegmentSpec(
        name=name,
        start=ts(start_hour),
        end=ts(end_hour),
        purpose=f"{name} segment",
    )


def test_build_time_segments_creates_contiguous_half_open_segments() -> None:
    train, valid, test = build_time_segments(ts(0), ts(10), 0.6, 0.2, 0.2)

    assert train.name == "train"
    assert valid.name == "valid"
    assert test.name == "test"
    assert train.start == ts(0)
    assert train.end == ts(6)
    assert valid.start == ts(6)
    assert valid.end == ts(8)
    assert test.start == ts(8)
    assert test.end == ts(10)


def test_segment_for_timestamp_uses_half_open_boundaries() -> None:
    segments = build_time_segments(ts(0), ts(10), 0.6, 0.2, 0.2)

    assert segment_for_timestamp(ts(0), segments).name == "train"
    assert segment_for_timestamp(ts(6), segments).name == "valid"
    assert segment_for_timestamp(ts(8), segments).name == "test"
    assert segment_for_timestamp(ts(10), segments) is None


def test_build_time_segments_rejects_ratio_sum_not_one() -> None:
    with pytest.raises(ValueError, match="sum to 1"):
        build_time_segments(ts(0), ts(10), 0.5, 0.2, 0.2)


def test_build_time_segments_rejects_empty_window() -> None:
    with pytest.raises(ValueError, match="window_end must be after window_start"):
        build_time_segments(ts(0), ts(0), 0.6, 0.2, 0.2)


def test_assert_no_leakage_rejects_non_replay_overlap() -> None:
    with pytest.raises(ValueError, match="must not overlap"):
        assert_no_leakage(
            [
                segment("train", 0, 5),
                segment("valid", 4, 8),
            ]
        )


def test_assert_no_leakage_allows_explicit_replay_overlap() -> None:
    segments = [
        segment("train", 0, 5),
        segment("replay", 4, 8),
        segment("test", 8, 10),
    ]

    assert_no_leakage(segments)


def test_segment_for_timestamp_rejects_timezone_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        segment_for_timestamp(datetime(2026, 5, 16, 1), [segment("train", 0, 2)])


def test_segment_for_timestamp_rejects_empty_segments() -> None:
    with pytest.raises(ValueError, match="segments must not be empty"):
        segment_for_timestamp(ts(1), [])
