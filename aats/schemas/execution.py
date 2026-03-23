from __future__ import annotations

from decimal import Decimal
from datetime import datetime
from typing import Literal

from pydantic import Field

from aats.schemas.common import SchemaBase, new_id
from aats.schemas.system import MarginModelType, ProductType

OrderLifecycleStatus = Literal[
    "CREATED",
    "SUBMITTING",
    "SUBMITTED",
    "PARTIALLY_FILLED",
    "FILLED",
    "CANCEL_PENDING",
    "CANCELED",
    "REJECTED",
    "FAILED",
    "BLOCKED",
    "DRY_RUN",
    "EXPIRED",
]

ObligationStatus = Literal[
    "ACTIVE",
    "PARTIALLY_CONSUMED",
    "RELEASED",
    "CANCELED",
    "FAILED",
]

ExecutionParameterSuggestionStatus = Literal["reserved_not_enabled", "diagnostic_only", "shadow_translation", "enabled"]
ExecutionSuggestionMode = Literal["disabled", "diagnostic_only", "shadow_translation", "enabled_live"]
ExecutionAction = Literal["hold", "enter", "scale_in", "reduce", "exit", "reverse"]
PositionMode = Literal["net_mode", "long_short_mode"]
PositionSide = Literal["net", "long", "short"]


def execution_action_from_position_intent(position_intent: str | None) -> ExecutionAction | None:
    mapping: dict[str, ExecutionAction] = {
        "hold": "hold",
        "open_long": "enter",
        "open_short": "enter",
        "reduce_long": "reduce",
        "reduce_short": "reduce",
        "close_long": "exit",
        "close_short": "exit",
        "reverse_to_long": "reverse",
        "reverse_to_short": "reverse",
    }
    if position_intent is None:
        return None
    return mapping.get(str(position_intent).strip().lower())


def reduce_only_from_position_intent(position_intent: str | None) -> bool:
    if position_intent is None:
        return False
    return str(position_intent).strip().lower() in {
        "reduce_long",
        "reduce_short",
        "close_long",
        "close_short",
    }


def close_only_from_position_intent(position_intent: str | None) -> bool:
    if position_intent is None:
        return False
    return str(position_intent).strip().lower() in {"close_long", "close_short"}


def default_reduce_only_reason(
    *,
    position_intent: str | None,
    reduce_only: bool,
) -> str | None:
    if not reduce_only:
        return None
    if close_only_from_position_intent(position_intent):
        return "position_intent_close_path"
    return "position_intent_reduce_path"


def default_close_only_reason(
    *,
    position_intent: str | None,
    close_only: bool,
) -> str | None:
    if not close_only:
        return None
    return "position_intent_close_path"


def pos_side_from_position_intent(
    *,
    position_intent: str | None,
    position_mode: PositionMode | None,
) -> PositionSide | None:
    if position_mode == "net_mode":
        return "net"
    normalized = None if position_intent is None else str(position_intent).strip().lower()
    mapping: dict[str, PositionSide] = {
        "open_long": "long",
        "reduce_long": "long",
        "close_long": "long",
        "reverse_to_long": "long",
        "open_short": "short",
        "reduce_short": "short",
        "close_short": "short",
        "reverse_to_short": "short",
    }
    return mapping.get(normalized) if normalized is not None else None


class ExecutionParameterSuggestion(SchemaBase):
    passive_bias: Decimal | None = None
    maker_taker_bias: Decimal | None = None
    max_cross_spread_bps: Decimal | None = None
    slice_count: int | None = None
    max_participation_rate: Decimal | None = None
    cancel_replace_patience_ms: int | None = None


class ExecutionParameterTranslationPreview(SchemaBase):
    execution_style: str = "taker"
    order_type: Literal["market", "limit"] = "market"
    time_in_force: str = "IOC"
    limit_offset_bps: Decimal | None = None
    slice_count: int | None = None
    max_participation_rate: Decimal | None = None
    cancel_replace_patience_ms: int | None = None
    passive_bias: Decimal | None = None
    maker_taker_bias: Decimal | None = None


