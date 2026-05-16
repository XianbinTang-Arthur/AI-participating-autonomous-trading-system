import json
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from aats.data_platform.research_factory.metrics.snapshots import (
    COST_METRICS,
    EXECUTION_METRICS,
    METRIC_GROUPS,
    RETURN_METRICS,
    SIGNAL_METRICS,
    execution_cost_summary_to_metric_snapshot,
    load_execution_cost_summary_metrics,
    merge_metric_snapshots,
    metric_group_for,
    metric_snapshot_to_dict,
    serialize_metric_snapshot,
)
from aats.data_platform.research_factory.specs import METRIC_FIELDS, MetricsSnapshot


@pytest.fixture
def workspace_tmp_path() -> Iterator[Path]:
    path = Path(".pytest_workspace_tmp") / f"research_factory_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def complete_metrics_snapshot(**overrides: float | None) -> MetricsSnapshot:
    values: dict[str, float | None] = {
        "ic": 0.1,
        "rank_ic": 0.2,
        "icir": 0.3,
        "rank_icir": 0.4,
        "annualized_return": 0.05,
        "net_annualized_return": 0.03,
        "information_ratio": 0.7,
        "sharpe": 0.8,
        "max_drawdown": -0.1,
        "turnover": 0.2,
        "fee_bps_mean": 5.0,
        "slippage_bps_mean": 2.0,
        "funding_bps_mean": 0.5,
        "fillable_ratio": 0.9,
        "partial_fill_ratio": 0.05,
        "cost_adjusted_edge_bps_mean": 1.2,
    }
    values.update(overrides)
    missing_reasons = {
        metric_name: f"{metric_name} not measured"
        for metric_name, value in values.items()
        if value is None
    }
    return MetricsSnapshot(**values, missing_reasons=missing_reasons)


def test_metric_groups_cover_all_snapshot_fields() -> None:
    grouped = SIGNAL_METRICS + RETURN_METRICS + COST_METRICS + EXECUTION_METRICS

    assert grouped == METRIC_FIELDS
    assert METRIC_GROUPS["signal"] == SIGNAL_METRICS
    assert metric_group_for("net_annualized_return") == "return"
    assert metric_group_for("cost_adjusted_edge_bps_mean") == "execution"


def test_null_metric_requires_missing_reason() -> None:
    with pytest.raises(ValueError, match="missing without a reason"):
        MetricsSnapshot(
            ic=None,
            rank_ic=0.2,
            icir=0.3,
            rank_icir=0.4,
            annualized_return=0.05,
            net_annualized_return=0.03,
            information_ratio=0.7,
            sharpe=0.8,
            max_drawdown=-0.1,
            turnover=0.2,
            fee_bps_mean=5.0,
            slippage_bps_mean=2.0,
            funding_bps_mean=0.5,
            fillable_ratio=0.9,
            partial_fill_ratio=0.05,
            cost_adjusted_edge_bps_mean=1.2,
        )


def test_metric_snapshot_to_dict_is_grouped() -> None:
    snapshot = complete_metrics_snapshot()

    payload = metric_snapshot_to_dict(snapshot)

    assert payload["groups"]["signal"]["ic"] == pytest.approx(0.1)
    assert payload["groups"]["return"]["max_drawdown"] == pytest.approx(-0.1)
    assert payload["groups"]["cost"]["fee_bps_mean"] == pytest.approx(5.0)
    assert payload["groups"]["execution"]["fillable_ratio"] == pytest.approx(0.9)


def test_merge_metric_snapshots_rejects_same_field_conflict() -> None:
    left = complete_metrics_snapshot(ic=0.1)
    right = complete_metrics_snapshot(ic=0.2)

    with pytest.raises(ValueError, match="conflicting metric 'ic'"):
        merge_metric_snapshots(left, right)


