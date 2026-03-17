from __future__ import annotations

import unittest

from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent, OrderIntent, OrderState
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


class _PreviewingFailingAdapter(_FailingAdapter):
    def preview_client_order_id(self, intent: OrderIntent) -> str | None:
        return f"cl{intent.idempotency_key}"

    async def submit(self, intent: OrderIntent):
        state = OrderState(
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            client_order_id=f"cl{intent.idempotency_key}",
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


class _PreviewingExceptionAdapter(_FailingAdapter):
    def preview_client_order_id(self, intent: OrderIntent) -> str | None:
        return f"cl{intent.idempotency_key}"

    async def submit(self, intent: OrderIntent):
        raise RuntimeError("preview_exception")


class _BackfillAdapter(_FailingAdapter):
    def __init__(self) -> None:
        self.synced_client_order_ids: list[str] = []

    async def sync(self, open_order_states):
        self.synced_client_order_ids = [state.client_order_id for state in open_order_states]
        if not open_order_states:
            return [], []
        state = open_order_states[0]
        fill = FillEvent(
            fill_id="fill_backfill_1",
            decision_id=state.decision_id,
            intent_id=state.intent_id,
            client_order_id=state.client_order_id,
            exchange_order_id=state.exchange_order_id,
            symbol=state.symbol,
            venue="OKX",
            side="buy",
            fill_qty=state.filled_qty,
            fill_price=state.average_fill_price or 100.0,
            fee_amount=state.fees,
            liquidity_role="taker",
            exchange_timestamp=state.last_exchange_update_ts or state.last_update_ts or state.created_at,
            ingestion_timestamp=state.last_update_ts or state.created_at,
        )
        return [state], [fill]


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

    async def test_preview_client_order_id_is_used_for_provisional_okx_states(self) -> None:
        repo = InMemoryExecutionRepository()
        manager = OrderManager(
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=_PreviewingFailingAdapter(),
            execution_repo=repo,
            kill_switch=KillSwitch(),
        )
        intent = OrderIntent(
            intent_id="intent_error_2",
            decision_id="decision_error_2",
            symbol="BTC-USDT",
            side="sell",
            quantity=0.001,
            execution_style="exchange",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            reduce_only=False,
            close_only=False,
            idempotency_key="preview_id",
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

        self.assertIsNotNone(repo.get_order_state("clpreview_id"))
        self.assertIsNone(repo.get_order_state("preview_id"))

    async def test_preview_client_order_id_is_used_after_adapter_exception(self) -> None:
        repo = InMemoryExecutionRepository()
        manager = OrderManager(
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=_PreviewingExceptionAdapter(),
            execution_repo=repo,
            kill_switch=KillSwitch(),
        )
        intent = OrderIntent(
            intent_id="intent_error_3",
            decision_id="decision_error_3",
            symbol="BTC-USDT",
            side="buy",
            quantity=0.001,
            execution_style="exchange",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            reduce_only=False,
            close_only=False,
            idempotency_key="preview_exception_id",
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

        persisted = repo.get_order_state("clpreview_exception_id")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.status, "FAILED")
        self.assertIsNone(repo.get_order_state("preview_exception_id"))

    async def test_sync_backfills_terminal_filled_order_without_local_fills(self) -> None:
        repo = InMemoryExecutionRepository()
        adapter = _BackfillAdapter()
        manager = OrderManager(
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=adapter,
            execution_repo=repo,
            kill_switch=KillSwitch(),
        )
        filled_state = OrderState(
            decision_id="decision_fill_backfill",
            intent_id="intent_fill_backfill",
            symbol="BTC-USDT",
            client_order_id="clord_fill_backfill",
            venue="OKX",
            exchange_order_id="ord_fill_backfill",
            status="FILLED",
            exchange_status="filled",
            submitted_ts=utc_now(),
            last_update_ts=utc_now(),
            last_exchange_update_ts=utc_now(),
            requested_qty=0.001,
            filled_qty=0.001,
            remaining_qty=0.0,
            average_fill_price=100.0,
            fees=0.1,
        )
        repo.save_order_state(filled_state)

        await manager.sync_exchange_state()

        self.assertIn("clord_fill_backfill", adapter.synced_client_order_ids)
        fills = repo.fills_for_order("clord_fill_backfill")
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].fill_id, "fill_backfill_1")


if __name__ == "__main__":
    unittest.main()
