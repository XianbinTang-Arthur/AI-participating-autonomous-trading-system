"""Read-only shadow/paper observation event aggregation for Research Factory."""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from aats.data_platform.research_factory.observation_sources import (
    OBSERVATION_SUMMARY_SCHEMA_VERSION,
)

ALLOWED_OBSERVATION_SUMMARY_MODES = frozenset({"shadow", "paper"})
OBSERVATION_SUMMARY_GENERATOR_SCHEMA_VERSION = "research_observation_event_v1"
FORBIDDEN_OBSERVATION_SUMMARY_PATH_TOKENS = (
    ".env",
    "api_key",
    "credential",
    "credentials",
    "live",
    "passwd",
    "password",
    "private_key",
    "production_config",
    "secret",
    "token",
)


@dataclass(frozen=True, slots=True)
class ObservationEventRecord:
    """One read-only shadow/paper observation event exported to artifacts."""

    ts: datetime
    recommendation_id: str
    candidate_id: str
    experiment_id: str
    mode: str
    bar_ts: datetime | None = None
    signal: bool = False
    paper_intent: bool = False
    fillable: bool | None = None
    partial_fill: bool | None = None
    fillable_ratio: float | None = None
    partial_fill_ratio: float | None = None
    fee_bps: float | None = None
    slippage_bps: float | None = None
    funding_bps: float | None = None
    cost_adjusted_edge_bps: float | None = None
    drawdown: float | None = None
    metric_drift: float | None = None
    abort_triggered: bool = False
    abort_reason: str | None = None
    source_event_id: str | None = None

    def __post_init__(self) -> None:
        _require_timezone_aware_datetime(self.ts, "ts")
        if self.bar_ts is not None:
            _require_timezone_aware_datetime(self.bar_ts, "bar_ts")
        _require_safe_identifier(self.recommendation_id, "recommendation_id")
        _require_safe_identifier(self.candidate_id, "candidate_id")
        _require_safe_identifier(self.experiment_id, "experiment_id")
        if self.mode not in ALLOWED_OBSERVATION_SUMMARY_MODES:
            allowed = ", ".join(sorted(ALLOWED_OBSERVATION_SUMMARY_MODES))
            raise ValueError(f"mode must be one of: {allowed}")
        for field_name in (
            "fillable_ratio",
            "partial_fill_ratio",
            "fee_bps",
            "slippage_bps",
            "funding_bps",
            "cost_adjusted_edge_bps",
            "drawdown",
            "metric_drift",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _require_finite_number(value, field_name))
        if self.abort_reason is not None:
            object.__setattr__(
                self,
                "abort_reason",
                _require_non_empty_text(self.abort_reason, "abort_reason"),
            )
        if self.source_event_id is not None:
            object.__setattr__(
                self,
                "source_event_id",
                _require_non_empty_text(self.source_event_id, "source_event_id"),
            )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ObservationEventRecord:
        """Load one event from a JSONL mapping."""
        if not isinstance(payload, Mapping):
            raise ValueError("observation event must be a mapping")
        return cls(
            ts=_parse_datetime(_require_mapping_value(payload, "ts"), "ts"),
            bar_ts=_parse_optional_datetime(payload.get("bar_ts"), "bar_ts"),
            recommendation_id=_require_mapping_text(payload, "recommendation_id"),
            candidate_id=_require_mapping_text(payload, "candidate_id"),
            experiment_id=_require_mapping_text(payload, "experiment_id"),
            mode=_require_mapping_text(payload, "mode"),
            signal=bool(payload.get("signal", False)),
            paper_intent=bool(payload.get("paper_intent", False)),
            fillable=_optional_bool(payload.get("fillable"), "fillable"),
            partial_fill=_optional_bool(payload.get("partial_fill"), "partial_fill"),
            fillable_ratio=_optional_number(payload.get("fillable_ratio"), "fillable_ratio"),
            partial_fill_ratio=_optional_number(payload.get("partial_fill_ratio"), "partial_fill_ratio"),
            fee_bps=_optional_number(payload.get("fee_bps"), "fee_bps"),
            slippage_bps=_optional_number(payload.get("slippage_bps"), "slippage_bps"),
            funding_bps=_optional_number(payload.get("funding_bps"), "funding_bps"),
            cost_adjusted_edge_bps=_optional_number(
                payload.get("cost_adjusted_edge_bps"),
                "cost_adjusted_edge_bps",
            ),
            drawdown=_optional_number(payload.get("drawdown"), "drawdown"),
            metric_drift=_optional_number(payload.get("metric_drift"), "metric_drift"),
            abort_triggered=bool(payload.get("abort_triggered", False)),
            abort_reason=_optional_text(payload.get("abort_reason"), "abort_reason"),
            source_event_id=_optional_text(payload.get("source_event_id"), "source_event_id"),
        )