class AIExecutionParameterSuggestionEnvelope(SchemaBase):
    status: ExecutionParameterSuggestionStatus = "reserved_not_enabled"
    diagnostic_only: bool = True
    requested_mode: ExecutionSuggestionMode = "disabled"
    suggestion: ExecutionParameterSuggestion = Field(default_factory=ExecutionParameterSuggestion)
    translation_preview: ExecutionParameterTranslationPreview | None = None
    accepted_by_execution_planner: bool = False
    applied_to_live_execution: bool = False
    applied_live_fields: list[str] = Field(default_factory=list)
    clipped_fields: list[str] = Field(default_factory=list)
    rejection_reasons: list[str] = Field(default_factory=lambda: ["execution_parameter_suggestions_not_enabled"])
    notes: list[str] = Field(default_factory=list)
    live_translation_reason: str | None = None
    live_translation_fallback_reason: str | None = None


class OrderIntent(SchemaBase):
    intent_id: str
    decision_id: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: Decimal
    execution_style: str
    order_type: Literal["market", "limit"]
    limit_price: Decimal | None = None
    reference_price: Decimal | None = None
    urgency: Literal["low", "medium", "high"]
    time_in_force: str
    max_slippage_tolerance_bps: int | None = None
    reduce_only: bool = False
    close_only: bool = False
    td_mode: MarginModelType | None = None
    position_mode: PositionMode | None = None
    pos_side: PositionSide | None = None
    reduce_only_reason: str | None = None
    close_only_reason: str | None = None
    instrument_family: str | None = None
    settle_currency: str | None = None
    required_initial_margin: Decimal | None = None
    projected_margin_usage: Decimal | None = None
    projected_notional: Decimal | None = None
    risk_budget_multiplier: Decimal | None = None
    risk_budget_state: dict[str, object] = Field(default_factory=dict)
    execution_aggressiveness_multiplier: Decimal | None = None
    execution_aggressiveness_state: dict[str, object] = Field(default_factory=dict)
    only_reduce_required: bool = False
    risk_limit_breached: bool = False
    liquidation_buffer_remaining: Decimal | None = None
    idempotency_key: str
    product_type: ProductType = "spot"
    target_leverage: float = 1.0
    margin_mode: MarginModelType = "cash"
    exposure_side: Literal["long", "short", "flat"] = "flat"
    execution_action: ExecutionAction | None = None
    position_intent: Literal[
        "open_long",
        "reduce_long",
        "close_long",
        "open_short",
        "reduce_short",
        "close_short",
        "reverse_to_long",
        "reverse_to_short",
    ] = "open_long"
    ai_execution_parameter_suggestion: AIExecutionParameterSuggestionEnvelope | None = None


class ExecutionPlan(SchemaBase):
    plan_id: str
    decision_id: str
    symbol: str
    current_position_qty: Decimal
    target_position_qty: Decimal
    approved_target_position_qty: Decimal
    delta_qty: Decimal
    side: Literal["buy", "sell"]
    execution_style: str
    order_type: Literal["market", "limit"]
    limit_price: Decimal | None = None
    time_in_force: str = "IOC"
    urgency: Literal["low", "medium", "high"]
    max_slippage_tolerance_bps: int
    reference_price: Decimal | None = None
    reduce_only: bool = False
    close_only: bool = False
    td_mode: MarginModelType | None = None
    position_mode: PositionMode | None = None
    pos_side: PositionSide | None = None
    reduce_only_reason: str | None = None
    close_only_reason: str | None = None
    instrument_family: str | None = None
    settle_currency: str | None = None
    required_initial_margin: Decimal | None = None
    projected_margin_usage: Decimal | None = None
    projected_notional: Decimal | None = None
    risk_budget_multiplier: Decimal | None = None
    risk_budget_state: dict[str, object] = Field(default_factory=dict)
    execution_aggressiveness_multiplier: Decimal | None = None
    execution_aggressiveness_state: dict[str, object] = Field(default_factory=dict)
    only_reduce_required: bool = False
    risk_limit_breached: bool = False
    liquidation_buffer_remaining: Decimal | None = None
    product_type: ProductType = "spot"
    target_leverage: float = 1.0
    margin_mode: MarginModelType = "cash"
    exposure_side: Literal["long", "short", "flat"] = "flat"
    execution_action: ExecutionAction | None = None
    position_intent: Literal[
        "open_long",
        "reduce_long",
        "close_long",
        "open_short",
        "reduce_short",
        "close_short",
        "reverse_to_long",
        "reverse_to_short",
    ] = "open_long"
    ai_execution_parameter_suggestion: AIExecutionParameterSuggestionEnvelope | None = None


