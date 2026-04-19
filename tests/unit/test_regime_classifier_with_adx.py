"""P2.9 — RegimeClassifier.classify_with_adx 契约.

锁定:
  1. adx / +di / -di 任一 None → 退化到旧 classify() (保证 state 未 ready 的安全)
  2. ADX >= trend_threshold → trend 或 breakout (依 volatility)
  3. ADX < range_threshold + volatility 非 high → range
  4. 20 <= ADX < 25 → uncertain
  5. +DI > -DI → long，反之 short, 相等 → flat
"""

from __future__ import annotations

import unittest

from aats.services.feature_engine.regime import RegimeClassifier


class RegimeClassifierWithADXTests(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = RegimeClassifier(
            adx_trend_threshold=25.0,
            adx_range_threshold=20.0,
        )

    def _base_kwargs(self, **overrides):
        defaults = dict(
            momentum_15m=0.002, momentum_1h=0.003,
            trend_strength_15m=0.2, trend_strength_1h=0.25,
            volatility_state_15m="medium", volatility_state_1h="medium",
            liquidity_score=0.7,
        )
        defaults.update(overrides)
        return defaults

    def test_adx_none_falls_back_to_legacy_classify(self) -> None:
        """adx None → classify_with_adx 退化到 classify()."""
        result = self.classifier.classify_with_adx(
            adx=None, plus_di=None, minus_di=None,
            **self._base_kwargs(),
        )
        # 应该返回一个合法 regime，不 raise
        self.assertIn(result.regime_indicator, {"trend", "range", "breakout", "uncertain"})

    def test_high_adx_trend_long_bias(self) -> None:
        result = self.classifier.classify_with_adx(
            adx=35.0, plus_di=28.0, minus_di=12.0,
            **self._base_kwargs(volatility_state_15m="medium"),
        )
        self.assertEqual(result.regime_indicator, "trend")
        self.assertEqual(result.trend_bias, "long")
        self.assertGreater(result.regime_confidence, 0.5)

    def test_high_adx_high_vol_aligned_breakout(self) -> None:
        result = self.classifier.classify_with_adx(
            adx=40.0, plus_di=32.0, minus_di=8.0,
            **self._base_kwargs(
                momentum_15m=0.005, momentum_1h=0.007,
                volatility_state_15m="high",
            ),
        )
        self.assertEqual(result.regime_indicator, "breakout")
        self.assertEqual(result.trend_bias, "long")

    def test_low_adx_range(self) -> None:
        result = self.classifier.classify_with_adx(
            adx=15.0, plus_di=18.0, minus_di=16.0,
            **self._base_kwargs(
                momentum_15m=0.0005, momentum_1h=-0.0003,
                volatility_state_15m="low",
                liquidity_score=0.7,
            ),
        )
        self.assertEqual(result.regime_indicator, "range")
        self.assertEqual(result.trend_bias, "flat")

    def test_adx_between_thresholds_uncertain(self) -> None:
        """20 <= ADX < 25 → uncertain (既不强趋势也不明显震荡)."""
        result = self.classifier.classify_with_adx(
            adx=22.0, plus_di=20.0, minus_di=18.0,
            **self._base_kwargs(),
        )
        self.assertEqual(result.regime_indicator, "uncertain")

    def test_short_direction_when_minus_di_dominates(self) -> None:
        result = self.classifier.classify_with_adx(
            adx=32.0, plus_di=12.0, minus_di=28.0,
            **self._base_kwargs(momentum_15m=-0.004, momentum_1h=-0.005),
        )
        self.assertEqual(result.regime_indicator, "trend")
        self.assertEqual(result.trend_bias, "short")

    def test_custom_thresholds_applied(self) -> None:
        """配置更严格阈值 (trend=35) → ADX=30 不再是 trend 而是 uncertain."""
        strict = RegimeClassifier(adx_trend_threshold=35.0, adx_range_threshold=15.0)
        result = strict.classify_with_adx(
            adx=30.0, plus_di=22.0, minus_di=15.0,
            **self._base_kwargs(),
        )
        self.assertNotEqual(result.regime_indicator, "trend")


if __name__ == "__main__":
    unittest.main()
