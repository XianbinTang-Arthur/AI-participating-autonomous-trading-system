"""Replay context: bar-level data model for historical replay.

Phase 2 replay 以 Gold replay bars 为输入，逐 bar 构建上下文供策略 adapter 评估。
本模块定义 replay 流程中所有共享的数据结构。

Edge Contract（P0-3 统一语义）：
    所有 family adapter 必须按以下 4 层分解输出 edge：
    - signal_edge_proxy_bps:   来自策略信号（score / momentum / trend / alpha）的机会代理
    - funding_adjustment_bps:  来自 funding rate 的附加调整
    - cost_bps:               成本总计（taker_fee + slippage）
    - expected_net_edge_bps:  = signal_edge_proxy_bps + funding_adjustment_bps - cost_bps

    内部估算方式可以不同，但输出语义必须统一。
"""

from __future__ import annotations

import dataclasses as dc
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal


# ---------------------------------------------------------------------------
# 输入：Gold replay bar 行
# ---------------------------------------------------------------------------

@dc.dataclass(frozen=True)
class ReplayBar:
    """从 gold.market_*_replay_bars_* 读取的一行。"""
    symbol: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    quote_volume: Decimal | None
    is_closed: bool
    aligned_funding_rate: Decimal | None
    funding_source_ts: datetime | None


# ---------------------------------------------------------------------------
# Replay 成本配置
# ---------------------------------------------------------------------------

@dc.dataclass(frozen=True)
class ReplayCostConfig:
    """可配置的交易成本模型。

    默认值使用保守估计（OKX swap taker + 合理滑点），不再硬编码在 adapter 里。
    所有值单位为 bps（1 bps = 0.01%）。

    OKX 费率参考（2024/2025）：
    - Swap taker fee:  0.05% = 5 bps（普通用户），VIP 可低至 2-3 bps
    - Spot taker fee:  0.10% = 10 bps（普通用户）
    - 滑点:           因 symbol/流动性/仓位 而异，保守估计 2-3 bps
    """
    taker_fee_bps: float = 5.0       # OKX swap taker 0.05% = 5 bps
    slippage_bps: float = 2.0        # 保守滑点估计

    @property
    def total_cost_bps(self) -> float:
        """单次开平仓的单边成本（bps）。"""
        return self.taker_fee_bps + self.slippage_bps

    def to_dict(self) -> dict[str, Any]:
        return {
            "taker_fee_bps": self.taker_fee_bps,
            "slippage_bps": self.slippage_bps,
            "total_cost_bps": self.total_cost_bps,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReplayCostConfig:
        return cls(
            taker_fee_bps=float(d.get("taker_fee_bps", 5.0)),
            slippage_bps=float(d.get("slippage_bps", 2.0)),
        )


# ---------------------------------------------------------------------------
# Replay 参数覆盖
# ---------------------------------------------------------------------------

@dc.dataclass(frozen=True)
class ReplayParameterOverrides:
    """可在 replay 实验中覆盖的策略参数。

    Phase 2 首批冻结 3 个策略参数 + 可配置的成本模型：
    - min_confirm_ticks        信号确认强度
    - score_stability_threshold 强信号是否被过度拦截
    - min_safe_net_edge_bps    边缘机会放行下限
    - cost_config              交易成本配置（taker_fee_bps + slippage_bps）
    """
    min_confirm_ticks: int = 2
    score_stability_threshold: float = 2.0
    min_safe_net_edge_bps: float = 0.0

    # 成本配置
    cost_config: ReplayCostConfig = dc.field(default_factory=ReplayCostConfig)

    # 可扩展的额外参数
    extra: dict[str, Any] = dc.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "min_confirm_ticks": self.min_confirm_ticks,
            "score_stability_threshold": self.score_stability_threshold,
            "min_safe_net_edge_bps": self.min_safe_net_edge_bps,
            "cost_config": self.cost_config.to_dict(),
        }
        if self.extra:
            d.update(self.extra)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReplayParameterOverrides:
        known = {
            "min_confirm_ticks", "score_stability_threshold",
            "min_safe_net_edge_bps", "cost_config",
        }
        cost_raw = d.get("cost_config")
        cost = ReplayCostConfig.from_dict(cost_raw) if isinstance(cost_raw, dict) else ReplayCostConfig()
        return cls(
            min_confirm_ticks=int(d.get("min_confirm_ticks", 2)),
            score_stability_threshold=float(d.get("score_stability_threshold", 2.0)),
            min_safe_net_edge_bps=float(d.get("min_safe_net_edge_bps", 0.0)),
            cost_config=cost,
            extra={k: v for k, v in d.items() if k not in known},
        )


