from __future__ import annotations

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
from aats.services.strategy_engines.base import (
    StrategyEvaluationContext,
    StrategyFamilyRuntimeControl,
)
from aats.services.strategy_engines.overlay_parent_exposure import (
    OverlayMainLegContract,
    OverlayParentExposureContract,
    resolve_overlay_main_leg_contract as _resolve_overlay_main_leg_contract_from_parent_exposure,
    resolve_overlay_parent_exposure_from_direct_args,
    resolve_overlay_parent_exposure_lifecycle,
)


class ProtectiveFamilyEngine:
    family_name: StrategyFamily = "protective"

    def __init__(self, *, settings: AATSSettings) -> None:
        self.settings = settings

    def evaluate(self, context: StrategyEvaluationContext) -> list[StrategyCandidate]:
        return [
            protective_candidate_from_directional_target(
                settings=self.settings,
                evaluation_context=context,
            )
        ]


def protective_candidate_from_directional_target(
    *,
    settings: AATSSettings,
    evaluation_context: StrategyEvaluationContext,
) -> StrategyCandidate:
    family: StrategyFamily = "protective"
    control = evaluation_context.family_runtime_controls.get(family, StrategyFamilyRuntimeControl())
    if not control.enabled:
        return _placeholder_family_candidate(
            family=family,
            context=evaluation_context,
            headline="Protective 家族已注册，但当前未启用。",
            placeholder_reason="strategy_family_protective_disabled",
            skeleton_mode=False,
        )

    context = evaluation_context.context
    baseline = evaluation_context.baseline
    ai_assessment = evaluation_context.ai_assessment
    parent_exposure = _resolve_overlay_parent_exposure_contract(
        settings=settings,
        evaluation_context=evaluation_context,
    )

    runtime_supported = protective_runtime_supported(settings=settings, context=context)
    configured_mode = settings.strategy_hedge_overlay_mode
    metrics = {
        "family_registry_enabled": True,
        "shadow_mode_enabled": control.shadow_mode_enabled,
        "live_execution_enabled": control.live_execution_enabled,
        "skeleton_mode": False,
        "execution_owner": family,
    }
    if not settings.strategy_hedge_overlay_enabled:
        return StrategyCandidate(
            family=family,
            state="disabled",
            enabled=True,
            selectable=False,
            execution_compatible=False,
            route_action="advisory_only",
            headline="Protective 家族已关闭：总 hedge overlay 开关未开启。",
            reason_codes=["strategy_hedge_overlay_disabled"],
            control_summary="Protective 家族已接入，但当前由总 overlay 开关关闭。",
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
            headline="Protective 家族当前运行域不支持。",
            reason_codes=["hedge_overlay_runtime_not_supported"],
            blocking_reasons=["hedge_overlay_runtime_not_supported"],
            control_summary="Protective 家族仅支持 derivatives + hedge 运行域。",
            metrics=metrics,
        )
    if configured_mode != "protective":
        return StrategyCandidate(
            family=family,
            state="inactive",
            enabled=True,
            selectable=False,
            execution_compatible=False,
            route_action="advisory_only",
            headline="Protective 家族当前不是激活模式。",
            reason_codes=["strategy_family_protective_waiting_for_activation"],
            blocking_reasons=["strategy_hedge_overlay_mode_not_protective"],
            control_summary="Protective 家族已评估，但当前主模式不是 protective。",
            execution_mode="protective_overlay",
            metrics={
                **metrics,
                "configured_mode": configured_mode,
            },
        )
    overlay_decision = evaluate_protective_overlay_decision(
        settings=settings,
        context=context,
        baseline=baseline,
        ai_assessment=ai_assessment,
        parent_exposure=parent_exposure,
    )
    hedge_leg = build_protective_candidate_leg(
        symbol=parent_exposure.symbol,
        target_leverage=parent_exposure.target_leverage,
        margin_mode=parent_exposure.margin_mode,
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
    reason_codes = list(dict.fromkeys([
        *overlay_decision.reason_codes,
        *(
            ["protective_family_candidate_active"]
            if overlay_decision.active
            else ["protective_family_candidate_inactive"]
        ),
    ]))
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
        family_action=_protective_family_action(
            hedge_leg=hedge_leg,
            overlay_decision=overlay_decision,
        ),
        headline=_protective_candidate_headline(overlay_decision=overlay_decision),
        recommended_symbol=parent_exposure.symbol,
        target_position_qty=target_qty,
        delta_position_qty=target_qty - current_qty,
        score=float(overlay_decision.pressure_score),
        confidence=min(0.95, 0.35 + max(float(overlay_decision.pressure_score), 0.0) * 0.55),
        urgency="high"
        if overlay_decision.state in {"opening", "closing"}
        else ("medium" if overlay_decision.active else "low"),
        reason_codes=reason_codes,
        control_summary=(
            "Protective 家族已独立评估，并可直接进入 allocator / apply 主路径。"
            if control.live_execution_enabled
            else "Protective 家族已独立评估；当前仅参与候选评估，不接管执行主路径。"
        ),
        execution_mode="protective_overlay",
        state_phase=overlay_decision.state,
        blocking_reasons=list(overlay_decision.blocked_reasons),
        metrics={
            **metrics,
            "configured_mode": configured_mode,
            "main_leg_contract_source": parent_exposure.source,
            "parent_family": parent_exposure.parent_family,
            "parent_lifecycle_state": parent_exposure.lifecycle_state,
            "parent_target_active": parent_exposure.target_active,
            "parent_inventory_active": parent_exposure.inventory_active,
            "parent_source_of_truth": parent_exposure.source_of_truth,
            "parent_exposure_signal_source": parent_exposure.signal_source,
            "parent_target_qty": parent_exposure.target_qty,
            "parent_current_qty": parent_exposure.current_qty,
            "parent_effective_qty": parent_exposure.effective_qty,
            "parent_target_signal": parent_exposure.target_signal,
            "parent_current_signal": parent_exposure.current_signal,
            "parent_effective_signal": parent_exposure.effective_signal,
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
            "min_hold_remaining_seconds": overlay_decision.min_hold_remaining_seconds,
            "rebalance_cooldown_remaining_seconds": overlay_decision.rebalance_cooldown_remaining_seconds,
            "blocked_reasons": list(overlay_decision.blocked_reasons),
        },
        legs=[] if hedge_leg is None else [hedge_leg],
    )


