from __future__ import annotations

from typing import Any
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
LegOrderAction = Literal["open", "reduce", "close"]


def execution_action_from_position_intent(position_intent: str | None) -> ExecutionAction | None:
    mapping: dict[str, ExecutionAction] = {
        "hold": "hold",
        "open_long": "enter",
        "scale_in_long": "scale_in",
        "open_short": "enter",
        "scale_in_short": "scale_in",
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


def execution_action_from_leg_action(action: LegOrderAction | None) -> ExecutionAction | None:
    mapping: dict[str, ExecutionAction] = {
        "open": "enter",
        "reduce": "reduce",
        "close": "exit",
    }
    if action is None:
        return None
    return mapping.get(str(action).strip().lower())


def side_from_position_intent(position_intent: str | None) -> Literal["buy", "sell"] | None:
    if position_intent is None:
        return None
    normalized = str(position_intent).strip().lower()
    mapping: dict[str, Literal["buy", "sell"]] = {
        "open_long": "buy",
        "scale_in_long": "buy",
        "reduce_long": "sell",
        "close_long": "sell",
        "reverse_to_long": "buy",
        "open_short": "sell",
        "scale_in_short": "sell",
        "reduce_short": "buy",
        "close_short": "buy",
        "reverse_to_short": "sell",
    }
    return mapping.get(normalized)


def reduce_only_from_position_intent(position_intent: str | None) -> bool:
    if position_intent is None:
        return False
    return str(position_intent).strip().lower() in {
        "reduce_long",
        "reduce_short",
        "close_long",
        "close_short",
    }


def reduce_only_from_leg_action(action: LegOrderAction | None) -> bool:
    if action is None:
        return False
    return str(action).strip().lower() in {"reduce", "close"}


def close_only_from_position_intent(position_intent: str | None) -> bool:
    if position_intent is None:
        return False
    return str(position_intent).strip().lower() in {"close_long", "close_short"}


def close_only_from_leg_action(action: LegOrderAction | None) -> bool:
    if action is None:
        return False
    return str(action).strip().lower() == "close"


def default_reduce_only_reason(
    *,
    position_intent: str | None,
    leg_action: LegOrderAction | None = None,
    reduce_only: bool,
) -> str | None:
    if not reduce_only:
        return None
    if close_only_from_leg_action(leg_action):
        return "explicit_leg_close_path"
    if reduce_only_from_leg_action(leg_action):
        return "explicit_leg_reduce_path"
    if close_only_from_position_intent(position_intent):
        return "position_intent_close_path"
    return "position_intent_reduce_path"


def default_close_only_reason(
    *,
    position_intent: str | None,
    leg_action: LegOrderAction | None = None,
    close_only: bool,
) -> str | None:
    if not close_only:
        return None
    if close_only_from_leg_action(leg_action):
        return "explicit_leg_close_path"
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
        "scale_in_long": "long",
        "reduce_long": "long",
        "close_long": "long",
        "reverse_to_long": "long",
        "open_short": "short",
        "scale_in_short": "short",
        "reduce_short": "short",
        "close_short": "short",
        "reverse_to_short": "short",
    }
    return mapping.get(normalized) if normalized is not None else None


def position_intent_from_leg_intent(
    *,
    side: Literal["buy", "sell"],
    pos_side: PositionSide | None,
    action: LegOrderAction,
    position_mode: PositionMode | None,
) -> Literal[
    "open_long",
    "reduce_long",
    "close_long",
    "open_short",
    "reduce_short",
    "close_short",
]:
    if position_mode != "long_short_mode":
        raise ValueError("explicit_leg_order_requires_long_short_mode")
    if pos_side not in {"long", "short"}:
        raise ValueError("explicit_leg_order_requires_pos_side")
    normalized_action = str(action).strip().lower()
    if pos_side == "long":
        expected_side = "buy" if normalized_action == "open" else "sell"
        if side != expected_side:
            raise ValueError("explicit_leg_order_side_pos_side_mismatch")
        mapping = {
            "open": "open_long",
            "reduce": "reduce_long",
            "close": "close_long",
        }
        return mapping[normalized_action]  # type: ignore[return-value]
    expected_side = "sell" if normalized_action == "open" else "buy"
    if side != expected_side:
        raise ValueError("explicit_leg_order_side_pos_side_mismatch")
    mapping = {
        "open": "open_short",
        "reduce": "reduce_short",
        "close": "close_short",
    }
    return mapping[normalized_action]  # type: ignore[return-value]


