from __future__ import annotations

import unittest
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent, OrderIntent, OrderState
from aats.services.execution_engine.order_manager import OrderManager
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.storage.event_store import InMemoryEventStore
from aats.storage.execution_repo import InMemoryExecutionRepository
from aats.storage.exit_execution_repo import InMemoryExitExecutionRepository


class _SubmittedExitAdapter:
    def preview_client_order_id(self, intent: OrderIntent) -> str | None:
        return intent.idempotency_key

    async def submit(self, intent: OrderIntent):
        state = OrderState(
            decision_id=intent.decision_id,
            execution_chain_id=intent.execution_chain_id,
            execution_attempt_id=intent.execution_attempt_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            client_order_id=intent.idempotency_key,
            venue="OKX",
            exchange_order_id="ord_submitted_exit",
            status="SUBMITTED",
            submission_mode="guarded_simulated_submit",
            exchange_status="live",
            submitted_ts=intent.created_at,
            last_update_ts=intent.created_at,
            requested_qty=intent.quantity,
            filled_qty=Decimal("0"),
            remaining_qty=intent.quantity,
            average_fill_price=None,
            fees=Decimal("0"),
            reduce_only=intent.reduce_only,
            close_only=intent.close_only,
            position_mode=intent.position_mode,
            pos_side=intent.pos_side,
            product_type=intent.product_type,
            margin_mode=intent.margin_mode,
            exposure_side=intent.exposure_side,
            execution_action=intent.execution_action,
            leg_action=intent.leg_action,
            position_intent=intent.position_intent,
            submission_payload={},
        )
        return state, []

    async def sync(self, open_order_states):
        synced = []
        fills: list[FillEvent] = []
        for state in open_order_states:
            synced_state = state.model_copy(
                update={
                    "status": "FILLED",
                    "exchange_status": "filled",
                    "filled_qty": state.requested_qty,
                    "remaining_qty": Decimal("0"),
                    "average_fill_price": Decimal("80000"),
                    "last_update_ts": utc_now(),
                }
            )
            synced.append(synced_state)
            fills.append(
                FillEvent(
                    fill_id=f"fill_{state.client_order_id}",
                    decision_id=state.decision_id,
                    execution_chain_id=state.execution_chain_id,
                    execution_attempt_id=state.execution_attempt_id,
                    intent_id=state.intent_id,
                    client_order_id=state.client_order_id,
                    exchange_order_id=state.exchange_order_id or "ord_submitted_exit",
                    symbol=state.symbol,
                    venue="OKX",
                    side="sell",
                    fill_qty=state.requested_qty,
                    fill_price=Decimal("80000"),
                    fee_amount=Decimal("0"),
                    reduce_only=state.reduce_only,
                    close_only=state.close_only,
                    position_mode=state.position_mode,
                    pos_side=state.pos_side,
                    product_type=state.product_type,
                    margin_mode=state.margin_mode,
                    exposure_side=state.exposure_side,
                    execution_action=state.execution_action,
                    leg_action=state.leg_action,
                    position_intent=state.position_intent,
                    liquidity_role="taker",
                    exchange_timestamp=utc_now(),
                    ingestion_timestamp=utc_now(),
                    order_status_after_fill="FILLED",
                )
            )
        return synced, fills

    async def cancel(self, order_state: OrderState):
        return order_state, []

    def readiness(self):
        return {"backend": "okx", "exchange_submit_allowed": True, "submit_blocked_reasons": []}


class _NoopSyncAdapter(_SubmittedExitAdapter):
    async def sync(self, open_order_states):
        return [], []


