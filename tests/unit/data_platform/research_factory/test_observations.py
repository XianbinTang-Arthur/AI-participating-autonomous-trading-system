import json
import shutil
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aats.data_platform.research_factory.metrics.gates import (
    CandidateArtifact,
    evaluate_candidate_gate,
)
from aats.data_platform.research_factory.observations import (
    ObservationRecorder,
    ObservationResult,
    ReviewOutcome,
    build_review_outcome,
)
from aats.data_platform.research_factory.recommendations import build_research_recommendation
from aats.data_platform.research_factory.specs import MetricsSnapshot

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


def observations_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "research" / "research_factory" / "observations"


def metrics_snapshot() -> MetricsSnapshot:
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
        fillable_ratio=0.9,
        partial_fill_ratio=0.05,
        cost_adjusted_edge_bps_mean=1.2,
        missing_reasons={},
    )


def candidate_artifact() -> CandidateArtifact:
    metrics = metrics_snapshot()
    gate = evaluate_candidate_gate(metrics, {"max_drawdown_limit": 0.2})
    return CandidateArtifact(
        candidate_id="cand_20260516_obs001",
        experiment_id="exp_20260516_obs001",
        candidate_type="factor",
        payload={
            "factor_expression": "Return(close, 1)",
            "dataset_fingerprint": "sha256:obs123",
            "benchmark_segment": "test",
            "generated_by": "unit_test",
            "research_only": True,
        },
        metrics=metrics,
        gate=gate,
        created_at=dt(8),
    )


def recommendation():
    return build_research_recommendation(
        candidate_artifact(),
        evidence_refs={
            "candidate_artifact": "candidate_artifact.json",
            "experiment_manifest": "experiment_manifest.json",
            "metrics_snapshot": "metrics_snapshot.json",
        },
        created_at=dt(9),
    )


def observation_result(*, review_decision: str = "eligible_for_preapply") -> ObservationResult:
    return ObservationResult(
        observation_id="obs_rec_cand_20260516_obs001",
        recommendation_id="rec_cand_20260516_obs001",
        candidate_id="cand_20260516_obs001",
        experiment_id="exp_20260516_obs001",
        mode="shadow",
        observation_start=dt(10),
        observation_end=dt(12),
        observed_bars=96,
        observed_events=12,
        signal_count=15,
        paper_intent_count=0,
        fillable_ratio=0.92,
        partial_fill_ratio=0.04,
        fee_bps_mean=5.0,
        slippage_bps_mean=1.8,
        funding_bps_mean=0.4,
        cost_adjusted_edge_bps_mean=1.1,
        drawdown=0.08,
        metric_drift=0.12,
        abort_triggered=False,
        review_decision=review_decision,
        created_at=dt(12, 1),
    )


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_observation_recorder_writes_lifecycle_artifacts(workspace_tmp_path: Path) -> None:
    root = observations_root(workspace_tmp_path)
    recorder = ObservationRecorder(root, code_version="test-sha", clock=lambda: dt(10))
    rec = recommendation()

    run = recorder.plan(rec)
    assert run.status == "planned"
    running = recorder.start(run.observation_id, started_at=dt(10, 1))
    assert running.status == "running"

    result = observation_result()
    result_manifest = recorder.record_result(result)
    outcome = build_review_outcome(
        result,
        rationale="shadow observation kept positive executable edge",
        created_at=dt(12, 2),
    )
    final_manifest = recorder.record_review_outcome(outcome)

    observation_dir = root / run.observation_id
    stored_run = read_json(observation_dir / "observation_run.json")
    stored_result = read_json(observation_dir / "observation_result.json")
    stored_outcome = read_json(observation_dir / "review_outcome.json")
    stored_manifest = read_json(observation_dir / "observation_manifest.json")

    assert result_manifest["status"] == "running"
    assert final_manifest["status"] == "succeeded"
    assert stored_manifest["artifact_type"] == "observation"
    assert stored_manifest["output_refs"]["observation_run"] == "observation_run.json"
    assert stored_manifest["output_refs"]["observation_result"] == "observation_result.json"
    assert stored_manifest["output_refs"]["review_outcome"] == "review_outcome.json"
    assert stored_run["status"] == "completed"
    assert stored_result["review_decision"] == "eligible_for_preapply"
    assert stored_outcome["decision"] == "eligible_for_preapply"
    assert stored_outcome["runtime_mutation_allowed"] is False
    assert stored_outcome["operator_approval_required"] is True
    assert stored_outcome["recommended_next_step"] == "prepare_preapply_evidence_review"


