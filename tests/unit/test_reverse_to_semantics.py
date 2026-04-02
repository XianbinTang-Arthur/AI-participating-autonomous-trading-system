from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal

from aats.schemas.execution import OrderState
from aats.services.execution_control.order_service import ExecutionOrderService
from aats.services.execution_control.shadow import Phase1ExecutionShadowService
from aats.storage.execution_repo_converged_postgres import ConvergedPostgresExecutionRepository


def _order_state(*, position_intent: str) -> OrderState:
    now = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)
    return OrderState(
        decision_id=f"decision_{position_intent}",
        intent_id=f"intent_{position_intent}",
        symbol="BTC-USDT-SWAP",
        client_order_id=f"cl_{position_intent}",
        venue="OKX",
        exchange_order_id=f"ord_{position_intent}",
        status="SUBMITTED",
        submission_mode="exchange",
        submitted_ts=now,
        last_update_ts=now,
        requested_qty=Decimal("0.01"),
        filled_qty=Decimal("0"),
        remaining_qty=Decimal("0.01"),
        average_fill_price=None,
        fees=Decimal("0"),
        reduce_only=False,
        close_only=False,
        td_mode="cross",
        position_mode="long_short_mode",
        pos_side="short",
        instrument_family="BTC-USDT",
        settle_currency="USDT",
        product_type="derivatives",
        target_leverage=2.0,
        margin_mode="cross",
        exposure_side="short",
        execution_action="reverse",
        leg_action="open",
        position_intent=position_intent,  # type: ignore[arg-type]
    )


class TestReverseToSemantics(unittest.TestCase):
    def test_execution_order_service_restores_reverse_to_short_as_sell(self) -> None:
        intent = ExecutionOrderService._intent_from_order_state(_order_state(position_intent="reverse_to_short"))

        self.assertEqual(intent.side, "sell")
        self.assertEqual(intent.position_intent, "reverse_to_short")

    def test_shadow_service_restores_reverse_to_short_as_sell(self) -> None:
        intent = Phase1ExecutionShadowService.intent_from_order_state(_order_state(position_intent="reverse_to_short"))

        self.assertEqual(intent.side, "sell")
        self.assertEqual(intent.position_intent, "reverse_to_short")

    def test_reduce_and_close_position_intents_restore_expected_sides(self) -> None:
        expected = {
            "reduce_long": "sell",
            "close_long": "sell",
            "reduce_short": "buy",
            "close_short": "buy",
        }

        for position_intent, expected_side in expected.items():
            with self.subTest(position_intent=position_intent):
                order_state = _order_state(position_intent=position_intent)
                order_state = order_state.model_copy(
                    update={
                        "execution_action": "reduce" if "reduce" in position_intent else "exit",
                        "leg_action": "reduce" if "reduce" in position_intent else "close",
                        "pos_side": "long" if position_intent.endswith("long") else "short",
                        "exposure_side": "long" if position_intent.endswith("long") else "short",
                    }
                )

                service_intent = ExecutionOrderService._intent_from_order_state(order_state)
                shadow_intent = Phase1ExecutionShadowService.intent_from_order_state(order_state)
                converged_intent = ConvergedPostgresExecutionRepository._intent_from_order_state(order_state)

                self.assertEqual(service_intent.side, expected_side)
                self.assertEqual(shadow_intent.side, expected_side)
                self.assertEqual(converged_intent.side, expected_side)
