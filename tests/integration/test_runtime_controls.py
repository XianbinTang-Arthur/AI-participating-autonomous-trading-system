from __future__ import annotations

import unittest

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.events.envelopes import build_envelope
from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.execution import OrderIntent


class TestRuntimeControls(unittest.IsolatedAsyncioTestCase):
    async def test_halt_blocks_execution_and_resume_allows_it(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "local_demo",
                "mode": "paper_live",
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "disabled",
                "account_read_enabled": False,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "enabled_decision_timeframes": ("15m",),
                "decision_min_interval_seconds_15m": 0.0,
                "decision_min_price_move_bps": 0.0,
                "decision_min_momentum_delta": 0.0,
            }
        )
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
        runtime.audit_repo.upsert(
            DecisionAuditRecord(
                decision_id="decision_resume_test",
                decision_context_ref="evt_decision_resume_test",
            )
        )
        resumed_intent = halted_intent.model_copy(
            update={
                "intent_id": "intent_resume_test",
                "decision_id": "decision_resume_test",
                "idempotency_key": "intent_resume_test",
            }
        )
        await runtime.order_manager.handle_order_intent(
            {
                "topic": "execution.order_intents",
                "key": settings.default_symbol,
                "payload": build_envelope(
                    topic="execution.order_intents",
                    key=settings.default_symbol,
                    payload_model=resumed_intent,
                    source_component="test",
                ).model_dump(mode="json"),
            }
        )

        self.assertGreaterEqual(len(runtime.execution_repo.order_states()), 1)
        self.assertGreaterEqual(len(runtime.execution_repo.fills()), 1)


if __name__ == "__main__":
    unittest.main()