# ---------------------------------------------------------------------------
# Replay 上下文：传递给策略 adapter 的逐 bar 状态
# ---------------------------------------------------------------------------

@dc.dataclass
class ReplayState:
    """在 replay 过程中跨 bar 累积的可变状态。"""
    position_qty: Decimal = Decimal("0")        # 当前持仓
    position_side: Literal["flat", "long", "short"] = "flat"
    entry_price: Decimal | None = None
    entry_ts: datetime | None = None
    score_history: list[float] = dc.field(default_factory=list)
    bar_index: int = 0
    last_close_ts: datetime | None = None       # 上次平仓时间（冷却用）


@dc.dataclass(frozen=True)
class ReplayBarContext:
    """单根 bar 传递给 adapter 的完整上下文。"""
    bar: ReplayBar
    bar_index: int
    state: ReplayState
    params: ReplayParameterOverrides
    family: str
    symbol: str
    timeframe: str
    dataset_version: str


# ---------------------------------------------------------------------------
# 输出：逐 bar 决策记录
# ---------------------------------------------------------------------------

@dc.dataclass(frozen=True)
class ReplayDecision:
    """策略 adapter 对单根 bar 的评估结果。

    字段对齐 Phase 2 设计决策文档 §8.3 + P0-3 统一 edge contract。

    Edge 分解（所有 family 统一语义）：
    - signal_edge_proxy_bps:   策略信号派生的机会代理值
    - funding_adjustment_bps:  funding rate 附加调整
    - cost_bps:               交易成本（taker_fee + slippage）
    - expected_net_edge_bps:  = signal + funding - cost（最终净 edge）
    """
    ts: datetime
    family: str
    symbol: str
    timeframe: str
    state: str                              # flat / probing / holding / ...
    selectable: bool                        # 是否可选中
    execution_compatible: bool              # 是否可执行
    long_score: float
    short_score: float
    blocking_reasons: list[str]
    expected_net_edge_bps: float
    target_position_qty: Decimal
    delta_position_qty: Decimal

    # Edge 分解字段（P0-3 统一 contract）
    signal_edge_proxy_bps: float = 0.0      # 来自策略信号的机会代理
    funding_adjustment_bps: float = 0.0     # 来自 funding rate 的附加调整
    cost_bps: float = 0.0                   # 交易成本

    # 扩展字段
    action: str = "hold"                    # open / hold / close / blocked
    score_stable: bool = False
    funding_rate: float | None = None
    close_price: float | None = None
    bar_index: int = 0

    def to_flat_dict(self) -> dict[str, Any]:
        """序列化为平坦字典（写 CSV / parquet 用）。"""
        return {
            "ts": self.ts.isoformat(),
            "family": self.family,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "state": self.state,
            "selectable": self.selectable,
            "execution_compatible": self.execution_compatible,
            "long_score": self.long_score,
            "short_score": self.short_score,
            "blocking_reasons": "|".join(self.blocking_reasons) if self.blocking_reasons else "",
            "signal_edge_proxy_bps": self.signal_edge_proxy_bps,
            "funding_adjustment_bps": self.funding_adjustment_bps,
            "cost_bps": self.cost_bps,
            "expected_net_edge_bps": self.expected_net_edge_bps,
            "target_position_qty": str(self.target_position_qty),
            "delta_position_qty": str(self.delta_position_qty),
            "action": self.action,
            "score_stable": self.score_stable,
            "funding_rate": self.funding_rate,
            "close_price": self.close_price,
            "bar_index": self.bar_index,
        }
