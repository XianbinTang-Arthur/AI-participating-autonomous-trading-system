"""Research-only manual apply design packages."""

from __future__ import annotations

import json
import math
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
from aats.data_platform.research_factory.preapply import (
    PREAPPLY_PACKAGE_REF,
    PREAPPLY_REVIEW_DECISION_REF,
    PREAPPLY_REVIEW_REF,
    PreApplyEvidencePackage,
    PreApplyReview,
    PreApplyReviewDecision,
)

MANUAL_APPLY_DESIGN_SCHEMA_VERSION = "research_manual_apply_design_v1"
MANUAL_APPLY_DESIGN_VALIDATION_SCHEMA_VERSION = "research_manual_apply_design_validation_v1"
MANUAL_APPLY_DESIGN_REVIEW_SCHEMA_VERSION = "research_manual_apply_design_review_v1"
MANUAL_APPLY_DESIGN_PACKAGE_REF = "manual_apply_design_package.json"
MANUAL_APPLY_DESIGN_MANIFEST_REF = "manual_apply_design_manifest.json"
MANUAL_APPLY_DESIGN_VALIDATION_REF = "manual_apply_design_validation_report.json"
MANUAL_APPLY_DESIGN_REVIEW_REF = "manual_apply_design_review.json"
MANUAL_APPLY_DESIGN_REVIEW_DECISION_REF = "manual_apply_design_review_decision.json"
MANUAL_APPLY_DESIGN_REVIEW_MANIFEST_REF = "manual_apply_design_review_manifest.json"

ALLOWED_MANUAL_APPLY_DESIGN_STATUSES = frozenset(
    {"design_draft", "design_ready_for_review", "design_rejected"}
)
ALLOWED_MANUAL_APPLY_CANDIDATE_TYPES = frozenset(
    {"factor", "model", "parameter", "execution_policy", "risk_budget", "regime_classifier"}
)
ALLOWED_MANUAL_APPLY_DESIGN_REVIEW_STATUSES = frozenset({"review_pending"})
ALLOWED_MANUAL_APPLY_DESIGN_REVIEW_DECISIONS = frozenset(
    {"design_ready_for_dry_run_planning", "design_rejected", "needs_design_revision"}
)
REQUIRED_MANUAL_APPLY_DESIGN_EVIDENCE_REFS = (
    "preapply_evidence_package",
    "preapply_review",
    "preapply_review_decision",
    "rollback_plan",
)
DESIGN_PROMOTION_TERMS = (
    "active_parameter",
    "active parameter",
    "active_parameters",
    "active parameters",
    "approved_for_apply",
    "auto_apply",
    "auto apply",
    "direct_apply",
    "direct apply",
    "live_order",
    "live order",
    "okx_write",
    "okx write",
    "operator_write",
    "operator write",
    "production_config",
    "production config",
    "production_deploy",
    "production deploy",
    "runtime_config_write",
    "runtime config write",
    "runtime_mutation",
    "runtime mutation",
)


@dataclass(frozen=True, slots=True)
class ManualApplyDesignPackage:
    """Design-only artifact for a separate manual apply design review."""

    design_id: str
    source_preapply_review_id: str
    source_preapply_package_id: str
    candidate_id: str
    recommendation_id: str
    observation_id: str
    experiment_id: str
    candidate_type: str
    status: str
    proposed_change_summary: str
    parameter_or_config_delta: Mapping[str, Any]
    affected_runtime_components: Sequence[str]
    required_risk_guards: Sequence[str]
    required_dry_run_checks: Sequence[str]
    rollback_plan_ref: str
    evidence_refs: Mapping[str, str]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = MANUAL_APPLY_DESIGN_SCHEMA_VERSION
    runtime_mutation_allowed: bool = False
    operator_approval_required: bool = True
    recommended_next_step: str = "submit_manual_apply_design_for_separate_governance_review"
    notes: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_safe_identifier(self.design_id, "design_id")
        _require_safe_identifier(self.source_preapply_review_id, "source_preapply_review_id")
        _require_safe_identifier(self.source_preapply_package_id, "source_preapply_package_id")
        _require_safe_identifier(self.candidate_id, "candidate_id")
        _require_safe_identifier(self.recommendation_id, "recommendation_id")
        _require_safe_identifier(self.observation_id, "observation_id")
        _require_safe_identifier(self.experiment_id, "experiment_id")
        candidate_type = _require_candidate_type(self.candidate_type)
        object.__setattr__(self, "candidate_type", candidate_type)
        if self.status not in ALLOWED_MANUAL_APPLY_DESIGN_STATUSES:
            allowed = ", ".join(sorted(ALLOWED_MANUAL_APPLY_DESIGN_STATUSES))
            raise ValueError(f"manual apply design status must be one of: {allowed}")

        proposed_change_summary = _require_non_empty_text(
            self.proposed_change_summary,
            "proposed_change_summary",
        )
        _reject_promotion_text(proposed_change_summary, "proposed_change_summary")
        object.__setattr__(self, "proposed_change_summary", proposed_change_summary)
        object.__setattr__(
            self,
            "parameter_or_config_delta",
            _normalize_json_mapping(
                self.parameter_or_config_delta,
                "parameter_or_config_delta",
            ),
        )
        object.__setattr__(
            self,
            "affected_runtime_components",
            _normalize_text_sequence(
                self.affected_runtime_components,
                "affected_runtime_components",
            ),
        )
        object.__setattr__(
            self,
            "required_risk_guards",
            _normalize_text_sequence(self.required_risk_guards, "required_risk_guards"),
        )
        object.__setattr__(
            self,
            "required_dry_run_checks",
            _normalize_text_sequence(self.required_dry_run_checks, "required_dry_run_checks"),
        )
        object.__setattr__(
            self,
            "rollback_plan_ref",
            _require_relative_ref(self.rollback_plan_ref, "rollback_plan_ref"),
        )
        object.__setattr__(
            self,
            "evidence_refs",
            _normalize_required_refs(
                self.evidence_refs,
                REQUIRED_MANUAL_APPLY_DESIGN_EVIDENCE_REFS,
                "evidence_refs",
            ),
        )
        _require_timezone_aware_datetime(self.created_at, "created_at")
        if self.schema_version != MANUAL_APPLY_DESIGN_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {MANUAL_APPLY_DESIGN_SCHEMA_VERSION!r}")
        if self.runtime_mutation_allowed is not False:
            raise ValueError("manual apply design package must not allow runtime mutation")
        if self.operator_approval_required is not True:
            raise ValueError("manual apply design package must require operator approval")
        next_step = _require_non_empty_text(self.recommended_next_step, "recommended_next_step")
        _reject_promotion_text(next_step, "recommended_next_step")
        object.__setattr__(self, "recommended_next_step", next_step)
        object.__setattr__(
            self,
            "notes",
            _normalize_text_sequence(self.notes, "notes", allow_empty=True),
        )


