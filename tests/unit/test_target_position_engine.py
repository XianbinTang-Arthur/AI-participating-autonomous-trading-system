from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.decision import AIMarketAssessment, BaselineAssessment, DecisionContext, ProfileControlDecision
from aats.services.decision_engine.target_position import TargetPositionEngine


class _FixedFeeResolver:
    def __init__(self, fee_bps: float) -> None:
        self.fee_bps = fee_bps

    def taker_fee_bps(self, *, symbol: str | None = None) -> float:
        _ = symbol
        return self.fee_bps

    def estimated_execution_fee_bps(self, *, symbol: str | None = None, **kwargs) -> float:
        _ = symbol
        _ = kwargs
        return self.fee_bps

    def funding_fee_bps(self, *, symbol: str | None = None) -> float:
        _ = symbol
        return 0.0


class _FundingAwareFeeResolver(_FixedFeeResolver):
    def __init__(self, fee_bps: float, funding_bps: float) -> None:
        super().__init__(fee_bps)
        self.funding_bps = funding_bps

    def funding_fee_bps(self, *, symbol: str | None = None) -> float:
        _ = symbol
        return self.funding_bps


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

    def test_baseline_target_qty_does_not_reapply_volatility_scale(self) -> None:
        engine = TargetPositionEngine(settings=AATSSettings.model_validate({"default_order_qty": 0.001}))

        low_vol_target = engine.build(
            self._context(),
            self._baseline(volatility_target_scale=0.6, suggested_position_scale=0.35),
            self._ai_assessment(),
        )
        high_vol_target = engine.build(
            self._context(),
            self._baseline(volatility_target_scale=1.0, suggested_position_scale=0.35),
            self._ai_assessment(),
        )

        self.assertEqual(low_vol_target.target_position_qty, high_vol_target.target_position_qty)

    def test_rebalance_band_keeps_existing_position_when_delta_is_tiny(self) -> None:
        engine = TargetPositionEngine(settings=AATSSettings.model_validate({"default_order_qty": 0.001}))
        context = self._context(current_position_qty=0.00039)
        baseline = self._baseline(volatility_target_scale=1.0, suggested_position_scale=0.4)

        target = engine.build(context, baseline, self._ai_assessment())

        self.assertEqual(target.target_position_qty, context.current_position_qty)
        self.assertEqual(target.delta_position_qty, Decimal("0"))

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

        self.assertEqual(target.target_position_qty, context.current_position_qty)
        self.assertEqual(target.delta_position_qty, Decimal("0"))
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

        self.assertEqual(target.target_position_qty, Decimal("0"))
        self.assertEqual(target.delta_position_qty, -context.current_position_qty)
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

    def test_derivatives_short_bias_setting_disables_short_targets(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": False,
                }
            )
        )
        context = self._context(product_type="derivatives", current_exposure_side="flat")
        baseline = self._baseline(
            volatility_target_scale=1.0,
            suggested_position_scale=1.0,
            direction_bias="short",
            confidence=0.92,
        )

        target = engine.build(context, baseline, self._ai_assessment(direction=-0.55, confidence=0.9))

        self.assertEqual(target.target_position_qty, Decimal("0"))
        self.assertEqual(target.position_intent, "hold")
        self.assertIn("short_bias_disabled", target.guardrail_flags)

    def test_derivatives_short_entry_uses_independent_short_thresholds(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": True,
                    "strategy_cost_guard_enabled": False,
                    "strategy_entry_min_signal_edge_bps": 30.0,
                    "strategy_entry_alpha_min": 0.30,
                    "strategy_entry_confidence_min": 0.80,
                    "strategy_short_entry_min_signal_edge_bps": 12.0,
                    "strategy_short_entry_alpha_min": 0.18,
                    "strategy_short_entry_confidence_min": 0.58,
                }
            )
        )
        context = self._context(product_type="derivatives", current_exposure_side="flat")
        baseline = self._baseline(
            volatility_target_scale=1.0,
            suggested_position_scale=1.0,
            direction_bias="short",
            confidence=0.60,
        ).model_copy(update={"composite_alpha_score": -0.22})

        target = engine.build(context, baseline, self._ai_assessment(direction=-0.22, confidence=0.60))

        self.assertEqual(target.target_position_qty, Decimal("-0.01"))
        self.assertEqual(target.position_intent, "open_short")
        self.assertNotIn("short_entry_alpha_below_threshold", target.guardrail_flags)

    def test_derivatives_short_reversal_uses_independent_short_thresholds(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.10,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": True,
                    "strategy_cost_guard_enabled": False,
                    "strategy_reversal_min_signal_edge_bps": 30.0,
                    "strategy_reversal_alpha_min": 0.30,
                    "strategy_reversal_confidence_min": 0.80,
                    "strategy_short_reversal_min_signal_edge_bps": 18.0,
                    "strategy_short_reversal_alpha_min": 0.20,
                    "strategy_short_reversal_confidence_min": 0.60,
                    "strategy_edge_noise_buffer_bps": 0.0,
                }
            )
        )
        context = self._context(current_position_qty=0.05, product_type="derivatives", current_exposure_side="long")
        baseline = self._baseline(
            volatility_target_scale=1.0,
            suggested_position_scale=1.0,
            direction_bias="short",
            confidence=0.61,
        ).model_copy(update={"composite_alpha_score": -0.24})

        target = engine.build(context, baseline, self._ai_assessment(direction=-0.24, confidence=0.61))

        self.assertLess(target.target_position_qty, Decimal("0"))
        self.assertEqual(target.position_intent, "reverse_to_short")
        self.assertNotIn("short_reversal_alpha_below_threshold", target.guardrail_flags)

    def test_derivatives_bearish_signal_reports_short_reversal_blocker_when_below_short_threshold(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.10,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": True,
                    "strategy_cost_guard_enabled": False,
                    "strategy_short_reversal_min_signal_edge_bps": 18.0,
                    "strategy_short_reversal_alpha_min": 0.20,
                    "strategy_short_reversal_confidence_min": 0.66,
                    "strategy_edge_noise_buffer_bps": 0.0,
                }
            )
        )
        context = self._context(current_position_qty=0.05, product_type="derivatives", current_exposure_side="long")
        baseline = self._baseline(
            volatility_target_scale=1.0,
            suggested_position_scale=1.0,
            direction_bias="short",
            confidence=0.60,
        ).model_copy(update={"composite_alpha_score": -0.24})

        target = engine.build(context, baseline, self._ai_assessment(direction=-0.24, confidence=0.60))

        self.assertGreater(target.target_position_qty, Decimal("0"))
        self.assertEqual(target.position_intent, "hold")
        self.assertIn("short_reversal_confidence_below_threshold", target.guardrail_flags)

    def test_derivatives_flat_entry_is_raised_to_min_actionable_qty(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": True,
                }
            )
        )
        context = self._context(product_type="derivatives", current_exposure_side="flat")
        baseline = self._baseline(
            volatility_target_scale=1.0,
            suggested_position_scale=0.2,
            direction_bias="short",
            confidence=0.92,
        )

        target = engine.build(context, baseline, self._ai_assessment(direction=-0.55, confidence=0.9))

        self.assertEqual(target.target_position_qty, Decimal("-0.01"))
        self.assertEqual(target.position_intent, "open_short")

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

    def test_derivatives_flat_signal_holds_existing_position_without_explicit_exit(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": True,
                    "strategy_flat_signal_hold_enabled": True,
                }
            )
        )
        context = self._context(current_position_qty=0.028, product_type="derivatives", current_exposure_side="long")
        baseline = self._baseline(
            volatility_target_scale=1.0,
            suggested_position_scale=0.2,
            direction_bias="flat",
            confidence=0.52,
            factor_scores={
                "momentum_alpha": 0.08,
                "trend_alpha": 0.05,
                "microstructure_alpha": -0.06,
                "liquidity_scale": 0.9,
            },
        )

        target = engine.build(context, baseline, self._ai_assessment(direction=-0.08, confidence=0.55))

        self.assertEqual(target.target_position_qty, context.current_position_qty)
        self.assertEqual(target.delta_position_qty, Decimal("0"))
        self.assertEqual(target.position_intent, "hold")

    def test_derivatives_flat_signal_exits_when_multiple_adverse_factors_align(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": True,
                    "strategy_flat_signal_hold_enabled": True,
                }
            )
        )
        context = self._context(current_position_qty=0.028, product_type="derivatives", current_exposure_side="long")
        baseline = self._baseline(
            volatility_target_scale=1.0,
            suggested_position_scale=0.2,
            direction_bias="flat",
            confidence=0.38,
            factor_scores={
                "momentum_alpha": -0.21,
                "trend_alpha": -0.19,
                "microstructure_alpha": -0.04,
                "liquidity_scale": 0.9,
            },
        ).model_copy(update={"composite_alpha_score": 0.04})

        target = engine.build(context, baseline, self._ai_assessment(direction=-0.10, confidence=0.72))

        self.assertEqual(target.target_position_qty, 0.0)
        self.assertEqual(target.position_intent, "close_long")
        self.assertEqual(target.decision_outcome.exit_attribution, "alpha_decay_exit")

    def test_derivatives_alpha_decay_reduce_scales_down_existing_position_when_signal_fades(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.1,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": True,
                }
            )
        )
        context = self._context(current_position_qty=0.05, product_type="derivatives", current_exposure_side="long")
        baseline = self._baseline(
            direction_bias="long",
            confidence=0.42,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={"momentum_alpha": 0.05, "trend_alpha": 0.04, "microstructure_alpha": 0.03, "liquidity_scale": 0.9},
        ).model_copy(update={"composite_alpha_score": 0.08})

        target = engine.build(context, baseline, self._ai_assessment(direction=0.06, confidence=0.45))

        self.assertGreater(target.target_position_qty, Decimal("0"))
        self.assertLess(target.target_position_qty, context.current_position_qty)
        self.assertEqual(target.position_intent, "reduce_long")
        self.assertIn("alpha_decay_reduce", target.guardrail_flags)
        self.assertEqual(target.decision_outcome.exit_attribution, "alpha_decay_reduce")

    def test_derivatives_risk_contraction_reduces_existing_position_in_high_volatility(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.1,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": True,
                }
            )
        )
        context = self._context(current_position_qty=0.05, product_type="derivatives", current_exposure_side="long")
        baseline = self._baseline(
            direction_bias="long",
            confidence=0.82,
            suggested_position_scale=1.0,
            volatility_target_scale=0.62,
            volatility_state="high",
            factor_scores={"momentum_alpha": 0.32, "trend_alpha": 0.28, "microstructure_alpha": 0.14, "liquidity_scale": 0.8},
        ).model_copy(update={"composite_alpha_score": 0.36})

        target = engine.build(context, baseline, self._ai_assessment(direction=0.3, confidence=0.84))

        self.assertGreater(target.target_position_qty, Decimal("0"))
        self.assertLess(target.target_position_qty, context.current_position_qty)
        self.assertIn("risk_contraction_exit", target.guardrail_flags)
        self.assertEqual(target.decision_outcome.exit_attribution, "risk_contraction_exit")

    def test_derivatives_emergency_protective_exit_flattens_on_severe_adverse_pressure(self) -> None:
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
            direction_bias="short",
            confidence=0.9,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            volatility_state="high",
            factor_scores={"momentum_alpha": -0.34, "trend_alpha": -0.31, "microstructure_alpha": -0.24, "liquidity_scale": 0.75},
        ).model_copy(update={"composite_alpha_score": -0.42, "regime": "breakout"})

        target = engine.build(context, baseline, self._ai_assessment(direction=-0.4, confidence=0.92))

        self.assertEqual(target.target_position_qty, Decimal("0"))
        self.assertEqual(target.position_intent, "close_long")
        self.assertIn("emergency_protective_exit", target.guardrail_flags)
        self.assertEqual(target.decision_outcome.exit_attribution, "emergency_protective_exit")

    def test_derivatives_strong_reversal_is_not_forced_flat_by_only_two_adverse_factors(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": True,
                    "strategy_min_hold_seconds": 0,
                }
            )
        )
        context = self._context(current_position_qty=0.01, product_type="derivatives", current_exposure_side="long")
        baseline = self._baseline(
            direction_bias="short",
            confidence=0.92,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            volatility_state="medium",
            factor_scores={"momentum_alpha": -0.22, "trend_alpha": -0.2, "microstructure_alpha": -0.04, "liquidity_scale": 0.9},
        ).model_copy(update={"composite_alpha_score": -0.42, "regime": "trend"})

        target = engine.build(context, baseline, self._ai_assessment(direction=-0.12, confidence=0.9))

        self.assertLess(target.target_position_qty, Decimal("0"))
        self.assertEqual(target.position_intent, "reverse_to_short")
        self.assertNotIn("emergency_protective_exit", target.guardrail_flags)

    def test_cost_guard_blocks_weak_derivatives_entry(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": True,
                    "paper_taker_fee_bps": 5.0,
                    "max_slippage_tolerance_bps": 20,
                    "strategy_cost_guard_enabled": True,
                    "strategy_min_net_edge_bps": 2.0,
                    "strategy_alpha_edge_bps_scale": 100.0,
                }
            )
        )
        context = self._context(product_type="derivatives", current_exposure_side="flat")
        baseline = self._baseline(
            volatility_target_scale=1.0,
            suggested_position_scale=0.55,
            direction_bias="long",
            confidence=0.55,
            factor_scores={"momentum_alpha": 0.08, "trend_alpha": 0.06, "microstructure_alpha": 0.04, "liquidity_scale": 0.85},
        ).model_copy(update={"composite_alpha_score": 0.06})

        target = engine.build(context, baseline, self._ai_assessment(direction=0.06, confidence=0.56))

        self.assertEqual(target.target_position_qty, 0.0)
        self.assertEqual(target.position_intent, "hold")

    def test_cost_guard_allows_strong_derivatives_entry(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": True,
                    "paper_taker_fee_bps": 5.0,
                    "max_slippage_tolerance_bps": 20,
                    "strategy_cost_guard_enabled": True,
                    "strategy_min_net_edge_bps": 2.0,
                    "strategy_alpha_edge_bps_scale": 100.0,
                }
            )
        )
        context = self._context(product_type="derivatives", current_exposure_side="flat")
        baseline = self._baseline(
            volatility_target_scale=1.0,
            suggested_position_scale=0.8,
            direction_bias="long",
            confidence=0.84,
            factor_scores={"momentum_alpha": 0.24, "trend_alpha": 0.22, "microstructure_alpha": 0.17, "liquidity_scale": 0.95},
        ).model_copy(update={"composite_alpha_score": 0.21})

        target = engine.build(context, baseline, self._ai_assessment(direction=0.24, confidence=0.86))

        self.assertGreater(target.target_position_qty, 0.0)
        self.assertEqual(target.position_intent, "open_long")

    def test_derivatives_entry_is_blocked_outside_allowed_regimes(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": True,
                    "strategy_entry_allowed_regimes": ("trend", "breakout"),
                    "strategy_entry_alpha_min": 0.18,
                    "strategy_entry_confidence_min": 0.62,
                }
            )
        )
        context = self._context(product_type="derivatives", current_exposure_side="flat")
        baseline = self._baseline(
            direction_bias="long",
            confidence=0.82,
            suggested_position_scale=0.8,
            volatility_target_scale=1.0,
        ).model_copy(update={"regime": "range", "composite_alpha_score": 0.32})

        target = engine.build(context, baseline, self._ai_assessment(direction=0.24, confidence=0.8))

        self.assertEqual(target.target_position_qty, 0.0)
        self.assertEqual(target.position_intent, "hold")
        self.assertIn("entry_regime_not_allowed", target.guardrail_flags)

    def test_derivatives_scale_in_requires_stronger_follow_through(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": True,
                    "strategy_scale_in_alpha_min": 0.24,
                    "strategy_scale_in_confidence_min": 0.68,
                }
            )
        )
        context = self._context(current_position_qty=0.0035, product_type="derivatives", current_exposure_side="long")
        baseline = self._baseline(
            direction_bias="long",
            confidence=0.61,
            suggested_position_scale=0.85,
            volatility_target_scale=1.0,
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.19})

        target = engine.build(context, baseline, self._ai_assessment(direction=0.14, confidence=0.6))

        self.assertEqual(target.target_position_qty, context.current_position_qty)
        self.assertEqual(target.position_intent, "hold")

    def test_derivatives_reversal_requires_strong_conviction(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": True,
                    "strategy_reversal_alpha_min": 0.3,
                    "strategy_reversal_confidence_min": 0.75,
                    "strategy_short_reversal_alpha_min": 0.3,
                    "strategy_short_reversal_confidence_min": 0.75,
                }
            )
        )
        context = self._context(current_position_qty=0.01, product_type="derivatives", current_exposure_side="long")
        baseline = self._baseline(
            direction_bias="short",
            confidence=0.66,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
        ).model_copy(update={"regime": "trend", "composite_alpha_score": -0.22})

        target = engine.build(context, baseline, self._ai_assessment(direction=-0.18, confidence=0.61))

        self.assertEqual(target.target_position_qty, context.current_position_qty)
        self.assertEqual(target.position_intent, "hold")

    def test_min_hold_blocks_early_reversal(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": True,
                    "strategy_min_hold_seconds": 1_800,
                }
            )
        )
        context = self._context(
            current_position_qty=0.01,
            product_type="derivatives",
            current_exposure_side="long",
            current_position_opened_seconds_ago=120,
        )
        baseline = self._baseline(
            direction_bias="short",
            confidence=0.9,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
        ).model_copy(update={"composite_alpha_score": -0.35})

        target = engine.build(context, baseline, self._ai_assessment(direction=-0.45, confidence=0.9))

        self.assertEqual(target.target_position_qty, context.current_position_qty)
        self.assertIn("min_hold_active", target.guardrail_flags)
        self.assertIn("min_hold_blocks_exit", target.guardrail_flags)

    def test_post_close_cooldown_blocks_new_entry(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": True,
                    "strategy_post_close_cooldown_seconds": 900,
                }
            )
        )
        context = self._context(
            product_type="derivatives",
            current_exposure_side="flat",
            last_position_closed_seconds_ago=120,
        )
        baseline = self._baseline(
            direction_bias="long",
            confidence=0.88,
            suggested_position_scale=0.9,
            volatility_target_scale=1.0,
        ).model_copy(update={"composite_alpha_score": 0.28})

        target = engine.build(context, baseline, self._ai_assessment(direction=0.3, confidence=0.84))

        self.assertEqual(target.target_position_qty, Decimal("0"))
        self.assertIn("post_close_cooldown_active", target.guardrail_flags)
        self.assertIn("post_close_cooldown_blocks_entry", target.guardrail_flags)

    def test_low_edge_streak_blocks_new_entry(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": True,
                    "strategy_low_edge_streak_limit": 3,
                    "strategy_low_edge_cooldown_seconds": 900,
                }
            )
        )
        context = self._context(
            product_type="derivatives",
            current_exposure_side="flat",
            recent_low_edge_trade_streak=3,
            recent_low_edge_trade_seconds_ago=60,
        )
        baseline = self._baseline(
            direction_bias="long",
            confidence=0.9,
            suggested_position_scale=0.8,
            volatility_target_scale=1.0,
        ).model_copy(update={"composite_alpha_score": 0.3})

        target = engine.build(context, baseline, self._ai_assessment(direction=0.34, confidence=0.88))

        self.assertEqual(target.target_position_qty, Decimal("0"))
        self.assertIn("low_edge_cooldown_active", target.guardrail_flags)
        self.assertIn("low_edge_cooldown_blocks_entry", target.guardrail_flags)

    def test_cost_guard_includes_noise_buffer(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": True,
                    "paper_taker_fee_bps": 5.0,
                    "max_slippage_tolerance_bps": 20,
                    "strategy_expected_slippage_bps_fraction": 0.25,
                    "strategy_cost_guard_enabled": True,
                    "strategy_min_net_edge_bps": 2.0,
                    "strategy_edge_noise_buffer_bps": 20.0,
                    "strategy_alpha_edge_bps_scale": 100.0,
                }
            )
        )
        context = self._context(product_type="derivatives", current_exposure_side="flat")
        baseline = self._baseline(
            direction_bias="long",
            confidence=0.84,
            suggested_position_scale=0.8,
            volatility_target_scale=1.0,
            factor_scores={"momentum_alpha": 0.24, "trend_alpha": 0.22, "microstructure_alpha": 0.17, "liquidity_scale": 0.95},
        ).model_copy(update={"composite_alpha_score": 0.21})

        target = engine.build(context, baseline, self._ai_assessment(direction=0.24, confidence=0.86))

        self.assertEqual(target.target_position_qty, Decimal("0"))
        self.assertIn("expected_edge_below_cost_buffer", target.guardrail_flags)

    def test_derivatives_hedge_mode_generates_explicit_primary_leg_for_directional_entry(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "derivatives_position_mode": "hedge",
                    "strategy_short_bias_enabled": True,
                    "strategy_hedge_overlay_enabled": True,
                    "strategy_cost_guard_enabled": False,
                    "strategy_entry_min_signal_edge_bps": 0.0,
                    "strategy_entry_alpha_min": 0.0,
                    "strategy_entry_confidence_min": 0.0,
                }
            )
        )
        context = self._context(product_type="derivatives", current_exposure_side="flat")
        baseline = self._baseline(
            direction_bias="long",
            confidence=0.82,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
        ).model_copy(update={"composite_alpha_score": 0.36})

        target = engine.build(context, baseline, self._ai_assessment(direction=0.24, confidence=0.84))

        self.assertGreater(target.target_position_qty, Decimal("0"))
        self.assertEqual(len(target.strategy_execution_legs), 1)
        leg = target.strategy_execution_legs[0]
        self.assertEqual(leg.role, "primary")
        self.assertEqual(leg.position_mode, "long_short_mode")
        self.assertEqual(leg.pos_side, "long")
        self.assertEqual(leg.action, "open")
        self.assertEqual(leg.execution_mode, "directional_main_leg")
        self.assertIsNotNone(target.hedge_overlay_decision)
        assert target.hedge_overlay_decision is not None
        self.assertTrue(target.hedge_overlay_decision.enabled)
        self.assertEqual(target.hedge_overlay_decision.state, "inactive")
        self.assertIn("protective_overlay_no_existing_inventory", target.hedge_overlay_decision.reason_codes)

    def test_derivatives_protective_overlay_opens_short_hedge_leg_against_existing_long(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "derivatives_position_mode": "hedge",
                    "strategy_short_bias_enabled": True,
                    "strategy_hedge_overlay_enabled": True,
                    "strategy_cost_guard_enabled": False,
                    "strategy_entry_min_signal_edge_bps": 0.0,
                    "strategy_entry_alpha_min": 0.0,
                    "strategy_entry_confidence_min": 0.0,
                    "strategy_reversal_min_signal_edge_bps": 50.0,
                    "strategy_reversal_alpha_min": 0.60,
                    "strategy_reversal_confidence_min": 0.95,
                    "strategy_short_reversal_min_signal_edge_bps": 50.0,
                    "strategy_short_reversal_alpha_min": 0.60,
                    "strategy_short_reversal_confidence_min": 0.95,
                    "strategy_edge_noise_buffer_bps": 0.0,
                }
            )
        )
        context = self._context(
            current_position_qty=0.05,
            current_long_position_qty=0.05,
            product_type="derivatives",
            current_exposure_side="long",
        )
        baseline = self._baseline(
            direction_bias="short",
            confidence=0.85,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            volatility_state="high",
            factor_scores={
                "momentum_alpha": -0.22,
                "trend_alpha": 0.03,
                "microstructure_alpha": -0.21,
                "liquidity_scale": 0.9,
            },
        ).model_copy(update={"regime": "range", "composite_alpha_score": -0.40})

        target = engine.build(context, baseline, self._ai_assessment(direction=-0.12, confidence=0.83))

        self.assertIsNotNone(target.hedge_overlay_decision)
        assert target.hedge_overlay_decision is not None
        self.assertTrue(target.hedge_overlay_decision.active)
        self.assertEqual(target.hedge_overlay_decision.state, "opening")
        hedge_leg = next((item for item in target.strategy_execution_legs if item.role == "hedge"), None)
        self.assertIsNotNone(hedge_leg)
        assert hedge_leg is not None
        self.assertEqual(hedge_leg.pos_side, "short")
        self.assertEqual(hedge_leg.action, "open")
        self.assertEqual(hedge_leg.position_mode, "long_short_mode")
        self.assertIn("protective_hedge_overlay_active", target.guardrail_flags)

    def test_derivatives_protective_overlay_respects_min_hold_before_closing_hedge(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "derivatives_position_mode": "hedge",
                    "strategy_short_bias_enabled": True,
                    "strategy_hedge_overlay_enabled": True,
                    "strategy_hedge_min_hold_seconds": 300.0,
                }
            )
        )
        context = self._context(
            current_position_qty=0.03,
            current_long_position_qty=0.05,
            current_short_position_qty=0.02,
            product_type="derivatives",
            current_exposure_side="long",
            current_short_leg_opened_seconds_ago=60,
            latest_short_leg_fill_seconds_ago=60,
        )
        baseline = self._baseline(
            direction_bias="long",
            confidence=0.62,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.12,
                "trend_alpha": 0.10,
                "microstructure_alpha": 0.04,
                "liquidity_scale": 0.92,
            },
        ).model_copy(update={"composite_alpha_score": 0.18})

        target = engine.build(context, baseline, self._ai_assessment(direction=0.08, confidence=0.64))

        self.assertIsNotNone(target.hedge_overlay_decision)
        assert target.hedge_overlay_decision is not None
        self.assertIn("protective_overlay_min_hold_active", target.hedge_overlay_decision.blocked_reasons)
        self.assertGreater(target.hedge_overlay_decision.min_hold_remaining_seconds, 0.0)
        self.assertEqual(target.hedge_overlay_decision.hedge_leg_target_qty, Decimal("0.02"))
        self.assertIsNone(next((item for item in target.strategy_execution_legs if item.role == "hedge"), None))

    def test_derivatives_protective_overlay_respects_dedicated_enabled_switch(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "derivatives_position_mode": "hedge",
                    "strategy_short_bias_enabled": True,
                    "strategy_hedge_overlay_enabled": True,
                    "strategy_hedge_protective_enabled": False,
                    "strategy_hedge_overlay_mode": "protective",
                    "strategy_cost_guard_enabled": False,
                    "strategy_entry_min_signal_edge_bps": 0.0,
                    "strategy_entry_alpha_min": 0.0,
                    "strategy_entry_confidence_min": 0.0,
                    "strategy_reversal_min_signal_edge_bps": 50.0,
                    "strategy_reversal_alpha_min": 0.60,
                    "strategy_reversal_confidence_min": 0.95,
                    "strategy_short_reversal_min_signal_edge_bps": 50.0,
                    "strategy_short_reversal_alpha_min": 0.60,
                    "strategy_short_reversal_confidence_min": 0.95,
                    "strategy_edge_noise_buffer_bps": 0.0,
                }
            )
        )
        context = self._context(
            current_position_qty=0.05,
            current_long_position_qty=0.05,
            product_type="derivatives",
            current_exposure_side="long",
        )
        baseline = self._baseline(
            direction_bias="short",
            confidence=0.85,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            volatility_state="high",
            factor_scores={
                "momentum_alpha": -0.22,
                "trend_alpha": 0.03,
                "microstructure_alpha": -0.21,
                "liquidity_scale": 0.9,
            },
        ).model_copy(update={"regime": "range", "composite_alpha_score": -0.40})

        target = engine.build(context, baseline, self._ai_assessment(direction=-0.12, confidence=0.83))

        self.assertIsNotNone(target.hedge_overlay_decision)
        assert target.hedge_overlay_decision is not None
        self.assertEqual(target.hedge_overlay_decision.state, "blocked")
        self.assertIn("protective_overlay_not_enabled", target.hedge_overlay_decision.blocked_reasons)
        self.assertIsNone(next((item for item in target.strategy_execution_legs if item.role == "hedge"), None))

    def test_derivatives_protective_overlay_respects_rebalance_cooldown_before_reopening(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "derivatives_position_mode": "hedge",
                    "strategy_short_bias_enabled": True,
                    "strategy_hedge_overlay_enabled": True,
                    "strategy_cost_guard_enabled": False,
                    "strategy_entry_min_signal_edge_bps": 0.0,
                    "strategy_entry_alpha_min": 0.0,
                    "strategy_entry_confidence_min": 0.0,
                    "strategy_reversal_min_signal_edge_bps": 50.0,
                    "strategy_reversal_alpha_min": 0.60,
                    "strategy_reversal_confidence_min": 0.95,
                    "strategy_short_reversal_min_signal_edge_bps": 50.0,
                    "strategy_short_reversal_alpha_min": 0.60,
                    "strategy_short_reversal_confidence_min": 0.95,
                    "strategy_edge_noise_buffer_bps": 0.0,
                    "strategy_hedge_rebalance_cooldown_seconds": 120.0,
                }
            )
        )
        context = self._context(
            current_position_qty=0.05,
            current_long_position_qty=0.05,
            product_type="derivatives",
            current_exposure_side="long",
            last_short_leg_closed_seconds_ago=30,
        )
        baseline = self._baseline(
            direction_bias="short",
            confidence=0.85,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            volatility_state="high",
            factor_scores={
                "momentum_alpha": -0.22,
                "trend_alpha": 0.03,
                "microstructure_alpha": -0.21,
                "liquidity_scale": 0.9,
            },
        ).model_copy(update={"regime": "range", "composite_alpha_score": -0.40})

        target = engine.build(context, baseline, self._ai_assessment(direction=-0.12, confidence=0.83))

        self.assertIsNotNone(target.hedge_overlay_decision)
        assert target.hedge_overlay_decision is not None
        self.assertIn("protective_overlay_rebalance_cooldown_active", target.hedge_overlay_decision.blocked_reasons)
        self.assertGreater(target.hedge_overlay_decision.rebalance_cooldown_remaining_seconds, 0.0)
        self.assertFalse(target.hedge_overlay_decision.active)
        self.assertIsNone(next((item for item in target.strategy_execution_legs if item.role == "hedge"), None))

    def test_derivatives_opportunistic_overlay_opens_short_opportunity_leg_against_existing_long(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "derivatives_position_mode": "hedge",
                    "strategy_short_bias_enabled": True,
                    "strategy_hedge_overlay_enabled": True,
                    "strategy_hedge_overlay_mode": "opportunistic",
                    "strategy_hedge_opportunistic_enabled": True,
                    "strategy_hedge_opportunistic_open_threshold": 0.62,
                    "strategy_hedge_opportunistic_close_threshold": 0.46,
                    "strategy_cost_guard_enabled": False,
                    "strategy_entry_min_signal_edge_bps": 0.0,
                    "strategy_entry_alpha_min": 0.0,
                    "strategy_entry_confidence_min": 0.0,
                    "strategy_reversal_min_signal_edge_bps": 50.0,
                    "strategy_reversal_alpha_min": 0.60,
                    "strategy_reversal_confidence_min": 0.95,
                    "strategy_short_reversal_min_signal_edge_bps": 50.0,
                    "strategy_short_reversal_alpha_min": 0.60,
                    "strategy_short_reversal_confidence_min": 0.95,
                    "strategy_edge_noise_buffer_bps": 0.0,
                }
            )
        )
        context = self._context(
            current_position_qty=0.05,
            current_long_position_qty=0.05,
            product_type="derivatives",
            current_exposure_side="long",
        )
        baseline = self._baseline(
            direction_bias="long",
            confidence=0.84,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            volatility_state="high",
            factor_scores={
                "momentum_alpha": -0.75,
                "trend_alpha": 0.05,
                "microstructure_alpha": -0.95,
                "liquidity_scale": 0.88,
            },
        ).model_copy(update={"regime": "uncertain", "composite_alpha_score": 0.28})

        with (
            patch.object(engine, "_emergency_protective_exit_required", return_value=False),
            patch.object(engine, "_opportunistic_overlay_score", return_value=0.82),
        ):
            target = engine.build(context, baseline, self._ai_assessment(direction=-0.25, confidence=0.80))

        self.assertIsNotNone(target.hedge_overlay_decision)
        assert target.hedge_overlay_decision is not None
        self.assertEqual(target.hedge_overlay_decision.effective_mode, "opportunistic")
        self.assertTrue(target.hedge_overlay_decision.active)
        self.assertEqual(target.hedge_overlay_decision.state, "opening")
        hedge_leg = next((item for item in target.strategy_execution_legs if item.role == "hedge"), None)
        self.assertIsNotNone(hedge_leg)
        assert hedge_leg is not None
        self.assertEqual(hedge_leg.pos_side, "short")
        self.assertEqual(hedge_leg.action, "open")
        self.assertEqual(hedge_leg.execution_mode, "opportunistic_overlay")
        self.assertEqual(hedge_leg.overlay_mode, "opportunistic")
        self.assertIn("opportunistic_hedge_overlay_active", target.guardrail_flags)

    def test_derivatives_opportunistic_overlay_blocks_new_leg_when_fee_drag_is_too_high(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "derivatives_position_mode": "hedge",
                    "strategy_short_bias_enabled": True,
                    "strategy_hedge_overlay_enabled": True,
                    "strategy_hedge_overlay_mode": "opportunistic",
                    "strategy_hedge_opportunistic_enabled": True,
                    "strategy_hedge_opportunistic_max_fee_drag_ratio": 0.18,
                    "strategy_performance_guard_min_closed_trades": 4,
                    "strategy_cost_guard_enabled": False,
                    "strategy_entry_min_signal_edge_bps": 0.0,
                    "strategy_entry_alpha_min": 0.0,
                    "strategy_entry_confidence_min": 0.0,
                    "strategy_reversal_min_signal_edge_bps": 50.0,
                    "strategy_reversal_alpha_min": 0.60,
                    "strategy_reversal_confidence_min": 0.95,
                    "strategy_short_reversal_min_signal_edge_bps": 50.0,
                    "strategy_short_reversal_alpha_min": 0.60,
                    "strategy_short_reversal_confidence_min": 0.95,
                    "strategy_edge_noise_buffer_bps": 0.0,
                }
            )
        )
        context = self._context(
            current_position_qty=0.05,
            current_long_position_qty=0.05,
            product_type="derivatives",
            current_exposure_side="long",
            recent_closed_trade_count=6,
            recent_fee_drag_ratio=0.30,
        )
        baseline = self._baseline(
            direction_bias="long",
            confidence=0.84,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            volatility_state="high",
            factor_scores={
                "momentum_alpha": -0.75,
                "trend_alpha": 0.05,
                "microstructure_alpha": -0.95,
                "liquidity_scale": 0.88,
            },
        ).model_copy(update={"regime": "uncertain", "composite_alpha_score": 0.28})

        with (
            patch.object(engine, "_emergency_protective_exit_required", return_value=False),
            patch.object(engine, "_opportunistic_overlay_score", return_value=0.82),
        ):
            target = engine.build(context, baseline, self._ai_assessment(direction=-0.25, confidence=0.80))

        self.assertIsNotNone(target.hedge_overlay_decision)
        assert target.hedge_overlay_decision is not None
        self.assertEqual(target.hedge_overlay_decision.effective_mode, "opportunistic")
        self.assertIn("opportunistic_overlay_fee_drag_guard_active", target.hedge_overlay_decision.blocked_reasons)
        self.assertEqual(target.hedge_overlay_decision.hedge_leg_target_qty, Decimal("0"))
        self.assertFalse(target.hedge_overlay_decision.active)
        self.assertIsNone(next((item for item in target.strategy_execution_legs if item.role == "hedge"), None))

    def test_derivatives_opportunistic_overlay_respects_min_hold_before_closing_leg(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "derivatives_position_mode": "hedge",
                    "strategy_short_bias_enabled": True,
                    "strategy_hedge_overlay_enabled": True,
                    "strategy_hedge_overlay_mode": "opportunistic",
                    "strategy_hedge_opportunistic_enabled": True,
                    "strategy_hedge_opportunistic_min_hold_seconds": 180.0,
                }
            )
        )
        context = self._context(
            current_position_qty=0.03,
            current_long_position_qty=0.05,
            current_short_position_qty=0.02,
            product_type="derivatives",
            current_exposure_side="long",
            current_short_leg_opened_seconds_ago=60,
            latest_short_leg_fill_seconds_ago=60,
        )
        baseline = self._baseline(
            direction_bias="long",
            confidence=0.72,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.16,
                "trend_alpha": 0.12,
                "microstructure_alpha": 0.18,
                "liquidity_scale": 0.92,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.24})

        target = engine.build(context, baseline, self._ai_assessment(direction=0.12, confidence=0.70))

        self.assertIsNotNone(target.hedge_overlay_decision)
        assert target.hedge_overlay_decision is not None
        self.assertEqual(target.hedge_overlay_decision.effective_mode, "opportunistic")
        self.assertIn("opportunistic_overlay_min_hold_active", target.hedge_overlay_decision.blocked_reasons)
        self.assertGreater(target.hedge_overlay_decision.min_hold_remaining_seconds, 0.0)
        self.assertEqual(target.hedge_overlay_decision.hedge_leg_target_qty, Decimal("0.02"))
        self.assertIsNone(next((item for item in target.strategy_execution_legs if item.role == "hedge"), None))

    def test_derivatives_opportunistic_overlay_respects_rebalance_cooldown_before_reopening(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "derivatives_position_mode": "hedge",
                    "strategy_short_bias_enabled": True,
                    "strategy_hedge_overlay_enabled": True,
                    "strategy_hedge_overlay_mode": "opportunistic",
                    "strategy_hedge_opportunistic_enabled": True,
                    "strategy_hedge_opportunistic_rebalance_cooldown_seconds": 90.0,
                    "strategy_cost_guard_enabled": False,
                    "strategy_entry_min_signal_edge_bps": 0.0,
                    "strategy_entry_alpha_min": 0.0,
                    "strategy_entry_confidence_min": 0.0,
                    "strategy_reversal_min_signal_edge_bps": 50.0,
                    "strategy_reversal_alpha_min": 0.60,
                    "strategy_reversal_confidence_min": 0.95,
                    "strategy_short_reversal_min_signal_edge_bps": 50.0,
                    "strategy_short_reversal_alpha_min": 0.60,
                    "strategy_short_reversal_confidence_min": 0.95,
                    "strategy_edge_noise_buffer_bps": 0.0,
                }
            )
        )
        context = self._context(
            current_position_qty=0.05,
            current_long_position_qty=0.05,
            product_type="derivatives",
            current_exposure_side="long",
            last_short_leg_closed_seconds_ago=30,
        )
        baseline = self._baseline(
            direction_bias="long",
            confidence=0.84,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            volatility_state="high",
            factor_scores={
                "momentum_alpha": -0.75,
                "trend_alpha": 0.05,
                "microstructure_alpha": -0.95,
                "liquidity_scale": 0.88,
            },
        ).model_copy(update={"regime": "uncertain", "composite_alpha_score": 0.28})

        with (
            patch.object(engine, "_emergency_protective_exit_required", return_value=False),
            patch.object(engine, "_opportunistic_overlay_score", return_value=0.82),
        ):
            target = engine.build(context, baseline, self._ai_assessment(direction=-0.25, confidence=0.80))

        self.assertIsNotNone(target.hedge_overlay_decision)
        assert target.hedge_overlay_decision is not None
        self.assertEqual(target.hedge_overlay_decision.effective_mode, "opportunistic")
        self.assertIn("opportunistic_overlay_rebalance_cooldown_active", target.hedge_overlay_decision.blocked_reasons)
        self.assertGreater(target.hedge_overlay_decision.rebalance_cooldown_remaining_seconds, 0.0)
        self.assertFalse(target.hedge_overlay_decision.active)
        self.assertIsNone(next((item for item in target.strategy_execution_legs if item.role == "hedge"), None))

    def test_derivatives_opportunistic_overlay_does_not_retag_old_main_leg_during_reversal_handover(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "derivatives_position_mode": "hedge",
                    "strategy_short_bias_enabled": True,
                    "strategy_hedge_overlay_enabled": True,
                    "strategy_hedge_overlay_mode": "opportunistic",
                    "strategy_hedge_opportunistic_enabled": True,
                    "strategy_cost_guard_enabled": False,
                    "strategy_entry_min_signal_edge_bps": 0.0,
                    "strategy_entry_alpha_min": 0.0,
                    "strategy_entry_confidence_min": 0.0,
                    "strategy_reversal_min_signal_edge_bps": 0.0,
                    "strategy_reversal_alpha_min": 0.0,
                    "strategy_reversal_confidence_min": 0.0,
                    "strategy_short_reversal_min_signal_edge_bps": 0.0,
                    "strategy_short_reversal_alpha_min": 0.0,
                    "strategy_short_reversal_confidence_min": 0.0,
                    "strategy_edge_noise_buffer_bps": 0.0,
                }
            )
        )
        context = self._context(
            current_position_qty=-0.05,
            current_short_position_qty=0.05,
            product_type="derivatives",
            current_exposure_side="short",
        )
        baseline = self._baseline(
            direction_bias="long",
            confidence=0.90,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            volatility_state="high",
            factor_scores={
                "momentum_alpha": -0.75,
                "trend_alpha": 0.05,
                "microstructure_alpha": -0.95,
                "liquidity_scale": 0.88,
            },
        ).model_copy(update={"regime": "uncertain", "composite_alpha_score": 0.28})

        with (
            patch.object(engine, "_emergency_protective_exit_required", return_value=False),
            patch.object(engine, "_target_quantity", return_value=Decimal("0.05")),
            patch.object(engine, "_opportunistic_overlay_score", return_value=0.20),
        ):
            target = engine.build(context, baseline, self._ai_assessment(direction=0.25, confidence=0.80))

        self.assertEqual(target.target_position_qty, Decimal("0.05"))
        self.assertIsNotNone(target.hedge_overlay_decision)
        assert target.hedge_overlay_decision is not None
        self.assertEqual(target.hedge_overlay_decision.effective_mode, "opportunistic")
        self.assertEqual(target.hedge_overlay_decision.state, "inactive")
        self.assertFalse(target.hedge_overlay_decision.active)
        self.assertEqual(target.hedge_overlay_decision.hedge_leg_current_qty, Decimal("0"))
        self.assertEqual(
            target.hedge_overlay_decision.reason_codes,
            ["opportunistic_overlay_no_existing_inventory"],
        )
        self.assertEqual(len(target.strategy_execution_legs), 2)
        long_leg = next(item for item in target.strategy_execution_legs if item.pos_side == "long")
        short_leg = next(item for item in target.strategy_execution_legs if item.pos_side == "short")
        self.assertEqual(long_leg.role, "primary")
        self.assertEqual(long_leg.execution_mode, "directional_main_leg")
        self.assertEqual(short_leg.role, "primary")
        self.assertEqual(short_leg.execution_mode, "directional_main_leg")
        self.assertIsNone(short_leg.overlay_mode)

    def test_derivatives_opportunistic_overlay_blocks_live_runtime_before_rollout_stage_is_live(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "derivatives_position_mode": "hedge",
                    "strategy_short_bias_enabled": True,
                    "strategy_hedge_overlay_enabled": True,
                    "strategy_hedge_overlay_mode": "opportunistic",
                    "strategy_hedge_opportunistic_enabled": True,
                    "strategy_hedge_opportunistic_rollout_stage": "dry_run",
                    "guarded_execution_dry_run": False,
                    "live_submit_enabled": True,
                    "okx_simulated_trading": False,
                    "strategy_cost_guard_enabled": False,
                    "strategy_entry_min_signal_edge_bps": 0.0,
                    "strategy_entry_alpha_min": 0.0,
                    "strategy_entry_confidence_min": 0.0,
                    "strategy_reversal_min_signal_edge_bps": 50.0,
                    "strategy_reversal_alpha_min": 0.60,
                    "strategy_reversal_confidence_min": 0.95,
                    "strategy_short_reversal_min_signal_edge_bps": 50.0,
                    "strategy_short_reversal_alpha_min": 0.60,
                    "strategy_short_reversal_confidence_min": 0.95,
                    "strategy_edge_noise_buffer_bps": 0.0,
                }
            )
        )
        context = self._context(
            current_position_qty=0.05,
            current_long_position_qty=0.05,
            product_type="derivatives",
            current_exposure_side="long",
        )
        baseline = self._baseline(
            direction_bias="long",
            confidence=0.84,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            volatility_state="high",
            factor_scores={
                "momentum_alpha": -0.75,
                "trend_alpha": 0.05,
                "microstructure_alpha": -0.95,
                "liquidity_scale": 0.88,
            },
        ).model_copy(update={"regime": "uncertain", "composite_alpha_score": 0.28})

        with (
            patch.object(engine, "_emergency_protective_exit_required", return_value=False),
            patch.object(engine, "_opportunistic_overlay_score", return_value=0.82),
        ):
            target = engine.build(context, baseline, self._ai_assessment(direction=-0.25, confidence=0.80))

        self.assertIsNotNone(target.hedge_overlay_decision)
        assert target.hedge_overlay_decision is not None
        self.assertEqual(target.hedge_overlay_decision.effective_mode, "opportunistic")
        self.assertEqual(target.hedge_overlay_decision.rollout_stage, "dry_run")
        self.assertEqual(target.hedge_overlay_decision.runtime_rollout_stage, "live")
        self.assertIn(
            "opportunistic_overlay_rollout_stage_blocks_live_runtime",
            target.hedge_overlay_decision.blocked_reasons,
        )
        self.assertFalse(target.hedge_overlay_decision.active)
        self.assertIsNone(next((item for item in target.strategy_execution_legs if item.role == "hedge"), None))

    def test_derivatives_independent_books_allow_long_reentry_while_short_book_is_still_cooling_down(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "derivatives_position_mode": "hedge",
                    "strategy_short_bias_enabled": True,
                    "strategy_hedge_overlay_enabled": True,
                    "strategy_hedge_overlay_mode": "independent",
                    "strategy_hedge_independent_enabled": True,
                    "strategy_hedge_independent_rebalance_cooldown_seconds": 120.0,
                    "strategy_post_close_cooldown_seconds": 300.0,
                    "strategy_cost_guard_enabled": False,
                    "strategy_entry_min_signal_edge_bps": 0.0,
                    "strategy_entry_alpha_min": 0.0,
                    "strategy_entry_confidence_min": 0.0,
                }
            )
        )
        context = self._context(
            product_type="derivatives",
            current_exposure_side="flat",
            last_short_leg_closed_seconds_ago=30,
        )
        baseline = self._baseline(
            direction_bias="long",
            confidence=0.84,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.48,
                "trend_alpha": 0.42,
                "microstructure_alpha": 0.18,
                "liquidity_scale": 0.95,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.32})

        with patch.object(
            engine,
            "_independent_book_score",
            side_effect=lambda *, leg, baseline, ai_assessment: 0.78 if leg == "long" else 0.75,
        ):
            target = engine.build(context, baseline, self._ai_assessment(direction=0.25, confidence=0.82))

        self.assertIsNotNone(target.hedge_overlay_decision)
        assert target.hedge_overlay_decision is not None
        self.assertEqual(target.hedge_overlay_decision.effective_mode, "independent")
        self.assertIn(
            "independent_short_book_post_close_cooldown_active",
            target.hedge_overlay_decision.short_leg_blocked_reasons,
        )
        long_leg = next((item for item in target.strategy_execution_legs if item.pos_side == "long"), None)
        short_leg = next((item for item in target.strategy_execution_legs if item.pos_side == "short"), None)
        self.assertIsNotNone(long_leg)
        assert long_leg is not None
        self.assertEqual(long_leg.action, "open")
        self.assertEqual(long_leg.execution_mode, "independent_long_book")
        self.assertIsNone(short_leg)

    def test_derivatives_independent_books_keep_short_book_when_long_book_trial_guard_is_bad(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "derivatives_position_mode": "hedge",
                    "strategy_short_bias_enabled": True,
                    "strategy_hedge_overlay_enabled": True,
                    "strategy_hedge_overlay_mode": "independent",
                    "strategy_hedge_independent_enabled": True,
                    "strategy_hedge_independent_trial_guard_enabled": True,
                    "strategy_performance_guard_min_closed_trades": 4,
                }
            )
        )
        context = self._context(
            current_position_qty=-0.02,
            current_short_position_qty=0.02,
            product_type="derivatives",
            current_exposure_side="short",
            current_short_leg_opened_seconds_ago=900,
            leg_strategy_health={
                "long": {
                    "recent_closed_trade_count": 5,
                    "recent_win_rate": 0.20,
                    "recent_fee_drag_ratio": 0.04,
                    "recent_churn_ratio": 0.08,
                    "recent_low_edge_trade_streak": 0,
                    "recent_low_edge_trade_at": None,
                    "recent_net_realized_pnl": Decimal("-18"),
                },
                "short": {
                    "recent_closed_trade_count": 5,
                    "recent_win_rate": 0.80,
                    "recent_fee_drag_ratio": 0.03,
                    "recent_churn_ratio": 0.05,
                    "recent_low_edge_trade_streak": 0,
                    "recent_low_edge_trade_at": None,
                    "recent_net_realized_pnl": Decimal("12"),
                },
            },
        )
        baseline = self._baseline(
            direction_bias="short",
            confidence=0.82,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": -0.42,
                "trend_alpha": -0.38,
                "microstructure_alpha": -0.16,
                "liquidity_scale": 0.92,
            },
        ).model_copy(update={"regime": "uncertain", "composite_alpha_score": -0.28})

        with patch.object(
            engine,
            "_independent_book_score",
            side_effect=lambda *, leg, baseline, ai_assessment: 0.76 if leg == "long" else 0.72,
        ):
            target = engine.build(context, baseline, self._ai_assessment(direction=-0.22, confidence=0.80))

        self.assertIsNotNone(target.hedge_overlay_decision)
        assert target.hedge_overlay_decision is not None
        self.assertEqual(target.hedge_overlay_decision.effective_mode, "independent")
        self.assertIn(
            "independent_long_book_trial_guard_active",
            target.hedge_overlay_decision.long_leg_blocked_reasons,
        )
        self.assertIn(
            "independent_short_book_hold_above_entry_threshold",
            target.hedge_overlay_decision.short_leg_reason_codes,
        )

    def test_derivatives_independent_books_block_live_runtime_before_rollout_stage_is_live(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "derivatives_position_mode": "hedge",
                    "strategy_short_bias_enabled": True,
                    "strategy_hedge_overlay_enabled": True,
                    "strategy_hedge_overlay_mode": "independent",
                    "strategy_hedge_independent_enabled": True,
                    "strategy_hedge_independent_rollout_stage": "dry_run",
                    "guarded_execution_dry_run": False,
                    "live_submit_enabled": True,
                    "okx_simulated_trading": False,
                }
            )
        )
        context = self._context(
            current_position_qty=0.01,
            current_long_position_qty=0.01,
            product_type="derivatives",
            current_exposure_side="long",
        )
        baseline = self._baseline(
            direction_bias="long",
            confidence=0.82,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.42,
                "trend_alpha": 0.38,
                "microstructure_alpha": 0.16,
                "liquidity_scale": 0.92,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.28})

        target = engine.build(context, baseline, self._ai_assessment(direction=0.22, confidence=0.80))

        self.assertIsNotNone(target.hedge_overlay_decision)
        assert target.hedge_overlay_decision is not None
        self.assertEqual(target.hedge_overlay_decision.effective_mode, "independent")
        self.assertEqual(target.hedge_overlay_decision.rollout_stage, "dry_run")
        self.assertEqual(target.hedge_overlay_decision.runtime_rollout_stage, "live")
        self.assertIn(
            "independent_overlay_rollout_stage_blocks_live_runtime",
            target.hedge_overlay_decision.blocked_reasons,
        )
        self.assertFalse(target.strategy_execution_legs)
        self.assertEqual(target.target_position_qty, Decimal("0.01"))
        self.assertEqual(target.current_position_qty, Decimal("0.01"))
        self.assertIsNone(next((item for item in target.strategy_execution_legs if item.pos_side == "long"), None))

    def test_expected_cost_uses_injected_dynamic_fee_resolver(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": True,
                    "max_slippage_tolerance_bps": 20,
                    "strategy_expected_slippage_bps_fraction": 0.25,
                }
            ),
            fee_resolver=_FixedFeeResolver(12.0),
        )

        target = engine.build(
            self._context(product_type="derivatives", current_exposure_side="flat"),
            self._baseline(
                direction_bias="long",
                confidence=0.84,
                suggested_position_scale=0.8,
                volatility_target_scale=1.0,
            ),
            self._ai_assessment(direction=0.24, confidence=0.86),
        )

        self.assertEqual(target.expected_cost_bps, 17.0)

    def test_cost_guard_uses_contextual_derivatives_cost_components(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": True,
                    "max_slippage_tolerance_bps": 0,
                    "strategy_expected_slippage_bps_fraction": 0.0,
                    "strategy_cost_guard_enabled": True,
                    "strategy_min_net_edge_bps": 2.0,
                    "strategy_alpha_edge_bps_scale": 100.0,
                }
            ),
            fee_resolver=_FundingAwareFeeResolver(5.0, 20.0),
        )

        target = engine.build(
            self._context(product_type="derivatives", current_exposure_side="flat"),
            self._baseline(
                direction_bias="long",
                confidence=0.84,
                suggested_position_scale=0.8,
                volatility_target_scale=1.0,
            ).model_copy(update={"composite_alpha_score": 0.24}),
            self._ai_assessment(direction=0.24, confidence=0.86),
        )

        self.assertEqual(target.expected_cost_bps, 25.0)
        self.assertEqual(target.target_position_qty, Decimal("0"))
        self.assertIn("expected_edge_below_cost_buffer", target.guardrail_flags)

    def test_ai_primary_requires_override_and_actionable_edge(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "ai_operating_mode": "ai_primary",
                    "strategy_short_bias_enabled": True,
                    "ai_primary_min_confidence": 0.75,
                    "ai_primary_min_directional_edge": 0.2,
                }
            )
        )
        context = self._context(product_type="derivatives", current_exposure_side="flat")
        baseline = self._baseline(
            direction_bias="long",
            confidence=0.84,
            suggested_position_scale=0.8,
            volatility_target_scale=1.0,
        )

        target = engine.build(
            context,
            baseline,
            self._ai_assessment(direction=-0.4, confidence=0.88, fallback_used=False, override=False, actionable=False),
        )

        self.assertGreater(target.target_position_qty, 0.0)
        self.assertIsNotNone(target.decision_outcome)
        self.assertEqual(target.decision_outcome.decision_source, "baseline_fallback")
        self.assertIn("ai_override_not_recommended", target.decision_outcome.decision_blocked_reasons)

    def test_ai_primary_can_take_over_direction_when_all_gates_pass(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "ai_operating_mode": "ai_primary",
                    "strategy_short_bias_enabled": True,
                    "ai_primary_min_confidence": 0.75,
                    "ai_primary_min_directional_edge": 0.2,
                }
            )
        )
        context = self._context(product_type="derivatives", current_exposure_side="flat")
        baseline = self._baseline(
            direction_bias="long",
            confidence=0.84,
            suggested_position_scale=0.8,
            volatility_target_scale=1.0,
        )

        target = engine.build(
            context,
            baseline,
            self._ai_assessment(direction=-0.45, confidence=0.9, fallback_used=False, override=True, actionable=True),
        )

        self.assertLess(target.target_position_qty, 0.0)
        self.assertIsNotNone(target.decision_outcome)
        self.assertEqual(target.decision_outcome.decision_source, "ai")
        self.assertEqual(target.decision_outcome.ai_operating_mode, "ai_decision_maker")

    def test_canonical_ai_decision_maker_emits_native_decision_outcome(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "ai_operating_mode": "ai_decision_maker",
                    "strategy_short_bias_enabled": True,
                    "ai_primary_min_confidence": 0.75,
                    "ai_primary_min_directional_edge": 0.2,
                }
            )
        )
        context = self._context(product_type="derivatives", current_exposure_side="flat")
        baseline = self._baseline(
            direction_bias="long",
            confidence=0.84,
            suggested_position_scale=0.8,
            volatility_target_scale=1.0,
        )

        target = engine.build(
            context,
            baseline,
            self._ai_assessment(direction=-0.45, confidence=0.9, fallback_used=False, override=True, actionable=True),
            operating_mode="ai_decision_maker",
        )

        self.assertIsNotNone(target.decision_outcome)
        self.assertIsNotNone(target.ai_decision_intent)
        self.assertEqual(target.decision_outcome.ai_operating_mode, "ai_decision_maker")
        self.assertEqual(target.decision_outcome.decision_authority, "final_decision")
        self.assertEqual(target.decision_outcome.final_target_qty, target.target_position_qty)
        self.assertEqual(target.ai_decision_intent.direction, "short")
        self.assertEqual(target.ai_decision_intent.action, "enter")

    def test_canonical_ai_assisted_uses_advisory_authority(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.001,
                    "ai_operating_mode": "ai_assisted",
                }
            )
        )

        target = engine.build(
            self._context(),
            self._baseline(volatility_target_scale=1.0, suggested_position_scale=0.4),
            self._ai_assessment(direction=-0.4, confidence=0.88, fallback_used=False, override=True, actionable=True),
            operating_mode="ai_assisted",
        )

        self.assertIsNotNone(target.decision_outcome)
        self.assertEqual(target.decision_outcome.ai_operating_mode, "ai_assisted")
        self.assertEqual(target.decision_outcome.decision_authority, "advisory")
        self.assertEqual(target.decision_outcome.decision_source, "baseline")

    def test_legacy_ai_blended_preserves_consistency_filter_behavior(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.001,
                    "ai_operating_mode": "ai_blended",
                }
            )
        )

        target = engine.build(
            self._context(),
            self._baseline(volatility_target_scale=1.0, suggested_position_scale=0.4, direction_bias="long"),
            self._ai_assessment(direction=-0.4, confidence=0.88, fallback_used=False, override=True, actionable=True),
            operating_mode="ai_blended",
        )

        self.assertEqual(target.target_position_qty, Decimal("0"))
        self.assertIsNotNone(target.decision_outcome)
        self.assertIn("ai_consistency_filter_blocked", target.decision_outcome.decision_blocked_reasons)
        self.assertEqual(target.decision_outcome.ai_operating_mode, "ai_assisted")

    def test_ai_decision_maker_uses_baseline_only_as_fallback_source_when_ai_blocked(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "ai_operating_mode": "ai_decision_maker",
                    "strategy_short_bias_enabled": True,
                    "ai_primary_min_confidence": 0.75,
                    "ai_primary_min_directional_edge": 0.2,
                }
            )
        )
        context = self._context(product_type="derivatives", current_exposure_side="flat")
        baseline = self._baseline(
            direction_bias="long",
            confidence=0.84,
            suggested_position_scale=0.8,
            volatility_target_scale=1.0,
        )

        target = engine.build(
            context,
            baseline,
            self._ai_assessment(direction=-0.45, confidence=0.9, fallback_used=False, override=False, actionable=False),
            operating_mode="ai_decision_maker",
        )

        self.assertIsNotNone(target.ai_decision_intent)
        self.assertIsNotNone(target.decision_outcome)
        self.assertEqual(target.decision_outcome.decision_source, "baseline_fallback")
        self.assertIn("ai_override_not_recommended", target.decision_outcome.decision_blocked_reasons)
        self.assertGreater(target.target_position_qty, Decimal("0"))

    def test_shadow_build_uses_canonical_ai_decision_maker_path(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "ai_operating_mode": "ai_decision_maker",
                    "strategy_short_bias_enabled": True,
                    "ai_primary_min_confidence": 0.75,
                    "ai_primary_min_directional_edge": 0.2,
                }
            )
        )
        context = self._context(product_type="derivatives", current_exposure_side="flat")
        baseline = self._baseline(
            direction_bias="long",
            confidence=0.84,
            suggested_position_scale=0.8,
            volatility_target_scale=1.0,
        )
        actual_target = engine.build(
            context,
            baseline,
            self._ai_assessment(direction=0.3, confidence=0.88, fallback_used=False, override=True, actionable=True),
            operating_mode="baseline_only",
        )

        shadow = engine.build_shadow(
            context=context,
            baseline=baseline,
            ai_assessment=self._ai_assessment(direction=-0.45, confidence=0.9, fallback_used=False, override=True, actionable=True),
            actual_target=actual_target,
            operating_mode="ai_decision_maker",
        )

        self.assertTrue(shadow.would_override_baseline)
        self.assertIn(shadow.shadow_action_type, {"entry_override", "exit_override", "reverse_override"})

    def test_ai_decision_maker_with_profile_control_emits_native_profile_control_decision(self) -> None:
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "default_order_qty": 0.01,
                    "trading_product_type": "derivatives",
                    "ai_operating_mode": "ai_decision_maker_with_profile_control",
                    "strategy_short_bias_enabled": True,
                    "ai_primary_min_confidence": 0.75,
                    "ai_primary_min_directional_edge": 0.2,
                }
            )
        )
        context = self._context(product_type="derivatives", current_exposure_side="flat")
        baseline = self._baseline(
            direction_bias="long",
            confidence=0.84,
            suggested_position_scale=0.8,
            volatility_target_scale=1.0,
        )
        profile_control = ProfileControlDecision(
            decision_id=context.decision_id,
            requested_by="ai",
            requested_profile_id="trend_strict",
            current_profile_id="trend_normal",
            applied=True,
            blocked_reasons=[],
            decision_reason_codes=["ai_profile_adjustment_accepted"],
        )

        target = engine.build(
            context,
            baseline,
            self._ai_assessment(direction=-0.45, confidence=0.9, fallback_used=False, override=True, actionable=True),
            profile_control_decision=profile_control,
            operating_mode="ai_decision_maker_with_profile_control",
        )

        self.assertIsNotNone(target.profile_control_decision)
        self.assertEqual(target.profile_control_decision.requested_profile_id, "trend_strict")
        self.assertIsNotNone(target.decision_outcome)
        self.assertEqual(target.decision_outcome.ai_operating_mode, "ai_decision_maker_with_profile_control")
        self.assertEqual(target.decision_outcome.decision_authority, "final_decision_with_profile_control")
        self.assertEqual(target.decision_outcome.active_profile_id, "trend_strict")
        self.assertEqual(target.decision_outcome.profile_control_source, "ai")

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
        current_long_position_qty: float | None = None,
        current_short_position_qty: float | None = None,
        product_type: str = "spot",
        current_exposure_side: str = "flat",
        current_position_opened_seconds_ago: int | None = None,
        last_position_closed_seconds_ago: int | None = None,
        current_long_leg_opened_seconds_ago: int | None = None,
        current_short_leg_opened_seconds_ago: int | None = None,
        last_long_leg_closed_seconds_ago: int | None = None,
        last_short_leg_closed_seconds_ago: int | None = None,
        latest_long_leg_fill_seconds_ago: int | None = None,
        latest_short_leg_fill_seconds_ago: int | None = None,
        recent_low_edge_trade_streak: int = 0,
        recent_low_edge_trade_seconds_ago: int | None = None,
        recent_closed_trade_count: int = 0,
        recent_fee_drag_ratio: float = 0.0,
        recent_churn_ratio: float = 0.0,
        leg_strategy_health: dict[str, dict[str, object]] | None = None,
    ) -> DecisionContext:
        now = utc_now()
        derived_long_qty = (
            current_position_qty
            if current_long_position_qty is None and current_position_qty > 0
            else (0.0 if current_long_position_qty is None else current_long_position_qty)
        )
        derived_short_qty = (
            abs(current_position_qty)
            if current_short_position_qty is None and current_position_qty < 0
            else (0.0 if current_short_position_qty is None else current_short_position_qty)
        )
        health_payload = leg_strategy_health or {
            "long": {
                "recent_closed_trade_count": 0,
                "recent_win_rate": 0.0,
                "recent_fee_drag_ratio": 0.0,
                "recent_churn_ratio": 0.0,
                "recent_low_edge_trade_streak": 0,
                "recent_low_edge_trade_at": None,
                "recent_net_realized_pnl": Decimal("0"),
            },
            "short": {
                "recent_closed_trade_count": 0,
                "recent_win_rate": 0.0,
                "recent_fee_drag_ratio": 0.0,
                "recent_churn_ratio": 0.0,
                "recent_low_edge_trade_streak": 0,
                "recent_low_edge_trade_at": None,
                "recent_net_realized_pnl": Decimal("0"),
            },
        }
        return DecisionContext(
            decision_id="decision_target_test",
            symbol="BTC-USDT",
            timeframe="15m",
            as_of_ts=now,
            market_snapshot_ref="evt_market",
            feature_snapshot_ref="evt_feature",
            portfolio_snapshot_ref="evt_portfolio",
            health_snapshot_ref="evt_health",
            mode="paper_live",
            current_position_qty=Decimal(str(current_position_qty)),
            current_net_position_qty=Decimal(str(current_position_qty)),
            current_long_position_qty=Decimal(str(derived_long_qty)),
            current_short_position_qty=Decimal(str(derived_short_qty)),
            product_type=product_type,  # type: ignore[arg-type]
            current_exposure_side=current_exposure_side,  # type: ignore[arg-type]
            current_target_leverage=1.0,
            current_position_opened_at=(
                now - timedelta(seconds=current_position_opened_seconds_ago)
                if current_position_opened_seconds_ago is not None
                else None
            ),
            last_position_closed_at=(
                now - timedelta(seconds=last_position_closed_seconds_ago)
                if last_position_closed_seconds_ago is not None
                else None
            ),
            current_long_leg_opened_at=(
                now - timedelta(seconds=current_long_leg_opened_seconds_ago)
                if current_long_leg_opened_seconds_ago is not None
                else None
            ),
            current_short_leg_opened_at=(
                now - timedelta(seconds=current_short_leg_opened_seconds_ago)
                if current_short_leg_opened_seconds_ago is not None
                else None
            ),
            last_long_leg_closed_at=(
                now - timedelta(seconds=last_long_leg_closed_seconds_ago)
                if last_long_leg_closed_seconds_ago is not None
                else None
            ),
            last_short_leg_closed_at=(
                now - timedelta(seconds=last_short_leg_closed_seconds_ago)
                if last_short_leg_closed_seconds_ago is not None
                else None
            ),
            latest_long_leg_fill_timestamp=(
                now - timedelta(seconds=latest_long_leg_fill_seconds_ago)
                if latest_long_leg_fill_seconds_ago is not None
                else None
            ),
            latest_short_leg_fill_timestamp=(
                now - timedelta(seconds=latest_short_leg_fill_seconds_ago)
                if latest_short_leg_fill_seconds_ago is not None
                else None
            ),
            recent_low_edge_trade_streak=recent_low_edge_trade_streak,
            recent_low_edge_trade_at=(
                now - timedelta(seconds=recent_low_edge_trade_seconds_ago)
                if recent_low_edge_trade_seconds_ago is not None
                else None
            ),
            recent_closed_trade_count=recent_closed_trade_count,
            recent_fee_drag_ratio=recent_fee_drag_ratio,
            recent_churn_ratio=recent_churn_ratio,
            leg_strategy_health=health_payload,
            strategy_guardrail_flags=[
                *(
                    ["min_hold_active"]
                    if current_position_opened_seconds_ago is not None and current_position_opened_seconds_ago < 900
                    else []
                ),
                *(
                    ["post_close_cooldown_active"]
                    if last_position_closed_seconds_ago is not None and last_position_closed_seconds_ago < 600
                    else []
                ),
                *(
                    ["low_edge_cooldown_active"]
                    if recent_low_edge_trade_seconds_ago is not None and recent_low_edge_trade_streak >= 3
                    else []
                ),
            ],
            strategy_cooldowns={},
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
    def _ai_assessment(
        *,
        direction: float = 0.1,
        confidence: float = 0.7,
        fallback_used: bool = True,
        override: bool = False,
        actionable: bool = False,
    ) -> AIMarketAssessment:
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
            fallback_used=fallback_used,
            fallback_reason="baseline_only_mode",
            degraded=False,
            calibrated_confidence=confidence,
            baseline_override_recommended=override,
            override_reason_codes=["ai_override"] if override else [],
            economically_actionable=actionable,
            estimated_edge_bps=45.0 if actionable else 4.0,
            estimated_cost_bps=12.0,
            estimated_net_edge_bps=33.0 if actionable else -8.0,
            source_mode="provider" if not fallback_used else "fallback",
            execution_condition="normal",
            model_name="none",
            model_version="1",
            prompt_version="1",
        )


if __name__ == "__main__":
    unittest.main()
