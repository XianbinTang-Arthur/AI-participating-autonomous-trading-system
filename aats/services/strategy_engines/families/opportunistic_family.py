from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.decision import AIMarketAssessment, BaselineAssessment, DecisionContext, HedgeOverlayDecision
from aats.schemas.strategy_runtime import (
    StrategyCandidate,
    StrategyFamily,
    StrategyFamilyAction,
    StrategyLegIntent,
)
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
from aats.services.strategy_engines.base import StrategyEvaluationContext, StrategyFamilyRuntimeControl
from aats.services.strategy_engines.families.protective_family import (
    _candidate_state_from_overlay_state,
    _overlay_route_action,
    _placeholder_family_candidate,
    _signed_leg_qty,
    protective_runtime_supported,
)
from aats.services.strategy_overlay_rollout import overlay_rollout_status


class OpportunisticFamilyEngine:
    family_name: StrategyFamily = "opportunistic"

    def __init__(self, *, settings: AATSSettings) -> None:
        self.settings = settings

    def evaluate(self, context: StrategyEvaluationContext) -> list[StrategyCandidate]:
        return [
            opportunistic_candidate_from_directional_target(
                settings=self.settings,
                evaluation_context=context,
            )
        ]


def opportunistic_candidate_from_directional_target(
    *,
    settings: AATSSettings,
    evaluation_context: StrategyEvaluationContext,
) -> StrategyCandidate:
    family: StrategyFamily = "opportunistic"
    control = evaluation_context.family_runtime_controls.get(family, StrategyFamilyRuntimeControl())
    if not control.enabled:
        return _placeholder_family_candidate(
            family=family,
            context=evaluation_context,
            headline="Opportunistic 家族已注册，但当前未启用。",
            placeholder_reason="strategy_family_opportunistic_disabled",
            skeleton_mode=False,
        )

    context = evaluation_context.context
    baseline = evaluation_context.baseline
    directional_target = evaluation_context.directional_target
    ai_assessment = evaluation_context.ai_assessment
    runtime_supported = protective_runtime_supported(settings=settings, context=context)
    configured_mode = settings.strategy_hedge_overlay_mode
    metrics = {
        "family_registry_enabled": True,
        "shadow_mode_enabled": control.shadow_mode_enabled,
        "live_execution_enabled": control.live_execution_enabled,
        "skeleton_mode": False,
        "legacy_execution_owner": "directional",
    }
    if not settings.strategy_hedge_overlay_enabled:
        return StrategyCandidate(
            family=family,
            state="disabled",
            enabled=True,
            selectable=False,
            execution_compatible=False,
            route_action="advisory_only",
            headline="Opportunistic 家族已关闭：总 hedge overlay 开关未开启。",
            reason_codes=["strategy_hedge_overlay_disabled"],
            control_summary="Opportunistic 家族已接入，但当前由总 overlay 开关关闭。",
            metrics=metrics,
        )
    if not runtime_supported:
        return StrategyCandidate(
            family=family,
            state="incompatible",
            enabled=True,
            selectable=False,
            execution_compatible=False,
            route_action="advisory_only",
            headline="Opportunistic 家族当前运行域不支持。",
            reason_codes=["hedge_overlay_runtime_not_supported"],
            blocking_reasons=["hedge_overlay_runtime_not_supported"],
            control_summary="Opportunistic 家族仅支持 derivatives + hedge 运行域。",
            metrics=metrics,
        )
    if configured_mode != "opportunistic":
        return StrategyCandidate(
            family=family,
            state="inactive",
            enabled=True,
            selectable=False,
            execution_compatible=False,
            route_action="advisory_only",
            headline="Opportunistic 家族当前不是激活模式。",
            reason_codes=["strategy_family_opportunistic_waiting_for_activation"],
            blocking_reasons=["strategy_hedge_overlay_mode_not_opportunistic"],
            control_summary="Opportunistic 家族已评估，但当前主模式不是 opportunistic。",
            execution_mode="opportunistic_overlay",
            metrics={
                **metrics,
                "configured_mode": configured_mode,
            },
        )

    overlay_decision = evaluate_opportunistic_overlay_decision(
        settings=settings,
        context=context,
        baseline=baseline,
        ai_assessment=ai_assessment,
        long_target_qty=max(to_decimal(directional_target.target_position_qty), Decimal("0")),
        short_target_qty=max(-to_decimal(directional_target.target_position_qty), Decimal("0")),
    )
    hedge_leg = build_opportunistic_candidate_leg(
        symbol=directional_target.symbol,
        target_leverage=float(directional_target.target_leverage),
        margin_mode=str(directional_target.margin_mode),
        overlay_decision=overlay_decision,
    )
    target_qty = _signed_leg_qty(
        signal=overlay_decision.hedge_leg_signal,
        quantity=overlay_decision.hedge_leg_target_qty,
    )
    current_qty = _signed_leg_qty(
        signal=overlay_decision.hedge_leg_signal,
        quantity=overlay_decision.hedge_leg_current_qty,
    )
    reason_codes = list(
        dict.fromkeys(
            [
                *overlay_decision.reason_codes,
                *(
                    ["opportunistic_family_candidate_active"]
                    if overlay_decision.active
                    else ["opportunistic_family_candidate_inactive"]
                ),
            ]
        )
    )
    return StrategyCandidate(
        family=family,
        state=_candidate_state_from_overlay_state(overlay_decision.state),
        enabled=True,
        selectable=bool(control.live_execution_enabled and (hedge_leg is not None or overlay_decision.active)),
        execution_compatible=bool(hedge_leg is not None or overlay_decision.active),
        route_action=_overlay_route_action(
            hedge_leg=hedge_leg,
            overlay_decision=overlay_decision,
            control=control,
        ),
        family_action=_opportunistic_family_action(
            hedge_leg=hedge_leg,
            overlay_decision=overlay_decision,
        ),
        headline=_opportunistic_candidate_headline(overlay_decision=overlay_decision),
        recommended_symbol=directional_target.symbol,
        target_position_qty=target_qty,
        delta_position_qty=target_qty - current_qty,
        score=float(overlay_decision.pressure_score),
        confidence=min(0.95, 0.35 + max(float(overlay_decision.pressure_score), 0.0) * 0.55),
        urgency="high"
        if overlay_decision.state in {"opening", "closing"}
        else ("medium" if overlay_decision.active else "low"),
        reason_codes=reason_codes,
        control_summary=(
            "Opportunistic 家族已独立评估，并可直接进入 allocator / apply 主路径。"
            if control.live_execution_enabled
            else "Opportunistic 家族已独立评估；当前执行仍由 directional 主链承接。"
        ),
        execution_mode="opportunistic_overlay",
        state_phase=overlay_decision.state,
        blocking_reasons=list(overlay_decision.blocked_reasons),
        metrics={
            **metrics,
            "configured_mode": configured_mode,
            "main_leg_signal": overlay_decision.main_leg_signal,
            "hedge_leg_signal": overlay_decision.hedge_leg_signal,
            "main_leg_current_qty": overlay_decision.main_leg_current_qty,
            "hedge_leg_current_qty": overlay_decision.hedge_leg_current_qty,
            "main_leg_target_qty": overlay_decision.main_leg_target_qty,
            "hedge_leg_target_qty": overlay_decision.hedge_leg_target_qty,
            "hedge_ratio": overlay_decision.hedge_ratio,
            "max_ratio": overlay_decision.max_ratio,
            "pressure_score": overlay_decision.pressure_score,
            "open_threshold": overlay_decision.open_threshold,
            "close_threshold": overlay_decision.close_threshold,
            "open_condition": overlay_decision.open_condition,
            "close_condition": overlay_decision.close_condition,
            "fee_drag_ratio": overlay_decision.fee_drag_ratio,
            "churn_ratio": overlay_decision.churn_ratio,
            "min_hold_remaining_seconds": overlay_decision.min_hold_remaining_seconds,
            "rebalance_cooldown_remaining_seconds": overlay_decision.rebalance_cooldown_remaining_seconds,
            "blocked_reasons": list(overlay_decision.blocked_reasons),
            "rollout_stage": overlay_decision.rollout_stage,
            "runtime_rollout_stage": overlay_decision.runtime_rollout_stage,
        },
        legs=[] if hedge_leg is None else [hedge_leg],
    )


