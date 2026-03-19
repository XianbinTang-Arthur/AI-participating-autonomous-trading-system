from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field

from aats.schemas.common import SchemaBase, new_id


class ExchangeBalance(SchemaBase):
    currency: str
    total: Decimal
    available: Decimal
    frozen: Decimal = Decimal("0")


class ExchangePosition(SchemaBase):
    instrument_id: str
    symbol: str
    quantity: Decimal
    average_entry_price: Decimal | None = None
    mark_price: Decimal | None = None
    notional_usd: Decimal | None = None
    side: str = "net"


class ExchangeOpenOrder(SchemaBase):
    instrument_id: str
    client_order_id: str | None = None
    exchange_order_id: str
    side: str
    order_type: str
    status: str
    quantity: Decimal
    filled_quantity: Decimal = Decimal("0")
    price: Decimal | None = None
    created_ts: datetime | None = None
    updated_ts: datetime | None = None


class ExchangeFill(SchemaBase):
    fill_id: str
    exchange_order_id: str
    client_order_id: str | None = None
    instrument_id: str
    symbol: str
    side: str
    fill_qty: Decimal
    fill_price: Decimal
    fee_amount: Decimal = Decimal("0")
    fee_currency: str | None = None
    fill_ts: datetime | None = None


class InstrumentMetadata(SchemaBase):
    instrument_id: str
    symbol: str
    base_currency: str
    quote_currency: str
    lot_size: Decimal
    tick_size: Decimal
    min_size: Decimal
    contract_value: Decimal = Decimal("1")
    state: str
    raw: dict[str, Any] = Field(default_factory=dict)


class ExchangeAccountSnapshot(SchemaBase):
    account_source: str
    fetched_at: datetime
    balances: list[ExchangeBalance] = Field(default_factory=list)
    positions: list[ExchangePosition] = Field(default_factory=list)
    open_orders: list[ExchangeOpenOrder] = Field(default_factory=list)
    fills: list[ExchangeFill] = Field(default_factory=list)
    instruments: list[InstrumentMetadata] = Field(default_factory=list)
    account_mode: str | None = None
    position_mode: str | None = None
    fee_rates: dict[str, Any] = Field(default_factory=dict)
    account_risk: dict[str, Any] = Field(default_factory=dict)
    system_status: list[dict[str, Any]] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


BaselineImportStatus = Literal[
    "baseline_imported",
    "baseline_import_requires_review",
    "rebaseline_completed",
]


class AccountBaselineSnapshot(SchemaBase):
    baseline_id: str = Field(default_factory=lambda: new_id("baseline"))
    account_source: str
    exchange_snapshot_ts: datetime
    imported_at: datetime
    product_type: Literal["spot", "derivatives"] = "spot"
    margin_mode: Literal["cash", "cross", "isolated"] = "cash"
    allowed_symbols: list[str] = Field(default_factory=list)
    baseline_status: BaselineImportStatus
    baseline_kind: Literal["startup_import", "operator_rebaseline"] = "startup_import"
    safe_for_automatic_continuation: bool = True
    requires_operator_review: bool = False
    previous_baseline_ref: str | None = None
    operator_action_ref: str | None = None
    trigger_reason: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    balance_count: int = 0
    position_count: int = 0
    open_order_count: int = 0
    fill_count: int = 0
    balances: list[ExchangeBalance] = Field(default_factory=list)
    positions: list[ExchangePosition] = Field(default_factory=list)
    open_orders: list[ExchangeOpenOrder] = Field(default_factory=list)
    fills: list[ExchangeFill] = Field(default_factory=list)
    account_mode: str | None = None
