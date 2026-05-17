"""Research-only pre-apply evidence packages."""

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
    write_artifact_manifest_atomic,
)
from aats.data_platform.research_factory.evidence import EvidenceBundle
from aats.data_platform.research_factory.metrics.gates import CandidateArtifact
from aats.data_platform.research_factory.observations import (
    ObservationGateResult,
    ReviewOutcome,
)
from aats.data_platform.research_factory.recommendations import ResearchRecommendation

PREAPPLY_SCHEMA_VERSION = "research_preapply_evidence_v1"
PREAPPLY_REVIEW_SCHEMA_VERSION = "research_preapply_review_v1"
PREAPPLY_PACKAGE_REF = "preapply_evidence_package.json"
PREAPPLY_MANIFEST_REF = "preapply_manifest.json"
PREAPPLY_REVIEW_REF = "preapply_review.json"
PREAPPLY_REVIEW_DECISION_REF = "preapply_review_decision.json"
PREAPPLY_REVIEW_MANIFEST_REF = "preapply_review_manifest.json"

ALLOWED_PREAPPLY_PACKAGE_STATUSES = frozenset(
    {"preapply_ready", "needs_more_observation", "preapply_rejected"}
)
EXPECTED_PREAPPLY_STATUS_BY_REVIEW_DECISION = {
    "eligible_for_preapply": "preapply_ready",
    "keep_reviewing": "needs_more_observation",
    "reject": "preapply_rejected",
}
ALLOWED_PREAPPLY_REVIEW_STATUSES = frozenset({"review_pending"})
ALLOWED_PREAPPLY_REVIEW_DECISIONS = frozenset(
    {
        "review_approved_for_manual_apply_design",
        "review_rejected",
        "needs_more_evidence",
    }
)
REQUIRED_PREAPPLY_EVIDENCE_REFS = (
    "candidate_artifact",
    "research_recommendation",
    "metrics_snapshot",
    "dataset_quality_report",
    "source_integrity_report",
    "execution_evidence_report",
    "evidence_bundle",
    "observation_result",
    "review_outcome",
    "rollback_plan",
)
REQUIRED_PREAPPLY_GATE_REFS = (
    "candidate_gate",
    "observation_gate_result",
)
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
class PreApplyEvidencePackage:
    """Evidence-only package for future governance review before any apply path."""

    package_id: str
    candidate_id: str
    recommendation_id: str
    observation_id: str
    experiment_id: str
    status: str
    evidence_refs: Mapping[str, str]
    gate_refs: Mapping[str, str]
    review_decision: str
    candidate_gate_passed: bool
    evidence_bundle_passed: bool
    observation_gate_passed: bool
    failure_reasons: Sequence[str] = field(default_factory=tuple)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = PREAPPLY_SCHEMA_VERSION
    runtime_mutation_allowed: bool = False
    operator_approval_required: bool = True
    recommended_next_step: str | None = None
    notes: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_safe_identifier(self.package_id, "package_id")
        _require_safe_identifier(self.candidate_id, "candidate_id")
        _require_safe_identifier(self.recommendation_id, "recommendation_id")
        _require_safe_identifier(self.observation_id, "observation_id")
        _require_safe_identifier(self.experiment_id, "experiment_id")
        if self.status not in ALLOWED_PREAPPLY_PACKAGE_STATUSES:
            allowed = ", ".join(sorted(ALLOWED_PREAPPLY_PACKAGE_STATUSES))
            raise ValueError(f"preapply status must be one of: {allowed}")
        _require_consistent_preapply_status(self.status, self.review_decision)
        if not isinstance(self.candidate_gate_passed, bool):
            raise ValueError("candidate_gate_passed must be a bool")
        if not isinstance(self.evidence_bundle_passed, bool):
            raise ValueError("evidence_bundle_passed must be a bool")
        if not isinstance(self.observation_gate_passed, bool):
            raise ValueError("observation_gate_passed must be a bool")
        failures = _normalize_text_sequence(self.failure_reasons, "failure_reasons", allow_empty=True)
        if self.status == "preapply_ready":
            if self.review_decision != "eligible_for_preapply":
                raise ValueError("preapply_ready requires eligible_for_preapply review decision")
            if not self.candidate_gate_passed:
                raise ValueError("preapply_ready requires a passing candidate gate")
            if not self.evidence_bundle_passed:
                raise ValueError("preapply_ready requires a passing evidence bundle")
            if not self.observation_gate_passed:
                raise ValueError("preapply_ready requires a passing observation gate")
            if failures:
                raise ValueError("preapply_ready package must not contain failure_reasons")
        elif not failures:
            raise ValueError("non-ready preapply package must include failure_reasons")
        _require_timezone_aware_datetime(self.created_at, "created_at")
        if self.schema_version != PREAPPLY_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {PREAPPLY_SCHEMA_VERSION!r}")
        if self.runtime_mutation_allowed is not False:
            raise ValueError("preapply package must not allow runtime mutation")
        if self.operator_approval_required is not True:
            raise ValueError("preapply package must require operator approval")
        next_step = self.recommended_next_step or _default_next_step(self.status)
        next_step = _require_non_empty_text(next_step, "recommended_next_step")
        _reject_runtime_promotion_text(next_step, "recommended_next_step")
        object.__setattr__(self, "recommended_next_step", next_step)
        object.__setattr__(
            self,
            "evidence_refs",
            _normalize_required_refs(
                self.evidence_refs,
                REQUIRED_PREAPPLY_EVIDENCE_REFS,
                "evidence_refs",
            ),
        )
        object.__setattr__(
            self,
            "gate_refs",
            _normalize_required_refs(self.gate_refs, REQUIRED_PREAPPLY_GATE_REFS, "gate_refs"),
        )
        object.__setattr__(self, "failure_reasons", failures)
        object.__setattr__(self, "notes", _normalize_text_sequence(self.notes, "notes", allow_empty=True))


