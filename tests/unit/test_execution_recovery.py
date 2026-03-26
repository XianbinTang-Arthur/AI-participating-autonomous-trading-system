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
from aats.schemas.reconciliation import ReconciliationReport


class TestExecutionRecovery(unittest.TestCase):
    def test_recovery_tracks_structured_bundle_open_orders_without_halting(self) -> None:
        execution_repo = InMemoryExecutionRepository()
        now = utc_now()
        for client_order_id, family, sleeve_id in (
            ("cl_bundle_grid", "spot_grid", "sleeve_grid"),
            ("cl_bundle_dca", "dca", "sleeve_dca"),
        ):
            execution_repo.save_order_state(
                OrderState(
                    decision_id="decision_bundle_1",
                    intent_id=f"intent_{client_order_id}",
                    symbol="BTC-USDT",
                    client_order_id=client_order_id,
                    venue="OKX",
                    exchange_order_id=f"ord_{client_order_id}",
                    status="SUBMITTED",
                    submission_mode="guarded_live_submit",
                    submitted_ts=now,
                    last_update_ts=now,
                    requested_qty=0.001,
                    filled_qty=0.0,
                    remaining_qty=0.001,
                    average_fill_price=None,
                    fees=0.0,
                    product_type="spot",
                    margin_mode="cash",
                    strategy_family=family,
                    strategy_sleeve_id=sleeve_id,
                    allocation_id="alloc_bundle_1",
                    strategy_bundle_id="bundle_spot_inventory",
                    strategy_leg_role="inventory",
                    submission_payload={},
                )
            )
        recovery = self._service(execution_repo=execution_repo)

        artifacts = recovery.recover(portfolio_state=PortfolioState(initial_usdt_balance=10_000.0))

        self.assertFalse(artifacts.status.halted)
        self.assertEqual(artifacts.status.recovery_state, "bundle_recovery")
        self.assertTrue(artifacts.status.bundle_recovery_required)
        self.assertEqual(artifacts.status.bundle_recovery_count, 1)
        self.assertEqual(artifacts.status.recoverable_bundle_count, 1)
        self.assertEqual(artifacts.status.open_order_count, 2)
        self.assertFalse(artifacts.status.safe_to_trade)
        self.assertTrue(artifacts.status.only_reduce_required)
        self.assertIn("strategy_bundle_recovery_in_progress", artifacts.status.only_reduce_reasons)
        self.assertEqual(artifacts.status.bundle_summaries[0].recovery_state, "structured_open_orders")

    def test_recovery_halts_when_bundle_open_orders_missing_identity(self) -> None:
        execution_repo = InMemoryExecutionRepository()
        now = utc_now()
        execution_repo.save_order_state(
            OrderState(
                decision_id="decision_bundle_missing_1",
                intent_id="intent_bundle_missing_1",
                symbol="BTC-USDT",
                client_order_id="cl_bundle_missing_1",
                venue="OKX",
                exchange_order_id="ord_bundle_missing_1",
                status="SUBMITTED",
                submission_mode="guarded_live_submit",
                submitted_ts=now,
                last_update_ts=now,
                requested_qty=0.001,
                filled_qty=0.0,
                remaining_qty=0.001,
                average_fill_price=None,
                fees=0.0,
                product_type="spot",
                margin_mode="cash",
                strategy_family="spot_grid",
                allocation_id="alloc_bundle_missing",
                strategy_bundle_id="bundle_missing_identity",
                strategy_leg_role="inventory",
                submission_payload={},
            )
        )
        kill_switch = KillSwitch()
        recovery = self._service(execution_repo=execution_repo, kill_switch=kill_switch)

        artifacts = recovery.recover(portfolio_state=PortfolioState(initial_usdt_balance=10_000.0))

        self.assertTrue(kill_switch.halted)
        self.assertTrue(artifacts.status.halted)
        self.assertTrue(artifacts.status.bundle_recovery_required)
        self.assertEqual(artifacts.status.recovery_state, "review_required")
        self.assertIn("strategy_bundle_recovery_requires_review", artifacts.status.resume_blocked_reasons)
        self.assertEqual(artifacts.status.recovery_action, "halted_open_orders_require_review")

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

    def test_recovery_scopes_margin_backed_smart_arbitrage_spot_obligation_into_derivatives_runtime(self) -> None:
        obligation_repo = InMemoryExecutionObligationRepository()
        obligation_repo.save_obligation(
            OrderObligation(
                client_order_id="cl_margin_backed_orphan_1",
                decision_id="decision_margin_backed_orphan_1",
                intent_id="intent_margin_backed_orphan_1",
                symbol="BTC-USDT",
                side="sell",
                reserve_currency="BTC",
                reserved_amount=0.25,
                status="ACTIVE",
                product_type="spot",
                margin_mode="cross",
                strategy_family="smart_arbitrage",
                strategy_bundle_id="bundle_margin_backed_1",
                strategy_leg_role="hedge",
                last_update_ts=utc_now(),
            )
        )
        recovery = self._service(
            obligation_repo=obligation_repo,
            settings_override={
                "config_profile": "guarded_derivatives_dry_run",
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ["BTC-USDT-SWAP", "BTC-USDT"],
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_negative_basis_mode": "margin_backed",
                "smart_arbitrage_margin_short_enabled": True,
                "smart_arbitrage_margin_short_execution_ready": True,
                "smart_arbitrage_margin_short_spot_margin_mode": "cross",
            },
        )

        artifacts = recovery.recover(portfolio_state=PortfolioState(initial_usdt_balance=10_000.0))

        obligation = obligation_repo.get_obligation("cl_margin_backed_orphan_1")
        self.assertIsNotNone(obligation)
        self.assertEqual(obligation.status, "FAILED")
        self.assertEqual(obligation.released_amount, Decimal("0.25"))
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

    def test_recovery_marks_derivatives_only_reduce_when_latest_reconciliation_requires_it(self) -> None:
        reconciliation_repo = InMemoryReconciliationRepository()
        reconciliation_repo.save_report(
            ReconciliationReport(
                reconciliation_id="recon_only_reduce_recovery",
                as_of_ts=utc_now(),
                product_type="derivatives",
                margin_mode="cross",
                allowed_symbols=["BTC-USDT-SWAP"],
                exchange_comparison_enabled=True,
                order_diff={"reconstructed": {}, "exchange": {}},
                fill_diff={"replayed": {}, "exchange": {}},
                balance_diff={"reconstructed": {}, "exchange": {}},
                position_diff={
                    "stored": {},
                    "reconstructed": {},
                    "reconstructed_mismatches": {},
                    "exchange": {"BTC-USDT-SWAP": "0.02"},
                    "exchange_mismatches": {"BTC-USDT-SWAP": {"stored": "0", "exchange": "0.02"}},
                },
                mismatch_categories=["derivatives_exchange_position_without_local_execution_chain"],
                mismatch_reasons=["derivatives_exchange_position_not_replayed_locally"],
                safety_impacts=["derivatives_only_reduce_until_position_reconciled"],
                severity="SOFT_MISMATCH",
                only_reduce_required=True,
                only_reduce_reasons=["derivatives_exchange_position_without_local_execution_chain"],
                recovery_classification="derivatives_only_reduce",
                recommended_operator_action="go_close_position_on_exchange",
            )
        )
        recovery = self._service(
            reconciliation_repo=reconciliation_repo,
            settings_override={
                "config_profile": "guarded_derivatives_dry_run",
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "margin_mode": "cross",
                "trading_product_type": "derivatives",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ["BTC-USDT-SWAP"],
            },
        )

        artifacts = recovery.recover(portfolio_state=PortfolioState(initial_usdt_balance=10_000.0))

        self.assertEqual(artifacts.status.recovery_state, "only_reduce")
        self.assertTrue(artifacts.status.safe_to_trade)
        self.assertTrue(artifacts.status.resume_eligible)
        self.assertTrue(artifacts.status.only_reduce_required)
        self.assertIn("derivatives_exchange_position_without_local_execution_chain", artifacts.status.only_reduce_reasons)

    @staticmethod
    def _service(
        *,
        execution_repo: InMemoryExecutionRepository | None = None,
        obligation_repo: InMemoryExecutionObligationRepository | None = None,
        portfolio_repo: InMemoryPortfolioRepository | None = None,
        kill_switch: KillSwitch | None = None,
        bootstrap_portfolio_from_exchange: bool = False,
        reconciliation_repo: InMemoryReconciliationRepository | None = None,
        settings_override: dict | None = None,
    ) -> ExecutionRecoveryService:
        payload = {
            "storage_mode": "memory",
            "market_data_backend": "demo",
            "execution_backend": "paper",
            "account_backend": "disabled",
            "account_read_enabled": False,
        }
        payload.update(settings_override or {})
        settings = AATSSettings.model_validate(payload)
        return ExecutionRecoveryService(
            settings=settings,
            execution_repo=execution_repo or InMemoryExecutionRepository(),
            obligation_repo=obligation_repo or InMemoryExecutionObligationRepository(),
            portfolio_repo=portfolio_repo or InMemoryPortfolioRepository(),
            reconciliation_repo=reconciliation_repo or InMemoryReconciliationRepository(),
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=settings.initial_usdt_balance,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            price_provider=lambda _symbol: Decimal("0"),
            kill_switch=kill_switch or KillSwitch(),
            bootstrap_portfolio_from_exchange=bootstrap_portfolio_from_exchange,
            reconciliation_stale_after_seconds=settings.reconciliation_stale_after_seconds,
        )
