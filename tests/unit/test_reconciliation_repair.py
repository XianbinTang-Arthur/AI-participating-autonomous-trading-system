from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.bus.memory_bus import InMemoryEventBus
from aats.storage.event_store import InMemoryEventStore
from aats.events import topics
from aats.schemas.execution import FillEvent, OrderState
from aats.schemas.portfolio import PortfolioSnapshot
from aats.services.portfolio_service.pnl import PortfolioPnLCalculator
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService
from aats.services.portfolio_service.snapshots import PortfolioSnapshotBuilder
from aats.services.reconciliation_service.comparator import StateComparator
from aats.services.reconciliation_service.fetcher import ExchangeStateFetcher
from aats.services.reconciliation_service.repair import ReconciliationRepairService, ReconciliationService
from aats.storage.execution_repo import InMemoryExecutionRepository
from aats.storage.portfolio_repo import InMemoryPortfolioRepository
from aats.storage.reconciliation_repo import InMemoryReconciliationRepository


def build_fill() -> FillEvent:
    now = datetime.now(timezone.utc)
    return FillEvent(
        fill_id="fill_repair_1",
        decision_id="decision_repair_1",
        intent_id="intent_repair_1",
        client_order_id="clord_repair_1",
        exchange_order_id="ord_repair_1",
        symbol="BTC-USDT",
        venue="PAPER",
        side="buy",
        fill_qty=0.001,
        fill_price=100.0,
        fee_amount=0.0,
        fee_currency="USDT",
        liquidity_role="taker",
        exchange_timestamp=now,
        ingestion_timestamp=now,
    )


class TestReconciliationRepair(unittest.IsolatedAsyncioTestCase):
    async def test_local_only_snapshot_divergence_is_rebuilt_safely(self) -> None:
        event_store = InMemoryEventStore()
        bus = InMemoryEventBus(event_store=event_store, persistence_mode="strict")
        execution_repo = InMemoryExecutionRepository()
        fill = build_fill()
        execution_repo.save_order_state(
            OrderState(
                decision_id="decision_repair_1",
                intent_id="intent_repair_1",
                symbol="BTC-USDT",
                client_order_id="clord_repair_1",
                venue="PAPER",
                exchange_order_id="ord_repair_1",
                status="FILLED",
                submitted_ts=fill.exchange_timestamp,
                last_update_ts=fill.ingestion_timestamp,
                requested_qty=0.001,
                filled_qty=0.001,
                remaining_qty=0.0,
                average_fill_price=100.0,
                fees=0.0,
            )
        )
        execution_repo.save_fill(fill)
        portfolio_repo = InMemoryPortfolioRepository()
        portfolio_repo.save_snapshot(
            PortfolioSnapshot(
                snapshot_ts=datetime.now(timezone.utc),
                decision_id="decision_repair_1",
                balances={"USDT": Decimal("10000.0")},
                positions=[],
                cost_basis={},
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                total_equity=Decimal("10000.0"),
                gross_exposure=Decimal("0"),
                net_exposure=Decimal("0"),
                risk_budget_usage={},
            )
        )
        reconciliation_repo = InMemoryReconciliationRepository()
        service = ReconciliationService(
            settings=AATSSettings.model_validate({}),
            bus=bus,
            fetcher=ExchangeStateFetcher(account_service=None),
            comparator=StateComparator(),
            repair_service=ReconciliationRepairService(),
            reconciliation_repo=reconciliation_repo,
            execution_repo=execution_repo,
            portfolio_repo=portfolio_repo,
            event_store=event_store,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            price_provider=lambda symbol: 100.0 if symbol == "BTC-USDT" else 0.0,
            bootstrap_portfolio_from_exchange=False,
            metrics=None,
        )

        first_report = await service.validate_now(reason="unit_repair")
        second_report = await service.validate_now(reason="unit_repair_after_rebuild")

        self.assertTrue(first_report.halt_required)
        repaired_snapshot = portfolio_repo.latest()
        self.assertIsNotNone(repaired_snapshot)
        self.assertEqual(repaired_snapshot.positions[0].symbol, "BTC-USDT")
        self.assertEqual(repaired_snapshot.positions[0].position_qty, Decimal("0.001"))
        self.assertEqual(repaired_snapshot.total_equity, Decimal("10000.0"))
        self.assertFalse(second_report.halt_required)
        self.assertEqual(second_report.severity, "CLEAN")
        self.assertEqual(event_store.count(topic=topics.PORTFOLIO_SNAPSHOTS), 1)
        self.assertEqual(event_store.count(topic=topics.RECONCILIATION_REPORTS), 2)


if __name__ == "__main__":
    unittest.main()
