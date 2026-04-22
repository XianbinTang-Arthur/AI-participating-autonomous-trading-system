"""Unit tests for backtest EquityBuilder.

覆盖空 builder、单点、连续上升无回撤、峰值反转回撤、24h 窗口 daily return、
首点 daily return 为 0、flat returns Sharpe = 0、混合 returns Sharpe > 0、
fill_count / fee_total 从最新 snapshot 取、curve 返回 tuple 不可变。

所有 Decimal 断言用 Decimal；float Sharpe 断言用 ``assertAlmostEqual(places=4)``。
"""

from __future__ import annotations

import math
import unittest
from decimal import Decimal

from aats.data_platform.replay.backtest.equity_builder import (
    BacktestSummary,
    EquityBuilder,
    EquityPoint,
)
from aats.data_platform.replay.backtest.position_tracker import PositionSnapshot


_DAY_MS: int = 24 * 60 * 60 * 1000


def _snap(
    *,
    realized: str = "0",
    unrealized: str = "0",
    fees: str = "0",
    ts_ms: int = 0,
    fill_count: int = 0,
    net_qty: str = "0",
    avg_entry_price: str = "0",
    last_mark_price: str = "0",
) -> PositionSnapshot:
    """Test helper：快速造 PositionSnapshot。"""
    return PositionSnapshot(
        net_qty=Decimal(net_qty),
        avg_entry_price=Decimal(avg_entry_price),
        realized_pnl=Decimal(realized),
        unrealized_pnl=Decimal(unrealized),
        last_mark_price=Decimal(last_mark_price),
        accumulated_fees=Decimal(fees),
        fill_count=fill_count,
        ts_ms=ts_ms,
    )


class EmptyBuilderTests(unittest.TestCase):
    def test_empty_builder_summary(self) -> None:
        """无 snapshot 时 summary 全为默认值。"""
        builder = EquityBuilder()
        summary = builder.summary()

        self.assertIsInstance(summary, BacktestSummary)
        self.assertEqual(summary.initial_equity, Decimal("0"))
        self.assertEqual(summary.final_equity, Decimal("0"))
        self.assertEqual(summary.cumulative_pnl, Decimal("0"))
        self.assertEqual(summary.max_drawdown_bps, Decimal("0"))
        self.assertEqual(summary.sharpe_ratio, 0.0)
        self.assertEqual(summary.fill_count, 0)
        self.assertEqual(summary.fee_total, Decimal("0"))
        self.assertEqual(summary.bar_count, 0)
        self.assertEqual(summary.start_ts_ms, 0)
        self.assertEqual(summary.end_ts_ms, 0)

    def test_empty_curve_is_tuple(self) -> None:
        builder = EquityBuilder()
        curve = builder.curve
        self.assertIsInstance(curve, tuple)
        self.assertEqual(curve, ())


class SingleSnapshotTests(unittest.TestCase):
    def test_single_snapshot_record(self) -> None:
        """一条 snapshot 后 curve 长度 1, equity = net_pnl。"""
        builder = EquityBuilder()
        snap = _snap(realized="10", unrealized="5", fees="2", ts_ms=1_000)

        point = builder.record(snap)

        self.assertIsInstance(point, EquityPoint)
        # net_pnl = 10 + 5 - 2 = 13
        self.assertEqual(point.equity, Decimal("13"))
        self.assertEqual(point.cumulative_pnl, Decimal("13"))
        self.assertEqual(point.drawdown_bps, Decimal("0"))
        self.assertEqual(point.daily_return_bps, Decimal("0"))
        self.assertEqual(point.ts_ms, 1_000)

        self.assertEqual(len(builder.curve), 1)
        self.assertEqual(builder.curve[0], point)

    def test_single_snapshot_summary(self) -> None:
        """一条 snapshot 后 summary 基本字段正确，sharpe = 0（n=1）。"""
        builder = EquityBuilder()
        builder.record(_snap(realized="10", fees="1", ts_ms=1_000, fill_count=3))

        summary = builder.summary()
        self.assertEqual(summary.final_equity, Decimal("9"))
        self.assertEqual(summary.cumulative_pnl, Decimal("9"))
        self.assertEqual(summary.max_drawdown_bps, Decimal("0"))
        self.assertEqual(summary.sharpe_ratio, 0.0)
        self.assertEqual(summary.fill_count, 3)
        self.assertEqual(summary.fee_total, Decimal("1"))
        self.assertEqual(summary.bar_count, 1)
        self.assertEqual(summary.start_ts_ms, 1_000)
        self.assertEqual(summary.end_ts_ms, 1_000)


