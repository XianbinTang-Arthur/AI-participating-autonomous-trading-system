"""End-to-end Research Factory smoke runner."""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aats.data_platform.research_factory.benchmarks.baseline import run_factor_baseline
from aats.data_platform.research_factory.datasets.gold_bars import (
    GoldBarDatasetHandler,
    GoldBarRecord,
    PreparedGoldBarDataset,
    dataset_fingerprint,
)
from aats.data_platform.research_factory.experiments.recorder import ExperimentRecorder
from aats.data_platform.research_factory.features.functions import evaluate_factor_expression
from aats.data_platform.research_factory.metrics.gates import (
    CandidateArtifact,
    CandidateGateResult,
    evaluate_candidate_gate,
)
from aats.data_platform.research_factory.metrics.snapshots import (
    load_execution_cost_summary_metrics,
    merge_metric_snapshots,
)
from aats.data_platform.research_factory.recommendations import build_research_recommendation
from aats.data_platform.research_factory.specs import (
    DatasetSpec,
    ExperimentSpec,
    FeatureSpec,
    LabelSpec,
    MetricsSnapshot,
    SegmentSpec,
)

DEFAULT_SMOKE_ARTIFACT_ROOT = Path("artifacts") / "research" / "research_factory" / "experiments"
DEFAULT_SMOKE_EXPERIMENT_ID = "rf_smoke_btc_swap_1h_v1"
DEFAULT_SMOKE_FACTOR_EXPRESSION = "Return(close, 1)"
DEFAULT_SMOKE_TIMESTAMP = datetime(2026, 5, 16, tzinfo=UTC)
SMOKE_CODE_VERSION = "research_factory_smoke_v1"
SMOKE_DATASET_VERSION = "research_factory_smoke_v1"


@dataclass(frozen=True, slots=True)
class ResearchFactorySmokeConfig:
    """Research-only smoke runner inputs."""

    artifact_root: Path = DEFAULT_SMOKE_ARTIFACT_ROOT
    experiment_id: str = DEFAULT_SMOKE_EXPERIMENT_ID
    factor_expression: str = DEFAULT_SMOKE_FACTOR_EXPRESSION
    overwrite: bool = False
    fee_bps: float = 5.0
    slippage_bps: float = 2.0
    funding_bps: float = 0.5
    periods_per_year: float = 1.0
    execution_cost_summary_path: Path | None = None
    timestamp: datetime = DEFAULT_SMOKE_TIMESTAMP


