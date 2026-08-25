from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aats.data_platform.operations.execution_funnel import (
    SimulationDeploymentIdentity,
    evaluate_simulation_execution_funnel,
    parse_simulation_deployment_identity,
)
from aats.events import topics


START = datetime(2026, 8, 25, 18, 0, tzinfo=UTC)
END = START + timedelta(minutes=20)


def _deployment() -> SimulationDeploymentIdentity:
    return SimulationDeploymentIdentity(
        profile="derivatives",
        deployed_commit="a" * 40,
        runtime_readiness_generation="a" * 12 + "-20260825T180000Z-1-2",
        generated_at=START,
        deployment_evidence_fingerprint="b" * 64,
    )


def _event(
    *,
    topic: str,
    decision_id: str,
    created_at: datetime,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "event_id": f"{topic}-{decision_id}",
        "created_at": created_at,
        "topic": topic,
        "decision_id": decision_id,
        "symbol": "BTC-USDT-SWAP",
        "product_type": "derivatives",
        "margin_mode": "cross",
        "payload": payload or {},
    }


def _complete_decision(
    index: int,
    *,
    target_notional: str = "1000",
    risk_approved: bool = True,
    rejection_reasons: list[str] | None = None,
    include_plan: bool = True,
    include_intent: bool = True,
) -> tuple[list[dict[str, object]], dict[str, object] | None]:
    decision_id = f"decision-{index:03d}"
    created_at = START + timedelta(seconds=index)
    rows = [
        _event(
            topic=topics.PORTFOLIO_ALLOCATION_DECISIONS,
            decision_id=decision_id,
            created_at=created_at,
        ),
        _event(
            topic=topics.POSITION_TARGETS,
            decision_id=decision_id,
            created_at=created_at,
            payload={
                "current_position_qty": "0",
                "target_position_qty": "0.01",
                "delta_position_qty": "0.01",
                "target_notional": target_notional,
            },
        ),
        _event(
            topic=topics.POLICY_DECISIONS,
            decision_id=decision_id,
            created_at=created_at,
        ),
        _event(
            topic=topics.RISK_DECISIONS,
            decision_id=decision_id,
            created_at=created_at,
            payload={
                "approved": risk_approved,
                "rejection_reasons": rejection_reasons or [],
            },
        ),
    ]
    if include_plan:
        rows.append(
            _event(
                topic=topics.EXECUTION_PLANS,
                decision_id=decision_id,
                created_at=created_at,
            )
        )
    if include_intent:
        rows.append(
            _event(
                topic=topics.ORDER_INTENTS,
                decision_id=decision_id,
                created_at=created_at,
            )
        )
    order = (
        {
            "order_id": f"order-{index:03d}",
            "decision_id": decision_id,
            "state": "FILLED",
            "source_system": "local_order_manager",
            "created_at": created_at,
            "updated_at": created_at + timedelta(seconds=1),
        }
        if risk_approved
        else None
    )
    return rows, order


def _evaluate(
    *,
    event_rows: list[dict[str, object]],
    order_rows: list[dict[str, object]] | None = None,
    fill_rows: list[dict[str, object]] | None = None,
    minimum: int = 100,
):
    return evaluate_simulation_execution_funnel(
        deployment=_deployment(),
        window_end=END,
        symbol="BTC-USDT-SWAP",
        max_new_risk_notional=Decimal("1250"),
        min_nonzero_targets=minimum,
        settle_delay_seconds=30,
        event_rows=event_rows,
        order_rows=order_rows or [],
        fill_rows=fill_rows or [],
        evaluated_at=END,
    )


def test_no_nonzero_target_is_unknown_not_pass() -> None:
    evidence = _evaluate(event_rows=[], minimum=1)

    assert evidence.status == "UNKNOWN"
    assert evidence.passed is False
    assert evidence.mature_nonzero_target_count == 0
    assert "nonzero_target_observation_missing" in evidence.reason_codes
    assert evidence.production_ready is False
    assert evidence.trading_ready is False


def test_one_hundred_complete_natural_targets_pass_funnel_gate() -> None:
    events: list[dict[str, object]] = []
    orders: list[dict[str, object]] = []
    for index in range(100):
        rows, order = _complete_decision(index)
        events.extend(rows)
        assert order is not None
        orders.append(order)

    evidence = _evaluate(event_rows=events, order_rows=orders)

    assert evidence.status == "PASS"
    assert evidence.passed is True
    assert evidence.mature_nonzero_target_count == 100
    assert evidence.oversized_new_risk_target_count == 0
    assert evidence.sizing_rejection_count == 0
    assert evidence.reason_codes == ()
    assert evidence.evidence_fingerprint.startswith("funnel_")


