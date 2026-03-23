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
    margin_mode: str | None = None
    margin_currency: str | None = None
    leverage: Decimal | None = None
    margin_allocated: Decimal | None = None
    maintenance_margin: Decimal | None = None
    margin_ratio: Decimal | None = None
    liquidation_price: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    instrument_family: str | None = None
    settle_currency: str | None = None


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


class ExchangeAccountConfiguration(SchemaBase):
    account_level_code: str | None = None
    account_level_label: str | None = None
    position_mode: str | None = None
    position_mode_label: str | None = None
    auto_loan_enabled: bool | None = None
    greeks_type: str | None = None
    isolated_margin_mode: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ExchangeFeeSchedule(SchemaBase):
    maker: Decimal | None = None
    taker: Decimal | None = None
    delivery: Decimal | None = None
    exercise: Decimal | None = None
    source: str = "unavailable"
    raw: dict[str, Any] = Field(default_factory=dict)


class ExchangeAccountRiskSnapshot(SchemaBase):
    adjusted_equity: Decimal | None = None
    total_equity: Decimal | None = None
    available_equity: Decimal | None = None
    initial_margin_requirement: Decimal | None = None
    maintenance_margin_requirement: Decimal | None = None
    margin_ratio: Decimal | None = None
    notional_usd: Decimal | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ExchangeSystemStatusItem(SchemaBase):
    state: str
    service_type: str | None = None
    title: str | None = None
    description: str | None = None
    begin_ts: datetime | None = None
    end_ts: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class InstrumentMetadata(SchemaBase):
    instrument_id: str
    symbol: str
    base_currency: str
    quote_currency: str
    lot_size: Decimal
    tick_size: Decimal
    min_size: Decimal
    contract_value: Decimal = Decimal("1")
    instrument_type: str | None = None
    instrument_family: str | None = None
    underlying: str | None = None
    settle_currency: str | None = None
    contract_value_currency: str | None = None
    max_leverage: Decimal | None = None
    max_market_size: Decimal | None = None
    max_limit_size: Decimal | None = None
    list_ts: datetime | None = None
    expiry_ts: datetime | None = None
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
    account_configuration: ExchangeAccountConfiguration | None = None
    fee_rates: dict[str, Any] = Field(default_factory=dict)
    fee_schedule: ExchangeFeeSchedule | None = None
    account_risk: dict[str, Any] = Field(default_factory=dict)
    risk_snapshot: ExchangeAccountRiskSnapshot | None = None
    system_status: list[dict[str, Any]] = Field(default_factory=list)
    system_status_items: list[ExchangeSystemStatusItem] = Field(default_factory=list)
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
