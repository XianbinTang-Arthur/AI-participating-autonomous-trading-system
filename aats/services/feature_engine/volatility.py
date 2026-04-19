from __future__ import annotations

from dataclasses import dataclass

from aats.schemas.features import VolatilityState
from aats.schemas.market import KlineBar, MarketSnapshot
from aats.services.feature_engine.timeseries import RollingCandleState


@dataclass(frozen=True, slots=True)
class VolatilityMetrics:
    volatility_state: VolatilityState
    volatility_value: float
    range_ratio: float


# ATR-归一化 volatility state 阈值（ATR / close，相对比例）.
# 基于 BTC-USDT-SWAP 15m K 线经验值；calibration 任务会用历史数据标定。
# 量级说明:
#   ATR/close 的典型分布:
#     - 平稳期 / 小时线压制日内:   0.001–0.003
#     - 一般波动:                   0.003–0.008
#     - 剧烈波动 / 日线级异动:     0.008+
_ATR_LOW_CEILING = 0.002
_ATR_MEDIUM_CEILING = 0.006


class VolatilityAnalyzer:
    # ── 旧接口：单 K 线瞬时 ───────────────────────────────────────────
    # 保留两个用途（同 trend.py）:
    #   1. 冷启动阶段 state 未 ready 时退化
    #   2. feature flag 关闭时完整回退

    def calculate(self, snapshot: MarketSnapshot) -> tuple[str, float]:
        metrics = self.analyze_kline(snapshot.kline_15m)
        return metrics.volatility_state, metrics.volatility_value

    def analyze_kline(self, kline: KlineBar) -> VolatilityMetrics:
        high = float(kline.high)
        low = float(kline.low)
        close = float(kline.close)
        open_price = float(kline.open)
        range_ratio = ((high - low) / close) if close else 0.0
        close_to_open = abs((close - open_price) / open_price) if open_price else 0.0
        volatility_value = (range_ratio * 0.7) + (close_to_open * 0.3)
        if volatility_value < 0.003:
            state = "low"
        elif volatility_value < 0.01:
            state = "medium"
        else:
            state = "high"
        return VolatilityMetrics(
            volatility_state=state,
            volatility_value=volatility_value,
            range_ratio=range_ratio,
        )

    # ── 新接口：基于滚动历史 (ATR 14 替换单 K 线振幅) ───────────────

    def analyze_with_state(
        self,
        state: RollingCandleState,
        current_kline: KlineBar,
    ) -> VolatilityMetrics:
        """基于 ATR(14) 的 volatility 计算.

        若 state 未 ready，降级到 ``analyze_kline(current_kline)``。

        语义变化 vs analyze_kline:
          - ``volatility_value``: range×0.7 + close_to_open×0.3 (单 K 线振幅)
              → ATR(14) / close (14 根真实波幅的相对比例)
          - ``volatility_state``: 阈值从 (0.003, 0.01) 改为 (0.002, 0.006) 匹配 ATR 分布
          - ``range_ratio``: 保留单 K 线瞬时（impulse_override_bias 用）
        """
        indicators = state.indicators()
        if not indicators.ready or indicators.atr_normalized is None:
            return self.analyze_kline(current_kline)

        volatility_value = indicators.atr_normalized
        if volatility_value < _ATR_LOW_CEILING:
            state_label: VolatilityState = "low"
        elif volatility_value < _ATR_MEDIUM_CEILING:
            state_label = "medium"
        else:
            state_label = "high"

        # range_ratio 仍是瞬时，原因同 trend.py.candle_body_ratio
        high = float(current_kline.high)
        low = float(current_kline.low)
        close = float(current_kline.close)
        range_ratio = ((high - low) / close) if close else 0.0

        return VolatilityMetrics(
            volatility_state=state_label,
            volatility_value=volatility_value,
            range_ratio=range_ratio,
        )