def evaluate_opportunistic_overlay_decision(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
    long_target_qty: Decimal,
    short_target_qty: Decimal,
    scorer: Callable[..., float] | None = None,
) -> HedgeOverlayDecision:
    configured_mode = settings.strategy_hedge_overlay_mode
    if not settings.strategy_hedge_opportunistic_enabled:
        return HedgeOverlayDecision(
            enabled=True,
            runtime_supported=True,
            configured_mode=configured_mode,
            state="blocked",
            blocked_reasons=["opportunistic_overlay_not_enabled"],
        )
    rollout = overlay_rollout_status(settings, mode="opportunistic")
    if not rollout["runtime_allowed"]:
        return HedgeOverlayDecision(
            enabled=True,
            runtime_supported=True,
            configured_mode=configured_mode,
            effective_mode="opportunistic",
            overlay_source="opportunistic",
            state="blocked",
            blocked_reasons=list(rollout["blocking_reasons"]),
            reason_codes=["opportunistic_overlay_rollout_gate_active"],
            rollout_stage=rollout["configured_rollout_stage"],
            runtime_rollout_stage=rollout["runtime_stage"],
        )

    main_leg_signal = _exposure_side(long_target_qty - short_target_qty)
    if main_leg_signal == "flat":
        return HedgeOverlayDecision(
            enabled=True,
            runtime_supported=True,
            configured_mode=configured_mode,
            effective_mode="opportunistic",
            overlay_source="opportunistic",
            state="inactive",
            main_leg_signal="flat",
            hedge_leg_signal="flat",
            max_ratio=to_decimal(settings.strategy_hedge_opportunistic_max_ratio),
            open_threshold=settings.strategy_hedge_opportunistic_open_threshold,
            close_threshold=settings.strategy_hedge_opportunistic_close_threshold,
            fee_drag_ratio=context.recent_fee_drag_ratio,
            churn_ratio=context.recent_churn_ratio,
            reason_codes=["opportunistic_overlay_main_signal_flat"],
            rollout_stage=rollout["configured_rollout_stage"],
            runtime_rollout_stage=rollout["runtime_stage"],
        )

    if main_leg_signal == "long":
        main_leg_current_qty = to_decimal(context.current_long_position_qty)
        hedge_leg_current_qty = to_decimal(context.current_short_position_qty)
        main_leg_target_qty = to_decimal(long_target_qty)
        hedge_leg_signal = "short"
        current_leg_opened_at = context.current_short_leg_opened_at
        last_leg_closed_at = context.last_short_leg_closed_at
        latest_leg_fill_timestamp = context.latest_short_leg_fill_timestamp
    else:
        main_leg_current_qty = to_decimal(context.current_short_position_qty)
        hedge_leg_current_qty = to_decimal(context.current_long_position_qty)
        main_leg_target_qty = to_decimal(short_target_qty)
        hedge_leg_signal = "long"
        current_leg_opened_at = context.current_long_leg_opened_at
        last_leg_closed_at = context.last_long_leg_closed_at
        latest_leg_fill_timestamp = context.latest_long_leg_fill_timestamp

    if main_leg_current_qty <= EPSILON_DECIMAL_12:
        return HedgeOverlayDecision(
            enabled=True,
            runtime_supported=True,
            configured_mode="opportunistic",
            effective_mode="opportunistic",
            overlay_source="opportunistic",
            state="inactive",
            main_leg_signal=main_leg_signal,
            hedge_leg_signal=hedge_leg_signal,
            main_leg_current_qty=main_leg_current_qty,
            hedge_leg_current_qty=Decimal("0"),
            main_leg_target_qty=main_leg_target_qty,
            hedge_leg_target_qty=Decimal("0"),
            max_ratio=to_decimal(settings.strategy_hedge_opportunistic_max_ratio),
            open_threshold=settings.strategy_hedge_opportunistic_open_threshold,
            close_threshold=settings.strategy_hedge_opportunistic_close_threshold,
            fee_drag_ratio=context.recent_fee_drag_ratio,
            churn_ratio=context.recent_churn_ratio,
            reason_codes=["opportunistic_overlay_no_existing_inventory"],
            rollout_stage=rollout["configured_rollout_stage"],
            runtime_rollout_stage=rollout["runtime_stage"],
        )

    opportunity_score = (
        scorer(
            main_leg_signal=main_leg_signal,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )
        if scorer is not None
        else opportunistic_overlay_score(
            main_leg_signal=main_leg_signal,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )
    )
    max_ratio = to_decimal(settings.strategy_hedge_opportunistic_max_ratio)
    open_threshold = float(settings.strategy_hedge_opportunistic_open_threshold)
    close_threshold = float(settings.strategy_hedge_opportunistic_close_threshold)
    target_ratio = Decimal("0")
    reason_codes: list[str] = []
    blocked_reasons: list[str] = []
    min_hold_remaining_seconds = 0.0
    rebalance_cooldown_remaining_seconds = 0.0

    if main_leg_target_qty <= EPSILON_DECIMAL_12:
        reason_codes.append("opportunistic_overlay_main_leg_target_flat")
    elif opportunity_score >= open_threshold:
        target_ratio = min(max_ratio, max_ratio * to_decimal(opportunity_score))
        reason_codes.append("opportunistic_overlay_signal_above_open_threshold")
    elif hedge_leg_current_qty > EPSILON_DECIMAL_12 and opportunity_score > close_threshold:
        target_ratio = min(max_ratio, max_ratio * to_decimal(opportunity_score))
        reason_codes.append("opportunistic_overlay_hold_above_close_threshold")
    else:
        reason_codes.append("opportunistic_overlay_signal_below_open_threshold")

    hedge_leg_target_qty = main_leg_target_qty * target_ratio
    opening_or_expanding = hedge_leg_target_qty > hedge_leg_current_qty + EPSILON_DECIMAL_12
    enough_history = context.recent_closed_trade_count >= settings.strategy_performance_guard_min_closed_trades
    if opening_or_expanding and enough_history:
        if context.recent_fee_drag_ratio > settings.strategy_hedge_opportunistic_max_fee_drag_ratio:
            hedge_leg_target_qty = hedge_leg_current_qty
            blocked_reasons.append("opportunistic_overlay_fee_drag_guard_active")
        elif context.recent_churn_ratio > settings.strategy_hedge_opportunistic_max_churn_ratio:
            hedge_leg_target_qty = hedge_leg_current_qty
            blocked_reasons.append("opportunistic_overlay_churn_guard_active")

    now = context.as_of_ts
    if hedge_leg_current_qty > EPSILON_DECIMAL_12:
        held_for = (
            0.0
            if current_leg_opened_at is None
            else max((now - current_leg_opened_at).total_seconds(), 0.0)
        )
        remaining_hold = max(settings.strategy_hedge_opportunistic_min_hold_seconds - held_for, 0.0)
        if hedge_leg_target_qty <= EPSILON_DECIMAL_12 and remaining_hold > 0:
            hedge_leg_target_qty = hedge_leg_current_qty
            min_hold_remaining_seconds = remaining_hold
            blocked_reasons.append("opportunistic_overlay_min_hold_active")

    if latest_leg_fill_timestamp is not None:
        since_rebalance = max((now - latest_leg_fill_timestamp).total_seconds(), 0.0)
        remaining_rebalance = max(
            settings.strategy_hedge_opportunistic_rebalance_cooldown_seconds - since_rebalance,
            0.0,
        )
        if (
            remaining_rebalance > 0
            and abs(hedge_leg_target_qty - hedge_leg_current_qty) > EPSILON_DECIMAL_12
        ):
            hedge_leg_target_qty = hedge_leg_current_qty
            rebalance_cooldown_remaining_seconds = remaining_rebalance
            blocked_reasons.append("opportunistic_overlay_rebalance_cooldown_active")
    elif hedge_leg_current_qty <= EPSILON_DECIMAL_12 and last_leg_closed_at is not None:
        since_close = max((now - last_leg_closed_at).total_seconds(), 0.0)
        remaining_rebalance = max(
            settings.strategy_hedge_opportunistic_rebalance_cooldown_seconds - since_close,
            0.0,
        )
        if remaining_rebalance > 0 and hedge_leg_target_qty > EPSILON_DECIMAL_12:
            hedge_leg_target_qty = Decimal("0")
            rebalance_cooldown_remaining_seconds = remaining_rebalance
            blocked_reasons.append("opportunistic_overlay_rebalance_cooldown_active")

    state = "inactive"
    active = hedge_leg_target_qty > EPSILON_DECIMAL_12 or hedge_leg_current_qty > EPSILON_DECIMAL_12
    if hedge_leg_target_qty > EPSILON_DECIMAL_12 and hedge_leg_current_qty <= EPSILON_DECIMAL_12:
        state = "blocked" if blocked_reasons else "opening"
    elif hedge_leg_target_qty > EPSILON_DECIMAL_12 and hedge_leg_current_qty > EPSILON_DECIMAL_12:
        state = (
            "blocked"
            if blocked_reasons and abs(hedge_leg_target_qty - hedge_leg_current_qty) > EPSILON_DECIMAL_12
            else "holding"
        )
    elif hedge_leg_target_qty <= EPSILON_DECIMAL_12 and hedge_leg_current_qty > EPSILON_DECIMAL_12:
        state = "blocked" if blocked_reasons else "closing"

    open_condition = f"机会分数 {opportunity_score:.2f} >= {open_threshold:.2f}"
    close_condition = f"机会分数 {opportunity_score:.2f} <= {close_threshold:.2f}"
    return HedgeOverlayDecision(
        enabled=True,
        runtime_supported=True,
        configured_mode="opportunistic",
        effective_mode="opportunistic",
        overlay_source="opportunistic",
        active=active,
        state=state,
        main_leg_signal=main_leg_signal,
        hedge_leg_signal=hedge_leg_signal,
        main_leg_current_qty=main_leg_current_qty,
        hedge_leg_current_qty=hedge_leg_current_qty,
        main_leg_target_qty=main_leg_target_qty,
        hedge_leg_target_qty=hedge_leg_target_qty,
        hedge_ratio=(
            Decimal("0")
            if main_leg_target_qty <= EPSILON_DECIMAL_12
            else min(hedge_leg_target_qty / main_leg_target_qty, Decimal("1"))
        ),
        max_ratio=max_ratio,
        pressure_score=opportunity_score,
        open_threshold=open_threshold,
        close_threshold=close_threshold,
        open_condition=open_condition,
        close_condition=close_condition,
        fee_drag_ratio=context.recent_fee_drag_ratio,
        churn_ratio=context.recent_churn_ratio,
        reason_codes=reason_codes,
        blocked_reasons=blocked_reasons,
        min_hold_remaining_seconds=min_hold_remaining_seconds,
        rebalance_cooldown_remaining_seconds=rebalance_cooldown_remaining_seconds,
        rollout_stage=rollout["configured_rollout_stage"],
        runtime_rollout_stage=rollout["runtime_stage"],
    )


