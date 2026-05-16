"""Deterministic research allocation policy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from aats.data_platform.research_factory.specs import MetricsSnapshot

ALLOCATION_ARMS = (
    "factor",
    "model",
    "execution_policy",
    "risk_budget",
    "regime_classifier",
    "validation",
)
DEFAULT_REWARD_WEIGHTS = {
    "ic": 1.0,
    "rank_ic": 1.0,
    "net_annualized_return": 1.5,
    "information_ratio": 1.0,
    "max_drawdown": -1.5,
    "turnover": -0.5,
    "missing_critical_metrics": -2.0,
}
CRITICAL_ALLOCATION_METRICS = (
    "ic",
    "rank_ic",
    "net_annualized_return",
    "information_ratio",
    "max_drawdown",
)
DEFAULT_EPSILON_FLOOR = 0.05


@dataclass(frozen=True, slots=True)
class ResearchAllocationInput:
    """Inputs for deterministic research action allocation."""

    metrics_by_arm: Mapping[str, MetricsSnapshot | None] = field(default_factory=dict)
    sample_counts: Mapping[str, int] = field(default_factory=dict)
    weights: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_REWARD_WEIGHTS))
    epsilon_floor: float = DEFAULT_EPSILON_FLOOR

    def __post_init__(self) -> None:
        metrics_by_arm = _normalize_metrics_by_arm(self.metrics_by_arm)
        sample_counts = _normalize_sample_counts(self.sample_counts)
        weights = _normalize_weights(self.weights)
        if isinstance(self.epsilon_floor, bool) or not isinstance(self.epsilon_floor, int | float):
            raise ValueError("epsilon_floor must be numeric")
        if self.epsilon_floor < 0:
            raise ValueError("epsilon_floor must be non-negative")
        object.__setattr__(self, "metrics_by_arm", metrics_by_arm)
        object.__setattr__(self, "sample_counts", sample_counts)
        object.__setattr__(self, "weights", weights)
        object.__setattr__(self, "epsilon_floor", float(self.epsilon_floor))


@dataclass(frozen=True, slots=True)
class AllocationDecision:
    """Selected research action and deterministic audit trace."""

    arm: str
    score: float
    scores: Mapping[str, float]
    reason_trace: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.arm not in ALLOCATION_ARMS:
            raise ValueError("decision arm must be a valid allocation arm")
        if isinstance(self.score, bool) or not isinstance(self.score, int | float):
            raise ValueError("decision score must be numeric")
        if not isinstance(self.scores, Mapping):
            raise ValueError("decision scores must be a mapping")
        if not self.reason_trace:
            raise ValueError("decision reason_trace must not be empty")
        object.__setattr__(self, "score", float(self.score))
        object.__setattr__(self, "scores", dict(self.scores))
        object.__setattr__(self, "reason_trace", tuple(self.reason_trace))


def choose_next_research_action(allocation_input: ResearchAllocationInput) -> AllocationDecision:
    """Choose the next research action using deterministic metrics only."""
    if not isinstance(allocation_input, ResearchAllocationInput):
        raise ValueError("allocation_input must be a ResearchAllocationInput")

    scores: dict[str, float] = {}
    reason_trace: list[str] = []
    for arm in ALLOCATION_ARMS:
        metrics = allocation_input.metrics_by_arm.get(arm)
        sample_count = allocation_input.sample_counts.get(arm, 0)
        metric_score, metric_reasons = _score_metrics(arm, metrics, allocation_input.weights)
        exploration_bonus = _exploration_bonus(allocation_input.epsilon_floor, sample_count)
        score = metric_score + exploration_bonus
        scores[arm] = score
        reason_trace.extend(metric_reasons)
        reason_trace.append(
            f"{arm}: sample_count={sample_count}, exploration_bonus={exploration_bonus:.6f}, score={score:.6f}"
        )

    selected_arm = max(ALLOCATION_ARMS, key=lambda arm: (scores[arm], -ALLOCATION_ARMS.index(arm)))
    reason_trace.append(f"selected={selected_arm}, score={scores[selected_arm]:.6f}")
    return AllocationDecision(
        arm=selected_arm,
        score=scores[selected_arm],
        scores=scores,
        reason_trace=tuple(reason_trace),
    )


def _score_metrics(
    arm: str,
    metrics: MetricsSnapshot | None,
    weights: Mapping[str, float],
) -> tuple[float, list[str]]:
    if metrics is None:
        return 0.0, [f"{arm}: no metrics; using exploration-only reward"]
    if not isinstance(metrics, MetricsSnapshot):
        raise ValueError(f"metrics for {arm!r} must be a MetricsSnapshot or None")

    score = 0.0
    reasons: list[str] = []
    missing_critical = _missing_critical_metrics(metrics)
    if missing_critical:
        penalty = weights["missing_critical_metrics"]
        score += penalty
        reasons.append(f"{arm}: missing_critical_metrics={','.join(missing_critical)}, penalty={penalty:.6f}")

    for metric_name in ("ic", "rank_ic", "net_annualized_return", "information_ratio"):
        value = getattr(metrics, metric_name)
        if value is None:
            continue
        contribution = float(value) * weights[metric_name]
        score += contribution
        reasons.append(f"{arm}: {metric_name}={float(value):.6f}, contribution={contribution:.6f}")

    if metrics.max_drawdown is not None:
        drawdown_magnitude = abs(float(metrics.max_drawdown))
        contribution = drawdown_magnitude * weights["max_drawdown"]
        score += contribution
        reasons.append(
            f"{arm}: max_drawdown={drawdown_magnitude:.6f}, contribution={contribution:.6f}"
        )

    if metrics.turnover is not None:
        contribution = float(metrics.turnover) * weights["turnover"]
        score += contribution
        reasons.append(f"{arm}: turnover={float(metrics.turnover):.6f}, contribution={contribution:.6f}")

    return score, reasons


def _missing_critical_metrics(metrics: MetricsSnapshot) -> tuple[str, ...]:
    missing_reasons = dict(metrics.missing_reasons)
    missing: list[str] = []
    for metric_name in CRITICAL_ALLOCATION_METRICS:
        if getattr(metrics, metric_name) is None or missing_reasons.get(metric_name):
            missing.append(metric_name)
    return tuple(missing)


def _exploration_bonus(epsilon_floor: float, sample_count: int) -> float:
    return epsilon_floor / float(sample_count + 1)


def _normalize_metrics_by_arm(value: Mapping[str, MetricsSnapshot | None]) -> dict[str, MetricsSnapshot | None]:
    if not isinstance(value, Mapping):
        raise ValueError("metrics_by_arm must be a mapping")
    normalized: dict[str, MetricsSnapshot | None] = {}
    for arm, metrics in value.items():
        arm = _require_arm(str(arm))
        if metrics is not None and not isinstance(metrics, MetricsSnapshot):
            raise ValueError(f"metrics for {arm!r} must be a MetricsSnapshot or None")
        normalized[arm] = metrics
    return normalized


def _normalize_sample_counts(value: Mapping[str, int]) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError("sample_counts must be a mapping")
    normalized: dict[str, int] = {}
    for arm, sample_count in value.items():
        arm = _require_arm(str(arm))
        if isinstance(sample_count, bool) or not isinstance(sample_count, int):
            raise ValueError("sample_count must be an integer")
        if sample_count < 0:
            raise ValueError("sample_count must be non-negative")
        normalized[arm] = sample_count
    return normalized


def _normalize_weights(value: Mapping[str, Any]) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise ValueError("weights must be a mapping")
    weights = dict(DEFAULT_REWARD_WEIGHTS)
    weights.update(value)
    missing = sorted(set(DEFAULT_REWARD_WEIGHTS) - set(weights))
    if missing:
        raise ValueError(f"weights missing required keys: {', '.join(missing)}")
    for key, weight in weights.items():
        if key not in DEFAULT_REWARD_WEIGHTS:
            raise ValueError(f"unknown reward weight: {key}")
        if isinstance(weight, bool) or not isinstance(weight, int | float):
            raise ValueError(f"weight {key!r} must be numeric")
        weights[key] = float(weight)
    return weights


def _require_arm(value: str) -> str:
    if value not in ALLOCATION_ARMS:
        allowed = ", ".join(ALLOCATION_ARMS)
        raise ValueError(f"allocation arm must be one of: {allowed}")
    return value
