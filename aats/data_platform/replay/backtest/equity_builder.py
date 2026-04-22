"""Equity curve builder for backtest MVP.

消费 ``PositionSnapshot`` 流，产出 ``EquityPoint`` 序列 + ``BacktestSummary``。

MVP 假设（严格单一路径）：
    - 无初始本金 notional（equity 从 0 起，仅累计 net PnL）
    - 无 funding rate（funding PnL 由上层另计）
    - net PnL 定义：``realized_pnl + unrealized_pnl - accumulated_fees``
    - Drawdown 按每步 net equity 相对历史最高值算 bps，分母
      ``max(|peak|, Decimal("1"))`` 避免除零
    - Daily return 按最近一个完整 24h 窗口内最早点的 equity 作基线
    - Sharpe 按 ``daily_return_bps`` 序列的 sample stdev（ddof=1）算，
      年化因子 ``sqrt(252)``；序列长度 < 2 或 stdev == 0 时返回 ``0.0``
    - 所有金额用 ``Decimal``，Sharpe 浮点计算（bps 级 float 精度够）
    - 无 I/O、无 logging、无消息总线、无副作用：纯计算

与 live path 隔离：本模块只被 backtest 消费，绝不被 ``aats/services/`` 引用。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from aats.data_platform.replay.backtest.position_tracker import PositionSnapshot


# ---------------------------------------------------------------------------
# DTO（frozen，外部不可变）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EquityPoint:
    """单个时间点的 equity snapshot。

    Semantics:
        - ``equity`` == ``cumulative_pnl``（MVP 从 0 起计 net PnL，不含本金）
        - ``drawdown_bps`` 非负；无仓无回撤时为 0
        - ``daily_return_bps`` 基线取 24h 窗口内最早点；
          首点或窗口不足时为 0
    """

    ts_ms: int
    equity: Decimal
    cumulative_pnl: Decimal
    drawdown_bps: Decimal
    daily_return_bps: Decimal


@dataclass(frozen=True)
class BacktestSummary:
    """回测完整结果指标。

    MVP 从 0 起计 net PnL；``initial_equity`` 恒为 0。
    空序列时所有 Decimal = 0、Sharpe = 0.0、bar_count = 0、ts 边界 = 0。
    """

    initial_equity: Decimal = Decimal("0")
    final_equity: Decimal = Decimal("0")
    cumulative_pnl: Decimal = Decimal("0")
    max_drawdown_bps: Decimal = Decimal("0")
    sharpe_ratio: float = 0.0
    fill_count: int = 0
    fee_total: Decimal = Decimal("0")
    bar_count: int = 0
    start_ts_ms: int = 0
    end_ts_ms: int = 0


# ---------------------------------------------------------------------------
# Equity builder
# ---------------------------------------------------------------------------


_DAY_MS: int = 24 * 60 * 60 * 1000
_TRADING_DAYS_PER_YEAR: float = 252.0


class EquityBuilder:
    """消费 ``PositionSnapshot`` 流，产出 ``EquityPoint`` 序列 + ``BacktestSummary``。

    Net PnL 定义：
        ``net_pnl = snapshot.realized_pnl + snapshot.unrealized_pnl
                   - snapshot.accumulated_fees``

    对外接口：
        - :meth:`record`   — 消费一个 snapshot，返回当前 ``EquityPoint``
        - :meth:`summary`  — 聚合所有数据返回 ``BacktestSummary``，可多次调用
        - :attr:`curve`    — 已记录所有 ``EquityPoint``（不可变 tuple）

    无 I/O、无 logging、无副作用：纯计算。
    """

    def __init__(self) -> None:
        self._curve: list[EquityPoint] = []
        self._peak: Decimal = Decimal("0")
        self._latest_fill_count: int = 0
        self._latest_fees: Decimal = Decimal("0")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, snapshot: PositionSnapshot) -> EquityPoint:
        """消费一个 snapshot，构造并存储 ``EquityPoint``，返回该点。

        内部维护 ``peak_equity`` / 历史 curve 以便后续 drawdown / daily return
        / Sharpe 计算。
        """
        net_pnl: Decimal = (
            snapshot.realized_pnl
            + snapshot.unrealized_pnl
            - snapshot.accumulated_fees
        )

        # peak 更新（首点时 _peak 从 0 起，若 net_pnl > 0 直接替换）
        if not self._curve:
            self._peak = net_pnl
        elif net_pnl > self._peak:
            self._peak = net_pnl

        drawdown_bps: Decimal = self._compute_drawdown_bps(net_pnl)
        daily_return_bps: Decimal = self._compute_daily_return_bps(
            snapshot.ts_ms, net_pnl
        )

        point = EquityPoint(
            ts_ms=snapshot.ts_ms,
            equity=net_pnl,
            cumulative_pnl=net_pnl,
            drawdown_bps=drawdown_bps,
            daily_return_bps=daily_return_bps,
        )
        self._curve.append(point)

        # 缓存最新 fill_count / fee（summary 直接取最新，不再扫全量）
        self._latest_fill_count = snapshot.fill_count
        self._latest_fees = snapshot.accumulated_fees

        return point

    def summary(self) -> BacktestSummary:
        """聚合已记录数据为 ``BacktestSummary``。可多次调用，幂等。"""
        if not self._curve:
            return BacktestSummary()

        last: EquityPoint = self._curve[-1]
        first: EquityPoint = self._curve[0]

        max_dd: Decimal = max(p.drawdown_bps for p in self._curve)
        sharpe: float = self._compute_sharpe()

        return BacktestSummary(
            initial_equity=Decimal("0"),
            final_equity=last.equity,
            cumulative_pnl=last.cumulative_pnl,
            max_drawdown_bps=max_dd,
            sharpe_ratio=sharpe,
            fill_count=self._latest_fill_count,
            fee_total=self._latest_fees,
            bar_count=len(self._curve),
            start_ts_ms=first.ts_ms,
            end_ts_ms=last.ts_ms,
        )

    @property
    def curve(self) -> tuple[EquityPoint, ...]:
        """已记录所有 ``EquityPoint`` 的不可变视图。"""
        return tuple(self._curve)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _compute_drawdown_bps(self, current_net_pnl: Decimal) -> Decimal:
        """相对 ``self._peak`` 的 drawdown，bps。

        公式：``(peak - current) / max(|peak|, 1) * 10000``；非负。
        分母用 ``max(|peak|, 1)`` 避免除零；peak < 0 且 current 更负时
        drawdown 仍非负。
        """
        drawdown: Decimal = self._peak - current_net_pnl
        if drawdown <= 0:
            return Decimal("0")
        denom: Decimal = max(abs(self._peak), Decimal("1"))
        return drawdown / denom * Decimal("10000")

    def _compute_daily_return_bps(
        self, ts_ms: int, current_net_pnl: Decimal
    ) -> Decimal:
        """相对 24h 窗口内最早点 equity 的 return，bps。

        找 curve 里第一个满足 ``p.ts_ms >= ts_ms - 24h`` 的点（不含当前点）
        作基线；无符合点则返回 0。
        """
        if not self._curve:
            return Decimal("0")

        window_start: int = ts_ms - _DAY_MS
        baseline: Decimal | None = None
        for p in self._curve:
            if p.ts_ms >= window_start:
                baseline = p.equity
                break
        if baseline is None:
            return Decimal("0")

        diff: Decimal = current_net_pnl - baseline
        denom: Decimal = max(abs(baseline), Decimal("1"))
        return diff / denom * Decimal("10000")

    def _compute_sharpe(self) -> float:
        """按 ``daily_return_bps`` 序列算年化 Sharpe。

        - 序列长度 < 2 → 0.0
        - sample stdev (ddof=1) == 0 → 0.0
        - 否则 ``mean / stdev * sqrt(252)``
        """
        if len(self._curve) < 2:
            return 0.0

        returns: list[float] = [float(p.daily_return_bps) for p in self._curve]
        n: int = len(returns)
        mean: float = sum(returns) / n
        # sample variance with ddof=1
        var: float = sum((r - mean) ** 2 for r in returns) / (n - 1)
        if var <= 0.0:
            return 0.0
        stdev: float = math.sqrt(var)
        return mean / stdev * math.sqrt(_TRADING_DAYS_PER_YEAR)


__all__ = [
    "BacktestSummary",
    "EquityBuilder",
    "EquityPoint",
]
