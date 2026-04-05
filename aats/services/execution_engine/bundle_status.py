from __future__ import annotations

from aats.schemas.execution import OrderState
from aats.schemas.strategy_runtime import StrategyExecutionBundleStatus
from aats.services.execution_engine.bundle_recovery import _is_open_order
from aats.services.execution_engine.state_machine import TERMINAL_ORDER_STATES as _TERMINAL_ORDER_STATES
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
_FAILURE_ORDER_STATES = {"REJECTED", "FAILED", "BLOCKED"}
_BUNDLE_STATUS_REASON_CODES = {
    "strategy_bundle_partial_fill_recovery",
    "strategy_bundle_review_required",
    "strategy_bundle_recovered",
}
_OVERLAY_EXECUTION_MODES = {
    "protective_overlay",
    "opportunistic_overlay",
    "independent_long_book",
    "independent_short_book",
    "directional_main_leg",
}


def derive_strategy_bundle_status(
    *,
    order_states: list[OrderState],
    previous_status: StrategyExecutionBundleStatus = "submitted",
) -> StrategyExecutionBundleStatus:
    if not order_states:
        return previous_status

    if not _overlay_bundle_detected(order_states):
        return previous_status

    has_open = any(_is_open_order(order_state) for order_state in order_states)
    has_terminal = any(not _is_open_order(order_state) for order_state in order_states)
    has_failure = any(str(order_state.status or "").upper() in _FAILURE_ORDER_STATES for order_state in order_states)
    has_filled_or_partially_filled = any(
        str(order_state.status or "").upper() in {"FILLED", "PARTIALLY_FILLED"}
        or to_decimal(order_state.filled_qty) > EPSILON_DECIMAL_12
        for order_state in order_states
    )

    if has_failure and (has_open or has_terminal or has_filled_or_partially_filled):
        return "review_required"
    if has_open and has_terminal:
        return "partial_fill_recovery"
    if not has_open and previous_status in {"partial_fill_recovery", "review_required"}:
        return "recovered"
    return previous_status


def apply_strategy_bundle_status_reason_codes(
    *,
    reason_codes: list[str],
    status: StrategyExecutionBundleStatus,
) -> list[str]:
    filtered = [code for code in reason_codes if code not in _BUNDLE_STATUS_REASON_CODES]
    status_reason = {
        "partial_fill_recovery": "strategy_bundle_partial_fill_recovery",
        "review_required": "strategy_bundle_review_required",
        "recovered": "strategy_bundle_recovered",
    }.get(status)
    if status_reason is not None:
        filtered.append(status_reason)
    return filtered


def _overlay_bundle_detected(order_states: list[OrderState]) -> bool:
    for order_state in order_states:
        execution_mode = str(order_state.strategy_execution_mode or "").strip().lower()
        position_mode = str(order_state.position_mode or "").strip().lower()
        if execution_mode in _OVERLAY_EXECUTION_MODES:
            return True
        if position_mode == "long_short_mode" and str(order_state.product_type or "").strip().lower() == "derivatives":
            return True
    return False

