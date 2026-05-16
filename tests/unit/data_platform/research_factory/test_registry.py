import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aats.data_platform.research_factory.metrics.gates import CandidateArtifact, evaluate_candidate_gate
from aats.data_platform.research_factory.registry import (
    ResearchMemoryEntry,
    ResearchMemoryRegistry,
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
