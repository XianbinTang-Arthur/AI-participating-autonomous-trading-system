"""Research-only dry-run planning artifacts."""

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
from aats.data_platform.research_factory.manual_apply_design import (
    ALLOWED_MANUAL_APPLY_CANDIDATE_TYPES,
    DESIGN_PROMOTION_TERMS,
    ManualApplyDesignPackage,
    ManualApplyDesignReview,
    ManualApplyDesignReviewDecision,
    ManualApplyDesignValidationReport,
)

DRY_RUN_PLAN_SCHEMA_VERSION = "research_dry_run_plan_v1"
DRY_RUN_PLAN_VALIDATION_SCHEMA_VERSION = "research_dry_run_plan_validation_v1"
DRY_RUN_PLAN_REVIEW_SCHEMA_VERSION = "research_dry_run_plan_review_v1"
DRY_RUN_PLAN_PACKAGE_REF = "dry_run_plan_package.json"
DRY_RUN_PLAN_VALIDATION_REF = "dry_run_plan_validation_report.json"
DRY_RUN_PLAN_MANIFEST_REF = "dry_run_plan_manifest.json"

ALLOWED_DRY_RUN_PLAN_STATUSES = frozenset(
    {"dry_run_plan_draft", "dry_run_plan_ready_for_review", "dry_run_plan_rejected"}
)
ALLOWED_DRY_RUN_TARGET_ENVIRONMENTS = frozenset({"paper", "shadow", "replay"})
ALLOWED_DRY_RUN_PLAN_REVIEW_DECISIONS = frozenset(
    {"dry_run_plan_ready_for_review", "dry_run_plan_rejected", "needs_dry_run_plan_revision"}
)


@dataclass(frozen=True, slots=True)
class DryRunPlanPackage:
    """Research-only plan for a future dry-run. This does not execute dry-run steps."""

    dry_run_plan_id: str
    source_manual_apply_design_review_id: str
    source_manual_apply_design_id: str
    candidate_id: str
    candidate_type: str
    target_environment: str
    dry_run_scope: str
    expected_runtime_components: Sequence[str]
    required_input_artifacts: Mapping[str, str]
    required_risk_guards: Sequence[str]
    rollback_plan_ref: str
    success_criteria: Sequence[str]
    abort_conditions: Sequence[str]
    status: str = "dry_run_plan_draft"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = DRY_RUN_PLAN_SCHEMA_VERSION
    runtime_mutation_allowed: bool = False
    operator_approval_required: bool = True
    recommended_next_step: str = "submit_dry_run_plan_for_operator_review"
    notes: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_safe_identifier(self.dry_run_plan_id, "dry_run_plan_id")
        _require_safe_identifier(
            self.source_manual_apply_design_review_id,
            "source_manual_apply_design_review_id",
        )
        _require_safe_identifier(self.source_manual_apply_design_id, "source_manual_apply_design_id")
        _require_safe_identifier(self.candidate_id, "candidate_id")
        candidate_type = _require_candidate_type(self.candidate_type)
        object.__setattr__(self, "candidate_type", candidate_type)
        if self.target_environment not in ALLOWED_DRY_RUN_TARGET_ENVIRONMENTS:
            allowed = ", ".join(sorted(ALLOWED_DRY_RUN_TARGET_ENVIRONMENTS))
            raise ValueError(f"target_environment must be one of: {allowed}")
        dry_run_scope = _require_non_empty_text(self.dry_run_scope, "dry_run_scope")
        _reject_promotion_text(dry_run_scope, "dry_run_scope")
        object.__setattr__(self, "dry_run_scope", dry_run_scope)
        object.__setattr__(
            self,
            "expected_runtime_components",
            _normalize_text_sequence(
                self.expected_runtime_components,
                "expected_runtime_components",
            ),
        )
        object.__setattr__(
            self,
            "required_input_artifacts",
            _normalize_refs(self.required_input_artifacts, "required_input_artifacts"),
        )
        object.__setattr__(
            self,
            "required_risk_guards",
            _normalize_text_sequence(self.required_risk_guards, "required_risk_guards"),
        )
        object.__setattr__(
            self,
            "rollback_plan_ref",
            _require_relative_ref(self.rollback_plan_ref, "rollback_plan_ref"),
        )
        object.__setattr__(
            self,
            "success_criteria",
            _normalize_text_sequence(self.success_criteria, "success_criteria", allow_empty=True),
        )
        object.__setattr__(
            self,
            "abort_conditions",
            _normalize_text_sequence(self.abort_conditions, "abort_conditions", allow_empty=True),
        )
        if self.status not in ALLOWED_DRY_RUN_PLAN_STATUSES:
            allowed = ", ".join(sorted(ALLOWED_DRY_RUN_PLAN_STATUSES))
            raise ValueError(f"dry-run plan status must be one of: {allowed}")
        _require_timezone_aware_datetime(self.created_at, "created_at")
        if self.schema_version != DRY_RUN_PLAN_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {DRY_RUN_PLAN_SCHEMA_VERSION!r}")
        if self.runtime_mutation_allowed is not False:
            raise ValueError("dry-run plan must not allow runtime mutation")
        if self.operator_approval_required is not True:
            raise ValueError("dry-run plan must require operator approval")
        next_step = _require_non_empty_text(self.recommended_next_step, "recommended_next_step")
        _reject_promotion_text(next_step, "recommended_next_step")
        object.__setattr__(self, "recommended_next_step", next_step)
        object.__setattr__(
            self,
            "notes",
            _normalize_text_sequence(self.notes, "notes", allow_empty=True),
        )


