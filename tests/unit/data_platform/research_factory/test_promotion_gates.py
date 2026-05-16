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


def experiment_spec(root: Path, experiment_id: str = "exp_20260516_000002") -> ExperimentSpec:
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
        metrics=["ic", "rank_ic", "net_annualized_return"],
        artifact_root=str(root),
    )


def metrics_snapshot(
    *,
    net_annualized_return: float | None = 0.03,
    max_drawdown: float | None = -0.1,
    cost_adjusted_edge_bps_mean: float | None = 1.2,
    missing_reasons: dict[str, str] | None = None,
) -> MetricsSnapshot:
    return MetricsSnapshot(
        ic=0.1,
        rank_ic=0.2,
        icir=0.3,
        rank_icir=0.4,
        annualized_return=0.05,
        net_annualized_return=net_annualized_return,
        information_ratio=0.7,
        sharpe=0.8,
        max_drawdown=max_drawdown,
        turnover=0.2,
        fee_bps_mean=5.0,
        slippage_bps_mean=2.0,
        funding_bps_mean=0.5,
        fillable_ratio=0.9,
        partial_fill_ratio=0.05,
        cost_adjusted_edge_bps_mean=cost_adjusted_edge_bps_mean,
        missing_reasons=missing_reasons or {},
    )


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_candidate_gate_rejects_negative_net_return() -> None:
    result = evaluate_candidate_gate(
        metrics_snapshot(net_annualized_return=-0.01),
        {"max_drawdown_limit": 0.2},
    )

    assert result.passed is False
    assert any("net_annualized_return" in failure for failure in result.failures)


def test_candidate_gate_rejects_drawdown_over_limit() -> None:
    result = evaluate_candidate_gate(
        metrics_snapshot(max_drawdown=-0.25),
        {"max_drawdown_limit": 0.2},
    )

    assert result.passed is False
    assert any("max_drawdown" in failure for failure in result.failures)


def test_candidate_gate_rejects_missing_critical_metric() -> None:
    result = evaluate_candidate_gate(
        metrics_snapshot(
            net_annualized_return=None,
            missing_reasons={"net_annualized_return": "not enough out-of-sample bars"},
        ),
        {"max_drawdown_limit": 0.2},
    )

    assert result.passed is False
    assert any("net_annualized_return is missing" in failure for failure in result.failures)


def test_candidate_gate_rejects_non_finite_threshold() -> None:
    with pytest.raises(ValueError, match="finite"):
        evaluate_candidate_gate(
            metrics_snapshot(),
            {"max_drawdown_limit": float("inf")},
        )


def test_gate_pass_writes_candidate_artifact_without_active_parameter(workspace_tmp_path: Path) -> None:
    root = artifact_root(workspace_tmp_path)
    recorder = ExperimentRecorder(root, code_version="test-sha")
    spec = experiment_spec(root)
    metrics = metrics_snapshot()
    recorder.start(spec)
    recorder.record_metrics(spec.experiment_id, metrics)
    gate = evaluate_candidate_gate(metrics, {"max_drawdown_limit": 0.2})

    candidate = CandidateArtifact(
        candidate_id="cand_20260516_000001",
        experiment_id=spec.experiment_id,
        candidate_type="parameter",
        payload={"parameter_values": {"min_safe_net_edge_bps": 1.2}},
        metrics=metrics,
        gate=gate,
    )
    manifest = recorder.record_candidate(spec.experiment_id, candidate)

    experiment_dir = root / spec.experiment_id
    raw_candidate = (experiment_dir / "candidate_artifact.json").read_text(encoding="utf-8")
    stored_candidate = json.loads(raw_candidate)
    stored_manifest = read_json(experiment_dir / "experiment_manifest.json")
    assert manifest["output_refs"]["candidate_artifact"] == "candidate_artifact.json"
    assert stored_manifest["output_refs"]["candidate_artifact"] == "candidate_artifact.json"
    assert stored_candidate["gate"]["passed"] is True
    assert stored_candidate["candidate_type"] == "parameter"
    assert not (experiment_dir / "active_parameter.json").exists()
    assert not (experiment_dir / "active_parameter_set.json").exists()
    assert "active_parameter" not in raw_candidate


def test_candidate_artifact_rejects_active_parameter_payload() -> None:
    metrics = metrics_snapshot()
    gate = evaluate_candidate_gate(metrics, {"max_drawdown_limit": 0.2})

    with pytest.raises(ValueError, match="research-only"):
        CandidateArtifact(
            candidate_id="cand_20260516_000002",
            experiment_id="exp_20260516_000002",
            candidate_type="parameter",
            payload={"active_parameter_set": {"min_safe_net_edge_bps": 1.2}},
            metrics=metrics,
            gate=gate,
        )
