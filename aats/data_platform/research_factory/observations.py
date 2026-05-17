"""Research-only shadow/paper observation artifacts."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

from aats.data_platform.research_factory.artifacts import (
    build_artifact_manifest,
    normalize_relative_artifact_path,
    validate_artifact_manifest,
    write_artifact_manifest_atomic,
)
from aats.data_platform.research_factory.numeric import require_finite_number
from aats.data_platform.research_factory.recommendations import (
    ALLOWED_OBSERVATION_MODES,
    ResearchRecommendation,
)

OBSERVATION_SCHEMA_VERSION = "research_observation_v1"
OBSERVATION_MANIFEST_REF = "observation_manifest.json"
OBSERVATION_RUN_REF = "observation_run.json"
OBSERVATION_RESULT_REF = "observation_result.json"
REVIEW_OUTCOME_REF = "review_outcome.json"

ALLOWED_OBSERVATION_RUN_STATUSES = frozenset({"planned", "running", "completed", "failed", "cancelled"})
TERMINAL_OBSERVATION_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})
ALLOWED_REVIEW_DECISIONS = frozenset({"keep_reviewing", "reject", "eligible_for_preapply"})

RUNTIME_PROMOTION_TERMS = (
    "active_parameter",
    "active parameter",
    "active_parameters",
    "active parameters",
    "live_order",
    "live order",
    "okx_write",
    "okx write",
    "operator_write",
    "operator write",
    "production_config",
    "production config",
    "runtime_mutation",
    "runtime mutation",
    "runtime_config",
    "runtime config",
    "direct_apply",
    "direct apply",
    "auto_apply",
    "auto apply",
)


@dataclass(frozen=True, slots=True)
class ObservationRun:
    """Planned or running shadow/paper observation for a recommendation."""

    observation_id: str
    recommendation_id: str
    candidate_id: str
    experiment_id: str
    mode: str
    status: str
    min_observation_bars: int
    min_observation_events: int
    planned_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    evidence_refs: Mapping[str, str] = field(default_factory=dict)
    notes: Sequence[str] = field(default_factory=tuple)
    schema_version: str = OBSERVATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_safe_identifier(self.observation_id, "observation_id")
        _require_safe_identifier(self.recommendation_id, "recommendation_id")
        _require_safe_identifier(self.candidate_id, "candidate_id")
        _require_safe_identifier(self.experiment_id, "experiment_id")
        if self.mode not in ALLOWED_OBSERVATION_MODES:
            allowed = ", ".join(sorted(ALLOWED_OBSERVATION_MODES))
            raise ValueError(f"observation mode must be one of: {allowed}")
        if self.status not in ALLOWED_OBSERVATION_RUN_STATUSES:
            allowed = ", ".join(sorted(ALLOWED_OBSERVATION_RUN_STATUSES))
            raise ValueError(f"observation status must be one of: {allowed}")
        _require_positive_int(self.min_observation_bars, "min_observation_bars")
        _require_positive_int(self.min_observation_events, "min_observation_events")
        _require_timezone_aware_datetime(self.planned_at, "planned_at")
        if self.started_at is not None:
            _require_timezone_aware_datetime(self.started_at, "started_at")
        if self.finished_at is not None:
            _require_timezone_aware_datetime(self.finished_at, "finished_at")
        if self.status == "planned" and (self.started_at is not None or self.finished_at is not None):
            raise ValueError("planned observation must not have started_at or finished_at")
        if self.status == "running":
            if self.started_at is None:
                raise ValueError("running observation requires started_at")
            if self.finished_at is not None:
                raise ValueError("running observation must not have finished_at")
        if self.status in TERMINAL_OBSERVATION_RUN_STATUSES:
            if self.started_at is None or self.finished_at is None:
                raise ValueError("terminal observation requires started_at and finished_at")
            if self.finished_at < self.started_at:
                raise ValueError("finished_at must not be before started_at")
        if self.schema_version != OBSERVATION_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {OBSERVATION_SCHEMA_VERSION!r}")
        object.__setattr__(self, "evidence_refs", _normalize_refs(self.evidence_refs, "evidence_refs", allow_empty=True))
        object.__setattr__(
            self,
            "notes",
            _normalize_optional_text_sequence(self.notes, "notes"),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ObservationRun:
        """Load an observation run from its JSON representation."""
        if not isinstance(value, Mapping):
            raise ValueError("observation run payload must be a mapping")
        return cls(
            observation_id=_require_mapping_text(value, "observation_id"),
            recommendation_id=_require_mapping_text(value, "recommendation_id"),
            candidate_id=_require_mapping_text(value, "candidate_id"),
            experiment_id=_require_mapping_text(value, "experiment_id"),
            mode=_require_mapping_text(value, "mode"),
            status=_require_mapping_text(value, "status"),
            min_observation_bars=value.get("min_observation_bars"),
            min_observation_events=value.get("min_observation_events"),
            planned_at=_parse_datetime(value.get("planned_at"), "planned_at"),
            started_at=_parse_optional_datetime(value.get("started_at"), "started_at"),
            finished_at=_parse_optional_datetime(value.get("finished_at"), "finished_at"),
            evidence_refs=value.get("evidence_refs", {}),
            notes=value.get("notes", ()),
            schema_version=value.get("schema_version", OBSERVATION_SCHEMA_VERSION),
        )


@dataclass(frozen=True, slots=True)
class ObservationResult:
    """Observed shadow/paper outcome metrics for a recommendation."""

    observation_id: str
    recommendation_id: str
    candidate_id: str
    experiment_id: str
    mode: str
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
    review_decision: str
    abort_reason: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = OBSERVATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_safe_identifier(self.observation_id, "observation_id")
        _require_safe_identifier(self.recommendation_id, "recommendation_id")
        _require_safe_identifier(self.candidate_id, "candidate_id")
        _require_safe_identifier(self.experiment_id, "experiment_id")
        if self.mode not in ALLOWED_OBSERVATION_MODES:
            allowed = ", ".join(sorted(ALLOWED_OBSERVATION_MODES))
            raise ValueError(f"observation mode must be one of: {allowed}")
        _require_timezone_aware_datetime(self.observation_start, "observation_start")
        _require_timezone_aware_datetime(self.observation_end, "observation_end")
        if self.observation_end <= self.observation_start:
            raise ValueError("observation_end must be after observation_start")
        _require_non_negative_int(self.observed_bars, "observed_bars")
        _require_non_negative_int(self.observed_events, "observed_events")
        _require_non_negative_int(self.signal_count, "signal_count")
        _require_non_negative_int(self.paper_intent_count, "paper_intent_count")
        object.__setattr__(self, "fillable_ratio", _require_ratio(self.fillable_ratio, "fillable_ratio"))
        object.__setattr__(self, "partial_fill_ratio", _require_ratio(self.partial_fill_ratio, "partial_fill_ratio"))
        object.__setattr__(self, "fee_bps_mean", _require_non_negative_number(self.fee_bps_mean, "fee_bps_mean"))
        object.__setattr__(self, "slippage_bps_mean", require_finite_number(self.slippage_bps_mean, "slippage_bps_mean"))
        object.__setattr__(self, "funding_bps_mean", require_finite_number(self.funding_bps_mean, "funding_bps_mean"))
        object.__setattr__(
            self,
            "cost_adjusted_edge_bps_mean",
            require_finite_number(self.cost_adjusted_edge_bps_mean, "cost_adjusted_edge_bps_mean"),
        )
        object.__setattr__(self, "drawdown", _require_non_negative_number(self.drawdown, "drawdown"))
        object.__setattr__(self, "metric_drift", _require_non_negative_number(self.metric_drift, "metric_drift"))
        if not isinstance(self.abort_triggered, bool):
            raise ValueError("abort_triggered must be a bool")
        if self.abort_triggered:
            object.__setattr__(self, "abort_reason", _require_non_empty_text(self.abort_reason, "abort_reason"))
        elif self.abort_reason is not None:
            object.__setattr__(self, "abort_reason", _require_non_empty_text(self.abort_reason, "abort_reason"))
        if self.review_decision not in ALLOWED_REVIEW_DECISIONS:
            allowed = ", ".join(sorted(ALLOWED_REVIEW_DECISIONS))
            raise ValueError(f"review_decision must be one of: {allowed}")
        if self.review_decision == "eligible_for_preapply" and self.abort_triggered:
            raise ValueError("aborted observation cannot be eligible_for_preapply")
        _require_timezone_aware_datetime(self.created_at, "created_at")
        if self.schema_version != OBSERVATION_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {OBSERVATION_SCHEMA_VERSION!r}")


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    """Governance review decision produced from an observation result."""

    outcome_id: str
    observation_id: str
    recommendation_id: str
    candidate_id: str
    experiment_id: str
    decision: str
    rationale: str
    observation_result_ref: str = OBSERVATION_RESULT_REF
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = OBSERVATION_SCHEMA_VERSION
    runtime_mutation_allowed: bool = False
    operator_approval_required: bool = True
    recommended_next_step: str | None = None

    def __post_init__(self) -> None:
        _require_safe_identifier(self.outcome_id, "outcome_id")
        _require_safe_identifier(self.observation_id, "observation_id")
        _require_safe_identifier(self.recommendation_id, "recommendation_id")
        _require_safe_identifier(self.candidate_id, "candidate_id")
        _require_safe_identifier(self.experiment_id, "experiment_id")
        if self.decision not in ALLOWED_REVIEW_DECISIONS:
            allowed = ", ".join(sorted(ALLOWED_REVIEW_DECISIONS))
            raise ValueError(f"review outcome decision must be one of: {allowed}")
        rationale = _require_non_empty_text(self.rationale, "rationale")
        _reject_runtime_promotion_text(rationale, "rationale")
        object.__setattr__(self, "rationale", rationale)
        object.__setattr__(
            self,
            "observation_result_ref",
            _require_relative_ref(self.observation_result_ref, "observation_result_ref"),
        )
        _require_timezone_aware_datetime(self.created_at, "created_at")
        if self.schema_version != OBSERVATION_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {OBSERVATION_SCHEMA_VERSION!r}")
        if self.runtime_mutation_allowed is not False:
            raise ValueError("review outcome must not allow runtime mutation")
        if self.operator_approval_required is not True:
            raise ValueError("review outcome must require operator approval")
        next_step = self.recommended_next_step or _default_next_step(self.decision)
        next_step = _require_non_empty_text(next_step, "recommended_next_step")
        _reject_runtime_promotion_text(next_step, "recommended_next_step")
        object.__setattr__(self, "recommended_next_step", next_step)


class ObservationRecorder:
    """Persist observation artifacts under a research-only root."""

    def __init__(
        self,
        root: str | Path = Path("artifacts") / "research" / "research_factory" / "observations",
        *,
        code_version: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = _require_research_observation_root(root)
        self.code_version = code_version
        self._clock = clock or _utc_now

    def plan(
        self,
        recommendation: ResearchRecommendation,
        *,
        observation_id: str | None = None,
        recommendation_ref: str = "research_recommendation.json",
    ) -> ObservationRun:
        """Create a planned observation artifact from a ready recommendation."""
        if not isinstance(recommendation, ResearchRecommendation):
            raise ValueError("recommendation must be a ResearchRecommendation")
        if recommendation.status != "ready_for_review":
            raise ValueError("observation requires a ready_for_review recommendation")
        observation_id = _require_safe_identifier(
            observation_id or f"obs_{recommendation.recommendation_id}",
            "observation_id",
        )
        observation_dir = self._observation_dir(observation_id)
        if observation_dir.exists():
            raise ValueError(f"observation {observation_id!r} already exists")

        observation_dir.mkdir(parents=True)
        run = ObservationRun(
            observation_id=observation_id,
            recommendation_id=recommendation.recommendation_id,
            candidate_id=recommendation.candidate_id,
            experiment_id=recommendation.experiment_id,
            mode=recommendation.observation_plan.mode,
            status="planned",
            min_observation_bars=recommendation.observation_plan.min_observation_bars,
            min_observation_events=recommendation.observation_plan.min_observation_events,
            planned_at=self._now(),
            evidence_refs={"research_recommendation": recommendation_ref},
            notes=(
                "observation is research-only evidence",
                "no trading-system change is authorized by this artifact",
            ),
        )
        _write_json_atomic(observation_dir / OBSERVATION_RUN_REF, _to_jsonable(run))

        manifest = build_artifact_manifest(
            artifact_id=observation_id,
            artifact_type="observation",
            status="pending",
            started_at=run.planned_at,
            input_refs={
                "recommendation_id": recommendation.recommendation_id,
                "candidate_id": recommendation.candidate_id,
                "experiment_id": recommendation.experiment_id,
                "mode": recommendation.observation_plan.mode,
            },
            output_refs={"observation_run": OBSERVATION_RUN_REF},
            code_version=self.code_version,
            notes="research-only shadow/paper observation",
        )
        self._write_manifest(observation_id, manifest)
        return run

    def start(self, observation_id: str, *, started_at: datetime | None = None) -> ObservationRun:
        """Mark a planned observation as running."""
        run = self._read_run(observation_id)
        if run.status != "planned":
            raise ValueError(f"observation {observation_id!r} is not planned")
        started_at = started_at or self._now()
        run = ObservationRun(
            observation_id=run.observation_id,
            recommendation_id=run.recommendation_id,
            candidate_id=run.candidate_id,
            experiment_id=run.experiment_id,
            mode=run.mode,
            status="running",
            min_observation_bars=run.min_observation_bars,
            min_observation_events=run.min_observation_events,
            planned_at=run.planned_at,
            started_at=started_at,
            evidence_refs=run.evidence_refs,
            notes=run.notes,
        )
        _write_json_atomic(self._observation_dir(observation_id) / OBSERVATION_RUN_REF, _to_jsonable(run))
        manifest = self._read_manifest(observation_id)
        self._write_manifest(
            observation_id,
            build_artifact_manifest(
                artifact_id=manifest["artifact_id"],
                artifact_type=manifest["artifact_type"],
                status="running",
                started_at=manifest["started_at"],
                finished_at=manifest.get("finished_at"),
                input_refs=manifest["input_refs"],
                output_refs=manifest["output_refs"],
                metrics_ref=manifest.get("metrics_ref"),
                code_version=manifest.get("code_version"),
                notes=manifest.get("notes"),
            ),
        )
        return run

    def record_result(self, result: ObservationResult) -> dict[str, Any]:
        """Write an observation result and keep the observation open for review outcome."""
        if not isinstance(result, ObservationResult):
            raise ValueError("result must be an ObservationResult")
        run = self._read_run(result.observation_id)
        if run.status != "running":
            raise ValueError(f"observation {result.observation_id!r} is not running")
        _require_matching_run(result, run)

        observation_dir = self._observation_dir(result.observation_id)
        _write_json_atomic(observation_dir / OBSERVATION_RESULT_REF, _to_jsonable(result))
        completed_run = ObservationRun(
            observation_id=run.observation_id,
            recommendation_id=run.recommendation_id,
            candidate_id=run.candidate_id,
            experiment_id=run.experiment_id,
            mode=run.mode,
            status="completed",
            min_observation_bars=run.min_observation_bars,
            min_observation_events=run.min_observation_events,
            planned_at=run.planned_at,
            started_at=run.started_at,
            finished_at=result.observation_end,
            evidence_refs=run.evidence_refs,
            notes=run.notes,
        )
        _write_json_atomic(observation_dir / OBSERVATION_RUN_REF, _to_jsonable(completed_run))

        manifest = self._read_manifest(result.observation_id)
        output_refs = dict(manifest["output_refs"])
        output_refs["observation_result"] = OBSERVATION_RESULT_REF
        updated = build_artifact_manifest(
            artifact_id=manifest["artifact_id"],
            artifact_type=manifest["artifact_type"],
            status="running",
            started_at=manifest["started_at"],
            finished_at=manifest.get("finished_at"),
            input_refs=manifest["input_refs"],
            output_refs=output_refs,
            metrics_ref=manifest.get("metrics_ref"),
            code_version=manifest.get("code_version"),
            notes=manifest.get("notes"),
        )
        self._write_manifest(result.observation_id, updated)
        return updated

    def record_review_outcome(self, outcome: ReviewOutcome) -> dict[str, Any]:
        """Write the review outcome and close the observation artifact."""
        if not isinstance(outcome, ReviewOutcome):
            raise ValueError("outcome must be a ReviewOutcome")
        run = self._read_run(outcome.observation_id)
        if run.status != "completed":
            raise ValueError(f"observation {outcome.observation_id!r} has no completed result")
        _require_matching_outcome(outcome, run)
        result_path = self._observation_dir(outcome.observation_id) / outcome.observation_result_ref
        if not result_path.exists():
            raise ValueError("observation result must be recorded before review outcome")

        observation_dir = self._observation_dir(outcome.observation_id)
        _write_json_atomic(observation_dir / REVIEW_OUTCOME_REF, _to_jsonable(outcome))
        manifest = self._read_manifest(outcome.observation_id)
        output_refs = dict(manifest["output_refs"])
        output_refs["review_outcome"] = REVIEW_OUTCOME_REF
        updated = build_artifact_manifest(
            artifact_id=manifest["artifact_id"],
            artifact_type=manifest["artifact_type"],
            status="succeeded",
            started_at=manifest["started_at"],
            finished_at=outcome.created_at,
            input_refs=manifest["input_refs"],
            output_refs=output_refs,
            metrics_ref=manifest.get("metrics_ref"),
            code_version=manifest.get("code_version"),
            notes=manifest.get("notes"),
        )
        self._write_manifest(outcome.observation_id, updated)
        return updated

    def _observation_dir(self, observation_id: str) -> Path:
        return self.root / _require_safe_identifier(observation_id, "observation_id")

    def _manifest_path(self, observation_id: str) -> Path:
        return self._observation_dir(observation_id) / OBSERVATION_MANIFEST_REF

    def _read_manifest(self, observation_id: str) -> dict[str, Any]:
        path = self._manifest_path(observation_id)
        if not path.exists():
            raise ValueError(f"observation {observation_id!r} has no manifest")
        with path.open("r", encoding="utf-8") as handle:
            return validate_artifact_manifest(json.load(handle))

    def _write_manifest(self, observation_id: str, manifest: Mapping[str, Any]) -> None:
        write_artifact_manifest_atomic(self._manifest_path(observation_id), manifest)

    def _read_run(self, observation_id: str) -> ObservationRun:
        path = self._observation_dir(observation_id) / OBSERVATION_RUN_REF
        if not path.exists():
            raise ValueError(f"observation {observation_id!r} has no run artifact")
        with path.open("r", encoding="utf-8") as handle:
            return ObservationRun.from_mapping(json.load(handle))

    def _now(self) -> datetime:
        return self._clock()


def build_review_outcome(
    result: ObservationResult,
    *,
    rationale: str,
    outcome_id: str | None = None,
    observation_result_ref: str = OBSERVATION_RESULT_REF,
    created_at: datetime | None = None,
) -> ReviewOutcome:
    """Build a review outcome from a validated observation result."""
    if not isinstance(result, ObservationResult):
        raise ValueError("result must be an ObservationResult")
    return ReviewOutcome(
        outcome_id=outcome_id or f"out_{result.observation_id}",
        observation_id=result.observation_id,
        recommendation_id=result.recommendation_id,
        candidate_id=result.candidate_id,
        experiment_id=result.experiment_id,
        decision=result.review_decision,
        rationale=rationale,
        observation_result_ref=observation_result_ref,
        created_at=created_at or datetime.now(UTC),
    )


def _require_matching_run(result: ObservationResult, run: ObservationRun) -> None:
    if result.recommendation_id != run.recommendation_id:
        raise ValueError("observation result recommendation_id must match run")
    if result.candidate_id != run.candidate_id:
        raise ValueError("observation result candidate_id must match run")
    if result.experiment_id != run.experiment_id:
        raise ValueError("observation result experiment_id must match run")
    if result.mode != run.mode:
        raise ValueError("observation result mode must match run")


def _require_matching_outcome(outcome: ReviewOutcome, run: ObservationRun) -> None:
    if outcome.recommendation_id != run.recommendation_id:
        raise ValueError("review outcome recommendation_id must match run")
    if outcome.candidate_id != run.candidate_id:
        raise ValueError("review outcome candidate_id must match run")
    if outcome.experiment_id != run.experiment_id:
        raise ValueError("review outcome experiment_id must match run")


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


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {field.name: _to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime values must be timezone-aware")
        return value.isoformat()
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, Mapping):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    raise TypeError(f"unsupported JSON artifact value: {type(value).__name__}")


def _default_next_step(decision: str) -> str:
    if decision == "eligible_for_preapply":
        return "prepare_preapply_evidence_review"
    if decision == "reject":
        return "archive_recommendation_without_runtime_change"
    return "continue_shadow_or_paper_observation"


def _normalize_refs(refs: Mapping[str, str], field_name: str, *, allow_empty: bool) -> dict[str, str]:
    if not isinstance(refs, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    if not refs and not allow_empty:
        raise ValueError(f"{field_name} must not be empty")
    normalized: dict[str, str] = {}
    for key, value in refs.items():
        ref_name = _require_non_empty_text(key, f"{field_name} key")
        _reject_runtime_promotion_text(ref_name, f"{field_name} key")
        normalized[ref_name] = _require_relative_ref(value, f"{field_name}.{ref_name}")
    return dict(sorted(normalized.items()))


def _normalize_optional_text_sequence(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, str | bytes | bytearray) or not isinstance(values, Sequence):
        raise ValueError(f"{field_name} must be a sequence of strings")
    normalized = tuple(_require_non_empty_text(value, field_name) for value in values)
    for value in normalized:
        _reject_runtime_promotion_text(value, field_name)
    return normalized


def _require_safe_identifier(value: Any, field_name: str) -> str:
    value = _require_non_empty_text(value, field_name)
    if "/" in value or "\\" in value or value in {".", ".."} or ".." in value:
        raise ValueError(f"{field_name} must not contain path traversal or separators")
    return value


def _require_mapping_text(value: Mapping[str, Any], field_name: str) -> str:
    return _require_non_empty_text(value.get(field_name), field_name)


def _require_non_empty_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_positive_int(value: Any, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")


def _require_non_negative_int(value: Any, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


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


def _require_timezone_aware_datetime(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")


def _parse_datetime(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        _require_timezone_aware_datetime(value, field_name)
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    parsed = datetime.fromisoformat(value)
    _require_timezone_aware_datetime(parsed, field_name)
    return parsed


def _parse_optional_datetime(value: Any, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _parse_datetime(value, field_name)


def _require_relative_ref(value: Any, field_name: str) -> str:
    ref = _require_non_empty_text(value, field_name)
    ref = normalize_relative_artifact_path(ref)
    if ref.startswith("~"):
        raise ValueError(f"{field_name} must be a relative artifact ref")
    posix_path = PurePosixPath(ref)
    windows_path = PureWindowsPath(ref)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError(f"{field_name} must be a relative artifact ref")
    return ref


def _reject_runtime_promotion_text(value: str, field_name: str) -> None:
    lowered = value.lower()
    for term in RUNTIME_PROMOTION_TERMS:
        if term in lowered:
            raise ValueError(f"{field_name} must not encode runtime promotion term: {term}")


def _require_research_observation_root(value: str | Path) -> Path:
    path = Path(value)
    parts = path.parts
    if ".." in parts:
        raise ValueError("observation root must not contain path traversal")
    has_research_artifact_root = any(
        parts[index] == "artifacts" and parts[index + 1] == "research"
        for index in range(len(parts) - 1)
    )
    if not has_research_artifact_root:
        raise ValueError("observation root must be under artifacts/research")
    return path


def _utc_now() -> datetime:
    return datetime.now(UTC)
