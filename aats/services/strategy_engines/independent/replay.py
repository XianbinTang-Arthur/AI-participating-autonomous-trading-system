from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Sequence

from aats.schemas.execution import OrderState
from aats.schemas.strategy_runtime import (
    PortfolioAllocationDecision,
    StrategyBookRuntimeState,
    StrategyExecutionBundle,
    StrategyLegIntent,
    StrategySleeveIntent,
)
from aats.services.portfolio_service.decimals import to_decimal

from .adaptive import IndependentAdaptiveSnapshot
from .health import IndependentLegHealthSnapshot
from .models import IndependentBookDecision
from .state_machine import IndependentStateSnapshot, transition_book_state


@dataclass(frozen=True, slots=True)
class IndependentReplayDecisionSnapshot:
    leg: str
    score: float
    state: str
    book_state: str | None
    holding_phase: str | None
    health_state: str | None
    book_action: str | None
    close_reason: str | None
    policy_reason: str | None
    prior_book_state: str | None = None
    transition_reconstructed: bool = False
    transition_source: str | None = None
    transition_reason: str | None = None
    transition_valid: bool = True
    transition_violation_reason: str | None = None
    threshold_snapshot: IndependentAdaptiveSnapshot | None = None
    state_snapshot: IndependentStateSnapshot | None = None
    health_snapshot: IndependentLegHealthSnapshot | None = None


@dataclass(frozen=True, slots=True)
class IndependentDecisionSnapshot:
    decision_id: str
    symbol: str
    leg: str
    book_state: str | None = None
    holding_phase: str | None = None
    health_state: str | None = None
    eligibility_state: str | None = None
    current_qty: Decimal | None = None
    target_qty: Decimal | None = None
    prior_book_state: str | None = None
    current_scale_in_count: int = 0
    current_de_risk_count: int = 0
    thesis_started_at: datetime | None = None
    thesis_age_seconds: float | None = None
    last_transition_at: datetime | None = None
    last_transition_reason: str | None = None
    suspended_until: datetime | None = None
    cooldown_until: datetime | None = None
    state_version: int = 1
    raw_score: float | None = None
    adjusted_score: float | None = None
    score_stability_metrics: dict[str, Any] | None = None
    expectancy_snapshot: dict[str, Any] | None = None
    eligibility_outcome: dict[str, Any] | None = None
    sizing_outcome: dict[str, Any] | None = None
    book_action: str | None = None
    close_reason: str | None = None
    transition_valid: bool = True
    transition_violation_reason: str | None = None
    execution_policy: dict[str, Any] | None = None
    threshold_snapshot: dict[str, Any] | None = None
    health_snapshot: dict[str, Any] | None = None
    replay_snapshot: dict[str, Any] | None = None
    reason_codes: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IndependentRecoverySnapshot:
    decision_id: str
    allocation_id: str | None
    symbol: str
    strategy_sleeve_id: str | None
    leg: str
    book_state: str | None
    holding_phase: str | None
    health_state: str | None
    current_qty: Decimal
    target_qty: Decimal
    expected_chain_ids: tuple[str, ...]
    active_execution_chain_ids: tuple[str, ...]
    unresolved_attempt_ids: tuple[str, ...]
    recovery_posture: str
    prior_book_state: str | None = None
    current_scale_in_count: int = 0
    current_de_risk_count: int = 0
    recovery_blockers: tuple[str, ...] = ()
    last_recovery_incident_at: datetime | None = None
    recovery_version: str = "independent_phase6_additive_v1"
    suspended_until: datetime | None = None
    cooldown_until: datetime | None = None
    state_version: int = 1
    threshold_snapshot: dict[str, Any] | None = None
    health_snapshot: dict[str, Any] | None = None
    replay_snapshot: dict[str, Any] | None = None
    decision_snapshot: IndependentDecisionSnapshot | None = None
    transition_valid: bool = True
    transition_violation_reason: str | None = None