@dataclass(frozen=True, slots=True)
class ResearchFactorySmokeResult:
    """Concise smoke run summary suitable for CLI JSON output."""

    experiment_id: str
    artifact_dir: str
    status: str
    candidate_generated: bool
    metrics_ref: str | None = None
    candidate_ref: str | None = None
    recommendation_ref: str | None = None
    failure_ref: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "artifact_dir": self.artifact_dir,
            "status": self.status,
            "candidate_generated": self.candidate_generated,
            "metrics_ref": self.metrics_ref,
            "candidate_ref": self.candidate_ref,
            "recommendation_ref": self.recommendation_ref,
            "failure_ref": self.failure_ref,
            "error": self.error,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def run_research_factory_smoke(
    config: ResearchFactorySmokeConfig | None = None,
) -> ResearchFactorySmokeResult:
    """Run the deterministic research-only candidate generation loop."""
    config = config or ResearchFactorySmokeConfig()
    artifact_root = _require_research_artifact_root(config.artifact_root)
    experiment_id = _require_safe_identifier(config.experiment_id, "experiment_id")
    experiment_dir = artifact_root / experiment_id

    if config.overwrite:
        _remove_existing_experiment_dir(artifact_root, experiment_id)

    dataset_spec = _build_dataset_spec()
    feature = FeatureSpec(name="smoke_factor", expression=config.factor_expression)
    label = LabelSpec(
        name="future_simple_return_h1",
        horizon_bars=1,
        return_kind="simple_return",
        net_of_fee=True,
        net_of_slippage=True,
        include_funding=True,
        fee_bps=config.fee_bps,
        slippage_bps=config.slippage_bps,
    )
    experiment_spec = ExperimentSpec(
        experiment_id=experiment_id,
        dataset=dataset_spec,
        features=[feature],
        label=label,
        model_ref="baseline_long_flat",
        metrics=["net_annualized_return", "max_drawdown", "cost_adjusted_edge_bps_mean"],
        artifact_root=str(artifact_root),
        governance_mode="candidate_only",
    )

    recorder = ExperimentRecorder(
        artifact_root,
        code_version=SMOKE_CODE_VERSION,
        clock=lambda: config.timestamp,
    )
    started = False
    try:
        recorder.start(experiment_spec)
        started = True
        prepared = GoldBarDatasetHandler().prepare(_build_smoke_records(), dataset_spec)
        test_rows = prepared.rows_for_segment("test")
        factor_values = evaluate_factor_expression(feature.expression, test_rows).values
        label_values = _future_simple_returns(test_rows, label.horizon_bars)
        metrics = run_factor_baseline(
            prepared,
            factor_values,
            label_values,
            cost_config={
                "fee_bps": config.fee_bps,
                "slippage_bps": config.slippage_bps,
                "funding_bps": config.funding_bps,
                "periods_per_year": config.periods_per_year,
            },
        )
        execution_cost_summary_ref = None
        if config.execution_cost_summary_path is not None:
            execution_metrics = load_execution_cost_summary_metrics(
                config.execution_cost_summary_path
            )
            _require_complete_execution_realism_metrics(execution_metrics)
            metrics = merge_metric_snapshots(
                metrics,
                execution_metrics,
                conflict_strategy="prefer_right",
            )
            execution_cost_summary_ref = Path(config.execution_cost_summary_path).name
        recorder.record_metrics(experiment_id, metrics)
        gate = _deterministic_gate(metrics, config.timestamp)
        if not gate.passed:
            failures = "; ".join(gate.failures)
            raise ValueError(f"candidate gate failed: {failures}")
        candidate = _build_candidate(
            experiment_id=experiment_id,
            feature=feature,
            metrics=metrics,
            gate=gate,
            prepared=prepared,
            execution_cost_summary_ref=execution_cost_summary_ref,
            created_at=config.timestamp,
        )
        recorder.record_candidate(experiment_id, candidate)
        recommendation = build_research_recommendation(
            candidate,
            evidence_refs=_recommendation_evidence_refs(execution_cost_summary_ref),
            created_at=config.timestamp,
        )
        recorder.record_recommendation(experiment_id, recommendation)
        manifest = recorder.finish(experiment_id, "succeeded")
        return ResearchFactorySmokeResult(
            experiment_id=experiment_id,
            artifact_dir=experiment_dir.as_posix(),
            status=manifest["status"],
            candidate_generated=True,
            metrics_ref=manifest.get("metrics_ref"),
            candidate_ref=manifest["output_refs"].get("candidate_artifact"),
            recommendation_ref=manifest["output_refs"].get("research_recommendation"),
        )
    except Exception as exc:
        if started:
            manifest = recorder.fail(experiment_id, str(exc))
            return ResearchFactorySmokeResult(
                experiment_id=experiment_id,
                artifact_dir=experiment_dir.as_posix(),
                status=manifest["status"],
                candidate_generated=False,
                metrics_ref=manifest.get("metrics_ref"),
                failure_ref=manifest["output_refs"].get("failure"),
                error=str(exc),
            )
        raise


def _build_dataset_spec() -> DatasetSpec:
    start = DEFAULT_SMOKE_TIMESTAMP
    segments = (
        SegmentSpec("train", start, start + timedelta(hours=4), "smoke training segment"),
        SegmentSpec("valid", start + timedelta(hours=4), start + timedelta(hours=8), "smoke validation segment"),
        SegmentSpec("test", start + timedelta(hours=8), start + timedelta(hours=12), "smoke out-of-sample test segment"),
    )
    return DatasetSpec(
        dataset_id="btc_swap_1h_smoke",
        symbol="BTC-USDT-SWAP",
        timeframe="1h",
        dataset_version=SMOKE_DATASET_VERSION,
        window_start=start,
        window_end=start + timedelta(hours=12),
        segments=segments,
        source_refs={"gold": "research_factory_smoke_fixture"},
    )


def _build_smoke_records() -> tuple[GoldBarRecord, ...]:
    closes = (100.0, 100.8, 101.3, 102.1, 102.6, 103.4, 104.0, 104.9, 105.5, 106.4, 107.2, 108.3)
    records: list[GoldBarRecord] = []
    for index, close in enumerate(closes):
        ts = DEFAULT_SMOKE_TIMESTAMP + timedelta(hours=index)
        open_price = close - 0.25
        high = close + 0.75
        low = open_price - 0.75
        records.append(
            GoldBarRecord(
                symbol="BTC-USDT-SWAP",
                timeframe="1h",
                ts=ts,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=10_000.0 + index * 100.0,
                vwap=close - 0.05,
                funding_rate=0.0001,
            )
        )
    return tuple(records)