@dataclass(frozen=True, slots=True)
class ObservationSummary:
    """JSON summary consumed by ReadOnlyObservationDataSource."""

    mode: str
    recommendation_id: str
    candidate_id: str
    experiment_id: str
    observation_start: datetime
    observation_end: datetime
    observed_bars: int
    observed_events: int
    signal_count: int
    paper_intent_count: int
    fillable_ratio: float
    partial_fill_ratio: float
    fee_bps_mean: float
    slippage_bps_mean: float
    funding_bps_mean: float
    cost_adjusted_edge_bps_mean: float
    drawdown: float
    metric_drift: float
    abort_triggered: bool
    abort_reason: str | None = None
    source_event_count: int = 0
    source_artifact_ref: str | None = None
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = OBSERVATION_SUMMARY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.mode not in ALLOWED_OBSERVATION_SUMMARY_MODES:
            allowed = ", ".join(sorted(ALLOWED_OBSERVATION_SUMMARY_MODES))
            raise ValueError(f"mode must be one of: {allowed}")
        _require_safe_identifier(self.recommendation_id, "recommendation_id")
        _require_safe_identifier(self.candidate_id, "candidate_id")
        _require_safe_identifier(self.experiment_id, "experiment_id")
        _require_timezone_aware_datetime(self.observation_start, "observation_start")
        _require_timezone_aware_datetime(self.observation_end, "observation_end")
        if self.observation_end < self.observation_start:
            raise ValueError("observation_end must be after or equal to observation_start")
        for field_name in ("observed_bars", "observed_events", "signal_count", "paper_intent_count"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        for field_name in (
            "fillable_ratio",
            "partial_fill_ratio",
            "fee_bps_mean",
            "slippage_bps_mean",
            "funding_bps_mean",
            "cost_adjusted_edge_bps_mean",
            "drawdown",
            "metric_drift",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_finite_number(getattr(self, field_name), field_name),
            )
        if self.abort_reason is not None:
            object.__setattr__(
                self,
                "abort_reason",
                _require_non_empty_text(self.abort_reason, "abort_reason"),
            )
        if not isinstance(self.source_event_count, int) or self.source_event_count < 0:
            raise ValueError("source_event_count must be a non-negative integer")
        if self.source_artifact_ref is not None:
            object.__setattr__(
                self,
                "source_artifact_ref",
                _require_relative_ref(self.source_artifact_ref, "source_artifact_ref"),
            )
        _require_timezone_aware_datetime(self.generated_at, "generated_at")
        if self.schema_version != OBSERVATION_SUMMARY_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {OBSERVATION_SUMMARY_SCHEMA_VERSION!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "recommendation_id": self.recommendation_id,
            "candidate_id": self.candidate_id,
            "experiment_id": self.experiment_id,
            "observation_start": self.observation_start.isoformat(),
            "observation_end": self.observation_end.isoformat(),
            "observed_bars": self.observed_bars,
            "observed_events": self.observed_events,
            "signal_count": self.signal_count,
            "paper_intent_count": self.paper_intent_count,
            "fillable_ratio": self.fillable_ratio,
            "partial_fill_ratio": self.partial_fill_ratio,
            "fee_bps_mean": self.fee_bps_mean,
            "slippage_bps_mean": self.slippage_bps_mean,
            "funding_bps_mean": self.funding_bps_mean,
            "cost_adjusted_edge_bps_mean": self.cost_adjusted_edge_bps_mean,
            "drawdown": self.drawdown,
            "metric_drift": self.metric_drift,
            "abort_triggered": self.abort_triggered,
            "abort_reason": self.abort_reason,
            "source_event_count": self.source_event_count,
            "source_artifact_ref": self.source_artifact_ref,
            "generated_at": self.generated_at.isoformat(),
        }


def load_observation_events_jsonl(path: str | Path) -> tuple[ObservationEventRecord, ...]:
    """Load observation event records from a research artifact JSONL file."""
    event_path = require_research_artifact_file(path, "events_jsonl", suffix=".jsonl")
    events: list[ObservationEventRecord] = []
    for line_number, line in enumerate(event_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"events_jsonl line {line_number} must be valid JSON") from exc
        events.append(ObservationEventRecord.from_mapping(payload))
    if not events:
        raise ValueError("events_jsonl must contain at least one event")
    return tuple(events)


def build_observation_summary_from_events(
    events: Iterable[ObservationEventRecord],
    *,
    recommendation_id: str,
    candidate_id: str,
    experiment_id: str,
    mode: str,
    source_artifact_ref: str | None = None,
    generated_at: datetime | None = None,
) -> ObservationSummary:
    """Aggregate shadow/paper observation events into a summary JSON payload."""
    events_tuple = tuple(events)
    if not events_tuple:
        raise ValueError("events must contain at least one observation event")
    if mode not in ALLOWED_OBSERVATION_SUMMARY_MODES:
        allowed = ", ".join(sorted(ALLOWED_OBSERVATION_SUMMARY_MODES))
        raise ValueError(f"mode must be one of: {allowed}")
    for event in events_tuple:
        if not isinstance(event, ObservationEventRecord):
            raise ValueError("events must be ObservationEventRecord instances")
        if event.recommendation_id != recommendation_id:
            raise ValueError("event recommendation_id must match requested recommendation_id")
        if event.candidate_id != candidate_id:
            raise ValueError("event candidate_id must match requested candidate_id")
        if event.experiment_id != experiment_id:
            raise ValueError("event experiment_id must match requested experiment_id")
        if event.mode != mode:
            raise ValueError("event mode must match requested mode")

    observed_bar_keys = {event.bar_ts or event.ts for event in events_tuple}
    abort_reasons = tuple(event.abort_reason for event in events_tuple if event.abort_reason)
    return ObservationSummary(
        mode=mode,
        recommendation_id=recommendation_id,
        candidate_id=candidate_id,
        experiment_id=experiment_id,
        observation_start=min(event.ts for event in events_tuple),
        observation_end=max(event.ts for event in events_tuple),
        observed_bars=len(observed_bar_keys),
        observed_events=len(events_tuple),
        signal_count=sum(1 for event in events_tuple if event.signal),
        paper_intent_count=sum(1 for event in events_tuple if event.paper_intent),
        fillable_ratio=_mean_metric(
            tuple(_event_ratio(event.fillable_ratio, event.fillable) for event in events_tuple),
            "fillable_ratio",
        ),
        partial_fill_ratio=_mean_metric(
            tuple(_event_ratio(event.partial_fill_ratio, event.partial_fill) for event in events_tuple),
            "partial_fill_ratio",
        ),
        fee_bps_mean=_mean_metric(tuple(event.fee_bps for event in events_tuple), "fee_bps"),
        slippage_bps_mean=_mean_metric(
            tuple(event.slippage_bps for event in events_tuple),
            "slippage_bps",
        ),
        funding_bps_mean=_mean_metric(tuple(event.funding_bps for event in events_tuple), "funding_bps"),
        cost_adjusted_edge_bps_mean=_mean_metric(
            tuple(event.cost_adjusted_edge_bps for event in events_tuple),
            "cost_adjusted_edge_bps",
        ),
        drawdown=max(_metric_values(tuple(event.drawdown for event in events_tuple), "drawdown")),
        metric_drift=max(
            _metric_values(tuple(event.metric_drift for event in events_tuple), "metric_drift")
        ),
        abort_triggered=any(event.abort_triggered for event in events_tuple),
        abort_reason=abort_reasons[0] if abort_reasons else None,
        source_event_count=len(events_tuple),
        source_artifact_ref=source_artifact_ref,
        generated_at=generated_at or datetime.now(UTC),
    )


def write_observation_summary(
    summary: ObservationSummary,
    output_path: str | Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Write an observation summary JSON atomically under artifacts/research."""
    if not isinstance(summary, ObservationSummary):
        raise ValueError("summary must be ObservationSummary")
    path = require_research_artifact_file(output_path, "output_path", suffix=".json", must_exist=False)
    if path.exists() and not overwrite:
        raise ValueError("output_path already exists; pass overwrite=True to replace it")
    _write_json_atomic(path, summary.to_dict())
    return path


def require_research_artifact_file(
    path: str | Path,
    field_name: str,
    *,
    suffix: str,
    must_exist: bool = True,
) -> Path:
    """Validate a file path is inside artifacts/research and has the expected suffix."""
    artifact_path = Path(path)
    _reject_unsafe_path_parts(artifact_path, field_name)
    if artifact_path.suffix.lower() != suffix:
        raise ValueError(f"{field_name} must be a {suffix} research artifact")
    if not _is_under_research_artifacts(artifact_path):
        raise ValueError(f"{field_name} must be under artifacts/research")
    if must_exist:
        if not artifact_path.exists():
            raise ValueError(f"{field_name} does not exist")
        if not artifact_path.is_file():
            raise ValueError(f"{field_name} must be a file")
    return artifact_path


def _event_ratio(value: float | None, flag: bool | None) -> float | None:
    if value is not None:
        return value
    if flag is None:
        return None
    return 1.0 if flag else 0.0


def _mean_metric(values: Sequence[float | None], field_name: str) -> float:
    metric_values = _metric_values(values, field_name)
    return sum(metric_values) / len(metric_values)


def _metric_values(values: Sequence[float | None], field_name: str) -> tuple[float, ...]:
    normalized = tuple(_require_finite_number(value, field_name) for value in values if value is not None)
    if not normalized:
        raise ValueError(f"events must contain at least one {field_name} value")
    return normalized


def _require_mapping_value(payload: Mapping[str, Any], field_name: str) -> Any:
    if field_name not in payload:
        raise ValueError(f"observation event {field_name} is required")
    return payload[field_name]


def _require_mapping_text(payload: Mapping[str, Any], field_name: str) -> str:
    return _require_non_empty_text(_require_mapping_value(payload, field_name), field_name)


def _parse_datetime(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        parsed = datetime.fromisoformat(raw)
    else:
        raise ValueError(f"{field_name} must be an ISO datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _parse_optional_datetime(value: Any, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _parse_datetime(value, field_name)


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_non_empty_text(value, field_name)


def _optional_bool(value: Any, field_name: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a bool when present")
    return value


def _optional_number(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    return _require_finite_number(value, field_name)


def _require_finite_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be finite")
    return result


def _require_safe_identifier(value: Any, field_name: str) -> str:
    value = _require_non_empty_text(value, field_name)
    if "/" in value or "\\" in value or value in {".", ".."} or ".." in value:
        raise ValueError(f"{field_name} must not contain path traversal or separators")
    return value


def _require_non_empty_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _require_relative_ref(value: Any, field_name: str) -> str:
    ref = _require_non_empty_text(value, field_name)
    posix_path = PurePosixPath(ref)
    windows_path = PureWindowsPath(ref)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError(f"{field_name} must be a relative artifact ref")
    if ".." in posix_path.parts or ".." in windows_path.parts:
        raise ValueError(f"{field_name} must not contain path traversal")
    return ref


def _require_timezone_aware_datetime(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _is_under_research_artifacts(path: Path) -> bool:
    resolved = path.resolve(strict=False)
    parts = resolved.parts
    return any(
        parts[index] == "artifacts" and parts[index + 1] == "research"
        for index in range(len(parts) - 1)
    )


def _reject_unsafe_path_parts(path: Path, field_name: str) -> None:
    if not path.parts:
        raise ValueError(f"{field_name} must be a non-empty path")
    if str(path).startswith("~") or "~" in path.parts:
        raise ValueError(f"{field_name} must not use home-directory expansion")
    if ".." in path.parts:
        raise ValueError(f"{field_name} must not contain path traversal")
    lowered = [part.lower() for part in path.parts]
    for part in lowered:
        for token in FORBIDDEN_OBSERVATION_SUMMARY_PATH_TOKENS:
            if token in part:
                raise ValueError(f"{field_name} contains forbidden path token: {token}")


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            temp_path = handle.name
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