class _SplitFillAdapter(_SubmittedExitAdapter):
    def __init__(self, *, first_child_live: bool = False) -> None:
        self.first_child_live = first_child_live
        self.submit_quantities: list[Decimal] = []

    async def risk_reducing_max_order_quantity_limit(self, *, intent: OrderIntent):
        return Decimal("2")

    async def submit(self, intent: OrderIntent):
        self.submit_quantities.append(Decimal(intent.quantity))
        client_order_id = intent.idempotency_key
        submitted_ts = utc_now()
        if self.first_child_live and len(self.submit_quantities) == 1:
            return (
                OrderState(
                    decision_id=intent.decision_id,
                    execution_chain_id=intent.execution_chain_id,
                    execution_attempt_id=intent.execution_attempt_id,
                    intent_id=intent.intent_id,
                    symbol=intent.symbol,
                    client_order_id=client_order_id,
                    venue="OKX",
                    exchange_order_id="ord_split_live_1",
                    status="SUBMITTED",
                    submission_mode="guarded_simulated_submit",
                    exchange_status="live",
                    submitted_ts=submitted_ts,
                    last_update_ts=submitted_ts,
                    requested_qty=intent.quantity,
                    filled_qty=Decimal("0"),
                    remaining_qty=intent.quantity,
                    average_fill_price=None,
                    fees=Decimal("0"),
                    reduce_only=intent.reduce_only,
                    close_only=intent.close_only,
                    position_mode=intent.position_mode,
                    pos_side=intent.pos_side,
                    product_type=intent.product_type,
                    margin_mode=intent.margin_mode,
                    exposure_side=intent.exposure_side,
                    execution_action=intent.execution_action,
                    leg_action=intent.leg_action,
                    position_intent=intent.position_intent,
                    submission_payload={},
                ),
                [],
            )
        fills = [
            FillEvent(
                fill_id=f"fill_{client_order_id}",
                decision_id=intent.decision_id,
                execution_chain_id=intent.execution_chain_id,
                execution_attempt_id=intent.execution_attempt_id,
                intent_id=intent.intent_id,
                client_order_id=client_order_id,
                exchange_order_id=f"ord_{client_order_id}",
                symbol=intent.symbol,
                venue="OKX",
                side=intent.side,
                fill_qty=intent.quantity,
                fill_price=Decimal("80000"),
                fee_amount=Decimal("0"),
                reduce_only=intent.reduce_only,
                close_only=intent.close_only,
                position_mode=intent.position_mode,
                pos_side=intent.pos_side,
                product_type=intent.product_type,
                margin_mode=intent.margin_mode,
                exposure_side=intent.exposure_side,
                execution_action=intent.execution_action,
                leg_action=intent.leg_action,
                position_intent=intent.position_intent,
                liquidity_role="taker",
                exchange_timestamp=submitted_ts,
                ingestion_timestamp=submitted_ts,
                order_status_after_fill="FILLED",
            )
        ]
        return (
            OrderState(
                decision_id=intent.decision_id,
                execution_chain_id=intent.execution_chain_id,
                execution_attempt_id=intent.execution_attempt_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                client_order_id=client_order_id,
                venue="OKX",
                exchange_order_id=f"ord_{client_order_id}",
                status="FILLED",
                submission_mode="guarded_simulated_submit",
                exchange_status="filled",
                submitted_ts=submitted_ts,
                last_update_ts=submitted_ts,
                requested_qty=intent.quantity,
                filled_qty=intent.quantity,
                remaining_qty=Decimal("0"),
                average_fill_price=Decimal("80000"),
                fees=Decimal("0"),
                reduce_only=intent.reduce_only,
                close_only=intent.close_only,
                position_mode=intent.position_mode,
                pos_side=intent.pos_side,
                product_type=intent.product_type,
                margin_mode=intent.margin_mode,
                exposure_side=intent.exposure_side,
                execution_action=intent.execution_action,
                leg_action=intent.leg_action,
                position_intent=intent.position_intent,
                submission_payload={},
            ),
            fills,
        )


