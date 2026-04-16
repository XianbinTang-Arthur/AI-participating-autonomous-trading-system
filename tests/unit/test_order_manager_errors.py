from __future__ import annotations

import unittest

from aats.bootstrap.settings import AATSSettings
from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent, LegOrderIntent, OrderIntent, OrderState, order_intent_from_leg_order_intent
from aats.schemas.governance import RiskDecision
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


class _BusyFailingAdapter(_PreviewingFailingAdapter):
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
            execution_error="code=50013 sMsg=Systems are busy",
            submission_payload={},
            position_intent=intent.position_intent,
            product_type=intent.product_type,
            target_leverage=intent.target_leverage,
            margin_mode=intent.margin_mode,
            exposure_side=intent.exposure_side,
        )
        return state, []


class _CountingAdapter(_FailingAdapter):
    def __init__(self) -> None:
        self.submit_calls = 0

    def preview_client_order_id(self, intent: OrderIntent) -> str | None:
        return f"cl{intent.idempotency_key}"

    async def submit(self, intent: OrderIntent):
        self.submit_calls += 1
        return await super().submit(intent)

    async def submit_leg_order(self, leg_intent: LegOrderIntent):
        self.submit_calls += 1
        state = OrderState(
            decision_id=leg_intent.decision_id,
            intent_id=leg_intent.leg_intent_id,
            symbol=leg_intent.symbol,
            client_order_id=f"cl{leg_intent.idempotency_key}",
            venue="OKX",
            exchange_order_id=None,
            status="FAILED",
            submission_mode="guarded_simulated_submit",
            submitted_ts=leg_intent.created_at,
            last_update_ts=leg_intent.created_at,
            requested_qty=leg_intent.quantity,
            filled_qty=0.0,
            remaining_qty=leg_intent.quantity,
            average_fill_price=None,
            fees=0.0,
            execution_error="unexpected_submit_leg_order",
            submission_payload={},
        )
        return state, []


