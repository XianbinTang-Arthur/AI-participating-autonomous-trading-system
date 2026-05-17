import json
import shutil
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aats.data_platform.research_factory.evidence import (
    DatasetQualityReport,
    DatasetQualityThresholds,
    EvidenceBundle,
    ExecutionEvidenceReport,
    SourceIntegrityReport,
)
from aats.data_platform.research_factory.metrics.gates import (
    CandidateArtifact,
    evaluate_candidate_gate,
)
from aats.data_platform.research_factory.observations import (
    ObservationGateResult,
    ReviewOutcome,
)
from aats.data_platform.research_factory.preapply import (
    PreApplyEvidencePackage,
    PreApplyEvidenceRecorder,
    build_preapply_evidence_package,
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


def preapply_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "research" / "research_factory" / "preapply"


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
        candidate_id="cand_20260516_pre001",
        experiment_id="exp_20260516_pre001",
        candidate_type="factor",
        payload={
            "factor_expression": "Return(close, 1)",
            "dataset_fingerprint": "sha256:pre123",
            "benchmark_segment": "test",
            "generated_by": "unit_test",
            "research_only": True,
        },
        metrics=metrics,
        gate=gate,
        created_at=dt(8),
    )


def recommendation(candidate: CandidateArtifact):
    return build_research_recommendation(
        candidate,
        evidence_refs={
            "candidate_artifact": "candidate_artifact.json",
            "experiment_manifest": "experiment_manifest.json",
            "metrics_snapshot": "metrics_snapshot.json",
        },
        created_at=dt(9),
    )


def evidence_bundle(*, passed: bool = True) -> EvidenceBundle:
    failures = () if passed else ("dataset_quality: row_count=0 < min_total_bars=10",)
    return EvidenceBundle(
        dataset_quality=DatasetQualityReport(
            dataset_id="btc_15m_v1",
            dataset_fingerprint="sha256:pre123",
            timeframe="15m",
            window_start=dt(1),
            window_end=dt(10),
            row_count=100,
            expected_bar_count=100,
            expected_interval_seconds=900.0,
            missing_bar_count=0,
            bar_gap_ratio=0.0,
            max_gap_seconds=900.0,
            funding_missing_count=0,
            funding_missing_ratio=0.0,
            segment_row_counts={"train": 60, "valid": 20, "test": 20},
            thresholds=DatasetQualityThresholds(),
            passed=True,
            failures=(),
            created_at=dt(9),
        ),
        source_integrity=SourceIntegrityReport(
            dataset_id="btc_15m_v1",
            source_candle_dataset_versions=("v1.0",),
            source_funding_dataset_versions=("f1.0",),
            build_run_ids=("build-1",),
            source_watermark={"build_run_ids": ["build-1"]},
            candle_version_consistent=True,
            funding_version_consistent=True,
            build_run_traceable=True,
            build_run_consistent=True,
            timestamp_timezone_assumption="timezone-aware database timestamp",
            passed=True,
            failures=(),
            created_at=dt(9),
        ),
        execution_evidence=ExecutionEvidenceReport(
            dataset_id="btc_15m_v1",
            evidence_ref="execution_cost_summary.json",
            contract_schema_version="execution_cost_summary_v1",
            source_run_id="exec-run-1",
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            window_start=dt(1),
            window_end=dt(10),
            dataset_fingerprint="sha256:pre123",
            dataset_fingerprint_compatible=False,
            compatibility_reason=None,
            passed=True,
            failures=(),
            created_at=dt(9),
        ),
        execution_evidence_required=True,
        passed=passed,
        failures=failures,
        created_at=dt(9),
    )


def observation_gate(
    candidate: CandidateArtifact,
    rec,
    *,
    passed: bool = True,
) -> ObservationGateResult:
    return ObservationGateResult(
        observation_id="obs_rec_cand_20260516_pre001",
        recommendation_id=rec.recommendation_id,
        candidate_id=candidate.candidate_id,
        experiment_id=candidate.experiment_id,
        passed=passed,
        failures=() if passed else ("cost_adjusted_edge_bps_mean=-0.100000 <= 0.000000",),
        thresholds={"min_observed_bars": 48, "min_observed_events": 10},
        evaluated_at=dt(12),
    )


