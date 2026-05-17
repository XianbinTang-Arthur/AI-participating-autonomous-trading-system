import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aats.data_platform.research_factory.metrics.gates import CandidateArtifact, evaluate_candidate_gate
from aats.data_platform.research_factory.observations import (
    ObservationGateResult,
    ObservationResult,
    ReviewOutcome,
)
from aats.data_platform.research_factory.registry import (
    ResearchMemoryEntry,
    ResearchMemoryRegistry,
    build_observation_memory_entry,
    build_research_memory_entry,
    factor_signature_from_expression,
)
from aats.data_platform.research_factory.specs import MetricsSnapshot

UTC = timezone.utc


def dt(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=UTC)


def registry_path(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "research" / "research_factory" / "registry" / "research_memory.jsonl"


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
    )


def candidate_artifact(experiment_id: str, *, expression: str = "Return(close, 1)") -> CandidateArtifact:
    metrics = metrics_snapshot()
    gate = evaluate_candidate_gate(metrics, {"max_drawdown_limit": 0.2})
    return CandidateArtifact(
        candidate_id=f"cand_{experiment_id}",
        experiment_id=experiment_id,
        candidate_type="factor",
        payload={
            "factor_expression": expression,
            "dataset_fingerprint": "sha256:dataset-fixture",
            "benchmark_segment": "test",
            "generated_by": "unit_test",
            "research_only": True,
        },
        metrics=metrics,
        gate=gate,
        created_at=dt(8),
    )


def observation_result(candidate: CandidateArtifact, *, review_decision: str) -> ObservationResult:
    return ObservationResult(
        observation_id=f"obs_{candidate.candidate_id}",
        recommendation_id=f"rec_{candidate.candidate_id}",
        candidate_id=candidate.candidate_id,
        experiment_id=candidate.experiment_id,
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
        created_at=dt(12),
    )


def observation_gate(result: ObservationResult, *, passed: bool = True) -> ObservationGateResult:
    return ObservationGateResult(
        observation_id=result.observation_id,
        recommendation_id=result.recommendation_id,
        candidate_id=result.candidate_id,
        experiment_id=result.experiment_id,
        passed=passed,
        failures=() if passed else ("cost_adjusted_edge_bps_mean=-0.100000 <= 0.000000",),
        thresholds={"min_observed_bars": 48, "min_observed_events": 10},
        evaluated_at=dt(12),
    )


def review_outcome(result: ObservationResult, *, decision: str, gate_passed: bool | None) -> ReviewOutcome:
    return ReviewOutcome(
        outcome_id=f"out_{result.observation_id}",
        observation_id=result.observation_id,
        recommendation_id=result.recommendation_id,
        candidate_id=result.candidate_id,
        experiment_id=result.experiment_id,
        decision=decision,
        rationale="observation review completed",
        observation_gate_passed=gate_passed,
        created_at=dt(12),
    )


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_factor_signature_uses_normalized_factor_ast() -> None:
    assert factor_signature_from_expression("Return(close, 1)") == factor_signature_from_expression(
        " Return(close,   1) "
    )
    assert factor_signature_from_expression("Return(close, 2)") != factor_signature_from_expression(
        "Return(close, 1)"
    )


def test_research_memory_registry_upsert_is_idempotent(tmp_path: Path) -> None:
    registry = ResearchMemoryRegistry(registry_path(tmp_path))
    candidate = candidate_artifact("exp_registry_1")
    entry = build_research_memory_entry(
        experiment_id=candidate.experiment_id,
        status="recommendation_ready",
        created_by="unit_test",
        created_at=dt(9),
        candidate=candidate,
        artifact_refs={
            "candidate_artifact": "exp_registry_1/candidate_artifact.json",
            "metrics_snapshot": "exp_registry_1/metrics_snapshot.json",
        },
    )

    first = registry.upsert(entry)
    first_text = registry.path.read_text(encoding="utf-8")
    second = registry.upsert(entry)

    assert first.entry_id == second.entry_id
    assert registry.path.read_text(encoding="utf-8") == first_text
    assert len(registry.load_entries()) == 1


def test_research_memory_registry_detects_same_factor_and_dataset(tmp_path: Path) -> None:
    registry = ResearchMemoryRegistry(registry_path(tmp_path))
    first_candidate = candidate_artifact("exp_registry_first")
    second_candidate = candidate_artifact("exp_registry_second")
    first_entry = build_research_memory_entry(
        experiment_id=first_candidate.experiment_id,
        status="recommendation_ready",
        created_by="unit_test",
        created_at=dt(9),
        candidate=first_candidate,
    )
    second_entry = build_research_memory_entry(
        experiment_id=second_candidate.experiment_id,
        status="recommendation_ready",
        created_by="unit_test",
        created_at=dt(10),
        candidate=second_candidate,
    )

    registry.upsert(first_entry)
    enriched = registry.upsert(second_entry)

    assert enriched.similarity_to_existing
    assert enriched.similarity_to_existing[0].score == pytest.approx(1.0)
    assert enriched.similarity_to_existing[0].experiment_id == "exp_registry_first"
    assert "same factor_signature" in enriched.similarity_to_existing[0].reason


def test_research_memory_registry_redacts_failure_reason(tmp_path: Path) -> None:
    registry = ResearchMemoryRegistry(registry_path(tmp_path))
    entry = build_research_memory_entry(
        experiment_id="exp_registry_failure",
        status="failed",
        created_by="unit_test",
        created_at=dt(9),
        factor_expression="Unknown(close)",
        dataset_fingerprint="sha256:dataset-fixture",
        failure_reason="password=abc OKX_KEY=def",
        artifact_refs={"failure": "exp_registry_failure/failure.json"},
    )

    registry.upsert(entry)
    raw = registry.path.read_text(encoding="utf-8")
    payload = read_jsonl(registry.path)[0]

    assert payload["failure_reason"] == "[REDACTED]"
    assert "abc" not in raw
    assert "def" not in raw
    assert "OKX_KEY" not in raw