class TestOrderManagerExecutionErrorHistory(unittest.IsolatedAsyncioTestCase):
    async def test_failed_order_publishes_execution_error_summary(self) -> None:
        event_store = InMemoryEventStore()
        bus = InMemoryEventBus(event_store=event_store, persistence_mode="strict")
        manager = OrderManager(
            settings=AATSSettings.model_validate({}),
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
            settings=AATSSettings.model_validate({}),
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
            settings=AATSSettings.model_validate({}),
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

    async def test_unknown_submit_blocks_duplicate_submit_for_same_execution_chain(self) -> None:
        repo = InMemoryExecutionRepository()
        adapter = _CountingAdapter()
        manager = OrderManager(
            settings=AATSSettings.model_validate({}),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=adapter,
            execution_repo=repo,
            kill_switch=KillSwitch(),
        )
        now = utc_now()
        repo.save_order_state(
            OrderState(
                decision_id="decision_unknown_chain_existing",
                execution_chain_id="chain_unknown_1",
                intent_id="intent_unknown_chain_existing",
                symbol="BTC-USDT",
                client_order_id="clord_unknown_chain_existing",
                venue="OKX",
                exchange_order_id=None,
                status="SUBMITTED",
                exchange_status="live",
                submitted_ts=now,
                last_update_ts=now,
                requested_qty=0.001,
                filled_qty=0.0,
                remaining_qty=0.001,
                average_fill_price=None,
                fees=0.0,
                execution_error="submission_unknown_check_exchange:OKXRequestError",
            )
        )
        intent = OrderIntent(
            intent_id="intent_unknown_chain_new",
            execution_chain_id="chain_unknown_1",
            decision_id="decision_unknown_chain_new",
            symbol="BTC-USDT",
            side="buy",
            quantity=0.001,
            execution_style="exchange",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            reduce_only=False,
            close_only=False,
            idempotency_key="unknown_chain_new",
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

        blocked = repo.get_order_state("clunknown_chain_new")
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.status, "BLOCKED")
        self.assertEqual(blocked.submission_mode, "unknown_write_duplicate_submit_blocked")
        self.assertEqual(
            blocked.execution_error,
            "unknown_submit_requires_reconciliation_for_execution_chain:clord_unknown_chain_existing",
        )
        self.assertEqual(adapter.submit_calls, 0)

    async def test_unknown_write_blocks_new_risk_submit_for_same_symbol(self) -> None:
        repo = InMemoryExecutionRepository()
        adapter = _CountingAdapter()
        manager = OrderManager(
            settings=AATSSettings.model_validate({}),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=adapter,
            execution_repo=repo,
            kill_switch=KillSwitch(),
        )
        now = utc_now()
        repo.save_order_state(
            OrderState(
                decision_id="decision_unknown_symbol_existing",
                execution_chain_id="chain_unknown_symbol_existing",
                intent_id="intent_unknown_symbol_existing",
                symbol="BTC-USDT",
                client_order_id="clord_unknown_symbol_existing",
                venue="OKX",
                exchange_order_id="ord_unknown_symbol_existing",
                status="CANCEL_PENDING",
                exchange_status="live",
                submitted_ts=now,
                cancellation_requested_ts=now,
                last_update_ts=now,
                requested_qty=0.001,
                filled_qty=0.0,
                remaining_qty=0.001,
                average_fill_price=None,
                fees=0.0,
                execution_error="cancel_unknown_check_exchange:OKXRequestError",
            )
        )
        intent = OrderIntent(
            intent_id="intent_unknown_symbol_new",
            execution_chain_id="chain_unknown_symbol_new",
            decision_id="decision_unknown_symbol_new",
            symbol="BTC-USDT",
            side="buy",
            quantity=0.002,
            execution_style="exchange",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            reduce_only=False,
            close_only=False,
            idempotency_key="unknown_symbol_new",
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

        blocked = repo.get_order_state("clunknown_symbol_new")
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.status, "BLOCKED")
        self.assertEqual(blocked.submission_mode, "unknown_write_symbol_risk_blocked")
        self.assertEqual(
            blocked.execution_error,
            "unknown_cancel_blocks_new_risk_actions_for_symbol:clord_unknown_symbol_existing",
        )
        self.assertEqual(adapter.submit_calls, 0)

    async def test_risk_reducing_submit_is_not_blocked_by_unknown_write_on_same_symbol(self) -> None:
        repo = InMemoryExecutionRepository()
        adapter = _CountingAdapter()
        manager = OrderManager(
            settings=AATSSettings.model_validate({}),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=adapter,
            execution_repo=repo,
            kill_switch=KillSwitch(),
        )
        now = utc_now()
        repo.save_order_state(
            OrderState(
                decision_id="decision_reduce_allowed_existing",
                execution_chain_id="chain_reduce_allowed_existing",
                intent_id="intent_reduce_allowed_existing",
                symbol="BTC-USDT",
                client_order_id="clord_reduce_allowed_existing",
                venue="OKX",
                exchange_order_id=None,
                status="SUBMITTED",
                exchange_status="live",
                submitted_ts=now,
                last_update_ts=now,
                requested_qty=0.001,
                filled_qty=0.0,
                remaining_qty=0.001,
                average_fill_price=None,
                fees=0.0,
                execution_error="submission_unknown_check_exchange:OKXRequestError",
            )
        )
        intent = OrderIntent(
            intent_id="intent_reduce_allowed_new",
            execution_chain_id="chain_reduce_allowed_new",
            decision_id="decision_reduce_allowed_new",
            symbol="BTC-USDT",
            side="sell",
            quantity=0.001,
            execution_style="exchange",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            reduce_only=True,
            close_only=True,
            execution_action="exit",
            position_intent="close_long",
            idempotency_key="reduce_allowed_new",
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

        persisted = repo.get_order_state("reduce_allowed_new")
        self.assertIsNotNone(persisted)
        self.assertEqual(adapter.submit_calls, 1)
        self.assertEqual(persisted.status, "FAILED")
        self.assertEqual(persisted.execution_error, "simulated_failure")

    async def test_sync_backfills_terminal_filled_order_without_local_fills(self) -> None:
        repo = InMemoryExecutionRepository()
        adapter = _BackfillAdapter()
        manager = OrderManager(
            settings=AATSSettings.model_validate({}),
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

    def test_sync_candidates_prioritize_unknown_write_states(self) -> None:
        repo = InMemoryExecutionRepository()
        manager = OrderManager(
            settings=AATSSettings.model_validate({}),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=_FailingAdapter(),
            execution_repo=repo,
            kill_switch=KillSwitch(),
        )
        now = utc_now()
        normal_state = OrderState(
            decision_id="decision_sync_normal",
            intent_id="intent_sync_normal",
            symbol="BTC-USDT",
            client_order_id="clord_sync_normal",
            venue="OKX",
            exchange_order_id="ord_sync_normal",
            status="SUBMITTED",
            exchange_status="live",
            submitted_ts=now,
            last_update_ts=now,
            last_exchange_update_ts=now,
            requested_qty=0.001,
            filled_qty=0.0,
            remaining_qty=0.001,
            average_fill_price=None,
            fees=0.0,
        )
        unknown_state = normal_state.model_copy(
            update={
                "decision_id": "decision_sync_unknown",
                "intent_id": "intent_sync_unknown",
                "client_order_id": "clord_sync_unknown",
                "exchange_order_id": None,
                "execution_error": "submission_unknown_check_exchange:OKXRequestError",
            }
        )
        terminal_filled = normal_state.model_copy(
            update={
                "decision_id": "decision_sync_filled",
                "intent_id": "intent_sync_filled",
                "client_order_id": "clord_sync_filled",
                "exchange_order_id": "ord_sync_filled",
                "status": "FILLED",
                "exchange_status": "filled",
                "filled_qty": 0.001,
                "remaining_qty": 0.0,
                "average_fill_price": 100.0,
                "fees": 0.1,
            }
        )
        repo.save_order_state(normal_state)
        repo.save_order_state(unknown_state)
        repo.save_order_state(terminal_filled)

        candidates = manager._sync_candidates()

        self.assertEqual(candidates[0].client_order_id, "clord_sync_unknown")
        self.assertIn("clord_sync_normal", [state.client_order_id for state in candidates])
        self.assertIn("clord_sync_filled", [state.client_order_id for state in candidates])

    async def test_transient_close_failures_enter_retry_cooldown(self) -> None:
        repo = InMemoryExecutionRepository()
        settings = AATSSettings.model_validate({"strategy_transient_close_retry_cooldown_seconds": 90.0})
        manager = OrderManager(
            settings=settings,
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=_BusyFailingAdapter(),
            execution_repo=repo,
            kill_switch=KillSwitch(),
        )
        first_intent = OrderIntent(
            intent_id="intent_close_1",
            decision_id="decision_close_1",
            symbol="BTC-USDT",
            side="sell",
            quantity=0.0028,
            execution_style="exchange",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            reduce_only=False,
            close_only=True,
            idempotency_key="close_1",
            product_type="derivatives",
            margin_mode="cross",
            exposure_side="long",
            position_intent="close_long",
        )
        second_intent = first_intent.model_copy(
            update={
                "intent_id": "intent_close_2",
                "decision_id": "decision_close_2",
                "idempotency_key": "close_2",
            }
        )

        await manager.handle_order_intent(
            {"topic": topics.ORDER_INTENTS, "key": first_intent.symbol, "payload": build_envelope(topic=topics.ORDER_INTENTS, key=first_intent.symbol, payload_model=first_intent, source_component="test").model_dump(mode="json")}
        )
        await manager.handle_order_intent(
            {"topic": topics.ORDER_INTENTS, "key": second_intent.symbol, "payload": build_envelope(topic=topics.ORDER_INTENTS, key=second_intent.symbol, payload_model=second_intent, source_component="test").model_dump(mode="json")}
        )

        first = repo.get_order_state("clclose_1")
        second = repo.get_order_state("close_2")
        self.assertIsNotNone(first)
        self.assertEqual(first.status, "FAILED")
        self.assertIsNotNone(second)
        self.assertEqual(second.status, "BLOCKED")
        self.assertEqual(second.submission_mode, "local_retry_cooldown")
        self.assertIn("transient_close_retry_cooldown_active", second.execution_error)

    async def test_high_urgency_close_bypasses_retry_cooldown(self) -> None:
        repo = InMemoryExecutionRepository()
        settings = AATSSettings.model_validate({"strategy_transient_close_retry_cooldown_seconds": 90.0})
        manager = OrderManager(
            settings=settings,
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=_BusyFailingAdapter(),
            execution_repo=repo,
            kill_switch=KillSwitch(),
        )
        first_intent = OrderIntent(
            intent_id="intent_close_high_1",
            decision_id="decision_close_high_1",
            symbol="BTC-USDT",
            side="sell",
            quantity=0.01,
            execution_style="exchange",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            close_only=True,
            idempotency_key="close_high_1",
            product_type="derivatives",
            margin_mode="cross",
            exposure_side="long",
            position_intent="close_long",
        )
        second_intent = first_intent.model_copy(
            update={
                "intent_id": "intent_close_high_2",
                "decision_id": "decision_close_high_2",
                "idempotency_key": "close_high_2",
                "urgency": "high",
            }
        )

        await manager.handle_order_intent(
            {"topic": topics.ORDER_INTENTS, "key": first_intent.symbol, "payload": build_envelope(topic=topics.ORDER_INTENTS, key=first_intent.symbol, payload_model=first_intent, source_component="test").model_dump(mode="json")}
        )
        await manager.handle_order_intent(
            {"topic": topics.ORDER_INTENTS, "key": second_intent.symbol, "payload": build_envelope(topic=topics.ORDER_INTENTS, key=second_intent.symbol, payload_model=second_intent, source_component="test").model_dump(mode="json")}
        )

        second = repo.get_order_state("clclose_high_2")
        self.assertIsNotNone(second)
        self.assertEqual(second.status, "FAILED")

    async def test_leg_risk_blocked_order_never_reaches_adapter_submit(self) -> None:
        repo = InMemoryExecutionRepository()
        adapter = _CountingAdapter()
        manager = OrderManager(
            settings=AATSSettings.model_validate({}),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=adapter,
            execution_repo=repo,
            leg_risk_evaluator=lambda _leg_intent: RiskDecision(
                decision_id="leg_risk_blocked",
                approved=False,
                modified=True,
                capped_target_position_qty=0.0,
                capped_target_notional=0.0,
                projected_notional=0.0,
                risk_score=1.0,
                only_reduce_required=True,
                risk_limit_breached=True,
                rejection_reasons=["risk_max_long_notional_exceeded", "leg_only_reduce_mode_active"],
            ),
            kill_switch=KillSwitch(),
        )

        await manager.submit_leg_order(
            leg_intent=LegOrderIntent(
                leg_intent_id="leg_blocked_1",
                decision_id="decision_leg_blocked_1",
                symbol="BTC-USDT-SWAP",
                side="buy",
                pos_side="long",
                action="open",
                quantity=0.001,
                execution_style="exchange",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                idempotency_key="leg_blocked_1",
                product_type="derivatives",
                margin_mode="cross",
                td_mode="cross",
                target_leverage=2.0,
                exposure_side="long",
            )
        )

        persisted = repo.get_order_state("clleg_blocked_1")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.status, "BLOCKED")
        self.assertEqual(persisted.submission_mode, "leg_risk_blocked")
        self.assertIn("leg_risk_blocked", persisted.execution_error)
        self.assertEqual(adapter.submit_calls, 0)

    async def test_leg_overlay_rollout_blocked_order_never_reaches_adapter_submit(self) -> None:
        repo = InMemoryExecutionRepository()
        adapter = _CountingAdapter()
        manager = OrderManager(
            settings=AATSSettings.model_validate(
                {
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "live_submit_enabled": True,
                    "guarded_execution_dry_run": False,
                    "okx_simulated_trading": False,
                    "strategy_hedge_overlay_enabled": True,
                    "strategy_hedge_independent_enabled": True,
                    "strategy_hedge_independent_rollout_stage": "dry_run",
                }
            ),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=adapter,
            execution_repo=repo,
            kill_switch=KillSwitch(),
        )

        await manager.submit_leg_order(
            leg_intent=LegOrderIntent(
                leg_intent_id="leg_overlay_blocked_1",
                decision_id="decision_leg_overlay_blocked_1",
                symbol="BTC-USDT-SWAP",
                side="buy",
                pos_side="long",
                action="open",
                quantity=0.001,
                execution_style="exchange",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                idempotency_key="leg_overlay_blocked_1",
                product_type="derivatives",
                margin_mode="cross",
                td_mode="cross",
                position_mode="long_short_mode",
                target_leverage=2.0,
                exposure_side="long",
                strategy_execution_mode="independent_long_book",
            )
        )

        persisted = repo.get_order_state("clleg_overlay_blocked_1")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.status, "BLOCKED")
        self.assertEqual(persisted.submission_mode, "leg_overlay_rollout_blocked")
        self.assertIn("independent_overlay_rollout_stage_blocks_live_runtime", persisted.execution_error)
        self.assertEqual(adapter.submit_calls, 0)

    async def test_leg_overlay_rollout_blocked_when_family_mode_uses_independent_books(self) -> None:
        repo = InMemoryExecutionRepository()
        adapter = _CountingAdapter()
        manager = OrderManager(
            settings=AATSSettings.model_validate(
                {
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "live_submit_enabled": True,
                    "guarded_execution_dry_run": False,
                    "okx_simulated_trading": False,
                    "strategy_hedge_overlay_enabled": True,
                    "strategy_hedge_independent_enabled": True,
                    "strategy_hedge_independent_rollout_stage": "dry_run",
                }
            ),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=adapter,
            execution_repo=repo,
            kill_switch=KillSwitch(),
        )

        await manager.submit_leg_order(
            leg_intent=LegOrderIntent(
                leg_intent_id="leg_overlay_family_mode_blocked_1",
                decision_id="decision_leg_overlay_family_mode_blocked_1",
                symbol="BTC-USDT-SWAP",
                side="buy",
                pos_side="long",
                action="open",
                quantity=0.001,
                execution_style="exchange",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                idempotency_key="leg_overlay_family_mode_blocked_1",
                product_type="derivatives",
                margin_mode="cross",
                td_mode="cross",
                position_mode="long_short_mode",
                target_leverage=2.0,
                exposure_side="long",
                strategy_execution_mode="independent_books",
            )
        )

        persisted = repo.get_order_state("clleg_overlay_family_mode_blocked_1")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.status, "BLOCKED")
        self.assertEqual(persisted.submission_mode, "leg_overlay_rollout_blocked")
        self.assertIn("independent_overlay_rollout_stage_blocks_live_runtime", persisted.execution_error)
        self.assertEqual(adapter.submit_calls, 0)

    async def test_leg_overlay_live_rollout_allows_direct_submit(self) -> None:
        repo = InMemoryExecutionRepository()
        adapter = _CountingAdapter()
        manager = OrderManager(
            settings=AATSSettings.model_validate(
                {
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "live_submit_enabled": True,
                    "guarded_execution_dry_run": False,
                    "okx_simulated_trading": False,
                    "strategy_hedge_overlay_enabled": True,
                    "strategy_hedge_opportunistic_enabled": True,
                    "strategy_hedge_opportunistic_rollout_stage": "live",
                }
            ),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=adapter,
            execution_repo=repo,
            kill_switch=KillSwitch(),
        )

        await manager.submit_leg_order(
            leg_intent=LegOrderIntent(
                leg_intent_id="leg_overlay_allowed_1",
                decision_id="decision_leg_overlay_allowed_1",
                symbol="BTC-USDT-SWAP",
                side="sell",
                pos_side="short",
                action="open",
                quantity=0.001,
                execution_style="exchange",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                idempotency_key="leg_overlay_allowed_1",
                product_type="derivatives",
                margin_mode="cross",
                td_mode="cross",
                position_mode="long_short_mode",
                target_leverage=2.0,
                exposure_side="short",
                strategy_execution_mode="opportunistic_overlay",
            )
        )

        persisted = repo.get_order_state("clleg_overlay_allowed_1")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.status, "FAILED")
        self.assertEqual(persisted.submission_mode, "guarded_simulated_submit")
        self.assertEqual(adapter.submit_calls, 1)

    async def test_leg_overlay_direct_submit_blocks_when_protective_mode_is_disabled(self) -> None:
        repo = InMemoryExecutionRepository()
        adapter = _CountingAdapter()
        manager = OrderManager(
            settings=AATSSettings.model_validate(
                {
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "live_submit_enabled": True,
                    "guarded_execution_dry_run": False,
                    "okx_simulated_trading": False,
                    "strategy_hedge_overlay_enabled": True,
                    "strategy_hedge_protective_enabled": False,
                }
            ),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=adapter,
            execution_repo=repo,
            kill_switch=KillSwitch(),
        )

        await manager.submit_leg_order(
            leg_intent=LegOrderIntent(
                leg_intent_id="leg_protective_disabled_1",
                decision_id="decision_leg_protective_disabled_1",
                symbol="BTC-USDT-SWAP",
                side="sell",
                pos_side="short",
                action="open",
                quantity=0.001,
                execution_style="exchange",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                idempotency_key="leg_protective_disabled_1",
                product_type="derivatives",
                margin_mode="cross",
                td_mode="cross",
                position_mode="long_short_mode",
                target_leverage=2.0,
                exposure_side="short",
                strategy_execution_mode="protective_overlay",
            )
        )

        persisted = repo.get_order_state("clleg_protective_disabled_1")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.status, "BLOCKED")
        self.assertEqual(persisted.submission_mode, "leg_overlay_rollout_blocked")
        self.assertIn("strategy_hedge_protective_disabled", persisted.execution_error)
        self.assertEqual(adapter.submit_calls, 0)

    async def test_normalized_leg_order_intent_still_applies_leg_risk_blockers(self) -> None:
        repo = InMemoryExecutionRepository()
        adapter = _CountingAdapter()
        manager = OrderManager(
            settings=AATSSettings.model_validate({}),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=adapter,
            execution_repo=repo,
            leg_risk_evaluator=lambda _leg_intent: RiskDecision(
                decision_id="normalized_leg_risk_blocked",
                approved=False,
                modified=True,
                capped_target_position_qty=0.0,
                capped_target_notional=0.0,
                projected_notional=0.0,
                risk_score=1.0,
                only_reduce_required=True,
                risk_limit_breached=True,
                rejection_reasons=["risk_max_short_notional_exceeded", "leg_only_reduce_mode_active"],
            ),
            kill_switch=KillSwitch(),
        )
        leg_intent = LegOrderIntent(
            leg_intent_id="normalized_leg_blocked_1",
            decision_id="decision_normalized_leg_blocked_1",
            symbol="BTC-USDT-SWAP",
            side="sell",
            pos_side="short",
            action="open",
            quantity=0.001,
            execution_style="exchange",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            idempotency_key="normalized_leg_blocked_1",
            product_type="derivatives",
            margin_mode="cross",
            td_mode="cross",
            position_mode="long_short_mode",
            target_leverage=2.0,
            exposure_side="short",
            strategy_execution_mode="independent_short_book",
        )
        intent = order_intent_from_leg_order_intent(leg_intent)

        await manager.handle_order_intent(
            {
                "topic": topics.ORDER_INTENTS,
                "key": intent.symbol,
                "payload": build_envelope(
                    topic=topics.ORDER_INTENTS,
                    key=intent.symbol,
                    payload_model=intent,
                    source_component="test",
                ).model_dump(mode="json"),
            }
        )

        persisted = repo.get_order_state("clnormalized_leg_blocked_1")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.status, "BLOCKED")
        self.assertEqual(persisted.submission_mode, "leg_risk_blocked")
        self.assertIn("leg_risk_blocked", persisted.execution_error)
        self.assertEqual(adapter.submit_calls, 0)

    async def test_normalized_leg_order_intent_still_applies_rollout_blockers(self) -> None:
        repo = InMemoryExecutionRepository()
        adapter = _CountingAdapter()
        manager = OrderManager(
            settings=AATSSettings.model_validate(
                {
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "live_submit_enabled": True,
                    "guarded_execution_dry_run": False,
                    "okx_simulated_trading": False,
                    "strategy_hedge_overlay_enabled": True,
                    "strategy_hedge_independent_enabled": True,
                    "strategy_hedge_independent_rollout_stage": "dry_run",
                }
            ),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=adapter,
            execution_repo=repo,
            kill_switch=KillSwitch(),
        )
        leg_intent = LegOrderIntent(
            leg_intent_id="normalized_rollout_blocked_1",
            decision_id="decision_normalized_rollout_blocked_1",
            symbol="BTC-USDT-SWAP",
            side="buy",
            pos_side="long",
            action="open",
            quantity=0.001,
            execution_style="exchange",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            idempotency_key="normalized_rollout_blocked_1",
            product_type="derivatives",
            margin_mode="cross",
            td_mode="cross",
            position_mode="long_short_mode",
            target_leverage=2.0,
            exposure_side="long",
            strategy_execution_mode="independent_long_book",
        )
        intent = order_intent_from_leg_order_intent(leg_intent)

        await manager.handle_order_intent(
            {
                "topic": topics.ORDER_INTENTS,
                "key": intent.symbol,
                "payload": build_envelope(
                    topic=topics.ORDER_INTENTS,
                    key=intent.symbol,
                    payload_model=intent,
                    source_component="test",
                ).model_dump(mode="json"),
            }
        )

        persisted = repo.get_order_state("clnormalized_rollout_blocked_1")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.status, "BLOCKED")
        self.assertEqual(persisted.submission_mode, "leg_overlay_rollout_blocked")
        self.assertIn("independent_overlay_rollout_stage_blocks_live_runtime", persisted.execution_error)
        self.assertEqual(adapter.submit_calls, 0)

    async def test_normalized_leg_order_intent_still_applies_rollout_blockers_for_independent_books(self) -> None:
        repo = InMemoryExecutionRepository()
        adapter = _CountingAdapter()
        manager = OrderManager(
            settings=AATSSettings.model_validate(
                {
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "live_submit_enabled": True,
                    "guarded_execution_dry_run": False,
                    "okx_simulated_trading": False,
                    "strategy_hedge_overlay_enabled": True,
                    "strategy_hedge_independent_enabled": True,
                    "strategy_hedge_independent_rollout_stage": "dry_run",
                }
            ),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=adapter,
            execution_repo=repo,
            kill_switch=KillSwitch(),
        )
        leg_intent = LegOrderIntent(
            leg_intent_id="normalized_rollout_family_mode_blocked_1",
            decision_id="decision_normalized_rollout_family_mode_blocked_1",
            symbol="BTC-USDT-SWAP",
            side="buy",
            pos_side="long",
            action="open",
            quantity=0.001,
            execution_style="exchange",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            idempotency_key="normalized_rollout_family_mode_blocked_1",
            product_type="derivatives",
            margin_mode="cross",
            td_mode="cross",
            position_mode="long_short_mode",
            target_leverage=2.0,
            exposure_side="long",
            strategy_execution_mode="independent_books",
        )
        intent = order_intent_from_leg_order_intent(leg_intent)

        await manager.handle_order_intent(
            {
                "topic": topics.ORDER_INTENTS,
                "key": intent.symbol,
                "payload": build_envelope(
                    topic=topics.ORDER_INTENTS,
                    key=intent.symbol,
                    payload_model=intent,
                    source_component="test",
                ).model_dump(mode="json"),
            }
        )

        persisted = repo.get_order_state("clnormalized_rollout_family_mode_blocked_1")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.status, "BLOCKED")
        self.assertEqual(persisted.submission_mode, "leg_overlay_rollout_blocked")
        self.assertIn("independent_overlay_rollout_stage_blocks_live_runtime", persisted.execution_error)
        self.assertEqual(adapter.submit_calls, 0)


if __name__ == "__main__":
    unittest.main()
