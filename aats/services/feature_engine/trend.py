from __future__ import annotations

from aats.schemas.market import MarketSnapshot


class TrendCalculator:
    def calculate(self, snapshot: MarketSnapshot) -> tuple[float, float]:
        open_price = float(snapshot.kline_15m["open"])
        close_price = float(snapshot.kline_15m["close"])
        momentum = (close_price - open_price) / open_price if open_price else 0.0
        trend_strength = max(min(momentum * 100.0, 1.0), -1.0)
        return trend_strength, momentum
