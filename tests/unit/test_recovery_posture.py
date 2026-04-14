from __future__ import annotations

import unittest
from types import SimpleNamespace

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.execution import OrderState
from aats.schemas.reconciliation import ReconciliationReport
from aats.schemas.strategy_runtime import StrategyExecutionBundle, StrategyLegIntent
from aats.schemas.system import RecoveryStatus
from aats.services.governance_engine.recovery_posture import RecoveryPostureEvaluator


class TestRecoveryPostureEvaluator(unittest.IsolatedAsyncioTestCase):
    async def test_paper_local_finalize_keeps_recovery_lightweight(self) -> None:
        runtime = await build_runtime(
            AATSSettings.model_validate(
                {
                    "mode": "paper_live",
                    "market_data_backend": "demo",
                    "execution_backend": "paper",
                    "account_backend": "disabled",
                    "account_read_enabled": False,
                    "storage_mode": "memory",
                    "event_persistence_mode": "strict",
                }
            )
        )
        evaluator = RecoveryPostureEvaluator(runtime)
        await runtime.market_gateway.run_local_publisher(
            symbol=runtime.settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )

        final = evaluator.finalize_status()

        self.assertEqual(final.recovery_state, "normal_operation")
        self.assertTrue(final.resume_eligible)
        self.assertTrue(final.safe_to_trade)
        self.assertFalse(final.rebaseline_available)
        self.assertEqual(final.resume_blocked_reasons, [])

    async def test_exchange_simulated_finalize_marks_review_required(self) -> None:
        runtime = await build_runtime(
            AATSSettings.model_validate(
                {
                    "config_profile": "guarded_simulated_submit_dry_run",
                    "mode": "guarded_live",
                    "market_data_backend": "demo",
                    "execution_backend": "okx",
                    "account_backend": "okx",
                    "account_read_enabled": True,
                    "okx_simulated_trading": True,
                    "live_submit_enabled": False,
                    "guarded_execution_dry_run": True,
                    "bootstrap_portfolio_from_exchange": True,
                    "storage_mode": "memory",
                    "event_persistence_mode": "strict",
                }
            )
        )
        evaluator = RecoveryPostureEvaluator(runtime)
        await runtime.market_gateway.run_local_publisher(
            symbol=runtime.settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        report = ReconciliationReport(
            reconciliation_id="recon_test",
            as_of_ts=utc_now(),
            exchange_comparison_enabled=True,
            order_diff={},
            fill_diff={},
            balance_diff={},
            position_diff={},
            mismatch_categories=["fills"],
            mismatch_reasons=["unexpected_on_exchange_fill"],
            safety_impacts=["operator_review_required"],
            severity="WARN",
            review_required=True,
            recommended_operator_action="rebaseline",
            halt_required=False,
        )
        base_status = RecoveryStatus(status="review", recovery_state="normal_operation")

        final = evaluator.finalize_status(base_status=base_status, latest_reconciliation=report)

        self.assertEqual(final.recovery_state, "review_required")
        self.assertFalse(final.safe_to_trade)
        self.assertFalse(final.resume_eligible)
        self.assertTrue(final.rebaseline_available)
        self.assertTrue(final.recovered_reconciliation_available)
        self.assertEqual(final.latest_reconciliation_id, "recon_test")
        self.assertEqual(final.latest_reconciliation_severity, "WARN")
        self.assertIn("operator_rebaseline_required", final.resume_blocked_reasons)

    async def test_manual_halt_stays_resume_eligible_when_no_other_blockers(self) -> None:
        runtime = await build_runtime(
            AATSSettings.model_validate(
                {
                    "mode": "paper_live",
                    "market_data_backend": "demo",
                    "execution_backend": "paper",
                    "account_backend": "disabled",
                    "account_read_enabled": False,
                    "storage_mode": "memory",
                    "event_persistence_mode": "strict",
                }
            )
        )
        await runtime.market_gateway.run_local_publisher(
            symbol=runtime.settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        runtime.kill_switch.halt(reason="operator_test_halt")
        evaluator = RecoveryPostureEvaluator(runtime)

        final = evaluator.finalize_status()

        self.assertEqual(final.recovery_state, "manually_halted")
        self.assertTrue(final.resume_eligible)
        self.assertFalse(final.safe_to_trade)
        self.assertEqual(final.resume_blocked_reasons, [])

    async def test_trial_guard_breach_blocks_resume_and_is_not_treated_as_manual_pause(self) -> None:
        runtime = await build_runtime(
            AATSSettings.model_validate(
                {
                    "mode": "paper_live",
                    "market_data_backend": "demo",
                    "execution_backend": "paper",
                    "account_backend": "disabled",
                    "account_read_enabled": False,
                    "storage_mode": "memory",
                    "event_persistence_mode": "strict",
                }
            )
        )
        await runtime.market_gateway.run_local_publisher(
            symbol=runtime.settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        runtime.kill_switch.halt(reason="trial_guard_threshold_breached")
        runtime.trial_guard_service.last_snapshot = {
            "enabled": True,
            "status": "breached",
            "summary": "试盘守护已触发自动停机。",
        }
        evaluator = RecoveryPostureEvaluator(runtime)

        final = evaluator.finalize_status()

        self.assertEqual(final.recovery_state, "resume_blocked")
        self.assertFalse(final.resume_eligible)
        self.assertFalse(final.safe_to_trade)
        self.assertIn("trial_guard_threshold_breached", final.resume_blocked_reasons)

    async def test_unknown_derivatives_position_requires_manual_review_even_when_only_reduce_is_active(self) -> None:
        runtime = await build_runtime(
            AATSSettings.model_validate(
                {
                    "mode": "paper_live",
                    "market_data_backend": "demo",
                    "execution_backend": "paper",
                    "account_backend": "disabled",
                    "account_read_enabled": False,
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "default_symbol": "BTC-USDT-SWAP",
                    "allowed_symbols": ("BTC-USDT-SWAP",),
                    "storage_mode": "memory",
                    "event_persistence_mode": "strict",
                }
            )
        )
        evaluator = RecoveryPostureEvaluator(runtime)
        await runtime.market_gateway.run_local_publisher(
            symbol=runtime.settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        report = ReconciliationReport(
            reconciliation_id="recon_only_reduce",
            as_of_ts=utc_now(),
            product_type="derivatives",
            margin_mode="cross",
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
            severity="REVIEW_REQUIRED",
            review_required=True,
            resume_blocking=True,
            only_reduce_required=True,
            only_reduce_reasons=["derivatives_exchange_position_without_local_execution_chain"],
            recovery_classification="manual_review_required",
            recommended_operator_action="go_close_position_on_exchange",
        )
        base_status = RecoveryStatus(status="recovered", recovery_state="normal_operation")

        final = evaluator.finalize_status(base_status=base_status, latest_reconciliation=report)

        self.assertEqual(final.recovery_state, "review_required")
        self.assertFalse(final.resume_eligible)
        self.assertFalse(final.safe_to_trade)
        self.assertTrue(final.review_required)
        self.assertTrue(final.only_reduce_required)
        self.assertIn("derivatives_exchange_position_without_local_execution_chain", final.only_reduce_reasons)
        self.assertIn("operator_rebaseline_required", final.resume_blocked_reasons)

    async def test_derivatives_only_reduce_recovery_blocks_resume_even_without_manual_review(self) -> None:
        runtime = await build_runtime(
            AATSSettings.model_validate(
                {
                    "mode": "paper_live",
                    "market_data_backend": "demo",
                    "execution_backend": "paper",
                    "account_backend": "disabled",
                    "account_read_enabled": False,
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "default_symbol": "BTC-USDT-SWAP",
                    "allowed_symbols": ("BTC-USDT-SWAP",),
                    "storage_mode": "memory",
                    "event_persistence_mode": "strict",
                }
            )
        )
        evaluator = RecoveryPostureEvaluator(runtime)
        await runtime.market_gateway.run_local_publisher(
            symbol=runtime.settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        report = ReconciliationReport(
            reconciliation_id="recon_margin_only_reduce",
            as_of_ts=utc_now(),
            product_type="derivatives",
            margin_mode="cross",
            exchange_comparison_enabled=True,
            order_diff={"reconstructed": {}, "exchange": {}},
            fill_diff={"replayed": {}, "exchange": {}},
            balance_diff={"reconstructed": {}, "exchange": {}},
            position_diff={"stored": {}, "reconstructed": {}, "reconstructed_mismatches": {}, "exchange": {}, "exchange_mismatches": {}},
            mismatch_categories=["derivatives_runtime_margin_guard"],
            mismatch_reasons=["derivatives_margin_usage_requires_only_reduce"],
            safety_impacts=["derivatives_only_reduce_until_position_reconciled"],
            severity="SOFT_MISMATCH",
            only_reduce_required=True,
            only_reduce_reasons=["derivatives_margin_usage_requires_only_reduce"],
            recovery_classification="derivatives_only_reduce",
            recommended_operator_action="go_close_position_on_exchange",
        )
        base_status = RecoveryStatus(status="recovered", recovery_state="normal_operation")

        final = evaluator.finalize_status(base_status=base_status, latest_reconciliation=report)

        self.assertEqual(final.recovery_state, "only_reduce")
        self.assertFalse(final.resume_eligible)
        self.assertFalse(final.safe_to_trade)
        self.assertFalse(final.review_required)
        self.assertTrue(final.only_reduce_required)
        self.assertIn("derivatives_margin_usage_requires_only_reduce", final.resume_blocked_reasons)

    async def test_clean_reconciliation_clears_lingering_review_and_only_reduce_flags(self) -> None:
        runtime = await build_runtime(
            AATSSettings.model_validate(
                {
                    "mode": "paper_live",
                    "market_data_backend": "demo",
                    "execution_backend": "paper",
                    "account_backend": "disabled",
                    "account_read_enabled": False,
                    "storage_mode": "memory",
                    "event_persistence_mode": "strict",
                }
            )
        )
        evaluator = RecoveryPostureEvaluator(runtime)
        await runtime.market_gateway.run_local_publisher(
            symbol=runtime.settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        report = ReconciliationReport(
            reconciliation_id="recon_clean_after_review",
            as_of_ts=utc_now(),
            exchange_comparison_enabled=True,
            order_diff={},
            fill_diff={},
            balance_diff={},
            position_diff={},
            mismatch_categories=[],
            mismatch_reasons=[],
            safety_impacts=[],
            severity="CLEAN",
            review_required=False,
            halt_required=False,
            only_reduce_required=False,
            only_reduce_reasons=[],
            recovery_classification="clean",
            recommended_operator_action="none",
        )
        base_status = RecoveryStatus(
            status="review",
            recovery_state="review_required",
            review_required=True,
            only_reduce_required=True,
            only_reduce_reasons=["stale_only_reduce"],
            resume_blocked_reasons=["operator_rebaseline_required"],
        )

        final = evaluator.finalize_status(base_status=base_status, latest_reconciliation=report)

        self.assertEqual(final.recovery_state, "normal_operation")
        self.assertTrue(final.safe_to_trade)
        self.assertTrue(final.resume_eligible)
        self.assertFalse(final.review_required)
        self.assertFalse(final.only_reduce_required)
        self.assertEqual(final.only_reduce_reasons, [])
        self.assertEqual(final.resume_blocked_reasons, [])

    async def test_finalize_status_tracks_dynamic_bundle_recovery_and_clears_after_orders_close(self) -> None:
        runtime = await build_runtime(
            AATSSettings.model_validate(
                {
                    "mode": "paper_live",
                    "market_data_backend": "demo",
                    "execution_backend": "paper",
                    "account_backend": "disabled",
                    "account_read_enabled": False,
                    "storage_mode": "memory",
                    "event_persistence_mode": "strict",
                }
            )
        )
        evaluator = RecoveryPostureEvaluator(runtime)
        await runtime.market_gateway.run_local_publisher(
            symbol=runtime.settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        now = utc_now()
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_bundle_runtime_1",
                intent_id="intent_bundle_runtime_1",
                symbol=runtime.settings.default_symbol,
                client_order_id="cl_bundle_runtime_1",
                venue="OKX",
                exchange_order_id="ord_bundle_runtime_1",
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
                strategy_sleeve_id="sleeve_runtime_grid",
                allocation_id="alloc_runtime_bundle",
                strategy_bundle_id="bundle_runtime_recovery",
                strategy_leg_role="inventory",
                submission_payload={},
            )
        )

        pending = evaluator.finalize_status()

        self.assertEqual(pending.recovery_state, "bundle_recovery")
        self.assertFalse(pending.safe_to_trade)
        self.assertFalse(pending.resume_eligible)
        self.assertTrue(pending.bundle_recovery_required)
        self.assertIn("strategy_bundle_recovery_in_progress", pending.resume_blocked_reasons)

        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_bundle_runtime_1",
                intent_id="intent_bundle_runtime_1",
                symbol=runtime.settings.default_symbol,
                client_order_id="cl_bundle_runtime_1",
                venue="OKX",
                exchange_order_id="ord_bundle_runtime_1",
                status="FILLED",
                submission_mode="guarded_live_submit",
                submitted_ts=now,
                last_update_ts=utc_now(),
                requested_qty=0.001,
                filled_qty=0.001,
                remaining_qty=0.0,
                average_fill_price=60_000.0,
                fees=0.0,
                product_type="spot",
                margin_mode="cash",
                strategy_family="spot_grid",
                strategy_sleeve_id="sleeve_runtime_grid",
                allocation_id="alloc_runtime_bundle",
                strategy_bundle_id="bundle_runtime_recovery",
                strategy_leg_role="inventory",
                submission_payload={},
            )
        )

        cleared = evaluator.finalize_status()

        self.assertEqual(cleared.recovery_state, "normal_operation")
        self.assertTrue(cleared.safe_to_trade)
        self.assertTrue(cleared.resume_eligible)
        self.assertFalse(cleared.bundle_recovery_required)
        self.assertEqual(cleared.bundle_summaries, [])

    async def test_finalize_status_respects_persisted_review_required_overlay_bundle(self) -> None:
        runtime = await build_runtime(
            AATSSettings.model_validate(
                {
                    "mode": "paper_live",
                    "market_data_backend": "demo",
                    "execution_backend": "paper",
                    "account_backend": "disabled",
                    "account_read_enabled": False,
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "default_symbol": "BTC-USDT-SWAP",
                    "allowed_symbols": ("BTC-USDT-SWAP",),
                    "storage_mode": "memory",
                    "event_persistence_mode": "strict",
                }
            )
        )
        evaluator = RecoveryPostureEvaluator(runtime)
        await runtime.market_gateway.run_local_publisher(
            symbol=runtime.settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        runtime.strategy_runtime_repo.save_execution_bundle(
            StrategyExecutionBundle(
                bundle_id="bundle_overlay_review",
                decision_id="decision_overlay_review",
                family="directional",
                participating_families=["directional"],
                strategy_sleeve_refs=["sleeve_independent_long", "sleeve_independent_short"],
                allocation_id="alloc_overlay_review",
                product_type="derivatives",
                margin_mode="cross",
                allowed_symbols=(runtime.settings.default_symbol,),
                route_action="override_target",
                bundle_type="hedge_protected",
                status="review_required",
                selected_symbol=runtime.settings.default_symbol,
                operator_summary="overlay bundle mixed terminal outcome",
                reason_codes=["strategy_bundle_review_required"],
                legs=[
                    StrategyLegIntent(
                        symbol=runtime.settings.default_symbol,
                        product_type="derivatives",
                        side="buy",
                        position_mode="long_short_mode",
                        pos_side="long",
                        action="open",
                        family="directional",
                        role="primary",
                        strategy_sleeve_id="sleeve_independent_long",
                        allocation_id="alloc_overlay_review",
                        margin_mode="cross",
                    ),
                    StrategyLegIntent(
                        symbol=runtime.settings.default_symbol,
                        product_type="derivatives",
                        side="sell",
                        position_mode="long_short_mode",
                        pos_side="short",
                        action="open",
                        family="directional",
                        role="primary",
                        strategy_sleeve_id="sleeve_independent_short",
                        allocation_id="alloc_overlay_review",
                        margin_mode="cross",
                    ),
                ],
            )
        )

        final = evaluator.finalize_status()

        self.assertEqual(final.recovery_state, "review_required")
        self.assertTrue(final.review_required)
        self.assertFalse(final.resume_eligible)
        self.assertFalse(final.safe_to_trade)
        self.assertTrue(final.bundle_recovery_required)
        self.assertEqual(len(final.bundle_summaries), 1)
        self.assertEqual(final.bundle_summaries[0].recovery_state, "review_required")
        self.assertIn("strategy_bundle_recovery_requires_review", final.resume_blocked_reasons)


class TestTrialGuardBreachedCrossProcessFallback(unittest.TestCase):
    """P0 修复：gateway 进程 trial_guard_service 为 None 时通过 kill switch
    reason 检测 execution 进程设置的 trial guard breach。

    问题：execution 进程检测到 trial guard breach → halt kill switch with
    reason="trial_guard_threshold_breached"。gateway 进程的 _trial_guard_breached()
    只检查本地 trial_guard_service.snapshot()，而 gateway 的
    trial_guard_enabled=False → service 为 None → 返回 False → blocker 不出现
    → "人工重置试盘守护" 按钮不渲染。

    修复后：_trial_guard_breached() 在本地 service 不可用时回退检查 kill switch
    的 reason 字段。
    """

    def _make_evaluator(
        self,
        *,
        trial_guard_service=None,
        kill_switch_halted: bool = False,
        kill_switch_reason: str | None = None,
    ) -> RecoveryPostureEvaluator:
        evaluator = RecoveryPostureEvaluator.__new__(RecoveryPostureEvaluator)
        ks = SimpleNamespace(
            halted=kill_switch_halted,
            status=lambda: {"halted": kill_switch_halted, "reason": kill_switch_reason},
        )
        evaluator.runtime = SimpleNamespace(
            trial_guard_service=trial_guard_service,
            kill_switch=ks,
        )
        return evaluator

    def test_returns_true_when_local_service_reports_breached(self) -> None:
        """Primary path: trial_guard_service available and reports breached."""
        service = SimpleNamespace(
            snapshot=lambda: {"enabled": True, "status": "breached"},
        )
        evaluator = self._make_evaluator(
            trial_guard_service=service,
            kill_switch_halted=True,
            kill_switch_reason="trial_guard_threshold_breached",
        )
        self.assertTrue(evaluator._trial_guard_breached())

    def test_returns_true_via_kill_switch_fallback_when_service_is_none(self) -> None:
        """Cross-process fallback: gateway has no trial_guard_service,
        but kill switch was halted by trial guard on execution process."""
        evaluator = self._make_evaluator(
            trial_guard_service=None,
            kill_switch_halted=True,
            kill_switch_reason="trial_guard_threshold_breached",
        )
        self.assertTrue(evaluator._trial_guard_breached())

    def test_returns_false_when_kill_switch_halted_with_different_reason(self) -> None:
        """Kill switch halted for a different reason (e.g., operator manual halt)
        should NOT be treated as trial guard breach."""
        evaluator = self._make_evaluator(
            trial_guard_service=None,
            kill_switch_halted=True,
            kill_switch_reason="operator_test_halt",
        )
        self.assertFalse(evaluator._trial_guard_breached())

    def test_returns_false_when_kill_switch_not_halted(self) -> None:
        """Neither local service nor kill switch indicates breach."""
        evaluator = self._make_evaluator(
            trial_guard_service=None,
            kill_switch_halted=False,
            kill_switch_reason=None,
        )
        self.assertFalse(evaluator._trial_guard_breached())

    def test_returns_false_after_reset_even_though_ks_still_halted(self) -> None:
        """关键场景：trial guard 已人工重置（status=warming_up），
        但 kill switch 仍然 halted with reason=trial_guard_threshold_breached
        （reset 不 resume kill switch，这是设计意图）。

        此时 _trial_guard_breached() 必须返回 False，否则 blocker 永远不消失，
        用户无法继续点"恢复自动运行"。路径 A（本地 service 可用）必须
        是排他的，不能 fall-through 到路径 B。"""
        service = SimpleNamespace(
            snapshot=lambda: {"enabled": True, "status": "warming_up"},
        )
        evaluator = self._make_evaluator(
            trial_guard_service=service,
            kill_switch_halted=True,
            kill_switch_reason="trial_guard_threshold_breached",
        )
        self.assertFalse(evaluator._trial_guard_breached())

    def test_returns_false_when_service_monitoring_and_ks_not_halted(self) -> None:
        """Local service exists but reports monitoring (not breached),
        and kill switch is not halted."""
        service = SimpleNamespace(
            snapshot=lambda: {"enabled": True, "status": "monitoring"},
        )
        evaluator = self._make_evaluator(
            trial_guard_service=service,
            kill_switch_halted=False,
            kill_switch_reason=None,
        )
        self.assertFalse(evaluator._trial_guard_breached())


class TestAiRequiresManualReviewNoneGuard(unittest.TestCase):
    """Stage 7 修复：gateway/market/execution role 下 runtime.ai_service is None。

    原版 _ai_requires_manual_review() 直接 self.runtime.ai_service.status() 触发
    AttributeError，向上传播到 finalize_status → recovery_view → build_system_health，
    让 /system/health 在 gateway 进程返回 500，整个 UI dashboard 无法加载。

    修复后：ai_service is None 时直接 return False（此进程对 AI 状态没有可见性，
    不应在 recovery 链里报告 ai_degraded_requires_manual_review）。
    """

    def test_returns_false_when_ai_service_is_none(self) -> None:
        evaluator = RecoveryPostureEvaluator.__new__(RecoveryPostureEvaluator)
        evaluator.runtime = SimpleNamespace(
            settings=SimpleNamespace(ai_operating_mode="ai_decision_maker"),
            ai_service=None,
        )

        self.assertFalse(evaluator._ai_requires_manual_review())

    def test_baseline_only_short_circuits_before_ai_service_check(self) -> None:
        """baseline_only 模式下哪怕 ai_service 不为 None 也不应触发 AI 复核检查。
        这条 fast-path 在我们的修复前后都应该保持。"""
        evaluator = RecoveryPostureEvaluator.__new__(RecoveryPostureEvaluator)
        evaluator.runtime = SimpleNamespace(
            settings=SimpleNamespace(ai_operating_mode="baseline_only"),
            ai_service=SimpleNamespace(status=lambda: {"degraded": True}),
        )

        self.assertFalse(evaluator._ai_requires_manual_review())

    def test_returns_true_when_ai_service_present_and_degraded(self) -> None:
        evaluator = RecoveryPostureEvaluator.__new__(RecoveryPostureEvaluator)
        evaluator.runtime = SimpleNamespace(
            settings=SimpleNamespace(ai_operating_mode="ai_decision_maker"),
            ai_service=SimpleNamespace(
                status=lambda: {
                    "degraded": True,
                    "auto_downgrade_active": False,
                    "manual_override_mode": None,
                }
            ),
        )

        self.assertTrue(evaluator._ai_requires_manual_review())

    def test_returns_false_when_ai_service_present_but_auto_downgraded(self) -> None:
        evaluator = RecoveryPostureEvaluator.__new__(RecoveryPostureEvaluator)
        evaluator.runtime = SimpleNamespace(
            settings=SimpleNamespace(ai_operating_mode="ai_decision_maker"),
            ai_service=SimpleNamespace(
                status=lambda: {
                    "degraded": True,
                    "auto_downgrade_active": True,
                    "manual_override_mode": None,
                }
            ),
        )

        self.assertFalse(evaluator._ai_requires_manual_review())


if __name__ == "__main__":
    unittest.main()
