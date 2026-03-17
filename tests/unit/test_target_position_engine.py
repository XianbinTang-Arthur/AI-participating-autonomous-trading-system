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

    def test_long_only_spot_holds_existing_long_when_signal_flips_short(self) -> None:
        engine = TargetPositionEngine(settings=AATSSettings.model_validate({"default_order_qty": 0.001}))
        context = self._context(current_position_qty=0.00035, current_exposure_side="long")
        baseline = self._baseline(
            volatility_target_scale=1.0,
            suggested_position_scale=1.0,
            direction_bias="short",
        )

        target = engine.build(context, baseline, self._ai_assessment(direction=-0.4))

        self.assertAlmostEqual(target.target_position_qty, context.current_position_qty)
        self.assertEqual(target.delta_position_qty, 0.0)
        self.assertEqual(target.position_intent, "hold")

    def test_long_only_spot_decays_existing_long_on_flat_signal(self) -> None:
        engine = TargetPositionEngine(settings=AATSSettings.model_validate({"default_order_qty": 0.001}))
        context = self._context(current_position_qty=0.00035, current_exposure_side="long")
        baseline = self._baseline(
            volatility_target_scale=1.0,
            suggested_position_scale=0.2,
            direction_bias="flat",
        )

        target = engine.build(context, baseline, self._ai_assessment(direction=0.0))

        self.assertGreater(target.target_position_qty, 0.0)
        self.assertLess(target.target_position_qty, context.current_position_qty)
        self.assertEqual(target.position_intent, "reduce_long")

    def test_long_only_spot_flat_signal_cleans_small_residual_position(self) -> None:
        engine = TargetPositionEngine(settings=AATSSettings.model_validate({"default_order_qty": 0.001}))
        context = self._context(current_position_qty=0.000083696216, current_exposure_side="long")
        baseline = self._baseline(
            volatility_target_scale=1.0,
            suggested_position_scale=0.15,
            direction_bias="flat",
        )

        target = engine.build(context, baseline, self._ai_assessment(direction=0.0))

        self.assertAlmostEqual(target.target_position_qty, 0.0)
        self.assertAlmostEqual(target.delta_position_qty, -context.current_position_qty)
        self.assertEqual(target.position_intent, "close_long")

    def test_derivatives_profile_allows_short_targets_and_sets_reversal_intent(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.001,
                    "trading_product_type": "derivatives",
                    "max_target_leverage": 3.0,
                    "default_target_leverage": 2.0,
                    "strategy_short_bias_enabled": True,
                    "strategy_dynamic_leverage_enabled": True,
                }
            )
        )
        context = self._context(current_position_qty=0.0008, product_type="derivatives", current_exposure_side="long")
        baseline = self._baseline(
            volatility_target_scale=1.0,
            suggested_position_scale=1.0,
            direction_bias="short",
            confidence=0.92,
        )

        target = engine.build(context, baseline, self._ai_assessment(direction=-0.55, confidence=0.9))

        self.assertLess(target.target_position_qty, 0.0)
        self.assertEqual(target.position_intent, "reverse_to_short")
        self.assertEqual(target.target_exposure_side, "short")
        self.assertEqual(target.product_type, "derivatives")
        self.assertGreaterEqual(target.target_leverage, 1.0)
        self.assertLessEqual(target.target_leverage, 3.0)

    def test_spot_context_stays_long_only_even_if_settings_default_to_derivatives(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": True,
                }
            )
        )
        context = self._context(product_type="spot", current_exposure_side="flat")
        baseline = self._baseline(
            volatility_target_scale=1.0,
            suggested_position_scale=1.0,
            direction_bias="short",
            confidence=0.9,
        )

        target = engine.build(context, baseline, self._ai_assessment(direction=-0.5, confidence=0.8))

        self.assertEqual(target.product_type, "spot")
        self.assertEqual(target.target_position_qty, 0.0)
        self.assertEqual(target.position_intent, "hold")

    def test_derivatives_reduces_before_reversing_on_weak_opposite_signal(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": True,
                }
            )
        )
        context = self._context(current_position_qty=0.05, product_type="derivatives", current_exposure_side="long")
        baseline = self._baseline(
            volatility_target_scale=1.0,
            suggested_position_scale=0.2,
            direction_bias="short",
            confidence=0.7,
        )

        target = engine.build(context, baseline, self._ai_assessment(direction=-0.18, confidence=0.6))

        self.assertGreater(target.target_position_qty, 0.0)
        self.assertLess(target.target_position_qty, context.current_position_qty)
        self.assertEqual(target.position_intent, "reduce_long")

    def test_derivatives_leverage_reduces_when_microstructure_conflicts(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "default_order_qty": 0.01,
                "trading_product_type": "derivatives",
                "max_target_leverage": 4.0,
                "default_target_leverage": 2.5,
                "strategy_short_bias_enabled": True,
                "strategy_dynamic_leverage_enabled": True,
            }
        )
        engine = TargetPositionEngine(settings=settings)
        context = self._context(product_type="derivatives", current_exposure_side="flat")
        supportive = self._baseline(
            volatility_target_scale=1.0,
            suggested_position_scale=1.0,
            direction_bias="long",
            confidence=0.92,
            factor_scores={"momentum_alpha": 0.4, "microstructure_alpha": 0.18, "liquidity_scale": 0.95},
        )
        conflicting = self._baseline(
            volatility_target_scale=1.0,
            suggested_position_scale=1.0,
            direction_bias="long",
            confidence=0.92,
            volatility_state="high",
            factor_scores={"momentum_alpha": 0.4, "microstructure_alpha": -0.18, "liquidity_scale": 0.6},
        )

        supportive_target = engine.build(context, supportive, self._ai_assessment(direction=0.5, confidence=0.9))
        conflicting_target = engine.build(context, conflicting, self._ai_assessment(direction=0.5, confidence=0.9))

        self.assertGreater(supportive_target.target_leverage, conflicting_target.target_leverage)

    @staticmethod
    def _context(
        *,
        current_position_qty: float = 0.0,
        product_type: str = "spot",
        current_exposure_side: str = "flat",
    ) -> DecisionContext:
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
            product_type=product_type,  # type: ignore[arg-type]
            current_exposure_side=current_exposure_side,  # type: ignore[arg-type]
            current_target_leverage=1.0,
        )

    @staticmethod
    def _baseline(
        *,
        volatility_target_scale: float,
        suggested_position_scale: float,
        direction_bias: str = "long",
        confidence: float = 0.8,
        volatility_state: str = "medium",
        factor_scores: dict[str, float] | None = None,
    ) -> BaselineAssessment:
        return BaselineAssessment(
            decision_id="decision_target_test",
            symbol="BTC-USDT",
            regime="trend",
            direction_bias=direction_bias,  # type: ignore[arg-type]
            trend_strength=0.7,
            volatility_state=volatility_state,
            confidence=confidence,
            composite_alpha_score=0.45,
            suggested_position_scale=suggested_position_scale,
            volatility_target_scale=volatility_target_scale,
            factor_scores=factor_scores or {"momentum_alpha": 0.4},
            holding_horizon="15m",
            invalidation_conditions=[],
            reason_codes=["test"],
            engine_version="test",
        )

    @staticmethod
    def _ai_assessment(*, direction: float = 0.1, confidence: float = 0.7) -> AIMarketAssessment:
        return AIMarketAssessment(
            decision_id="decision_target_test",
            symbol="BTC-USDT",
            regime="trend",
            directional_edge=direction,
            expected_volatility=0.02,
            confidence=confidence,
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
            calibrated_confidence=confidence,
            model_name="none",
            model_version="1",
            prompt_version="1",
        )


if __name__ == "__main__":
    unittest.main()
