from __future__ import annotations

from dataclasses import dataclass

from aats.schemas.features import VolatilityState
from aats.schemas.market import KlineBar, MarketSnapshot


@dataclass(frozen=True, slots=True)
class VolatilityMetrics:
    volatility_state: VolatilityState
    volatility_value: float
    range_ratio: float


class VolatilityAnalyzer:
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
