from __future__ import annotations

import unittest
from decimal import Decimal

from aats.services.strategy_engines.families.independent_family import (
    _independent_close_reason,
    _independent_de_risk_target_qty,
    _independent_min_hold_remaining_seconds,
    _independent_thesis_age_seconds,
)
from aats.services.strategy_engines.independent.lifecycle import (
    catastrophic_failed_thesis_threshold_bps,
    compute_de_risk_target_qty,
    compute_thesis_age_seconds,
    determine_close_reason,
    is_catastrophic_failed_thesis,
    min_hold_remaining_seconds,
)
from tests.support.strategy_family import make_context, make_derivatives_hedge_settings


class TestIndependentLifecycle(unittest.TestCase):
    def test_compute_thesis_age_seconds_matches_legacy_wrapper(self) -> None:
        context = make_context(
            current_long_position_qty=0.02,
            current_exposure_side="long",
            current_long_leg_opened_seconds_ago=300,
        )

        extracted = compute_thesis_age_seconds(
            context=context,
            leg="long",
            current_qty=Decimal("0.02"),
        )
        legacy = _independent_thesis_age_seconds(
            context=context,
            leg="long",
            current_qty=Decimal("0.02"),
        )

        self.assertEqual(extracted, legacy)
        self.assertEqual(extracted, 300.0)

    def test_determine_close_reason_matches_legacy_failed_thesis_behavior(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_failed_thesis_net_edge_bps=-1.0,
            strategy_hedge_independent_de_risk_net_edge_bps=2.0,
        )

        extracted = determine_close_reason(
            settings=settings,
            score=0.70,
            close_threshold=0.50,
            expected_net_edge_bps=-2.0,
            liquidity_quality_score=0.95,
            execution_health_state="ok",
            age_seconds=120.0,
        )
        legacy = _independent_close_reason(
            settings=settings,
            score=0.70,
            close_threshold=0.50,
            expected_net_edge_bps=-2.0,
            liquidity_quality_score=0.95,
            execution_health_state="ok",
            age_seconds=120.0,
        )

        self.assertEqual(extracted, legacy)
        self.assertEqual(extracted, "failed_thesis")

    def test_compute_de_risk_target_qty_matches_legacy_wrapper(self) -> None:
        extracted = compute_de_risk_target_qty(
            current_qty=Decimal("0.08"),
            directional_leg_target_qty=Decimal("0.03"),
        )
        legacy = _independent_de_risk_target_qty(
            current_qty=Decimal("0.08"),
            directional_leg_target_qty=Decimal("0.03"),
        )
        self.assertEqual(extracted, legacy)
        self.assertEqual(extracted, Decimal("0.03"))

    def test_min_hold_remaining_seconds_matches_legacy_wrapper(self) -> None:
        settings = make_derivatives_hedge_settings(strategy_hedge_independent_long_min_hold_seconds=600.0)
        context = make_context(
            current_long_position_qty=0.02,
            current_exposure_side="long",
            current_long_leg_opened_seconds_ago=300,
        )

        extracted = min_hold_remaining_seconds(
            settings=settings,
            context=context,
            leg="long",
        )
        legacy = _independent_min_hold_remaining_seconds(
            settings=settings,
            context=context,
            leg="long",
        )

        self.assertEqual(extracted, legacy)
        self.assertEqual(extracted, 300.0)


