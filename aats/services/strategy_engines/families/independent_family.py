from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, replace as _dc_replace
from decimal import Decimal
from typing import Literal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.decision import AIMarketAssessment, BaselineAssessment, DecisionContext, HedgeOverlayDecision
from aats.schemas.market import MarketSnapshot
from aats.schemas.strategy_runtime import (
    StrategyCandidate,
    StrategyBookExpectancyEntry,
    StrategyBookExpectancySummary,
    StrategyBookRuntimeState,
    StrategyFamily,
    StrategyFamilyAction,
    StrategyLegIntent,
)
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, clamp_float as _clamp, to_decimal
from aats.services.strategy_engines.base import (
    StrategyEvaluationContext,
    StrategyFamilyRuntimeControl,
    StrategyTargetHistory,
)
from aats.services.strategy_engines.independent.diagnostics import (
    legacy_runtime_state_snapshot as _legacy_runtime_state_snapshot,
    runtime_state_from_decision as _runtime_state_from_decision,
)
from aats.services.strategy_engines.independent.engine import (
    build_independent_family_candidate as _build_independent_family_candidate,
    evaluate_independent_book as _evaluate_independent_book_v2,
)
from aats.services.strategy_engines.independent.execution_policy import (
    resolve_execution_policy as _resolve_execution_policy,
    resolve_execution_policy_from_mode as _resolve_execution_policy_from_mode,
)
from aats.services.strategy_engines.independent.gates import (
    evaluate_entry_quality_gate as _evaluate_entry_quality_gate,
    evaluate_open_eligibility as _evaluate_open_eligibility,
    low_edge_cooldown_active as _independent_low_edge_cooldown_active_v2,
    performance_degraded as _independent_performance_degraded_v2,
    post_close_cooldown_active as _independent_post_close_cooldown_active_v2,
    required_safe_net_edge_bps as _required_safe_net_edge_bps_v2,
    trial_guard_active as _independent_trial_guard_active_v2,
)
from aats.services.strategy_engines.families.independent_models import IndependentBookRuntimeState
from aats.services.strategy_engines.independent.lifecycle import (
    close_reason_code as _independent_close_reason_code_v2,
    close_reason_summary as _independent_close_reason_summary_v2,
    compute_de_risk_target_qty as _independent_de_risk_target_qty_v2,
    compute_thesis_age_seconds as _independent_thesis_age_seconds_v2,
    cooldown_until as _independent_cooldown_until_v2,
    determine_close_reason as _independent_close_reason_v2,
    last_transition_at as _independent_last_transition_at_v2,
    min_hold_remaining_seconds as _independent_min_hold_remaining_seconds_v2,
    rebalance_remaining_seconds as _independent_rebalance_remaining_seconds_v2,
)
from aats.services.strategy_engines.independent.models import (
    IndependentBookAction,
    IndependentBookDecision as IndependentBookEvaluation,
    IndependentBookExpectancy,
    IndependentBookExpectancyResolver,
    IndependentBookScorer,
    IndependentExecutionHealthState,
    IndependentExecutionPolicy,
    IndependentFamilyEvaluation,
    IndependentLeg,
    ScoreStabilityMetrics,
)
from aats.services.strategy_engines.independent.scoring import (
    compute_candidate_confidence as _candidate_confidence_v2,
    effective_score_drawdown_threshold_bps as _effective_score_drawdown_threshold_bps,
    compute_raw_book_score as _independent_book_score_v2,
    compute_score_stability as _score_stability_metrics_v2,
    compute_signal_edge_bps as _independent_signal_edge_bps_v2,
)
from aats.services.decision_engine.target_position import resolve_target_leverage
from aats.services.strategy_overlay_rollout import overlay_rollout_status
from aats.services.trade_costs import TradeCostService

# 需要独立计算 leverage（而非继承 directional parent）的 book_action 集合。
# open / scale_in 是 IndependentBookAction 中新增暴露的动作。
_OPENING_ACTIONS: frozenset[str] = frozenset({"open", "scale_in"})


class IndependentFamilyEngine:
    family_name: StrategyFamily = "independent"

    def __init__(self, *, settings: AATSSettings) -> None:
        self.settings = settings
        self.trade_cost_service = TradeCostService(settings=settings)

    def evaluate(self, context: StrategyEvaluationContext) -> list[StrategyCandidate]:
        return [
            independent_candidate_from_directional_target(
                settings=self.settings,
                evaluation_context=context,
                trade_cost_service=self.trade_cost_service,
            )
        ]