@dataclass(frozen=True, slots=True)
class ManualApplyDesignValidationReport:
    """Candidate-type domain validation for a manual apply design draft."""

    design_id: str
    candidate_id: str
    candidate_type: str
    passed: bool
    failures: Sequence[str] = field(default_factory=tuple)
    warnings: Sequence[str] = field(default_factory=tuple)
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = MANUAL_APPLY_DESIGN_VALIDATION_SCHEMA_VERSION
    runtime_mutation_allowed: bool = False
    operator_approval_required: bool = True

    def __post_init__(self) -> None:
        _require_safe_identifier(self.design_id, "design_id")
        _require_safe_identifier(self.candidate_id, "candidate_id")
        candidate_type = _require_candidate_type(self.candidate_type)
        object.__setattr__(self, "candidate_type", candidate_type)
        if not isinstance(self.passed, bool):
            raise ValueError("passed must be a bool")
        failures = _normalize_text_sequence(self.failures, "failures", allow_empty=True)
        warnings = _normalize_text_sequence(self.warnings, "warnings", allow_empty=True)
        if self.passed and failures:
            raise ValueError("passing validation report must not contain failures")
        if not self.passed and not failures:
            raise ValueError("failing validation report must include failures")
        object.__setattr__(self, "failures", failures)
        object.__setattr__(self, "warnings", warnings)
        _require_timezone_aware_datetime(self.evaluated_at, "evaluated_at")
        if self.schema_version != MANUAL_APPLY_DESIGN_VALIDATION_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {MANUAL_APPLY_DESIGN_VALIDATION_SCHEMA_VERSION!r}")
        if self.runtime_mutation_allowed is not False:
            raise ValueError("manual apply design validation must not allow runtime mutation")
        if self.operator_approval_required is not True:
            raise ValueError("manual apply design validation must require operator approval")


@dataclass(frozen=True, slots=True)
class ManualApplyDesignReview:
    """Pending research-only review for a manual apply design package."""

    review_id: str
    design_id: str
    source_preapply_review_id: str
    source_preapply_package_id: str
    candidate_id: str
    recommendation_id: str
    observation_id: str
    experiment_id: str
    candidate_type: str
    design_status: str
    status: str = "review_pending"
    design_ref: str = MANUAL_APPLY_DESIGN_PACKAGE_REF
    validation_ref: str | None = None
    validation_passed: bool | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = MANUAL_APPLY_DESIGN_REVIEW_SCHEMA_VERSION
    runtime_mutation_allowed: bool = False
    operator_approval_required: bool = True
    recommended_next_step: str = "collect_manual_apply_design_review_decision"
    notes: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_safe_identifier(self.review_id, "review_id")
        _require_safe_identifier(self.design_id, "design_id")
        _require_safe_identifier(self.source_preapply_review_id, "source_preapply_review_id")
        _require_safe_identifier(self.source_preapply_package_id, "source_preapply_package_id")
        _require_safe_identifier(self.candidate_id, "candidate_id")
        _require_safe_identifier(self.recommendation_id, "recommendation_id")
        _require_safe_identifier(self.observation_id, "observation_id")
        _require_safe_identifier(self.experiment_id, "experiment_id")
        candidate_type = _require_candidate_type(self.candidate_type)
        object.__setattr__(self, "candidate_type", candidate_type)
        if self.design_status not in ALLOWED_MANUAL_APPLY_DESIGN_STATUSES:
            allowed = ", ".join(sorted(ALLOWED_MANUAL_APPLY_DESIGN_STATUSES))
            raise ValueError(f"design_status must be one of: {allowed}")
        if self.status not in ALLOWED_MANUAL_APPLY_DESIGN_REVIEW_STATUSES:
            allowed = ", ".join(sorted(ALLOWED_MANUAL_APPLY_DESIGN_REVIEW_STATUSES))
            raise ValueError(f"manual apply design review status must be one of: {allowed}")
        object.__setattr__(self, "design_ref", _require_relative_ref(self.design_ref, "design_ref"))
        if self.validation_ref is not None:
            object.__setattr__(
                self,
                "validation_ref",
                _require_relative_ref(self.validation_ref, "validation_ref"),
            )
        if self.validation_passed is not None and not isinstance(self.validation_passed, bool):
            raise ValueError("validation_passed must be a bool when provided")
        _require_timezone_aware_datetime(self.created_at, "created_at")
        if self.schema_version != MANUAL_APPLY_DESIGN_REVIEW_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {MANUAL_APPLY_DESIGN_REVIEW_SCHEMA_VERSION!r}")
        if self.runtime_mutation_allowed is not False:
            raise ValueError("manual apply design review must not allow runtime mutation")
        if self.operator_approval_required is not True:
            raise ValueError("manual apply design review must require operator approval")
        next_step = _require_non_empty_text(self.recommended_next_step, "recommended_next_step")
        _reject_promotion_text(next_step, "recommended_next_step")
        object.__setattr__(self, "recommended_next_step", next_step)
        object.__setattr__(self, "notes", _normalize_text_sequence(self.notes, "notes", allow_empty=True))


