from __future__ import annotations

from dataclasses import dataclass

from aats.schemas.market import KlineBar, MarketSnapshot


@dataclass(frozen=True, slots=True)
class TrendMetrics:
    momentum_score: float
    trend_strength: float
    candle_body_ratio: float


class TrendCalculator:
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
