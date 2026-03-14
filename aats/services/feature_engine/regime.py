from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RegimeClassification:
    regime_indicator: str
    regime_confidence: float
    trend_bias: str
    regime_alignment_score: float
    reasons: list[str]


class RegimeClassifier:
    def classify(
        self,
        *,
        momentum_15m: float,
        momentum_1h: float,
        trend_strength_15m: float,
        trend_strength_1h: float,
        volatility_state_15m: str,
        volatility_state_1h: str,
        liquidity_score: float,
    ) -> RegimeClassification:
        direction_15m = self._direction(momentum_15m)
        direction_1h = self._direction(momentum_1h)
        aligned = direction_15m == direction_1h and direction_15m != "flat"
        alignment_score = min(
            (abs(momentum_15m) + abs(momentum_1h) + abs(trend_strength_15m) + abs(trend_strength_1h)) / 4.0,
            1.0,
        )
        reasons: list[str] = []

        if abs(momentum_15m) >= 0.004 and abs(trend_strength_15m) >= 0.35:
            if aligned:
                reasons.append("aligned_multi_timeframe_direction")
            else:
                reasons.append("strong_short_term_dislocation")
            reasons.append(f"short_term_volatility_{volatility_state_15m}")
            return RegimeClassification(
                regime_indicator="breakout",
                regime_confidence=min(0.58 + alignment_score * (0.3 if aligned else 0.18), 0.96),
                trend_bias=direction_15m,
                regime_alignment_score=alignment_score,
                reasons=reasons,
            )

        if abs(trend_strength_15m) >= 0.2 and (aligned or abs(momentum_1h) <= 0.003):
            reasons.extend(
                ["persistent_trend_strength", "aligned_multi_timeframe_direction" if aligned else "higher_timeframe_weak"]
            )
            return RegimeClassification(
                regime_indicator="trend",
                regime_confidence=min(0.57 + alignment_score * 0.22, 0.94),
                trend_bias=direction_15m,
                regime_alignment_score=alignment_score,
                reasons=reasons,
            )

        if (
            abs(momentum_15m) <= 0.0015
            and abs(momentum_1h) <= 0.0025
            and volatility_state_15m != "high"
            and liquidity_score >= 0.3
        ):
            reasons.extend(["muted_momentum", "contained_volatility"])
            return RegimeClassification(
                regime_indicator="range",
                regime_confidence=min(0.55 + max(liquidity_score, 0.0) * 0.2, 0.9),
                trend_bias="flat",
                regime_alignment_score=alignment_score,
                reasons=reasons,
            )

        reasons.append("mixed_or_weak_signals")
        return RegimeClassification(
            regime_indicator="uncertain",
            regime_confidence=max(0.2, 0.45 - (alignment_score * 0.1)),
            trend_bias=direction_15m if direction_15m == direction_1h else "mixed",
            regime_alignment_score=alignment_score,
            reasons=reasons,
        )

    @staticmethod
    def _direction(momentum: float) -> str:
        if momentum > 0.0:
            return "long"
        if momentum < 0.0:
            return "short"
        return "flat"
