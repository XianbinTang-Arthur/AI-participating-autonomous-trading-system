from __future__ import annotations

import unittest

from aats.bootstrap.config import build_runtime, load_settings


class TestAuditLinkage(unittest.IsolatedAsyncioTestCase):
    async def test_sequential_decisions_do_not_cross_link_snapshots_or_reconciliation(self) -> None:
        settings = load_settings()
        runtime = await build_runtime(settings)

        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=6,
            interval_seconds=0.0,
        )

        event_store = runtime.event_store
        records_with_execution = [record for record in runtime.audit_repo.all() if record.fill_event_refs]
        self.assertGreaterEqual(len(records_with_execution), 2)
        self.assertEqual(
            len({record.portfolio_delta_ref for record in records_with_execution if record.portfolio_delta_ref}),
            len(records_with_execution),
        )

        for record in records_with_execution:
            self.assertIsNotNone(record.portfolio_delta_ref)
            snapshot_event = event_store.get(record.portfolio_delta_ref)
            self.assertIsNotNone(snapshot_event)
            self.assertEqual(snapshot_event.payload.get("decision_id"), record.decision_id)

            source_fill_ids = {
                event_store.get(ref).payload.get("fill_id")
                for ref in record.fill_event_refs
                if event_store.get(ref) is not None
            }
            self.assertEqual(snapshot_event.payload.get("source_fill_id") in source_fill_ids, True)

            self.assertTrue(record.reconciliation_refs)
            for reconciliation_ref in record.reconciliation_refs:
                reconciliation_event = event_store.get(reconciliation_ref)
                self.assertIsNotNone(reconciliation_event)
                self.assertEqual(reconciliation_event.payload.get("decision_id"), record.decision_id)
                self.assertEqual(
                    reconciliation_event.payload.get("portfolio_snapshot_ref"),
                    record.portfolio_delta_ref,
                )


if __name__ == "__main__":
    unittest.main()