def test_merge_metric_snapshots_allows_explicit_prefer_right_strategy() -> None:
    left = complete_metrics_snapshot(ic=0.1)
    right = complete_metrics_snapshot(ic=0.2)

    merged = merge_metric_snapshots(left, right, conflict_strategy="prefer_right")

    assert merged.ic == pytest.approx(0.2)


def test_merge_metric_snapshots_combines_missing_and_present_values() -> None:
    left = complete_metrics_snapshot(fillable_ratio=None)
    right = complete_metrics_snapshot(fillable_ratio=0.95)

    merged = merge_metric_snapshots(left, right, conflict_strategy="prefer_right")

    assert merged.fillable_ratio == pytest.approx(0.95)
    assert "fillable_ratio" not in merged.missing_reasons


def test_json_serialization_is_stable() -> None:
    snapshot = complete_metrics_snapshot(fillable_ratio=None)

    rendered = serialize_metric_snapshot(snapshot)
    rendered_again = serialize_metric_snapshot(snapshot)
    payload = json.loads(rendered)

    assert rendered == rendered_again
    assert rendered.endswith("\n")
    assert payload["metrics"]["fillable_ratio"] is None
    assert payload["missing_reasons"]["fillable_ratio"] == "fillable_ratio not measured"


def test_execution_cost_summary_adapter_maps_full_fill_ratio() -> None:
    snapshot = execution_cost_summary_to_metric_snapshot(execution_cost_summary())

    assert snapshot.fillable_ratio == pytest.approx(0.875)


def test_execution_cost_summary_adapter_maps_slippage_mean() -> None:
    snapshot = execution_cost_summary_to_metric_snapshot(execution_cost_summary())

    assert snapshot.slippage_bps_mean == pytest.approx(2.25)


def test_execution_cost_summary_adapter_maps_cost_adjusted_edge_mean() -> None:
    snapshot = execution_cost_summary_to_metric_snapshot(execution_cost_summary())

    assert snapshot.cost_adjusted_edge_bps_mean == pytest.approx(1.75)


def test_execution_cost_summary_adapter_reads_json_file(workspace_tmp_path: Path) -> None:
    path = workspace_tmp_path / "execution_cost_summary.json"
    path.write_text(json.dumps(execution_cost_summary()), encoding="utf-8")

    snapshot = load_execution_cost_summary_metrics(path)

    assert snapshot.fillable_ratio == pytest.approx(0.875)
    assert snapshot.partial_fill_ratio == pytest.approx(0.1)


def test_execution_cost_summary_adapter_missing_file_returns_missing_reason(workspace_tmp_path: Path) -> None:
    snapshot = load_execution_cost_summary_metrics(workspace_tmp_path / "missing_execution_cost_summary.json")

    assert snapshot.fillable_ratio is None
    assert snapshot.cost_adjusted_edge_bps_mean is None
    assert snapshot.missing_reasons["fillable_ratio"] == "execution cost summary file not found"
    assert snapshot.missing_reasons["cost_adjusted_edge_bps_mean"] == "execution cost summary file not found"


def test_execution_cost_summary_adapter_missing_field_returns_missing_reason() -> None:
    summary = execution_cost_summary()
    del summary["cost_adjusted_edge"]["mean"]

    snapshot = execution_cost_summary_to_metric_snapshot(summary)

    assert snapshot.cost_adjusted_edge_bps_mean is None
    assert (
        snapshot.missing_reasons["cost_adjusted_edge_bps_mean"]
        == "execution cost summary missing field: cost_adjusted_edge.mean"
    )


def execution_cost_summary() -> dict:
    return {
        "total_candidates": 8,
        "full_fill_ratio": 0.875,
        "partial_fill_ratio": 0.1,
        "slippage": {"mean": 2.25, "p95": 4.0},
        "total_execution_cost": {"mean": 7.25},
        "cost_adjusted_edge": {"mean": 1.75, "p95": 4.2},
        "positive_edge_ratio": 0.75,
    }
