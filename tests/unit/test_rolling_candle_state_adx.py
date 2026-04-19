"""P2.9 — RollingCandleState ADX/+DI/-DI 计算契约.

Wilder ADX 是经典技术指标，我们用简化实现（最后一期 DX 代 ADX）。这里用
合成数据锁定:
  1. 平稳震荡序列 → ADX 低 (<20)
  2. 强单向趋势序列 → ADX 高 (>25)
  3. 上涨趋势 → +DI > -DI
  4. 下跌趋势 → -DI > +DI
  5. 样本不足时 adx/plus_di/minus_di = None
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from aats.schemas.market import KlineBar
from aats.services.feature_engine.timeseries import RollingCandleState


def _ts(n: int) -> datetime:
    return datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=15 * n)


def _bar(*, o: float, h: float, l: float, c: float) -> KlineBar:
    return KlineBar(
        open=Decimal(str(o)), high=Decimal(str(h)),
        low=Decimal(str(l)), close=Decimal(str(c)),
    )


class RollingCandleStateADXTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = RollingCandleState(
            symbol="BTC-USDT-SWAP", timeframe="15m",
            max_bars=50, roc_window=5, atr_window=14,
        )

    def test_adx_none_before_enough_bars(self) -> None:
        """样本不足以 atr_window+1 根时 ADX = None (和 roc/atr 一致 not ready)."""
        for i in range(10):
            self.state.update(_bar(o=100, h=101, l=99, c=100), ts=_ts(i))
        ind = self.state.indicators()
        self.assertFalse(ind.ready)
        self.assertIsNone(ind.adx)
        self.assertIsNone(ind.plus_di)
        self.assertIsNone(ind.minus_di)

    def test_adx_low_for_flat_range(self) -> None:
        """平稳震荡（无方向）→ ADX 应较低 (< 25)."""
        # 20 根 close=100 小幅震荡（high=101, low=99）
        for i in range(20):
            h, l = 101.0, 99.0
            self.state.update(_bar(o=100, h=h, l=l, c=100), ts=_ts(i))
        ind = self.state.indicators()
        self.assertTrue(ind.ready)
        assert ind.adx is not None
        self.assertLess(ind.adx, 25.0, f"flat range ADX too high: {ind.adx}")

    def test_adx_high_for_strong_uptrend(self) -> None:
        """强单向上涨趋势 → ADX > 25, +DI > -DI."""
        # 30 根阶梯上涨 close: 100, 101, 102, ..., 129
        for i in range(30):
            c = 100.0 + i * 1.0
            h = c + 0.5
            l = c - 0.3
            o = c - 0.2
            self.state.update(_bar(o=o, h=h, l=l, c=c), ts=_ts(i))
        ind = self.state.indicators()
        self.assertTrue(ind.ready)
        assert ind.adx is not None
        assert ind.plus_di is not None
        assert ind.minus_di is not None
        self.assertGreater(ind.adx, 25.0, f"uptrend ADX too low: {ind.adx}")
        self.assertGreater(ind.plus_di, ind.minus_di, "+DI should dominate in uptrend")

    def test_adx_high_for_strong_downtrend_with_minus_di_dominant(self) -> None:
        """强单向下跌 → ADX > 25, -DI > +DI."""
        for i in range(30):
            c = 130.0 - i * 1.0
            h = c + 0.3
            l = c - 0.5
            o = c + 0.2
            self.state.update(_bar(o=o, h=h, l=l, c=c), ts=_ts(i))
        ind = self.state.indicators()
        assert ind.adx is not None
        assert ind.plus_di is not None
        assert ind.minus_di is not None
        self.assertGreater(ind.adx, 25.0)
        self.assertGreater(ind.minus_di, ind.plus_di, "-DI should dominate in downtrend")


if __name__ == "__main__":
    unittest.main()
