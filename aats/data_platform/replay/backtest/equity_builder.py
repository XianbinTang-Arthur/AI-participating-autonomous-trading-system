"""Equity curve builder for backtest MVP.

消费 ``PositionSnapshot`` 流，产出 ``EquityPoint`` 序列 + ``BacktestSummary``。

MVP 假设（严格单一路径）：
    - 无初始本金 notional（equity 从 0 起，仅累计 net PnL）
    - 无 funding rate（funding PnL 由上层另计）
    - net PnL 定义：``realized_pnl + unrealized_pnl - accumulated_fees``
    - Drawdown 按每步 net equity 相对历史最高值算 bps，分母
      ``max(|peak|, Decimal("1"))`` 避免除零
    - Daily return 按最近一个完整 24h 窗口内最早点的 equity 作基线
    - ``sharpe_ratio`` 按不重叠 bar-close PnL increment 的 sample stdev
      （ddof=1）计算，并依据实际中位 cadence 按 365.25 日日历年年化。
      当前无初始本金，因此该字段是显式版本化的 PnL-increment
      risk-adjusted proxy，不得解读为资本收益率 Sharpe。
    - 所有金额用 ``Decimal``，Sharpe 浮点计算（bps 级 float 精度够）
    - 无 I/O、无 logging、无消息总线、无副作用：纯计算

与 live path 隔离：本模块只被 backtest 消费，绝不被 ``aats/services/`` 引用。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, DecimalException

from aats.data_platform.replay.backtest.position_tracker import PositionSnapshot
from aats.domain.instrument_contract import (
    InstrumentContract,
    instrument_arithmetic_context,
)


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
    settlement_currency: str = ""
    instrument_symbol: str = ""
    instrument_contract_fingerprint: str = ""
    # Appended for source compatibility. Complete backtest-run/v2 artifacts
    # require the full position ledger so validators can replay fills and
    # independently recompute net equity instead of trusting a declared total.
    realized_pnl: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    net_qty: Decimal | None = None
    avg_entry_price: Decimal | None = None
    mark_price: Decimal | None = None
    fill_count: int | None = None
    accumulated_fees: Decimal | None = None


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
    settlement_currency: str = ""
    instrument_symbol: str = ""
    instrument_contract_fingerprint: str = ""
    risk_metric_policy_id: str = "calendar-365.25-bar-pnl-increment/v1"

    def __post_init__(self) -> None:
        if type(self.sharpe_ratio) is float and self.sharpe_ratio == 0.0:
            object.__setattr__(self, "sharpe_ratio", 0.0)


# ---------------------------------------------------------------------------
# Equity builder
# ---------------------------------------------------------------------------


_DAY_MS: int = 24 * 60 * 60 * 1000
_MS_PER_CALENDAR_YEAR = Decimal("31557600000")
REPLAY_RISK_METRIC_POLICY_ID = "calendar-365.25-bar-pnl-increment/v1"


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

    def __init__(self, *, instrument_contract: InstrumentContract) -> None:
        if not isinstance(instrument_contract, InstrumentContract):
            raise ValueError("instrument_contract_required")
        self._contract = instrument_contract
        self._settlement_currency = instrument_contract.settle_currency
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
        if snapshot.settlement_currency != self._settlement_currency:
            raise ValueError("position_snapshot_settlement_currency_mismatch")
        if snapshot.instrument_symbol != self._contract.symbol:
            raise ValueError("position_snapshot_instrument_symbol_mismatch")
        if snapshot.instrument_contract_fingerprint != self._contract.fingerprint:
            raise ValueError("position_snapshot_contract_fingerprint_mismatch")

        net_pnl = self._contract.add_settlement_amounts(
            snapshot.realized_pnl,
            snapshot.unrealized_pnl,
            snapshot.accumulated_fees.copy_negate(),
        )

        candidate_peak = (
            net_pnl if not self._curve or net_pnl > self._peak else self._peak
        )

        drawdown_bps = self._compute_drawdown_bps(
            net_pnl,
            peak=candidate_peak,
        )
        daily_return_bps: Decimal = self._compute_daily_return_bps(
            snapshot.ts_ms, net_pnl
        )

        point = EquityPoint(
            ts_ms=snapshot.ts_ms,
            equity=net_pnl,
            cumulative_pnl=net_pnl,
            drawdown_bps=drawdown_bps,
            daily_return_bps=daily_return_bps,
            settlement_currency=self._settlement_currency,
            instrument_symbol=self._contract.symbol,
            instrument_contract_fingerprint=self._contract.fingerprint,
            realized_pnl=snapshot.realized_pnl,
            unrealized_pnl=snapshot.unrealized_pnl,
            net_qty=snapshot.net_qty,
            avg_entry_price=snapshot.avg_entry_price,
            mark_price=snapshot.last_mark_price,
            fill_count=snapshot.fill_count,
            accumulated_fees=snapshot.accumulated_fees,
        )
        self._peak = candidate_peak
        self._curve.append(point)

        # 缓存最新 fill_count / fee（summary 直接取最新，不再扫全量）
        self._latest_fill_count = snapshot.fill_count
        self._latest_fees = snapshot.accumulated_fees

        return point

    def summary(self) -> BacktestSummary:
        """聚合已记录数据为 ``BacktestSummary``。可多次调用，幂等。"""
        if not self._curve:
            return BacktestSummary(
                settlement_currency=self._settlement_currency,
                instrument_symbol=self._contract.symbol,
                instrument_contract_fingerprint=self._contract.fingerprint,
            )

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
            settlement_currency=self._settlement_currency,
            instrument_symbol=self._contract.symbol,
            instrument_contract_fingerprint=self._contract.fingerprint,
        )

    @property
    def curve(self) -> tuple[EquityPoint, ...]:
        """已记录所有 ``EquityPoint`` 的不可变视图。"""
        return tuple(self._curve)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _compute_drawdown_bps(
        self,
        current_net_pnl: Decimal,
        *,
        peak: Decimal,
    ) -> Decimal:
        """相对 ``self._peak`` 的 drawdown，bps。

        公式：``(peak - current) / max(|peak|, 1) * 10000``；非负。
        分母用 ``max(|peak|, 1)`` 避免除零；peak < 0 且 current 更负时
        drawdown 仍非负。
        """
        drawdown = self._contract.add_settlement_amounts(
            peak,
            current_net_pnl.copy_negate(),
        )
        if drawdown <= 0:
            return Decimal("0")
        denom = max(peak.copy_abs(), Decimal("1"))
        return self._contract.settlement_basis_points(
            drawdown,
            denominator=denom,
        )

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

        diff = self._contract.add_settlement_amounts(
            current_net_pnl,
            baseline.copy_negate(),
        )
        denom = max(baseline.copy_abs(), Decimal("1"))
        return self._contract.settlement_basis_points(
            diff,
            denominator=denom,
        )

    def _compute_sharpe(self) -> float:
        """按不重叠 bar PnL increment 计算年化风险调整 proxy。

        - PnL increment 样本少于 2 → 0.0
        - sample stdev (ddof=1) == 0 → 0.0
        - 否则 ``mean / stdev * sqrt(calendar_year / median_bar_spacing)``
        """
        return _compute_pnl_increment_sharpe(
            self._curve,
            instrument_contract=self._contract,
        )


def recompute_equity_curve_metrics(
    curve: Sequence[EquityPoint],
    *,
    instrument_contract: InstrumentContract,
) -> tuple[tuple[tuple[Decimal, Decimal], ...], float]:
    """Recompute drawdown, rolling-24h change and risk proxy from equity only.

    Versioned artifact validation uses this independent reduction so serialized
    metric fields cannot validate themselves.  The returned pair for each point
    is ``(drawdown_bps, daily_return_bps)`` in input order.
    """

    peak = Decimal("0")
    expected: list[tuple[Decimal, Decimal]] = []
    for index, point in enumerate(curve):
        current = point.equity
        candidate_peak = current if index == 0 or current > peak else peak
        drawdown = instrument_contract.add_settlement_amounts(
            candidate_peak,
            current.copy_negate(),
        )
        expected_drawdown = (
            Decimal("0")
            if drawdown <= 0
            else instrument_contract.settlement_basis_points(
                drawdown,
                denominator=max(candidate_peak.copy_abs(), Decimal("1")),
            )
        )

        expected_daily = Decimal("0")
        if index > 0:
            window_start = point.ts_ms - _DAY_MS
            baseline = next(
                (
                    prior.equity
                    for prior in curve[:index]
                    if prior.ts_ms >= window_start
                ),
                None,
            )
            if baseline is not None:
                change = instrument_contract.add_settlement_amounts(
                    current,
                    baseline.copy_negate(),
                )
                expected_daily = instrument_contract.settlement_basis_points(
                    change,
                    denominator=max(baseline.copy_abs(), Decimal("1")),
                )
        expected.append((expected_drawdown, expected_daily))
        peak = candidate_peak

    return (
        tuple(expected),
        _compute_pnl_increment_sharpe(
            curve,
            instrument_contract=instrument_contract,
        ),
    )


def _compute_pnl_increment_sharpe(
    curve: Sequence[EquityPoint],
    *,
    instrument_contract: InstrumentContract,
) -> float:
    if len(curve) < 3:
        return 0.0

    returns = [
        instrument_contract.add_settlement_amounts(
            curve[index].equity,
            curve[index - 1].equity.copy_negate(),
        )
        for index in range(1, len(curve))
    ]
    spacings = sorted(
        curve[index].ts_ms - curve[index - 1].ts_ms
        for index in range(1, len(curve))
    )
    if not spacings or spacings[0] <= 0:
        raise ValueError("equity_sharpe_invalid_cadence")
    n = len(returns)
    try:
        with instrument_arithmetic_context():
            spacing_count = len(spacings)
            if spacing_count % 2:
                median_spacing = Decimal(spacings[spacing_count // 2])
            else:
                median_spacing = Decimal(
                    spacings[spacing_count // 2 - 1]
                    + spacings[spacing_count // 2]
                ) / Decimal("2")
            mean = sum(returns, Decimal("0")) / Decimal(n)
            var = sum(
                ((value - mean) ** 2 for value in returns),
                Decimal("0"),
            ) / Decimal(n - 1)
            if var <= 0:
                return 0.0
            annual_periods = _MS_PER_CALENDAR_YEAR / median_spacing
            sharpe_decimal = mean / var.sqrt() * annual_periods.sqrt()
    except DecimalException as exc:
        raise ValueError("equity_sharpe_non_finite") from exc

    try:
        sharpe = float(sharpe_decimal)
    except (OverflowError, ValueError) as exc:
        raise ValueError("equity_sharpe_non_finite") from exc
    if not math.isfinite(sharpe):
        raise ValueError("equity_sharpe_non_finite")
    return sharpe


__all__ = [
    "BacktestSummary",
    "EquityBuilder",
    "EquityPoint",
    "REPLAY_RISK_METRIC_POLICY_ID",
    "recompute_equity_curve_metrics",
]
