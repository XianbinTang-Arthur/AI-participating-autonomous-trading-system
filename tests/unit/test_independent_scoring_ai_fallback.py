"""三档 AI 运行模式下 independent 评分行为的锁定测试。

对应改动: scoring.py 引入 _pick_ai_mode() + 三组 composite 权重 + 三组 legacy bonus 系数。
对应设计: Mode A (baseline_only / AI=None) / Mode B (ai_assisted) / Mode C (ai_decision_maker)。
"""

from __future__ import annotations

import unittest

from aats.data_platform.replay.adapters.independent_adapter import (
    _W_ALPHA as _REPLAY_W_ALPHA,
    _W_CONFIDENCE as _REPLAY_W_CONFIDENCE,
    _W_MICRO as _REPLAY_W_MICRO,
    _W_MOMENTUM as _REPLAY_W_MOMENTUM,
    _W_TREND as _REPLAY_W_TREND,
)
from aats.services.strategy_engines.independent.scoring import (
    _MODE_A_BONUS_AI,
    _MODE_A_BONUS_MICRO,
    _MODE_A_BONUS_MOMENTUM,
    _MODE_A_BONUS_TREND,
    _MODE_A_W_AI,
    _MODE_A_W_ALPHA,
    _MODE_A_W_CONFIDENCE,
    _MODE_A_W_MICRO,
    _MODE_A_W_MOMENTUM,
    _MODE_A_W_TREND,
    _MODE_B_BONUS_AI,
    _MODE_B_BONUS_MICRO,
    _MODE_B_BONUS_MOMENTUM,
    _MODE_B_BONUS_TREND,
    _MODE_B_W_AI,
    _MODE_B_W_ALPHA,
    _MODE_B_W_CONFIDENCE,
    _MODE_B_W_MICRO,
    _MODE_B_W_MOMENTUM,
    _MODE_B_W_TREND,
    _MODE_C_BONUS_AI,
    _MODE_C_BONUS_MICRO,
    _MODE_C_BONUS_MOMENTUM,
    _MODE_C_BONUS_TREND,
    _MODE_C_W_AI,
    _MODE_C_W_ALPHA,
    _MODE_C_W_CONFIDENCE,
    _MODE_C_W_MICRO,
    _MODE_C_W_MOMENTUM,
    _MODE_C_W_TREND,
    _pick_ai_mode,
    _signal_confirmation_count,
    compute_raw_book_score,
    compute_signal_edge_bps,
)
from tests.support.strategy_family import (
    make_ai_assessment,
    make_baseline,
    make_derivatives_hedge_settings,
)


def _fixture_baseline_strong_long():
    """多头方向、特征派生信号全部较强的 baseline，用于算分基准。"""
    return make_baseline(
        direction_bias="long",
        confidence=0.82,
        suggested_position_scale=1.0,
        volatility_target_scale=1.0,
        factor_scores={
            "momentum_alpha": 0.48,
            "trend_alpha": 0.42,
            "microstructure_alpha": 0.18,
            "liquidity_scale": 0.95,
        },
    ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.32})


class TestPickAIMode(unittest.TestCase):
    """_pick_ai_mode 触发判断：ai_assessment 缺失 + 四种运行模式枚举 + 未知值兜底。"""

    def test_ai_assessment_none_always_returns_mode_a(self):
        """AI 挂了（assessment 为 None）无条件 fallback，即使配置的 mode 是 ai_decision_maker。"""
        for mode in ("baseline_only", "ai_assisted", "ai_decision_maker"):
            settings = make_derivatives_hedge_settings(ai_operating_mode=mode)
            picked = _pick_ai_mode(settings=settings, ai_assessment=None)
            self.assertEqual(picked, "MODE_A", f"mode={mode} with None assessment must fallback to MODE_A")

    def test_baseline_only_returns_mode_a(self):
        settings = make_derivatives_hedge_settings(ai_operating_mode="baseline_only")
        ai = make_ai_assessment(direction=0.3)
        self.assertEqual(_pick_ai_mode(settings=settings, ai_assessment=ai), "MODE_A")

    def test_ai_assisted_returns_mode_b(self):
        settings = make_derivatives_hedge_settings(ai_operating_mode="ai_assisted")
        ai = make_ai_assessment(direction=0.3)
        self.assertEqual(_pick_ai_mode(settings=settings, ai_assessment=ai), "MODE_B")

    def test_ai_decision_maker_returns_mode_c(self):
        settings = make_derivatives_hedge_settings(ai_operating_mode="ai_decision_maker")
        ai = make_ai_assessment(direction=0.3)
        self.assertEqual(_pick_ai_mode(settings=settings, ai_assessment=ai), "MODE_C")

    def test_legacy_ai_blended_normalizes_to_ai_assisted(self):
        """历史遗留值 ai_blended 映射到 ai_assisted，走 Mode B。"""
        settings = make_derivatives_hedge_settings(ai_operating_mode="ai_blended")
        ai = make_ai_assessment(direction=0.3)
        self.assertEqual(_pick_ai_mode(settings=settings, ai_assessment=ai), "MODE_B")

    def test_legacy_ai_advisory_normalizes_to_ai_assisted(self):
        """历史遗留值 ai_advisory 映射到 ai_assisted，走 Mode B。"""
        settings = make_derivatives_hedge_settings(ai_operating_mode="ai_advisory")
        ai = make_ai_assessment(direction=0.3)
        self.assertEqual(_pick_ai_mode(settings=settings, ai_assessment=ai), "MODE_B")

    def test_legacy_ai_primary_normalizes_to_ai_decision_maker(self):
        """历史遗留值 ai_primary 映射到 ai_decision_maker，走 Mode C。"""
        settings = make_derivatives_hedge_settings(ai_operating_mode="ai_primary")
        ai = make_ai_assessment(direction=0.3)
        self.assertEqual(_pick_ai_mode(settings=settings, ai_assessment=ai), "MODE_C")


