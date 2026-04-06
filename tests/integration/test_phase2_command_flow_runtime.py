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
    async def _wait_for_command_state(
        self,
        runtime,
        *,
        idempotency_key: str,
        expected_state: str,
        timeout_seconds: float = 5.0,
        poll_interval: float = 0.05,
    ) -> dict:
        # 命令状态机异步推进；不要用固定 sleep 等待，否则会撞上时序边界。
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_seconds
        last_state: str | None = None
        while True:
            command = await asyncio.to_thread(
                runtime.execution_command_repo.get_by_idempotency_key,
                idempotency_key,
            )
            last_state = (command or {}).get("state")
            if command is not None and last_state == expected_state:
                return command
            if loop.time() >= deadline:
                self.fail(
                    f"timed_out_waiting_for_command_state idempotency_key={idempotency_key}"
                    f" expected={expected_state} last_state={last_state}"
                )
            await asyncio.sleep(poll_interval)

    async def test_runtime_drains_persisted_execution_commands_when_phase2_enabled(self) -> None:
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
            try:
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

                command = await self._wait_for_command_state(
                    runtime,
                    idempotency_key="submit:clphase2_runtime_1",
                    expected_state="ACKED",
                    timeout_seconds=5.0,
                )

                state = runtime.execution_repo.get_order_state("clphase2_runtime_1")
                self.assertIsNotNone(state)
                self.assertEqual(state.status, "FILLED")
                self.assertIsNotNone(command)
                self.assertEqual(command["state"], "ACKED")
                processor_snapshot = runtime.execution_command_processor.snapshot()
                self.assertEqual(processor_snapshot["status"], "healthy")
                self.assertGreaterEqual(processor_snapshot["success_count"], 1)
            finally:
                # 必须在 with 退出（=DROP SCHEMA）之前停掉后台任务，
                # 否则后台 loop 会撞上已被删除的表，制造大量噪声日志。
                await runtime.stop_background_tasks()