def test_research_memory_registry_rejects_unsafe_paths_and_refs(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="under artifacts/research"):
        ResearchMemoryRegistry(tmp_path / "registry" / "research_memory.jsonl")

    with pytest.raises(ValueError, match="path traversal"):
        ResearchMemoryEntry(
            entry_id="mem_bad_ref",
            experiment_id="exp_bad_ref",
            status="failed",
            created_at=dt(9),
            created_by="unit_test",
            artifact_refs={"failure": "../failure.json"},
        )


def test_observation_memory_entry_records_eligible_outcome(tmp_path: Path) -> None:
    registry = ResearchMemoryRegistry(registry_path(tmp_path))
    candidate = candidate_artifact("exp_registry_observation_ready")
    result = observation_result(candidate, review_decision="eligible_for_preapply")
    gate = observation_gate(result, passed=True)
    outcome = review_outcome(result, decision="eligible_for_preapply", gate_passed=True)

    entry = build_observation_memory_entry(
        candidate=candidate,
        observation_result=result,
        observation_gate=gate,
        review_outcome=outcome,
        created_by="unit_test",
        created_at=dt(13),
        artifact_refs={
            "observation_result": "observations/obs_ready/observation_result.json",
            "observation_gate_result": "observations/obs_ready/observation_gate_result.json",
            "review_outcome": "observations/obs_ready/review_outcome.json",
        },
    )
    registry.upsert(entry)
    payload = read_jsonl(registry.path)[0]

    assert payload["status"] == "observation_eligible_for_preapply"
    assert payload["candidate_id"] == candidate.candidate_id
    assert payload["recommendation_id"] == result.recommendation_id
    assert payload["observation_id"] == result.observation_id
    assert payload["review_decision"] == "eligible_for_preapply"
    assert payload["observation_metrics"]["cost_adjusted_edge_bps_mean"] == pytest.approx(1.1)
    assert payload["observation_metrics"]["fillable_ratio"] == pytest.approx(0.92)
    assert payload["observation_gate_result"]["passed"] is True
    assert payload["observation_failure_reasons"] == []
    assert payload["failure_reason"] is None


def test_observation_memory_entry_records_failed_gate_reasons(tmp_path: Path) -> None:
    registry = ResearchMemoryRegistry(registry_path(tmp_path))
    candidate = candidate_artifact("exp_registry_observation_keep")
    result = observation_result(candidate, review_decision="keep_reviewing")
    gate = observation_gate(result, passed=False)
    outcome = review_outcome(result, decision="keep_reviewing", gate_passed=False)

    entry = build_observation_memory_entry(
        candidate=candidate,
        observation_result=result,
        observation_gate=gate,
        review_outcome=outcome,
        created_by="unit_test",
        created_at=dt(13),
    )
    registry.upsert(entry)
    payload = read_jsonl(registry.path)[0]

    assert payload["status"] == "observation_keep_reviewing"
    assert payload["failure_reason"].startswith("review_decision=keep_reviewing")
    assert payload["observation_failure_reasons"][0] == "review_decision=keep_reviewing"
    assert "observation_gate:" in payload["observation_failure_reasons"][1]
    assert payload["observation_gate_result"]["passed"] is False


def test_observation_memory_entry_validates_identity() -> None:
    candidate = candidate_artifact("exp_registry_observation_bad")
    result = observation_result(candidate, review_decision="keep_reviewing")
    gate = observation_gate(result, passed=True)
    outcome = ReviewOutcome(
        outcome_id=f"out_{result.observation_id}",
        observation_id=result.observation_id,
        recommendation_id=result.recommendation_id,
        candidate_id="cand_other",
        experiment_id=result.experiment_id,
        decision="keep_reviewing",
        rationale="observation review completed",
        observation_gate_passed=True,
        created_at=dt(12),
    )

    with pytest.raises(ValueError, match="candidate_id must match candidate"):
        build_observation_memory_entry(
            candidate=candidate,
            observation_result=result,
            observation_gate=gate,
            review_outcome=outcome,
            created_by="unit_test",
            created_at=dt(13),
        )


def test_observation_memory_similarity_reuses_factor_and_dataset(tmp_path: Path) -> None:
    registry = ResearchMemoryRegistry(registry_path(tmp_path))
    first_candidate = candidate_artifact("exp_registry_observation_first")
    first_result = observation_result(first_candidate, review_decision="reject")
    first_gate = observation_gate(first_result, passed=False)
    first_outcome = review_outcome(first_result, decision="reject", gate_passed=False)
    second_candidate = candidate_artifact("exp_registry_observation_second")
    second_result = observation_result(second_candidate, review_decision="eligible_for_preapply")
    second_gate = observation_gate(second_result, passed=True)
    second_outcome = review_outcome(second_result, decision="eligible_for_preapply", gate_passed=True)

    registry.upsert(
        build_observation_memory_entry(
            candidate=first_candidate,
            observation_result=first_result,
            observation_gate=first_gate,
            review_outcome=first_outcome,
            created_by="unit_test",
            created_at=dt(13),
        )
    )
    enriched = registry.upsert(
        build_observation_memory_entry(
            candidate=second_candidate,
            observation_result=second_result,
            observation_gate=second_gate,
            review_outcome=second_outcome,
            created_by="unit_test",
            created_at=dt(14),
        )
    )

    assert enriched.similarity_to_existing
    assert enriched.similarity_to_existing[0].status == "observation_rejected"
    assert enriched.similarity_to_existing[0].score == pytest.approx(1.0)
