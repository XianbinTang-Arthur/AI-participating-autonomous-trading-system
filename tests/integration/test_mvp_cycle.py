from __future__ import annotations

import unittest

from aats.bootstrap.config import build_runtime, load_settings


class TestMVPCycle(unittest.IsolatedAsyncioTestCase):
    async def test_local_paper_loop_produces_audited_execution_flow(self) -> None:
        settings = load_settings()
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


if __name__ == "__main__":
    unittest.main()
