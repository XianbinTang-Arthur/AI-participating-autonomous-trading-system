import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from aats.data_platform.research_factory.datasets.gold_bars import GoldBarRecord
from aats.data_platform.research_factory.real_data import (
    GoldReplayDataSource,
    GoldReplayLoadResult,
    ResearchFactoryExperimentConfig,
    run_research_factory_experiment,
)

UTC = timezone.utc
START = datetime(2026, 5, 1, tzinfo=UTC)


class FakeDataSource:
    def __init__(self, load_result: GoldReplayLoadResult) -> None:
        self.load_result = load_result
        self.calls: list[dict] = []

    def load(self, **kwargs) -> GoldReplayLoadResult:
        self.calls.append(kwargs)
        return self.load_result


class FakeSession:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows
        self.sql = ""
        self.params = {}

    def execute(self, statement, params):
        self.sql = str(statement)
        self.params = params
        return SimpleNamespace(fetchall=lambda: self.rows)


def artifact_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "research" / "research_factory" / "experiments"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def write_execution_cost_summary(path: Path, *, cost_adjusted_edge: float = 1.75) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "full_fill_ratio": 0.9,
                "partial_fill_ratio": 0.1,
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


def gold_records() -> tuple[GoldBarRecord, ...]:
    closes = (100.0, 100.8, 101.3, 102.1, 102.6, 103.4, 104.0, 104.9, 105.5, 106.4, 107.2, 108.3)
    records: list[GoldBarRecord] = []
    for index, close in enumerate(closes):
        records.append(
            GoldBarRecord(
                symbol="BTC-USDT-SWAP",
                timeframe="1h",
                ts=START + timedelta(hours=index),
                open=close - 0.25,
                high=close + 0.75,
                low=close - 1.0,
                close=close,
                volume=10_000 + index * 100,
                funding_rate=0.0001,
            )
        )
    return tuple(records)


def load_result() -> GoldReplayLoadResult:
    return GoldReplayLoadResult(
        records=gold_records(),
        source_watermark={
            "gold_table": "gold.market_swap_replay_bars_1h",
            "row_count": 12,
            "min_ts": START.isoformat(),
            "max_ts": (START + timedelta(hours=11)).isoformat(),
        },
        gold_table="gold.market_swap_replay_bars_1h",
        dataset_version="v1.0",
    )


def experiment_config(
    root: Path,
    *,
    execution_cost_summary_path: Path | None,
    require_execution_realism: bool = True,
    experiment_id: str = "rf_real_success",
) -> ResearchFactoryExperimentConfig:
    return ResearchFactoryExperimentConfig(
        symbol="BTC-USDT-SWAP",
        timeframe="1h",
        start=START,
        end=START + timedelta(hours=12),
        factor_expression="Return(close, 1)",
        artifact_root=root,
        experiment_id=experiment_id,
        train_ratio=0.4,
        valid_ratio=0.2,
        test_ratio=0.4,
        execution_cost_summary_path=execution_cost_summary_path,
        require_execution_realism=require_execution_realism,
        overwrite=True,
    )


def test_gold_replay_data_source_loads_records_with_watermark() -> None:
    rows = [
        SimpleNamespace(
            symbol="BTC-USDT-SWAP",
            ts=START,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=10_000.0,
            aligned_funding_rate=0.0001,
            source_candle_dataset_version="v1.0",
            source_funding_dataset_version="funding_v1",
            build_run_id="run-1",
        )
    ]
    session = FakeSession(rows)

    result = GoldReplayDataSource(session).load(
        symbol="BTC-USDT-SWAP",
        timeframe="1h",
        start=START,
        end=START + timedelta(hours=1),
        dataset_version="v1.0",
    )

    assert "gold.market_swap_replay_bars_1h" in session.sql
    assert "source_candle_dataset_version = :dataset_version" in session.sql
    assert session.params["symbol"] == "BTC-USDT-SWAP"
    assert result.records[0].close == pytest.approx(100.5)
    assert result.source_watermark["row_count"] == 1
    assert result.source_watermark["build_run_ids"] == ["run-1"]


def test_real_data_config_timestamp_defaults_to_current_utc(tmp_path: Path) -> None:
    before = datetime.now(UTC)
    config = ResearchFactoryExperimentConfig(
        symbol="BTC-USDT-SWAP",
        timeframe="1h",
        start=START,
        end=START + timedelta(hours=12),
        factor_expression="Return(close, 1)",
        artifact_root=artifact_root(tmp_path),
    )
    after = datetime.now(UTC)

    assert before <= config.timestamp <= after
    assert config.timestamp != datetime(2026, 5, 16, tzinfo=UTC)