@dataclass(frozen=True, slots=True)
class PreApplyReview:
    """Pending research-only review for a pre-apply evidence package."""

    review_id: str
    package_id: str
    candidate_id: str
    recommendation_id: str
    observation_id: str
    experiment_id: str
    package_status: str
    status: str = "review_pending"
    package_ref: str = PREAPPLY_PACKAGE_REF
    reference_integrity_ref: str | None = None
    reference_integrity_passed: bool | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = PREAPPLY_REVIEW_SCHEMA_VERSION
    runtime_mutation_allowed: bool = False
    operator_approval_required: bool = True
    recommended_next_step: str = "collect_preapply_review_decision"
    notes: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_safe_identifier(self.review_id, "review_id")
        _require_safe_identifier(self.package_id, "package_id")
        _require_safe_identifier(self.candidate_id, "candidate_id")
        _require_safe_identifier(self.recommendation_id, "recommendation_id")
        _require_safe_identifier(self.observation_id, "observation_id")
        _require_safe_identifier(self.experiment_id, "experiment_id")
        if self.package_status not in ALLOWED_PREAPPLY_PACKAGE_STATUSES:
            allowed = ", ".join(sorted(ALLOWED_PREAPPLY_PACKAGE_STATUSES))
            raise ValueError(f"package_status must be one of: {allowed}")
        if self.status not in ALLOWED_PREAPPLY_REVIEW_STATUSES:
            allowed = ", ".join(sorted(ALLOWED_PREAPPLY_REVIEW_STATUSES))
            raise ValueError(f"preapply review status must be one of: {allowed}")
        object.__setattr__(self, "package_ref", _require_relative_ref(self.package_ref, "package_ref"))
        if self.reference_integrity_ref is not None:
            object.__setattr__(
                self,
                "reference_integrity_ref",
                _require_relative_ref(self.reference_integrity_ref, "reference_integrity_ref"),
            )
        if self.reference_integrity_passed is not None and not isinstance(self.reference_integrity_passed, bool):
            raise ValueError("reference_integrity_passed must be a bool when provided")
        _require_timezone_aware_datetime(self.created_at, "created_at")
        if self.schema_version != PREAPPLY_REVIEW_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {PREAPPLY_REVIEW_SCHEMA_VERSION!r}")
        if self.runtime_mutation_allowed is not False:
            raise ValueError("preapply review must not allow runtime mutation")
        if self.operator_approval_required is not True:
            raise ValueError("preapply review must require operator approval")
        next_step = _require_non_empty_text(self.recommended_next_step, "recommended_next_step")
        _reject_runtime_promotion_text(next_step, "recommended_next_step")
        object.__setattr__(self, "recommended_next_step", next_step)
        object.__setattr__(self, "notes", _normalize_text_sequence(self.notes, "notes", allow_empty=True))