def replay_snapshot_from_decision(
    *,
    decision: IndependentBookDecision,
    threshold_snapshot: IndependentAdaptiveSnapshot | None = None,
    state_snapshot: IndependentStateSnapshot | None = None,
    health_snapshot: IndependentLegHealthSnapshot | None = None,
    prior_book_state: str | None = None,
    prior_state_source: str | None = None,
) -> IndependentReplayDecisionSnapshot:
    transition = None
    if state_snapshot is not None and prior_book_state is not None:
        transition = transition_book_state(prior_state=prior_book_state, snapshot=state_snapshot)
    return IndependentReplayDecisionSnapshot(
        leg=decision.leg,
        score=decision.score,
        state=decision.state,
        book_state=decision.book_state if decision.book_state is not None else (None if transition is None else transition.next_state),
        holding_phase=decision.holding_phase if decision.holding_phase is not None else (None if transition is None else transition.holding_phase),
        health_state=decision.health_state,
        book_action=decision.book_action,
        close_reason=decision.close_reason,
        policy_reason=decision.policy_reason,
        prior_book_state=prior_book_state,
        transition_reconstructed=transition is not None,
        transition_source=prior_state_source,
        transition_reason=(
            None
            if transition is None
            else transition.transition_reason
        ) or decision.last_transition_reason or decision.close_reason or decision.book_action,
        transition_valid=True if transition is None else transition.valid_transition,
        transition_violation_reason=None if transition is None else transition.violation_reason,
        threshold_snapshot=threshold_snapshot,
        state_snapshot=state_snapshot,
        health_snapshot=health_snapshot,
    )


def recovery_snapshots_from_allocation_decisions(
    *,
    decisions: Sequence[PortfolioAllocationDecision],
    open_orders: Sequence[OrderState],
    recent_bundles: Sequence[StrategyExecutionBundle],
) -> tuple[IndependentRecoverySnapshot, ...]:
    independent_orders = [order for order in open_orders if str(order.strategy_family or "").strip() == "independent"]
    snapshots: list[IndependentRecoverySnapshot] = []
    seen: set[tuple[str, str | None, str]] = set()
    for decision in decisions:
        for sleeve_intent in decision.sleeve_intents:
            if sleeve_intent.family != "independent":
                continue
            runtime_states = _runtime_states_by_leg(sleeve_intent)
            family_blockers = tuple(
                str(item)
                for item in (sleeve_intent.metrics or {}).get("family_health_blockers", [])
                if str(item or "").strip()
            )
            for leg in ("long", "short"):
                runtime_state = runtime_states.get(leg)
                if runtime_state is None and not _has_leg_specific_metrics(sleeve_intent=sleeve_intent, leg=leg):
                    continue
                snapshot = _build_recovery_snapshot(
                    decision=decision,
                    sleeve_intent=sleeve_intent,
                    leg=leg,
                    runtime_state=runtime_state,
                    open_orders=independent_orders,
                    recent_bundles=recent_bundles,
                    family_blockers=family_blockers,
                )
                identity = (snapshot.decision_id, snapshot.strategy_sleeve_id, snapshot.leg)
                if identity in seen:
                    continue
                seen.add(identity)
                snapshots.append(snapshot)
    return tuple(snapshots)


