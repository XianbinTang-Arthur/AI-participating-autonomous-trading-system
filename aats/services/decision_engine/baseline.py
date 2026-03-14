from __future__ import annotations

from aats.schemas.decision import BaselineAssessment, DecisionContext
from aats.schemas.features import FeatureSnapshot
from aats.storage.base import EventStore


class BaselineStrategy:
    def __init__(self, *, event_store: EventStore) -> None:
        self.event_store = event_store

    def evaluate(self, context: DecisionContext) -> BaselineAssessment:
        feature_event = self.event_store.get(context.feature_snapshot_ref)
        if feature_event is None:
            raise RuntimeError("Feature snapshot reference is missing from the event store")

        features = FeatureSnapshot.model_validate(feature_event.payload)
        direction_bias = "flat"
        if features.momentum_score > 0.003 and features.regime_indicator in {"trend", "breakout"}:
            direction_bias = "long"
        elif features.momentum_score < -0.003 and features.regime_indicator in {"trend", "breakout"}:
            direction_bias = "short"

        confidence = min(max(abs(features.momentum_score) * 40.0, 0.5), 0.9)
        return BaselineAssessment(
            decision_id=context.decision_id,
            symbol=context.symbol,
            regime=features.regime_indicator,
            direction_bias=direction_bias,
            trend_strength=features.trend_strength,
            volatility_state=features.volatility_state,
            confidence=confidence,
            holding_horizon=context.timeframe,
            invalidation_conditions=["feature_regime_flip"],
            reason_codes=["baseline_momentum_regime"],
            engine_version="0.1.0",
        )
