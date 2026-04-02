from __future__ import annotations

import asyncio
import os
import unittest

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.execution import OrderIntent
from tests.support.postgres import temporary_postgres_url


@unittest.skipUnless(os.getenv("AATS_DATABASE_URL"), "AATS_DATABASE_URL is required for PostgreSQL-backed tests")
class TestPhase4RecoveryReconciliationRuntime(unittest.IsolatedAsyncioTestCase):
    async def test_phase4_recovery_uses_execution_ledger_view_for_clean_restart(self) -> None:
        runtime = None
        recovered_runtime = None
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            settings = AATSSettings.model_validate(
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
                    "portfolio_ledger_truth_enabled": True,
                    "recovery_reconciliation_execution_ledger_enabled": True,
                }
            )
            runtime = await build_runtime(settings)
            await runtime.market_gateway.run_local_publisher(
                symbol=settings.default_symbol,
                iterations=4,
                interval_seconds=0.0,
            )
            recovered_runtime = await build_runtime(settings, bootstrap_portfolio_snapshot=False)

            self.assertEqual(recovered_runtime.recovery_status.recovery_source, "execution_ledger")
            self.assertEqual(recovered_runtime.recovery_status.reconciliation_classification, "clean")
            self.assertTrue(recovered_runtime.recovery_status.recovered_snapshot_available)
            self.assertTrue(recovered_runtime.recovery_status.resume_eligible)
            self.assertFalse(recovered_runtime.recovery_status.halted)
        if runtime is not None and runtime.database_runtime is not None:
            runtime.database_runtime.dispose()
        if recovered_runtime is not None and recovered_runtime.database_runtime is not None:
            recovered_runtime.database_runtime.dispose()

    async def test_phase4_recovery_halts_when_persisted_execution_commands_are_pending(self) -> None:
        runtime = None
        recovered_runtime = None
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            settings = AATSSettings.model_validate(
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
                    "portfolio_ledger_truth_enabled": True,
                    "recovery_reconciliation_execution_ledger_enabled": True,
                }
            )
            runtime = await build_runtime(settings)
            seeded_snapshot = runtime.market_gateway.normalizer.normalize(
                runtime.market_gateway._build_local_payload(runtime.settings.default_symbol)  # type: ignore[attr-defined]
            )
            runtime.market_gateway._latest_snapshots[runtime.settings.default_symbol] = seeded_snapshot  # type: ignore[attr-defined]
            runtime.market_gateway._latest_received_at[runtime.settings.default_symbol] = utc_now()  # type: ignore[attr-defined]
            intent = OrderIntent(
                intent_id="intent_phase4_pending_1",
                decision_id="decision_phase4_pending_1",
                symbol="BTC-USDT",
                side="buy",
                quantity=0.001,
                execution_style="exchange",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                reduce_only=False,
                close_only=False,
                idempotency_key="phase4_pending_1",
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

            recovered_runtime = await build_runtime(settings, bootstrap_portfolio_snapshot=False)

            self.assertEqual(recovered_runtime.recovery_status.recovery_source, "execution_ledger")
            self.assertEqual(recovered_runtime.recovery_status.pending_command_count, 1)
            self.assertTrue(recovered_runtime.recovery_status.halted)
            self.assertFalse(recovered_runtime.recovery_status.resume_eligible)
            self.assertEqual(recovered_runtime.recovery_status.recovery_action, "halted_pending_execution_commands")
            self.assertIn("pending_execution_commands", recovered_runtime.recovery_status.resume_blocked_reasons)
        if runtime is not None and runtime.database_runtime is not None:
            runtime.database_runtime.dispose()
        if recovered_runtime is not None and recovered_runtime.database_runtime is not None:
            recovered_runtime.database_runtime.dispose()

    async def test_phase4_halted_runtime_does_not_auto_submit_pending_commands(self) -> None:
        runtime = None
        recovered_runtime = None
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            settings = AATSSettings.model_validate(
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
                    "execution_command_poll_interval_seconds": 0.05,
                    "execution_command_sent_retry_after_seconds": 0.0,
                    "portfolio_ledger_truth_enabled": True,
                    "recovery_reconciliation_execution_ledger_enabled": True,
                }
            )
            runtime = await build_runtime(settings)
            seeded_snapshot = runtime.market_gateway.normalizer.normalize(
                runtime.market_gateway._build_local_payload(runtime.settings.default_symbol)  # type: ignore[attr-defined]
            )
            runtime.market_gateway._latest_snapshots[runtime.settings.default_symbol] = seeded_snapshot  # type: ignore[attr-defined]
            runtime.market_gateway._latest_received_at[runtime.settings.default_symbol] = utc_now()  # type: ignore[attr-defined]
            intent = OrderIntent(
                intent_id="intent_phase4_pending_hold",
                decision_id="decision_phase4_pending_hold",
                symbol="BTC-USDT",
                side="buy",
                quantity=0.001,
                execution_style="exchange",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                reduce_only=False,
                close_only=False,
                idempotency_key="phase4_pending_hold",
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

            recovered_runtime = await build_runtime(settings, bootstrap_portfolio_snapshot=False)
            self.assertTrue(recovered_runtime.recovery_status.halted)
            await recovered_runtime.start_background_tasks()
            await asyncio.sleep(0.25)

            command = recovered_runtime.execution_command_repo.get_by_idempotency_key("submit:clphase4_pending_hold")
            self.assertIsNotNone(command)
            self.assertEqual(command["state"], "PENDING")
            order_state = recovered_runtime.execution_repo.get_order_state("clphase4_pending_hold")
            self.assertIsNotNone(order_state)
            self.assertEqual(order_state.status, "CREATED")
        if runtime is not None and runtime.database_runtime is not None:
            runtime.database_runtime.dispose()
        if recovered_runtime is not None:
            await recovered_runtime.stop_background_tasks()
            if recovered_runtime.database_runtime is not None:
                recovered_runtime.database_runtime.dispose()

    async def test_phase4_recovery_halts_when_submit_command_is_stuck_in_sent_before_venue_ack(self) -> None:
        runtime = None
        recovered_runtime = None
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            settings = AATSSettings.model_validate(
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
                    "portfolio_ledger_truth_enabled": True,
                    "recovery_reconciliation_execution_ledger_enabled": True,
                }
            )
            runtime = await build_runtime(settings)
            seeded_snapshot = runtime.market_gateway.normalizer.normalize(
                runtime.market_gateway._build_local_payload(runtime.settings.default_symbol)  # type: ignore[attr-defined]
            )
            runtime.market_gateway._latest_snapshots[runtime.settings.default_symbol] = seeded_snapshot  # type: ignore[attr-defined]
            runtime.market_gateway._latest_received_at[runtime.settings.default_symbol] = utc_now()  # type: ignore[attr-defined]
            intent = OrderIntent(
                intent_id="intent_phase4_stuck_sent_submit",
                decision_id="decision_phase4_stuck_sent_submit",
                symbol="BTC-USDT",
                side="buy",
                quantity=0.001,
                execution_style="exchange",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                reduce_only=False,
                close_only=False,
                idempotency_key="phase4_stuck_sent_submit",
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
            command = runtime.execution_command_repo.get_by_idempotency_key("submit:clphase4_stuck_sent_submit")
            assert command is not None
            self.assertTrue(
                runtime.execution_command_repo.claim_command(
                    command_id=str(command["command_id"]),
                    expected_state=str(command["state"]),
                    expected_updated_at=command["updated_at"],
                    updated_at=utc_now(),
                )
            )

            recovered_runtime = await build_runtime(settings, bootstrap_portfolio_snapshot=False)

            self.assertTrue(recovered_runtime.recovery_status.halted)
            self.assertEqual(
                recovered_runtime.recovery_status.recovery_action,
                "halted_stuck_sent_submit_commands",
            )
            self.assertEqual(recovered_runtime.recovery_status.pending_command_count, 0)
            self.assertEqual(recovered_runtime.recovery_status.stuck_sent_submit_order_count, 1)
            self.assertIn(
                "stuck_sent_submit_commands",
                recovered_runtime.recovery_status.resume_blocked_reasons,
            )
            self.assertEqual(recovered_runtime.recovery_status.sent_stale_command_count, 0)
        if runtime is not None and runtime.database_runtime is not None:
            runtime.database_runtime.dispose()
        if recovered_runtime is not None and recovered_runtime.database_runtime is not None:
            recovered_runtime.database_runtime.dispose()

    async def test_phase4_recovery_halts_when_created_order_is_missing_submit_command(self) -> None:
        runtime = None
        recovered_runtime = None
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            settings = AATSSettings.model_validate(
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
                    "portfolio_ledger_truth_enabled": True,
                    "recovery_reconciliation_execution_ledger_enabled": True,
                }
            )
            runtime = await build_runtime(settings)
            intent = OrderIntent(
                intent_id="intent_phase4_stranded_created",
                decision_id="decision_phase4_stranded_created",
                symbol="BTC-USDT",
                side="buy",
                quantity=0.001,
                execution_style="exchange",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                reduce_only=False,
                close_only=False,
                idempotency_key="phase4_stranded_created",
            )
            runtime.execution_order_repo.create_order(
                order_id="clphase4_stranded_created",
                intent=intent,
                initial_state="CREATED",
                created_at=utc_now(),
                raw_payload={
                    "client_order_id": "clphase4_stranded_created",
                    "source_system": "phase2_execution_command_flow",
                },
            )

            recovered_runtime = await build_runtime(settings, bootstrap_portfolio_snapshot=False)

            self.assertTrue(recovered_runtime.recovery_status.halted)
            self.assertFalse(recovered_runtime.recovery_status.resume_eligible)
            self.assertEqual(
                recovered_runtime.recovery_status.recovery_action,
                "halted_created_orders_missing_submit_commands",
            )
            self.assertIn(
                "created_orders_missing_submit_commands",
                recovered_runtime.recovery_status.resume_blocked_reasons,
            )
        if runtime is not None and runtime.database_runtime is not None:
            runtime.database_runtime.dispose()
        if recovered_runtime is not None and recovered_runtime.database_runtime is not None:
            recovered_runtime.database_runtime.dispose()


if __name__ == "__main__":
    unittest.main()