class DrawdownTests(unittest.TestCase):
    def test_peak_tracked_and_drawdown_zero_when_rising(self) -> None:
        """连续上升 net_pnl，每点 drawdown 一直 0。"""
        builder = EquityBuilder()

        for i, realized in enumerate(["10", "20", "30", "40", "50"], start=1):
            point = builder.record(_snap(realized=realized, ts_ms=i * 1_000))
            self.assertEqual(
                point.drawdown_bps,
                Decimal("0"),
                msg=f"step {i} drawdown should be 0",
            )

        self.assertEqual(builder.summary().max_drawdown_bps, Decimal("0"))

    def test_drawdown_bps_after_peak_reversal(self) -> None:
        """net_pnl 先 +100 再 +50 → drawdown_bps = 5000。"""
        builder = EquityBuilder()

        builder.record(_snap(realized="100", ts_ms=1_000))
        point2 = builder.record(_snap(realized="50", ts_ms=2_000))

        # (100 - 50) / max(|100|, 1) * 10000 = 5000
        self.assertEqual(point2.drawdown_bps, Decimal("5000"))
        self.assertEqual(builder.summary().max_drawdown_bps, Decimal("5000"))

    def test_drawdown_uses_peak_not_latest(self) -> None:
        """peak 后即使回升也不重置 peak，直到新高才变。"""
        builder = EquityBuilder()

        builder.record(_snap(realized="100", ts_ms=1_000))  # peak=100
        builder.record(_snap(realized="50", ts_ms=2_000))   # dd=5000
        p3 = builder.record(_snap(realized="80", ts_ms=3_000))  # dd=2000

        # (100 - 80) / 100 * 10000 = 2000
        self.assertEqual(p3.drawdown_bps, Decimal("2000"))
        self.assertEqual(builder.summary().max_drawdown_bps, Decimal("5000"))

    def test_drawdown_when_peak_is_zero_or_negative(self) -> None:
        """peak <= 0 时分母保底为 1，drawdown 仍非负。"""
        builder = EquityBuilder()

        # 首点 net_pnl = 0, peak = 0
        p1 = builder.record(_snap(ts_ms=1_000))
        self.assertEqual(p1.drawdown_bps, Decimal("0"))

        # 转负：drawdown = 0 - (-10) = 10, denom=max(0,1)=1 → 100000 bps
        p2 = builder.record(_snap(realized="-10", ts_ms=2_000))
        self.assertEqual(p2.drawdown_bps, Decimal("100000"))


class DailyReturnTests(unittest.TestCase):
    def test_daily_return_bps_first_snapshot_is_zero(self) -> None:
        """首个 point 无历史 → daily_return_bps = 0。"""
        builder = EquityBuilder()
        point = builder.record(_snap(realized="50", ts_ms=1_000))
        self.assertEqual(point.daily_return_bps, Decimal("0"))

    def test_daily_return_bps_uses_24h_window(self) -> None:
        """2 个 snapshot 刚好 24h 差距，计算正确。"""
        builder = EquityBuilder()

        builder.record(_snap(realized="100", ts_ms=0))
        # 刚好 24h 后（边界 inclusive：window_start = ts - 24h，取 ts_ms >= window_start）
        p2 = builder.record(_snap(realized="110", ts_ms=_DAY_MS))

        # 基线是 window 内最早点（0 时刻 100），当前 110
        # (110 - 100) / max(|100|, 1) * 10000 = 1000
        self.assertEqual(p2.daily_return_bps, Decimal("1000"))

    def test_daily_return_within_window_uses_earliest_point(self) -> None:
        """window 内多个点时取最早点作基线。"""
        builder = EquityBuilder()

        # t=0: equity=100 (基线)
        builder.record(_snap(realized="100", ts_ms=0))
        # t=6h: equity=200
        builder.record(_snap(realized="200", ts_ms=6 * 60 * 60 * 1000))
        # t=12h: 最早仍是 t=0 点
        p3 = builder.record(
            _snap(realized="150", ts_ms=12 * 60 * 60 * 1000)
        )

        # (150 - 100) / 100 * 10000 = 5000
        self.assertEqual(p3.daily_return_bps, Decimal("5000"))

    def test_daily_return_bps_negative_when_dropping(self) -> None:
        """下跌时 daily_return_bps 为负。"""
        builder = EquityBuilder()
        builder.record(_snap(realized="200", ts_ms=0))
        p2 = builder.record(_snap(realized="100", ts_ms=_DAY_MS))
        # (100 - 200) / 200 * 10000 = -5000
        self.assertEqual(p2.daily_return_bps, Decimal("-5000"))