class OrderState(SchemaBase):
    decision_id: str
    intent_id: str
    symbol: str
    client_order_id: str
    venue: str = "PAPER"
    exchange_order_id: str | None = None
    status: OrderLifecycleStatus
    submission_mode: str = "paper_local"
    exchange_status: str | None = None
    exchange_status_history: list[str] = Field(default_factory=list)
    submitted_ts: datetime | None = None
    last_update_ts: datetime | None = None
    last_exchange_update_ts: datetime | None = None
    cancellation_requested_ts: datetime | None = None
    canceled_ts: datetime | None = None
    requested_qty: Decimal
    filled_qty: Decimal = Decimal("0")
    remaining_qty: Decimal
    average_fill_price: Decimal | None = None
    fees: Decimal = Decimal("0")
    reduce_only: bool = False
    close_only: bool = False
    td_mode: MarginModelType | None = None
    position_mode: PositionMode | None = None
    pos_side: PositionSide | None = None
    reduce_only_reason: str | None = None
    close_only_reason: str | None = None
    instrument_family: str | None = None
    settle_currency: str | None = None
    product_type: ProductType = "spot"
    target_leverage: float = 1.0
    margin_mode: MarginModelType = "cash"
    exposure_side: Literal["long", "short", "flat"] = "flat"
    execution_action: ExecutionAction | None = None
    position_intent: Literal[
        "open_long",
        "reduce_long",
        "close_long",
        "open_short",
        "reduce_short",
        "close_short",
        "reverse_to_long",
        "reverse_to_short",
    ] = "open_long"
    cancel_reason: str | None = None
    execution_error: str | None = None
    submission_payload: dict[str, str] = Field(default_factory=dict)


class FillEvent(SchemaBase):
    fill_id: str
    decision_id: str
    intent_id: str
    client_order_id: str
    exchange_order_id: str
    symbol: str
    venue: str = "PAPER"
    side: Literal["buy", "sell"]
    fill_qty: Decimal
    fill_price: Decimal
    fee_amount: Decimal
    fee_currency: str | None = None
    reduce_only: bool = False
    close_only: bool = False
    td_mode: MarginModelType | None = None
    position_mode: PositionMode | None = None
    pos_side: PositionSide | None = None
    reduce_only_reason: str | None = None
    close_only_reason: str | None = None
    instrument_family: str | None = None
    settle_currency: str | None = None
    product_type: ProductType = "spot"
    target_leverage: float = 1.0
    margin_mode: MarginModelType = "cash"
    exposure_side: Literal["long", "short", "flat"] = "flat"
    execution_action: ExecutionAction | None = None
    position_intent: Literal[
        "open_long",
        "reduce_long",
        "close_long",
        "open_short",
        "reduce_short",
        "close_short",
        "reverse_to_long",
        "reverse_to_short",
    ] = "open_long"
    liquidity_role: Literal["maker", "taker"]
    exchange_timestamp: datetime
    ingestion_timestamp: datetime
    order_status_after_fill: OrderLifecycleStatus | None = None


class OrderObligation(SchemaBase):
    obligation_id: str = Field(default_factory=lambda: new_id("obl"))
    client_order_id: str
    decision_id: str
    intent_id: str
    symbol: str
    side: Literal["buy", "sell"]
    reserve_currency: str
    reserved_amount: Decimal
    consumed_amount: Decimal = Decimal("0")
    released_amount: Decimal = Decimal("0")
    consumed_fill_ids: list[str] = Field(default_factory=list)
    status: ObligationStatus = "ACTIVE"
    product_type: ProductType = "spot"
    margin_mode: MarginModelType = "cash"
    reference_price: Decimal | None = None
    last_update_ts: datetime | None = None
