"""FeatureCalculator.ls_alpha (P2.7) 契约.

锁定:
  1. poller=None or flag off → ls_alpha=0
  2. ls_ratio > 1 (多头占优) → ls_alpha < 0 (反转抑制 long)
  3. ls_ratio < 1 (空头占优) → ls_alpha > 0 (反转鼓励 long)
  4. stale 缓存 > max_staleness → ls_alpha = 0
  5. P2.7 composite 权重总和 = 1.00
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from aats.schemas.common import utc_now
from aats.schemas.market import MarketSnapshot
from aats.services.feature_engine.calculator import FeatureCalculator
from aats.services.feature_engine.long_short_poller import (
    LongShortRatioPoller,
    LongShortRatioSample,
)


def _snapshot(ts=None) -> MarketSnapshot:
    ts = ts or utc_now()
    return MarketSnapshot(
        created_at=ts, symbol="BTC-USDT-SWAP", exchange="OKX",
        snapshot_ts=ts,
        best_bid=Decimal("67000"), best_ask=Decimal("67001"),
        last_price=Decimal("67000.5"),
        bid_size=Decimal("3"), ask_size=Decimal("2"), volume_24h=Decimal("1000"),
        kline_15m={"open": 66800, "high": 67200, "low": 66700, "close": 67100},
        kline_1h={"open": 66000, "high": 67300, "low": 65900, "close": 67100},
        orderbook_depth={
            "bids": [{"price": 67000, "size": 5}],
            "asks": [{"price": 67001, "size": 4}],
        },
        recent_trades=[{"side": "buy", "size": 0.9}],
    )


def _poller_with(sample: LongShortRatioSample | None) -> LongShortRatioPoller:
    p = LongShortRatioPoller(okx_rest_url="https://www.okx.com")
    if sample is not None:
        p._cache[sample.symbol.upper()] = sample  # type: ignore[attr-defined]
    return p


class FeatureCalculatorLSAlphaTests(unittest.TestCase):
    def test_no_poller_gives_zero(self) -> None:
        calc = FeatureCalculator(enable_ls_ratio_signal=True)
        f = calc.calculate(_snapshot(), market_snapshot_ref="evt_no_poller")
        assert f.analysis_context is not None
        self.assertEqual(f.analysis_context.alpha_factors.ls_alpha, 0.0)

    def test_flag_off_gives_zero_even_with_poller(self) -> None:
        now = utc_now()
        poller = _poller_with(LongShortRatioSample(symbol="BTC-USDT-SWAP", ts=now, ls_ratio=3.0))
        calc = FeatureCalculator(
            long_short_poller=poller, enable_ls_ratio_signal=False,
        )
        f = calc.calculate(_snapshot(ts=now), market_snapshot_ref="evt_flag_off")
        assert f.analysis_context is not None
        self.assertEqual(f.analysis_context.alpha_factors.ls_alpha, 0.0)

    def test_high_ls_ratio_yields_negative_alpha(self) -> None:
        """ls_ratio=3 (多头拥挤) → ls_alpha < 0 (反转抑制 long)."""
        now = utc_now()
        poller = _poller_with(LongShortRatioSample(symbol="BTC-USDT-SWAP", ts=now, ls_ratio=3.0))
        calc = FeatureCalculator(
            long_short_poller=poller, enable_ls_ratio_signal=True, ls_ratio_scale=2.0,
        )
        f = calc.calculate(_snapshot(ts=now), market_snapshot_ref="evt_crowded_long")
        assert f.analysis_context is not None
        # ls_ratio=3, scale=2 → -tanh((3-1)/2) = -tanh(1) ≈ -0.76
        self.assertLess(f.analysis_context.alpha_factors.ls_alpha, -0.5)

    def test_low_ls_ratio_yields_positive_alpha(self) -> None:
        """ls_ratio=0.33 (空头拥挤) → ls_alpha > 0 (鼓励 long)."""
        now = utc_now()
        poller = _poller_with(LongShortRatioSample(symbol="BTC-USDT-SWAP", ts=now, ls_ratio=0.33))
        calc = FeatureCalculator(
            long_short_poller=poller, enable_ls_ratio_signal=True, ls_ratio_scale=2.0,
        )
        f = calc.calculate(_snapshot(ts=now), market_snapshot_ref="evt_crowded_short")
        assert f.analysis_context is not None
        self.assertGreater(f.analysis_context.alpha_factors.ls_alpha, 0.1)

    def test_stale_sample_gives_zero_alpha(self) -> None:
        """缓存 ts 距 snapshot_ts > max_staleness (默认 900s) → ls_alpha=0."""
        now = utc_now()
        stale_ts = now - timedelta(seconds=3600)  # 1 小时前
        poller = _poller_with(LongShortRatioSample(symbol="BTC-USDT-SWAP", ts=stale_ts, ls_ratio=3.0))
        calc = FeatureCalculator(
            long_short_poller=poller,
            enable_ls_ratio_signal=True,
            ls_ratio_max_staleness_seconds=900.0,
        )
        f = calc.calculate(_snapshot(ts=now), market_snapshot_ref="evt_stale")
        assert f.analysis_context is not None
        self.assertEqual(f.analysis_context.alpha_factors.ls_alpha, 0.0)

    def test_p2_7_composite_weights_sum_to_one(self) -> None:
        """P2.7 最终权重: momentum 0.24 + trend 0.17 + regime 0.12 + multi_tf 0.08
        + micro 0.09 + basis 0.10 + funding 0.07 + oi 0.07 + ls 0.06 = 1.00."""
        expected = {
            "momentum_alpha": 0.24, "trend_alpha": 0.17, "regime_alpha": 0.12,
            "multi_timeframe_alpha": 0.08, "microstructure_alpha": 0.09,
            "basis_alpha": 0.10, "funding_alpha": 0.07, "oi_alpha": 0.07,
            "ls_alpha": 0.06,
        }
        self.assertAlmostEqual(sum(expected.values()), 1.00, places=6)

    def test_ls_alpha_field_present_in_schema(self) -> None:
        calc = FeatureCalculator()
        f = calc.calculate(_snapshot(), market_snapshot_ref="evt")
        assert f.analysis_context is not None
        self.assertIn("ls_alpha", f.analysis_context.alpha_factors.model_dump())


if __name__ == "__main__":
    unittest.main()
