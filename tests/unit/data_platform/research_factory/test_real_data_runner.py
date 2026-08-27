import json
import shutil
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from aats.data_platform.research_factory import real_data as real_data_module
from aats.data_platform.research_factory.contract_lineage import (
    ContractAwareArtifactLineage,
)
from aats.data_platform.research_factory.datasets.gold_bars import GoldBarRecord
from aats.data_platform.research_factory.real_data import (
    GoldReplayDataSource,
    GoldReplayLoadResult,
    ResearchFactoryExperimentConfig,
    run_research_factory_experiment,
)
from aats.data_platform.research_factory.proposals import FactorDSLProposal
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


@pytest.fixture
def future_contract_lineage_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicitly simulate the future immutable artifact verifier in tests."""

    monkeypatch.setattr(
        real_data_module,
        "_verify_contract_aware_artifact_lineage",
        lambda **_kwargs: True,
    )


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
                "benchmark_segment": "valid",
                "window_start": (START + timedelta(hours=4.8)).isoformat(),
                "window_end": (START + timedelta(hours=7.2)).isoformat(),
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
    include_contract_lineage: bool = True,
    contract_lineage_verified: bool = True,
    lineage_symbol: str | None = None,
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
        contract_lineage=(
            ContractAwareArtifactLineage(
                artifact_output_fingerprint="a" * 64,
                instrument_snapshot_digest="b" * 64,
                instrument_snapshot_source_ref=(
                    "meta.data_source_registry/instrument-snapshot-test"
                ),
                verification_ref="meta.historical_research_artifacts/test-artifact",
                symbol=lineage_symbol or records[0].symbol,
                timeframe=records[0].timeframe,
                coverage_start=records[0].ts.isoformat(),
                coverage_end=(records[-1].ts + timedelta(hours=1)).isoformat(),
                verified=contract_lineage_verified,
            )
            if include_contract_lineage
            else None
        ),
    )


def experiment_config(
    root: Path,
    *,
    execution_cost_summary_path: Path | None,
    require_execution_realism: bool = True,
    experiment_id: str = "rf_real_success",
    proposal: FactorDSLProposal | None = None,
    research_profile: str | None = None,
) -> ResearchFactoryExperimentConfig:
    return ResearchFactoryExperimentConfig(
        symbol="BTC-USDT-SWAP",
        timeframe="1h",
        start=START,
        end=START + timedelta(hours=12),
        factor_expression="Return(close, 1)",
        proposal=proposal,
        research_profile=research_profile,
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


def test_gold_replay_data_source_joins_and_fingerprints_microstructure_fields() -> None:
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
            build_run_id="gold-run-1",
            top5_weighted_imbalance=0.25,
            trade_flow_imbalance=0.4,
            bbo_samples_n=900,
            books5_samples_n=900,
            ob_dataset_version="p1d_microstructure_v1.0",
            ob_ingest_run_id="micro-run-1",
            ob_quality_flags=(),
            trade_count=100,
            tf_dataset_version="p1d_microstructure_v1.0",
            tf_ingest_run_id="micro-run-1",
            tf_quality_flags=(),
            oi_samples_n=10,
            oi_dataset_version="p1d_microstructure_v1.0",
            oi_ingest_run_id="micro-run-1",
            oi_quality_flags=(),
        )
    ]
    session = FakeSession(rows)

    result = GoldReplayDataSource(session).load(
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
        start=START,
        end=START + timedelta(minutes=15),
        dataset_version="v1.0",
        required_factor_fields=(
            "top5_weighted_imbalance",
            "trade_flow_imbalance",
        ),
    )

    assert "silver.market_orderbook_metrics_15m" in session.sql
    assert "silver.market_trade_flow_15m" in session.sql
    assert result.records[0].feature_values == {
        "top5_weighted_imbalance": pytest.approx(0.25),
        "trade_flow_imbalance": pytest.approx(0.4),
    }
    assert result.source_tables == (
        "gold.market_swap_replay_bars_15m",
        "silver.market_orderbook_metrics_15m",
        "silver.market_trade_flow_15m",
    )
    micro = result.source_watermark["microstructure"]
    assert micro["eligible_non_null_counts"] == {
        "top5_weighted_imbalance": 1,
        "trade_flow_imbalance": 1,
    }
    assert micro["source_fingerprint"].startswith("sha256:")


def test_gold_replay_data_source_rejects_microstructure_on_non_15m_timeframe() -> None:
    with pytest.raises(ValueError, match="require_15m"):
        GoldReplayDataSource(FakeSession([])).load(
            symbol="BTC-USDT-SWAP",
            timeframe="1h",
            start=START,
            end=START + timedelta(hours=1),
            required_factor_fields=("trade_flow_imbalance",),
        )


def test_gold_replay_data_source_nulls_microstructure_when_lineage_mismatches() -> None:
    row = SimpleNamespace(
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
        build_run_id="gold-run-1",
        top5_weighted_imbalance=0.25,
        trade_flow_imbalance=0.4,
        bbo_samples_n=900,
        books5_samples_n=900,
        ob_dataset_version="micro-v1",
        ob_ingest_run_id="micro-run-1",
        ob_quality_flags=(),
        trade_count=100,
        tf_dataset_version="micro-v2",
        tf_ingest_run_id="micro-run-1",
        tf_quality_flags=(),
        oi_samples_n=10,
        oi_dataset_version="micro-v1",
        oi_ingest_run_id="micro-run-1",
        oi_quality_flags=(),
    )

    result = GoldReplayDataSource(FakeSession([row])).load(
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
        start=START,
        end=START + timedelta(minutes=15),
        required_factor_fields=(
            "top5_weighted_imbalance",
            "trade_flow_imbalance",
        ),
    )

    assert result.records[0].feature_values == {
        "top5_weighted_imbalance": None,
        "trade_flow_imbalance": None,
    }


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


def test_real_data_runner_writes_recommendation_and_registry(
    tmp_path: Path,
    future_contract_lineage_verifier: None,
) -> None:
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
    development_evidence = read_json(experiment_dir / "development_evidence.json")
    development_returns = read_json(experiment_dir / "development_return_series.json")
    candidate = read_json(experiment_dir / "candidate_artifact.json")
    registry_entries = read_jsonl(root.parent / "registry" / "research_memory.jsonl")

    assert result.status == "succeeded"
    assert result.candidate_generated is True
    assert result.recommendation_ref == "research_recommendation.json"
    assert result.development_evidence_ref == "development_evidence.json"
    assert result.dataset_fingerprint
    assert (experiment_dir / "execution_cost_summary.json").exists()
    assert read_json(experiment_dir / "execution_cost_summary.json")["cost_adjusted_edge"]["mean"] == pytest.approx(1.75)
    assert manifest["output_refs"]["execution_cost_summary"] == "execution_cost_summary.json"
    assert manifest["output_refs"]["dataset_quality_report"] == "dataset_quality_report.json"
    assert manifest["output_refs"]["source_integrity_report"] == "source_integrity_report.json"
    assert manifest["output_refs"]["execution_evidence_report"] == "execution_evidence_report.json"
    assert manifest["output_refs"]["evidence_bundle"] == "evidence_bundle.json"
    assert manifest["output_refs"]["development_evidence"] == "development_evidence.json"
    assert (
        manifest["output_refs"]["development_return_series"]
        == "development_return_series.json"
    )
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
    assert execution_evidence["benchmark_segment"] == "valid"
    assert execution_evidence["window_start"] == (START + timedelta(hours=4.8)).isoformat()
    assert execution_evidence["dataset_fingerprint_compatible"] is True
    assert recommendation["evidence"]["execution_realism_required"] is True
    assert recommendation["evidence"]["benchmark_segment"] == "valid"
    assert (
        "sealed test holdout has not been evaluated; metrics are development evidence"
        in recommendation["evidence"]["limitations"]
    )
    assert recommendation["evidence"]["evidence_refs"]["execution_cost_summary"] == "execution_cost_summary.json"
    assert recommendation["evidence"]["evidence_refs"]["novelty_gate_result"] == "novelty_gate_result.json"
    assert recommendation["evidence"]["evidence_refs"]["evidence_bundle"] == "evidence_bundle.json"
    assert recommendation["evidence"]["evidence_refs"]["dataset_quality_report"] == "dataset_quality_report.json"
    assert (
        recommendation["evidence"]["evidence_refs"]["development_evidence"]
        == "development_evidence.json"
    )
    assert (
        recommendation["evidence"]["evidence_refs"]["development_return_series"]
        == "development_return_series.json"
    )
    assert development_evidence["schema_version"] == "train_valid_selection_test_holdout_v2"
    assert development_evidence["selection_rule"] == "train_and_valid_must_pass"
    assert development_evidence["benchmark_segment"] == "valid"
    assert set(development_evidence["segments"]) == {"train", "valid"}
    assert development_evidence["segments"]["train"]["gate"]["passed"] is True
    assert development_evidence["segments"]["valid"]["gate"]["passed"] is True
    assert (
        development_evidence["segments"]["train"]["metrics"][
            "cost_adjusted_edge_bps_mean"
        ]
        != pytest.approx(1.75)
    )
    assert development_evidence["segments"]["valid"]["metrics"][
        "cost_adjusted_edge_bps_mean"
    ] == pytest.approx(1.75)
    assert development_evidence["holdout"]["segment"] == "test"
    assert development_evidence["holdout"]["status"] == "sealed_not_evaluated"
    assert development_evidence["holdout"]["metrics_exposed"] is False
    assert "metrics" not in development_evidence["holdout"]
    assert development_returns["schema_version"] == "research_development_return_series_v1"
    assert set(development_returns["segments"]) == {"train", "valid"}
    assert development_returns["benchmark_segment"] == "valid"
    assert development_returns["segments"]["valid"]["net_returns"]
    assert development_returns["segments"]["valid"]["sample_count"] == len(
        development_returns["segments"]["valid"]["net_returns"]
    )
    assert development_returns["segments"]["valid"]["series_fingerprint"].startswith(
        "sha256:"
    )
    assert development_returns["holdout"] == {
        "segment": "test",
        "status": "sealed_not_evaluated",
        "content_fingerprint": development_evidence["holdout"]["content_fingerprint"],
        "values_exposed": False,
    }
    assert candidate["payload"]["benchmark_segment"] == "valid"
    assert candidate["payload"]["development_segments"] == ["train", "valid"]
    assert (
        candidate["payload"]["development_return_series_ref"]
        == "development_return_series.json"
    )
    assert candidate["payload"]["holdout_status"] == "sealed_not_evaluated"
    assert candidate["payload"]["symbol"] == "BTC-USDT-SWAP"
    assert candidate["payload"]["timeframe"] == "1h"
    assert candidate["payload"]["source_contract_lineage"] == {
        "artifact_output_fingerprint": "a" * 64,
        "instrument_snapshot_digest": "b" * 64,
        "instrument_snapshot_source_ref": (
            "meta.data_source_registry/instrument-snapshot-test"
        ),
        "schema_version": "research_contract_artifact_lineage_v1",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "1h",
        "coverage_start": START.isoformat(),
        "coverage_end": (START + timedelta(hours=12)).isoformat(),
        "verification_ref": "meta.historical_research_artifacts/test-artifact",
        "verified": True,
    }
    assert (
        candidate["payload"]["holdout_content_fingerprint"]
        == development_evidence["holdout"]["content_fingerprint"]
    )
    assert registry_entries[0]["status"] == "recommendation_ready"
    assert registry_entries[0]["created_by"] == "research_factory_real_data_runner"


def test_derivative_research_input_without_contract_lineage_fails_closed(
    tmp_path: Path,
) -> None:
    root = artifact_root(tmp_path)
    result = run_research_factory_experiment(
        experiment_config(
            root,
            execution_cost_summary_path=None,
            require_execution_realism=False,
            experiment_id="rf_unbound_derivative",
        ),
        data_source=FakeDataSource(load_result(include_contract_lineage=False)),
    )

    assert result.status == "failed"
    assert result.candidate_generated is False
    assert result.error == "derivative_research_input_contract_lineage_required"
    assert not (root / "rf_unbound_derivative" / "candidate_artifact.json").exists()


def test_unknown_instrument_cannot_fall_through_to_spot_research(
    tmp_path: Path,
) -> None:
    root = artifact_root(tmp_path)
    config = replace(
        experiment_config(
            root,
            execution_cost_summary_path=None,
            require_execution_realism=False,
            experiment_id="rf_unknown_instrument",
        ),
        symbol="BTC-USDT-260925",
    )

    with pytest.raises(
        ValueError,
        match="instrument_scope_unsupported_or_unproven",
    ):
        run_research_factory_experiment(
            config,
            data_source=FakeDataSource(load_result()),
        )

    assert not (root / config.experiment_id).exists()


def test_derivative_self_attested_lineage_cannot_authorize_without_verifier(
    tmp_path: Path,
) -> None:
    root = artifact_root(tmp_path)
    result = run_research_factory_experiment(
        experiment_config(
            root,
            execution_cost_summary_path=None,
            require_execution_realism=False,
            experiment_id="rf_self_attested_derivative",
        ),
        data_source=FakeDataSource(load_result(contract_lineage_verified=True)),
    )

    assert result.status == "failed"
    assert result.candidate_generated is False
    assert result.error == (
        "derivative_research_input_contract_lineage_verifier_unavailable"
    )


def test_derivative_research_input_rejects_lineage_for_a_different_scope(
    tmp_path: Path,
) -> None:
    root = artifact_root(tmp_path)
    result = run_research_factory_experiment(
        experiment_config(
            root,
            execution_cost_summary_path=None,
            require_execution_realism=False,
            experiment_id="rf_mismatched_derivative_lineage",
        ),
        data_source=FakeDataSource(
            load_result(lineage_symbol="ETH-USDT-SWAP")
        ),
    )

    assert result.status == "failed"
    assert result.candidate_generated is False
    assert result.error == "derivative_research_input_contract_lineage_scope_mismatch"


def test_spot_research_input_does_not_require_derivative_contract_lineage(
    tmp_path: Path,
) -> None:
    root = artifact_root(tmp_path)
    spot_records = tuple(
        replace(record, symbol="BTC-USDT") for record in gold_records()
    )
    config = replace(
        experiment_config(
            root,
            execution_cost_summary_path=None,
            require_execution_realism=False,
            experiment_id="rf_spot_without_contract_lineage",
        ),
        symbol="BTC-USDT",
    )

    result = run_research_factory_experiment(
        config,
        data_source=FakeDataSource(
            load_result(spot_records, include_contract_lineage=False)
        ),
    )

    assert result.status == "succeeded"
    assert result.candidate_generated is True
    candidate = read_json(root / config.experiment_id / "candidate_artifact.json")
    assert candidate["payload"]["symbol"] == "BTC-USDT"
    assert "source_contract_lineage" not in candidate["payload"]


def test_real_data_runner_does_not_evaluate_sealed_test_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    future_contract_lineage_verifier: None,
) -> None:
    root = artifact_root(tmp_path)
    observed_segments: list[tuple[datetime, ...]] = []
    original_evaluator = real_data_module.evaluate_factor_expression

    def tracking_evaluator(expression, rows):
        observed_segments.append(tuple(row["ts"] for row in rows))
        return original_evaluator(expression, rows)

    monkeypatch.setattr(real_data_module, "evaluate_factor_expression", tracking_evaluator)
    result = run_research_factory_experiment(
        experiment_config(
            root,
            execution_cost_summary_path=None,
            require_execution_realism=False,
            experiment_id="rf_real_holdout_not_evaluated",
        ),
        data_source=FakeDataSource(load_result()),
    )

    test_timestamps = {START + timedelta(hours=index) for index in range(8, 12)}
    assert result.status == "succeeded"
    assert len(observed_segments) == 2
    assert all(not (set(segment) & test_timestamps) for segment in observed_segments)


def test_real_data_runner_requires_train_and_valid_gates_to_pass(
    tmp_path: Path,
    future_contract_lineage_verifier: None,
) -> None:
    root = artifact_root(tmp_path)
    closes = (105.0, 104.0, 103.0, 102.0, 101.0, 100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0)
    records = tuple(
        replace(
            record,
            open=close - 0.25,
            high=close + 0.75,
            low=close - 1.0,
            close=close,
        )
        for record, close in zip(gold_records(), closes, strict=True)
    )

    result = run_research_factory_experiment(
        experiment_config(
            root,
            execution_cost_summary_path=None,
            require_execution_realism=False,
            experiment_id="rf_real_train_gate_failure",
        ),
        data_source=FakeDataSource(load_result(records)),
    )

    experiment_dir = root / "rf_real_train_gate_failure"
    development_evidence = read_json(experiment_dir / "development_evidence.json")
    development_returns = read_json(experiment_dir / "development_return_series.json")
    assert result.status == "failed"
    assert result.candidate_generated is False
    assert "development selection gate failed: train:" in result.error
    assert development_evidence["segments"]["train"]["gate"]["passed"] is False
    assert development_evidence["segments"]["valid"]["gate"]["passed"] is True
    assert development_evidence["holdout"]["metrics_exposed"] is False
    assert development_returns["segments"]["train"]["net_returns"]
    assert development_returns["segments"]["valid"]["net_returns"]
    assert development_returns["holdout"]["values_exposed"] is False
    assert (experiment_dir / "factor_input_quality_report.json").is_file()
    assert not (experiment_dir / "candidate_artifact.json").exists()


def test_holdout_content_change_does_not_change_development_metrics(
    tmp_path: Path,
    future_contract_lineage_verifier: None,
) -> None:
    root_a = tmp_path / "case_a" / "artifacts" / "research" / "research_factory" / "experiments"
    root_b = tmp_path / "case_b" / "artifacts" / "research" / "research_factory" / "experiments"
    original_records = gold_records()
    changed_records = list(original_records)
    changed_records[-1] = replace(changed_records[-1], close=107.8)

    result_a = run_research_factory_experiment(
        experiment_config(
            root_a,
            execution_cost_summary_path=None,
            require_execution_realism=False,
            experiment_id="rf_real_holdout_a",
        ),
        data_source=FakeDataSource(load_result(original_records)),
    )
    result_b = run_research_factory_experiment(
        experiment_config(
            root_b,
            execution_cost_summary_path=None,
            require_execution_realism=False,
            experiment_id="rf_real_holdout_b",
        ),
        data_source=FakeDataSource(load_result(tuple(changed_records))),
    )

    candidate_a = read_json(root_a / result_a.experiment_id / "candidate_artifact.json")
    candidate_b = read_json(root_b / result_b.experiment_id / "candidate_artifact.json")
    assert result_a.status == result_b.status == "succeeded"
    assert candidate_a["metrics"] == candidate_b["metrics"]
    assert (
        candidate_a["payload"]["holdout_content_fingerprint"]
        != candidate_b["payload"]["holdout_content_fingerprint"]
    )


def test_real_data_runner_applies_research_profile_quality_thresholds(
    tmp_path: Path,
    future_contract_lineage_verifier: None,
) -> None:
    root = artifact_root(tmp_path)
    execution_summary = root.parent / "phase4" / "execution_cost_summary.json"
    write_execution_cost_summary(execution_summary)

    result = run_research_factory_experiment(
        experiment_config(
            root,
            execution_cost_summary_path=execution_summary,
            require_execution_realism=False,
            experiment_id="rf_real_profile_strict",
            research_profile="real_factor_research",
        ),
        data_source=FakeDataSource(load_result()),
    )

    experiment_dir = root / "rf_real_profile_strict"
    dataset_quality = read_json(experiment_dir / "dataset_quality_report.json")
    evidence_bundle = read_json(experiment_dir / "evidence_bundle.json")

    assert result.status == "failed"
    assert result.candidate_generated is False
    assert dataset_quality["thresholds"]["min_total_bars"] == 500
    assert any("min_total_bars=500" in failure for failure in dataset_quality["failures"])
    assert any("dataset_quality" in failure for failure in evidence_bundle["failures"])


def test_real_data_runner_profile_rejects_compatible_execution_evidence(
    tmp_path: Path,
    future_contract_lineage_verifier: None,
) -> None:
    root = artifact_root(tmp_path)
    execution_summary = root.parent / "phase4" / "execution_cost_summary.json"
    write_execution_cost_summary(execution_summary)

    result = run_research_factory_experiment(
        experiment_config(
            root,
            execution_cost_summary_path=execution_summary,
            experiment_id="rf_real_profile_exact_evidence",
            research_profile="paper_review",
        ),
        data_source=FakeDataSource(load_result()),
    )

    experiment_dir = root / "rf_real_profile_exact_evidence"
    execution_evidence = read_json(experiment_dir / "execution_evidence_report.json")

    assert result.status == "failed"
    assert execution_evidence["dataset_fingerprint_compatible"] is True
    assert any("compatibility is not allowed" in failure for failure in execution_evidence["failures"])


def test_real_data_runner_records_factor_proposal_artifact(
    tmp_path: Path,
    future_contract_lineage_verifier: None,
) -> None:
    root = artifact_root(tmp_path)
    execution_summary = root.parent / "phase4" / "execution_cost_summary.json"
    write_execution_cost_summary(execution_summary)
    proposal = FactorDSLProposal(
        hypothesis="Short horizon close momentum may preserve positive executable edge.",
        factor_expression="Return(close, 1)",
        rationale="Submit only a safe Factor DSL expression before novelty and evidence gates.",
        created_at=START,
    )

    result = run_research_factory_experiment(
        experiment_config(
            root,
            execution_cost_summary_path=execution_summary,
            experiment_id="rf_real_with_proposal",
            proposal=proposal,
        ),
        data_source=FakeDataSource(load_result()),
    )

    experiment_dir = root / "rf_real_with_proposal"
    manifest = read_json(experiment_dir / "experiment_manifest.json")
    proposal_artifact = read_json(experiment_dir / "factor_proposal.json")
    candidate = read_json(experiment_dir / "candidate_artifact.json")
    recommendation = read_json(experiment_dir / "research_recommendation.json")

    assert result.status == "succeeded"
    assert result.proposal_ref == "factor_proposal.json"
    assert manifest["output_refs"]["factor_proposal"] == "factor_proposal.json"
    assert proposal_artifact["hypothesis"] == proposal.hypothesis
    assert proposal_artifact["factor_expression"] == "Return(close, 1)"
    assert candidate["payload"]["factor_proposal_ref"] == "factor_proposal.json"
    assert candidate["payload"]["factor_proposal_id"] == proposal.proposal_id
    assert recommendation["evidence"]["evidence_refs"]["factor_proposal"] == "factor_proposal.json"


def test_real_data_runner_rejects_proposal_expression_mismatch(tmp_path: Path) -> None:
    root = artifact_root(tmp_path)
    proposal = FactorDSLProposal(
        hypothesis="Two bar close momentum should not be silently substituted.",
        factor_expression="Return(close, 2)",
        rationale="The proposal expression must be the exact executed research factor.",
        created_at=START,
    )

    with pytest.raises(ValueError, match="proposal factor_expression must match"):
        run_research_factory_experiment(
            experiment_config(
                root,
                execution_cost_summary_path=None,
                require_execution_realism=False,
                experiment_id="rf_real_proposal_mismatch",
                proposal=proposal,
            ),
            data_source=FakeDataSource(load_result()),
        )


def test_real_data_runner_skips_duplicate_from_novelty_gate(
    tmp_path: Path,
    future_contract_lineage_verifier: None,
) -> None:
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


def test_real_data_runner_suppresses_repeated_failed_factor_family(
    tmp_path: Path,
    future_contract_lineage_verifier: None,
) -> None:
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


def test_real_data_runner_records_warn_novelty_gate_and_continues(
    tmp_path: Path,
    future_contract_lineage_verifier: None,
) -> None:
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


def test_real_data_runner_rejects_mixed_source_candle_versions(
    tmp_path: Path,
    future_contract_lineage_verifier: None,
) -> None:
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


def test_real_data_runner_rejects_mixed_build_run_ids(
    tmp_path: Path,
    future_contract_lineage_verifier: None,
) -> None:
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


def test_real_data_runner_rejects_missing_funding_alignment(
    tmp_path: Path,
    future_contract_lineage_verifier: None,
) -> None:
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


def test_real_data_runner_rejects_execution_summary_missing_contract_fields(
    tmp_path: Path,
    future_contract_lineage_verifier: None,
) -> None:
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


def test_real_data_runner_rejects_execution_summary_covering_sealed_test(
    tmp_path: Path,
    future_contract_lineage_verifier: None,
) -> None:
    root = artifact_root(tmp_path)
    execution_summary = root.parent / "phase4" / "execution_cost_summary.json"
    write_execution_cost_summary(execution_summary)
    payload = read_json(execution_summary)
    payload["window_start"] = START.isoformat()
    payload["window_end"] = (START + timedelta(hours=12)).isoformat()
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
    assert any("benchmark window_start" in failure for failure in execution_evidence["failures"])
    assert any("benchmark window_end" in failure for failure in execution_evidence["failures"])
    assert not (experiment_dir / "candidate_artifact.json").exists()


def test_real_data_runner_rejects_execution_summary_for_test_segment(
    tmp_path: Path,
    future_contract_lineage_verifier: None,
) -> None:
    root = artifact_root(tmp_path)
    execution_summary = root.parent / "phase4" / "execution_cost_summary.json"
    write_execution_cost_summary(execution_summary)
    payload = read_json(execution_summary)
    payload["benchmark_segment"] = "test"
    execution_summary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = run_research_factory_experiment(
        experiment_config(
            root,
            execution_cost_summary_path=execution_summary,
            experiment_id="rf_real_test_execution_evidence",
        ),
        data_source=FakeDataSource(load_result()),
    )

    experiment_dir = root / "rf_real_test_execution_evidence"
    execution_evidence = read_json(experiment_dir / "execution_evidence_report.json")

    assert result.status == "failed"
    assert execution_evidence["passed"] is False
    assert any("benchmark_segment must be valid" in failure for failure in execution_evidence["failures"])
    assert not (experiment_dir / "candidate_artifact.json").exists()


def test_real_data_runner_rejects_unsafe_execution_summary_path(
    tmp_path: Path,
    future_contract_lineage_verifier: None,
) -> None:
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


def test_real_data_runner_rejects_execution_summary_outside_research_artifacts(
    tmp_path: Path,
    future_contract_lineage_verifier: None,
) -> None:
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
    future_contract_lineage_verifier: None,
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