def build_opportunistic_candidate_leg(
    *,
    symbol: str,
    target_leverage: float,
    margin_mode: str,
    overlay_decision: HedgeOverlayDecision,
) -> StrategyLegIntent | None:
    pos_side = overlay_decision.hedge_leg_signal
    if pos_side not in {"long", "short"}:
        return None
    current_leg_qty = max(to_decimal(overlay_decision.hedge_leg_current_qty), Decimal("0"))
    target_leg_qty = max(to_decimal(overlay_decision.hedge_leg_target_qty), Decimal("0"))
    delta_qty = target_leg_qty - current_leg_qty
    if abs(delta_qty) <= EPSILON_DECIMAL_12:
        return None
    opening = delta_qty > 0
    action = "open" if opening else ("close" if target_leg_qty <= EPSILON_DECIMAL_12 else "reduce")
    if pos_side == "long":
        side = "buy" if opening else "sell"
        signed_current_qty = current_leg_qty
        signed_target_qty = target_leg_qty
    else:
        side = "sell" if opening else "buy"
        signed_current_qty = -current_leg_qty
        signed_target_qty = -target_leg_qty
    return StrategyLegIntent(
        symbol=symbol,
        product_type="derivatives",
        side=side,
        position_mode="long_short_mode",
        pos_side=pos_side,
        action=action,
        family="opportunistic",
        role="hedge",
        margin_mode=margin_mode,
        target_leverage=target_leverage,
        current_position_qty=signed_current_qty,
        target_position_qty=signed_target_qty,
        delta_position_qty=signed_target_qty - signed_current_qty,
        execution_compatible=True,
        execution_mode="opportunistic_overlay",
        state_phase=overlay_decision.state,
        overlay_mode="opportunistic",
        hedge_ratio=overlay_decision.hedge_ratio,
        trigger_reason_codes=list(overlay_decision.reason_codes),
        note="Opportunistic family 生成的机会腿。",
    )


