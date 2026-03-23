from __future__ import annotations

import os
import unittest
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.storage.event_store import InMemoryEventStore
from aats.storage.execution_fill_repo_v2_postgres import PostgresExecutionFillRepositoryV2
from aats.storage.execution_order_repo_postgres import (
    PostgresExecutionOrderHistoryRepository,
    PostgresExecutionOrderRepository,
)
from aats.storage.execution_repo import InMemoryExecutionRepository
from aats.services.execution_control.shadow import Phase1ExecutionShadowService
from aats.services.execution_engine.order_manager import OrderManager
from aats.services.execution_engine.paper_adapter import PaperExecutionAdapter
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.schemas.execution import OrderIntent
from tests.support.postgres import temporary_postgres_runtime


@unittest.skipUnless(os.getenv("AATS_DATABASE_URL"), "AATS_DATABASE_URL is required for PostgreSQL-backed tests")
class TestTask39ShadowWrites(unittest.IsolatedAsyncioTestCase):
    async def test_order_manager_shadow_writes_phase1_execution_rows(self) -> None:
        with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
            shadow_order_repo = PostgresExecutionOrderRepository(runtime.session_factory)
            shadow_order_history_repo = PostgresExecutionOrderHistoryRepository(runtime.session_factory)
            shadow_fill_repo = PostgresExecutionFillRepositoryV2(runtime.session_factory)
            phase1_execution_shadow_service = Phase1ExecutionShadowService(
                execution_order_repo=shadow_order_repo,
                execution_order_history_repo=shadow_order_history_repo,
                execution_fill_repo=shadow_fill_repo,
            )

            manager = OrderManager(
                settings=AATSSettings.model_validate({}),
                bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
                adapter=PaperExecutionAdapter(price_provider=lambda _symbol: Decimal("100"), taker_fee_bps=5.0),
                execution_repo=InMemoryExecutionRepository(),
                shadow_execution_service=phase1_execution_shadow_service,
                kill_switch=KillSwitch(),
            )
            intent = OrderIntent(
                intent_id="intent_task39_shadow",
                decision_id="decision_task39_shadow",
                symbol="BTC-USDT",
                side="buy",
                quantity=Decimal("0.001"),
                execution_style="exchange",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                reduce_only=False,
                close_only=False,
                idempotency_key="task39_shadow",
            )

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

            order_row = shadow_order_repo.get_order_by_client_order_id("cltask39_shadow")
            self.assertIsNotNone(order_row)
            self.assertEqual(order_row["state"], "FILLED")
            history_rows = shadow_order_history_repo.history_for_order("cltask39_shadow")
            self.assertEqual(
                [row["to_state"] for row in history_rows],
                ["CREATED", "SUBMITTING", "FILLED"],
            )
            fill_rows = shadow_fill_repo.fills_for_order("cltask39_shadow")
            self.assertEqual(len(fill_rows), 1)
            self.assertEqual(fill_rows[0]["client_order_id"], "cltask39_shadow")
            self.assertEqual(fill_rows[0]["source_system"], "paper")
