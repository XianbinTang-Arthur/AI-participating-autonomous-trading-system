"""Research-only Gold bar dataset handler for Research Factory."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from typing import Any

from aats.data_platform.research_factory.datasets.segments import assert_no_leakage
from aats.data_platform.research_factory.numeric import require_finite_number
from aats.data_platform.research_factory.specs import DatasetSpec, ProcessorSpec, SegmentSpec

NumericValue = int | float | Decimal
DATASET_FINGERPRINT_SCHEMA = "research_factory.gold_bars.dataset_fingerprint.v1"


@dataclass(frozen=True, slots=True)
class GoldBarRecord:
    """Minimal in-memory Gold replay bar record used by the research handler."""

    symbol: str
    timeframe: str
    ts: datetime
    open: NumericValue
    high: NumericValue
    low: NumericValue
    close: NumericValue
    volume: NumericValue
    vwap: NumericValue | None = None
    funding_rate: NumericValue | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.symbol, "record.symbol")
        _require_non_empty(self.timeframe, "record.timeframe")
        _require_aware_datetime(self.ts, "record.ts")
        prices = {
            field_name: require_finite_number(getattr(self, field_name), f"record.{field_name}")
            for field_name in ("open", "high", "low", "close")
        }
        volume = require_finite_number(self.volume, "record.volume")
        for field_name, value in prices.items():
            if value <= 0:
                raise ValueError(f"record.{field_name} must be positive")
        if volume < 0:
            raise ValueError("record.volume must be non-negative")
        if prices["high"] < max(prices["open"], prices["low"], prices["close"]):
            raise ValueError("record.high must be greater than or equal to open, low, and close")
        if prices["low"] > min(prices["open"], prices["high"], prices["close"]):
            raise ValueError("record.low must be less than or equal to open, high, and close")
        if self.vwap is not None:
            vwap = require_finite_number(self.vwap, "record.vwap")
            if vwap <= 0:
                raise ValueError("record.vwap must be positive")
        if self.funding_rate is not None:
            require_finite_number(self.funding_rate, "record.funding_rate")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("record.metadata must be a mapping")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_row(self) -> dict[str, Any]:
        """Return a detached row dictionary suitable for feature processing."""
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "ts": self.ts,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "vwap": self.vwap,
            "funding_rate": self.funding_rate,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class PreparedGoldBarDataset:
    """Prepared rows grouped by dataset segment name."""

    dataset_spec: DatasetSpec
    rows_by_segment: Mapping[str, tuple[Mapping[str, Any], ...]]
    missing_reasons: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.dataset_spec, DatasetSpec):
            raise ValueError("dataset_spec must be a DatasetSpec")
        rows_by_segment = {
            segment_name: tuple(rows) for segment_name, rows in self.rows_by_segment.items()
        }
        missing_reasons = {
            field_name: tuple(reasons)
            for field_name, reasons in self.missing_reasons.items()
        }
        object.__setattr__(self, "rows_by_segment", rows_by_segment)
        object.__setattr__(self, "missing_reasons", missing_reasons)

    def rows_for_segment(self, segment_name: str) -> tuple[Mapping[str, Any], ...]:
        """Return prepared rows for a segment or an empty tuple when absent."""
        return self.rows_by_segment.get(segment_name, ())


class GoldBarDatasetHandler:
    """Prepare in-memory Gold bars according to a Research Factory DatasetSpec."""

    def prepare(
        self,
        records: Sequence[GoldBarRecord],
        dataset_spec: DatasetSpec,
    ) -> PreparedGoldBarDataset:
        if not isinstance(dataset_spec, DatasetSpec):
            raise ValueError("dataset_spec must be a DatasetSpec")
        if not isinstance(records, Sequence):
            raise ValueError("records must be a sequence")

        segments = tuple(dataset_spec.segments)
        _require_unique_segment_names(segments)
        assert_no_leakage(segments)

        rows_by_segment: dict[str, list[Mapping[str, Any]]] = {
            segment.name: [] for segment in segments
        }
        missing_reasons: defaultdict[str, list[str]] = defaultdict(list)
        seen_timestamps: set[datetime] = set()

        sorted_records = sorted(_validate_records(records), key=lambda record: record.ts)
        for record in sorted_records:
            _require_matching_scope(record, dataset_spec)
            if record.ts in seen_timestamps:
                raise ValueError(f"duplicate timestamp in gold bar records: {record.ts.isoformat()}")
            seen_timestamps.add(record.ts)

            if record.ts < dataset_spec.window_start or record.ts >= dataset_spec.window_end:
                continue

            row = record.to_row()
            matched_segment_names = _matching_segment_names(record.ts, segments)
            if matched_segment_names and record.funding_rate is None:
                missing_reasons["funding_rate"].append(
                    f"{record.ts.isoformat()}: funding_rate missing"
                )
            for segment_name in matched_segment_names:
                rows_by_segment[segment_name].append(row)

        for segment_name, rows in rows_by_segment.items():
            if not rows:
                raise ValueError(f"segment {segment_name!r} has no rows")

        return PreparedGoldBarDataset(
            dataset_spec=dataset_spec,
            rows_by_segment=rows_by_segment,
            missing_reasons=missing_reasons,
        )


def dataset_fingerprint(
    dataset_spec: DatasetSpec,
    source_watermark: Any,
    processor_versions: Mapping[str, Any] | Sequence[ProcessorSpec],
) -> str:
    """Build a deterministic cache key for a Gold bar dataset specification."""
    if not isinstance(dataset_spec, DatasetSpec):
        raise ValueError("dataset_spec must be a DatasetSpec")
    _require_cache_material(source_watermark, "source_watermark")
    _require_cache_material(processor_versions, "processor_versions")

    payload = {
        "schema": DATASET_FINGERPRINT_SCHEMA,
        "dataset": {
            "dataset_id": dataset_spec.dataset_id,
            "symbol": dataset_spec.symbol,
            "timeframe": dataset_spec.timeframe,
            "dataset_version": dataset_spec.dataset_version,
            "window_start": dataset_spec.window_start.isoformat(),
            "window_end": dataset_spec.window_end.isoformat(),
            "segments": [
                {
                    "name": segment.name,
                    "start": segment.start.isoformat(),
                    "end": segment.end.isoformat(),
                    "purpose": segment.purpose,
                }
                for segment in dataset_spec.segments
            ],
            "source_refs": _normalize_cache_value(dataset_spec.source_refs),
        },
        "source_watermark": _normalize_cache_value(source_watermark),
        "processor_versions": _normalize_cache_value(
            _normalize_processor_versions(processor_versions)
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"rfds_{hashlib.sha256(encoded).hexdigest()}"


def _validate_records(records: Sequence[GoldBarRecord]) -> tuple[GoldBarRecord, ...]:
    validated_records = tuple(records)
    if not all(isinstance(record, GoldBarRecord) for record in validated_records):
        raise ValueError("records must contain GoldBarRecord instances")
    return validated_records


def _matching_segment_names(ts: datetime, segments: Sequence[SegmentSpec]) -> tuple[str, ...]:
    return tuple(segment.name for segment in segments if segment.start <= ts < segment.end)


def _require_matching_scope(record: GoldBarRecord, dataset_spec: DatasetSpec) -> None:
    if record.symbol != dataset_spec.symbol:
        raise ValueError(
            f"record symbol {record.symbol!r} does not match dataset symbol {dataset_spec.symbol!r}"
        )
    if record.timeframe != dataset_spec.timeframe:
        raise ValueError(
            "record timeframe "
            f"{record.timeframe!r} does not match dataset timeframe {dataset_spec.timeframe!r}"
        )


def _require_unique_segment_names(segments: Sequence[SegmentSpec]) -> None:
    names = [segment.name for segment in segments]
    if len(names) != len(set(names)):
        raise ValueError("dataset segment names must be unique")


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_aware_datetime(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _require_cache_material(value: Any, field_name: str) -> None:
    if value is None:
        raise ValueError(f"{field_name} is required for production cache")
    if isinstance(value, str) and not value.strip():
        raise ValueError(f"{field_name} is required for production cache")
    if isinstance(value, Mapping) and not value:
        raise ValueError(f"{field_name} is required for production cache")
    if (
        isinstance(value, Sequence)
        and not isinstance(value, str | bytes | bytearray)
        and not value
    ):
        raise ValueError(f"{field_name} is required for production cache")


def _normalize_processor_versions(
    processor_versions: Mapping[str, Any] | Sequence[ProcessorSpec],
) -> Mapping[str, Any]:
    if isinstance(processor_versions, Mapping):
        return dict(processor_versions)
    if not all(isinstance(processor, ProcessorSpec) for processor in processor_versions):
        raise ValueError("processor_versions must be a mapping or ProcessorSpec sequence")
    return {processor.name: processor.version for processor in processor_versions}


def _normalize_cache_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_cache_value(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_normalize_cache_value(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, str):
        return _normalize_cache_string(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    return _normalize_cache_string(str(value))


def _normalize_cache_string(value: str) -> str | Mapping[str, str]:
    path_ref_name = _absolute_path_ref_name(value)
    if path_ref_name is not None:
        return {"path_ref": path_ref_name}
    return value


def _absolute_path_ref_name(value: str) -> str | None:
    windows_path = PureWindowsPath(value)
    if windows_path.is_absolute():
        return windows_path.name
    posix_path = PurePosixPath(value)
    if posix_path.is_absolute():
        return posix_path.name
    pure_path = PurePath(value)
    if pure_path.is_absolute():
        return pure_path.name
    return None
