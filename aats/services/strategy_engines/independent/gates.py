from __future__ import annotations

from aats.bootstrap.settings import AATSSettings
from aats.schemas.decision import DecisionContext

from .models import (
    IndependentBookExpectancy,
    IndependentEligibilityOutcome,
    IndependentExecutionHealthState,
    IndependentLeg,
    ScoreStabilityMetrics,
    clamp as _clamp,
)


def required_safe_net_edge_bps(*, settings: AATSSettings) -> float:
    return (
        max(float(settings.strategy_hedge_independent_min_safe_net_edge_bps), 0.0)
        + max(float(settings.strategy_hedge_independent_expected_slippage_buffer_bps), 0.0)
        + max(float(settings.strategy_hedge_independent_expected_execution_buffer_bps), 0.0)
    )


def evaluate_open_eligibility(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    leg: IndependentLeg,
    expectancy: IndependentBookExpectancy | None,
    blocked_reasons: tuple[str, ...] = (),
) -> IndependentEligibilityOutcome:
    reasons = list(blocked_reasons)
    warnings: list[str] = []
    expected_cost_bps = 0.0 if expectancy is None else expectancy.expected_cost_bps
    expected_net_edge_bps = 0.0 if expectancy is None else expectancy.expected_net_edge_bps
    safe_edge_bps = required_safe_net_edge_bps(settings=settings)
    # 边界语义（严格 <）: net_edge == safe_edge 时允许入场。
    # 与 lifecycle.py determine_close_reason 的 `net_edge <= de_risk` 形成一致的持仓区间：
    #   持仓区间 = [safe_edge, +∞) ∩ (de_risk, +∞) = [safe_edge, +∞)
    # 这依赖约束 safe_edge > de_risk (由 ReplayParameterOverrides.__post_init__ 保证)。
    if expected_net_edge_bps < safe_edge_bps:
        if settings.strategy_hedge_independent_weak_edge_execution_mode == "block":
            reasons.append(f"independent_{leg}_book_expected_net_edge_below_safe_threshold")
        else:
            warnings.append(f"independent_{leg}_book_expected_net_edge_below_safe_threshold_report_only")
    max_acceptable_cost_bps = anomaly_cost_fuse_threshold_bps(
        settings=settings,
        expectancy=expectancy,
    )
    if max_acceptable_cost_bps is not None and expected_cost_bps > max_acceptable_cost_bps:
        reasons.append(f"independent_{leg}_book_expected_cost_above_max_acceptable")
    if post_close_cooldown_active(settings=settings, context=context, leg=leg):
        reasons.append(f"independent_{leg}_book_post_close_cooldown_active")
    if low_edge_cooldown_active(settings=settings, context=context, leg=leg):
        reasons.append(f"independent_{leg}_book_low_edge_cooldown_active")
    if performance_degraded(settings=settings, context=context, leg=leg):
        fee_drag_ratio = float(_leg_health_value(context, leg, "recent_fee_drag_ratio") or 0.0)
        churn_ratio = float(_leg_health_value(context, leg, "recent_churn_ratio") or 0.0)
        if fee_drag_ratio > settings.strategy_max_fee_drag_ratio:
            reasons.append(f"independent_{leg}_book_fee_drag_guard_active")
        if churn_ratio > settings.strategy_max_churn_ratio:
            reasons.append(f"independent_{leg}_book_churn_guard_active")
    if trial_guard_active(settings=settings, context=context, leg=leg):
        reasons.append(f"independent_{leg}_book_trial_guard_active")
    return IndependentEligibilityOutcome(
        eligible=not reasons,
        hard_block_reasons=tuple(reasons),
        warnings=tuple(warnings),
        effective_safe_net_edge_bps=safe_edge_bps,
        effective_max_cost_bps=max_acceptable_cost_bps,
    )


