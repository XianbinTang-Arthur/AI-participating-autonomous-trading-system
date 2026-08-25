from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aats.data_platform.operations.fault_matrix import (
    REQUIRED_CHECKS,
    REQUIRED_FAULT_CASES,
    FaultCaseObservation,
    FaultCheck,
    evaluate_fault_matrix,
)


NOW = datetime(2026, 8, 25, 13, tzinfo=UTC)


def _case(name: str, *, failed_check: str | None = None) -> FaultCaseObservation:
    return FaultCaseObservation(
        case_name=name,
        isolated_profile="derivatives",
        checks=tuple(
            FaultCheck(
                name=check,
                status="FAIL" if check == failed_check else "PASS",
                evidence_ref=f"evidence/{name}/{check}.json",
                observed_at=NOW,
            )
            for check in REQUIRED_CHECKS
        ),
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=30),
    )


def test_complete_fault_matrix_passes_and_is_deterministic() -> None:
    observations = [_case(name) for name in REQUIRED_FAULT_CASES]
    first = evaluate_fault_matrix(observations, evaluated_at=NOW + timedelta(seconds=30))
    second = evaluate_fault_matrix(observations, evaluated_at=NOW + timedelta(hours=1))
    assert first.passed is True
    assert first.reason_codes == ()
    assert first.evidence_fingerprint == second.evidence_fingerprint


def test_missing_case_fails_closed() -> None:
    evidence = evaluate_fault_matrix(
        [_case(REQUIRED_FAULT_CASES[0])],
        evaluated_at=NOW + timedelta(seconds=30),
    )
    assert evidence.passed is False
    assert "case_missing:nats_disconnect" in evidence.reason_codes


def test_unknown_or_failed_check_fails_case_and_matrix() -> None:
    observations = [_case(name) for name in REQUIRED_FAULT_CASES]
    observations[1] = _case("nats_disconnect", failed_check="new_risk_blocked")
    evidence = evaluate_fault_matrix(observations, evaluated_at=NOW + timedelta(seconds=30))
    assert evidence.passed is False
    nats = next(case for case in evidence.cases if case.case_name == "nats_disconnect")
    assert "check_fail:new_risk_blocked" in nats.reason_codes


def test_live_profile_fault_drill_is_rejected() -> None:
    case = _case("redis_disconnect")
    with pytest.raises(ValueError, match="simulation_only"):
        FaultCaseObservation(
            case_name=case.case_name,
            isolated_profile="derivatives-live",
            checks=case.checks,
            started_at=case.started_at,
            completed_at=case.completed_at,
        )


def test_passing_check_requires_evidence_reference() -> None:
    with pytest.raises(ValueError, match="requires_evidence_ref"):
        FaultCheck(name="cleanup_verified", status="PASS", evidence_ref=None)


def test_future_or_stale_fault_case_fails_closed() -> None:
    observations = [_case(name) for name in REQUIRED_FAULT_CASES]
    future = evaluate_fault_matrix(observations, evaluated_at=NOW)
    stale = evaluate_fault_matrix(
        observations,
        evaluated_at=NOW + timedelta(days=1, seconds=31),
    )
    assert future.passed is False
    assert "case_completed_in_future:redis_disconnect" in future.reason_codes
    assert stale.passed is False
    assert "case_stale:redis_disconnect" in stale.reason_codes
