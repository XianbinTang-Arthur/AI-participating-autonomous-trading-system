import json
import shutil
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aats.data_platform.research_factory.manual_apply_design import (
    ManualApplyDesignPackage,
    ManualApplyDesignRecorder,
    build_manual_apply_design_package,
)
from aats.data_platform.research_factory.preapply import (
    PreApplyEvidencePackage,
    PreApplyReviewDecision,
    build_preapply_review,
    build_preapply_review_decision,
)

UTC = timezone.utc


@pytest.fixture
def workspace_tmp_path() -> Iterator[Path]:
    path = Path(".pytest_workspace_tmp") / f"research_factory_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def dt(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 1, day, hour, tzinfo=UTC)


def manual_design_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "research" / "research_factory" / "manual_apply_designs"


def evidence_refs() -> dict[str, str]:
    return {
        "candidate_artifact": "candidate_artifact.json",
        "research_recommendation": "research_recommendation.json",
        "metrics_snapshot": "metrics_snapshot.json",
        "dataset_quality_report": "dataset_quality_report.json",
        "source_integrity_report": "source_integrity_report.json",
        "execution_evidence_report": "execution_evidence_report.json",
        "evidence_bundle": "evidence_bundle.json",
        "observation_result": "observation_result.json",
        "review_outcome": "review_outcome.json",
        "rollback_plan": "research_recommendation.json",
    }


def gate_refs() -> dict[str, str]:
    return {
        "candidate_gate": "candidate_artifact.json",
        "observation_gate_result": "observation_gate_result.json",
    }


def ready_preapply_package() -> PreApplyEvidencePackage:
    return PreApplyEvidencePackage(
        package_id="preapply_obs_001",
        candidate_id="cand_001",
        recommendation_id="rec_001",
        observation_id="obs_001",
        experiment_id="exp_001",
        status="preapply_ready",
        evidence_refs=evidence_refs(),
        gate_refs=gate_refs(),
        review_decision="eligible_for_preapply",
        candidate_gate_passed=True,
        evidence_bundle_passed=True,
        observation_gate_passed=True,
        created_at=dt(13),
    )


def approved_review_and_decision(package: PreApplyEvidencePackage):
    review = build_preapply_review(
        package,
        reference_integrity_ref="evidence_reference_integrity_report.json",
        reference_integrity_passed=True,
        created_at=dt(14),
    )
    decision = build_preapply_review_decision(
        review=review,
        package=package,
        decision="review_approved_for_manual_apply_design",
        rationale="evidence is complete enough for a separate manual design review",
        reviewed_by="operator_reviewer",
        reviewed_at=dt(14, 1),
    )
    return review, decision


def design_delta() -> dict[str, object]:
    return {
        "scope": "candidate configuration draft",
        "factor_expression": "Return(close, 1)",
        "max_position_multiplier": 0.75,
        "dry_run_only": True,
    }


def build_ready_design() -> ManualApplyDesignPackage:
    package = ready_preapply_package()
    review, decision = approved_review_and_decision(package)
    return build_manual_apply_design_package(
        preapply_package=package,
        preapply_review=review,
        preapply_review_decision=decision,
        candidate_type="factor",
        proposed_change_summary="prepare a separate manual design draft for this factor candidate",
        parameter_or_config_delta=design_delta(),
        affected_runtime_components=("decision_engine_research_config",),
        required_risk_guards=("position_limit_guard", "drawdown_guard"),
        required_dry_run_checks=("paper_replay_validation", "operator_review_checklist"),
        rollback_plan_ref="research_recommendation.json",
        created_at=dt(15),
    )


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_build_manual_apply_design_package_from_approved_preapply_review() -> None:
    design = build_ready_design()

    assert design.status == "design_draft"
    assert design.source_preapply_review_id == "review_preapply_obs_001"
    assert design.source_preapply_package_id == "preapply_obs_001"
    assert design.candidate_id == "cand_001"
    assert design.candidate_type == "factor"
    assert design.runtime_mutation_allowed is False
    assert design.operator_approval_required is True
    assert design.evidence_refs["preapply_review_decision"] == "preapply_review_decision.json"
    assert design.rollback_plan_ref == "research_recommendation.json"
    assert design.recommended_next_step == "submit_manual_apply_design_for_separate_governance_review"