def review_outcome(candidate: CandidateArtifact, rec, *, decision: str, gate_passed: bool | None):
    return ReviewOutcome(
        outcome_id="out_obs_rec_cand_20260516_pre001",
        observation_id="obs_rec_cand_20260516_pre001",
        recommendation_id=rec.recommendation_id,
        candidate_id=candidate.candidate_id,
        experiment_id=candidate.experiment_id,
        decision=decision,
        rationale="observation review completed",
        observation_gate_passed=gate_passed,
        created_at=dt(12, 1),
    )


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


def build_ready_package() -> PreApplyEvidencePackage:
    candidate = candidate_artifact()
    rec = recommendation(candidate)
    gate = observation_gate(candidate, rec, passed=True)
    outcome = review_outcome(candidate, rec, decision="eligible_for_preapply", gate_passed=True)
    return build_preapply_evidence_package(
        candidate=candidate,
        recommendation=rec,
        evidence_bundle=evidence_bundle(),
        observation_gate=gate,
        review_outcome=outcome,
        evidence_refs=evidence_refs(),
        gate_refs=gate_refs(),
        created_at=dt(13),
    )


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_build_preapply_ready_package_from_passed_observation() -> None:
    package = build_ready_package()

    assert package.status == "preapply_ready"
    assert package.review_decision == "eligible_for_preapply"
    assert package.candidate_gate_passed is True
    assert package.evidence_bundle_passed is True
    assert package.observation_gate_passed is True
    assert package.failure_reasons == ()
    assert package.runtime_mutation_allowed is False
    assert package.operator_approval_required is True
    assert package.recommended_next_step == "submit_preapply_evidence_for_operator_review"
    assert package.evidence_refs["evidence_bundle"] == "evidence_bundle.json"
    assert package.gate_refs["observation_gate_result"] == "observation_gate_result.json"


def test_build_preapply_package_maps_keep_reviewing_and_reject() -> None:
    candidate = candidate_artifact()
    rec = recommendation(candidate)
    gate = observation_gate(candidate, rec, passed=False)

    keep_reviewing = build_preapply_evidence_package(
        candidate=candidate,
        recommendation=rec,
        evidence_bundle=evidence_bundle(),
        observation_gate=gate,
        review_outcome=review_outcome(candidate, rec, decision="keep_reviewing", gate_passed=False),
        evidence_refs=evidence_refs(),
        gate_refs=gate_refs(),
        created_at=dt(13),
    )
    rejected = build_preapply_evidence_package(
        candidate=candidate,
        recommendation=rec,
        evidence_bundle=evidence_bundle(),
        observation_gate=gate,
        review_outcome=review_outcome(candidate, rec, decision="reject", gate_passed=False),
        evidence_refs=evidence_refs(),
        gate_refs=gate_refs(),
        created_at=dt(13),
    )

    assert keep_reviewing.status == "needs_more_observation"
    assert keep_reviewing.failure_reasons[0] == "review_decision=keep_reviewing"
    assert "observation_gate:" in keep_reviewing.failure_reasons[1]
    assert rejected.status == "preapply_rejected"
    assert rejected.failure_reasons[0] == "review_decision=reject"