class SharpeTests(unittest.TestCase):
    def test_sharpe_with_flat_returns_is_zero(self) -> None:
        """所有 daily_return 相同 → stdev = 0 → sharpe = 0。"""
        builder = EquityBuilder()

        # 多个点，realized 全为 0 → 每点 daily_return_bps = 0（flat）
        for i in range(5):
            builder.record(_snap(realized="0", ts_ms=i * 1_000))

        self.assertEqual(builder.summary().sharpe_ratio, 0.0)

    def test_sharpe_with_single_point_is_zero(self) -> None:
        """序列长度 < 2 → sharpe = 0。"""
        builder = EquityBuilder()
        builder.record(_snap(realized="100", ts_ms=1_000))
        self.assertEqual(builder.summary().sharpe_ratio, 0.0)

    def test_sharpe_with_varied_returns_positive(self) -> None:
        """混合 daily returns → sharpe 合理数值，与手算一致。"""
        builder = EquityBuilder()

        # 三点，每点间隔 24h，daily_return_bps 序列可手算：
        #   t=0        realized=100, no history → dr=0
        #   t=DAY      realized=110, baseline 100 → dr=1000
        #   t=2*DAY    realized=121, baseline 110 (t=DAY) → dr=1000
        # 等等 — 用更多变化造出非零 stdev
        builder.record(_snap(realized="100", ts_ms=0))
        builder.record(_snap(realized="110", ts_ms=_DAY_MS))       # dr = 1000
        builder.record(_snap(realized="99", ts_ms=2 * _DAY_MS))    # dr = (99-110)/110*10000
        builder.record(_snap(realized="120", ts_ms=3 * _DAY_MS))   # dr = (120-99)/99*10000

        # 手算 returns 列表
        returns = [
            0.0,
            1000.0,
            float(Decimal("-11") / Decimal("110") * Decimal("10000")),
            float(Decimal("21") / Decimal("99") * Decimal("10000")),
        ]
        n = len(returns)
        mean = sum(returns) / n
        var = sum((r - mean) ** 2 for r in returns) / (n - 1)
        expected = mean / math.sqrt(var) * math.sqrt(252.0)

        self.assertAlmostEqual(
            builder.summary().sharpe_ratio, expected, places=4
        )
        # 正数 returns 占优，sharpe 应为正
        self.assertGreater(builder.summary().sharpe_ratio, 0.0)


class SummaryAggregationTests(unittest.TestCase):
    def test_summary_includes_fill_count_and_fees(self) -> None:
        """fill_count / fee_total 从最新 snapshot 取。"""
        builder = EquityBuilder()

        builder.record(_snap(realized="10", fees="0.5", fill_count=1, ts_ms=1_000))
        builder.record(_snap(realized="20", fees="1.25", fill_count=3, ts_ms=2_000))
        builder.record(_snap(realized="30", fees="2.0", fill_count=5, ts_ms=3_000))

        summary = builder.summary()
        self.assertEqual(summary.fill_count, 5)
        self.assertEqual(summary.fee_total, Decimal("2.0"))
        self.assertEqual(summary.bar_count, 3)
        self.assertEqual(summary.start_ts_ms, 1_000)
        self.assertEqual(summary.end_ts_ms, 3_000)
        # final net pnl = 30 - 2 = 28
        self.assertEqual(summary.final_equity, Decimal("28"))
        self.assertEqual(summary.cumulative_pnl, Decimal("28"))

    def test_summary_idempotent(self) -> None:
        """summary 可多次调用，结果一致。"""
        builder = EquityBuilder()
        builder.record(_snap(realized="10", ts_ms=1_000))
        builder.record(_snap(realized="20", ts_ms=2_000))

        s1 = builder.summary()
        s2 = builder.summary()
        self.assertEqual(s1, s2)


class CurvePropertyTests(unittest.TestCase):
    def test_curve_property_returns_tuple_not_list(self) -> None:
        """curve 返回 tuple 不可变。"""
        builder = EquityBuilder()
        builder.record(_snap(realized="10", ts_ms=1_000))

        curve = builder.curve
        self.assertIsInstance(curve, tuple)
        self.assertNotIsInstance(curve, list)

    def test_curve_property_is_immutable_across_calls(self) -> None:
        """多次调用 curve 返回等价 snapshot，且调用者对 tuple 的操作
        不影响 builder 内部状态。"""
        builder = EquityBuilder()
        builder.record(_snap(realized="10", ts_ms=1_000))
        builder.record(_snap(realized="20", ts_ms=2_000))

        curve1 = builder.curve
        self.assertEqual(len(curve1), 2)

        # tuple 本身不可变，无法 append/pop
        with self.assertRaises(AttributeError):
            curve1.append(curve1[0])  # type: ignore[attr-defined]

        # 再 record 一个后，curve1 仍保留旧快照（因为是 tuple 拷贝）
        builder.record(_snap(realized="30", ts_ms=3_000))
        self.assertEqual(len(curve1), 2)
        curve2 = builder.curve
        self.assertEqual(len(curve2), 3)


if __name__ == "__main__":
    unittest.main()
