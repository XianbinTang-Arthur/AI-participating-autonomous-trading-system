from __future__ import annotations

import unittest

from aats.services.strategy_engines.independent.replay import IndependentDecisionSnapshot
from aats.services.strategy_engines.independent.tuning import (
    IndependentScoreDrawdownSweepSample,
    score_drawdown_sample_from_decision_snapshot,
    summarize_score_drawdown_threshold_sweep,
)


class TestIndependentScoreDrawdownTuning(unittest.TestCase):
    def test_score_drawdown_sample_from_decision_snapshot_extracts_effective_threshold(self) -> None:
        sample = score_drawdown_sample_from_decision_snapshot(
            IndependentDecisionSnapshot(
                decision_id="decision_drawdown_extract",
                symbol="BTC-USDT-SWAP",
                leg="short",
                adjusted_score=0.43,
                score_stability_metrics={
                    "support_count": 3,
                    "downward_drawdown_bps": 4.0,
                },
                expectancy_snapshot={
                    "expected_net_edge_bps": 28.0,
                },
                threshold_snapshot={
                    "entry_threshold": 0.30,
                    "effective_entry_threshold": 0.34,
                },
            )
        )

        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertEqual(sample.entry_threshold, 0.34)
        self.assertEqual(sample.downward_drawdown_bps, 4.0)
        self.assertEqual(sample.expected_net_edge_bps, 28.0)

    def test_summarize_score_drawdown_threshold_sweep_reports_released_signals(self) -> None:
        summaries = summarize_score_drawdown_threshold_sweep(
            samples=(
                IndependentScoreDrawdownSweepSample(
                    decision_id="d1",
                    leg="short",
                    score=0.42,
                    entry_threshold=0.30,
                    support_count=3,
                    downward_drawdown_bps=0.0,
                    expected_net_edge_bps=20.0,
                ),
                IndependentScoreDrawdownSweepSample(
                    decision_id="d2",
                    leg="short",
                    score=0.41,
                    entry_threshold=0.30,
                    support_count=3,
                    downward_drawdown_bps=4.0,
                    expected_net_edge_bps=24.0,
                ),
                IndependentScoreDrawdownSweepSample(
                    decision_id="d3",
                    leg="short",
                    score=0.39,
                    entry_threshold=0.30,
                    support_count=3,
                    downward_drawdown_bps=9.0,
                    expected_net_edge_bps=36.0,
                ),
                IndependentScoreDrawdownSweepSample(
                    decision_id="d4",
                    leg="short",
                    score=0.28,
                    entry_threshold=0.30,
                    support_count=3,
                    downward_drawdown_bps=1.0,
                    expected_net_edge_bps=12.0,
                ),
                IndependentScoreDrawdownSweepSample(
                    decision_id="d5",
                    leg="short",
                    score=0.40,
                    entry_threshold=0.30,
                    support_count=1,
                    downward_drawdown_bps=2.0,
                    expected_net_edge_bps=18.0,
                ),
            ),
            thresholds_bps=(2.0, 5.0, 10.0),
            min_confirm_ticks=2,
            baseline_threshold_bps=2.0,
        )

        self.assertEqual([item.threshold_bps for item in summaries], [2.0, 5.0, 10.0])
        self.assertEqual(summaries[0].qualifying_signal_count, 4)
        self.assertEqual(summaries[0].support_ready_signal_count, 3)
        self.assertEqual(summaries[0].stable_signal_count, 1)
        self.assertEqual(summaries[0].blocked_by_drawdown_count, 2)
        self.assertEqual(summaries[0].released_vs_baseline_count, 0)
        self.assertEqual(summaries[1].stable_signal_count, 2)
        self.assertEqual(summaries[1].released_vs_baseline_count, 1)
        self.assertEqual(summaries[2].stable_signal_count, 3)
        self.assertEqual(summaries[2].released_vs_baseline_count, 2)
        self.assertAlmostEqual(summaries[2].stable_avg_expected_net_edge_bps or 0.0, 26.6666666667, places=6)


if __name__ == "__main__":
    unittest.main()
