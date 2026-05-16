"""Metric snapshot taxonomy and merge helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from aats.data_platform.research_factory.numeric import require_finite_number
from aats.data_platform.research_factory.specs import METRIC_FIELDS, MetricsSnapshot

SIGNAL_METRICS = ("ic", "rank_ic", "icir", "rank_icir")
RETURN_METRICS = (
    "annualized_return",
    "net_annualized_return",
    "information_ratio",
    "sharpe",
    "max_drawdown",
)
COST_METRICS = ("turnover", "fee_bps_mean", "slippage_bps_mean", "funding_bps_mean")
EXECUTION_METRICS = ("fillable_ratio", "partial_fill_ratio", "cost_adjusted_edge_bps_mean")
METRIC_GROUPS: Mapping[str, tuple[str, ...]] = {
    "signal": SIGNAL_METRICS,
    "return": RETURN_METRICS,
    "cost": COST_METRICS,
    "execution": EXECUTION_METRICS,
}
MERGE_STRATEGIES = frozenset({"reject", "prefer_left", "prefer_right"})
EXECUTION_COST_SUMMARY_MAPPINGS = {
    "turnover": (("turnover", "mean"),),
    "fee_bps_mean": (("fee", "mean"), ("estimated_fee", "mean")),
    "slippage_bps_mean": (("slippage", "mean"),),
    "funding_bps_mean": (("funding", "mean"), ("funding_adjustment", "mean")),
    "fillable_ratio": (("full_fill_ratio",),),
    "partial_fill_ratio": (("partial_fill_ratio",),),
    "cost_adjusted_edge_bps_mean": (("cost_adjusted_edge", "mean"),),
}

_ALL_GROUPED_METRICS = tuple(metric for metrics in METRIC_GROUPS.values() for metric in metrics)
if tuple(METRIC_FIELDS) != _ALL_GROUPED_METRICS:
    raise RuntimeError("Research Factory metric taxonomy must cover METRIC_FIELDS exactly")


def metric_group_for(metric_name: str) -> str:
    """Return the taxonomy group for a metric field."""
    if not isinstance(metric_name, str) or not metric_name.strip():
        raise ValueError("metric_name must be a non-empty string")
    for group_name, metric_names in METRIC_GROUPS.items():
        if metric_name in metric_names:
            return group_name
    raise ValueError(f"unknown metric: {metric_name}")


def normalize_missing_reasons(missing_reasons: Mapping[str, str]) -> dict[str, str]:
    """Validate and stabilize a missing-reasons mapping."""
    if not isinstance(missing_reasons, Mapping):
        raise ValueError("missing_reasons must be a mapping")
    normalized: dict[str, str] = {}
    for metric_name, reason in missing_reasons.items():
        if not isinstance(metric_name, str) or not metric_name.strip():
            raise ValueError("missing reason metric name must be a non-empty string")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"missing reason for {metric_name!r} must be a non-empty string")
        normalized[metric_name] = reason
    return dict(sorted(normalized.items()))


def metric_snapshot_to_dict(snapshot: MetricsSnapshot) -> dict[str, Any]:
    """Serialize a MetricsSnapshot to a stable grouped dictionary."""
    _require_metric_snapshot(snapshot)
    metrics = {metric_name: getattr(snapshot, metric_name) for metric_name in METRIC_FIELDS}
    return {
        "groups": {
            group_name: {metric_name: metrics[metric_name] for metric_name in metric_names}
            for group_name, metric_names in METRIC_GROUPS.items()
        },
        "metrics": metrics,
        "missing_reasons": normalize_missing_reasons(snapshot.missing_reasons),
    }


def serialize_metric_snapshot(snapshot: MetricsSnapshot) -> str:
    """Render a MetricsSnapshot as stable JSON for artifact diffs."""
    return json.dumps(metric_snapshot_to_dict(snapshot), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def load_execution_cost_summary_metrics(path: str | Path) -> MetricsSnapshot:
    """Load Phase 4 execution_cost_summary.json as a Research Factory metrics snapshot."""
    source_path = Path(path)
    if not source_path.exists():
        return _missing_metrics_snapshot("execution cost summary file not found")
    if not source_path.is_file():
        return _missing_metrics_snapshot("execution cost summary path is not a file")

    try:
        with source_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError:
        return _missing_metrics_snapshot("execution cost summary is not valid JSON")

    return execution_cost_summary_to_metric_snapshot(payload)


def execution_cost_summary_to_metric_snapshot(summary: Mapping[str, Any]) -> MetricsSnapshot:
    """Extract Research Factory metrics from a Phase 4 execution cost summary."""
    if not isinstance(summary, Mapping):
        return _missing_metrics_snapshot("execution cost summary must be a mapping")

    values: dict[str, float | None] = {metric_name: None for metric_name in METRIC_FIELDS}
    missing_reasons = {
        metric_name: "not provided by Phase 4 execution cost summary"
        for metric_name in METRIC_FIELDS
    }

    for metric_name, paths in EXECUTION_COST_SUMMARY_MAPPINGS.items():
        value, reason = _read_first_numeric_path(summary, paths)
        if reason is None:
            values[metric_name] = value
            missing_reasons.pop(metric_name, None)
        else:
            missing_reasons[metric_name] = reason

    return MetricsSnapshot(**values, missing_reasons=missing_reasons)


def merge_metric_snapshots(
    *snapshots: MetricsSnapshot,
    conflict_strategy: Literal["reject", "prefer_left", "prefer_right"] = "reject",
) -> MetricsSnapshot:
    """Merge metric snapshots while rejecting same-field conflicts by default."""
    if conflict_strategy not in MERGE_STRATEGIES:
        allowed = ", ".join(sorted(MERGE_STRATEGIES))
        raise ValueError(f"conflict_strategy must be one of: {allowed}")
    if not snapshots:
        raise ValueError("at least one metrics snapshot is required")
    for snapshot in snapshots:
        _require_metric_snapshot(snapshot)

    merged_values: dict[str, float | None] = {metric_name: None for metric_name in METRIC_FIELDS}
    merged_missing_reasons: dict[str, str] = {}

    for snapshot in snapshots:
        missing_reasons = normalize_missing_reasons(snapshot.missing_reasons)
        for metric_name in METRIC_FIELDS:
            current_value = merged_values[metric_name]
            incoming_value = getattr(snapshot, metric_name)
            if incoming_value is not None:
                merged_values[metric_name] = _merge_metric_value(
                    metric_name,
                    current_value,
                    incoming_value,
                    conflict_strategy,
                )
                merged_missing_reasons.pop(metric_name, None)
                continue

            if merged_values[metric_name] is None:
                incoming_reason = missing_reasons[metric_name]
                merged_missing_reasons[metric_name] = _merge_missing_reason(
                    metric_name,
                    merged_missing_reasons.get(metric_name),
                    incoming_reason,
                    conflict_strategy,
                )

        for reason_key, reason in missing_reasons.items():
            if reason_key in METRIC_FIELDS:
                continue
            merged_missing_reasons[reason_key] = _merge_missing_reason(
                reason_key,
                merged_missing_reasons.get(reason_key),
                reason,
                conflict_strategy,
            )

    return MetricsSnapshot(**merged_values, missing_reasons=merged_missing_reasons)


def _merge_metric_value(
    metric_name: str,
    current_value: float | None,
    incoming_value: float,
    conflict_strategy: str,
) -> float:
    if current_value is None or current_value == incoming_value:
        return incoming_value
    if conflict_strategy == "prefer_left":
        return current_value
    if conflict_strategy == "prefer_right":
        return incoming_value
    raise ValueError(f"conflicting metric {metric_name!r}: {current_value!r} != {incoming_value!r}")


def _merge_missing_reason(
    reason_key: str,
    current_reason: str | None,
    incoming_reason: str,
    conflict_strategy: str,
) -> str:
    if current_reason is None or current_reason == incoming_reason:
        return incoming_reason
    if conflict_strategy == "prefer_left":
        return current_reason
    if conflict_strategy == "prefer_right":
        return incoming_reason
    raise ValueError(f"conflicting missing reason {reason_key!r}: {current_reason!r} != {incoming_reason!r}")


def _read_numeric_path(summary: Mapping[str, Any], path: tuple[str, ...]) -> tuple[float | None, str | None]:
    current: Any = summary
    rendered_path = ".".join(path)
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None, f"execution cost summary missing field: {rendered_path}"
        current = current[key]
    if current is None:
        return None, f"execution cost summary field is null: {rendered_path}"
    if isinstance(current, bool) or not isinstance(current, int | float | Decimal):
        return None, f"execution cost summary field is not numeric: {rendered_path}"
    try:
        return require_finite_number(current, rendered_path), None
    except ValueError:
        return None, f"execution cost summary field is not finite: {rendered_path}"


def _read_first_numeric_path(
    summary: Mapping[str, Any],
    paths: tuple[tuple[str, ...], ...],
) -> tuple[float | None, str | None]:
    reasons: list[str] = []
    for path in paths:
        value, reason = _read_numeric_path(summary, path)
        if reason is None:
            return value, None
        reasons.append(reason)
    return None, reasons[0]


def _missing_metrics_snapshot(reason: str) -> MetricsSnapshot:
    return MetricsSnapshot(missing_reasons={metric_name: reason for metric_name in METRIC_FIELDS})


def _require_metric_snapshot(snapshot: MetricsSnapshot) -> None:
    if not isinstance(snapshot, MetricsSnapshot):
        raise ValueError("snapshot must be a MetricsSnapshot")

    missing_reasons = normalize_missing_reasons(snapshot.missing_reasons)
    for metric_name in METRIC_FIELDS:
        if getattr(snapshot, metric_name) is None and not missing_reasons.get(metric_name):
            raise ValueError(f"metric {metric_name!r} is missing without a reason")

    for group_name, metric_names in METRIC_GROUPS.items():
        _require_metric_names(group_name, metric_names)


def _require_metric_names(group_name: str, metric_names: Sequence[str]) -> None:
    if not metric_names:
        raise ValueError(f"metric group {group_name!r} must not be empty")
    for metric_name in metric_names:
        if metric_name not in METRIC_FIELDS:
            raise ValueError(f"metric group {group_name!r} contains unknown metric {metric_name!r}")
