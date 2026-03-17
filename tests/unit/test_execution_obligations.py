from __future__ import annotations

import unittest

from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent, OrderIntent, OrderState
from aats.schemas.exchange import ExchangeAccountSnapshot, ExchangeBalance
from aats.services.execution_engine.obligations import ExecutionObligationService
from aats.services.execution_engine.order_manager import OrderManager
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.storage.event_store import InMemoryEventStore
from aats.storage.execution_repo import InMemoryExecutionRepository
from aats.storage.obligation_repo import InMemoryExecutionObligationRepository


class _SubmittedAdapter:
    def preview_client_order_id(self, intent: OrderIntent) -> str | None:
        return f"cl{intent.idempotency_key}"

    async def submit(self, intent: OrderIntent):
        now = utc_now()
        state = OrderState(
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            client_order_id=f"cl{intent.idempotency_key}",
            venue="OKX",
            exchange_order_id=f"ord_{intent.intent_id}",
            status="SUBMITTED",
            submission_mode="guarded_simulated_submit",
            submitted_ts=now,
            last_update_ts=now,
            requested_qty=intent.quantity,
            filled_qty=0.0,
            remaining_qty=intent.quantity,
            average_fill_price=None,
            fees=0.0,
            product_type=intent.product_type,
            target_leverage=intent.target_leverage,
            margin_mode=intent.margin_mode,
            exposure_side=intent.exposure_side,
            position_intent=intent.position_intent,
            submission_payload={},
        )
        return state, []

    async def sync(self, open_order_states):
        return [], []

    async def cancel(self, order_state: OrderState):
        return order_state, []

    def readiness(self):
        return {"backend": "okx"}


class _FailedAdapter(_SubmittedAdapter):
    async def submit(self, intent: OrderIntent):
        now = utc_now()
        state = OrderState(
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            client_order_id=f"cl{intent.idempotency_key}",
            venue="OKX",
            exchange_order_id=None,
            status="FAILED",
            submission_mode="guarded_simulated_submit",
            submitted_ts=now,
            last_update_ts=now,
            requested_qty=intent.quantity,
            filled_qty=0.0,
            remaining_qty=intent.quantity,
            average_fill_price=None,
            fees=0.0,
            product_type=intent.product_type,
            target_leverage=intent.target_leverage,
            margin_mode=intent.margin_mode,
            exposure_side=intent.exposure_side,
            position_intent=intent.position_intent,
            execution_error="submit_failed",
            submission_payload={},
        )
        return state, []


class _FilledAdapter(_SubmittedAdapter):
    async def submit(self, intent: OrderIntent):
        now = utc_now()
        client_order_id = f"cl{intent.idempotency_key}"
        state = OrderState(
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            client_order_id=client_order_id,
            venue="OKX",
            exchange_order_id=f"ord_{intent.intent_id}",
            status="FILLED",
            submission_mode="guarded_simulated_submit",
            submitted_ts=now,
            last_update_ts=now,
            requested_qty=intent.quantity,
            filled_qty=intent.quantity,
            remaining_qty=0.0,
            average_fill_price=60_000.0,
            fees=0.0,
            product_type=intent.product_type,
            target_leverage=intent.target_leverage,
            margin_mode=intent.margin_mode,
            exposure_side=intent.exposure_side,
            position_intent=intent.position_intent,
            submission_payload={},
        )
        fill = FillEvent(
            fill_id=f"fill_{intent.intent_id}",
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            client_order_id=client_order_id,
            exchange_order_id=f"ord_{intent.intent_id}",
            symbol=intent.symbol,
            venue="OKX",
            side=intent.side,
            fill_qty=intent.quantity,
            fill_price=60_000.0,
            fee_amount=0.0,
            fee_currency="USDT",
            product_type=intent.product_type,
            target_leverage=intent.target_leverage,
            margin_mode=intent.margin_mode,
            exposure_side=intent.exposure_side,
            position_intent=intent.position_intent,
            liquidity_role="taker",
            exchange_timestamp=now,
            ingestion_timestamp=now,
        )
        return state, [fill]


