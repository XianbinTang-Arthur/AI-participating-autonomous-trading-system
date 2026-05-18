"""End-to-end Research Factory governance workflow orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Any

from aats.data_platform.research_factory.artifacts import (
    build_artifact_manifest,
    write_artifact_manifest_atomic,
)
from aats.data_platform.research_factory.evidence import (
    DatasetQualityReport,
    DatasetQualityThresholds,
    EvidenceBundle,
    ExecutionEvidenceReport,
    SourceIntegrityReport,
)
from aats.data_platform.research_factory.integrity import (
    EvidenceReferenceIntegrityReport,
    validate_preapply_package_reference_integrity,
)
from aats.data_platform.research_factory.metrics.gates import (
    CandidateArtifact,
    CandidateGateResult,
)
from aats.data_platform.research_factory.observation_sources import (
    PaperObservationDataSource,
    ShadowObservationDataSource,
)
from aats.data_platform.research_factory.observations import (
    ObservationRecorder,
    build_review_outcome,
    evaluate_observation_gate,
)
from aats.data_platform.research_factory.preapply import (
    PreApplyEvidenceRecorder,
    PreApplyReviewRecorder,
    build_preapply_evidence_package,
)
from aats.data_platform.research_factory.real_data import (
    GoldReplayDataSource,
    ResearchFactoryExperimentConfig,
    run_research_factory_experiment,
)
from aats.data_platform.research_factory.recommendations import (
    ObservationPlan,
    PreApplyEvidence,
    ResearchRecommendation,
    RollbackPlan,
)
from aats.data_platform.research_factory.registry import (
    ResearchMemoryRegistry,
    build_observation_memory_entry,
    build_preapply_memory_entry,
    default_research_memory_path_for_artifact_root,
)
from aats.data_platform.research_factory.specs import METRIC_FIELDS, MetricsSnapshot

WORKFLOW_CODE_VERSION = "research_factory_governance_workflow_v1"
WORKFLOW_SUMMARY_REF = "workflow_summary.json"
WORKFLOW_MANIFEST_REF = "workflow_manifest.json"
REFERENCE_INTEGRITY_REPORT_REF = "evidence_reference_integrity_report.json"
OPERATOR_REVIEW_SUMMARY_REF = "preapply_review_summary.md"
OPERATOR_REVIEW_CHECKLIST_REF = "operator_review_checklist.json"
OPERATOR_REVIEW_CHECKLIST_SCHEMA_VERSION = "research_operator_review_checklist_v1"


@dataclass(frozen=True, slots=True)
class ResearchGovernanceWorkflowConfig:
    """Inputs for the research-only governance workflow."""

    experiment_config: ResearchFactoryExperimentConfig
    observation_summary_path: Path
    workflow_id: str | None = None
    observation_source_type: str | None = None
    observation_id: str | None = None
    workflow_root: Path | None = None
    registry_path: Path | None = None
    allow_smoke_profile: bool = False
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not isinstance(self.experiment_config, ResearchFactoryExperimentConfig):
            raise ValueError("experiment_config must be ResearchFactoryExperimentConfig")
        if self.experiment_config.research_profile is None:
            raise ValueError("research governance workflow requires an explicit research_profile")
        if not isinstance(self.allow_smoke_profile, bool):
            raise ValueError("allow_smoke_profile must be a bool")
        if _profile_name(self.experiment_config) == "smoke" and not self.allow_smoke_profile:
            raise ValueError("smoke research_profile is only allowed when allow_smoke_profile=True")
        _require_timezone_aware_datetime(self.timestamp, "timestamp")
        object.__setattr__(self, "observation_summary_path", Path(self.observation_summary_path))
        if self.workflow_id is not None:
            _require_safe_identifier(self.workflow_id, "workflow_id")
        if self.observation_id is not None:
            _require_safe_identifier(self.observation_id, "observation_id")
        if self.observation_source_type is not None and self.observation_source_type not in {"shadow", "paper"}:
            raise ValueError("observation_source_type must be shadow or paper")


@dataclass(frozen=True, slots=True)
class ResearchGovernanceWorkflowResult:
    """Concise workflow result for CLI output."""

    workflow_id: str
    workflow_dir: str
    status: str
    profile: str
    experiment_id: str | None = None
    candidate_id: str | None = None
    recommendation_id: str | None = None
    observation_id: str | None = None
    package_id: str | None = None
    preapply_review_id: str | None = None
    observation_gate_passed: bool | None = None
    reference_integrity_passed: bool | None = None
    workflow_summary_ref: str = WORKFLOW_SUMMARY_REF
    next_step: str = "inspect_workflow_summary"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "workflow_dir": self.workflow_dir,
            "status": self.status,
            "profile": self.profile,
            "experiment_id": self.experiment_id,
            "candidate_id": self.candidate_id,
            "recommendation_id": self.recommendation_id,
            "observation_id": self.observation_id,
            "package_id": self.package_id,
            "preapply_review_id": self.preapply_review_id,
            "observation_gate_passed": self.observation_gate_passed,
            "reference_integrity_passed": self.reference_integrity_passed,
            "workflow_summary_ref": self.workflow_summary_ref,
            "next_step": self.next_step,
            "error": self.error,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def run_research_governance_workflow(
    config: ResearchGovernanceWorkflowConfig,
    *,
    data_source: GoldReplayDataSource | None = None,
) -> ResearchGovernanceWorkflowResult:
    """Run the evidence-only governance workflow from research to pre-apply review."""
    if not isinstance(config, ResearchGovernanceWorkflowConfig):
        raise ValueError("config must be ResearchGovernanceWorkflowConfig")
    profile_name = _profile_name(config.experiment_config)
    research_factory_root = _research_factory_root(config.experiment_config.artifact_root)
    workflow_root = _workflow_root(config, research_factory_root)
    workflow_id = config.workflow_id or _failed_workflow_id(config)
    workflow_dir: Path | None = None

    try:
        experiment_result = run_research_factory_experiment(
            config.experiment_config,
            data_source=data_source,
        )
        workflow_id = config.workflow_id or f"wf_{experiment_result.experiment_id}"
        workflow_dir = _prepare_workflow_dir(workflow_root, workflow_id)
        if experiment_result.status != "succeeded" or not experiment_result.candidate_generated:
            return _finish_failed_workflow(
                workflow_dir=workflow_dir,
                workflow_id=workflow_id,
                profile_name=profile_name,
                experiment_id=experiment_result.experiment_id,
            error=experiment_result.error or f"experiment status={experiment_result.status}",
            timestamp=config.timestamp,
        )

        experiment_dir = Path(experiment_result.artifact_dir)
        candidate = _load_candidate(experiment_dir / "candidate_artifact.json")
        recommendation = _load_recommendation(experiment_dir / "research_recommendation.json")
        evidence_bundle = _load_evidence_bundle(experiment_dir / "evidence_bundle.json")
        observation_recorder = ObservationRecorder(
            research_factory_root / "observations",
            code_version=WORKFLOW_CODE_VERSION,
            clock=lambda: config.timestamp,
        )
        observation_run = observation_recorder.plan(
            recommendation,
            observation_id=config.observation_id,
        )
        running_observation = observation_recorder.start(
            observation_run.observation_id,
            started_at=config.timestamp,
        )
        source = _observation_source(
            config,
            recommendation=recommendation,
            research_factory_root=research_factory_root,
        )
        neutral_result = source.load_result(
            recommendation,
            observation_id=observation_run.observation_id,
            review_decision="keep_reviewing",
            created_at=config.timestamp,
        )
        neutral_gate = evaluate_observation_gate(
            neutral_result,
            running_observation,
            thresholds=_research_profile(config.experiment_config).observation_thresholds,
            evaluated_at=config.timestamp,
        )
        observation_result = replace(
            neutral_result,
            review_decision=_decision_for_observation_gate(neutral_gate),
        )
        observation_gate = evaluate_observation_gate(
            observation_result,
            running_observation,
            thresholds=_research_profile(config.experiment_config).observation_thresholds,
            evaluated_at=config.timestamp,
        )
        observation_recorder.record_result(observation_result)
        observation_recorder.record_gate_result(observation_gate)
        review_outcome = build_review_outcome(
            observation_result,
            gate=observation_gate,
            rationale=_review_rationale(observation_gate),
            created_at=config.timestamp,
        )
        observation_recorder.record_review_outcome(review_outcome)

        package = build_preapply_evidence_package(
            candidate=candidate,
            recommendation=recommendation,
            evidence_bundle=evidence_bundle,
            observation_gate=observation_gate,
            review_outcome=review_outcome,
            evidence_refs=_preapply_evidence_refs(
                experiment_id=experiment_result.experiment_id,
                observation_id=observation_run.observation_id,
            ),
            gate_refs=_preapply_gate_refs(
                experiment_id=experiment_result.experiment_id,
                observation_id=observation_run.observation_id,
            ),
            created_at=config.timestamp,
        )
        preapply_recorder = PreApplyEvidenceRecorder(
            research_factory_root / "preapply",
            code_version=WORKFLOW_CODE_VERSION,
            clock=lambda: config.timestamp,
        )
        preapply_recorder.record_package(package)

        integrity_report = validate_preapply_package_reference_integrity(
            package,
            research_factory_root,
            created_at=config.timestamp,
        )
        review_id = f"review_{package.package_id}"
        review_recorder = PreApplyReviewRecorder(
            research_factory_root / "preapply_reviews",
            code_version=WORKFLOW_CODE_VERSION,
            clock=lambda: config.timestamp,
        )
        review = review_recorder.start_review(
            package,
            review_id=review_id,
            package_ref=f"preapply/{package.package_id}/preapply_evidence_package.json",
            reference_integrity_ref=f"preapply_reviews/{review_id}/{REFERENCE_INTEGRITY_REPORT_REF}",
            reference_integrity_passed=integrity_report.passed,
            reference_integrity_payload=_to_jsonable(integrity_report),
            reference_integrity_output_ref=REFERENCE_INTEGRITY_REPORT_REF,
            notes=("workflow created review-pending evidence only",),
        )

        registry = ResearchMemoryRegistry(
            config.registry_path
            or config.experiment_config.registry_path
            or default_research_memory_path_for_artifact_root(config.experiment_config.artifact_root)
        )
        registry.upsert(
            build_observation_memory_entry(
                candidate=candidate,
                observation_result=observation_result,
                observation_gate=observation_gate,
                review_outcome=review_outcome,
                created_by="research_governance_workflow",
                created_at=config.timestamp,
                artifact_refs=_observation_artifact_refs(observation_run.observation_id),
            )
        )
        registry.upsert(
            build_preapply_memory_entry(
                candidate=candidate,
                package=package,
                created_by="research_governance_workflow",
                created_at=config.timestamp,
                artifact_refs=_preapply_artifact_refs(package.package_id, review.review_id),
            )
        )

        status = _workflow_status(package.status, integrity_report)
        artifact_refs = _workflow_artifact_refs(
            workflow_id=workflow_id,
            experiment_id=experiment_result.experiment_id,
            observation_id=observation_run.observation_id,
            package_id=package.package_id,
            review_id=review.review_id,
            experiment_dir=experiment_dir,
        )
        risk_flags = _workflow_risk_flags(
            evidence_bundle=evidence_bundle,
            observation_gate=observation_gate,
            package=package,
            integrity_report=integrity_report,
            experiment_dir=experiment_dir,
        )
        blocking_failures = _workflow_blocking_failures(
            evidence_bundle=evidence_bundle,
            observation_gate=observation_gate,
            package=package,
            integrity_report=integrity_report,
        )
        summary = {
            "workflow_id": workflow_id,
            "status": status,
            "profile": profile_name,
            "experiment_id": experiment_result.experiment_id,
            "candidate_id": candidate.candidate_id,
            "recommendation_id": recommendation.recommendation_id,
            "observation_id": observation_run.observation_id,
            "observation_gate_passed": observation_gate.passed,
            "review_decision": review_outcome.decision,
            "package_id": package.package_id,
            "preapply_status": package.status,
            "preapply_review_id": review.review_id,
            "reference_integrity_passed": integrity_report.passed,
            "registry_path": registry.path.as_posix(),
            "next_step": _workflow_next_step(status),
            "artifact_refs": artifact_refs,
            "risk_flags": risk_flags,
            "blocking_failures": blocking_failures,
            "runtime_mutation_allowed": False,
            "operator_approval_required": True,
            "created_at": config.timestamp.isoformat(),
        }
        operator_checklist = _operator_review_checklist(
            summary=summary,
            candidate=candidate,
            recommendation=recommendation,
            evidence_bundle=evidence_bundle,
            observation_gate=observation_gate,
            package=package,
            integrity_report=integrity_report,
            experiment_dir=experiment_dir,
            timestamp=config.timestamp,
        )
        operator_summary = _operator_review_summary(
            summary=summary,
            candidate=candidate,
            recommendation=recommendation,
            evidence_bundle=evidence_bundle,
            observation_gate=observation_gate,
            package=package,
            integrity_report=integrity_report,
            operator_checklist=operator_checklist,
        )
        _write_workflow_artifacts(
            workflow_dir=workflow_dir,
            workflow_id=workflow_id,
            summary=summary,
            operator_summary=operator_summary,
            operator_checklist=operator_checklist,
            timestamp=config.timestamp,
        )
        return ResearchGovernanceWorkflowResult(
            workflow_id=workflow_id,
            workflow_dir=workflow_dir.as_posix(),
            status=status,
            profile=profile_name,
            experiment_id=experiment_result.experiment_id,
            candidate_id=candidate.candidate_id,
            recommendation_id=recommendation.recommendation_id,
            observation_id=observation_run.observation_id,
            package_id=package.package_id,
            preapply_review_id=review.review_id,
            observation_gate_passed=observation_gate.passed,
            reference_integrity_passed=integrity_report.passed,
            next_step=_workflow_next_step(status),
        )
    except Exception as exc:
        if workflow_dir is None:
            workflow_dir = _prepare_workflow_dir(workflow_root, workflow_id)
        return _finish_failed_workflow(
            workflow_dir=workflow_dir,
            workflow_id=workflow_id,
            profile_name=profile_name,
            experiment_id=config.experiment_config.experiment_id,
            error=str(exc),
            timestamp=config.timestamp,
        )


def _finish_failed_workflow(
    *,
    workflow_dir: Path,
    workflow_id: str,
    profile_name: str,
    experiment_id: str | None,
    error: str,
    timestamp: datetime,
) -> ResearchGovernanceWorkflowResult:
    summary = {
        "workflow_id": workflow_id,
        "status": "failed",
        "profile": profile_name,
        "experiment_id": experiment_id,
        "error": error,
        "runtime_mutation_allowed": False,
        "operator_approval_required": True,
        "next_step": "inspect_failed_research_workflow",
        "created_at": timestamp.isoformat(),
    }
    _write_workflow_artifacts(
        workflow_dir=workflow_dir,
        workflow_id=workflow_id,
        summary=summary,
        timestamp=timestamp,
    )
    return ResearchGovernanceWorkflowResult(
        workflow_id=workflow_id,
        workflow_dir=workflow_dir.as_posix(),
        status="failed",
        profile=profile_name,
        experiment_id=experiment_id,
        next_step="inspect_failed_research_workflow",
        error=error,
    )


def _write_workflow_artifacts(
    *,
    workflow_dir: Path,
    workflow_id: str,
    summary: Mapping[str, Any],
    operator_summary: str | None = None,
    operator_checklist: Mapping[str, Any] | None = None,
    timestamp: datetime,
) -> None:
    _write_json_atomic(workflow_dir / WORKFLOW_SUMMARY_REF, _to_jsonable(summary))
    output_refs = {"workflow_summary": WORKFLOW_SUMMARY_REF}
    if operator_summary is not None:
        _write_text_atomic(workflow_dir / OPERATOR_REVIEW_SUMMARY_REF, operator_summary)
        output_refs["preapply_review_summary"] = OPERATOR_REVIEW_SUMMARY_REF
    if operator_checklist is not None:
        _write_json_atomic(workflow_dir / OPERATOR_REVIEW_CHECKLIST_REF, _to_jsonable(operator_checklist))
        output_refs["operator_review_checklist"] = OPERATOR_REVIEW_CHECKLIST_REF
    manifest = build_artifact_manifest(
        artifact_id=workflow_id,
        artifact_type="workflow",
        status="succeeded" if summary.get("status") != "failed" else "failed",
        started_at=timestamp,
        finished_at=timestamp,
        input_refs={
            "profile": summary.get("profile"),
            "experiment_id": summary.get("experiment_id"),
        },
        output_refs=output_refs,
        code_version=WORKFLOW_CODE_VERSION,
        notes="research-only governance workflow summary",
    )
    write_artifact_manifest_atomic(workflow_dir / WORKFLOW_MANIFEST_REF, manifest)


def _prepare_workflow_dir(workflow_root: Path, workflow_id: str) -> Path:
    workflow_id = _require_safe_identifier(workflow_id, "workflow_id")
    workflow_root.mkdir(parents=True, exist_ok=True)
    workflow_dir = workflow_root / workflow_id
    if workflow_dir.exists():
        raise ValueError(f"research governance workflow {workflow_id!r} already exists")
    workflow_dir.mkdir(parents=True)
    return workflow_dir


def _observation_source(
    config: ResearchGovernanceWorkflowConfig,
    *,
    recommendation: ResearchRecommendation,
    research_factory_root: Path,
) -> ShadowObservationDataSource | PaperObservationDataSource:
    source_type = config.observation_source_type or recommendation.observation_plan.mode
    if source_type == "shadow":
        return ShadowObservationDataSource(
            config.observation_summary_path,
            research_root=research_factory_root,
        )
    if source_type == "paper":
        return PaperObservationDataSource(
            config.observation_summary_path,
            research_root=research_factory_root,
        )
    raise ValueError("observation_source_type must be shadow or paper")


def _decision_for_observation_gate(gate: Any) -> str:
    if gate.passed:
        return "eligible_for_preapply"
    if all(failure.startswith(("observed_bars=", "observed_events=")) for failure in gate.failures):
        return "keep_reviewing"
    return "reject"


def _review_rationale(gate: Any) -> str:
    if gate.passed:
        return "observation gate passed and evidence is ready for pre-apply review packaging"
    return "observation gate failed: " + "; ".join(gate.failures)


def _preapply_evidence_refs(*, experiment_id: str, observation_id: str) -> dict[str, str]:
    return {
        "candidate_artifact": f"experiments/{experiment_id}/candidate_artifact.json",
        "research_recommendation": f"experiments/{experiment_id}/research_recommendation.json",
        "metrics_snapshot": f"experiments/{experiment_id}/metrics_snapshot.json",
        "dataset_quality_report": f"experiments/{experiment_id}/dataset_quality_report.json",
        "source_integrity_report": f"experiments/{experiment_id}/source_integrity_report.json",
        "execution_evidence_report": f"experiments/{experiment_id}/execution_evidence_report.json",
        "evidence_bundle": f"experiments/{experiment_id}/evidence_bundle.json",
        "observation_result": f"observations/{observation_id}/observation_result.json",
        "review_outcome": f"observations/{observation_id}/review_outcome.json",
        "rollback_plan": f"experiments/{experiment_id}/research_recommendation.json",
    }


def _preapply_gate_refs(*, experiment_id: str, observation_id: str) -> dict[str, str]:
    return {
        "candidate_gate": f"experiments/{experiment_id}/candidate_artifact.json",
        "observation_gate_result": f"observations/{observation_id}/observation_gate_result.json",
    }


def _observation_artifact_refs(observation_id: str) -> dict[str, str]:
    return {
        "observation_result": f"observations/{observation_id}/observation_result.json",
        "observation_gate_result": f"observations/{observation_id}/observation_gate_result.json",
        "review_outcome": f"observations/{observation_id}/review_outcome.json",
    }


def _preapply_artifact_refs(package_id: str, review_id: str) -> dict[str, str]:
    return {
        "preapply_evidence_package": f"preapply/{package_id}/preapply_evidence_package.json",
        "preapply_review": f"preapply_reviews/{review_id}/preapply_review.json",
        "evidence_reference_integrity_report": (
            f"preapply_reviews/{review_id}/{REFERENCE_INTEGRITY_REPORT_REF}"
        ),
    }


def _workflow_status(
    package_status: str,
    integrity_report: EvidenceReferenceIntegrityReport,
) -> str:
    if not integrity_report.passed:
        return "reference_integrity_failed"
    if package_status == "preapply_ready":
        return "preapply_review_pending"
    return package_status


def _workflow_next_step(status: str) -> str:
    if status == "preapply_review_pending":
        return "operator_preapply_review"
    if status == "needs_more_observation":
        return "continue_shadow_or_paper_observation"
    if status == "reference_integrity_failed":
        return "repair_research_evidence_refs"
    if status == "preapply_rejected":
        return "archive_preapply_rejection"
    return "inspect_workflow_summary"


def _workflow_artifact_refs(
    *,
    workflow_id: str,
    experiment_id: str,
    observation_id: str,
    package_id: str,
    review_id: str,
    experiment_dir: Path,
) -> dict[str, str]:
    refs = {
        "experiment_manifest": f"experiments/{experiment_id}/experiment_manifest.json",
        "candidate_artifact": f"experiments/{experiment_id}/candidate_artifact.json",
        "research_recommendation": f"experiments/{experiment_id}/research_recommendation.json",
        "metrics_snapshot": f"experiments/{experiment_id}/metrics_snapshot.json",
        "dataset_quality_report": f"experiments/{experiment_id}/dataset_quality_report.json",
        "source_integrity_report": f"experiments/{experiment_id}/source_integrity_report.json",
        "execution_evidence_report": f"experiments/{experiment_id}/execution_evidence_report.json",
        "evidence_bundle": f"experiments/{experiment_id}/evidence_bundle.json",
        "observation_run": f"observations/{observation_id}/observation_run.json",
        "observation_result": f"observations/{observation_id}/observation_result.json",
        "observation_gate_result": f"observations/{observation_id}/observation_gate_result.json",
        "review_outcome": f"observations/{observation_id}/review_outcome.json",
        "preapply_evidence_package": f"preapply/{package_id}/preapply_evidence_package.json",
        "preapply_review": f"preapply_reviews/{review_id}/preapply_review.json",
        "reference_integrity_report": f"preapply_reviews/{review_id}/{REFERENCE_INTEGRITY_REPORT_REF}",
        "workflow_summary": f"workflows/{workflow_id}/{WORKFLOW_SUMMARY_REF}",
        "operator_review_summary": f"workflows/{workflow_id}/{OPERATOR_REVIEW_SUMMARY_REF}",
        "operator_review_checklist": f"workflows/{workflow_id}/{OPERATOR_REVIEW_CHECKLIST_REF}",
    }
    if (experiment_dir / "novelty_gate_result.json").exists():
        refs["novelty_gate_result"] = f"experiments/{experiment_id}/novelty_gate_result.json"
    if (experiment_dir / "factor_proposal.json").exists():
        refs["factor_proposal"] = f"experiments/{experiment_id}/factor_proposal.json"
    return refs


def _workflow_risk_flags(
    *,
    evidence_bundle: EvidenceBundle,
    observation_gate: Any,
    package: Any,
    integrity_report: EvidenceReferenceIntegrityReport,
    experiment_dir: Path,
) -> list[str]:
    flags: list[str] = []
    if not evidence_bundle.passed:
        flags.append("evidence_bundle_failed")
    execution_evidence = evidence_bundle.execution_evidence
    if execution_evidence is not None and execution_evidence.dataset_fingerprint_compatible:
        flags.append("execution_evidence_uses_dataset_compatibility")
    if not observation_gate.passed:
        flags.append("observation_gate_failed")
    if package.status != "preapply_ready":
        flags.append(f"preapply_status_{package.status}")
    if not integrity_report.passed:
        flags.append("reference_integrity_failed")
    novelty_payload = _optional_json_mapping(experiment_dir / "novelty_gate_result.json")
    novelty_decision = novelty_payload.get("decision") if novelty_payload is not None else None
    if novelty_decision in {"warn", "retest", "suppress", "duplicate"}:
        flags.append(f"novelty_{novelty_decision}")
    return flags


def _workflow_blocking_failures(
    *,
    evidence_bundle: EvidenceBundle,
    observation_gate: Any,
    package: Any,
    integrity_report: EvidenceReferenceIntegrityReport,
) -> list[str]:
    failures: list[str] = []
    failures.extend(f"evidence_bundle: {failure}" for failure in evidence_bundle.failures)
    failures.extend(f"observation_gate: {failure}" for failure in observation_gate.failures)
    failures.extend(f"preapply_package: {failure}" for failure in package.failure_reasons)
    failures.extend(f"reference_integrity: {failure}" for failure in integrity_report.failures)
    return failures


def _operator_review_checklist(
    *,
    summary: Mapping[str, Any],
    candidate: CandidateArtifact,
    recommendation: ResearchRecommendation,
    evidence_bundle: EvidenceBundle,
    observation_gate: Any,
    package: Any,
    integrity_report: EvidenceReferenceIntegrityReport,
    experiment_dir: Path,
    timestamp: datetime,
) -> dict[str, Any]:
    novelty_payload = _optional_json_mapping(experiment_dir / "novelty_gate_result.json")
    execution_evidence = evidence_bundle.execution_evidence
    checklist_items = [
        {
            "item": "candidate_gate_passed",
            "passed": candidate.gate.passed,
            "details": list(candidate.gate.failures),
        },
        {
            "item": "evidence_bundle_passed",
            "passed": evidence_bundle.passed,
            "details": list(evidence_bundle.failures),
        },
        {
            "item": "execution_evidence_passed",
            "passed": execution_evidence.passed if execution_evidence is not None else False,
            "details": list(execution_evidence.failures) if execution_evidence is not None else ["missing execution evidence"],
        },
        {
            "item": "observation_gate_passed",
            "passed": observation_gate.passed,
            "details": list(observation_gate.failures),
        },
        {
            "item": "preapply_package_ready",
            "passed": package.status == "preapply_ready",
            "details": list(package.failure_reasons),
        },
        {
            "item": "reference_integrity_passed",
            "passed": integrity_report.passed,
            "details": list(integrity_report.failures),
        },
        {
            "item": "runtime_mutation_not_authorized",
            "passed": True,
            "details": ["workflow output is evidence-only and does not authorize active parameter or runtime mutation"],
        },
    ]
    return {
        "schema_version": OPERATOR_REVIEW_CHECKLIST_SCHEMA_VERSION,
        "workflow_id": summary["workflow_id"],
        "status": summary["status"],
        "profile": summary["profile"],
        "candidate_id": candidate.candidate_id,
        "recommendation_id": recommendation.recommendation_id,
        "package_id": package.package_id,
        "preapply_review_id": summary["preapply_review_id"],
        "novelty_gate_decision": novelty_payload.get("decision") if novelty_payload is not None else None,
        "artifact_refs": summary["artifact_refs"],
        "risk_flags": summary["risk_flags"],
        "blocking_failures": summary["blocking_failures"],
        "checklist_items": checklist_items,
        "recommended_next_step": summary["next_step"],
        "runtime_mutation_allowed": False,
        "operator_approval_required": True,
        "no_runtime_mutation_statement": (
            "This research governance workflow does not authorize active parameter changes, "
            "runtime config mutation, live orders, or OKX writes."
        ),
        "created_at": timestamp.isoformat(),
    }


def _operator_review_summary(
    *,
    summary: Mapping[str, Any],
    candidate: CandidateArtifact,
    recommendation: ResearchRecommendation,
    evidence_bundle: EvidenceBundle,
    observation_gate: Any,
    package: Any,
    integrity_report: EvidenceReferenceIntegrityReport,
    operator_checklist: Mapping[str, Any],
) -> str:
    factor_expression = candidate.payload.get("factor_expression", "n/a")
    dataset_fingerprint = candidate.payload.get("dataset_fingerprint", "n/a")
    novelty_decision = operator_checklist.get("novelty_gate_decision") or "not_recorded"
    risk_flags = ", ".join(summary["risk_flags"]) if summary["risk_flags"] else "none"
    blocking_failures = summary["blocking_failures"]
    failure_text = "\n".join(f"- {failure}" for failure in blocking_failures) if blocking_failures else "- none"
    return (
        f"# Research Factory Pre-Apply Review Summary\n\n"
        f"- Workflow: `{summary['workflow_id']}`\n"
        f"- Status: `{summary['status']}`\n"
        f"- Profile: `{summary['profile']}`\n"
        f"- Candidate: `{candidate.candidate_id}`\n"
        f"- Recommendation: `{recommendation.recommendation_id}`\n"
        f"- PreApply package: `{package.package_id}`\n"
        f"- PreApply review: `{summary['preapply_review_id']}`\n"
        f"- Factor expression: `{factor_expression}`\n"
        f"- Dataset fingerprint: `{dataset_fingerprint}`\n\n"
        f"## Gate Status\n\n"
        f"- Candidate gate passed: `{candidate.gate.passed}`\n"
        f"- Evidence bundle passed: `{evidence_bundle.passed}`\n"
        f"- Observation gate passed: `{observation_gate.passed}`\n"
        f"- PreApply package status: `{package.status}`\n"
        f"- Reference integrity passed: `{integrity_report.passed}`\n"
        f"- Novelty gate decision: `{novelty_decision}`\n\n"
        f"## Review Controls\n\n"
        f"- Recommended next step: `{summary['next_step']}`\n"
        f"- Risk flags: `{risk_flags}`\n"
        f"- Runtime mutation allowed: `False`\n"
        f"- Operator approval required: `True`\n"
        f"- No runtime mutation authorized: this summary does not authorize active parameter changes, "
        f"runtime config mutation, live orders, or OKX writes.\n\n"
        f"## Blocking Failures\n\n"
        f"{failure_text}\n"
    )


def _load_candidate(path: Path) -> CandidateArtifact:
    payload = _load_json_mapping(path, "candidate_artifact")
    return CandidateArtifact(
        candidate_id=_require_text(payload, "candidate_id"),
        experiment_id=_require_text(payload, "experiment_id"),
        candidate_type=_require_text(payload, "candidate_type"),
        payload=_require_mapping(payload.get("payload"), "candidate.payload"),
        metrics=_metrics_from_mapping(_require_mapping(payload.get("metrics"), "candidate.metrics")),
        gate=_candidate_gate_from_mapping(_require_mapping(payload.get("gate"), "candidate.gate")),
        created_at=_parse_datetime(payload.get("created_at"), "candidate.created_at"),
    )


def _load_recommendation(path: Path) -> ResearchRecommendation:
    payload = _load_json_mapping(path, "research_recommendation")
    evidence_payload = _require_mapping(payload.get("evidence"), "recommendation.evidence")
    observation_plan_payload = _require_mapping(payload.get("observation_plan"), "recommendation.observation_plan")
    rollback_plan_payload = _require_mapping(payload.get("rollback_plan"), "recommendation.rollback_plan")
    evidence = PreApplyEvidence(
        candidate_id=_require_text(evidence_payload, "candidate_id"),
        experiment_id=_require_text(evidence_payload, "experiment_id"),
        metrics=_metrics_from_mapping(_require_mapping(evidence_payload.get("metrics"), "evidence.metrics")),
        gate=_candidate_gate_from_mapping(_require_mapping(evidence_payload.get("gate"), "evidence.gate")),
        dataset_fingerprint=_require_text(evidence_payload, "dataset_fingerprint"),
        benchmark_segment=_require_text(evidence_payload, "benchmark_segment"),
        evidence_refs=_require_mapping(evidence_payload.get("evidence_refs"), "evidence.evidence_refs"),
        execution_realism_required=bool(evidence_payload.get("execution_realism_required", False)),
        limitations=_text_sequence(evidence_payload.get("limitations", ()), "evidence.limitations"),
    )
    observation_plan = ObservationPlan(
        mode=_require_text(observation_plan_payload, "mode"),
        min_observation_bars=int(observation_plan_payload.get("min_observation_bars")),
        min_observation_events=int(observation_plan_payload.get("min_observation_events")),
        success_criteria=_text_sequence(observation_plan_payload.get("success_criteria", ()), "success_criteria"),
        abort_conditions=_text_sequence(observation_plan_payload.get("abort_conditions", ()), "abort_conditions"),
        notes=observation_plan_payload.get("notes"),
    )
    rollback_plan = RollbackPlan(
        rollback_required=rollback_plan_payload.get("rollback_required"),
        trigger_conditions=_text_sequence(rollback_plan_payload.get("trigger_conditions", ()), "trigger_conditions"),
        operator_actions=_text_sequence(rollback_plan_payload.get("operator_actions", ()), "operator_actions"),
        verification_checks=_text_sequence(rollback_plan_payload.get("verification_checks", ()), "verification_checks"),
    )
    return ResearchRecommendation(
        recommendation_id=_require_text(payload, "recommendation_id"),
        candidate_id=_require_text(payload, "candidate_id"),
        experiment_id=_require_text(payload, "experiment_id"),
        status=_require_text(payload, "status"),
        evidence=evidence,
        observation_plan=observation_plan,
        rollback_plan=rollback_plan,
        created_at=_parse_datetime(payload.get("created_at"), "recommendation.created_at"),
        runtime_mutation_allowed=payload.get("runtime_mutation_allowed", False),
        operator_approval_required=payload.get("operator_approval_required", True),
        recommended_next_step=_require_text(payload, "recommended_next_step"),
        notes=_text_sequence(payload.get("notes", ()), "recommendation.notes"),
    )


def _load_evidence_bundle(path: Path) -> EvidenceBundle:
    payload = _load_json_mapping(path, "evidence_bundle")
    execution_payload = payload.get("execution_evidence")
    return EvidenceBundle(
        dataset_quality=_dataset_quality_from_mapping(
            _require_mapping(payload.get("dataset_quality"), "dataset_quality")
        ),
        source_integrity=_source_integrity_from_mapping(
            _require_mapping(payload.get("source_integrity"), "source_integrity")
        ),
        execution_evidence=(
            _execution_evidence_from_mapping(_require_mapping(execution_payload, "execution_evidence"))
            if execution_payload is not None
            else None
        ),
        execution_evidence_required=bool(payload.get("execution_evidence_required")),
        passed=bool(payload.get("passed")),
        failures=_text_sequence(payload.get("failures", ()), "evidence_bundle.failures"),
        created_at=_parse_datetime(payload.get("created_at"), "evidence_bundle.created_at"),
    )


def _dataset_quality_from_mapping(payload: Mapping[str, Any]) -> DatasetQualityReport:
    thresholds = _require_mapping(payload.get("thresholds"), "dataset_quality.thresholds")
    return DatasetQualityReport(
        dataset_id=_require_text(payload, "dataset_id"),
        dataset_fingerprint=_require_text(payload, "dataset_fingerprint"),
        timeframe=_require_text(payload, "timeframe"),
        window_start=_parse_datetime(payload.get("window_start"), "dataset_quality.window_start"),
        window_end=_parse_datetime(payload.get("window_end"), "dataset_quality.window_end"),
        row_count=int(payload.get("row_count")),
        expected_bar_count=int(payload.get("expected_bar_count")),
        expected_interval_seconds=float(payload.get("expected_interval_seconds")),
        missing_bar_count=int(payload.get("missing_bar_count")),
        bar_gap_ratio=float(payload.get("bar_gap_ratio")),
        max_gap_seconds=float(payload.get("max_gap_seconds")),
        funding_missing_count=int(payload.get("funding_missing_count")),
        funding_missing_ratio=float(payload.get("funding_missing_ratio")),
        segment_row_counts=_require_mapping(payload.get("segment_row_counts"), "segment_row_counts"),
        thresholds=DatasetQualityThresholds(**thresholds),
        passed=bool(payload.get("passed")),
        failures=_text_sequence(payload.get("failures", ()), "dataset_quality.failures"),
        created_at=_parse_datetime(payload.get("created_at"), "dataset_quality.created_at"),
    )


def _source_integrity_from_mapping(payload: Mapping[str, Any]) -> SourceIntegrityReport:
    return SourceIntegrityReport(
        dataset_id=_require_text(payload, "dataset_id"),
        source_candle_dataset_versions=_text_sequence(payload.get("source_candle_dataset_versions", ()), "candle_versions"),
        source_funding_dataset_versions=_text_sequence(payload.get("source_funding_dataset_versions", ()), "funding_versions"),
        build_run_ids=_text_sequence(payload.get("build_run_ids", ()), "build_run_ids"),
        source_watermark=_require_mapping(payload.get("source_watermark"), "source_watermark"),
        candle_version_consistent=bool(payload.get("candle_version_consistent")),
        funding_version_consistent=bool(payload.get("funding_version_consistent")),
        build_run_traceable=bool(payload.get("build_run_traceable")),
        build_run_consistent=bool(payload.get("build_run_consistent")),
        timestamp_timezone_assumption=_require_text(payload, "timestamp_timezone_assumption"),
        passed=bool(payload.get("passed")),
        failures=_text_sequence(payload.get("failures", ()), "source_integrity.failures"),
        created_at=_parse_datetime(payload.get("created_at"), "source_integrity.created_at"),
    )


def _execution_evidence_from_mapping(payload: Mapping[str, Any]) -> ExecutionEvidenceReport:
    return ExecutionEvidenceReport(
        dataset_id=_require_text(payload, "dataset_id"),
        evidence_ref=_require_text(payload, "evidence_ref"),
        contract_schema_version=payload.get("contract_schema_version"),
        source_run_id=payload.get("source_run_id"),
        symbol=payload.get("symbol"),
        timeframe=payload.get("timeframe"),
        window_start=_parse_optional_datetime(payload.get("window_start"), "execution_evidence.window_start"),
        window_end=_parse_optional_datetime(payload.get("window_end"), "execution_evidence.window_end"),
        dataset_fingerprint=payload.get("dataset_fingerprint"),
        dataset_fingerprint_compatible=bool(payload.get("dataset_fingerprint_compatible")),
        compatibility_reason=payload.get("compatibility_reason"),
        passed=bool(payload.get("passed")),
        failures=_text_sequence(payload.get("failures", ()), "execution_evidence.failures"),
        created_at=_parse_datetime(payload.get("created_at"), "execution_evidence.created_at"),
    )


def _candidate_gate_from_mapping(payload: Mapping[str, Any]) -> CandidateGateResult:
    return CandidateGateResult(
        passed=bool(payload.get("passed")),
        failures=_text_sequence(payload.get("failures", ()), "candidate_gate.failures"),
        thresholds=_require_mapping(payload.get("thresholds"), "candidate_gate.thresholds"),
        critical_metrics=_text_sequence(payload.get("critical_metrics", ()), "candidate_gate.critical_metrics"),
        evaluated_at=_parse_datetime(payload.get("evaluated_at"), "candidate_gate.evaluated_at"),
    )


def _metrics_from_mapping(payload: Mapping[str, Any]) -> MetricsSnapshot:
    metrics_payload = payload.get("metrics") if isinstance(payload.get("metrics"), Mapping) else payload
    values = {field_name: metrics_payload.get(field_name) for field_name in METRIC_FIELDS}
    return MetricsSnapshot(
        **values,
        missing_reasons=_require_mapping(metrics_payload.get("missing_reasons", {}), "metrics.missing_reasons"),
    )


def _research_profile(config: ResearchFactoryExperimentConfig) -> Any:
    from aats.data_platform.research_factory.profiles import resolve_research_profile

    profile = resolve_research_profile(config.research_profile)
    if profile is None:
        raise ValueError("research governance workflow requires an explicit research_profile")
    return profile


def _profile_name(config: ResearchFactoryExperimentConfig) -> str:
    return _research_profile(config).name


def _failed_workflow_id(config: ResearchGovernanceWorkflowConfig) -> str:
    timestamp = config.timestamp.strftime("%Y%m%dT%H%M%SZ")
    seed = "|".join(
        (
            config.experiment_config.experiment_id or "",
            config.experiment_config.symbol,
            config.experiment_config.timeframe,
            str(config.observation_summary_path),
            timestamp,
        )
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]
    return f"wf_failed_{timestamp}_{digest}"


def _workflow_root(config: ResearchGovernanceWorkflowConfig, research_factory_root: Path) -> Path:
    if config.workflow_root is not None:
        return _require_research_artifact_directory(config.workflow_root)
    return research_factory_root / "workflows"


def _research_factory_root(experiment_root: str | Path) -> Path:
    root = _require_research_artifact_directory(experiment_root)
    if root.name == "experiments":
        return root.parent
    return root


def _require_research_artifact_directory(value: str | Path) -> Path:
    path = Path(value)
    if ".." in path.parts:
        raise ValueError("artifact directory must not contain path traversal")
    if not any(path.parts[index] == "artifacts" and path.parts[index + 1] == "research" for index in range(len(path.parts) - 1)):
        raise ValueError("artifact directory must be under artifacts/research")
    return path


def _load_json_mapping(path: Path, field_name: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"{field_name} must be a JSON object")
    return payload


def _optional_json_mapping(path: Path) -> Mapping[str, Any] | None:
    if not path.exists() or not path.is_file():
        return None
    return _load_json_mapping(path, path.name)


def _require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    return value


def _require_text(payload: Mapping[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _text_sequence(value: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be a sequence")
    return tuple(str(item) for item in value)


def _parse_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be an ISO datetime string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO datetime string") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _parse_optional_datetime(value: Any, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _parse_datetime(value, field_name)


def _require_timezone_aware_datetime(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _require_safe_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if "/" in value or "\\" in value or value in {".", ".."} or ".." in value:
        raise ValueError(f"{field_name} must not contain path traversal or separators")
    return value


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {field.name: _to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, datetime):
        _require_timezone_aware_datetime(value, "datetime")
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


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized_payload = {str(key): _to_jsonable(value) for key, value in payload.items()}
    rendered = json.dumps(normalized_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
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


def _write_text_atomic(path: Path, text: str) -> None:
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
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
