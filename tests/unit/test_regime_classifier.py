from __future__ import annotations

import unittest

from aats.services.feature_engine.regime import RegimeClassifier


class TestRegimeClassifier(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = RegimeClassifier()

    def test_classifies_trend(self) -> None:
        result = self.classifier.classify(
            momentum_15m=0.003,
            momentum_1h=0.008,
            trend_strength_15m=0.32,
            trend_strength_1h=0.41,
            volatility_state_15m="medium",
            volatility_state_1h="medium",
            liquidity_score=0.8,
        )

        self.assertEqual(result.regime_indicator, "trend")
        self.assertEqual(result.trend_bias, "long")
        self.assertGreater(result.regime_confidence, 0.6)

    def test_classifies_range(self) -> None:
        result = self.classifier.classify(
            momentum_15m=0.0005,
            momentum_1h=-0.0004,
            trend_strength_15m=0.05,
            trend_strength_1h=-0.03,
            volatility_state_15m="low",
            volatility_state_1h="low",
            liquidity_score=0.7,
        )

        self.assertEqual(result.regime_indicator, "range")
        self.assertEqual(result.trend_bias, "flat")

    def test_classifies_breakout(self) -> None:
        result = self.classifier.classify(
            momentum_15m=0.006,
            momentum_1h=0.009,
            trend_strength_15m=0.5,
            trend_strength_1h=0.45,
            volatility_state_15m="high",
            volatility_state_1h="medium",
            liquidity_score=0.6,
        )

        self.assertEqual(result.regime_indicator, "breakout")
        self.assertEqual(result.trend_bias, "long")

    def test_classifies_uncertain(self) -> None:
        result = self.classifier.classify(
            momentum_15m=0.002,
            momentum_1h=-0.003,
            trend_strength_15m=0.18,
            trend_strength_1h=-0.22,
            volatility_state_15m="medium",
            volatility_state_1h="high",
            liquidity_score=0.4,
        )

        self.assertEqual(result.regime_indicator, "uncertain")
        self.assertIn(result.trend_bias, {"mixed", "long", "short"})


if __name__ == "__main__":
    unittest.main()
