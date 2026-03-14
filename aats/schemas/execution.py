from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from aats.schemas.common import SchemaBase


class OrderIntent(SchemaBase):
    intent_id: str
    decision_id: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: float
    execution_style: str
    order_type: Literal["market", "limit"]
    limit_price: float | None = None
    urgency: Literal["low", "medium", "high"]
    time_in_force: str
    reduce_only: bool = False
    close_only: bool = False
    idempotency_key: str


class OrderState(SchemaBase):
    decision_id: str
    intent_id: str
    client_order_id: str
    venue: str = "PAPER"
    exchange_order_id: str | None = None
    status: str
    submitted_ts: datetime | None = None
    last_update_ts: datetime | None = None
    requested_qty: float
    filled_qty: float = 0.0
    remaining_qty: float
    average_fill_price: float | None = None
    fees: float = 0.0
    cancel_reason: str | None = None
    submission_payload: dict[str, str] = Field(default_factory=dict)


class FillEvent(SchemaBase):
    fill_id: str
    decision_id: str
    intent_id: str
    client_order_id: str
    exchange_order_id: str
    symbol: str
    side: Literal["buy", "sell"]
    fill_qty: float
    fill_price: float
    fee_amount: float
    liquidity_role: Literal["maker", "taker"]
    exchange_timestamp: datetime
    ingestion_timestamp: datetime