@dataclass(frozen=True, slots=True)
class ManualApplyDesignReviewDecision:
    """Research-only decision for a manual apply design review."""

    review_id: str
    design_id: str
    candidate_id: str
    candidate_type: str
    decision: str
    rationale: str
    reviewed_by: str
    required_revisions: Sequence[str] = field(default_factory=tuple)
    reviewed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    design_ref: str = MANUAL_APPLY_DESIGN_PACKAGE_REF
    review_ref: str = MANUAL_APPLY_DESIGN_REVIEW_REF
    validation_ref: str | None = None
    validation_passed: bool | None = None
    schema_version: str = MANUAL_APPLY_DESIGN_REVIEW_SCHEMA_VERSION
    runtime_mutation_allowed: bool = False
    operator_approval_required: bool = True
    recommended_next_step: str | None = None

    def __post_init__(self) -> None:
        _require_safe_identifier(self.review_id, "review_id")
        _require_safe_identifier(self.design_id, "design_id")
        _require_safe_identifier(self.candidate_id, "candidate_id")
        candidate_type = _require_candidate_type(self.candidate_type)
        object.__setattr__(self, "candidate_type", candidate_type)
        if self.decision not in ALLOWED_MANUAL_APPLY_DESIGN_REVIEW_DECISIONS:
            allowed = ", ".join(sorted(ALLOWED_MANUAL_APPLY_DESIGN_REVIEW_DECISIONS))
            raise ValueError(f"manual apply design review decision must be one of: {allowed}")
        rationale = _require_non_empty_text(self.rationale, "rationale")
        _reject_promotion_text(rationale, "rationale")
        object.__setattr__(self, "rationale", rationale)
        reviewed_by = _require_non_empty_text(self.reviewed_by, "reviewed_by")
        _reject_promotion_text(reviewed_by, "reviewed_by")
        object.__setattr__(self, "reviewed_by", reviewed_by)
        revisions = _normalize_text_sequence(
            self.required_revisions,
            "required_revisions",
            allow_empty=True,
        )
        if self.decision == "needs_design_revision" and not revisions:
            raise ValueError("needs_design_revision requires required_revisions")
        object.__setattr__(self, "required_revisions", revisions)
        _require_timezone_aware_datetime(self.reviewed_at, "reviewed_at")
        object.__setattr__(self, "design_ref", _require_relative_ref(self.design_ref, "design_ref"))
        object.__setattr__(self, "review_ref", _require_relative_ref(self.review_ref, "review_ref"))
        if self.validation_ref is not None:
            object.__setattr__(
                self,
                "validation_ref",
                _require_relative_ref(self.validation_ref, "validation_ref"),
            )
        if self.validation_passed is not None and not isinstance(self.validation_passed, bool):
            raise ValueError("validation_passed must be a bool when provided")
        if self.decision == "design_ready_for_dry_run_planning":
            if self.validation_ref is None:
                raise ValueError("dry-run planning readiness requires validation report")
            if self.validation_passed is not True:
                raise ValueError("dry-run planning readiness requires passing validation")
        if self.schema_version != MANUAL_APPLY_DESIGN_REVIEW_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {MANUAL_APPLY_DESIGN_REVIEW_SCHEMA_VERSION!r}")
        if self.runtime_mutation_allowed is not False:
            raise ValueError("manual apply design review decision must not allow runtime mutation")
        if self.operator_approval_required is not True:
            raise ValueError("manual apply design review decision must require operator approval")
        next_step = self.recommended_next_step or _default_design_review_next_step(self.decision)
        next_step = _require_non_empty_text(next_step, "recommended_next_step")
        _reject_promotion_text(next_step, "recommended_next_step")
        object.__setattr__(self, "recommended_next_step", next_step)