def test_observation_result_rejects_direct_apply_decision() -> None:
    with pytest.raises(ValueError, match="review_decision must be one of"):
        observation_result(review_decision="apply")


def test_eligible_for_preapply_rejects_aborted_observation() -> None:
    with pytest.raises(ValueError, match="aborted observation"):
        ObservationResult(
            observation_id="obs_aborted",
            recommendation_id="rec_aborted",
            candidate_id="cand_aborted",
            experiment_id="exp_aborted",
            mode="paper",
            observation_start=dt(10),
            observation_end=dt(11),
            observed_bars=12,
            observed_events=2,
            signal_count=3,
            paper_intent_count=3,
            fillable_ratio=0.5,
            partial_fill_ratio=0.2,
            fee_bps_mean=5.0,
            slippage_bps_mean=3.0,
            funding_bps_mean=0.0,
            cost_adjusted_edge_bps_mean=0.2,
            drawdown=0.03,
            metric_drift=0.1,
            abort_triggered=True,
            abort_reason="fillability below review threshold",
            review_decision="eligible_for_preapply",
            created_at=dt(11, 1),
        )


def test_review_outcome_rejects_runtime_mutation_and_direct_apply_text() -> None:
    result = observation_result(review_decision="keep_reviewing")

    with pytest.raises(ValueError, match="must not allow runtime mutation"):
        ReviewOutcome(
            outcome_id="out_obs",
            observation_id=result.observation_id,
            recommendation_id=result.recommendation_id,
            candidate_id=result.candidate_id,
            experiment_id=result.experiment_id,
            decision=result.review_decision,
            rationale="continue observation",
            runtime_mutation_allowed=True,
        )

    with pytest.raises(ValueError, match="runtime promotion term"):
        build_review_outcome(result, rationale="direct_apply candidate after observation")


def test_observation_recorder_rejects_result_before_start(workspace_tmp_path: Path) -> None:
    root = observations_root(workspace_tmp_path)
    recorder = ObservationRecorder(root, clock=lambda: dt(10))
    run = recorder.plan(recommendation())

    with pytest.raises(ValueError, match="is not running"):
        recorder.record_result(observation_result())

    stored_run = read_json(root / run.observation_id / "observation_run.json")
    assert stored_run["status"] == "planned"


def test_observation_recorder_rejects_mismatched_result(workspace_tmp_path: Path) -> None:
    root = observations_root(workspace_tmp_path)
    recorder = ObservationRecorder(root, clock=lambda: dt(10))
    run = recorder.plan(recommendation())
    recorder.start(run.observation_id, started_at=dt(10, 1))
    bad_result = ObservationResult(
        observation_id=run.observation_id,
        recommendation_id="rec_other",
        candidate_id="cand_20260516_obs001",
        experiment_id="exp_20260516_obs001",
        mode="shadow",
        observation_start=dt(10),
        observation_end=dt(12),
        observed_bars=96,
        observed_events=12,
        signal_count=15,
        paper_intent_count=0,
        fillable_ratio=0.92,
        partial_fill_ratio=0.04,
        fee_bps_mean=5.0,
        slippage_bps_mean=1.8,
        funding_bps_mean=0.4,
        cost_adjusted_edge_bps_mean=1.1,
        drawdown=0.08,
        metric_drift=0.12,
        abort_triggered=False,
        review_decision="keep_reviewing",
        created_at=dt(12, 1),
    )

    with pytest.raises(ValueError, match="recommendation_id must match run"):
        recorder.record_result(bad_result)


def test_observation_root_must_be_under_research_artifacts(workspace_tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="under artifacts/research"):
        ObservationRecorder(workspace_tmp_path / "configs")
