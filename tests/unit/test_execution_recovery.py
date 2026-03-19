from __future__ import annotations

import unittest
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.execution import OrderObligation, OrderState
from aats.schemas.portfolio import PortfolioSnapshot
from aats.services.execution_engine.recovery import ExecutionRecoveryService
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.portfolio_service.positions import PortfolioState
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService
from aats.services.portfolio_service.snapshots import PortfolioSnapshotBuilder
from aats.services.portfolio_service.pnl import PortfolioPnLCalculator
from aats.storage.execution_repo import InMemoryExecutionRepository
from aats.storage.obligation_repo import InMemoryExecutionObligationRepository
from aats.storage.portfolio_repo import InMemoryPortfolioRepository
from aats.storage.reconciliation_repo import InMemoryReconciliationRepository


class TestExecutionRecovery(unittest.TestCase):
    def test_recovery_releases_orphan_active_obligation_without_order_state(self) -> None:
        obligation_repo = InMemoryExecutionObligationRepository()
        obligation_repo.save_obligation(
            OrderObligation(
                client_order_id="cl_orphan_1",
                decision_id="decision_orphan_1",
                intent_id="intent_orphan_1",
                symbol="BTC-USDT",
                side="buy",
                reserve_currency="USDT",
                reserved_amount=60.0,
                status="ACTIVE",
                product_type="spot",
                margin_mode="cash",
                last_update_ts=utc_now(),
            )
        )
        recovery = self._service(obligation_repo=obligation_repo)

        artifacts = recovery.recover(portfolio_state=PortfolioState(initial_usdt_balance=10_000.0))

        obligation = obligation_repo.get_obligation("cl_orphan_1")
        self.assertIsNotNone(obligation)
        self.assertEqual(obligation.status, "FAILED")
        self.assertEqual(obligation.released_amount, Decimal("60.0"))
        self.assertIn("released_orphan_obligations:1", artifacts.status.notes)

    def test_recovery_finalizes_active_obligation_for_terminal_order_state(self) -> None:
        execution_repo = InMemoryExecutionRepository()
        now = utc_now()
        execution_repo.save_order_state(
            OrderState(
                decision_id="decision_canceled_1",
                intent_id="intent_canceled_1",
                symbol="BTC-USDT",
                client_order_id="cl_canceled_1",
                venue="OKX",
                exchange_order_id="ord_canceled_1",
                status="CANCELED",
                submission_mode="guarded_simulated_submit",
                submitted_ts=now,
                last_update_ts=now,
                requested_qty=0.001,
                filled_qty=0.0,
                remaining_qty=0.001,
                average_fill_price=None,
                fees=0.0,
                product_type="spot",
                margin_mode="cash",
                submission_payload={},
            )
        )
        obligation_repo = InMemoryExecutionObligationRepository()
        obligation_repo.save_obligation(
            OrderObligation(
                client_order_id="cl_canceled_1",
                decision_id="decision_canceled_1",
                intent_id="intent_canceled_1",
                symbol="BTC-USDT",
                side="buy",
                reserve_currency="USDT",
                reserved_amount=60.0,
                status="ACTIVE",
                product_type="spot",
                margin_mode="cash",
                last_update_ts=now,
            )
        )
        recovery = self._service(execution_repo=execution_repo, obligation_repo=obligation_repo)

        artifacts = recovery.recover(portfolio_state=PortfolioState(initial_usdt_balance=10_000.0))

        obligation = obligation_repo.get_obligation("cl_canceled_1")
        self.assertIsNotNone(obligation)
        self.assertEqual(obligation.status, "CANCELED")
        self.assertEqual(obligation.released_amount, Decimal("60.0"))
        self.assertIn("released_orphan_obligations:1", artifacts.status.notes)

    def test_bootstrap_recovery_validates_latest_snapshot_against_trusted_baseline(self) -> None:
        portfolio_repo = InMemoryPortfolioRepository()
        baseline_snapshot = PortfolioSnapshot(
            snapshot_ts=utc_now(),
            snapshot_origin="exchange_import",
            balances={"USDT": Decimal("1000")},
            positions=[],
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            total_equity=Decimal("1000"),
            gross_exposure=Decimal("0"),
            net_exposure=Decimal("0"),
            product_type="spot",
            margin_mode="cash",
        )
        portfolio_repo.save_snapshot(baseline_snapshot)
        divergent_snapshot = baseline_snapshot.model_copy(
            update={
                "snapshot_ts": utc_now(),
                "snapshot_origin": "fill_derived",
                "balances": {"USDT": Decimal("900"), "BTC": Decimal("1")},
                "total_equity": Decimal("1000"),
            }
        )
        portfolio_repo.save_snapshot(divergent_snapshot)
        kill_switch = KillSwitch()
        recovery = self._service(
            portfolio_repo=portfolio_repo,
            kill_switch=kill_switch,
            bootstrap_portfolio_from_exchange=True,
        )

        artifacts = recovery.recover(portfolio_state=PortfolioState(initial_usdt_balance=1_000.0))

        self.assertTrue(kill_switch.halted)
        self.assertFalse(artifacts.status.safe_to_trade)
        self.assertIn("stored_snapshot_differs_from_fill_reconstruction", artifacts.status.notes)

    @staticmethod
    def _service(
        *,
        execution_repo: InMemoryExecutionRepository | None = None,
        obligation_repo: InMemoryExecutionObligationRepository | None = None,
        portfolio_repo: InMemoryPortfolioRepository | None = None,
        kill_switch: KillSwitch | None = None,
        bootstrap_portfolio_from_exchange: bool = False,
    ) -> ExecutionRecoveryService:
        settings = AATSSettings.model_validate(
            {
                "storage_mode": "memory",
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "disabled",
                "account_read_enabled": False,
            }
        )
        return ExecutionRecoveryService(
            settings=settings,
            execution_repo=execution_repo or InMemoryExecutionRepository(),
            obligation_repo=obligation_repo or InMemoryExecutionObligationRepository(),
            portfolio_repo=portfolio_repo or InMemoryPortfolioRepository(),
            reconciliation_repo=InMemoryReconciliationRepository(),
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=settings.initial_usdt_balance,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            price_provider=lambda _symbol: Decimal("0"),
            kill_switch=kill_switch or KillSwitch(),
            bootstrap_portfolio_from_exchange=bootstrap_portfolio_from_exchange,
            reconciliation_stale_after_seconds=settings.reconciliation_stale_after_seconds,
        )
