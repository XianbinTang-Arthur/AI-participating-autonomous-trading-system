from __future__ import annotations

import unittest
from decimal import Decimal

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.schemas.execution import LegOrderIntent
from aats.schemas.governance import RiskDecision
from aats.schemas.strategy_runtime import StrategyExecutionBundle, StrategyLegIntent


class TestIndependentLiveRolloutGuard(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_blocks_independent_books_family_mode_before_live_rollout(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "disabled",
                "account_read_enabled": False,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "enabled_decision_timeframes": ("15m",),
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "derivatives_position_mode": "hedge",
                "strategy_hedge_overlay_enabled": True,
                "strategy_hedge_overlay_mode": "independent",
                "strategy_hedge_independent_enabled": True,
                "strategy_hedge_independent_rollout_stage": "dry_run",
                "strategy_family_independent_enabled": True,
                "strategy_family_independent_live_execution_enabled": True,
            }
        )
        runtime = await build_runtime(settings)
        try:
            runtime.order_manager.leg_risk_evaluator = lambda _leg_intent: RiskDecision(
                decision_id="integration_independent_books_risk_ok",
                approved=True,
                modified=False,
                capped_target_position_qty=Decimal("0.001"),
                projected_notional=Decimal("80"),
                current_open_order_count=0,
                risk_budget_multiplier=Decimal("1"),
                execution_aggressiveness_multiplier=Decimal("1"),
                risk_score=0.1,
                rejection_reasons=[],
                constraints_applied=[],
            )
            await runtime.order_manager.submit_leg_order(
                leg_intent=LegOrderIntent(
                    leg_intent_id="leg_integration_independent_books_guard_1",
                    decision_id="decision_integration_independent_books_guard_1",
                    symbol="BTC-USDT-SWAP",
                    side="buy",
                    pos_side="long",
                    action="open",
                    quantity=0.001,
                    execution_style="exchange",
                    order_type="market",
                    urgency="medium",
                    time_in_force="IOC",
                    idempotency_key="leg_integration_independent_books_guard_1",
                    product_type="derivatives",
                    margin_mode="cross",
                    td_mode="cross",
                    position_mode="long_short_mode",
                    target_leverage=2.0,
                    exposure_side="long",
                    strategy_execution_mode="independent_books",
                )
            )

            persisted = runtime.execution_repo.get_order_state("clleg_integration_independent_books_guard_1")
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted.status, "BLOCKED")
            self.assertEqual(persisted.submission_mode, "leg_overlay_rollout_blocked")
            self.assertIn(
                "independent_overlay_rollout_stage_blocks_live_runtime",
                str(persisted.execution_error or ""),
            )
        finally:
            await runtime.stop_background_tasks()

    async def test_runtime_marks_independent_bundle_as_blocked_when_all_legs_fail_pre_submit(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "disabled",
                "account_read_enabled": False,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "enabled_decision_timeframes": ("15m",),
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "derivatives_position_mode": "hedge",
                "strategy_hedge_overlay_enabled": True,
                "strategy_hedge_overlay_mode": "independent",
                "strategy_hedge_independent_enabled": True,
                "strategy_hedge_independent_rollout_stage": "dry_run",
                "strategy_family_independent_enabled": True,
                "strategy_family_independent_live_execution_enabled": True,
            }
        )
        runtime = await build_runtime(settings)
        try:
            runtime.strategy_runtime_repo.save_execution_bundle(
                StrategyExecutionBundle(
                    bundle_id="bundle_integration_independent_blocked_only_1",
                    decision_id="decision_integration_independent_blocked_only_1",
                    family="independent",
                    participating_families=["independent"],
                    strategy_sleeve_refs=["sleeve_integration_independent_blocked_only_1"],
                    allocation_id="alloc_integration_independent_blocked_only_1",
                    product_type="derivatives",
                    margin_mode="cross",
                    allowed_symbols=("BTC-USDT-SWAP",),
                    route_action="override_target",
                    bundle_type="single_sleeve",
                    status="submitted",
                    selected_symbol="BTC-USDT-SWAP",
                    reason_codes=["independent_books_active"],
                    legs=[
                        StrategyLegIntent(
                            symbol="BTC-USDT-SWAP",
                            product_type="derivatives",
                            side="buy",
                            position_mode="long_short_mode",
                            pos_side="long",
                            action="open",
                            family="independent",
                            role="hedge",
                            strategy_sleeve_id="sleeve_integration_independent_blocked_only_1",
                            allocation_id="alloc_integration_independent_blocked_only_1",
                            margin_mode="cross",
                            target_leverage=2.0,
                            current_position_qty=Decimal("0"),
                            target_position_qty=Decimal("0.001"),
                            delta_position_qty=Decimal("0.001"),
                            execution_compatible=True,
                            execution_mode="independent_long_book",
                        )
                    ],
                )
            )
            runtime.order_manager.leg_risk_evaluator = lambda _leg_intent: RiskDecision(
                decision_id="integration_independent_blocked_only_risk_ok",
                approved=True,
                modified=False,
                capped_target_position_qty=Decimal("0.001"),
                projected_notional=Decimal("80"),
                current_open_order_count=0,
                risk_budget_multiplier=Decimal("1"),
                execution_aggressiveness_multiplier=Decimal("1"),
                risk_score=0.1,
                rejection_reasons=[],
                constraints_applied=[],
            )
            await runtime.order_manager.submit_leg_order(
                leg_intent=LegOrderIntent(
                    leg_intent_id="leg_integration_independent_blocked_only_1",
                    decision_id="decision_integration_independent_blocked_only_1",
                    symbol="BTC-USDT-SWAP",
                    side="buy",
                    pos_side="long",
                    action="open",
                    quantity=0.001,
                    execution_style="exchange",
                    order_type="market",
                    urgency="medium",
                    time_in_force="IOC",
                    idempotency_key="leg_integration_independent_blocked_only_1",
                    product_type="derivatives",
                    margin_mode="cross",
                    td_mode="cross",
                    position_mode="long_short_mode",
                    target_leverage=2.0,
                    exposure_side="long",
                    strategy_bundle_id="bundle_integration_independent_blocked_only_1",
                    strategy_sleeve_id="sleeve_integration_independent_blocked_only_1",
                    strategy_execution_mode="independent_books",
                )
            )

            bundle = runtime.strategy_runtime_repo.get_execution_bundle(
                "bundle_integration_independent_blocked_only_1"
            )
            self.assertIsNotNone(bundle)
            assert bundle is not None
            self.assertEqual(bundle.status, "blocked")
            self.assertIn("strategy_bundle_blocked", bundle.reason_codes)
            self.assertNotIn("strategy_bundle_review_required", bundle.reason_codes)
        finally:
            await runtime.stop_background_tasks()


if __name__ == "__main__":
    unittest.main()