def execution_attempt_id_from_components(
    *,
    execution_attempt_id: str | None = None,
    client_order_id: str | None = None,
    execution_chain_id: str | None = None,
    intent_id: str | None = None,
) -> str | None:
    normalized_attempt_id = str(execution_attempt_id or "").strip()
    if normalized_attempt_id:
        return normalized_attempt_id
    normalized_client_order_id = str(client_order_id or "").strip()
    if normalized_client_order_id:
        return f"execution_attempt:{normalized_client_order_id}"
    normalized_chain_id = str(execution_chain_id or "").strip()
    if normalized_chain_id:
        return f"execution_attempt:{normalized_chain_id}"
    normalized_intent_id = str(intent_id or "").strip()
    if normalized_intent_id:
        return f"execution_attempt:{normalized_intent_id}"
    return None


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
    execution_chain_id: str | None = None
    execution_attempt_id: str | None = None
    leg_intent_id: str | None = None
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
    strategy_family: str | None = None
    strategy_sleeve_id: str | None = None
    allocation_id: str | None = None
    strategy_bundle_id: str | None = None
    strategy_leg_role: Literal["primary", "hedge", "inventory", "accumulation"] | None = None
    strategy_pair_id: str | None = None
    strategy_opportunity_kind: str | None = None
    strategy_execution_mode: str | None = None
    strategy_state_phase: str | None = None
    product_type: ProductType = "spot"
    target_leverage: float = 1.0
    margin_mode: MarginModelType = "cash"
    exposure_side: Literal["long", "short", "flat"] = "flat"
    execution_action: ExecutionAction | None = None
    leg_action: LegOrderAction | None = None
    position_intent: Literal[
        "open_long",
        "scale_in_long",
        "reduce_long",
        "close_long",
        "open_short",
        "scale_in_short",
        "reduce_short",
        "close_short",
        "reverse_to_long",
        "reverse_to_short",
    ] = "open_long"
    ai_execution_parameter_suggestion: AIExecutionParameterSuggestionEnvelope | None = None


class LegOrderIntent(SchemaBase):
    leg_intent_id: str
    execution_chain_id: str | None = None
    execution_attempt_id: str | None = None
    decision_id: str
    symbol: str
    side: Literal["buy", "sell"]
    pos_side: Literal["long", "short"]
    action: LegOrderAction
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
    position_mode: PositionMode = "long_short_mode"
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
    strategy_family: str | None = None
    strategy_sleeve_id: str | None = None
    allocation_id: str | None = None
    strategy_bundle_id: str | None = None
    strategy_leg_role: Literal["primary", "hedge", "inventory", "accumulation"] | None = None
    strategy_pair_id: str | None = None
    strategy_opportunity_kind: str | None = None
    strategy_execution_mode: str | None = None
    strategy_state_phase: str | None = None
    product_type: ProductType = "derivatives"
    target_leverage: float = 1.0
    margin_mode: MarginModelType = "cross"
    exposure_side: Literal["long", "short", "flat"] = "flat"
    position_intent: Literal[
        "open_long",
        "scale_in_long",
        "reduce_long",
        "close_long",
        "open_short",
        "scale_in_short",
        "reduce_short",
        "close_short",
        "reverse_to_long",
        "reverse_to_short",
    ] | None = None
    ai_execution_parameter_suggestion: AIExecutionParameterSuggestionEnvelope | None = None


