import json
from pathlib import Path

import pytest

from aats.data_platform.research_factory.smoke import (
    ResearchFactorySmokeConfig,
    run_research_factory_smoke,
)


def artifact_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "research" / "research_factory" / "experiments"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def write_execution_cost_summary(path: Path, *, cost_adjusted_edge: float = 1.75) -> None:
    path.write_text(
        json.dumps(
            {
                "total_candidates": 4,
                "full_fill_ratio": 0.75,
                "partial_fill_ratio": 0.25,
                "turnover": {"mean": 0.5},
                "fee": {"mean": 4.5},
                "funding": {"mean": 0.2},
                "slippage": {"mean": 1.5},
                "cost_adjusted_edge": {"mean": cost_adjusted_edge},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_research_factory_smoke_runner_writes_success_artifacts(tmp_path: Path) -> None:
    root = artifact_root(tmp_path)
    result = run_research_factory_smoke(
        ResearchFactorySmokeConfig(
            artifact_root=root,
            experiment_id="rf_smoke_success",
            overwrite=True,
        )
    )

    experiment_dir = root / "rf_smoke_success"
    manifest = read_json(experiment_dir / "experiment_manifest.json")
    metrics = read_json(experiment_dir / "metrics_snapshot.json")
    candidate = read_json(experiment_dir / "candidate_artifact.json")
    recommendation = read_json(experiment_dir / "research_recommendation.json")
    registry_entries = read_jsonl(root.parent / "registry" / "research_memory.jsonl")

    assert result.status == "succeeded"
    assert result.candidate_generated is True
    assert result.recommendation_ref == "research_recommendation.json"
    assert result.registry_ref == (root.parent / "registry" / "research_memory.jsonl").as_posix()
    assert manifest["status"] == "succeeded"
    assert manifest["metrics_ref"] == "metrics_snapshot.json"
    assert manifest["output_refs"]["candidate_artifact"] == "candidate_artifact.json"
    assert manifest["output_refs"]["research_recommendation"] == "research_recommendation.json"
    assert metrics["net_annualized_return"] > 0
    assert metrics["cost_adjusted_edge_bps_mean"] > 0
    assert candidate["candidate_type"] == "factor"
    assert candidate["payload"]["benchmark_segment"] == "test"
    assert recommendation["runtime_mutation_allowed"] is False
    assert recommendation["operator_approval_required"] is True
    assert recommendation["evidence"]["candidate_id"] == candidate["candidate_id"]
    assert registry_entries[0]["status"] == "recommendation_ready"
    assert registry_entries[0]["candidate_id"] == candidate["candidate_id"]
    assert registry_entries[0]["artifact_refs"]["research_recommendation"] == (
        "rf_smoke_success/research_recommendation.json"
    )
    assert "active_parameter" not in (experiment_dir / "candidate_artifact.json").read_text(
        encoding="utf-8"
    )


def test_research_factory_smoke_runner_outputs_stable_artifacts(tmp_path: Path) -> None:
    root = artifact_root(tmp_path)
    config = ResearchFactorySmokeConfig(
        artifact_root=root,
        experiment_id="rf_smoke_stable",
        overwrite=True,
    )

    run_research_factory_smoke(config)
    experiment_dir = root / "rf_smoke_stable"
    first_metrics = (experiment_dir / "metrics_snapshot.json").read_text(encoding="utf-8")
    first_candidate = (experiment_dir / "candidate_artifact.json").read_text(encoding="utf-8")
    first_recommendation = (experiment_dir / "research_recommendation.json").read_text(
        encoding="utf-8"
    )
    first_manifest = (experiment_dir / "experiment_manifest.json").read_text(encoding="utf-8")
    first_registry = (root.parent / "registry" / "research_memory.jsonl").read_text(
        encoding="utf-8"
    )

    run_research_factory_smoke(config)

    assert (experiment_dir / "metrics_snapshot.json").read_text(encoding="utf-8") == first_metrics
    assert (experiment_dir / "candidate_artifact.json").read_text(encoding="utf-8") == first_candidate
    assert (
        experiment_dir / "research_recommendation.json"
    ).read_text(encoding="utf-8") == first_recommendation
    assert (experiment_dir / "experiment_manifest.json").read_text(encoding="utf-8") == first_manifest
    assert (
        root.parent / "registry" / "research_memory.jsonl"
    ).read_text(encoding="utf-8") == first_registry


def test_research_factory_smoke_runner_writes_failure_artifact(tmp_path: Path) -> None:
    root = artifact_root(tmp_path)
    result = run_research_factory_smoke(
        ResearchFactorySmokeConfig(
            artifact_root=root,
            experiment_id="rf_smoke_failure",
            factor_expression="Unknown(close)",
            overwrite=True,
        )
    )

    experiment_dir = root / "rf_smoke_failure"
    manifest = read_json(experiment_dir / "experiment_manifest.json")
    failure = read_json(experiment_dir / "failure.json")
    registry_entries = read_jsonl(root.parent / "registry" / "research_memory.jsonl")

    assert result.status == "failed"
    assert result.candidate_generated is False
    assert result.registry_ref == (root.parent / "registry" / "research_memory.jsonl").as_posix()
    assert result.failure_ref == "failure.json"
    assert manifest["status"] == "failed"
    assert manifest["output_refs"]["failure"] == "failure.json"
    assert failure["reason"] == "unknown factor function: Unknown"
    assert registry_entries[0]["status"] == "failed"
    assert registry_entries[0]["failure_reason"] == "unknown factor function: Unknown"
    assert not (experiment_dir / "candidate_artifact.json").exists()
    assert not (experiment_dir / "research_recommendation.json").exists()


def test_research_factory_smoke_runner_merges_execution_realism_metrics(tmp_path: Path) -> None:
    root = artifact_root(tmp_path)
    execution_summary_path = tmp_path / "execution_cost_summary.json"
    write_execution_cost_summary(execution_summary_path)

    result = run_research_factory_smoke(
        ResearchFactorySmokeConfig(
            artifact_root=root,
            experiment_id="rf_smoke_execution_realism",
            execution_cost_summary_path=execution_summary_path,
            overwrite=True,
        )
    )

    experiment_dir = root / "rf_smoke_execution_realism"
    metrics = read_json(experiment_dir / "metrics_snapshot.json")
    candidate = read_json(experiment_dir / "candidate_artifact.json")
    recommendation = read_json(experiment_dir / "research_recommendation.json")
    registry_entries = read_jsonl(root.parent / "registry" / "research_memory.jsonl")

    assert result.status == "succeeded"
    assert metrics["fillable_ratio"] == pytest.approx(0.75)
    assert metrics["partial_fill_ratio"] == pytest.approx(0.25)
    assert metrics["turnover"] == pytest.approx(0.5)
    assert metrics["fee_bps_mean"] == pytest.approx(4.5)
    assert metrics["funding_bps_mean"] == pytest.approx(0.2)
    assert metrics["slippage_bps_mean"] == pytest.approx(1.5)
    assert metrics["cost_adjusted_edge_bps_mean"] == pytest.approx(1.75)
    assert candidate["payload"]["execution_cost_summary_ref"] == "execution_cost_summary.json"
    assert recommendation["evidence"]["execution_realism_required"] is True
    assert (
        recommendation["evidence"]["evidence_refs"]["execution_cost_summary"]
        == "execution_cost_summary.json"
    )
    assert registry_entries[0]["status"] == "recommendation_ready"
    assert registry_entries[0]["metric_snapshot"]["metrics"]["cost_adjusted_edge_bps_mean"] == pytest.approx(
        1.75
    )


def test_research_factory_smoke_runner_fails_on_negative_executable_edge(tmp_path: Path) -> None:
    root = artifact_root(tmp_path)
    execution_summary_path = tmp_path / "execution_cost_summary.json"
    write_execution_cost_summary(execution_summary_path, cost_adjusted_edge=-0.25)

    result = run_research_factory_smoke(
        ResearchFactorySmokeConfig(
            artifact_root=root,
            experiment_id="rf_smoke_execution_realism_fail",
            execution_cost_summary_path=execution_summary_path,
            overwrite=True,
        )
    )

    experiment_dir = root / "rf_smoke_execution_realism_fail"
    manifest = read_json(experiment_dir / "experiment_manifest.json")
    failure = read_json(experiment_dir / "failure.json")
    metrics = read_json(experiment_dir / "metrics_snapshot.json")
    registry_entries = read_jsonl(root.parent / "registry" / "research_memory.jsonl")

    assert result.status == "failed"
    assert manifest["status"] == "failed"
    assert metrics["cost_adjusted_edge_bps_mean"] == pytest.approx(-0.25)
    assert "cost_adjusted_edge_bps_mean" in failure["reason"]
    assert registry_entries[0]["status"] == "gate_failed"
    assert "cost_adjusted_edge_bps_mean" in registry_entries[0]["failure_reason"]
    assert not (experiment_dir / "candidate_artifact.json").exists()
    assert not (experiment_dir / "research_recommendation.json").exists()


def test_research_factory_smoke_runner_rejects_non_research_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="under artifacts/research"):
        run_research_factory_smoke(
            ResearchFactorySmokeConfig(
                artifact_root=tmp_path / "artifacts" / "private",
                experiment_id="rf_smoke_bad_root",
            )
        )
