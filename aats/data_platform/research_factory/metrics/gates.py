"""Deterministic candidate promotion gates for Research Factory experiments."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from aats.data_platform.research_factory.numeric import require_finite_number
from aats.data_platform.research_factory.specs import MetricsSnapshot

ALLOWED_CANDIDATE_TYPES = frozenset(
    {
        "factor",
        "model",
        "parameter",
        "execution_policy",
        "risk_budget",
        "regime_classifier",
    }
)
DEFAULT_CRITICAL_METRICS = (
    "net_annualized_return",
    "max_drawdown",
    "cost_adjusted_edge_bps_mean",
)
DEFAULT_THRESHOLDS = {
    "min_net_annualized_return": 0.0,
    "max_drawdown_limit": 0.2,
    "min_cost_adjusted_edge_bps_mean": 0.0,
}
FORBIDDEN_CANDIDATE_TERMS = (
    "active_parameter",
    "active_parameters",
    "apply",
    "live_order",
    "okx_write",
    "operator_write",
    "production_config",
)


@dataclass(frozen=True, slots=True)
class CandidateGateResult:
    """Deterministic gate result for candidate artifact generation."""

    passed: bool
    failures: tuple[str, ...]
    thresholds: Mapping[str, Any]
    critical_metrics: tuple[str, ...] = DEFAULT_CRITICAL_METRICS
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool):
            raise ValueError("gate passed must be a bool")
        if not all(isinstance(failure, str) and failure.strip() for failure in self.failures):
            raise ValueError("gate failures must be non-empty strings")
        if self.passed and self.failures:
            raise ValueError("passing gate must not contain failures")
        if not self.passed and not self.failures:
            raise ValueError("failing gate must contain at least one failure")
        if not isinstance(self.thresholds, Mapping):
            raise ValueError("gate thresholds must be a mapping")
        _reject_nonfinite_threshold_values(self.thresholds)
        if not all(isinstance(metric, str) and metric.strip() for metric in self.critical_metrics):
            raise ValueError("critical metrics must be non-empty strings")
        object.__setattr__(self, "thresholds", dict(self.thresholds))
        object.__setattr__(self, "critical_metrics", tuple(self.critical_metrics))


@dataclass(frozen=True, slots=True)
class CandidateArtifact:
    """Research-only candidate artifact that cannot represent active parameters."""

    candidate_id: str
    experiment_id: str
    candidate_type: str
    payload: Mapping[str, Any]
    metrics: MetricsSnapshot
    gate: CandidateGateResult
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        _require_safe_identifier(self.candidate_id, "candidate_id")
        _require_safe_identifier(self.experiment_id, "experiment_id")
        if self.candidate_type not in ALLOWED_CANDIDATE_TYPES:
            allowed = ", ".join(sorted(ALLOWED_CANDIDATE_TYPES))
            raise ValueError(f"candidate_type must be one of: {allowed}")
        if not isinstance(self.payload, Mapping) or not self.payload:
            raise ValueError("candidate payload must be a non-empty mapping")
        if not isinstance(self.metrics, MetricsSnapshot):
            raise ValueError("candidate metrics must be a MetricsSnapshot")
        if not isinstance(self.gate, CandidateGateResult):
            raise ValueError("candidate gate must be a CandidateGateResult")
        if not self.gate.passed:
            raise ValueError("candidate artifact requires a passing gate")
        _reject_forbidden_candidate_terms(self.payload)
        object.__setattr__(self, "payload", dict(self.payload))


def evaluate_candidate_gate(
    metrics_snapshot: MetricsSnapshot,
    thresholds: Mapping[str, Any] | None,
) -> CandidateGateResult:
    """Evaluate whether metrics are strong enough to generate a candidate artifact."""
    if not isinstance(metrics_snapshot, MetricsSnapshot):
        raise ValueError("metrics_snapshot must be a MetricsSnapshot")

    normalized_thresholds = _normalize_thresholds(thresholds or {})
    failures: list[str] = []
    missing_reasons = dict(metrics_snapshot.missing_reasons)

    if missing_reasons.get("candidate_generated") == "false":
        failures.append("candidate_generated=false")

    for metric_name in normalized_thresholds["critical_metrics"]:
        if getattr(metrics_snapshot, metric_name) is None:
            failures.append(f"{metric_name} is missing")
        elif missing_reasons.get(metric_name):
            failures.append(f"{metric_name} has missing reason: {missing_reasons[metric_name]}")

    net_annualized_return = metrics_snapshot.net_annualized_return
    if (
        net_annualized_return is not None
        and net_annualized_return <= normalized_thresholds["min_net_annualized_return"]
    ):
        failures.append(
            "net_annualized_return="
            f"{net_annualized_return:.6f} <= {normalized_thresholds['min_net_annualized_return']:.6f}"
        )

    max_drawdown = metrics_snapshot.max_drawdown
    if max_drawdown is not None:
        drawdown_magnitude = abs(max_drawdown)
        if drawdown_magnitude > normalized_thresholds["max_drawdown_limit"]:
            failures.append(
                f"max_drawdown={drawdown_magnitude:.6f} > "
                f"{normalized_thresholds['max_drawdown_limit']:.6f}"
            )

    cost_adjusted_edge = metrics_snapshot.cost_adjusted_edge_bps_mean
    if (
        cost_adjusted_edge is not None
        and cost_adjusted_edge <= normalized_thresholds["min_cost_adjusted_edge_bps_mean"]
    ):
        failures.append(
            "cost_adjusted_edge_bps_mean="
            f"{cost_adjusted_edge:.6f} <= {normalized_thresholds['min_cost_adjusted_edge_bps_mean']:.6f}"
        )

    return CandidateGateResult(
        passed=not failures,
        failures=tuple(failures),
        thresholds=normalized_thresholds,
        critical_metrics=tuple(normalized_thresholds["critical_metrics"]),
    )


def _normalize_thresholds(thresholds: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(thresholds, Mapping):
        raise ValueError("thresholds must be a mapping")

    critical_metrics = thresholds.get("critical_metrics", DEFAULT_CRITICAL_METRICS)
    if not isinstance(critical_metrics, Sequence) or isinstance(critical_metrics, str | bytes | bytearray):
        raise ValueError("critical_metrics must be a sequence")
    critical_metrics = tuple(str(metric) for metric in critical_metrics)
    if not critical_metrics:
        raise ValueError("critical_metrics must not be empty")
    for metric in critical_metrics:
        if not hasattr(MetricsSnapshot, metric):
            raise ValueError(f"unknown critical metric: {metric}")

    return {
        "min_net_annualized_return": _float_threshold(
            thresholds.get(
                "min_net_annualized_return",
                DEFAULT_THRESHOLDS["min_net_annualized_return"],
            ),
            "min_net_annualized_return",
        ),
        "max_drawdown_limit": _non_negative_threshold(
            thresholds.get("max_drawdown_limit", DEFAULT_THRESHOLDS["max_drawdown_limit"]),
            "max_drawdown_limit",
        ),
        "min_cost_adjusted_edge_bps_mean": _float_threshold(
            thresholds.get(
                "min_cost_adjusted_edge_bps_mean",
                DEFAULT_THRESHOLDS["min_cost_adjusted_edge_bps_mean"],
            ),
            "min_cost_adjusted_edge_bps_mean",
        ),
        "critical_metrics": critical_metrics,
    }


def _float_threshold(value: Any, field_name: str) -> float:
    return require_finite_number(value, field_name)


def _non_negative_threshold(value: Any, field_name: str) -> float:
    result = _float_threshold(value, field_name)
    if result < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return result


def _require_safe_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if "/" in value or "\\" in value or value in {".", ".."} or ".." in value:
        raise ValueError(f"{field_name} must not contain path traversal or separators")
    return value


def _reject_forbidden_candidate_terms(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_forbidden_text(str(key))
            _reject_forbidden_candidate_terms(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for item in value:
            _reject_forbidden_candidate_terms(item)
        return
    if isinstance(value, str):
        _reject_forbidden_text(value)


def _reject_forbidden_text(value: str) -> None:
    lowered = value.lower()
    for forbidden in FORBIDDEN_CANDIDATE_TERMS:
        if forbidden in lowered:
            raise ValueError(f"candidate artifact must remain research-only; forbidden term: {forbidden}")


def _reject_nonfinite_threshold_values(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_nonfinite_threshold_values(key)
            _reject_nonfinite_threshold_values(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for item in value:
            _reject_nonfinite_threshold_values(item)
        return
    if isinstance(value, bool):
        return
    if isinstance(value, int | float | Decimal):
        require_finite_number(value, "gate thresholds")
