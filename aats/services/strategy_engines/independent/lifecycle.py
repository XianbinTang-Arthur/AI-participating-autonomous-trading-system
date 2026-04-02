from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.decision import DecisionContext
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal

from .models import IndependentBookDecision, IndependentExecutionHealthState, IndependentLeg


def compute_thesis_age_seconds(
    *,
    context: DecisionContext,
    leg: IndependentLeg,
    current_qty: Decimal,
) -> float | None:
    if current_qty <= EPSILON_DECIMAL_12:
        return None
    opened_at = context.current_long_leg_opened_at if leg == "long" else context.current_short_leg_opened_at
    if opened_at is None:
        return None
    return max((context.as_of_ts - opened_at).total_seconds(), 0.0)


def min_hold_remaining_seconds(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    leg: IndependentLeg,
) -> float:
    opened_at = context.current_long_leg_opened_at if leg == "long" else context.current_short_leg_opened_at
    min_hold_seconds = (
        settings.strategy_hedge_independent_long_min_hold_seconds
        if leg == "long"
        else settings.strategy_hedge_independent_short_min_hold_seconds
    )
    current_qty = (
        to_decimal(context.current_long_position_qty)
        if leg == "long"
        else to_decimal(context.current_short_position_qty)
    )
    if opened_at is None or min_hold_seconds <= 0 or current_qty <= EPSILON_DECIMAL_12:
        return 0.0
    held_for = max((context.as_of_ts - opened_at).total_seconds(), 0.0)
    return max(float(min_hold_seconds) - held_for, 0.0)


def rebalance_remaining_seconds(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    leg: IndependentLeg,
    opening_or_expanding: bool,
    desired_target_qty: Decimal,
    current_qty: Decimal,
) -> float:
    if (
        settings.strategy_hedge_independent_rebalance_cooldown_seconds <= 0
        or abs(desired_target_qty - current_qty) <= EPSILON_DECIMAL_12
    ):
        return 0.0
    anchor = context.latest_long_leg_fill_timestamp if leg == "long" else context.latest_short_leg_fill_timestamp
    if opening_or_expanding and current_qty <= EPSILON_DECIMAL_12 and anchor is None:
        anchor = context.last_long_leg_closed_at if leg == "long" else context.last_short_leg_closed_at
    if anchor is None:
        return 0.0
    since_anchor = max((context.as_of_ts - anchor).total_seconds(), 0.0)
    return max(settings.strategy_hedge_independent_rebalance_cooldown_seconds - since_anchor, 0.0)


def determine_close_reason(
    *,
    settings: AATSSettings,
    score: float,
    close_threshold: float,
    expected_net_edge_bps: float | None,
    liquidity_quality_score: float | None,
    execution_health_state: IndependentExecutionHealthState | None,
    age_seconds: float | None,
    max_thesis_age_seconds: float | None = None,
    de_risk_net_edge_bps: float | None = None,
    failed_thesis_net_edge_bps: float | None = None,
    execution_health_de_risk_enabled: bool | None = None,
    liquidity_de_risk_enabled: bool | None = None,
) -> str | None:
    failed_thesis_threshold = (
        float(settings.strategy_hedge_independent_failed_thesis_net_edge_bps)
        if failed_thesis_net_edge_bps is None
        else float(failed_thesis_net_edge_bps)
    )
    thesis_age_limit = (
        float(settings.strategy_hedge_independent_max_thesis_age_seconds)
        if max_thesis_age_seconds is None
        else float(max_thesis_age_seconds)
    )
    de_risk_threshold = (
        float(settings.strategy_hedge_independent_de_risk_net_edge_bps)
        if de_risk_net_edge_bps is None
        else float(de_risk_net_edge_bps)
    )
    execution_health_de_risk = (
        bool(settings.strategy_hedge_independent_execution_health_de_risk_enabled)
        if execution_health_de_risk_enabled is None
        else bool(execution_health_de_risk_enabled)
    )
    liquidity_de_risk = (
        bool(settings.strategy_hedge_independent_liquidity_de_risk_enabled)
        if liquidity_de_risk_enabled is None
        else bool(liquidity_de_risk_enabled)
    )
    if (
        expected_net_edge_bps is not None
        and expected_net_edge_bps <= failed_thesis_threshold
    ):
        return "failed_thesis"
    if age_seconds is not None and age_seconds >= thesis_age_limit:
        return "stale_thesis"
    if (
        execution_health_de_risk
        and execution_health_state in {"degraded", "blocked"}
    ):
        return "execution_health_degraded"
    if (
        liquidity_de_risk
        and liquidity_quality_score is not None
        and liquidity_quality_score + 1e-9 < float(settings.strategy_hedge_independent_min_liquidity_quality)
    ):
        return "liquidity_degraded"
    if (
        (
            expected_net_edge_bps is not None
            and expected_net_edge_bps <= de_risk_threshold
        )
        or score + 1e-9 < close_threshold
    ):
        return "weak_edge_de_risk"
    return None


def close_reason_code(*, leg: IndependentLeg, close_reason: str) -> str:
    return {
        "failed_thesis": f"independent_{leg}_book_close_failed_thesis",
        "stale_thesis": f"independent_{leg}_book_close_stale_thesis",
        "execution_health_degraded": f"independent_{leg}_book_de_risk_execution_health_degraded",
        "liquidity_degraded": f"independent_{leg}_book_de_risk_liquidity_degraded",
        "weak_edge_de_risk": f"independent_{leg}_book_de_risk_weak_edge",
    }.get(close_reason, f"independent_{leg}_book_de_risk")


def compute_de_risk_target_qty(
    *,
    current_qty: Decimal,
    directional_leg_target_qty: Decimal,
) -> Decimal:
    current_qty = max(to_decimal(current_qty), Decimal("0"))
    if current_qty <= EPSILON_DECIMAL_12:
        return Decimal("0")
    half_qty = current_qty / Decimal("2")
    directional_target_qty = max(to_decimal(directional_leg_target_qty), Decimal("0"))
    if directional_target_qty > EPSILON_DECIMAL_12:
        return min(half_qty, directional_target_qty)
    return half_qty


def close_reason_summary(
    *,
    long_book: IndependentBookDecision,
    short_book: IndependentBookDecision,
) -> str | None:
    reasons = [book.close_reason for book in (long_book, short_book) if book.close_reason is not None]
    unique = list(dict.fromkeys(reasons))
    if not unique:
        return None
    if len(unique) == 1:
        return unique[0]
    return "mixed"


def cooldown_until(
    *,
    context: DecisionContext,
    min_hold_remaining_seconds: float,
    rebalance_cooldown_remaining_seconds: float,
):
    remaining_seconds = max(
        float(min_hold_remaining_seconds or 0.0),
        float(rebalance_cooldown_remaining_seconds or 0.0),
    )
    if remaining_seconds <= 0.0:
        return None
    return context.as_of_ts + timedelta(seconds=remaining_seconds)


def last_transition_at(
    *,
    context: DecisionContext,
    leg: IndependentLeg,
):
    latest_fill = context.latest_long_leg_fill_timestamp if leg == "long" else context.latest_short_leg_fill_timestamp
    opened_at = context.current_long_leg_opened_at if leg == "long" else context.current_short_leg_opened_at
    closed_at = context.last_long_leg_closed_at if leg == "long" else context.last_short_leg_closed_at
    for candidate in (latest_fill, opened_at, closed_at):
        if candidate is not None:
            return candidate
    return None