class TestCompositeWeightsSum(unittest.TestCase):
    """三档 composite 权重 Σ = 1.0 必须严格成立。"""

    def test_mode_a_weights_sum_to_one(self):
        total = (
            _MODE_A_W_ALPHA + _MODE_A_W_AI + _MODE_A_W_MOMENTUM
            + _MODE_A_W_TREND + _MODE_A_W_MICRO + _MODE_A_W_CONFIDENCE
        )
        self.assertAlmostEqual(total, 1.0, places=9)

    def test_mode_b_weights_sum_to_one(self):
        total = (
            _MODE_B_W_ALPHA + _MODE_B_W_AI + _MODE_B_W_MOMENTUM
            + _MODE_B_W_TREND + _MODE_B_W_MICRO + _MODE_B_W_CONFIDENCE
        )
        self.assertAlmostEqual(total, 1.0, places=9)

    def test_mode_c_weights_sum_to_one(self):
        total = (
            _MODE_C_W_ALPHA + _MODE_C_W_AI + _MODE_C_W_MOMENTUM
            + _MODE_C_W_TREND + _MODE_C_W_MICRO + _MODE_C_W_CONFIDENCE
        )
        self.assertAlmostEqual(total, 1.0, places=9)

    def test_mode_c_is_original_production_weights(self):
        """Mode C 必须保持原始生产公式权重（回归保护）。"""
        self.assertEqual(_MODE_C_W_ALPHA, 0.28)
        self.assertEqual(_MODE_C_W_AI, 0.26)
        self.assertEqual(_MODE_C_W_MOMENTUM, 0.16)
        self.assertEqual(_MODE_C_W_TREND, 0.12)
        self.assertEqual(_MODE_C_W_MICRO, 0.08)
        self.assertEqual(_MODE_C_W_CONFIDENCE, 0.10)


class TestCompositeModeAReplaySync(unittest.TestCase):
    """Mode A 权重必须与 RDP replay adapter 严格同步。任一侧改变这个测试会红。"""

    def test_mode_a_alpha_matches_replay(self):
        self.assertEqual(_MODE_A_W_ALPHA, _REPLAY_W_ALPHA)

    def test_mode_a_momentum_matches_replay(self):
        self.assertEqual(_MODE_A_W_MOMENTUM, _REPLAY_W_MOMENTUM)

    def test_mode_a_trend_matches_replay(self):
        self.assertEqual(_MODE_A_W_TREND, _REPLAY_W_TREND)

    def test_mode_a_micro_matches_replay(self):
        self.assertEqual(_MODE_A_W_MICRO, _REPLAY_W_MICRO)

    def test_mode_a_confidence_matches_replay(self):
        self.assertEqual(_MODE_A_W_CONFIDENCE, _REPLAY_W_CONFIDENCE)

    def test_mode_a_ai_weight_is_zero(self):
        self.assertEqual(_MODE_A_W_AI, 0.0)


