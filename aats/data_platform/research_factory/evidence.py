"""Evidence-quality contracts for Research Factory real-data recommendations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aats.data_platform.research_factory.datasets.gold_bars import (
    GoldBarRecord,
    PreparedGoldBarDataset,
)
from aats.data_platform.research_factory.numeric import require_finite_number
from aats.data_platform.research_factory.specs import DatasetSpec

EVIDENCE_SCHEMA_VERSION = "research_factory_evidence_v1"
TIMEFRAME_DELTAS = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
}


@dataclass(frozen=True, slots=True)
class DatasetQualityThresholds:
    """Minimum dataset quality required before recommendation generation."""

    min_total_bars: int = 10
    min_train_bars: int = 2
    min_valid_bars: int = 2
    min_test_bars: int = 2
    max_bar_gap_ratio: float = 0.0
    max_funding_missing_ratio: float = 0.0

    def __post_init__(self) -> None:
        _require_positive_int(self.min_total_bars, "min_total_bars")
        _require_positive_int(self.min_train_bars, "min_train_bars")
        _require_positive_int(self.min_valid_bars, "min_valid_bars")
        _require_positive_int(self.min_test_bars, "min_test_bars")
        object.__setattr__(
            self,
            "max_bar_gap_ratio",
            _require_ratio(self.max_bar_gap_ratio, "max_bar_gap_ratio"),
        )
        object.__setattr__(
            self,
            "max_funding_missing_ratio",
            _require_ratio(self.max_funding_missing_ratio, "max_funding_missing_ratio"),
        )


@dataclass(frozen=True, slots=True)
class DatasetQualityReport:
    """Audit report for Gold replay dataset coverage and completeness."""

    dataset_id: str
    dataset_fingerprint: str
    timeframe: str
    window_start: datetime
    window_end: datetime
    row_count: int
    expected_bar_count: int
    expected_interval_seconds: float
    missing_bar_count: int
    bar_gap_ratio: float
    max_gap_seconds: float
    funding_missing_count: int
    funding_missing_ratio: float
    segment_row_counts: Mapping[str, int]
    thresholds: DatasetQualityThresholds
    passed: bool
    failures: Sequence[str]
    created_at: datetime
    schema_version: str = EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_non_empty(self.dataset_id, "dataset_id")
        _require_non_empty(self.dataset_fingerprint, "dataset_fingerprint")
        _require_non_empty(self.timeframe, "timeframe")
        _require_aware_datetime(self.window_start, "window_start")
        _require_aware_datetime(self.window_end, "window_end")
        _require_non_negative_int(self.row_count, "row_count")
        _require_non_negative_int(self.expected_bar_count, "expected_bar_count")
        _require_non_negative_int(self.missing_bar_count, "missing_bar_count")
        _require_non_negative_int(self.funding_missing_count, "funding_missing_count")
        object.__setattr__(
            self,
            "expected_interval_seconds",
            _require_non_negative_number(
                self.expected_interval_seconds,
                "expected_interval_seconds",
            ),
        )
        object.__setattr__(
            self,
            "bar_gap_ratio",
            _require_ratio(self.bar_gap_ratio, "bar_gap_ratio"),
        )
        object.__setattr__(
            self,
            "max_gap_seconds",
            _require_non_negative_number(self.max_gap_seconds, "max_gap_seconds"),
        )
        object.__setattr__(
            self,
            "funding_missing_ratio",
            _require_ratio(self.funding_missing_ratio, "funding_missing_ratio"),
        )
        _require_aware_datetime(self.created_at, "created_at")
        if not isinstance(self.thresholds, DatasetQualityThresholds):
            raise ValueError("thresholds must be DatasetQualityThresholds")
        object.__setattr__(self, "segment_row_counts", _normalize_segment_counts(self.segment_row_counts))
        object.__setattr__(self, "failures", _normalize_failures(self.failures, self.passed))


@dataclass(frozen=True, slots=True)
class SourceIntegrityReport:
    """Audit report for source version consistency and traceability."""

    dataset_id: str
    source_candle_dataset_versions: Sequence[str]
    source_funding_dataset_versions: Sequence[str]
    build_run_ids: Sequence[str]
    source_watermark: Mapping[str, Any]
    candle_version_consistent: bool
    funding_version_consistent: bool
    build_run_traceable: bool
    timestamp_timezone_assumption: str
    passed: bool
    failures: Sequence[str]
    created_at: datetime
    schema_version: str = EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_non_empty(self.dataset_id, "dataset_id")
        if not isinstance(self.source_watermark, Mapping):
            raise ValueError("source_watermark must be a mapping")
        _require_non_empty(
            self.timestamp_timezone_assumption,
            "timestamp_timezone_assumption",
        )
        _require_aware_datetime(self.created_at, "created_at")
        object.__setattr__(
            self,
            "source_candle_dataset_versions",
            _normalize_text_tuple(
                self.source_candle_dataset_versions,
                "source_candle_dataset_versions",
            ),
        )
        object.__setattr__(
            self,
            "source_funding_dataset_versions",
            _normalize_text_tuple(
                self.source_funding_dataset_versions,
                "source_funding_dataset_versions",
            ),
        )
        object.__setattr__(
            self,
            "build_run_ids",
            _normalize_text_tuple(self.build_run_ids, "build_run_ids"),
        )
        object.__setattr__(self, "source_watermark", dict(self.source_watermark))
        object.__setattr__(self, "failures", _normalize_failures(self.failures, self.passed))


@dataclass(frozen=True, slots=True)
class ExecutionEvidenceReport:
    """Audit report for execution realism summary identity and window match."""

    dataset_id: str
    evidence_ref: str
    symbol: str | None
    timeframe: str | None
    window_start: datetime | None
    window_end: datetime | None
    dataset_fingerprint: str | None
    passed: bool
    failures: Sequence[str]
    created_at: datetime
    schema_version: str = EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_non_empty(self.dataset_id, "dataset_id")
        _require_non_empty(self.evidence_ref, "evidence_ref")
        if self.symbol is not None:
            _require_non_empty(self.symbol, "symbol")
        if self.timeframe is not None:
            _require_non_empty(self.timeframe, "timeframe")
        if self.window_start is not None:
            _require_aware_datetime(self.window_start, "window_start")
        if self.window_end is not None:
            _require_aware_datetime(self.window_end, "window_end")
        if self.dataset_fingerprint is not None:
            _require_non_empty(self.dataset_fingerprint, "dataset_fingerprint")
        _require_aware_datetime(self.created_at, "created_at")
        object.__setattr__(self, "failures", _normalize_failures(self.failures, self.passed))


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """Combined evidence gate result for recommendation eligibility."""

    dataset_quality: DatasetQualityReport
    source_integrity: SourceIntegrityReport
    execution_evidence: ExecutionEvidenceReport | None
    execution_evidence_required: bool
    passed: bool
    failures: Sequence[str]
    created_at: datetime
    schema_version: str = EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.dataset_quality, DatasetQualityReport):
            raise ValueError("dataset_quality must be DatasetQualityReport")
        if not isinstance(self.source_integrity, SourceIntegrityReport):
            raise ValueError("source_integrity must be SourceIntegrityReport")
        if self.execution_evidence is not None and not isinstance(
            self.execution_evidence,
            ExecutionEvidenceReport,
        ):
            raise ValueError("execution_evidence must be ExecutionEvidenceReport")
        if not isinstance(self.execution_evidence_required, bool):
            raise ValueError("execution_evidence_required must be a bool")
        _require_aware_datetime(self.created_at, "created_at")
        object.__setattr__(self, "failures", _normalize_failures(self.failures, self.passed))


def build_dataset_quality_report(
    *,
    records: Sequence[GoldBarRecord],
    prepared: PreparedGoldBarDataset,
    dataset_spec: DatasetSpec,
    dataset_fingerprint: str,
    thresholds: DatasetQualityThresholds,
    created_at: datetime,
) -> DatasetQualityReport:
    """Build a deterministic Gold replay dataset quality report."""
    if not isinstance(prepared, PreparedGoldBarDataset):
        raise ValueError("prepared must be PreparedGoldBarDataset")
    if not isinstance(dataset_spec, DatasetSpec):
        raise ValueError("dataset_spec must be DatasetSpec")
    if not isinstance(thresholds, DatasetQualityThresholds):
        raise ValueError("thresholds must be DatasetQualityThresholds")

    interval = _timeframe_delta(dataset_spec.timeframe)
    rows = tuple(
        sorted(
            (
                record
                for record in records
                if dataset_spec.window_start <= record.ts < dataset_spec.window_end
            ),
            key=lambda record: record.ts,
        )
    )
    row_count = len(rows)
    expected_bar_count = _expected_bar_count(dataset_spec, interval)
    missing_bar_count = max(expected_bar_count - row_count, 0)
    bar_gap_ratio = missing_bar_count / expected_bar_count if expected_bar_count else 1.0
    max_gap_seconds = _max_gap_seconds(rows)
    funding_missing_count = sum(1 for record in rows if record.funding_rate is None)
    funding_missing_ratio = funding_missing_count / row_count if row_count else 1.0
    segment_row_counts = {
        segment.name: len(prepared.rows_for_segment(segment.name))
        for segment in dataset_spec.segments
    }
    failures: list[str] = []
    if row_count < thresholds.min_total_bars:
        failures.append(f"row_count={row_count} < min_total_bars={thresholds.min_total_bars}")
    for segment_name, minimum in (
        ("train", thresholds.min_train_bars),
        ("valid", thresholds.min_valid_bars),
        ("test", thresholds.min_test_bars),
    ):
        count = segment_row_counts.get(segment_name, 0)
        if count < minimum:
            failures.append(f"{segment_name}_rows={count} < min_{segment_name}_bars={minimum}")
    if bar_gap_ratio > thresholds.max_bar_gap_ratio:
        failures.append(
            f"bar_gap_ratio={bar_gap_ratio:.6f} > "
            f"max_bar_gap_ratio={thresholds.max_bar_gap_ratio:.6f}"
        )
    if max_gap_seconds > interval.total_seconds():
        failures.append(
            f"max_gap_seconds={max_gap_seconds:.6f} > "
            f"expected_interval_seconds={interval.total_seconds():.6f}"
        )
    if funding_missing_ratio > thresholds.max_funding_missing_ratio:
        failures.append(
            f"funding_missing_ratio={funding_missing_ratio:.6f} > "
            f"max_funding_missing_ratio={thresholds.max_funding_missing_ratio:.6f}"
        )

    return DatasetQualityReport(
        dataset_id=dataset_spec.dataset_id,
        dataset_fingerprint=dataset_fingerprint,
        timeframe=dataset_spec.timeframe,
        window_start=dataset_spec.window_start,
        window_end=dataset_spec.window_end,
        row_count=row_count,
        expected_bar_count=expected_bar_count,
        expected_interval_seconds=interval.total_seconds(),
        missing_bar_count=missing_bar_count,
        bar_gap_ratio=bar_gap_ratio,
        max_gap_seconds=max_gap_seconds,
        funding_missing_count=funding_missing_count,
        funding_missing_ratio=funding_missing_ratio,
        segment_row_counts=segment_row_counts,
        thresholds=thresholds,
        passed=not failures,
        failures=tuple(failures),
        created_at=created_at,
    )


def build_source_integrity_report(
    *,
    records: Sequence[GoldBarRecord],
    dataset_spec: DatasetSpec,
    source_watermark: Mapping[str, Any],
    created_at: datetime,
) -> SourceIntegrityReport:
    """Build a source-integrity report from Gold replay metadata."""
    candle_versions = _metadata_values(
        records,
        "source_candle_dataset_version",
        source_watermark.get("source_candle_dataset_versions"),
    )
    funding_versions = _metadata_values(
        records,
        "source_funding_dataset_version",
        source_watermark.get("source_funding_dataset_versions"),
    )
    build_run_ids = _metadata_values(
        records,
        "build_run_id",
        source_watermark.get("build_run_ids"),
    )
    candle_version_consistent = len(candle_versions) == 1 and (
        candle_versions[0] == dataset_spec.dataset_version
        or dataset_spec.dataset_version == "gold_replay_mixed_versions"
    )
    funding_version_consistent = len(funding_versions) == 1
    build_run_traceable = bool(build_run_ids)
    failures: list[str] = []
    if not candle_version_consistent:
        failures.append("source candle dataset version must be single and match dataset_version")
    if not funding_version_consistent:
        failures.append("source funding dataset version must be single and traceable")
    if not build_run_traceable:
        failures.append("build_run_id is required for source traceability")

    return SourceIntegrityReport(
        dataset_id=dataset_spec.dataset_id,
        source_candle_dataset_versions=candle_versions,
        source_funding_dataset_versions=funding_versions,
        build_run_ids=build_run_ids,
        source_watermark=source_watermark,
        candle_version_consistent=candle_version_consistent,
        funding_version_consistent=funding_version_consistent,
        build_run_traceable=build_run_traceable,
        timestamp_timezone_assumption=str(
            source_watermark.get(
                "timestamp_timezone_assumption",
                "timezone-aware database timestamp",
            )
        ),
        passed=not failures,
        failures=tuple(failures),
        created_at=created_at,
    )


def build_execution_evidence_report(
    *,
    summary: Mapping[str, Any],
    dataset_spec: DatasetSpec,
    dataset_fingerprint: str,
    evidence_ref: str,
    created_at: datetime,
) -> ExecutionEvidenceReport:
    """Verify execution evidence is aligned with the experiment dataset."""
    if not isinstance(summary, Mapping):
        raise ValueError("summary must be a mapping")
    symbol = _optional_text(summary.get("symbol"))
    timeframe = _optional_text(summary.get("timeframe"))
    window_start = _optional_datetime(summary.get("window_start"))
    window_end = _optional_datetime(summary.get("window_end"))
    summary_dataset_fingerprint = _optional_text(summary.get("dataset_fingerprint"))
    failures: list[str] = []
    if symbol is None:
        failures.append("execution evidence symbol is required")
    elif symbol.upper() != dataset_spec.symbol.upper():
        failures.append("execution evidence symbol must match dataset symbol")
    if timeframe is None:
        failures.append("execution evidence timeframe is required")
    elif timeframe.lower() != dataset_spec.timeframe.lower():
        failures.append("execution evidence timeframe must match dataset timeframe")
    if window_start is None:
        failures.append("execution evidence window_start is required")
    elif window_start != dataset_spec.window_start:
        failures.append("execution evidence window_start must match dataset window_start")
    if window_end is None:
        failures.append("execution evidence window_end is required")
    elif window_end != dataset_spec.window_end:
        failures.append("execution evidence window_end must match dataset window_end")
    if summary_dataset_fingerprint is not None and summary_dataset_fingerprint != dataset_fingerprint:
        failures.append("execution evidence dataset_fingerprint must match dataset fingerprint")

    return ExecutionEvidenceReport(
        dataset_id=dataset_spec.dataset_id,
        evidence_ref=evidence_ref,
        symbol=symbol,
        timeframe=timeframe,
        window_start=window_start,
        window_end=window_end,
        dataset_fingerprint=summary_dataset_fingerprint,
        passed=not failures,
        failures=tuple(failures),
        created_at=created_at,
    )


def build_evidence_bundle(
    *,
    dataset_quality: DatasetQualityReport,
    source_integrity: SourceIntegrityReport,
    execution_evidence: ExecutionEvidenceReport | None,
    execution_evidence_required: bool,
    created_at: datetime,
) -> EvidenceBundle:
    """Combine evidence reports into the recommendation eligibility gate."""
    failures: list[str] = []
    if not dataset_quality.passed:
        failures.extend(f"dataset_quality: {failure}" for failure in dataset_quality.failures)
    if not source_integrity.passed:
        failures.extend(f"source_integrity: {failure}" for failure in source_integrity.failures)
    if execution_evidence_required and execution_evidence is None:
        failures.append("execution_evidence: execution evidence report is required")
    if execution_evidence is not None and not execution_evidence.passed:
        failures.extend(f"execution_evidence: {failure}" for failure in execution_evidence.failures)

    return EvidenceBundle(
        dataset_quality=dataset_quality,
        source_integrity=source_integrity,
        execution_evidence=execution_evidence,
        execution_evidence_required=execution_evidence_required,
        passed=not failures,
        failures=tuple(failures),
        created_at=created_at,
    )


def _timeframe_delta(timeframe: str) -> timedelta:
    normalized = timeframe.lower()
    if normalized not in TIMEFRAME_DELTAS:
        allowed = ", ".join(sorted(TIMEFRAME_DELTAS))
        raise ValueError(f"timeframe must be one of: {allowed}")
    return TIMEFRAME_DELTAS[normalized]


def _expected_bar_count(dataset_spec: DatasetSpec, interval: timedelta) -> int:
    window_seconds = (dataset_spec.window_end - dataset_spec.window_start).total_seconds()
    interval_seconds = interval.total_seconds()
    if window_seconds <= 0:
        return 0
    quotient = window_seconds / interval_seconds
    if quotient != int(quotient):
        return int(quotient) + 1
    return int(quotient)


def _max_gap_seconds(records: Sequence[GoldBarRecord]) -> float:
    if len(records) < 2:
        return 0.0
    return max(
        (right.ts - left.ts).total_seconds()
        for left, right in zip(records, records[1:])
    )


def _metadata_values(
    records: Sequence[GoldBarRecord],
    key: str,
    fallback: Any,
) -> tuple[str, ...]:
    values = {
        str(record.metadata[key]).strip()
        for record in records
        if isinstance(record.metadata, Mapping)
        and record.metadata.get(key) is not None
        and str(record.metadata[key]).strip()
    }
    if values:
        return tuple(sorted(values))
    if isinstance(fallback, Sequence) and not isinstance(fallback, str | bytes | bytearray):
        return tuple(sorted(str(item).strip() for item in fallback if str(item).strip()))
    if isinstance(fallback, str) and fallback.strip():
        return (fallback.strip(),)
    return ()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _optional_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _ensure_aware(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return _ensure_aware(parsed)
    return None


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value


def _normalize_segment_counts(value: Mapping[str, int]) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError("segment_row_counts must be a mapping")
    return {str(name): _require_non_negative_int(count, str(name)) for name, count in value.items()}


def _normalize_text_tuple(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, str | bytes | bytearray) or not isinstance(values, Sequence):
        raise ValueError(f"{field_name} must be a sequence")
    return tuple(str(value) for value in values)


def _normalize_failures(values: Sequence[str], passed: bool) -> tuple[str, ...]:
    if isinstance(values, str | bytes | bytearray) or not isinstance(values, Sequence):
        raise ValueError("failures must be a sequence")
    failures = tuple(_require_non_empty(str(value), "failure") for value in values)
    if passed and failures:
        raise ValueError("passing evidence report must not contain failures")
    if not passed and not failures:
        raise ValueError("failing evidence report must contain failures")
    return failures


def _require_positive_int(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _require_non_negative_int(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _require_ratio(value: Any, field_name: str) -> float:
    result = require_finite_number(value, field_name)
    if result < 0 or result > 1:
        raise ValueError(f"{field_name} must be between 0 and 1")
    return result


def _require_non_negative_number(value: Any, field_name: str) -> float:
    result = require_finite_number(value, field_name)
    if result < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return result


def _require_non_empty(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_aware_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value
