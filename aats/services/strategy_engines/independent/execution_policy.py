from __future__ import annotations

from decimal import Decimal
from typing import Literal

from aats.bootstrap.settings import AATSSettings
from aats.services.portfolio_service.decimals import to_decimal

from .models import IndependentBookDecision, IndependentExecutionPolicy


def resolve_execution_policy_from_mode(
    *,
    mode: str,
    edge_strength: Literal["weak", "medium", "strong"],
    urgency: Literal["low", "medium", "high"],
    limit_offset_bps: Decimal | None,
    max_acceptable_cost_bps: float | None,
    policy_reason: str,
) -> IndependentExecutionPolicy:
    if mode in {"passive_first", "bounded_limit"}:
        return IndependentExecutionPolicy(
            edge_strength=edge_strength,
            urgency=urgency,
            execution_style_preference="bounded_limit_ioc",
            order_type_preference="limit",
            time_in_force_preference="IOC",
            limit_offset_bps_preference=limit_offset_bps,
            max_acceptable_cost_bps=max_acceptable_cost_bps,
            policy_reason=policy_reason,
            mode=mode,
            price_style="limit",
            passive_first=mode == "passive_first",
            bounded_limit_ioc=True,
            bounded_taker=False,
            reason=policy_reason,
        )
    if mode == "post_only_with_timeout_fallback":
        # post_only_with_timeout_fallback (2026-04-21): 挂 post_only,
        # 超时 fallback 由 order_manager orchestration 层完成 (Layer 4).
        # 本层只生成策略; timeout_ms / fallback_mode 由 settings 承载.
        # 详见 docs/design/post_only_maker_exit_mode_2026_04_21.md §3.2
        return IndependentExecutionPolicy(
            edge_strength=edge_strength,
            urgency=urgency,
            execution_style_preference="post_only",
            order_type_preference="post_only",
            time_in_force_preference="GTC",  # post_only 在 OKX 永远挂单, 不适用 IOC
            limit_offset_bps_preference=limit_offset_bps,
            max_acceptable_cost_bps=max_acceptable_cost_bps,
            policy_reason=policy_reason,
            mode=mode,
            price_style="post_only",
            passive_first=True,
            bounded_limit_ioc=False,
            bounded_taker=False,
            post_only=True,
            reason=policy_reason,
        )
    execution_style = "bounded_taker_cap"
    if mode == "aggressive_bounded_taker":
        execution_style = "aggressive_bounded_taker_cap"
    return IndependentExecutionPolicy(
        edge_strength=edge_strength,
        urgency=urgency,
        execution_style_preference=execution_style,
        order_type_preference="market",
        time_in_force_preference="IOC",
        limit_offset_bps_preference=None,
        max_acceptable_cost_bps=max_acceptable_cost_bps,
        policy_reason=policy_reason,
        mode=mode,
        price_style="market",
        passive_first=False,
        bounded_limit_ioc=False,
        bounded_taker=True,
        reason=policy_reason,
    )


