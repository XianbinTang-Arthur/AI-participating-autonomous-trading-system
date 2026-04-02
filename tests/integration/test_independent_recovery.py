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
from aats.schemas.strategy_runtime import (
    PortfolioAllocationDecision,
    StrategyBookRuntimeState,
    StrategyExecutionBundle,
    StrategyLegIntent,
    StrategySleeveIntent,
)
from aats.services.execution_engine.recovery import ExecutionRecoveryService
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService


class TestIndependentRecoveryIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_system_recovery_surfaces_independent_recovery_snapshot_with_adaptive_posture(self) -> None:
        runtime = await self._runtime()
        now = datetime.now(timezone.utc)
        runtime.portfolio_repo.save_snapshot(
            PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": Decimal("10000")},
                positions=[],
                cost_basis={},
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                total_equity=Decimal("10000"),
                gross_exposure=Decimal("0"),
                net_exposure=Decimal("0"),
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            )
        )
        long_runtime_state = StrategyBookRuntimeState(
            leg="long",
            current_qty=Decimal("0"),
            target_qty=Decimal("0.02"),
            state="opening",
            score=0.81,
            score_raw=0.81,
            score_adjusted=0.81,
            book_state="probing",
            holding_phase="entry",
            health_state="ok",
            eligibility_state="eligible",
            book_action="open",
            prior_book_state="flat",
            reason_codes=["independent_long_book_signal_above_entry_threshold"],
            blocked_reasons=[],
            size_multiplier=Decimal("0.73"),
            capital_multiplier=Decimal("0.73"),
            execution_chain_id="independent:decision_independent_recovery:long:open",
            execution_attempt_id="attempt_independent_recovery_1",
            current_scale_in_count=0,
            current_de_risk_count=0,
            state_version=2,
            transition_valid=True,
            threshold_snapshot={
                "leg": "long",
                "shadow_only": False,
                "rollout_enabled": True,
                "live_applied": True,
                "health_enforcement_enabled": True,
                "size_down_entry_enabled": True,
                "long_short_asymmetry_enabled": True,
                "entry_threshold": 0.60,
                "adaptive_entry_threshold": 0.66,
                "effective_entry_threshold": 0.66,
                "close_threshold": 0.48,
                "adaptive_close_threshold": 0.50,
                "effective_close_threshold": 0.50,
                "scale_in_threshold": 0.90,
                "adaptive_scale_in_threshold": 0.96,
                "effective_scale_in_threshold": 0.96,
                "thesis_age_seconds": 1800.0,
                "adaptive_thesis_age_seconds": 1500.0,
                "effective_thesis_age_seconds": 1500.0,
                "de_risk_net_edge_bps": 2.0,
                "adaptive_de_risk_net_edge_bps": 2.6,
                "effective_de_risk_net_edge_bps": 2.6,
                "capital_multiplier": 0.73,
                "reason_codes": ["adaptive_shadow_confidence_adjusted"],
            },
            leg_health_summary={
                "leg": "long",
                "health_state": "ok",
                "halt_openings": False,
                "only_reduce": False,
                "suspended": False,
                "warnings": [],
                "blockers": [],
            },
        )
        short_runtime_state = StrategyBookRuntimeState(
            leg="short",
            current_qty=Decimal("0"),
            target_qty=Decimal("0"),
            state="inactive",
            score=0.12,
            score_raw=0.12,
            score_adjusted=0.12,
            book_state="flat",
            holding_phase=None,
            health_state="ok",
            book_action="inactive",
            reason_codes=["independent_short_book_signal_below_entry_threshold"],
            blocked_reasons=[],
        )
        runtime.strategy_runtime_repo.save_allocation_decision(
            PortfolioAllocationDecision(
                allocation_id="alloc_independent_recovery",
                decision_id="decision_independent_recovery",
                symbol="BTC-USDT-SWAP",
                product_type="derivatives",
                margin_mode="cross",
                primary_family="independent",
                approved_families=["independent"],
                sleeve_intents=[
                    StrategySleeveIntent(
                        decision_id="decision_independent_recovery",
                        family="independent",
                        strategy_sleeve_id="sleeve_independent_recovery",
                        state="candidate",
                        symbol="BTC-USDT-SWAP",
                        product_type="derivatives",
                        margin_mode="cross",
                        inventory_policy="paired_inventory",
                        route_action="override_target",
                        family_action="open_independent_book",
                        selectable=True,
                        execution_compatible=True,
                        metrics={
                            "book_runtime_states": [
                                long_runtime_state.model_dump(mode="json"),
                                short_runtime_state.model_dump(mode="json"),
                            ],
                            "family_health_overall_state": "ok",
                            "family_health_blockers": [],
                            "long_threshold_snapshot": long_runtime_state.threshold_snapshot,
                            "long_health_snapshot": long_runtime_state.leg_health_summary,
                            "long_replay_snapshot": {
                                "leg": "long",
                                "score": 0.81,
                                "state": "opening",
                                "book_state": "probing",
                                "holding_phase": "entry",
                                "health_state": "ok",
                                "book_action": "open",
                                "policy_reason": "independent_entry_guarded_passive_first",
                                "prior_book_state": "flat",
                                "transition_reconstructed": True,
                                "transition_source": "runtime_state",
                            },
                            "long_execution_style_preference": "bounded_limit_ioc",
                            "long_order_type_preference": "limit",
                            "long_time_in_force_preference": "IOC",
                        },
                    )
                ],
                execution_legs=[
                    StrategyLegIntent(
                        symbol="BTC-USDT-SWAP",
                        execution_chain_id="independent:decision_independent_recovery:long:open",
                        execution_attempt_id="attempt_independent_recovery_1",
                        product_type="derivatives",
                        side="buy",
                        position_mode="long_short_mode",
                        pos_side="long",
                        action="open",
                        family="independent",
                        role="hedge",
                        strategy_sleeve_id="sleeve_independent_recovery",
                        margin_mode="cross",
                        current_position_qty=Decimal("0"),
                        target_position_qty=Decimal("0.02"),
                        delta_position_qty=Decimal("0.02"),
                        execution_compatible=True,
                        execution_mode="independent_long_book",
                    )
                ],
            )
        )
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_independent_recovery",
                execution_chain_id="independent:decision_independent_recovery:long:open",
                execution_attempt_id="attempt_independent_recovery_1",
                intent_id="intent_independent_recovery_1",
                symbol="BTC-USDT-SWAP",
                client_order_id="cl_independent_recovery_1",
                venue="OKX",
                exchange_order_id=None,
                status="SUBMITTED",
                submission_mode="guarded_simulated_submit",
                submitted_ts=now,
                last_update_ts=now,
                requested_qty=Decimal("0.02"),
                filled_qty=Decimal("0"),
                remaining_qty=Decimal("0.02"),
                average_fill_price=None,
                fees=Decimal("0"),
                product_type="derivatives",
                margin_mode="cross",
                strategy_family="independent",
                strategy_sleeve_id="sleeve_independent_recovery",
                allocation_id="alloc_independent_recovery",
                strategy_bundle_id="bundle_independent_recovery",
                strategy_leg_role="hedge",
                pos_side="long",
                strategy_execution_mode="independent_long_book",
                submission_payload={},
            )
        )
        runtime.strategy_runtime_repo.save_execution_bundle(
            StrategyExecutionBundle(
                bundle_id="bundle_independent_recovery",
                decision_id="decision_independent_recovery",
                family="independent",
                participating_families=["independent"],
                strategy_sleeve_refs=["sleeve_independent_recovery"],
                allocation_id="alloc_independent_recovery",
                product_type="derivatives",
                margin_mode="cross",
                allowed_symbols=("BTC-USDT-SWAP",),
                route_action="override_target",
                bundle_type="single_sleeve",
                status="partial_fill_recovery",
                selected_symbol="BTC-USDT-SWAP",
                legs=[
                    StrategyLegIntent(
                        symbol="BTC-USDT-SWAP",
                        execution_chain_id="independent:decision_independent_recovery:long:open",
                        execution_attempt_id="attempt_independent_recovery_1",
                        product_type="derivatives",
                        side="buy",
                        position_mode="long_short_mode",
                        pos_side="long",
                        action="open",
                        family="independent",
                        role="hedge",
                        strategy_sleeve_id="sleeve_independent_recovery",
                        margin_mode="cross",
                        current_position_qty=Decimal("0"),
                        target_position_qty=Decimal("0.02"),
                        delta_position_qty=Decimal("0.02"),
                        execution_compatible=True,
                        execution_mode="independent_long_book",
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

        self.assertTrue(recovery["recovery"]["independent_recovery_snapshots"])
        snapshot = recovery["recovery"]["independent_recovery_snapshots"][0]
        self.assertEqual(snapshot["recovery_posture"], "pending_execution_attempts")
        self.assertEqual(snapshot["book_state"], "probing")
        self.assertEqual(snapshot["prior_book_state"], "flat")
        self.assertTrue(snapshot["transition_valid"])
        self.assertEqual(snapshot["decision_snapshot"]["sizing_outcome"]["capital_multiplier"], 0.73)
        self.assertEqual(snapshot["decision_snapshot"]["prior_book_state"], "flat")
        self.assertTrue(snapshot["threshold_snapshot"]["live_applied"])
        self.assertEqual(snapshot["threshold_snapshot"]["effective_entry_threshold"], 0.66)
        self.assertEqual(
            snapshot["active_execution_chain_ids"],
            ["independent:decision_independent_recovery:long:open"],
        )
        self.assertEqual(snapshot["unresolved_attempt_ids"], ["attempt_independent_recovery_1"])

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