@dataclass(frozen=True, slots=True)
class PreApplyReviewDecision:
    """Research-only decision on whether a pre-apply package merits manual design work."""

    review_id: str
    package_id: str
    candidate_id: str
    recommendation_id: str
    observation_id: str
    experiment_id: str
    decision: str
    rationale: str
    reviewed_by: str
    required_followups: Sequence[str] = field(default_factory=tuple)
    reviewed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    package_ref: str = PREAPPLY_PACKAGE_REF
    review_ref: str = PREAPPLY_REVIEW_REF
    schema_version: str = PREAPPLY_REVIEW_SCHEMA_VERSION
    runtime_mutation_allowed: bool = False
    operator_approval_required: bool = True
    recommended_next_step: str | None = None

    def __post_init__(self) -> None:
        _require_safe_identifier(self.review_id, "review_id")
        _require_safe_identifier(self.package_id, "package_id")
        _require_safe_identifier(self.candidate_id, "candidate_id")
        _require_safe_identifier(self.recommendation_id, "recommendation_id")
        _require_safe_identifier(self.observation_id, "observation_id")
        _require_safe_identifier(self.experiment_id, "experiment_id")
        if self.decision not in ALLOWED_PREAPPLY_REVIEW_DECISIONS:
            allowed = ", ".join(sorted(ALLOWED_PREAPPLY_REVIEW_DECISIONS))
            raise ValueError(f"preapply review decision must be one of: {allowed}")
        rationale = _require_non_empty_text(self.rationale, "rationale")
        _reject_runtime_promotion_text(rationale, "rationale")
        object.__setattr__(self, "rationale", rationale)
        reviewed_by = _require_non_empty_text(self.reviewed_by, "reviewed_by")
        _reject_runtime_promotion_text(reviewed_by, "reviewed_by")
        object.__setattr__(self, "reviewed_by", reviewed_by)
        followups = _normalize_text_sequence(self.required_followups, "required_followups", allow_empty=True)
        if self.decision == "needs_more_evidence" and not followups:
            raise ValueError("needs_more_evidence requires required_followups")
        object.__setattr__(self, "required_followups", followups)
        _require_timezone_aware_datetime(self.reviewed_at, "reviewed_at")
        object.__setattr__(self, "package_ref", _require_relative_ref(self.package_ref, "package_ref"))
        object.__setattr__(self, "review_ref", _require_relative_ref(self.review_ref, "review_ref"))
        if self.schema_version != PREAPPLY_REVIEW_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {PREAPPLY_REVIEW_SCHEMA_VERSION!r}")
        if self.runtime_mutation_allowed is not False:
            raise ValueError("preapply review decision must not allow runtime mutation")
        if self.operator_approval_required is not True:
            raise ValueError("preapply review decision must require operator approval")
        next_step = self.recommended_next_step or _default_review_next_step(self.decision)
        next_step = _require_non_empty_text(next_step, "recommended_next_step")
        _reject_runtime_promotion_text(next_step, "recommended_next_step")
        object.__setattr__(self, "recommended_next_step", next_step)


