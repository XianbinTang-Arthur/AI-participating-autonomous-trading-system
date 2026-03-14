from __future__ import annotations

from aats.schemas.market import MarketSnapshot


class VolatilityAnalyzer:
    def calculate(self, snapshot: MarketSnapshot) -> tuple[str, float]:
        high = float(snapshot.kline_15m["high"])
        low = float(snapshot.kline_15m["low"])
        close = float(snapshot.kline_15m["close"])
        volatility_value = ((high - low) / close) if close else 0.0
        if volatility_value < 0.003:
            return "low", volatility_value
        if volatility_value < 0.01:
            return "medium", volatility_value
        return "high", volatility_value

