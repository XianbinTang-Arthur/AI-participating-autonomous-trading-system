"""FeatureCalculator 时序平滑接入契约测试 (Bug-1).

锁定:
  1. ``enable_timeseries_smoothing=True`` 默认下，calculate 调用会更新对应
     (symbol, timeframe) 的 RollingCandleState。
  2. 同一 snapshot 反复 calculate → 结果完全一致（test_feature_engine 的既有
     确定性契约），因为 RollingCandleState.update 同 ts 幂等。
  3. ``enable_timeseries_smoothing=False`` 时完全跳过 state 写入，走旧单 K 线
     路径（紧急回滚手段）。
  4. 预热后 state ready → analyze_with_state 不再退化到 analyze_kline，
     momentum_score 语义变为 ROC。
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from aats.schemas.common import utc_now
from aats.schemas.market import KlineBar, MarketSnapshot
from aats.services.feature_engine.calculator import FeatureCalculator


def _snapshot(*, close_15m: float = 67100.0, close_1h: float = 67100.0) -> MarketSnapshot:
    now = utc_now()
    return MarketSnapshot(
        created_at=now,
        symbol="BTC-USDT-SWAP",
        exchange="OKX",
        snapshot_ts=now,
        best_bid=67_000.0,
        best_ask=67_001.0,
        last_price=67_000.5,
        bid_size=3.0,
        ask_size=2.0,
        volume_24h=1000.0,
        kline_15m={"open": 66_800.0, "high": 67_200.0, "low": 66_700.0, "close": close_15m},
        kline_1h={"open": 66_000.0, "high": 67_300.0, "low": 65_900.0, "close": close_1h},
        orderbook_depth={
            "bids": [{"price": 67_000.0, "size": 5.0}],
            "asks": [{"price": 67_001.0, "size": 4.0}],
        },
        recent_trades=[
            {"side": "buy", "size": 0.9},
            {"side": "sell", "size": 0.9},
        ],
    )


class FeatureCalculatorSmoothingTests(unittest.TestCase):
    def test_default_state_registration_happens_on_calculate(self) -> None:
        """默认 flag=True：calculate() 后 (symbol, 15m) 和 (symbol, 1h) 两个 state
        都应被注册（即使 state 未 ready，用于后续增量 update / warmup 灌入）。"""
        calc = FeatureCalculator()  # 默认 enable_timeseries_smoothing=True
        snap = _snapshot()
        calc.calculate(snap, market_snapshot_ref="evt_1")
        states = calc.rolling_state_snapshot()
        self.assertIn(("BTC-USDT-SWAP", "15m"), states)
        self.assertIn(("BTC-USDT-SWAP", "1h"), states)

    def test_flag_disabled_skips_state_registration(self) -> None:
        """flag off：完全不构造 RollingCandleState（回滚到纯瞬时）."""
        calc = FeatureCalculator(enable_timeseries_smoothing=False)
        snap = _snapshot()
        calc.calculate(snap, market_snapshot_ref="evt_1")
        states = calc.rolling_state_snapshot()
        self.assertEqual(len(states), 0, "flag off 时不应注册 state")

    def test_same_snapshot_twice_produces_identical_features_preserving_determinism(self) -> None:
        """守 test_feature_calculation_is_deterministic_for_same_snapshot:
        同 snapshot 两次 calculate 结果应完全一致（RollingCandleState.update 同 ts 幂等）."""
        calc = FeatureCalculator()
        snap = _snapshot()
        first = calc.calculate(snap, market_snapshot_ref="evt_1")
        second = calc.calculate(snap, market_snapshot_ref="evt_1")
        self.assertEqual(
            first.model_dump(mode="json"),
            second.model_dump(mode="json"),
        )

    def test_prewarmed_state_switches_momentum_to_roc_semantics(self) -> None:
        """预热后 state ready → analyze_with_state 生效。
        - analyze_kline (旧) 的 momentum = (close - open) / open，单 K 线瞬时
        - analyze_with_state (新) 的 momentum = ROC(5)，5 根前到现在累积

        构造一个"过去 5 根 close 全 = 65000, 当前 close = 67100"的历史，
        则 ROC ≈ (67100 - 65000) / 65000 = 0.0323，而 (close-open)/open
        = (67100 - 66800) / 66800 ≈ 0.0045。两者差一个量级。
        """
        calc = FeatureCalculator()
        # 手动注册 + 预热 15m state，让它 ready
        state = calc.register_rolling_state(symbol="BTC-USDT-SWAP", timeframe="15m")
        base_ts = utc_now() - timedelta(minutes=15 * 20)
        bars: list[tuple] = []
        # 20 根 close=65000 的平稳序列（够过 atr_window+1=15 的门槛）
        for i in range(20):
            ts = base_ts + timedelta(minutes=15 * i)
            bar = KlineBar(
                open=Decimal("65000"),
                high=Decimal("65100"),
                low=Decimal("64900"),
                close=Decimal("65000"),
            )
            bars.append((ts, bar))
        state.prewarm(bars)
        self.assertTrue(state.is_ready(), "20 根 bar 应足以让 state ready")

        # 再跑 calculate，当前 close=67100 vs 历史 close=65000
        snap = _snapshot(close_15m=67100.0)
        features = calc.calculate(snap, market_snapshot_ref="evt_live")

        analysis = features.analysis_context
        assert analysis is not None
        momentum = analysis.timeframe_features["15m"].momentum_score
        # ROC(5) ≈ (67100 - 65000) / 65000 ≈ 0.0323；旧算法 (67100-66800)/66800 ≈ 0.0045
        # 用 > 0.02 为下界，充分区别于旧算法的 0.0045
        self.assertGreater(
            momentum, 0.02,
            "预热后应输出 ROC 级别的 momentum，显著大于 (close-open)/open 单 K 线瞬时值",
        )


if __name__ == "__main__":
    unittest.main()
