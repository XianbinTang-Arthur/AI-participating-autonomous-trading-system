from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from aats.schemas.execution import OrderLifecycleStatus, OrderState
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12


TERMINAL_ORDER_STATES: set[OrderLifecycleStatus] = {
    "FILLED",
    "CANCELED",
    "REJECTED",
    "FAILED",
    "BLOCKED",
    "DRY_RUN",
    "EXPIRED",
}

OPEN_ORDER_STATES: set[OrderLifecycleStatus] = {
    "CREATED",
    "SUBMITTING",
    "SUBMITTED",
    "PARTIALLY_FILLED",
    "CANCEL_PENDING",
}

ALLOWED_TRANSITIONS: dict[OrderLifecycleStatus, set[OrderLifecycleStatus]] = {
    "CREATED": {"CREATED", "SUBMITTING", "BLOCKED", "FAILED", "REJECTED", "DRY_RUN"},
    "SUBMITTING": {"SUBMITTING", "SUBMITTED", "PARTIALLY_FILLED", "FILLED", "REJECTED", "FAILED", "BLOCKED", "DRY_RUN"},
    "SUBMITTED": {"SUBMITTED", "PARTIALLY_FILLED", "FILLED", "CANCEL_PENDING", "CANCELED", "FAILED", "EXPIRED"},
    "PARTIALLY_FILLED": {"PARTIALLY_FILLED", "FILLED", "CANCEL_PENDING", "CANCELED", "FAILED", "EXPIRED"},
    "CANCEL_PENDING": {"CANCEL_PENDING", "CANCELED", "FILLED", "FAILED", "EXPIRED"},
    "FILLED": {"FILLED"},
    "CANCELED": {"CANCELED"},
    "REJECTED": {"REJECTED"},
    "FAILED": {"FAILED"},
    "BLOCKED": {"BLOCKED"},
    "DRY_RUN": {"DRY_RUN"},
    "EXPIRED": {"EXPIRED"},
}

STATE_PRIORITY: dict[OrderLifecycleStatus, int] = {
    "CREATED": 0,
    "SUBMITTING": 1,
    "SUBMITTED": 2,
    "PARTIALLY_FILLED": 3,
    "CANCEL_PENDING": 4,
    "FILLED": 5,
    "CANCELED": 5,
    "EXPIRED": 5,
    "REJECTED": 5,
    "FAILED": 5,
    "BLOCKED": 5,
    "DRY_RUN": 5,
}


@dataclass(frozen=True, slots=True)
class TransitionValidation:
    accepted: bool
    reason: str


