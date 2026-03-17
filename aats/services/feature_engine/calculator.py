from __future__ import annotations

from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_envelope, publish_model
from aats.schemas.features import (
    AlphaFactorSet,
    AnalysisContext,
    FeatureSnapshot,
    MultiTimeframeContext,
    PositionSizingContext,
    TimeframeFeatureSet,
)
from aats.schemas.market import MarketSnapshot
from aats.services.feature_engine.liquidity import LiquidityAnalyzer
from aats.services.feature_engine.regime import RegimeClassifier
from aats.services.feature_engine.trend import TrendCalculator
from aats.services.feature_engine.volatility import VolatilityAnalyzer


class FeatureCalculator:
    def __init__(
        self,
        *,
        trend: TrendCalculator | None = None,
        volatility: VolatilityAnalyzer | None = None,
        liquidity: LiquidityAnalyzer | None = None,
        regime: RegimeClassifier | None = None,
    ) -> None:
        self.trend = trend or TrendCalculator()
        self.volatility = volatility or VolatilityAnalyzer()
        self.liquidity = liquidity or LiquidityAnalyzer()
        self.regime = regime or RegimeClassifier()

    def calculate(self, snapshot: MarketSnapshot, *, market_snapshot_ref: str | None = None) -> FeatureSnapshot:
        features_15m = self._timeframe_features(snapshot=snapshot, timeframe="15m", kline=snapshot.kline_15m)
        features_1h = self._timeframe_features(snapshot=snapshot, timeframe="1h", kline=snapshot.kline_1h)
        liquidity = self.liquidity.calculate(snapshot)
        regime = self.regime.classify(
            momentum_15m=features_15m.momentum_score,
            momentum_1h=features_1h.momentum_score,
            trend_strength_15m=features_15m.trend_strength,
            trend_strength_1h=features_1h.trend_strength,
            volatility_state_15m=features_15m.volatility_state,
            volatility_state_1h=features_1h.volatility_state,
            liquidity_score=liquidity.liquidity_score,
        )
        multi_timeframe_context = self._multi_timeframe_context(
            snapshot_ts=snapshot.snapshot_ts,
            features_15m=features_15m,
            features_1h=features_1h,
            regime_alignment_score=regime.regime_alignment_score,
        )
        alpha_factors = self._alpha_factors(
            features_15m=features_15m,
            features_1h=features_1h,
            multi_timeframe=multi_timeframe_context,
            liquidity_score=liquidity.liquidity_score,
            top_of_book_imbalance=liquidity.top_of_book_imbalance,
            depth_imbalance=liquidity.depth_imbalance,
            trade_flow_imbalance=liquidity.trade_flow_imbalance,
            execution_quality_scale=liquidity.execution_quality_scale,
            spread_penalty=liquidity.spread_penalty,
            regime_indicator=regime.regime_indicator,
            regime_confidence=regime.regime_confidence,
            regime_bias=regime.trend_bias,
        )
        position_sizing = self._position_sizing_context(
            alpha_factors=alpha_factors,
            execution_quality_scale=liquidity.execution_quality_scale,
            volatility_state=features_15m.volatility_state,
            volatility_value=features_15m.volatility_value,
        )
        analysis_context = AnalysisContext(
            created_at=snapshot.snapshot_ts,
            symbol=snapshot.symbol,
            snapshot_ts=snapshot.snapshot_ts,
            analysis_version="0.2.0",
            regime_version="0.2.0",
            trend_bias=regime.trend_bias,  # type: ignore[arg-type]
            regime_indicator=regime.regime_indicator,  # type: ignore[arg-type]
            regime_confidence=regime.regime_confidence,
            regime_reasons=list(regime.reasons),
            timeframe_features={
                "15m": features_15m,
                "1h": features_1h,
            },
            liquidity=liquidity,
            multi_timeframe=multi_timeframe_context,
            alpha_factors=alpha_factors,
            position_sizing=position_sizing,
        )
        return FeatureSnapshot(
            created_at=snapshot.snapshot_ts,
            symbol=snapshot.symbol,
            snapshot_ts=snapshot.snapshot_ts,
            market_snapshot_ref=market_snapshot_ref,
            trend_strength=features_15m.trend_strength,
            volatility_state=features_15m.volatility_state,
            volatility_value=features_15m.volatility_value,
            momentum_score=features_15m.momentum_score,
            liquidity_score=liquidity.liquidity_score,
            regime_indicator=regime.regime_indicator,  # type: ignore[arg-type]
            regime_confidence=regime.regime_confidence,
            multi_timeframe_alignment=multi_timeframe_context.regime_alignment_score,
            composite_alpha_score=alpha_factors.composite_alpha_score,
            suggested_position_scale=position_sizing.suggested_position_scale,
            volatility_target_scale=position_sizing.volatility_target_scale,
            feature_version="0.2.0",
            analysis_context=analysis_context,
        )

    def _timeframe_features(
        self,
        *,
        snapshot: MarketSnapshot,
        timeframe: str,
        kline: dict[str, object],
    ) -> TimeframeFeatureSet:
        trend_metrics = self.trend.analyze_kline(kline)
        volatility_metrics = self.volatility.analyze_kline(kline)
        return TimeframeFeatureSet(
            created_at=snapshot.snapshot_ts,
            timeframe=timeframe,  # type: ignore[arg-type]
            open_price=float(kline["open"]),
            high_price=float(kline["high"]),
            low_price=float(kline["low"]),
            close_price=float(kline["close"]),
            momentum_score=trend_metrics.momentum_score,
            trend_strength=trend_metrics.trend_strength,
            volatility_value=volatility_metrics.volatility_value,
            volatility_state=volatility_metrics.volatility_state,  # type: ignore[arg-type]
            candle_body_ratio=trend_metrics.candle_body_ratio,
            range_ratio=volatility_metrics.range_ratio,
        )

    @staticmethod
    def _multi_timeframe_context(
        *,
        snapshot_ts,
        features_15m: TimeframeFeatureSet,
        features_1h: TimeframeFeatureSet,
        regime_alignment_score: float,
    ) -> MultiTimeframeContext:
        direction_15m = FeatureCalculator._direction(features_15m.momentum_score)
        direction_1h = FeatureCalculator._direction(features_1h.momentum_score)
        if direction_15m == direction_1h:
            directional_alignment = direction_15m
        elif direction_15m == "flat" and direction_1h == "flat":
            directional_alignment = "flat"
        else:
            directional_alignment = "mixed"
        momentum_alignment_score = min(
            1.0 - abs(features_15m.momentum_score - features_1h.momentum_score),
            1.0,
        )
        if abs(features_15m.trend_strength) > abs(features_1h.trend_strength) + 0.1:
            dominant_timeframe = "15m"
        elif abs(features_1h.trend_strength) > abs(features_15m.trend_strength) + 0.1:
            dominant_timeframe = "1h"
        else:
            dominant_timeframe = "balanced"
        return MultiTimeframeContext(
            created_at=snapshot_ts,
            directional_alignment=directional_alignment,  # type: ignore[arg-type]
            momentum_alignment_score=max(min(momentum_alignment_score, 1.0), -1.0),
            regime_alignment_score=max(min(regime_alignment_score, 1.0), 0.0),
            dominant_timeframe=dominant_timeframe,  # type: ignore[arg-type]
        )

    @staticmethod
    def _direction(momentum: float) -> str:
        if momentum > 0.0:
            return "long"
        if momentum < 0.0:
            return "short"
        return "flat"

    @staticmethod
    def _alpha_factors(
        *,
        features_15m: TimeframeFeatureSet,
        features_1h: TimeframeFeatureSet,
        multi_timeframe: MultiTimeframeContext,
        liquidity_score: float,
        top_of_book_imbalance: float,
        depth_imbalance: float,
        trade_flow_imbalance: float,
        execution_quality_scale: float,
        spread_penalty: float,
        regime_indicator: str,
        regime_confidence: float,
        regime_bias: str,
    ) -> AlphaFactorSet:
        momentum_alpha = FeatureCalculator._clamp(
            (features_15m.momentum_score * 140.0 * 0.65) + (features_1h.momentum_score * 90.0 * 0.35),
            -1.0,
            1.0,
        )
        trend_alpha = FeatureCalculator._clamp(
            (
                FeatureCalculator._direction_sign(features_15m.momentum_score) * features_15m.trend_strength * 0.65
                + FeatureCalculator._direction_sign(features_1h.momentum_score) * features_1h.trend_strength * 0.35
            ),
            -1.0,
            1.0,
        )
        regime_weight = 1.0 if regime_indicator in {"trend", "breakout"} else 0.35 if regime_indicator == "uncertain" else 0.0
        regime_alpha = FeatureCalculator._clamp(
            FeatureCalculator._direction_sign_from_bias(regime_bias) * regime_confidence * regime_weight,
            -1.0,
            1.0,
        )
        multi_timeframe_alpha = FeatureCalculator._clamp(
            FeatureCalculator._direction_sign_from_bias(multi_timeframe.directional_alignment)
            * multi_timeframe.momentum_alignment_score
            * multi_timeframe.regime_alignment_score,
            -1.0,
            1.0,
        )
        microstructure_direction = FeatureCalculator._clamp(
            (top_of_book_imbalance * 0.25)
            + (depth_imbalance * 0.4)
            + (trade_flow_imbalance * 0.35),
            -1.0,
            1.0,
        )
        microstructure_alpha = FeatureCalculator._clamp(
            microstructure_direction * execution_quality_scale * (1.0 - min(spread_penalty * 0.5, 0.45)),
            -1.0,
            1.0,
        )
        liquidity_scale = FeatureCalculator._clamp(0.45 + (liquidity_score * 0.55), 0.25, 1.0)
        composite_alpha_score = FeatureCalculator._clamp(
            (
                momentum_alpha * 0.34
                + trend_alpha * 0.22
                + regime_alpha * 0.17
                + multi_timeframe_alpha * 0.12
                + microstructure_alpha * 0.15
            )
            * liquidity_scale,
            -1.0,
            1.0,
        )
        conviction_score = FeatureCalculator._clamp(
            (abs(composite_alpha_score) * 0.7)
            + (regime_confidence * 0.15)
            + (multi_timeframe.regime_alignment_score * 0.08)
            + (execution_quality_scale * 0.07),
            0.0,
            1.0,
        )
        return AlphaFactorSet(
            created_at=features_15m.created_at,
            momentum_alpha=momentum_alpha,
            trend_alpha=trend_alpha,
            regime_alpha=regime_alpha,
            multi_timeframe_alpha=multi_timeframe_alpha,
            microstructure_alpha=microstructure_alpha,
            liquidity_scale=liquidity_scale,
            composite_alpha_score=composite_alpha_score,
            conviction_score=conviction_score,
        )

    @staticmethod
    def _position_sizing_context(
        *,
        alpha_factors: AlphaFactorSet,
        execution_quality_scale: float,
        volatility_state: str,
        volatility_value: float,
    ) -> PositionSizingContext:
        volatility_target_scale = {
            "low": 1.1,
            "medium": 1.0,
            "high": 0.65,
        }.get(volatility_state, 0.85)
        if volatility_value > 0.04:
            volatility_target_scale *= 0.85
        elif volatility_value < 0.01:
            volatility_target_scale *= 1.05
        volatility_target_scale = FeatureCalculator._clamp(volatility_target_scale, 0.45, 1.2)
        suggested_position_scale = FeatureCalculator._clamp(
            alpha_factors.conviction_score
            * alpha_factors.liquidity_scale
            * execution_quality_scale
            * volatility_target_scale,
            0.0,
            1.0,
        )
        if abs(alpha_factors.composite_alpha_score) >= 0.18:
            suggested_position_scale = max(
                suggested_position_scale,
                0.2 * max(min(execution_quality_scale, 1.0), 0.1),
            )
        return PositionSizingContext(
            created_at=alpha_factors.created_at,
            volatility_target_scale=volatility_target_scale,
            liquidity_scale=alpha_factors.liquidity_scale,
            execution_quality_scale=execution_quality_scale,
            conviction_scale=alpha_factors.conviction_score,
            suggested_position_scale=suggested_position_scale,
        )

    @staticmethod
    def _direction_sign(momentum: float) -> float:
        if momentum > 0.0:
            return 1.0
        if momentum < 0.0:
            return -1.0
        return 0.0

    @staticmethod
    def _direction_sign_from_bias(direction_bias: str) -> float:
        if direction_bias == "long":
            return 1.0
        if direction_bias == "short":
            return -1.0
        return 0.0

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        return max(lower, min(value, upper))


class FeatureEngine:
    def __init__(self, *, bus: EventBus, calculator: FeatureCalculator) -> None:
        self.bus = bus
        self.calculator = calculator
        self._latest_snapshots: dict[str, FeatureSnapshot] = {}

    def latest_snapshot(self, symbol: str) -> FeatureSnapshot | None:
        return self._latest_snapshots.get(symbol)

    async def handle_market_snapshot(self, message: dict) -> None:
        envelope = parse_envelope(message)
        market_snapshot = MarketSnapshot.model_validate(envelope.payload)
        feature_snapshot = self.calculator.calculate(
            market_snapshot,
            market_snapshot_ref=envelope.event_id,
        )
        self._latest_snapshots[feature_snapshot.symbol] = feature_snapshot
        await publish_model(
            bus=self.bus,
            topic=topics.FEATURE_SNAPSHOTS,
            key=feature_snapshot.symbol,
            payload_model=feature_snapshot,
            source_component="feature_engine",
        )
