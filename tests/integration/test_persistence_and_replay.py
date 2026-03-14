from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aats.bootstrap.config import build_runtime, build_storage_backends
from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.services.portfolio_service.pnl import PortfolioPnLCalculator
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService
from aats.services.portfolio_service.snapshots import PortfolioSnapshotBuilder
from aats.services.reconciliation_service.replay import ReplayEngine


class TestPersistenceAndReplay(unittest.IsolatedAsyncioTestCase):
    async def test_sqlalchemy_event_store_persists_and_queries_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._sqlite_settings(Path(temp_dir))
            runtime = await build_runtime(settings)
            fresh_storage = None
            try:
                await runtime.market_gateway.run_local_publisher(
                    symbol=settings.default_symbol,
                    iterations=4,
                    interval_seconds=0.0,
                )

                audit_records = runtime.audit_repo.all()
                self.assertTrue(audit_records)

                event_store = runtime.event_store
                self.assertGreater(len(event_store.all()), 0)
                self.assertEqual(len(event_store.by_topic(topics.MARKET_SNAPSHOTS)), 4)
                self.assertTrue(event_store.by_decision(audit_records[0].decision_id))

                fresh_storage = build_storage_backends(settings)
                self.assertEqual(len(fresh_storage.event_store.all()), len(event_store.all()))
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()
                if fresh_storage is not None and fresh_storage.database_runtime is not None:
                    fresh_storage.database_runtime.dispose()

    async def test_replay_reconstructs_same_portfolio_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._sqlite_settings(Path(temp_dir))
            runtime = await build_runtime(settings)
            storage = None
            try:
                await runtime.market_gateway.run_local_publisher(
                    symbol=settings.default_symbol,
                    iterations=4,
                    interval_seconds=0.0,
                )

                storage = build_storage_backends(settings)
                replay = ReplayEngine(
                    event_store=storage.event_store,
                    reconstruction_service=PortfolioReconstructionService(
                        initial_usdt_balance=settings.initial_usdt_balance,
                        snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
                    ),
                ).replay()

                self.assertEqual(replay.divergence_count, 0)
                self.assertIsNotNone(replay.final_reconstructed_snapshot)
                self.assertIsNotNone(replay.final_stored_snapshot)
                self.assertEqual(
                    self._snapshot_signature(replay.final_stored_snapshot),
                    self._snapshot_signature(runtime.portfolio_repo.latest()),
                )
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()
                if storage is not None and storage.database_runtime is not None:
                    storage.database_runtime.dispose()

    async def test_audit_records_reference_persisted_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._sqlite_settings(Path(temp_dir))
            runtime = await build_runtime(settings)
            storage = None
            try:
                await runtime.market_gateway.run_local_publisher(
                    symbol=settings.default_symbol,
                    iterations=4,
                    interval_seconds=0.0,
                )

                storage = build_storage_backends(settings)
                event_store = storage.event_store
                for record in storage.audit_repo.all():
                    self.assertIsNotNone(event_store.get(record.decision_context_ref))
                    if record.baseline_assessment_ref is not None:
                        self.assertIsNotNone(event_store.get(record.baseline_assessment_ref))
                    if record.ai_market_assessment_ref is not None:
                        self.assertIsNotNone(event_store.get(record.ai_market_assessment_ref))
                    if record.position_target_ref is not None:
                        self.assertIsNotNone(event_store.get(record.position_target_ref))
                    if record.policy_decision_ref is not None:
                        self.assertIsNotNone(event_store.get(record.policy_decision_ref))
                    if record.risk_decision_ref is not None:
                        self.assertIsNotNone(event_store.get(record.risk_decision_ref))
                    for ref in record.order_intent_refs + record.fill_event_refs + record.reconciliation_refs:
                        self.assertIsNotNone(event_store.get(ref))
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()
                if storage is not None and storage.database_runtime is not None:
                    storage.database_runtime.dispose()

    @staticmethod
    def _sqlite_settings(temp_dir: Path) -> AATSSettings:
        database_path = (temp_dir / "aats.db").resolve().as_posix()
        return AATSSettings.model_validate(
            {
                "storage_mode": "postgres",
                "database_url": f"sqlite+pysqlite:///{database_path}",
                "database_auto_create_schema": True,
                "local_publish_iterations": 4,
                "local_publish_interval_seconds": 0.0,
            }
        )

    @staticmethod
    def _snapshot_signature(snapshot) -> dict:
        return {
            "balances": snapshot.balances,
            "positions": {position.symbol: position.position_qty for position in snapshot.positions},
            "cost_basis": snapshot.cost_basis,
            "realized_pnl": snapshot.realized_pnl,
            "unrealized_pnl": snapshot.unrealized_pnl,
            "total_equity": snapshot.total_equity,
            "gross_exposure": snapshot.gross_exposure,
            "net_exposure": snapshot.net_exposure,
        }


if __name__ == "__main__":
    unittest.main()