def _resolve_overlay_parent_exposure_contract(
    *,
    settings: AATSSettings,
    evaluation_context: StrategyEvaluationContext,
) -> OverlayParentExposureContract:
    precomputed_parent_exposure = getattr(evaluation_context, "overlay_parent_exposure", None)
    if precomputed_parent_exposure is not None:
        return precomputed_parent_exposure
    return resolve_overlay_parent_exposure_lifecycle(
        settings=settings,
        context=evaluation_context.context,
        directional_target=evaluation_context.directional_target,
        parent_family="directional",
    )


def _resolve_overlay_main_leg_contract(
    *,
    settings: AATSSettings,
    evaluation_context: StrategyEvaluationContext,
) -> OverlayMainLegContract:
    parent_exposure = _resolve_overlay_parent_exposure_contract(
        settings=settings,
        evaluation_context=evaluation_context,
    )
    return _resolve_overlay_main_leg_contract_from_parent_exposure(parent_exposure)


def evaluate_protective_overlay_decision(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
    parent_exposure: OverlayParentExposureContract | None = None,
    long_target_qty: Decimal | None = None,
    short_target_qty: Decimal | None = None,
) -> HedgeOverlayDecision:
    configured_mode = settings.strategy_hedge_overlay_mode
    if configured_mode != "protective":
        return HedgeOverlayDecision(
            enabled=True,
            runtime_supported=True,
            configured_mode=configured_mode,
            state="blocked",
            blocked_reasons=["hedge_overlay_mode_not_enabled_in_current_phase"],
        )
    if not settings.strategy_hedge_protective_enabled:
        return HedgeOverlayDecision(
            enabled=True,
            runtime_supported=True,
            configured_mode=configured_mode,
            state="blocked",
            blocked_reasons=["protective_overlay_not_enabled"],
        )

    resolved_parent_exposure = (
        resolve_overlay_parent_exposure_from_direct_args(
            settings=settings,
            context=context,
            long_target_qty=long_target_qty,
            short_target_qty=short_target_qty,
            parent_family="directional",
        )
        if parent_exposure is None
        else parent_exposure
    )
    main_leg_signal = resolved_parent_exposure.effective_signal
    main_signal_inferred_from_inventory = resolved_parent_exposure.signal_source == "inventory"
    if main_leg_signal == "flat":
        return HedgeOverlayDecision(
            enabled=True,
            runtime_supported=True,
            configured_mode=configured_mode,
            effective_mode="protective",
            overlay_source="protective",
            state="inactive",
            main_leg_signal="flat",
            hedge_leg_signal="flat",
            max_ratio=to_decimal(settings.strategy_hedge_max_ratio),
            open_threshold=settings.strategy_hedge_open_threshold,
            close_threshold=settings.strategy_hedge_close_threshold,
            fee_drag_ratio=context.recent_fee_drag_ratio,
            churn_ratio=context.recent_churn_ratio,
            reason_codes=["protective_overlay_main_signal_flat"],
        )

    if main_leg_signal == "long":
        main_leg_current_qty = resolved_parent_exposure.current_long_qty
        hedge_leg_current_qty = resolved_parent_exposure.current_short_qty
        main_leg_target_qty = resolved_parent_exposure.target_long_qty
        hedge_leg_signal = "short"
        current_leg_opened_at = context.current_short_leg_opened_at
        last_leg_closed_at = context.last_short_leg_closed_at
        latest_leg_fill_timestamp = context.latest_short_leg_fill_timestamp
    else:
        main_leg_current_qty = resolved_parent_exposure.current_short_qty
        hedge_leg_current_qty = resolved_parent_exposure.current_long_qty
        main_leg_target_qty = resolved_parent_exposure.target_short_qty
        hedge_leg_signal = "long"
        current_leg_opened_at = context.current_long_leg_opened_at
        last_leg_closed_at = context.last_long_leg_closed_at
        latest_leg_fill_timestamp = context.latest_long_leg_fill_timestamp

    if main_leg_current_qty <= EPSILON_DECIMAL_12 and hedge_leg_current_qty <= EPSILON_DECIMAL_12:
        return HedgeOverlayDecision(
            enabled=True,
            runtime_supported=True,
            configured_mode="protective",
            effective_mode="protective",
            overlay_source="protective",
            state="inactive",
            main_leg_signal=main_leg_signal,
            hedge_leg_signal=hedge_leg_signal,
            main_leg_current_qty=main_leg_current_qty,
            hedge_leg_current_qty=hedge_leg_current_qty,
            main_leg_target_qty=main_leg_target_qty,
            hedge_leg_target_qty=Decimal("0"),
            max_ratio=to_decimal(settings.strategy_hedge_max_ratio),
            open_threshold=settings.strategy_hedge_open_threshold,
            close_threshold=settings.strategy_hedge_close_threshold,
            fee_drag_ratio=context.recent_fee_drag_ratio,
            churn_ratio=context.recent_churn_ratio,
            reason_codes=["protective_overlay_no_existing_inventory"],
        )

    pressure_score = _protective_pressure_score(
        settings=settings,
        main_leg_signal=main_leg_signal,
        baseline=baseline,
        ai_assessment=ai_assessment,
    )
    max_ratio = to_decimal(settings.strategy_hedge_max_ratio)
    open_threshold = float(settings.strategy_hedge_open_threshold)
    close_threshold = float(settings.strategy_hedge_close_threshold)
    target_ratio = Decimal("0")
    reason_codes: list[str] = []
    blocked_reasons: list[str] = []
    min_hold_remaining_seconds = 0.0
    rebalance_cooldown_remaining_seconds = 0.0

    if main_leg_target_qty <= EPSILON_DECIMAL_12:
        reason_codes.append("protective_overlay_main_leg_target_flat")
    elif pressure_score >= open_threshold:
        target_ratio = min(max_ratio, max_ratio * to_decimal(pressure_score))
        reason_codes.append("protective_overlay_pressure_above_open_threshold")
    elif hedge_leg_current_qty > EPSILON_DECIMAL_12 and pressure_score > close_threshold:
        target_ratio = min(max_ratio, max_ratio * to_decimal(pressure_score))
        reason_codes.append("protective_overlay_hold_above_close_threshold")
    else:
        reason_codes.append("protective_overlay_pressure_below_open_threshold")
    if main_signal_inferred_from_inventory:
        reason_codes.append("protective_overlay_main_signal_inferred_from_inventory")

    hedge_leg_target_qty = main_leg_target_qty * target_ratio
    now = context.as_of_ts
    if hedge_leg_current_qty > EPSILON_DECIMAL_12:
        held_for = (
            0.0
            if current_leg_opened_at is None
            else max((now - current_leg_opened_at).total_seconds(), 0.0)
        )
        remaining_hold = max(settings.strategy_hedge_min_hold_seconds - held_for, 0.0)
        if hedge_leg_target_qty <= EPSILON_DECIMAL_12 and remaining_hold > 0:
            hedge_leg_target_qty = hedge_leg_current_qty
            min_hold_remaining_seconds = remaining_hold
            blocked_reasons.append("protective_overlay_min_hold_active")

    if latest_leg_fill_timestamp is not None:
        since_rebalance = max((now - latest_leg_fill_timestamp).total_seconds(), 0.0)
        remaining_rebalance = max(
            settings.strategy_hedge_rebalance_cooldown_seconds - since_rebalance,
            0.0,
        )
        if (
            remaining_rebalance > 0
            and abs(hedge_leg_target_qty - hedge_leg_current_qty) > EPSILON_DECIMAL_12
        ):
            hedge_leg_target_qty = hedge_leg_current_qty
            rebalance_cooldown_remaining_seconds = remaining_rebalance
            blocked_reasons.append("protective_overlay_rebalance_cooldown_active")
    elif hedge_leg_current_qty <= EPSILON_DECIMAL_12 and last_leg_closed_at is not None:
        since_close = max((now - last_leg_closed_at).total_seconds(), 0.0)
        remaining_rebalance = max(
            settings.strategy_hedge_rebalance_cooldown_seconds - since_close,
            0.0,
        )
        if remaining_rebalance > 0 and hedge_leg_target_qty > EPSILON_DECIMAL_12:
            hedge_leg_target_qty = Decimal("0")
            rebalance_cooldown_remaining_seconds = remaining_rebalance
            blocked_reasons.append("protective_overlay_rebalance_cooldown_active")

    state = "inactive"
    active = hedge_leg_target_qty > EPSILON_DECIMAL_12 or hedge_leg_current_qty > EPSILON_DECIMAL_12
    if hedge_leg_target_qty > EPSILON_DECIMAL_12 and hedge_leg_current_qty <= EPSILON_DECIMAL_12:
        state = "blocked" if blocked_reasons else "opening"
    elif hedge_leg_target_qty > EPSILON_DECIMAL_12 and hedge_leg_current_qty > EPSILON_DECIMAL_12:
        state = "blocked" if blocked_reasons and abs(hedge_leg_target_qty - hedge_leg_current_qty) > EPSILON_DECIMAL_12 else "holding"
    elif hedge_leg_target_qty <= EPSILON_DECIMAL_12 and hedge_leg_current_qty > EPSILON_DECIMAL_12:
        state = "blocked" if blocked_reasons else "closing"

    open_condition = f"压力分 {pressure_score:.2f} >= {open_threshold:.2f}"
    close_condition = f"压力分 {pressure_score:.2f} <= {close_threshold:.2f}"
    return HedgeOverlayDecision(
        enabled=True,
        runtime_supported=True,
        configured_mode="protective",
        effective_mode="protective",
        overlay_source="protective",
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
        pressure_score=pressure_score,
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
    )