def opportunistic_overlay_score(
    *,
    main_leg_signal: str,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
) -> float:
    side_sign = 1.0 if main_leg_signal == "long" else -1.0
    microstructure_alpha = float(baseline.factor_scores.get("microstructure_alpha", 0.0))
    momentum_alpha = float(baseline.factor_scores.get("momentum_alpha", 0.0))
    trend_alpha = float(baseline.factor_scores.get("trend_alpha", 0.0))
    opposite_microstructure = max(0.0, -(side_sign * microstructure_alpha))
    opposite_momentum = max(0.0, -(side_sign * momentum_alpha))
    opposite_trend = max(0.0, -(side_sign * trend_alpha))
    opposite_ai = max(0.0, -(side_sign * _ai_directional_edge(ai_assessment)))
    confidence = _clamp(float(baseline.confidence), 0.0, 1.0)
    opportunity = (
        (_clamp(opposite_microstructure, 0.0, 1.0) * 0.28)
        + (_clamp(opposite_ai, 0.0, 1.0) * 0.24)
        + (_clamp(opposite_momentum, 0.0, 1.0) * 0.18)
        + (_clamp(opposite_trend, 0.0, 1.0) * 0.12)
        + (confidence * 0.10)
    )
    if baseline.regime in {"range", "uncertain"}:
        opportunity += 0.08
    if baseline.volatility_state == "high":
        opportunity += 0.06
    if baseline.direction_bias not in {main_leg_signal, "flat"}:
        opportunity += 0.10
    return _clamp(opportunity, 0.0, 1.0)


