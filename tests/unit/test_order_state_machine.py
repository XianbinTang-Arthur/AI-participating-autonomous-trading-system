from __future__ import annotations

import unittest

from aats.schemas.common import utc_now
from aats.schemas.execution import OrderState
from aats.services.execution_engine.state_machine import OrderStateMachine


def make_state(
    *,
    status: str,
    filled_qty: float = 0.0,
    remaining_qty: float = 1.0,
    exchange_order_id: str | None = "ord_1",
    execution_error: str | None = None,
) -> OrderState:
    now = utc_now()
    return OrderState(
        decision_id="decision_1",
        intent_id="intent_1",
        symbol="BTC-USDT",
        client_order_id="clord_1",
        venue="OKX",
        exchange_order_id=exchange_order_id,
        status=status,
        submission_mode="guarded_simulated_submit",
        exchange_status=status.lower(),
        submitted_ts=now,
        last_update_ts=now,
        last_exchange_update_ts=now,
        requested_qty=1.0,
        filled_qty=filled_qty,
        remaining_qty=remaining_qty,
        average_fill_price=68_000.0 if filled_qty > 0.0 else None,
        fees=0.0,
        execution_error=execution_error,
        submission_payload={"instId": "BTC-USDT"},
    )


class TestOrderStateMachine(unittest.TestCase):
    def test_valid_transition_path_is_accepted(self) -> None:
        state_machine = OrderStateMachine()
        states = [
            make_state(status="CREATED"),
            make_state(status="SUBMITTING"),
            make_state(status="SUBMITTED"),
            make_state(status="PARTIALLY_FILLED", filled_qty=0.4, remaining_qty=0.6),
            make_state(status="CANCEL_PENDING", filled_qty=0.4, remaining_qty=0.6),
            make_state(status="CANCELED", filled_qty=0.4, remaining_qty=0.6),
        ]

        self.assertEqual(state_machine.validate_path(states), [])

    def test_regression_does_not_override_terminal_state(self) -> None:
        state_machine = OrderStateMachine()
        current = make_state(status="FILLED", filled_qty=1.0, remaining_qty=0.0)
        incoming = make_state(status="SUBMITTED", filled_qty=0.0, remaining_qty=1.0)

        merged = state_machine.merge(current=current, incoming=incoming)

        self.assertEqual(merged.status, "FILLED")
        self.assertEqual(merged.filled_qty, 1.0)
        self.assertEqual(merged.remaining_qty, 0.0)

    def test_cancel_pending_to_canceled_path_is_accepted(self) -> None:
        state_machine = OrderStateMachine()
        states = [
            make_state(status="CREATED"),
            make_state(status="SUBMITTING"),
            make_state(status="SUBMITTED"),
            make_state(status="PARTIALLY_FILLED", filled_qty=0.4, remaining_qty=0.6),
            make_state(status="CANCEL_PENDING", filled_qty=0.4, remaining_qty=0.6),
            make_state(status="CANCELED", filled_qty=0.4, remaining_qty=0.6),
        ]

        self.assertEqual(state_machine.validate_path(states), [])

    def test_submitting_to_blocked_path_is_accepted(self) -> None:
        state_machine = OrderStateMachine()
        states = [
            make_state(status="CREATED"),
            make_state(status="SUBMITTING"),
            make_state(status="BLOCKED"),
        ]

        self.assertEqual(state_machine.validate_path(states), [])

    def test_invalid_terminal_regression_is_rejected_in_path_validation(self) -> None:
        state_machine = OrderStateMachine()
        states = [
            make_state(status="SUBMITTED"),
            make_state(status="PARTIALLY_FILLED", filled_qty=0.4, remaining_qty=0.6),
            make_state(status="FILLED", filled_qty=1.0, remaining_qty=0.0),
            make_state(status="PARTIALLY_FILLED", filled_qty=0.4, remaining_qty=0.6),
        ]

        issues = state_machine.validate_path(states)

        self.assertTrue(any("invalid_transition" in issue or "status_regression" in issue for issue in issues))

    def test_unknown_submission_error_clears_when_exchange_confirms_live_order(self) -> None:
        state_machine = OrderStateMachine()
        current = make_state(
            status="SUBMITTED",
            exchange_order_id=None,
            execution_error="submission_unknown_check_exchange:OKXRequestError",
        )
        incoming = make_state(
            status="SUBMITTED",
            exchange_order_id="ord_confirmed",
            execution_error=None,
        )

        merged = state_machine.merge(current=current, incoming=incoming)

        self.assertEqual(merged.status, "SUBMITTED")
        self.assertEqual(merged.exchange_order_id, "ord_confirmed")
        self.assertIsNone(merged.execution_error)

    def test_unknown_cancel_error_clears_when_exchange_confirms_terminal_state(self) -> None:
        state_machine = OrderStateMachine()
        current = make_state(
            status="CANCEL_PENDING",
            execution_error="cancel_unknown_check_exchange:OKXRequestError",
        )
        incoming = make_state(
            status="CANCELED",
            remaining_qty=1.0,
            execution_error=None,
        )

        merged = state_machine.merge(current=current, incoming=incoming)

        self.assertEqual(merged.status, "CANCELED")
        self.assertIsNone(merged.execution_error)

    def test_unknown_submission_error_is_not_cleared_by_local_cancel_pending_transition(self) -> None:
        state_machine = OrderStateMachine()
        current = make_state(
            status="SUBMITTED",
            exchange_order_id=None,
            execution_error="submission_unknown_check_exchange:OKXRequestError",
        )
        incoming = make_state(
            status="CANCEL_PENDING",
            exchange_order_id=None,
            execution_error=None,
        )

        merged = state_machine.merge(current=current, incoming=incoming)

        self.assertEqual(merged.status, "CANCEL_PENDING")
        self.assertEqual(merged.execution_error, "submission_unknown_check_exchange:OKXRequestError")

    def test_non_unknown_execution_error_is_preserved_on_later_terminal_state(self) -> None:
        state_machine = OrderStateMachine()
        current = make_state(
            status="SUBMITTED",
            execution_error="order_lookup_failed_after_accept",
        )
        incoming = make_state(
            status="FILLED",
            filled_qty=1.0,
            remaining_qty=0.0,
            execution_error=None,
        )

        merged = state_machine.merge(current=current, incoming=incoming)

        self.assertEqual(merged.status, "FILLED")
        self.assertEqual(merged.execution_error, "order_lookup_failed_after_accept")


if __name__ == "__main__":
    unittest.main()