def test_manual_apply_design_requires_approved_review_decision() -> None:
    package = ready_preapply_package()
    review = build_preapply_review(package, created_at=dt(14))
    rejected_decision = build_preapply_review_decision(
        review=review,
        package=package,
        decision="review_rejected",
        rationale="manual review rejected this package",
        reviewed_by="operator_reviewer",
        reviewed_at=dt(14, 1),
    )

    with pytest.raises(ValueError, match="approved manual design review decision"):
        build_manual_apply_design_package(
            preapply_package=package,
            preapply_review=review,
            preapply_review_decision=rejected_decision,
            candidate_type="factor",
            proposed_change_summary="prepare a manual design draft",
            parameter_or_config_delta=design_delta(),
            affected_runtime_components=("decision_engine_research_config",),
            required_risk_guards=("position_limit_guard",),
            required_dry_run_checks=("paper_replay_validation",),
            rollback_plan_ref="research_recommendation.json",
            created_at=dt(15),
        )


def test_manual_apply_design_requires_reference_integrity() -> None:
    package = ready_preapply_package()
    review = build_preapply_review(package, created_at=dt(14))
    decision = PreApplyReviewDecision(
        review_id=review.review_id,
        package_id=package.package_id,
        candidate_id=package.candidate_id,
        recommendation_id=package.recommendation_id,
        observation_id=package.observation_id,
        experiment_id=package.experiment_id,
        decision="review_approved_for_manual_apply_design",
        rationale="synthetic approval object without integrity report",
        reviewed_by="operator_reviewer",
        reviewed_at=dt(14, 1),
    )

    with pytest.raises(ValueError, match="requires reference integrity report"):
        build_manual_apply_design_package(
            preapply_package=package,
            preapply_review=review,
            preapply_review_decision=decision,
            candidate_type="factor",
            proposed_change_summary="prepare a manual design draft",
            parameter_or_config_delta=design_delta(),
            affected_runtime_components=("decision_engine_research_config",),
            required_risk_guards=("position_limit_guard",),
            required_dry_run_checks=("paper_replay_validation",),
            rollback_plan_ref="research_recommendation.json",
            created_at=dt(15),
        )


def test_manual_apply_design_rejects_runtime_promotion_text() -> None:
    design = build_ready_design()

    with pytest.raises(ValueError, match="runtime promotion term"):
        ManualApplyDesignPackage(
            design_id="manual_design_bad_summary",
            source_preapply_review_id=design.source_preapply_review_id,
            source_preapply_package_id=design.source_preapply_package_id,
            candidate_id=design.candidate_id,
            recommendation_id=design.recommendation_id,
            observation_id=design.observation_id,
            experiment_id=design.experiment_id,
            candidate_type=design.candidate_type,
            status=design.status,
            proposed_change_summary="direct_apply this candidate",
            parameter_or_config_delta=design.parameter_or_config_delta,
            affected_runtime_components=design.affected_runtime_components,
            required_risk_guards=design.required_risk_guards,
            required_dry_run_checks=design.required_dry_run_checks,
            rollback_plan_ref=design.rollback_plan_ref,
            evidence_refs=design.evidence_refs,
        )

    with pytest.raises(ValueError, match="runtime promotion term"):
        ManualApplyDesignPackage(
            design_id="manual_design_bad_delta",
            source_preapply_review_id=design.source_preapply_review_id,
            source_preapply_package_id=design.source_preapply_package_id,
            candidate_id=design.candidate_id,
            recommendation_id=design.recommendation_id,
            observation_id=design.observation_id,
            experiment_id=design.experiment_id,
            candidate_type=design.candidate_type,
            status=design.status,
            proposed_change_summary=design.proposed_change_summary,
            parameter_or_config_delta={"active_parameter": "should not be written"},
            affected_runtime_components=design.affected_runtime_components,
            required_risk_guards=design.required_risk_guards,
            required_dry_run_checks=design.required_dry_run_checks,
            rollback_plan_ref=design.rollback_plan_ref,
            evidence_refs=design.evidence_refs,
        )

    with pytest.raises(ValueError, match="must not allow runtime mutation"):
        ManualApplyDesignPackage(
            design_id="manual_design_flag_check",
            source_preapply_review_id=design.source_preapply_review_id,
            source_preapply_package_id=design.source_preapply_package_id,
            candidate_id=design.candidate_id,
            recommendation_id=design.recommendation_id,
            observation_id=design.observation_id,
            experiment_id=design.experiment_id,
            candidate_type=design.candidate_type,
            status=design.status,
            proposed_change_summary=design.proposed_change_summary,
            parameter_or_config_delta=design.parameter_or_config_delta,
            affected_runtime_components=design.affected_runtime_components,
            required_risk_guards=design.required_risk_guards,
            required_dry_run_checks=design.required_dry_run_checks,
            rollback_plan_ref=design.rollback_plan_ref,
            evidence_refs=design.evidence_refs,
            runtime_mutation_allowed=True,
        )


