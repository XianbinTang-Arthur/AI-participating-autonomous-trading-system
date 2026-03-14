from __future__ import annotations

from datetime import datetime
from typing import Literal

from aats.schemas.common import SchemaBase


class FeatureSnapshot(SchemaBase):
    symbol: str
    snapshot_ts: datetime
    trend_strength: float
    volatility_state: Literal["low", "medium", "high"]
    volatility_value: float
    momentum_score: float
    liquidity_score: float
    regime_indicator: Literal["trend", "range", "breakout", "uncertain"]
    feature_version: str

