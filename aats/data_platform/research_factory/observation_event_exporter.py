"""Normalize read-only shadow/paper event exports into observation event JSONL."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from aats.data_platform.research_factory.observation_summary_generator import (
    ALLOWED_OBSERVATION_SUMMARY_MODES,
    OBSERVATION_SUMMARY_GENERATOR_SCHEMA_VERSION,
    ObservationEventRecord,
    require_research_artifact_file,
)

OBSERVATION_EVENT_EXPORTER_SCHEMA_VERSION = "research_observation_event_export_v1"

SOURCE_EVENT_KINDS = frozenset({"shadow_decision", "paper_intent", "observation_event"})

_TIMESTAMP_ALIASES = ("ts", "timestamp", "event_ts", "created_at", "evaluated_at")
_BAR_TIMESTAMP_ALIASES = ("bar_ts", "bar_timestamp", "market_ts", "candle_ts")
_FILLABLE_ALIASES = ("fillable", "is_fillable")
_FILLABLE_RATIO_ALIASES = ("fillable_ratio", "fillability_ratio")
_PARTIAL_FILL_ALIASES = ("partial_fill", "is_partial_fill")
_PARTIAL_FILL_RATIO_ALIASES = ("partial_fill_ratio",)
_FEE_BPS_ALIASES = ("fee_bps", "fees_bps", "estimated_fee_bps", "cost_fee_bps")
_SLIPPAGE_BPS_ALIASES = ("slippage_bps", "estimated_slippage_bps")
_FUNDING_BPS_ALIASES = ("funding_bps", "funding_cost_bps")
_EDGE_BPS_ALIASES = (
    "cost_adjusted_edge_bps",
    "edge_bps",
    "net_edge_bps",
    "expected_edge_bps_after_costs",
)
_DRAWDOWN_ALIASES = ("drawdown", "max_drawdown", "observed_drawdown")
_METRIC_DRIFT_ALIASES = ("metric_drift", "drift", "metric_drift_ratio")
_ABORT_ALIASES = ("abort_triggered", "aborted")
_ABORT_REASON_ALIASES = ("abort_reason", "abort_cause")
_SOURCE_EVENT_ID_ALIASES = (
    "source_event_id",
    "event_id",
    "id",
    "decision_id",
    "intent_id",
)
_SIGNAL_ALIASES = ("signal", "has_signal", "signal_emitted")
_PAPER_INTENT_ALIASES = ("paper_intent", "has_paper_intent", "paper_intent_emitted")


def load_source_events_jsonl(path: str | Path) -> tuple[Mapping[str, Any], ...]:
    """Load read-only exported shadow/paper event mappings from artifacts/research."""
    event_path = require_research_artifact_file(path, "source_events", suffix=".jsonl")
    events: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(event_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"source_events line {line_number} must be valid JSON") from exc
        if not isinstance(payload, Mapping):
            raise ValueError(f"source_events line {line_number} must be a JSON object")
        events.append(payload)
    if not events:
        raise ValueError("source_events must contain at least one event")
    return tuple(events)


def normalize_source_events(
    events: Iterable[Mapping[str, Any]],
    *,
    recommendation_id: str,
    candidate_id: str,
    experiment_id: str,
    mode: str,
    source_kind: str,
) -> tuple[ObservationEventRecord, ...]:
    """Normalize exported source events into canonical ObservationEventRecord values."""
    if mode not in ALLOWED_OBSERVATION_SUMMARY_MODES:
        allowed = ", ".join(sorted(ALLOWED_OBSERVATION_SUMMARY_MODES))
        raise ValueError(f"mode must be one of: {allowed}")
    if source_kind not in SOURCE_EVENT_KINDS:
        allowed = ", ".join(sorted(SOURCE_EVENT_KINDS))
        raise ValueError(f"source_kind must be one of: {allowed}")
    normalized: list[ObservationEventRecord] = []
    for index, event in enumerate(tuple(events), start=1):
        if not isinstance(event, Mapping):
            raise ValueError(f"source event {index} must be a mapping")
        if source_kind == "observation_event":
            record = ObservationEventRecord.from_mapping(event)
        else:
            record = _normalize_raw_source_event(
                event,
                recommendation_id=recommendation_id,
                candidate_id=candidate_id,
                experiment_id=experiment_id,
                mode=mode,
                source_kind=source_kind,
                index=index,
            )
        _require_expected_identity(
            record,
            recommendation_id=recommendation_id,
            candidate_id=candidate_id,
            experiment_id=experiment_id,
            mode=mode,
        )
        normalized.append(record)
    if not normalized:
        raise ValueError("events must contain at least one event")
    return tuple(normalized)


def write_observation_events_jsonl(
    events: Iterable[ObservationEventRecord],
    output_path: str | Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Write canonical observation events atomically under artifacts/research."""
    event_tuple = tuple(events)
    if not event_tuple:
        raise ValueError("events must contain at least one observation event")
    for event in event_tuple:
        if not isinstance(event, ObservationEventRecord):
            raise ValueError("events must be ObservationEventRecord instances")
    path = require_research_artifact_file(
        output_path,
        "output_events",
        suffix=".jsonl",
        must_exist=False,
    )
    if path.exists() and not overwrite:
        raise ValueError("output_events already exists; pass overwrite=True to replace it")
    rendered = "\n".join(
        json.dumps(_event_to_dict(event), ensure_ascii=False, sort_keys=True)
        for event in event_tuple
    ) + "\n"
    _write_text_atomic(path, rendered)
    return path