def build_protective_candidate_leg(
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
        family="protective",
        role="hedge",
        margin_mode=margin_mode,
        target_leverage=target_leverage,
        current_position_qty=signed_current_qty,
        target_position_qty=signed_target_qty,
        delta_position_qty=signed_target_qty - signed_current_qty,
        execution_compatible=True,
        execution_mode="protective_overlay",
        state_phase=overlay_decision.state,
        overlay_mode="protective",
        hedge_ratio=overlay_decision.hedge_ratio,
        trigger_reason_codes=list(overlay_decision.reason_codes),
        note="Protective family 生成的保护腿。",
    )


def protective_runtime_supported(*, settings: AATSSettings, context: DecisionContext) -> bool:
    return (
        context.product_type == "derivatives"
        and settings.margin_mode != "cash"
        and settings.derivatives_position_mode == "hedge"
    )


def _protective_pressure_score(
    *,
    settings: AATSSettings,
    main_leg_signal: str,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
) -> float:
    current_position_qty = Decimal("1") if main_leg_signal == "long" else Decimal("-1")
    factors = _position_adverse_factors(
        settings=settings,
        current_position_qty=current_position_qty,
        baseline=baseline,
        ai_assessment=ai_assessment,
    )
    adverse_score = _clamp(float(int(factors["adverse_count"])) / 4.0, 0.0, 1.0)
    side_sign = 1.0 if main_leg_signal == "long" else -1.0
    opposite_alpha = _clamp(max(0.0, -(side_sign * float(baseline.composite_alpha_score))), 0.0, 1.0)
    opposite_ai = _clamp(max(0.0, -(side_sign * _ai_directional_edge(ai_assessment))), 0.0, 1.0)
    confidence = _clamp(float(baseline.confidence), 0.0, 1.0)
    pressure = (adverse_score * 0.45) + (opposite_alpha * 0.25) + (opposite_ai * 0.20) + (confidence * 0.10)
    if baseline.direction_bias not in {main_leg_signal, "flat"}:
        pressure += 0.08
    if baseline.volatility_state == "high":
        pressure += 0.08
    if baseline.regime in {"breakout", "trend"} and adverse_score >= 0.5:
        pressure += 0.05
    return _clamp(pressure, 0.0, 1.0)


