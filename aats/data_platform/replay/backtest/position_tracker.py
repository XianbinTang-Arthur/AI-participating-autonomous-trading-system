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
    - WAC (weighted average cost)：linear/spot 用算术加权，inverse 用调和成本
    - 减仓/平仓时锁定 realized_pnl，avg_entry_price 保持剩余仓位的基线
    - 翻仓 = close + open_new 两阶段，realized_pnl 先结再建新仓
    - fee 独立累计（``accumulated_fees``），**不**从 realized_pnl 扣；
      净 PnL（realized + unrealized − fees）由上层 equity_builder 聚合
    - unrealized_pnl 按 ``last_mark_price`` 与 ``avg_entry_price`` 的差值算
    - fee/PnL/数量换算全部委托给显式 ``InstrumentContract``；不存在默认乘数
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from aats.domain.instrument_contract import InstrumentContract


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
    fee_currency: str
    instrument_symbol: str
    instrument_contract_fingerprint: str
    ts_ms: int
    # ``fee_notional`` remains the settlement-currency valuation.  These
    # optional fields identify the asset actually charged; omission preserves
    # the historical settlement-asset behavior for in-memory callers.
    fee_asset: str | None = None
    fee_asset_quantity: Decimal | None = None


@dataclass(frozen=True)
class PositionSnapshot:
    """position 状态快照（逐 fill 后 / 逐 bar 盯市后）。

    Invariants：
        - 无仓时 ``net_qty == 0`` 且 ``avg_entry_price == 0``
        - 无仓时 ``unrealized_pnl == 0``
        - ``accumulated_fees`` 是有符号累计费用（maker rebate 可令其下降）
    """

    net_qty: Decimal  # 正数 = long, 负数 = short, 0 = flat
    avg_entry_price: Decimal  # 加权平均成本（无仓 = 0）
    realized_pnl: Decimal  # 累计已实现 PnL（含翻仓中平仓部分）
    unrealized_pnl: Decimal  # 按 last_mark_price 算（无仓 = 0）
    last_mark_price: Decimal  # 最近用于盯市的价格
    accumulated_fees: Decimal  # 累计费用（与 realized_pnl 分离）
    fill_count: int  # 累计应用的 fill 数
    ts_ms: int  # 本次快照的时间戳
    settlement_currency: str  # realized/unrealized/fee/equity 的唯一币种
    instrument_symbol: str
    instrument_contract_fingerprint: str


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
        instrument_contract: InstrumentContract,
    ) -> None:
        if not isinstance(instrument_contract, InstrumentContract):
            raise ValueError("instrument_contract_required")
        if instrument_contract.instrument_type == "MARGIN":
            raise ValueError("margin_position_accounting_unavailable")
        self._contract = instrument_contract
        self._symbol = instrument_contract.symbol

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
    def instrument_contract(self) -> InstrumentContract:
        return self._contract

    @property
    def snapshot(self) -> PositionSnapshot:
        """只读视图，调用不会改变内部状态。"""
        return self._build_snapshot()

    def apply_fill(self, fill: Fill) -> PositionSnapshot:
        """应用一个 fill 更新状态。

        mark_price 在 fill 事件时 = fill.avg_fill_price（fill 成交价即是最新市价参考）。
        """
        self._validate_fill(fill)
        checkpoint = self._capture_state()

        # fill 的 gross signed qty：买入为正，卖出为负。SPOT 若手续费从
        # base asset 扣除，实际库存变化还必须减去该 fee asset quantity。
        gross_signed_fill_qty: Decimal = (
            fill.filled_qty
            if fill.side == "buy"
            else fill.filled_qty.copy_negate()
        )
        fee_asset, fee_asset_quantity = self._resolve_fee_lineage(fill)
        signed_fill_qty = gross_signed_fill_qty
        if (
            self._contract.contract_type == "spot"
            and fee_asset == self._contract.base_currency
        ):
            signed_fill_qty = self._contract.add_exchange_quantities(
                gross_signed_fill_qty,
                fee_asset_quantity.copy_negate(),
            )
            if signed_fill_qty == 0 or not _same_direction(
                gross_signed_fill_qty,
                signed_fill_qty,
            ):
                raise ValueError("fill_base_fee_invalid_inventory_delta")
        fill_price: Decimal = fill.avg_fill_price

        old_net_qty: Decimal = self._net_qty
        prospective_net_qty = self._contract.add_exchange_quantities(
            old_net_qty,
            signed_fill_qty,
        )
        if (
            self._contract.instrument_type == "SPOT"
            and prospective_net_qty < Decimal("0")
        ):
            raise ValueError("spot_short_position_unavailable")

        try:
            if old_net_qty == 0:
                self._open_from_flat(signed_fill_qty, fill_price)
            elif _same_direction(old_net_qty, signed_fill_qty):
                self._add_to_position(signed_fill_qty, fill_price)
            else:
                fill_abs = signed_fill_qty.copy_abs()
                old_abs = old_net_qty.copy_abs()
                if fill_abs < old_abs:
                    self._reduce_position(signed_fill_qty, fill_price)
                elif fill_abs == old_abs:
                    self._close_position(fill_price)
                else:
                    self._reverse_position(signed_fill_qty, fill_price, old_net_qty)

            self._accumulated_fees = self._contract.add_settlement_amounts(
                self._accumulated_fees,
                fill.fee_notional,
            )
            self._last_mark_price = fill_price
            self._fill_count += 1
            self._last_ts_ms = fill.ts_ms
            return self._build_snapshot()
        except Exception:
            self._restore_state(checkpoint)
            raise

    def mark_to_market(self, mark_price: Decimal, ts_ms: int) -> PositionSnapshot:
        """按最新 mark_price 盯市，不改仓位，只更新 unrealized_pnl。"""
        if not isinstance(mark_price, Decimal) or not (
            mark_price.is_finite() and mark_price > 0
        ):
            raise ValueError(f"mark_price must be positive, got {mark_price}")
        checkpoint = self._capture_state()
        try:
            self._last_mark_price = mark_price
            self._last_ts_ms = ts_ms
            return self._build_snapshot()
        except Exception:
            self._restore_state(checkpoint)
            raise

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
        old_abs: Decimal = self._net_qty.copy_abs()
        add_abs: Decimal = signed_fill_qty.copy_abs()

        self._avg_entry_price = self._contract.combined_entry_price(
            old_abs,
            existing_price=self._avg_entry_price,
            added_quantity=add_abs,
            added_price=fill_price,
        )
        self._net_qty = self._contract.add_exchange_quantities(
            self._net_qty,
            signed_fill_qty,
        )
        # realized_pnl 不变

    def _reduce_position(
        self, signed_fill_qty: Decimal, fill_price: Decimal
    ) -> None:
        """反方向部分平仓：锁定已实现 PnL，avg_entry_price 保持不变。"""
        direction: int = 1 if self._net_qty > 0 else -1
        reduced_qty: Decimal = signed_fill_qty.copy_abs()
        signed_reduced_qty = (
            reduced_qty if direction > 0 else reduced_qty.copy_negate()
        )

        self._realized_pnl = self._contract.add_settlement_amounts(
            self._realized_pnl,
            self._contract.settlement_pnl(
                signed_reduced_qty,
                entry_price=self._avg_entry_price,
                exit_price=fill_price,
            ),
        )
        self._net_qty = self._contract.add_exchange_quantities(
            self._net_qty,
            signed_fill_qty,
        )
        # avg_entry_price 不变（剩余仓位的基线）

    def _close_position(self, fill_price: Decimal) -> None:
        """完全平仓：结算所有 PnL，仓位归零。"""
        direction: int = 1 if self._net_qty > 0 else -1
        closed_abs: Decimal = self._net_qty.copy_abs()
        signed_closed_qty = closed_abs if direction > 0 else closed_abs.copy_negate()

        self._realized_pnl = self._contract.add_settlement_amounts(
            self._realized_pnl,
            self._contract.settlement_pnl(
                signed_closed_qty,
                entry_price=self._avg_entry_price,
                exit_price=fill_price,
            ),
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
        old_abs: Decimal = old_net_qty.copy_abs()
        signed_old_qty = old_abs if direction > 0 else old_abs.copy_negate()

        self._realized_pnl = self._contract.add_settlement_amounts(
            self._realized_pnl,
            self._contract.settlement_pnl(
                signed_old_qty,
                entry_price=self._avg_entry_price,
                exit_price=fill_price,
            ),
        )

        # 阶段 2：建新仓（方向相反，剩余 qty）
        # signed_fill_qty 和 old_net_qty 方向相反，|signed_fill_qty| > |old_net_qty|
        # 新仓 signed qty = signed_fill_qty + old_net_qty（会改方向）
        new_signed_qty = self._contract.add_exchange_quantities(
            signed_fill_qty,
            old_net_qty,
        )
        self._net_qty = new_signed_qty
        self._avg_entry_price = fill_price

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _validate_fill(self, fill: Fill) -> None:
        if fill.side not in ("buy", "sell"):
            raise ValueError(f"fill.side must be 'buy' or 'sell', got {fill.side!r}")
        if not isinstance(fill.filled_qty, Decimal) or not (
            fill.filled_qty.is_finite() and fill.filled_qty > 0
        ):
            raise ValueError(
                f"fill.filled_qty must be positive, got {fill.filled_qty}"
            )
        self._contract.validate_exchange_quantity(fill.filled_qty)
        if not isinstance(fill.avg_fill_price, Decimal) or not (
            fill.avg_fill_price.is_finite() and fill.avg_fill_price > 0
        ):
            raise ValueError(
                f"fill.avg_fill_price must be positive, got {fill.avg_fill_price}"
            )
        if not isinstance(fill.fee_notional, Decimal) or not (
            fill.fee_notional.is_finite()
        ):
            raise ValueError(
                f"fill.fee_notional must be finite, got {fill.fee_notional}"
            )
        if str(fill.fee_currency or "").strip().upper() != self._contract.settle_currency:
            raise ValueError("fill_fee_currency_mismatch")
        self._resolve_fee_lineage(fill)
        if str(fill.instrument_symbol or "").strip().upper() != self._contract.symbol:
            raise ValueError("fill_instrument_symbol_mismatch")
        if fill.instrument_contract_fingerprint != self._contract.fingerprint:
            raise ValueError("fill_instrument_contract_fingerprint_mismatch")

    def _resolve_fee_lineage(self, fill: Fill) -> tuple[str, Decimal]:
        """Validate and return the actual fee asset and signed quantity."""

        if (fill.fee_asset is None) != (fill.fee_asset_quantity is None):
            raise ValueError("fill_fee_asset_lineage_incomplete")
        fee_asset = str(fill.fee_asset or fill.fee_currency or "").strip().upper()
        fee_asset_quantity = (
            fill.fee_notional
            if fill.fee_asset_quantity is None
            else fill.fee_asset_quantity
        )
        if not isinstance(fee_asset_quantity, Decimal) or not (
            fee_asset_quantity.is_finite()
        ):
            raise ValueError("fill_fee_asset_quantity_invalid")
        try:
            settlement_value = self._contract.fee_settlement_value(
                fee_asset_quantity,
                fee_asset=fee_asset,
                price=fill.avg_fill_price,
            )
        except ValueError as exc:
            raise ValueError("fill_fee_asset_unsupported") from exc
        if settlement_value != fill.fee_notional:
            raise ValueError("fill_fee_asset_settlement_value_mismatch")
        return fee_asset, fee_asset_quantity

    def _compute_unrealized(self) -> Decimal:
        if self._net_qty == 0:
            return Decimal("0")
        return self._contract.settlement_pnl(
            self._net_qty,
            entry_price=self._avg_entry_price,
            exit_price=self._last_mark_price,
        )

    def _capture_state(self) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, int, int]:
        return (
            self._net_qty,
            self._avg_entry_price,
            self._realized_pnl,
            self._accumulated_fees,
            self._last_mark_price,
            self._fill_count,
            self._last_ts_ms,
        )

    def _restore_state(
        self,
        state: tuple[Decimal, Decimal, Decimal, Decimal, Decimal, int, int],
    ) -> None:
        (
            self._net_qty,
            self._avg_entry_price,
            self._realized_pnl,
            self._accumulated_fees,
            self._last_mark_price,
            self._fill_count,
            self._last_ts_ms,
        ) = state

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
            settlement_currency=self._contract.settle_currency,
            instrument_symbol=self._contract.symbol,
            instrument_contract_fingerprint=self._contract.fingerprint,
        )


# ---------------------------------------------------------------------------
# Module-local helpers
# ---------------------------------------------------------------------------


def _same_direction(a: Decimal, b: Decimal) -> bool:
    """两个 signed 数是否同方向（同号）。零不在本辅助的定义域内。"""
    return (a > 0 and b > 0) or (a < 0 and b < 0)
