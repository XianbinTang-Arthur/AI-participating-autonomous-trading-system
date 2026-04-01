from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Sequence

from aats.bootstrap.settings import AATSSettings
from aats.schemas.decision import AIMarketAssessment, BaselineAssessment, DecisionContext, HedgeOverlayDecision
from aats.schemas.strategy_runtime import StrategyBookRuntimeState, StrategyLegIntent
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal

from .adaptive import threshold_snapshot
from .diagnostics import legacy_runtime_state_snapshot
from .execution_policy import resolve_execution_policy
from .gates import evaluate_entry_quality_gate, evaluate_open_eligibility, required_safe_net_edge_bps
from .health import aggregate_family_health, evaluate_leg_health
from .lifecycle import (
    close_reason_code,
    compute_de_risk_target_qty,
    compute_thesis_age_seconds,
    min_hold_remaining_seconds,
    rebalance_remaining_seconds,
    determine_close_reason,
)
from .models import (
    IndependentBookAction,
    IndependentBookDecision,
    IndependentBookExpectancy,
    IndependentBookScorer,
    IndependentEligibilityOutcome,
    IndependentFamilyEvaluation,
    IndependentLeg,
)
from .replay import replay_snapshot_from_decision
from .sizing import (
    build_sizing_outcome,
    compute_entry_target_qty,
    compute_scale_in_target_qty,
    resolve_entry_size_multiplier,
)
from .scoring import compute_raw_book_score, compute_score_stability
from .state_machine import derive_book_state, derive_holding_phase, snapshot_from_decision, transition_book_state


def evaluate_independent_book(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
    leg: IndependentLeg,
    expectancy: IndependentBookExpectancy | None,
    directional_leg_target_qty: Decimal,
    scorer: IndependentBookScorer | None,
    prior_runtime_state: StrategyBookRuntimeState | None = None,
    recent_score_history: Sequence[float] = (),
) -> IndependentBookDecision:
    current_qty = (
        to_decimal(context.current_long_position_qty)
        if leg == "long"
        else to_decimal(context.current_short_position_qty)
    )
    score = (
        scorer(leg=leg, baseline=baseline, ai_assessment=ai_assessment)
        if scorer is not None
        else compute_raw_book_score(
            settings=settings,
            leg=leg,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )
    )
    preview_decision = _evaluate_book_core(
        settings=settings,
        context=context,
        baseline=baseline,
        ai_assessment=ai_assessment,
        leg=leg,
        expectancy=expectancy,
        directional_leg_target_qty=directional_leg_target_qty,
        score=score,
        current_qty=current_qty,
        prior_runtime_state=prior_runtime_state,
        entry_threshold=_entry_threshold(settings=settings, leg=leg),
        close_threshold=_close_threshold(settings=settings, leg=leg),
        scale_threshold=_scale_in_threshold(settings=settings, leg=leg),
        recent_score_history=recent_score_history,
    )
    shadow_decision = _complete_decision(
        settings=settings,
        context=context,
        baseline=baseline,
        ai_assessment=ai_assessment,
        decision=preview_decision,
        expectancy=expectancy,
        live_applied=False,
    )
    if not settings.strategy_hedge_independent_adaptive_rollout_enabled:
        return shadow_decision

    live_seed_threshold = threshold_snapshot(
        settings=settings,
        leg=leg,
        baseline=baseline,
        ai_assessment=ai_assessment,
        context=context,
        decision=shadow_decision,
        health_snapshot=shadow_decision.health_snapshot,
        live_applied=True,
    )
    entry_multiplier, entry_size_reason_codes = resolve_entry_size_multiplier(
        settings=settings,
        leg=leg,
        threshold_snapshot=live_seed_threshold,
    )
    live_preview = _evaluate_book_core(
        settings=settings,
        context=context,
        baseline=baseline,
        ai_assessment=ai_assessment,
        leg=leg,
        expectancy=expectancy,
        directional_leg_target_qty=directional_leg_target_qty,
        score=score,
        current_qty=current_qty,
        prior_runtime_state=prior_runtime_state,
        entry_threshold=_effective_threshold_value(
            live_seed_threshold.effective_entry_threshold,
            _entry_threshold(settings=settings, leg=leg),
        ),
        close_threshold=_effective_threshold_value(
            live_seed_threshold.effective_close_threshold,
            _close_threshold(settings=settings, leg=leg),
        ),
        scale_threshold=_effective_threshold_value(
            live_seed_threshold.effective_scale_in_threshold,
            _scale_in_threshold(settings=settings, leg=leg),
        ),
        recent_score_history=recent_score_history,
        max_thesis_age_seconds=live_seed_threshold.effective_thesis_age_seconds,
        de_risk_net_edge_bps=live_seed_threshold.effective_de_risk_net_edge_bps,
        entry_size_multiplier=entry_multiplier,
        entry_size_reason_codes=entry_size_reason_codes,
    )
    if settings.strategy_hedge_independent_health_enforcement_enabled:
        live_preview = _apply_health_enforcement(
            settings=settings,
            leg=leg,
            decision=live_preview,
            directional_leg_target_qty=directional_leg_target_qty,
        )
    return _complete_decision(
        settings=settings,
        context=context,
        baseline=baseline,
        ai_assessment=ai_assessment,
        decision=live_preview,
        expectancy=expectancy,
        live_applied=True,
    )


