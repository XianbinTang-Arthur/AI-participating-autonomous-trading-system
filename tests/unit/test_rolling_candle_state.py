"""RollingCandleState 单元测试 — Bug-1 时序平滑的基础设施契约.

锁定以下契约:
  1. ``update(bar, ts=)`` 幂等 —— 同 ts 再次 update 不推进 EMA/窗口，保证
     FeatureCalculator 同 snapshot 多次 calculate 的确定性（守
     test_feature_calculation_is_deterministic_for_same_snapshot）。
  2. ``update`` 拒绝旧 ts（乱序回放不破坏单调时序）。
  3. ``indicators()`` 在历史 < max(roc_window+1, atr_window+1) 时 ready=False；
     调用方必须退化到单 K 线瞬时算法。
  4. ``roc`` = (close_now - close_{roc_window 根前}) / close_{roc_window 根前}。
  5. ``atr`` = 最近 ``atr_window`` 根 True Range 的算术平均。
  6. ``atr_normalized`` = atr / close_now（相对比例）。
  7. ``prewarm(bars)`` 可接受任意顺序输入，内部按 ts 升序处理。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from aats.schemas.market import KlineBar
from aats.services.feature_engine.timeseries import RollingCandleState


def _bar(*, o: float, h: float, low: float, c: float) -> KlineBar:
    return KlineBar(
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(low)),
        close=Decimal(str(c)),
    )


def _ts(n: int, *, base: datetime | None = None, step_minutes: int = 15) -> datetime:
    base = base or datetime(2026, 4, 18, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(minutes=step_minutes * n)


class RollingCandleStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = RollingCandleState(
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            max_bars=50,
            roc_window=5,
            atr_window=14,
        )

    # ── 幂等契约 ─────────────────────────────────────────────────────

    def test_update_with_same_ts_overwrites_last_bar_and_does_not_advance_ema(self) -> None:
        """同 ts 反复 update（未闭合 K 线 tick 级更新）→ bar 覆盖但 EMA/窗口不推进.

        这是 FeatureCalculator 的确定性契约基础：同一 MarketSnapshot 反复调用
        calculate() 必须产出相同结果（见 test_feature_engine
        .test_feature_calculation_is_deterministic_for_same_snapshot）。
        """
        ts = _ts(0)
        self.state.update(_bar(o=100, h=101, low=99, c=100.5), ts=ts)
        ema_after_first = self.state.indicators().close_ema

        # 同 ts 反复 update（模拟同根未闭合 K 线的多个 tick）
        self.state.update(_bar(o=100, h=102, low=98, c=101.5), ts=ts)
        self.state.update(_bar(o=100, h=103, low=97, c=102.5), ts=ts)
        ema_after_multiple = self.state.indicators().close_ema

        self.assertEqual(self.state.bars_count(), 1, "同 ts 应覆盖而非 append")
        self.assertEqual(
            ema_after_first, ema_after_multiple,
            "EMA 在同 ts 反复 update 下不应推进",
        )

    def test_update_with_older_ts_is_silently_ignored(self) -> None:
        """乱序回放：晚到的旧 bar 必须丢弃，否则破坏单调时序和 ROC/ATR 计算."""
        self.state.update(_bar(o=100, h=101, low=99, c=100.5), ts=_ts(5))
        old_bar_count = self.state.bars_count()
        self.state.update(_bar(o=50, h=51, low=49, c=50), ts=_ts(2))  # 旧 ts
        self.assertEqual(
            self.state.bars_count(), old_bar_count,
            "旧 ts 的 bar 不应进入 state",
        )

    # ── Ready 判定 ────────────────────────────────────────────────────

    def test_indicators_not_ready_before_enough_bars(self) -> None:
        # atr_window=14 需要 15 根；roc_window=5 需要 6 根 → needed = 15
        for i in range(10):
            self.state.update(_bar(o=100, h=101, low=99, c=100 + i * 0.1), ts=_ts(i))
        ind = self.state.indicators()
        self.assertFalse(ind.ready)
        self.assertEqual(ind.bars_available, 10)
        self.assertIsNone(ind.roc)
        self.assertIsNone(ind.atr)

    def test_indicators_ready_after_enough_bars(self) -> None:
        for i in range(20):
            self.state.update(_bar(o=100, h=101, low=99, c=100 + i * 0.1), ts=_ts(i))
        ind = self.state.indicators()
        self.assertTrue(ind.ready)
        self.assertEqual(ind.bars_available, 20)
        self.assertIsNotNone(ind.roc)
        self.assertIsNotNone(ind.atr)
        self.assertIsNotNone(ind.atr_normalized)

    # ── ROC 语义 ──────────────────────────────────────────────────────

    def test_roc_computed_against_bar_n_ago(self) -> None:
        """ROC(5) = (close_now - close_{5 bars ago}) / close_{5 bars ago}."""
        # 构造 closes: 100, 100.5, 101, 101.5, 102, 102.5, ..., 110
        # 最后一根 close = 110, 5 根前 close = 107.5
        # ROC = (110 - 107.5) / 107.5 ≈ 0.02326
        for i in range(20):
            close = 100 + i * 0.5
            self.state.update(
                _bar(o=close - 0.1, h=close + 0.2, low=close - 0.3, c=close),
                ts=_ts(i),
            )
        ind = self.state.indicators()
        assert ind.roc is not None
        expected_roc = (109.5 - 107.0) / 107.0  # close_19 = 100+19*0.5=109.5, close_14 = 100+14*0.5=107
        self.assertAlmostEqual(ind.roc, expected_roc, places=6)

    # ── ATR 语义 ──────────────────────────────────────────────────────

    def test_atr_is_average_of_last_n_true_ranges(self) -> None:
        """ATR(n=14) = 算术平均 of last 14 True Ranges.

        构造固定 range=2.0, 价格不跳空，则 TR = max(h-l, |h-prev_c|, |l-prev_c|) = 2.0,
        ATR = 2.0, ATR / close ≈ 2.0 / close_now.
        """
        # 构造平稳序列 close = 100 + i*0.0（全相同）, high = close+1, low = close-1
        for i in range(20):
            self.state.update(
                _bar(o=100.0, h=101.0, low=99.0, c=100.0),
                ts=_ts(i),
            )
        ind = self.state.indicators()
        assert ind.atr is not None
        self.assertAlmostEqual(ind.atr, 2.0, places=6)
        assert ind.atr_normalized is not None
        self.assertAlmostEqual(ind.atr_normalized, 2.0 / 100.0, places=6)

    def test_atr_handles_gap_via_prev_close(self) -> None:
        """TR 定义包括 |high - prev_close| 与 |low - prev_close|，跳空时 TR > high-low."""
        # 全部稳定在 100 附近，然后最后一根跳空到 200
        for i in range(19):
            self.state.update(
                _bar(o=100.0, h=100.5, low=99.5, c=100.0),
                ts=_ts(i),
            )
        # 第 20 根跳空
        self.state.update(_bar(o=200.0, h=200.5, low=199.5, c=200.0), ts=_ts(19))
        ind = self.state.indicators()
        assert ind.atr is not None
        # 最近 14 根里最后一根的 TR = max(200.5-199.5, |200.5-100|, |199.5-100|) = 100.5
        # 其余 13 根 TR = 1.0 → ATR = (13*1 + 100.5) / 14 ≈ 8.107
        self.assertGreater(ind.atr, 5.0, "跳空应该让 ATR 显著抬高")

    # ── Prewarm ──────────────────────────────────────────────────────

    def test_prewarm_sorts_input_by_ts_ascending(self) -> None:
        """prewarm 接受任意顺序，内部按 ts 升序处理."""
        out_of_order: list[tuple[datetime, KlineBar]] = [
            (_ts(3), _bar(o=103, h=104, low=102, c=103.5)),
            (_ts(1), _bar(o=101, h=102, low=100, c=101.5)),
            (_ts(2), _bar(o=102, h=103, low=101, c=102.5)),
            (_ts(0), _bar(o=100, h=101, low=99, c=100.5)),
        ]
        self.state.prewarm(out_of_order)
        self.assertEqual(self.state.bars_count(), 4)
        self.assertEqual(self.state.last_timestamp(), _ts(3))

    def test_prewarm_then_live_update_continues_chain(self) -> None:
        """预热后的实时 update 应无缝续接（新 ts 正常 append）."""
        bars: list[tuple[datetime, KlineBar]] = [
            (_ts(i), _bar(o=100, h=101, low=99, c=100 + i * 0.1))
            for i in range(15)
        ]
        self.state.prewarm(bars)
        ind_before = self.state.indicators()
        self.assertTrue(ind_before.ready)

        # 推一根新的
        self.state.update(_bar(o=110, h=111, low=109, c=110.5), ts=_ts(15))
        ind_after = self.state.indicators()
        self.assertTrue(ind_after.ready)
        # ROC 应该有变化（新的 close 进入了计算）
        self.assertNotEqual(ind_before.roc, ind_after.roc)


if __name__ == "__main__":
    unittest.main()