def _position_adverse_factors(
    *,
    settings: AATSSettings,
    current_position_qty: Decimal,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
) -> dict[str, object]:
    side_sign = _sign(current_position_qty)
    microstructure = to_decimal(baseline.factor_scores.get("microstructure_alpha", 0.0))
    momentum_alpha = to_decimal(baseline.factor_scores.get("momentum_alpha", 0.0))
    trend_alpha = to_decimal(baseline.factor_scores.get("trend_alpha", 0.0))
    ai_edge = Decimal("0") if ai_assessment is None else to_decimal(ai_assessment.directional_edge)
    adverse_microstructure = (
        side_sign * microstructure
    ) <= -abs(to_decimal(settings.strategy_flat_exit_microstructure_threshold))
    adverse_momentum = (
        side_sign * momentum_alpha
    ) <= -abs(to_decimal(settings.strategy_flat_exit_factor_threshold))
    adverse_trend = (
        side_sign * trend_alpha
    ) <= -abs(to_decimal(settings.strategy_flat_exit_factor_threshold))
    adverse_ai = (
        side_sign * ai_edge
    ) <= -abs(to_decimal(settings.strategy_flat_exit_ai_edge_threshold))
    return {
        "adverse_microstructure": adverse_microstructure,
        "adverse_momentum": adverse_momentum,
        "adverse_trend": adverse_trend,
        "adverse_ai": adverse_ai,
        "adverse_count": sum((adverse_microstructure, adverse_momentum, adverse_trend, adverse_ai)),
    }


