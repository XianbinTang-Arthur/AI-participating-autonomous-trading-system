from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from typing import Literal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.decision import AIMarketAssessment, BaselineAssessment, DecisionContext, HedgeOverlayDecision
from aats.schemas.strategy_runtime import (
    StrategyCandidate,
    StrategyBookExpectancyEntry,
    StrategyBookExpectancySummary,
    StrategyBookRuntimeState,
    StrategyFamily,
    StrategyFamilyAction,
    StrategyLegIntent,
)
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
from aats.services.strategy_engines.base import (
    StrategyEvaluationContext,
    StrategyFamilyRuntimeControl,
    StrategyTargetHistory,
)
from aats.services.strategy_engines.families.protective_family import (
    _candidate_state_from_overlay_state,
    _placeholder_family_candidate,
    protective_runtime_supported,
)
from aats.services.strategy_engines.families.independent_models import IndependentBookRuntimeState
from aats.services.strategy_overlay_rollout import overlay_rollout_status
from aats.services.trade_costs import TradeCostService

IndependentLeg = Literal["long", "short"]
IndependentExecutionHealthState = Literal["ok", "degraded", "blocked"]
IndependentBookAction = Literal[
    "inactive",
    "open",
    "hold",
    "scale_in",
    "de_risk",
    "close_failed_thesis",
    "close_stale_thesis",
    "blocked",
]
IndependentBookScorer = Callable[..., float]
IndependentBookExpectancyResolver = Callable[..., "IndependentBookExpectancy"]


@dataclass(frozen=True, slots=True)
class IndependentBookExpectancy:
    leg: IndependentLeg
    expected_signal_edge_bps: float
    expected_slippage_bps: float
    expected_cost_bps: float
    expected_net_edge_bps: float
    resolution_failed: bool = False


@dataclass(frozen=True, slots=True)
class ScoreStabilityMetrics:
    support_count: int
    min_score: float
    mean_score: float
    max_drawdown_bps: float
    stable: bool
    source: Literal["recent_target_history", "current_signal_confirmation"]


@dataclass(frozen=True, slots=True)
class IndependentExecutionPolicy:
    edge_strength: Literal["weak", "medium", "strong"]
    urgency: Literal["low", "medium", "high"]
    execution_style_preference: str | None
    order_type_preference: Literal["market", "limit"] | None
    time_in_force_preference: str | None
    limit_offset_bps_preference: Decimal | None
    max_acceptable_cost_bps: float | None
    policy_reason: str


@dataclass(frozen=True, slots=True)
class IndependentBookEvaluation:
    leg: IndependentLeg
    expectancy: IndependentBookExpectancy
    score: float
    current_qty: Decimal
    target_qty: Decimal
    state: str
    reason_codes: list[str]
    blocked_reasons: list[str]
    min_hold_remaining_seconds: float
    rebalance_cooldown_remaining_seconds: float
    book_action: IndependentBookAction = "inactive"
    close_reason: str | None = None
    thesis_age_seconds: float | None = None
    weak_edge_report_only: bool = False
    liquidity_quality_score: float | None = None
    score_stability_metrics: ScoreStabilityMetrics | None = None
    execution_health_state: IndependentExecutionHealthState | None = None
    execution_policy: IndependentExecutionPolicy | None = None