def independent_candidate_from_directional_target(
    *,
    settings: AATSSettings,
    evaluation_context: StrategyEvaluationContext,
    trade_cost_service: TradeCostService | None = None,
) -> StrategyCandidate:
    family: StrategyFamily = "independent"
    control = evaluation_context.family_runtime_controls.get(family, StrategyFamilyRuntimeControl())
    if not control.enabled:
        return _placeholder_family_candidate(
            family=family,
            context=evaluation_context,
            headline="Independent 家族已注册，但当前未启用。",
            placeholder_reason="strategy_family_independent_disabled",
            skeleton_mode=False,
        )

    context = evaluation_context.context
    baseline = evaluation_context.baseline
    directional_target = evaluation_context.directional_target
    ai_assessment = evaluation_context.ai_assessment
    runtime_supported = _independent_runtime_supported(settings=settings, context=context)
    configured_mode = settings.strategy_hedge_overlay_mode
    metrics = {
        "family_registry_enabled": True,
        "shadow_mode_enabled": control.shadow_mode_enabled,
        "live_execution_enabled": control.live_execution_enabled,
        "skeleton_mode": False,
        "execution_owner": family,
        "weak_edge_execution_mode": settings.strategy_hedge_independent_weak_edge_execution_mode,
        "passive_first_enabled": settings.strategy_hedge_independent_passive_first_enabled,
        "min_safe_net_edge_bps": settings.strategy_hedge_independent_min_safe_net_edge_bps,
        "expected_slippage_buffer_bps": settings.strategy_hedge_independent_expected_slippage_buffer_bps,
        "expected_execution_buffer_bps": settings.strategy_hedge_independent_expected_execution_buffer_bps,
        "max_acceptable_cost_bps": settings.strategy_hedge_independent_max_acceptable_cost_bps,
        "min_confirm_ticks": settings.strategy_hedge_independent_min_confirm_ticks,
        "min_score_stability_bps": settings.strategy_hedge_independent_min_score_stability_bps,
        "min_score_drawdown_bps": settings.strategy_hedge_independent_min_score_drawdown_bps,
        "effective_score_drawdown_threshold_bps": _effective_score_drawdown_threshold_bps(settings=settings),
        "min_liquidity_quality": settings.strategy_hedge_independent_min_liquidity_quality,
        "require_execution_health_ok": settings.strategy_hedge_independent_require_execution_health_ok,
        "max_thesis_age_seconds": settings.strategy_hedge_independent_max_thesis_age_seconds,
        "de_risk_net_edge_bps": settings.strategy_hedge_independent_de_risk_net_edge_bps,
        "failed_thesis_net_edge_bps": settings.strategy_hedge_independent_failed_thesis_net_edge_bps,
        "catastrophic_failed_thesis_buffer_bps": settings.strategy_hedge_independent_catastrophic_failed_thesis_buffer_bps,
        "execution_health_de_risk_enabled": settings.strategy_hedge_independent_execution_health_de_risk_enabled,
        "liquidity_de_risk_enabled": settings.strategy_hedge_independent_liquidity_de_risk_enabled,
        "entry_execution_mode": settings.strategy_hedge_independent_entry_execution_mode,
        "scale_in_execution_mode": settings.strategy_hedge_independent_scale_in_execution_mode,
        "de_risk_execution_mode": settings.strategy_hedge_independent_de_risk_execution_mode,
        "close_failed_thesis_execution_mode": settings.strategy_hedge_independent_close_failed_thesis_execution_mode,
        "close_stale_execution_mode": settings.strategy_hedge_independent_close_stale_execution_mode,
        "limit_offset_bps_entry": settings.strategy_hedge_independent_limit_offset_bps_entry,
        "limit_offset_bps_scale_in": settings.strategy_hedge_independent_limit_offset_bps_scale_in,
        "limit_offset_bps_stale_close": settings.strategy_hedge_independent_limit_offset_bps_stale_close,
        "emit_book_level_metrics": settings.strategy_hedge_independent_emit_book_level_metrics,
        "emit_expected_vs_realized_metrics": settings.strategy_hedge_independent_emit_expected_vs_realized_metrics,
        "emit_close_reason_metrics": settings.strategy_hedge_independent_emit_close_reason_metrics,
        "emit_execution_policy_metrics": settings.strategy_hedge_independent_emit_execution_policy_metrics,
        "long_entry_threshold": settings.strategy_hedge_independent_long_entry_threshold,
        "short_entry_threshold": settings.strategy_hedge_independent_short_entry_threshold,
        "long_close_threshold": settings.strategy_hedge_independent_long_close_threshold,
        "short_close_threshold": settings.strategy_hedge_independent_short_close_threshold,
        "long_scale_in_threshold": settings.strategy_hedge_independent_long_scale_in_threshold,
        "short_scale_in_threshold": settings.strategy_hedge_independent_short_scale_in_threshold,
    }
    if not settings.strategy_hedge_overlay_enabled:
        return StrategyCandidate(
            family=family,
            state="disabled",
            enabled=True,
            selectable=False,
            execution_compatible=False,
            route_action="advisory_only",
            headline="Independent 家族已关闭：总 hedge overlay 开关未开启。",
            reason_codes=["strategy_hedge_overlay_disabled"],
            control_summary="Independent 家族已接入，但当前由总 overlay 开关关闭。",
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
            headline="Independent 家族当前运行域不支持。",
            reason_codes=["hedge_overlay_runtime_not_supported"],
            blocking_reasons=["hedge_overlay_runtime_not_supported"],
            control_summary="Independent 家族仅支持 derivatives + hedge 运行域。",
            metrics=metrics,
        )
    if configured_mode != "independent":
        return StrategyCandidate(
            family=family,
            state="inactive",
            enabled=True,
            selectable=False,
            execution_compatible=False,
            route_action="advisory_only",
            headline="Independent 家族当前不是激活模式。",
            reason_codes=["strategy_family_independent_waiting_for_activation"],
            blocking_reasons=["strategy_hedge_overlay_mode_not_independent"],
            control_summary="Independent 家族已评估，但当前主模式不是 independent。",
            execution_mode="independent_books",
            metrics={**metrics, "configured_mode": configured_mode},
        )

    result = evaluate_independent_books(
        settings=settings,
        context=context,
        baseline=baseline,
        ai_assessment=ai_assessment,
        runtime_margin_mode=str(directional_target.margin_mode or settings.margin_mode),
        directional_target_qty=to_decimal(directional_target.target_position_qty),
        target_leverage=float(directional_target.target_leverage),
        leverage_bias=float(directional_target.leverage_bias),
        signal_edge_bps=float(directional_target.expected_signal_edge_bps),
        expected_cost_bps=float(directional_target.expected_cost_bps),
        expected_net_edge_bps=float(directional_target.expected_net_edge_bps),
        execution_leg_family="independent",
        trade_cost_service=trade_cost_service,
        latest_market_snapshot=evaluation_context.latest_market_snapshot,
        recent_score_history_by_leg=_independent_recent_score_history_by_leg(
            recent_targets_by_family=evaluation_context.recent_targets_by_family,
            max_points=max(int(settings.strategy_hedge_independent_min_confirm_ticks), 3),
        ),
        prior_runtime_states_by_leg=_independent_prior_runtime_states_by_leg(
            recent_targets_by_family=evaluation_context.recent_targets_by_family,
        ),
    )
    overlay_decision = result.overlay_decision
    book_expectancy_summary = _independent_book_expectancy_summary(result=result, settings=settings)
    book_runtime_states = _independent_book_runtime_state_summary(
        context=context,
        result=result,
    )
    reason_codes = list(
        dict.fromkeys(
            [
                *overlay_decision.reason_codes,
                *(
                    ["independent_family_candidate_active"]
                    if overlay_decision.active
                    else ["independent_family_candidate_inactive"]
                ),
            ]
        )
    )
    return StrategyCandidate(
        family=family,
        state=_candidate_state_from_overlay_state(overlay_decision.state),
        enabled=True,
        selectable=bool(
            control.live_execution_enabled
            and (
                bool(result.legs)
                or _independent_active_books_present(result)
            )
        ),
        execution_compatible=bool(result.legs or overlay_decision.active),
        route_action=_independent_route_action(result=result, control=control),
        family_action=_independent_family_action(result=result),
        headline=_independent_candidate_headline(overlay_decision=overlay_decision),
        recommended_symbol=directional_target.symbol,
        target_position_qty=result.final_target_qty,
        delta_position_qty=result.final_target_qty - to_decimal(context.current_position_qty),
        score=max(float(overlay_decision.long_leg_score), float(overlay_decision.short_leg_score)),
        confidence=_candidate_confidence(
            max(float(overlay_decision.long_leg_score), float(overlay_decision.short_leg_score))
        ),
        urgency=_candidate_urgency(overlay_decision=overlay_decision),
        reason_codes=reason_codes,
        control_summary=(
            "Independent 家族已独立评估，并可直接进入 allocator / apply 主路径。"
            if control.live_execution_enabled
            else "Independent 家族已独立评估；当前仅参与候选评估，不接管执行主路径。"
        ),
        execution_mode="independent_books",
        state_phase=overlay_decision.state,
        blocking_reasons=list(overlay_decision.blocked_reasons),
        book_expectancy_summary=book_expectancy_summary,
        book_runtime_states=book_runtime_states,
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
            "long_leg_score": overlay_decision.long_leg_score,
            "short_leg_score": overlay_decision.short_leg_score,
            "long_leg_reason_codes": list(overlay_decision.long_leg_reason_codes),
            "short_leg_reason_codes": list(overlay_decision.short_leg_reason_codes),
            "long_leg_blocked_reasons": list(overlay_decision.long_leg_blocked_reasons),
            "short_leg_blocked_reasons": list(overlay_decision.short_leg_blocked_reasons),
            "blocked_reasons": list(overlay_decision.blocked_reasons),
            "min_hold_remaining_seconds": overlay_decision.min_hold_remaining_seconds,
            "rebalance_cooldown_remaining_seconds": overlay_decision.rebalance_cooldown_remaining_seconds,
            "rollout_stage": overlay_decision.rollout_stage,
            "runtime_rollout_stage": overlay_decision.runtime_rollout_stage,
            "expectancy_source": "independent_book",
            "long_expected_signal_edge_bps": _expectancy_signal_edge_bps(result.long_book.expectancy),
            "long_expected_slippage_bps": _expectancy_slippage_bps(
                result.long_book.expectancy,
                settings=settings,
            ),
            "long_expected_cost_bps": _expectancy_cost_bps(result.long_book.expectancy),
            "long_expected_net_edge_bps": _expectancy_net_edge_bps(result.long_book.expectancy),
            "long_liquidity_quality_score": result.long_book.liquidity_quality_score,
            "long_score_support_count": (
                None
                if result.long_book.score_stability_metrics is None
                else result.long_book.score_stability_metrics.support_count
            ),
            "long_score_stable": (
                None
                if result.long_book.score_stability_metrics is None
                else result.long_book.score_stability_metrics.stable
            ),
            "long_score_stability_upward_excursion_bps": (
                None
                if result.long_book.score_stability_metrics is None
                else result.long_book.score_stability_metrics.upward_excursion_bps
            ),
            "long_score_stability_downward_drawdown_bps": (
                None
                if result.long_book.score_stability_metrics is None
                else result.long_book.score_stability_metrics.downward_drawdown_bps
            ),
            "long_score_stability_semantics_version": (
                None
                if result.long_book.score_stability_metrics is None
                else result.long_book.score_stability_metrics.semantics_version
            ),
            "long_score_stability_source": (
                None
                if result.long_book.score_stability_metrics is None
                else result.long_book.score_stability_metrics.source
            ),
            "long_execution_health_state": result.long_book.execution_health_state,
            "long_health_state": result.long_book.health_state,
            "long_book_state": result.long_book.book_state,
            "long_guard_state": result.long_book.guard_state,
            "long_holding_phase": result.long_book.holding_phase,
            "long_book_action": result.long_book.book_action,
            "long_close_reason": result.long_book.close_reason,
            "long_thesis_age_seconds": result.long_book.thesis_age_seconds,
            "long_execution_policy_reason": (
                None
                if result.long_book.execution_policy is None
                else result.long_book.execution_policy.policy_reason
            ),
            "long_execution_policy_urgency": (
                None
                if result.long_book.execution_policy is None
                else result.long_book.execution_policy.urgency
            ),
            "long_execution_style_preference": (
                None
                if result.long_book.execution_policy is None
                else result.long_book.execution_policy.execution_style_preference
            ),
            "long_order_type_preference": (
                None
                if result.long_book.execution_policy is None
                else result.long_book.execution_policy.order_type_preference
            ),
            "long_time_in_force_preference": (
                None
                if result.long_book.execution_policy is None
                else result.long_book.execution_policy.time_in_force_preference
            ),
            "long_limit_offset_bps_preference": (
                None
                if result.long_book.execution_policy is None
                else result.long_book.execution_policy.limit_offset_bps_preference
            ),
            "long_threshold_snapshot": (
                None
                if result.long_book.threshold_snapshot is None
                else asdict(result.long_book.threshold_snapshot)
            ),
            "long_health_snapshot": (
                None
                if result.long_book.health_snapshot is None
                else asdict(result.long_book.health_snapshot)
            ),
            "long_replay_snapshot": (
                None
                if result.long_book.replay_snapshot is None
                else asdict(result.long_book.replay_snapshot)
            ),
            "short_expected_signal_edge_bps": _expectancy_signal_edge_bps(result.short_book.expectancy),
            "short_expected_slippage_bps": _expectancy_slippage_bps(
                result.short_book.expectancy,
                settings=settings,
            ),
            "short_expected_cost_bps": _expectancy_cost_bps(result.short_book.expectancy),
            "short_expected_net_edge_bps": _expectancy_net_edge_bps(result.short_book.expectancy),
            "short_liquidity_quality_score": result.short_book.liquidity_quality_score,
            "short_score_support_count": (
                None
                if result.short_book.score_stability_metrics is None
                else result.short_book.score_stability_metrics.support_count
            ),
            "short_score_stable": (
                None
                if result.short_book.score_stability_metrics is None
                else result.short_book.score_stability_metrics.stable
            ),
            "short_score_stability_upward_excursion_bps": (
                None
                if result.short_book.score_stability_metrics is None
                else result.short_book.score_stability_metrics.upward_excursion_bps
            ),
            "short_score_stability_downward_drawdown_bps": (
                None
                if result.short_book.score_stability_metrics is None
                else result.short_book.score_stability_metrics.downward_drawdown_bps
            ),
            "short_score_stability_semantics_version": (
                None
                if result.short_book.score_stability_metrics is None
                else result.short_book.score_stability_metrics.semantics_version
            ),
            "short_score_stability_source": (
                None
                if result.short_book.score_stability_metrics is None
                else result.short_book.score_stability_metrics.source
            ),
            "short_execution_health_state": result.short_book.execution_health_state,
            "short_health_state": result.short_book.health_state,
            "short_book_state": result.short_book.book_state,
            "short_guard_state": result.short_book.guard_state,
            "short_holding_phase": result.short_book.holding_phase,
            "short_book_action": result.short_book.book_action,
            "short_close_reason": result.short_book.close_reason,
            "short_thesis_age_seconds": result.short_book.thesis_age_seconds,
            "short_execution_policy_reason": (
                None
                if result.short_book.execution_policy is None
                else result.short_book.execution_policy.policy_reason
            ),
            "short_execution_policy_urgency": (
                None
                if result.short_book.execution_policy is None
                else result.short_book.execution_policy.urgency
            ),
            "short_execution_style_preference": (
                None
                if result.short_book.execution_policy is None
                else result.short_book.execution_policy.execution_style_preference
            ),
            "short_order_type_preference": (
                None
                if result.short_book.execution_policy is None
                else result.short_book.execution_policy.order_type_preference
            ),
            "short_time_in_force_preference": (
                None
                if result.short_book.execution_policy is None
                else result.short_book.execution_policy.time_in_force_preference
            ),
            "short_limit_offset_bps_preference": (
                None
                if result.short_book.execution_policy is None
                else result.short_book.execution_policy.limit_offset_bps_preference
            ),
            "short_threshold_snapshot": (
                None
                if result.short_book.threshold_snapshot is None
                else asdict(result.short_book.threshold_snapshot)
            ),
            "short_health_snapshot": (
                None
                if result.short_book.health_snapshot is None
                else asdict(result.short_book.health_snapshot)
            ),
            "short_replay_snapshot": (
                None
                if result.short_book.replay_snapshot is None
                else asdict(result.short_book.replay_snapshot)
            ),
            "family_health_overall_state": (
                None if result.family_health is None else result.family_health.overall_state
            ),
            "family_health_blockers": (
                [] if result.family_health is None else list(result.family_health.family_blockers)
            ),
            "close_reason": _independent_close_reason_summary(
                long_book=result.long_book,
                short_book=result.short_book,
            ),
            "expected_signal_edge_bps": max(
                _expectancy_signal_edge_bps(result.long_book.expectancy),
                _expectancy_signal_edge_bps(result.short_book.expectancy),
            ),
            "expected_cost_bps": max(
                _expectancy_cost_bps(result.long_book.expectancy),
                _expectancy_cost_bps(result.short_book.expectancy),
            ),
            "expected_net_edge_bps": max(
                _expectancy_net_edge_bps(result.long_book.expectancy),
                _expectancy_net_edge_bps(result.short_book.expectancy),
            ),
            "book_runtime_states": [
                state.model_dump(mode="json")
                for state in book_runtime_states
            ],
        },
        legs=list(result.legs),
    )


