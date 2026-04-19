"""H4 方向门控在 RDP replay adapter 中的锁定测试。

对应改动: independent_adapter._compute_book_score 的 confidence_raw 现在基于
`trend_dir > 0`（翻转后坐标系，两腿都是"正=对齐"）进行方向门控。

对应设计: docs/design/h4_confidence_direction_gating_2026_04_19.md §2.2
对应根因: docs/review/short_leg_asymmetry_root_cause_2026_04_19.md §5
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from aats.data_platform.replay.adapters.independent_adapter import (
    IndependentReplayAdapter,
    _W_CONFIDENCE,
)
from aats.data_platform.replay.core.replay_context import ReplayBar


def _bar(ts: datetime, open_: float, close: float, vol: float = 1000.0) -> ReplayBar:
    """最小 ReplayBar 构造：用于填满 adapter 的 _bar_history 以触发 score 计算。"""
    return ReplayBar(
        symbol="BTC-USDT-SWAP",
        ts=ts,
        open=Decimal(str(open_)),
        high=Decimal(str(max(open_, close) + 10)),
        low=Decimal(str(min(open_, close) - 10)),
        close=Decimal(str(close)),
        volume=Decimal(str(vol)),
        quote_volume=Decimal(str(vol * close)),
        is_closed=True,
        aligned_funding_rate=None,
        funding_source_ts=None,
    )


def _make_uptrend_bars(n: int = 12) -> list[ReplayBar]:
    """构造持续上涨的 bar 序列：每根 bar close 比前一根高。

    最后一根 bar 成交量显著放大（模拟 volume spike → 高 confidence_raw）。
    """
    base_ts = datetime(2026, 4, 10, 0, 0, tzinfo=timezone.utc)
    bars: list[ReplayBar] = []
    price = 70000.0
    for i in range(n):
        open_ = price
        close = price + 30  # 每根上涨 30
        vol = 1000.0 if i < n - 1 else 3000.0  # 最后一根放量
        bars.append(_bar(base_ts + timedelta(minutes=15 * i), open_, close, vol))
        price = close
    return bars


class TestReplayAdapterH4DirectionGating(unittest.TestCase):
    """replay adapter 的 H4 方向门控：confidence_raw 只在 trend_dir > 0 时计入。

    trend_dir 在 short leg 时已翻转（independent_adapter.py:237-238），
    所以两腿都以 "trend_dir > 0 = 对齐方向" 为门控条件。
    """

    def test_long_leg_in_uptrend_gets_confidence_contribution(self):
        """持续上涨 + 放量 → long leg 的 trend_dir > 0 → confidence_raw 应非零且被计入。"""
        adapter = IndependentReplayAdapter()
        adapter.reset_state()
        bars = _make_uptrend_bars(n=12)

        # 先用前 11 根 bar 填满 adapter 的 _bar_history（模拟 prewarm）
        for bar in bars[:-1]:
            adapter._bar_history.append(bar)

        # 将最后一根（含 volume spike）作为被评估的 bar
        last_bar = bars[-1]
        adapter._bar_history.append(last_bar)
        score_long = adapter._compute_book_score(last_bar, leg="long")

        # 断言：long leg 在上涨 + 放量下应有非零 score，且 confidence 贡献存在
        # 更精确的断言：把 confidence_raw 设为 0 重新算，差值应 > 0
        # 这里用间接法——long 上涨 + 放量 应产生接近 1 的 score
        self.assertGreater(score_long, 0.5,
                           f"long leg in uptrend should produce strong score, got {score_long}")

    def test_short_leg_in_uptrend_has_confidence_gated_regardless_of_volume(self):
        """持续上涨下 short leg 的 trend_dir（翻转后）< 0 → confidence_raw 被门控归零。

        验证手段：在两个上涨场景下对比 short leg 的 score —— 一个最后一根放量、
        一个最后一根不放量。若 H4 门控生效，放量不应影响 short 的 score（confidence=0）；
        若门控失效，放量会让 confidence_raw 飙升、short score 上涨 0.02-0.12（_W_CONFIDENCE 0.12 × conf_raw）。
        """
        # 场景 A：持续上涨，最后一根放量（原 _make_uptrend_bars）
        adapter_a = IndependentReplayAdapter()
        adapter_a.reset_state()
        bars_a = _make_uptrend_bars(n=12)  # 最后一根 vol=3000
        for bar in bars_a[:-1]:
            adapter_a._bar_history.append(bar)
        adapter_a._bar_history.append(bars_a[-1])
        score_short_a = adapter_a._compute_book_score(bars_a[-1], leg="short")

        # 场景 B：持续上涨，最后一根 volume 持平（没有 spike）
        adapter_b = IndependentReplayAdapter()
        adapter_b.reset_state()
        base_ts = datetime(2026, 4, 10, 0, 0, tzinfo=timezone.utc)
        bars_b: list[ReplayBar] = []
        price = 70000.0
        for i in range(12):
            open_ = price
            close = price + 30
            vol = 1000.0  # 所有 bar vol 相同，无 spike
            bars_b.append(_bar(base_ts + timedelta(minutes=15 * i), open_, close, vol))
            price = close
        for bar in bars_b[:-1]:
            adapter_b._bar_history.append(bar)
        adapter_b._bar_history.append(bars_b[-1])
        score_short_b = adapter_b._compute_book_score(bars_b[-1], leg="short")

        # H4 门控生效：两个场景 short score 几乎相同（confidence=0 无论 vol_ratio 多大）
        # 容差取极小值 < 0.001（浮点误差级别）
        self.assertAlmostEqual(
            score_short_a, score_short_b, places=6,
            msg=f"H4 gating should zero out confidence for misaligned short leg; "
                f"score_with_spike={score_short_a}, score_without_spike={score_short_b}, "
                f"if gating failed, delta would be ~{_W_CONFIDENCE * 0.5}"
        )

    def test_short_leg_in_downtrend_gets_full_confidence_contribution(self):
        """持续下跌 → short leg 的 trend_dir（翻转后）> 0 → confidence_raw 应按 vol_ratio 计入。

        验证手段：在下跌场景下，放量 vs 不放量会产生可观察的 score 差异，
        说明 confidence 项在 aligned 情况下正确生效。
        """
        # 场景 A：持续下跌 + 最后一根放量
        adapter_a = IndependentReplayAdapter()
        adapter_a.reset_state()
        base_ts = datetime(2026, 4, 10, 0, 0, tzinfo=timezone.utc)
        bars_a: list[ReplayBar] = []
        price = 70000.0
        for i in range(12):
            open_ = price
            close = price - 30
            vol = 1000.0 if i < 11 else 3000.0  # 最后一根放量
            bars_a.append(_bar(base_ts + timedelta(minutes=15 * i), open_, close, vol))
            price = close
        for bar in bars_a[:-1]:
            adapter_a._bar_history.append(bar)
        adapter_a._bar_history.append(bars_a[-1])
        score_short_a = adapter_a._compute_book_score(bars_a[-1], leg="short")

        # 场景 B：持续下跌 + 最后一根不放量
        adapter_b = IndependentReplayAdapter()
        adapter_b.reset_state()
        bars_b: list[ReplayBar] = []
        price = 70000.0
        for i in range(12):
            open_ = price
            close = price - 30
            vol = 1000.0  # 所有 bar vol 相同
            bars_b.append(_bar(base_ts + timedelta(minutes=15 * i), open_, close, vol))
            price = close
        for bar in bars_b[:-1]:
            adapter_b._bar_history.append(bar)
        adapter_b._bar_history.append(bars_b[-1])
        score_short_b = adapter_b._compute_book_score(bars_b[-1], leg="short")

        # aligned 情况下，volume spike 应显著提升 score（confidence_raw 从 sigmoid(0)=0.5 升至接近 1）
        delta = score_short_a - score_short_b
        self.assertGreater(
            delta, _W_CONFIDENCE * 0.3,
            f"aligned short leg should see confidence contribution from volume spike; "
            f"with_spike={score_short_a}, without={score_short_b}, delta={delta} "
            f"(expected > {_W_CONFIDENCE * 0.3})"
        )


if __name__ == "__main__":
    unittest.main()
