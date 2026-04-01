from __future__ import annotations

import unittest

from aats.services.strategy_engines.families.independent_family import (
    _score_stability_metrics,
    independent_book_score,
)
from aats.services.strategy_engines.independent.scoring import (
    compute_raw_book_score,
    compute_score_stability,
)
from tests.support.strategy_family import make_ai_assessment, make_baseline, make_derivatives_hedge_settings


class TestIndependentScoring(unittest.TestCase):
    def test_compute_raw_book_score_matches_legacy_wrapper_and_fixture(self) -> None:
        settings = make_derivatives_hedge_settings(strategy_short_bias_enabled=True)
        baseline = make_baseline(
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
        ai_assessment = make_ai_assessment(direction=0.25, confidence=0.82)

        extracted = compute_raw_book_score(
            settings=settings,
            leg="long",
            baseline=baseline,
            ai_assessment=ai_assessment,
        )
        legacy = independent_book_score(
            settings=settings,
            leg="long",
            baseline=baseline,
            ai_assessment=ai_assessment,
        )

        self.assertAlmostEqual(extracted, legacy, places=10)
        self.assertAlmostEqual(extracted, 0.4402, places=4)

    def test_compute_score_stability_matches_legacy_wrapper(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_min_confirm_ticks=2,
            strategy_hedge_independent_min_score_stability_bps=10.0,
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.80,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={"momentum_alpha": 0.35, "trend_alpha": 0.22, "microstructure_alpha": 0.10},
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.24})
        ai_assessment = make_ai_assessment(direction=0.18, confidence=0.76)

        extracted = compute_score_stability(
            settings=settings,
            leg="long",
            score=0.78,
            entry_threshold=0.60,
            baseline=baseline,
            ai_assessment=ai_assessment,
            recent_score_history=(0.74, 0.76),
        )
        legacy = _score_stability_metrics(
            settings=settings,
            leg="long",
            score=0.78,
            entry_threshold=0.60,
            baseline=baseline,
            ai_assessment=ai_assessment,
            recent_score_history=(0.74, 0.76),
        )

        self.assertEqual(extracted, legacy)
        self.assertEqual(extracted.support_count, 3)
        self.assertEqual(extracted.source, "recent_target_history")
        self.assertTrue(extracted.stable)

    def test_compute_score_stability_honors_effective_min_confirm_ticks_override(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_min_confirm_ticks=2,
            strategy_hedge_independent_min_score_stability_bps=2.0,
        )
        baseline = make_baseline(
            direction_bias="short",
            confidence=0.78,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": -0.35,
                "trend_alpha": -0.24,
                "microstructure_alpha": -0.12,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": -0.26})

        default_metrics = compute_score_stability(
            settings=settings,
            leg="short",
            score=0.304,
            entry_threshold=0.30,
            baseline=baseline,
            ai_assessment=None,
            recent_score_history=(0.286,),
        )
        relaxed_metrics = compute_score_stability(
            settings=settings,
            leg="short",
            score=0.304,
            entry_threshold=0.30,
            baseline=baseline,
            ai_assessment=None,
            recent_score_history=(0.286,),
            min_confirm_ticks=1,
        )

        self.assertEqual(default_metrics.support_count, 1)
        self.assertFalse(default_metrics.stable)
        self.assertEqual(relaxed_metrics.support_count, 1)
        self.assertTrue(relaxed_metrics.stable)
        self.assertEqual(relaxed_metrics.source, "recent_target_history")


if __name__ == "__main__":
    unittest.main()
