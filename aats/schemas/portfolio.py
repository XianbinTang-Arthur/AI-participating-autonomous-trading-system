from __future__ import annotations

from datetime import datetime

from pydantic import Field

from aats.schemas.common import SchemaBase


class Position(SchemaBase):
    symbol: str
    position_qty: float
    position_notional: float
    avg_entry_price: float
    unrealized_pnl: float


class PortfolioSnapshot(SchemaBase):
    decision_id: str | None = None
    source_intent_id: str | None = None
    source_fill_id: str | None = None
    snapshot_ts: datetime
    balances: dict[str, float]
    positions: list[Position] = Field(default_factory=list)
    cost_basis: dict[str, float] = Field(default_factory=dict)
    realized_pnl: float
    unrealized_pnl: float
    total_equity: float
    gross_exposure: float
    net_exposure: float
    risk_budget_usage: dict[str, float] = Field(default_factory=dict)
