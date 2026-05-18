"""Read-only Shadow/Paper observation summary adapters."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aats.data_platform.research_factory.observations import (
    ALLOWED_REVIEW_DECISIONS,
    ObservationResult,
)
from aats.data_platform.research_factory.paths import require_research_artifact_json_file
from aats.data_platform.research_factory.recommendations import ResearchRecommendation

OBSERVATION_SUMMARY_SCHEMA_VERSION = "research_observation_summary_v1"


@dataclass(frozen=True, slots=True)
class ReadOnlyObservationDataSource:
    """Read a research observation summary and build an ObservationResult."""

    summary_path: Path
    expected_mode: str
    research_root: Path | None = None

    def __post_init__(self) -> None:
        if self.expected_mode not in {"shadow", "paper"}:
            raise ValueError("expected_mode must be shadow or paper")
        source_path = require_research_artifact_json_file(
            self.summary_path,
            "summary_path",
            research_root=self.research_root,
        )
        object.__setattr__(self, "summary_path", source_path)

    def load_result(
        self,
        recommendation: ResearchRecommendation,
        *,
        observation_id: str | None = None,
        review_decision: str | None = None,
        created_at: datetime | None = None,
    ) -> ObservationResult:
        """Build an observation result from a read-only artifact summary."""
        if not isinstance(recommendation, ResearchRecommendation):
            raise ValueError("recommendation must be a ResearchRecommendation")
        if recommendation.observation_plan.mode != self.expected_mode:
            raise ValueError("recommendation observation mode does not match observation data source")
        payload = _load_summary(self.summary_path)
        _require_summary_identity(payload, recommendation)
        mode = _require_text(payload, "mode")
        if mode != self.expected_mode:
            raise ValueError("observation summary mode does not match observation data source")
        decision = review_decision or "keep_reviewing"
        if decision not in ALLOWED_REVIEW_DECISIONS:
            allowed = ", ".join(sorted(ALLOWED_REVIEW_DECISIONS))
            raise ValueError(f"review_decision must be one of: {allowed}")
        return ObservationResult(
            observation_id=observation_id or f"obs_{recommendation.recommendation_id}",
            recommendation_id=recommendation.recommendation_id,
            candidate_id=recommendation.candidate_id,
            experiment_id=recommendation.experiment_id,
            mode=mode,
            observation_start=_require_datetime(payload, "observation_start"),
            observation_end=_require_datetime(payload, "observation_end"),
            observed_bars=_require_int(payload, "observed_bars"),
            observed_events=_require_int(payload, "observed_events"),
            signal_count=_require_int(payload, "signal_count"),
            paper_intent_count=_require_int(payload, "paper_intent_count"),
            fillable_ratio=_require_number(payload, "fillable_ratio"),
            partial_fill_ratio=_require_number(payload, "partial_fill_ratio"),
            fee_bps_mean=_require_number(payload, "fee_bps_mean"),
            slippage_bps_mean=_require_number(payload, "slippage_bps_mean"),
            funding_bps_mean=_require_number(payload, "funding_bps_mean"),
            cost_adjusted_edge_bps_mean=_require_number(payload, "cost_adjusted_edge_bps_mean"),
            drawdown=_require_number(payload, "drawdown"),
            metric_drift=_require_number(payload, "metric_drift"),
            abort_triggered=_require_bool(payload, "abort_triggered"),
            abort_reason=_optional_text(payload.get("abort_reason")),
            review_decision=decision,
            created_at=created_at or datetime.now(UTC),
        )


class ShadowObservationDataSource(ReadOnlyObservationDataSource):
    """Read-only source for shadow observation summaries."""

    def __init__(self, summary_path: str | Path, *, research_root: str | Path | None = None) -> None:
        super().__init__(
            summary_path=Path(summary_path),
            expected_mode="shadow",
            research_root=Path(research_root) if research_root is not None else None,
        )


class PaperObservationDataSource(ReadOnlyObservationDataSource):
    """Read-only source for paper observation summaries."""

    def __init__(self, summary_path: str | Path, *, research_root: str | Path | None = None) -> None:
        super().__init__(
            summary_path=Path(summary_path),
            expected_mode="paper",
            research_root=Path(research_root) if research_root is not None else None,
        )


def _load_summary(path: Path) -> Mapping[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError("observation summary must be valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("observation summary must be a JSON object")
    schema_version = _require_text(payload, "schema_version")
    if schema_version != OBSERVATION_SUMMARY_SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {OBSERVATION_SUMMARY_SCHEMA_VERSION!r}")
    return payload


def _require_summary_identity(payload: Mapping[str, Any], recommendation: ResearchRecommendation) -> None:
    expected = {
        "recommendation_id": recommendation.recommendation_id,
        "candidate_id": recommendation.candidate_id,
        "experiment_id": recommendation.experiment_id,
    }
    for field_name, expected_value in expected.items():
        actual = _require_text(payload, field_name)
        if actual != expected_value:
            raise ValueError(f"observation summary {field_name} must match recommendation")


def _require_text(payload: Mapping[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"observation summary {field_name} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("optional observation summary text must be non-empty when present")
    return value.strip()


def _require_datetime(payload: Mapping[str, Any], field_name: str) -> datetime:
    raw = _require_text(payload, field_name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"observation summary {field_name} must be an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"observation summary {field_name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _require_int(payload: Mapping[str, Any], field_name: str) -> int:
    value = payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"observation summary {field_name} must be an integer")
    return value


def _require_number(payload: Mapping[str, Any], field_name: str) -> float:
    value = payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"observation summary {field_name} must be numeric")
    return float(value)


def _require_bool(payload: Mapping[str, Any], field_name: str) -> bool:
    value = payload.get(field_name)
    if not isinstance(value, bool):
        raise ValueError(f"observation summary {field_name} must be a bool")
    return value