class TestCompositeModeBInterpolation(unittest.TestCase):
    """Mode B 是 Mode A 和 Mode C 之间的线性插值（factor = 16/26）。

    Mode B[i] = Mode C[i] + (Mode A[i] - Mode C[i]) × (16/26)

    容差: round 到 4 位小数可能有 1e-4 误差；alpha 做了 +0.0001 补齐。
    """

    FACTOR = 16.0 / 26.0  # 节省 AI 0.16 / 原 AI 权重 0.26

    def _expected(self, a: float, c: float) -> float:
        return c + (a - c) * self.FACTOR

    def test_mode_b_ai_is_zero_point_one(self):
        self.assertEqual(_MODE_B_W_AI, 0.10)

    def test_mode_b_alpha_near_interpolation(self):
        exact = self._expected(_MODE_A_W_ALPHA, _MODE_C_W_ALPHA)
        # alpha 做了 +0.0001 补齐 Σ=1，容许 1e-4 偏差
        self.assertAlmostEqual(_MODE_B_W_ALPHA, exact, places=3)

    def test_mode_b_momentum_interpolation(self):
        self.assertAlmostEqual(
            _MODE_B_W_MOMENTUM,
            self._expected(_MODE_A_W_MOMENTUM, _MODE_C_W_MOMENTUM),
            places=4,
        )

    def test_mode_b_trend_interpolation(self):
        self.assertAlmostEqual(
            _MODE_B_W_TREND,
            self._expected(_MODE_A_W_TREND, _MODE_C_W_TREND),
            places=4,
        )

    def test_mode_b_micro_interpolation(self):
        self.assertAlmostEqual(
            _MODE_B_W_MICRO,
            self._expected(_MODE_A_W_MICRO, _MODE_C_W_MICRO),
            places=4,
        )

    def test_mode_b_confidence_interpolation(self):
        self.assertAlmostEqual(
            _MODE_B_W_CONFIDENCE,
            self._expected(_MODE_A_W_CONFIDENCE, _MODE_C_W_CONFIDENCE),
            places=4,
        )


class TestComputeRawBookScoreThreeTier(unittest.TestCase):
    """compute_raw_book_score 在三档下使用对应权重组。"""

    def test_mode_c_matches_original_formula(self):
        """Mode C（ai_decision_maker）的算分结果必须与原始公式逐位相同。"""
        settings = make_derivatives_hedge_settings(ai_operating_mode="ai_decision_maker")
        baseline = _fixture_baseline_strong_long()
        ai = make_ai_assessment(direction=0.25, confidence=0.82)

        actual = compute_raw_book_score(
            settings=settings, leg="long", baseline=baseline, ai_assessment=ai,
        )

        # 手工展开原公式
        alpha_c = 0.32  # composite_alpha_score
        ai_c = 0.25  # directional_edge
        momentum_c = 0.48
        trend_c = 0.42
        micro_c = 0.18
        conf_c = 0.82
        expected = (
            alpha_c * 0.28 + ai_c * 0.26 + momentum_c * 0.16
            + trend_c * 0.12 + micro_c * 0.08 + conf_c * 0.10
        )
        # regime=trend, direction_bias=long, volatility_state=medium → direction_bias bonus=+0.06
        expected += 0.06

        self.assertAlmostEqual(actual, expected, places=9)

    def test_mode_a_baseline_only_uses_fallback_weights(self):
        """Mode A（baseline_only）的算分结果必须使用 Mode A 权重（AI 项不参与）。"""
        settings = make_derivatives_hedge_settings(ai_operating_mode="baseline_only")
        baseline = _fixture_baseline_strong_long()
        ai = make_ai_assessment(direction=0.25, confidence=0.82)

        actual = compute_raw_book_score(
            settings=settings, leg="long", baseline=baseline, ai_assessment=ai,
        )

        # Mode A 公式：AI 项完全消失
        expected = (
            0.32 * _MODE_A_W_ALPHA + 0.48 * _MODE_A_W_MOMENTUM
            + 0.42 * _MODE_A_W_TREND + 0.18 * _MODE_A_W_MICRO
            + 0.82 * _MODE_A_W_CONFIDENCE
        )
        expected += 0.06  # direction_bias=long

        self.assertAlmostEqual(actual, expected, places=9)

    def test_mode_a_ai_none_uses_fallback_weights(self):
        """ai_assessment=None 时（即使 mode=ai_decision_maker）走 Mode A。"""
        settings = make_derivatives_hedge_settings(ai_operating_mode="ai_decision_maker")
        baseline = _fixture_baseline_strong_long()

        actual = compute_raw_book_score(
            settings=settings, leg="long", baseline=baseline, ai_assessment=None,
        )

        expected = (
            0.32 * _MODE_A_W_ALPHA + 0.48 * _MODE_A_W_MOMENTUM
            + 0.42 * _MODE_A_W_TREND + 0.18 * _MODE_A_W_MICRO
            + 0.82 * _MODE_A_W_CONFIDENCE
        )
        expected += 0.06

        self.assertAlmostEqual(actual, expected, places=9)

    def test_mode_b_ai_assisted_uses_low_weight_formula(self):
        """Mode B（ai_assisted）使用 AI=0.10 的低权重公式。"""
        settings = make_derivatives_hedge_settings(ai_operating_mode="ai_assisted")
        baseline = _fixture_baseline_strong_long()
        ai = make_ai_assessment(direction=0.25, confidence=0.82)

        actual = compute_raw_book_score(
            settings=settings, leg="long", baseline=baseline, ai_assessment=ai,
        )

        expected = (
            0.32 * _MODE_B_W_ALPHA + 0.25 * _MODE_B_W_AI + 0.48 * _MODE_B_W_MOMENTUM
            + 0.42 * _MODE_B_W_TREND + 0.18 * _MODE_B_W_MICRO + 0.82 * _MODE_B_W_CONFIDENCE
        )
        expected += 0.06

        self.assertAlmostEqual(actual, expected, places=9)

    def test_three_modes_produce_different_scores(self):
        """同一 baseline + AI，三档应产出三种不同的评分（顺序 A < B < C，因 AI 方向为正）。"""
        baseline = _fixture_baseline_strong_long()
        ai = make_ai_assessment(direction=0.25, confidence=0.82)

        scores = {}
        for mode, tag in [
            ("baseline_only", "MODE_A"),
            ("ai_assisted", "MODE_B"),
            ("ai_decision_maker", "MODE_C"),
        ]:
            settings = make_derivatives_hedge_settings(ai_operating_mode=mode)
            scores[tag] = compute_raw_book_score(
                settings=settings, leg="long", baseline=baseline, ai_assessment=ai,
            )

        # Mode A 没有 AI 项但其他权重放大，Mode C 有 AI 贡献但其他权重低
        # 本 fixture 中 AI=0.25（较强），所以 Mode C > Mode B > Mode A
        # 具体数值关系不 hard-code，仅断言 "三档不相等"
        self.assertNotAlmostEqual(scores["MODE_A"], scores["MODE_B"], places=4)
        self.assertNotAlmostEqual(scores["MODE_B"], scores["MODE_C"], places=4)
        self.assertNotAlmostEqual(scores["MODE_A"], scores["MODE_C"], places=4)


