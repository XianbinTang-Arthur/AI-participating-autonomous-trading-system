from __future__ import annotations

import asyncio
import os
import unittest

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.execution import OrderIntent
from tests.support.postgres import temporary_postgres_url


@unittest.skipUnless(os.getenv("AATS_DATABASE_URL"), "AATS_DATABASE_URL is required for PostgreSQL-backed tests")
class TestPhase2CommandFlowRuntime(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_drains_persisted_execution_commands_when_phase2_enabled(self) -> None:
        runtime = None
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            runtime = await build_runtime(
                AATSSettings.model_validate(
                    {
                        "config_profile": "local_demo",
                        "mode": "paper_live",
                        "market_data_backend": "demo",
                        "execution_backend": "paper",
                        "account_backend": "disabled",
                        "account_read_enabled": False,
                        "storage_mode": "postgres",
                        "database_url": database_url,
                        "database_auto_create_schema": True,
                        "database_single_runtime_guard_enabled": False,
                        "event_persistence_mode": "strict",
                        "execution_command_flow_enabled": True,
                        "execution_command_poll_interval_seconds": 0.1,
                    }
                )
            )
            await runtime.start_background_tasks()
            intent = OrderIntent(
                intent_id="intent_phase2_runtime_1",
                decision_id="decision_phase2_runtime_1",
                symbol="BTC-USDT",
                side="buy",
                quantity=0.001,
                execution_style="exchange",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                reduce_only=False,
                close_only=False,
                idempotency_key="phase2_runtime_1",
            )
            await runtime.order_manager.handle_order_intent(
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

            await asyncio.sleep(0.35)

            state = runtime.execution_repo.get_order_state("clphase2_runtime_1")
            self.assertIsNotNone(state)
            self.assertEqual(state.status, "FILLED")
            command = runtime.execution_command_repo.get_by_idempotency_key("submit:clphase2_runtime_1")
            self.assertIsNotNone(command)
            self.assertEqual(command["state"], "ACKED")
            processor_snapshot = runtime.execution_command_processor.snapshot()
            self.assertEqual(processor_snapshot["status"], "healthy")
            self.assertGreaterEqual(processor_snapshot["success_count"], 1)
        if runtime is not None:
            await runtime.stop_background_tasks()
