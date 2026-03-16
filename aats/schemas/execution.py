from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from aats.schemas.common import SchemaBase
from aats.schemas.system import MarginModelType, ProductType

OrderLifecycleStatus = Literal[
    "CREATED",
    "SUBMITTING",
    "SUBMITTED",
    "PARTIALLY_FILLED",
    "FILLED",
    "CANCEL_PENDING",
    "CANCELED",
    "REJECTED",
    "FAILED",
    "BLOCKED",
    "DRY_RUN",
    "EXPIRED",
]


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
    product_type: ProductType = "spot"
    target_leverage: float = 1.0
    margin_mode: MarginModelType = "cash"
    exposure_side: Literal["long", "short", "flat"] = "flat"
    position_intent: Literal[
        "open_long",
        "reduce_long",
        "close_long",
        "open_short",
        "reduce_short",
        "close_short",
        "reverse_to_long",
        "reverse_to_short",
    ] = "open_long"


class ExecutionPlan(SchemaBase):
    plan_id: str
    decision_id: str
    symbol: str
    current_position_qty: float
    target_position_qty: float
    approved_target_position_qty: float
    delta_qty: float
    side: Literal["buy", "sell"]
    execution_style: str
    order_type: Literal["market", "limit"]
    urgency: Literal["low", "medium", "high"]
    max_slippage_tolerance_bps: int
    product_type: ProductType = "spot"
    target_leverage: float = 1.0
    margin_mode: MarginModelType = "cash"
    exposure_side: Literal["long", "short", "flat"] = "flat"
    position_intent: Literal[
        "open_long",
        "reduce_long",
        "close_long",
        "open_short",
        "reduce_short",
        "close_short",
        "reverse_to_long",
        "reverse_to_short",
    ] = "open_long"


class OrderState(SchemaBase):
    decision_id: str
    intent_id: str
    symbol: str
    client_order_id: str
    venue: str = "PAPER"
    exchange_order_id: str | None = None
    status: OrderLifecycleStatus
    submission_mode: str = "paper_local"
    exchange_status: str | None = None
    exchange_status_history: list[str] = Field(default_factory=list)
    submitted_ts: datetime | None = None
    last_update_ts: datetime | None = None
    last_exchange_update_ts: datetime | None = None
    cancellation_requested_ts: datetime | None = None
    canceled_ts: datetime | None = None
    requested_qty: float
    filled_qty: float = 0.0
    remaining_qty: float
    average_fill_price: float | None = None
    fees: float = 0.0
    product_type: ProductType = "spot"
    target_leverage: float = 1.0
    margin_mode: MarginModelType = "cash"
    exposure_side: Literal["long", "short", "flat"] = "flat"
    position_intent: Literal[
        "open_long",
        "reduce_long",
        "close_long",
        "open_short",
        "reduce_short",
        "close_short",
        "reverse_to_long",
        "reverse_to_short",
    ] = "open_long"
    cancel_reason: str | None = None
    execution_error: str | None = None
    submission_payload: dict[str, str] = Field(default_factory=dict)


class FillEvent(SchemaBase):
    fill_id: str
    decision_id: str
    intent_id: str
    client_order_id: str
    exchange_order_id: str
    symbol: str
    venue: str = "PAPER"
    side: Literal["buy", "sell"]
    fill_qty: float
    fill_price: float
    fee_amount: float
    fee_currency: str | None = None
    product_type: ProductType = "spot"
    target_leverage: float = 1.0
    margin_mode: MarginModelType = "cash"
    exposure_side: Literal["long", "short", "flat"] = "flat"
    position_intent: Literal[
        "open_long",
        "reduce_long",
        "close_long",
        "open_short",
        "reduce_short",
        "close_short",
        "reverse_to_long",
        "reverse_to_short",
    ] = "open_long"
    liquidity_role: Literal["maker", "taker"]
    exchange_timestamp: datetime
    ingestion_timestamp: datetime
    order_status_after_fill: OrderLifecycleStatus | None = None