@dataclass(frozen=True, slots=True)
class IndependentFamilyEvaluation:
    final_target_qty: Decimal
    legs: list[StrategyLegIntent]
    overlay_decision: HedgeOverlayDecision
    long_book: IndependentBookEvaluation
    short_book: IndependentBookEvaluation
    book_runtime_states: tuple[IndependentBookRuntimeState, ...] = ()


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
    runtime_supported = protective_runtime_supported(settings=settings, context=context)
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
        "min_liquidity_quality": settings.strategy_hedge_independent_min_liquidity_quality,
        "require_execution_health_ok": settings.strategy_hedge_independent_require_execution_health_ok,
        "max_thesis_age_seconds": settings.strategy_hedge_independent_max_thesis_age_seconds,
        "de_risk_net_edge_bps": settings.strategy_hedge_independent_de_risk_net_edge_bps,
        "failed_thesis_net_edge_bps": settings.strategy_hedge_independent_failed_thesis_net_edge_bps,
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
        signal_edge_bps=float(directional_target.expected_signal_edge_bps),
        expected_cost_bps=float(directional_target.expected_cost_bps),
        expected_net_edge_bps=float(directional_target.expected_net_edge_bps),
        execution_leg_family="independent",
        trade_cost_service=trade_cost_service,
        recent_score_history_by_leg=_independent_recent_score_history_by_leg(
            recent_targets_by_family=evaluation_context.recent_targets_by_family,
            max_points=max(int(settings.strategy_hedge_independent_min_confirm_ticks), 3),
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
            "long_expected_signal_edge_bps": result.long_book.expectancy.expected_signal_edge_bps,
            "long_expected_slippage_bps": result.long_book.expectancy.expected_slippage_bps,
            "long_expected_cost_bps": result.long_book.expectancy.expected_cost_bps,
            "long_expected_net_edge_bps": result.long_book.expectancy.expected_net_edge_bps,
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
            "long_score_stability_max_drawdown_bps": (
                None
                if result.long_book.score_stability_metrics is None
                else result.long_book.score_stability_metrics.max_drawdown_bps
            ),
            "long_score_stability_source": (
                None
                if result.long_book.score_stability_metrics is None
                else result.long_book.score_stability_metrics.source
            ),
            "long_execution_health_state": result.long_book.execution_health_state,
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
            "short_expected_signal_edge_bps": result.short_book.expectancy.expected_signal_edge_bps,
            "short_expected_slippage_bps": result.short_book.expectancy.expected_slippage_bps,
            "short_expected_cost_bps": result.short_book.expectancy.expected_cost_bps,
            "short_expected_net_edge_bps": result.short_book.expectancy.expected_net_edge_bps,
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
            "short_score_stability_max_drawdown_bps": (
                None
                if result.short_book.score_stability_metrics is None
                else result.short_book.score_stability_metrics.max_drawdown_bps
            ),
            "short_score_stability_source": (
                None
                if result.short_book.score_stability_metrics is None
                else result.short_book.score_stability_metrics.source
            ),
            "short_execution_health_state": result.short_book.execution_health_state,
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
            "close_reason": _independent_close_reason_summary(
                long_book=result.long_book,
                short_book=result.short_book,
            ),
            "expected_signal_edge_bps": max(
                result.long_book.expectancy.expected_signal_edge_bps,
                result.short_book.expectancy.expected_signal_edge_bps,
            ),
            "expected_cost_bps": max(
                result.long_book.expectancy.expected_cost_bps,
                result.short_book.expectancy.expected_cost_bps,
            ),
            "expected_net_edge_bps": max(
                result.long_book.expectancy.expected_net_edge_bps,
                result.short_book.expectancy.expected_net_edge_bps,
            ),
            "book_runtime_states": [
                state.model_dump(mode="json")
                for state in book_runtime_states
            ],
        },
        legs=list(result.legs),
    )


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
                expected_gross_edge_bps=book.expectancy.expected_signal_edge_bps,
                expected_signal_edge_bps=book.expectancy.expected_signal_edge_bps,
                expected_slippage_bps=book.expectancy.expected_slippage_bps,
                expected_cost_bps=book.expectancy.expected_cost_bps,
                expected_net_edge_bps=book.expectancy.expected_net_edge_bps,
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
                expected_leg_cost_bps=book.expectancy.expected_cost_bps,
                liquidity_quality_score=book.liquidity_quality_score,
                execution_health_state=book.execution_health_state,
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
        StrategyBookRuntimeState(
            leg=state.side,
            execution_chain_id=state.execution_chain_id,
            current_qty=state.current_qty,
            target_qty=state.target_qty,
            state=state.state,
            score=state.score,
            book_action=state.book_action,
            close_reason=state.close_reason,
            policy_reason=state.policy_reason,
            thesis_started_at=state.thesis_started_at,
            thesis_age_seconds=state.thesis_age_seconds,
            last_transition_at=state.last_transition_at,
            last_transition_reason=state.last_transition_reason,
            expected_signal_edge_bps=state.expected_signal_edge_bps,
            expected_cost_bps=state.expected_cost_bps,
            expected_net_edge_bps=state.expected_net_edge_bps,
            liquidity_quality_score=state.liquidity_quality_score,
            execution_health_state=state.execution_health_state,
            cooldown_until=state.cooldown_until,
            min_hold_remaining_seconds=state.min_hold_remaining_seconds,
            rebalance_cooldown_remaining_seconds=state.rebalance_cooldown_remaining_seconds,
            execution_policy_urgency=state.execution_policy_urgency,
            edge_strength=state.edge_strength,
            reason_codes=list(state.reason_codes),
            blocked_reasons=list(state.blocked_reasons),
        )
        for state in result.book_runtime_states
    ]


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
    signal_edge_bps: float,
    expected_cost_bps: float,
    expected_net_edge_bps: float,
    execution_leg_family: StrategyFamily,
    scorer: IndependentBookScorer | None = None,
    trade_cost_service: TradeCostService | None = None,
    expectancy_resolver: IndependentBookExpectancyResolver | None = None,
    recent_score_history_by_leg: dict[IndependentLeg, tuple[float, ...]] | None = None,
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
    long_expectancy = _resolve_independent_book_expectancy(
        settings=settings,
        context=context,
        baseline=baseline,
        ai_assessment=ai_assessment,
        runtime_margin_mode=resolved_margin_mode,
        leg="long",
        trade_cost_service=cost_service,
        fallback_signal_edge_bps=signal_edge_bps,
        fallback_expected_cost_bps=expected_cost_bps,
        fallback_expected_net_edge_bps=expected_net_edge_bps,
        expectancy_resolver=expectancy_resolver,
    )
    short_expectancy = _resolve_independent_book_expectancy(
        settings=settings,
        context=context,
        baseline=baseline,
        ai_assessment=ai_assessment,
        runtime_margin_mode=resolved_margin_mode,
        leg="short",
        trade_cost_service=cost_service,
        fallback_signal_edge_bps=signal_edge_bps,
        fallback_expected_cost_bps=expected_cost_bps,
        fallback_expected_net_edge_bps=expected_net_edge_bps,
        expectancy_resolver=expectancy_resolver,
    )
    long_book = _evaluate_independent_book(
        settings=settings,
        context=context,
        baseline=baseline,
        ai_assessment=ai_assessment,
        leg="long",
        expectancy=long_expectancy,
        directional_leg_target_qty=directional_long_target_qty,
        scorer=scorer,
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
        scorer=scorer,
        recent_score_history=(
            ()
            if recent_score_history_by_leg is None
            else recent_score_history_by_leg.get("short", ())
        ),
    )
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
    legs = [
        leg
        for leg in (
            build_independent_leg(
                decision_id=context.decision_id,
                symbol=context.symbol,
                book=long_book,
                margin_mode=resolved_margin_mode,
                target_leverage=target_leverage,
                reason_codes=list(long_book.reason_codes),
                family=execution_leg_family,
            ),
            build_independent_leg(
                decision_id=context.decision_id,
                symbol=context.symbol,
                book=short_book,
                margin_mode=resolved_margin_mode,
                target_leverage=target_leverage,
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
    book_runtime_states = (
        _independent_book_runtime_state(context=context, book=long_book),
        _independent_book_runtime_state(context=context, book=short_book),
    )
    return IndependentFamilyEvaluation(
        final_target_qty=final_target_qty,
        legs=legs,
        overlay_decision=overlay_decision,
        long_book=long_book,
        short_book=short_book,
        book_runtime_states=book_runtime_states,
    )


def independent_book_score(
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


def build_independent_leg(
    *,
    decision_id: str,
    symbol: str,
    book: IndependentBookEvaluation,
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
        expected_leg_cost_bps=book.expectancy.expected_cost_bps,
        expected_net_edge_bps=book.expectancy.expected_net_edge_bps,
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
    if book.book_action in {"inactive", "hold", "blocked"}:
        return None
    edge_strength = _independent_edge_strength(
        settings=settings,
        expected_net_edge_bps=book.expectancy.expected_net_edge_bps,
        weak_edge_report_only=book.weak_edge_report_only,
    )
    min_liquidity_quality = float(settings.strategy_hedge_independent_min_liquidity_quality)
    liquidity_degraded = (
        book.liquidity_quality_score is not None
        and book.liquidity_quality_score + 1e-9 < min_liquidity_quality
    )
    execution_degraded = book.execution_health_state in {"degraded", "blocked"}
    passive_limit_offset_bps = max(
        Decimal("0.5"),
        to_decimal(book.expectancy.expected_slippage_bps),
        to_decimal(settings.strategy_hedge_independent_expected_slippage_buffer_bps),
    )
    max_acceptable_cost_bps = float(settings.strategy_hedge_independent_max_acceptable_cost_bps)
    max_cost = max_acceptable_cost_bps if max_acceptable_cost_bps > 0.0 else None

    if book.book_action == "close_failed_thesis":
        configured_mode = settings.strategy_hedge_independent_close_failed_thesis_execution_mode
        if configured_mode != "adaptive":
            return _independent_execution_policy_from_mode(
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
        )
    if book.book_action == "close_stale_thesis":
        configured_mode = settings.strategy_hedge_independent_close_stale_execution_mode
        if configured_mode != "adaptive":
            return _independent_execution_policy_from_mode(
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
        )
    if book.book_action == "de_risk":
        configured_mode = settings.strategy_hedge_independent_de_risk_execution_mode
        de_risk_urgency: Literal["low", "medium", "high"] = (
            "high" if book.close_reason == "execution_health_degraded" else "medium"
        )
        if configured_mode != "adaptive":
            return _independent_execution_policy_from_mode(
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
        )
    if book.book_action == "scale_in":
        configured_mode = settings.strategy_hedge_independent_scale_in_execution_mode
        if configured_mode != "adaptive":
            return _independent_execution_policy_from_mode(
                mode=configured_mode,
                edge_strength=edge_strength,
                urgency="low" if configured_mode in {"passive_first", "bounded_limit"} else "medium",
                limit_offset_bps=to_decimal(settings.strategy_hedge_independent_limit_offset_bps_scale_in),
                max_acceptable_cost_bps=max_cost,
                policy_reason=f"independent_scale_in_configured_{configured_mode}",
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
                policy_reason="independent_scale_strong_edge_aggressive",
            )
        if bool(settings.strategy_hedge_independent_passive_first_enabled):
            return IndependentExecutionPolicy(
                edge_strength=edge_strength,
                urgency="low",
                execution_style_preference="bounded_limit_ioc",
                order_type_preference="limit",
                time_in_force_preference="IOC",
                limit_offset_bps_preference=passive_limit_offset_bps,
                max_acceptable_cost_bps=max_cost,
                policy_reason="independent_scale_guarded_passive_first",
            )
        return IndependentExecutionPolicy(
            edge_strength=edge_strength,
            urgency="medium",
            execution_style_preference="taker",
            order_type_preference="market",
            time_in_force_preference="IOC",
            limit_offset_bps_preference=None,
            max_acceptable_cost_bps=max_cost,
            policy_reason="independent_scale_guarded_aggressive_fallback",
        )
    if book.book_action == "open":
        configured_mode = settings.strategy_hedge_independent_entry_execution_mode
        if configured_mode != "adaptive":
            return _independent_execution_policy_from_mode(
                mode=configured_mode,
                edge_strength=edge_strength,
                urgency="low" if configured_mode in {"passive_first", "bounded_limit"} else "medium",
                limit_offset_bps=to_decimal(settings.strategy_hedge_independent_limit_offset_bps_entry),
                max_acceptable_cost_bps=max_cost,
                policy_reason=f"independent_entry_configured_{configured_mode}",
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
                policy_reason="independent_entry_strong_edge_aggressive",
            )
        if bool(settings.strategy_hedge_independent_passive_first_enabled):
            return IndependentExecutionPolicy(
                edge_strength=edge_strength,
                urgency="low",
                execution_style_preference="bounded_limit_ioc",
                order_type_preference="limit",
                time_in_force_preference="IOC",
                limit_offset_bps_preference=passive_limit_offset_bps,
                max_acceptable_cost_bps=max_cost,
                policy_reason=(
                    "independent_weak_edge_passive_first_required"
                    if book.weak_edge_report_only
                    else "independent_entry_guarded_passive_first"
                ),
            )
        return IndependentExecutionPolicy(
            edge_strength=edge_strength,
            urgency="medium",
            execution_style_preference="taker",
            order_type_preference="market",
            time_in_force_preference="IOC",
            limit_offset_bps_preference=None,
            max_acceptable_cost_bps=max_cost,
            policy_reason="independent_entry_guarded_aggressive_fallback",
        )
    return None


def _independent_execution_policy_from_mode(
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
    )


def _evaluate_independent_book(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
    leg: IndependentLeg,
    expectancy: IndependentBookExpectancy,
    directional_leg_target_qty: Decimal,
    scorer: IndependentBookScorer | None,
    recent_score_history: Sequence[float] = (),
) -> IndependentBookEvaluation:
    current_qty = (
        to_decimal(context.current_long_position_qty)
        if leg == "long"
        else to_decimal(context.current_short_position_qty)
    )
    score = (
        scorer(leg=leg, baseline=baseline, ai_assessment=ai_assessment)
        if scorer is not None
        else independent_book_score(
            settings=settings,
            leg=leg,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )
    )
    entry_threshold = (
        float(settings.strategy_hedge_independent_long_entry_threshold)
        if leg == "long"
        else float(settings.strategy_hedge_independent_short_entry_threshold)
    )
    close_threshold = (
        float(settings.strategy_hedge_independent_long_close_threshold)
        if leg == "long"
        else float(settings.strategy_hedge_independent_short_close_threshold)
    )
    scale_threshold = (
        float(settings.strategy_hedge_independent_long_scale_in_threshold)
        if leg == "long"
        else float(settings.strategy_hedge_independent_short_scale_in_threshold)
    )
    target_qty = current_qty
    base_target_qty = max(to_decimal(settings.default_order_qty), directional_leg_target_qty)
    reason_codes: list[str] = []
    blocked_reasons: list[str] = []
    state = "inactive"
    min_hold_remaining_seconds = 0.0
    rebalance_cooldown_remaining_seconds = 0.0
    book_action: IndependentBookAction = "inactive"
    close_reason: str | None = None
    thesis_age_seconds = _independent_thesis_age_seconds(
        context=context,
        leg=leg,
        current_qty=current_qty,
    )
    weak_edge_report_only = False
    liquidity_quality_score = _compute_liquidity_quality_score(
        settings=settings,
        context=context,
        baseline=baseline,
        leg=leg,
        expected_slippage_bps=expectancy.expected_slippage_bps,
    )
    score_stability_metrics = _score_stability_metrics(
        settings=settings,
        leg=leg,
        score=score,
        entry_threshold=entry_threshold,
        baseline=baseline,
        ai_assessment=ai_assessment,
        recent_score_history=recent_score_history,
    )
    execution_health_state = _independent_execution_health_state(
        settings=settings,
        context=context,
        leg=leg,
    )
    expectancy_resolution_failed = bool(expectancy.resolution_failed)

    if current_qty <= EPSILON_DECIMAL_12:
        if score >= entry_threshold:
            reason_codes.append(f"independent_{leg}_book_signal_above_entry_threshold")
            if expectancy_resolution_failed:
                blocked_reasons.append(f"independent_{leg}_book_expectancy_resolution_failed")
            open_gate = _independent_open_gate(
                settings=settings,
                context=context,
                leg=leg,
                expected_cost_bps=expectancy.expected_cost_bps,
                expected_net_edge_bps=expectancy.expected_net_edge_bps,
            )
            blocked_reasons.extend(open_gate["blocked_reasons"])
            weak_edge_report_only = bool(open_gate["weak_edge_report_only"])
            if weak_edge_report_only:
                reason_codes.append(f"independent_{leg}_book_expected_net_edge_below_safe_threshold_report_only")
            _, quality_blocked_reasons = _independent_entry_quality_gate(
                side=leg,
                score=score,
                entry_threshold=entry_threshold,
                liquidity_quality_score=liquidity_quality_score,
                score_stability_metrics=score_stability_metrics,
                execution_health_state=execution_health_state,
                min_confirm_ticks=int(settings.strategy_hedge_independent_min_confirm_ticks),
                min_liquidity_quality=float(settings.strategy_hedge_independent_min_liquidity_quality),
                require_execution_health_ok=bool(settings.strategy_hedge_independent_require_execution_health_ok),
            )
            blocked_reasons.extend(quality_blocked_reasons)
            rebalance_cooldown_remaining_seconds = _independent_rebalance_remaining_seconds(
                settings=settings,
                context=context,
                leg=leg,
                opening_or_expanding=True,
                desired_target_qty=base_target_qty,
                current_qty=current_qty,
            )
            if rebalance_cooldown_remaining_seconds > 0:
                blocked_reasons.append(f"independent_{leg}_book_rebalance_cooldown_active")
            if blocked_reasons:
                state = "blocked"
                book_action = "blocked"
                target_qty = Decimal("0")
            else:
                target_qty = base_target_qty
                state = "opening"
                book_action = "open"
        else:
            reason_codes.append(f"independent_{leg}_book_signal_below_entry_threshold")
    else:
        state = "holding"
        book_action = "hold"
        close_reason = _independent_close_reason(
            settings=settings,
            score=score,
            close_threshold=close_threshold,
            expected_net_edge_bps=expectancy.expected_net_edge_bps,
            liquidity_quality_score=liquidity_quality_score,
            execution_health_state=execution_health_state,
            age_seconds=thesis_age_seconds,
        )
        if close_reason is not None:
            reason_codes.append(_independent_close_reason_code(leg=leg, close_reason=close_reason))
            min_hold_remaining_seconds = _independent_min_hold_remaining_seconds(
                settings=settings,
                context=context,
                leg=leg,
            )
            if min_hold_remaining_seconds > 0:
                blocked_reasons.append(f"independent_{leg}_book_min_hold_active")
                state = "blocked"
                book_action = "blocked"
            elif close_reason == "failed_thesis":
                target_qty = Decimal("0")
                state = "closing"
                book_action = "close_failed_thesis"
            elif close_reason == "stale_thesis":
                target_qty = Decimal("0")
                state = "closing"
                book_action = "close_stale_thesis"
            else:
                target_qty = _independent_de_risk_target_qty(
                    current_qty=current_qty,
                    directional_leg_target_qty=directional_leg_target_qty,
                )
                if target_qty + EPSILON_DECIMAL_12 < current_qty:
                    state = "closing"
                    book_action = "de_risk"
                else:
                    state = "holding"
                    book_action = "hold"
        elif score >= scale_threshold and base_target_qty > current_qty + EPSILON_DECIMAL_12:
            reason_codes.append(f"independent_{leg}_book_signal_above_scale_in_threshold")
            if expectancy_resolution_failed:
                blocked_reasons.append(f"independent_{leg}_book_expectancy_resolution_failed")
            open_gate = _independent_open_gate(
                settings=settings,
                context=context,
                leg=leg,
                expected_cost_bps=expectancy.expected_cost_bps,
                expected_net_edge_bps=expectancy.expected_net_edge_bps,
            )
            blocked_reasons.extend(open_gate["blocked_reasons"])
            weak_edge_report_only = bool(open_gate["weak_edge_report_only"])
            if weak_edge_report_only:
                reason_codes.append(f"independent_{leg}_book_expected_net_edge_below_safe_threshold_report_only")
            _, quality_blocked_reasons = _independent_entry_quality_gate(
                side=leg,
                score=score,
                entry_threshold=entry_threshold,
                liquidity_quality_score=liquidity_quality_score,
                score_stability_metrics=score_stability_metrics,
                execution_health_state=execution_health_state,
                min_confirm_ticks=int(settings.strategy_hedge_independent_min_confirm_ticks),
                min_liquidity_quality=float(settings.strategy_hedge_independent_min_liquidity_quality),
                require_execution_health_ok=bool(settings.strategy_hedge_independent_require_execution_health_ok),
            )
            blocked_reasons.extend(quality_blocked_reasons)
            rebalance_cooldown_remaining_seconds = _independent_rebalance_remaining_seconds(
                settings=settings,
                context=context,
                leg=leg,
                opening_or_expanding=True,
                desired_target_qty=base_target_qty,
                current_qty=current_qty,
            )
            if rebalance_cooldown_remaining_seconds > 0:
                blocked_reasons.append(f"independent_{leg}_book_rebalance_cooldown_active")
            if blocked_reasons:
                target_qty = current_qty
                state = "blocked"
                book_action = "blocked"
            else:
                target_qty = base_target_qty
                state = "opening"
                book_action = "scale_in"
        elif score >= entry_threshold:
            reason_codes.append(f"independent_{leg}_book_hold_above_entry_threshold")
        elif score >= close_threshold:
            reason_codes.append(f"independent_{leg}_book_hold_above_close_threshold")
        else:
            reason_codes.append(f"independent_{leg}_book_hold_without_thesis_break")

    preview_book = IndependentBookEvaluation(
        leg=leg,
        expectancy=expectancy,
        score=score,
        current_qty=current_qty,
        target_qty=target_qty,
        state=state,
        reason_codes=reason_codes,
        blocked_reasons=blocked_reasons,
        min_hold_remaining_seconds=min_hold_remaining_seconds,
        rebalance_cooldown_remaining_seconds=rebalance_cooldown_remaining_seconds,
        book_action=book_action,
        close_reason=close_reason,
        thesis_age_seconds=thesis_age_seconds,
        weak_edge_report_only=weak_edge_report_only,
        liquidity_quality_score=liquidity_quality_score,
        score_stability_metrics=score_stability_metrics,
        execution_health_state=execution_health_state,
    )
    return replace(
        preview_book,
        execution_policy=_independent_execution_policy(
            settings=settings,
            book=preview_book,
        ),
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


def _independent_thesis_age_seconds(
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


def _independent_book_runtime_state(
    *,
    context: DecisionContext,
    book: IndependentBookEvaluation,
) -> IndependentBookRuntimeState:
    thesis_started_at = (
        context.current_long_leg_opened_at
        if book.leg == "long"
        else context.current_short_leg_opened_at
    )
    last_transition_at = _independent_last_transition_at(context=context, leg=book.leg)
    last_transition_reason = (
        book.close_reason
        or (None if book.execution_policy is None else book.execution_policy.policy_reason)
        or book.book_action
    )
    cooldown_until = _independent_cooldown_until(
        context=context,
        min_hold_remaining_seconds=book.min_hold_remaining_seconds,
        rebalance_cooldown_remaining_seconds=book.rebalance_cooldown_remaining_seconds,
    )
    return IndependentBookRuntimeState(
        side=book.leg,
        current_qty=book.current_qty,
        target_qty=book.target_qty,
        state=book.state,
        execution_chain_id=_independent_execution_chain_id(
            decision_id=context.decision_id,
            leg=book.leg,
            book_action=book.book_action,
            close_reason=book.close_reason,
        ),
        thesis_started_at=thesis_started_at,
        thesis_age_seconds=book.thesis_age_seconds,
        last_transition_at=last_transition_at,
        last_transition_reason=last_transition_reason,
        expected_signal_edge_bps=book.expectancy.expected_signal_edge_bps,
        expected_cost_bps=book.expectancy.expected_cost_bps,
        expected_net_edge_bps=book.expectancy.expected_net_edge_bps,
        liquidity_quality_score=book.liquidity_quality_score,
        execution_health_state=book.execution_health_state,
        cooldown_until=cooldown_until,
        min_hold_remaining_seconds=book.min_hold_remaining_seconds,
        rebalance_cooldown_remaining_seconds=book.rebalance_cooldown_remaining_seconds,
        score=book.score,
        reason_codes=tuple(book.reason_codes),
        blocked_reasons=tuple(book.blocked_reasons),
        book_action=book.book_action,
        close_reason=book.close_reason,
        policy_reason=None if book.execution_policy is None else book.execution_policy.policy_reason,
        execution_policy_urgency=(
            None if book.execution_policy is None else book.execution_policy.urgency
        ),
        edge_strength=(
            None if book.execution_policy is None else book.execution_policy.edge_strength
        ),
    )


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
    latest_fill = context.latest_long_leg_fill_timestamp if leg == "long" else context.latest_short_leg_fill_timestamp
    opened_at = context.current_long_leg_opened_at if leg == "long" else context.current_short_leg_opened_at
    closed_at = context.last_long_leg_closed_at if leg == "long" else context.last_short_leg_closed_at
    for candidate in (latest_fill, opened_at, closed_at):
        if candidate is not None:
            return candidate
    return None


def _independent_cooldown_until(
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


def _independent_close_reason(
    *,
    settings: AATSSettings,
    score: float,
    close_threshold: float,
    expected_net_edge_bps: float,
    liquidity_quality_score: float | None,
    execution_health_state: IndependentExecutionHealthState | None,
    age_seconds: float | None,
) -> str | None:
    if expected_net_edge_bps <= float(settings.strategy_hedge_independent_failed_thesis_net_edge_bps):
        return "failed_thesis"
    if age_seconds is not None and age_seconds >= float(settings.strategy_hedge_independent_max_thesis_age_seconds):
        return "stale_thesis"
    if (
        bool(settings.strategy_hedge_independent_execution_health_de_risk_enabled)
        and execution_health_state in {"degraded", "blocked"}
    ):
        return "execution_health_degraded"
    if (
        bool(settings.strategy_hedge_independent_liquidity_de_risk_enabled)
        and liquidity_quality_score is not None
        and liquidity_quality_score + 1e-9 < float(settings.strategy_hedge_independent_min_liquidity_quality)
    ):
        return "liquidity_degraded"
    if (
        expected_net_edge_bps <= float(settings.strategy_hedge_independent_de_risk_net_edge_bps)
        or score + 1e-9 < close_threshold
    ):
        return "weak_edge_de_risk"
    return None


def _independent_close_reason_code(*, leg: IndependentLeg, close_reason: str) -> str:
    return {
        "failed_thesis": f"independent_{leg}_book_close_failed_thesis",
        "stale_thesis": f"independent_{leg}_book_close_stale_thesis",
        "execution_health_degraded": f"independent_{leg}_book_de_risk_execution_health_degraded",
        "liquidity_degraded": f"independent_{leg}_book_de_risk_liquidity_degraded",
        "weak_edge_de_risk": f"independent_{leg}_book_de_risk_weak_edge",
    }.get(close_reason, f"independent_{leg}_book_de_risk")


def _independent_de_risk_target_qty(
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


def _independent_close_reason_summary(
    *,
    long_book: IndependentBookEvaluation,
    short_book: IndependentBookEvaluation,
) -> str | None:
    reasons = [
        book.close_reason
        for book in (long_book, short_book)
        if book.close_reason is not None
    ]
    unique = list(dict.fromkeys(reasons))
    if not unique:
        return None
    if len(unique) == 1:
        return unique[0]
    return "mixed"


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
    history = [
        float(item)
        for item in recent_score_history
        if item is not None
    ]
    min_confirm_ticks = max(int(settings.strategy_hedge_independent_min_confirm_ticks), 1)
    if history:
        window_size = max(min_confirm_ticks, 2)
        window = [*history[-window_size:], float(score)]
        support_count = sum(1 for item in window if item + 1e-9 >= entry_threshold)
        min_score = min(window)
        mean_score = sum(window) / max(len(window), 1)
        max_drawdown_bps = max(float(score) - min_score, 0.0) * 100.0
        stable = (
            support_count >= min_confirm_ticks
            and max_drawdown_bps <= float(settings.strategy_hedge_independent_min_score_stability_bps) + 1e-9
        )
        return ScoreStabilityMetrics(
            support_count=support_count,
            min_score=min_score,
            mean_score=mean_score,
            max_drawdown_bps=max_drawdown_bps,
            stable=stable,
            source="recent_target_history",
        )
    support_count = _independent_signal_confirmation_count(
        leg=leg,
        baseline=baseline,
        ai_assessment=ai_assessment,
    )
    return ScoreStabilityMetrics(
        support_count=support_count,
        min_score=float(score),
        mean_score=float(score),
        max_drawdown_bps=0.0,
        stable=support_count >= min_confirm_ticks,
        source="current_signal_confirmation",
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
    fallback_signal_edge_bps: float,
    fallback_expected_cost_bps: float,
    fallback_expected_net_edge_bps: float,
    expectancy_resolver: IndependentBookExpectancyResolver | None,
) -> IndependentBookExpectancy:
    if expectancy_resolver is not None:
        try:
            return expectancy_resolver(
                settings=settings,
                context=context,
                baseline=baseline,
                ai_assessment=ai_assessment,
                leg=leg,
                trade_cost_service=trade_cost_service,
            )
        except Exception:
            return IndependentBookExpectancy(
                leg=leg,
                expected_signal_edge_bps=max(float(fallback_signal_edge_bps), 0.0),
                expected_slippage_bps=_independent_expected_slippage_bps(settings=settings),
                expected_cost_bps=max(float(fallback_expected_cost_bps), 0.0),
                expected_net_edge_bps=float(fallback_expected_net_edge_bps),
                resolution_failed=True,
            )
    try:
        return _compute_independent_book_expectancy(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            runtime_margin_mode=runtime_margin_mode,
            leg=leg,
            trade_cost_service=trade_cost_service,
        )
    except Exception:
        return IndependentBookExpectancy(
            leg=leg,
            expected_signal_edge_bps=max(float(fallback_signal_edge_bps), 0.0),
            expected_slippage_bps=_independent_expected_slippage_bps(settings=settings),
            expected_cost_bps=max(float(fallback_expected_cost_bps), 0.0),
            expected_net_edge_bps=float(fallback_expected_net_edge_bps),
            resolution_failed=True,
        )


def _compute_independent_book_expectancy(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
    runtime_margin_mode: str,
    leg: IndependentLeg,
    trade_cost_service: TradeCostService,
) -> IndependentBookExpectancy:
    expected_signal_edge_bps = _independent_signal_edge_bps(
        settings=settings,
        baseline=baseline,
        ai_assessment=ai_assessment,
        leg=leg,
    )
    expected_slippage_bps = _independent_expected_slippage_bps(settings=settings)
    estimate = trade_cost_service.estimate_single_leg_entry(
        model_name=f"independent_{leg}_book",
        symbol=context.symbol,
        product_type=context.product_type,
        margin_mode=runtime_margin_mode,
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
    return IndependentBookExpectancy(
        leg=leg,
        expected_signal_edge_bps=expected_signal_edge_bps,
        expected_slippage_bps=expected_slippage_bps,
        expected_cost_bps=expected_cost_bps,
        expected_net_edge_bps=expected_net_edge_bps,
    )


def _independent_signal_edge_bps(
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


def _independent_expected_slippage_bps(*, settings: AATSSettings) -> float:
    return max(float(settings.max_slippage_tolerance_bps), 0.0) * max(
        float(settings.strategy_expected_slippage_bps_fraction),
        0.0,
    )


def _independent_open_gate(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    leg: IndependentLeg,
    expected_cost_bps: float,
    expected_net_edge_bps: float,
) -> dict[str, object]:
    blocked_reasons: list[str] = []
    required_safe_net_edge_bps = _required_safe_net_edge_bps(settings=settings)
    weak_edge_report_only = False
    if expected_net_edge_bps < required_safe_net_edge_bps:
        if settings.strategy_hedge_independent_weak_edge_execution_mode == "block":
            blocked_reasons.append(f"independent_{leg}_book_expected_net_edge_below_safe_threshold")
        else:
            weak_edge_report_only = True
    max_acceptable_cost_bps = float(settings.strategy_hedge_independent_max_acceptable_cost_bps)
    if max_acceptable_cost_bps > 0.0 and expected_cost_bps > max_acceptable_cost_bps:
        blocked_reasons.append(f"independent_{leg}_book_expected_cost_above_max_acceptable")
    if _independent_post_close_cooldown_active(settings=settings, context=context, leg=leg):
        blocked_reasons.append(f"independent_{leg}_book_post_close_cooldown_active")
    if _independent_low_edge_cooldown_active(settings=settings, context=context, leg=leg):
        blocked_reasons.append(f"independent_{leg}_book_low_edge_cooldown_active")
    if _independent_performance_degraded(settings=settings, context=context, leg=leg):
        fee_drag_ratio = float(_leg_health_value(context, leg, "recent_fee_drag_ratio") or 0.0)
        churn_ratio = float(_leg_health_value(context, leg, "recent_churn_ratio") or 0.0)
        if fee_drag_ratio > settings.strategy_max_fee_drag_ratio:
            blocked_reasons.append(f"independent_{leg}_book_fee_drag_guard_active")
        if churn_ratio > settings.strategy_max_churn_ratio:
            blocked_reasons.append(f"independent_{leg}_book_churn_guard_active")
    if _independent_trial_guard_active(settings=settings, context=context, leg=leg):
        blocked_reasons.append(f"independent_{leg}_book_trial_guard_active")
    return {
        "blocked_reasons": blocked_reasons,
        "weak_edge_report_only": weak_edge_report_only,
    }


def _independent_min_hold_remaining_seconds(
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


def _independent_rebalance_remaining_seconds(
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


def _independent_post_close_cooldown_active(
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


def _independent_low_edge_cooldown_active(
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


def _independent_performance_degraded(
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


def _independent_trial_guard_active(
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
    recent_net_realized_pnl = to_decimal(_leg_health_value(context, leg, "recent_net_realized_pnl") or Decimal("0"))
    recent_win_rate = float(_leg_health_value(context, leg, "recent_win_rate") or 0.0)
    return recent_net_realized_pnl < -EPSILON_DECIMAL_12 and recent_win_rate < 0.5


def _leg_health_value(context: DecisionContext, leg: IndependentLeg, key: str) -> object | None:
    payload = context.leg_strategy_health.get(leg)
    if not isinstance(payload, dict):
        return None
    return payload.get(key)


def _leg_health_datetime(context: DecisionContext, leg: IndependentLeg, key: str):
    value = _leg_health_value(context, leg, key)
    return value if hasattr(value, "isoformat") else None


def _required_safe_net_edge_bps(*, settings: AATSSettings) -> float:
    return (
        max(float(settings.strategy_hedge_independent_min_safe_net_edge_bps), 0.0)
        + max(float(settings.strategy_hedge_independent_expected_slippage_buffer_bps), 0.0)
        + max(float(settings.strategy_hedge_independent_expected_execution_buffer_bps), 0.0)
    )


def _candidate_confidence(score: float) -> float:
    return min(0.95, 0.30 + max(score, 0.0) * 0.55)


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


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))
