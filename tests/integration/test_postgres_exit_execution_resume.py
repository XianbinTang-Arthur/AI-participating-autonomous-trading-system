from __future__ import annotations

import unittest
from decimal import Decimal

from aats.bootstrap.config import build_storage_backends
from aats.bootstrap.settings import AATSSettings
from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent, OrderIntent, OrderState
from aats.services.execution_engine.order_manager import OrderManager
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.storage.event_store import InMemoryEventStore
from aats.storage.execution_repo_postgres import PostgresExecutionRepository
from aats.storage.exit_execution_repo_postgres import PostgresExitExecutionRepository
from tests.support.postgres import temporary_postgres_runtime, temporary_postgres_url


class _PostgresSplitAdapter:
    def __init__(self, *, first_child_live: bool = False) -> None:
        self.first_child_live = first_child_live
        self.submit_quantities: list[Decimal] = []

    def preview_client_order_id(self, intent: OrderIntent) -> str | None:
        return intent.idempotency_key

    async def risk_reducing_max_order_quantity_limit(self, *, intent: OrderIntent):
        _ = intent
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
                    exchange_order_id="ord_pg_split_live_1",
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
                    fill_id=f"sync_fill_{state.client_order_id}",
                    decision_id=state.decision_id,
                    execution_chain_id=state.execution_chain_id,
                    execution_attempt_id=state.execution_attempt_id,
                    intent_id=state.intent_id,
                    client_order_id=state.client_order_id,
                    exchange_order_id=state.exchange_order_id or "ord_pg_split_live_1",
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


class TestPostgresExitExecutionResume(unittest.IsolatedAsyncioTestCase):
    def test_build_storage_backends_wires_postgres_exit_execution_repo(self) -> None:
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            settings = AATSSettings.model_validate(
                {
                    "storage_mode": "postgres",
                    "database_url": database_url,
                    "database_auto_create_schema": True,
                }
            )
            storage = build_storage_backends(settings)
            try:
                self.assertIsInstance(storage.exit_execution_repo, PostgresExitExecutionRepository)
            finally:
                if storage.database_runtime is not None:
                    storage.database_runtime.dispose()

    async def test_postgres_exit_parent_template_persists_and_resumes_after_restart(self) -> None:
        with temporary_postgres_runtime(use_migrations=True) as (runtime, _admin_engine, _schema_name):
            settings = AATSSettings.model_validate(
                {
                    "storage_mode": "postgres",
                    "database_url": "postgresql://unused",
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "default_symbol": "BTC-USDT-SWAP",
                    "allowed_symbols": ("BTC-USDT-SWAP",),
                }
            )
            execution_repo = PostgresExecutionRepository(runtime.session_factory)
            exit_execution_repo = PostgresExitExecutionRepository(runtime.session_factory)
            adapter_first = _PostgresSplitAdapter(first_child_live=True)
            manager_first = OrderManager(
                settings=settings,
                bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
                adapter=adapter_first,
                execution_repo=execution_repo,
                exit_execution_repo=exit_execution_repo,
                kill_switch=KillSwitch(),
            )
            intent = OrderIntent(
                intent_id="intent_pg_split_resume",
                execution_chain_id="chain_pg_split_resume",
                decision_id="decision_pg_split_resume",
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
                idempotency_key="pg_split_resume",
            )

            await manager_first.handle_order_intent(
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

            parent_before = exit_execution_repo.get_exit_execution_intent_by_execution_chain("chain_pg_split_resume")
            self.assertIsNotNone(parent_before)
            self.assertIn("dispatch_template", parent_before.metadata)
            self.assertEqual(parent_before.aggregate_status, "WORKING")
            self.assertEqual(parent_before.remaining_dispatchable_quantity, Decimal("3"))
            self.assertEqual(adapter_first.submit_quantities, [Decimal("2")])

            adapter_resumed = _PostgresSplitAdapter()
            resumed_execution_repo = PostgresExecutionRepository(runtime.session_factory)
            resumed_exit_repo = PostgresExitExecutionRepository(runtime.session_factory)
            manager_resumed = OrderManager(
                settings=settings,
                bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
                adapter=adapter_resumed,
                execution_repo=resumed_execution_repo,
                exit_execution_repo=resumed_exit_repo,
                kill_switch=KillSwitch(),
            )

            await manager_resumed.sync_exchange_state()

            parent_after = resumed_exit_repo.get_exit_execution_intent_by_execution_chain("chain_pg_split_resume")
            self.assertIsNotNone(parent_after)
            self.assertEqual(parent_after.aggregate_status, "COMPLETED")
            self.assertEqual(parent_after.aggregated_filled_quantity, Decimal("5"))
            self.assertEqual(parent_after.remaining_dispatchable_quantity, Decimal("0"))
            self.assertEqual(adapter_resumed.submit_quantities, [Decimal("2"), Decimal("1")])
            self.assertEqual(len(resumed_execution_repo.order_states()), 3)


if __name__ == "__main__":
    unittest.main()
