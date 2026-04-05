from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.decision import AIMarketAssessment, BaselineAssessment, DecisionContext, HedgeOverlayDecision
from aats.schemas.strategy_runtime import (
    StrategyCandidate,
    StrategyBookExpectancyEntry,
    StrategyBookExpectancySummary,
    StrategyFamily,
    StrategyFamilyAction,
    StrategyLegIntent,
)
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, clamp_float as _clamp, to_decimal
from aats.services.strategy_engines.base import StrategyEvaluationContext, StrategyFamilyRuntimeControl
from aats.services.strategy_engines.overlay_parent_exposure import (
    OverlayParentExposureContract,
    overlay_parent_exposure_audit,
    resolve_overlay_parent_exposure_from_direct_args,
)
from aats.services.strategy_engines.families.protective_family import (
    _candidate_state_from_overlay_state,
    _overlay_route_action,
    _placeholder_family_candidate,
    _resolve_overlay_parent_exposure_contract,
    _signed_leg_qty,
    protective_runtime_supported,
)
from aats.services.strategy_overlay_rollout import overlay_rollout_status
from aats.services.trade_costs import TradeCostService


@dataclass(frozen=True, slots=True)
class OpportunisticExecutionDiscipline:
    expected_signal_edge_bps: float = 0.0
    expected_slippage_bps: float = 0.0
    expected_cost_bps: float = 0.0
    expected_net_edge_bps: float = 0.0
    required_safe_net_edge_bps: float = 0.0
    max_acceptable_cost_bps: float = 0.0
    weak_edge_execution_mode: str = "block"
    weak_edge_report_only: bool = False
    passive_first_required: bool = False
    blocked_reasons: tuple[str, ...] = ()


class OpportunisticFamilyEngine:
    family_name: StrategyFamily = "opportunistic"

    def __init__(self, *, settings: AATSSettings) -> None:
        self.settings = settings
        self.trade_cost_service = TradeCostService(settings=settings)

    def evaluate(self, context: StrategyEvaluationContext) -> list[StrategyCandidate]:
        return [
            opportunistic_candidate_from_directional_target(
                settings=self.settings,
                evaluation_context=context,
                trade_cost_service=self.trade_cost_service,
            )
        ]


