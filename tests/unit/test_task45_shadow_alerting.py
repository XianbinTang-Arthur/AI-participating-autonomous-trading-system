from __future__ import annotations

import unittest

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.events import topics


class _MutablePhase1ShadowMonitor:
    def __init__(self, *, status: str) -> None:
        self.status = status

    def snapshot(self) -> dict[str, object]:
        summary = (
            "Phase 1 shadow compatibility layer is tracking legacy execution and obligation flows."
            if self.status == "healthy"
            else "Phase 1 shadow compatibility layer is behind the legacy runtime."
        )
        blockers = []
        if self.status == "lagging":
            blockers = ["phase1_shadow_lagging"]
        elif self.status == "degraded":
            blockers = ["phase1_shadow_degraded"]
        return {
            "configured": True,
            "status": self.status,
            "connected": True,
            "ready": self.status == "healthy",
            "fresh": self.status == "healthy",
            "detail": summary,
            "blockers": blockers,
            "summary": summary,
            "lag": {
                "order_backlog": 1 if self.status == "lagging" else 0,
                "fill_backlog": 0,
                "obligation_backlog": 0,
            },
            "execution_shadow": {"configured": True, "status": "healthy"},
            "ledger_shadow": {"configured": True, "status": "healthy"},
        }


class TestTask45ShadowAlerting(unittest.IsolatedAsyncioTestCase):
    async def test_phase1_shadow_state_records_alert_and_recovery_events(self) -> None:
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
        monitor = _MutablePhase1ShadowMonitor(status="lagging")
        runtime.phase1_shadow_monitor = monitor
        runtime.health_service.phase1_shadow_provider = monitor

        runtime._record_phase1_shadow_state()

        summary_event = runtime.event_store.latest(topics.EXECUTION_ERROR_SUMMARIES, key="phase1_shadow")
        failure_event = runtime.event_store.latest(topics.PROCESSING_FAILURES, key="phase1_shadow")
        self.assertIsNotNone(summary_event)
        self.assertIsNotNone(failure_event)
        self.assertEqual(summary_event.payload["subsystem"], "phase1_shadow")
        self.assertEqual(summary_event.payload["severity"], "warning")
        self.assertEqual(failure_event.payload["stage"], "shadow_lagging")
        self.assertEqual(runtime.metrics.snapshot().get("phase1_shadow_alerts"), 1)

        monitor.status = "healthy"
        runtime._record_phase1_shadow_state()

        recovered_summary = runtime.event_store.latest(topics.EXECUTION_ERROR_SUMMARIES, key="phase1_shadow")
        self.assertIsNotNone(recovered_summary)
        self.assertIn("phase1_shadow_recovered", recovered_summary.payload["message"])
        self.assertEqual(runtime.metrics.snapshot().get("phase1_shadow_recoveries"), 1)