class TestLegacyBonusCoefs(unittest.TestCase):
    """Legacy bonus 系数常量的数学关系：
    - ai_coef 按 (AI权重/0.26) 缩放
    - 节省 pool (20 - ai_coef) 按 25:15:12 比例补偿到三个 bonus
    """

    def test_mode_c_bonus_is_original_values(self):
        self.assertEqual(_MODE_C_BONUS_MICRO, 25.0)
        self.assertEqual(_MODE_C_BONUS_MOMENTUM, 15.0)
        self.assertEqual(_MODE_C_BONUS_TREND, 12.0)
        self.assertEqual(_MODE_C_BONUS_AI, 20.0)

    def test_mode_a_ai_coef_is_zero(self):
        self.assertEqual(_MODE_A_BONUS_AI, 0.0)

    def test_mode_b_ai_coef_scaled_by_ten_over_twenty_six(self):
        expected = 20.0 * (0.10 / 0.26)
        self.assertAlmostEqual(_MODE_B_BONUS_AI, expected, places=3)

    def test_mode_a_compensation_matches_ratio(self):
        """Mode A 应按 25:15:12 比例把节省的 20 bps 分给三个 bonus。"""
        saved = 20.0
        total_ratio = 25.0 + 15.0 + 12.0  # 52
        self.assertAlmostEqual(_MODE_A_BONUS_MICRO, 25.0 + saved * 25.0 / total_ratio, places=3)
        self.assertAlmostEqual(_MODE_A_BONUS_MOMENTUM, 15.0 + saved * 15.0 / total_ratio, places=3)
        self.assertAlmostEqual(_MODE_A_BONUS_TREND, 12.0 + saved * 12.0 / total_ratio, places=3)

    def test_mode_b_compensation_matches_ratio(self):
        """Mode B 应按同样比例分配节省的 (20 - 7.692) ≈ 12.308 bps。"""
        saved = 20.0 - _MODE_B_BONUS_AI
        total_ratio = 25.0 + 15.0 + 12.0
        self.assertAlmostEqual(_MODE_B_BONUS_MICRO, 25.0 + saved * 25.0 / total_ratio, places=3)
        self.assertAlmostEqual(_MODE_B_BONUS_MOMENTUM, 15.0 + saved * 15.0 / total_ratio, places=3)
        self.assertAlmostEqual(_MODE_B_BONUS_TREND, 12.0 + saved * 12.0 / total_ratio, places=3)