def opportunistic_candidate_from_directional_target(
    *,
    settings: AATSSettings,
    evaluation_context: StrategyEvaluationContext,
    trade_cost_service: TradeCostService | None = None,
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
        "weak_edge_execution_mode": settings.strategy_hedge_opportunistic_weak_edge_execution_mode,
        "passive_first_enabled": settings.strategy_hedge_opportunistic_passive_first_enabled,
        "min_safe_net_edge_bps": settings.strategy_hedge_opportunistic_min_safe_net_edge_bps,
        "expected_slippage_buffer_bps": settings.strategy_hedge_opportunistic_expected_slippage_buffer_bps,
        "expected_execution_buffer_bps": settings.strategy_hedge_opportunistic_expected_execution_buffer_bps,
        "max_acceptable_cost_bps": settings.strategy_hedge_opportunistic_max_acceptable_cost_bps,
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
        parent_exposure=parent_exposure,
    )
    execution_discipline = _resolve_opportunistic_execution_discipline(
        settings=settings,
        context=context,
        baseline=baseline,
        ai_assessment=ai_assessment,
        overlay_decision=overlay_decision,
        trade_cost_service=trade_cost_service,
        symbol=parent_exposure.symbol,
        margin_mode=parent_exposure.margin_mode,
    )
    if execution_discipline.weak_edge_report_only:
        overlay_decision = overlay_decision.model_copy(
            update={
                "reason_codes": list(
                    dict.fromkeys(
                        [
                            *overlay_decision.reason_codes,
                            "opportunistic_overlay_expected_net_edge_below_safe_threshold_report_only",
                        ]
                    )
                )
            }
        )
    if execution_discipline.blocked_reasons:
        blocked_reasons = list(dict.fromkeys([*overlay_decision.blocked_reasons, *execution_discipline.blocked_reasons]))
        hold_current = max(to_decimal(overlay_decision.hedge_leg_current_qty), Decimal("0"))
        overlay_decision = overlay_decision.model_copy(
            update={
                "active": hold_current > EPSILON_DECIMAL_12,
                "state": "blocked",
                "hedge_leg_target_qty": hold_current,
                "hedge_ratio": (
                    Decimal("0")
                    if max(to_decimal(overlay_decision.main_leg_target_qty), Decimal("0")) <= EPSILON_DECIMAL_12
                    else min(hold_current / max(to_decimal(overlay_decision.main_leg_target_qty), Decimal("0")), Decimal("1"))
                ),
                "blocked_reasons": blocked_reasons,
            }
        )
    hedge_leg = build_opportunistic_candidate_leg(
        symbol=parent_exposure.symbol,
        target_leverage=parent_exposure.target_leverage,
        margin_mode=parent_exposure.margin_mode,
        overlay_decision=overlay_decision,
        weak_edge_report_only=execution_discipline.weak_edge_report_only,
        passive_first_enabled=settings.strategy_hedge_opportunistic_passive_first_enabled,
        limit_offset_bps=max(
            Decimal("0.5"),
            to_decimal(settings.strategy_hedge_opportunistic_expected_slippage_buffer_bps),
        ),
    )
    target_qty = _signed_leg_qty(
        signal=overlay_decision.hedge_leg_signal,
        quantity=overlay_decision.hedge_leg_target_qty,
    )
    current_qty = _signed_leg_qty(
        signal=overlay_decision.hedge_leg_signal,
        quantity=overlay_decision.hedge_leg_current_qty,
    )
    book_expectancy_summary = _opportunistic_book_expectancy_summary(
        overlay_decision=overlay_decision,
        execution_discipline=execution_discipline,
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
    parent_exposure_audit = overlay_parent_exposure_audit(parent_exposure)
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
            "Opportunistic 家族已独立评估，并可直接进入 allocator / apply 主路径。"
            if control.live_execution_enabled
            else "Opportunistic 家族已独立评估；当前仅参与候选评估，不接管执行主路径。"
        ),
        execution_mode="opportunistic_overlay",
        state_phase=overlay_decision.state,
        blocking_reasons=list(overlay_decision.blocked_reasons),
        book_expectancy_summary=book_expectancy_summary,
        metrics={
            **metrics,
            "configured_mode": configured_mode,
            "overlay_parent_exposure": (
                None if parent_exposure_audit is None else parent_exposure_audit.model_dump(mode="python")
            ),
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
            "fee_drag_ratio": overlay_decision.fee_drag_ratio,
            "churn_ratio": overlay_decision.churn_ratio,
            "min_hold_remaining_seconds": overlay_decision.min_hold_remaining_seconds,
            "rebalance_cooldown_remaining_seconds": overlay_decision.rebalance_cooldown_remaining_seconds,
            "blocked_reasons": list(overlay_decision.blocked_reasons),
            "rollout_stage": overlay_decision.rollout_stage,
            "runtime_rollout_stage": overlay_decision.runtime_rollout_stage,
            "expected_signal_edge_bps": execution_discipline.expected_signal_edge_bps,
            "expected_slippage_bps": execution_discipline.expected_slippage_bps,
            "expected_cost_bps": execution_discipline.expected_cost_bps,
            "expected_net_edge_bps": execution_discipline.expected_net_edge_bps,
            "weak_edge_report_only": execution_discipline.weak_edge_report_only,
        },
        legs=[] if hedge_leg is None else [hedge_leg],
    )


def _opportunistic_book_expectancy_summary(
    *,
    overlay_decision: HedgeOverlayDecision,
    execution_discipline: OpportunisticExecutionDiscipline,
) -> StrategyBookExpectancySummary | None:
    leg = str(overlay_decision.hedge_leg_signal or "").strip().lower()
    if leg not in {"long", "short"}:
        return None
    return StrategyBookExpectancySummary(
        source="opportunistic_overlay",
        books=[
            StrategyBookExpectancyEntry(
                leg=leg,
                expected_gross_edge_bps=execution_discipline.expected_signal_edge_bps,
                expected_signal_edge_bps=execution_discipline.expected_signal_edge_bps,
                expected_slippage_bps=execution_discipline.expected_slippage_bps,
                expected_cost_bps=execution_discipline.expected_cost_bps,
                expected_net_edge_bps=execution_discipline.expected_net_edge_bps,
                required_safe_net_edge_bps=execution_discipline.required_safe_net_edge_bps,
                max_acceptable_cost_bps=execution_discipline.max_acceptable_cost_bps,
                weak_edge_execution_mode=execution_discipline.weak_edge_execution_mode,
                weak_edge_report_only=execution_discipline.weak_edge_report_only,
                passive_first_required=execution_discipline.passive_first_required,
            )
        ],
    )


