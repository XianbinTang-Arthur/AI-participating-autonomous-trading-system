import json
import shutil
import uuid
from collections.abc import Iterator
from dataclasses import replace
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
from aats.data_platform.research_factory.registry import ResearchMemoryRegistry, build_research_memory_entry

UTC = timezone.utc
START = datetime(2026, 5, 1, tzinfo=UTC)


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    path = Path(".pytest_workspace_tmp") / f"real_data_runner_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class FakeDataSource:
    def __init__(self, load_result: GoldReplayLoadResult) -> None:
        self.load_result = load_result
        self.calls: list[dict] = []

    def load(self, **kwargs) -> GoldReplayLoadResult:
        self.calls.append(kwargs)
        return self.load_result


class FailingDataSource:
    def load(self, **kwargs) -> GoldReplayLoadResult:
        raise ValueError("no Gold replay bars found for requested research window")


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
                "schema_version": "execution_cost_summary_v1",
                "source_run_id": "phase4-run-1",
                "symbol": "BTC-USDT-SWAP",
                "timeframe": "1h",
                "window_start": START.isoformat(),
                "window_end": (START + timedelta(hours=12)).isoformat(),
                "dataset_fingerprint_compatibility": "compatible",
                "compatibility_reason": "unit test fixture uses the same configured dataset window",
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
                metadata={
                    "source_candle_dataset_version": "v1.0",
                    "source_funding_dataset_version": "funding_v1",
                    "build_run_id": "build-1",
                },
            )
        )
    return tuple(records)


