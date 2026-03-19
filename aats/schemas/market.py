from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import Field
from pydantic import field_validator

from aats.schemas.common import SchemaBase


def _maybe_decimal(value: Any) -> Any:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            return value
    return value


def _decimalize_nested(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _decimalize_nested(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decimalize_nested(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_decimalize_nested(item) for item in value)
    return _maybe_decimal(value)


class MarketSnapshot(SchemaBase):
    symbol: str
    exchange: str
    snapshot_ts: datetime
    best_bid: Decimal
    best_ask: Decimal
    last_price: Decimal
    bid_size: Decimal
    ask_size: Decimal
    volume_24h: Decimal
    kline_15m: dict[str, Any]
    kline_1h: dict[str, Any]
    recent_trades: list[dict[str, Any]] = Field(default_factory=list)
    orderbook_depth: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kline_15m", "kline_1h", "recent_trades", "orderbook_depth", mode="before")
    @classmethod
    def _normalize_nested_numbers(cls, value: Any) -> Any:
        return _decimalize_nested(value)