class TestComputeSignalEdgeBpsLegacyBranch(unittest.TestCase):
    """Legacy component_edge 分支（rdp_scale is None 时）使用三档系数。"""

    def _common_baseline_ai(self):
        """产生 directional_alpha=0.5, micro=0.30, momentum=0.20, trend=0.15 的 fixture。"""
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.80,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.20,
                "trend_alpha": 0.15,
                "microstructure_alpha": 0.30,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.50})
        ai = make_ai_assessment(direction=0.25, confidence=0.82)
        return baseline, ai

    def test_mode_c_legacy_matches_original_bonus_values(self):
        """Mode C legacy 路径必须复现原始 bonus 系数 (25/15/12/20) 的计算结果。"""
        settings = make_derivatives_hedge_settings(
            ai_operating_mode="ai_decision_maker",
            strategy_signal_edge_scale_bps=None,
            strategy_alpha_edge_bps_scale=100.0,
        )
        baseline, ai = self._common_baseline_ai()
        actual = compute_signal_edge_bps(
            settings=settings, baseline=baseline, ai_assessment=ai, leg="long",
        )

        # 手工展开原公式
        expected = (
            0.50 * 100.0  # alpha_edge
            + max(0.30 - 0.08, 0) * 25.0  # micro_bonus
            + max(0.20 - 0.08, 0) * 15.0  # momentum_bonus
            + max(0.15 - 0.08, 0) * 12.0  # trend_bonus
            + max(0.25 - 0.10, 0) * 20.0  # ai_bonus
        )
        self.assertAlmostEqual(actual, expected, places=6)

    def test_mode_a_legacy_ai_bonus_zero_others_boosted(self):
        settings = make_derivatives_hedge_settings(
            ai_operating_mode="baseline_only",
            strategy_signal_edge_scale_bps=None,
            strategy_alpha_edge_bps_scale=100.0,
        )
        baseline, ai = self._common_baseline_ai()
        actual = compute_signal_edge_bps(
            settings=settings, baseline=baseline, ai_assessment=ai, leg="long",
        )

        expected = (
            0.50 * 100.0  # alpha_edge 不变
            + max(0.30 - 0.08, 0) * _MODE_A_BONUS_MICRO
            + max(0.20 - 0.08, 0) * _MODE_A_BONUS_MOMENTUM
            + max(0.15 - 0.08, 0) * _MODE_A_BONUS_TREND
            + max(0.25 - 0.10, 0) * _MODE_A_BONUS_AI  # = 0
        )
        self.assertAlmostEqual(actual, expected, places=6)

    def test_mode_b_legacy_uses_assisted_bonus_coefs(self):
        settings = make_derivatives_hedge_settings(
            ai_operating_mode="ai_assisted",
            strategy_signal_edge_scale_bps=None,
            strategy_alpha_edge_bps_scale=100.0,
        )
        baseline, ai = self._common_baseline_ai()
        actual = compute_signal_edge_bps(
            settings=settings, baseline=baseline, ai_assessment=ai, leg="long",
        )

        expected = (
            0.50 * 100.0
            + max(0.30 - 0.08, 0) * _MODE_B_BONUS_MICRO
            + max(0.20 - 0.08, 0) * _MODE_B_BONUS_MOMENTUM
            + max(0.15 - 0.08, 0) * _MODE_B_BONUS_TREND
            + max(0.25 - 0.10, 0) * _MODE_B_BONUS_AI
        )
        self.assertAlmostEqual(actual, expected, places=6)

    def test_three_modes_signal_edge_within_tight_band(self):
        """三档 signal_edge 在相同输入下数量级应该接近（补偿机制有效性）。"""
        baseline, ai = self._common_baseline_ai()
        results = {}
        for mode in ("baseline_only", "ai_assisted", "ai_decision_maker"):
            settings = make_derivatives_hedge_settings(
                ai_operating_mode=mode,
                strategy_signal_edge_scale_bps=None,
                strategy_alpha_edge_bps_scale=100.0,
            )
            results[mode] = compute_signal_edge_bps(
                settings=settings, baseline=baseline, ai_assessment=ai, leg="long",
            )

        # 三档最大差异 < 5 bps（补偿机制让 AI 开关不造成突变）
        spread = max(results.values()) - min(results.values())
        self.assertLess(spread, 5.0, f"Three-mode signal_edge spread too large: {results}")

    def test_rdp_path_used_when_scale_configured(self):
        """当 strategy_signal_edge_scale_bps > 0 时走 RDP 路径（composite_score × scale），不走 legacy。"""
        settings = make_derivatives_hedge_settings(
            ai_operating_mode="ai_decision_maker",
            strategy_signal_edge_scale_bps=20.0,
            strategy_alpha_edge_bps_scale=100.0,
        )
        baseline, ai = self._common_baseline_ai()
        actual = compute_signal_edge_bps(
            settings=settings, baseline=baseline, ai_assessment=ai, leg="long",
        )

        composite = compute_raw_book_score(
            settings=settings, leg="long", baseline=baseline, ai_assessment=ai,
        )
        self.assertAlmostEqual(actual, composite * 20.0, places=6)


