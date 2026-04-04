"""Replay context: bar-level data model for historical replay.

Phase 2 replay 以 Gold replay bars 为输入，逐 bar 构建上下文供策略 adapter 评估。
本模块定义 replay 流程中所有共享的数据结构。
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
# Replay 参数覆盖
# ---------------------------------------------------------------------------

@dc.dataclass(frozen=True)
class ReplayParameterOverrides:
    """可在 replay 实验中覆盖的策略参数。

    Phase 2 首批冻结 3 个参数：
    - min_confirm_ticks        信号确认强度
    - score_stability_threshold 强信号是否被过度拦截
    - min_safe_net_edge_bps    边缘机会放行下限
    """
    min_confirm_ticks: int = 2
    score_stability_threshold: float = 2.0
    min_safe_net_edge_bps: float = 0.0

    # 可扩展的额外参数
    extra: dict[str, Any] = dc.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "min_confirm_ticks": self.min_confirm_ticks,
            "score_stability_threshold": self.score_stability_threshold,
            "min_safe_net_edge_bps": self.min_safe_net_edge_bps,
        }
        if self.extra:
            d.update(self.extra)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReplayParameterOverrides:
        known = {"min_confirm_ticks", "score_stability_threshold", "min_safe_net_edge_bps"}
        return cls(
            min_confirm_ticks=int(d.get("min_confirm_ticks", 2)),
            score_stability_threshold=float(d.get("score_stability_threshold", 2.0)),
            min_safe_net_edge_bps=float(d.get("min_safe_net_edge_bps", 0.0)),
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

    字段对齐 Phase 2 设计决策文档 §8.3。
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
            "expected_net_edge_bps": self.expected_net_edge_bps,
            "target_position_qty": str(self.target_position_qty),
            "delta_position_qty": str(self.delta_position_qty),
            "action": self.action,
            "score_stable": self.score_stable,
            "funding_rate": self.funding_rate,
            "close_price": self.close_price,
            "bar_index": self.bar_index,
        }