def test_real_data_runner_writes_recommendation_and_registry(tmp_path: Path) -> None:
    root = artifact_root(tmp_path)
    execution_summary = root.parent / "phase4" / "execution_cost_summary.json"
    write_execution_cost_summary(execution_summary)

    result = run_research_factory_experiment(
        experiment_config(root, execution_cost_summary_path=execution_summary),
        data_source=FakeDataSource(load_result()),
    )

    experiment_dir = root / "rf_real_success"
    manifest = read_json(experiment_dir / "experiment_manifest.json")
    recommendation = read_json(experiment_dir / "research_recommendation.json")
    registry_entries = read_jsonl(root.parent / "registry" / "research_memory.jsonl")

    assert result.status == "succeeded"
    assert result.candidate_generated is True
    assert result.recommendation_ref == "research_recommendation.json"
    assert result.dataset_fingerprint
    assert (experiment_dir / "execution_cost_summary.json").exists()
    assert read_json(experiment_dir / "execution_cost_summary.json")["cost_adjusted_edge"]["mean"] == pytest.approx(1.75)
    assert manifest["output_refs"]["execution_cost_summary"] == "execution_cost_summary.json"
    assert manifest["output_refs"]["research_recommendation"] == "research_recommendation.json"
    assert recommendation["evidence"]["execution_realism_required"] is True
    assert recommendation["evidence"]["evidence_refs"]["execution_cost_summary"] == "execution_cost_summary.json"
    assert registry_entries[0]["status"] == "recommendation_ready"
    assert registry_entries[0]["created_by"] == "research_factory_real_data_runner"


def test_real_data_runner_fails_when_execution_realism_required_but_missing(tmp_path: Path) -> None:
    root = artifact_root(tmp_path)

    result = run_research_factory_experiment(
        experiment_config(
            root,
            execution_cost_summary_path=None,
            require_execution_realism=True,
            experiment_id="rf_real_missing_exec",
        ),
        data_source=FakeDataSource(load_result()),
    )

    experiment_dir = root / "rf_real_missing_exec"
    failure = read_json(experiment_dir / "failure.json")
    registry_entries = read_jsonl(root.parent / "registry" / "research_memory.jsonl")

    assert result.status == "failed"
    assert result.candidate_generated is False
    assert "execution realism summary is required" in failure["reason"]
    assert registry_entries[0]["status"] == "failed"
    assert "execution realism summary is required" in registry_entries[0]["failure_reason"]
    assert not (experiment_dir / "candidate_artifact.json").exists()


def test_real_data_runner_rejects_unsafe_execution_summary_path(tmp_path: Path) -> None:
    root = artifact_root(tmp_path)
    unsafe_path = tmp_path / "artifacts" / "research" / "live_execution_cost_summary.json"
    write_execution_cost_summary(unsafe_path)

    result = run_research_factory_experiment(
        experiment_config(
            root,
            execution_cost_summary_path=unsafe_path,
            experiment_id="rf_real_unsafe_exec",
        ),
        data_source=FakeDataSource(load_result()),
    )

    experiment_dir = root / "rf_real_unsafe_exec"
    failure = read_json(experiment_dir / "failure.json")

    assert result.status == "failed"
    assert "forbidden path token" in result.error
    assert failure["reason"] == "[REDACTED]"
    assert not (experiment_dir / "execution_cost_summary.json").exists()


def test_real_data_runner_rejects_execution_summary_outside_research_artifacts(tmp_path: Path) -> None:
    root = artifact_root(tmp_path)
    outside_path = tmp_path / "execution_cost_summary.json"
    write_execution_cost_summary(outside_path)

    result = run_research_factory_experiment(
        experiment_config(
            root,
            execution_cost_summary_path=outside_path,
            experiment_id="rf_real_outside_exec",
        ),
        data_source=FakeDataSource(load_result()),
    )

    failure = read_json(root / "rf_real_outside_exec" / "failure.json")

    assert result.status == "failed"
    assert "under artifacts/research" in failure["reason"]


def test_real_data_runner_rejects_execution_summary_outside_configured_research_root(
    tmp_path: Path,
) -> None:
    root = artifact_root(tmp_path)
    other_research_root = tmp_path / "other" / "artifacts" / "research"
    outside_path = other_research_root / "phase4" / "execution_cost_summary.json"
    write_execution_cost_summary(outside_path)

    result = run_research_factory_experiment(
        experiment_config(
            root,
            execution_cost_summary_path=outside_path,
            experiment_id="rf_real_other_root_exec",
        ),
        data_source=FakeDataSource(load_result()),
    )

    failure = read_json(root / "rf_real_other_root_exec" / "failure.json")

    assert result.status == "failed"
    assert "under artifacts/research" in failure["reason"]