def _protective_candidate_headline(*, overlay_decision: HedgeOverlayDecision) -> str:
    hedge_label = _signal_label(overlay_decision.hedge_leg_signal, default="保护腿")
    if overlay_decision.state == "opening":
        return f"Protective 家族计划建立{hedge_label}。"
    if overlay_decision.state == "holding":
        return f"Protective 家族当前维持{hedge_label}。"
    if overlay_decision.state == "closing":
        return f"Protective 家族计划退出{hedge_label}。"
    if overlay_decision.state == "blocked":
        return f"Protective 家族当前被阻断：{_reason_text(overlay_decision.blocked_reasons)}。"
    if overlay_decision.main_leg_signal == "flat":
        return "Protective 家族当前没有需要保护的主腿暴露。"
    if overlay_decision.main_leg_current_qty <= EPSILON_DECIMAL_12 and overlay_decision.hedge_leg_current_qty <= EPSILON_DECIMAL_12:
        return "Protective 家族当前没有可保护的既有库存。"
    return "Protective 家族当前未触发保护腿。"


def _signed_leg_qty(*, signal: str, quantity: Decimal) -> Decimal:
    qty = max(to_decimal(quantity), Decimal("0"))
    if signal == "long":
        return qty
    if signal == "short":
        return -qty
    return Decimal("0")


