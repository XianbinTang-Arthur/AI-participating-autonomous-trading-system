"""Research-only manual design workflow orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Any

from aats.data_platform.research_factory.artifacts import (
    build_artifact_manifest,
    write_artifact_manifest_atomic,
)
from aats.data_platform.research_factory.dry_run_planning import (
    DRY_RUN_PLAN_PACKAGE_REF,
    DRY_RUN_PLAN_VALIDATION_REF,
    DryRunPlanRecorder,
    build_dry_run_plan_package,
    validate_dry_run_plan,
)
from aats.data_platform.research_factory.manual_apply_design import (
    MANUAL_APPLY_DESIGN_PACKAGE_REF,
    MANUAL_APPLY_DESIGN_REVIEW_DECISION_REF,
    MANUAL_APPLY_DESIGN_REVIEW_REF,
    MANUAL_APPLY_DESIGN_VALIDATION_REF,
    ManualApplyDesignPolicy,
    ManualApplyDesignRecorder,
    ManualApplyDesignReviewRecorder,
    build_manual_apply_design_package,
    build_manual_apply_design_review_decision,
    validate_manual_apply_design_domain,
)
from aats.data_platform.research_factory.preapply import (
    PreApplyEvidencePackage,
    PreApplyReview,
    PreApplyReviewDecision,
)

MANUAL_DESIGN_WORKFLOW_CODE_VERSION = "research_factory_manual_design_workflow_v1"
MANUAL_DESIGN_WORKFLOW_SUMMARY_REF = "manual_design_workflow_summary.json"
MANUAL_DESIGN_WORKFLOW_MANIFEST_REF = "manual_design_workflow_manifest.json"
STAGE_PREPARE_MANUAL_DESIGN_WORKFLOW = "prepare_manual_design_workflow"
STAGE_MANUAL_APPLY_DESIGN = "manual_apply_design"
STAGE_MANUAL_APPLY_DESIGN_VALIDATION = "manual_apply_design_validation"
STAGE_MANUAL_APPLY_DESIGN_REVIEW = "manual_apply_design_review"
STAGE_DRY_RUN_PLAN = "dry_run_plan"
STAGE_MANUAL_DESIGN_WORKFLOW_SUMMARY = "manual_design_workflow_summary"
ALLOWED_MANUAL_DESIGN_WORKFLOW_STAGE_STATUSES = frozenset({"succeeded", "blocked", "failed"})


@dataclass(frozen=True, slots=True)
class ManualDesignWorkflowConfig:
    """Inputs for the separate manual design to dry-run planning workflow."""

    preapply_package: PreApplyEvidencePackage
    preapply_review: PreApplyReview
    preapply_review_decision: PreApplyReviewDecision
    candidate_type: str
    proposed_change_summary: str
    parameter_or_config_delta: Mapping[str, Any]
    affected_runtime_components: Sequence[str]
    required_risk_guards: Sequence[str]
    required_dry_run_checks: Sequence[str]
    rollback_plan_ref: str
    dry_run_target_environment: str
    dry_run_scope: str
    dry_run_success_criteria: Sequence[str]
    dry_run_abort_conditions: Sequence[str]
    research_factory_root: Path = Path("artifacts") / "research" / "research_factory"
    workflow_id: str | None = None
    design_id: str | None = None
    design_review_id: str | None = None
    dry_run_plan_id: str | None = None
    manual_design_policy: ManualApplyDesignPolicy | None = None
    manual_design_policy_profile: str = "dry_run_planning"
    manual_design_review_decision: str | None = None
    manual_design_review_rationale: str | None = None
    manual_design_required_revisions: Sequence[str] = field(default_factory=tuple)
    reviewed_by: str = "research_manual_design_workflow"
    dry_run_expected_runtime_components: Sequence[str] | None = None
    dry_run_required_input_artifacts: Mapping[str, str] | None = None
    dry_run_required_risk_guards: Sequence[str] | None = None
    workflow_root: Path | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not isinstance(self.preapply_package, PreApplyEvidencePackage):
            raise ValueError("preapply_package must be a PreApplyEvidencePackage")
        if not isinstance(self.preapply_review, PreApplyReview):
            raise ValueError("preapply_review must be a PreApplyReview")
        if not isinstance(self.preapply_review_decision, PreApplyReviewDecision):
            raise ValueError("preapply_review_decision must be a PreApplyReviewDecision")
        _require_safe_identifier(self.candidate_type, "candidate_type")
        if self.workflow_id is not None:
            _require_safe_identifier(self.workflow_id, "workflow_id")
        if self.design_id is not None:
            _require_safe_identifier(self.design_id, "design_id")
        if self.design_review_id is not None:
            _require_safe_identifier(self.design_review_id, "design_review_id")
        if self.dry_run_plan_id is not None:
            _require_safe_identifier(self.dry_run_plan_id, "dry_run_plan_id")
        object.__setattr__(
            self,
            "research_factory_root",
            _require_research_artifact_directory(self.research_factory_root),
        )
        if self.workflow_root is not None:
            object.__setattr__(
                self,
                "workflow_root",
                _require_research_artifact_directory(self.workflow_root),
            )
        _require_timezone_aware_datetime(self.timestamp, "timestamp")


@dataclass(frozen=True, slots=True)
class ManualDesignWorkflowResult:
    """Concise result for the manual design workflow."""

    workflow_id: str
    workflow_dir: str
    status: str
    design_id: str | None = None
    design_review_id: str | None = None
    dry_run_plan_id: str | None = None
    design_validation_passed: bool | None = None
    design_review_decision: str | None = None
    dry_run_plan_validation_passed: bool | None = None
    workflow_summary_ref: str = MANUAL_DESIGN_WORKFLOW_SUMMARY_REF
    next_step: str = "inspect_manual_design_workflow_summary"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "workflow_dir": self.workflow_dir,
            "status": self.status,
            "design_id": self.design_id,
            "design_review_id": self.design_review_id,
            "dry_run_plan_id": self.dry_run_plan_id,
            "design_validation_passed": self.design_validation_passed,
            "design_review_decision": self.design_review_decision,
            "dry_run_plan_validation_passed": self.dry_run_plan_validation_passed,
            "workflow_summary_ref": self.workflow_summary_ref,
            "next_step": self.next_step,
            "error": self.error,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True, slots=True)
class ManualDesignWorkflowStageResult:
    """Machine-readable result for one manual design workflow stage."""

    stage_name: str
    status: str
    artifact_refs: Mapping[str, str] = field(default_factory=dict)
    blocking_failures: Sequence[str] = field(default_factory=tuple)
    next_debug_action: str = "inspect manual_design_workflow_summary.json"
    blocking_artifact: str | None = None
    runtime_mutation_allowed: bool = False

    def __post_init__(self) -> None:
        _require_safe_identifier(self.stage_name, "stage_name")
        if self.status not in ALLOWED_MANUAL_DESIGN_WORKFLOW_STAGE_STATUSES:
            allowed = ", ".join(sorted(ALLOWED_MANUAL_DESIGN_WORKFLOW_STAGE_STATUSES))
            raise ValueError(f"stage status must be one of: {allowed}")
        if not isinstance(self.artifact_refs, Mapping):
            raise ValueError("stage artifact_refs must be a mapping")
        object.__setattr__(
            self,
            "artifact_refs",
            {str(key): str(value) for key, value in sorted(self.artifact_refs.items())},
        )
        object.__setattr__(
            self,
            "blocking_failures",
            tuple(str(item) for item in self.blocking_failures),
        )
        if not isinstance(self.next_debug_action, str) or not self.next_debug_action.strip():
            raise ValueError("stage next_debug_action must be a non-empty string")
        object.__setattr__(self, "next_debug_action", self.next_debug_action.strip())
        if self.blocking_artifact is not None:
            object.__setattr__(self, "blocking_artifact", str(self.blocking_artifact))
        if self.runtime_mutation_allowed is not False:
            raise ValueError("manual design workflow stage must not allow runtime mutation")


def run_manual_design_workflow(config: ManualDesignWorkflowConfig) -> ManualDesignWorkflowResult:
    """Run the design-only workflow from pre-apply review decision to dry-run plan evidence."""
    if not isinstance(config, ManualDesignWorkflowConfig):
        raise ValueError("config must be ManualDesignWorkflowConfig")
    workflow_root = config.workflow_root or config.research_factory_root / "manual_design_workflows"
    workflow_id = config.workflow_id or _default_workflow_id(config)
    workflow_dir: Path | None = None
    current_stage = STAGE_PREPARE_MANUAL_DESIGN_WORKFLOW
    design_id: str | None = None
    design_review_id: str | None = None
    dry_run_plan_id: str | None = None

    try:
        workflow_dir = _prepare_workflow_dir(workflow_root, workflow_id)
        current_stage = STAGE_MANUAL_APPLY_DESIGN
        design = build_manual_apply_design_package(
            preapply_package=config.preapply_package,
            preapply_review=config.preapply_review,
            preapply_review_decision=config.preapply_review_decision,
            candidate_type=config.candidate_type,
            proposed_change_summary=config.proposed_change_summary,
            parameter_or_config_delta=config.parameter_or_config_delta,
            affected_runtime_components=config.affected_runtime_components,
            required_risk_guards=config.required_risk_guards,
            required_dry_run_checks=config.required_dry_run_checks,
            rollback_plan_ref=config.rollback_plan_ref,
            design_id=config.design_id,
            created_at=config.timestamp,
        )
        design_id = design.design_id
        ManualApplyDesignRecorder(
            config.research_factory_root / "manual_apply_designs",
            code_version=MANUAL_DESIGN_WORKFLOW_CODE_VERSION,
            clock=lambda: config.timestamp,
        ).record_package(design)

        current_stage = STAGE_MANUAL_APPLY_DESIGN_VALIDATION
        design_validation = validate_manual_apply_design_domain(
            design,
            policy=config.manual_design_policy,
            policy_profile=config.manual_design_policy_profile,
            evaluated_at=config.timestamp,
        )

        current_stage = STAGE_MANUAL_APPLY_DESIGN_REVIEW
        design_review_recorder = ManualApplyDesignReviewRecorder(
            config.research_factory_root / "manual_apply_design_reviews",
            code_version=MANUAL_DESIGN_WORKFLOW_CODE_VERSION,
            clock=lambda: config.timestamp,
        )
        design_review = design_review_recorder.start_review(
            design,
            validation_report=design_validation,
            review_id=config.design_review_id,
            notes=("manual design workflow created review-pending design evidence only",),
        )
        design_review_id = design_review.review_id
        design_review_decision_name = _manual_design_review_decision_name(
            config=config,
            validation_passed=design_validation.passed,
        )
        design_review_decision = build_manual_apply_design_review_decision(
            review=design_review,
            design=design,
            validation_report=design_validation,
            decision=design_review_decision_name,
            rationale=config.manual_design_review_rationale
            or _manual_design_review_rationale(
                decision=design_review_decision_name,
                validation_failures=design_validation.failures,
            ),
            reviewed_by=config.reviewed_by,
            required_revisions=_manual_design_required_revisions(
                config=config,
                decision=design_review_decision_name,
                validation_failures=design_validation.failures,
            ),
            reviewed_at=config.timestamp,
        )
        design_review_recorder.record_decision(design_review_decision)

        if design_review_decision.decision != "design_ready_for_dry_run_planning":
            status = design_review_decision.decision
            summary = _manual_design_workflow_summary(
                config=config,
                workflow_id=workflow_id,
                status=status,
                design_id=design.design_id,
                design_review_id=design_review.review_id,
                dry_run_plan_id=None,
                design_validation_passed=design_validation.passed,
                design_review_decision=design_review_decision.decision,
                dry_run_plan_validation_passed=None,
                failed_stage=STAGE_MANUAL_APPLY_DESIGN_REVIEW,
                blocking_artifact=_design_review_decision_ref(design_review.review_id),
                blocking_failures=tuple(design_review_decision.required_revisions),
                stage_results=_stage_results(
                    workflow_id=workflow_id,
                    design_id=design.design_id,
                    design_review_id=design_review.review_id,
                    dry_run_plan_id=None,
                    design_validation_passed=design_validation.passed,
                    design_validation_failures=design_validation.failures,
                    design_review_decision=design_review_decision.decision,
                    dry_run_plan_validation_passed=None,
                    dry_run_plan_failures=(),
                ),
                artifact_refs=_artifact_refs(
                    workflow_id=workflow_id,
                    design_id=design.design_id,
                    design_review_id=design_review.review_id,
                    dry_run_plan_id=None,
                ),
                timestamp=config.timestamp,
            )
            _write_workflow_artifacts(workflow_dir, workflow_id, summary, config.timestamp)
            return ManualDesignWorkflowResult(
                workflow_id=workflow_id,
                workflow_dir=workflow_dir.as_posix(),
                status=status,
                design_id=design.design_id,
                design_review_id=design_review.review_id,
                design_validation_passed=design_validation.passed,
                design_review_decision=design_review_decision.decision,
                next_step=_workflow_next_step(status),
            )

        current_stage = STAGE_DRY_RUN_PLAN
        dry_run_plan = build_dry_run_plan_package(
            design=design,
            design_validation=design_validation,
            design_review=design_review,
            design_review_decision=design_review_decision,
            target_environment=config.dry_run_target_environment,
            dry_run_scope=config.dry_run_scope,
            expected_runtime_components=config.dry_run_expected_runtime_components
            or design.affected_runtime_components,
            required_input_artifacts=config.dry_run_required_input_artifacts
            or _default_dry_run_input_artifacts(design_id=design.design_id, review_id=design_review.review_id, rollback_plan_ref=design.rollback_plan_ref),
            required_risk_guards=config.dry_run_required_risk_guards or design.required_risk_guards,
            rollback_plan_ref=design.rollback_plan_ref,
            success_criteria=config.dry_run_success_criteria,
            abort_conditions=config.dry_run_abort_conditions,
            dry_run_plan_id=config.dry_run_plan_id,
            created_at=config.timestamp,
        )
        dry_run_plan_id = dry_run_plan.dry_run_plan_id
        dry_run_validation = validate_dry_run_plan(dry_run_plan, evaluated_at=config.timestamp)
        DryRunPlanRecorder(
            config.research_factory_root / "dry_run_plans",
            code_version=MANUAL_DESIGN_WORKFLOW_CODE_VERSION,
            clock=lambda: config.timestamp,
        ).record_plan(dry_run_plan, validation_report=dry_run_validation)

        status = "dry_run_plan_ready_for_review" if dry_run_validation.passed else "dry_run_plan_rejected"
        blocking_failures = tuple(dry_run_validation.failures)
        failed_stage = None if dry_run_validation.passed else STAGE_DRY_RUN_PLAN
        blocking_artifact = None if dry_run_validation.passed else _dry_run_validation_ref(dry_run_plan.dry_run_plan_id)
        artifact_refs = _artifact_refs(
            workflow_id=workflow_id,
            design_id=design.design_id,
            design_review_id=design_review.review_id,
            dry_run_plan_id=dry_run_plan.dry_run_plan_id,
        )
        summary = _manual_design_workflow_summary(
            config=config,
            workflow_id=workflow_id,
            status=status,
            design_id=design.design_id,
            design_review_id=design_review.review_id,
            dry_run_plan_id=dry_run_plan.dry_run_plan_id,
            design_validation_passed=design_validation.passed,
            design_review_decision=design_review_decision.decision,
            dry_run_plan_validation_passed=dry_run_validation.passed,
            failed_stage=failed_stage,
            blocking_artifact=blocking_artifact,
            blocking_failures=blocking_failures,
            stage_results=_stage_results(
                workflow_id=workflow_id,
                design_id=design.design_id,
                design_review_id=design_review.review_id,
                dry_run_plan_id=dry_run_plan.dry_run_plan_id,
                design_validation_passed=design_validation.passed,
                design_validation_failures=design_validation.failures,
                design_review_decision=design_review_decision.decision,
                dry_run_plan_validation_passed=dry_run_validation.passed,
                dry_run_plan_failures=dry_run_validation.failures,
            ),
            artifact_refs=artifact_refs,
            timestamp=config.timestamp,
        )
        _write_workflow_artifacts(workflow_dir, workflow_id, summary, config.timestamp)
        return ManualDesignWorkflowResult(
            workflow_id=workflow_id,
            workflow_dir=workflow_dir.as_posix(),
            status=status,
            design_id=design.design_id,
            design_review_id=design_review.review_id,
            dry_run_plan_id=dry_run_plan.dry_run_plan_id,
            design_validation_passed=design_validation.passed,
            design_review_decision=design_review_decision.decision,
            dry_run_plan_validation_passed=dry_run_validation.passed,
            next_step=_workflow_next_step(status),
        )
    except Exception as exc:
        if workflow_dir is None:
            return ManualDesignWorkflowResult(
                workflow_id=workflow_id,
                workflow_dir=_workflow_dir_path(workflow_root, workflow_id).as_posix(),
                status="failed",
                next_step="inspect_failed_manual_design_workflow",
                error=str(exc),
            )
        summary = _manual_design_workflow_summary(
            config=config,
            workflow_id=workflow_id,
            status="failed",
            design_id=design_id,
            design_review_id=design_review_id,
            dry_run_plan_id=dry_run_plan_id,
            design_validation_passed=None,
            design_review_decision=None,
            dry_run_plan_validation_passed=None,
            failed_stage=current_stage,
            blocking_artifact=_blocking_artifact_for_failed_stage(
                current_stage,
                design_id=design_id,
                design_review_id=design_review_id,
                dry_run_plan_id=dry_run_plan_id,
            ),
            blocking_failures=(str(exc),),
            stage_results=(
                ManualDesignWorkflowStageResult(
                    stage_name=current_stage,
                    status="failed",
                    artifact_refs={},
                    blocking_failures=(str(exc),),
                    next_debug_action=_failed_next_debug_action(current_stage),
                    blocking_artifact=_blocking_artifact_for_failed_stage(
                        current_stage,
                        design_id=design_id,
                        design_review_id=design_review_id,
                        dry_run_plan_id=dry_run_plan_id,
                    ),
                ),
            ),
            artifact_refs={"manual_design_workflow_summary": _workflow_summary_ref(workflow_id)},
            timestamp=config.timestamp,
            error=str(exc),
        )
        _write_workflow_artifacts(workflow_dir, workflow_id, summary, config.timestamp)
        return ManualDesignWorkflowResult(
            workflow_id=workflow_id,
            workflow_dir=workflow_dir.as_posix(),
            status="failed",
            design_id=design_id,
            design_review_id=design_review_id,
            dry_run_plan_id=dry_run_plan_id,
            next_step="inspect_failed_manual_design_workflow",
            error=str(exc),
        )


def _manual_design_workflow_summary(
    *,
    config: ManualDesignWorkflowConfig,
    workflow_id: str,
    status: str,
    design_id: str | None,
    design_review_id: str | None,
    dry_run_plan_id: str | None,
    design_validation_passed: bool | None,
    design_review_decision: str | None,
    dry_run_plan_validation_passed: bool | None,
    failed_stage: str | None,
    blocking_artifact: str | None,
    blocking_failures: Sequence[str],
    stage_results: Sequence[ManualDesignWorkflowStageResult],
    artifact_refs: Mapping[str, str],
    timestamp: datetime,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "workflow_id": workflow_id,
        "status": status,
        "source_preapply_review_id": config.preapply_review.review_id,
        "source_preapply_package_id": config.preapply_package.package_id,
        "source_preapply_review_decision": config.preapply_review_decision.decision,
        "candidate_id": config.preapply_package.candidate_id,
        "candidate_type": config.candidate_type,
        "design_id": design_id,
        "design_review_id": design_review_id,
        "dry_run_plan_id": dry_run_plan_id,
        "design_validation_passed": design_validation_passed,
        "design_review_decision": design_review_decision,
        "dry_run_plan_validation_passed": dry_run_plan_validation_passed,
        "risk_flags": _risk_flags(
            status=status,
            design_validation_passed=design_validation_passed,
            dry_run_plan_validation_passed=dry_run_plan_validation_passed,
        ),
        "blocking_failures": tuple(blocking_failures),
        "failed_stage": failed_stage,
        "blocking_artifact": blocking_artifact,
        "next_debug_action": _next_debug_action(status=status, failed_stage=failed_stage, blocking_artifact=blocking_artifact),
        "stage_results": tuple(stage_results),
        "artifact_refs": dict(sorted(artifact_refs.items())),
        "runtime_mutation_allowed": False,
        "operator_approval_required": True,
        "next_step": _workflow_next_step(status),
        "created_at": timestamp.isoformat(),
        "error": error,
    }


def _write_workflow_artifacts(
    workflow_dir: Path,
    workflow_id: str,
    summary: Mapping[str, Any],
    timestamp: datetime,
) -> None:
    _write_json_atomic(workflow_dir / MANUAL_DESIGN_WORKFLOW_SUMMARY_REF, _to_jsonable(summary))
    manifest = build_artifact_manifest(
        artifact_id=workflow_id,
        artifact_type="workflow",
        status="failed" if summary.get("status") == "failed" else "succeeded",
        started_at=timestamp,
        finished_at=timestamp,
        input_refs={
            "source_preapply_review_id": summary.get("source_preapply_review_id"),
            "source_preapply_package_id": summary.get("source_preapply_package_id"),
        },
        output_refs={
            "manual_design_workflow_summary": MANUAL_DESIGN_WORKFLOW_SUMMARY_REF,
        },
        code_version=MANUAL_DESIGN_WORKFLOW_CODE_VERSION,
        notes="research-only manual design workflow; does not execute dry-runs or apply",
    )
    write_artifact_manifest_atomic(workflow_dir / MANUAL_DESIGN_WORKFLOW_MANIFEST_REF, manifest)


def _prepare_workflow_dir(workflow_root: Path, workflow_id: str) -> Path:
    workflow_dir = _workflow_dir_path(workflow_root, workflow_id)
    workflow_root = workflow_dir.parent
    workflow_root.mkdir(parents=True, exist_ok=True)
    if workflow_dir.exists():
        raise ValueError(f"manual design workflow {workflow_id!r} already exists")
    workflow_dir.mkdir(parents=True)
    return workflow_dir


def _workflow_dir_path(workflow_root: Path, workflow_id: str) -> Path:
    workflow_id = _require_safe_identifier(workflow_id, "workflow_id")
    return _require_research_artifact_directory(workflow_root) / workflow_id


def _manual_design_review_decision_name(
    *,
    config: ManualDesignWorkflowConfig,
    validation_passed: bool,
) -> str:
    if config.manual_design_review_decision is not None:
        return config.manual_design_review_decision
    if validation_passed:
        return "design_ready_for_dry_run_planning"
    return "needs_design_revision"


def _manual_design_required_revisions(
    *,
    config: ManualDesignWorkflowConfig,
    decision: str,
    validation_failures: Sequence[str],
) -> tuple[str, ...]:
    if config.manual_design_required_revisions:
        return tuple(config.manual_design_required_revisions)
    if decision == "needs_design_revision":
        return tuple(validation_failures) or ("revise manual apply design package",)
    return ()


def _manual_design_review_rationale(
    *,
    decision: str,
    validation_failures: Sequence[str],
) -> str:
    if decision == "design_ready_for_dry_run_planning":
        return "manual design validation passed and dry-run planning evidence can be prepared"
    if decision == "needs_design_revision":
        details = "; ".join(validation_failures) if validation_failures else "manual design needs revision"
        return f"manual design needs revision before dry-run planning: {details}"
    return "manual design review rejected the design draft"


def _default_dry_run_input_artifacts(
    *,
    design_id: str,
    review_id: str,
    rollback_plan_ref: str,
) -> dict[str, str]:
    return {
        "manual_apply_design_package": f"manual_apply_designs/{design_id}/{MANUAL_APPLY_DESIGN_PACKAGE_REF}",
        "manual_apply_design_review": f"manual_apply_design_reviews/{review_id}/{MANUAL_APPLY_DESIGN_REVIEW_REF}",
        "manual_apply_design_review_decision": (
            f"manual_apply_design_reviews/{review_id}/{MANUAL_APPLY_DESIGN_REVIEW_DECISION_REF}"
        ),
        "manual_apply_design_validation_report": (
            f"manual_apply_design_reviews/{review_id}/{MANUAL_APPLY_DESIGN_VALIDATION_REF}"
        ),
        "rollback_plan": rollback_plan_ref,
    }


def _artifact_refs(
    *,
    workflow_id: str,
    design_id: str | None,
    design_review_id: str | None,
    dry_run_plan_id: str | None,
) -> dict[str, str]:
    refs = {"manual_design_workflow_summary": _workflow_summary_ref(workflow_id)}
    if design_id is not None:
        refs["manual_apply_design_package"] = f"manual_apply_designs/{design_id}/{MANUAL_APPLY_DESIGN_PACKAGE_REF}"
        refs["manual_apply_design_manifest"] = f"manual_apply_designs/{design_id}/manual_apply_design_manifest.json"
    if design_review_id is not None:
        refs["manual_apply_design_review"] = _design_review_ref(design_review_id)
        refs["manual_apply_design_validation_report"] = (
            f"manual_apply_design_reviews/{design_review_id}/{MANUAL_APPLY_DESIGN_VALIDATION_REF}"
        )
        refs["manual_apply_design_review_decision"] = _design_review_decision_ref(design_review_id)
    if dry_run_plan_id is not None:
        refs["dry_run_plan_package"] = f"dry_run_plans/{dry_run_plan_id}/{DRY_RUN_PLAN_PACKAGE_REF}"
        refs["dry_run_plan_validation_report"] = _dry_run_validation_ref(dry_run_plan_id)
    return refs


def _stage_results(
    *,
    workflow_id: str,
    design_id: str,
    design_review_id: str,
    dry_run_plan_id: str | None,
    design_validation_passed: bool,
    design_validation_failures: Sequence[str],
    design_review_decision: str,
    dry_run_plan_validation_passed: bool | None,
    dry_run_plan_failures: Sequence[str],
) -> tuple[ManualDesignWorkflowStageResult, ...]:
    design_ref = f"manual_apply_designs/{design_id}/{MANUAL_APPLY_DESIGN_PACKAGE_REF}"
    validation_ref = f"manual_apply_design_reviews/{design_review_id}/{MANUAL_APPLY_DESIGN_VALIDATION_REF}"
    review_decision_ref = _design_review_decision_ref(design_review_id)
    stages: list[ManualDesignWorkflowStageResult] = [
        ManualDesignWorkflowStageResult(
            stage_name=STAGE_MANUAL_APPLY_DESIGN,
            status="succeeded",
            artifact_refs={"manual_apply_design_package": design_ref},
            next_debug_action=f"inspect {design_ref}",
        ),
        ManualDesignWorkflowStageResult(
            stage_name=STAGE_MANUAL_APPLY_DESIGN_VALIDATION,
            status="succeeded" if design_validation_passed else "blocked",
            artifact_refs={"manual_apply_design_validation_report": validation_ref},
            blocking_failures=tuple(design_validation_failures),
            next_debug_action=f"inspect {validation_ref}",
            blocking_artifact=None if design_validation_passed else validation_ref,
        ),
        ManualDesignWorkflowStageResult(
            stage_name=STAGE_MANUAL_APPLY_DESIGN_REVIEW,
            status="succeeded" if design_review_decision == "design_ready_for_dry_run_planning" else "blocked",
            artifact_refs={"manual_apply_design_review_decision": review_decision_ref},
            blocking_failures=() if design_review_decision == "design_ready_for_dry_run_planning" else (design_review_decision,),
            next_debug_action=f"inspect {review_decision_ref}",
            blocking_artifact=None
            if design_review_decision == "design_ready_for_dry_run_planning"
            else review_decision_ref,
        ),
    ]
    if dry_run_plan_id is not None:
        dry_run_ref = f"dry_run_plans/{dry_run_plan_id}/{DRY_RUN_PLAN_PACKAGE_REF}"
        dry_run_validation_ref = _dry_run_validation_ref(dry_run_plan_id)
        stages.append(
            ManualDesignWorkflowStageResult(
                stage_name=STAGE_DRY_RUN_PLAN,
                status="succeeded" if dry_run_plan_validation_passed else "blocked",
                artifact_refs={
                    "dry_run_plan_package": dry_run_ref,
                    "dry_run_plan_validation_report": dry_run_validation_ref,
                },
                blocking_failures=tuple(dry_run_plan_failures),
                next_debug_action=f"inspect {dry_run_validation_ref}",
                blocking_artifact=None if dry_run_plan_validation_passed else dry_run_validation_ref,
            )
        )
    stages.append(
        ManualDesignWorkflowStageResult(
            stage_name=STAGE_MANUAL_DESIGN_WORKFLOW_SUMMARY,
            status="succeeded",
            artifact_refs={"manual_design_workflow_summary": _workflow_summary_ref(workflow_id)},
            next_debug_action=f"inspect {_workflow_summary_ref(workflow_id)}",
        )
    )
    return tuple(stages)


def _risk_flags(
    *,
    status: str,
    design_validation_passed: bool | None,
    dry_run_plan_validation_passed: bool | None,
) -> list[str]:
    flags: list[str] = []
    if design_validation_passed is False:
        flags.append("manual_design_validation_failed")
    if status in {"design_rejected", "needs_design_revision"}:
        flags.append(f"manual_design_review_{status}")
    if dry_run_plan_validation_passed is False:
        flags.append("dry_run_plan_validation_failed")
    return flags


def _workflow_next_step(status: str) -> str:
    if status == "dry_run_plan_ready_for_review":
        return "operator_review_dry_run_plan_evidence"
    if status == "dry_run_plan_rejected":
        return "revise_dry_run_plan_package"
    if status == "needs_design_revision":
        return "revise_manual_apply_design_package"
    if status == "design_rejected":
        return "archive_manual_apply_design_rejection"
    return "inspect_manual_design_workflow_summary"


def _next_debug_action(
    *,
    status: str,
    failed_stage: str | None,
    blocking_artifact: str | None,
) -> str:
    if failed_stage is None:
        return "inspect dry_run_plan_package.json before any separate dry-run review"
    if blocking_artifact is not None:
        return f"inspect {blocking_artifact}"
    return f"inspect manual_design_workflow_summary.json for status={status}"


def _blocking_artifact_for_failed_stage(
    stage: str,
    *,
    design_id: str | None,
    design_review_id: str | None,
    dry_run_plan_id: str | None,
) -> str | None:
    if stage == STAGE_MANUAL_APPLY_DESIGN and design_id is not None:
        return f"manual_apply_designs/{design_id}/{MANUAL_APPLY_DESIGN_PACKAGE_REF}"
    if stage in {STAGE_MANUAL_APPLY_DESIGN_VALIDATION, STAGE_MANUAL_APPLY_DESIGN_REVIEW} and design_review_id is not None:
        return f"manual_apply_design_reviews/{design_review_id}/{MANUAL_APPLY_DESIGN_REVIEW_REF}"
    if stage == STAGE_DRY_RUN_PLAN and dry_run_plan_id is not None:
        return f"dry_run_plans/{dry_run_plan_id}/{DRY_RUN_PLAN_PACKAGE_REF}"
    return None


def _failed_next_debug_action(stage: str) -> str:
    return f"inspect manual_design_workflow_summary.json for failed stage {stage}"


def _default_workflow_id(config: ManualDesignWorkflowConfig) -> str:
    seed = "|".join(
        (
            config.preapply_review.review_id,
            config.preapply_review_decision.decision,
            config.candidate_type,
            config.timestamp.isoformat(),
        )
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]
    return f"manual_design_wf_{config.preapply_review.review_id}_{digest}"


def _workflow_summary_ref(workflow_id: str) -> str:
    return f"manual_design_workflows/{workflow_id}/{MANUAL_DESIGN_WORKFLOW_SUMMARY_REF}"


def _design_review_ref(review_id: str) -> str:
    return f"manual_apply_design_reviews/{review_id}/{MANUAL_APPLY_DESIGN_REVIEW_REF}"


def _design_review_decision_ref(review_id: str) -> str:
    return f"manual_apply_design_reviews/{review_id}/{MANUAL_APPLY_DESIGN_REVIEW_DECISION_REF}"


def _dry_run_validation_ref(plan_id: str) -> str:
    return f"dry_run_plans/{plan_id}/{DRY_RUN_PLAN_VALIDATION_REF}"


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
    if isinstance(value, int | float | str | bool) or value is None:
        return value
    raise TypeError(f"unsupported JSON artifact value: {type(value).__name__}")


def _require_safe_identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    value = value.strip()
    if "/" in value or "\\" in value or value in {".", ".."} or ".." in value:
        raise ValueError(f"{field_name} must not contain path traversal or separators")
    lowered = value.lower()
    for forbidden in (
        "active_parameter",
        "approved_for_apply",
        "auto_apply",
        "direct_apply",
        "live_order",
        "okx_write",
        "operator_write",
        "production_config",
        "runtime_config_write",
        "runtime_mutation",
    ):
        if forbidden in lowered:
            raise ValueError(f"{field_name} must not encode runtime promotion term: {forbidden}")
    return value


def _require_timezone_aware_datetime(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")


def _require_research_artifact_directory(value: str | Path) -> Path:
    path = Path(value)
    if ".." in path.parts:
        raise ValueError("artifact directory must not contain path traversal")
    if not any(
        path.parts[index] == "artifacts" and path.parts[index + 1] == "research"
        for index in range(len(path.parts) - 1)
    ):
        raise ValueError("artifact directory must be under artifacts/research")
    return path
