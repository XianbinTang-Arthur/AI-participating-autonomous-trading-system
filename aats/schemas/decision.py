from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from aats.schemas.common import SchemaBase


class DecisionContext(SchemaBase):
    decision_id: str
    symbol: str
    timeframe: Literal["15m", "1h"]
    as_of_ts: datetime
    market_snapshot_ref: str
    feature_snapshot_ref: str
    portfolio_snapshot_ref: str
    health_snapshot_ref: str
    mode: str
    policy_flags: list[str] = Field(default_factory=list)
    risk_budget_state: dict[str, float] = Field(default_factory=dict)
    current_position_qty: float
    current_open_orders: list[str] = Field(default_factory=list)

class BaselineAssessment(SchemaBase):
    decision_id: str
    symbol: str
    regime: str
    direction_bias: Literal["long", "short", "flat"]
    trend_strength: float
    volatility_state: str
    confidence: float
    holding_horizon: str
    invalidation_conditions: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    engine_version: str


class AIMarketAssessment(SchemaBase):
    decision_id: str
    symbol: str
    regime: str
    directional_edge: float
    expected_volatility: float
    confidence: float
    uncertainty: float
    expected_holding_horizon: str
    invalidation_conditions: list[str] = Field(default_factory=list)
    risk_tags: list[str] = Field(default_factory=list)
    rationale_summary: str
    model_name: str
    model_version: str
    prompt_version: str


class AIActionProposal(SchemaBase):
    decision_id: str
    symbol: str
    suggested_target_exposure: float
    suggested_entry_style: str
    suggested_reduce_conditions: list[str] = Field(default_factory=list)
    suggested_exit_conditions: list[str] = Field(default_factory=list)
    urgency: Literal["low", "medium", "high"]
    ttl_seconds: int


class PositionTarget(SchemaBase):
    decision_id: str
    symbol: str
    current_position_qty: float
    target_position_qty: float
    delta_position_qty: float
    current_notional: float
    target_notional: float
    rebalance_reason: str
    urgency: Literal["low", "medium", "high"]
    max_slippage_tolerance_bps: int
    source_mix: dict[str, float]
    decision_expiry_ts: datetime