def _reason_text(reasons: list[str]) -> str:
    if not reasons:
        return "没有额外阻断原因"
    return str(reasons[0])


def _signal_label(signal: str, *, default: str) -> str:
    if signal == "long":
        return "开多保护腿"
    if signal == "short":
        return "开空保护腿"
    return default


def _sign(value: Decimal) -> Decimal:
    if value > EPSILON_DECIMAL_12:
        return Decimal("1")
    if value < -EPSILON_DECIMAL_12:
        return Decimal("-1")
    return Decimal("0")


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def _ai_directional_edge(ai_assessment: AIMarketAssessment | None) -> float:
    return 0.0 if ai_assessment is None else ai_assessment.directional_edge


def _placeholder_family_candidate(
    *,
    family: StrategyFamily,
    context: StrategyEvaluationContext,
    headline: str,
    placeholder_reason: str,
    skeleton_mode: bool = True,
) -> StrategyCandidate:
    control = context.family_runtime_controls.get(family, StrategyFamilyRuntimeControl())
    if not control.enabled:
        state = "disabled"
        reason_codes = list(dict.fromkeys([f"strategy_family_{family}_disabled", placeholder_reason]))
        control_summary = f"{family} 家族已注册但未启用。"
    else:
        state = "inactive"
        reason_codes = [placeholder_reason]
        control_summary = f"{family} 家族骨架已接入，当前仅参与 snapshot/audit。"
    return StrategyCandidate(
        family=family,
        state=state,
        enabled=control.enabled,
        selectable=False,
        execution_compatible=False,
        route_action="hold_current",
        headline=headline,
        reason_codes=reason_codes,
        control_summary=control_summary,
        metrics={
            "family_registry_enabled": True,
            "shadow_mode_enabled": control.shadow_mode_enabled,
            "live_execution_enabled": control.live_execution_enabled,
            "skeleton_mode": skeleton_mode,
        },
    )


def _overlay_route_action(
    *,
    hedge_leg: StrategyLegIntent | None,
    overlay_decision: HedgeOverlayDecision,
    control: StrategyFamilyRuntimeControl,
) -> str:
    if not control.live_execution_enabled:
        return "advisory_only"
    if hedge_leg is not None:
        return "override_target"
    if overlay_decision.active:
        return "hold_current"
    return "advisory_only"


def _protective_family_action(
    *,
    hedge_leg: StrategyLegIntent | None,
    overlay_decision: HedgeOverlayDecision,
) -> StrategyFamilyAction:
    if hedge_leg is not None:
        if str(hedge_leg.action).lower() == "close":
            return "close_protection_leg"
        current_qty = abs(to_decimal(hedge_leg.current_position_qty or Decimal("0")))
        if current_qty <= EPSILON_DECIMAL_12 and hedge_leg.action == "open":
            return "protect"
        return "rebalance_protection"
    if overlay_decision.blocked_reasons:
        return "blocked"
    return "hold_family"


def _candidate_state_from_overlay_state(state: str) -> str:
    mapping = {
        "opening": "opening",
        "holding": "active",
        "closing": "unwinding",
        "blocked": "blocked",
        "inactive": "inactive",
        "disabled": "disabled",
    }
    return mapping.get(str(state), "inactive")