def test_oversized_target_and_sizing_rejection_fail() -> None:
    rows, _ = _complete_decision(
        1,
        target_notional="50000",
        risk_approved=False,
        rejection_reasons=["max_pending_notional_per_symbol_exceeded"],
        include_plan=False,
        include_intent=False,
    )

    evidence = _evaluate(event_rows=rows, minimum=1)

    assert evidence.status == "FAIL"
    assert evidence.oversized_new_risk_target_count == 1
    assert evidence.sizing_rejection_count == 1
    assert "new_risk_target_notional_above_cap" in evidence.reason_codes
    assert "sizing_risk_rejection_observed" in evidence.reason_codes


def test_sub_microunit_notional_quantization_does_not_exceed_cap() -> None:
    rows, order = _complete_decision(
        1,
        target_notional="1250.00000002",
    )
    assert order is not None

    evidence = _evaluate(event_rows=rows, order_rows=[order], minimum=1)

    assert evidence.status == "PASS"
    assert evidence.oversized_new_risk_target_count == 0


def test_notional_above_quantization_tolerance_exceeds_cap() -> None:
    rows, order = _complete_decision(
        1,
        target_notional="1250.000002",
    )
    assert order is not None

    evidence = _evaluate(event_rows=rows, order_rows=[order], minimum=1)

    assert evidence.status == "FAIL"
    assert evidence.oversized_new_risk_target_count == 1
    assert "new_risk_target_notional_above_cap" in evidence.reason_codes


def test_approved_risk_requires_plan_intent_and_order() -> None:
    rows, _ = _complete_decision(
        1,
        include_plan=False,
        include_intent=False,
    )

    evidence = _evaluate(event_rows=rows, minimum=1)

    assert evidence.status == "FAIL"
    assert "approved_risk_plan_stage_missing" in evidence.reason_codes
    assert "approved_risk_intent_stage_missing" in evidence.reason_codes
    assert "approved_risk_order_stage_missing" in evidence.reason_codes


def test_order_after_risk_rejection_fails() -> None:
    rows, _ = _complete_decision(
        1,
        risk_approved=False,
        rejection_reasons=["market_data_stale"],
        include_plan=False,
        include_intent=False,
    )
    order = {
        "order_id": "order-rejected",
        "decision_id": "decision-001",
        "state": "CREATED",
        "source_system": "local_order_manager",
        "created_at": START + timedelta(seconds=1),
        "updated_at": START + timedelta(seconds=1),
    }

    evidence = _evaluate(event_rows=rows, order_rows=[order], minimum=1)

    assert evidence.status == "FAIL"
    assert "order_observed_after_risk_rejection" in evidence.reason_codes


def test_immature_nonzero_target_is_not_counted_as_complete() -> None:
    rows, order = _complete_decision(1)
    recent = END - timedelta(seconds=5)
    for row in rows:
        row["created_at"] = recent
    assert order is not None
    order["created_at"] = recent

    evidence = _evaluate(event_rows=rows, order_rows=[order], minimum=1)

    assert evidence.status == "UNKNOWN"
    assert evidence.mature_nonzero_target_count == 0
    assert evidence.immature_nonzero_target_count == 1


def test_duplicate_target_does_not_inflate_observation_count() -> None:
    rows, order = _complete_decision(1)
    duplicate = next(row.copy() for row in rows if row["topic"] == topics.POSITION_TARGETS)
    duplicate["event_id"] = "duplicate-position-target"
    rows.append(duplicate)
    assert order is not None

    evidence = _evaluate(event_rows=rows, order_rows=[order], minimum=1)

    assert evidence.status == "FAIL"
    assert evidence.mature_nonzero_target_count == 1
    assert "duplicate_nonzero_target_for_decision" in evidence.reason_codes


def test_deployment_identity_accepts_only_non_production_derivatives_packet() -> None:
    payload = {
        "profile": "derivatives",
        "status": "simulation_stack_healthy",
        "production_ready": False,
        "trading_ready": False,
        "deployed_commit": "a" * 40,
        "runtime_readiness_generation": "generation-1",
        "generated_at": START.isoformat(),
    }

    identity = parse_simulation_deployment_identity(
        payload,
        evidence_fingerprint="b" * 64,
    )

    assert identity.profile == "derivatives"
    assert identity.generated_at == START

    with pytest.raises(ValueError, match="deployment_profile_must_be_derivatives"):
        parse_simulation_deployment_identity(
            {**payload, "profile": "derivatives_live"},
            evidence_fingerprint="b" * 64,
        )
    with pytest.raises(ValueError, match="deployment_production_ready_must_be_false"):
        parse_simulation_deployment_identity(
            {**payload, "production_ready": True},
            evidence_fingerprint="b" * 64,
        )
