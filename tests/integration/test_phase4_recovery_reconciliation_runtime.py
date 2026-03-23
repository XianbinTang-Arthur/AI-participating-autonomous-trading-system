from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
