from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.execution import (
    LegOrderIntent,
    OrderIntent,
    OrderState,
    close_only_from_leg_action,
    close_only_from_position_intent,
    execution_action_from_leg_action,
    execution_action_from_position_intent,
    reduce_only_from_leg_action,
    reduce_only_from_position_intent,
)


UnknownWriteOperation = Literal["submit", "cancel"]

UNKNOWN_SUBMIT_ERROR_PREFIX = "submission_unknown_check_exchange"
UNKNOWN_CANCEL_ERROR_PREFIX = "cancel_unknown_check_exchange"
DEFAULT_UNKNOWN_SUBMIT_REVIEW_AFTER_SECONDS = 30.0
DEFAULT_UNKNOWN_CANCEL_REVIEW_AFTER_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class UnknownWriteState:
    operation: UnknownWriteOperation
    exchange_truth_pending: bool
    age_seconds: float
    review_after_seconds: float
    review_required: bool


def _normalized_error(error: str | None) -> str:
    return str(error or "").strip().lower()


def unknown_write_operation(order: OrderState) -> UnknownWriteOperation | None:
    error = _normalized_error(order.execution_error)
    if error == UNKNOWN_SUBMIT_ERROR_PREFIX or error.startswith(f"{UNKNOWN_SUBMIT_ERROR_PREFIX}:"):
        return "submit"
    if error == UNKNOWN_CANCEL_ERROR_PREFIX or error.startswith(f"{UNKNOWN_CANCEL_ERROR_PREFIX}:"):
        return "cancel"
    return None


def is_unknown_write_state(order: OrderState) -> bool:
    return unknown_write_operation(order) is not None


def exchange_truth_pending(order: OrderState) -> bool:
    return is_unknown_write_state(order)


def unknown_write_age_seconds(
    order: OrderState,
    *,
    now: datetime | None = None,
) -> float:
    operation = unknown_write_operation(order)
    if operation is None:
        return 0.0
    reference = (
        order.cancellation_requested_ts
        if operation == "cancel" and order.cancellation_requested_ts is not None
        else order.last_update_ts or order.submitted_ts or order.created_at
    )
    age = (now or utc_now()) - reference
    return max(age.total_seconds(), 0.0)


def unknown_write_review_after_seconds(
    order: OrderState,
    *,
    settings: AATSSettings | None = None,
) -> float:
    operation = unknown_write_operation(order)
    if operation == "submit":
        configured = (
            getattr(settings, "execution_unknown_submit_review_after_seconds", None)
            if settings is not None
            else None
        )
        resolved = (
            DEFAULT_UNKNOWN_SUBMIT_REVIEW_AFTER_SECONDS
            if configured is None
            else configured
        )
        return max(float(resolved), 0.0)
    if operation == "cancel":
        configured = (
            getattr(settings, "execution_unknown_cancel_review_after_seconds", None)
            if settings is not None
            else None
        )
        resolved = (
            DEFAULT_UNKNOWN_CANCEL_REVIEW_AFTER_SECONDS
            if configured is None
            else configured
        )
        return max(float(resolved), 0.0)
    return 0.0


def requires_unknown_write_review(
    order: OrderState,
    *,
    now: datetime | None = None,
    settings: AATSSettings | None = None,
) -> bool:
    if not exchange_truth_pending(order):
        return False
    threshold = unknown_write_review_after_seconds(order, settings=settings)
    if threshold <= 0.0:
        return True
    return unknown_write_age_seconds(order, now=now) >= threshold


def unknown_write_state(
    order: OrderState,
    *,
    now: datetime | None = None,
    settings: AATSSettings | None = None,
) -> UnknownWriteState | None:
    operation = unknown_write_operation(order)
    if operation is None:
        return None
    age_seconds = unknown_write_age_seconds(order, now=now)
    review_after_seconds = unknown_write_review_after_seconds(order, settings=settings)
    return UnknownWriteState(
        operation=operation,
        exchange_truth_pending=True,
        age_seconds=age_seconds,
        review_after_seconds=review_after_seconds,
        review_required=requires_unknown_write_review(order, now=now, settings=settings),
    )


def blocks_new_risk_actions(order: OrderState) -> bool:
    return exchange_truth_pending(order)


def _is_risk_reducing_fields(
    *,
    reduce_only: bool,
    close_only: bool,
    position_intent: str | None,
    leg_action,
    explicit_execution_action,
) -> bool:
    position_action = execution_action_from_position_intent(position_intent)
    leg_execution_action = execution_action_from_leg_action(leg_action)
    normalized_actions = {
        str(action).strip().lower()
        for action in (position_action, leg_execution_action, explicit_execution_action)
        if str(action or "").strip()
    }
    return bool(
        reduce_only
        or close_only
        or reduce_only_from_leg_action(leg_action)
        or close_only_from_leg_action(leg_action)
        or reduce_only_from_position_intent(position_intent)
        or close_only_from_position_intent(position_intent)
        or bool({"reduce", "exit"} & normalized_actions)
    )


def is_risk_reducing_order_intent(intent: OrderIntent | LegOrderIntent) -> bool:
    return _is_risk_reducing_fields(
        reduce_only=bool(getattr(intent, "reduce_only", False)),
        close_only=bool(getattr(intent, "close_only", False)),
        position_intent=getattr(intent, "position_intent", None),
        leg_action=getattr(intent, "leg_action", None) or getattr(intent, "action", None),
        explicit_execution_action=getattr(intent, "execution_action", None),
    )


def is_risk_reducing_order_state(order: OrderState) -> bool:
    return _is_risk_reducing_fields(
        reduce_only=bool(order.reduce_only),
        close_only=bool(order.close_only),
        position_intent=order.position_intent,
        leg_action=order.leg_action,
        explicit_execution_action=order.execution_action,
    )