def _future_simple_returns(
    rows: Sequence[Mapping[str, Any]],
    horizon_bars: int,
) -> tuple[float | None, ...]:
    values: list[float | None] = []
    for index, row in enumerate(rows):
        target_index = index + horizon_bars
        if target_index >= len(rows):
            values.append(None)
            continue
        current_close = float(row["close"])
        future_close = float(rows[target_index]["close"])
        values.append(future_close / current_close - 1.0)
    return tuple(values)


def _deterministic_gate(metrics: MetricsSnapshot, evaluated_at: datetime) -> CandidateGateResult:
    gate = evaluate_candidate_gate(
        metrics,
        {
            "min_net_annualized_return": 0.0,
            "max_drawdown_limit": 0.2,
            "min_cost_adjusted_edge_bps_mean": 0.0,
            "critical_metrics": (
                "net_annualized_return",
                "max_drawdown",
                "cost_adjusted_edge_bps_mean",
            ),
        },
    )
    return CandidateGateResult(
        passed=gate.passed,
        failures=gate.failures,
        thresholds=gate.thresholds,
        critical_metrics=gate.critical_metrics,
        evaluated_at=evaluated_at,
    )


def _build_candidate(
    *,
    experiment_id: str,
    feature: FeatureSpec,
    metrics: MetricsSnapshot,
    gate: CandidateGateResult,
    prepared: PreparedGoldBarDataset,
    execution_cost_summary_ref: str | None,
    created_at: datetime,
) -> CandidateArtifact:
    fingerprint = dataset_fingerprint(
        prepared.dataset_spec,
        source_watermark={"fixture_max_ts": _build_smoke_records()[-1].ts.isoformat()},
        processor_versions={"research_factory_smoke": SMOKE_CODE_VERSION},
    )
    return CandidateArtifact(
        candidate_id=f"cand_{experiment_id}",
        experiment_id=experiment_id,
        candidate_type="factor",
        payload={
            "factor_expression": feature.expression,
            "dataset_fingerprint": fingerprint,
            "benchmark_segment": "test",
            "execution_cost_summary_ref": execution_cost_summary_ref,
            "generated_by": "research_factory_smoke_runner",
            "research_only": True,
        },
        metrics=metrics,
        gate=gate,
        created_at=created_at,
    )


def _recommendation_evidence_refs(execution_cost_summary_ref: str | None) -> dict[str, str]:
    refs = {
        "candidate_artifact": "candidate_artifact.json",
        "experiment_manifest": "experiment_manifest.json",
        "metrics_snapshot": "metrics_snapshot.json",
    }
    if execution_cost_summary_ref is not None:
        refs["execution_cost_summary"] = execution_cost_summary_ref
    return refs


def _require_complete_execution_realism_metrics(snapshot: MetricsSnapshot) -> None:
    required_metrics = (
        "turnover",
        "fee_bps_mean",
        "slippage_bps_mean",
        "funding_bps_mean",
        "fillable_ratio",
        "partial_fill_ratio",
        "cost_adjusted_edge_bps_mean",
    )
    missing = [metric for metric in required_metrics if getattr(snapshot, metric) is None]
    if missing:
        rendered = ", ".join(missing)
        raise ValueError(f"execution realism metrics missing required fields: {rendered}")


def _remove_existing_experiment_dir(root: Path, experiment_id: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    root_resolved = root.resolve()
    target = (root / experiment_id).resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("experiment directory must stay under artifact root") from exc
    if target.exists():
        shutil.rmtree(target)


def _require_research_artifact_root(value: Path) -> Path:
    path = Path(value)
    parts = path.parts
    if ".." in parts:
        raise ValueError("artifact root must not contain path traversal")
    if not any(
        parts[index] == "artifacts" and parts[index + 1] == "research"
        for index in range(len(parts) - 1)
    ):
        raise ValueError("artifact root must be under artifacts/research")
    return path


def _require_safe_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if "/" in value or "\\" in value or value in {".", ".."} or ".." in value:
        raise ValueError(f"{field_name} must not contain path traversal or separators")
    return value