def anomaly_cost_fuse_threshold_bps(
    *,
    settings: AATSSettings,
    expectancy: IndependentBookExpectancy | None,
) -> float | None:
    nominal_max_cost_bps = float(settings.strategy_hedge_independent_max_acceptable_cost_bps)
    if nominal_max_cost_bps <= 0.0:
        return None
    safe_edge_bps = required_safe_net_edge_bps(settings=settings)
    base_slack_bps = max(1.0, safe_edge_bps * 0.2)
    if expectancy is None:
        return nominal_max_cost_bps + base_slack_bps

    expected_signal_edge_bps = max(float(expectancy.expected_signal_edge_bps), 0.0)
    expected_net_edge_bps = max(float(expectancy.expected_net_edge_bps), 0.0)
    edge_headroom_bps = max(expected_net_edge_bps - safe_edge_bps, 0.0)
    depth_consumption_ratio = max(float(expectancy.depth_consumption_ratio or 0.0), 0.0)
    size_impact_bps = max(float(expectancy.size_impact_bps or 0.0), 0.0)
    cost_confidence = _clamp(float(expectancy.cost_confidence or 0.45), 0.25, 0.95)

    # Let strong, well-priced signals absorb moderate cost overruns, but tighten
    # the anomaly fuse when the order would consume a large share of visible depth
    # or when the cost estimate itself is low confidence.
    edge_allowance_bps = (edge_headroom_bps * 0.15) + min(
        expected_signal_edge_bps * 0.03,
        max(nominal_max_cost_bps * 0.2, 1.0),
    )
    depth_penalty_bps = max(depth_consumption_ratio - 0.25, 0.0) * max(nominal_max_cost_bps * 1.25, 2.0)
    size_penalty_bps = min(size_impact_bps * 0.35, nominal_max_cost_bps)
    confidence_penalty_bps = max(0.60 - cost_confidence, 0.0) * nominal_max_cost_bps * 0.75

    dynamic_fuse_bps = (
        nominal_max_cost_bps
        + base_slack_bps
        + edge_allowance_bps
        - depth_penalty_bps
        - size_penalty_bps
        - confidence_penalty_bps
    )
    fuse_floor_bps = nominal_max_cost_bps + max(0.75, safe_edge_bps * 0.1)
    fuse_ceiling_bps = nominal_max_cost_bps + base_slack_bps + max(
        edge_headroom_bps * 0.25,
        expected_signal_edge_bps * 0.08,
        safe_edge_bps * 0.5,
        1.5,
    )
    return _clamp(dynamic_fuse_bps, fuse_floor_bps, fuse_ceiling_bps)


def resolve_entry_min_confirm_ticks(
    *,
    settings: AATSSettings,
    side: IndependentLeg,
    score: float,
    entry_threshold: float,
    scale_threshold: float | None = None,
    expected_net_edge_bps: float | None = None,
) -> int:
    configured_min_confirm_ticks = max(int(settings.strategy_hedge_independent_min_confirm_ticks), 1)
    if configured_min_confirm_ticks <= 1 or side != "short":
        return configured_min_confirm_ticks
    strong_signal_threshold = (
        float(scale_threshold)
        if scale_threshold is not None and float(scale_threshold) > float(entry_threshold)
        else min(float(entry_threshold) + 0.05, 1.0)
    )
    strong_signal = score + 1e-9 >= strong_signal_threshold
    nominal_cost_bps = max(float(settings.strategy_hedge_independent_max_acceptable_cost_bps), 0.0)
    high_net_edge_threshold = required_safe_net_edge_bps(settings=settings) + max(
        float(settings.strategy_hedge_independent_expected_execution_buffer_bps),
        nominal_cost_bps,
        1.0,
    )
    high_net_edge = (
        expected_net_edge_bps is not None
        and float(expected_net_edge_bps) + 1e-9 >= high_net_edge_threshold
    )
    if strong_signal or high_net_edge:
        return max(configured_min_confirm_ticks - 1, 1)
    return configured_min_confirm_ticks


