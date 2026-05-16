import json
import shutil
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aats.data_platform.research_factory.experiments.recorder import ExperimentRecorder
from aats.data_platform.research_factory.metrics.gates import (
    CandidateArtifact,
    evaluate_candidate_gate,
)
from aats.data_platform.research_factory.recommendations import (
    ResearchRecommendation,
    RollbackPlan,
    build_research_recommendation,
)
from aats.data_platform.research_factory.specs import (
    DatasetSpec,
    ExperimentSpec,
    FeatureSpec,
    LabelSpec,
    MetricsSnapshot,
    SegmentSpec,
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


def dt(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=UTC)


def segment(name: str, start_day: int, end_day: int) -> SegmentSpec:
    return SegmentSpec(name=name, start=dt(start_day), end=dt(end_day), purpose=f"{name} segment")


def artifact_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "research" / "research_factory" / "experiments"


def experiment_spec(root: Path, experiment_id: str = "exp_20260516_000003") -> ExperimentSpec:
    return ExperimentSpec(
        experiment_id=experiment_id,
        dataset=DatasetSpec(
            dataset_id="btc_15m_v1",
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            dataset_version="v1.0",
            window_start=dt(1),
            window_end=dt(10),
            segments=[
                segment("train", 1, 5),
                segment("valid", 5, 7),
                segment("test", 7, 10),
            ],
            source_refs={"gold": "gold.replay_bars"},
        ),
        features=[FeatureSpec(name="close_return_1", expression="Return(close, 1)")],
        label=LabelSpec(
            name="future_net_return_h4",
            horizon_bars=4,
            return_kind="simple_return",
            net_of_fee=True,
            net_of_slippage=True,
            include_funding=True,
            fee_bps=5.0,
            slippage_bps=2.0,
        ),
        model_ref="baseline_long_flat",
        metrics=["net_annualized_return", "max_drawdown", "cost_adjusted_edge_bps_mean"],
        artifact_root=str(root),
    )


def metrics_snapshot(
    *,
    fillable_ratio: float | None = 0.9,
    partial_fill_ratio: float | None = 0.05,
    missing_reasons: dict[str, str] | None = None,
) -> MetricsSnapshot:
    return MetricsSnapshot(
        ic=0.1,
        rank_ic=0.2,
        icir=0.3,
        rank_icir=0.4,
        annualized_return=0.05,
        net_annualized_return=0.03,
        information_ratio=0.7,
        sharpe=0.8,
        max_drawdown=0.1,
        turnover=0.2,
        fee_bps_mean=5.0,
        slippage_bps_mean=2.0,
        funding_bps_mean=0.5,
        fillable_ratio=fillable_ratio,
        partial_fill_ratio=partial_fill_ratio,
        cost_adjusted_edge_bps_mean=1.2,
        missing_reasons=missing_reasons or {},
    )


def candidate_artifact(*, execution_ref: str | None = None) -> CandidateArtifact:
    metrics = metrics_snapshot()
    gate = evaluate_candidate_gate(metrics, {"max_drawdown_limit": 0.2})
    payload = {
        "factor_expression": "Return(close, 1)",
        "dataset_fingerprint": "sha256:abc123",
        "benchmark_segment": "test",
        "generated_by": "unit_test",
        "research_only": True,
    }
    if execution_ref is not None:
        payload["execution_cost_summary_ref"] = execution_ref
    return CandidateArtifact(
        candidate_id="cand_20260516_000003",
        experiment_id="exp_20260516_000003",
        candidate_type="factor",
        payload=payload,
        metrics=metrics,
        gate=gate,
        created_at=dt(8),
    )


def evidence_refs() -> dict[str, str]:
    return {
        "candidate_artifact": "candidate_artifact.json",
        "experiment_manifest": "experiment_manifest.json",
        "metrics_snapshot": "metrics_snapshot.json",
    }


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_build_research_recommendation_from_candidate() -> None:
    candidate = candidate_artifact()

    recommendation = build_research_recommendation(
        candidate,
        evidence_refs=evidence_refs(),
        created_at=dt(9),
    )

    assert recommendation.status == "ready_for_review"
    assert recommendation.runtime_mutation_allowed is False
    assert recommendation.operator_approval_required is True
    assert recommendation.evidence.candidate_id == candidate.candidate_id
    assert recommendation.evidence.benchmark_segment == "test"
    assert recommendation.evidence.evidence_refs["candidate_artifact"] == "candidate_artifact.json"
    assert recommendation.observation_plan.mode == "shadow"
    assert recommendation.rollback_plan.rollback_required is True


def test_recommendation_rejects_runtime_mutation_flag() -> None:
    recommendation = build_research_recommendation(
        candidate_artifact(),
        evidence_refs=evidence_refs(),
        created_at=dt(9),
    )

    with pytest.raises(ValueError, match="must not allow runtime mutation"):
        ResearchRecommendation(
            recommendation_id=recommendation.recommendation_id,
            candidate_id=recommendation.candidate_id,
            experiment_id=recommendation.experiment_id,
            status=recommendation.status,
            evidence=recommendation.evidence,
            observation_plan=recommendation.observation_plan,
            rollback_plan=recommendation.rollback_plan,
            created_at=recommendation.created_at,
            runtime_mutation_allowed=True,
        )


def test_recommendation_rejects_absolute_or_traversal_evidence_refs() -> None:
    candidate = candidate_artifact()

    bad_absolute_refs = evidence_refs()
    bad_absolute_refs["metrics_snapshot"] = "C:\\secret\\metrics_snapshot.json"
    with pytest.raises(ValueError, match="relative artifact ref"):
        build_research_recommendation(candidate, evidence_refs=bad_absolute_refs)

    bad_traversal_refs = evidence_refs()
    bad_traversal_refs["metrics_snapshot"] = "../metrics_snapshot.json"
    with pytest.raises(ValueError, match="path traversal"):
        build_research_recommendation(candidate, evidence_refs=bad_traversal_refs)


def test_recommendation_rejects_runtime_command_operator_action() -> None:
    with pytest.raises(ValueError, match="runtime command term"):
        RollbackPlan(
            rollback_required=True,
            trigger_conditions=("observation invalidates evidence",),
            operator_actions=("okx_write replacement order",),
            verification_checks=("recommendation remains archived",),
        )


def test_recommendation_requires_execution_realism_metrics_when_execution_ref_present() -> None:
    metrics = metrics_snapshot(
        fillable_ratio=None,
        missing_reasons={"fillable_ratio": "not provided by execution summary"},
    )
    gate = evaluate_candidate_gate(metrics, {"max_drawdown_limit": 0.2})
    candidate = CandidateArtifact(
        candidate_id="cand_20260516_000004",
        experiment_id="exp_20260516_000004",
        candidate_type="factor",
        payload={
            "factor_expression": "Return(close, 1)",
            "dataset_fingerprint": "sha256:def456",
            "benchmark_segment": "test",
            "execution_cost_summary_ref": "execution_cost_summary.json",
            "generated_by": "unit_test",
            "research_only": True,
        },
        metrics=metrics,
        gate=gate,
    )

    with pytest.raises(ValueError, match="execution realism evidence missing"):
        build_research_recommendation(candidate, evidence_refs=evidence_refs())


def test_recorder_writes_research_recommendation_after_candidate(workspace_tmp_path: Path) -> None:
    root = artifact_root(workspace_tmp_path)
    recorder = ExperimentRecorder(root, code_version="test-sha", clock=lambda: dt(9))
    spec = experiment_spec(root)
    metrics = metrics_snapshot()
    gate = evaluate_candidate_gate(metrics, {"max_drawdown_limit": 0.2})
    candidate = CandidateArtifact(
        candidate_id="cand_20260516_000003",
        experiment_id=spec.experiment_id,
        candidate_type="factor",
        payload={
            "factor_expression": "Return(close, 1)",
            "dataset_fingerprint": "sha256:abc123",
            "benchmark_segment": "test",
            "generated_by": "unit_test",
            "research_only": True,
        },
        metrics=metrics,
        gate=gate,
    )

    recorder.start(spec)
    recorder.record_metrics(spec.experiment_id, metrics)
    recorder.record_candidate(spec.experiment_id, candidate)
    recommendation = build_research_recommendation(
        candidate,
        evidence_refs=evidence_refs(),
        created_at=dt(9),
    )
    manifest = recorder.record_recommendation(spec.experiment_id, recommendation)

    experiment_dir = root / spec.experiment_id
    stored_recommendation = read_json(experiment_dir / "research_recommendation.json")
    stored_manifest = read_json(experiment_dir / "experiment_manifest.json")
    assert manifest["output_refs"]["research_recommendation"] == "research_recommendation.json"
    assert stored_manifest["output_refs"]["research_recommendation"] == "research_recommendation.json"
    assert stored_recommendation["runtime_mutation_allowed"] is False
    assert stored_recommendation["operator_approval_required"] is True
    assert stored_recommendation["evidence"]["candidate_id"] == candidate.candidate_id


def test_recorder_rejects_recommendation_before_candidate(workspace_tmp_path: Path) -> None:
    root = artifact_root(workspace_tmp_path)
    recorder = ExperimentRecorder(root)
    spec = experiment_spec(root)
    candidate = candidate_artifact()
    recommendation = build_research_recommendation(candidate, evidence_refs=evidence_refs())

    recorder.start(spec)
    recorder.record_metrics(spec.experiment_id, metrics_snapshot())

    with pytest.raises(ValueError, match="candidate artifact must be recorded"):
        recorder.record_recommendation(spec.experiment_id, recommendation)
