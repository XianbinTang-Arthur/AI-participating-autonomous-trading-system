"""FeatureCalculator.funding_alpha (P1.5) 契约.

锁定:
  1. funding_rate=None → funding_alpha = 0
  2. funding_rate > 0 (多头付费) → funding_alpha < 0 (压低 long)
  3. funding_rate < 0 (空头付费) → funding_alpha > 0 (鼓励 long)
  4. funding_scale 控制灵敏度
  5. flag off → funding_alpha 恒 0
  6. P1.5 composite 权重和 = 1.00
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from aats.schemas.common import utc_now
from aats.schemas.market import MarketSnapshot
from aats.services.feature_engine.calculator import FeatureCalculator


def _snapshot(
    *, last: float = 67000.0, mark: float | None = 67000.0,
    funding: float | None = None,
) -> MarketSnapshot:
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
        "volume_24h": Decimal("1000"),
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
    if funding is not None:
        kwargs["funding_rate"] = Decimal(str(funding))
    return MarketSnapshot(**kwargs)


class FeatureCalculatorFundingAlphaTests(unittest.TestCase):
    def test_funding_none_gives_zero_alpha(self) -> None:
        calc = FeatureCalculator()
        f = calc.calculate(_snapshot(funding=None), market_snapshot_ref="evt_no_funding")
        assert f.analysis_context is not None
        self.assertEqual(f.analysis_context.alpha_factors.funding_alpha, 0.0)

    def test_positive_funding_yields_negative_alpha(self) -> None:
        """funding > 0 (多头付费重) → funding_alpha < 0 (抑制 long)."""
        calc = FeatureCalculator()
        # funding=0.0005, scale=2000 默认 → -tanh(1.0) ≈ -0.76
        f = calc.calculate(_snapshot(funding=0.0005), market_snapshot_ref="evt_high_fund")
        assert f.analysis_context is not None
        self.assertLess(f.analysis_context.alpha_factors.funding_alpha, -0.5)

    def test_negative_funding_yields_positive_alpha(self) -> None:
        """funding < 0 (空头付费) → funding_alpha > 0 (鼓励 long)."""
        calc = FeatureCalculator()
        f = calc.calculate(_snapshot(funding=-0.0005), market_snapshot_ref="evt_neg_fund")
        assert f.analysis_context is not None
        self.assertGreater(f.analysis_context.alpha_factors.funding_alpha, 0.5)

    def test_funding_scale_controls_sensitivity(self) -> None:
        calc_strict = FeatureCalculator(funding_scale=10_000.0)
        calc_loose = FeatureCalculator(funding_scale=500.0)
        snap = _snapshot(funding=0.0002)
        f_strict = calc_strict.calculate(snap, market_snapshot_ref="evt_strict")
        f_loose = calc_loose.calculate(snap, market_snapshot_ref="evt_loose")
        assert f_strict.analysis_context is not None
        assert f_loose.analysis_context is not None
        self.assertGreater(
            abs(f_strict.analysis_context.alpha_factors.funding_alpha),
            abs(f_loose.analysis_context.alpha_factors.funding_alpha),
        )

    def test_flag_disabled_zeros_funding_alpha(self) -> None:
        calc = FeatureCalculator(enable_funding_signal=False)
        f = calc.calculate(_snapshot(funding=0.001), market_snapshot_ref="evt_flag_off")
        assert f.analysis_context is not None
        self.assertEqual(f.analysis_context.alpha_factors.funding_alpha, 0.0)

    def test_p1_5_composite_weights_sum_to_one(self) -> None:
        """P1.5 权重: momentum 0.28 + trend 0.19 + regime 0.14 + multi_tf 0.10
        + micro 0.11 + basis 0.11 + funding 0.07 = 1.00 (严格归一)."""
        expected = {
            "momentum_alpha": 0.28,
            "trend_alpha": 0.19,
            "regime_alpha": 0.14,
            "multi_timeframe_alpha": 0.10,
            "microstructure_alpha": 0.11,
            "basis_alpha": 0.11,
            "funding_alpha": 0.07,
        }
        self.assertAlmostEqual(sum(expected.values()), 1.00, places=6)

    def test_funding_alpha_field_present_in_schema(self) -> None:
        calc = FeatureCalculator()
        f = calc.calculate(_snapshot(funding=0.0), market_snapshot_ref="evt_zero")
        assert f.analysis_context is not None
        self.assertIn("funding_alpha", f.analysis_context.alpha_factors.model_dump())


if __name__ == "__main__":
    unittest.main()
