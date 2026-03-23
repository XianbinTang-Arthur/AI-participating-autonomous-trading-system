from __future__ import annotations

from decimal import Decimal
from typing import Any

from aats.bootstrap.settings import AATSSettings

_DECIMAL_ZERO = Decimal("0")
_DECIMAL_ONE = Decimal("1")


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _clamp_decimal(value: Decimal, minimum: Decimal, maximum: Decimal) -> Decimal:
    return max(minimum, min(maximum, value))


def _normalized_multiplier_floor(value: float) -> Decimal:
    return _clamp_decimal(_to_decimal(value) or Decimal("0.3"), Decimal("0.1"), Decimal("1"))


def reconciliation_clean_from_safety_state(safety_state: dict[str, Any] | None) -> bool:
    if not isinstance(safety_state, dict):
        return True
    if bool(safety_state.get("reconciliation_halt_required")):
        return False
    if bool(safety_state.get("reconciliation_review_required")):
        return False
    return str(safety_state.get("reconciliation_severity") or "unknown").upper() in {"CLEAN", "UNKNOWN"}


def resolve_risk_budget_state(
    settings: AATSSettings,
    *,
    execution_error_count: int = 0,
    safe_to_trade: bool = True,
    review_required: bool = False,
    market_snapshot_fresh: bool = True,
    account_snapshot_fresh: bool = True,
    reconciliation_clean: bool = True,
    only_reduce_required: bool = False,
    auto_halt_required: bool = False,
    risk_snapshot_stage: str | None = None,
    trial_guard_breached: bool = False,
    current_margin_usage_fraction: Any | None = None,
    projected_margin_usage_fraction: Any | None = None,
    nearest_liquidation_gap_ratio: Any | None = None,
) -> dict[str, Any]:
    floor = _normalized_multiplier_floor(settings.strategy_risk_budget_multiplier_floor)
    multiplier = _DECIMAL_ONE
    reasons: list[str] = []
    hard_margin_cap = _to_decimal(settings.max_margin_usage_fraction) or Decimal("1")
    liquidation_buffer = _to_decimal(settings.liquidation_buffer_fraction) or Decimal("0")
    projected_margin_usage = _to_decimal(projected_margin_usage_fraction)
    current_margin_usage = _to_decimal(current_margin_usage_fraction)
    liquidation_gap = _to_decimal(nearest_liquidation_gap_ratio)

    if auto_halt_required:
        multiplier = floor
        reasons.append("auto_halt_required")
    elif only_reduce_required:
        multiplier = min(multiplier, max(floor, Decimal("0.45")))
        reasons.append("only_reduce_required")
    elif str(risk_snapshot_stage or "").lower() == "grace":
        multiplier = min(
            multiplier,
            max(
                floor,
                _clamp_decimal(
                    _to_decimal(settings.strategy_risk_snapshot_missing_budget_multiplier) or Decimal("0.70"),
                    Decimal("0.10"),
                    Decimal("1.00"),
                ),
            ),
        )
        reasons.append("risk_snapshot_missing_grace_active")

    if trial_guard_breached:
        multiplier = min(multiplier, max(floor, Decimal("0.50")))
        reasons.append("trial_guard_breached")

    if review_required or not safe_to_trade:
        multiplier = min(multiplier, max(floor, Decimal("0.55")))
        reasons.append("runtime_safety_degraded")

    if not market_snapshot_fresh or not account_snapshot_fresh or not reconciliation_clean:
        multiplier = min(multiplier, max(floor, Decimal("0.60")))
        reasons.append("state_freshness_or_reconciliation_degraded")

    execution_error_threshold = max(int(settings.strategy_profile_safety_trigger_execution_error_count), 1)
    if execution_error_count >= execution_error_threshold:
        severity = min(execution_error_count, execution_error_threshold * 2)
        error_multiplier = Decimal("1") - (Decimal("0.10") * Decimal(str(severity)))
        multiplier = min(multiplier, max(floor, error_multiplier))
        reasons.append("execution_errors_elevated")

    if projected_margin_usage is not None and hard_margin_cap > _DECIMAL_ZERO:
        if projected_margin_usage >= hard_margin_cap * Decimal("0.90"):
            multiplier = min(multiplier, max(floor, Decimal("0.45")))
            reasons.append("projected_margin_usage_near_hard_cap")
        elif projected_margin_usage >= hard_margin_cap * Decimal("0.75"):
            multiplier = min(multiplier, max(floor, Decimal("0.65")))
            reasons.append("projected_margin_usage_elevated")

    if current_margin_usage is not None and hard_margin_cap > _DECIMAL_ZERO:
        if current_margin_usage >= hard_margin_cap * Decimal("0.90"):
            multiplier = min(multiplier, max(floor, Decimal("0.50")))
            reasons.append("current_margin_usage_near_hard_cap")
        elif current_margin_usage >= hard_margin_cap * Decimal("0.75"):
            multiplier = min(multiplier, max(floor, Decimal("0.70")))
            reasons.append("current_margin_usage_elevated")

    if liquidation_gap is not None and liquidation_buffer > _DECIMAL_ZERO:
        if liquidation_gap <= liquidation_buffer * Decimal("0.75"):
            multiplier = min(multiplier, max(floor, Decimal("0.50")))
            reasons.append("liquidation_gap_tight")
        elif liquidation_gap <= liquidation_buffer * Decimal("1.25"):
            multiplier = min(multiplier, max(floor, Decimal("0.70")))
            reasons.append("liquidation_gap_narrowing")

    multiplier = _clamp_decimal(multiplier, floor, _DECIMAL_ONE)
    status = "normal"
    if multiplier <= floor + Decimal("0.000001"):
        status = "floor_contracted"
    elif multiplier < _DECIMAL_ONE:
        status = "contracted"
    return {
        "multiplier": float(multiplier),
        "status": status,
        "reasons": reasons,
        "floor": float(floor),
        "current_margin_usage_fraction": None if current_margin_usage is None else float(current_margin_usage),
        "projected_margin_usage_fraction": None if projected_margin_usage is None else float(projected_margin_usage),
        "nearest_liquidation_gap_ratio": None if liquidation_gap is None else float(liquidation_gap),
    }


