from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from aats.schemas.common import SchemaBase


class AIDecisionBrief(SchemaBase):
    decision_id: str
    symbol: str
    timeframe: str
    product_type: str
    margin_mode: str

    last_price: Decimal | None = None
    mid_price: Decimal | None = None
    spread_bps: Decimal | None = None

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

    current_position_qty: Decimal
    current_exposure_side: str
    current_open_order_count: int
    has_pending_close: bool = False
    gross_exposure: Decimal | None = None
    margin_usage: Decimal | None = None

    baseline_direction_bias: str
    baseline_confidence: float
    baseline_suggested_position_scale: float | None = None
    baseline_reason_codes: list[str] = Field(default_factory=list)

    fee_bps: Decimal
    funding_fee_bps: Decimal = Decimal("0")
    max_slippage_tolerance_bps: Decimal
    expected_slippage_proxy_bps: Decimal
    min_net_edge_bps: Decimal

    safe_to_trade: bool
    review_required: bool
    halted: bool
    reconciliation_halt_required: bool
    market_snapshot_fresh: bool
    account_snapshot_fresh: bool
    execution_condition: str
