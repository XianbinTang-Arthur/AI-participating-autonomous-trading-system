from __future__ import annotations

import unittest

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.services.governance_engine.recovery_posture import RecoveryPostureEvaluator


class _LaggingPhase1ShadowMonitor:
    def snapshot(self) -> dict[str, object]:
        return {
            "configured": True,
            "status": "lagging",
            "connected": True,
            "ready": False,
            "fresh": False,
            "detail": "Phase 1 shadow compatibility layer is behind the legacy runtime.",
            "blockers": ["phase1_shadow_lagging"],
            "summary": "Phase 1 shadow compatibility layer is behind the legacy runtime.",
            "lag": {
                "order_backlog": 1,
                "fill_backlog": 0,
                "obligation_backlog": 0,
            },
            "execution_shadow": {
                "configured": True,
                "status": "healthy",
            },
            "ledger_shadow": {
                "configured": True,
                "status": "healthy",
            },
        }


class TestTask44ShadowGate(unittest.IsolatedAsyncioTestCase):
    async def test_phase1_shadow_lagging_becomes_health_blocker(self) -> None:
        runtime = await build_runtime(
            AATSSettings.model_validate(
                {
                    "mode": "paper_live",
                    "market_data_backend": "demo",
                    "execution_backend": "paper",
                    "account_backend": "disabled",
                    "account_read_enabled": False,
                    "storage_mode": "memory",
                    "event_persistence_mode": "strict",
                }
            )
        )
        await runtime.market_gateway.run_local_publisher(
            symbol=runtime.settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        runtime.phase1_shadow_monitor = _LaggingPhase1ShadowMonitor()
        runtime.health_service.phase1_shadow_provider = runtime.phase1_shadow_monitor

        health = runtime.health_service.snapshot()

        self.assertIn("phase1_shadow_lagging", health.blockers)
        phase1_component = next(component for component in health.components if component.component == "phase1_shadow")
        self.assertEqual(phase1_component.status, "warn")
        self.assertIn("phase1_shadow_lagging", phase1_component.blockers)

    async def test_phase1_shadow_lagging_blocks_resume_and_safe_to_trade(self) -> None:
        runtime = await build_runtime(
            AATSSettings.model_validate(
                {
                    "mode": "paper_live",
                    "market_data_backend": "demo",
                    "execution_backend": "paper",
                    "account_backend": "disabled",
                    "account_read_enabled": False,
                    "storage_mode": "memory",
                    "event_persistence_mode": "strict",
                }
            )
        )
        await runtime.market_gateway.run_local_publisher(
            symbol=runtime.settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        runtime.phase1_shadow_monitor = _LaggingPhase1ShadowMonitor()
        runtime.health_service.phase1_shadow_provider = runtime.phase1_shadow_monitor

        final = RecoveryPostureEvaluator(runtime).finalize_status()

        self.assertFalse(final.resume_eligible)
        self.assertFalse(final.safe_to_trade)
        self.assertIn("phase1_shadow_lagging", final.resume_blocked_reasons)
