import json
import shutil
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path, PurePath

import pytest

from aats.data_platform.research_factory.experiments.recorder import ExperimentRecorder
from aats.data_platform.research_factory.specs import (
    DatasetSpec,
    ExperimentSpec,
    FeatureSpec,
    LabelSpec,
    MetricsSnapshot,
    SegmentSpec,
)

UTC = timezone.utc


def dt(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=UTC)


def segment(name: str, start_day: int, end_day: int) -> SegmentSpec:
    return SegmentSpec(
        name=name,
        start=dt(start_day),
        end=dt(end_day),
        purpose=f"{name} segment",
    )


def dataset_spec() -> DatasetSpec:
    return DatasetSpec(
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
    )


def experiment_spec(root: Path, experiment_id: str = "exp_20260516_000001") -> ExperimentSpec:
    return ExperimentSpec(
        experiment_id=experiment_id,
        dataset=dataset_spec(),
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


def complete_metrics_snapshot() -> MetricsSnapshot:
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


def artifact_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "research" / "research_factory" / "experiments"


@pytest.fixture
def workspace_tmp_path() -> Iterator[Path]:
    path = Path(".pytest_workspace_tmp") / f"research_factory_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_start_writes_running_manifest_and_experiment_spec(workspace_tmp_path: Path) -> None:
    root = artifact_root(workspace_tmp_path)
    recorder = ExperimentRecorder(root, code_version="test-sha")
    spec = experiment_spec(root)

    manifest = recorder.start(spec)

    experiment_dir = root / spec.experiment_id
    stored_manifest = read_json(experiment_dir / "experiment_manifest.json")
    stored_spec = read_json(experiment_dir / "experiment_spec.json")
    assert manifest["status"] == "running"
    assert stored_manifest["status"] == "running"
    assert stored_manifest["code_version"] == "test-sha"
    assert stored_manifest["output_refs"] == {"experiment_spec": "experiment_spec.json"}
    assert stored_spec["experiment_id"] == spec.experiment_id
    assert stored_spec["dataset"]["dataset_id"] == "btc_15m_v1"


def test_record_metrics_writes_snapshot_and_updates_manifest(workspace_tmp_path: Path) -> None:
    root = artifact_root(workspace_tmp_path)
    recorder = ExperimentRecorder(root)
    spec = experiment_spec(root)
    recorder.start(spec)

    manifest = recorder.record_metrics(spec.experiment_id, complete_metrics_snapshot())

    experiment_dir = root / spec.experiment_id
    stored_metrics = read_json(experiment_dir / "metrics_snapshot.json")
    stored_manifest = read_json(experiment_dir / "experiment_manifest.json")
    assert stored_metrics["net_annualized_return"] == pytest.approx(0.03)
    assert manifest["metrics_ref"] == "metrics_snapshot.json"
    assert stored_manifest["output_refs"]["metrics_snapshot"] == "metrics_snapshot.json"
    assert stored_manifest["status"] == "running"


def test_finish_requires_terminal_status_and_writes_finished_manifest(workspace_tmp_path: Path) -> None:
    root = artifact_root(workspace_tmp_path)
    recorder = ExperimentRecorder(root)
    spec = experiment_spec(root)
    recorder.start(spec)

    with pytest.raises(ValueError, match="terminal"):
        recorder.finish(spec.experiment_id, "running")

    manifest = recorder.finish(spec.experiment_id, "succeeded")

    stored_manifest = read_json(root / spec.experiment_id / "experiment_manifest.json")
    assert manifest["status"] == "succeeded"
    assert stored_manifest["status"] == "succeeded"
    assert stored_manifest["finished_at"] is not None


def test_fail_writes_failure_artifact_without_sensitive_values(workspace_tmp_path: Path) -> None:
    root = artifact_root(workspace_tmp_path)
    recorder = ExperimentRecorder(root)
    spec = experiment_spec(root)
    recorder.start(spec)

    manifest = recorder.fail(spec.experiment_id, "password=abc token=def OKX_KEY=ghi")

    failure_path = root / spec.experiment_id / "failure.json"
    raw_failure = failure_path.read_text(encoding="utf-8")
    failure = json.loads(raw_failure)
    assert manifest["status"] == "failed"
    assert failure["reason"] == "[REDACTED]"
    assert failure["redacted"] is True
    assert "abc" not in raw_failure
    assert "def" not in raw_failure
    assert "ghi" not in raw_failure
    assert "OKX_KEY" not in raw_failure


def test_duplicate_experiment_id_rejects_overwrite(workspace_tmp_path: Path) -> None:
    root = artifact_root(workspace_tmp_path)
    recorder = ExperimentRecorder(root)
    spec = experiment_spec(root)
    recorder.start(spec)

    with pytest.raises(ValueError, match="already exists"):
        recorder.start(spec)


def test_manifest_output_refs_remain_relative(workspace_tmp_path: Path) -> None:
    root = artifact_root(workspace_tmp_path)
    recorder = ExperimentRecorder(root)
    spec = experiment_spec(root)
    recorder.start(spec)
    recorder.record_metrics(spec.experiment_id, complete_metrics_snapshot())
    recorder.fail(spec.experiment_id, "offline validation failed")

    manifest = read_json(root / spec.experiment_id / "experiment_manifest.json")
    for output_ref in manifest["output_refs"].values():
        path = PurePath(output_ref)
        assert not path.is_absolute()
        assert ".." not in path.parts
