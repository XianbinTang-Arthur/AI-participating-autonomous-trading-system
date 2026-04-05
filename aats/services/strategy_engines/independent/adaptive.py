from __future__ import annotations

from dataclasses import dataclass

from aats.bootstrap.settings import AATSSettings
from aats.schemas.decision import AIMarketAssessment, BaselineAssessment, DecisionContext

from .health import IndependentLegHealthSnapshot
from .models import IndependentBookDecision, IndependentLeg, clamp as _clamp
from .scoring import effective_score_drawdown_threshold_bps


@dataclass(frozen=True, slots=True)
class IndependentAdaptiveSnapshot:
    leg: IndependentLeg
    shadow_only: bool = True
    rollout_enabled: bool = False
    live_applied: bool = False
    health_enforcement_enabled: bool = False
    size_down_entry_enabled: bool = False
    long_short_asymmetry_enabled: bool = False
    entry_threshold: float | None = None
    close_threshold: float | None = None
    scale_in_threshold: float | None = None
    thesis_age_seconds: float | None = None
    de_risk_net_edge_bps: float | None = None
    score_drawdown_bps: float | None = None
    adaptive_entry_threshold: float | None = None
    adaptive_close_threshold: float | None = None
    adaptive_scale_in_threshold: float | None = None
    adaptive_thesis_age_seconds: float | None = None
    adaptive_de_risk_net_edge_bps: float | None = None
    effective_entry_threshold: float | None = None
    effective_close_threshold: float | None = None
    effective_scale_in_threshold: float | None = None
    effective_thesis_age_seconds: float | None = None
    effective_de_risk_net_edge_bps: float | None = None
    effective_score_drawdown_bps: float | None = None
    capital_multiplier: float | None = None
    confidence_multiplier: float | None = None
    volatility_multiplier: float | None = None
    liquidity_multiplier: float | None = None
    health_multiplier: float | None = None
    direction_bias_multiplier: float | None = None
    reason_codes: tuple[str, ...] = ()


def threshold_snapshot(
    *,
    settings: AATSSettings,
    leg: IndependentLeg,
    baseline: BaselineAssessment | None = None,
    ai_assessment: AIMarketAssessment | None = None,
    context: DecisionContext | None = None,
    decision: IndependentBookDecision | None = None,
    health_snapshot: IndependentLegHealthSnapshot | None = None,
    live_applied: bool = False,
) -> IndependentAdaptiveSnapshot:
    base_entry = _base_entry_threshold(settings=settings, leg=leg)
    base_close = _base_close_threshold(settings=settings, leg=leg)
    base_scale = _base_scale_in_threshold(settings=settings, leg=leg)
    base_thesis_age = float(settings.strategy_hedge_independent_max_thesis_age_seconds)
    base_de_risk = float(settings.strategy_hedge_independent_de_risk_net_edge_bps)
    base_score_drawdown = effective_score_drawdown_threshold_bps(settings=settings)

    confidence_multiplier, confidence_reason = _confidence_multiplier(
        baseline=baseline,
        ai_assessment=ai_assessment,
    )
    volatility_multiplier, volatility_reason = _volatility_multiplier(baseline=baseline)
    liquidity_multiplier, liquidity_reason = _liquidity_multiplier(baseline=baseline)
    health_multiplier, health_reason = _health_multiplier(health_snapshot=health_snapshot)
    direction_bias_multiplier, direction_bias_reason = _direction_bias_multiplier(
        baseline=baseline,
        leg=leg,
    )
    capital_multiplier = _clamp(
        (1.0 / max(confidence_multiplier * volatility_multiplier * liquidity_multiplier * health_multiplier, 0.5))
        * (1.02 if direction_bias_multiplier < 1.0 else 0.98 if direction_bias_multiplier > 1.0 else 1.0),
        0.55,
        1.10,
    )

    entry_strictness = _clamp(
        confidence_multiplier
        * volatility_multiplier
        * liquidity_multiplier
        * health_multiplier
        * direction_bias_multiplier,
        0.92,
        1.15,
    )
    scale_strictness = _clamp(entry_strictness * 1.02, 0.94, 1.18)
    close_strictness = _clamp(1.0 + ((entry_strictness - 1.0) * 0.65), 0.95, 1.12)
    thesis_age_multiplier = _clamp(1.0 / max(entry_strictness, 0.75), 0.75, 1.20)
    de_risk_multiplier = _clamp(
        (entry_strictness + health_multiplier) / 2.0,
        0.90,
        1.20,
    )

    adaptive_entry = _clamp(base_entry * entry_strictness, 0.0, 1.0)
    adaptive_scale = max(adaptive_entry, _clamp(base_scale * scale_strictness, 0.0, 1.0))
    adaptive_close = min(adaptive_entry, _clamp(base_close * close_strictness, 0.0, 1.0))
    adaptive_thesis_age = max(60.0, base_thesis_age * thesis_age_multiplier)
    adaptive_de_risk = max(0.0, base_de_risk * de_risk_multiplier)

    reason_codes = tuple(
        reason
        for reason in (
            confidence_reason,
            volatility_reason,
            liquidity_reason,
            health_reason,
            direction_bias_reason,
            _decision_reason(decision=decision),
            _context_reason(context=context, leg=leg),
        )
        if reason is not None
    )

    return IndependentAdaptiveSnapshot(
        leg=leg,
        shadow_only=not live_applied,
        rollout_enabled=bool(settings.strategy_hedge_independent_adaptive_rollout_enabled),
        live_applied=live_applied,
        health_enforcement_enabled=bool(settings.strategy_hedge_independent_health_enforcement_enabled),
        size_down_entry_enabled=bool(settings.strategy_hedge_independent_size_down_entry_enabled),
        long_short_asymmetry_enabled=bool(settings.strategy_hedge_independent_long_short_asymmetry_enabled),
        entry_threshold=base_entry,
        close_threshold=base_close,
        scale_in_threshold=base_scale,
        thesis_age_seconds=base_thesis_age,
        de_risk_net_edge_bps=base_de_risk,
        score_drawdown_bps=base_score_drawdown,
        adaptive_entry_threshold=adaptive_entry,
        adaptive_close_threshold=adaptive_close,
        adaptive_scale_in_threshold=adaptive_scale,
        adaptive_thesis_age_seconds=adaptive_thesis_age,
        adaptive_de_risk_net_edge_bps=adaptive_de_risk,
        effective_entry_threshold=adaptive_entry if live_applied else base_entry,
        effective_close_threshold=adaptive_close if live_applied else base_close,
        effective_scale_in_threshold=adaptive_scale if live_applied else base_scale,
        effective_thesis_age_seconds=adaptive_thesis_age if live_applied else base_thesis_age,
        effective_de_risk_net_edge_bps=adaptive_de_risk if live_applied else base_de_risk,
        effective_score_drawdown_bps=base_score_drawdown,
        capital_multiplier=capital_multiplier,
        confidence_multiplier=confidence_multiplier,
        volatility_multiplier=volatility_multiplier,
        liquidity_multiplier=liquidity_multiplier,
        health_multiplier=health_multiplier,
        direction_bias_multiplier=direction_bias_multiplier,
        reason_codes=reason_codes,
    )


