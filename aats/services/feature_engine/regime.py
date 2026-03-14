from __future__ import annotations


class RegimeClassifier:
    def classify(self, trend_strength: float, volatility_state: str) -> str:
        if abs(trend_strength) >= 0.35:
            return "breakout" if volatility_state == "high" else "trend"
        if abs(trend_strength) <= 0.1:
            return "range"
        return "uncertain"