class ExecutionPlan(SchemaBase):
    plan_id: str
    execution_chain_id: str | None = None
    execution_attempt_id: str | None = None
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
    strategy_family: str | None = None
    strategy_sleeve_id: str | None = None
    allocation_id: str | None = None
    strategy_bundle_id: str | None = None
    strategy_leg_role: Literal["primary", "hedge", "inventory", "accumulation"] | None = None
    strategy_pair_id: str | None = None
    strategy_opportunity_kind: str | None = None
    strategy_execution_mode: str | None = None
    strategy_state_phase: str | None = None
    product_type: ProductType = "spot"
    target_leverage: float = 1.0
    margin_mode: MarginModelType = "cash"
    exposure_side: Literal["long", "short", "flat"] = "flat"
    execution_action: ExecutionAction | None = None
    leg_action: LegOrderAction | None = None
    position_intent: Literal[
        "open_long",
        "scale_in_long",
        "reduce_long",
        "close_long",
        "open_short",
        "scale_in_short",
        "reduce_short",
        "close_short",
        "reverse_to_long",
        "reverse_to_short",
    ] = "open_long"
    ai_execution_parameter_suggestion: AIExecutionParameterSuggestionEnvelope | None = None


class LegExecutionPlan(SchemaBase):
    plan_id: str
    execution_chain_id: str | None = None
    execution_attempt_id: str | None = None
    leg_intent_id: str
    decision_id: str
    symbol: str
    side: Literal["buy", "sell"]
    pos_side: Literal["long", "short"]
    action: LegOrderAction
    quantity: Decimal
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
    position_mode: PositionMode = "long_short_mode"
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
    strategy_family: str | None = None
    strategy_sleeve_id: str | None = None
    allocation_id: str | None = None
    strategy_bundle_id: str | None = None
    strategy_leg_role: Literal["primary", "hedge", "inventory", "accumulation"] | None = None
    strategy_pair_id: str | None = None
    strategy_opportunity_kind: str | None = None
    strategy_execution_mode: str | None = None
    strategy_state_phase: str | None = None
    product_type: ProductType = "derivatives"
    target_leverage: float = 1.0
    margin_mode: MarginModelType = "cross"
    exposure_side: Literal["long", "short", "flat"] = "flat"
    execution_action: ExecutionAction | None = None
    position_intent: Literal[
        "open_long",
        "scale_in_long",
        "reduce_long",
        "close_long",
        "open_short",
        "scale_in_short",
        "reduce_short",
        "close_short",
        "reverse_to_long",
        "reverse_to_short",
    ]
    ai_execution_parameter_suggestion: AIExecutionParameterSuggestionEnvelope | None = None