class PreApplyEvidenceRecorder:
    """Persist pre-apply evidence packages under a research-only artifact root."""

    def __init__(
        self,
        root: str | Path = Path("artifacts") / "research" / "research_factory" / "preapply",
        *,
        code_version: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = _require_research_preapply_root(root)
        self.code_version = code_version
        self._clock = clock or _utc_now

    def record_package(self, package: PreApplyEvidencePackage) -> dict[str, Any]:
        """Write a pre-apply evidence package and manifest."""
        if not isinstance(package, PreApplyEvidencePackage):
            raise ValueError("package must be a PreApplyEvidencePackage")
        package_dir = self._package_dir(package.package_id)
        if package_dir.exists():
            raise ValueError(f"preapply package {package.package_id!r} already exists")
        package_dir.mkdir(parents=True)
        _write_json_atomic(package_dir / PREAPPLY_PACKAGE_REF, _to_jsonable(package))
        manifest = build_artifact_manifest(
            artifact_id=package.package_id,
            artifact_type="preapply",
            status="succeeded",
            started_at=package.created_at,
            finished_at=self._now(),
            input_refs={
                "candidate_id": package.candidate_id,
                "recommendation_id": package.recommendation_id,
                "observation_id": package.observation_id,
                "experiment_id": package.experiment_id,
                "package_status": package.status,
            },
            output_refs={"preapply_evidence_package": PREAPPLY_PACKAGE_REF},
            code_version=self.code_version,
            notes="research-only pre-apply evidence package",
        )
        write_artifact_manifest_atomic(package_dir / PREAPPLY_MANIFEST_REF, manifest)
        return manifest

    def _package_dir(self, package_id: str) -> Path:
        return self.root / _require_safe_identifier(package_id, "package_id")

    def _now(self) -> datetime:
        return self._clock()


class PreApplyReviewRecorder:
    """Persist research-only pre-apply review artifacts."""

    def __init__(
        self,
        root: str | Path = Path("artifacts") / "research" / "research_factory" / "preapply_reviews",
        *,
        code_version: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = _require_research_preapply_root(root)
        self.code_version = code_version
        self._clock = clock or _utc_now

    def start_review(
        self,
        package: PreApplyEvidencePackage,
        *,
        package_ref: str = PREAPPLY_PACKAGE_REF,
        reference_integrity_ref: str | None = None,
        reference_integrity_passed: bool | None = None,
        review_id: str | None = None,
        notes: Sequence[str] = (),
    ) -> PreApplyReview:
        """Create a pending review for a pre-apply evidence package."""
        if not isinstance(package, PreApplyEvidencePackage):
            raise ValueError("package must be a PreApplyEvidencePackage")
        review = build_preapply_review(
            package,
            review_id=review_id,
            package_ref=package_ref,
            reference_integrity_ref=reference_integrity_ref,
            reference_integrity_passed=reference_integrity_passed,
            created_at=self._now(),
            notes=notes,
        )
        review_dir = self._review_dir(review.review_id)
        if review_dir.exists():
            raise ValueError(f"preapply review {review.review_id!r} already exists")
        review_dir.mkdir(parents=True)
        _write_json_atomic(review_dir / PREAPPLY_REVIEW_REF, _to_jsonable(review))
        manifest = build_artifact_manifest(
            artifact_id=review.review_id,
            artifact_type="preapply_review",
            status="running",
            started_at=review.created_at,
            input_refs={
                "package_id": review.package_id,
                "candidate_id": review.candidate_id,
                "recommendation_id": review.recommendation_id,
                "observation_id": review.observation_id,
                "experiment_id": review.experiment_id,
                "package_status": review.package_status,
                "package_ref": review.package_ref,
                "reference_integrity_ref": review.reference_integrity_ref,
                "reference_integrity_passed": review.reference_integrity_passed,
            },
            output_refs={"preapply_review": PREAPPLY_REVIEW_REF},
            code_version=self.code_version,
            notes="research-only pre-apply review",
        )
        write_artifact_manifest_atomic(review_dir / PREAPPLY_REVIEW_MANIFEST_REF, manifest)
        return review

    def record_decision(self, decision: PreApplyReviewDecision) -> dict[str, Any]:
        """Record a final decision for a pending pre-apply review."""
        if not isinstance(decision, PreApplyReviewDecision):
            raise ValueError("decision must be a PreApplyReviewDecision")
        review_dir = self._review_dir(decision.review_id)
        manifest_path = review_dir / PREAPPLY_REVIEW_MANIFEST_REF
        if not manifest_path.exists():
            raise ValueError(f"preapply review {decision.review_id!r} does not exist")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["status"] != "running":
            raise ValueError(f"preapply review {decision.review_id!r} is already terminal")
        stored_review = _load_json_mapping(review_dir / PREAPPLY_REVIEW_REF, "preapply_review")
        _require_decision_matches_review(decision, stored_review)

        _write_json_atomic(review_dir / PREAPPLY_REVIEW_DECISION_REF, _to_jsonable(decision))
        output_refs = dict(manifest["output_refs"])
        output_refs["preapply_review_decision"] = PREAPPLY_REVIEW_DECISION_REF
        updated = build_artifact_manifest(
            artifact_id=manifest["artifact_id"],
            artifact_type=manifest["artifact_type"],
            status="succeeded",
            started_at=manifest["started_at"],
            finished_at=self._now(),
            input_refs=manifest["input_refs"],
            output_refs=output_refs,
            code_version=manifest.get("code_version"),
            notes=manifest.get("notes"),
        )
        write_artifact_manifest_atomic(manifest_path, updated)
        return updated

    def _review_dir(self, review_id: str) -> Path:
        return self.root / _require_safe_identifier(review_id, "review_id")

    def _now(self) -> datetime:
        return self._clock()


def build_preapply_evidence_package(
    *,
    candidate: CandidateArtifact,
    recommendation: ResearchRecommendation,
    evidence_bundle: EvidenceBundle,
    observation_gate: ObservationGateResult,
    review_outcome: ReviewOutcome,
    evidence_refs: Mapping[str, str],
    gate_refs: Mapping[str, str],
    package_id: str | None = None,
    created_at: datetime | None = None,
) -> PreApplyEvidencePackage:
    """Build a research-only evidence package for later pre-apply governance review."""
    if not isinstance(candidate, CandidateArtifact):
        raise ValueError("candidate must be a CandidateArtifact")
    if not isinstance(recommendation, ResearchRecommendation):
        raise ValueError("recommendation must be a ResearchRecommendation")
    if not isinstance(evidence_bundle, EvidenceBundle):
        raise ValueError("evidence_bundle must be an EvidenceBundle")
    if not isinstance(observation_gate, ObservationGateResult):
        raise ValueError("observation_gate must be an ObservationGateResult")
    if not isinstance(review_outcome, ReviewOutcome):
        raise ValueError("review_outcome must be a ReviewOutcome")
    _require_matching_package_inputs(candidate, recommendation, observation_gate, review_outcome)

    status = _package_status(review_outcome.decision)
    failure_reasons = _package_failure_reasons(
        status=status,
        review_outcome=review_outcome,
        evidence_bundle=evidence_bundle,
        observation_gate=observation_gate,
        candidate=candidate,
    )
    if status == "preapply_ready":
        _require_preapply_ready_inputs(candidate, recommendation, evidence_bundle, observation_gate, review_outcome)

    return PreApplyEvidencePackage(
        package_id=package_id or f"preapply_{review_outcome.observation_id}",
        candidate_id=candidate.candidate_id,
        recommendation_id=recommendation.recommendation_id,
        observation_id=review_outcome.observation_id,
        experiment_id=candidate.experiment_id,
        status=status,
        evidence_refs=evidence_refs,
        gate_refs=gate_refs,
        review_decision=review_outcome.decision,
        candidate_gate_passed=candidate.gate.passed,
        evidence_bundle_passed=evidence_bundle.passed,
        observation_gate_passed=observation_gate.passed,
        failure_reasons=failure_reasons,
        created_at=created_at or datetime.now(UTC),
        notes=(
            "pre-apply evidence package is review-only",
            "separate governance approval is required before any trading-system change",
        ),
    )


def build_preapply_review(
    package: PreApplyEvidencePackage,
    *,
    review_id: str | None = None,
    package_ref: str = PREAPPLY_PACKAGE_REF,
    reference_integrity_ref: str | None = None,
    reference_integrity_passed: bool | None = None,
    created_at: datetime | None = None,
    notes: Sequence[str] = (),
) -> PreApplyReview:
    """Build a pending research-only review from a pre-apply evidence package."""
    if not isinstance(package, PreApplyEvidencePackage):
        raise ValueError("package must be a PreApplyEvidencePackage")
    return PreApplyReview(
        review_id=review_id or f"review_{package.package_id}",
        package_id=package.package_id,
        candidate_id=package.candidate_id,
        recommendation_id=package.recommendation_id,
        observation_id=package.observation_id,
        experiment_id=package.experiment_id,
        package_status=package.status,
        package_ref=package_ref,
        reference_integrity_ref=reference_integrity_ref,
        reference_integrity_passed=reference_integrity_passed,
        created_at=created_at or datetime.now(UTC),
        notes=notes,
    )


def build_preapply_review_decision(
    *,
    review: PreApplyReview,
    package: PreApplyEvidencePackage,
    decision: str,
    rationale: str,
    reviewed_by: str,
    required_followups: Sequence[str] = (),
    reviewed_at: datetime | None = None,
) -> PreApplyReviewDecision:
    """Build a research-only decision for a pending pre-apply review."""
    if not isinstance(review, PreApplyReview):
        raise ValueError("review must be a PreApplyReview")
    if not isinstance(package, PreApplyEvidencePackage):
        raise ValueError("package must be a PreApplyEvidencePackage")
    _require_review_matches_package(review, package)
    if decision == "review_approved_for_manual_apply_design" and package.status != "preapply_ready":
        raise ValueError("manual apply design review approval requires a preapply_ready package")
    if decision == "review_approved_for_manual_apply_design" and review.reference_integrity_passed is False:
        raise ValueError("manual apply design review approval requires passing reference integrity")
    return PreApplyReviewDecision(
        review_id=review.review_id,
        package_id=package.package_id,
        candidate_id=package.candidate_id,
        recommendation_id=package.recommendation_id,
        observation_id=package.observation_id,
        experiment_id=package.experiment_id,
        decision=decision,
        rationale=rationale,
        reviewed_by=reviewed_by,
        required_followups=required_followups,
        reviewed_at=reviewed_at or datetime.now(UTC),
        package_ref=review.package_ref,
        review_ref=PREAPPLY_REVIEW_REF,
    )


def _require_preapply_ready_inputs(
    candidate: CandidateArtifact,
    recommendation: ResearchRecommendation,
    evidence_bundle: EvidenceBundle,
    observation_gate: ObservationGateResult,
    review_outcome: ReviewOutcome,
) -> None:
    if recommendation.status != "ready_for_review":
        raise ValueError("preapply_ready requires a ready_for_review recommendation")
    if not candidate.gate.passed:
        raise ValueError("preapply_ready requires a passing candidate gate")
    if not evidence_bundle.passed:
        raise ValueError("preapply_ready requires a passing evidence bundle")
    if not observation_gate.passed:
        raise ValueError("preapply_ready requires a passing observation gate")
    if review_outcome.observation_gate_passed is not True:
        raise ValueError("preapply_ready requires review outcome to confirm passing observation gate")
    if review_outcome.runtime_mutation_allowed is not False:
        raise ValueError("preapply_ready requires review outcome without runtime mutation")
    if review_outcome.operator_approval_required is not True:
        raise ValueError("preapply_ready requires operator approval")


def _require_matching_package_inputs(
    candidate: CandidateArtifact,
    recommendation: ResearchRecommendation,
    observation_gate: ObservationGateResult,
    review_outcome: ReviewOutcome,
) -> None:
    if recommendation.candidate_id != candidate.candidate_id:
        raise ValueError("recommendation candidate_id must match candidate")
    if recommendation.experiment_id != candidate.experiment_id:
        raise ValueError("recommendation experiment_id must match candidate")
    if observation_gate.recommendation_id != recommendation.recommendation_id:
        raise ValueError("observation gate recommendation_id must match recommendation")
    if observation_gate.candidate_id != candidate.candidate_id:
        raise ValueError("observation gate candidate_id must match candidate")
    if observation_gate.experiment_id != candidate.experiment_id:
        raise ValueError("observation gate experiment_id must match candidate")
    if review_outcome.recommendation_id != recommendation.recommendation_id:
        raise ValueError("review outcome recommendation_id must match recommendation")
    if review_outcome.candidate_id != candidate.candidate_id:
        raise ValueError("review outcome candidate_id must match candidate")
    if review_outcome.experiment_id != candidate.experiment_id:
        raise ValueError("review outcome experiment_id must match candidate")
    if review_outcome.observation_id != observation_gate.observation_id:
        raise ValueError("review outcome observation_id must match observation gate")
    if (
        review_outcome.observation_gate_passed is not None
        and review_outcome.observation_gate_passed != observation_gate.passed
    ):
        raise ValueError("review outcome observation_gate_passed must match observation gate")


def _package_status(review_decision: str) -> str:
    if review_decision == "eligible_for_preapply":
        return "preapply_ready"
    if review_decision == "keep_reviewing":
        return "needs_more_observation"
    if review_decision == "reject":
        return "preapply_rejected"
    raise ValueError("review_decision must be keep_reviewing, reject, or eligible_for_preapply")


def _package_failure_reasons(
    *,
    status: str,
    review_outcome: ReviewOutcome,
    evidence_bundle: EvidenceBundle,
    observation_gate: ObservationGateResult,
    candidate: CandidateArtifact,
) -> tuple[str, ...]:
    if status == "preapply_ready":
        return ()
    reasons: list[str] = [f"review_decision={review_outcome.decision}"]
    if not candidate.gate.passed:
        reasons.extend(f"candidate_gate: {failure}" for failure in candidate.gate.failures)
    if not evidence_bundle.passed:
        reasons.extend(f"evidence_bundle: {failure}" for failure in evidence_bundle.failures)
    if not observation_gate.passed:
        reasons.extend(f"observation_gate: {failure}" for failure in observation_gate.failures)
    return tuple(reasons)


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


def _normalize_required_refs(
    refs: Mapping[str, str],
    required_names: Sequence[str],
    field_name: str,
) -> dict[str, str]:
    if not isinstance(refs, Mapping) or not refs:
        raise ValueError(f"{field_name} must be a non-empty mapping")
    normalized: dict[str, str] = {}
    for name, value in refs.items():
        ref_name = _require_non_empty_text(name, f"{field_name} key")
        _reject_runtime_promotion_text(ref_name, f"{field_name} key")
        normalized[ref_name] = _require_relative_ref(value, f"{field_name}.{ref_name}")
    for required_ref in required_names:
        if required_ref not in normalized:
            raise ValueError(f"{field_name} missing required ref: {required_ref}")
    return dict(sorted(normalized.items()))


def _normalize_text_sequence(values: Sequence[str], field_name: str, *, allow_empty: bool) -> tuple[str, ...]:
    if isinstance(values, str | bytes | bytearray) or not isinstance(values, Sequence):
        raise ValueError(f"{field_name} must be a sequence of strings")
    normalized = tuple(_require_non_empty_text(value, field_name) for value in values)
    if not allow_empty and not normalized:
        raise ValueError(f"{field_name} must not be empty")
    for value in normalized:
        _reject_runtime_promotion_text(value, field_name)
    return normalized


def _default_next_step(status: str) -> str:
    if status == "preapply_ready":
        return "submit_preapply_evidence_for_operator_review"
    if status == "needs_more_observation":
        return "continue_shadow_or_paper_observation"
    return "archive_preapply_rejection"


def _default_review_next_step(decision: str) -> str:
    if decision == "review_approved_for_manual_apply_design":
        return "prepare_manual_apply_design_for_separate_governance_review"
    if decision == "needs_more_evidence":
        return "collect_additional_preapply_evidence"
    return "archive_preapply_review_rejection"


def _require_consistent_preapply_status(status: str, review_decision: str) -> None:
    review_decision = _require_non_empty_text(review_decision, "review_decision")
    expected_status = EXPECTED_PREAPPLY_STATUS_BY_REVIEW_DECISION.get(review_decision)
    if expected_status is None:
        allowed = ", ".join(sorted(EXPECTED_PREAPPLY_STATUS_BY_REVIEW_DECISION))
        raise ValueError(f"review_decision must be one of: {allowed}")
    if status != expected_status:
        raise ValueError(f"preapply status {status!r} must match review_decision {review_decision!r}")


def _require_review_matches_package(review: PreApplyReview, package: PreApplyEvidencePackage) -> None:
    if review.package_id != package.package_id:
        raise ValueError("review package_id must match package")
    if review.candidate_id != package.candidate_id:
        raise ValueError("review candidate_id must match package")
    if review.recommendation_id != package.recommendation_id:
        raise ValueError("review recommendation_id must match package")
    if review.observation_id != package.observation_id:
        raise ValueError("review observation_id must match package")
    if review.experiment_id != package.experiment_id:
        raise ValueError("review experiment_id must match package")
    if review.package_status != package.status:
        raise ValueError("review package_status must match package status")


def _require_decision_matches_review(
    decision: PreApplyReviewDecision,
    review_payload: Mapping[str, Any],
) -> None:
    fields_to_match = (
        "review_id",
        "package_id",
        "candidate_id",
        "recommendation_id",
        "observation_id",
        "experiment_id",
        "package_ref",
    )
    for field_name in fields_to_match:
        if getattr(decision, field_name) != review_payload.get(field_name):
            raise ValueError(f"review decision {field_name} must match preapply review")


def _load_json_mapping(path: Path, field_name: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"{field_name} must be a JSON object")
    return payload


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


def _require_safe_identifier(value: Any, field_name: str) -> str:
    value = _require_non_empty_text(value, field_name)
    if "/" in value or "\\" in value or value in {".", ".."} or ".." in value:
        raise ValueError(f"{field_name} must not contain path traversal or separators")
    return value


def _require_non_empty_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_timezone_aware_datetime(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")


def _reject_runtime_promotion_text(value: str, field_name: str) -> None:
    lowered = value.lower()
    for term in RUNTIME_PROMOTION_TERMS:
        if term in lowered:
            raise ValueError(f"{field_name} must not encode runtime promotion term: {term}")


def _require_research_preapply_root(value: str | Path) -> Path:
    path = Path(value)
    parts = path.parts
    if ".." in parts:
        raise ValueError("preapply root must not contain path traversal")
    has_research_artifact_root = any(
        parts[index] == "artifacts" and parts[index + 1] == "research"
        for index in range(len(parts) - 1)
    )
    if not has_research_artifact_root:
        raise ValueError("preapply root must be under artifacts/research")
    return path


def _utc_now() -> datetime:
    return datetime.now(UTC)
