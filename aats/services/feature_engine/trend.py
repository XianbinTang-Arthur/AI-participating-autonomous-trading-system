from __future__ import annotations

from dataclasses import dataclass

from aats.schemas.market import KlineBar, MarketSnapshot
from aats.services.feature_engine.timeseries import RollingCandleState


@dataclass(frozen=True, slots=True)
class TrendMetrics:
    momentum_score: float
    trend_strength: float
    candle_body_ratio: float


# ATR-归一化 trend_strength 的尺度参数。
# 意图：trend_strength = clamp(ROC / (TREND_STRENGTH_ATR_SPAN × ATR_normalized), -1, 1)
# ATR_SPAN = 5 表示"当前 ROC 超过过去 5 倍平均波动即视为满强度趋势"。
# 这是启发式常量；P1 calibration 任务会用历史数据标定。
_TREND_STRENGTH_ATR_SPAN = 5.0
_TREND_STRENGTH_ATR_FLOOR = 1e-4   # ATR 归零兜底避免除以 0


class TrendCalculator:
    # ── 旧接口：单 K 线瞬时 ───────────────────────────────────────────
    # 保留两个用途:
    #   1. 冷启动阶段 RollingCandleState 未 ready 时退化
    #   2. feature flag ``strategy_baseline_timeseries_smoothing_enabled``
    #      关闭时完整回退（紧急回滚手段）

    def calculate(self, snapshot: MarketSnapshot) -> tuple[float, float]:
        metrics = self.analyze_kline(snapshot.kline_15m)
        return metrics.trend_strength, metrics.momentum_score

    def analyze_kline(self, kline: KlineBar) -> TrendMetrics:
        open_price = float(kline.open)
        close_price = float(kline.close)
        high_price = float(kline.high)
        low_price = float(kline.low)

        momentum = (close_price - open_price) / open_price if open_price else 0.0
        candle_range = max(high_price - low_price, 0.0)
        candle_body = abs(close_price - open_price)
        candle_body_ratio = candle_body / candle_range if candle_range else 0.0
        trend_strength = max(min((momentum * 120.0) + (candle_body_ratio * 0.25), 1.0), -1.0)
        return TrendMetrics(
            momentum_score=momentum,
            trend_strength=trend_strength,
            candle_body_ratio=candle_body_ratio,
        )

    # ── 新接口：基于滚动历史 (Bug-1 时序平滑) ───────────────────────

    def analyze_with_state(
        self,
        state: RollingCandleState,
        current_kline: KlineBar,
    ) -> TrendMetrics:
        """结合滚动历史的 momentum / trend_strength 计算.

        若 state 未 ready（历史不足），降级到 ``analyze_kline(current_kline)``，
        调用方无需自己处理。

        语义变化 vs analyze_kline:
          - ``momentum_score``: (close - open) / open (单根瞬时) → ROC(5)（5 根前到现在累积）
          - ``trend_strength``: momentum×120 + body×0.25 → ROC / (5 × ATR/close) 归一化
          - ``candle_body_ratio``: 保留（仍是瞬时，用于 impulse_override_bias）
        """
        indicators = state.indicators()
        if not indicators.ready or indicators.roc is None:
            return self.analyze_kline(current_kline)

        roc = indicators.roc
        atr_norm = indicators.atr_normalized or 0.0
        denom = max(atr_norm * _TREND_STRENGTH_ATR_SPAN, _TREND_STRENGTH_ATR_FLOOR)
        trend_strength = max(min(roc / denom, 1.0), -1.0)

        # candle_body_ratio 保持单 K 线瞬时 —— impulse_override_bias 仍用它
        # 来判断"当前这根 K 线实体是否足够结实"（语义与时序无关，不应平滑）
        open_price = float(current_kline.open)
        close_price = float(current_kline.close)
        high_price = float(current_kline.high)
        low_price = float(current_kline.low)
        candle_range = max(high_price - low_price, 0.0)
        candle_body = abs(close_price - open_price)
        candle_body_ratio = candle_body / candle_range if candle_range else 0.0

        return TrendMetrics(
            momentum_score=roc,
            trend_strength=trend_strength,
            candle_body_ratio=candle_body_ratio,
        )
