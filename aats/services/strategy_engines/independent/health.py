from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .models import IndependentBookDecision, IndependentExecutionHealthState, IndependentLeg


@dataclass(frozen=True, slots=True)
class IndependentLegHealthSnapshot:
    leg: IndependentLeg
    health_state: IndependentExecutionHealthState
    halt_openings: bool = False
    only_reduce: bool = False
    suspended: bool = False
    warnings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IndependentFamilyHealthSnapshot:
    overall_state: IndependentExecutionHealthState
    long_leg: IndependentLegHealthSnapshot
    short_leg: IndependentLegHealthSnapshot
    family_blockers: tuple[str, ...] = ()


def evaluate_leg_health(*, decision: IndependentBookDecision) -> IndependentLegHealthSnapshot:
    state = decision.execution_health_state or "ok"
    blocker_items = [
        reason
        for reason in decision.blocked_reasons
        if "execution_health" in reason or "trial_guard" in reason
    ]
    warning_items = [
        reason
        for reason in decision.reason_codes
        if "guard" in reason and reason not in blocker_items
    ]
    if decision.weak_edge_report_only:
        warning_items.append("independent_weak_edge_report_only")
    if decision.liquidity_quality_score is not None and float(decision.liquidity_quality_score) < 0.5:
        warning_items.append("independent_liquidity_quality_degraded")
    blockers = tuple(dict.fromkeys(blocker_items))
    warnings = tuple(dict.fromkeys(warning_items))
    suspended = any("trial_guard" in reason for reason in decision.blocked_reasons)
    halt_openings = state == "blocked"
    only_reduce = state in {"degraded", "blocked"} and decision.current_qty > 0
    return IndependentLegHealthSnapshot(
        leg=decision.leg,
        health_state=state,
        halt_openings=halt_openings,
        only_reduce=only_reduce,
        suspended=suspended,
        warnings=warnings,
        blockers=blockers,
    )


def aggregate_family_health(
    *,
    long_leg: IndependentLegHealthSnapshot,
    short_leg: IndependentLegHealthSnapshot,
) -> IndependentFamilyHealthSnapshot:
    severity_rank: dict[IndependentExecutionHealthState, int] = {"ok": 0, "degraded": 1, "blocked": 2}
    overall_state: Literal["ok", "degraded", "blocked"] = max(
        (long_leg.health_state, short_leg.health_state),
        key=lambda item: severity_rank[item],
    )
    blockers = tuple(dict.fromkeys([*long_leg.blockers, *short_leg.blockers]))
    return IndependentFamilyHealthSnapshot(
        overall_state=overall_state,
        long_leg=long_leg,
        short_leg=short_leg,
        family_blockers=blockers,
    )
