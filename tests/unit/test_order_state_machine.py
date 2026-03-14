from __future__ import annotations

import unittest

from aats.schemas.common import utc_now
from aats.schemas.execution import OrderState
from aats.services.execution_engine.state_machine import OrderStateMachine


def make_state(*, status: str, filled_qty: float = 0.0, remaining_qty: float = 1.0) -> OrderState:
    now = utc_now()
    return OrderState(
        decision_id="decision_1",
        intent_id="intent_1",
        symbol="BTC-USDT",
        client_order_id="clord_1",
        venue="OKX",
        exchange_order_id="ord_1",
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


if __name__ == "__main__":
    unittest.main()
