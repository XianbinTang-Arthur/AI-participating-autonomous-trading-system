"""Unit tests for backtest CostValidator.

覆盖：
    - 空 summary 边界
    - 单记录无翻转
    - cost_diff sign 语义（actual 更高 → 正；actual 更低 → 负）
    - 翻负 / 翻正 / 稳定同号的计数
    - percentile（p50 / p95）与 max（绝对值最大保留原 sign）
    - diagnostics 不可变性（tuple 类型 + 外部赋值不影响内部）

所有 float assert 走 assertAlmostEqual(places=4)。
"""

from __future__ import annotations

import unittest

from aats.data_platform.replay.backtest.cost_validator import (
    CostDiagnostic,
    CostValidationSummary,
    CostValidator,
)


class EmptySummaryTests(unittest.TestCase):
    def test_empty_summary(self) -> None:
        validator = CostValidator()
        summary = validator.summary()

        self.assertIsInstance(summary, CostValidationSummary)
        self.assertEqual(summary.total_decisions, 0)
        self.assertEqual(summary.decisions_with_fills, 0)
        self.assertAlmostEqual(summary.avg_cost_diff_bps, 0.0, places=4)
        self.assertAlmostEqual(summary.max_cost_diff_bps, 0.0, places=4)
        self.assertEqual(summary.flipped_negative_count, 0)
        self.assertEqual(summary.flipped_positive_count, 0)
        self.assertEqual(summary.stable_sign_count, 0)
        self.assertAlmostEqual(summary.p50_cost_diff_bps, 0.0, places=4)
        self.assertAlmostEqual(summary.p95_cost_diff_bps, 0.0, places=4)
        self.assertEqual(validator.diagnostics, ())


class SingleRecordTests(unittest.TestCase):
    def test_single_record_no_flip(self) -> None:
        validator = CostValidator()
        diag = validator.record(
            decision_id="bar-1",
            assumed_cost_bps=5.0,
            actual_cost_bps=5.5,
            assumed_net_edge_bps=10.0,
        )

        # diag 字段
        self.assertEqual(diag.decision_id, "bar-1")
        self.assertAlmostEqual(diag.assumed_cost_bps, 5.0, places=4)
        self.assertAlmostEqual(diag.actual_cost_bps, 5.5, places=4)
        self.assertAlmostEqual(diag.cost_diff_bps, 0.5, places=4)
        self.assertAlmostEqual(diag.assumed_net_edge_bps, 10.0, places=4)
        # 10 - 0.5 = 9.5
        self.assertAlmostEqual(diag.actual_net_edge_bps, 9.5, places=4)
        self.assertFalse(diag.edge_flipped_negative)

        # summary
        summary = validator.summary()
        self.assertEqual(summary.total_decisions, 1)
        self.assertEqual(summary.decisions_with_fills, 1)
        self.assertAlmostEqual(summary.avg_cost_diff_bps, 0.5, places=4)
        self.assertEqual(summary.flipped_negative_count, 0)
        self.assertEqual(summary.flipped_positive_count, 0)
        self.assertEqual(summary.stable_sign_count, 1)


class CostDiffSignTests(unittest.TestCase):
    def test_cost_diff_positive_when_actual_higher(self) -> None:
        validator = CostValidator()
        diag = validator.record(
            decision_id="a",
            assumed_cost_bps=3.0,
            actual_cost_bps=7.0,
            assumed_net_edge_bps=5.0,
        )
        # actual - assumed = 4.0
        self.assertAlmostEqual(diag.cost_diff_bps, 4.0, places=4)
        # actual_net = 5.0 - 4.0 = 1.0
        self.assertAlmostEqual(diag.actual_net_edge_bps, 1.0, places=4)
        self.assertFalse(diag.edge_flipped_negative)

    def test_cost_diff_negative_when_actual_lower(self) -> None:
        validator = CostValidator()
        diag = validator.record(
            decision_id="b",
            assumed_cost_bps=7.0,
            actual_cost_bps=3.0,
            assumed_net_edge_bps=5.0,
        )
        # actual - assumed = -4.0
        self.assertAlmostEqual(diag.cost_diff_bps, -4.0, places=4)
        # actual_net = 5.0 - (-4.0) = 9.0
        self.assertAlmostEqual(diag.actual_net_edge_bps, 9.0, places=4)
        self.assertFalse(diag.edge_flipped_negative)


