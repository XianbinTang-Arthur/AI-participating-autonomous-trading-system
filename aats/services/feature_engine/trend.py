from __future__ import annotations

from dataclasses import dataclass

from aats.schemas.market import MarketSnapshot


@dataclass(frozen=True, slots=True)
class TrendMetrics:
    momentum_score: float
    trend_strength: float
    candle_body_ratio: float


class TrendCalculator:
    def calculate(self, snapshot: MarketSnapshot) -> tuple[float, float]:
        metrics = self.analyze_kline(snapshot.kline_15m)
        return metrics.trend_strength, metrics.momentum_score

    def analyze_kline(self, kline: dict[str, object]) -> TrendMetrics:
        open_price = self._value(kline, "open")
        close_price = self._value(kline, "close")
        high_price = self._value(kline, "high")
        low_price = self._value(kline, "low")

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

    @staticmethod
    def _value(kline: dict[str, object], key: str) -> float:
        return float(kline[key])