class TestCatastrophicFailedThesis(unittest.TestCase):
    """测试 failed_thesis 的 whipsaw 防护逻辑。

    设计验证:
      - catastrophic 阈值 = failed_thesis_threshold - catastrophic_buffer
      - 仅当 net_edge <= catastrophic 阈值 时，才豁免 min_hold 紧急止损
      - 瞬时抖动触及标准 failed_thesis (但未跨越 catastrophic 缓冲) 应保持 min_hold 阻塞
    """

    def test_catastrophic_threshold_computation_default(self) -> None:
        """默认参数: failed_thesis=-1.0, buffer=3.0 → catastrophic=-4.0"""
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_failed_thesis_net_edge_bps=-1.0,
            strategy_hedge_independent_catastrophic_failed_thesis_buffer_bps=3.0,
        )
        threshold = catastrophic_failed_thesis_threshold_bps(settings=settings)
        self.assertAlmostEqual(threshold, -4.0, places=6)

    def test_catastrophic_threshold_custom_values(self) -> None:
        """自定义参数: failed_thesis=-2.0, buffer=5.0 → catastrophic=-7.0"""
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_failed_thesis_net_edge_bps=-2.0,
            strategy_hedge_independent_catastrophic_failed_thesis_buffer_bps=5.0,
        )
        threshold = catastrophic_failed_thesis_threshold_bps(settings=settings)
        self.assertAlmostEqual(threshold, -7.0, places=6)

    def test_catastrophic_threshold_zero_buffer(self) -> None:
        """buffer=0 时: catastrophic == failed_thesis（退化为无 whipsaw 防护）"""
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_failed_thesis_net_edge_bps=-1.0,
            strategy_hedge_independent_catastrophic_failed_thesis_buffer_bps=0.0,
        )
        threshold = catastrophic_failed_thesis_threshold_bps(settings=settings)
        self.assertAlmostEqual(threshold, -1.0, places=6)

    def test_is_catastrophic_returns_false_when_net_edge_missing(self) -> None:
        """没有 net_edge 数据时不判定为 catastrophic（保守处理，遵守 min_hold）"""
        settings = make_derivatives_hedge_settings()
        self.assertFalse(
            is_catastrophic_failed_thesis(
                settings=settings,
                expected_net_edge_bps=None,
            )
        )

    def test_is_catastrophic_false_for_transient_dip(self) -> None:
        """抗抖动: net_edge=-1.5 bps 触及 failed_thesis 但未跨 catastrophic 缓冲 → False"""
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_failed_thesis_net_edge_bps=-1.0,
            strategy_hedge_independent_catastrophic_failed_thesis_buffer_bps=3.0,
        )
        # 抖动情形：瞬时跌至 -1.5 bps，但尚未深度跌破
        self.assertFalse(
            is_catastrophic_failed_thesis(
                settings=settings,
                expected_net_edge_bps=-1.5,
            )
        )
        # 边界情形：刚过 failed_thesis 阈值
        self.assertFalse(
            is_catastrophic_failed_thesis(
                settings=settings,
                expected_net_edge_bps=-1.0,
            )
        )
        # 接近但仍未到 catastrophic 阈值
        self.assertFalse(
            is_catastrophic_failed_thesis(
                settings=settings,
                expected_net_edge_bps=-3.9,
            )
        )

    def test_is_catastrophic_true_for_deep_loss(self) -> None:
        """真正灾难: net_edge <= -4.0 bps → True，豁免 min_hold 立即止损"""
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_failed_thesis_net_edge_bps=-1.0,
            strategy_hedge_independent_catastrophic_failed_thesis_buffer_bps=3.0,
        )
        # 边界刚好
        self.assertTrue(
            is_catastrophic_failed_thesis(
                settings=settings,
                expected_net_edge_bps=-4.0,
            )
        )
        # 深度亏损
        self.assertTrue(
            is_catastrophic_failed_thesis(
                settings=settings,
                expected_net_edge_bps=-5.0,
            )
        )
        self.assertTrue(
            is_catastrophic_failed_thesis(
                settings=settings,
                expected_net_edge_bps=-10.0,
            )
        )

    def test_is_catastrophic_false_for_positive_net_edge(self) -> None:
        """正边际: 不应触发 catastrophic 判定"""
        settings = make_derivatives_hedge_settings()
        self.assertFalse(
            is_catastrophic_failed_thesis(
                settings=settings,
                expected_net_edge_bps=2.0,
            )
        )

    def test_is_catastrophic_respects_override_parameters(self) -> None:
        """支持运行时参数覆盖（测试灵活性）"""
        settings = make_derivatives_hedge_settings()
        # 覆盖 failed_thesis=-2.0, buffer=1.0 → catastrophic=-3.0
        self.assertTrue(
            is_catastrophic_failed_thesis(
                settings=settings,
                expected_net_edge_bps=-3.0,
                failed_thesis_net_edge_bps=-2.0,
                catastrophic_buffer_bps=1.0,
            )
        )
        self.assertFalse(
            is_catastrophic_failed_thesis(
                settings=settings,
                expected_net_edge_bps=-2.9,
                failed_thesis_net_edge_bps=-2.0,
                catastrophic_buffer_bps=1.0,
            )
        )

    def test_catastrophic_threshold_negative_buffer_treated_as_zero(self) -> None:
        """负 buffer 被 clamp 为 0（防御性编程）"""
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_failed_thesis_net_edge_bps=-1.0,
        )
        threshold = catastrophic_failed_thesis_threshold_bps(
            settings=settings,
            catastrophic_buffer_bps=-5.0,
        )
        self.assertAlmostEqual(threshold, -1.0, places=6)


if __name__ == "__main__":
    unittest.main()