def _build_recovery_snapshot(
    *,
    decision: PortfolioAllocationDecision,
    sleeve_intent: StrategySleeveIntent,
    leg: str,
    runtime_state: StrategyBookRuntimeState | None,
    open_orders: Sequence[OrderState],
    recent_bundles: Sequence[StrategyExecutionBundle],
    family_blockers: tuple[str, ...],
) -> IndependentRecoverySnapshot:
    expected_chain_ids = _ordered_unique(
        [
            *(
                [runtime_state.execution_chain_id]
                if runtime_state is not None and str(runtime_state.execution_chain_id or "").strip()
                else []
            ),
            *[
                chain_id
                for chain_id in (
                    leg_intent.execution_chain_id
                    for leg_intent in decision.execution_legs
                    if leg_intent.family == "independent"
                    and str(leg_intent.strategy_sleeve_id or "").strip() == str(sleeve_intent.strategy_sleeve_id or "").strip()
                    and _leg_from_leg_intent(leg_intent) == leg
                )
                if str(chain_id or "").strip()
            ],
        ]
    )
    active_orders = [
        order
        for order in open_orders
        if str(order.symbol or "").strip() == sleeve_intent.symbol
        and str(order.strategy_sleeve_id or "").strip() == str(sleeve_intent.strategy_sleeve_id or "").strip()
        and _leg_from_order(order) == leg
    ]
    active_chain_ids = _ordered_unique(
        [
            *[str(order.execution_chain_id) for order in active_orders if str(order.execution_chain_id or "").strip()],
            *[
                str(bundle_leg.execution_chain_id)
                for bundle in recent_bundles
                for bundle_leg in bundle.legs
                if _bundle_leg_matches(
                    bundle=bundle,
                    leg_intent=bundle_leg,
                    symbol=sleeve_intent.symbol,
                    strategy_sleeve_id=sleeve_intent.strategy_sleeve_id,
                    leg=leg,
                )
                and str(bundle_leg.execution_chain_id or "").strip()
            ],
        ]
    )
    unresolved_attempt_ids = _ordered_unique(
        [
            str(order.execution_attempt_id)
            for order in active_orders
            if str(order.execution_attempt_id or "").strip()
        ]
    )
    unexpected_chain_ids = [
        chain_id
        for chain_id in active_chain_ids
        if expected_chain_ids and chain_id not in expected_chain_ids
    ]
    blockers = list(
        dict.fromkeys(
            [
                *([] if runtime_state is None else list(runtime_state.blocked_reasons)),
                *family_blockers,
                *(["independent_unexpected_execution_chain"] if unexpected_chain_ids else []),
            ]
        )
    )
    posture = _recovery_posture(
        active_chain_ids=active_chain_ids,
        unresolved_attempt_ids=unresolved_attempt_ids,
        unexpected_chain_ids=unexpected_chain_ids,
        runtime_state=runtime_state,
    )
    last_recovery_incident_at = _last_recovery_incident_at(
        active_orders=active_orders,
        recent_bundles=recent_bundles,
        symbol=sleeve_intent.symbol,
        strategy_sleeve_id=sleeve_intent.strategy_sleeve_id,
        leg=leg,
    )
    current_qty = Decimal("0") if runtime_state is None else to_decimal(runtime_state.current_qty)
    target_qty = Decimal("0") if runtime_state is None else to_decimal(runtime_state.target_qty)
    return IndependentRecoverySnapshot(
        decision_id=decision.decision_id,
        allocation_id=decision.allocation_id,
        symbol=sleeve_intent.symbol,
        strategy_sleeve_id=sleeve_intent.strategy_sleeve_id,
        leg=leg,
        book_state=None if runtime_state is None else runtime_state.book_state,
        holding_phase=None if runtime_state is None else runtime_state.holding_phase,
        health_state=None if runtime_state is None else runtime_state.health_state,
        current_qty=current_qty,
        target_qty=target_qty,
        prior_book_state=None if runtime_state is None else runtime_state.prior_book_state,
        current_scale_in_count=0 if runtime_state is None else int(runtime_state.current_scale_in_count or 0),
        current_de_risk_count=0 if runtime_state is None else int(runtime_state.current_de_risk_count or 0),
        expected_chain_ids=tuple(expected_chain_ids),
        active_execution_chain_ids=tuple(active_chain_ids),
        unresolved_attempt_ids=tuple(unresolved_attempt_ids),
        recovery_posture=posture,
        recovery_blockers=tuple(blockers),
        last_recovery_incident_at=last_recovery_incident_at,
        suspended_until=None if runtime_state is None else runtime_state.suspended_until,
        cooldown_until=None if runtime_state is None else runtime_state.cooldown_until,
        state_version=1 if runtime_state is None else int(runtime_state.state_version or 1),
        threshold_snapshot=(
            _leg_metrics_value(sleeve_intent, leg, "threshold_snapshot")
            or _runtime_state_snapshot_value(runtime_state, "threshold_snapshot")
        ),
        health_snapshot=(
            _leg_metrics_value(sleeve_intent, leg, "health_snapshot")
            or _runtime_state_snapshot_value(runtime_state, "leg_health_summary")
        ),
        replay_snapshot=_leg_metrics_value(sleeve_intent, leg, "replay_snapshot"),
        decision_snapshot=_decision_snapshot_from_sources(
            decision=decision,
            sleeve_intent=sleeve_intent,
            leg=leg,
            runtime_state=runtime_state,
        ),
        transition_valid=True if runtime_state is None else bool(runtime_state.transition_valid),
        transition_violation_reason=None if runtime_state is None else runtime_state.transition_violation_reason,
    )


