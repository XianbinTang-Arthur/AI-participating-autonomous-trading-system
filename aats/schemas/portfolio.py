from __future__ import annotations

from decimal import Decimal
from datetime import datetime
from typing import Literal

from pydantic import Field

from aats.schemas.common import SchemaBase
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
