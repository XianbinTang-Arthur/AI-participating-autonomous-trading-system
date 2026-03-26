from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


ArbitrageExecutionMode = Literal[
    "spot_carry",
    "inventory_reverse_carry",
    "margin_reverse_carry",
    "inter_derivatives_spread",
]
ArbitrageOpportunityKind = Literal[
    "positive_basis",
    "negative_basis",
    "pair_hold",
    "pair_exit",
    "pair_recovery",
    "protective_exit",
    "market_unavailable",
]
ArbitrageStatePhase = Literal[
    "inactive",
    "candidate",
    "blocked",
    "opening",
    "active",
    "rebalancing",
    "unwinding",
    "recovery",
    "advisory",
]


class ArbitragePairDefinition(BaseModel):
    pair_id: str
    spot_symbol: str
    hedge_symbol: str
    spot_product_type: Literal["spot"] = "spot"
    hedge_product_type: Literal["derivatives"] = "derivatives"
    settle_currency: str | None = None
    execution_modes: tuple[ArbitrageExecutionMode, ...] = (
        "spot_carry",
        "inventory_reverse_carry",
        "margin_reverse_carry",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArbitrageExecutionCapability(BaseModel):
    runtime_supported: bool = True
    inventory_backed_spot_sell_supported: bool = False
    spot_margin_short_supported: bool = False
    spot_margin_mode: Literal["cash", "cross", "isolated"] = "cash"
    margin_short_execution_ready: bool = False
    derivatives_short_supported: bool = True
    funding_data_available: bool = False
    borrow_rate_available: bool = False
    multi_leg_recovery_supported: bool = True
    inventory_reservation_enabled: bool = False
    auto_repay_supported: bool = False
    available_inventory_qty: Decimal = Decimal("0")
    only_reduce_required: bool = False
    blocking_reasons: list[str] = Field(default_factory=list)


class ArbitragePairState(BaseModel):
    pair_id: str
    state_phase: ArbitrageStatePhase = "inactive"
    current_direction: Literal["flat", "positive_carry", "reverse_carry", "mixed"] = "flat"
    current_spot_qty: Decimal = Decimal("0")
    current_cash_spot_qty: Decimal = Decimal("0")
    current_margin_spot_qty: Decimal = Decimal("0")
    current_hedge_qty: Decimal = Decimal("0")
    current_positive_pair_qty: Decimal = Decimal("0")
    current_reverse_pair_qty: Decimal = Decimal("0")
    current_inventory_reverse_pair_qty: Decimal = Decimal("0")
    current_margin_reverse_pair_qty: Decimal = Decimal("0")
    current_pair_qty: Decimal = Decimal("0")
    current_account_spot_qty: Decimal = Decimal("0")
    current_account_cash_spot_qty: Decimal = Decimal("0")
    current_account_margin_spot_qty: Decimal = Decimal("0")
    current_account_hedge_qty: Decimal = Decimal("0")
    foreign_spot_qty: Decimal = Decimal("0")
    foreign_hedge_qty: Decimal = Decimal("0")
    unpaired_spot_qty: Decimal = Decimal("0")
    unpaired_hedge_qty: Decimal = Decimal("0")
    current_short_qty: Decimal = Decimal("0")
    current_long_qty: Decimal = Decimal("0")
    recovery_required: bool = False
    unwind_required: bool = False
    blocking_reasons: list[str] = Field(default_factory=list)


class ArbitrageCostBreakdown(BaseModel):
    estimated_fee_bps: Decimal = Decimal("0")
    estimated_slippage_bps: Decimal = Decimal("0")
    estimated_funding_bps: Decimal = Decimal("0")
    estimated_borrow_bps: Decimal = Decimal("0")
    estimated_inventory_cost_bps: Decimal = Decimal("0")
    estimated_total_cost_bps: Decimal = Decimal("0")
    net_edge_bps: Decimal = Decimal("0")


class ArbitrageOpportunity(BaseModel):
    pair_id: str
    spot_symbol: str
    hedge_symbol: str
    opportunity_kind: ArbitrageOpportunityKind
    direction: Literal["positive_basis", "negative_basis", "neutral"]
    execution_mode: ArbitrageExecutionMode | None = None
    state_phase: ArbitrageStatePhase = "inactive"
    basis_bps: Decimal = Decimal("0")
    entry_threshold_bps: Decimal = Decimal("0")
    exit_threshold_bps: Decimal = Decimal("0")
    desired_pair_qty: Decimal = Decimal("0")
    target_spot_qty: Decimal = Decimal("0")
    target_hedge_qty: Decimal = Decimal("0")
    target_account_spot_qty: Decimal = Decimal("0")
    target_account_hedge_qty: Decimal = Decimal("0")
    score: float = 0.0
    confidence: float = 0.0
    urgency: Literal["low", "medium", "high"] = "low"
    reason_codes: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    route_action: Literal["override_target", "hold_current", "advisory_only"] = "hold_current"
    cost_breakdown: ArbitrageCostBreakdown = Field(default_factory=ArbitrageCostBreakdown)
    metadata: dict[str, Any] = Field(default_factory=dict)
