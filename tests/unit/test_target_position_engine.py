from __future__ import annotations

import unittest

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.decision import AIMarketAssessment, BaselineAssessment, DecisionContext
from aats.services.decision_engine.target_position import TargetPositionEngine


class TestTargetPositionEngine(unittest.TestCase):
    def test_volatility_targeting_and_conviction_scale_reduce_target_size(self) -> None:
        engine = TargetPositionEngine(settings=AATSSettings.model_validate({"default_order_qty": 0.001}))

        conservative_target = engine.build(
            self._context(),
            self._baseline(volatility_target_scale=0.6, suggested_position_scale=0.35),
            self._ai_assessment(),
        )
        aggressive_target = engine.build(
            self._context(),
            self._baseline(volatility_target_scale=1.0, suggested_position_scale=0.9),
            self._ai_assessment(),
        )

        self.assertGreater(abs(aggressive_target.target_position_qty), abs(conservative_target.target_position_qty))
        self.assertGreater(conservative_target.target_position_qty, 0.0)

    def test_rebalance_band_keeps_existing_position_when_delta_is_tiny(self) -> None:
        engine = TargetPositionEngine(settings=AATSSettings.model_validate({"default_order_qty": 0.001}))
        context = self._context(current_position_qty=0.00039)
        baseline = self._baseline(volatility_target_scale=1.0, suggested_position_scale=0.4)

        target = engine.build(context, baseline, self._ai_assessment())

        self.assertAlmostEqual(target.target_position_qty, context.current_position_qty)
        self.assertAlmostEqual(target.delta_position_qty, 0.0)

    def test_same_direction_scale_in_is_staged(self) -> None:
        engine = TargetPositionEngine(settings=AATSSettings.model_validate({"default_order_qty": 0.001}))
        context = self._context(current_position_qty=0.0002)
        baseline = self._baseline(volatility_target_scale=1.0, suggested_position_scale=1.0)

        target = engine.build(context, baseline, self._ai_assessment())

        self.assertGreater(target.target_position_qty, context.current_position_qty)
        self.assertLess(target.target_position_qty, 0.001)

    @staticmethod
    def _context(*, current_position_qty: float = 0.0) -> DecisionContext:
        return DecisionContext(
            decision_id="decision_target_test",
            symbol="BTC-USDT",
            timeframe="15m",
            as_of_ts=utc_now(),
            market_snapshot_ref="evt_market",
            feature_snapshot_ref="evt_feature",
            portfolio_snapshot_ref="evt_portfolio",
            health_snapshot_ref="evt_health",
            mode="paper_live",
            current_position_qty=current_position_qty,
        )

    @staticmethod
    def _baseline(*, volatility_target_scale: float, suggested_position_scale: float) -> BaselineAssessment:
        return BaselineAssessment(
            decision_id="decision_target_test",
            symbol="BTC-USDT",
            regime="trend",
            direction_bias="long",
            trend_strength=0.7,
            volatility_state="medium",
            confidence=0.8,
            composite_alpha_score=0.45,
            suggested_position_scale=suggested_position_scale,
            volatility_target_scale=volatility_target_scale,
            factor_scores={"momentum_alpha": 0.4},
            holding_horizon="15m",
            invalidation_conditions=[],
            reason_codes=["test"],
            engine_version="test",
        )

    @staticmethod
    def _ai_assessment() -> AIMarketAssessment:
        return AIMarketAssessment(
            decision_id="decision_target_test",
            symbol="BTC-USDT",
            regime="trend",
            directional_edge=0.1,
            expected_volatility=0.02,
            confidence=0.7,
            uncertainty=0.2,
            expected_holding_horizon="15m",
            invalidation_conditions=[],
            risk_tags=[],
            rationale_summary="test",
            operating_mode="baseline_only",
            provider_name="baseline_fallback",
            output_valid=True,
            fallback_used=True,
            fallback_reason="baseline_only_mode",
            degraded=False,
            calibrated_confidence=0.0,
            model_name="none",
            model_version="1",
            prompt_version="1",
        )


if __name__ == "__main__":
    unittest.main()