@dataclass(frozen=True, slots=True)
class DryRunPlanValidationReport:
    """Validation report for a dry-run plan package."""

    dry_run_plan_id: str
    candidate_id: str
    candidate_type: str
    passed: bool
    failures: Sequence[str] = field(default_factory=tuple)
    warnings: Sequence[str] = field(default_factory=tuple)
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = DRY_RUN_PLAN_VALIDATION_SCHEMA_VERSION
    runtime_mutation_allowed: bool = False
    operator_approval_required: bool = True

    def __post_init__(self) -> None:
        _require_safe_identifier(self.dry_run_plan_id, "dry_run_plan_id")
        _require_safe_identifier(self.candidate_id, "candidate_id")
        candidate_type = _require_candidate_type(self.candidate_type)
        object.__setattr__(self, "candidate_type", candidate_type)
        if not isinstance(self.passed, bool):
            raise ValueError("passed must be a bool")
        failures = _normalize_text_sequence(self.failures, "failures", allow_empty=True)
        warnings = _normalize_text_sequence(self.warnings, "warnings", allow_empty=True)
        if self.passed and failures:
            raise ValueError("passing dry-run plan validation must not contain failures")
        if not self.passed and not failures:
            raise ValueError("failing dry-run plan validation must include failures")
        object.__setattr__(self, "failures", failures)
        object.__setattr__(self, "warnings", warnings)
        _require_timezone_aware_datetime(self.evaluated_at, "evaluated_at")
        if self.schema_version != DRY_RUN_PLAN_VALIDATION_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {DRY_RUN_PLAN_VALIDATION_SCHEMA_VERSION!r}")
        if self.runtime_mutation_allowed is not False:
            raise ValueError("dry-run plan validation must not allow runtime mutation")
        if self.operator_approval_required is not True:
            raise ValueError("dry-run plan validation must require operator approval")


