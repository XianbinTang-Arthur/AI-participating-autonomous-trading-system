from __future__ import annotations

from dataclasses import dataclass

from aats.schemas.features import DirectionalBias, RegimeIndicator, VolatilityState

# P2.9 — Wilder ADX 经典阈值. ADX >= 25 视为强趋势, ADX <= 20 视为震荡,
# 20-25 之间 uncertain. 默认值来自学术公认; 可由 settings 覆盖. calibration
# 任务可用历史数据回测调整到最优区间.
DEFAULT_ADX_TREND_THRESHOLD = 25.0
DEFAULT_ADX_RANGE_THRESHOLD = 20.0


@dataclass(frozen=True, slots=True)
class RegimeClassification:
    regime_indicator: RegimeIndicator
    regime_confidence: float
    trend_bias: DirectionalBias
    regime_alignment_score: float
    reasons: list[str]


class RegimeClassifier:
    def __init__(
        self,
        *,
        adx_trend_threshold: float = DEFAULT_ADX_TREND_THRESHOLD,
        adx_range_threshold: float = DEFAULT_ADX_RANGE_THRESHOLD,
    ) -> None:
        self.adx_trend_threshold = adx_trend_threshold
        self.adx_range_threshold = adx_range_threshold

    def classify(
        self,
        *,
        momentum_15m: float,
        momentum_1h: float,
        trend_strength_15m: float,
        trend_strength_1h: float,
        volatility_state_15m: VolatilityState,
        volatility_state_1h: VolatilityState,
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

    def classify_with_adx(
        self,
        *,
        adx: float | None,
        plus_di: float | None,
        minus_di: float | None,
        momentum_15m: float,
        momentum_1h: float,
        trend_strength_15m: float,
        trend_strength_1h: float,
        volatility_state_15m: VolatilityState,
        volatility_state_1h: VolatilityState,
        liquidity_score: float,
    ) -> RegimeClassification:
        """P2.9 — ADX 驱动的 regime 分类.

        用 Wilder ADX 替换原始 ``classify()`` 里硬编码的 momentum/trend_strength
        阈值. ADX 的优点: 1) 只刻画"趋势强度"，与方向无关；2) 有学术共识阈值
        (> 25 强趋势, < 20 震荡)；3) 能区分"方向强但波动也大"的 breakout vs
        "方向强且波动收敛"的 trend.

        退化路径: adx / plus_di / minus_di 任一 None → 回退到 ``classify()``
        (state 未 ready 时自动生效，flag off 时 caller 直接调 classify).
        """
        if adx is None or plus_di is None or minus_di is None:
            return self.classify(
                momentum_15m=momentum_15m,
                momentum_1h=momentum_1h,
                trend_strength_15m=trend_strength_15m,
                trend_strength_1h=trend_strength_1h,
                volatility_state_15m=volatility_state_15m,
                volatility_state_1h=volatility_state_1h,
                liquidity_score=liquidity_score,
            )

        # 方向由 +DI vs -DI 决定. ADX 不代表方向.
        if plus_di > minus_di:
            dir_bias: DirectionalBias = "long"
        elif minus_di > plus_di:
            dir_bias = "short"
        else:
            dir_bias = "flat"

        # regime 置信度: ADX 越高越置信趋势, 越低越置信 range;
        # uncertain 区间取较小 baseline.
        direction_aligned = self._direction(momentum_15m) == self._direction(momentum_1h)
        alignment_score = min(
            (abs(momentum_15m) + abs(momentum_1h) + abs(trend_strength_15m) + abs(trend_strength_1h)) / 4.0,
            1.0,
        )

        reasons: list[str] = []

        if adx >= self.adx_trend_threshold:
            # 强趋势 —— 进一步用 volatility 分 breakout vs trend
            reasons.append(f"adx_trend_{adx:.1f}".replace(".", "_"))
            if volatility_state_15m == "high" and direction_aligned:
                reasons.append("aligned_high_volatility_breakout")
                return RegimeClassification(
                    regime_indicator="breakout",
                    regime_confidence=min(0.55 + min(adx / 100.0, 0.35), 0.96),
                    trend_bias=dir_bias,
                    regime_alignment_score=alignment_score,
                    reasons=reasons,
                )
            reasons.append("adx_strong_trend" if direction_aligned else "adx_trend_mtf_mixed")
            return RegimeClassification(
                regime_indicator="trend",
                regime_confidence=min(0.52 + min(adx / 120.0, 0.30), 0.94),
                trend_bias=dir_bias,
                regime_alignment_score=alignment_score,
                reasons=reasons,
            )

        if adx < self.adx_range_threshold and volatility_state_15m != "high" and liquidity_score >= 0.3:
            reasons.append(f"adx_range_{adx:.1f}".replace(".", "_"))
            reasons.append("contained_volatility")
            return RegimeClassification(
                regime_indicator="range",
                regime_confidence=min(0.50 + (1.0 - adx / self.adx_range_threshold) * 0.25 + liquidity_score * 0.15, 0.9),
                trend_bias="flat",
                regime_alignment_score=alignment_score,
                reasons=reasons,
            )

        # 20 <= ADX < 25 或波动过高 → uncertain
        reasons.append(f"adx_uncertain_{adx:.1f}".replace(".", "_"))
        return RegimeClassification(
            regime_indicator="uncertain",
            regime_confidence=max(0.25, 0.45 - abs(adx - 22.5) / 25.0),
            trend_bias=dir_bias if direction_aligned else "mixed",
            regime_alignment_score=alignment_score,
            reasons=reasons,
        )

    @staticmethod
    def _direction(momentum: float) -> DirectionalBias:
        if momentum > 0.0:
            return "long"
        if momentum < 0.0:
            return "short"
        return "flat"