def load_result(
    records: tuple[GoldBarRecord, ...] | None = None,
    *,
    dataset_version: str = "v1.0",
) -> GoldReplayLoadResult:
    records = records or gold_records()
    return GoldReplayLoadResult(
        records=records,
        source_watermark={
            "gold_table": "gold.market_swap_replay_bars_1h",
            "row_count": len(records),
            "min_ts": records[0].ts.isoformat(),
            "max_ts": records[-1].ts.isoformat(),
            "source_candle_dataset_versions": ["v1.0"],
            "source_funding_dataset_versions": ["funding_v1"],
            "build_run_ids": ["build-1"],
        },
        gold_table="gold.market_swap_replay_bars_1h",
        dataset_version=dataset_version,
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
    assert result.source_watermark["source_funding_dataset_versions"] == ["funding_v1"]
    assert result.source_watermark["timestamp_timezone_assumption"] == "timezone-aware database timestamp"


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
    evidence_bundle = read_json(experiment_dir / "evidence_bundle.json")
    dataset_quality = read_json(experiment_dir / "dataset_quality_report.json")
    source_integrity = read_json(experiment_dir / "source_integrity_report.json")
    execution_evidence = read_json(experiment_dir / "execution_evidence_report.json")
    registry_entries = read_jsonl(root.parent / "registry" / "research_memory.jsonl")

    assert result.status == "succeeded"
    assert result.candidate_generated is True
    assert result.recommendation_ref == "research_recommendation.json"
    assert result.dataset_fingerprint
    assert (experiment_dir / "execution_cost_summary.json").exists()
    assert read_json(experiment_dir / "execution_cost_summary.json")["cost_adjusted_edge"]["mean"] == pytest.approx(1.75)
    assert manifest["output_refs"]["execution_cost_summary"] == "execution_cost_summary.json"
    assert manifest["output_refs"]["dataset_quality_report"] == "dataset_quality_report.json"
    assert manifest["output_refs"]["source_integrity_report"] == "source_integrity_report.json"
    assert manifest["output_refs"]["execution_evidence_report"] == "execution_evidence_report.json"
    assert manifest["output_refs"]["evidence_bundle"] == "evidence_bundle.json"
    assert manifest["output_refs"]["novelty_gate_result"] == "novelty_gate_result.json"
    assert manifest["output_refs"]["research_recommendation"] == "research_recommendation.json"
    assert evidence_bundle["passed"] is True
    assert dataset_quality["row_count"] == 12
    assert dataset_quality["funding_missing_ratio"] == 0.0
    assert source_integrity["source_candle_dataset_versions"] == ["v1.0"]
    assert source_integrity["source_funding_dataset_versions"] == ["funding_v1"]
    assert source_integrity["build_run_consistent"] is True
    assert execution_evidence["contract_schema_version"] == "execution_cost_summary_v1"
    assert execution_evidence["source_run_id"] == "phase4-run-1"
    assert execution_evidence["window_start"] == START.isoformat()
    assert execution_evidence["dataset_fingerprint_compatible"] is True
    assert recommendation["evidence"]["execution_realism_required"] is True
    assert recommendation["evidence"]["evidence_refs"]["execution_cost_summary"] == "execution_cost_summary.json"
    assert recommendation["evidence"]["evidence_refs"]["novelty_gate_result"] == "novelty_gate_result.json"
    assert recommendation["evidence"]["evidence_refs"]["evidence_bundle"] == "evidence_bundle.json"
    assert recommendation["evidence"]["evidence_refs"]["dataset_quality_report"] == "dataset_quality_report.json"
    assert registry_entries[0]["status"] == "recommendation_ready"
    assert registry_entries[0]["created_by"] == "research_factory_real_data_runner"


def test_real_data_runner_skips_duplicate_from_novelty_gate(tmp_path: Path) -> None:
    root = artifact_root(tmp_path)
    execution_summary = root.parent / "phase4" / "execution_cost_summary.json"
    write_execution_cost_summary(execution_summary)
    first_data_source = FakeDataSource(load_result())
    first_result = run_research_factory_experiment(
        experiment_config(
            root,
            execution_cost_summary_path=execution_summary,
            experiment_id="rf_real_duplicate_seed",
        ),
        data_source=first_data_source,
    )
    second_data_source = FakeDataSource(load_result())

    second_result = run_research_factory_experiment(
        experiment_config(
            root,
            execution_cost_summary_path=execution_summary,
            experiment_id="rf_real_duplicate_skip",
        ),
        data_source=second_data_source,
    )

    experiment_dir = root / "rf_real_duplicate_skip"
    manifest = read_json(experiment_dir / "experiment_manifest.json")
    novelty_gate = read_json(experiment_dir / "novelty_gate_result.json")
    failure = read_json(experiment_dir / "failure.json")
    registry_entries = read_jsonl(root.parent / "registry" / "research_memory.jsonl")

    assert first_result.status == "succeeded"
    assert second_result.status == "failed"
    assert second_result.candidate_generated is False
    assert second_result.novelty_gate_ref == "novelty_gate_result.json"
    assert second_data_source.calls
    assert novelty_gate["decision"] == "duplicate"
    assert novelty_gate["should_run"] is False
    assert "novelty gate rejected proposal: duplicate" in failure["reason"]
    assert manifest["output_refs"]["novelty_gate_result"] == "novelty_gate_result.json"
    assert not (experiment_dir / "candidate_artifact.json").exists()
    assert registry_entries[-1]["status"] == "duplicate"
    assert registry_entries[-1]["dataset_fingerprint"] == first_result.dataset_fingerprint


def test_real_data_runner_suppresses_repeated_failed_factor_family(tmp_path: Path) -> None:
    root = artifact_root(tmp_path)
    execution_summary = root.parent / "phase4" / "execution_cost_summary.json"
    write_execution_cost_summary(execution_summary)
    registry = ResearchMemoryRegistry(root.parent / "registry" / "research_memory.jsonl")
    for index in range(1, 4):
        registry.upsert(
            build_research_memory_entry(
                experiment_id=f"rf_failed_factor_family_{index}",
                status="observation_rejected",
                created_by="unit_test",
                created_at=START + timedelta(hours=index),
                factor_expression="Return(close, 1)",
                dataset_fingerprint=f"sha256:failed-family-{index}",
                failure_reason="observation failed executable edge",
            )
        )

    result = run_research_factory_experiment(
        experiment_config(
            root,
            execution_cost_summary_path=execution_summary,
            experiment_id="rf_real_suppressed_family",
        ),
        data_source=FakeDataSource(load_result()),
    )

    experiment_dir = root / "rf_real_suppressed_family"
    novelty_gate = read_json(experiment_dir / "novelty_gate_result.json")
    registry_entries = read_jsonl(registry.path)

    assert result.status == "failed"
    assert result.candidate_generated is False
    assert novelty_gate["decision"] == "suppress"
    assert novelty_gate["failure_match_count"] == 3
    assert "prior failure outcomes" in novelty_gate["reasons"][0]
    assert registry_entries[-1]["status"] == "novelty_suppressed"
    assert not (experiment_dir / "candidate_artifact.json").exists()


def test_real_data_runner_records_warn_novelty_gate_and_continues(tmp_path: Path) -> None:
    root = artifact_root(tmp_path)
    execution_summary = root.parent / "phase4" / "execution_cost_summary.json"
    write_execution_cost_summary(execution_summary)
    seed = run_research_factory_experiment(
        experiment_config(
            root,
            execution_cost_summary_path=execution_summary,
            experiment_id="rf_real_warn_seed",
        ),
        data_source=FakeDataSource(load_result()),
    )
    registry = ResearchMemoryRegistry(root.parent / "registry" / "research_memory.jsonl")
    registry.upsert(
        build_research_memory_entry(
            experiment_id="rf_real_warn_prior_failure",
            status="observation_rejected",
            created_by="unit_test",
            created_at=START + timedelta(hours=14),
            factor_expression="Return(close, 3)",
            dataset_fingerprint=seed.dataset_fingerprint,
            failure_reason="observation failed on same dataset",
        )
    )

    result = run_research_factory_experiment(
        replace(
            experiment_config(
                root,
                execution_cost_summary_path=execution_summary,
                experiment_id="rf_real_warn_continue",
            ),
            factor_expression="Delta(close, 1)",
        ),
        data_source=FakeDataSource(load_result()),
    )

    experiment_dir = root / "rf_real_warn_continue"
    novelty_gate = read_json(experiment_dir / "novelty_gate_result.json")
    manifest = read_json(experiment_dir / "experiment_manifest.json")

    assert result.status == "succeeded"
    assert result.candidate_generated is True
    assert novelty_gate["decision"] == "warn"
    assert novelty_gate["should_run"] is True
    assert manifest["output_refs"]["novelty_gate_result"] == "novelty_gate_result.json"
    assert (experiment_dir / "candidate_artifact.json").exists()


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


def test_real_data_runner_writes_failure_artifact_when_gold_load_fails(tmp_path: Path) -> None:
    root = artifact_root(tmp_path)

    result = run_research_factory_experiment(
        experiment_config(
            root,
            execution_cost_summary_path=None,
            require_execution_realism=False,
            experiment_id="rf_real_load_failure",
        ),
        data_source=FailingDataSource(),
    )

    experiment_dir = root / "rf_real_load_failure"
    manifest = read_json(experiment_dir / "experiment_manifest.json")
    failure = read_json(experiment_dir / "failure.json")
    registry_entries = read_jsonl(root.parent / "registry" / "research_memory.jsonl")

    assert result.status == "failed"
    assert result.candidate_generated is False
    assert result.failure_ref == "failure.json"
    assert result.dataset_fingerprint is None
    assert manifest["status"] == "failed"
    assert manifest["output_refs"]["failure"] == "failure.json"
    assert "no Gold replay bars found" in failure["reason"]
    assert registry_entries[0]["status"] == "failed"
    assert "no Gold replay bars found" in registry_entries[0]["failure_reason"]
    assert not (experiment_dir / "candidate_artifact.json").exists()


def test_real_data_runner_rejects_mixed_source_candle_versions(tmp_path: Path) -> None:
    root = artifact_root(tmp_path)
    execution_summary = root.parent / "phase4" / "execution_cost_summary.json"
    write_execution_cost_summary(execution_summary)
    records = list(gold_records())
    records[-1] = replace(
        records[-1],
        metadata={
            "source_candle_dataset_version": "v2.0",
            "source_funding_dataset_version": "funding_v1",
            "build_run_id": "build-1",
        },
    )

    result = run_research_factory_experiment(
        experiment_config(
            root,
            execution_cost_summary_path=execution_summary,
            experiment_id="rf_real_mixed_versions",
        ),
        data_source=FakeDataSource(load_result(tuple(records))),
    )

    experiment_dir = root / "rf_real_mixed_versions"
    failure = read_json(experiment_dir / "failure.json")
    bundle = read_json(experiment_dir / "evidence_bundle.json")
    source_integrity = read_json(experiment_dir / "source_integrity_report.json")

    assert result.status == "failed"
    assert "evidence quality gate failed" in failure["reason"]
    assert bundle["passed"] is False
    assert source_integrity["source_candle_dataset_versions"] == ["v1.0", "v2.0"]
    assert not (experiment_dir / "candidate_artifact.json").exists()


def test_real_data_runner_rejects_mixed_build_run_ids(tmp_path: Path) -> None:
    root = artifact_root(tmp_path)
    execution_summary = root.parent / "phase4" / "execution_cost_summary.json"
    write_execution_cost_summary(execution_summary)
    records = list(gold_records())
    records[-1] = replace(
        records[-1],
        metadata={
            "source_candle_dataset_version": "v1.0",
            "source_funding_dataset_version": "funding_v1",
            "build_run_id": "build-2",
        },
    )

    result = run_research_factory_experiment(
        experiment_config(
            root,
            execution_cost_summary_path=execution_summary,
            experiment_id="rf_real_mixed_builds",
        ),
        data_source=FakeDataSource(load_result(tuple(records))),
    )

    experiment_dir = root / "rf_real_mixed_builds"
    source_integrity = read_json(experiment_dir / "source_integrity_report.json")
    bundle = read_json(experiment_dir / "evidence_bundle.json")

    assert result.status == "failed"
    assert source_integrity["build_run_ids"] == ["build-1", "build-2"]
    assert source_integrity["build_run_consistent"] is False
    assert "build_run_id" in bundle["failures"][0]
    assert not (experiment_dir / "candidate_artifact.json").exists()


def test_real_data_runner_rejects_missing_funding_alignment(tmp_path: Path) -> None:
    root = artifact_root(tmp_path)
    execution_summary = root.parent / "phase4" / "execution_cost_summary.json"
    write_execution_cost_summary(execution_summary)
    records = list(gold_records())
    records[3] = replace(records[3], funding_rate=None)

    result = run_research_factory_experiment(
        experiment_config(
            root,
            execution_cost_summary_path=execution_summary,
            experiment_id="rf_real_missing_funding",
        ),
        data_source=FakeDataSource(load_result(tuple(records))),
    )

    experiment_dir = root / "rf_real_missing_funding"
    bundle = read_json(experiment_dir / "evidence_bundle.json")
    dataset_quality = read_json(experiment_dir / "dataset_quality_report.json")

    assert result.status == "failed"
    assert bundle["passed"] is False
    assert dataset_quality["funding_missing_count"] == 1
    assert "funding_missing_ratio" in bundle["failures"][0]
    assert not (experiment_dir / "candidate_artifact.json").exists()


def test_real_data_runner_rejects_execution_summary_missing_contract_fields(tmp_path: Path) -> None:
    root = artifact_root(tmp_path)
    execution_summary = root.parent / "phase4" / "execution_cost_summary.json"
    write_execution_cost_summary(execution_summary)
    payload = read_json(execution_summary)
    payload.pop("schema_version")
    payload.pop("source_run_id")
    payload.pop("dataset_fingerprint_compatibility")
    payload.pop("compatibility_reason")
    execution_summary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = run_research_factory_experiment(
        experiment_config(
            root,
            execution_cost_summary_path=execution_summary,
            experiment_id="rf_real_missing_execution_contract",
        ),
        data_source=FakeDataSource(load_result()),
    )

    experiment_dir = root / "rf_real_missing_execution_contract"
    bundle = read_json(experiment_dir / "evidence_bundle.json")
    execution_evidence = read_json(experiment_dir / "execution_evidence_report.json")

    assert result.status == "failed"
    assert bundle["passed"] is False
    assert execution_evidence["passed"] is False
    assert "schema_version" in execution_evidence["failures"][0]
    assert any("source_run_id" in failure for failure in execution_evidence["failures"])
    assert any("dataset_fingerprint" in failure for failure in execution_evidence["failures"])
    assert not (experiment_dir / "candidate_artifact.json").exists()


def test_real_data_runner_rejects_execution_summary_window_mismatch(tmp_path: Path) -> None:
    root = artifact_root(tmp_path)
    execution_summary = root.parent / "phase4" / "execution_cost_summary.json"
    write_execution_cost_summary(execution_summary)
    payload = read_json(execution_summary)
    payload["window_end"] = (START + timedelta(hours=11)).isoformat()
    execution_summary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = run_research_factory_experiment(
        experiment_config(
            root,
            execution_cost_summary_path=execution_summary,
            experiment_id="rf_real_bad_execution_window",
        ),
        data_source=FakeDataSource(load_result()),
    )

    experiment_dir = root / "rf_real_bad_execution_window"
    bundle = read_json(experiment_dir / "evidence_bundle.json")
    execution_evidence = read_json(experiment_dir / "execution_evidence_report.json")

    assert result.status == "failed"
    assert bundle["passed"] is False
    assert execution_evidence["passed"] is False
    assert "window_end" in execution_evidence["failures"][0]
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
