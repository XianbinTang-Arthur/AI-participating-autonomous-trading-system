import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aats.data_platform.research_factory.metrics.gates import CandidateArtifact, evaluate_candidate_gate
from aats.data_platform.research_factory.observation_sources import (
    OBSERVATION_SUMMARY_SCHEMA_VERSION,
    PaperObservationDataSource,
    ShadowObservationDataSource,
)
from aats.data_platform.research_factory.recommendations import (
    ObservationPlan,
    build_research_recommendation,
)
from aats.data_platform.research_factory.specs import MetricsSnapshot

UTC = timezone.utc


def dt(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=UTC)


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


def candidate_artifact() -> CandidateArtifact:
    metrics = metrics_snapshot()
    gate = evaluate_candidate_gate(metrics, {"max_drawdown_limit": 0.2})
    return CandidateArtifact(
        candidate_id="cand_obs_source",
        experiment_id="exp_obs_source",
        candidate_type="factor",
        payload={
            "factor_expression": "Return(close, 1)",
            "dataset_fingerprint": "sha256:obs-source",
            "benchmark_segment": "test",
            "generated_by": "unit_test",
            "research_only": True,
        },
        metrics=metrics,
        gate=gate,
        created_at=dt(8),
    )


def recommendation(*, mode: str = "shadow"):
    observation_plan = ObservationPlan(
        mode=mode,
        min_observation_bars=48,
        min_observation_events=10,
        success_criteria=("cost-adjusted edge remains positive",),
        abort_conditions=("edge turns negative",),
    )
    return build_research_recommendation(
        candidate_artifact(),
        evidence_refs={
            "candidate_artifact": "candidate_artifact.json",
            "experiment_manifest": "experiment_manifest.json",
            "metrics_snapshot": "metrics_snapshot.json",
        },
        observation_plan=observation_plan,
        created_at=dt(9),
    )


def summary_path(tmp_path: Path, *, mode: str = "shadow", payload_overrides: dict | None = None) -> Path:
    path = tmp_path / "artifacts" / "research" / "research_factory" / "observations" / "summary.json"
    payload = {
        "schema_version": OBSERVATION_SUMMARY_SCHEMA_VERSION,
        "mode": mode,
        "recommendation_id": "rec_cand_obs_source",
        "candidate_id": "cand_obs_source",
        "experiment_id": "exp_obs_source",
        "observation_start": dt(10).isoformat(),
        "observation_end": dt(12).isoformat(),
        "observed_bars": 96,
        "observed_events": 12,
        "signal_count": 15,
        "paper_intent_count": 0 if mode == "shadow" else 7,
        "fillable_ratio": 0.92,
        "partial_fill_ratio": 0.04,
        "fee_bps_mean": 5.0,
        "slippage_bps_mean": 1.8,
        "funding_bps_mean": 0.4,
        "cost_adjusted_edge_bps_mean": 1.1,
        "drawdown": 0.08,
        "metric_drift": 0.12,
        "abort_triggered": False,
    }
    if payload_overrides:
        payload.update(payload_overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_shadow_observation_source_builds_neutral_result(tmp_path: Path) -> None:
    source = ShadowObservationDataSource(summary_path(tmp_path, mode="shadow"))

    result = source.load_result(recommendation(mode="shadow"), created_at=dt(13))

    assert result.mode == "shadow"
    assert result.review_decision == "keep_reviewing"
    assert result.fillable_ratio == pytest.approx(0.92)
    assert result.paper_intent_count == 0
    assert result.created_at == dt(13)


def test_paper_observation_source_requires_paper_recommendation(tmp_path: Path) -> None:
    source = PaperObservationDataSource(summary_path(tmp_path, mode="paper"))

    with pytest.raises(ValueError, match="recommendation observation mode"):
        source.load_result(recommendation(mode="shadow"))

    result = source.load_result(
        recommendation(mode="paper"),
        review_decision="reject",
        created_at=dt(13),
    )

    assert result.mode == "paper"
    assert result.review_decision == "reject"
    assert result.paper_intent_count == 7


def test_observation_source_rejects_identity_mismatch(tmp_path: Path) -> None:
    path = summary_path(tmp_path, payload_overrides={"candidate_id": "cand_other"})
    source = ShadowObservationDataSource(path)

    with pytest.raises(ValueError, match="candidate_id must match"):
        source.load_result(recommendation())


def test_observation_source_rejects_non_research_path(tmp_path: Path) -> None:
    path = tmp_path / "outside.json"
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="under artifacts/research"):
        ShadowObservationDataSource(path)


def test_observation_source_requires_timezone_aware_summary(tmp_path: Path) -> None:
    path = summary_path(tmp_path, payload_overrides={"observation_start": "2026-01-10T00:00:00"})
    source = ShadowObservationDataSource(path)

    with pytest.raises(ValueError, match="timezone-aware"):
        source.load_result(recommendation())
