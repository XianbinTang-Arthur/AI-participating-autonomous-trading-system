from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aats.api.routes import router
from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.events import topics


class TestMVPCycle(unittest.IsolatedAsyncioTestCase):
    async def test_local_paper_loop_produces_audited_execution_flow(self) -> None:
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
            }
        )
        runtime = await build_runtime(settings)

        snapshots = await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=4,
            interval_seconds=0.0,
        )

        self.assertEqual(len(snapshots), 4)
        self.assertEqual(len(runtime.execution_repo.order_states()), 2)
        self.assertEqual(len(runtime.execution_repo.fills()), 2)
        self.assertEqual(len(runtime.audit_repo.all()), 4)
        self.assertGreater(runtime.event_store.count(topic=topics.HEALTH_SNAPSHOTS), 0)
        self.assertGreater(runtime.event_store.count(topic=topics.EXECUTION_PLANS), 0)

        portfolio_snapshot = runtime.portfolio_repo.latest()
        self.assertIsNotNone(portfolio_snapshot)
        self.assertTrue(any(position.symbol == settings.default_symbol for position in portfolio_snapshot.positions))
        btc_position = next(position for position in portfolio_snapshot.positions if position.symbol == settings.default_symbol)
        self.assertAlmostEqual(btc_position.position_qty, -0.001)

        reconciliation_report = runtime.reconciliation_repo.latest()
        self.assertIsNotNone(reconciliation_report)
        self.assertEqual(reconciliation_report.severity, "CLEAN")

        audited_with_execution = [record for record in runtime.audit_repo.all() if record.order_intent_refs]
        self.assertTrue(audited_with_execution)
        self.assertTrue(any(record.fill_event_refs for record in audited_with_execution))
        self.assertTrue(any(record.portfolio_delta_ref is not None for record in audited_with_execution))
        self.assertTrue(any(record.reconciliation_refs for record in audited_with_execution))
        self.assertGreater(runtime.event_store.count(), 0)

        app = FastAPI()
        app.include_router(router)
        app.state.runtime = runtime
        with TestClient(app) as client:
            response = client.get("/decision/latest")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        decision_id = payload["decision_id"]
        self.assertIsNotNone(decision_id)
        self.assertEqual(payload["decision_context"]["decision_id"], decision_id)
        self.assertEqual(payload["policy_decision"]["decision_id"], decision_id)
        self.assertEqual(payload["risk_decision"]["decision_id"], decision_id)
        self.assertEqual(payload["audit"]["decision_id"], decision_id)
        self.assertEqual(payload["health_snapshot"]["decision_id"], decision_id)
        if payload["execution_plan"] is not None:
            self.assertEqual(payload["execution_plan"]["decision_id"], decision_id)
        if payload["latest_order_intent"] is not None:
            self.assertEqual(payload["latest_order_intent"]["decision_id"], decision_id)
        if payload["latest_fill_event"] is not None:
            self.assertEqual(payload["latest_fill_event"]["decision_id"], decision_id)


if __name__ == "__main__":
    unittest.main()
