from __future__ import annotations

from aats.schemas.decision import DecisionContext
from aats.schemas.strategy_runtime import (
    StrategyAdaptiveThresholdSnapshot,
    StrategyBookRuntimeState,
    StrategyIndependentLegHealthSummary,
)
from aats.services.strategy_engines.families.independent_models import IndependentBookRuntimeState

from .lifecycle import cooldown_until, last_transition_at
from .adaptive import IndependentAdaptiveSnapshot
from .health import IndependentLegHealthSnapshot
from .models import IndependentBookDecision, IndependentLeg


def runtime_state_from_decision(
    *,
    context: DecisionContext,
    decision: IndependentBookDecision,
    threshold_snapshot: IndependentAdaptiveSnapshot | None = None,
    health_snapshot: IndependentLegHealthSnapshot | None = None,
) -> StrategyBookRuntimeState:
    thesis_started_at = (
        context.current_long_leg_opened_at
        if decision.leg == "long"
        else context.current_short_leg_opened_at
    )
    transition_at = last_transition_at(context=context, leg=decision.leg)
    transition_reason = (
        decision.last_transition_reason
        or decision.close_reason
        or (None if decision.execution_policy is None else decision.execution_policy.policy_reason)
        or decision.book_action
    )
    cooldown = cooldown_until(
        context=context,
        min_hold_remaining_seconds=decision.min_hold_remaining_seconds,
        rebalance_cooldown_remaining_seconds=decision.rebalance_cooldown_remaining_seconds,
    )
    effective_cooldown = decision.cooldown_until or cooldown
    return StrategyBookRuntimeState(
        leg=decision.leg,
        execution_chain_id=_execution_chain_id(
            decision_id=context.decision_id,
            leg=decision.leg,
            book_action=decision.book_action,
            close_reason=decision.close_reason,
        ),
        current_qty=decision.current_qty,
        target_qty=decision.target_qty,
        state=decision.state,
        score=decision.score,
        score_raw=decision.score_raw,
        score_adjusted=decision.score if decision.score_adjusted is None else decision.score_adjusted,
        size_multiplier=(
            None if decision.sizing is None else float(decision.sizing.size_multiplier)
        ),
        capital_multiplier=(
            None if decision.sizing is None else float(decision.sizing.capital_multiplier)
        ),
        book_state=decision.book_state,
        holding_phase=decision.holding_phase,
        health_state=decision.health_state,
        eligibility_state=(
            None
            if decision.eligibility is None
            else "eligible"
            if decision.eligibility.eligible
            else "blocked"
        ),
        book_action=decision.book_action,
        close_reason=decision.close_reason,
        policy_reason=decision.policy_reason,
        thesis_started_at=thesis_started_at,
        thesis_age_seconds=decision.thesis_age_seconds,
        current_scale_in_count=(
            int(decision.current_scale_in_count)
            if decision.state_snapshot is None
            else int(decision.state_snapshot.current_scale_in_count)
        ),
        current_de_risk_count=(
            int(decision.current_de_risk_count)
            if decision.state_snapshot is None
            else int(decision.state_snapshot.current_de_risk_count)
        ),
        prior_book_state=(
            decision.prior_book_state
            if decision.state_snapshot is None
            else decision.state_snapshot.prior_book_state
        ),
        last_transition_at=decision.last_transition_at or transition_at,
        last_transition_reason=transition_reason,
        suspended_until=(
            decision.suspended_until
            if decision.suspended_until is not None
            else effective_cooldown
            if health_snapshot is not None and bool(health_snapshot.suspended)
            else None
        ),
        state_version=(
            max(int(decision.state_version or 1), 1)
            if decision.state_snapshot is None
            else int(decision.state_snapshot.state_version)
        ),
        expected_signal_edge_bps=(
            None if decision.expectancy is None else decision.expectancy.expected_signal_edge_bps
        ),
        expected_cost_bps=(
            None if decision.expectancy is None else decision.expectancy.expected_cost_bps
        ),
        expected_net_edge_bps=(
            None if decision.expectancy is None else decision.expectancy.expected_net_edge_bps
        ),
        liquidity_quality_score=decision.liquidity_quality_score,
        execution_health_state=decision.execution_health_state,
        cooldown_until=effective_cooldown,
        min_hold_remaining_seconds=decision.min_hold_remaining_seconds,
        rebalance_cooldown_remaining_seconds=decision.rebalance_cooldown_remaining_seconds,
        execution_policy_urgency=(
            None if decision.execution_policy is None else decision.execution_policy.urgency
        ),
        edge_strength=(
            None if decision.execution_policy is None else decision.execution_policy.edge_strength
        ),
        transition_valid=(
            True
            if decision.state_snapshot is None
            else bool(decision.state_snapshot.transition_valid)
        ),
        transition_violation_reason=(
            None
            if decision.state_snapshot is None
            else decision.state_snapshot.transition_violation_reason
        ),
        threshold_snapshot=(
            None
            if threshold_snapshot is None
            else StrategyAdaptiveThresholdSnapshot.model_validate(
                {
                    "leg": threshold_snapshot.leg,
                    "shadow_only": threshold_snapshot.shadow_only,
                    "rollout_enabled": threshold_snapshot.rollout_enabled,
                    "live_applied": threshold_snapshot.live_applied,
                    "health_enforcement_enabled": threshold_snapshot.health_enforcement_enabled,
                    "size_down_entry_enabled": threshold_snapshot.size_down_entry_enabled,
                    "long_short_asymmetry_enabled": threshold_snapshot.long_short_asymmetry_enabled,
                    "entry_threshold": threshold_snapshot.entry_threshold,
                    "close_threshold": threshold_snapshot.close_threshold,
                    "scale_in_threshold": threshold_snapshot.scale_in_threshold,
                    "thesis_age_seconds": threshold_snapshot.thesis_age_seconds,
                    "de_risk_net_edge_bps": threshold_snapshot.de_risk_net_edge_bps,
                    "adaptive_entry_threshold": threshold_snapshot.adaptive_entry_threshold,
                    "adaptive_close_threshold": threshold_snapshot.adaptive_close_threshold,
                    "adaptive_scale_in_threshold": threshold_snapshot.adaptive_scale_in_threshold,
                    "adaptive_thesis_age_seconds": threshold_snapshot.adaptive_thesis_age_seconds,
                    "adaptive_de_risk_net_edge_bps": threshold_snapshot.adaptive_de_risk_net_edge_bps,
                    "effective_entry_threshold": threshold_snapshot.effective_entry_threshold,
                    "effective_close_threshold": threshold_snapshot.effective_close_threshold,
                    "effective_scale_in_threshold": threshold_snapshot.effective_scale_in_threshold,
                    "effective_thesis_age_seconds": threshold_snapshot.effective_thesis_age_seconds,
                    "effective_de_risk_net_edge_bps": threshold_snapshot.effective_de_risk_net_edge_bps,
                    "capital_multiplier": threshold_snapshot.capital_multiplier,
                    "confidence_multiplier": threshold_snapshot.confidence_multiplier,
                    "volatility_multiplier": threshold_snapshot.volatility_multiplier,
                    "liquidity_multiplier": threshold_snapshot.liquidity_multiplier,
                    "health_multiplier": threshold_snapshot.health_multiplier,
                    "direction_bias_multiplier": threshold_snapshot.direction_bias_multiplier,
                    "reason_codes": list(threshold_snapshot.reason_codes),
                }
            )
        ),
        leg_health_summary=(
            None
            if health_snapshot is None
            else StrategyIndependentLegHealthSummary.model_validate(
                {
                    "leg": health_snapshot.leg,
                    "health_state": health_snapshot.health_state,
                    "halt_openings": health_snapshot.halt_openings,
                    "only_reduce": health_snapshot.only_reduce,
                    "suspended": health_snapshot.suspended,
                    "warnings": list(health_snapshot.warnings),
                    "blockers": list(health_snapshot.blockers),
                }
            )
        ),
        reason_codes=list(decision.reason_codes),
        blocked_reasons=list(decision.blocked_reasons),
    )