def _evaluate_book_core(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
    leg: IndependentLeg,
    expectancy: IndependentBookExpectancy | None,
    directional_leg_target_qty: Decimal,
    score: float,
    current_qty: Decimal,
    prior_runtime_state: StrategyBookRuntimeState | None,
    entry_threshold: float,
    close_threshold: float,
    scale_threshold: float,
    recent_score_history: Sequence[float],
    max_thesis_age_seconds: float | None = None,
    de_risk_net_edge_bps: float | None = None,
    entry_size_multiplier: Decimal = Decimal("1"),
    entry_size_reason_codes: Sequence[str] = (),
) -> IndependentBookDecision:
    target_qty = current_qty
    raw_base_target_qty = compute_entry_target_qty(
        settings=settings,
        directional_leg_target_qty=directional_leg_target_qty,
    )
    effective_base_target_qty = max(
        Decimal("0"),
        to_decimal(raw_base_target_qty) * max(to_decimal(entry_size_multiplier), Decimal("0")),
    )
    reason_codes: list[str] = []
    blocked_reasons: list[str] = []
    state = "inactive"
    min_hold_seconds = 0.0
    rebalance_seconds = 0.0
    book_action: IndependentBookAction = "inactive"
    close_reason: str | None = None
    thesis_age_seconds = compute_thesis_age_seconds(
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
        expected_slippage_bps=_expectancy_slippage_bps(expectancy, settings=settings),
    )
    score_stability_metrics = compute_score_stability(
        settings=settings,
        leg=leg,
        score=score,
        entry_threshold=entry_threshold,
        baseline=baseline,
        ai_assessment=ai_assessment,
        recent_score_history=recent_score_history,
    )
    execution_health_state = _execution_health_state(
        settings=settings,
        context=context,
        leg=leg,
    )
    eligibility: IndependentEligibilityOutcome | None = None
    prior_book_state = _runtime_prior_book_state(prior_runtime_state=prior_runtime_state)
    prior_scale_in_count = _runtime_counter(prior_runtime_state=prior_runtime_state, field_name="current_scale_in_count")
    prior_de_risk_count = _runtime_counter(prior_runtime_state=prior_runtime_state, field_name="current_de_risk_count")
    prior_state_version = _runtime_state_version(prior_runtime_state=prior_runtime_state)
    prior_last_transition_reason = _runtime_text(prior_runtime_state=prior_runtime_state, field_name="last_transition_reason")
    prior_last_transition_at = None if prior_runtime_state is None else prior_runtime_state.last_transition_at
    prior_suspended_until = None if prior_runtime_state is None else prior_runtime_state.suspended_until
    prior_cooldown_until = None if prior_runtime_state is None else prior_runtime_state.cooldown_until

    if current_qty <= EPSILON_DECIMAL_12:
        if score >= entry_threshold:
            reason_codes.append(f"independent_{leg}_book_signal_above_entry_threshold")
            if expectancy is None:
                blocked_reasons.append(f"independent_{leg}_book_expectancy_resolution_failed")
                eligibility = IndependentEligibilityOutcome(
                    eligible=False,
                    hard_block_reasons=tuple(blocked_reasons),
                )
            else:
                eligibility = evaluate_open_eligibility(
                    settings=settings,
                    context=context,
                    leg=leg,
                    expectancy=expectancy,
                )
                blocked_reasons.extend(eligibility.hard_block_reasons)
                weak_edge_report_only = bool(eligibility.warnings)
                reason_codes.extend(list(eligibility.warnings))
            _, quality_blocked_reasons = evaluate_entry_quality_gate(
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
            reason_codes.extend(list(entry_size_reason_codes))
            rebalance_seconds = rebalance_remaining_seconds(
                settings=settings,
                context=context,
                leg=leg,
                opening_or_expanding=True,
                desired_target_qty=effective_base_target_qty,
                current_qty=current_qty,
            )
            if rebalance_seconds > 0:
                blocked_reasons.append(f"independent_{leg}_book_rebalance_cooldown_active")
            if blocked_reasons:
                state = "blocked"
                book_action = "blocked"
                target_qty = Decimal("0")
            else:
                target_qty = effective_base_target_qty
                state = "opening"
                book_action = "open"
        else:
            reason_codes.append(f"independent_{leg}_book_signal_below_entry_threshold")
    else:
        state = "holding"
        book_action = "hold"
        close_reason = determine_close_reason(
            settings=settings,
            score=score,
            close_threshold=close_threshold,
            expected_net_edge_bps=(None if expectancy is None else _expectancy_net_edge_bps(expectancy)),
            liquidity_quality_score=liquidity_quality_score,
            execution_health_state=execution_health_state,
            age_seconds=thesis_age_seconds,
            max_thesis_age_seconds=max_thesis_age_seconds,
            de_risk_net_edge_bps=de_risk_net_edge_bps,
        )
        if close_reason is not None:
            reason_codes.append(close_reason_code(leg=leg, close_reason=close_reason))
            min_hold_seconds = min_hold_remaining_seconds(
                settings=settings,
                context=context,
                leg=leg,
            )
            if min_hold_seconds > 0:
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
                target_qty = compute_de_risk_target_qty(
                    current_qty=current_qty,
                    directional_leg_target_qty=directional_leg_target_qty,
                )
                if target_qty + EPSILON_DECIMAL_12 < current_qty:
                    state = "closing"
                    book_action = "de_risk"
                else:
                    state = "holding"
                    book_action = "hold"
        elif score >= scale_threshold and effective_base_target_qty > current_qty + EPSILON_DECIMAL_12:
            reason_codes.append(f"independent_{leg}_book_signal_above_scale_in_threshold")
            if expectancy is None:
                blocked_reasons.append(f"independent_{leg}_book_expectancy_resolution_failed")
                eligibility = IndependentEligibilityOutcome(
                    eligible=False,
                    hard_block_reasons=tuple(blocked_reasons),
                )
            else:
                eligibility = evaluate_open_eligibility(
                    settings=settings,
                    context=context,
                    leg=leg,
                    expectancy=expectancy,
                )
                blocked_reasons.extend(eligibility.hard_block_reasons)
                weak_edge_report_only = bool(eligibility.warnings)
                reason_codes.extend(list(eligibility.warnings))
            _, quality_blocked_reasons = evaluate_entry_quality_gate(
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
            reason_codes.extend(list(entry_size_reason_codes))
            rebalance_seconds = rebalance_remaining_seconds(
                settings=settings,
                context=context,
                leg=leg,
                opening_or_expanding=True,
                desired_target_qty=effective_base_target_qty,
                current_qty=current_qty,
            )
            if rebalance_seconds > 0:
                blocked_reasons.append(f"independent_{leg}_book_rebalance_cooldown_active")
            if blocked_reasons:
                target_qty = current_qty
                state = "blocked"
                book_action = "blocked"
            else:
                target_qty = compute_scale_in_target_qty(
                    base_target_qty=effective_base_target_qty,
                    current_qty=current_qty,
                )
                state = "opening"
                book_action = "scale_in"
        elif score >= entry_threshold:
            reason_codes.append(f"independent_{leg}_book_hold_above_entry_threshold")
        elif score >= close_threshold:
            reason_codes.append(f"independent_{leg}_book_hold_above_close_threshold")
        else:
            reason_codes.append(f"independent_{leg}_book_hold_without_thesis_break")

    return IndependentBookDecision(
        leg=leg,
        expectancy=expectancy,
        score=score,
        score_raw=score,
        score_adjusted=score,
        current_qty=current_qty,
        target_qty=target_qty,
        state=state,
        book_state=None,
        holding_phase=None,
        health_state=execution_health_state,
        reason_codes=reason_codes,
        blocked_reasons=blocked_reasons,
        min_hold_remaining_seconds=min_hold_seconds,
        rebalance_cooldown_remaining_seconds=rebalance_seconds,
        book_action=book_action,
        close_reason=close_reason,
        thesis_age_seconds=thesis_age_seconds,
        weak_edge_report_only=weak_edge_report_only,
        liquidity_quality_score=liquidity_quality_score,
        score_stability_metrics=score_stability_metrics,
        execution_health_state=execution_health_state,
        policy_reason=None,
        eligibility=eligibility,
        sizing=build_sizing_outcome(
            book_action=book_action,
            current_qty=current_qty,
            target_qty=target_qty,
            base_target_qty=raw_base_target_qty,
            sizing_reason_codes=tuple(reason_codes),
        ),
        prior_book_state=prior_book_state,
        current_scale_in_count=prior_scale_in_count,
        current_de_risk_count=prior_de_risk_count,
        last_transition_reason=prior_last_transition_reason,
        last_transition_at=prior_last_transition_at,
        suspended_until=prior_suspended_until,
        cooldown_until=prior_cooldown_until,
        state_version=prior_state_version,
    )


def _apply_health_enforcement(
    *,
    settings: AATSSettings,
    leg: IndependentLeg,
    decision: IndependentBookDecision,
    directional_leg_target_qty: Decimal,
) -> IndependentBookDecision:
    current_qty = max(to_decimal(decision.current_qty), Decimal("0"))
    health_snapshot = evaluate_leg_health(decision=decision)
    blocked_reasons = list(decision.blocked_reasons)
    reason_codes = list(decision.reason_codes)
    target_qty = decision.target_qty
    state = decision.state
    book_action = decision.book_action
    close_reason = decision.close_reason
    changed = False

    if health_snapshot.halt_openings and current_qty <= EPSILON_DECIMAL_12 and book_action == "open":
        blocked_reasons.append(f"independent_{leg}_book_execution_health_not_ok")
        target_qty = Decimal("0")
        state = "blocked"
        book_action = "blocked"
        changed = True
    elif health_snapshot.only_reduce and current_qty > EPSILON_DECIMAL_12 and book_action == "scale_in":
        blocked_reasons.append(f"independent_{leg}_book_execution_health_not_ok")
        target_qty = current_qty
        state = "blocked"
        book_action = "blocked"
        changed = True
    elif health_snapshot.only_reduce and current_qty > EPSILON_DECIMAL_12 and book_action == "hold":
        reduced_target = compute_de_risk_target_qty(
            current_qty=current_qty,
            directional_leg_target_qty=directional_leg_target_qty,
        )
        if reduced_target + EPSILON_DECIMAL_12 < current_qty:
            close_reason = "execution_health_degraded"
            reason_codes.append(close_reason_code(leg=leg, close_reason=close_reason))
            target_qty = reduced_target
            state = "closing"
            book_action = "de_risk"
            changed = True

    if not changed:
        return decision
    return replace(
        decision,
        target_qty=target_qty,
        state=state,
        book_action=book_action,
        close_reason=close_reason,
        blocked_reasons=list(dict.fromkeys(blocked_reasons)),
        reason_codes=list(dict.fromkeys(reason_codes)),
        sizing=build_sizing_outcome(
            book_action=book_action,
            current_qty=current_qty,
            target_qty=target_qty,
            base_target_qty=(
                decision.sizing.base_target_qty
                if decision.sizing is not None
                else compute_entry_target_qty(
                    settings=settings,
                    directional_leg_target_qty=directional_leg_target_qty,
                )
            ),
            sizing_reason_codes=tuple(dict.fromkeys(reason_codes)),
        ),
    )


def _complete_decision(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
    decision: IndependentBookDecision,
    expectancy: IndependentBookExpectancy | None,
    live_applied: bool,
) -> IndependentBookDecision:
    initial_state_snapshot = snapshot_from_decision(decision=decision)
    derived_book_state = derive_book_state(snapshot=initial_state_snapshot)
    derived_holding_phase = derive_holding_phase(
        snapshot=initial_state_snapshot,
        book_state=derived_book_state,
    )
    prior_book_state = _replay_prior_book_state(decision=decision)
    transition = (
        None
        if prior_book_state is None
        else transition_book_state(
            prior_state=prior_book_state,
            snapshot=initial_state_snapshot,
        )
    )
    next_scale_in_count = int(decision.current_scale_in_count or 0) + (
        1 if decision.book_action == "scale_in" else 0
    )
    next_de_risk_count = int(decision.current_de_risk_count or 0) + (
        1 if decision.book_action == "de_risk" else 0
    )
    transition_changed = (
        prior_book_state is not None
        and prior_book_state != derived_book_state
    ) or decision.book_action in {"open", "scale_in", "de_risk", "close_failed_thesis", "close_stale_thesis", "blocked"}
    next_state_version = max(int(decision.state_version or 1), 1) + (1 if transition_changed else 0)
    next_transition_reason = (
        decision.close_reason
        or decision.book_action
        if transition_changed
        else decision.last_transition_reason
    )
    stateful_decision = replace(
        decision,
        book_state=derived_book_state,
        holding_phase=derived_holding_phase,
        prior_book_state=prior_book_state,
        current_scale_in_count=next_scale_in_count,
        current_de_risk_count=next_de_risk_count,
        last_transition_reason=next_transition_reason,
        last_transition_at=(context.as_of_ts if transition_changed else decision.last_transition_at),
        state_version=next_state_version,
    )
    health_snapshot = evaluate_leg_health(decision=stateful_decision)
    final_decision = replace(
        stateful_decision,
        health_state=health_snapshot.health_state,
    )
    policy = resolve_execution_policy(
        settings=settings,
        book=final_decision,
        expectancy_cost_bps=_expectancy_cost_bps(expectancy),
        expectancy_net_edge_bps=_expectancy_net_edge_bps(expectancy),
        expectancy_slippage_bps=_expectancy_slippage_bps(expectancy, settings=settings),
        required_safe_net_edge_bps=required_safe_net_edge_bps(settings=settings),
    )
    threshold = threshold_snapshot(
        settings=settings,
        leg=final_decision.leg,
        baseline=baseline,
        ai_assessment=ai_assessment,
        context=context,
        decision=final_decision,
        health_snapshot=health_snapshot,
        live_applied=live_applied,
    )
    decided_state_snapshot = snapshot_from_decision(decision=final_decision)
    if transition is not None:
        decided_state_snapshot = replace(
            decided_state_snapshot,
            prior_book_state=transition.prior_state,
            transition_valid=transition.valid_transition,
            transition_violation_reason=transition.violation_reason,
            last_transition_reason=transition.transition_reason or decided_state_snapshot.last_transition_reason,
        )
    replay_snapshot = replay_snapshot_from_decision(
        decision=final_decision,
        threshold_snapshot=threshold,
        state_snapshot=decided_state_snapshot,
        health_snapshot=health_snapshot,
        prior_book_state=prior_book_state,
        prior_state_source=(
            None
            if prior_book_state is None
            else "runtime_state"
            if decision.prior_book_state is not None
            else "heuristic_inference"
        ),
    )
    return replace(
        final_decision,
        execution_policy=policy,
        policy_reason=None if policy is None else policy.policy_reason,
        threshold_snapshot=threshold,
        state_snapshot=decided_state_snapshot,
        health_snapshot=health_snapshot,
        replay_snapshot=replay_snapshot,
    )


def _entry_threshold(*, settings: AATSSettings, leg: IndependentLeg) -> float:
    return (
        float(settings.strategy_hedge_independent_long_entry_threshold)
        if leg == "long"
        else float(settings.strategy_hedge_independent_short_entry_threshold)
    )


def _close_threshold(*, settings: AATSSettings, leg: IndependentLeg) -> float:
    return (
        float(settings.strategy_hedge_independent_long_close_threshold)
        if leg == "long"
        else float(settings.strategy_hedge_independent_short_close_threshold)
    )


def _scale_in_threshold(*, settings: AATSSettings, leg: IndependentLeg) -> float:
    return (
        float(settings.strategy_hedge_independent_long_scale_in_threshold)
        if leg == "long"
        else float(settings.strategy_hedge_independent_short_scale_in_threshold)
    )


def _effective_threshold_value(value: float | None, fallback: float) -> float:
    return fallback if value is None else float(value)


def build_independent_family_candidate(
    *,
    final_target_qty: Decimal,
    legs: list[StrategyLegIntent],
    overlay_decision: HedgeOverlayDecision,
    long_book: IndependentBookDecision,
    short_book: IndependentBookDecision,
    context: DecisionContext | None = None,
) -> IndependentFamilyEvaluation:
    runtime_states = ()
    family_health = aggregate_family_health(
        long_leg=evaluate_leg_health(decision=long_book),
        short_leg=evaluate_leg_health(decision=short_book),
    )
    if context is not None:
        runtime_states = (
            legacy_runtime_state_snapshot(context=context, decision=long_book),
            legacy_runtime_state_snapshot(context=context, decision=short_book),
        )
    return IndependentFamilyEvaluation(
        final_target_qty=final_target_qty,
        legs=legs,
        overlay_decision=overlay_decision,
        long_book=long_book,
        short_book=short_book,
        book_runtime_states=runtime_states,
        family_health=family_health,
    )


def _expectancy_slippage_bps(
    expectancy: IndependentBookExpectancy | None,
    *,
    settings: AATSSettings,
) -> float:
    return _expected_slippage_bps(settings=settings) if expectancy is None else expectancy.expected_slippage_bps


def _expectancy_cost_bps(expectancy: IndependentBookExpectancy | None) -> float:
    return 0.0 if expectancy is None else expectancy.expected_cost_bps


def _expectancy_net_edge_bps(expectancy: IndependentBookExpectancy | None) -> float:
    return 0.0 if expectancy is None else expectancy.expected_net_edge_bps


def _expected_slippage_bps(*, settings: AATSSettings) -> float:
    return max(float(settings.max_slippage_tolerance_bps), 0.0) * max(
        float(settings.strategy_expected_slippage_bps_fraction),
        0.0,
    )


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


def _execution_health_state(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    leg: IndependentLeg,
) -> str:
    if _trial_guard_active(settings=settings, context=context, leg=leg):
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


def _trial_guard_active(
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


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def _replay_prior_book_state(*, decision: IndependentBookDecision) -> str | None:
    if decision.prior_book_state is not None:
        return decision.prior_book_state
    if decision.book_action == "open":
        return "flat"
    if decision.book_action == "scale_in":
        return "holding" if decision.current_qty > EPSILON_DECIMAL_12 else "probing"
    if decision.book_action == "de_risk":
        return "holding"
    if decision.book_action in {"close_failed_thesis", "close_stale_thesis"}:
        return "holding" if decision.current_qty > EPSILON_DECIMAL_12 else "probing"
    if decision.book_action == "blocked":
        if any("trial_guard" in reason for reason in decision.blocked_reasons):
            return "suspended"
        if decision.current_qty > EPSILON_DECIMAL_12:
            return "holding"
        return "cooldown"
    if decision.book_state is not None:
        return decision.book_state
    if decision.current_qty > EPSILON_DECIMAL_12:
        return "holding"
    return "flat"


def _runtime_prior_book_state(*, prior_runtime_state: StrategyBookRuntimeState | None) -> str | None:
    if prior_runtime_state is None:
        return None
    value = str(prior_runtime_state.book_state or "").strip()
    return None if not value else value


def _runtime_counter(*, prior_runtime_state: StrategyBookRuntimeState | None, field_name: str) -> int:
    if prior_runtime_state is None:
        return 0
    value = getattr(prior_runtime_state, field_name, 0)
    return max(int(value or 0), 0)


def _runtime_state_version(*, prior_runtime_state: StrategyBookRuntimeState | None) -> int:
    if prior_runtime_state is None:
        return 1
    return max(int(prior_runtime_state.state_version or 1), 1)


def _runtime_text(*, prior_runtime_state: StrategyBookRuntimeState | None, field_name: str) -> str | None:
    if prior_runtime_state is None:
        return None
    value = str(getattr(prior_runtime_state, field_name, "") or "").strip()
    return None if not value else value
