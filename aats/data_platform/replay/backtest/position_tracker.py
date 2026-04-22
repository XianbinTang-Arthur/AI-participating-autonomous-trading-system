"""Position tracker for backtest MVP.

单 symbol 持仓追踪器，回放 fill 序列产出 PnL 序列，供 equity_builder 消费。

MVP 假设（严格单一路径）：
    - single symbol（一个 tracker 实例 = 一个 symbol）
    - no funding rate（funding PnL 由上层另计，不进本 tracker）
    - no multi-account（一个 tracker 实例 = 一个 account）
    - 无 I/O、无 logging、无消息总线、无副作用：纯状态机
    - 所有运算 ``decimal.Decimal``，不允许 float 混入

与 live path 隔离：本模块只被 backtest 消费，绝不被 ``aats/services/`` 引用。

记账语义：
    - WAC (weighted average cost)：加仓时按 qty 加权更新 avg_entry_price
    - 减仓/平仓时锁定 realized_pnl，avg_entry_price 保持剩余仓位的基线
    - 翻仓 = close + open_new 两阶段，realized_pnl 先结再建新仓
    - fee 独立累计（``accumulated_fees``），**不**从 realized_pnl 扣；
      净 PnL（realized + unrealized − fees）由上层 equity_builder 聚合
    - unrealized_pnl 按 ``last_mark_price`` 与 ``avg_entry_price`` 的差值算
    - PnL 计算含 ``contract_multiplier``（OKX BTC-USDT-SWAP ctVal=0.01 BTC/contract）
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


# ---------------------------------------------------------------------------
# DTO：Fill / Snapshot（frozen，外部不可变）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fill:
    """position tracker 消费的 fill 记录。

    与 ``fill_simulator.FillResult`` duck-typed 兼容：字段名/类型一致即可。
    上层把 FillResult 的相关字段映射到 Fill 即可喂入。
    """

    side: Literal["buy", "sell"]
    filled_qty: Decimal
    avg_fill_price: Decimal
    fee_notional: Decimal
    ts_ms: int


@dataclass(frozen=True)
class PositionSnapshot:
    """position 状态快照（逐 fill 后 / 逐 bar 盯市后）。

    Invariants：
        - 无仓时 ``net_qty == 0`` 且 ``avg_entry_price == 0``
        - ``unrealized_pnl == 0`` 当且仅当 ``net_qty == 0``
        - ``accumulated_fees`` 单调不减（fee 只加不退）
    """

    net_qty: Decimal  # 正数 = long, 负数 = short, 0 = flat
    avg_entry_price: Decimal  # 加权平均成本（无仓 = 0）
    realized_pnl: Decimal  # 累计已实现 PnL（含翻仓中平仓部分）
    unrealized_pnl: Decimal  # 按 last_mark_price 算（无仓 = 0）
    last_mark_price: Decimal  # 最近用于盯市的价格
    accumulated_fees: Decimal  # 累计费用（与 realized_pnl 分离）
    fill_count: int  # 累计应用的 fill 数
    ts_ms: int  # 本次快照的时间戳


# ---------------------------------------------------------------------------
# 状态机：PositionTracker
# ---------------------------------------------------------------------------


class PositionTracker:
    """单 symbol 持仓追踪器。

    对外只有三个方法：
        - :meth:`apply_fill`        — 应用一个 fill，返回新快照
        - :meth:`mark_to_market`    — 盯市（只改 unrealized），返回新快照
        - :attr:`snapshot`          — 当前状态的只读视图

    状态变量藏在私有字段，所有对外接口只返回 frozen 的 ``PositionSnapshot``。
    """

    def __init__(
        self,
        symbol: str = "BTC-USDT-SWAP",
        *,
        contract_multiplier: Decimal = Decimal("0.01"),
    ) -> None:
        if contract_multiplier <= 0:
            raise ValueError(
                f"contract_multiplier must be positive, got {contract_multiplier}"
            )
        self._symbol: str = symbol
        self._contract_multiplier: Decimal = contract_multiplier

        # 可变状态（外部只能通过 apply_fill / mark_to_market 间接修改）
        self._net_qty: Decimal = Decimal("0")  # 正=long, 负=short
        self._avg_entry_price: Decimal = Decimal("0")
        self._realized_pnl: Decimal = Decimal("0")
        self._accumulated_fees: Decimal = Decimal("0")
        self._last_mark_price: Decimal = Decimal("0")
        self._fill_count: int = 0
        self._last_ts_ms: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def contract_multiplier(self) -> Decimal:
        return self._contract_multiplier

    @property
    def snapshot(self) -> PositionSnapshot:
        """只读视图，调用不会改变内部状态。"""
        return self._build_snapshot()

    def apply_fill(self, fill: Fill) -> PositionSnapshot:
        """应用一个 fill 更新状态。

        mark_price 在 fill 事件时 = fill.avg_fill_price（fill 成交价即是最新市价参考）。
        """
        self._validate_fill(fill)

        # fill 的 signed qty：买入为正，卖出为负
        signed_fill_qty: Decimal = (
            fill.filled_qty if fill.side == "buy" else -fill.filled_qty
        )
        fill_price: Decimal = fill.avg_fill_price

        old_net_qty: Decimal = self._net_qty

        if old_net_qty == 0:
            # 从 flat 开仓（long 或 short）
            self._open_from_flat(signed_fill_qty, fill_price)
        elif _same_direction(old_net_qty, signed_fill_qty):
            # 同方向 → 加仓
            self._add_to_position(signed_fill_qty, fill_price)
        else:
            # 反方向 fill
            if abs(signed_fill_qty) < abs(old_net_qty):
                # 部分平仓（减仓）
                self._reduce_position(signed_fill_qty, fill_price)
            elif abs(signed_fill_qty) == abs(old_net_qty):
                # 完全平仓
                self._close_position(fill_price)
            else:
                # 翻仓：先平后开
                self._reverse_position(signed_fill_qty, fill_price, old_net_qty)

        # fee 独立累计
        self._accumulated_fees += fill.fee_notional

        # 盯市价 = 本次 fill 价
        self._last_mark_price = fill_price
        self._fill_count += 1
        self._last_ts_ms = fill.ts_ms

        return self._build_snapshot()

    def mark_to_market(self, mark_price: Decimal, ts_ms: int) -> PositionSnapshot:
        """按最新 mark_price 盯市，不改仓位，只更新 unrealized_pnl。"""
        if mark_price <= 0:
            raise ValueError(f"mark_price must be positive, got {mark_price}")
        self._last_mark_price = mark_price
        self._last_ts_ms = ts_ms
        return self._build_snapshot()

    # ------------------------------------------------------------------
    # Internal transitions
    # ------------------------------------------------------------------

    def _open_from_flat(self, signed_fill_qty: Decimal, fill_price: Decimal) -> None:
        self._net_qty = signed_fill_qty
        self._avg_entry_price = fill_price
        # realized_pnl 不变

    def _add_to_position(
        self, signed_fill_qty: Decimal, fill_price: Decimal
    ) -> None:
        old_abs: Decimal = abs(self._net_qty)
        add_abs: Decimal = abs(signed_fill_qty)
        new_abs: Decimal = old_abs + add_abs

        # WAC：按 qty 加权
        self._avg_entry_price = (
            self._avg_entry_price * old_abs + fill_price * add_abs
        ) / new_abs
        self._net_qty = self._net_qty + signed_fill_qty
        # realized_pnl 不变

    def _reduce_position(
        self, signed_fill_qty: Decimal, fill_price: Decimal
    ) -> None:
        """反方向部分平仓：锁定已实现 PnL，avg_entry_price 保持不变。"""
        direction: int = 1 if self._net_qty > 0 else -1
        reduced_qty: Decimal = abs(signed_fill_qty)

        # PnL = reduced_qty * (fill_price - entry) * direction * ct_mult
        self._realized_pnl += (
            reduced_qty
            * (fill_price - self._avg_entry_price)
            * Decimal(direction)
            * self._contract_multiplier
        )
        self._net_qty = self._net_qty + signed_fill_qty
        # avg_entry_price 不变（剩余仓位的基线）

    def _close_position(self, fill_price: Decimal) -> None:
        """完全平仓：结算所有 PnL，仓位归零。"""
        direction: int = 1 if self._net_qty > 0 else -1
        closed_abs: Decimal = abs(self._net_qty)

        self._realized_pnl += (
            closed_abs
            * (fill_price - self._avg_entry_price)
            * Decimal(direction)
            * self._contract_multiplier
        )
        self._net_qty = Decimal("0")
        self._avg_entry_price = Decimal("0")

    def _reverse_position(
        self,
        signed_fill_qty: Decimal,
        fill_price: Decimal,
        old_net_qty: Decimal,
    ) -> None:
        """翻仓 = close 原仓 + open 反向新仓。"""
        # 阶段 1：平原仓（只结算等量 qty）
        direction: int = 1 if old_net_qty > 0 else -1
        old_abs: Decimal = abs(old_net_qty)

        self._realized_pnl += (
            old_abs
            * (fill_price - self._avg_entry_price)
            * Decimal(direction)
            * self._contract_multiplier
        )

        # 阶段 2：建新仓（方向相反，剩余 qty）
        # signed_fill_qty 和 old_net_qty 方向相反，|signed_fill_qty| > |old_net_qty|
        # 新仓 signed qty = signed_fill_qty + old_net_qty（会改方向）
        new_signed_qty: Decimal = signed_fill_qty + old_net_qty
        self._net_qty = new_signed_qty
        self._avg_entry_price = fill_price

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_fill(fill: Fill) -> None:
        if fill.side not in ("buy", "sell"):
            raise ValueError(f"fill.side must be 'buy' or 'sell', got {fill.side!r}")
        if fill.filled_qty <= 0:
            raise ValueError(
                f"fill.filled_qty must be positive, got {fill.filled_qty}"
            )
        if fill.avg_fill_price <= 0:
            raise ValueError(
                f"fill.avg_fill_price must be positive, got {fill.avg_fill_price}"
            )
        if fill.fee_notional < 0:
            raise ValueError(
                f"fill.fee_notional must be non-negative, got {fill.fee_notional}"
            )

    def _compute_unrealized(self) -> Decimal:
        if self._net_qty == 0:
            return Decimal("0")
        direction: int = 1 if self._net_qty > 0 else -1
        return (
            abs(self._net_qty)
            * (self._last_mark_price - self._avg_entry_price)
            * Decimal(direction)
            * self._contract_multiplier
        )

    def _build_snapshot(self) -> PositionSnapshot:
        return PositionSnapshot(
            net_qty=self._net_qty,
            avg_entry_price=self._avg_entry_price,
            realized_pnl=self._realized_pnl,
            unrealized_pnl=self._compute_unrealized(),
            last_mark_price=self._last_mark_price,
            accumulated_fees=self._accumulated_fees,
            fill_count=self._fill_count,
            ts_ms=self._last_ts_ms,
        )


# ---------------------------------------------------------------------------
# Module-local helpers
# ---------------------------------------------------------------------------


def _same_direction(a: Decimal, b: Decimal) -> bool:
    """两个 signed 数是否同方向（同号）。零不在本辅助的定义域内。"""
    return (a > 0 and b > 0) or (a < 0 and b < 0)
