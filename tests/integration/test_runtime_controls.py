from __future__ import annotations

import unittest

from aats.bootstrap.config import build_runtime, load_settings
from aats.events.envelopes import build_envelope
from aats.schemas.execution import OrderIntent


class TestRuntimeControls(unittest.IsolatedAsyncioTestCase):
    async def test_halt_blocks_execution_and_resume_allows_it(self) -> None:
        settings = load_settings()
        runtime = await build_runtime(settings)

        runtime.kill_switch.halt("test_halt")
        halted_intent = OrderIntent(
            intent_id="intent_halt_test",
            decision_id="decision_halt_test",
            symbol=settings.default_symbol,
            side="buy",
            quantity=settings.default_order_qty,
            execution_style="taker",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            reduce_only=False,
            close_only=False,
            idempotency_key="intent_halt_test",
        )
        await runtime.order_manager.handle_order_intent(
            {
                "topic": "execution.order_intents",
                "key": settings.default_symbol,
                "payload": build_envelope(
                    topic="execution.order_intents",
                    key=settings.default_symbol,
                    payload_model=halted_intent,
                    source_component="test",
                ).model_dump(mode="json"),
            }
        )
        self.assertEqual(len(runtime.execution_repo.order_states()), 0)
        self.assertEqual(len(runtime.execution_repo.fills()), 0)

        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        self.assertEqual(len(runtime.execution_repo.order_states()), 0)

        runtime.kill_switch.resume()
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=1,
            interval_seconds=0.0,
        )

        self.assertEqual(len(runtime.execution_repo.order_states()), 1)
        self.assertEqual(len(runtime.execution_repo.fills()), 1)


if __name__ == "__main__":
    unittest.main()
