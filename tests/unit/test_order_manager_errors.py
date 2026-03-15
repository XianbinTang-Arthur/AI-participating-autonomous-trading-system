from __future__ import annotations

import unittest

from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.execution import OrderIntent, OrderState
from aats.services.execution_engine.order_manager import OrderManager
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.storage.event_store import InMemoryEventStore
from aats.storage.execution_repo import InMemoryExecutionRepository


class _FailingAdapter:
    async def submit(self, intent: OrderIntent):
        state = OrderState(
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            client_order_id=intent.idempotency_key,
            venue="OKX",
            exchange_order_id=None,
            status="FAILED",
            submission_mode="guarded_simulated_submit",
            submitted_ts=intent.created_at,
            last_update_ts=intent.created_at,
            requested_qty=intent.quantity,
            filled_qty=0.0,
            remaining_qty=intent.quantity,
            average_fill_price=None,
            fees=0.0,
            execution_error="simulated_failure",
            submission_payload={},
        )
        return state, []

    async def sync(self, open_order_states):
        return [], []

    async def cancel(self, order_state: OrderState):
        return order_state, []

    def readiness(self):
        return {"backend": "okx", "exchange_submit_allowed": False, "submit_blocked_reasons": ["simulated_failure"]}


class TestOrderManagerExecutionErrorHistory(unittest.IsolatedAsyncioTestCase):
    async def test_failed_order_publishes_execution_error_summary(self) -> None:
        event_store = InMemoryEventStore()
        bus = InMemoryEventBus(event_store=event_store, persistence_mode="strict")
        manager = OrderManager(
            bus=bus,
            adapter=_FailingAdapter(),
            execution_repo=InMemoryExecutionRepository(),
            kill_switch=KillSwitch(),
        )
        intent = OrderIntent(
            intent_id="intent_error_1",
            decision_id="decision_error_1",
            symbol="BTC-USDT",
            side="buy",
            quantity=0.001,
            execution_style="exchange",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            reduce_only=False,
            close_only=False,
            idempotency_key="client_error_1",
        )
        envelope = build_envelope(
            topic=topics.ORDER_INTENTS,
            key=intent.symbol,
            payload_model=intent,
            source_component="test",
        )

        await manager.handle_order_intent(
            {"topic": topics.ORDER_INTENTS, "key": intent.symbol, "payload": envelope.model_dump(mode="json")}
        )

        summaries = event_store.by_topic(topics.EXECUTION_ERROR_SUMMARIES)
        self.assertEqual(len(summaries), 1)
        payload = summaries[0].payload
        self.assertEqual(payload["decision_id"], "decision_error_1")
        self.assertEqual(payload["order_id"], "client_error_1")
        self.assertEqual(payload["message"], "simulated_failure")


if __name__ == "__main__":
    unittest.main()