def _independent_runtime_supported(*, settings: AATSSettings, context: DecisionContext) -> bool:
    return (
        context.product_type == "derivatives"
        and settings.margin_mode != "cash"
        and settings.derivatives_position_mode == "hedge"
    )


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


def _independent_active_books_present(result: IndependentFamilyEvaluation) -> bool:
    return any(
        book.current_qty > EPSILON_DECIMAL_12 or book.target_qty > EPSILON_DECIMAL_12
        for book in (result.long_book, result.short_book)
    )


def _independent_recent_score_history_by_leg(
    *,
    recent_targets_by_family: dict[str, list[StrategyTargetHistory]],
    max_points: int,
) -> dict[IndependentLeg, tuple[float, ...]]:
    rows = recent_targets_by_family.get("independent", [])
    long_scores: list[float] = []
    short_scores: list[float] = []
    for row in rows:
        overlay = getattr(row.target, "hedge_overlay_decision", None)
        if overlay is None:
            continue
        long_scores.append(float(getattr(overlay, "long_leg_score", 0.0) or 0.0))
        short_scores.append(float(getattr(overlay, "short_leg_score", 0.0) or 0.0))
    limit = max(int(max_points), 1)
    return {
        "long": tuple(long_scores[-limit:]),
        "short": tuple(short_scores[-limit:]),
    }


def _independent_prior_runtime_states_by_leg(
    *,
    recent_targets_by_family: dict[str, list[StrategyTargetHistory]],
) -> dict[IndependentLeg, StrategyBookRuntimeState]:
    rows = recent_targets_by_family.get("independent", [])
    for row in reversed(rows):
        raw_states = list(getattr(row.target, "book_runtime_states", []) or [])
        parsed: dict[IndependentLeg, StrategyBookRuntimeState] = {}
        for item in raw_states:
            try:
                runtime_state = (
                    item
                    if isinstance(item, StrategyBookRuntimeState)
                    else StrategyBookRuntimeState.model_validate(item)
                )
            except Exception:
                continue
            if runtime_state.leg in {"long", "short"}:
                parsed[runtime_state.leg] = runtime_state
        if parsed:
            return parsed
    return {}


def _independent_book_expectancy_summary(
    *,
    result: IndependentFamilyEvaluation,
    settings: AATSSettings,
) -> StrategyBookExpectancySummary:
    required_safe_net_edge_bps = _required_safe_net_edge_bps(settings=settings)
    max_acceptable_cost_bps = float(settings.strategy_hedge_independent_max_acceptable_cost_bps)
    return StrategyBookExpectancySummary(
        source="independent_book",
        books=[
            StrategyBookExpectancyEntry(
                leg=book.leg,
                expected_gross_edge_bps=_expectancy_signal_edge_bps(book.expectancy),
                expected_signal_edge_bps=_expectancy_signal_edge_bps(book.expectancy),
                expected_slippage_bps=_expectancy_slippage_bps(book.expectancy, settings=settings),
                expected_cost_bps=_expectancy_cost_bps(book.expectancy),
                expected_net_edge_bps=_expectancy_net_edge_bps(book.expectancy),
                required_safe_net_edge_bps=required_safe_net_edge_bps,
                max_acceptable_cost_bps=max_acceptable_cost_bps,
                weak_edge_execution_mode=settings.strategy_hedge_independent_weak_edge_execution_mode,
                weak_edge_report_only=book.weak_edge_report_only,
                passive_first_required=bool(
                    book.execution_policy is not None
                    and book.execution_policy.order_type_preference == "limit"
                ),
                book_action=book.book_action,
                close_reason=book.close_reason,
                policy_reason=None if book.execution_policy is None else book.execution_policy.policy_reason,
                execution_policy_urgency=(
                    None if book.execution_policy is None else book.execution_policy.urgency
                ),
                execution_style_preference=(
                    None if book.execution_policy is None else book.execution_policy.execution_style_preference
                ),
                order_type_preference=(
                    None if book.execution_policy is None else book.execution_policy.order_type_preference
                ),
                time_in_force_preference=(
                    None if book.execution_policy is None else book.execution_policy.time_in_force_preference
                ),
                limit_offset_bps_preference=(
                    None if book.execution_policy is None else book.execution_policy.limit_offset_bps_preference
                ),
                expected_leg_cost_bps=_expectancy_cost_bps(book.expectancy),
                liquidity_quality_score=book.liquidity_quality_score,
                execution_health_state=book.execution_health_state,
                score_raw=book.score_raw,
                score_adjusted=book.score_adjusted,
                size_multiplier=(
                    None if book.sizing is None else float(book.sizing.size_multiplier)
                ),
                capital_multiplier=(
                    None if book.sizing is None else float(book.sizing.capital_multiplier)
                ),
                health_state=book.health_state,
                book_state=book.book_state,
                guard_state=book.guard_state,
                holding_phase=book.holding_phase,
                edge_strength=(
                    None if book.execution_policy is None else book.execution_policy.edge_strength
                ),
            )
            for book in (result.long_book, result.short_book)
        ],
    )


def _independent_book_runtime_state_summary(
    *,
    context: DecisionContext,
    result: IndependentFamilyEvaluation,
) -> list[StrategyBookRuntimeState]:
    return [
        _runtime_state_from_decision(
            context=context,
            decision=book,
            threshold_snapshot=book.threshold_snapshot,
            health_snapshot=book.health_snapshot,
        )
        for book in (result.long_book, result.short_book)
    ]


def _expectancy_signal_edge_bps(expectancy: IndependentBookExpectancy | None) -> float:
    return 0.0 if expectancy is None else expectancy.expected_signal_edge_bps


def _expectancy_slippage_bps(
    expectancy: IndependentBookExpectancy | None,
    *,
    settings: AATSSettings,
) -> float:
    return _independent_expected_slippage_bps(settings=settings) if expectancy is None else expectancy.expected_slippage_bps


def _expectancy_cost_bps(expectancy: IndependentBookExpectancy | None) -> float:
    return 0.0 if expectancy is None else expectancy.expected_cost_bps


def _expectancy_net_edge_bps(expectancy: IndependentBookExpectancy | None) -> float:
    return 0.0 if expectancy is None else expectancy.expected_net_edge_bps


def _expectancy_lifecycle_net_edge_bps(expectancy: IndependentBookExpectancy | None) -> float:
    if expectancy is None:
        return 0.0
    if expectancy.expected_lifecycle_net_edge_bps is not None:
        return expectancy.expected_lifecycle_net_edge_bps
    return expectancy.expected_net_edge_bps


def _independent_route_action(
    *,
    result: IndependentFamilyEvaluation,
    control: StrategyFamilyRuntimeControl,
) -> Literal["override_target", "hold_current", "advisory_only"]:
    if not control.live_execution_enabled:
        return "advisory_only"
    if result.legs:
        return "override_target"
    if _independent_active_books_present(result):
        return "hold_current"
    return "advisory_only"


def _independent_family_action(*, result: IndependentFamilyEvaluation) -> StrategyFamilyAction:
    books = (result.long_book, result.short_book)
    opening_actions = {"open", "scale_in"}
    closing_actions = {"de_risk", "close_failed_thesis", "close_stale_thesis"}
    opening_books = [book for book in books if book.book_action in opening_actions]
    closing_books = [book for book in books if book.book_action in closing_actions]
    if opening_books and closing_books:
        return "rebalance_independent_books"
    if any(book.book_action == "scale_in" for book in books):
        return "scale_independent_book"
    if any(book.book_action == "open" for book in books):
        return "open_independent_book"
    if any(book.book_action == "de_risk" for book in books):
        return "de_risk_independent_book"
    if any(book.book_action == "close_failed_thesis" for book in books):
        return "close_failed_thesis_independent_book"
    if any(book.book_action == "close_stale_thesis" for book in books):
        return "close_stale_thesis_independent_book"
    if closing_books:
        return "close_independent_book"
    if any(book.book_action == "blocked" or book.state == "blocked" for book in books):
        return "blocked"
    return "hold_family"