def test_manual_apply_design_delta_must_be_json_safe_and_finite() -> None:
    design = build_ready_design()

    with pytest.raises(ValueError, match="finite numbers"):
        ManualApplyDesignPackage(
            design_id="manual_design_bad_number",
            source_preapply_review_id=design.source_preapply_review_id,
            source_preapply_package_id=design.source_preapply_package_id,
            candidate_id=design.candidate_id,
            recommendation_id=design.recommendation_id,
            observation_id=design.observation_id,
            experiment_id=design.experiment_id,
            candidate_type=design.candidate_type,
            status=design.status,
            proposed_change_summary=design.proposed_change_summary,
            parameter_or_config_delta={"max_position_multiplier": float("inf")},
            affected_runtime_components=design.affected_runtime_components,
            required_risk_guards=design.required_risk_guards,
            required_dry_run_checks=design.required_dry_run_checks,
            rollback_plan_ref=design.rollback_plan_ref,
            evidence_refs=design.evidence_refs,
        )

    with pytest.raises(TypeError, match="unsupported JSON value"):
        ManualApplyDesignPackage(
            design_id="manual_design_bad_object",
            source_preapply_review_id=design.source_preapply_review_id,
            source_preapply_package_id=design.source_preapply_package_id,
            candidate_id=design.candidate_id,
            recommendation_id=design.recommendation_id,
            observation_id=design.observation_id,
            experiment_id=design.experiment_id,
            candidate_type=design.candidate_type,
            status=design.status,
            proposed_change_summary=design.proposed_change_summary,
            parameter_or_config_delta={"object": object()},
            affected_runtime_components=design.affected_runtime_components,
            required_risk_guards=design.required_risk_guards,
            required_dry_run_checks=design.required_dry_run_checks,
            rollback_plan_ref=design.rollback_plan_ref,
            evidence_refs=design.evidence_refs,
        )


def test_manual_apply_design_requires_all_evidence_refs() -> None:
    design = build_ready_design()
    refs = dict(design.evidence_refs)
    del refs["preapply_review_decision"]

    with pytest.raises(ValueError, match="missing required ref: preapply_review_decision"):
        ManualApplyDesignPackage(
            design_id="manual_design_missing_ref",
            source_preapply_review_id=design.source_preapply_review_id,
            source_preapply_package_id=design.source_preapply_package_id,
            candidate_id=design.candidate_id,
            recommendation_id=design.recommendation_id,
            observation_id=design.observation_id,
            experiment_id=design.experiment_id,
            candidate_type=design.candidate_type,
            status=design.status,
            proposed_change_summary=design.proposed_change_summary,
            parameter_or_config_delta=design.parameter_or_config_delta,
            affected_runtime_components=design.affected_runtime_components,
            required_risk_guards=design.required_risk_guards,
            required_dry_run_checks=design.required_dry_run_checks,
            rollback_plan_ref=design.rollback_plan_ref,
            evidence_refs=refs,
        )


def test_manual_apply_design_recorder_writes_package_and_manifest(workspace_tmp_path: Path) -> None:
    root = manual_design_root(workspace_tmp_path)
    recorder = ManualApplyDesignRecorder(root, code_version="test-sha", clock=lambda: dt(15, 1))
    design = build_ready_design()

    manifest = recorder.record_package(design)

    design_dir = root / design.design_id
    stored_design = read_json(design_dir / "manual_apply_design_package.json")
    stored_manifest = read_json(design_dir / "manual_apply_design_manifest.json")
    assert manifest["artifact_type"] == "manual_apply_design"
    assert manifest["status"] == "succeeded"
    assert stored_manifest["output_refs"]["manual_apply_design_package"] == (
        "manual_apply_design_package.json"
    )
    assert stored_manifest["input_refs"]["source_preapply_review_id"] == (
        design.source_preapply_review_id
    )
    assert stored_design["status"] == "design_draft"
    assert stored_design["runtime_mutation_allowed"] is False
    assert stored_design["operator_approval_required"] is True

    with pytest.raises(ValueError, match="already exists"):
        recorder.record_package(design)


def test_manual_apply_design_recorder_root_must_be_under_research_artifacts(
    workspace_tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="under artifacts/research"):
        ManualApplyDesignRecorder(workspace_tmp_path / "configs")