def resolve_execution_policy(
    *,
    settings: AATSSettings,
    book: IndependentBookDecision,
    expectancy_cost_bps: float,
    expectancy_net_edge_bps: float,
    expectancy_slippage_bps: float,
    required_safe_net_edge_bps: float,
) -> IndependentExecutionPolicy | None:
    if book.book_action in {"inactive", "hold", "blocked"}:
        return None
    edge_strength = _edge_strength(
        settings=settings,
        expected_net_edge_bps=expectancy_net_edge_bps,
        weak_edge_report_only=book.weak_edge_report_only,
        required_safe_net_edge_bps=required_safe_net_edge_bps,
    )
    min_liquidity_quality = float(settings.strategy_hedge_independent_min_liquidity_quality)
    liquidity_degraded = (
        book.liquidity_quality_score is not None
        and book.liquidity_quality_score + 1e-9 < min_liquidity_quality
    )
    execution_degraded = book.execution_health_state in {"degraded", "blocked"}
    passive_limit_offset_bps = max(
        Decimal("0.5"),
        to_decimal(expectancy_slippage_bps),
        to_decimal(settings.strategy_hedge_independent_expected_slippage_buffer_bps),
    )
    max_acceptable_cost_bps = float(settings.strategy_hedge_independent_max_acceptable_cost_bps)
    max_cost = max_acceptable_cost_bps if max_acceptable_cost_bps > 0.0 else None

    if book.book_action == "close_failed_thesis":
        configured_mode = settings.strategy_hedge_independent_close_failed_thesis_execution_mode
        if configured_mode != "adaptive":
            return resolve_execution_policy_from_mode(
                mode=configured_mode,
                edge_strength=edge_strength,
                urgency="high",
                limit_offset_bps=None,
                max_acceptable_cost_bps=max_cost,
                policy_reason=f"independent_failed_thesis_configured_{configured_mode}",
            )
        return IndependentExecutionPolicy(
            edge_strength=edge_strength,
            urgency="high",
            execution_style_preference="taker",
            order_type_preference="market",
            time_in_force_preference="IOC",
            limit_offset_bps_preference=None,
            max_acceptable_cost_bps=max_cost,
            policy_reason="independent_failed_thesis_force_exit",
            mode="adaptive_failed_thesis_force_exit",
            price_style="market",
            bounded_taker=True,
            reason="independent_failed_thesis_force_exit",
        )
    if book.book_action == "close_stale_thesis":
        configured_mode = settings.strategy_hedge_independent_close_stale_execution_mode
        if configured_mode != "adaptive":
            return resolve_execution_policy_from_mode(
                mode=configured_mode,
                edge_strength=edge_strength,
                urgency="medium",
                limit_offset_bps=to_decimal(settings.strategy_hedge_independent_limit_offset_bps_stale_close),
                max_acceptable_cost_bps=max_cost,
                policy_reason=f"independent_stale_thesis_configured_{configured_mode}",
            )
        return IndependentExecutionPolicy(
            edge_strength=edge_strength,
            urgency="medium",
            execution_style_preference="bounded_limit_ioc",
            order_type_preference="limit",
            time_in_force_preference="IOC",
            limit_offset_bps_preference=passive_limit_offset_bps,
            max_acceptable_cost_bps=max_cost,
            policy_reason="independent_stale_thesis_guarded_exit",
            mode="adaptive_stale_thesis_guarded_exit",
            price_style="limit",
            passive_first=True,
            bounded_limit_ioc=True,
            reason="independent_stale_thesis_guarded_exit",
        )
    if book.book_action == "de_risk":
        configured_mode = settings.strategy_hedge_independent_de_risk_execution_mode
        de_risk_urgency: Literal["low", "medium", "high"] = (
            "high" if book.close_reason == "execution_health_degraded" else "medium"
        )
        if configured_mode != "adaptive":
            return resolve_execution_policy_from_mode(
                mode=configured_mode,
                edge_strength=edge_strength,
                urgency=de_risk_urgency,
                limit_offset_bps=passive_limit_offset_bps,
                max_acceptable_cost_bps=max_cost,
                policy_reason=f"independent_de_risk_configured_{configured_mode}",
            )
        if book.close_reason == "execution_health_degraded":
            return IndependentExecutionPolicy(
                edge_strength=edge_strength,
                urgency="high",
                execution_style_preference="taker",
                order_type_preference="market",
                time_in_force_preference="IOC",
                limit_offset_bps_preference=None,
                max_acceptable_cost_bps=max_cost,
                policy_reason="independent_execution_health_urgent_exit",
                mode="adaptive_execution_health_urgent_exit",
                price_style="market",
                bounded_taker=True,
                reason="independent_execution_health_urgent_exit",
            )
        if book.close_reason == "liquidity_degraded":
            return IndependentExecutionPolicy(
                edge_strength=edge_strength,
                urgency="medium",
                execution_style_preference="bounded_limit_ioc",
                order_type_preference="limit",
                time_in_force_preference="IOC",
                limit_offset_bps_preference=passive_limit_offset_bps,
                max_acceptable_cost_bps=max_cost,
                policy_reason="independent_liquidity_degraded_guarded_reduce",
                mode="adaptive_liquidity_degraded_guarded_reduce",
                price_style="limit",
                passive_first=True,
                bounded_limit_ioc=True,
                reason="independent_liquidity_degraded_guarded_reduce",
            )
        return IndependentExecutionPolicy(
            edge_strength=edge_strength,
            urgency="medium",
            execution_style_preference="bounded_limit_ioc",
            order_type_preference="limit",
            time_in_force_preference="IOC",
            limit_offset_bps_preference=passive_limit_offset_bps,
            max_acceptable_cost_bps=max_cost,
            policy_reason="independent_weak_edge_guarded_reduce",
            mode="adaptive_weak_edge_guarded_reduce",
            price_style="limit",
            passive_first=True,
            bounded_limit_ioc=True,
            reason="independent_weak_edge_guarded_reduce",
        )
    if book.book_action in {"scale_in", "open"}:
        return _adaptive_entry_or_scale(
            settings=settings,
            book=book,
            edge_strength=edge_strength,
            liquidity_degraded=liquidity_degraded,
            execution_degraded=execution_degraded,
            passive_limit_offset_bps=passive_limit_offset_bps,
            max_cost=max_cost,
        )
    return None


