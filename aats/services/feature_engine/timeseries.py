"""RollingCandleState — per (symbol, timeframe) 滚动 K 线状态机.

Bug-1 时序平滑修复：baseline 原本只看单根未闭合 K 线，无时序记忆，
每 tick 决策抖动。本模块维护滚动历史，支撑 EMA / ROC / ATR 计算。

契约（重要，不改）:
  - ``update(bar, ts=)`` 幂等：同 ``ts`` 再次调用覆盖末尾 bar 但不推进 EMA/
    窗口指针 → 测试与上游反复传同一 snapshot 结果保持一致
  - ``indicators()`` 返回 ``ready=False`` 时表示历史不足（< max(roc_window+1,
    atr_window+1)），调用方**必须**退化到单 K 线瞬时算法
  - EMA 仅在新 ``ts`` 到达时推进，避免同 bar tick 反复扰动
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Deque, Iterable

from aats.schemas.market import KlineBar

_LOGGER_TS = logging.getLogger("aats.feature_engine.timeseries")

# 默认窗口参数。与 settings 字段同步，覆盖时由 FeatureCalculator 传入。
DEFAULT_MAX_BARS = 50
DEFAULT_ROC_WINDOW = 5       # ROC(n=5)：当前收盘相对 5 根前收盘的涨跌
DEFAULT_ATR_WINDOW = 14      # ATR(14)：经典 Wilder 期
DEFAULT_EMA_PERIOD = 20      # EMA(20)：close 平滑


@dataclass(frozen=True, slots=True)
class RollingIndicators:
    """EMA/ROC/ATR/ADX 计算结果。``ready=False`` 表示历史不足，所有数值字段应忽略。"""

    close_ema: float | None = None
    roc: float | None = None              # (close_now - close_{n ago}) / close_{n ago}
    atr: float | None = None              # 原始 ATR（绝对价格单位）
    atr_normalized: float | None = None   # ATR / close_now（相对比例，pct）
    prev_close: float | None = None       # 上一根 bar 的 close（调用方可能用作 basis）
    # P2.9 — Wilder ADX(14). ADX 刻画"趋势强度"（无方向）：> 25 强趋势，< 20 震荡.
    # +DI / -DI 分别给出 long / short 方向性 Demand Index. 用于 RegimeClassifier
    # 的 classify_with_adx 路径，替换硬编码 momentum / trend_strength 阈值.
    adx: float | None = None
    plus_di: float | None = None
    minus_di: float | None = None
    bars_available: int = 0
    ready: bool = False


@dataclass
class RollingCandleState:
    """Per (symbol, timeframe) 滚动 K 线状态。

    设计:
      - 内部 deque(maxlen=max_bars) 存历史 bars，按 ts 升序
      - ``update(bar, ts=)`` 若 ts == 上一根则覆盖（未闭合 tick），否则 append
      - ``prewarm(bars)`` 批量加载（warmup.py 从 OKX REST 拉历史后调用）
      - ``indicators()`` 当场算一次 ROC / ATR（O(atr_window) 复杂度，可接受）
    """

    symbol: str
    timeframe: str
    max_bars: int = DEFAULT_MAX_BARS
    roc_window: int = DEFAULT_ROC_WINDOW
    atr_window: int = DEFAULT_ATR_WINDOW
    ema_period: int = DEFAULT_EMA_PERIOD

    _bars: Deque[KlineBar] = field(init=False)
    _timestamps: Deque[datetime] = field(init=False)
    _ema_value: float | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        # dataclass field 不能用 lambda 引用 self.max_bars；在 __post_init__ 里构造
        self._bars = deque(maxlen=self.max_bars)
        self._timestamps = deque(maxlen=self.max_bars)

    # ── 写入 ────────────────────────────────────────────────────────

    def update(self, bar: KlineBar, *, ts: datetime) -> None:
        """追加/覆盖一根 bar。

        - 若 ts 与最后一根相同：覆盖末尾（未闭合 K 线 tick 级更新），
          **不推进 EMA**。同一根 bar 的 EMA 已在第一次 ts 到达时计入。
        - 若 ts 新于最后一根：append，推进 EMA。
        - 若 ts 早于最后一根（乱序回放）：忽略，避免破坏单调时序。
        """
        if self._timestamps:
            last_ts = self._timestamps[-1]
            if ts == last_ts:
                self._bars[-1] = bar  # 覆盖末尾
                return
            if ts < last_ts:
                # R2-N2 审查修复: 乱序/回退 ts 静默丢弃 → 诊断盲区. 即使罕见也
                # 要可见. 频率由 OKX WS 乱序决定; 若上游 bug 会变常态.
                _LOGGER_TS.warning(
                    "rolling_state_out_of_order_bar_dropped symbol=%s "
                    "timeframe=%s last_ts=%s dropped_ts=%s regression_seconds=%s",
                    self.symbol, self.timeframe,
                    last_ts.isoformat(), ts.isoformat(),
                    (last_ts - ts).total_seconds(),
                )
                return  # 乱序丢弃

        self._bars.append(bar)
        self._timestamps.append(ts)

        close = float(bar.close)
        if self._ema_value is None:
            self._ema_value = close
        else:
            alpha = 2.0 / (self.ema_period + 1)
            self._ema_value = alpha * close + (1.0 - alpha) * self._ema_value

    def prewarm(self, bars: Iterable[tuple[datetime, KlineBar]]) -> None:
        """批量加载历史 bars（任意顺序）。内部按 ts 升序处理。"""
        ordered = sorted(bars, key=lambda pair: pair[0])
        for ts, bar in ordered:
            self.update(bar, ts=ts)

    # ── 读出 ────────────────────────────────────────────────────────

    def indicators(self) -> RollingIndicators:
        bars = list(self._bars)
        n = len(bars)
        needed = max(self.roc_window + 1, self.atr_window + 1)
        if n < needed:
            return RollingIndicators(
                close_ema=self._ema_value,
                bars_available=n,
                ready=False,
            )

        close_now = float(bars[-1].close)
        close_prev = float(bars[-2].close)
        close_roc_base = float(bars[-1 - self.roc_window].close)
        roc = (close_now - close_roc_base) / close_roc_base if close_roc_base else 0.0

        # ATR = mean of last atr_window True Ranges（简单平均，与 Wilder 的
        # 指数平滑近似；在 14 根窗口下误差可忽略且实现更直白）。
        trs: list[float] = []
        start = n - self.atr_window
        for i in range(start, n):
            b = bars[i]
            high = float(b.high)
            low = float(b.low)
            # 第一根没 prev_close（i=0），用 open 兜底避免 index -1 取到末尾
            prev_c = float(bars[i - 1].close) if i > 0 else float(b.open)
            tr = max(
                high - low,
                abs(high - prev_c),
                abs(low - prev_c),
            )
            trs.append(tr)
        atr = sum(trs) / len(trs) if trs else 0.0
        atr_norm = atr / close_now if close_now else 0.0

        # P2.9 — Wilder ADX / +DI / -DI (审查 M-1: 双重 Wilder 平滑修复)
        #
        # 标准 Wilder ADX 需要两次平滑:
        #   1. 对 +DM / -DM / TR 做 N 期 Wilder smoothing 得到 +DI / -DI / DX
        #   2. 对 DX 再做 N 期 Wilder smoothing 得到 ADX
        # 前一版用"最后一期 DX"代替 ADX，方向正确但数值抖动大得多，ADX 阈值
        # 25/20 的学术共识基于"双重平滑 DX"，直接套 DX 阈值 → 在 ADX 25 附近
        # 频繁 flip regime (trend↔uncertain↔range)，实盘反复进出场 (M-1 审查).
        #
        # 实现需要 2N+1 根 bar: N 根用于首期 SMA-smoothed +DM/-DM/TR, 下一根开始
        # 产生第一个 DX, 再 N 期 DX 才能产生 ADX. ready 门槛提高到 2*atr_window+1.
        adx_value: float | None = None
        plus_di: float | None = None
        minus_di: float | None = None
        if n >= 2 * self.atr_window + 1:
            plus_dms: list[float] = []
            minus_dms: list[float] = []
            tr_all: list[float] = []
            for i in range(1, n):
                prev_b = bars[i - 1]
                cur_b = bars[i]
                high_diff = float(cur_b.high) - float(prev_b.high)
                low_diff = float(prev_b.low) - float(cur_b.low)
                plus_dm = high_diff if (high_diff > 0 and high_diff > low_diff) else 0.0
                minus_dm = low_diff if (low_diff > 0 and low_diff > high_diff) else 0.0
                plus_dms.append(plus_dm)
                minus_dms.append(minus_dm)
                prev_c = float(prev_b.close)
                tr_all.append(max(
                    float(cur_b.high) - float(cur_b.low),
                    abs(float(cur_b.high) - prev_c),
                    abs(float(cur_b.low) - prev_c),
                ))

            w = self.atr_window
            # 首期 Wilder smoothed = 前 w 个的和 (等价于 SMA × w 的 Wilder 惯例)
            pdm_s = sum(plus_dms[:w])
            mdm_s = sum(minus_dms[:w])
            tr_s = sum(tr_all[:w])
            dx_history: list[float] = []
            # 从索引 w 开始: 每次推进 Wilder smooth 一步，产出一个 DX
            for i in range(w, len(tr_all)):
                pdm_s = pdm_s - (pdm_s / w) + plus_dms[i]
                mdm_s = mdm_s - (mdm_s / w) + minus_dms[i]
                tr_s = tr_s - (tr_s / w) + tr_all[i]
                if tr_s > 0:
                    di_plus_i = 100.0 * pdm_s / tr_s
                    di_minus_i = 100.0 * mdm_s / tr_s
                    di_sum_i = di_plus_i + di_minus_i
                    dx_i = 100.0 * abs(di_plus_i - di_minus_i) / di_sum_i if di_sum_i > 0 else 0.0
                else:
                    dx_i = 0.0
                dx_history.append(dx_i)

            if len(dx_history) >= w:
                # Wilder smooth DX → ADX: 首期 SMA + 后续递推
                adx_s = sum(dx_history[:w]) / w
                for dx in dx_history[w:]:
                    adx_s = (adx_s * (w - 1) + dx) / w
                adx_value = adx_s
                # +DI / -DI 取最后一期 smoothed 值 (方向指标本就只需"当下")
                if tr_s > 0:
                    plus_di = 100.0 * pdm_s / tr_s
                    minus_di = 100.0 * mdm_s / tr_s
                else:
                    plus_di = 0.0
                    minus_di = 0.0

        return RollingIndicators(
            close_ema=self._ema_value,
            roc=roc,
            atr=atr,
            atr_normalized=atr_norm,
            prev_close=close_prev,
            adx=adx_value,
            plus_di=plus_di,
            minus_di=minus_di,
            bars_available=n,
            ready=True,
        )

    def is_ready(self) -> bool:
        return self.indicators().ready

    # ── 诊断辅助 ────────────────────────────────────────────────────

    def bars_count(self) -> int:
        return len(self._bars)

    def last_timestamp(self) -> datetime | None:
        return self._timestamps[-1] if self._timestamps else None