def evaluate_independent_books(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
    runtime_margin_mode: str | None = None,
    directional_target_qty: Decimal,
    target_leverage: float,
    leverage_bias: float = 1.0,
    signal_edge_bps: float,
    expected_cost_bps: float,
    expected_net_edge_bps: float,
    execution_leg_family: StrategyFamily,
    scorer: IndependentBookScorer | None = None,
    trade_cost_service: TradeCostService | None = None,
    expectancy_resolver: IndependentBookExpectancyResolver | None = None,
    latest_market_snapshot: MarketSnapshot | None = None,
    recent_score_history_by_leg: dict[IndependentLeg, tuple[float, ...]] | None = None,
    prior_runtime_states_by_leg: dict[IndependentLeg, StrategyBookRuntimeState] | None = None,
) -> IndependentFamilyEvaluation:
    resolved_margin_mode = str(runtime_margin_mode or settings.margin_mode)
    configured_mode = settings.strategy_hedge_overlay_mode
    if not settings.strategy_hedge_independent_enabled:
        overlay_decision = HedgeOverlayDecision(
            enabled=True,
            runtime_supported=True,
            configured_mode=configured_mode,
            state="blocked",
            effective_mode="independent",
            overlay_source="independent_books",
            blocked_reasons=["independent_books_not_enabled"],
        )
        empty = _inactive_book("long")
        return IndependentFamilyEvaluation(
            final_target_qty=to_decimal(directional_target_qty),
            legs=[],
            overlay_decision=overlay_decision,
            long_book=empty,
            short_book=_inactive_book("short"),
            book_runtime_states=(),
        )
    rollout = overlay_rollout_status(settings, mode="independent")
    if not rollout["runtime_allowed"]:
        overlay_decision = HedgeOverlayDecision(
            enabled=True,
            runtime_supported=True,
            configured_mode=configured_mode,
            effective_mode="independent",
            overlay_source="independent_books",
            state="blocked",
            blocked_reasons=list(rollout["blocking_reasons"]),
            reason_codes=["independent_books_rollout_gate_active"],
            rollout_stage=rollout["configured_rollout_stage"],
            runtime_rollout_stage=rollout["runtime_stage"],
        )
        empty = _inactive_book("long")
        return IndependentFamilyEvaluation(
            final_target_qty=to_decimal(directional_target_qty),
            legs=[],
            overlay_decision=overlay_decision,
            long_book=empty,
            short_book=_inactive_book("short"),
            book_runtime_states=(),
        )

    directional_long_target_qty = max(to_decimal(directional_target_qty), Decimal("0"))
    directional_short_target_qty = max(-to_decimal(directional_target_qty), Decimal("0"))
    cost_service = trade_cost_service or TradeCostService(settings=settings)
    long_current_qty = to_decimal(context.current_long_position_qty)
    short_current_qty = to_decimal(context.current_short_position_qty)

    # ── Phase 1: provisional expectancy with ENTRY side ──────────
    # The book state machine may later decide to close/de-risk for
    # reasons unknown at this point (stale thesis, failed thesis,
    # weak edge, etc.).  Using the entry side here gives a
    # conservative cost estimate; the actual execution side is
    # corrected in Phase 3 after the action is known.
    long_expectancy = _resolve_independent_book_expectancy(
        settings=settings,
        context=context,
        baseline=baseline,
        ai_assessment=ai_assessment,
        runtime_margin_mode=resolved_margin_mode,
        leg="long",
        trade_cost_service=cost_service,
        expectancy_resolver=expectancy_resolver,
        latest_market_snapshot=latest_market_snapshot,
        planned_delta_qty=_planned_leg_delta_qty(
            current_qty=long_current_qty,
            target_qty=directional_long_target_qty,
        ),
        projected_notional=_planned_leg_notional(
            current_qty=long_current_qty,
            current_notional=to_decimal(context.current_long_position_notional),
            target_qty=directional_long_target_qty,
            latest_market_snapshot=latest_market_snapshot,
        ),
        execution_side="buy",
    )
    short_expectancy = _resolve_independent_book_expectancy(
        settings=settings,
        context=context,
        baseline=baseline,
        ai_assessment=ai_assessment,
        runtime_margin_mode=resolved_margin_mode,
        leg="short",
        trade_cost_service=cost_service,
        expectancy_resolver=expectancy_resolver,
        latest_market_snapshot=latest_market_snapshot,
        planned_delta_qty=_planned_leg_delta_qty(
            current_qty=short_current_qty,
            target_qty=directional_short_target_qty,
        ),
        projected_notional=_planned_leg_notional(
            current_qty=short_current_qty,
            current_notional=to_decimal(context.current_short_position_notional),
            target_qty=directional_short_target_qty,
            latest_market_snapshot=latest_market_snapshot,
        ),
        execution_side="sell",
    )

    # ── Phase 2: book state-machine evaluation ───────────────────
    long_book = _evaluate_independent_book(
        settings=settings,
        context=context,
        baseline=baseline,
        ai_assessment=ai_assessment,
        leg="long",
        expectancy=long_expectancy,
        directional_leg_target_qty=directional_long_target_qty,
        leverage_bias=leverage_bias,
        scorer=scorer,
        prior_runtime_state=(
            None
            if prior_runtime_states_by_leg is None
            else prior_runtime_states_by_leg.get("long")
        ),
        recent_score_history=(
            ()
            if recent_score_history_by_leg is None
            else recent_score_history_by_leg.get("long", ())
        ),
    )
    short_book = _evaluate_independent_book(
        settings=settings,
        context=context,
        baseline=baseline,
        ai_assessment=ai_assessment,
        leg="short",
        expectancy=short_expectancy,
        directional_leg_target_qty=directional_short_target_qty,
        leverage_bias=leverage_bias,
        scorer=scorer,
        prior_runtime_state=(
            None
            if prior_runtime_states_by_leg is None
            else prior_runtime_states_by_leg.get("short")
        ),
        recent_score_history=(
            ()
            if recent_score_history_by_leg is None
            else recent_score_history_by_leg.get("short", ())
        ),
    )

    # ── Phase 3: recompute expectancy for exit/reduce actions ────
    # When the book state machine decided to close or de-risk, the
    # actual execution trades the EXIT side (sell to close long, buy
    # to close short).  Recompute cost estimates with the correct
    # side so that downstream execution planning and net-edge
    # reporting reflect reality.
    _EXIT_BOOK_ACTIONS = {"de_risk", "close_failed_thesis", "close_stale_thesis"}
    # P2 fix: book_action → execution_mode 映射。退出/降风险使用各自的
    # execution_mode 估算成本，而非 entry mode。例如 derivatives_live 中
    # entry=passive_first 但 close_failed=aggressive_bounded_taker，
    # 用 entry mode 会低估退出成本约 3-5 bps。
    _EXIT_ACTION_MODE_FIELDS: dict[str, str] = {
        "de_risk": "strategy_hedge_independent_de_risk_execution_mode",
        "close_failed_thesis": "strategy_hedge_independent_close_failed_thesis_execution_mode",
        "close_stale_thesis": "strategy_hedge_independent_close_stale_execution_mode",
    }
    for leg_label, book, current_qty, exit_side in (
        ("long", long_book, long_current_qty, "sell"),
        ("short", short_book, short_current_qty, "buy"),
    ):
        if book.book_action not in _EXIT_BOOK_ACTIONS:
            continue
        if current_qty <= EPSILON_DECIMAL_12:
            continue
        exit_delta_qty = _planned_leg_delta_qty(
            current_qty=current_qty, target_qty=book.target_qty,
        )
        if exit_delta_qty <= EPSILON_DECIMAL_12:
            continue
        exit_notional = _planned_leg_notional(
            current_qty=current_qty,
            current_notional=to_decimal(
                context.current_long_position_notional
                if leg_label == "long"
                else context.current_short_position_notional
            ),
            target_qty=book.target_qty,
            latest_market_snapshot=latest_market_snapshot,
        )
        # 根据退出动作获取对应的 execution_mode settings 字段
        _exit_mode_field = _EXIT_ACTION_MODE_FIELDS.get(book.book_action)
        _exit_mode = str(getattr(settings, _exit_mode_field)) if _exit_mode_field else None
        exit_expectancy = _resolve_independent_book_expectancy(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            runtime_margin_mode=resolved_margin_mode,
            leg=leg_label,
            trade_cost_service=cost_service,
            expectancy_resolver=expectancy_resolver,
            latest_market_snapshot=latest_market_snapshot,
            planned_delta_qty=exit_delta_qty,
            projected_notional=exit_notional,
            execution_side=exit_side,
            execution_mode_override=_exit_mode,
        )
        if exit_expectancy is not None:
            updated = _dc_replace(book, expectancy=exit_expectancy)
            # P1-1 fix: 退出腿重算 expectancy 后必须同步重算 execution_policy。
            # 否则 downstream build_independent_leg() 会拿到 entry-side 的旧 policy
            # （order_type / limit_offset / urgency 与实际退出成本不匹配）。
            exit_policy = _independent_execution_policy(
                settings=settings, book=updated,
            )
            if exit_policy is not None:
                updated = _dc_replace(
                    updated,
                    execution_policy=exit_policy,
                    # P2 fix: book.policy_reason 必须与 execution_policy.policy_reason
                    # 保持一致——diagnostics / replay / runtime_state 直接读
                    # decision.policy_reason，硬编码会导致审计展示与真实下单策略不符。
                    policy_reason=exit_policy.policy_reason,
                )
            if leg_label == "long":
                long_book = updated
            else:
                short_book = updated

    # ── Phase 3b: recompute expectancy for scale_in actions ─────
    # scale_in uses the same execution side as entry (buy for long,
    # sell for short) but a different execution_mode.  Phase 1 used
    # entry mode; if scale_in mode differs we need to recompute so
    # that cost estimates match the actual execution strategy.
    for leg_label, book, current_qty, scale_side in (
        ("long", long_book, long_current_qty, "buy"),
        ("short", short_book, short_current_qty, "sell"),
    ):
        if book.book_action != "scale_in":
            continue
        if current_qty <= EPSILON_DECIMAL_12:
            continue
        scale_in_mode = str(settings.strategy_hedge_independent_scale_in_execution_mode)
        entry_mode = str(settings.strategy_hedge_independent_entry_execution_mode)
        if scale_in_mode == entry_mode:
            # mode 相同则 Phase 1 的 entry-side 估算已正确，跳过
            continue
        scale_delta_qty = _planned_leg_delta_qty(
            current_qty=current_qty, target_qty=book.target_qty,
        )
        if scale_delta_qty <= EPSILON_DECIMAL_12:
            continue
        scale_notional = _planned_leg_notional(
            current_qty=current_qty,
            current_notional=to_decimal(
                context.current_long_position_notional
                if leg_label == "long"
                else context.current_short_position_notional
            ),
            target_qty=book.target_qty,
            latest_market_snapshot=latest_market_snapshot,
        )
        scale_expectancy = _resolve_independent_book_expectancy(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            runtime_margin_mode=resolved_margin_mode,
            leg=leg_label,
            trade_cost_service=cost_service,
            expectancy_resolver=expectancy_resolver,
            latest_market_snapshot=latest_market_snapshot,
            planned_delta_qty=scale_delta_qty,
            projected_notional=scale_notional,
            execution_side=scale_side,
            execution_mode_override=scale_in_mode,
        )
        if scale_expectancy is not None:
            updated = _dc_replace(book, expectancy=scale_expectancy)
            scale_policy = _independent_execution_policy(
                settings=settings, book=updated,
            )
            if scale_policy is not None:
                updated = _dc_replace(
                    updated,
                    execution_policy=scale_policy,
                    policy_reason=scale_policy.policy_reason,
                )
            if leg_label == "long":
                long_book = updated
            else:
                short_book = updated

    long_target_qty = long_book.target_qty
    short_target_qty = short_book.target_qty
    final_target_qty = long_target_qty - short_target_qty
    active = any(
        book.target_qty > EPSILON_DECIMAL_12 or book.current_qty > EPSILON_DECIMAL_12
        for book in (long_book, short_book)
    )
    blocked_reasons = list(
        dict.fromkeys(
            [
                *long_book.blocked_reasons,
                *short_book.blocked_reasons,
            ]
        )
    )
    state = _independent_overlay_state(
        long_book=long_book,
        short_book=short_book,
        blocked_reasons=blocked_reasons,
        active=active,
    )
    reason_codes = list(
        dict.fromkeys(
            [
                *long_book.reason_codes,
                *short_book.reason_codes,
            ]
        )
    )
    # ── Leverage resolution ────────────────────────────────────────
    # independent 家族不能直接透传 directional parent 的 target_leverage：
    # 当 directional target_qty=0（parent 认为不动作）但 independent
    # scorer 超阈值独立开仓时，parent leverage 是 1.0（flat 默认），
    # 会导致保证金估算错误 → 风控 only-reduce 误拦。
    # 对 _OPENING_ACTIONS（open/scale_in）腿，用 resolve_target_leverage
    # 根据 settings.default_target_leverage * leverage_bias 独立计算；
    # 平仓/减仓腿保留 parent leverage（不影响保证金分配）。

    def _leg_leverage(book: IndependentBookEvaluation) -> float:
        if book.book_action in _OPENING_ACTIONS and book.target_qty > EPSILON_DECIMAL_12:
            return resolve_target_leverage(
                settings=settings,
                product_type=context.product_type,
                target_qty=book.target_qty,
                leverage_bias=leverage_bias,
            )
        return target_leverage

    legs = [
        leg
        for leg in (
            build_independent_leg(
                decision_id=context.decision_id,
                symbol=context.symbol,
                book=long_book,
                current_leg_notional=to_decimal(context.current_long_position_notional),
                margin_mode=resolved_margin_mode,
                target_leverage=_leg_leverage(long_book),
                reason_codes=list(long_book.reason_codes),
                family=execution_leg_family,
            ),
            build_independent_leg(
                decision_id=context.decision_id,
                symbol=context.symbol,
                book=short_book,
                current_leg_notional=to_decimal(context.current_short_position_notional),
                margin_mode=resolved_margin_mode,
                target_leverage=_leg_leverage(short_book),
                reason_codes=list(short_book.reason_codes),
                family=execution_leg_family,
            ),
        )
        if leg is not None
    ]
    main_leg, secondary_leg = _primary_and_secondary_book(long_book=long_book, short_book=short_book)
    main_leg_signal = main_leg.leg if main_leg.target_qty > EPSILON_DECIMAL_12 else "flat"
    hedge_leg_signal = (
        secondary_leg.leg
        if secondary_leg.target_qty > EPSILON_DECIMAL_12 or secondary_leg.current_qty > EPSILON_DECIMAL_12
        else "flat"
    )
    open_threshold = max(
        float(settings.strategy_hedge_independent_long_entry_threshold),
        float(settings.strategy_hedge_independent_short_entry_threshold),
    )
    close_threshold = min(
        float(settings.strategy_hedge_independent_long_close_threshold),
        float(settings.strategy_hedge_independent_short_close_threshold),
    )
    required_safe_net_edge_bps = _required_safe_net_edge_bps(settings=settings)
    overlay_decision = HedgeOverlayDecision(
        enabled=True,
        runtime_supported=True,
        configured_mode="independent",
        effective_mode="independent",
        overlay_source="independent_books",
        active=active,
        state=state,
        close_reason=_independent_close_reason_summary(
            long_book=long_book,
            short_book=short_book,
        ),
        main_leg_signal=main_leg_signal,  # type: ignore[arg-type]
        hedge_leg_signal=hedge_leg_signal,  # type: ignore[arg-type]
        main_leg_current_qty=main_leg.current_qty,
        hedge_leg_current_qty=secondary_leg.current_qty,
        main_leg_target_qty=main_leg.target_qty,
        hedge_leg_target_qty=secondary_leg.target_qty,
        hedge_ratio=(
            Decimal("0")
            if main_leg.target_qty <= EPSILON_DECIMAL_12
            else min(secondary_leg.target_qty / main_leg.target_qty, Decimal("1"))
        ),
        max_ratio=Decimal("1"),
        pressure_score=max(long_book.score, short_book.score),
        open_threshold=open_threshold,
        close_threshold=close_threshold,
        open_condition=(
            f"各腿需满足 entry 阈值、quality gate 与 expected_net_edge_bps >= {required_safe_net_edge_bps:.2f}"
            "，同时不得超过执行成本上限。"
        ),
        close_condition="各腿退出已升级为 thesis-aware state machine：支持 de-risk、failed thesis 和 stale thesis 三类收缩路径。",
        fee_drag_ratio=max(
            float(_leg_health_value(context, "long", "recent_fee_drag_ratio") or 0.0),
            float(_leg_health_value(context, "short", "recent_fee_drag_ratio") or 0.0),
        ),
        churn_ratio=max(
            float(_leg_health_value(context, "long", "recent_churn_ratio") or 0.0),
            float(_leg_health_value(context, "short", "recent_churn_ratio") or 0.0),
        ),
        long_leg_score=long_book.score,
        short_leg_score=short_book.score,
        long_leg_reason_codes=list(long_book.reason_codes),
        short_leg_reason_codes=list(short_book.reason_codes),
        long_leg_close_reason=long_book.close_reason,
        short_leg_close_reason=short_book.close_reason,
        long_leg_blocked_reasons=list(long_book.blocked_reasons),
        short_leg_blocked_reasons=list(short_book.blocked_reasons),
        reason_codes=reason_codes,
        blocked_reasons=blocked_reasons,
        min_hold_remaining_seconds=max(
            long_book.min_hold_remaining_seconds,
            short_book.min_hold_remaining_seconds,
        ),
        rebalance_cooldown_remaining_seconds=max(
            long_book.rebalance_cooldown_remaining_seconds,
            short_book.rebalance_cooldown_remaining_seconds,
        ),
        rollout_stage=rollout["configured_rollout_stage"],
        runtime_rollout_stage=rollout["runtime_stage"],
    )
    return _build_independent_family_candidate(
        final_target_qty=final_target_qty,
        legs=legs,
        overlay_decision=overlay_decision,
        long_book=long_book,
        short_book=short_book,
        context=context,
    )