def resolve_execution_aggressiveness_state(
    settings: AATSSettings,
    *,
    execution_error_count: int = 0,
    safe_to_trade: bool = True,
    review_required: bool = False,
    market_snapshot_fresh: bool = True,
    account_snapshot_fresh: bool = True,
    reconciliation_clean: bool = True,
    only_reduce_required: bool = False,
    auto_halt_required: bool = False,
    risk_snapshot_stage: str | None = None,
    trial_guard_breached: bool = False,
    current_margin_usage_fraction: Any | None = None,
    projected_margin_usage_fraction: Any | None = None,
    nearest_liquidation_gap_ratio: Any | None = None,
) -> dict[str, Any]:
    floor = _normalized_multiplier_floor(settings.strategy_execution_aggressiveness_multiplier_floor)
    multiplier = _DECIMAL_ONE
    reasons: list[str] = []
    hard_margin_cap = _to_decimal(settings.max_margin_usage_fraction) or Decimal("1")
    liquidation_buffer = _to_decimal(settings.liquidation_buffer_fraction) or Decimal("0")
    projected_margin_usage = _to_decimal(projected_margin_usage_fraction)
    current_margin_usage = _to_decimal(current_margin_usage_fraction)
    liquidation_gap = _to_decimal(nearest_liquidation_gap_ratio)

    if auto_halt_required:
        multiplier = floor
        reasons.append("auto_halt_required")
    elif only_reduce_required:
        multiplier = min(multiplier, max(floor, Decimal("0.35")))
        reasons.append("only_reduce_required")
    elif str(risk_snapshot_stage or "").lower() == "grace":
        multiplier = min(
            multiplier,
            max(
                floor,
                _clamp_decimal(
                    _to_decimal(settings.strategy_risk_snapshot_missing_execution_aggressiveness_multiplier)
                    or Decimal("0.55"),
                    Decimal("0.10"),
                    Decimal("1.00"),
                ),
            ),
        )
        reasons.append("risk_snapshot_missing_grace_active")

    if trial_guard_breached:
        multiplier = min(multiplier, max(floor, Decimal("0.40")))
        reasons.append("trial_guard_breached")

    if review_required or not safe_to_trade:
        multiplier = min(multiplier, max(floor, Decimal("0.45")))
        reasons.append("runtime_safety_degraded")

    if not market_snapshot_fresh or not account_snapshot_fresh or not reconciliation_clean:
        multiplier = min(multiplier, max(floor, Decimal("0.50")))
        reasons.append("state_freshness_or_reconciliation_degraded")

    execution_error_threshold = max(int(settings.strategy_profile_safety_trigger_execution_error_count), 1)
    if execution_error_count >= execution_error_threshold:
        severity = min(execution_error_count, execution_error_threshold * 2)
        error_multiplier = Decimal("1") - (Decimal("0.12") * Decimal(str(severity)))
        multiplier = min(multiplier, max(floor, error_multiplier))
        reasons.append("execution_errors_elevated")

    if projected_margin_usage is not None and hard_margin_cap > _DECIMAL_ZERO:
        if projected_margin_usage >= hard_margin_cap * Decimal("0.90"):
            multiplier = min(multiplier, max(floor, Decimal("0.35")))
            reasons.append("projected_margin_usage_near_hard_cap")
        elif projected_margin_usage >= hard_margin_cap * Decimal("0.75"):
            multiplier = min(multiplier, max(floor, Decimal("0.55")))
            reasons.append("projected_margin_usage_elevated")

    if current_margin_usage is not None and hard_margin_cap > _DECIMAL_ZERO:
        if current_margin_usage >= hard_margin_cap * Decimal("0.90"):
            multiplier = min(multiplier, max(floor, Decimal("0.40")))
            reasons.append("current_margin_usage_near_hard_cap")
        elif current_margin_usage >= hard_margin_cap * Decimal("0.75"):
            multiplier = min(multiplier, max(floor, Decimal("0.60")))
            reasons.append("current_margin_usage_elevated")

    if liquidation_gap is not None and liquidation_buffer > _DECIMAL_ZERO:
        if liquidation_gap <= liquidation_buffer * Decimal("0.75"):
            multiplier = min(multiplier, max(floor, Decimal("0.40")))
            reasons.append("liquidation_gap_tight")
        elif liquidation_gap <= liquidation_buffer * Decimal("1.25"):
            multiplier = min(multiplier, max(floor, Decimal("0.60")))
            reasons.append("liquidation_gap_narrowing")

    multiplier = _clamp_decimal(multiplier, floor, _DECIMAL_ONE)
    status = "normal"
    if multiplier <= floor + Decimal("0.000001"):
        status = "safe_mode"
    elif multiplier < _DECIMAL_ONE:
        status = "contracted"
    return {
        "multiplier": float(multiplier),
        "status": status,
        "reasons": reasons,
        "floor": float(floor),
        "prefer_passive_execution": bool(
            multiplier <= Decimal("0.60")
            or only_reduce_required
            or auto_halt_required
            or str(risk_snapshot_stage or "").lower() == "grace"
        ),
    }


