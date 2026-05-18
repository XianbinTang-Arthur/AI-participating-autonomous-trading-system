import json
import shutil
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aats.data_platform.research_factory.dry_run_planning import (
    DryRunPlanPackage,
    DryRunPlanRecorder,
    DryRunPlanReviewDecision,
    build_dry_run_plan_package,
    build_dry_run_plan_review_decision,
    validate_dry_run_plan,
)
from aats.data_platform.research_factory.manual_apply_design import (
    ManualApplyDesignPackage,
    ManualApplyDesignReview,
    ManualApplyDesignReviewDecision,
    ManualApplyDesignValidationReport,
    build_manual_apply_design_package,
    build_manual_apply_design_review,
    build_manual_apply_design_review_decision,
    validate_manual_apply_design_domain,
)
from aats.data_platform.research_factory.preapply import (
    PreApplyEvidencePackage,
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


def dry_run_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "research" / "research_factory" / "dry_run_plans"


def ready_preapply_package() -> PreApplyEvidencePackage:
    return PreApplyEvidencePackage(
        package_id="preapply_obs_001",
        candidate_id="cand_001",
        recommendation_id="rec_001",
        observation_id="obs_001",
        experiment_id="exp_001",
        status="preapply_ready",
        evidence_refs={
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
        },
        gate_refs={
            "candidate_gate": "candidate_artifact.json",
            "observation_gate_result": "observation_gate_result.json",
        },
        review_decision="eligible_for_preapply",
        candidate_gate_passed=True,
        evidence_bundle_passed=True,
        observation_gate_passed=True,
        created_at=dt(13),
    )


def ready_manual_design_chain() -> tuple[
    ManualApplyDesignPackage,
    ManualApplyDesignValidationReport,
    ManualApplyDesignReview,
    ManualApplyDesignReviewDecision,
]:
    package = ready_preapply_package()
    preapply_review = build_preapply_review(
        package,
        reference_integrity_ref="evidence_reference_integrity_report.json",
        reference_integrity_passed=True,
        created_at=dt(14),
    )
    preapply_decision = build_preapply_review_decision(
        review=preapply_review,
        package=package,
        decision="review_approved_for_manual_apply_design",
        rationale="evidence is complete enough for a separate manual design review",
        reviewed_by="operator_reviewer",
        reviewed_at=dt(14, 1),
    )
    design = build_manual_apply_design_package(
        preapply_package=package,
        preapply_review=preapply_review,
        preapply_review_decision=preapply_decision,
        candidate_type="factor",
        proposed_change_summary="prepare a separate manual design draft for this factor candidate",
        parameter_or_config_delta={
            "scope": "candidate configuration draft",
            "factor_expression": "Return(close, 1)",
            "max_position_multiplier": 0.75,
            "dry_run_only": True,
        },
        affected_runtime_components=("decision_engine_research_config",),
        required_risk_guards=("position_limit_guard", "drawdown_guard"),
        required_dry_run_checks=("paper_replay_validation", "operator_review_checklist"),
        rollback_plan_ref="research_recommendation.json",
        created_at=dt(15),
    )
    validation = validate_manual_apply_design_domain(design, evaluated_at=dt(16))
    design_review = build_manual_apply_design_review(
        design,
        validation_report=validation,
        created_at=dt(16, 1),
    )
    design_decision = build_manual_apply_design_review_decision(
        review=design_review,
        design=design,
        validation_report=validation,
        decision="design_ready_for_dry_run_planning",
        rationale="design has enough evidence to prepare dry-run planning",
        reviewed_by="operator_reviewer",
        reviewed_at=dt(16, 2),
    )
    return design, validation, design_review, design_decision


def dry_run_inputs() -> dict[str, str]:
    return {
        "manual_apply_design_package": "manual_apply_design_package.json",
        "manual_apply_design_review": "manual_apply_design_review.json",
        "manual_apply_design_review_decision": "manual_apply_design_review_decision.json",
        "manual_apply_design_validation_report": "manual_apply_design_validation_report.json",
        "rollback_plan": "research_recommendation.json",
    }


def build_ready_plan() -> DryRunPlanPackage:
    design, validation, review, decision = ready_manual_design_chain()
    return build_dry_run_plan_package(
        design=design,
        design_validation=validation,
        design_review=review,
        design_review_decision=decision,
        target_environment="paper",
        dry_run_scope="paper-only replay of the candidate design",
        expected_runtime_components=("decision_engine_research_config",),
        required_input_artifacts=dry_run_inputs(),
        required_risk_guards=("position_limit_guard", "drawdown_guard"),
        rollback_plan_ref="research_recommendation.json",
        success_criteria=("positive cost-adjusted edge remains observable",),
        abort_conditions=("cost-adjusted edge turns negative",),
        created_at=dt(17),
    )


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_build_dry_run_plan_from_ready_manual_design_review() -> None:
    plan = build_ready_plan()

    assert plan.status == "dry_run_plan_draft"
    assert plan.target_environment == "paper"
    assert plan.runtime_mutation_allowed is False
    assert plan.operator_approval_required is True
    assert plan.required_input_artifacts["manual_apply_design_review_decision"] == (
        "manual_apply_design_review_decision.json"
    )


def test_dry_run_plan_requires_ready_manual_design_decision() -> None:
    design, validation, review, _decision = ready_manual_design_chain()
    rejected = ManualApplyDesignReviewDecision(
        review_id=review.review_id,
        design_id=design.design_id,
        candidate_id=design.candidate_id,
        candidate_type=design.candidate_type,
        decision="design_rejected",
        rationale="reject design before dry-run planning",
        reviewed_by="operator_reviewer",
        reviewed_at=dt(16, 2),
        design_ref=review.design_ref,
        review_ref="manual_apply_design_review.json",
        validation_ref=review.validation_ref,
        validation_passed=validation.passed,
    )

    with pytest.raises(ValueError, match="design_ready_for_dry_run_planning"):
        build_dry_run_plan_package(
            design=design,
            design_validation=validation,
            design_review=review,
            design_review_decision=rejected,
            target_environment="paper",
            dry_run_scope="paper-only replay of the candidate design",
            expected_runtime_components=("decision_engine_research_config",),
            required_input_artifacts=dry_run_inputs(),
            required_risk_guards=("position_limit_guard", "drawdown_guard"),
            rollback_plan_ref="research_recommendation.json",
            success_criteria=("positive cost-adjusted edge remains observable",),
            abort_conditions=("cost-adjusted edge turns negative",),
        )


def test_dry_run_plan_validation_requires_success_criteria_and_abort_conditions() -> None:
    plan = build_ready_plan()
    invalid = DryRunPlanPackage(
        dry_run_plan_id="dry_run_plan_missing_criteria",
        source_manual_apply_design_review_id=plan.source_manual_apply_design_review_id,
        source_manual_apply_design_id=plan.source_manual_apply_design_id,
        candidate_id=plan.candidate_id,
        candidate_type=plan.candidate_type,
        target_environment=plan.target_environment,
        dry_run_scope=plan.dry_run_scope,
        expected_runtime_components=plan.expected_runtime_components,
        required_input_artifacts=plan.required_input_artifacts,
        required_risk_guards=plan.required_risk_guards,
        rollback_plan_ref=plan.rollback_plan_ref,
        success_criteria=(),
        abort_conditions=(),
        created_at=dt(17),
    )

    report = validate_dry_run_plan(invalid, evaluated_at=dt(17, 1))

    assert report.passed is False
    assert "success_criteria must not be empty" in report.failures
    assert "abort_conditions must not be empty" in report.failures


def test_dry_run_plan_review_decision_requires_passing_validation() -> None:
    plan = build_ready_plan()
    validation = validate_dry_run_plan(plan, evaluated_at=dt(17, 1))
    decision = build_dry_run_plan_review_decision(
        plan=plan,
        validation_report=validation,
        decision="dry_run_plan_ready_for_review",
        rationale="plan is complete enough for separate dry-run review",
        reviewed_by="operator_reviewer",
        reviewed_at=dt(17, 2),
    )

    assert decision.decision == "dry_run_plan_ready_for_review"
    assert decision.validation_passed is True
    assert decision.runtime_mutation_allowed is False
    assert decision.recommended_next_step == "submit_dry_run_plan_for_separate_execution_approval"

    failed_plan = DryRunPlanPackage(
        dry_run_plan_id="dry_run_plan_failed",
        source_manual_apply_design_review_id=plan.source_manual_apply_design_review_id,
        source_manual_apply_design_id=plan.source_manual_apply_design_id,
        candidate_id=plan.candidate_id,
        candidate_type=plan.candidate_type,
        target_environment=plan.target_environment,
        dry_run_scope=plan.dry_run_scope,
        expected_runtime_components=plan.expected_runtime_components,
        required_input_artifacts=plan.required_input_artifacts,
        required_risk_guards=plan.required_risk_guards,
        rollback_plan_ref=plan.rollback_plan_ref,
        success_criteria=(),
        abort_conditions=("cost-adjusted edge turns negative",),
        created_at=dt(17),
    )
    failed_validation = validate_dry_run_plan(failed_plan, evaluated_at=dt(17, 1))
    with pytest.raises(ValueError, match="passing validation"):
        build_dry_run_plan_review_decision(
            plan=failed_plan,
            validation_report=failed_validation,
            decision="dry_run_plan_ready_for_review",
            rationale="attempt to advance incomplete dry-run plan",
            reviewed_by="operator_reviewer",
            reviewed_at=dt(17, 2),
        )


def test_dry_run_plan_rejects_apply_language() -> None:
    with pytest.raises(ValueError, match="dry-run plan review decision must be one of"):
        DryRunPlanReviewDecision(
            dry_run_plan_id="dry_run_plan_1",
            candidate_id="cand_001",
            candidate_type="factor",
            decision="approved_for_apply",
            rationale="invalid decision",
            reviewed_by="operator_reviewer",
        )


def test_dry_run_plan_recorder_writes_plan_validation_and_manifest(
    workspace_tmp_path: Path,
) -> None:
    root = dry_run_root(workspace_tmp_path)
    recorder = DryRunPlanRecorder(root, code_version="test-sha", clock=lambda: dt(17, 1))
    plan = build_ready_plan()
    validation = validate_dry_run_plan(plan, evaluated_at=dt(17, 1))

    manifest = recorder.record_plan(plan, validation_report=validation)

    plan_dir = root / plan.dry_run_plan_id
    stored_plan = read_json(plan_dir / "dry_run_plan_package.json")
    stored_validation = read_json(plan_dir / "dry_run_plan_validation_report.json")
    stored_manifest = read_json(plan_dir / "dry_run_plan_manifest.json")
    assert manifest["artifact_type"] == "dry_run_plan"
    assert manifest["status"] == "succeeded"
    assert stored_manifest["output_refs"]["dry_run_plan_package"] == "dry_run_plan_package.json"
    assert stored_manifest["output_refs"]["dry_run_plan_validation_report"] == (
        "dry_run_plan_validation_report.json"
    )
    assert stored_plan["runtime_mutation_allowed"] is False
    assert stored_validation["passed"] is True

    with pytest.raises(ValueError, match="already exists"):
        recorder.record_plan(plan, validation_report=validation)


def test_dry_run_plan_recorder_root_must_be_under_research_artifacts(
    workspace_tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="under artifacts/research"):
        DryRunPlanRecorder(workspace_tmp_path / "configs")