def independent_book_score(
    *,
    settings: AATSSettings,
    leg: IndependentLeg,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
) -> float:
    return _independent_book_score_v2(
        settings=settings,
        leg=leg,
        baseline=baseline,
        ai_assessment=ai_assessment,
    )


def build_independent_leg(
    *,
    decision_id: str,
    symbol: str,
    book: IndependentBookEvaluation,
    current_leg_notional: Decimal,
    margin_mode: str,
    target_leverage: float,
    reason_codes: list[str],
    family: StrategyFamily,
) -> StrategyLegIntent | None:
    if book.execution_policy is None:
        return None
    pos_side = book.leg
    current_leg_qty = book.current_qty
    target_leg_qty = book.target_qty
    current_leg_qty = max(to_decimal(current_leg_qty), Decimal("0"))
    target_leg_qty = max(to_decimal(target_leg_qty), Decimal("0"))
    delta_qty = target_leg_qty - current_leg_qty
    if abs(delta_qty) <= EPSILON_DECIMAL_12:
        return None
    opening = delta_qty > 0
    action = "open" if opening else ("close" if target_leg_qty <= EPSILON_DECIMAL_12 else "reduce")
    if pos_side == "long":
        side = "buy" if opening else "sell"
        signed_current_qty = current_leg_qty
        signed_target_qty = target_leg_qty
        execution_mode = "independent_long_book"
        note = "Independent long book 决策腿。"
    else:
        side = "sell" if opening else "buy"
        signed_current_qty = -current_leg_qty
        signed_target_qty = -target_leg_qty
        execution_mode = "independent_short_book"
        note = "Independent short book 决策腿。"
    policy = book.execution_policy
    execution_chain_id = _independent_execution_chain_id(
        decision_id=decision_id,
        leg=book.leg,
        book_action=book.book_action,
        close_reason=book.close_reason,
    )
    reference_price = None
    if book.expectancy is not None:
        reference_price = book.expectancy.reference_price
        projected_notional = book.expectancy.projected_notional
        planned_delta_qty = abs(to_decimal(book.expectancy.planned_delta_qty or Decimal("0")))
        if (
            reference_price is None
            and projected_notional is not None
            and planned_delta_qty > EPSILON_DECIMAL_12
        ):
            reference_price = to_decimal(projected_notional) / planned_delta_qty
    resolved_current_leg_notional = abs(to_decimal(current_leg_notional))
    if (
        reference_price is None
        and not opening
        and current_leg_qty > EPSILON_DECIMAL_12
        and resolved_current_leg_notional > EPSILON_DECIMAL_12
    ):
        reference_price = resolved_current_leg_notional / current_leg_qty
    return StrategyLegIntent(
        symbol=symbol,
        execution_chain_id=execution_chain_id,
        product_type="derivatives",
        side=side,
        position_mode="long_short_mode",
        pos_side=pos_side,
        action=action,
        family=family,
        role="primary",
        margin_mode=margin_mode,
        target_leverage=target_leverage,
        current_position_qty=signed_current_qty,
        target_position_qty=signed_target_qty,
        delta_position_qty=signed_target_qty - signed_current_qty,
        reference_price=reference_price,
        execution_compatible=True,
        execution_mode=execution_mode,
        state_phase="active",
        overlay_mode="independent",
        trigger_reason_codes=reason_codes,
        note=note,
        execution_style_preference=policy.execution_style_preference,
        order_type_preference=policy.order_type_preference,
        time_in_force_preference=policy.time_in_force_preference,
        limit_offset_bps_preference=policy.limit_offset_bps_preference,
        execution_preference_reason_codes=[policy.policy_reason],
        book_action=book.book_action,
        close_reason=book.close_reason,
        policy_reason=policy.policy_reason,
        execution_policy_urgency=policy.urgency,
        expected_leg_cost_bps=_expectancy_cost_bps(book.expectancy),
        expected_net_edge_bps=_expectancy_net_edge_bps(book.expectancy),
        liquidity_quality_score=book.liquidity_quality_score,
        execution_health_state=book.execution_health_state,
        max_acceptable_cost_bps=policy.max_acceptable_cost_bps,
    )


