from __future__ import annotations

from collections.abc import Iterable

APPROVED_FOR_NON_PROTECTIVE_EXECUTION = "approved_for_non_protective_execution"
AUTO_EXECUTION_DISABLED_BY_PROFILE = "auto_execution_disabled_by_profile"
CANDIDATE_DISABLED = "candidate_disabled"
RUNTIME_NOT_SUPPORTED = "runtime_not_supported"
PROTECTIVE_INTENT_OVERRIDE = "protective_intent_override"

NO_BUDGET_CONTRACTION = "no_budget_contraction"
BASELINE_VOLATILITY_CONTRACTION_ACTIVE = "baseline_volatility_contraction_active"
PNL_CONTRACTION_ACTIVE = "pnl_contraction_active"
RECONCILIATION_CONTRACTION_ACTIVE = "reconciliation_contraction_active"
RECONCILIATION_HARD_BLOCK = "reconciliation_hard_block"
HARD_LOSS_BUDGET_BLOCK = "hard_loss_budget_block"
BUDGET_CONTRACTED_TO_ZERO = "budget_contracted_to_zero"
SCALE_BELOW_MIN_TRADEABLE_STEP = "scale_below_min_tradeable_step"

COMPOSED_AS_ADVISORY_ONLY = "composed_as_advisory_only"
COMPOSED_AS_HOLD_CURRENT = "composed_as_hold_current"
COMPOSED_AS_OVERRIDE_TARGET = "composed_as_override_target"
COMPOSED_AS_PROTECTIVE_EXECUTION = "composed_as_protective_execution"
APPROVED_BUT_BUDGET_ZERO_SUPPRESSED = "approved_but_budget_zero_suppressed"


def unique_reason_codes(*groups: Iterable[str]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for code in group:
            normalized = str(code or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
    return tuple(ordered)
