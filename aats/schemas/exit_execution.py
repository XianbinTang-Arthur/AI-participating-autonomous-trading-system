from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field

from aats.schemas.common import SchemaBase, utc_now


ParentAggregateStatus = Literal[
    "CREATED",
    "DISPATCHING",
    "WORKING",
    "PARTIALLY_FILLED",
    "CANCEL_PENDING",
    "COMPLETED",
    "CANCELED",
    "REVIEW_REQUIRED",
    "FAILED_SAFE",
]

ParentReconciliationState = Literal["clean", "truth_pending", "review_required"]
ExitIntentKind = Literal["reduce", "close", "flatten"]
ChildExitOrderCategory = Literal[
    "PENDING_DISPATCH",
    "WORKING",
    "TERMINAL_FILLED",
    "TERMINAL_NONFILLED",
    "UNKNOWN_TRUTH",
]
WriteConfirmationState = Literal["confirmed", "unknown", "not_required"]


class ExitExecutionIntent(SchemaBase):
    parent_intent_id: str
    execution_chain_id: str
    strategy_run_id: str | None = None
    symbol: str
    market: str | None = None
    instrument_type: str | None = None
    side: Literal["sell", "buy"]
    position_side: Literal["long", "short", "net"] | None = None
    intent_kind: ExitIntentKind
    target_exit_quantity: Decimal
    target_exit_notional: Decimal | None = None
    aggregated_filled_quantity: Decimal = Decimal("0")
    aggregated_canceled_quantity: Decimal = Decimal("0")
    aggregated_rejected_quantity: Decimal = Decimal("0")
    open_child_working_quantity: Decimal = Decimal("0")
    open_child_unknown_quantity: Decimal = Decimal("0")
    remaining_dispatchable_quantity: Decimal = Decimal("0")
    remaining_unresolved_quantity: Decimal = Decimal("0")
    aggregate_status: ParentAggregateStatus = "CREATED"
    reconciliation_state: ParentReconciliationState = "clean"
    risk_reducing_invariant: bool = True
    dispatch_policy: str = "serial_exit_only"
    aggregate_version: int = 0
    child_order_ids: list[str] = Field(default_factory=list)
    cancel_requested: bool = False
    cancel_requested_ts: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    operator_review_required: bool = False
    operator_review_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChildExitOrderRef(SchemaBase):
    parent_intent_id: str
    child_order_id: str
    client_order_id: str
    exchange_order_id: str | None = None
    execution_chain_id: str | None = None
    intent_id: str | None = None
    symbol: str
    planned_quantity: Decimal
    known_filled_quantity: Decimal = Decimal("0")
    remaining_quantity_estimate: Decimal = Decimal("0")
    child_status: str
    aggregate_category: ChildExitOrderCategory
    write_confirmation_state: WriteConfirmationState = "not_required"
    exchange_truth_pending: bool = False
    operator_review_required: bool = False
    risk_reducing_invariant: bool = True
    updated_at: datetime = Field(default_factory=utc_now)