@dataclass(frozen=True, slots=True)
class DryRunPlanReviewDecision:
    """Research-only decision for whether a dry-run plan is ready for review."""

    dry_run_plan_id: str
    candidate_id: str
    candidate_type: str
    decision: str
    rationale: str
    reviewed_by: str
    required_revisions: Sequence[str] = field(default_factory=tuple)
    reviewed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    validation_ref: str | None = None
    validation_passed: bool | None = None
    schema_version: str = DRY_RUN_PLAN_REVIEW_SCHEMA_VERSION
    runtime_mutation_allowed: bool = False
    operator_approval_required: bool = True
    recommended_next_step: str | None = None

    def __post_init__(self) -> None:
        _require_safe_identifier(self.dry_run_plan_id, "dry_run_plan_id")
        _require_safe_identifier(self.candidate_id, "candidate_id")
        candidate_type = _require_candidate_type(self.candidate_type)
        object.__setattr__(self, "candidate_type", candidate_type)
        if self.decision not in ALLOWED_DRY_RUN_PLAN_REVIEW_DECISIONS:
            allowed = ", ".join(sorted(ALLOWED_DRY_RUN_PLAN_REVIEW_DECISIONS))
            raise ValueError(f"dry-run plan review decision must be one of: {allowed}")
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
        if self.decision == "needs_dry_run_plan_revision" and not revisions:
            raise ValueError("needs_dry_run_plan_revision requires required_revisions")
        object.__setattr__(self, "required_revisions", revisions)
        _require_timezone_aware_datetime(self.reviewed_at, "reviewed_at")
        if self.validation_ref is not None:
            object.__setattr__(
                self,
                "validation_ref",
                _require_relative_ref(self.validation_ref, "validation_ref"),
            )
        if self.validation_passed is not None and not isinstance(self.validation_passed, bool):
            raise ValueError("validation_passed must be a bool when provided")
        if self.decision == "dry_run_plan_ready_for_review":
            if self.validation_ref is None:
                raise ValueError("dry-run plan ready decision requires validation report")
            if self.validation_passed is not True:
                raise ValueError("dry-run plan ready decision requires passing validation")
        if self.schema_version != DRY_RUN_PLAN_REVIEW_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {DRY_RUN_PLAN_REVIEW_SCHEMA_VERSION!r}")
        if self.runtime_mutation_allowed is not False:
            raise ValueError("dry-run plan review decision must not allow runtime mutation")
        if self.operator_approval_required is not True:
            raise ValueError("dry-run plan review decision must require operator approval")
        next_step = self.recommended_next_step or _default_review_next_step(self.decision)
        next_step = _require_non_empty_text(next_step, "recommended_next_step")
        _reject_promotion_text(next_step, "recommended_next_step")
        object.__setattr__(self, "recommended_next_step", next_step)