class OrderStateMachine:
    def validate_transition(
        self,
        *,
        current_status: OrderLifecycleStatus | None,
        next_status: OrderLifecycleStatus,
    ) -> TransitionValidation:
        if current_status is None:
            return TransitionValidation(accepted=True, reason="initial_state")
        if next_status == current_status:
            return TransitionValidation(accepted=True, reason="idempotent")
        if next_status in ALLOWED_TRANSITIONS.get(current_status, set()):
            return TransitionValidation(accepted=True, reason="valid_transition")
        if STATE_PRIORITY[next_status] < STATE_PRIORITY[current_status]:
            return TransitionValidation(accepted=False, reason="status_regression")
        return TransitionValidation(accepted=False, reason="invalid_transition")

    def merge(self, *, current: OrderState | None, incoming: OrderState) -> OrderState:
        if current is None:
            return self._normalize(incoming)

        validation = self.validate_transition(
            current_status=current.status,
            next_status=incoming.status,
        )
        if not validation.accepted:
            return self._merge_without_regression(current=current, incoming=incoming)

        requested_qty = incoming.requested_qty if incoming.requested_qty > Decimal("0") else current.requested_qty
        filled_qty = min(max(current.filled_qty, incoming.filled_qty), requested_qty)
        remaining_floor = max(requested_qty - filled_qty, Decimal("0"))
        remaining_candidates = [remaining_floor]
        if current.remaining_qty >= Decimal("0"):
            remaining_candidates.append(current.remaining_qty)
        if incoming.remaining_qty >= Decimal("0"):
            remaining_candidates.append(incoming.remaining_qty)
        remaining_qty = min(remaining_candidates)
        average_fill_price = incoming.average_fill_price
        if average_fill_price is None and current.average_fill_price is not None:
            average_fill_price = current.average_fill_price
        elif (
            incoming.average_fill_price is not None
            and current.average_fill_price is not None
            and incoming.filled_qty < current.filled_qty
        ):
            average_fill_price = current.average_fill_price

        exchange_history = list(dict.fromkeys([*current.exchange_status_history, *incoming.exchange_status_history]))
        if incoming.exchange_status is not None and incoming.exchange_status not in exchange_history:
            exchange_history.append(incoming.exchange_status)

        merged = incoming.model_copy(
            update={
                "exchange_order_id": incoming.exchange_order_id or current.exchange_order_id,
                "exchange_status": incoming.exchange_status or current.exchange_status,
                "exchange_status_history": exchange_history,
                "submitted_ts": self._earliest(current.submitted_ts, incoming.submitted_ts),
                "last_update_ts": self._latest(current.last_update_ts, incoming.last_update_ts),
                "last_exchange_update_ts": self._latest(
                    current.last_exchange_update_ts,
                    incoming.last_exchange_update_ts,
                ),
                "cancellation_requested_ts": self._earliest(
                    current.cancellation_requested_ts,
                    incoming.cancellation_requested_ts,
                ),
                "canceled_ts": self._earliest(current.canceled_ts, incoming.canceled_ts),
                "requested_qty": requested_qty,
                "filled_qty": filled_qty,
                "remaining_qty": remaining_qty,
                "average_fill_price": average_fill_price,
                "fees": max(current.fees, incoming.fees),
                "cancel_reason": incoming.cancel_reason or current.cancel_reason,
                "execution_error": incoming.execution_error or current.execution_error,
                "submission_payload": incoming.submission_payload or current.submission_payload,
            }
        )
        return self._normalize(merged)

    def validate_path(self, states: list[OrderState]) -> list[str]:
        issues: list[str] = []
        previous: OrderState | None = None
        for state in states:
            validation = self.validate_transition(
                current_status=None if previous is None else previous.status,
                next_status=state.status,
            )
            if not validation.accepted:
                issues.append(
                    f"invalid_transition previous={previous.status if previous is not None else 'NONE'} next={state.status} reason={validation.reason}"
                )
            previous = self.merge(current=previous, incoming=state)
        return issues

    def _merge_without_regression(self, *, current: OrderState, incoming: OrderState) -> OrderState:
        if incoming.filled_qty > current.filled_qty or incoming.fees > current.fees:
            return self.merge(
                current=current,
                incoming=current.model_copy(
                    update={
                        "filled_qty": incoming.filled_qty,
                        "remaining_qty": incoming.remaining_qty,
                        "average_fill_price": incoming.average_fill_price or current.average_fill_price,
                        "fees": incoming.fees,
                        "last_update_ts": self._latest(current.last_update_ts, incoming.last_update_ts),
                        "last_exchange_update_ts": self._latest(
                            current.last_exchange_update_ts,
                            incoming.last_exchange_update_ts,
                        ),
                    }
                ),
            )
        return current.model_copy(
            update={
                "last_update_ts": self._latest(current.last_update_ts, incoming.last_update_ts),
                "last_exchange_update_ts": self._latest(
                    current.last_exchange_update_ts,
                    incoming.last_exchange_update_ts,
                ),
                "execution_error": current.execution_error or incoming.execution_error,
            }
        )

    def _normalize(self, state: OrderState) -> OrderState:
        filled_qty = min(max(state.filled_qty, Decimal("0")), state.requested_qty)
        remaining_qty = max(min(state.remaining_qty, state.requested_qty), Decimal("0"))
        if filled_qty >= state.requested_qty - EPSILON_DECIMAL_12 and state.requested_qty > Decimal("0"):
            normalized_status: OrderLifecycleStatus = "FILLED"
            remaining_qty = Decimal("0")
        elif state.status in {"SUBMITTED", "PARTIALLY_FILLED"} and filled_qty > EPSILON_DECIMAL_12:
            normalized_status = "PARTIALLY_FILLED"
            remaining_qty = max(state.requested_qty - filled_qty, Decimal("0"))
        else:
            normalized_status = state.status
        if normalized_status == "CANCELED" and state.canceled_ts is None:
            return state.model_copy(
                update={
                    "status": normalized_status,
                    "filled_qty": filled_qty,
                    "remaining_qty": remaining_qty,
                    "canceled_ts": state.last_exchange_update_ts or state.last_update_ts,
                }
            )
        return state.model_copy(
            update={
                "status": normalized_status,
                "filled_qty": filled_qty,
                "remaining_qty": remaining_qty,
            }
        )

    @staticmethod
    def is_terminal(status: OrderLifecycleStatus) -> bool:
        return status in TERMINAL_ORDER_STATES

    @staticmethod
    def is_open(status: OrderLifecycleStatus) -> bool:
        return status in OPEN_ORDER_STATES

    @staticmethod
    def _latest(left: datetime | None, right: datetime | None) -> datetime | None:
        if left is None:
            return right
        if right is None:
            return left
        return max(left, right)

    @staticmethod
    def _earliest(left: datetime | None, right: datetime | None) -> datetime | None:
        if left is None:
            return right
        if right is None:
            return left
        return min(left, right)
