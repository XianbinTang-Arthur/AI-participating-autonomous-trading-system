from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from aats.schemas.common import SchemaBase


class ExchangeBalance(SchemaBase):
    currency: str
    total: float
    available: float
    frozen: float = 0.0


class ExchangePosition(SchemaBase):
    instrument_id: str
    symbol: str
    quantity: float
    average_entry_price: float | None = None
    mark_price: float | None = None
    notional_usd: float | None = None
    side: str = "net"


class ExchangeOpenOrder(SchemaBase):
    instrument_id: str
    client_order_id: str | None = None
    exchange_order_id: str
    side: str
    order_type: str
    status: str
    quantity: float
    filled_quantity: float = 0.0
    price: float | None = None
    created_ts: datetime | None = None
    updated_ts: datetime | None = None


class InstrumentMetadata(SchemaBase):
    instrument_id: str
    symbol: str
    base_currency: str
    quote_currency: str
    lot_size: float
    tick_size: float
    min_size: float
    state: str
    raw: dict[str, Any] = Field(default_factory=dict)


class ExchangeAccountSnapshot(SchemaBase):
    account_source: str
    fetched_at: datetime
    balances: list[ExchangeBalance] = Field(default_factory=list)
    positions: list[ExchangePosition] = Field(default_factory=list)
    open_orders: list[ExchangeOpenOrder] = Field(default_factory=list)
    instruments: list[InstrumentMetadata] = Field(default_factory=list)
    account_mode: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
