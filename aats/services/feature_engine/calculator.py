from __future__ import annotations

from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_payload, publish_model
from aats.schemas.features import FeatureSnapshot
from aats.schemas.market import MarketSnapshot
from aats.services.feature_engine.regime import RegimeClassifier
from aats.services.feature_engine.trend import TrendCalculator
from aats.services.feature_engine.volatility import VolatilityAnalyzer


class FeatureCalculator:
    def __init__(
        self,
        *,
        trend: TrendCalculator | None = None,
        volatility: VolatilityAnalyzer | None = None,
        regime: RegimeClassifier | None = None,
    ) -> None:
        self.trend = trend or TrendCalculator()
        self.volatility = volatility or VolatilityAnalyzer()
        self.regime = regime or RegimeClassifier()

    def calculate(self, snapshot: MarketSnapshot) -> FeatureSnapshot:
        trend_strength, momentum_score = self.trend.calculate(snapshot)
        volatility_state, volatility_value = self.volatility.calculate(snapshot)
        regime_indicator = self.regime.classify(trend_strength, volatility_state)
        spread = snapshot.best_ask - snapshot.best_bid
        liquidity_score = max(0.0, 1.0 - (spread / snapshot.last_price)) if snapshot.last_price else 0.0
        return FeatureSnapshot(
            symbol=snapshot.symbol,
            snapshot_ts=snapshot.snapshot_ts,
            trend_strength=trend_strength,
            volatility_state=volatility_state,
            volatility_value=volatility_value,
            momentum_score=momentum_score,
            liquidity_score=liquidity_score,
            regime_indicator=regime_indicator,
            feature_version="0.1.0",
        )


class FeatureEngine:
    def __init__(self, *, bus: EventBus, calculator: FeatureCalculator) -> None:
        self.bus = bus
        self.calculator = calculator
        self._latest_snapshots: dict[str, FeatureSnapshot] = {}

    def latest_snapshot(self, symbol: str) -> FeatureSnapshot | None:
        return self._latest_snapshots.get(symbol)

    async def handle_market_snapshot(self, message: dict) -> None:
        market_snapshot = parse_payload(message, MarketSnapshot)
        feature_snapshot = self.calculator.calculate(market_snapshot)
        self._latest_snapshots[feature_snapshot.symbol] = feature_snapshot
        await publish_model(
            bus=self.bus,
            topic=topics.FEATURE_SNAPSHOTS,
            key=feature_snapshot.symbol,
            payload_model=feature_snapshot,
            source_component="feature_engine",
        )