class TestOrderManagerExitExecution(unittest.IsolatedAsyncioTestCase):
    async def test_risk_reducing_submit_creates_parent_exit_intent(self) -> None:
        execution_repo = InMemoryExecutionRepository()
        exit_repo = InMemoryExitExecutionRepository()
        manager = OrderManager(
            settings=AATSSettings.model_validate({}),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=_SubmittedExitAdapter(),
            execution_repo=execution_repo,
            exit_execution_repo=exit_repo,
            kill_switch=KillSwitch(),
        )
        intent = OrderIntent(
            intent_id="intent_exit_parent_create",
            execution_chain_id="chain_exit_parent_create",
            decision_id="decision_exit_parent_create",
            symbol="BTC-USDT-SWAP",
            side="sell",
            quantity=Decimal("2"),
            execution_style="exchange",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            reduce_only=True,
            close_only=True,
            position_mode="long_short_mode",
            pos_side="long",
            execution_action="exit",
            leg_action="close",
            position_intent="close_long",
            product_type="derivatives",
            margin_mode="cross",
            exposure_side="long",
            idempotency_key="clord_exit_parent_create",
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

        parent = exit_repo.get_exit_execution_intent_by_execution_chain("chain_exit_parent_create")
        self.assertIsNotNone(parent)
        self.assertEqual(parent.symbol, "BTC-USDT-SWAP")
        self.assertEqual(parent.aggregate_status, "WORKING")
        self.assertEqual(parent.remaining_dispatchable_quantity, Decimal("0"))
        self.assertEqual(parent.child_order_ids, ["clord_exit_parent_create"])

    async def test_sync_recomputes_parent_exit_intent_after_child_fill(self) -> None:
        execution_repo = InMemoryExecutionRepository()
        exit_repo = InMemoryExitExecutionRepository()
        manager = OrderManager(
            settings=AATSSettings.model_validate({}),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=_SubmittedExitAdapter(),
            execution_repo=execution_repo,
            exit_execution_repo=exit_repo,
            kill_switch=KillSwitch(),
        )
        intent = OrderIntent(
            intent_id="intent_exit_parent_sync",
            execution_chain_id="chain_exit_parent_sync",
            decision_id="decision_exit_parent_sync",
            symbol="BTC-USDT-SWAP",
            side="sell",
            quantity=Decimal("2"),
            execution_style="exchange",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            reduce_only=True,
            close_only=True,
            position_mode="long_short_mode",
            pos_side="long",
            execution_action="exit",
            leg_action="close",
            position_intent="close_long",
            product_type="derivatives",
            margin_mode="cross",
            exposure_side="long",
            idempotency_key="clord_exit_parent_sync",
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
        await manager.sync_exchange_state()

        parent = exit_repo.get_exit_execution_intent_by_execution_chain("chain_exit_parent_sync")
        self.assertIsNotNone(parent)
        self.assertEqual(parent.aggregate_status, "COMPLETED")
        self.assertEqual(parent.aggregated_filled_quantity, Decimal("2"))
        self.assertEqual(parent.remaining_unresolved_quantity, Decimal("0"))
        self.assertEqual(parent.remaining_dispatchable_quantity, Decimal("0"))

    async def test_non_risk_reducing_submit_does_not_create_parent_exit_intent(self) -> None:
        execution_repo = InMemoryExecutionRepository()
        exit_repo = InMemoryExitExecutionRepository()
        manager = OrderManager(
            settings=AATSSettings.model_validate({}),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=_SubmittedExitAdapter(),
            execution_repo=execution_repo,
            exit_execution_repo=exit_repo,
            kill_switch=KillSwitch(),
        )
        intent = OrderIntent(
            intent_id="intent_open_no_parent",
            execution_chain_id="chain_open_no_parent",
            decision_id="decision_open_no_parent",
            symbol="BTC-USDT-SWAP",
            side="buy",
            quantity=Decimal("2"),
            execution_style="exchange",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            reduce_only=False,
            close_only=False,
            position_mode="long_short_mode",
            pos_side="long",
            execution_action="enter",
            leg_action="open",
            position_intent="open_long",
            product_type="derivatives",
            margin_mode="cross",
            exposure_side="long",
            idempotency_key="clord_open_no_parent",
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

        self.assertIsNone(exit_repo.get_exit_execution_intent_by_execution_chain("chain_open_no_parent"))

    async def test_sync_recomputes_parent_when_unknown_write_ages_without_new_child_state(self) -> None:
        execution_repo = InMemoryExecutionRepository()
        exit_repo = InMemoryExitExecutionRepository()
        settings = AATSSettings.model_validate({"execution_unknown_submit_review_after_seconds": 300.0})
        manager = OrderManager(
            settings=settings,
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=_NoopSyncAdapter(),
            execution_repo=execution_repo,
            exit_execution_repo=exit_repo,
            kill_switch=KillSwitch(),
        )
        now = utc_now()
        recent_unknown = OrderState(
            decision_id="decision_unknown_parent_refresh",
            execution_chain_id="chain_unknown_parent_refresh",
            intent_id="intent_unknown_parent_refresh",
            symbol="BTC-USDT-SWAP",
            client_order_id="clord_unknown_parent_refresh",
            venue="OKX",
            exchange_order_id=None,
            status="SUBMITTED",
            exchange_status="live",
            submitted_ts=now,
            last_update_ts=now,
            requested_qty=Decimal("2"),
            filled_qty=Decimal("0"),
            remaining_qty=Decimal("2"),
            average_fill_price=None,
            fees=Decimal("0"),
            reduce_only=True,
            close_only=True,
            product_type="derivatives",
            margin_mode="cross",
            position_mode="long_short_mode",
            pos_side="long",
            exposure_side="long",
            execution_action="exit",
            leg_action="close",
            position_intent="close_long",
            execution_error="submission_unknown_check_exchange:OKXRequestError",
        )

        await manager._persist_order_state(order_state=recent_unknown, key=recent_unknown.symbol)
        parent = exit_repo.get_exit_execution_intent_by_execution_chain("chain_unknown_parent_refresh")
        self.assertIsNotNone(parent)
        self.assertEqual(parent.aggregate_status, "WORKING")
        self.assertFalse(parent.operator_review_required)

        manager.settings = manager.settings.model_copy(
            update={"execution_unknown_submit_review_after_seconds": 0.0}
        )

        await manager.sync_exchange_state()

        refreshed_parent = exit_repo.get_exit_execution_intent_by_execution_chain("chain_unknown_parent_refresh")
        self.assertIsNotNone(refreshed_parent)
        self.assertEqual(refreshed_parent.aggregate_status, "REVIEW_REQUIRED")
        self.assertTrue(refreshed_parent.operator_review_required)

    async def test_serial_exit_split_submits_multiple_children_until_parent_completed(self) -> None:
        execution_repo = InMemoryExecutionRepository()
        exit_repo = InMemoryExitExecutionRepository()
        adapter = _SplitFillAdapter()
        manager = OrderManager(
            settings=AATSSettings.model_validate({}),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=adapter,
            execution_repo=execution_repo,
            exit_execution_repo=exit_repo,
            kill_switch=KillSwitch(),
        )
        intent = OrderIntent(
            intent_id="intent_split_parent_complete",
            execution_chain_id="chain_split_parent_complete",
            decision_id="decision_split_parent_complete",
            symbol="BTC-USDT-SWAP",
            side="sell",
            quantity=Decimal("5"),
            execution_style="exchange",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            reduce_only=True,
            close_only=True,
            position_mode="long_short_mode",
            pos_side="long",
            execution_action="exit",
            leg_action="close",
            position_intent="close_long",
            product_type="derivatives",
            margin_mode="cross",
            exposure_side="long",
            idempotency_key="split_parent_complete",
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

        self.assertEqual(adapter.submit_quantities, [Decimal("2"), Decimal("2"), Decimal("1")])
        parent = exit_repo.get_exit_execution_intent_by_execution_chain("chain_split_parent_complete")
        self.assertIsNotNone(parent)
        self.assertEqual(parent.aggregate_status, "COMPLETED")
        self.assertEqual(parent.aggregated_filled_quantity, Decimal("5"))
        self.assertEqual(parent.remaining_dispatchable_quantity, Decimal("0"))
        self.assertEqual(len(parent.child_order_ids), 3)
        self.assertEqual(len(execution_repo.order_states()), 3)

    async def test_serial_exit_split_stops_when_first_child_remains_working(self) -> None:
        execution_repo = InMemoryExecutionRepository()
        exit_repo = InMemoryExitExecutionRepository()
        adapter = _SplitFillAdapter(first_child_live=True)
        manager = OrderManager(
            settings=AATSSettings.model_validate({}),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=adapter,
            execution_repo=execution_repo,
            exit_execution_repo=exit_repo,
            kill_switch=KillSwitch(),
        )
        intent = OrderIntent(
            intent_id="intent_split_parent_working",
            execution_chain_id="chain_split_parent_working",
            decision_id="decision_split_parent_working",
            symbol="BTC-USDT-SWAP",
            side="sell",
            quantity=Decimal("5"),
            execution_style="exchange",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            reduce_only=True,
            close_only=True,
            position_mode="long_short_mode",
            pos_side="long",
            execution_action="exit",
            leg_action="close",
            position_intent="close_long",
            product_type="derivatives",
            margin_mode="cross",
            exposure_side="long",
            idempotency_key="split_parent_working",
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

        self.assertEqual(adapter.submit_quantities, [Decimal("2")])
        parent = exit_repo.get_exit_execution_intent_by_execution_chain("chain_split_parent_working")
        self.assertIsNotNone(parent)
        self.assertEqual(parent.aggregate_status, "WORKING")
        self.assertEqual(parent.remaining_dispatchable_quantity, Decimal("3"))
        self.assertEqual(parent.child_order_ids, ["split_parent_working"])

    async def test_sync_resumes_serial_exit_split_after_first_child_converges(self) -> None:
        execution_repo = InMemoryExecutionRepository()
        exit_repo = InMemoryExitExecutionRepository()
        adapter = _SplitFillAdapter(first_child_live=True)
        manager = OrderManager(
            settings=AATSSettings.model_validate({}),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=adapter,
            execution_repo=execution_repo,
            exit_execution_repo=exit_repo,
            kill_switch=KillSwitch(),
        )
        intent = OrderIntent(
            intent_id="intent_split_parent_resume",
            execution_chain_id="chain_split_parent_resume",
            decision_id="decision_split_parent_resume",
            symbol="BTC-USDT-SWAP",
            side="sell",
            quantity=Decimal("5"),
            execution_style="exchange",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            reduce_only=True,
            close_only=True,
            position_mode="long_short_mode",
            pos_side="long",
            execution_action="exit",
            leg_action="close",
            position_intent="close_long",
            product_type="derivatives",
            margin_mode="cross",
            exposure_side="long",
            idempotency_key="split_parent_resume",
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

        self.assertEqual(adapter.submit_quantities, [Decimal("2")])

        await manager.sync_exchange_state()

        self.assertEqual(adapter.submit_quantities, [Decimal("2"), Decimal("2"), Decimal("1")])
        parent = exit_repo.get_exit_execution_intent_by_execution_chain("chain_split_parent_resume")
        self.assertIsNotNone(parent)
        self.assertEqual(parent.aggregate_status, "COMPLETED")
        self.assertEqual(parent.aggregated_filled_quantity, Decimal("5"))
        self.assertEqual(parent.remaining_dispatchable_quantity, Decimal("0"))
        self.assertEqual(len(execution_repo.order_states()), 3)


if __name__ == "__main__":
    unittest.main()