def _base_entry_threshold(*, settings: AATSSettings, leg: IndependentLeg) -> float:
    return float(
        settings.strategy_hedge_independent_long_entry_threshold
        if leg == "long"
        else settings.strategy_hedge_independent_short_entry_threshold
    )


def _base_close_threshold(*, settings: AATSSettings, leg: IndependentLeg) -> float:
    return float(
        settings.strategy_hedge_independent_long_close_threshold
        if leg == "long"
        else settings.strategy_hedge_independent_short_close_threshold
    )


def _base_scale_in_threshold(*, settings: AATSSettings, leg: IndependentLeg) -> float:
    return float(
        settings.strategy_hedge_independent_long_scale_in_threshold
        if leg == "long"
        else settings.strategy_hedge_independent_short_scale_in_threshold
    )


def _confidence_multiplier(
    *,
    baseline: BaselineAssessment | None,
    ai_assessment: AIMarketAssessment | None,
) -> tuple[float, str | None]:
    confidence = None
    if ai_assessment is not None and ai_assessment.calibrated_confidence is not None:
        confidence = float(ai_assessment.calibrated_confidence)
    elif ai_assessment is not None:
        confidence = float(ai_assessment.confidence)
    elif baseline is not None:
        confidence = float(baseline.confidence)
    if confidence is None:
        return 1.0, None
    multiplier = _clamp(1.0 + ((0.72 - confidence) * 0.28), 0.94, 1.10)
    if abs(multiplier - 1.0) < 1e-9:
        return multiplier, None
    return multiplier, "adaptive_shadow_confidence_adjusted"


def _volatility_multiplier(*, baseline: BaselineAssessment | None) -> tuple[float, str | None]:
    if baseline is None:
        return 1.0, None
    scale = max(float(baseline.volatility_target_scale), 0.1)
    multiplier = _clamp(1.0 + ((1.0 - scale) * 0.18), 0.92, 1.10)
    if abs(multiplier - 1.0) < 1e-9:
        return multiplier, None
    return multiplier, "adaptive_shadow_volatility_adjusted"


def _liquidity_multiplier(*, baseline: BaselineAssessment | None) -> tuple[float, str | None]:
    if baseline is None:
        return 1.0, None
    liquidity_scale = float(baseline.factor_scores.get("liquidity_scale", 1.0))
    multiplier = _clamp(1.0 + ((0.85 - liquidity_scale) * 0.16), 0.94, 1.08)
    if abs(multiplier - 1.0) < 1e-9:
        return multiplier, None
    return multiplier, "adaptive_shadow_liquidity_adjusted"


def _health_multiplier(
    *,
    health_snapshot: IndependentLegHealthSnapshot | None,
) -> tuple[float, str | None]:
    if health_snapshot is None:
        return 1.0, None
    if health_snapshot.health_state == "blocked":
        return 1.10, "adaptive_shadow_health_blocked"
    if health_snapshot.health_state == "degraded":
        return 1.05, "adaptive_shadow_health_degraded"
    return 1.0, None


def _direction_bias_multiplier(
    *,
    baseline: BaselineAssessment | None,
    leg: IndependentLeg,
) -> tuple[float, str | None]:
    if baseline is None:
        return 1.0, None
    direction_bias = str(baseline.direction_bias or "").strip().lower()
    if direction_bias == leg:
        return 0.97, "adaptive_shadow_directional_alignment_bonus"
    if direction_bias in {"long", "short"}:
        return 1.03, "adaptive_shadow_directional_alignment_penalty"
    return 1.0, None


def _decision_reason(*, decision: IndependentBookDecision | None) -> str | None:
    if decision is None or decision.book_action in {"inactive", "hold", "blocked"}:
        return None
    return f"adaptive_shadow_book_action_{decision.book_action}"


def _context_reason(*, context: DecisionContext | None, leg: IndependentLeg) -> str | None:
    if context is None:
        return None
    if leg == "long" and context.current_long_position_qty > 0:
        return "adaptive_shadow_existing_long_inventory"
    if leg == "short" and context.current_short_position_qty > 0:
        return "adaptive_shadow_existing_short_inventory"
    return None