def evaluate_entry_quality_gate(
    *,
    side: IndependentLeg,
    score: float,
    entry_threshold: float,
    liquidity_quality_score: float | None,
    score_stability_metrics: ScoreStabilityMetrics | None,
    execution_health_state: IndependentExecutionHealthState | None,
    min_confirm_ticks: int,
    min_liquidity_quality: float,
    require_execution_health_ok: bool,
) -> tuple[bool, list[str]]:
    blocked_reasons: list[str] = []
    if score + 1e-9 < entry_threshold:
        blocked_reasons.append(f"independent_{side}_book_signal_below_entry_threshold")
    if liquidity_quality_score is not None and liquidity_quality_score + 1e-9 < min_liquidity_quality:
        blocked_reasons.append(f"independent_{side}_book_liquidity_quality_below_minimum")
    if score_stability_metrics is not None:
        if score_stability_metrics.support_count < min_confirm_ticks:
            blocked_reasons.append(f"independent_{side}_book_score_support_below_min_confirm_ticks")
        elif not score_stability_metrics.stable:
            blocked_reasons.append(f"independent_{side}_book_score_stability_below_threshold")
    if require_execution_health_ok and execution_health_state not in {None, "ok"}:
        blocked_reasons.append(f"independent_{side}_book_execution_health_not_ok")
    return (not blocked_reasons, blocked_reasons)


def post_close_cooldown_active(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    leg: IndependentLeg,
) -> bool:
    if settings.strategy_post_close_cooldown_seconds <= 0:
        return False
    closed_at = context.last_long_leg_closed_at if leg == "long" else context.last_short_leg_closed_at
    if closed_at is None:
        return False
    return max((context.as_of_ts - closed_at).total_seconds(), 0.0) < settings.strategy_post_close_cooldown_seconds


def low_edge_cooldown_active(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    leg: IndependentLeg,
) -> bool:
    if settings.strategy_low_edge_cooldown_seconds <= 0:
        return False
    streak = int(_leg_health_value(context, leg, "recent_low_edge_trade_streak") or 0)
    if streak < settings.strategy_low_edge_streak_limit:
        return False
    recent_at = _leg_health_datetime(context, leg, "recent_low_edge_trade_at")
    if recent_at is None:
        return False
    return max((context.as_of_ts - recent_at).total_seconds(), 0.0) < settings.strategy_low_edge_cooldown_seconds


def performance_degraded(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    leg: IndependentLeg,
) -> bool:
    closed_trade_count = int(_leg_health_value(context, leg, "recent_closed_trade_count") or 0)
    if closed_trade_count < settings.strategy_performance_guard_min_closed_trades:
        return False
    return (
        float(_leg_health_value(context, leg, "recent_fee_drag_ratio") or 0.0) > settings.strategy_max_fee_drag_ratio
        or float(_leg_health_value(context, leg, "recent_churn_ratio") or 0.0) > settings.strategy_max_churn_ratio
    )


def trial_guard_active(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    leg: IndependentLeg,
) -> bool:
    if not settings.strategy_hedge_independent_trial_guard_enabled:
        return False
    closed_trade_count = int(_leg_health_value(context, leg, "recent_closed_trade_count") or 0)
    if closed_trade_count < settings.strategy_performance_guard_min_closed_trades:
        return False
    recent_net_realized_pnl = float(_leg_health_value(context, leg, "recent_net_realized_pnl") or 0.0)
    recent_win_rate = float(_leg_health_value(context, leg, "recent_win_rate") or 0.0)
    return recent_net_realized_pnl < 0 and recent_win_rate < 0.5


def _leg_health_value(context: DecisionContext, leg: IndependentLeg, key: str) -> object | None:
    payload = context.leg_strategy_health.get(leg)
    if not isinstance(payload, dict):
        return None
    return payload.get(key)


def _leg_health_datetime(context: DecisionContext, leg: IndependentLeg, key: str):
    value = _leg_health_value(context, leg, key)
    return value if hasattr(value, "isoformat") else None