class FlipSemanticsTests(unittest.TestCase):
    def test_flip_negative_counted(self) -> None:
        """assumed_net_edge=2 → actual_net_edge=-1 should be flipped negative."""
        validator = CostValidator()
        diag = validator.record(
            decision_id="flip-neg",
            assumed_cost_bps=4.0,
            actual_cost_bps=7.0,
            assumed_net_edge_bps=2.0,
        )
        # cost_diff = 3.0; actual_net_edge = 2.0 - 3.0 = -1.0
        self.assertAlmostEqual(diag.actual_net_edge_bps, -1.0, places=4)
        self.assertTrue(diag.edge_flipped_negative)

        summary = validator.summary()
        self.assertEqual(summary.flipped_negative_count, 1)
        self.assertEqual(summary.flipped_positive_count, 0)
        self.assertEqual(summary.stable_sign_count, 0)

    def test_flip_positive_counted(self) -> None:
        """assumed_net_edge=-1 → actual_net_edge=+2 should be flipped positive."""
        validator = CostValidator()
        diag = validator.record(
            decision_id="flip-pos",
            assumed_cost_bps=7.0,
            actual_cost_bps=4.0,
            assumed_net_edge_bps=-1.0,
        )
        # cost_diff = -3.0; actual_net_edge = -1.0 - (-3.0) = 2.0
        self.assertAlmostEqual(diag.actual_net_edge_bps, 2.0, places=4)
        self.assertFalse(diag.edge_flipped_negative)  # was negative, not flipping to negative

        summary = validator.summary()
        self.assertEqual(summary.flipped_negative_count, 0)
        self.assertEqual(summary.flipped_positive_count, 1)
        self.assertEqual(summary.stable_sign_count, 0)

    def test_stable_sign_when_both_positive(self) -> None:
        validator = CostValidator()
        validator.record(
            decision_id="s1",
            assumed_cost_bps=2.0,
            actual_cost_bps=3.0,
            assumed_net_edge_bps=10.0,
        )
        summary = validator.summary()
        # 10 - 1 = 9, still positive
        self.assertEqual(summary.flipped_negative_count, 0)
        self.assertEqual(summary.flipped_positive_count, 0)
        self.assertEqual(summary.stable_sign_count, 1)

    def test_stable_sign_when_both_negative(self) -> None:
        validator = CostValidator()
        validator.record(
            decision_id="s2",
            assumed_cost_bps=5.0,
            actual_cost_bps=4.0,
            assumed_net_edge_bps=-3.0,
        )
        # cost_diff = -1.0; actual_net = -3.0 - (-1.0) = -2.0 (still negative)
        summary = validator.summary()
        self.assertEqual(summary.flipped_negative_count, 0)
        self.assertEqual(summary.flipped_positive_count, 0)
        self.assertEqual(summary.stable_sign_count, 1)


