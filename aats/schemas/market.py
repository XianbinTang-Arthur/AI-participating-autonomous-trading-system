from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from aats.schemas.common import SchemaBase


class MarketSnapshot(SchemaBase):
    symbol: str
    exchange: str
    snapshot_ts: datetime
    best_bid: float
    best_ask: float
    last_price: float
    bid_size: float
    ask_size: float
    volume_24h: float
    kline_15m: dict[str, Any]
    kline_1h: dict[str, Any]
    recent_trades: list[dict[str, Any]] = Field(default_factory=list)
    orderbook_depth: dict[str, Any] = Field(default_factory=dict)