def evaluate_opportunistic_overlay_decision(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
    parent_exposure: OverlayParentExposureContract | None = None,
    long_target_qty: Decimal | None = None,
    short_target_qty: Decimal | None = None,
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
        return _inactive_opportunistic_decision(
            settings=settings, context=context, rollout=rollout,
            configured_mode=configured_mode,
            main_leg_signal="flat", hedge_leg_signal="flat",
            reason_codes=["opportunistic_overlay_main_signal_flat"],
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

    if main_leg_current_qty <= EPSILON_DECIMAL_12:
        return _inactive_opportunistic_decision(
            settings=settings, context=context, rollout=rollout,
            configured_mode=configured_mode,
            main_leg_signal=main_leg_signal, hedge_leg_signal=hedge_leg_signal,
            reason_codes=["opportunistic_overlay_no_existing_inventory"],
            main_leg_current_qty=main_leg_current_qty,
            hedge_leg_current_qty=Decimal("0"),
            main_leg_target_qty=main_leg_target_qty,
            hedge_leg_target_qty=Decimal("0"),
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
    if main_signal_inferred_from_inventory:
        reason_codes.append("opportunistic_overlay_main_signal_inferred_from_inventory")

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
    weak_edge_report_only: bool = False,
    passive_first_enabled: bool = False,
    limit_offset_bps: Decimal | None = None,
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
    passive_first_required = bool(opening and weak_edge_report_only and passive_first_enabled)
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
        execution_style_preference="bounded_limit_ioc" if passive_first_required else None,
        order_type_preference="limit" if passive_first_required else None,
        time_in_force_preference="IOC" if passive_first_required else None,
        limit_offset_bps_preference=limit_offset_bps if passive_first_required else None,
        execution_preference_reason_codes=(
            ["opportunistic_weak_edge_passive_first_required"] if passive_first_required else []
        ),
    )


def opportunistic_overlay_score(
    *,
    main_leg_signal: str,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
) -> float:
    opposite_microstructure, opposite_momentum, opposite_trend, opposite_ai = _opposite_signals(
        main_leg_signal, baseline, ai_assessment,
    )
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



def _ai_directional_edge(ai_assessment: AIMarketAssessment | None) -> float:
    return 0.0 if ai_assessment is None else ai_assessment.directional_edge


def _opposite_signals(
    main_leg_signal: str,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
) -> tuple[float, float, float, float]:
    side_sign = 1.0 if main_leg_signal == "long" else -1.0
    return (
        max(0.0, -(side_sign * float(baseline.factor_scores.get("microstructure_alpha", 0.0)))),
        max(0.0, -(side_sign * float(baseline.factor_scores.get("momentum_alpha", 0.0)))),
        max(0.0, -(side_sign * float(baseline.factor_scores.get("trend_alpha", 0.0)))),
        max(0.0, -(side_sign * _ai_directional_edge(ai_assessment))),
    )


def _inactive_opportunistic_decision(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    rollout: dict,
    configured_mode: str,
    main_leg_signal: str,
    hedge_leg_signal: str,
    reason_codes: list[str],
    **extra_fields: object,
) -> HedgeOverlayDecision:
    return HedgeOverlayDecision(
        enabled=True,
        runtime_supported=True,
        configured_mode=configured_mode,
        effective_mode="opportunistic",
        overlay_source="opportunistic",
        state="inactive",
        main_leg_signal=main_leg_signal,
        hedge_leg_signal=hedge_leg_signal,
        max_ratio=to_decimal(settings.strategy_hedge_opportunistic_max_ratio),
        open_threshold=settings.strategy_hedge_opportunistic_open_threshold,
        close_threshold=settings.strategy_hedge_opportunistic_close_threshold,
        fee_drag_ratio=context.recent_fee_drag_ratio,
        churn_ratio=context.recent_churn_ratio,
        reason_codes=reason_codes,
        rollout_stage=rollout["configured_rollout_stage"],
        runtime_rollout_stage=rollout["runtime_stage"],
        **extra_fields,
    )


def _resolve_opportunistic_execution_discipline(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
    overlay_decision: HedgeOverlayDecision,
    trade_cost_service: TradeCostService | None,
    symbol: str,
    margin_mode: str,
) -> OpportunisticExecutionDiscipline:
    opening_or_expanding = (
        to_decimal(overlay_decision.hedge_leg_target_qty)
        > to_decimal(overlay_decision.hedge_leg_current_qty) + EPSILON_DECIMAL_12
    )
    if not opening_or_expanding or trade_cost_service is None:
        return OpportunisticExecutionDiscipline()
    return _compute_opportunistic_execution_discipline(
        settings=settings,
        context=context,
        baseline=baseline,
        ai_assessment=ai_assessment,
        overlay_decision=overlay_decision,
        trade_cost_service=trade_cost_service,
        symbol=symbol,
        margin_mode=margin_mode,
    )


def _compute_opportunistic_execution_discipline(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
    overlay_decision: HedgeOverlayDecision,
    trade_cost_service: TradeCostService,
    symbol: str,
    margin_mode: str,
) -> OpportunisticExecutionDiscipline:
    expected_signal_edge_bps = _opportunistic_signal_edge_bps(
        settings=settings,
        baseline=baseline,
        ai_assessment=ai_assessment,
        main_leg_signal=overlay_decision.main_leg_signal,
        opportunity_score=float(overlay_decision.pressure_score),
        open_threshold=float(overlay_decision.open_threshold),
    )
    expected_slippage_bps = _opportunistic_expected_slippage_bps(settings=settings)
    estimate = trade_cost_service.estimate_single_leg_entry(
        model_name="opportunistic_overlay",
        symbol=symbol,
        product_type=context.product_type,
        margin_mode=margin_mode,
        execution_style="taker",
        order_type="market",
        expected_slippage_bps=expected_slippage_bps,
        include_spread=False,
        include_funding=context.product_type == "derivatives",
    )
    expected_cost_bps = float(estimate.executable_total_drag_bps)
    expected_net_edge_bps = (
        expected_signal_edge_bps
        - expected_cost_bps
        - max(float(settings.strategy_edge_noise_buffer_bps), 0.0)
    )
    blocked_reasons: list[str] = []
    weak_edge_report_only = False
    required_safe_net_edge_bps = _required_opportunistic_safe_net_edge_bps(settings=settings)
    if expected_net_edge_bps < required_safe_net_edge_bps:
        if settings.strategy_hedge_opportunistic_weak_edge_execution_mode == "block":
            blocked_reasons.append("opportunistic_overlay_expected_net_edge_below_safe_threshold")
        else:
            weak_edge_report_only = True
    max_acceptable_cost_bps = float(settings.strategy_hedge_opportunistic_max_acceptable_cost_bps)
    if max_acceptable_cost_bps > 0.0 and expected_cost_bps > max_acceptable_cost_bps:
        blocked_reasons.append("opportunistic_overlay_expected_cost_above_max_acceptable")
    passive_first_required = bool(
        overlay_decision.state == "opening"
        and weak_edge_report_only
        and settings.strategy_hedge_opportunistic_passive_first_enabled
    )
    return OpportunisticExecutionDiscipline(
        expected_signal_edge_bps=expected_signal_edge_bps,
        expected_slippage_bps=expected_slippage_bps,
        expected_cost_bps=expected_cost_bps,
        expected_net_edge_bps=expected_net_edge_bps,
        required_safe_net_edge_bps=required_safe_net_edge_bps,
        max_acceptable_cost_bps=max_acceptable_cost_bps,
        weak_edge_execution_mode=settings.strategy_hedge_opportunistic_weak_edge_execution_mode,
        weak_edge_report_only=weak_edge_report_only,
        passive_first_required=passive_first_required,
        blocked_reasons=tuple(blocked_reasons),
    )


def _opportunistic_signal_edge_bps(
    *,
    settings: AATSSettings,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
    main_leg_signal: str,
    opportunity_score: float,
    open_threshold: float,
) -> float:
    score_excess_bps = max(opportunity_score - open_threshold, 0.0) * max(
        float(settings.strategy_alpha_edge_bps_scale),
        0.0,
    )
    opposite_microstructure, opposite_momentum, opposite_trend, opposite_ai = _opposite_signals(
        main_leg_signal, baseline, ai_assessment,
    )
    bonus_bps = (
        max(opposite_microstructure - 0.08, 0.0) * 22.0
        + max(opposite_momentum - 0.08, 0.0) * 12.0
        + max(opposite_trend - 0.08, 0.0) * 8.0
        + max(opposite_ai - 0.10, 0.0) * 18.0
    )
    return score_excess_bps + bonus_bps


def _opportunistic_expected_slippage_bps(*, settings: AATSSettings) -> float:
    return max(float(settings.max_slippage_tolerance_bps), 0.0) * max(
        float(settings.strategy_expected_slippage_bps_fraction),
        0.0,
    )


def _required_opportunistic_safe_net_edge_bps(*, settings: AATSSettings) -> float:
    return (
        max(float(settings.strategy_hedge_opportunistic_min_safe_net_edge_bps), 0.0)
        + max(float(settings.strategy_hedge_opportunistic_expected_slippage_buffer_bps), 0.0)
        + max(float(settings.strategy_hedge_opportunistic_expected_execution_buffer_bps), 0.0)
    )
