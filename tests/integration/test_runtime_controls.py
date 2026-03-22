from __future__ import annotations

import asyncio
import unittest

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.events import topics
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

    async def test_halt_blocks_background_decision_cycles(self) -> None:
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

        runtime.kill_switch.halt("test_halt_background_cycles")
        targets_before = len(runtime.event_store.by_topic(topics.POSITION_TARGETS))
        outcomes_before = len(runtime.event_store.by_topic(topics.DECISION_OUTCOMES))

        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=3,
            interval_seconds=0.0,
        )

        self.assertEqual(len(runtime.event_store.by_topic(topics.POSITION_TARGETS)), targets_before)
        self.assertEqual(len(runtime.event_store.by_topic(topics.DECISION_OUTCOMES)), outcomes_before)

    async def test_background_reconciliation_refresh_keeps_reports_current(self) -> None:
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
                "reconciliation_stale_after_seconds": 1.0,
            }
        )
        runtime = await build_runtime(settings)
        await runtime.start_background_tasks()
        try:
            await asyncio.sleep(0.1)
            first_report = runtime.reconciliation_repo.latest()
            self.assertIsNotNone(first_report)
            await asyncio.sleep(0.7)
            second_report = runtime.reconciliation_repo.latest()
            self.assertIsNotNone(second_report)
            self.assertGreater(second_report.as_of_ts, first_report.as_of_ts)
        finally:
            await runtime.stop_background_tasks()

    async def test_background_reconciliation_failures_are_persisted(self) -> None:
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
                "reconciliation_stale_after_seconds": 1.0,
            }
        )
        runtime = await build_runtime(settings)

        async def failing_validate_now(*, reason: str):
            raise RuntimeError(f"forced_failure:{reason}")

        runtime.reconciliation_service.validate_now = failing_validate_now  # type: ignore[method-assign]
        await runtime.start_background_tasks()
        try:
            await asyncio.sleep(0.1)
        finally:
            await runtime.stop_background_tasks()

        summaries = runtime.event_store.by_topic(topics.EXECUTION_ERROR_SUMMARIES)
        self.assertTrue(summaries)
        self.assertEqual(summaries[-1].payload["subsystem"], "reconciliation_refresh")
        self.assertIn("forced_failure:background_refresh", summaries[-1].payload["message"])

    async def test_guarded_simulated_submit_requires_persistent_storage(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "guarded_simulated_submit_enabled",
                "mode": "guarded_live",
                "market_data_backend": "okx",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "okx_simulated_trading": True,
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
            }
        )

        with self.assertRaisesRegex(ValueError, "guarded_simulated_submit_requires_persistent_storage"):
            await build_runtime(settings)


if __name__ == "__main__":
    unittest.main()