def _independent_edge_strength(
    *,
    settings: AATSSettings,
    expected_net_edge_bps: float,
    weak_edge_report_only: bool,
) -> Literal["weak", "medium", "strong"]:
    required_safe_net_edge_bps = _required_safe_net_edge_bps(settings=settings)
    medium_edge_threshold = (
        required_safe_net_edge_bps
        + max(float(settings.strategy_hedge_independent_expected_execution_buffer_bps), 1.0)
    )
    if weak_edge_report_only or expected_net_edge_bps < required_safe_net_edge_bps:
        return "weak"
    if expected_net_edge_bps < medium_edge_threshold:
        return "medium"
    return "strong"


def _independent_execution_policy(
    *,
    settings: AATSSettings,
    book: IndependentBookEvaluation,
) -> IndependentExecutionPolicy | None:
    return _resolve_execution_policy(
        settings=settings,
        book=book,
        expectancy_cost_bps=_expectancy_cost_bps(book.expectancy),
        expectancy_net_edge_bps=_expectancy_net_edge_bps(book.expectancy),
        expectancy_slippage_bps=_expectancy_slippage_bps(book.expectancy, settings=settings),
        required_safe_net_edge_bps=_required_safe_net_edge_bps(settings=settings),
    )


def _independent_execution_policy_from_mode(
    *,
    mode: str,
    edge_strength: Literal["weak", "medium", "strong"],
    urgency: Literal["low", "medium", "high"],
    limit_offset_bps: Decimal | None,
    max_acceptable_cost_bps: float | None,
    policy_reason: str,
) -> IndependentExecutionPolicy:
    return _resolve_execution_policy_from_mode(
        mode=mode,
        edge_strength=edge_strength,
        urgency=urgency,
        limit_offset_bps=limit_offset_bps,
        max_acceptable_cost_bps=max_acceptable_cost_bps,
        policy_reason=policy_reason,
    )


def _evaluate_independent_book(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
    leg: IndependentLeg,
    expectancy: IndependentBookExpectancy | None,
    directional_leg_target_qty: Decimal,
    leverage_bias: float = 1.0,
    scorer: IndependentBookScorer | None,
    prior_runtime_state: StrategyBookRuntimeState | None = None,
    recent_score_history: Sequence[float] = (),
) -> IndependentBookEvaluation:
    return _evaluate_independent_book_v2(
        settings=settings,
        context=context,
        baseline=baseline,
        ai_assessment=ai_assessment,
        leg=leg,
        expectancy=expectancy,
        directional_leg_target_qty=directional_leg_target_qty,
        leverage_bias=leverage_bias,
        scorer=scorer,
        prior_runtime_state=prior_runtime_state,
        recent_score_history=recent_score_history,
    )