class ManualApplyDesignRecorder:
    """Persist manual apply design packages under a research-only artifact root."""

    def __init__(
        self,
        root: str | Path = Path("artifacts")
        / "research"
        / "research_factory"
        / "manual_apply_designs",
        *,
        code_version: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = _require_research_manual_design_root(root)
        self.code_version = code_version
        self._clock = clock or _utc_now

    def record_package(self, package: ManualApplyDesignPackage) -> dict[str, Any]:
        """Write a manual apply design package and manifest."""
        if not isinstance(package, ManualApplyDesignPackage):
            raise ValueError("package must be a ManualApplyDesignPackage")
        design_dir = self._design_dir(package.design_id)
        if design_dir.exists():
            raise ValueError(f"manual apply design package {package.design_id!r} already exists")
        design_dir.mkdir(parents=True)
        _write_json_atomic(design_dir / MANUAL_APPLY_DESIGN_PACKAGE_REF, _to_jsonable(package))
        manifest = build_artifact_manifest(
            artifact_id=package.design_id,
            artifact_type="manual_apply_design",
            status="succeeded",
            started_at=package.created_at,
            finished_at=self._now(),
            input_refs={
                "source_preapply_review_id": package.source_preapply_review_id,
                "source_preapply_package_id": package.source_preapply_package_id,
                "candidate_id": package.candidate_id,
                "recommendation_id": package.recommendation_id,
                "observation_id": package.observation_id,
                "experiment_id": package.experiment_id,
                "candidate_type": package.candidate_type,
                "design_status": package.status,
            },
            output_refs={"manual_apply_design_package": MANUAL_APPLY_DESIGN_PACKAGE_REF},
            code_version=self.code_version,
            notes="research-only manual apply design package; does not authorize runtime mutation",
        )
        write_artifact_manifest_atomic(design_dir / MANUAL_APPLY_DESIGN_MANIFEST_REF, manifest)
        return manifest

    def _design_dir(self, design_id: str) -> Path:
        return self.root / _require_safe_identifier(design_id, "design_id")

    def _now(self) -> datetime:
        return self._clock()


def build_manual_apply_design_package(
    *,
    preapply_package: PreApplyEvidencePackage,
    preapply_review: PreApplyReview,
    preapply_review_decision: PreApplyReviewDecision,
    candidate_type: str,
    proposed_change_summary: str,
    parameter_or_config_delta: Mapping[str, Any],
    affected_runtime_components: Sequence[str],
    required_risk_guards: Sequence[str],
    required_dry_run_checks: Sequence[str],
    rollback_plan_ref: str,
    evidence_refs: Mapping[str, str] | None = None,
    design_id: str | None = None,
    status: str = "design_draft",
    created_at: datetime | None = None,
    notes: Sequence[str] = (),
) -> ManualApplyDesignPackage:
    """Build a research-only design draft from an approved pre-apply review decision."""
    if not isinstance(preapply_package, PreApplyEvidencePackage):
        raise ValueError("preapply_package must be a PreApplyEvidencePackage")
    if not isinstance(preapply_review, PreApplyReview):
        raise ValueError("preapply_review must be a PreApplyReview")
    if not isinstance(preapply_review_decision, PreApplyReviewDecision):
        raise ValueError("preapply_review_decision must be a PreApplyReviewDecision")
    _require_review_matches_package(preapply_review, preapply_package)
    _require_decision_matches_review(preapply_review_decision, preapply_review)
    if preapply_package.status != "preapply_ready":
        raise ValueError("manual apply design package requires a preapply_ready package")
    if preapply_review_decision.decision != "review_approved_for_manual_apply_design":
        raise ValueError("manual apply design package requires approved manual design review decision")
    if preapply_review.reference_integrity_ref is None:
        raise ValueError("manual apply design package requires reference integrity report")
    if preapply_review.reference_integrity_passed is not True:
        raise ValueError("manual apply design package requires passing reference integrity")

    normalized_rollback_ref = _require_relative_ref(rollback_plan_ref, "rollback_plan_ref")
    resolved_evidence_refs = _default_design_evidence_refs(
        rollback_plan_ref=normalized_rollback_ref,
        evidence_refs=evidence_refs,
    )
    return ManualApplyDesignPackage(
        design_id=design_id or f"manual_design_{preapply_review.review_id}",
        source_preapply_review_id=preapply_review.review_id,
        source_preapply_package_id=preapply_package.package_id,
        candidate_id=preapply_package.candidate_id,
        recommendation_id=preapply_package.recommendation_id,
        observation_id=preapply_package.observation_id,
        experiment_id=preapply_package.experiment_id,
        candidate_type=candidate_type,
        status=status,
        proposed_change_summary=proposed_change_summary,
        parameter_or_config_delta=parameter_or_config_delta,
        affected_runtime_components=affected_runtime_components,
        required_risk_guards=required_risk_guards,
        required_dry_run_checks=required_dry_run_checks,
        rollback_plan_ref=normalized_rollback_ref,
        evidence_refs=resolved_evidence_refs,
        created_at=created_at or datetime.now(UTC),
        notes=notes
        or (
            "manual apply design package is evidence-only",
            "separate operator and governance approval is required before any runtime change",
        ),
    )


class ManualApplyDesignReviewRecorder:
    """Persist manual apply design review artifacts under research-only root."""

    def __init__(
        self,
        root: str | Path = Path("artifacts")
        / "research"
        / "research_factory"
        / "manual_apply_design_reviews",
        *,
        code_version: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = _require_research_manual_design_root(root)
        self.code_version = code_version
        self._clock = clock or _utc_now

    def start_review(
        self,
        design: ManualApplyDesignPackage,
        *,
        validation_report: ManualApplyDesignValidationReport | None = None,
        validation_ref: str | None = None,
        review_id: str | None = None,
        notes: Sequence[str] = (),
    ) -> ManualApplyDesignReview:
        """Create a pending research-only review for a manual apply design package."""
        if not isinstance(design, ManualApplyDesignPackage):
            raise ValueError("design must be a ManualApplyDesignPackage")
        if validation_report is not None:
            _require_validation_matches_design(validation_report, design)
            if validation_ref is None:
                validation_ref = MANUAL_APPLY_DESIGN_VALIDATION_REF
        review = build_manual_apply_design_review(
            design,
            validation_report=validation_report,
            validation_ref=validation_ref,
            review_id=review_id,
            created_at=self._now(),
            notes=notes,
        )
        review_dir = self._review_dir(review.review_id)
        if review_dir.exists():
            raise ValueError(f"manual apply design review {review.review_id!r} already exists")
        review_dir.mkdir(parents=True)
        _write_json_atomic(review_dir / MANUAL_APPLY_DESIGN_REVIEW_REF, _to_jsonable(review))
        output_refs = {"manual_apply_design_review": MANUAL_APPLY_DESIGN_REVIEW_REF}
        if validation_report is not None:
            output_ref = _require_plain_output_ref(
                validation_ref or MANUAL_APPLY_DESIGN_VALIDATION_REF,
                "validation_ref",
            )
            _write_json_atomic(review_dir / output_ref, _to_jsonable(validation_report))
            output_refs["manual_apply_design_validation_report"] = output_ref
        manifest = build_artifact_manifest(
            artifact_id=review.review_id,
            artifact_type="manual_apply_design_review",
            status="running",
            started_at=review.created_at,
            input_refs={
                "design_id": review.design_id,
                "source_preapply_review_id": review.source_preapply_review_id,
                "source_preapply_package_id": review.source_preapply_package_id,
                "candidate_id": review.candidate_id,
                "candidate_type": review.candidate_type,
                "design_status": review.design_status,
                "design_ref": review.design_ref,
                "validation_ref": review.validation_ref,
                "validation_passed": review.validation_passed,
            },
            output_refs=output_refs,
            code_version=self.code_version,
            notes="research-only manual apply design review",
        )
        write_artifact_manifest_atomic(review_dir / MANUAL_APPLY_DESIGN_REVIEW_MANIFEST_REF, manifest)
        return review

    def record_decision(self, decision: ManualApplyDesignReviewDecision) -> dict[str, Any]:
        """Record a terminal decision for a pending manual apply design review."""
        if not isinstance(decision, ManualApplyDesignReviewDecision):
            raise ValueError("decision must be a ManualApplyDesignReviewDecision")
        review_dir = self._review_dir(decision.review_id)
        manifest_path = review_dir / MANUAL_APPLY_DESIGN_REVIEW_MANIFEST_REF
        if not manifest_path.exists():
            raise ValueError(f"manual apply design review {decision.review_id!r} does not exist")
        manifest = _load_json_mapping(manifest_path, "manual_apply_design_review_manifest")
        if manifest["status"] != "running":
            raise ValueError(f"manual apply design review {decision.review_id!r} is already terminal")
        stored_review = _load_json_mapping(review_dir / MANUAL_APPLY_DESIGN_REVIEW_REF, "manual_apply_design_review")
        _require_design_review_decision_matches_review(decision, stored_review)

        _write_json_atomic(review_dir / MANUAL_APPLY_DESIGN_REVIEW_DECISION_REF, _to_jsonable(decision))
        output_refs = dict(manifest["output_refs"])
        output_refs["manual_apply_design_review_decision"] = MANUAL_APPLY_DESIGN_REVIEW_DECISION_REF
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


def validate_manual_apply_design_domain(
    design: ManualApplyDesignPackage,
    *,
    evaluated_at: datetime | None = None,
) -> ManualApplyDesignValidationReport:
    """Validate a manual apply design draft against candidate-type domain rules."""
    if not isinstance(design, ManualApplyDesignPackage):
        raise ValueError("design must be a ManualApplyDesignPackage")
    failures: list[str] = []
    warnings: list[str] = []
    if design.runtime_mutation_allowed is not False:
        failures.append("runtime_mutation_allowed must be false")
    if design.operator_approval_required is not True:
        failures.append("operator_approval_required must be true")
    if design.status == "design_rejected":
        failures.append("design_rejected packages cannot enter design review")
    if not design.rollback_plan_ref:
        failures.append("rollback_plan_ref is required")
    if not design.required_risk_guards:
        failures.append("required_risk_guards must not be empty")
    if not design.required_dry_run_checks:
        failures.append("required_dry_run_checks must not be empty")

    candidate_type = _require_candidate_type(design.candidate_type)
    delta = design.parameter_or_config_delta
    evidence_refs = design.evidence_refs
    dry_run_text = " ".join(design.required_dry_run_checks).lower()
    guard_text = " ".join(design.required_risk_guards).lower()
    component_text = " ".join(design.affected_runtime_components).lower()

    if candidate_type == "factor":
        if not isinstance(delta.get("factor_expression"), str) or not delta["factor_expression"].strip():
            failures.append("factor design requires parameter_or_config_delta.factor_expression")
        if "research" not in component_text:
            failures.append("factor design must target a research-scoped component")
        if "paper" not in dry_run_text and "replay" not in dry_run_text:
            failures.append("factor design requires a paper or replay dry-run check")
    elif candidate_type == "parameter":
        changes = delta.get("parameter_changes")
        if not isinstance(changes, Mapping) or not changes:
            failures.append("parameter design requires parameter_or_config_delta.parameter_changes")
        else:
            for name, change in changes.items():
                parameter_name = _require_non_empty_text(name, "parameter_changes key")
                _reject_promotion_text(parameter_name, "parameter_changes key")
                if not isinstance(change, Mapping):
                    failures.append(f"parameter change {parameter_name!r} must be a mapping")
                    continue
                if "proposed_value" not in change:
                    failures.append(f"parameter change {parameter_name!r} requires proposed_value")
                if not isinstance(change.get("rollback_old_value_ref"), str) or not change[
                    "rollback_old_value_ref"
                ].strip():
                    failures.append(f"parameter change {parameter_name!r} requires rollback_old_value_ref")
    elif candidate_type == "risk_budget":
        if "exposure" not in guard_text:
            failures.append("risk_budget design requires exposure risk guard")
        if "drawdown" not in guard_text:
            failures.append("risk_budget design requires drawdown risk guard")
        if "kill" not in guard_text:
            failures.append("risk_budget design requires kill switch guard")
        if "max_exposure" not in delta and "max_exposure_multiplier" not in delta:
            failures.append("risk_budget design requires max_exposure or max_exposure_multiplier")
    elif candidate_type == "execution_policy":
        for required_ref in (
            "execution_evidence",
            "slippage_evidence",
            "fillability_evidence",
        ):
            if required_ref not in evidence_refs:
                failures.append(f"execution_policy design missing evidence ref: {required_ref}")
        if "paper" not in dry_run_text:
            failures.append("execution_policy design requires paper-only validation")
    elif candidate_type == "model":
        if "model_artifact_ref" not in delta:
            failures.append("model design requires model_artifact_ref")
        if "inference" not in dry_run_text and "paper" not in dry_run_text:
            failures.append("model design requires inference or paper dry-run check")
    elif candidate_type == "regime_classifier":
        if "regime_definition_ref" not in delta:
            failures.append("regime_classifier design requires regime_definition_ref")
        if "regime" not in dry_run_text:
            failures.append("regime_classifier design requires regime dry-run check")

    return ManualApplyDesignValidationReport(
        design_id=design.design_id,
        candidate_id=design.candidate_id,
        candidate_type=design.candidate_type,
        passed=not failures,
        failures=tuple(failures),
        warnings=tuple(warnings),
        evaluated_at=evaluated_at or datetime.now(UTC),
    )


def build_manual_apply_design_review(
    design: ManualApplyDesignPackage,
    *,
    validation_report: ManualApplyDesignValidationReport | None = None,
    validation_ref: str | None = None,
    review_id: str | None = None,
    created_at: datetime | None = None,
    notes: Sequence[str] = (),
) -> ManualApplyDesignReview:
    """Build a pending research-only review from a manual apply design package."""
    if not isinstance(design, ManualApplyDesignPackage):
        raise ValueError("design must be a ManualApplyDesignPackage")
    if validation_report is not None:
        _require_validation_matches_design(validation_report, design)
        if validation_ref is None:
            validation_ref = MANUAL_APPLY_DESIGN_VALIDATION_REF
    return ManualApplyDesignReview(
        review_id=review_id or f"review_{design.design_id}",
        design_id=design.design_id,
        source_preapply_review_id=design.source_preapply_review_id,
        source_preapply_package_id=design.source_preapply_package_id,
        candidate_id=design.candidate_id,
        recommendation_id=design.recommendation_id,
        observation_id=design.observation_id,
        experiment_id=design.experiment_id,
        candidate_type=design.candidate_type,
        design_status=design.status,
        validation_ref=validation_ref,
        validation_passed=validation_report.passed if validation_report is not None else None,
        created_at=created_at or datetime.now(UTC),
        notes=notes,
    )


def build_manual_apply_design_review_decision(
    *,
    review: ManualApplyDesignReview,
    design: ManualApplyDesignPackage,
    validation_report: ManualApplyDesignValidationReport | None,
    decision: str,
    rationale: str,
    reviewed_by: str,
    required_revisions: Sequence[str] = (),
    reviewed_at: datetime | None = None,
) -> ManualApplyDesignReviewDecision:
    """Build a research-only manual apply design review decision."""
    if not isinstance(review, ManualApplyDesignReview):
        raise ValueError("review must be a ManualApplyDesignReview")
    if not isinstance(design, ManualApplyDesignPackage):
        raise ValueError("design must be a ManualApplyDesignPackage")
    _require_design_review_matches_design(review, design)
    if validation_report is not None:
        _require_validation_matches_design(validation_report, design)
        if review.validation_ref is None:
            raise ValueError("manual apply design review decision requires validation_ref")
        if review.validation_passed != validation_report.passed:
            raise ValueError("manual apply design review validation_passed must match validation report")
    if decision == "design_ready_for_dry_run_planning":
        if validation_report is None:
            raise ValueError("dry-run planning readiness requires validation report")
        if validation_report.passed is not True:
            raise ValueError("dry-run planning readiness requires passing validation")
        if design.status == "design_rejected":
            raise ValueError("dry-run planning readiness requires a non-rejected design")
    return ManualApplyDesignReviewDecision(
        review_id=review.review_id,
        design_id=design.design_id,
        candidate_id=design.candidate_id,
        candidate_type=design.candidate_type,
        decision=decision,
        rationale=rationale,
        reviewed_by=reviewed_by,
        required_revisions=required_revisions,
        reviewed_at=reviewed_at or datetime.now(UTC),
        design_ref=review.design_ref,
        review_ref=MANUAL_APPLY_DESIGN_REVIEW_REF,
        validation_ref=review.validation_ref,
        validation_passed=validation_report.passed if validation_report is not None else review.validation_passed,
    )


def _default_design_evidence_refs(
    *,
    rollback_plan_ref: str,
    evidence_refs: Mapping[str, str] | None,
) -> Mapping[str, str]:
    if evidence_refs is not None:
        return evidence_refs
    return {
        "preapply_evidence_package": PREAPPLY_PACKAGE_REF,
        "preapply_review": PREAPPLY_REVIEW_REF,
        "preapply_review_decision": PREAPPLY_REVIEW_DECISION_REF,
        "rollback_plan": rollback_plan_ref,
    }


def _require_review_matches_package(review: PreApplyReview, package: PreApplyEvidencePackage) -> None:
    if review.package_id != package.package_id:
        raise ValueError("preapply review package_id must match package")
    if review.candidate_id != package.candidate_id:
        raise ValueError("preapply review candidate_id must match package")
    if review.recommendation_id != package.recommendation_id:
        raise ValueError("preapply review recommendation_id must match package")
    if review.observation_id != package.observation_id:
        raise ValueError("preapply review observation_id must match package")
    if review.experiment_id != package.experiment_id:
        raise ValueError("preapply review experiment_id must match package")
    if review.package_status != package.status:
        raise ValueError("preapply review package_status must match package status")


def _require_decision_matches_review(decision: PreApplyReviewDecision, review: PreApplyReview) -> None:
    if decision.review_id != review.review_id:
        raise ValueError("preapply review decision review_id must match review")
    if decision.package_id != review.package_id:
        raise ValueError("preapply review decision package_id must match review")
    if decision.candidate_id != review.candidate_id:
        raise ValueError("preapply review decision candidate_id must match review")
    if decision.recommendation_id != review.recommendation_id:
        raise ValueError("preapply review decision recommendation_id must match review")
    if decision.observation_id != review.observation_id:
        raise ValueError("preapply review decision observation_id must match review")
    if decision.experiment_id != review.experiment_id:
        raise ValueError("preapply review decision experiment_id must match review")
    if decision.package_ref != review.package_ref:
        raise ValueError("preapply review decision package_ref must match review")
    if decision.review_ref != PREAPPLY_REVIEW_REF:
        raise ValueError("preapply review decision review_ref must point to preapply review")


def _require_validation_matches_design(
    validation: ManualApplyDesignValidationReport,
    design: ManualApplyDesignPackage,
) -> None:
    if validation.design_id != design.design_id:
        raise ValueError("manual apply design validation design_id must match design")
    if validation.candidate_id != design.candidate_id:
        raise ValueError("manual apply design validation candidate_id must match design")
    if validation.candidate_type != design.candidate_type:
        raise ValueError("manual apply design validation candidate_type must match design")


def _require_design_review_matches_design(
    review: ManualApplyDesignReview,
    design: ManualApplyDesignPackage,
) -> None:
    if review.design_id != design.design_id:
        raise ValueError("manual apply design review design_id must match design")
    if review.source_preapply_review_id != design.source_preapply_review_id:
        raise ValueError("manual apply design review source_preapply_review_id must match design")
    if review.source_preapply_package_id != design.source_preapply_package_id:
        raise ValueError("manual apply design review source_preapply_package_id must match design")
    if review.candidate_id != design.candidate_id:
        raise ValueError("manual apply design review candidate_id must match design")
    if review.recommendation_id != design.recommendation_id:
        raise ValueError("manual apply design review recommendation_id must match design")
    if review.observation_id != design.observation_id:
        raise ValueError("manual apply design review observation_id must match design")
    if review.experiment_id != design.experiment_id:
        raise ValueError("manual apply design review experiment_id must match design")
    if review.candidate_type != design.candidate_type:
        raise ValueError("manual apply design review candidate_type must match design")
    if review.design_status != design.status:
        raise ValueError("manual apply design review design_status must match design")


def _require_design_review_decision_matches_review(
    decision: ManualApplyDesignReviewDecision,
    review_payload: Mapping[str, Any],
) -> None:
    fields_to_match = (
        "review_id",
        "design_id",
        "candidate_id",
        "candidate_type",
        "design_ref",
        "validation_ref",
        "validation_passed",
    )
    for field_name in fields_to_match:
        if getattr(decision, field_name) != review_payload.get(field_name):
            raise ValueError(f"manual apply design review decision {field_name} must match review")


def _default_design_review_next_step(decision: str) -> str:
    if decision == "design_ready_for_dry_run_planning":
        return "prepare_research_only_dry_run_plan"
    if decision == "needs_design_revision":
        return "revise_manual_apply_design_package"
    return "archive_manual_apply_design_rejection"


def _load_json_mapping(path: Path, field_name: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"{field_name} must be a JSON object")
    return payload


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
        return {item.name: _to_jsonable(getattr(value, item.name)) for item in fields(value)}
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
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("float values must be finite")
        return value
    if isinstance(value, int | str | bool) or value is None:
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
        _reject_promotion_text(ref_name, f"{field_name} key")
        normalized[ref_name] = _require_relative_ref(value, f"{field_name}.{ref_name}")
    for required_ref in required_names:
        if required_ref not in normalized:
            raise ValueError(f"{field_name} missing required ref: {required_ref}")
    return dict(sorted(normalized.items()))


def _normalize_text_sequence(
    values: Sequence[str],
    field_name: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if isinstance(values, str | bytes | bytearray) or not isinstance(values, Sequence):
        raise ValueError(f"{field_name} must be a sequence of strings")
    normalized = tuple(_require_non_empty_text(value, field_name) for value in values)
    if not allow_empty and not normalized:
        raise ValueError(f"{field_name} must not be empty")
    for value in normalized:
        _reject_promotion_text(value, field_name)
    return normalized


def _normalize_json_mapping(value: Mapping[str, Any], field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{field_name} must be a non-empty mapping")
    normalized = _normalize_json_value(value, field_name)
    if not isinstance(normalized, dict):
        raise ValueError(f"{field_name} must be a mapping")
    return normalized


def _normalize_json_value(value: Any, field_name: str) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = _require_non_empty_text(key, f"{field_name} key")
            _reject_promotion_text(key_text, f"{field_name} key")
            normalized[key_text] = _normalize_json_value(item, f"{field_name}.{key_text}")
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_normalize_json_value(item, field_name) for item in value]
    if isinstance(value, str):
        _reject_promotion_text(value, field_name)
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} must contain only finite numbers")
        return value
    if isinstance(value, int | bool) or value is None:
        return value
    raise TypeError(f"{field_name} contains unsupported JSON value: {type(value).__name__}")


def _require_relative_ref(value: Any, field_name: str) -> str:
    ref = _require_non_empty_text(value, field_name)
    ref = normalize_relative_artifact_path(ref)
    if ref.startswith("~"):
        raise ValueError(f"{field_name} must be a relative artifact ref")
    posix_path = PurePosixPath(ref)
    windows_path = PureWindowsPath(ref)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError(f"{field_name} must be a relative artifact ref")
    _reject_promotion_text(ref, field_name)
    return ref


def _require_plain_output_ref(value: Any, field_name: str) -> str:
    ref = _require_relative_ref(value, field_name)
    if "/" in ref or "\\" in ref or ref in {".", ".."}:
        raise ValueError(f"{field_name} must be a plain relative filename")
    return ref


def _require_safe_identifier(value: Any, field_name: str) -> str:
    value = _require_non_empty_text(value, field_name)
    if "/" in value or "\\" in value or value in {".", ".."} or ".." in value:
        raise ValueError(f"{field_name} must not contain path traversal or separators")
    _reject_promotion_text(value, field_name)
    return value


def _require_candidate_type(value: Any) -> str:
    candidate_type = _require_non_empty_text(value, "candidate_type")
    _reject_promotion_text(candidate_type, "candidate_type")
    if candidate_type not in ALLOWED_MANUAL_APPLY_CANDIDATE_TYPES:
        allowed = ", ".join(sorted(ALLOWED_MANUAL_APPLY_CANDIDATE_TYPES))
        raise ValueError(f"candidate_type must be one of: {allowed}")
    return candidate_type


def _require_non_empty_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_timezone_aware_datetime(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")


def _reject_promotion_text(value: str, field_name: str) -> None:
    lowered = value.lower()
    for term in DESIGN_PROMOTION_TERMS:
        if term in lowered:
            raise ValueError(f"{field_name} must not encode runtime promotion term: {term}")


def _require_research_manual_design_root(value: str | Path) -> Path:
    path = Path(value)
    parts = path.parts
    if ".." in parts:
        raise ValueError("manual apply design root must not contain path traversal")
    has_research_artifact_root = any(
        parts[index] == "artifacts" and parts[index + 1] == "research"
        for index in range(len(parts) - 1)
    )
    if not has_research_artifact_root:
        raise ValueError("manual apply design root must be under artifacts/research")
    return path


def _utc_now() -> datetime:
    return datetime.now(UTC)
