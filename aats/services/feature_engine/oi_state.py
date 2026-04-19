"""OpenInterestState — per-symbol Open Interest 滚动历史 + EMA 平滑 (P1.6).

OI (Open Interest) 是衍生品特有指标，表示市场上未平仓合约总数。与价格变动
联合判断可揭示"新资金入场 vs 平仓出场":
  - 价 ↑ 且 OI ↑ = 新多头入场 (趋势确认)
  - 价 ↑ 且 OI ↓ = 空头平仓反弹 (短期谨慎)
  - 价 ↓ 且 OI ↑ = 新空头入场 (趋势确认)
  - 价 ↓ 且 OI ↓ = 多头平仓回调 (短期谨慎)

FeatureCalculator 用 ``oi_delta = (oi_now - oi_ema) / oi_ema`` 与 ROC 联合计算
oi_alpha。本状态机负责维护 OI 历史窗口和 EMA。

和 RollingCandleState 一样幂等：同 ts 的 update 覆盖末尾样本不推进 EMA，保证
FeatureCalculator 同 snapshot 多次 calculate 一致。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Deque

DEFAULT_OI_MAX_SNAPSHOTS = 60      # ~3 分钟窗口（OKX 每 3s 推一次）
DEFAULT_OI_EMA_PERIOD = 20         # EMA(20) 作为"基线 OI"
DEFAULT_OI_DEAD_ZONE = 0.005       # |delta| < 0.5% 视为噪声，oi_alpha=0


@dataclass(frozen=True, slots=True)
class OpenInterestIndicators:
    """OI 状态快照。``ready=False`` 表示样本不足，caller 应忽略 delta."""

    oi_now: float | None = None
    oi_ema: float | None = None
    oi_delta: float | None = None   # (oi_now - oi_ema) / oi_ema
    samples_available: int = 0
    ready: bool = False


@dataclass
class OpenInterestState:
    """Per-symbol OI 滚动状态。

    契约:
      - ``update(oi, ts=)`` 同 ts 幂等（同 snapshot 反复调用结果一致）
      - ``update`` 拒绝旧 ts（乱序回放不破坏单调时序）
      - ``indicators()`` 需要 >= ema_period 样本才 ready
      - 负 OI 或非数值自动拒绝（防 schema 污染）
    """

    symbol: str
    max_snapshots: int = DEFAULT_OI_MAX_SNAPSHOTS
    ema_period: int = DEFAULT_OI_EMA_PERIOD

    _oi_history: Deque[float] = field(init=False)
    _timestamps: Deque[datetime] = field(init=False)
    _ema_value: float | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self._oi_history = deque(maxlen=self.max_snapshots)
        self._timestamps = deque(maxlen=self.max_snapshots)

    def update(self, oi: float, *, ts: datetime) -> None:
        if not isinstance(oi, (int, float)) or oi < 0:
            return  # 非法样本拒绝（不触发异常，运行稳定优先）
        oi_float = float(oi)

        if self._timestamps:
            last_ts = self._timestamps[-1]
            if ts == last_ts:
                self._oi_history[-1] = oi_float
                return  # 同 ts 覆盖，不推 EMA
            if ts < last_ts:
                return  # 乱序丢弃

        self._oi_history.append(oi_float)
        self._timestamps.append(ts)

        if self._ema_value is None:
            self._ema_value = oi_float
        else:
            alpha = 2.0 / (self.ema_period + 1)
            self._ema_value = alpha * oi_float + (1.0 - alpha) * self._ema_value

    def indicators(self) -> OpenInterestIndicators:
        n = len(self._oi_history)
        if n < self.ema_period or self._ema_value is None or self._ema_value <= 0:
            return OpenInterestIndicators(
                oi_now=self._oi_history[-1] if self._oi_history else None,
                oi_ema=self._ema_value,
                samples_available=n,
                ready=False,
            )
        oi_now = self._oi_history[-1]
        delta = (oi_now - self._ema_value) / self._ema_value
        return OpenInterestIndicators(
            oi_now=oi_now,
            oi_ema=self._ema_value,
            oi_delta=delta,
            samples_available=n,
            ready=True,
        )

    def samples_count(self) -> int:
        return len(self._oi_history)