def resolve_emergency_safety_fast_track(
    settings: AATSSettings,
    *,
    candidate_profile_id: str | None,
    safety_profile_ids: set[str],
    transition_risk_direction: str,
    safety_profile_required: bool,
    safe_to_trade: bool,
    review_required: bool,
    execution_error_count: int,
    only_reduce_required: bool = False,
    auto_halt_required: bool = False,
    trial_guard_breached: bool = False,
) -> dict[str, Any]:
    reasons: list[str] = []
    execution_error_threshold = max(int(settings.strategy_profile_safety_trigger_execution_error_count), 1)
    if safety_profile_required:
        reasons.append("safety_profile_required")
    if not safe_to_trade:
        reasons.append("runtime_not_safe_to_trade")
    if review_required:
        reasons.append("review_required")
    if execution_error_count >= execution_error_threshold:
        reasons.append("execution_errors_elevated")
    if only_reduce_required:
        reasons.append("only_reduce_required")
    if auto_halt_required:
        reasons.append("auto_halt_required")
    if trial_guard_breached:
        reasons.append("trial_guard_breached")

    eligible = bool(
        settings.strategy_profile_emergency_safety_fast_track_enabled
        and candidate_profile_id in safety_profile_ids
        and transition_risk_direction == "more_conservative"
        and reasons
    )
    return {
        "eligible": eligible,
        "confidence_floor": float(
            settings.strategy_profile_emergency_safety_confidence_min if eligible else 0.0
        ),
        "reasons": reasons,
        "bypass_gates": [
            "strategy_profile_safety_profile_requires_explicit_trigger",
            "strategy_profile_requires_more_realized_trades",
            "strategy_profile_requires_more_replay_validations",
            "strategy_profile_cold_start_lock_active",
            "strategy_profile_candidate_requires_more_confirmations",
            "strategy_profile_min_active_duration_not_reached",
            "strategy_profile_score_delta_below_threshold",
            "strategy_profile_runtime_not_safe_to_trade",
            "strategy_profile_review_required",
            "strategy_profile_auto_switch_frozen",
            "strategy_profile_switch_cooldown_active",
        ]
        if eligible
        else [],
    }
