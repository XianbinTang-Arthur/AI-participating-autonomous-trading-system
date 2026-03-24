from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field

from aats.schemas.common import SchemaBase, new_id, utc_now
from aats.schemas.system import MarginModelType, ProductType


StrategyFamily = Literal["directional", "smart_arbitrage", "spot_grid", "dca"]
StrategyCandidateState = Literal["ready", "inactive", "disabled", "incompatible", "advisory_only"]
StrategyRouteAction = Literal["override_target", "hold_current", "advisory_only", "protective_fallback"]


class StrategyLegIntent(SchemaBase):
    symbol: str
    product_type: ProductType
    side: Literal["buy", "sell"]
    role: Literal["primary", "hedge", "inventory", "accumulation"] = "primary"
    target_position_qty: Decimal | None = None
    delta_position_qty: Decimal | None = None
    note: str | None = None


class StrategyCandidate(SchemaBase):
    family: StrategyFamily
    state: StrategyCandidateState
    enabled: bool = False
    selectable: bool = False
    execution_compatible: bool = False
    route_action: StrategyRouteAction = "hold_current"
    headline: str
    recommended_symbol: str | None = None
    target_position_qty: Decimal | None = None
    delta_position_qty: Decimal | None = None
    score: float = 0.0
    confidence: float = 0.0
    urgency: Literal["low", "medium", "high"] = "low"
    reason_codes: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    legs: list[StrategyLegIntent] = Field(default_factory=list)


class StrategyCoordinatorSnapshot(SchemaBase):
    snapshot_id: str = Field(default_factory=lambda: new_id("strategy"))
    decision_id: str
    symbol: str
    timeframe: Literal["15m", "1h"]
    product_type: ProductType
    margin_mode: MarginModelType
    allowed_symbols: tuple[str, ...] = Field(default_factory=tuple)
    active_family: StrategyFamily = "directional"
    selected_family: StrategyFamily = "directional"
    selected_state: StrategyCandidateState = "ready"
    selected_route_action: StrategyRouteAction = "override_target"
    selected_headline: str | None = None
    selection_reason_codes: list[str] = Field(default_factory=list)
    candidates: list[StrategyCandidate] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
