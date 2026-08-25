from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aats.data_platform.execution_realism.simulation_calibration import (
    ExecutionCalibrationPolicy,
    ObservedCommand,
    ObservedFill,
    ObservedPaperOrder,
    ObservedStateTransition,
    PredictedExecution,
    calibrate_l2_against_paper_lifecycle,
)


START = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _observed(order_id: str = "order-1", *, source_system: str = "paper_local") -> ObservedPaperOrder:
    return ObservedPaperOrder(
        order_id=order_id,
        symbol="BTC-USDT-SWAP",
        side="buy",
        requested_quantity=Decimal("2"),
        state="FILLED",
        source_system=source_system,
        created_at=START,
        updated_at=START + timedelta(seconds=1),
        transitions=(
            ObservedStateTransition(None, "CREATED", START),
            ObservedStateTransition("CREATED", "SUBMITTING", START + timedelta(milliseconds=10)),
            ObservedStateTransition(
                "SUBMITTING", "SUBMITTED", START + timedelta(milliseconds=100)
            ),
            ObservedStateTransition("SUBMITTED", "FILLED", START + timedelta(seconds=1)),
        ),
        commands=(
            ObservedCommand(
                "SUBMIT",
                "ACKED",
                START + timedelta(milliseconds=10),
                START + timedelta(milliseconds=100),
            ),
        ),
        fills=(
            ObservedFill(
                fill_id=f"fill-{order_id}",
                quantity=Decimal("2"),
                price=Decimal("101"),
                fee_amount=Decimal("0.101"),
                exchange_ts=START + timedelta(milliseconds=900),
                ingestion_ts=START + timedelta(milliseconds=950),
            ),
        ),
    )


def _predicted(order_id: str = "order-1") -> PredictedExecution:
    return PredictedExecution(
        order_id=order_id,
        target_quantity=Decimal("2"),
        filled_quantity=Decimal("2"),
        average_fill_price=Decimal("101"),
        fee_bps_weighted=5.0,
    )


def _policy() -> ExecutionCalibrationPolicy:
    return ExecutionCalibrationPolicy(
        min_matched_orders=1,
        max_fill_ratio_mae=0.01,
        max_price_error_bps_mean=0.01,
        max_fee_error_bps_mean=0.01,
        max_command_to_terminal_p95_ms=2_000,
    )


def test_calibration_passes_only_with_matching_lifecycle_and_costs() -> None:
    report = calibrate_l2_against_paper_lifecycle(
        observed_orders=[_observed()],
        predicted_executions=[_predicted()],
        l2_execution_evidence_fingerprint="l2_" + "a" * 64,
        policy=_policy(),
    )
    assert report.passed is True
    assert report.lifecycle_valid_ratio == 1.0
    assert report.fill_ratio_mae == 0.0
    assert report.price_error_bps_mean == 0.0
    assert report.fee_error_bps_mean == 0.0


def test_calibration_rejects_non_paper_source_even_if_numbers_match() -> None:
    report = calibrate_l2_against_paper_lifecycle(
        observed_orders=[_observed(source_system="okx_live")],
        predicted_executions=[_predicted()],
        l2_execution_evidence_fingerprint="l2_" + "a" * 64,
        policy=_policy(),
    )
    assert report.passed is False
    assert "paper_lifecycle_integrity_failed" in report.reason_codes
    assert "source_system_not_expected_paper_mode" in report.results[0].lifecycle_reason_codes


def test_calibration_rejects_disconnected_state_history_and_fill_mismatch() -> None:
    observed = _observed()
    broken = ObservedPaperOrder(
        order_id=observed.order_id,
        symbol=observed.symbol,
        side=observed.side,
        requested_quantity=observed.requested_quantity,
        state="FILLED",
        source_system=observed.source_system,
        created_at=observed.created_at,
        updated_at=observed.updated_at,
        transitions=(
            ObservedStateTransition(None, "CREATED", START),
            ObservedStateTransition("SUBMITTED", "FILLED", START + timedelta(seconds=1)),
        ),
        commands=observed.commands,
        fills=(
            ObservedFill(
                "fill-short",
                Decimal("1"),
                Decimal("101"),
                Decimal("0.0505"),
                START + timedelta(milliseconds=900),
                START + timedelta(milliseconds=950),
            ),
        ),
    )
    report = calibrate_l2_against_paper_lifecycle(
        observed_orders=[broken],
        predicted_executions=[_predicted()],
        l2_execution_evidence_fingerprint="l2_" + "a" * 64,
        policy=_policy(),
    )
    assert report.passed is False
    assert "transition_chain_disconnected" in report.results[0].lifecycle_reason_codes
    assert "filled_state_quantity_mismatch" in report.results[0].lifecycle_reason_codes
    assert "fill_ratio_mae_above_maximum" in report.reason_codes


def test_unmatched_observed_order_fails_closed() -> None:
    report = calibrate_l2_against_paper_lifecycle(
        observed_orders=[_observed()],
        predicted_executions=[],
        l2_execution_evidence_fingerprint="l2_" + "a" * 64,
        policy=_policy(),
    )
    assert report.passed is False
    assert "observed_orders_missing_l2_prediction" in report.reason_codes
    assert "matched_order_count_below_minimum" in report.reason_codes