class _RecordingOutboxPublisher:
    def __init__(self, obligation_repo) -> None:
        self.order_state_obligations = []
        self.fill_obligations = []
        self.obligation_repo = obligation_repo

    async def persist_order_state(self, *, order_state: OrderState, key: str, obligation=None) -> OrderState:
        self.order_state_obligations.append(obligation)
        if obligation is not None:
            self.obligation_repo.save_obligation(obligation)
        return order_state

    async def persist_fill(self, *, fill: FillEvent, obligation=None) -> bool:
        self.fill_obligations.append(obligation)
        if obligation is not None:
            self.obligation_repo.save_obligation(obligation)
        return True


class TestExecutionObligations(unittest.IsolatedAsyncioTestCase):
    async def test_second_spot_buy_is_blocked_by_local_reserved_quote_balance(self) -> None:
        snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=100.0, available=100.0, frozen=0.0)],
        )
        obligation_repo = InMemoryExecutionObligationRepository()
        settings = AATSSettings.model_validate({"account_backend": "okx", "account_read_enabled": True})
        manager = OrderManager(
            settings=settings,
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=_SubmittedAdapter(),
            execution_repo=InMemoryExecutionRepository(),
            obligation_service=ExecutionObligationService(
                settings=settings,
                obligation_repo=obligation_repo,
                account_snapshot_loader=lambda: _return_snapshot(snapshot),
                price_provider=lambda _symbol: 60_000.0,
            ),
            kill_switch=KillSwitch(),
        )

        await manager.handle_order_intent(_intent_message(_intent("intent_1", "decision_1", "client_1")))
        await manager.handle_order_intent(_intent_message(_intent("intent_2", "decision_2", "client_2")))

        second_order = manager.execution_repo.get_order_state("clclient_2")
        self.assertIsNotNone(second_order)
        self.assertEqual(second_order.status, "BLOCKED")
        self.assertEqual(len(obligation_repo.active_obligations()), 1)

    async def test_failed_terminal_order_releases_reserved_amount(self) -> None:
        snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=100.0, available=100.0, frozen=0.0)],
        )
        obligation_repo = InMemoryExecutionObligationRepository()
        settings = AATSSettings.model_validate({"account_backend": "okx", "account_read_enabled": True})
        manager = OrderManager(
            settings=settings,
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=_FailedAdapter(),
            execution_repo=InMemoryExecutionRepository(),
            obligation_service=ExecutionObligationService(
                settings=settings,
                obligation_repo=obligation_repo,
                account_snapshot_loader=lambda: _return_snapshot(snapshot),
                price_provider=lambda _symbol: 60_000.0,
            ),
            kill_switch=KillSwitch(),
        )

        await manager.handle_order_intent(_intent_message(_intent("intent_failed", "decision_failed", "client_failed")))

        obligation = obligation_repo.get_obligation("clclient_failed")
        self.assertIsNotNone(obligation)
        self.assertEqual(obligation.status, "FAILED")
        self.assertAlmostEqual(obligation.released_amount, 60.0)
        self.assertAlmostEqual(ExecutionObligationService.remaining_amount(obligation), 0.0)

    async def test_filled_order_consumes_reserved_amount(self) -> None:
        snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=100.0, available=100.0, frozen=0.0)],
        )
        obligation_repo = InMemoryExecutionObligationRepository()
        settings = AATSSettings.model_validate({"account_backend": "okx", "account_read_enabled": True})
        manager = OrderManager(
            settings=settings,
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=_FilledAdapter(),
            execution_repo=InMemoryExecutionRepository(),
            obligation_service=ExecutionObligationService(
                settings=settings,
                obligation_repo=obligation_repo,
                account_snapshot_loader=lambda: _return_snapshot(snapshot),
                price_provider=lambda _symbol: 60_000.0,
            ),
            kill_switch=KillSwitch(),
        )

        await manager.handle_order_intent(_intent_message(_intent("intent_filled", "decision_filled", "client_filled")))

        obligation = obligation_repo.get_obligation("clclient_filled")
        self.assertIsNotNone(obligation)
        self.assertEqual(obligation.status, "RELEASED")
        self.assertAlmostEqual(obligation.consumed_amount, 60.0)
        self.assertAlmostEqual(obligation.released_amount, 0.0)

    async def test_outbox_path_does_not_finalize_obligation_before_fill_consumption(self) -> None:
        snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=100.0, available=100.0, frozen=0.0)],
        )
        obligation_repo = InMemoryExecutionObligationRepository()
        outbox = _RecordingOutboxPublisher(obligation_repo)
        settings = AATSSettings.model_validate({"account_backend": "okx", "account_read_enabled": True})
        manager = OrderManager(
            settings=settings,
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=_FilledAdapter(),
            execution_repo=InMemoryExecutionRepository(),
            obligation_service=ExecutionObligationService(
                settings=settings,
                obligation_repo=obligation_repo,
                account_snapshot_loader=lambda: _return_snapshot(snapshot),
                price_provider=lambda _symbol: 60_000.0,
            ),
            execution_outbox_publisher=outbox,
            kill_switch=KillSwitch(),
        )

        await manager.handle_order_intent(_intent_message(_intent("intent_outbox_filled", "decision_outbox_filled", "client_outbox_filled")))

        obligation = obligation_repo.get_obligation("clclient_outbox_filled")
        self.assertIsNotNone(obligation)
        self.assertEqual(len(outbox.order_state_obligations), 3)
        self.assertIsNotNone(outbox.order_state_obligations[0])
        self.assertEqual(outbox.order_state_obligations[0].client_order_id, "clclient_outbox_filled")
        self.assertEqual(outbox.order_state_obligations[1:], [None, None])
        self.assertEqual(len(outbox.fill_obligations), 1)
        self.assertAlmostEqual(outbox.fill_obligations[0].consumed_amount, 60.0)
        self.assertAlmostEqual(outbox.fill_obligations[0].released_amount, 0.0)
        self.assertEqual(obligation.status, "RELEASED")
        self.assertAlmostEqual(obligation.consumed_amount, 60.0)
        self.assertAlmostEqual(obligation.released_amount, 0.0)

    async def test_outbox_path_finalizes_failed_zero_fill_obligation_with_terminal_state(self) -> None:
        snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=100.0, available=100.0, frozen=0.0)],
        )
        obligation_repo = InMemoryExecutionObligationRepository()
        outbox = _RecordingOutboxPublisher(obligation_repo)
        settings = AATSSettings.model_validate({"account_backend": "okx", "account_read_enabled": True})
        manager = OrderManager(
            settings=settings,
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=_FailedAdapter(),
            execution_repo=InMemoryExecutionRepository(),
            obligation_service=ExecutionObligationService(
                settings=settings,
                obligation_repo=obligation_repo,
                account_snapshot_loader=lambda: _return_snapshot(snapshot),
                price_provider=lambda _symbol: 60_000.0,
            ),
            execution_outbox_publisher=outbox,
            kill_switch=KillSwitch(),
        )

        await manager.handle_order_intent(_intent_message(_intent("intent_outbox_failed", "decision_outbox_failed", "client_outbox_failed")))

        obligation = obligation_repo.get_obligation("clclient_outbox_failed")
        self.assertIsNotNone(obligation)
        self.assertEqual(len(outbox.order_state_obligations), 3)
        self.assertIsNotNone(outbox.order_state_obligations[0])
        self.assertEqual(outbox.order_state_obligations[1], None)
        self.assertIsNotNone(outbox.order_state_obligations[2])
        self.assertEqual(outbox.order_state_obligations[2].status, "FAILED")
        self.assertAlmostEqual(outbox.order_state_obligations[2].released_amount, 60.0)
        self.assertEqual(obligation.status, "FAILED")
        self.assertAlmostEqual(obligation.released_amount, 60.0)


async def _return_snapshot(snapshot: ExchangeAccountSnapshot) -> ExchangeAccountSnapshot:
    return snapshot


def _intent(intent_id: str, decision_id: str, idempotency_key: str) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        decision_id=decision_id,
        symbol="BTC-USDT",
        side="buy",
        quantity=0.001,
        execution_style="exchange",
        order_type="market",
        urgency="medium",
        time_in_force="IOC",
        reduce_only=False,
        close_only=False,
        idempotency_key=idempotency_key,
    )


def _intent_message(intent: OrderIntent) -> dict:
    envelope = build_envelope(
        topic=topics.ORDER_INTENTS,
        key=intent.symbol,
        payload_model=intent,
        source_component="test",
    )
    return {"topic": topics.ORDER_INTENTS, "key": intent.symbol, "payload": envelope.model_dump(mode="json")}


if __name__ == "__main__":
    unittest.main()
