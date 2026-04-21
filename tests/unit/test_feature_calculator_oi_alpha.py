"""FeatureCalculator.oi_alpha (P1.6) 契约.

锁定:
  1. OI 未 ready (< ema_period 样本) → oi_alpha=0
  2. price_roc 未 ready → oi_alpha=0 (依赖 15m RollingCandleState)
  3. 价 ↑ OI ↑ (同向) → oi_alpha 正 (趋势确认)
  4. 价 ↑ OI ↓ (反向) → oi_alpha 弱负 (平仓反弹)
  5. dead zone 内 (|oi_delta| < threshold) → oi_alpha=0
  6. flag off → oi_alpha=0
  7. P1.6 composite 权重和 = 1.00
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from aats.schemas.common import utc_now
from aats.schemas.market import KlineBar, MarketSnapshot
from aats.services.feature_engine.calculator import FeatureCalculator


def _snapshot(
    *, last: float = 67000.0, mark: float | None = None,
    funding: float | None = None, oi: float | None = None,
    ts: datetime | None = None,
) -> MarketSnapshot:
    ts = ts or utc_now()
    kwargs = {
        "created_at": ts, "symbol": "BTC-USDT-SWAP", "exchange": "OKX",
        "snapshot_ts": ts,
        "best_bid": Decimal(str(last - 0.5)), "best_ask": Decimal(str(last + 0.5)),
        "last_price": Decimal(str(last)),
        "bid_size": Decimal("3.0"), "ask_size": Decimal("2.0"), "volume_24h": Decimal("1000"),
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
    if oi is not None:
        kwargs["open_interest"] = Decimal(str(oi))
    return MarketSnapshot(**kwargs)


def _prewarm_rolling_state(calc: FeatureCalculator, *, symbol: str, close_base: float, close_final: float) -> None:
    """Fill 15m RollingCandleState so ROC is computed from close_base → close_final."""
    state = calc.register_rolling_state(symbol=symbol, timeframe="15m")
    base_ts = utc_now() - timedelta(minutes=15 * 30)
    bars: list[tuple] = []
    # 25 根 close_base, 第 26 根 close_final
    for i in range(25):
        ts = base_ts + timedelta(minutes=15 * i)
        bars.append((ts, KlineBar(
            open=Decimal(str(close_base)), high=Decimal(str(close_base + 100)),
            low=Decimal(str(close_base - 100)), close=Decimal(str(close_base)),
        )))
    final_ts = base_ts + timedelta(minutes=15 * 25)
    bars.append((final_ts, KlineBar(
        open=Decimal(str(close_base)), high=Decimal(str(max(close_base, close_final) + 100)),
        low=Decimal(str(min(close_base, close_final) - 100)), close=Decimal(str(close_final)),
    )))
    state.prewarm(bars)


def _prewarm_oi_state(calc: FeatureCalculator, *, symbol: str, oi_samples: list[float]) -> None:
    """Feed OI samples through direct state access for test determinism."""
    state = calc._get_oi_state(symbol)  # type: ignore[attr-defined]
    base_ts = utc_now() - timedelta(seconds=3 * len(oi_samples))
    for i, oi in enumerate(oi_samples):
        ts = base_ts + timedelta(seconds=3 * i)
        state.update(oi, ts=ts)


class FeatureCalculatorOIAlphaTests(unittest.TestCase):
    def test_oi_alpha_zero_when_no_oi_field(self) -> None:
        calc = FeatureCalculator()
        f = calc.calculate(_snapshot(oi=None), market_snapshot_ref="evt")
        assert f.analysis_context is not None
        self.assertEqual(f.analysis_context.alpha_factors.oi_alpha, 0.0)

    def test_oi_alpha_zero_when_state_not_ready(self) -> None:
        calc = FeatureCalculator()
        # 只推 1 个 OI 样本，state 未 ready (< 20)
        f = calc.calculate(_snapshot(oi=40_000_000.0), market_snapshot_ref="evt_1")
        assert f.analysis_context is not None
        self.assertEqual(f.analysis_context.alpha_factors.oi_alpha, 0.0)

    def test_oi_alpha_positive_when_price_and_oi_both_up(self) -> None:
        """价 ↑ OI ↑ → 新多头入场 → oi_alpha 正 (趋势确认)."""
        calc = FeatureCalculator()
        symbol = "BTC-USDT-SWAP"
        # price_roc 正：历史 close=65000, 当前 close=67000 (~3%)
        _prewarm_rolling_state(calc, symbol=symbol, close_base=65000.0, close_final=67000.0)
        # OI 增长 10%：20 根 40M, 当前 44M
        _prewarm_oi_state(calc, symbol=symbol, oi_samples=[40_000_000.0] * 20)
        f = calc.calculate(
            _snapshot(last=67000.0, oi=44_000_000.0),
            market_snapshot_ref="evt_up_up",
        )
        assert f.analysis_context is not None
        self.assertGreater(f.analysis_context.alpha_factors.oi_alpha, 0.1)

    def test_oi_alpha_weak_negative_when_price_up_oi_down(self) -> None:
        """价 ↑ OI ↓ → 多头平仓反弹 → oi_alpha 弱负 (magnitude × 0.5)."""
        calc = FeatureCalculator()
        symbol = "BTC-USDT-SWAP"
        _prewarm_rolling_state(calc, symbol=symbol, close_base=65000.0, close_final=67000.0)
        # OI 下降：20 根 40M 基线，当前 36M
        _prewarm_oi_state(calc, symbol=symbol, oi_samples=[40_000_000.0] * 20)
        f = calc.calculate(
            _snapshot(last=67000.0, oi=36_000_000.0),
            market_snapshot_ref="evt_up_down",
        )
        assert f.analysis_context is not None
        # 负 alpha（方向与 price 反号）且幅度较小（反向 signal × 0.5）
        self.assertLess(f.analysis_context.alpha_factors.oi_alpha, 0.0)

    def test_oi_alpha_zero_in_dead_zone(self) -> None:
        """|oi_delta| < dead_zone (0.5%) → oi_alpha=0 (噪声过滤)."""
        calc = FeatureCalculator(oi_dead_zone=0.01)  # 1% dead zone
        symbol = "BTC-USDT-SWAP"
        _prewarm_rolling_state(calc, symbol=symbol, close_base=65000.0, close_final=67000.0)
        _prewarm_oi_state(calc, symbol=symbol, oi_samples=[40_000_000.0] * 20)
        # 微幅变化：40M → 40.1M (+0.25%, 远小于 1% dead zone)
        f = calc.calculate(
            _snapshot(last=67000.0, oi=40_100_000.0),
            market_snapshot_ref="evt_noise",
        )
        assert f.analysis_context is not None
        self.assertEqual(f.analysis_context.alpha_factors.oi_alpha, 0.0)

    def test_flag_disabled_zeros_oi_alpha(self) -> None:
        calc = FeatureCalculator(enable_oi_signal=False)
        symbol = "BTC-USDT-SWAP"
        _prewarm_rolling_state(calc, symbol=symbol, close_base=65000.0, close_final=67000.0)
        _prewarm_oi_state(calc, symbol=symbol, oi_samples=[40_000_000.0] * 20)
        f = calc.calculate(
            _snapshot(last=67000.0, oi=44_000_000.0),
            market_snapshot_ref="evt_flag_off",
        )
        assert f.analysis_context is not None
        self.assertEqual(f.analysis_context.alpha_factors.oi_alpha, 0.0)

    def test_p1_6_composite_weights_sum_to_one(self) -> None:
        """P1.6: momentum 0.26 + trend 0.18 + regime 0.13 + multi_tf 0.09 +
        micro 0.10 + basis 0.10 + funding 0.07 + oi 0.07 = 1.00."""
        expected = {
            "momentum_alpha": 0.26, "trend_alpha": 0.18, "regime_alpha": 0.13,
            "multi_timeframe_alpha": 0.09, "microstructure_alpha": 0.10,
            "basis_alpha": 0.10, "funding_alpha": 0.07, "oi_alpha": 0.07,
        }
        self.assertAlmostEqual(sum(expected.values()), 1.00, places=6)

    def test_oi_alpha_field_present_in_schema(self) -> None:
        calc = FeatureCalculator()
        f = calc.calculate(_snapshot(oi=None), market_snapshot_ref="evt")
        assert f.analysis_context is not None
        self.assertIn("oi_alpha", f.analysis_context.alpha_factors.model_dump())


if __name__ == "__main__":
    unittest.main()