class TestSignalConfirmationCountThreeTier(unittest.TestCase):
    """_signal_confirmation_count 三档行为: Mode A 4 项，Mode B/C 5 项。"""

    def _strong_baseline(self):
        """全部 4 个特征派生信号都 >= 0.08 的 baseline。"""
        return make_baseline(
            direction_bias="long",
            confidence=0.80,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.20,
                "trend_alpha": 0.15,
                "microstructure_alpha": 0.30,
            },
        ).model_copy(update={"composite_alpha_score": 0.40})

    def test_mode_c_ai_strong_returns_five(self):
        """Mode C + AI edge >= 0.10 应返回 5（全部确认）。"""
        settings = make_derivatives_hedge_settings(ai_operating_mode="ai_decision_maker")
        baseline = self._strong_baseline()
        ai = make_ai_assessment(direction=0.25)

        self.assertEqual(
            _signal_confirmation_count(
                settings=settings, leg="long", baseline=baseline, ai_assessment=ai,
            ),
            5,
        )

    def test_mode_b_ai_strong_returns_five(self):
        """Mode B 保留 AI 投票位。"""
        settings = make_derivatives_hedge_settings(ai_operating_mode="ai_assisted")
        baseline = self._strong_baseline()
        ai = make_ai_assessment(direction=0.25)

        self.assertEqual(
            _signal_confirmation_count(
                settings=settings, leg="long", baseline=baseline, ai_assessment=ai,
            ),
            5,
        )

    def test_mode_a_max_is_four(self):
        """Mode A 没有 AI 投票位，全部信号最多 4 个。"""
        settings = make_derivatives_hedge_settings(ai_operating_mode="baseline_only")
        baseline = self._strong_baseline()
        ai = make_ai_assessment(direction=0.99)  # AI edge 很大也不会算进去

        result = _signal_confirmation_count(
            settings=settings, leg="long", baseline=baseline, ai_assessment=ai,
        )
        self.assertEqual(result, 4)

    def test_mode_a_ai_none_max_is_four(self):
        """ai_assessment=None + 任何 mode 都走 Mode A 4 项语义。"""
        settings = make_derivatives_hedge_settings(ai_operating_mode="ai_decision_maker")
        baseline = self._strong_baseline()

        result = _signal_confirmation_count(
            settings=settings, leg="long", baseline=baseline, ai_assessment=None,
        )
        self.assertEqual(result, 4)

    def test_mode_c_weak_ai_returns_four(self):
        """Mode C + AI edge < 0.10 → AI 投反对票，返回 4（仅特征信号通过）。"""
        settings = make_derivatives_hedge_settings(ai_operating_mode="ai_decision_maker")
        baseline = self._strong_baseline()
        ai = make_ai_assessment(direction=0.05)  # 低于 0.10 阈值

        self.assertEqual(
            _signal_confirmation_count(
                settings=settings, leg="long", baseline=baseline, ai_assessment=ai,
            ),
            4,
        )


class TestModeAFallbackRecoversEntryCapability(unittest.TestCase):
    """端到端：Mode A fallback 公式下，强信号 baseline 仍能跨过 0.66 入场阈值。"""

    def test_baseline_only_strong_signals_score_above_entry_threshold(self):
        """证明 AI 关闭时 independent 不会被"静默降级"锁死。

        使用"非常强但合理"的 baseline 信号组合（alpha/momentum/trend 均 >=0.55 +
        高波动 regime bonus + direction_bias 命中），验证 Mode A 公式仍能跨过
        0.66 入场阈值。这是修复前"静默降级 bug"的最关键防回归锁定。
        """
        settings = make_derivatives_hedge_settings(ai_operating_mode="baseline_only")
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.90,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            volatility_state="high",
            factor_scores={
                "momentum_alpha": 0.65,
                "trend_alpha": 0.60,
                "microstructure_alpha": 0.40,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.65})

        score = compute_raw_book_score(
            settings=settings, leg="long", baseline=baseline, ai_assessment=None,
        )

        # Mode A 下，强信号组合应该能达 >= 0.66（独立家族入场阈值）
        self.assertGreater(score, 0.66, f"Mode A strong-signal score {score:.4f} failed to clear 0.66 threshold")


