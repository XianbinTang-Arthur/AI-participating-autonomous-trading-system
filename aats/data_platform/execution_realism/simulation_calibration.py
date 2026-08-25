"""Calibrate L2 replay against the local paper execution lifecycle."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Sequence

from aats.services.execution_engine.state_machine import (
    ALLOWED_TRANSITIONS,
    TERMINAL_ORDER_STATES,
)


CALIBRATION_MODEL_VERSION = "paper_lifecycle_l2_calibration_v1"


def _decimal(value: Decimal | str | int, *, field_name: str) -> Decimal:
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite():
        raise ValueError(f"{field_name}_must_be_finite")
    return result


def _utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name}_must_be_timezone_aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ObservedStateTransition:
    from_state: str | None
    to_state: str
    created_at: datetime

    def __post_init__(self) -> None:
        if self.to_state not in ALLOWED_TRANSITIONS:
            raise ValueError("observed_transition_to_state_invalid")
        if self.from_state is not None and self.from_state not in ALLOWED_TRANSITIONS:
            raise ValueError("observed_transition_from_state_invalid")
        object.__setattr__(
            self,
            "created_at",
            _utc(self.created_at, field_name="transition_created_at"),
        )


@dataclass(frozen=True, slots=True)
class ObservedCommand:
    command_type: str
    state: str
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.command_type.strip() or not self.state.strip():
            raise ValueError("observed_command_fields_required")
        object.__setattr__(self, "created_at", _utc(self.created_at, field_name="command_created_at"))
        object.__setattr__(self, "updated_at", _utc(self.updated_at, field_name="command_updated_at"))
        if self.updated_at < self.created_at:
            raise ValueError("command_timestamp_regression")


@dataclass(frozen=True, slots=True)
class ObservedFill:
    fill_id: str
    quantity: Decimal
    price: Decimal
    fee_amount: Decimal
    exchange_ts: datetime
    ingestion_ts: datetime

    def __post_init__(self) -> None:
        if not self.fill_id.strip():
            raise ValueError("fill_id_required")
        quantity = _decimal(self.quantity, field_name="fill_quantity")
        price = _decimal(self.price, field_name="fill_price")
        fee = _decimal(self.fee_amount, field_name="fee_amount")
        if quantity <= 0 or price <= 0:
            raise ValueError("fill_quantity_and_price_must_be_positive")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "fee_amount", fee)
        object.__setattr__(self, "exchange_ts", _utc(self.exchange_ts, field_name="exchange_ts"))
        object.__setattr__(self, "ingestion_ts", _utc(self.ingestion_ts, field_name="ingestion_ts"))
        if self.ingestion_ts < self.exchange_ts:
            raise ValueError("fill_ingestion_precedes_exchange_timestamp")


@dataclass(frozen=True, slots=True)
class ObservedPaperOrder:
    order_id: str
    symbol: str
    side: str
    requested_quantity: Decimal
    state: str
    source_system: str
    created_at: datetime
    updated_at: datetime
    transitions: tuple[ObservedStateTransition, ...]
    commands: tuple[ObservedCommand, ...]
    fills: tuple[ObservedFill, ...]

    def __post_init__(self) -> None:
        if not self.order_id.strip() or not self.symbol.strip():
            raise ValueError("observed_order_identity_required")
        if self.side not in {"buy", "sell"}:
            raise ValueError("observed_order_side_invalid")
        if self.state not in ALLOWED_TRANSITIONS:
            raise ValueError("observed_order_state_invalid")
        requested = _decimal(self.requested_quantity, field_name="requested_quantity")
        if requested <= 0:
            raise ValueError("requested_quantity_must_be_positive")
        object.__setattr__(self, "requested_quantity", requested)
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "created_at", _utc(self.created_at, field_name="order_created_at"))
        object.__setattr__(self, "updated_at", _utc(self.updated_at, field_name="order_updated_at"))
        object.__setattr__(self, "transitions", tuple(self.transitions))
        object.__setattr__(self, "commands", tuple(self.commands))
        object.__setattr__(self, "fills", tuple(self.fills))


@dataclass(frozen=True, slots=True)
class PredictedExecution:
    order_id: str
    target_quantity: Decimal
    filled_quantity: Decimal
    average_fill_price: Decimal | None
    fee_bps_weighted: float | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_quantity",
            _decimal(self.target_quantity, field_name="predicted_target_quantity"),
        )
        object.__setattr__(
            self,
            "filled_quantity",
            _decimal(self.filled_quantity, field_name="predicted_filled_quantity"),
        )
        if self.average_fill_price is not None:
            object.__setattr__(
                self,
                "average_fill_price",
                _decimal(self.average_fill_price, field_name="predicted_average_fill_price"),
            )


@dataclass(frozen=True, slots=True)
class ExecutionCalibrationPolicy:
    expected_source_system: str = "paper_local"
    min_matched_orders: int = 20
    max_fill_ratio_mae: float = 0.20
    max_price_error_bps_mean: float = 10.0
    max_fee_error_bps_mean: float = 1.0
    max_command_to_terminal_p95_ms: float = 5_000.0


@dataclass(frozen=True, slots=True)
class OrderCalibrationResult:
    order_id: str
    lifecycle_valid: bool
    lifecycle_reason_codes: tuple[str, ...]
    actual_fill_ratio: float
    predicted_fill_ratio: float
    fill_ratio_absolute_error: float
    price_error_bps: float | None
    fee_error_bps: float | None
    command_to_terminal_ms: float | None


@dataclass(frozen=True, slots=True)
class ExecutionCalibrationReport:
    format_version: int
    model_version: str
    passed: bool
    reason_codes: tuple[str, ...]
    observed_order_count: int
    matched_order_count: int
    lifecycle_valid_ratio: float
    fill_ratio_mae: float
    price_error_bps_mean: float
    fee_error_bps_mean: float
    command_to_terminal_p95_ms: float | None
    l2_execution_evidence_fingerprint: str
    results: tuple[OrderCalibrationResult, ...]
    evidence_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes)
        payload["results"] = [asdict(result) for result in self.results]
        for row in payload["results"]:
            row["lifecycle_reason_codes"] = list(row["lifecycle_reason_codes"])
        return payload


def _percentile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _lifecycle_reasons(order: ObservedPaperOrder, policy: ExecutionCalibrationPolicy) -> tuple[str, ...]:
    reasons: set[str] = set()
    if order.source_system != policy.expected_source_system:
        reasons.add("source_system_not_expected_paper_mode")
    transitions = sorted(order.transitions, key=lambda item: item.created_at)
    for left, right in zip(transitions, transitions[1:]):
        if right.created_at < left.created_at:
            reasons.add("transition_timestamp_regression")
        if right.from_state is not None and right.from_state != left.to_state:
            reasons.add("transition_chain_disconnected")
    for transition in transitions:
        if transition.from_state is not None and transition.to_state not in ALLOWED_TRANSITIONS[
            transition.from_state
        ]:
            reasons.add("invalid_state_transition")
    if transitions and transitions[-1].to_state != order.state:
        reasons.add("terminal_state_readback_mismatch")
    filled_quantity = sum((fill.quantity for fill in order.fills), Decimal(0))
    if filled_quantity > order.requested_quantity:
        reasons.add("actual_fill_quantity_exceeds_requested")
    if order.state == "FILLED" and filled_quantity != order.requested_quantity:
        reasons.add("filled_state_quantity_mismatch")
    if order.state == "PARTIALLY_FILLED" and not Decimal(0) < filled_quantity < order.requested_quantity:
        reasons.add("partial_state_quantity_mismatch")
    if order.state not in {"CREATED", "BLOCKED", "DRY_RUN"}:
        submit_commands = [
            command for command in order.commands if command.command_type.upper() == "SUBMIT"
        ]
        if not submit_commands:
            reasons.add("submit_command_missing")
        elif not any(command.state.upper() == "ACKED" for command in submit_commands):
            reasons.add("submit_command_not_acked")
    return tuple(sorted(reasons))


def _actual_metrics(order: ObservedPaperOrder) -> tuple[float, Decimal | None, float | None]:
    quantity = sum((fill.quantity for fill in order.fills), Decimal(0))
    fill_ratio = float(quantity / order.requested_quantity)
    if quantity <= 0:
        return fill_ratio, None, None
    notional = sum((fill.quantity * fill.price for fill in order.fills), Decimal(0))
    average_price = notional / quantity
    total_fee = sum((fill.fee_amount for fill in order.fills), Decimal(0))
    fee_bps = float(total_fee / notional * Decimal(10_000))
    return fill_ratio, average_price, fee_bps


def _command_to_terminal_ms(order: ObservedPaperOrder) -> float | None:
    if order.state not in TERMINAL_ORDER_STATES:
        return None
    submit_commands = [
        command for command in order.commands if command.command_type.upper() == "SUBMIT"
    ]
    if not submit_commands:
        return None
    started = min(command.created_at for command in submit_commands)
    terminal_times = [
        transition.created_at
        for transition in order.transitions
        if transition.to_state in TERMINAL_ORDER_STATES
    ]
    if not terminal_times:
        return None
    return max(0.0, (min(terminal_times) - started).total_seconds() * 1_000.0)


def calibrate_l2_against_paper_lifecycle(
    *,
    observed_orders: Sequence[ObservedPaperOrder],
    predicted_executions: Sequence[PredictedExecution],
    l2_execution_evidence_fingerprint: str,
    policy: ExecutionCalibrationPolicy | None = None,
) -> ExecutionCalibrationReport:
    if not l2_execution_evidence_fingerprint.startswith("l2_"):
        raise ValueError("l2_execution_evidence_fingerprint_invalid")
    if not observed_orders:
        raise ValueError("observed_orders_required")
    selected_policy = policy or ExecutionCalibrationPolicy()
    predicted_by_id = {item.order_id: item for item in predicted_executions}
    if len(predicted_by_id) != len(predicted_executions):
        raise ValueError("duplicate_predicted_order_id")
    results: list[OrderCalibrationResult] = []
    unmatched = 0
    for order in sorted(observed_orders, key=lambda item: (item.created_at, item.order_id)):
        predicted = predicted_by_id.get(order.order_id)
        if predicted is None:
            unmatched += 1
            continue
        lifecycle_reasons = _lifecycle_reasons(order, selected_policy)
        actual_ratio, actual_price, actual_fee_bps = _actual_metrics(order)
        predicted_ratio = float(predicted.filled_quantity / predicted.target_quantity)
        if actual_price is not None and predicted.average_fill_price is not None:
            price_error_bps = float(
                abs(actual_price - predicted.average_fill_price)
                / actual_price
                * Decimal(10_000)
            )
        else:
            price_error_bps = None
        fee_error = (
            abs(actual_fee_bps - predicted.fee_bps_weighted)
            if actual_fee_bps is not None and predicted.fee_bps_weighted is not None
            else None
        )
        results.append(
            OrderCalibrationResult(
                order_id=order.order_id,
                lifecycle_valid=not lifecycle_reasons,
                lifecycle_reason_codes=lifecycle_reasons,
                actual_fill_ratio=actual_ratio,
                predicted_fill_ratio=predicted_ratio,
                fill_ratio_absolute_error=abs(actual_ratio - predicted_ratio),
                price_error_bps=price_error_bps,
                fee_error_bps=fee_error,
                command_to_terminal_ms=_command_to_terminal_ms(order),
            )
        )
    matched = len(results)
    lifecycle_ratio = sum(result.lifecycle_valid for result in results) / matched if matched else 0.0
    fill_mae = statistics.fmean(result.fill_ratio_absolute_error for result in results) if results else math.inf
    price_errors = [result.price_error_bps for result in results if result.price_error_bps is not None]
    fee_errors = [result.fee_error_bps for result in results if result.fee_error_bps is not None]
    latencies = [
        result.command_to_terminal_ms
        for result in results
        if result.command_to_terminal_ms is not None
    ]
    price_mean = statistics.fmean(price_errors) if price_errors else math.inf
    fee_mean = statistics.fmean(fee_errors) if fee_errors else math.inf
    latency_p95 = _percentile(latencies, 0.95)
    reasons: set[str] = set()
    if matched < selected_policy.min_matched_orders:
        reasons.add("matched_order_count_below_minimum")
    if unmatched:
        reasons.add("observed_orders_missing_l2_prediction")
    if lifecycle_ratio < 1.0:
        reasons.add("paper_lifecycle_integrity_failed")
    if fill_mae > selected_policy.max_fill_ratio_mae:
        reasons.add("fill_ratio_mae_above_maximum")
    if price_mean > selected_policy.max_price_error_bps_mean:
        reasons.add("price_error_bps_above_maximum")
    if fee_mean > selected_policy.max_fee_error_bps_mean:
        reasons.add("fee_error_bps_above_maximum")
    if latency_p95 is None:
        reasons.add("terminal_latency_missing")
    elif latency_p95 > selected_policy.max_command_to_terminal_p95_ms:
        reasons.add("command_to_terminal_p95_above_maximum")
    ordered_reasons = tuple(sorted(reasons))
    fingerprint_payload = {
        "model_version": CALIBRATION_MODEL_VERSION,
        "policy": asdict(selected_policy),
        "l2_execution_evidence_fingerprint": l2_execution_evidence_fingerprint,
        "results": [asdict(result) for result in results],
        "reason_codes": ordered_reasons,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return ExecutionCalibrationReport(
        format_version=1,
        model_version=CALIBRATION_MODEL_VERSION,
        passed=not ordered_reasons,
        reason_codes=ordered_reasons,
        observed_order_count=len(observed_orders),
        matched_order_count=matched,
        lifecycle_valid_ratio=lifecycle_ratio,
        fill_ratio_mae=fill_mae,
        price_error_bps_mean=price_mean,
        fee_error_bps_mean=fee_mean,
        command_to_terminal_p95_ms=latency_p95,
        l2_execution_evidence_fingerprint=l2_execution_evidence_fingerprint,
        results=tuple(results),
        evidence_fingerprint=f"cal_{fingerprint}",
    )