def _adaptive_entry_or_scale(
    *,
    settings: AATSSettings,
    book: IndependentBookDecision,
    edge_strength: Literal["weak", "medium", "strong"],
    liquidity_degraded: bool,
    execution_degraded: bool,
    passive_limit_offset_bps: Decimal,
    max_cost: float | None,
) -> IndependentExecutionPolicy:
    is_scale = book.book_action == "scale_in"
    label = "scale" if is_scale else "entry"
    configured_mode = (
        settings.strategy_hedge_independent_scale_in_execution_mode
        if is_scale
        else settings.strategy_hedge_independent_entry_execution_mode
    )
    limit_offset = (
        to_decimal(settings.strategy_hedge_independent_limit_offset_bps_scale_in)
        if is_scale
        else to_decimal(settings.strategy_hedge_independent_limit_offset_bps_entry)
    )
    if configured_mode != "adaptive":
        configured_label = "scale_in" if is_scale else "entry"
        return resolve_execution_policy_from_mode(
            mode=configured_mode,
            edge_strength=edge_strength,
            urgency="low" if configured_mode in {
                "passive_first",
                "bounded_limit",
                "post_only_with_timeout_fallback",
            } else "medium",
            limit_offset_bps=limit_offset,
            max_acceptable_cost_bps=max_cost,
            policy_reason=f"independent_{configured_label}_configured_{configured_mode}",
        )
    if edge_strength == "strong" and not liquidity_degraded and not execution_degraded:
        return IndependentExecutionPolicy(
            edge_strength=edge_strength,
            urgency="medium",
            execution_style_preference="taker",
            order_type_preference="market",
            time_in_force_preference="IOC",
            limit_offset_bps_preference=None,
            max_acceptable_cost_bps=max_cost,
            policy_reason=f"independent_{label}_strong_edge_aggressive",
            mode=f"adaptive_{label}_strong_edge_aggressive",
            price_style="market",
            bounded_taker=True,
            reason=f"independent_{label}_strong_edge_aggressive",
        )
    if bool(settings.strategy_hedge_independent_passive_first_enabled):
        passive_reason = (
            f"independent_{label}_guarded_passive_first"
            if is_scale or not book.weak_edge_report_only
            else "independent_weak_edge_passive_first_required"
        )
        return IndependentExecutionPolicy(
            edge_strength=edge_strength,
            urgency="low",
            execution_style_preference="bounded_limit_ioc",
            order_type_preference="limit",
            time_in_force_preference="IOC",
            limit_offset_bps_preference=passive_limit_offset_bps,
            max_acceptable_cost_bps=max_cost,
            policy_reason=passive_reason,
            mode=f"adaptive_{label}_guarded_passive_first",
            price_style="limit",
            passive_first=True,
            bounded_limit_ioc=True,
            reason=passive_reason,
        )
    return IndependentExecutionPolicy(
        edge_strength=edge_strength,
        urgency="medium",
        execution_style_preference="taker",
        order_type_preference="market",
        time_in_force_preference="IOC",
        limit_offset_bps_preference=None,
        max_acceptable_cost_bps=max_cost,
        policy_reason=f"independent_{label}_guarded_aggressive_fallback",
        mode=f"adaptive_{label}_guarded_aggressive_fallback",
        price_style="market",
        bounded_taker=True,
        reason=f"independent_{label}_guarded_aggressive_fallback",
    )


def _edge_strength(
    *,
    settings: AATSSettings,
    expected_net_edge_bps: float,
    weak_edge_report_only: bool,
    required_safe_net_edge_bps: float,
) -> Literal["weak", "medium", "strong"]:
    medium_edge_threshold = (
        required_safe_net_edge_bps
        + max(float(settings.strategy_hedge_independent_expected_execution_buffer_bps), 1.0)
    )
    if weak_edge_report_only or expected_net_edge_bps < required_safe_net_edge_bps:
        return "weak"
    if expected_net_edge_bps < medium_edge_threshold:
        return "medium"
    return "strong"