class TestShortLegThreeTierSymmetry(unittest.TestCase):
    """short leg 三档行为对称性：side_sign=-1 时三档仍正确分流，且 short_bias_enabled=False 早退优先级高于 mode 选择。"""

    def _short_baseline(self):
        """多空翻转的 baseline：所有 alpha 符号取反，direction_bias=short。"""
        return make_baseline(
            direction_bias="short",
            confidence=0.82,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": -0.48,
                "trend_alpha": -0.42,
                "microstructure_alpha": -0.18,
                "liquidity_scale": 0.95,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": -0.32})

    def test_short_bias_disabled_returns_zero_regardless_of_mode(self):
        """leg=short 且 short_bias_enabled=False 时，无论何种 mode 都立即返回 0（早退优先）。"""
        baseline = self._short_baseline()
        ai = make_ai_assessment(direction=-0.25, confidence=0.82)
        for mode in ("baseline_only", "ai_assisted", "ai_decision_maker"):
            settings = make_derivatives_hedge_settings(
                strategy_short_bias_enabled=False,
                ai_operating_mode=mode,
            )
            score = compute_raw_book_score(
                settings=settings, leg="short", baseline=baseline, ai_assessment=ai,
            )
            self.assertEqual(score, 0.0, f"mode={mode} short-bias-off must early-return 0")

    def test_short_mode_a_uses_fallback_weights(self):
        """short leg + Mode A 下，side_sign=-1 应让所有负 alpha 转为正分量后按 Mode A 权重算。"""
        settings = make_derivatives_hedge_settings(
            strategy_short_bias_enabled=True,
            ai_operating_mode="baseline_only",
        )
        baseline = self._short_baseline()

        actual = compute_raw_book_score(
            settings=settings, leg="short", baseline=baseline, ai_assessment=None,
        )

        # side_sign=-1 * -0.32 = 0.32 (alpha), 等类似所有 short 特征都转成正分量
        expected = (
            0.32 * _MODE_A_W_ALPHA + 0.48 * _MODE_A_W_MOMENTUM
            + 0.42 * _MODE_A_W_TREND + 0.18 * _MODE_A_W_MICRO
            + 0.82 * _MODE_A_W_CONFIDENCE
        )
        expected += 0.06  # direction_bias=short == leg=short

        self.assertAlmostEqual(actual, expected, places=9)

    def test_short_three_modes_produce_different_scores(self):
        """short leg 下三档同样产出不同分数（与 long leg 对称）。"""
        baseline = self._short_baseline()
        ai = make_ai_assessment(direction=-0.25, confidence=0.82)

        scores = {}
        for mode, tag in [
            ("baseline_only", "MODE_A"),
            ("ai_assisted", "MODE_B"),
            ("ai_decision_maker", "MODE_C"),
        ]:
            settings = make_derivatives_hedge_settings(
                strategy_short_bias_enabled=True,
                ai_operating_mode=mode,
            )
            scores[tag] = compute_raw_book_score(
                settings=settings, leg="short", baseline=baseline, ai_assessment=ai,
            )

        self.assertNotAlmostEqual(scores["MODE_A"], scores["MODE_B"], places=4)
        self.assertNotAlmostEqual(scores["MODE_B"], scores["MODE_C"], places=4)
        self.assertNotAlmostEqual(scores["MODE_A"], scores["MODE_C"], places=4)

    def test_short_signal_confirmation_count_three_tier(self):
        """short leg 下 _signal_confirmation_count 的三档行为（Mode A=4, B/C 含 AI 票=5）。"""
        baseline = self._short_baseline()
        # AI 方向为负（看空），对 short leg 而言 side_sign=-1 → directional_edge 正分量 = 0.25
        ai = make_ai_assessment(direction=-0.25)

        settings_a = make_derivatives_hedge_settings(
            strategy_short_bias_enabled=True, ai_operating_mode="baseline_only",
        )
        settings_c = make_derivatives_hedge_settings(
            strategy_short_bias_enabled=True, ai_operating_mode="ai_decision_maker",
        )

        count_a = _signal_confirmation_count(
            settings=settings_a, leg="short", baseline=baseline, ai_assessment=ai,
        )
        count_c = _signal_confirmation_count(
            settings=settings_c, leg="short", baseline=baseline, ai_assessment=ai,
        )
        self.assertEqual(count_a, 4, "Mode A short leg must cap at 4 confirmations")
        self.assertEqual(count_c, 5, "Mode C short leg with strong AI must reach 5 confirmations")


class TestH4DirectionGating(unittest.TestCase):
    """H4 修复锁定测试：方向无关加项（confidence + regime_bonus + volatility_bonus）
    仅在 leg 与 baseline.direction_bias 对齐时计入。

    参见:
      - docs/design/h4_confidence_direction_gating_2026_04_19.md
      - docs/review/short_leg_asymmetry_root_cause_2026_04_19.md §5 方案 A.1
    """

    def _baseline_long_bias_with_all_bonuses_eligible(self):
        """direction_bias=long, regime=range, volatility_state=high → 所有方向无关加项都应触发。"""
        return make_baseline(
            direction_bias="long",
            confidence=0.80,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            volatility_state="high",
            factor_scores={
                "momentum_alpha": 0.30,
                "trend_alpha": 0.20,
                "microstructure_alpha": 0.10,
            },
        ).model_copy(update={"regime": "range", "composite_alpha_score": 0.40})

    def test_misaligned_short_leg_has_zero_confidence_contribution(self):
        """direction_bias=long, leg=short → leg_aligned=False，confidence 项应为 0。"""
        settings = make_derivatives_hedge_settings(
            strategy_short_bias_enabled=True,
            ai_operating_mode="baseline_only",
        )
        baseline = self._baseline_long_bias_with_all_bonuses_eligible()

        # leg=short 但 direction_bias=long → 所有方向相关项 = 0（被 max(0, -α)=0 过滤）
        #   （因为 composite_alpha=+0.40，short 方向 side_sign * α = -0.40 → clamp max(0,...) = 0）
        # 所以整个 score 应该 = 0（方向相关 = 0 + confidence_contribution = 0 + regime_bonus 门控
        # = 0 + direction_bias_bonus（short != long）= 0 + volatility_bonus 门控 = 0）
        actual = compute_raw_book_score(
            settings=settings, leg="short", baseline=baseline, ai_assessment=None,
        )
        self.assertAlmostEqual(actual, 0.0, places=9,
                               msg="misaligned short leg should produce 0 score (all direction-agnostic "
                                   "terms gated, all direction-aware terms 0 via max(0,-α))")

    def test_misaligned_long_leg_with_short_bias_has_zero_confidence_contribution(self):
        """direction_bias=short, leg=long → leg_aligned=False，confidence 项应为 0。"""
        settings = make_derivatives_hedge_settings(
            strategy_short_bias_enabled=True,
            ai_operating_mode="baseline_only",
        )
        # 翻转方向: direction_bias=short, composite_alpha 为负
        baseline = make_baseline(
            direction_bias="short",
            confidence=0.80,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            volatility_state="high",
            factor_scores={
                "momentum_alpha": -0.30,
                "trend_alpha": -0.20,
                "microstructure_alpha": -0.10,
            },
        ).model_copy(update={"regime": "range", "composite_alpha_score": -0.40})

        # leg=long 但 direction_bias=short → 方向相关项全 0，方向无关项被门控
        actual = compute_raw_book_score(
            settings=settings, leg="long", baseline=baseline, ai_assessment=None,
        )
        self.assertAlmostEqual(actual, 0.0, places=9,
                               msg="misaligned long leg should produce 0 score")

    def test_aligned_leg_preserves_full_confidence_and_bonuses(self):
        """direction_bias=long, leg=long, regime=range, vol=high → 全部加项都应触发。"""
        settings = make_derivatives_hedge_settings(
            strategy_short_bias_enabled=True,
            ai_operating_mode="baseline_only",
        )
        baseline = self._baseline_long_bias_with_all_bonuses_eligible()

        actual = compute_raw_book_score(
            settings=settings, leg="long", baseline=baseline, ai_assessment=None,
        )

        # 手工展开：confidence 全量 + regime_bonus + direction_bias_bonus + volatility_bonus 全部触发
        expected = (
            0.40 * _MODE_A_W_ALPHA        # alpha_component
            + 0.30 * _MODE_A_W_MOMENTUM    # momentum_component
            + 0.20 * _MODE_A_W_TREND       # trend_component
            + 0.10 * _MODE_A_W_MICRO       # micro_component
            + 0.80 * _MODE_A_W_CONFIDENCE  # confidence_component (aligned)
        )
        expected += 0.04  # regime_bonus (range + aligned)
        expected += 0.06  # direction_bias_bonus (long == long)
        expected += 0.03  # volatility_bonus (high + aligned)

        self.assertAlmostEqual(actual, expected, places=9,
                               msg="aligned leg must receive confidence + regime + direction + volatility bonuses")

    def test_misaligned_short_leg_no_contribution_across_all_modes(self):
        """三档 mode × direction_bias=long, leg=short → score 均应为 0（方向相关项=0 + 门控=0）。"""
        baseline = self._baseline_long_bias_with_all_bonuses_eligible()
        ai = make_ai_assessment(direction=0.25, confidence=0.82)

        for mode in ("baseline_only", "ai_assisted", "ai_decision_maker"):
            settings = make_derivatives_hedge_settings(
                strategy_short_bias_enabled=True,
                ai_operating_mode=mode,
            )
            actual = compute_raw_book_score(
                settings=settings, leg="short", baseline=baseline, ai_assessment=ai,
            )
            self.assertAlmostEqual(actual, 0.0, places=9,
                                   msg=f"mode={mode} misaligned short leg must produce 0 score "
                                       f"regardless of AI presence")


if __name__ == "__main__":
    unittest.main()