def legacy_runtime_state_snapshot(
    *,
    context: DecisionContext,
    decision: IndependentBookDecision,
) -> IndependentBookRuntimeState:
    runtime_state = runtime_state_from_decision(
        context=context,
        decision=decision,
        threshold_snapshot=decision.threshold_snapshot,
        health_snapshot=decision.health_snapshot,
    )
    return IndependentBookRuntimeState(
        side=runtime_state.leg,
        current_qty=runtime_state.current_qty,
        target_qty=runtime_state.target_qty,
        state=runtime_state.state,
        execution_chain_id=runtime_state.execution_chain_id,
        thesis_started_at=runtime_state.thesis_started_at,
        thesis_age_seconds=runtime_state.thesis_age_seconds,
        current_scale_in_count=runtime_state.current_scale_in_count,
        current_de_risk_count=runtime_state.current_de_risk_count,
        prior_book_state=runtime_state.prior_book_state,
        last_transition_at=runtime_state.last_transition_at,
        last_transition_reason=runtime_state.last_transition_reason,
        suspended_until=runtime_state.suspended_until,
        eligibility_state=runtime_state.eligibility_state,
        state_version=runtime_state.state_version,
        expected_signal_edge_bps=runtime_state.expected_signal_edge_bps,
        expected_cost_bps=runtime_state.expected_cost_bps,
        expected_net_edge_bps=runtime_state.expected_net_edge_bps,
        liquidity_quality_score=runtime_state.liquidity_quality_score,
        execution_health_state=runtime_state.execution_health_state,
        cooldown_until=runtime_state.cooldown_until,
        min_hold_remaining_seconds=runtime_state.min_hold_remaining_seconds,
        rebalance_cooldown_remaining_seconds=runtime_state.rebalance_cooldown_remaining_seconds,
        score=runtime_state.score,
        reason_codes=tuple(runtime_state.reason_codes),
        blocked_reasons=tuple(runtime_state.blocked_reasons),
        book_action=runtime_state.book_action,
        close_reason=runtime_state.close_reason,
        policy_reason=runtime_state.policy_reason,
        execution_policy_urgency=runtime_state.execution_policy_urgency,
        edge_strength=runtime_state.edge_strength,
        transition_valid=runtime_state.transition_valid,
        transition_violation_reason=runtime_state.transition_violation_reason,
    )


def _execution_chain_id(
    *,
    decision_id: str,
    leg: IndependentLeg,
    book_action: str | None,
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