def _decision_snapshot_from_sources(
    *,
    decision: PortfolioAllocationDecision,
    sleeve_intent: StrategySleeveIntent,
    leg: str,
    runtime_state: StrategyBookRuntimeState | None,
) -> IndependentDecisionSnapshot | None:
    if runtime_state is None and not _has_leg_specific_metrics(sleeve_intent=sleeve_intent, leg=leg):
        return None
    metrics = sleeve_intent.metrics or {}
    return IndependentDecisionSnapshot(
        decision_id=decision.decision_id,
        symbol=sleeve_intent.symbol,
        leg=leg,
        book_state=None if runtime_state is None else runtime_state.book_state,
        holding_phase=None if runtime_state is None else runtime_state.holding_phase,
        health_state=None if runtime_state is None else runtime_state.health_state,
        eligibility_state=None if runtime_state is None else runtime_state.eligibility_state,
        current_qty=None if runtime_state is None else runtime_state.current_qty,
        target_qty=None if runtime_state is None else runtime_state.target_qty,
        prior_book_state=None if runtime_state is None else runtime_state.prior_book_state,
        current_scale_in_count=0 if runtime_state is None else int(runtime_state.current_scale_in_count or 0),
        current_de_risk_count=0 if runtime_state is None else int(runtime_state.current_de_risk_count or 0),
        thesis_started_at=None if runtime_state is None else runtime_state.thesis_started_at,
        thesis_age_seconds=None if runtime_state is None else runtime_state.thesis_age_seconds,
        last_transition_at=None if runtime_state is None else runtime_state.last_transition_at,
        last_transition_reason=None if runtime_state is None else runtime_state.last_transition_reason,
        suspended_until=None if runtime_state is None else runtime_state.suspended_until,
        cooldown_until=None if runtime_state is None else runtime_state.cooldown_until,
        state_version=1 if runtime_state is None else int(runtime_state.state_version or 1),
        raw_score=None if runtime_state is None else runtime_state.score_raw,
        adjusted_score=None if runtime_state is None else runtime_state.score_adjusted,
        score_stability_metrics=_compact_dict(
            {
                "support_count": metrics.get(f"{leg}_score_support_count"),
                "stable": metrics.get(f"{leg}_score_stable"),
                "max_drawdown_bps": metrics.get(f"{leg}_score_stability_max_drawdown_bps"),
                "source": metrics.get(f"{leg}_score_stability_source"),
            }
        ),
        expectancy_snapshot=_compact_dict(
            {
                "expected_signal_edge_bps": None if runtime_state is None else runtime_state.expected_signal_edge_bps,
                "expected_cost_bps": None if runtime_state is None else runtime_state.expected_cost_bps,
                "expected_net_edge_bps": None if runtime_state is None else runtime_state.expected_net_edge_bps,
                "liquidity_quality_score": None if runtime_state is None else runtime_state.liquidity_quality_score,
                "execution_health_state": None if runtime_state is None else runtime_state.execution_health_state,
            }
        ),
        eligibility_outcome=_compact_dict(
            {
                "eligibility_state": None if runtime_state is None else runtime_state.eligibility_state,
                "blocked_reasons": None if runtime_state is None else list(runtime_state.blocked_reasons),
            }
        ),
        sizing_outcome=_compact_dict(
            {
                "size_multiplier": None if runtime_state is None else runtime_state.size_multiplier,
                "capital_multiplier": None if runtime_state is None else runtime_state.capital_multiplier,
                "current_scale_in_count": None if runtime_state is None else runtime_state.current_scale_in_count,
                "current_de_risk_count": None if runtime_state is None else runtime_state.current_de_risk_count,
            }
        ),
        book_action=None if runtime_state is None else runtime_state.book_action,
        close_reason=None if runtime_state is None else runtime_state.close_reason,
        transition_valid=True if runtime_state is None else bool(runtime_state.transition_valid),
        transition_violation_reason=None if runtime_state is None else runtime_state.transition_violation_reason,
        execution_policy=_compact_dict(
            {
                "policy_reason": None if runtime_state is None else runtime_state.policy_reason,
                "execution_policy_urgency": None if runtime_state is None else runtime_state.execution_policy_urgency,
                "edge_strength": None if runtime_state is None else runtime_state.edge_strength,
                "execution_style_preference": metrics.get(f"{leg}_execution_style_preference"),
                "order_type_preference": metrics.get(f"{leg}_order_type_preference"),
                "time_in_force_preference": metrics.get(f"{leg}_time_in_force_preference"),
                "limit_offset_bps_preference": metrics.get(f"{leg}_limit_offset_bps_preference"),
            }
        ),
        threshold_snapshot=(
            _leg_metrics_value(sleeve_intent, leg, "threshold_snapshot")
            or _runtime_state_snapshot_value(runtime_state, "threshold_snapshot")
        ),
        health_snapshot=(
            _leg_metrics_value(sleeve_intent, leg, "health_snapshot")
            or _runtime_state_snapshot_value(runtime_state, "leg_health_summary")
        ),
        replay_snapshot=_leg_metrics_value(sleeve_intent, leg, "replay_snapshot"),
        reason_codes=() if runtime_state is None else tuple(runtime_state.reason_codes),
        blocked_reasons=() if runtime_state is None else tuple(runtime_state.blocked_reasons),
    )


