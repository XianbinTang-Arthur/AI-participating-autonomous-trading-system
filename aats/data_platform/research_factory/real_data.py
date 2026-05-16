"""Real-data Research Factory runner backed by AATS Gold replay bars."""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.models import replay_bar_table_name
from aats.data_platform.research_factory.benchmarks.baseline import run_factor_baseline
from aats.data_platform.research_factory.datasets.gold_bars import (
    GoldBarDatasetHandler,
    GoldBarRecord,
    dataset_fingerprint,
)
from aats.data_platform.research_factory.datasets.segments import build_time_segments
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
from aats.data_platform.research_factory.paths import (
    copy_research_artifact_file,
    require_research_artifact_json_file,
)
from aats.data_platform.research_factory.recommendations import build_research_recommendation
from aats.data_platform.research_factory.registry import (
    ResearchMemoryRegistry,
    build_research_memory_entry,
    default_research_memory_path_for_artifact_root,
)
from aats.data_platform.research_factory.specs import (
    DatasetSpec,
    ExperimentSpec,
    FeatureSpec,
    LabelSpec,
    MetricsSnapshot,
)

DEFAULT_EXPERIMENT_ARTIFACT_ROOT = Path("artifacts") / "research" / "research_factory" / "experiments"
REAL_DATA_CODE_VERSION = "research_factory_real_data_runner_v1"
EXECUTION_COST_SUMMARY_REF = "execution_cost_summary.json"
TIMEFRAME_PERIODS_PER_YEAR = {
    "1m": 365.0 * 24.0 * 60.0,
    "5m": 365.0 * 24.0 * 12.0,
    "15m": 365.0 * 24.0 * 4.0,
    "1h": 365.0 * 24.0,
}


@dataclass(frozen=True, slots=True)
class GoldReplayLoadResult:
    """Gold replay rows and cache-key material loaded from RDP."""

    records: tuple[GoldBarRecord, ...]
    source_watermark: Mapping[str, Any]
    gold_table: str
    dataset_version: str

    def __post_init__(self) -> None:
        if not self.records:
            raise ValueError("gold replay load result must contain records")
        if not all(isinstance(record, GoldBarRecord) for record in self.records):
            raise ValueError("gold replay load records must be GoldBarRecord instances")
        if not isinstance(self.source_watermark, Mapping) or not self.source_watermark:
            raise ValueError("gold replay source_watermark must be a non-empty mapping")
        object.__setattr__(self, "records", tuple(self.records))
        object.__setattr__(self, "source_watermark", dict(self.source_watermark))
        object.__setattr__(self, "gold_table", _require_non_empty(self.gold_table, "gold_table"))
        object.__setattr__(self, "dataset_version", _require_non_empty(self.dataset_version, "dataset_version"))


@dataclass(frozen=True, slots=True)
class ResearchFactoryExperimentConfig:
    """Real-data research experiment inputs."""

    symbol: str
    timeframe: str
    start: datetime
    end: datetime
    factor_expression: str
    artifact_root: Path = DEFAULT_EXPERIMENT_ARTIFACT_ROOT
    experiment_id: str | None = None
    label_horizon_bars: int = 1
    dataset_version: str | None = None
    train_ratio: float = 0.6
    valid_ratio: float = 0.2
    test_ratio: float = 0.2
    fee_bps: float = 5.0
    slippage_bps: float = 2.0
    funding_bps: float = 0.5
    periods_per_year: float | None = None
    execution_cost_summary_path: Path | None = None
    require_execution_realism: bool = True
    registry_path: Path | None = None
    overwrite: bool = False
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class ResearchFactoryExperimentResult:
    """Concise real-data experiment result suitable for CLI JSON output."""

    experiment_id: str
    artifact_dir: str
    status: str
    candidate_generated: bool
    metrics_ref: str | None = None
    candidate_ref: str | None = None
    recommendation_ref: str | None = None
    registry_ref: str | None = None
    failure_ref: str | None = None
    dataset_fingerprint: str | None = None
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
            "registry_ref": self.registry_ref,
            "failure_ref": self.failure_ref,
            "dataset_fingerprint": self.dataset_fingerprint,
            "error": self.error,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


