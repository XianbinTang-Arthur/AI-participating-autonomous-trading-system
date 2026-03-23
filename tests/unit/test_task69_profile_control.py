from __future__ import annotations

from datetime import timedelta
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.strategy_profiles import StrategyProfileMarketRegimeAssessment, StrategyProfileRecommendation
from aats.services.operator.strategy_profiles import StrategyProfileControlService


class Task69ProfileControlTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
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
                "strategy_profile_auto_control_enabled": True,
            }
        )
        self.runtime = await build_runtime(settings)
        await self.runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=4,
            interval_seconds=0.0,
        )
        self.control = StrategyProfileControlService(self.runtime)

    async def test_replay_history_insufficient_is_neutral_and_does_not_prefer_safety_profile(self) -> None:
        result = await self.control.evaluate_now(allow_auto_activation=False)
        report = result["optimization_report"]
        candidates = {item["profile_id"]: item for item in report["candidates"]}

        self.assertEqual(report["control_summary"]["evidence"]["replay_validations"], 0)
        self.assertTrue(report["control_summary"]["evidence"]["cold_start_active"])
        self.assertEqual(candidates["execution_degraded_safe"]["offline_replay_score"], 0.0)
        self.assertEqual(candidates["trend_normal"]["offline_replay_score"], 0.0)
        self.assertFalse(candidates["execution_degraded_safe"]["selection_eligible"])
        self.assertIn(
            "strategy_profile_safety_profile_requires_explicit_trigger",
            candidates["execution_degraded_safe"]["selection_blocked_reasons"],
        )
        self.assertEqual(report["recommended_profile_id"], "trend_normal")

    async def test_activation_gate_blocks_non_safety_switch_during_cold_start(self) -> None:
        result = await self.control.evaluate_now(allow_auto_activation=False)
        report = self.control._latest_optimization_report()
        self.assertIsNotNone(report)
        recommendation = StrategyProfileRecommendation(
            product_type=self.runtime.settings.trading_product_type,
            margin_mode=self.runtime.settings.margin_mode,
            allowed_symbols=self.runtime.settings.allowed_symbols,
            active_profile_id="trend_normal",
            recommended_profile_id="trend_strict",
            confidence=0.95,
            market_regime_assessment=StrategyProfileMarketRegimeAssessment(
                regime="trend",
                volatility_state="low",
                execution_condition="normal",
            ),
            reason_codes=["test_candidate"],
            human_summary="test stricter trend candidate",
            risk_notes=[],
            valid_for_minutes=120,
            generated_by="test",
            input_digest="digest_task69",
            input_snapshot={"source": "test"},
            expires_at=utc_now() + timedelta(minutes=120),
        )

        validation = self.control._activation_gate_decision(
            recommendation=recommendation,
            optimization_report=report.model_copy(update={"recommended_profile_id": "trend_strict"}),
        )

        self.assertFalse(validation["auto_apply_allowed"])
        self.assertIn("strategy_profile_cold_start_lock_active", validation["blocked_reasons"])
        self.assertIn("strategy_profile_requires_more_realized_trades", validation["blocked_reasons"])
        self.assertIn("strategy_profile_requires_more_replay_validations", validation["blocked_reasons"])

    async def test_activation_gate_fast_tracks_emergency_safety_profile_during_cold_start(self) -> None:
        await self.control.evaluate_now(allow_auto_activation=False)
        report = self.control._latest_optimization_report()
        self.assertIsNotNone(report)
        recommendation = StrategyProfileRecommendation(
            product_type=self.runtime.settings.trading_product_type,
            margin_mode=self.runtime.settings.margin_mode,
            allowed_symbols=self.runtime.settings.allowed_symbols,
            active_profile_id="trend_normal",
            recommended_profile_id="execution_degraded_safe",
            confidence=0.70,
            market_regime_assessment=StrategyProfileMarketRegimeAssessment(
                regime="trend",
                volatility_state="high",
                execution_condition="degraded",
            ),
            reason_codes=["execution_errors_elevated"],
            human_summary="test emergency safety fast track",
            risk_notes=["fast_track"],
            valid_for_minutes=120,
            generated_by="test",
            input_digest="digest_task69_fast_track",
            input_snapshot={"execution_health": {"recent_execution_error_count": 4}},
            expires_at=utc_now() + timedelta(minutes=120),
        )
        with patch.object(
            self.control.context,
            "safety_state",
            return_value={
                "safe_to_trade": False,
                "review_required": True,
                "halted": False,
                "recovery_state": "only_reduce",
                "resume_blocked_reasons": [],
                "market_snapshot_fresh": True,
                "account_snapshot_fresh": True,
                "market_status": {},
                "account_status": {},
                "reconciliation_id": None,
                "reconciliation_severity": "unknown",
                "reconciliation_halt_required": False,
                "reconciliation_review_required": False,
                "auto_switch_frozen": False,
                "auto_switch_cooldown_active": False,
                "live_guard": {"status": "warning"},
                "trial_guard": {"status": "breached"},
                "only_reduce_required": True,
                "auto_halt_required": False,
                "trial_guard_breached": True,
            },
        ):
            validation = self.control._activation_gate_decision(
                recommendation=recommendation,
                optimization_report=report.model_copy(update={"recommended_profile_id": "execution_degraded_safe"}),
            )

        self.assertTrue(validation["auto_apply_allowed"])
        self.assertEqual(validation["transition_class"], "emergency_safety")
        self.assertTrue(validation["fast_track_eligible"])
        self.assertTrue(validation["fast_track_applied"])
        self.assertEqual(validation["blocked_reasons"], [])
        self.assertIn("runtime_not_safe_to_trade", validation["gating_state"]["fast_track_reasons"])

    async def test_seeded_profiles_now_manage_hold_and_cooldown_fields(self) -> None:
        self.control.ensure_seed_profiles()
        by_profile = {
            item.profile_id: item
            for item in self.runtime.strategy_profile_repo.list_revisions(
                product_type=self.runtime.settings.trading_product_type,
                margin_mode=self.runtime.settings.margin_mode,
            )
        }
        self.assertLess(
            by_profile["trend_aggressive"].payload.strategy_min_hold_seconds,
            by_profile["trend_normal"].payload.strategy_min_hold_seconds,
        )
        self.assertGreater(
            by_profile["execution_degraded_safe"].payload.strategy_post_close_cooldown_seconds,
            by_profile["trend_normal"].payload.strategy_post_close_cooldown_seconds,
        )
        self.assertGreater(
            by_profile["range_defensive"].payload.strategy_low_edge_cooldown_seconds,
            by_profile["trend_normal"].payload.strategy_low_edge_cooldown_seconds,
        )