def _independent_entry_quality_gate(
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
    return _evaluate_entry_quality_gate(
        side=side,
        score=score,
        entry_threshold=entry_threshold,
        liquidity_quality_score=liquidity_quality_score,
        score_stability_metrics=score_stability_metrics,
        execution_health_state=execution_health_state,
        min_confirm_ticks=min_confirm_ticks,
        min_liquidity_quality=min_liquidity_quality,
        require_execution_health_ok=require_execution_health_ok,
    )


def _independent_thesis_age_seconds(
    *,
    context: DecisionContext,
    leg: IndependentLeg,
    current_qty: Decimal,
) -> float | None:
    return _independent_thesis_age_seconds_v2(
        context=context,
        leg=leg,
        current_qty=current_qty,
    )


def _independent_book_runtime_state(
    *,
    context: DecisionContext,
    book: IndependentBookEvaluation,
) -> IndependentBookRuntimeState:
    return _legacy_runtime_state_snapshot(context=context, decision=book)


def _independent_execution_chain_id(
    *,
    decision_id: str,
    leg: IndependentLeg,
    book_action: IndependentBookAction,
    close_reason: str | None,
) -> str | None:
    actionable_actions = {
        "open",
        "scale_in",
        "de_risk",
        "close_failed_thesis",
        "close_stale_thesis",
    }
    if book_action not in actionable_actions:
        return None
    suffix = ""
    if book_action == "de_risk" and close_reason:
        suffix = f":{str(close_reason).strip().lower()}"
    return f"independent:{decision_id}:{leg}:{book_action}{suffix}"


def _independent_last_transition_at(
    *,
    context: DecisionContext,
    leg: IndependentLeg,
):
    return _independent_last_transition_at_v2(context=context, leg=leg)


def _independent_cooldown_until(
    *,
    context: DecisionContext,
    min_hold_remaining_seconds: float,
    rebalance_cooldown_remaining_seconds: float,
):
    return _independent_cooldown_until_v2(
        context=context,
        min_hold_remaining_seconds=min_hold_remaining_seconds,
        rebalance_cooldown_remaining_seconds=rebalance_cooldown_remaining_seconds,
    )


def _independent_close_reason(
    *,
    settings: AATSSettings,
    score: float,
    close_threshold: float,
    expected_net_edge_bps: float | None,
    liquidity_quality_score: float | None,
    execution_health_state: IndependentExecutionHealthState | None,
    age_seconds: float | None,
) -> str | None:
    return _independent_close_reason_v2(
        settings=settings,
        score=score,
        close_threshold=close_threshold,
        expected_net_edge_bps=expected_net_edge_bps,
        liquidity_quality_score=liquidity_quality_score,
        execution_health_state=execution_health_state,
        age_seconds=age_seconds,
    )


def _independent_close_reason_code(*, leg: IndependentLeg, close_reason: str) -> str:
    return _independent_close_reason_code_v2(leg=leg, close_reason=close_reason)


def _independent_de_risk_target_qty(
    *,
    current_qty: Decimal,
    directional_leg_target_qty: Decimal,
) -> Decimal:
    return _independent_de_risk_target_qty_v2(
        current_qty=current_qty,
        directional_leg_target_qty=directional_leg_target_qty,
    )


def _independent_close_reason_summary(
    *,
    long_book: IndependentBookEvaluation,
    short_book: IndependentBookEvaluation,
) -> str | None:
    return _independent_close_reason_summary_v2(
        long_book=long_book,
        short_book=short_book,
    )


def _score_stability_metrics(
    *,
    settings: AATSSettings,
    leg: IndependentLeg,
    score: float,
    entry_threshold: float,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
    recent_score_history: Sequence[float],
) -> ScoreStabilityMetrics:
    return _score_stability_metrics_v2(
        settings=settings,
        leg=leg,
        score=score,
        entry_threshold=entry_threshold,
        baseline=baseline,
        ai_assessment=ai_assessment,
        recent_score_history=recent_score_history,
    )


def _independent_signal_confirmation_count(
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


def _compute_liquidity_quality_score(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    baseline: BaselineAssessment,
    leg: IndependentLeg,
    expected_slippage_bps: float,
) -> float:
    side_sign = 1.0 if leg == "long" else -1.0
    liquidity_scale = _clamp(float(baseline.factor_scores.get("liquidity_scale", 1.0)), 0.0, 1.0)
    microstructure_alignment = _clamp(
        max(0.0, side_sign * float(baseline.factor_scores.get("microstructure_alpha", 0.0))),
        0.0,
        1.0,
    )
    slippage_score = _clamp(
        1.0 - (max(float(expected_slippage_bps), 0.0) / max(float(settings.max_slippage_tolerance_bps), 1.0)),
        0.0,
        1.0,
    )
    fee_drag_ratio = float(_leg_health_value(context, leg, "recent_fee_drag_ratio") or 0.0)
    churn_ratio = float(_leg_health_value(context, leg, "recent_churn_ratio") or 0.0)
    fee_drag_score = 1.0
    if settings.strategy_max_fee_drag_ratio > 0:
        fee_drag_score = _clamp(1.0 - (fee_drag_ratio / float(settings.strategy_max_fee_drag_ratio)), 0.0, 1.0)
    churn_score = 1.0
    if settings.strategy_max_churn_ratio > 0:
        churn_score = _clamp(1.0 - (churn_ratio / float(settings.strategy_max_churn_ratio)), 0.0, 1.0)
    return round(
        _clamp(
            (liquidity_scale * 0.40)
            + (microstructure_alignment * 0.20)
            + (slippage_score * 0.20)
            + (fee_drag_score * 0.10)
            + (churn_score * 0.10),
            0.0,
            1.0,
        ),
        4,
    )


def _independent_execution_health_state(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    leg: IndependentLeg,
) -> IndependentExecutionHealthState:
    if _independent_trial_guard_active(settings=settings, context=context, leg=leg):
        return "blocked"
    closed_trade_count = int(_leg_health_value(context, leg, "recent_closed_trade_count") or 0)
    fee_drag_ratio = float(_leg_health_value(context, leg, "recent_fee_drag_ratio") or 0.0)
    churn_ratio = float(_leg_health_value(context, leg, "recent_churn_ratio") or 0.0)
    if closed_trade_count >= settings.strategy_performance_guard_min_closed_trades:
        if (
            fee_drag_ratio > float(settings.strategy_max_fee_drag_ratio)
            or churn_ratio > float(settings.strategy_max_churn_ratio)
        ):
            return "blocked"
        if (
            fee_drag_ratio >= float(settings.strategy_max_fee_drag_ratio) * 0.75
            or churn_ratio >= float(settings.strategy_max_churn_ratio) * 0.75
        ):
            return "degraded"
    low_edge_streak = int(_leg_health_value(context, leg, "recent_low_edge_trade_streak") or 0)
    if low_edge_streak >= max(int(settings.strategy_low_edge_streak_limit) - 1, 1):
        return "degraded"
    return "ok"


def _resolve_independent_book_expectancy(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
    runtime_margin_mode: str,
    leg: IndependentLeg,
    trade_cost_service: TradeCostService,
    expectancy_resolver: IndependentBookExpectancyResolver | None,
    latest_market_snapshot: MarketSnapshot | None = None,
    planned_delta_qty: Decimal = Decimal("0"),
    projected_notional: Decimal | None = None,
    execution_side: str = "",
    execution_mode_override: str | None = None,
) -> IndependentBookExpectancy | None:
    if expectancy_resolver is not None:
        try:
            # P3 fix: 将 execution_side 和 execution_mode_override 作为
            # kwargs 传入自定义 resolver，确保外部 resolver 也能感知
            # action-aware 的执行方向与模式。现有 resolver 多数接受 **kwargs，
            # 不会因多出的参数而中断。
            expectancy = expectancy_resolver(
                settings=settings,
                context=context,
                baseline=baseline,
                ai_assessment=ai_assessment,
                leg=leg,
                trade_cost_service=trade_cost_service,
                latest_market_snapshot=latest_market_snapshot,
                planned_delta_qty=planned_delta_qty,
                projected_notional=projected_notional,
                execution_side=execution_side,
                execution_mode_override=execution_mode_override,
            )
            if expectancy is None:
                return None
            if not isinstance(expectancy, IndependentBookExpectancy):
                return None
            if expectancy.leg != leg:
                return None
            return expectancy
        except Exception:
            return None
    try:
        return _compute_independent_book_expectancy(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            runtime_margin_mode=runtime_margin_mode,
            leg=leg,
            trade_cost_service=trade_cost_service,
            latest_market_snapshot=latest_market_snapshot,
            planned_delta_qty=planned_delta_qty,
            projected_notional=projected_notional,
            execution_side=execution_side,
            execution_mode_override=execution_mode_override,
        )
    except Exception:
        return None


def _compute_independent_book_expectancy(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
    runtime_margin_mode: str,
    execution_side: str = "",
    leg: IndependentLeg,
    trade_cost_service: TradeCostService,
    latest_market_snapshot: MarketSnapshot | None,
    planned_delta_qty: Decimal,
    projected_notional: Decimal | None,
    execution_mode_override: str | None = None,
) -> IndependentBookExpectancy:
    expected_signal_edge_bps = _independent_signal_edge_bps(
        settings=settings,
        baseline=baseline,
        ai_assessment=ai_assessment,
        leg=leg,
    )
    configured_slippage_bps = _independent_expected_slippage_bps(settings=settings)
    reference_price = _market_reference_price(latest_market_snapshot=latest_market_snapshot)
    # Use action-aware execution_side when provided; fall back to the
    # entry-side default.  Close/reduce legs trade the opposite side of
    # the book (e.g., selling to close a long, buying to close a short).
    resolved_side = execution_side if execution_side else ("buy" if leg == "long" else "sell")
    # FIX: 成本估算应反映实际执行策略，而非硬编码 taker。
    # 当 passive_first/bounded_limit 启用时，实际执行走 limit IOC，
    # 费率主要是 maker fee；硬编码 taker 会多估 ~3 bps，导致
    # 安全门控（safe_net_edge）误判本可盈利的入场信号。
    # P2 fix: execution_mode_override 由 Phase 3 exit-side 调用方传入，
    # 确保 de_risk/close_failed/close_stale 使用各自的 execution_mode
    # 而非 entry mode（它们的执行策略可能截然不同，如 taker vs passive_first）。
    entry_mode = str(
        execution_mode_override
        if execution_mode_override is not None
        else settings.strategy_hedge_independent_entry_execution_mode
    )
    estimate = _estimate_independent_execution_drag(
        settings=settings,
        trade_cost_service=trade_cost_service,
        model_name=f"independent_{leg}_book",
        symbol=context.symbol,
        product_type=context.product_type,
        margin_mode=runtime_margin_mode,
        execution_mode=entry_mode,
        execution_action="entry",
        side=resolved_side,
        quantity=planned_delta_qty,
        projected_notional=projected_notional,
        reference_price=reference_price,
        market_snapshot=latest_market_snapshot,
        expected_slippage_bps=configured_slippage_bps,
        include_funding=context.product_type == "derivatives",
    )
    size_impact_bps = _estimate_component_float(
        estimate=estimate,
        component_name="size_impact_bps",
    ) or 0.0
    expected_slippage_bps = float(
        getattr(estimate, "executable_slippage_bps", configured_slippage_bps) or configured_slippage_bps
    ) + size_impact_bps
    resolved_projected_notional = _estimate_component_decimal(
        estimate=estimate,
        component_name="projected_notional",
    )
    resolved_reference_price = _estimate_component_decimal(
        estimate=estimate,
        component_name="reference_price",
    )
    expected_cost_bps = float(estimate.executable_total_drag_bps)
    expected_lifecycle_cost_bps = _estimate_independent_lifecycle_cost_bps(
        settings=settings,
        trade_cost_service=trade_cost_service,
        symbol=context.symbol,
        product_type=context.product_type,
        runtime_margin_mode=runtime_margin_mode,
        leg=leg,
        quantity=planned_delta_qty,
        projected_notional=resolved_projected_notional or projected_notional,
        reference_price=resolved_reference_price or reference_price,
        market_snapshot=latest_market_snapshot,
        expected_slippage_bps=configured_slippage_bps,
        entry_cost_bps=expected_cost_bps,
    )
    expected_net_edge_bps = (
        expected_signal_edge_bps
        - expected_cost_bps
        - max(float(settings.strategy_edge_noise_buffer_bps), 0.0)
    )
    expected_lifecycle_net_edge_bps = (
        expected_signal_edge_bps
        - expected_lifecycle_cost_bps
        - max(float(settings.strategy_edge_noise_buffer_bps), 0.0)
    )
    return IndependentBookExpectancy(
        leg=leg,
        expected_signal_edge_bps=expected_signal_edge_bps,
        expected_slippage_bps=expected_slippage_bps,
        expected_cost_bps=expected_cost_bps,
        expected_net_edge_bps=expected_net_edge_bps,
        expected_lifecycle_cost_bps=expected_lifecycle_cost_bps,
        expected_lifecycle_net_edge_bps=expected_lifecycle_net_edge_bps,
        expected_alpha_bps=expected_signal_edge_bps,
        planned_delta_qty=planned_delta_qty,
        projected_notional=resolved_projected_notional or projected_notional,
        reference_price=resolved_reference_price or reference_price,
        quoted_depth_notional=_estimate_component_decimal(
            estimate=estimate,
            component_name="quoted_depth_notional",
        ),
        depth_consumption_ratio=_estimate_component_float(
            estimate=estimate,
            component_name="depth_consumption_ratio",
        ),
        size_impact_bps=size_impact_bps,
        cost_confidence=float(getattr(estimate, "cost_confidence", 0.0) or 0.0),
    )


def _estimate_independent_execution_drag(
    *,
    settings: AATSSettings,
    trade_cost_service: TradeCostService,
    model_name: str,
    symbol: str,
    product_type: str,
    margin_mode: str,
    execution_mode: str,
    execution_action: Literal["entry", "de_risk", "close_failed_thesis", "close_stale_thesis"],
    side: str,
    quantity: Decimal,
    projected_notional: Decimal | None,
    reference_price: Decimal | None,
    market_snapshot: MarketSnapshot | None,
    expected_slippage_bps: float,
    include_funding: bool,
):
    normalized_mode = _normalized_cost_estimate_mode(
        execution_mode=execution_mode,
        execution_action=execution_action,
    )
    if normalized_mode in {"passive_first", "bounded_limit"}:
        estimate_style = "bounded_limit_ioc"
        estimate_order_type = "limit"
        estimate_passive_bias = Decimal("0.7") if normalized_mode == "passive_first" else Decimal("0.5")
    else:
        estimate_style = "taker"
        estimate_order_type = "market"
        estimate_passive_bias = None
    return trade_cost_service.estimate_single_leg_entry(
        model_name=model_name,
        symbol=symbol,
        product_type=product_type,
        margin_mode=margin_mode,
        execution_style=estimate_style,
        order_type=estimate_order_type,
        passive_bias=estimate_passive_bias,
        side=side,
        quantity=quantity,
        projected_notional=projected_notional,
        reference_price=reference_price,
        market_snapshot=market_snapshot,
        expected_slippage_bps=expected_slippage_bps,
        include_spread=False,
        include_funding=include_funding,
    )


def _normalized_cost_estimate_mode(
    *,
    execution_mode: str,
    execution_action: Literal["entry", "de_risk", "close_failed_thesis", "close_stale_thesis"],
) -> str:
    normalized = str(execution_mode or "").strip().lower()
    if normalized != "adaptive":
        return normalized or "bounded_taker"
    if execution_action == "close_failed_thesis":
        return "aggressive_bounded_taker"
    if execution_action in {"de_risk", "close_stale_thesis"}:
        return "bounded_limit"
    return "bounded_taker"


def _estimate_independent_lifecycle_cost_bps(
    *,
    settings: AATSSettings,
    trade_cost_service: TradeCostService,
    symbol: str,
    product_type: str,
    runtime_margin_mode: str,
    leg: IndependentLeg,
    quantity: Decimal,
    projected_notional: Decimal | None,
    reference_price: Decimal | None,
    market_snapshot: MarketSnapshot | None,
    expected_slippage_bps: float,
    entry_cost_bps: float,
) -> float:
    if quantity <= EPSILON_DECIMAL_12:
        return max(entry_cost_bps, 0.0)
    exit_side = "sell" if leg == "long" else "buy"
    exit_modes = (
        ("close_failed_thesis", str(settings.strategy_hedge_independent_close_failed_thesis_execution_mode)),
        ("close_stale_thesis", str(settings.strategy_hedge_independent_close_stale_execution_mode)),
        ("de_risk", str(settings.strategy_hedge_independent_de_risk_execution_mode)),
    )
    # Exit estimates can occasionally degrade to negative values in edge cases;
    # clamp the exit-side fallback at zero so we do not double-count entry cost.
    exit_cost_candidates = [0.0]
    for action, mode in exit_modes:
        estimate = _estimate_independent_execution_drag(
            settings=settings,
            trade_cost_service=trade_cost_service,
            model_name=f"independent_{leg}_{action}_floor",
            symbol=symbol,
            product_type=product_type,
            margin_mode=runtime_margin_mode,
            execution_mode=mode,
            execution_action=action,
            side=exit_side,
            quantity=quantity,
            projected_notional=projected_notional,
            reference_price=reference_price,
            market_snapshot=market_snapshot,
            expected_slippage_bps=expected_slippage_bps,
            include_funding=False,
        )
        exit_cost_candidates.append(float(estimate.executable_total_drag_bps))
    return max(entry_cost_bps, 0.0) + max(exit_cost_candidates)


def _independent_signal_edge_bps(
    *,
    settings: AATSSettings,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
    leg: IndependentLeg,
) -> float:
    return _independent_signal_edge_bps_v2(
        settings=settings,
        baseline=baseline,
        ai_assessment=ai_assessment,
        leg=leg,
    )


def _independent_expected_slippage_bps(*, settings: AATSSettings) -> float:
    return max(float(settings.max_slippage_tolerance_bps), 0.0) * max(
        float(settings.strategy_expected_slippage_bps_fraction),
        0.0,
    )


def _planned_leg_delta_qty(*, current_qty: Decimal, target_qty: Decimal) -> Decimal:
    return max(
        abs(max(to_decimal(target_qty), Decimal("0")) - max(to_decimal(current_qty), Decimal("0"))),
        Decimal("0"),
    )


def _planned_leg_notional(
    *,
    current_qty: Decimal,
    current_notional: Decimal,
    target_qty: Decimal,
    latest_market_snapshot: MarketSnapshot | None,
) -> Decimal | None:
    planned_delta_qty = _planned_leg_delta_qty(current_qty=current_qty, target_qty=target_qty)
    if planned_delta_qty <= EPSILON_DECIMAL_12:
        return Decimal("0")
    reference_price = _market_reference_price(latest_market_snapshot=latest_market_snapshot)
    if reference_price is not None and reference_price > Decimal("0"):
        return planned_delta_qty * reference_price
    normalized_current_qty = max(to_decimal(current_qty), Decimal("0"))
    normalized_current_notional = max(to_decimal(current_notional), Decimal("0"))
    if normalized_current_qty > EPSILON_DECIMAL_12 and normalized_current_notional > Decimal("0"):
        return planned_delta_qty * (normalized_current_notional / normalized_current_qty)
    return None


def _market_reference_price(*, latest_market_snapshot: MarketSnapshot | None) -> Decimal | None:
    if latest_market_snapshot is None:
        return None
    best_bid = max(to_decimal(latest_market_snapshot.best_bid), Decimal("0"))
    best_ask = max(to_decimal(latest_market_snapshot.best_ask), Decimal("0"))
    if best_bid > Decimal("0") and best_ask > Decimal("0"):
        return (best_bid + best_ask) / Decimal("2")
    last_price = max(to_decimal(latest_market_snapshot.last_price), Decimal("0"))
    return None if last_price <= Decimal("0") else last_price


def _estimate_component_decimal(*, estimate: object, component_name: str) -> Decimal | None:
    components = getattr(estimate, "execution_context", None)
    value = components.get(component_name) if isinstance(components, dict) else None
    if value is None:
        drag_components = getattr(estimate, "execution_drag_components_bps", None)
        value = drag_components.get(component_name) if isinstance(drag_components, dict) else None
    if value is None:
        return None
    try:
        return to_decimal(value)
    except Exception:
        return None


def _estimate_component_float(*, estimate: object, component_name: str) -> float | None:
    value = _estimate_component_decimal(estimate=estimate, component_name=component_name)
    return None if value is None else float(value)


def _independent_open_gate(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    leg: IndependentLeg,
    expected_cost_bps: float,
    expected_net_edge_bps: float,
) -> dict[str, object]:
    eligibility = _evaluate_open_eligibility(
        settings=settings,
        context=context,
        leg=leg,
        expectancy=IndependentBookExpectancy(
            leg=leg,
            expected_signal_edge_bps=max(expected_net_edge_bps + expected_cost_bps, 0.0),
            expected_slippage_bps=_independent_expected_slippage_bps(settings=settings),
            expected_cost_bps=expected_cost_bps,
            expected_net_edge_bps=expected_net_edge_bps,
            expected_alpha_bps=max(expected_net_edge_bps + expected_cost_bps, 0.0),
        ),
    )
    return {
        "blocked_reasons": list(eligibility.hard_block_reasons),
        "weak_edge_report_only": bool(eligibility.warnings),
    }


def _independent_min_hold_remaining_seconds(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    leg: IndependentLeg,
) -> float:
    return _independent_min_hold_remaining_seconds_v2(
        settings=settings,
        context=context,
        leg=leg,
    )


def _independent_rebalance_remaining_seconds(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    leg: IndependentLeg,
    opening_or_expanding: bool,
    desired_target_qty: Decimal,
    current_qty: Decimal,
) -> float:
    return _independent_rebalance_remaining_seconds_v2(
        settings=settings,
        context=context,
        leg=leg,
        opening_or_expanding=opening_or_expanding,
        desired_target_qty=desired_target_qty,
        current_qty=current_qty,
    )


def _independent_post_close_cooldown_active(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    leg: IndependentLeg,
) -> bool:
    return _independent_post_close_cooldown_active_v2(
        settings=settings,
        context=context,
        leg=leg,
    )


def _independent_low_edge_cooldown_active(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    leg: IndependentLeg,
) -> bool:
    return _independent_low_edge_cooldown_active_v2(
        settings=settings,
        context=context,
        leg=leg,
    )


def _independent_performance_degraded(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    leg: IndependentLeg,
) -> bool:
    return _independent_performance_degraded_v2(
        settings=settings,
        context=context,
        leg=leg,
    )


def _independent_trial_guard_active(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    leg: IndependentLeg,
) -> bool:
    return _independent_trial_guard_active_v2(
        settings=settings,
        context=context,
        leg=leg,
    )


def _leg_health_value(context: DecisionContext, leg: IndependentLeg, key: str) -> object | None:
    payload = context.leg_strategy_health.get(leg)
    if not isinstance(payload, dict):
        return None
    return payload.get(key)


def _leg_health_datetime(context: DecisionContext, leg: IndependentLeg, key: str):
    value = _leg_health_value(context, leg, key)
    return value if hasattr(value, "isoformat") else None


def _required_safe_net_edge_bps(*, settings: AATSSettings) -> float:
    return _required_safe_net_edge_bps_v2(settings=settings)


def _candidate_confidence(score: float) -> float:
    return _candidate_confidence_v2(score)


def _candidate_urgency(*, overlay_decision: HedgeOverlayDecision) -> Literal["low", "medium", "high"]:
    if overlay_decision.state in {"opening", "closing"}:
        return "high"
    if overlay_decision.active:
        return "medium"
    return "low"


def _independent_overlay_state(
    *,
    long_book: IndependentBookEvaluation,
    short_book: IndependentBookEvaluation,
    blocked_reasons: list[str],
    active: bool,
) -> Literal["inactive", "opening", "holding", "closing", "blocked"]:
    actionable_states = {"opening", "closing"}
    if long_book.state in actionable_states or short_book.state in actionable_states:
        return "opening" if "opening" in {long_book.state, short_book.state} else "closing"
    if blocked_reasons:
        return "blocked"
    if active:
        return "holding"
    return "inactive"


def _primary_and_secondary_book(
    *,
    long_book: IndependentBookEvaluation,
    short_book: IndependentBookEvaluation,
) -> tuple[IndependentBookEvaluation, IndependentBookEvaluation]:
    main_leg = long_book if long_book.score >= short_book.score else short_book
    secondary_leg = short_book if main_leg is long_book else long_book
    if (
        main_leg.target_qty <= EPSILON_DECIMAL_12
        and main_leg.current_qty <= EPSILON_DECIMAL_12
        and (secondary_leg.target_qty > EPSILON_DECIMAL_12 or secondary_leg.current_qty > EPSILON_DECIMAL_12)
    ):
        main_leg, secondary_leg = secondary_leg, main_leg
    return main_leg, secondary_leg


def _independent_candidate_headline(*, overlay_decision: HedgeOverlayDecision) -> str:
    if overlay_decision.state == "opening":
        if overlay_decision.main_leg_signal == "long":
            return "Independent 家族计划建立多头独立账本。"
        if overlay_decision.main_leg_signal == "short":
            return "Independent 家族计划建立空头独立账本。"
        return "Independent 家族计划建立独立账本。"
    if overlay_decision.state == "holding":
        return "Independent 家族当前维持独立双账本。"
    if overlay_decision.state == "closing":
        if overlay_decision.close_reason == "failed_thesis":
            return "Independent 家族计划按 thesis 失效关闭独立账本。"
        if overlay_decision.close_reason == "stale_thesis":
            return "Independent 家族计划按 thesis 过期退出独立账本。"
        if overlay_decision.close_reason in {"weak_edge_de_risk", "execution_health_degraded", "liquidity_degraded"}:
            return "Independent 家族计划先降低独立账本风险暴露。"
        return "Independent 家族计划退出独立账本。"
    if overlay_decision.state == "blocked":
        return f"Independent 家族当前被阻断：{_first_reason(overlay_decision.blocked_reasons)}。"
    return "Independent 家族当前未触发可执行账本。"


def _first_reason(reasons: list[str]) -> str:
    if not reasons:
        return "没有额外阻断原因"
    return str(reasons[0])


def _inactive_book(leg: IndependentLeg) -> IndependentBookEvaluation:
    return IndependentBookEvaluation(
        leg=leg,
        expectancy=IndependentBookExpectancy(
            leg=leg,
            expected_signal_edge_bps=0.0,
            expected_slippage_bps=0.0,
            expected_cost_bps=0.0,
            expected_net_edge_bps=0.0,
            expected_alpha_bps=0.0,
        ),
        score=0.0,
        current_qty=Decimal("0"),
        target_qty=Decimal("0"),
        state="inactive",
        reason_codes=[],
        blocked_reasons=[],
        min_hold_remaining_seconds=0.0,
        rebalance_cooldown_remaining_seconds=0.0,
    )


def _ai_directional_edge(ai_assessment: AIMarketAssessment | None) -> float:
    return 0.0 if ai_assessment is None else ai_assessment.directional_edge
