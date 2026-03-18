from __future__ import annotations

from pydantic import Field

from aats.schemas.common import SchemaBase


class AIDecisionBrief(SchemaBase):
    decision_id: str
    symbol: str
    timeframe: str
    product_type: str
    margin_mode: str

    last_price: float | None = None
    mid_price: float | None = None
    spread_bps: float | None = None

    regime_indicator: str
    regime_confidence: float
    composite_alpha_score: float
    momentum_score: float
    trend_strength: float | None = None
    volatility_state: str
    volatility_value: float
    multi_timeframe_alignment: float | None = None

    liquidity_score: float | None = None
    execution_quality_scale: float | None = None
    top_of_book_imbalance: float | None = None
    depth_imbalance: float | None = None
    trade_flow_imbalance: float | None = None

    current_position_qty: float
    current_exposure_side: str
    current_open_order_count: int
    has_pending_close: bool = False
    gross_exposure: float | None = None
    margin_usage: float | None = None

    baseline_direction_bias: str
    baseline_confidence: float
    baseline_suggested_position_scale: float | None = None
    baseline_reason_codes: list[str] = Field(default_factory=list)

    fee_bps: float
    max_slippage_tolerance_bps: float
    expected_slippage_proxy_bps: float
    min_net_edge_bps: float

    safe_to_trade: bool
    review_required: bool
    halted: bool
    reconciliation_halt_required: bool
    market_snapshot_fresh: bool
    account_snapshot_fresh: bool
    execution_condition: str