def _opportunistic_family_action(
    *,
    hedge_leg: StrategyLegIntent | None,
    overlay_decision: HedgeOverlayDecision,
) -> StrategyFamilyAction:
    if hedge_leg is not None:
        if str(hedge_leg.action).lower() == "close":
            return "close_opportunity_leg"
        return "open_opportunity_leg"
    if overlay_decision.blocked_reasons:
        return "blocked"
    return "hold_family"


def _opportunistic_candidate_headline(*, overlay_decision: HedgeOverlayDecision) -> str:
    hedge_label = _opportunistic_signal_label(overlay_decision.hedge_leg_signal, default="机会腿")
    if overlay_decision.state == "opening":
        return f"Opportunistic 家族计划建立{hedge_label}。"
    if overlay_decision.state == "holding":
        return f"Opportunistic 家族当前维持{hedge_label}。"
    if overlay_decision.state == "closing":
        return f"Opportunistic 家族计划退出{hedge_label}。"
    if overlay_decision.state == "blocked":
        return f"Opportunistic 家族当前被阻断：{_first_reason(overlay_decision.blocked_reasons)}。"
    if overlay_decision.main_leg_signal == "flat":
        return "Opportunistic 家族当前没有可依附的主腿。"
    if overlay_decision.main_leg_current_qty <= EPSILON_DECIMAL_12:
        return "Opportunistic 家族当前没有既有主腿库存。"
    return "Opportunistic 家族当前未触发机会腿。"


def _opportunistic_signal_label(signal: str, *, default: str) -> str:
    if signal == "long":
        return "开多机会腿"
    if signal == "short":
        return "开空机会腿"
    return default


def _first_reason(reasons: list[str]) -> str:
    if not reasons:
        return "没有额外阻断原因"
    return str(reasons[0])


def _exposure_side(quantity: Decimal) -> str:
    if quantity > EPSILON_DECIMAL_12:
        return "long"
    if quantity < -EPSILON_DECIMAL_12:
        return "short"
    return "flat"


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def _ai_directional_edge(ai_assessment: AIMarketAssessment | None) -> float:
    return 0.0 if ai_assessment is None else ai_assessment.directional_edge
