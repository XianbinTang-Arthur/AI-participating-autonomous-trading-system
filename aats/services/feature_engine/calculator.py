from __future__ import annotations

from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_envelope, publish_model
from aats.schemas.features import (
    AnalysisContext,
    FeatureSnapshot,
    MultiTimeframeContext,
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