class OrderState(SchemaBase):
    decision_id: str
    execution_chain_id: str | None = None
    execution_attempt_id: str | None = None
    intent_id: str
    leg_intent_id: str | None = None
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
    strategy_family: str | None = None
    strategy_sleeve_id: str | None = None
    allocation_id: str | None = None
    strategy_bundle_id: str | None = None
    strategy_leg_role: Literal["primary", "hedge", "inventory", "accumulation"] | None = None
    strategy_pair_id: str | None = None
    strategy_opportunity_kind: str | None = None
    strategy_execution_mode: str | None = None
    strategy_state_phase: str | None = None
    product_type: ProductType = "spot"
    target_leverage: float = 1.0
    margin_mode: MarginModelType = "cash"
    exposure_side: Literal["long", "short", "flat"] = "flat"
    execution_action: ExecutionAction | None = None
    leg_action: LegOrderAction | None = None
    position_intent: Literal[
        "open_long",
        "scale_in_long",
        "reduce_long",
        "close_long",
        "open_short",
        "scale_in_short",
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
    execution_chain_id: str | None = None
    execution_attempt_id: str | None = None
    intent_id: str
    leg_intent_id: str | None = None
    client_order_id: str
    exchange_order_id: str
    symbol: str
    venue: str = "PAPER"
    side: Literal["buy", "sell"]
    fill_qty: Decimal
    fill_price: Decimal
    fee_amount: Decimal  # 正值=费用支出，负值=返佣。交易所原始值需按约定映射（OKX: 取反）。
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
    strategy_family: str | None = None
    strategy_sleeve_id: str | None = None
    allocation_id: str | None = None
    strategy_bundle_id: str | None = None
    strategy_leg_role: Literal["primary", "hedge", "inventory", "accumulation"] | None = None
    strategy_pair_id: str | None = None
    strategy_opportunity_kind: str | None = None
    strategy_execution_mode: str | None = None
    strategy_state_phase: str | None = None
    product_type: ProductType = "spot"
    target_leverage: float = 1.0
    margin_mode: MarginModelType = "cash"
    exposure_side: Literal["long", "short", "flat"] = "flat"
    execution_action: ExecutionAction | None = None
    leg_action: LegOrderAction | None = None
    position_intent: Literal[
        "open_long",
        "scale_in_long",
        "reduce_long",
        "close_long",
        "open_short",
        "scale_in_short",
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
    blocked_fill_ids: list[str] = Field(default_factory=list)
    status: ObligationStatus = "ACTIVE"
    product_type: ProductType = "spot"
    margin_mode: MarginModelType = "cash"
    strategy_family: str | None = None
    strategy_sleeve_id: str | None = None
    allocation_id: str | None = None
    strategy_bundle_id: str | None = None
    strategy_leg_role: Literal["primary", "hedge", "inventory", "accumulation"] | None = None
    strategy_pair_id: str | None = None
    strategy_opportunity_kind: str | None = None
    strategy_execution_mode: str | None = None
    strategy_state_phase: str | None = None
    reference_price: Decimal | None = None
    processing_failure_reason: str | None = None
    processing_failure_details: dict[str, Any] = Field(default_factory=dict)
    last_update_ts: datetime | None = None


def leg_intent_from_order_intent(intent: OrderIntent) -> LegOrderIntent | None:
    if intent.position_mode != "long_short_mode":
        return None
    if intent.pos_side not in {"long", "short"}:
        return None
    if intent.leg_action not in {"open", "reduce", "close"}:
        return None
    return LegOrderIntent(
        leg_intent_id=intent.leg_intent_id or intent.intent_id,
        execution_chain_id=intent.execution_chain_id or intent.leg_intent_id or intent.intent_id,
        execution_attempt_id=intent.execution_attempt_id,
        decision_id=intent.decision_id,
        symbol=intent.symbol,
        side=intent.side,
        pos_side=intent.pos_side,
        action=intent.leg_action,
        quantity=intent.quantity,
        execution_style=intent.execution_style,
        order_type=intent.order_type,
        limit_price=intent.limit_price,
        reference_price=intent.reference_price,
        urgency=intent.urgency,
        time_in_force=intent.time_in_force,
        max_slippage_tolerance_bps=intent.max_slippage_tolerance_bps,
        reduce_only=intent.reduce_only,
        close_only=intent.close_only,
        td_mode=intent.td_mode,
        position_mode="long_short_mode",
        reduce_only_reason=intent.reduce_only_reason,
        close_only_reason=intent.close_only_reason,
        instrument_family=intent.instrument_family,
        settle_currency=intent.settle_currency,
        required_initial_margin=intent.required_initial_margin,
        projected_margin_usage=intent.projected_margin_usage,
        projected_notional=intent.projected_notional,
        risk_budget_multiplier=intent.risk_budget_multiplier,
        risk_budget_state=dict(intent.risk_budget_state),
        execution_aggressiveness_multiplier=intent.execution_aggressiveness_multiplier,
        execution_aggressiveness_state=dict(intent.execution_aggressiveness_state),
        only_reduce_required=intent.only_reduce_required,
        risk_limit_breached=intent.risk_limit_breached,
        liquidation_buffer_remaining=intent.liquidation_buffer_remaining,
        idempotency_key=intent.idempotency_key,
        strategy_family=intent.strategy_family,
        strategy_sleeve_id=intent.strategy_sleeve_id,
        allocation_id=intent.allocation_id,
        strategy_bundle_id=intent.strategy_bundle_id,
        strategy_leg_role=intent.strategy_leg_role,
        strategy_pair_id=intent.strategy_pair_id,
        strategy_opportunity_kind=intent.strategy_opportunity_kind,
        strategy_execution_mode=intent.strategy_execution_mode,
        strategy_state_phase=intent.strategy_state_phase,
        product_type=intent.product_type,
        target_leverage=intent.target_leverage,
        margin_mode=intent.margin_mode,
        exposure_side=intent.exposure_side,
        position_intent=intent.position_intent,
        ai_execution_parameter_suggestion=intent.ai_execution_parameter_suggestion,
    )


def order_intent_from_leg_order_intent(leg_intent: LegOrderIntent) -> OrderIntent:
    position_intent = leg_intent.position_intent or position_intent_from_leg_intent(
        side=leg_intent.side,
        pos_side=leg_intent.pos_side,
        action=leg_intent.action,
        position_mode=leg_intent.position_mode,
    )
    reduce_only = bool(leg_intent.reduce_only or reduce_only_from_leg_action(leg_intent.action))
    close_only = bool(leg_intent.close_only or close_only_from_leg_action(leg_intent.action))
    return OrderIntent(
        intent_id=leg_intent.leg_intent_id,
        execution_chain_id=leg_intent.execution_chain_id or leg_intent.leg_intent_id,
        execution_attempt_id=leg_intent.execution_attempt_id,
        leg_intent_id=leg_intent.leg_intent_id,
        decision_id=leg_intent.decision_id,
        symbol=leg_intent.symbol,
        side=leg_intent.side,
        quantity=leg_intent.quantity,
        execution_style=leg_intent.execution_style,
        order_type=leg_intent.order_type,
        limit_price=leg_intent.limit_price,
        reference_price=leg_intent.reference_price,
        urgency=leg_intent.urgency,
        time_in_force=leg_intent.time_in_force,
        max_slippage_tolerance_bps=leg_intent.max_slippage_tolerance_bps,
        reduce_only=reduce_only,
        close_only=close_only,
        td_mode=leg_intent.td_mode,
        position_mode=leg_intent.position_mode,
        pos_side=leg_intent.pos_side,
        reduce_only_reason=(
            leg_intent.reduce_only_reason
            or default_reduce_only_reason(
                position_intent=position_intent,
                leg_action=leg_intent.action,
                reduce_only=reduce_only,
            )
        ),
        close_only_reason=(
            leg_intent.close_only_reason
            or default_close_only_reason(
                position_intent=position_intent,
                leg_action=leg_intent.action,
                close_only=close_only,
            )
        ),
        instrument_family=leg_intent.instrument_family,
        settle_currency=leg_intent.settle_currency,
        required_initial_margin=leg_intent.required_initial_margin,
        projected_margin_usage=leg_intent.projected_margin_usage,
        projected_notional=leg_intent.projected_notional,
        risk_budget_multiplier=leg_intent.risk_budget_multiplier,
        risk_budget_state=dict(leg_intent.risk_budget_state),
        execution_aggressiveness_multiplier=leg_intent.execution_aggressiveness_multiplier,
        execution_aggressiveness_state=dict(leg_intent.execution_aggressiveness_state),
        only_reduce_required=leg_intent.only_reduce_required,
        risk_limit_breached=leg_intent.risk_limit_breached,
        liquidation_buffer_remaining=leg_intent.liquidation_buffer_remaining,
        idempotency_key=leg_intent.idempotency_key,
        strategy_family=leg_intent.strategy_family,
        strategy_sleeve_id=leg_intent.strategy_sleeve_id,
        allocation_id=leg_intent.allocation_id,
        strategy_bundle_id=leg_intent.strategy_bundle_id,
        strategy_leg_role=leg_intent.strategy_leg_role,
        strategy_pair_id=leg_intent.strategy_pair_id,
        strategy_opportunity_kind=leg_intent.strategy_opportunity_kind,
        strategy_execution_mode=leg_intent.strategy_execution_mode,
        strategy_state_phase=leg_intent.strategy_state_phase,
        product_type=leg_intent.product_type,
        target_leverage=leg_intent.target_leverage,
        margin_mode=leg_intent.margin_mode,
        exposure_side=leg_intent.exposure_side,
        execution_action=execution_action_from_leg_action(leg_intent.action),
        leg_action=leg_intent.action,
        position_intent=position_intent,
        ai_execution_parameter_suggestion=leg_intent.ai_execution_parameter_suggestion,
    )
