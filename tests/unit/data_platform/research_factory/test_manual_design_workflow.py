import json
import shutil
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aats.data_platform.research_factory.manual_design_workflow import (
    ManualDesignWorkflowConfig,
    run_manual_design_workflow,
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
    path = Path(".pytest_workspace_tmp") / f"research_factory_manual_design_workflow_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def dt(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 1, day, hour, tzinfo=UTC)


def research_factory_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "research" / "research_factory"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


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


def approved_preapply_chain():
    package = ready_preapply_package()
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
    return package, review, decision


def rejected_preapply_chain() -> tuple[PreApplyEvidencePackage, object, PreApplyReviewDecision]:
    package = ready_preapply_package()
    review = build_preapply_review(
        package,
        reference_integrity_ref="evidence_reference_integrity_report.json",
        reference_integrity_passed=True,
        created_at=dt(14),
    )
    decision = build_preapply_review_decision(
        review=review,
        package=package,
        decision="review_rejected",
        rationale="manual review rejected this package",
        reviewed_by="operator_reviewer",
        reviewed_at=dt(14, 1),
        required_followups=("archive this preapply evidence package",),
    )
    return package, review, decision


def workflow_config(
    root: Path,
    *,
    workflow_id: str = "manual_design_wf_success",
    factor_expression: str = "Return(close, 1)",
    preapply_decision: PreApplyReviewDecision | None = None,
    manual_design_review_decision: str | None = None,
    dry_run_success_criteria: tuple[str, ...] = ("positive cost-adjusted edge remains observable",),
    dry_run_abort_conditions: tuple[str, ...] = ("cost-adjusted edge turns negative",),
) -> ManualDesignWorkflowConfig:
    package, review, decision = approved_preapply_chain()
    return ManualDesignWorkflowConfig(
        preapply_package=package,
        preapply_review=review,
        preapply_review_decision=preapply_decision or decision,
        candidate_type="factor",
        proposed_change_summary="prepare a separate manual design draft for this factor candidate",
        parameter_or_config_delta={
            "scope": "candidate configuration draft",
            "factor_expression": factor_expression,
            "max_position_multiplier": 0.75,
            "dry_run_only": True,
        },
        affected_runtime_components=("decision_engine_research_config",),
        required_risk_guards=(
            "position_limit_guard",
            "drawdown_guard",
            "paper_only_guard",
            "rollback_guard",
        ),
        required_dry_run_checks=("paper_replay_validation", "operator_review_checklist"),
        rollback_plan_ref="research_recommendation.json",
        dry_run_target_environment="paper",
        dry_run_scope="paper-only replay of the candidate design",
        dry_run_success_criteria=dry_run_success_criteria,
        dry_run_abort_conditions=dry_run_abort_conditions,
        research_factory_root=root,
        workflow_id=workflow_id,
        manual_design_review_decision=manual_design_review_decision,
        timestamp=dt(15),
    )


def test_manual_design_workflow_creates_design_review_and_dry_run_plan(
    workspace_tmp_path: Path,
) -> None:
    root = research_factory_root(workspace_tmp_path)

    result = run_manual_design_workflow(workflow_config(root))

    assert result.status == "dry_run_plan_ready_for_review"
    assert result.design_validation_passed is True
    assert result.design_review_decision == "design_ready_for_dry_run_planning"
    assert result.dry_run_plan_validation_passed is True
    summary = read_json(root / "manual_design_workflows" / result.workflow_id / "manual_design_workflow_summary.json")
    design = read_json(root / "manual_apply_designs" / result.design_id / "manual_apply_design_package.json")
    design_review_decision = read_json(
        root
        / "manual_apply_design_reviews"
        / result.design_review_id
        / "manual_apply_design_review_decision.json"
    )
    dry_run_plan = read_json(root / "dry_run_plans" / result.dry_run_plan_id / "dry_run_plan_package.json")
    dry_run_validation = read_json(
        root / "dry_run_plans" / result.dry_run_plan_id / "dry_run_plan_validation_report.json"
    )
    manifest = read_json(
        root / "manual_design_workflows" / result.workflow_id / "manual_design_workflow_manifest.json"
    )

    assert summary["status"] == "dry_run_plan_ready_for_review"
    assert summary["runtime_mutation_allowed"] is False
    assert summary["next_step"] == "operator_review_dry_run_plan_evidence"
    assert summary["artifact_refs"]["dry_run_plan_package"] == (
        f"dry_run_plans/{result.dry_run_plan_id}/dry_run_plan_package.json"
    )
    assert [stage["stage_name"] for stage in summary["stage_results"]] == [
        "manual_apply_design",
        "manual_apply_design_validation",
        "manual_apply_design_review",
        "dry_run_plan",
        "manual_design_workflow_summary",
    ]
    assert all(stage["runtime_mutation_allowed"] is False for stage in summary["stage_results"])
    assert design["runtime_mutation_allowed"] is False
    assert design_review_decision["decision"] == "design_ready_for_dry_run_planning"
    assert dry_run_plan["runtime_mutation_allowed"] is False
    assert dry_run_validation["passed"] is True
    assert manifest["artifact_type"] == "workflow"
    assert manifest["output_refs"]["manual_design_workflow_summary"] == "manual_design_workflow_summary.json"


def test_manual_design_workflow_rejects_non_approved_preapply_decision(
    workspace_tmp_path: Path,
) -> None:
    root = research_factory_root(workspace_tmp_path)
    package, review, decision = rejected_preapply_chain()
    config = workflow_config(
        root,
        workflow_id="manual_design_wf_bad_preapply_decision",
        preapply_decision=decision,
    )
    config = ManualDesignWorkflowConfig(
        **{
            **{field: getattr(config, field) for field in config.__dataclass_fields__},
            "preapply_package": package,
            "preapply_review": review,
            "preapply_review_decision": decision,
        }
    )

    result = run_manual_design_workflow(config)

    assert result.status == "failed"
    assert "approved manual design review decision" in result.error
    summary = read_json(root / "manual_design_workflows" / result.workflow_id / "manual_design_workflow_summary.json")
    assert summary["failed_stage"] == "manual_apply_design"
    assert not (root / "dry_run_plans").exists()


def test_manual_design_workflow_validation_failure_stops_before_dry_run_plan(
    workspace_tmp_path: Path,
) -> None:
    root = research_factory_root(workspace_tmp_path)

    result = run_manual_design_workflow(
        workflow_config(
            root,
            workflow_id="manual_design_wf_needs_revision",
            factor_expression="",
        )
    )

    assert result.status == "needs_design_revision"
    assert result.design_validation_passed is False
    assert result.dry_run_plan_id is None
    summary = read_json(
        root / "manual_design_workflows" / result.workflow_id / "manual_design_workflow_summary.json"
    )
    validation_report = read_json(
        root
        / "manual_apply_design_reviews"
        / result.design_review_id
        / "manual_apply_design_validation_report.json"
    )

    assert "factor design requires parameter_or_config_delta.factor_expression" in validation_report["failures"]
    assert summary["failed_stage"] == "manual_apply_design_review"
    assert summary["risk_flags"] == [
        "manual_design_validation_failed",
        "manual_design_review_needs_design_revision",
    ]
    assert not (root / "dry_run_plans").exists()


def test_manual_design_workflow_explicit_rejected_review_stops_before_dry_run_plan(
    workspace_tmp_path: Path,
) -> None:
    root = research_factory_root(workspace_tmp_path)

    result = run_manual_design_workflow(
        workflow_config(
            root,
            workflow_id="manual_design_wf_rejected",
            manual_design_review_decision="design_rejected",
        )
    )

    assert result.status == "design_rejected"
    assert result.design_validation_passed is True
    assert result.dry_run_plan_id is None
    summary = read_json(root / "manual_design_workflows" / result.workflow_id / "manual_design_workflow_summary.json")
    assert summary["failed_stage"] == "manual_apply_design_review"
    assert summary["artifact_refs"]["manual_apply_design_review_decision"] == (
        f"manual_apply_design_reviews/{result.design_review_id}/manual_apply_design_review_decision.json"
    )
    assert not (root / "dry_run_plans").exists()


def test_manual_design_workflow_dry_run_validation_failure_records_plan(
    workspace_tmp_path: Path,
) -> None:
    root = research_factory_root(workspace_tmp_path)

    result = run_manual_design_workflow(
        workflow_config(
            root,
            workflow_id="manual_design_wf_bad_dry_run_plan",
            dry_run_success_criteria=(),
        )
    )

    assert result.status == "dry_run_plan_rejected"
    assert result.dry_run_plan_validation_passed is False
    summary = read_json(root / "manual_design_workflows" / result.workflow_id / "manual_design_workflow_summary.json")
    dry_run_validation = read_json(
        root / "dry_run_plans" / result.dry_run_plan_id / "dry_run_plan_validation_report.json"
    )
    assert "success_criteria must not be empty" in dry_run_validation["failures"]
    assert summary["failed_stage"] == "dry_run_plan"
    assert summary["blocking_artifact"] == (
        f"dry_run_plans/{result.dry_run_plan_id}/dry_run_plan_validation_report.json"
    )


def test_manual_design_workflow_root_must_be_under_research_artifacts(
    workspace_tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="under artifacts/research"):
        workflow_config(workspace_tmp_path / "configs")


def test_manual_design_workflow_duplicate_workflow_id_fails_closed_without_overwrite(
    workspace_tmp_path: Path,
) -> None:
    root = research_factory_root(workspace_tmp_path)
    first = run_manual_design_workflow(workflow_config(root))
    summary_path = root / "manual_design_workflows" / first.workflow_id / "manual_design_workflow_summary.json"
    original_summary = summary_path.read_text(encoding="utf-8")

    duplicate = run_manual_design_workflow(workflow_config(root))

    assert duplicate.status == "failed"
    assert duplicate.workflow_id == first.workflow_id
    assert duplicate.workflow_dir == (root / "manual_design_workflows" / first.workflow_id).as_posix()
    assert "already exists" in str(duplicate.error)
    assert summary_path.read_text(encoding="utf-8") == original_summary
