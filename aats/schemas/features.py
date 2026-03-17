from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from aats.schemas.common import SchemaBase

VolatilityState = Literal["low", "medium", "high"]
RegimeIndicator = Literal["trend", "range", "breakout", "uncertain"]
DirectionalBias = Literal["long", "short", "flat", "mixed"]


class TimeframeFeatureSet(SchemaBase):
    timeframe: Literal["15m", "1h"]
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    momentum_score: float
    trend_strength: float
    volatility_value: float
    volatility_state: VolatilityState
    candle_body_ratio: float
    range_ratio: float


class LiquidityFeatureSet(SchemaBase):
    spread_bps: float
    top_of_book_imbalance: float
    depth_imbalance: float
    trade_flow_imbalance: float = 0.0
    quoted_depth: float
    spread_penalty: float = 0.0
    execution_quality_scale: float = 1.0
    liquidity_score: float


class AlphaFactorSet(SchemaBase):
    momentum_alpha: float
    trend_alpha: float
    regime_alpha: float
    multi_timeframe_alpha: float
    microstructure_alpha: float = 0.0
    liquidity_scale: float
    composite_alpha_score: float
    conviction_score: float


class PositionSizingContext(SchemaBase):
    volatility_target_scale: float
    liquidity_scale: float
    execution_quality_scale: float = 1.0
    conviction_scale: float
    suggested_position_scale: float


class MultiTimeframeContext(SchemaBase):
    primary_timeframe: Literal["15m"] = "15m"
    higher_timeframe: Literal["1h"] = "1h"
    directional_alignment: DirectionalBias
    momentum_alignment_score: float
    regime_alignment_score: float
    dominant_timeframe: Literal["15m", "1h", "balanced"]


class AnalysisContext(SchemaBase):
    symbol: str
    snapshot_ts: datetime
    analysis_version: str
    regime_version: str
    trend_bias: DirectionalBias
    regime_indicator: RegimeIndicator
    regime_confidence: float
    regime_reasons: list[str] = Field(default_factory=list)
    timeframe_features: dict[str, TimeframeFeatureSet] = Field(default_factory=dict)
    liquidity: LiquidityFeatureSet
    multi_timeframe: MultiTimeframeContext
    alpha_factors: AlphaFactorSet
    position_sizing: PositionSizingContext


class FeatureSnapshot(SchemaBase):
    symbol: str
    snapshot_ts: datetime
    market_snapshot_ref: str | None = None
    trend_strength: float
    volatility_state: VolatilityState
    volatility_value: float
    momentum_score: float
    liquidity_score: float
    regime_indicator: RegimeIndicator
    regime_confidence: float = 0.0
    multi_timeframe_alignment: float = 0.0
    composite_alpha_score: float = 0.0
    suggested_position_scale: float = 0.0
    volatility_target_scale: float = 1.0
    feature_version: str
    analysis_context: AnalysisContext | None = None
