"""Time segment helpers for Research Factory datasets."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from aats.data_platform.research_factory.specs import SegmentSpec

RATIO_TOLERANCE = 1e-9


def build_time_segments(
    window_start: datetime,
    window_end: datetime,
    train_ratio: float,
    valid_ratio: float,
    test_ratio: float,
) -> tuple[SegmentSpec, SegmentSpec, SegmentSpec]:
    """Build contiguous train/valid/test segments for a research window."""
    _require_aware_datetime(window_start, "window_start")
    _require_aware_datetime(window_end, "window_end")
    if window_end <= window_start:
        raise ValueError("window_end must be after window_start")

    ratios = (train_ratio, valid_ratio, test_ratio)
    if any(ratio <= 0 for ratio in ratios):
        raise ValueError("segment ratios must be positive")
    if abs(sum(ratios) - 1.0) > RATIO_TOLERANCE:
        raise ValueError("segment ratios must sum to 1")

    duration = window_end - window_start
    train_end = window_start + duration * train_ratio
    valid_end = train_end + duration * valid_ratio
    segments = (
        SegmentSpec("train", window_start, train_end, "training segment"),
        SegmentSpec("valid", train_end, valid_end, "validation segment"),
        SegmentSpec("test", valid_end, window_end, "out-of-sample test segment"),
    )
    assert_no_leakage(segments)
    return segments


def assert_no_leakage(segments: Sequence[SegmentSpec]) -> None:
    """Reject overlapping non-replay segments while allowing explicit replay overlap."""
    if not segments:
        raise ValueError("segments must not be empty")
    if not all(isinstance(segment, SegmentSpec) for segment in segments):
        raise ValueError("segments must be SegmentSpec instances")

    comparable_segments = [segment for segment in segments if segment.name != "replay"]
    for index, left in enumerate(comparable_segments):
        for right in comparable_segments[index + 1 :]:
            if _intervals_overlap(left, right):
                raise ValueError("non-replay segments must not overlap")


def segment_for_timestamp(ts: datetime, segments: Sequence[SegmentSpec]) -> SegmentSpec | None:
    """Return the first segment containing a timestamp using half-open intervals."""
    _require_aware_datetime(ts, "timestamp")
    if not segments:
        raise ValueError("segments must not be empty")
    for segment in segments:
        if not isinstance(segment, SegmentSpec):
            raise ValueError("segments must be SegmentSpec instances")
        if segment.start <= ts < segment.end:
            return segment
    return None


def _intervals_overlap(left: SegmentSpec, right: SegmentSpec) -> bool:
    return left.start < right.end and right.start < left.end


def _require_aware_datetime(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
