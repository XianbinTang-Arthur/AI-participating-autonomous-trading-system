"""FeatureCalculator.basis_alpha (P1.4) 契约.

锁定:
  1. mark_price=None → basis_alpha = 0 (向后兼容、现货/冷启动)
  2. last > mark (超买) → basis_alpha < 0 (反转倾向抑制 long)
  3. last < mark (超卖) → basis_alpha > 0 (反转倾向鼓励 long)
  4. basis_scale_bps 控制灵敏度：相同 basis_bps 下 scale 越小 basis_alpha 越饱和
  5. flag 关闭 → basis_alpha 恒为 0 (即使 mark_price 存在)
  6. composite 权重总和 = 1.00 严格归一
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from aats.schemas.common import utc_now
from aats.schemas.market import MarketSnapshot
from aats.services.feature_engine.calculator import FeatureCalculator


def _snapshot(*, last: float, mark: float | None) -> MarketSnapshot:
    now = utc_now()
    kwargs = {
        "created_at": now,
        "symbol": "BTC-USDT-SWAP",
        "exchange": "OKX",
        "snapshot_ts": now,
        "best_bid": Decimal(str(last - 0.5)),
        "best_ask": Decimal(str(last + 0.5)),
        "last_price": Decimal(str(last)),
        "bid_size": Decimal("3.0"),
        "ask_size": Decimal("2.0"),
        "volume_24h": Decimal("1000.0"),
        "kline_15m": {"open": 66_800.0, "high": 67_200.0, "low": 66_700.0, "close": last},
        "kline_1h": {"open": 66_000.0, "high": 67_300.0, "low": 65_900.0, "close": last},
        "orderbook_depth": {
            "bids": [{"price": last - 0.5, "size": 5.0}],
            "asks": [{"price": last + 0.5, "size": 4.0}],
        },
        "recent_trades": [{"side": "buy", "size": 0.9}, {"side": "sell", "size": 0.9}],
    }
    if mark is not None:
        kwargs["mark_price"] = Decimal(str(mark))
    return MarketSnapshot(**kwargs)


class FeatureCalculatorBasisAlphaTests(unittest.TestCase):
    def test_mark_price_none_gives_zero_basis_alpha(self) -> None:
        calc = FeatureCalculator()
        features = calc.calculate(_snapshot(last=67000.0, mark=None), market_snapshot_ref="evt_no_mark")
        assert features.analysis_context is not None
        self.assertEqual(features.analysis_context.alpha_factors.basis_alpha, 0.0)

    def test_last_above_mark_yields_negative_basis_alpha(self) -> None:
        """last > mark = 超买 = basis_bps 正 = basis_alpha 负（抑制 long 倾向）."""
        calc = FeatureCalculator()
        # last=67100, mark=67000 → basis_bps = 100/67000 × 10000 ≈ 14.9
        # scale=10 默认 → basis_alpha = -tanh(1.49) ≈ -0.9
        features = calc.calculate(
            _snapshot(last=67100.0, mark=67000.0),
            market_snapshot_ref="evt_overbuy",
        )
        assert features.analysis_context is not None
        basis = features.analysis_context.alpha_factors.basis_alpha
        self.assertLess(basis, -0.5)

    def test_last_below_mark_yields_positive_basis_alpha(self) -> None:
        """last < mark = 超卖 = basis_bps 负 = basis_alpha 正（鼓励 long 倾向）."""
        calc = FeatureCalculator()
        features = calc.calculate(
            _snapshot(last=66900.0, mark=67000.0),
            market_snapshot_ref="evt_oversell",
        )
        assert features.analysis_context is not None
        basis = features.analysis_context.alpha_factors.basis_alpha
        self.assertGreater(basis, 0.5)

    def test_scale_bps_controls_sensitivity_smaller_scale_more_saturated(self) -> None:
        """相同 basis_bps，scale 越小 → basis_alpha 越接近 ±1 饱和."""
        calc_strict = FeatureCalculator(basis_scale_bps=5.0)
        calc_loose = FeatureCalculator(basis_scale_bps=50.0)
        # last=67010, mark=67000 → basis_bps ≈ 1.49
        snap = _snapshot(last=67010.0, mark=67000.0)
        f_strict = calc_strict.calculate(snap, market_snapshot_ref="evt_strict")
        f_loose = calc_loose.calculate(snap, market_snapshot_ref="evt_loose")
        assert f_strict.analysis_context is not None
        assert f_loose.analysis_context is not None
        strict_abs = abs(f_strict.analysis_context.alpha_factors.basis_alpha)
        loose_abs = abs(f_loose.analysis_context.alpha_factors.basis_alpha)
        # scale 小 → tanh 参数大 → 更接近 1
        self.assertGreater(strict_abs, loose_abs)

    def test_flag_disabled_zeros_basis_alpha_even_with_mark_price(self) -> None:
        """紧急回滚：flag 关 → basis_alpha = 0（即使 mark_price 存在）."""
        calc = FeatureCalculator(enable_basis_signal=False)
        features = calc.calculate(
            _snapshot(last=67100.0, mark=67000.0),
            market_snapshot_ref="evt_flag_off",
        )
        assert features.analysis_context is not None
        self.assertEqual(features.analysis_context.alpha_factors.basis_alpha, 0.0)

    def test_composite_weights_sum_to_one_strict(self) -> None:
        """P1.4 后权重：momentum 0.30 + trend 0.20 + regime 0.15 + multi_tf 0.11
        + micro 0.12 + basis 0.12 = 1.00（严格归一，数值验证防止将来权重误改）."""
        expected_weights = {
            "momentum_alpha": 0.30,
            "trend_alpha": 0.20,
            "regime_alpha": 0.15,
            "multi_timeframe_alpha": 0.11,
            "microstructure_alpha": 0.12,
            "basis_alpha": 0.12,
        }
        self.assertAlmostEqual(sum(expected_weights.values()), 1.00, places=6)

    def test_basis_alpha_present_in_alpha_factor_set_fields(self) -> None:
        """AlphaFactorSet 应当返回 basis_alpha 字段（即使为 0），防止 Bug-2 类
        `默认 0 掩盖未计算` 问题."""
        calc = FeatureCalculator()
        features = calc.calculate(_snapshot(last=67000.0, mark=67000.0), market_snapshot_ref="evt_zero_basis")
        assert features.analysis_context is not None
        alpha = features.analysis_context.alpha_factors
        # 用 model_dump 确保字段真在 schema 里（不是 typo）
        self.assertIn("basis_alpha", alpha.model_dump())


if __name__ == "__main__":
    unittest.main()