def test_preapply_ready_requires_passing_gate_and_evidence_bundle() -> None:
    candidate = candidate_artifact()
    rec = recommendation(candidate)
    failed_gate = observation_gate(candidate, rec, passed=False)
    outcome = review_outcome(candidate, rec, decision="eligible_for_preapply", gate_passed=True)

    with pytest.raises(ValueError, match="observation_gate_passed must match observation gate"):
        build_preapply_evidence_package(
            candidate=candidate,
            recommendation=rec,
            evidence_bundle=evidence_bundle(),
            observation_gate=failed_gate,
            review_outcome=outcome,
            evidence_refs=evidence_refs(),
            gate_refs=gate_refs(),
        )

    with pytest.raises(ValueError, match="passing observation gate"):
        PreApplyEvidencePackage(
            package_id="preapply_failed_gate",
            candidate_id=candidate.candidate_id,
            recommendation_id=rec.recommendation_id,
            observation_id=failed_gate.observation_id,
            experiment_id=candidate.experiment_id,
            status="preapply_ready",
            evidence_refs=evidence_refs(),
            gate_refs=gate_refs(),
            review_decision="eligible_for_preapply",
            candidate_gate_passed=True,
            evidence_bundle_passed=True,
            observation_gate_passed=False,
        )

    with pytest.raises(ValueError, match="passing evidence bundle"):
        build_preapply_evidence_package(
            candidate=candidate,
            recommendation=rec,
            evidence_bundle=evidence_bundle(passed=False),
            observation_gate=observation_gate(candidate, rec, passed=True),
            review_outcome=outcome,
            evidence_refs=evidence_refs(),
            gate_refs=gate_refs(),
        )


def test_preapply_package_requires_all_refs() -> None:
    candidate = candidate_artifact()
    rec = recommendation(candidate)
    gate = observation_gate(candidate, rec)
    outcome = review_outcome(candidate, rec, decision="eligible_for_preapply", gate_passed=True)
    refs = evidence_refs()
    del refs["evidence_bundle"]

    with pytest.raises(ValueError, match="missing required ref: evidence_bundle"):
        build_preapply_evidence_package(
            candidate=candidate,
            recommendation=rec,
            evidence_bundle=evidence_bundle(),
            observation_gate=gate,
            review_outcome=outcome,
            evidence_refs=refs,
            gate_refs=gate_refs(),
        )


def test_preapply_package_rejects_runtime_promotion_text() -> None:
    package = build_ready_package()

    bad_refs = dict(package.evidence_refs)
    bad_refs["active_parameter"] = "candidate_artifact.json"
    with pytest.raises(ValueError, match="runtime promotion term"):
        PreApplyEvidencePackage(
            package_id="preapply_bad_ref",
            candidate_id=package.candidate_id,
            recommendation_id=package.recommendation_id,
            observation_id=package.observation_id,
            experiment_id=package.experiment_id,
            status=package.status,
            evidence_refs=bad_refs,
            gate_refs=package.gate_refs,
            review_decision=package.review_decision,
            candidate_gate_passed=True,
            evidence_bundle_passed=True,
            observation_gate_passed=True,
        )

    with pytest.raises(ValueError, match="runtime promotion term"):
        PreApplyEvidencePackage(
            package_id="preapply_bad_note",
            candidate_id=package.candidate_id,
            recommendation_id=package.recommendation_id,
            observation_id=package.observation_id,
            experiment_id=package.experiment_id,
            status=package.status,
            evidence_refs=package.evidence_refs,
            gate_refs=package.gate_refs,
            review_decision=package.review_decision,
            candidate_gate_passed=True,
            evidence_bundle_passed=True,
            observation_gate_passed=True,
            notes=("direct_apply after review",),
        )


def test_preapply_recorder_writes_package_and_manifest(workspace_tmp_path: Path) -> None:
    root = preapply_root(workspace_tmp_path)
    recorder = PreApplyEvidenceRecorder(root, code_version="test-sha", clock=lambda: dt(13, 1))
    package = build_ready_package()

    manifest = recorder.record_package(package)

    package_dir = root / package.package_id
    stored_package = read_json(package_dir / "preapply_evidence_package.json")
    stored_manifest = read_json(package_dir / "preapply_manifest.json")
    assert manifest["artifact_type"] == "preapply"
    assert manifest["status"] == "succeeded"
    assert stored_manifest["output_refs"]["preapply_evidence_package"] == "preapply_evidence_package.json"
    assert stored_manifest["input_refs"]["package_status"] == "preapply_ready"
    assert stored_package["status"] == "preapply_ready"
    assert stored_package["runtime_mutation_allowed"] is False

    with pytest.raises(ValueError, match="already exists"):
        recorder.record_package(package)


def test_preapply_recorder_root_must_be_under_research_artifacts(workspace_tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="under artifacts/research"):
        PreApplyEvidenceRecorder(workspace_tmp_path / "configs")
