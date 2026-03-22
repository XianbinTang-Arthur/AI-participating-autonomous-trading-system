from __future__ import annotations

from decimal import Decimal
from datetime import datetime
from typing import Literal

from pydantic import Field

from aats.schemas.common import SchemaBase, utc_now
from aats.schemas.execution import FillEvent
from aats.schemas.system import MarginModelType, ProductType

PortfolioSnapshotOrigin = Literal[
    "fill_derived",
    "exchange_import",
    "operator_rebaseline",
    "local_repair",
    "runtime_bootstrap",
    "recovery_rebuild",
    "manual_rebuild",
]

TRUSTED_BASELINE_SNAPSHOT_ORIGINS = {"exchange_import", "operator_rebaseline"}
BASELINE_SNAPSHOT_ORIGINS = {
    "exchange_import",
    "operator_rebaseline",
    "runtime_bootstrap",
    "recovery_rebuild",
    "manual_rebuild",
}


def is_legacy_baseline_snapshot(snapshot: "PortfolioSnapshot") -> bool:
    return snapshot.source_fill_id is None and snapshot.source_intent_id is None


def is_baseline_snapshot(snapshot: "PortfolioSnapshot") -> bool:
    return snapshot.snapshot_origin in BASELINE_SNAPSHOT_ORIGINS or is_legacy_baseline_snapshot(snapshot)


def is_trusted_baseline_snapshot(snapshot: "PortfolioSnapshot") -> bool:
    return snapshot.snapshot_origin in TRUSTED_BASELINE_SNAPSHOT_ORIGINS or is_legacy_baseline_snapshot(snapshot)


class Position(SchemaBase):
    symbol: str
    position_qty: Decimal
    position_notional: Decimal
    avg_entry_price: Decimal
    unrealized_pnl: Decimal
    product_type: ProductType = "spot"
    exposure_side: str = "flat"
    target_leverage: float = 1.0
    margin_mode: MarginModelType = "cash"
    margin_allocated: Decimal = Decimal("0")
    maintenance_margin: Decimal = Decimal("0")
    liquidation_price: Decimal | None = None


class PortfolioSnapshot(SchemaBase):
    decision_id: str | None = None
    source_intent_id: str | None = None
    source_fill_id: str | None = None
    snapshot_origin: PortfolioSnapshotOrigin = "fill_derived"
    snapshot_ts: datetime
    balances: dict[str, Decimal]
    positions: list[Position] = Field(default_factory=list)
    cost_basis: dict[str, Decimal] = Field(default_factory=dict)
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_equity: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    risk_budget_usage: dict[str, Decimal] = Field(default_factory=dict)
    product_type: ProductType = "spot"
    margin_mode: MarginModelType = "cash"
    margin_usage: Decimal = Decimal("0")
    leverage_profile: dict[str, float] = Field(default_factory=dict)
    cash_equity: Decimal = Decimal("0")
    spot_asset_equity: Decimal = Decimal("0")
    off_position_asset_equity: Decimal = Decimal("0")
    derivatives_unrealized_pnl: Decimal = Decimal("0")
    collateral_value: Decimal = Decimal("0")


class PortfolioBalanceDelta(SchemaBase):
    decision_id: str | None = None
    intent_id: str | None = None
    order_id: str | None = None
    fill_id: str
    symbol: str
    balances_before: dict[str, Decimal] = Field(default_factory=dict)
    balances_after: dict[str, Decimal] = Field(default_factory=dict)
    balance_deltas: dict[str, Decimal] = Field(default_factory=dict)
    realized_pnl_delta: Decimal = Decimal("0")
    fee_delta: Decimal = Decimal("0")
    product_type: ProductType = "spot"
    margin_mode: MarginModelType = "cash"
    created_at: datetime = Field(default_factory=utc_now)


class FillOutcomeRecord(SchemaBase):
    fill_id: str
    decision_id: str | None = None
    intent_id: str | None = None
    order_id: str | None = None
    symbol: str
    venue: str | None = None
    side: str | None = None
    fill_qty: Decimal | None = None
    fill_price: Decimal | None = None
    fill_notional: Decimal | None = None
    fee_amount: Decimal | None = None
    fee_currency: str | None = None
    liquidity_role: str | None = None
    exchange_timestamp: datetime | None = None
    ingestion_timestamp: datetime | None = None
    order_status_after_fill: str | None = None
    target_leverage: float | None = None
    exposure_side: str | None = None
    execution_action: str | None = None
    position_intent: str | None = None
    starting_position_qty: Decimal | None = None
    starting_avg_entry_price: Decimal | None = None
    ending_position_qty: Decimal | None = None
    ending_avg_entry_price: Decimal | None = None
    balances_before: dict[str, Decimal] = Field(default_factory=dict)
    balances_after: dict[str, Decimal] = Field(default_factory=dict)
    balance_deltas: dict[str, Decimal] = Field(default_factory=dict)
    realized_pnl_delta: Decimal = Decimal("0")
    fee_delta: Decimal = Decimal("0")
    product_type: ProductType = "spot"
    margin_mode: MarginModelType = "cash"
    created_at: datetime = Field(default_factory=utc_now)

    @classmethod
    def from_balance_delta(cls, balance_delta: PortfolioBalanceDelta) -> "FillOutcomeRecord":
        return cls.model_validate(balance_delta.model_dump(mode="python"))

    @classmethod
    def from_fill_and_balance_delta(
        cls,
        *,
        fill: FillEvent,
        balance_delta: PortfolioBalanceDelta,
        starting_position_qty: Decimal | None = None,
        starting_avg_entry_price: Decimal | None = None,
        ending_position_qty: Decimal | None = None,
        ending_avg_entry_price: Decimal | None = None,
    ) -> "FillOutcomeRecord":
        return cls.model_validate(
            {
                **balance_delta.model_dump(mode="python"),
                "venue": fill.venue,
                "side": fill.side,
                "fill_qty": fill.fill_qty,
                "fill_price": fill.fill_price,
                "fill_notional": fill.fill_qty * fill.fill_price,
                "fee_amount": fill.fee_amount,
                "fee_currency": fill.fee_currency,
                "liquidity_role": fill.liquidity_role,
                "exchange_timestamp": fill.exchange_timestamp,
                "ingestion_timestamp": fill.ingestion_timestamp,
                "order_status_after_fill": fill.order_status_after_fill,
                "target_leverage": fill.target_leverage,
                "exposure_side": fill.exposure_side,
                "execution_action": fill.execution_action,
                "position_intent": fill.position_intent,
                "starting_position_qty": starting_position_qty,
                "starting_avg_entry_price": starting_avg_entry_price,
                "ending_position_qty": ending_position_qty,
                "ending_avg_entry_price": ending_avg_entry_price,
            }
        )
