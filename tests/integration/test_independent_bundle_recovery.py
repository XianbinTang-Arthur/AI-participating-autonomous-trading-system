from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aats.api.auth_routes import auth_router
from aats.api.routes import router
from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.schemas.execution import OrderState
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.strategy_runtime import StrategyExecutionBundle, StrategyLegIntent
from aats.services.execution_engine.recovery import ExecutionRecoveryService
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService


class TestIndependentBundleRecoveryIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_system_recovery_preserves_bundle_leg_chain_and_attempt_ids_for_independent(self) -> None:
        runtime = await self._runtime()
        now = datetime.now(timezone.utc)
        runtime.portfolio_repo.save_snapshot(
            PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": Decimal("9000")},
                positions=[],
                cost_basis={},
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                total_equity=Decimal("9000"),
                gross_exposure=Decimal("0"),
                net_exposure=Decimal("0"),
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            )
        )
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_bundle_independent_1",
                execution_chain_id="independent:decision_bundle_independent_1:short:open",
                execution_attempt_id="attempt_bundle_independent_1",
                intent_id="intent_bundle_independent_1",
                symbol="BTC-USDT-SWAP",
                client_order_id="cl_bundle_independent_1",
                venue="OKX",
                exchange_order_id=None,
                status="SUBMITTED",
                submission_mode="guarded_simulated_submit",
                submitted_ts=now,
                last_update_ts=now,
                requested_qty=Decimal("0.01"),
                filled_qty=Decimal("0"),
                remaining_qty=Decimal("0.01"),
                average_fill_price=None,
                fees=Decimal("0"),
                product_type="derivatives",
                margin_mode="cross",
                position_mode="long_short_mode",
                pos_side="short",
                strategy_family="independent",
                strategy_sleeve_id="sleeve_bundle_independent_short",
                allocation_id="alloc_bundle_independent_1",
                strategy_bundle_id="bundle_independent_open_short",
                strategy_leg_role="hedge",
                strategy_execution_mode="independent_short_book",
                submission_payload={},
            )
        )
        runtime.strategy_runtime_repo.save_execution_bundle(
            StrategyExecutionBundle(
                bundle_id="bundle_independent_open_short",
                decision_id="decision_bundle_independent_1",
                family="independent",
                participating_families=["independent"],
                strategy_sleeve_refs=["sleeve_bundle_independent_short"],
                allocation_id="alloc_bundle_independent_1",
                product_type="derivatives",
                margin_mode="cross",
                allowed_symbols=("BTC-USDT-SWAP",),
                route_action="override_target",
                bundle_type="single_sleeve",
                status="partial_fill_recovery",
                selected_symbol="BTC-USDT-SWAP",
                reason_codes=["strategy_bundle_partial_fill_recovery"],
                legs=[
                    StrategyLegIntent(
                        symbol="BTC-USDT-SWAP",
                        execution_chain_id="independent:decision_bundle_independent_1:short:open",
                        execution_attempt_id="attempt_bundle_independent_1",
                        product_type="derivatives",
                        side="sell",
                        position_mode="long_short_mode",
                        pos_side="short",
                        action="open",
                        family="independent",
                        role="hedge",
                        strategy_sleeve_id="sleeve_bundle_independent_short",
                        allocation_id="alloc_bundle_independent_1",
                        margin_mode="cross",
                        current_position_qty=Decimal("0"),
                        target_position_qty=Decimal("-0.01"),
                        delta_position_qty=Decimal("-0.01"),
                        execution_compatible=True,
                        execution_mode="independent_short_book",
                    )
                ],
            )
        )

        recovery_service = ExecutionRecoveryService(
            settings=runtime.settings,
            execution_repo=runtime.execution_repo,
            obligation_repo=runtime.obligation_repo,
            portfolio_repo=runtime.portfolio_repo,
            reconciliation_repo=runtime.reconciliation_repo,
            strategy_runtime_repo=runtime.strategy_runtime_repo,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=runtime.settings.initial_usdt_balance,
                snapshot_builder=runtime.portfolio_service.snapshot_builder,
            ),
            price_provider=runtime.market_gateway.latest_price,
            kill_switch=runtime.kill_switch,
            bootstrap_portfolio_from_exchange=False,
            reconciliation_stale_after_seconds=runtime.settings.reconciliation_stale_after_seconds,
            recovery_policy=runtime.recovery_policy,
        )
        artifacts = recovery_service.recover(portfolio_state=runtime.portfolio_service.state)
        runtime.recovery_status = artifacts.status
        app = self._app(runtime)

        with TestClient(app) as client:
            recovery = client.get("/system/recovery").json()

        self.assertTrue(recovery["recovery"]["bundle_summaries"])
        bundle = next(
            item
            for item in recovery["recovery"]["bundle_summaries"]
            if item["bundle_id"] == "bundle_independent_open_short"
        )
        self.assertEqual(bundle["recovery_state"], "partial_fill_recovery")
        self.assertEqual(bundle["participating_families"], ["independent"])
        self.assertEqual(
            bundle["legs"][0]["execution_chain_id"],
            "independent:decision_bundle_independent_1:short:open",
        )
        self.assertEqual(bundle["legs"][0]["execution_attempt_id"], "attempt_bundle_independent_1")
        self.assertEqual(bundle["legs"][0]["strategy_execution_mode"], "independent_short_book")

    async def test_system_recovery_ignores_historical_all_blocked_independent_bundle(self) -> None:
        runtime = await self._runtime()
        now = datetime.now(timezone.utc)
        runtime.portfolio_repo.save_snapshot(
            PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": Decimal("9000")},
                positions=[],
                cost_basis={},
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                total_equity=Decimal("9000"),
                gross_exposure=Decimal("0"),
                net_exposure=Decimal("0"),
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            )
        )
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_bundle_blocked_1",
                execution_chain_id="independent:decision_bundle_blocked_1:short:open",
                execution_attempt_id="attempt_bundle_blocked_1",
                intent_id="intent_bundle_blocked_1",
                symbol="BTC-USDT-SWAP",
                client_order_id="cl_bundle_blocked_1",
                venue="OKX",
                exchange_order_id=None,
                status="BLOCKED",
                submission_mode="leg_overlay_rollout_blocked",
                submitted_ts=now,
                last_update_ts=now,
                requested_qty=Decimal("0.01"),
                filled_qty=Decimal("0"),
                remaining_qty=Decimal("0.01"),
                average_fill_price=None,
                fees=Decimal("0"),
                product_type="derivatives",
                margin_mode="cross",
                position_mode="long_short_mode",
                pos_side="short",
                strategy_family="independent",
                strategy_sleeve_id="sleeve_bundle_blocked_short",
                allocation_id="alloc_bundle_blocked_1",
                strategy_bundle_id="bundle_independent_blocked_short",
                strategy_leg_role="hedge",
                strategy_execution_mode="independent_short_book",
                submission_payload={},
            )
        )
        runtime.strategy_runtime_repo.save_execution_bundle(
            StrategyExecutionBundle(
                bundle_id="bundle_independent_blocked_short",
                decision_id="decision_bundle_blocked_1",
                family="independent",
                participating_families=["independent"],
                strategy_sleeve_refs=["sleeve_bundle_blocked_short"],
                allocation_id="alloc_bundle_blocked_1",
                product_type="derivatives",
                margin_mode="cross",
                allowed_symbols=("BTC-USDT-SWAP",),
                route_action="override_target",
                bundle_type="single_sleeve",
                status="blocked",
                selected_symbol="BTC-USDT-SWAP",
                reason_codes=["strategy_bundle_blocked"],
                legs=[
                    StrategyLegIntent(
                        symbol="BTC-USDT-SWAP",
                        execution_chain_id="independent:decision_bundle_blocked_1:short:open",
                        execution_attempt_id="attempt_bundle_blocked_1",
                        product_type="derivatives",
                        side="sell",
                        position_mode="long_short_mode",
                        pos_side="short",
                        action="open",
                        family="independent",
                        role="hedge",
                        strategy_sleeve_id="sleeve_bundle_blocked_short",
                        allocation_id="alloc_bundle_blocked_1",
                        margin_mode="cross",
                        current_position_qty=Decimal("0"),
                        target_position_qty=Decimal("-0.01"),
                        delta_position_qty=Decimal("-0.01"),
                        execution_compatible=True,
                        execution_mode="independent_short_book",
                    )
                ],
            )
        )

        recovery_service = ExecutionRecoveryService(
            settings=runtime.settings,
            execution_repo=runtime.execution_repo,
            obligation_repo=runtime.obligation_repo,
            portfolio_repo=runtime.portfolio_repo,
            reconciliation_repo=runtime.reconciliation_repo,
            strategy_runtime_repo=runtime.strategy_runtime_repo,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=runtime.settings.initial_usdt_balance,
                snapshot_builder=runtime.portfolio_service.snapshot_builder,
            ),
            price_provider=runtime.market_gateway.latest_price,
            kill_switch=runtime.kill_switch,
            bootstrap_portfolio_from_exchange=False,
            reconciliation_stale_after_seconds=runtime.settings.reconciliation_stale_after_seconds,
            recovery_policy=runtime.recovery_policy,
        )
        artifacts = recovery_service.recover(portfolio_state=runtime.portfolio_service.state)
        runtime.recovery_status = artifacts.status
        app = self._app(runtime)

        with TestClient(app) as client:
            recovery = client.get("/system/recovery").json()

        self.assertFalse(recovery["recovery"]["review_required"])
        self.assertFalse(recovery["recovery"]["bundle_summaries"])

    async def _runtime(self):
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
                "operator_unsafe_write_without_auth": True,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
            }
        )
        runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=4,
            interval_seconds=0.0,
        )
        return runtime

    @staticmethod
    def _app(runtime) -> FastAPI:
        app = FastAPI()
        app.include_router(auth_router)
        app.include_router(router)
        app.state.runtime = runtime
        return app


if __name__ == "__main__":
    unittest.main()