def _normalize_raw_source_event(
    event: Mapping[str, Any],
    *,
    recommendation_id: str,
    candidate_id: str,
    experiment_id: str,
    mode: str,
    source_kind: str,
    index: int,
) -> ObservationEventRecord:
    candidates = _mapping_candidates(event)
    canonical = {
        "schema_version": OBSERVATION_SUMMARY_GENERATOR_SCHEMA_VERSION,
        "ts": _required_alias(candidates, _TIMESTAMP_ALIASES, "ts", index),
        "bar_ts": _optional_alias(candidates, _BAR_TIMESTAMP_ALIASES),
        "recommendation_id": recommendation_id,
        "candidate_id": candidate_id,
        "experiment_id": experiment_id,
        "mode": mode,
        "signal": _source_bool_default(
            candidates,
            _SIGNAL_ALIASES,
            default=True,
            field_name="signal",
            index=index,
        ),
        "paper_intent": _source_bool_default(
            candidates,
            _PAPER_INTENT_ALIASES,
            default=(source_kind == "paper_intent"),
            field_name="paper_intent",
            index=index,
        ),
        "fillable": _optional_bool_alias(candidates, _FILLABLE_ALIASES, "fillable", index),
        "partial_fill": _optional_bool_alias(
            candidates,
            _PARTIAL_FILL_ALIASES,
            "partial_fill",
            index,
        ),
        "fillable_ratio": _optional_number_alias(
            candidates,
            _FILLABLE_RATIO_ALIASES,
            "fillable_ratio",
            index,
        ),
        "partial_fill_ratio": _optional_number_alias(
            candidates,
            _PARTIAL_FILL_RATIO_ALIASES,
            "partial_fill_ratio",
            index,
        ),
        "fee_bps": _required_number_alias(candidates, _FEE_BPS_ALIASES, "fee_bps", index),
        "slippage_bps": _required_number_alias(
            candidates,
            _SLIPPAGE_BPS_ALIASES,
            "slippage_bps",
            index,
        ),
        "funding_bps": _required_number_alias(
            candidates,
            _FUNDING_BPS_ALIASES,
            "funding_bps",
            index,
        ),
        "cost_adjusted_edge_bps": _required_number_alias(
            candidates,
            _EDGE_BPS_ALIASES,
            "cost_adjusted_edge_bps",
            index,
        ),
        "drawdown": _required_number_alias(candidates, _DRAWDOWN_ALIASES, "drawdown", index),
        "metric_drift": _required_number_alias(
            candidates,
            _METRIC_DRIFT_ALIASES,
            "metric_drift",
            index,
        ),
        "abort_triggered": _source_bool_default(
            candidates,
            _ABORT_ALIASES,
            default=False,
            field_name="abort_triggered",
            index=index,
        ),
        "abort_reason": _optional_alias(candidates, _ABORT_REASON_ALIASES),
        "source_event_id": _optional_alias(candidates, _SOURCE_EVENT_ID_ALIASES),
    }
    if canonical["fillable"] is None and canonical["fillable_ratio"] is None:
        raise ValueError(
            f"source event {index} missing required execution metric: fillable or fillable_ratio"
        )
    if canonical["partial_fill"] is None and canonical["partial_fill_ratio"] is None:
        raise ValueError(
            f"source event {index} missing required execution metric: partial_fill or "
            "partial_fill_ratio"
        )
    return ObservationEventRecord.from_mapping(canonical)