class DryRunPlanRecorder:
    """Persist dry-run plan artifacts under a research-only artifact root."""

    def __init__(
        self,
        root: str | Path = Path("artifacts") / "research" / "research_factory" / "dry_run_plans",
        *,
        code_version: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = _require_research_dry_run_root(root)
        self.code_version = code_version
        self._clock = clock or _utc_now

    def record_plan(
        self,
        plan: DryRunPlanPackage,
        *,
        validation_report: DryRunPlanValidationReport | None = None,
    ) -> dict[str, Any]:
        """Write a dry-run plan package, optional validation report, and manifest."""
        if not isinstance(plan, DryRunPlanPackage):
            raise ValueError("plan must be a DryRunPlanPackage")
        if validation_report is not None:
            _require_validation_matches_plan(validation_report, plan)
        plan_dir = self._plan_dir(plan.dry_run_plan_id)
        if plan_dir.exists():
            raise ValueError(f"dry-run plan {plan.dry_run_plan_id!r} already exists")
        plan_dir.mkdir(parents=True)
        _write_json_atomic(plan_dir / DRY_RUN_PLAN_PACKAGE_REF, _to_jsonable(plan))
        output_refs = {"dry_run_plan_package": DRY_RUN_PLAN_PACKAGE_REF}
        if validation_report is not None:
            _write_json_atomic(plan_dir / DRY_RUN_PLAN_VALIDATION_REF, _to_jsonable(validation_report))
            output_refs["dry_run_plan_validation_report"] = DRY_RUN_PLAN_VALIDATION_REF
        manifest = build_artifact_manifest(
            artifact_id=plan.dry_run_plan_id,
            artifact_type="dry_run_plan",
            status="succeeded",
            started_at=plan.created_at,
            finished_at=self._now(),
            input_refs={
                "source_manual_apply_design_review_id": plan.source_manual_apply_design_review_id,
                "source_manual_apply_design_id": plan.source_manual_apply_design_id,
                "candidate_id": plan.candidate_id,
                "candidate_type": plan.candidate_type,
                "target_environment": plan.target_environment,
                "plan_status": plan.status,
                "validation_passed": validation_report.passed if validation_report is not None else None,
            },
            output_refs=output_refs,
            code_version=self.code_version,
            notes="research-only dry-run plan package; does not execute dry-run or apply",
        )
        write_artifact_manifest_atomic(plan_dir / DRY_RUN_PLAN_MANIFEST_REF, manifest)
        return manifest

    def _plan_dir(self, dry_run_plan_id: str) -> Path:
        return self.root / _require_safe_identifier(dry_run_plan_id, "dry_run_plan_id")

    def _now(self) -> datetime:
        return self._clock()


def build_dry_run_plan_package(
    *,
    design: ManualApplyDesignPackage,
    design_validation: ManualApplyDesignValidationReport,
    design_review: ManualApplyDesignReview,
    design_review_decision: ManualApplyDesignReviewDecision,
    target_environment: str,
    dry_run_scope: str,
    expected_runtime_components: Sequence[str],
    required_input_artifacts: Mapping[str, str],
    required_risk_guards: Sequence[str],
    rollback_plan_ref: str,
    success_criteria: Sequence[str],
    abort_conditions: Sequence[str],
    dry_run_plan_id: str | None = None,
    status: str = "dry_run_plan_draft",
    created_at: datetime | None = None,
    notes: Sequence[str] = (),
) -> DryRunPlanPackage:
    """Build a dry-run plan package from a validated manual design review."""
    if not isinstance(design, ManualApplyDesignPackage):
        raise ValueError("design must be a ManualApplyDesignPackage")
    if not isinstance(design_validation, ManualApplyDesignValidationReport):
        raise ValueError("design_validation must be a ManualApplyDesignValidationReport")
    if not isinstance(design_review, ManualApplyDesignReview):
        raise ValueError("design_review must be a ManualApplyDesignReview")
    if not isinstance(design_review_decision, ManualApplyDesignReviewDecision):
        raise ValueError("design_review_decision must be a ManualApplyDesignReviewDecision")
    _require_design_review_matches_design(design_review, design)
    _require_design_decision_matches_review(design_review_decision, design_review)
    if design_validation.design_id != design.design_id:
        raise ValueError("design validation design_id must match design")
    if design_validation.passed is not True:
        raise ValueError("dry-run plan requires passing manual apply design validation")
    if design_review_decision.decision != "design_ready_for_dry_run_planning":
        raise ValueError("dry-run plan requires design_ready_for_dry_run_planning decision")
    if design_review_decision.validation_passed is not True:
        raise ValueError("dry-run plan requires design review decision with passing validation")
    return DryRunPlanPackage(
        dry_run_plan_id=dry_run_plan_id or f"dry_run_plan_{design.design_id}",
        source_manual_apply_design_review_id=design_review.review_id,
        source_manual_apply_design_id=design.design_id,
        candidate_id=design.candidate_id,
        candidate_type=design.candidate_type,
        target_environment=target_environment,
        dry_run_scope=dry_run_scope,
        expected_runtime_components=expected_runtime_components,
        required_input_artifacts=required_input_artifacts,
        required_risk_guards=required_risk_guards,
        rollback_plan_ref=rollback_plan_ref,
        success_criteria=success_criteria,
        abort_conditions=abort_conditions,
        status=status,
        created_at=created_at or datetime.now(UTC),
        notes=notes
        or (
            "dry-run plan package is planning-only",
            "separate approval is required before any dry-run execution",
        ),
    )


def validate_dry_run_plan(
    plan: DryRunPlanPackage,
    *,
    evaluated_at: datetime | None = None,
) -> DryRunPlanValidationReport:
    """Validate a dry-run plan without executing it."""
    if not isinstance(plan, DryRunPlanPackage):
        raise ValueError("plan must be a DryRunPlanPackage")
    failures: list[str] = []
    if plan.runtime_mutation_allowed is not False:
        failures.append("runtime_mutation_allowed must be false")
    if plan.operator_approval_required is not True:
        failures.append("operator_approval_required must be true")
    if plan.status == "dry_run_plan_rejected":
        failures.append("dry_run_plan_rejected packages cannot enter plan review")
    if not plan.required_input_artifacts:
        failures.append("required_input_artifacts must not be empty")
    for required_ref in (
        "manual_apply_design_package",
        "manual_apply_design_review",
        "manual_apply_design_review_decision",
        "manual_apply_design_validation_report",
        "rollback_plan",
    ):
        if required_ref not in plan.required_input_artifacts:
            failures.append(f"dry-run plan missing input artifact: {required_ref}")
    if not plan.required_risk_guards:
        failures.append("required_risk_guards must not be empty")
    if not plan.success_criteria:
        failures.append("success_criteria must not be empty")
    if not plan.abort_conditions:
        failures.append("abort_conditions must not be empty")
    if plan.rollback_plan_ref != plan.required_input_artifacts.get("rollback_plan"):
        failures.append("rollback_plan_ref must match required_input_artifacts.rollback_plan")
    return DryRunPlanValidationReport(
        dry_run_plan_id=plan.dry_run_plan_id,
        candidate_id=plan.candidate_id,
        candidate_type=plan.candidate_type,
        passed=not failures,
        failures=tuple(failures),
        evaluated_at=evaluated_at or datetime.now(UTC),
    )


def build_dry_run_plan_review_decision(
    *,
    plan: DryRunPlanPackage,
    validation_report: DryRunPlanValidationReport | None,
    decision: str,
    rationale: str,
    reviewed_by: str,
    required_revisions: Sequence[str] = (),
    reviewed_at: datetime | None = None,
) -> DryRunPlanReviewDecision:
    """Build a research-only dry-run plan review decision."""
    if not isinstance(plan, DryRunPlanPackage):
        raise ValueError("plan must be a DryRunPlanPackage")
    if validation_report is not None:
        _require_validation_matches_plan(validation_report, plan)
    if decision == "dry_run_plan_ready_for_review":
        if validation_report is None:
            raise ValueError("dry-run plan ready decision requires validation report")
        if validation_report.passed is not True:
            raise ValueError("dry-run plan ready decision requires passing validation")
    return DryRunPlanReviewDecision(
        dry_run_plan_id=plan.dry_run_plan_id,
        candidate_id=plan.candidate_id,
        candidate_type=plan.candidate_type,
        decision=decision,
        rationale=rationale,
        reviewed_by=reviewed_by,
        required_revisions=required_revisions,
        reviewed_at=reviewed_at or datetime.now(UTC),
        validation_ref=DRY_RUN_PLAN_VALIDATION_REF if validation_report is not None else None,
        validation_passed=validation_report.passed if validation_report is not None else None,
    )


def _require_design_review_matches_design(
    review: ManualApplyDesignReview,
    design: ManualApplyDesignPackage,
) -> None:
    if review.design_id != design.design_id:
        raise ValueError("manual apply design review design_id must match design")
    if review.candidate_id != design.candidate_id:
        raise ValueError("manual apply design review candidate_id must match design")
    if review.candidate_type != design.candidate_type:
        raise ValueError("manual apply design review candidate_type must match design")
    if review.design_status != design.status:
        raise ValueError("manual apply design review design_status must match design")


def _require_design_decision_matches_review(
    decision: ManualApplyDesignReviewDecision,
    review: ManualApplyDesignReview,
) -> None:
    if decision.review_id != review.review_id:
        raise ValueError("manual apply design review decision review_id must match review")
    if decision.design_id != review.design_id:
        raise ValueError("manual apply design review decision design_id must match review")
    if decision.candidate_id != review.candidate_id:
        raise ValueError("manual apply design review decision candidate_id must match review")
    if decision.candidate_type != review.candidate_type:
        raise ValueError("manual apply design review decision candidate_type must match review")


def _require_validation_matches_plan(
    validation: DryRunPlanValidationReport,
    plan: DryRunPlanPackage,
) -> None:
    if validation.dry_run_plan_id != plan.dry_run_plan_id:
        raise ValueError("dry-run plan validation dry_run_plan_id must match plan")
    if validation.candidate_id != plan.candidate_id:
        raise ValueError("dry-run plan validation candidate_id must match plan")
    if validation.candidate_type != plan.candidate_type:
        raise ValueError("dry-run plan validation candidate_type must match plan")


def _default_review_next_step(decision: str) -> str:
    if decision == "dry_run_plan_ready_for_review":
        return "submit_dry_run_plan_for_separate_execution_approval"
    if decision == "needs_dry_run_plan_revision":
        return "revise_dry_run_plan_package"
    return "archive_dry_run_plan_rejection"


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


def _normalize_refs(refs: Mapping[str, str], field_name: str) -> dict[str, str]:
    if not isinstance(refs, Mapping) or not refs:
        raise ValueError(f"{field_name} must be a non-empty mapping")
    normalized: dict[str, str] = {}
    for name, value in refs.items():
        ref_name = _require_non_empty_text(name, f"{field_name} key")
        _reject_promotion_text(ref_name, f"{field_name} key")
        normalized[ref_name] = _require_relative_ref(value, f"{field_name}.{ref_name}")
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


def _require_research_dry_run_root(value: str | Path) -> Path:
    path = Path(value)
    parts = path.parts
    if ".." in parts:
        raise ValueError("dry-run plan root must not contain path traversal")
    has_research_artifact_root = any(
        parts[index] == "artifacts" and parts[index + 1] == "research"
        for index in range(len(parts) - 1)
    )
    if not has_research_artifact_root:
        raise ValueError("dry-run plan root must be under artifacts/research")
    return path


def _utc_now() -> datetime:
    return datetime.now(UTC)
