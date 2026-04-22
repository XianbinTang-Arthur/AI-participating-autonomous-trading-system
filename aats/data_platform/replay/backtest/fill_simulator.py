"""Backtest Fill Simulator — 纯函数式成交模拟器（MVP）.

背景
----
AATS 已有 replay core（aats/data_platform/replay/）能够回放历史 candle 并产出策略
决策，但尚缺 fill 模拟、position tracking 与 PnL 计算。本模块补齐 fill 一环：
给定一笔下单意图（``FillRequest``）与当前 bar 的 close/volume，返回模拟成交结果
（``FillResult``），从而让 backtest runner 能够连接 "信号 → 订单 → 仓位 → 盈亏"
闭环。

与 aats/data_platform/execution_realism/slippage_estimator.py 的关系：
    slippage_estimator 面向 "候选订单可行性批量评估"，以 dict-of-floats 为载体；
    本模块面向 "单笔订单的 deterministic 成交"，以 Decimal dataclass 为载体。
    两者关注点不同、类型严格度不同，所以独立实现。

与 live path 的隔离
-------------------
本模块绝不导入任何 live execution / services / configs 代码，也不产生副作用
（无 logging、无 DB、无网络）。它只是 replay 侧的一个纯函数对象。

设计问题（主协作者讨论过的 3 个关键点）
---------------------------------------
Q1：成交价模型选什么？
    方案 A：bar close ± slippage（本模块选择）。优点：实现简单、deterministic、
             符合 MVP "先能跑通" 的目标；
    方案 B：bar VWAP。缺点：V1 gold 表里不一定有 VWAP；
    方案 C：bar range 内采样。缺点：需要额外随机源且不易 reproduce。
    决策：采用 A。IOC 在 close 基础上加/减 ``ioc_slippage_bps`` 代表 taker
           穿盘口的成本；post_only 不加 slippage（maker 定义上只在限价触达时成交）。

Q2：post_only 成交率模型？
    Explore agent 建议按 volume_ratio 分段：
        < 1%  → high prob（默认 0.90）
        1-5%  → mid prob（默认 0.60）
        5-10% → low prob（默认 0.20）
        > 10% → 必不成交
    本模块采用这个分段，可通过构造参数覆盖。用 hashlib.md5(seed) 产生
    deterministic 的 [0, 1) 抽样，保证同 order_id 同种子同 outcome，
    backtest 可完整复现。

Q3：bounded_limit 的成交价与费率？
    bounded_limit 既有 maker 先挂单部分也有 taker 兜底部分。MVP 简化为
    100% 成交 @ bar_close（不加 slippage，因为有部分 maker fill 改善均价），
    fee 取 ``0.5 * (maker_fee_bps + taker_fee_bps)`` = 3.5 bps。fill_kind
    保守归类为 "taker"，因为真实场景下至少有一部分 taker leg 触发。

所有金额/价格计算走 ``Decimal``，float 只保留给配置项（fee_bps, slippage_bps
等），避免中间量的二进制舍入误差。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

# ---------------------------------------------------------------------------
# 类型别名
# ---------------------------------------------------------------------------

OrderType = Literal["ioc", "post_only", "bounded_limit"]
OrderSide = Literal["buy", "sell"]

FillKind = Literal["taker", "maker", "no_fill"]


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FillRequest:
    """一次下单意图。

    Attributes:
        order_id: 唯一标识（也用作 deterministic 概率抽样的 fallback 种子）。
        side: buy / sell。
        order_type: ioc / post_only / bounded_limit。
        target_qty: 想要成交的数量（正数），单位同 replay bar 的 volume 单位
                    （合约张数或 base asset 数量，调用方保持一致即可）。
        submitted_at_ts: 毫秒时间戳，用于排序/审计，不直接影响成交逻辑。
    """
    order_id: str
    side: OrderSide
    order_type: OrderType
    target_qty: Decimal
    submitted_at_ts: int


@dataclass(frozen=True)
class FillResult:
    """模拟成交结果。

    约定：
        * ``filled_qty == 0`` 时，``avg_fill_price`` / ``fee_bps`` /
          ``fee_notional`` 一律为零值（Decimal(0) / 0.0），``fill_kind``
          为 "no_fill"，``notes`` 描述原因。
        * 成交时 ``avg_fill_price > 0``。
    """
    order_id: str
    side: OrderSide
    filled_qty: Decimal
    avg_fill_price: Decimal
    fee_bps: float
    fee_notional: Decimal
    fill_kind: FillKind
    notes: str = ""


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# volume_ratio 分段阈值（与 execution_realism.fill_feasibility 的分段思路一致）
_POST_ONLY_HIGH_RATIO = Decimal("0.01")   # < 1%
_POST_ONLY_MID_RATIO = Decimal("0.05")    # 1% ~ 5%
_POST_ONLY_LOW_RATIO = Decimal("0.10")    # 5% ~ 10%
# > 10% → 必不成交

# bps → ratio
_BPS_DENOM = Decimal("10000")

# deterministic 抽样：md5 截断到 16 字节 → 128-bit int → 除以 2**128
_MD5_MAX = Decimal(2) ** 128


# ---------------------------------------------------------------------------
# 模拟器
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FillSimulator:
    """纯函数式 fill 模拟器，无状态。

    所有参数在构造时传入，调用 ``simulate`` 仅基于入参计算，不修改任何 self 字段。
    因为是 frozen dataclass，多个 backtest runner 可以共享同一个实例。
    """

    maker_fee_bps: float = 2.0
    taker_fee_bps: float = 5.0
    ioc_slippage_bps: float = 1.0

    # Q2: volume_ratio 分段成交率（post_only）
    post_only_fill_prob_high: float = 0.90   # qty/bar_vol < 1%
    post_only_fill_prob_mid: float = 0.60    # 1% ~ 5%
    post_only_fill_prob_low: float = 0.20    # 5% ~ 10%
    # > 10% 强制 no_fill

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def simulate(
        self,
        request: FillRequest,
        bar_close_price: Decimal,
        bar_volume: Decimal,
        *,
        rng_seed: str | None = None,
    ) -> FillResult:
        """模拟一次下单。

        绝不抛异常：对任何无效输入返回 no_fill + notes 解释。

        Args:
            request: 下单意图。
            bar_close_price: 当前 bar 收盘价（Decimal）。
            bar_volume: 当前 bar 成交量（Decimal，单位同 ``target_qty``）。
            rng_seed: post_only 概率抽样的种子，缺省时用 ``request.order_id``。
                      同一 (seed, order_id) 给相同的抽样值，保证可复现。

        Returns:
            FillResult。
        """
        # -- 1. 入参健壮性检查 --
        if request.target_qty <= 0:
            return self._no_fill(request, notes="non-positive qty")

        if bar_close_price <= 0:
            return self._no_fill(request, notes="non-positive bar_close_price")

        # -- 2. 分支派发 --
        if request.order_type == "ioc":
            return self._simulate_ioc(request, bar_close_price)

        if request.order_type == "post_only":
            return self._simulate_post_only(
                request,
                bar_close_price=bar_close_price,
                bar_volume=bar_volume,
                rng_seed=rng_seed,
            )

        if request.order_type == "bounded_limit":
            return self._simulate_bounded_limit(request, bar_close_price)

        # 未知 order_type（类型系统应已拦截，这里只作防御）
        return self._no_fill(request, notes=f"unknown order_type={request.order_type!r}")

    # ------------------------------------------------------------------
    # 分支实现
    # ------------------------------------------------------------------

    def _simulate_ioc(
        self,
        request: FillRequest,
        bar_close_price: Decimal,
    ) -> FillResult:
        """IOC：100% 成交 @ bar_close ± ioc_slippage_bps。"""
        direction = Decimal(1) if request.side == "buy" else Decimal(-1)
        slip_ratio = direction * Decimal(str(self.ioc_slippage_bps)) / _BPS_DENOM
        avg_price = bar_close_price * (Decimal(1) + slip_ratio)

        fee_bps = self.taker_fee_bps
        fee_ratio = Decimal(str(fee_bps)) / _BPS_DENOM
        fee_notional = request.target_qty * avg_price * fee_ratio

        return FillResult(
            order_id=request.order_id,
            side=request.side,
            filled_qty=request.target_qty,
            avg_fill_price=avg_price,
            fee_bps=fee_bps,
            fee_notional=fee_notional,
            fill_kind="taker",
            notes="ioc filled",
        )

    def _simulate_post_only(
        self,
        request: FillRequest,
        *,
        bar_close_price: Decimal,
        bar_volume: Decimal,
        rng_seed: str | None,
    ) -> FillResult:
        """post_only：按 volume_ratio 分段概率决定成交。

        成交 → maker fill @ bar_close，无 slippage；否则 no_fill。
        """
        if bar_volume <= 0:
            return self._no_fill(request, notes="zero bar_volume")

        volume_ratio = request.target_qty / bar_volume
        fill_prob = self._post_only_fill_prob(volume_ratio)

        if fill_prob <= 0.0:
            return self._no_fill(
                request,
                notes=(
                    f"post_only ratio {self._fmt_pct(volume_ratio)} > 10% "
                    f"volume_ratio={self._fmt_pct(volume_ratio)} fill_prob=0.0%"
                ),
            )

        # deterministic [0, 1) 抽样
        sample = _deterministic_uniform(rng_seed or request.order_id)
        if sample >= fill_prob:
            return self._no_fill(
                request,
                notes=(
                    f"post_only missed: "
                    f"volume_ratio={self._fmt_pct(volume_ratio)} "
                    f"fill_prob={fill_prob * 100:.1f}% "
                    f"sample={sample:.4f}"
                ),
            )

        # 成交：maker fill @ bar_close（无 slippage）
        fee_bps = self.maker_fee_bps
        fee_ratio = Decimal(str(fee_bps)) / _BPS_DENOM
        fee_notional = request.target_qty * bar_close_price * fee_ratio

        return FillResult(
            order_id=request.order_id,
            side=request.side,
            filled_qty=request.target_qty,
            avg_fill_price=bar_close_price,
            fee_bps=fee_bps,
            fee_notional=fee_notional,
            fill_kind="maker",
            notes=(
                f"post_only filled: "
                f"volume_ratio={self._fmt_pct(volume_ratio)} "
                f"fill_prob={fill_prob * 100:.1f}%"
            ),
        )

    def _simulate_bounded_limit(
        self,
        request: FillRequest,
        bar_close_price: Decimal,
    ) -> FillResult:
        """bounded_limit：100% 成交 @ bar_close，fee = 0.5*(maker + taker)。"""
        fee_bps = 0.5 * (self.maker_fee_bps + self.taker_fee_bps)
        fee_ratio = Decimal(str(fee_bps)) / _BPS_DENOM
        fee_notional = request.target_qty * bar_close_price * fee_ratio

        return FillResult(
            order_id=request.order_id,
            side=request.side,
            filled_qty=request.target_qty,
            avg_fill_price=bar_close_price,
            fee_bps=fee_bps,
            fee_notional=fee_notional,
            # 保守归类：真实 bounded_limit 至少有部分 taker leg 触发
            fill_kind="taker",
            notes="bounded_limit filled (blended fee)",
        )

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _post_only_fill_prob(self, volume_ratio: Decimal) -> float:
        """根据 volume_ratio 返回成交概率（0.0 ~ 1.0）。"""
        if volume_ratio < _POST_ONLY_HIGH_RATIO:
            return self.post_only_fill_prob_high
        if volume_ratio < _POST_ONLY_MID_RATIO:
            return self.post_only_fill_prob_mid
        if volume_ratio < _POST_ONLY_LOW_RATIO:
            return self.post_only_fill_prob_low
        return 0.0

    @staticmethod
    def _no_fill(request: FillRequest, *, notes: str) -> FillResult:
        """构造一个统一的 no_fill 结果。"""
        return FillResult(
            order_id=request.order_id,
            side=request.side,
            filled_qty=Decimal(0),
            avg_fill_price=Decimal(0),
            fee_bps=0.0,
            fee_notional=Decimal(0),
            fill_kind="no_fill",
            notes=notes,
        )

    @staticmethod
    def _fmt_pct(ratio: Decimal) -> str:
        """格式化百分比（仅用于 notes，不参与计算）。"""
        return f"{float(ratio) * 100:.2f}%"


# ---------------------------------------------------------------------------
# deterministic uniform sampler
# ---------------------------------------------------------------------------

def _deterministic_uniform(seed: str) -> float:
    """基于 md5(seed) 得到 [0, 1) 的 deterministic 抽样值。

    用 md5 是因为：
        * 纯函数；
        * 同 seed 同 outcome，保证 backtest 可复现；
        * 分布对 "是否 < fill_prob" 这种门槛比较足够均匀（非加密用途）。

    Args:
        seed: 任意字符串（order_id 或调用方传入的 rng_seed）。

    Returns:
        [0.0, 1.0) 范围内的 float。
    """
    digest = hashlib.md5(seed.encode("utf-8")).digest()
    as_int = int.from_bytes(digest, "big")
    # Decimal 精确除法再转 float，避免大整数直接 float 化的舍入不稳
    return float(Decimal(as_int) / _MD5_MAX)