def _mapping_candidates(event: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    candidates: list[Mapping[str, Any]] = []
    for key in ("payload", "data", "event", "body"):
        nested = event.get(key)
        if isinstance(nested, Mapping):
            candidates.append(nested)
    candidates.append(event)
    return tuple(candidates)


def _required_alias(
    candidates: tuple[Mapping[str, Any], ...],
    aliases: tuple[str, ...],
    field_name: str,
    index: int,
) -> Any:
    value = _optional_alias(candidates, aliases)
    if value is None:
        raise ValueError(f"source event {index} missing required field: {field_name}")
    return value


def _optional_alias(candidates: tuple[Mapping[str, Any], ...], aliases: tuple[str, ...]) -> Any | None:
    for candidate in candidates:
        for alias in aliases:
            value = candidate.get(alias)
            if value is not None:
                return value
    return None


def _required_number_alias(
    candidates: tuple[Mapping[str, Any], ...],
    aliases: tuple[str, ...],
    field_name: str,
    index: int,
) -> float:
    value = _optional_number_alias(candidates, aliases, field_name, index)
    if value is None:
        raise ValueError(f"source event {index} missing required execution metric: {field_name}")
    return value


def _optional_number_alias(
    candidates: tuple[Mapping[str, Any], ...],
    aliases: tuple[str, ...],
    field_name: str,
    index: int,
) -> float | None:
    value = _optional_alias(candidates, aliases)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"source event {index} {field_name} must be numeric")
    return float(value)


def _optional_bool_alias(
    candidates: tuple[Mapping[str, Any], ...],
    aliases: tuple[str, ...],
    field_name: str,
    index: int,
) -> bool | None:
    value = _optional_alias(candidates, aliases)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"source event {index} {field_name} must be a bool")
    return value


def _source_bool_default(
    candidates: tuple[Mapping[str, Any], ...],
    aliases: tuple[str, ...],
    *,
    default: bool,
    field_name: str,
    index: int,
) -> bool:
    value = _optional_bool_alias(candidates, aliases, field_name, index)
    return default if value is None else value


def _require_expected_identity(
    event: ObservationEventRecord,
    *,
    recommendation_id: str,
    candidate_id: str,
    experiment_id: str,
    mode: str,
) -> None:
    if event.recommendation_id != recommendation_id:
        raise ValueError("event recommendation_id must match requested recommendation_id")
    if event.candidate_id != candidate_id:
        raise ValueError("event candidate_id must match requested candidate_id")
    if event.experiment_id != experiment_id:
        raise ValueError("event experiment_id must match requested experiment_id")
    if event.mode != mode:
        raise ValueError("event mode must match requested mode")


def _event_to_dict(event: ObservationEventRecord) -> dict[str, Any]:
    return {
        "schema_version": OBSERVATION_SUMMARY_GENERATOR_SCHEMA_VERSION,
        "ts": event.ts.isoformat(),
        "bar_ts": event.bar_ts.isoformat() if event.bar_ts else None,
        "recommendation_id": event.recommendation_id,
        "candidate_id": event.candidate_id,
        "experiment_id": event.experiment_id,
        "mode": event.mode,
        "signal": event.signal,
        "paper_intent": event.paper_intent,
        "fillable": event.fillable,
        "partial_fill": event.partial_fill,
        "fillable_ratio": event.fillable_ratio,
        "partial_fill_ratio": event.partial_fill_ratio,
        "fee_bps": event.fee_bps,
        "slippage_bps": event.slippage_bps,
        "funding_bps": event.funding_bps,
        "cost_adjusted_edge_bps": event.cost_adjusted_edge_bps,
        "drawdown": event.drawdown,
        "metric_drift": event.metric_drift,
        "abort_triggered": event.abort_triggered,
        "abort_reason": event.abort_reason,
        "source_event_id": event.source_event_id,
    }


def _write_text_atomic(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
