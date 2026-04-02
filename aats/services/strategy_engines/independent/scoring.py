from __future__ import annotations

from collections.abc import Sequence

from aats.bootstrap.settings import AATSSettings
from aats.schemas.decision import AIMarketAssessment, BaselineAssessment

from .models import IndependentLeg, ScoreStabilityMetrics


def effective_score_drawdown_threshold_bps(*, settings: AATSSettings) -> float:
    configured = settings.strategy_hedge_independent_min_score_drawdown_bps
    if configured is not None:
        return float(configured)
    return float(settings.strategy_hedge_independent_min_score_stability_bps)


def compute_raw_book_score(
    *,
    settings: AATSSettings,
    leg: IndependentLeg,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
) -> float:
    if leg == "short" and not bool(settings.strategy_short_bias_enabled):
        return 0.0
    side_sign = 1.0 if leg == "long" else -1.0
    momentum_alpha = float(baseline.factor_scores.get("momentum_alpha", 0.0))
    trend_alpha = float(baseline.factor_scores.get("trend_alpha", 0.0))
    microstructure_alpha = float(baseline.factor_scores.get("microstructure_alpha", 0.0))
    alpha_component = _clamp(max(0.0, side_sign * float(baseline.composite_alpha_score)), 0.0, 1.0)
    ai_component = _clamp(max(0.0, side_sign * _ai_directional_edge(ai_assessment)), 0.0, 1.0)
    momentum_component = _clamp(max(0.0, side_sign * momentum_alpha), 0.0, 1.0)
    trend_component = _clamp(max(0.0, side_sign * trend_alpha), 0.0, 1.0)
    microstructure_component = _clamp(max(0.0, side_sign * microstructure_alpha), 0.0, 1.0)
    confidence = _clamp(float(baseline.confidence), 0.0, 1.0)
    score = (
        (alpha_component * 0.28)
        + (ai_component * 0.26)
        + (momentum_component * 0.16)
        + (trend_component * 0.12)
        + (microstructure_component * 0.08)
        + (confidence * 0.10)
    )
    if baseline.regime in {"range", "uncertain"}:
        score += 0.04
    if baseline.direction_bias == leg:
        score += 0.06
    if baseline.volatility_state == "high":
        score += 0.03
    return _clamp(score, 0.0, 1.0)


def compute_signal_edge_bps(
    *,
    settings: AATSSettings,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
    leg: IndependentLeg,
) -> float:
    side_sign = 1.0 if leg == "long" else -1.0
    directional_alpha = max(0.0, side_sign * float(baseline.composite_alpha_score))
    directional_microstructure = max(0.0, side_sign * float(baseline.factor_scores.get("microstructure_alpha", 0.0)))
    directional_momentum = max(0.0, side_sign * float(baseline.factor_scores.get("momentum_alpha", 0.0)))
    directional_trend = max(0.0, side_sign * float(baseline.factor_scores.get("trend_alpha", 0.0)))
    directional_ai = max(0.0, side_sign * _ai_directional_edge(ai_assessment))
    alpha_edge = directional_alpha * max(float(settings.strategy_alpha_edge_bps_scale), 0.0)
    microstructure_bonus = max(directional_microstructure - 0.08, 0.0) * 25.0
    momentum_bonus = max(directional_momentum - 0.08, 0.0) * 15.0
    trend_bonus = max(directional_trend - 0.08, 0.0) * 12.0
    ai_bonus = max(directional_ai - 0.1, 0.0) * 20.0
    return alpha_edge + microstructure_bonus + momentum_bonus + trend_bonus + ai_bonus


def compute_score_stability(
    *,
    settings: AATSSettings,
    leg: IndependentLeg,
    score: float,
    entry_threshold: float,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
    recent_score_history: Sequence[float],
    min_confirm_ticks: int | None = None,
) -> ScoreStabilityMetrics:
    history = [float(item) for item in recent_score_history if item is not None]
    effective_min_confirm_ticks = max(
        int(settings.strategy_hedge_independent_min_confirm_ticks)
        if min_confirm_ticks is None
        else int(min_confirm_ticks),
        1,
    )
    if history:
        window_size = max(effective_min_confirm_ticks, 2)
        window = [*history[-window_size:], float(score)]
        support_count = sum(1 for item in window if item + 1e-9 >= entry_threshold)
        min_score = min(window)
        max_score = max(window)
        mean_score = sum(window) / max(len(window), 1)
        score_slope = 0.0 if len(window) < 2 else (window[-1] - window[0]) / max(len(window) - 1, 1)
        variance = sum((item - mean_score) ** 2 for item in window) / max(len(window), 1)
        score_volatility_bps = (variance**0.5) * 100.0
        upward_excursion_bps = max(float(score) - min_score, 0.0) * 100.0
        downward_drawdown_bps = max(max_score - float(score), 0.0) * 100.0
        stable = (
            support_count >= effective_min_confirm_ticks
            and downward_drawdown_bps <= effective_score_drawdown_threshold_bps(settings=settings) + 1e-9
        )
        return ScoreStabilityMetrics(
            support_count=support_count,
            min_score=min_score,
            max_score=max_score,
            mean_score=mean_score,
            # Backward-compatible alias retained for downstream readers that still expect
            # the old metric name; new consumers should prefer upward/downward fields.
            max_drawdown_bps=upward_excursion_bps,
            stable=stable,
            source="recent_target_history",
            score_slope=score_slope,
            score_volatility_bps=score_volatility_bps,
            upward_excursion_bps=upward_excursion_bps,
            downward_drawdown_bps=downward_drawdown_bps,
        )
    support_count = _signal_confirmation_count(
        leg=leg,
        baseline=baseline,
        ai_assessment=ai_assessment,
    )
    return ScoreStabilityMetrics(
        support_count=support_count,
        min_score=float(score),
        max_score=float(score),
        mean_score=float(score),
        max_drawdown_bps=0.0,
        stable=support_count >= effective_min_confirm_ticks,
        source="current_signal_confirmation",
        score_slope=0.0,
        score_volatility_bps=0.0,
        upward_excursion_bps=0.0,
        downward_drawdown_bps=0.0,
    )


def compute_candidate_confidence(score: float) -> float:
    return min(0.95, 0.30 + max(score, 0.0) * 0.55)


def _signal_confirmation_count(
    *,
    leg: IndependentLeg,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
) -> int:
    side_sign = 1.0 if leg == "long" else -1.0
    confirmations = (
        max(0.0, side_sign * float(baseline.composite_alpha_score)) >= 0.08,
        max(0.0, side_sign * float(baseline.factor_scores.get("momentum_alpha", 0.0))) >= 0.08,
        max(0.0, side_sign * float(baseline.factor_scores.get("trend_alpha", 0.0))) >= 0.08,
        max(0.0, side_sign * float(baseline.factor_scores.get("microstructure_alpha", 0.0))) >= 0.08,
        max(0.0, side_sign * _ai_directional_edge(ai_assessment)) >= 0.10,
    )
    return sum(1 for item in confirmations if item)


def _ai_directional_edge(ai_assessment: AIMarketAssessment | None) -> float:
    return 0.0 if ai_assessment is None else ai_assessment.directional_edge


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))
