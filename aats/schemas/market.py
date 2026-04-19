from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field
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


class KlineBar(BaseModel):
    """Typed K-line (candlestick) bar with backward-compatible dict-style access."""

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal("0")

    @field_validator("open", "high", "low", "close", "volume", mode="before")
    @classmethod
    def _coerce_decimal(cls, value: Any) -> Any:
        return _maybe_decimal(value)

    def __getitem__(self, key: str) -> Any:
        """Allow ``bar["open"]`` style access for backward compatibility."""
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and hasattr(self, key)


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
    kline_15m: KlineBar
    kline_1h: KlineBar
    recent_trades: list[dict[str, Any]] = Field(default_factory=list)
    orderbook_depth: dict[str, Any] = Field(default_factory=dict)
    # P1.4 — OKX 衍生品 mark-price 频道推送的标记价。None 表示：
    #   - 现货 symbol（OKX 不推 mark-price）
    #   - WebSocket 暂未收到第一条 mark-price 推送
    #   - REST fallback 路径（当前不拉 mark-price）
    # basis 信号在 FeatureCalculator 里遇到 None 会返回 0 贡献，不破坏 composite.
    mark_price: Decimal | None = None

    @field_validator("recent_trades", "orderbook_depth", mode="before")
    @classmethod
    def _normalize_nested_numbers(cls, value: Any) -> Any:
        return _decimalize_nested(value)