class GoldReplayDataSource:
    """Read-only adapter from RDP Gold replay bars to Research Factory records."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def load(
        self,
        *,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        dataset_version: str | None = None,
    ) -> GoldReplayLoadResult:
        symbol = _require_non_empty(symbol, "symbol").upper()
        _require_aware_datetime(start, "start")
        _require_aware_datetime(end, "end")
        if end <= start:
            raise ValueError("end must be after start")
        gold_table = replay_bar_table_name(symbol, timeframe)

        sql = f"""
            SELECT
                symbol, ts,
                open, high, low, close,
                volume, aligned_funding_rate,
                source_candle_dataset_version,
                source_funding_dataset_version,
                build_run_id
            FROM {gold_table}
            WHERE symbol = :symbol
              AND ts >= :start
              AND ts < :end
              AND is_closed = TRUE
        """
        params: dict[str, Any] = {"symbol": symbol, "start": start, "end": end}
        if dataset_version:
            sql += " AND source_candle_dataset_version = :dataset_version"
            params["dataset_version"] = dataset_version
        sql += " ORDER BY ts"

        rows = self.session.execute(text(sql), params).fetchall()
        records = tuple(_row_to_gold_bar_record(row, timeframe) for row in rows)
        if not records:
            raise ValueError("no Gold replay bars found for requested research window")

        row_dataset_versions = sorted(
            {
                str(getattr(row, "source_candle_dataset_version"))
                for row in rows
                if getattr(row, "source_candle_dataset_version", None) is not None
            }
        )
        resolved_dataset_version = dataset_version or (
            row_dataset_versions[0] if len(row_dataset_versions) == 1 else "gold_replay_mixed_versions"
        )
        source_watermark = {
            "gold_table": gold_table,
            "row_count": len(records),
            "min_ts": records[0].ts.isoformat(),
            "max_ts": records[-1].ts.isoformat(),
            "source_candle_dataset_versions": row_dataset_versions,
            "build_run_ids": sorted(
                {
                    str(getattr(row, "build_run_id"))
                    for row in rows
                    if getattr(row, "build_run_id", None) is not None
                }
            ),
        }
        return GoldReplayLoadResult(
            records=records,
            source_watermark=source_watermark,
            gold_table=gold_table,
            dataset_version=resolved_dataset_version,
        )


def run_research_factory_experiment(
    config: ResearchFactoryExperimentConfig,
    *,
    data_source: GoldReplayDataSource | None = None,
) -> ResearchFactoryExperimentResult:
    """Run a real-data Research Factory candidate experiment."""
    if not isinstance(config, ResearchFactoryExperimentConfig):
        raise ValueError("config must be a ResearchFactoryExperimentConfig")
    artifact_root = _require_research_artifact_root(config.artifact_root)
    experiment_id = config.experiment_id or _default_experiment_id(config)
    experiment_id = _require_safe_identifier(experiment_id, "experiment_id")
    experiment_dir = artifact_root / experiment_id
    registry = ResearchMemoryRegistry(
        config.registry_path or default_research_memory_path_for_artifact_root(artifact_root)
    )
    registry_ref = registry.path.as_posix()

    if config.overwrite:
        _remove_existing_experiment_dir(artifact_root, experiment_id)

    _require_aware_datetime(config.start, "start")
    _require_aware_datetime(config.end, "end")
    if config.end <= config.start:
        raise ValueError("end must be after start")
    if config.label_horizon_bars <= 0:
        raise ValueError("label_horizon_bars must be positive")
    if config.require_execution_realism and config.execution_cost_summary_path is None:
        preflight_execution_error = "execution realism summary is required for real-data ready_for_review"
    else:
        preflight_execution_error = None

    load_result = _load_gold_replay_records(config, data_source)
    segments = build_time_segments(
        config.start,
        config.end,
        config.train_ratio,
        config.valid_ratio,
        config.test_ratio,
    )
    dataset_spec = DatasetSpec(
        dataset_id=_dataset_id(config),
        symbol=config.symbol.upper(),
        timeframe=config.timeframe,
        dataset_version=load_result.dataset_version,
        window_start=config.start,
        window_end=config.end,
        segments=segments,
        source_refs={
            "gold_replay_bars": load_result.gold_table,
            "source_candle_dataset_version": config.dataset_version,
        },
    )
    research_dataset_fingerprint = dataset_fingerprint(
        dataset_spec,
        source_watermark=load_result.source_watermark,
        processor_versions={"research_factory_real_data_runner": REAL_DATA_CODE_VERSION},
    )
    feature = FeatureSpec(name="research_factor", expression=config.factor_expression)
    label = LabelSpec(
        name=f"future_simple_return_h{config.label_horizon_bars}",
        horizon_bars=config.label_horizon_bars,
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
        code_version=REAL_DATA_CODE_VERSION,
        clock=lambda: config.timestamp,
    )
    started = False
    metrics: MetricsSnapshot | None = None
    gate: CandidateGateResult | None = None
    candidate: CandidateArtifact | None = None
    try:
        recorder.start(experiment_spec)
        started = True
        if preflight_execution_error is not None:
            raise ValueError(preflight_execution_error)

        prepared = GoldBarDatasetHandler().prepare(load_result.records, dataset_spec)
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
                "periods_per_year": _periods_per_year(config),
            },
        )
        execution_cost_summary_ref = None
        if config.execution_cost_summary_path is not None:
            execution_cost_summary_path = require_research_artifact_json_file(
                config.execution_cost_summary_path,
                "execution_cost_summary_path",
                research_root=config.artifact_root,
            )
            execution_metrics = load_execution_cost_summary_metrics(execution_cost_summary_path)
            _require_complete_execution_realism_metrics(execution_metrics)
            metrics = merge_metric_snapshots(
                metrics,
                execution_metrics,
                conflict_strategy="prefer_right",
            )
            execution_cost_summary_ref = copy_research_artifact_file(
                execution_cost_summary_path,
                experiment_dir,
                destination_name=EXECUTION_COST_SUMMARY_REF,
                research_root=config.artifact_root,
            )
            recorder.record_output_ref(
                experiment_id,
                "execution_cost_summary",
                execution_cost_summary_ref,
            )
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
            dataset_fingerprint_value=research_dataset_fingerprint,
            benchmark_segment="test",
            execution_cost_summary_ref=execution_cost_summary_ref,
            created_at=config.timestamp,
        )
        recorder.record_candidate(experiment_id, candidate)
        recommendation = build_research_recommendation(
            candidate,
            evidence_refs=_recommendation_evidence_refs(execution_cost_summary_ref),
            created_at=config.timestamp,
            require_execution_realism=config.require_execution_realism,
        )
        manifest = recorder.record_recommendation(experiment_id, recommendation)
        registry.upsert(
            build_research_memory_entry(
                experiment_id=experiment_id,
                status="recommendation_ready",
                created_by="research_factory_real_data_runner",
                created_at=config.timestamp,
                candidate=candidate,
                metrics=metrics,
                gate=gate,
                artifact_refs=_memory_artifact_refs(experiment_id, manifest),
            )
        )
        manifest = recorder.finish(experiment_id, "succeeded")
        return ResearchFactoryExperimentResult(
            experiment_id=experiment_id,
            artifact_dir=experiment_dir.as_posix(),
            status=manifest["status"],
            candidate_generated=True,
            metrics_ref=manifest.get("metrics_ref"),
            candidate_ref=manifest["output_refs"].get("candidate_artifact"),
            recommendation_ref=manifest["output_refs"].get("research_recommendation"),
            registry_ref=registry_ref,
            dataset_fingerprint=research_dataset_fingerprint,
        )
    except Exception as exc:
        if started:
            manifest = recorder.fail(experiment_id, str(exc))
            registry.upsert(
                build_research_memory_entry(
                    experiment_id=experiment_id,
                    status=_memory_failure_status(gate),
                    created_by="research_factory_real_data_runner",
                    created_at=config.timestamp,
                    metrics=metrics,
                    gate=gate,
                    factor_expression=feature.expression,
                    dataset_fingerprint=research_dataset_fingerprint,
                    failure_reason=str(exc),
                    artifact_refs=_memory_artifact_refs(experiment_id, manifest),
                )
            )
            return ResearchFactoryExperimentResult(
                experiment_id=experiment_id,
                artifact_dir=experiment_dir.as_posix(),
                status=manifest["status"],
                candidate_generated=False,
                metrics_ref=manifest.get("metrics_ref"),
                registry_ref=registry_ref,
                failure_ref=manifest["output_refs"].get("failure"),
                dataset_fingerprint=research_dataset_fingerprint,
                error=str(exc),
            )
        raise


def _load_gold_replay_records(
    config: ResearchFactoryExperimentConfig,
    data_source: GoldReplayDataSource | None,
) -> GoldReplayLoadResult:
    if data_source is None:
        raise ValueError("data_source is required; CLI should provide a GoldReplayDataSource")
    return data_source.load(
        symbol=config.symbol,
        timeframe=config.timeframe,
        start=config.start,
        end=config.end,
        dataset_version=config.dataset_version,
    )


def _row_to_gold_bar_record(row: Any, timeframe: str) -> GoldBarRecord:
    ts = _coerce_aware_datetime(getattr(row, "ts"), "row.ts")
    volume = getattr(row, "volume")
    if volume is None:
        raise ValueError(f"Gold replay bar volume is missing at {ts.isoformat()}")
    return GoldBarRecord(
        symbol=str(getattr(row, "symbol")),
        timeframe=timeframe,
        ts=ts,
        open=getattr(row, "open"),
        high=getattr(row, "high"),
        low=getattr(row, "low"),
        close=getattr(row, "close"),
        volume=volume,
        funding_rate=getattr(row, "aligned_funding_rate", None),
        metadata={
            "source_candle_dataset_version": getattr(row, "source_candle_dataset_version", None),
            "source_funding_dataset_version": getattr(row, "source_funding_dataset_version", None),
            "build_run_id": getattr(row, "build_run_id", None),
        },
    )


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
    dataset_fingerprint_value: str,
    benchmark_segment: str,
    execution_cost_summary_ref: str | None,
    created_at: datetime,
) -> CandidateArtifact:
    return CandidateArtifact(
        candidate_id=f"cand_{experiment_id}",
        experiment_id=experiment_id,
        candidate_type="factor",
        payload={
            "factor_expression": feature.expression,
            "dataset_fingerprint": dataset_fingerprint_value,
            "benchmark_segment": benchmark_segment,
            "execution_cost_summary_ref": execution_cost_summary_ref,
            "generated_by": "research_factory_real_data_runner",
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


def _memory_artifact_refs(experiment_id: str, manifest: Mapping[str, Any]) -> dict[str, str]:
    refs = {"experiment_manifest": f"{experiment_id}/experiment_manifest.json"}
    metrics_ref = manifest.get("metrics_ref")
    if isinstance(metrics_ref, str) and metrics_ref:
        refs["metrics_snapshot"] = f"{experiment_id}/{metrics_ref}"
    output_refs = manifest.get("output_refs", {})
    if isinstance(output_refs, Mapping):
        for name, ref in output_refs.items():
            if isinstance(ref, str) and ref:
                refs[str(name)] = f"{experiment_id}/{ref}"
    return refs


def _memory_failure_status(gate: CandidateGateResult | None) -> str:
    if gate is not None and not gate.passed:
        return "gate_failed"
    return "failed"


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


def _periods_per_year(config: ResearchFactoryExperimentConfig) -> float:
    if config.periods_per_year is not None:
        return config.periods_per_year
    timeframe = config.timeframe.lower()
    if timeframe not in TIMEFRAME_PERIODS_PER_YEAR:
        allowed = ", ".join(sorted(TIMEFRAME_PERIODS_PER_YEAR))
        raise ValueError(f"timeframe must be one of: {allowed}")
    return TIMEFRAME_PERIODS_PER_YEAR[timeframe]


def _dataset_id(config: ResearchFactoryExperimentConfig) -> str:
    return f"{config.symbol.lower().replace('-', '_')}_{config.timeframe.lower()}_gold_replay"


def _default_experiment_id(config: ResearchFactoryExperimentConfig) -> str:
    start = config.start.strftime("%Y%m%dT%H%M%S")
    end = config.end.strftime("%Y%m%dT%H%M%S")
    return f"rf_{config.symbol.lower().replace('-', '_')}_{config.timeframe.lower()}_{start}_{end}"


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


def _require_non_empty(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_aware_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _coerce_aware_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value