def _runtime_states_by_leg(sleeve_intent: StrategySleeveIntent) -> dict[str, StrategyBookRuntimeState]:
    metrics = sleeve_intent.metrics or {}
    runtime_states = metrics.get("book_runtime_states", [])
    parsed: dict[str, StrategyBookRuntimeState] = {}
    if not isinstance(runtime_states, list):
        return parsed
    for item in runtime_states:
        if not isinstance(item, dict):
            continue
        try:
            runtime_state = StrategyBookRuntimeState.model_validate(item)
        except Exception:
            continue
        parsed[runtime_state.leg] = runtime_state
    return parsed


def _has_leg_specific_metrics(*, sleeve_intent: StrategySleeveIntent, leg: str) -> bool:
    metrics = sleeve_intent.metrics or {}
    return any(
        key in metrics
        for key in (
            f"{leg}_threshold_snapshot",
            f"{leg}_health_snapshot",
            f"{leg}_replay_snapshot",
            f"{leg}_book_action",
            f"{leg}_close_reason",
        )
    )


def _leg_metrics_value(sleeve_intent: StrategySleeveIntent, leg: str, suffix: str) -> dict[str, Any] | None:
    metrics = sleeve_intent.metrics or {}
    value = metrics.get(f"{leg}_{suffix}")
    return value if isinstance(value, dict) else None


def _runtime_state_snapshot_value(
    runtime_state: StrategyBookRuntimeState | None,
    field_name: str,
) -> dict[str, Any] | None:
    if runtime_state is None:
        return None
    value = getattr(runtime_state, field_name, None)
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    return None


def _leg_from_order(order: OrderState) -> str | None:
    execution_chain_id = str(order.execution_chain_id or "")
    if execution_chain_id.startswith("independent:"):
        parts = execution_chain_id.split(":")
        if len(parts) >= 3 and parts[2] in {"long", "short"}:
            return parts[2]
    pos_side = str(order.pos_side or "").strip().lower()
    if pos_side in {"long", "short"}:
        return pos_side
    return None


def _leg_from_leg_intent(leg: StrategyLegIntent) -> str | None:
    execution_chain_id = str(leg.execution_chain_id or "")
    if execution_chain_id.startswith("independent:"):
        parts = execution_chain_id.split(":")
        if len(parts) >= 3 and parts[2] in {"long", "short"}:
            return parts[2]
    pos_side = str(leg.pos_side or "").strip().lower()
    if pos_side in {"long", "short"}:
        return pos_side
    return None


def _bundle_leg_matches(
    *,
    bundle: StrategyExecutionBundle,
    leg_intent: StrategyLegIntent,
    symbol: str,
    strategy_sleeve_id: str | None,
    leg: str,
) -> bool:
    if bundle.family != "independent" and "independent" not in bundle.participating_families:
        return False
    if str(leg_intent.family or "").strip() != "independent":
        return False
    if str(leg_intent.symbol or "").strip() != str(symbol or "").strip():
        return False
    if str(leg_intent.strategy_sleeve_id or "").strip() != str(strategy_sleeve_id or "").strip():
        return False
    return _leg_from_leg_intent(leg_intent) == leg


def _recovery_posture(
    *,
    active_chain_ids: list[str],
    unresolved_attempt_ids: list[str],
    unexpected_chain_ids: list[str],
    runtime_state: StrategyBookRuntimeState | None,
) -> str:
    if unexpected_chain_ids:
        return "unexpected_active_chain"
    if unresolved_attempt_ids:
        return "pending_execution_attempts"
    if active_chain_ids:
        return "tracking_active_chain"
    if runtime_state is not None and (
        abs(to_decimal(runtime_state.current_qty)) > Decimal("1e-12")
        or abs(to_decimal(runtime_state.target_qty)) > Decimal("1e-12")
    ):
        return "book_state_only"
    return "idle"


def _last_recovery_incident_at(
    *,
    active_orders: Sequence[OrderState],
    recent_bundles: Sequence[StrategyExecutionBundle],
    symbol: str,
    strategy_sleeve_id: str | None,
    leg: str,
) -> datetime | None:
    timestamps: list[datetime] = [
        ts
        for order in active_orders
        for ts in (order.last_update_ts, order.submitted_ts)
        if ts is not None
    ]
    for bundle in recent_bundles:
        for leg_intent in bundle.legs:
            if _bundle_leg_matches(
                bundle=bundle,
                leg_intent=leg_intent,
                symbol=symbol,
                strategy_sleeve_id=strategy_sleeve_id,
                leg=leg,
            ):
                timestamps.append(bundle.created_at)
                break
    return max(timestamps, default=None)


def _ordered_unique(values: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _compact_dict(payload: dict[str, Any]) -> dict[str, Any] | None:
    compact = {key: value for key, value in payload.items() if value is not None}
    return compact or None