class PercentileTests(unittest.TestCase):
    def test_p50_p95_percentiles(self) -> None:
        """Feed 20 diffs; assert p50/p95 hit sorted[10] and sorted[19]."""
        validator = CostValidator()
        # assumed_cost=0 for convenience so actual_cost == cost_diff
        # 20 increasing diffs: 0.0, 1.0, 2.0, ..., 19.0
        for i in range(20):
            validator.record(
                decision_id=f"d{i}",
                assumed_cost_bps=0.0,
                actual_cost_bps=float(i),
                assumed_net_edge_bps=100.0,  # high enough to never flip
            )

        summary = validator.summary()
        # sorted diffs = [0, 1, 2, ..., 19]
        # p50 index = int(20 * 0.5) = 10 → value 10.0
        self.assertAlmostEqual(summary.p50_cost_diff_bps, 10.0, places=4)
        # p95 index = int(20 * 0.95) = 19 → value 19.0
        self.assertAlmostEqual(summary.p95_cost_diff_bps, 19.0, places=4)
        # avg = sum(0..19)/20 = 190/20 = 9.5
        self.assertAlmostEqual(summary.avg_cost_diff_bps, 9.5, places=4)

    def test_max_cost_diff_preserves_sign(self) -> None:
        """Input diffs [-5, 2, 3] → max by absolute value = -5 (negative)."""
        validator = CostValidator()
        # 构造 actual_cost = diff，assumed_cost = 0
        for i, diff in enumerate([-5.0, 2.0, 3.0]):
            validator.record(
                decision_id=f"m{i}",
                assumed_cost_bps=0.0,
                actual_cost_bps=diff,
                assumed_net_edge_bps=100.0,
            )

        summary = validator.summary()
        self.assertAlmostEqual(summary.max_cost_diff_bps, -5.0, places=4)


class AverageTests(unittest.TestCase):
    def test_avg_cost_diff_with_simple_values(self) -> None:
        validator = CostValidator()
        # diffs = [1, 2, 3, 4] → avg = 2.5
        for i, diff in enumerate([1.0, 2.0, 3.0, 4.0]):
            validator.record(
                decision_id=f"a{i}",
                assumed_cost_bps=0.0,
                actual_cost_bps=diff,
                assumed_net_edge_bps=100.0,
            )

        summary = validator.summary()
        self.assertAlmostEqual(summary.avg_cost_diff_bps, 2.5, places=4)
        self.assertEqual(summary.total_decisions, 4)


class DiagnosticsImmutabilityTests(unittest.TestCase):
    def test_diagnostics_property_immutable(self) -> None:
        """diagnostics returns a tuple; external mutation attempts do nothing to internal state."""
        validator = CostValidator()
        validator.record(
            decision_id="i1",
            assumed_cost_bps=1.0,
            actual_cost_bps=2.0,
            assumed_net_edge_bps=5.0,
        )

        diags = validator.diagnostics
        self.assertIsInstance(diags, tuple)
        self.assertEqual(len(diags), 1)
        self.assertIsInstance(diags[0], CostDiagnostic)

        # tuple 本身不可变：试图调用 append 应当抛 AttributeError
        with self.assertRaises(AttributeError):
            diags.append(  # type: ignore[attr-defined]
                CostDiagnostic(
                    decision_id="x",
                    assumed_cost_bps=0.0,
                    actual_cost_bps=0.0,
                    cost_diff_bps=0.0,
                    assumed_net_edge_bps=0.0,
                    actual_net_edge_bps=0.0,
                    edge_flipped_negative=False,
                )
            )

        # 再次记录 → 内部 list 增长，外部 tuple 保持原样（快照语义）
        validator.record(
            decision_id="i2",
            assumed_cost_bps=1.0,
            actual_cost_bps=2.0,
            assumed_net_edge_bps=5.0,
        )
        self.assertEqual(len(diags), 1)  # 原快照未变
        self.assertEqual(len(validator.diagnostics), 2)  # 新 tuple 反映最新状态

    def test_diagnostic_frozen_cannot_assign(self) -> None:
        """CostDiagnostic is frozen — attribute assignment raises."""
        validator = CostValidator()
        diag = validator.record(
            decision_id="f1",
            assumed_cost_bps=1.0,
            actual_cost_bps=2.0,
            assumed_net_edge_bps=5.0,
        )
        with self.assertRaises((AttributeError, TypeError)):
            diag.actual_cost_bps = 999.0  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
