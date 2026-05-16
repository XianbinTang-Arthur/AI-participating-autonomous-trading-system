"""Research-only recommendation evidence packages."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from aats.data_platform.research_factory.metrics.gates import (
    CandidateArtifact,
    CandidateGateResult,
)
from aats.data_platform.research_factory.specs import MetricsSnapshot

RECOMMENDATION_SCHEMA_VERSION = "research_recommendation_v1"
ALLOWED_RECOMMENDATION_STATUSES = frozenset({"draft", "ready_for_review", "rejected"})
ALLOWED_OBSERVATION_MODES = frozenset({"shadow", "paper"})
ALLOWED_BENCHMARK_SEGMENTS = frozenset({"valid", "test", "replay"})
REQUIRED_EVIDENCE_REFS = (
    "candidate_artifact",
    "metrics_snapshot",
    "experiment_manifest",
)
RUNTIME_COMMAND_TERMS = (
    "live_order",
    "okx_write",
    "operator_write",
    "production_config",
)
REQUIRED_EXECUTION_REALISM_METRICS = (
    "turnover",
    "fee_bps_mean",
    "slippage_bps_mean",
    "funding_bps_mean",
    "fillable_ratio",
    "partial_fill_ratio",
    "cost_adjusted_edge_bps_mean",
)


@dataclass(frozen=True, slots=True)
class ObservationPlan:
    """Shadow or paper observation requirements for a research recommendation."""

    mode: str
    min_observation_bars: int
    min_observation_events: int
    success_criteria: Sequence[str]
    abort_conditions: Sequence[str]
    notes: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in ALLOWED_OBSERVATION_MODES:
            allowed = ", ".join(sorted(ALLOWED_OBSERVATION_MODES))
            raise ValueError(f"observation mode must be one of: {allowed}")
        _require_positive_int(self.min_observation_bars, "min_observation_bars")
        _require_positive_int(self.min_observation_events, "min_observation_events")
        object.__setattr__(
            self,
            "success_criteria",
            _normalize_text_sequence(self.success_criteria, "success_criteria"),
        )
        object.__setattr__(
            self,
            "abort_conditions",
            _normalize_text_sequence(self.abort_conditions, "abort_conditions"),
        )
        if self.notes is not None:
            object.__setattr__(self, "notes", _require_non_empty_text(self.notes, "notes"))


@dataclass(frozen=True, slots=True)
class RollbackPlan:
    """Required rollback evidence for future promotion workflows."""

    rollback_required: bool
    trigger_conditions: Sequence[str]
    operator_actions: Sequence[str]
    verification_checks: Sequence[str]

    def __post_init__(self) -> None:
        if self.rollback_required is not True:
            raise ValueError("rollback plan must be required")
        object.__setattr__(
            self,
            "trigger_conditions",
            _normalize_text_sequence(self.trigger_conditions, "trigger_conditions"),
        )
        operator_actions = _normalize_text_sequence(self.operator_actions, "operator_actions")
        for action in operator_actions:
            _reject_runtime_command_text(action, "operator_actions")
        object.__setattr__(self, "operator_actions", operator_actions)
        object.__setattr__(
            self,
            "verification_checks",
            _normalize_text_sequence(self.verification_checks, "verification_checks"),
        )


@dataclass(frozen=True, slots=True)
class PreApplyEvidence:
    """Evidence bundle that must be reviewed before any future promotion workflow."""

    candidate_id: str
    experiment_id: str
    metrics: MetricsSnapshot
    gate: CandidateGateResult
    dataset_fingerprint: str
    benchmark_segment: str
    evidence_refs: Mapping[str, str]
    execution_realism_required: bool = False
    limitations: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_safe_identifier(self.candidate_id, "candidate_id")
        _require_safe_identifier(self.experiment_id, "experiment_id")
        if not isinstance(self.metrics, MetricsSnapshot):
            raise ValueError("pre-apply evidence metrics must be a MetricsSnapshot")
        if not isinstance(self.gate, CandidateGateResult):
            raise ValueError("pre-apply evidence gate must be a CandidateGateResult")
        if not self.gate.passed:
            raise ValueError("pre-apply evidence requires a passing gate")
        _require_non_empty_text(self.dataset_fingerprint, "dataset_fingerprint")
        if self.benchmark_segment not in ALLOWED_BENCHMARK_SEGMENTS:
            allowed = ", ".join(sorted(ALLOWED_BENCHMARK_SEGMENTS))
            raise ValueError(f"benchmark_segment must be one of: {allowed}")
        if not isinstance(self.execution_realism_required, bool):
            raise ValueError("execution_realism_required must be a bool")
        if self.execution_realism_required:
            _require_execution_realism_metrics(self.metrics)
        object.__setattr__(self, "evidence_refs", _normalize_evidence_refs(self.evidence_refs))
        object.__setattr__(
            self,
            "limitations",
            _normalize_optional_text_sequence(self.limitations, "limitations"),
        )


@dataclass(frozen=True, slots=True)
class ResearchRecommendation:
    """Research-only recommendation package for governance review."""

    recommendation_id: str
    candidate_id: str
    experiment_id: str
    status: str
    evidence: PreApplyEvidence
    observation_plan: ObservationPlan
    rollback_plan: RollbackPlan
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = RECOMMENDATION_SCHEMA_VERSION
    runtime_mutation_allowed: bool = False
    operator_approval_required: bool = True
    recommended_next_step: str = "review_for_shadow_or_paper"
    notes: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_safe_identifier(self.recommendation_id, "recommendation_id")
        _require_safe_identifier(self.candidate_id, "candidate_id")
        _require_safe_identifier(self.experiment_id, "experiment_id")
        if self.status not in ALLOWED_RECOMMENDATION_STATUSES:
            allowed = ", ".join(sorted(ALLOWED_RECOMMENDATION_STATUSES))
            raise ValueError(f"recommendation status must be one of: {allowed}")
        if not isinstance(self.evidence, PreApplyEvidence):
            raise ValueError("recommendation evidence must be PreApplyEvidence")
        if not isinstance(self.observation_plan, ObservationPlan):
            raise ValueError("recommendation observation_plan must be ObservationPlan")
        if not isinstance(self.rollback_plan, RollbackPlan):
            raise ValueError("recommendation rollback_plan must be RollbackPlan")
        if self.evidence.candidate_id != self.candidate_id:
            raise ValueError("recommendation candidate_id must match evidence")
        if self.evidence.experiment_id != self.experiment_id:
            raise ValueError("recommendation experiment_id must match evidence")
        _require_timezone_aware_datetime(self.created_at, "created_at")
        if self.schema_version != RECOMMENDATION_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {RECOMMENDATION_SCHEMA_VERSION!r}")
        if self.runtime_mutation_allowed is not False:
            raise ValueError("research recommendation must not allow runtime mutation")
        if self.operator_approval_required is not True:
            raise ValueError("research recommendation must require operator approval")
        object.__setattr__(
            self,
            "recommended_next_step",
            _require_non_empty_text(self.recommended_next_step, "recommended_next_step"),
        )
        _reject_runtime_command_text(self.recommended_next_step, "recommended_next_step")
        object.__setattr__(
            self,
            "notes",
            _normalize_optional_text_sequence(self.notes, "notes"),
        )


def build_research_recommendation(
    candidate: CandidateArtifact,
    *,
    evidence_refs: Mapping[str, str],
    observation_plan: ObservationPlan | None = None,
    rollback_plan: RollbackPlan | None = None,
    recommendation_id: str | None = None,
    status: str = "ready_for_review",
    created_at: datetime | None = None,
    require_execution_realism: bool = False,
) -> ResearchRecommendation:
    """Build a deterministic research-only recommendation from a passing candidate."""
    if not isinstance(candidate, CandidateArtifact):
        raise ValueError("candidate must be a CandidateArtifact")
    if not isinstance(require_execution_realism, bool):
        raise ValueError("require_execution_realism must be a bool")

    dataset_fingerprint = _require_non_empty_text(
        candidate.payload.get("dataset_fingerprint"),
        "candidate.payload.dataset_fingerprint",
    )
    benchmark_segment = _require_non_empty_text(
        candidate.payload.get("benchmark_segment"),
        "candidate.payload.benchmark_segment",
    )
    execution_realism_required = (
        require_execution_realism or candidate.payload.get("execution_cost_summary_ref") is not None
    )

    evidence = PreApplyEvidence(
        candidate_id=candidate.candidate_id,
        experiment_id=candidate.experiment_id,
        metrics=candidate.metrics,
        gate=candidate.gate,
        dataset_fingerprint=dataset_fingerprint,
        benchmark_segment=benchmark_segment,
        evidence_refs=evidence_refs,
        execution_realism_required=execution_realism_required,
        limitations=(
            "research recommendation is evidence only",
            "future promotion requires separate governance approval",
        ),
    )
    return ResearchRecommendation(
        recommendation_id=recommendation_id or f"rec_{candidate.candidate_id}",
        candidate_id=candidate.candidate_id,
        experiment_id=candidate.experiment_id,
        status=status,
        evidence=evidence,
        observation_plan=observation_plan or default_observation_plan(),
        rollback_plan=rollback_plan or default_rollback_plan(),
        created_at=created_at or datetime.now(UTC),
        notes=(
            "candidate is eligible for research review",
            "no runtime configuration mutation is authorized by this artifact",
        ),
    )


def default_observation_plan() -> ObservationPlan:
    """Return conservative shadow observation defaults."""
    return ObservationPlan(
        mode="shadow",
        min_observation_bars=48,
        min_observation_events=10,
        success_criteria=(
            "cost-adjusted edge remains positive",
            "drawdown stays within candidate gate limit",
            "turnover remains consistent with research evidence",
        ),
        abort_conditions=(
            "cost-adjusted edge turns non-positive",
            "fillability falls below review threshold",
            "unexplained metric drift appears",
        ),
    )


def default_rollback_plan() -> RollbackPlan:
    """Return a review-only rollback plan for future promotion workflows."""
    return RollbackPlan(
        rollback_required=True,
        trigger_conditions=(
            "shadow or paper observation invalidates research evidence",
            "execution costs erase the expected net edge",
            "governance review rejects the evidence package",
        ),
        operator_actions=(
            "withdraw the recommendation from review",
            "retain the prior runtime configuration",
            "record the rejection reason in the observation report",
        ),
        verification_checks=(
            "recommendation status is no longer ready for review",
            "candidate artifact remains archived for audit",
            "no runtime configuration change was produced by Research Factory",
        ),
    )


def _normalize_evidence_refs(refs: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(refs, Mapping) or not refs:
        raise ValueError("evidence_refs must be a non-empty mapping")
    normalized: dict[str, str] = {}
    for name, value in refs.items():
        ref_name = _require_non_empty_text(name, "evidence ref name")
        ref_value = _require_relative_ref(value, f"evidence_refs.{ref_name}")
        normalized[ref_name] = ref_value
    for required_ref in REQUIRED_EVIDENCE_REFS:
        if required_ref not in normalized:
            raise ValueError(f"evidence_refs missing required ref: {required_ref}")
    return dict(sorted(normalized.items()))


def _require_execution_realism_metrics(metrics: MetricsSnapshot) -> None:
    missing = [field_name for field_name in REQUIRED_EXECUTION_REALISM_METRICS if getattr(metrics, field_name) is None]
    if missing:
        rendered = ", ".join(missing)
        raise ValueError(f"execution realism evidence missing required metrics: {rendered}")


def _normalize_text_sequence(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    normalized = _normalize_optional_text_sequence(values, field_name)
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _normalize_optional_text_sequence(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, str | bytes | bytearray) or not isinstance(values, Sequence):
        raise ValueError(f"{field_name} must be a sequence of strings")
    return tuple(_require_non_empty_text(value, field_name) for value in values)


def _require_safe_identifier(value: str, field_name: str) -> str:
    value = _require_non_empty_text(value, field_name)
    if "/" in value or "\\" in value or value in {".", ".."} or ".." in value:
        raise ValueError(f"{field_name} must not contain path traversal or separators")
    return value


def _require_non_empty_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_positive_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")


def _require_timezone_aware_datetime(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")


def _require_relative_ref(value: Any, field_name: str) -> str:
    ref = _require_non_empty_text(value, field_name)
    if ref.startswith("~"):
        raise ValueError(f"{field_name} must be a relative artifact ref")
    posix_path = PurePosixPath(ref)
    windows_path = PureWindowsPath(ref)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError(f"{field_name} must be a relative artifact ref")
    if ".." in posix_path.parts or ".." in windows_path.parts:
        raise ValueError(f"{field_name} must not contain path traversal")
    return ref


def _reject_runtime_command_text(value: str, field_name: str) -> None:
    lowered = value.lower()
    for term in RUNTIME_COMMAND_TERMS:
        if term in lowered:
            raise ValueError(f"{field_name} must not encode runtime command term: {term}")
