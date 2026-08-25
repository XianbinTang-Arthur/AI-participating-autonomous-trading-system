from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from aats.data_platform.governance.parameter_activation import (
    ParameterActivationOperation,
    ParameterRuntimeAck,
    activation_evidence,
    advance_activation,
    make_operation_id,
    parameter_payload_sha256,
)


NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)


def _operation(state: str = "pending") -> ParameterActivationOperation:
    sha = parameter_payload_sha256({"edge_bps": 12.5})
    return ParameterActivationOperation(
        operation_id=make_operation_id(
            scope="combo",
            scope_ref="trend_following:15m",
            generation="generation-42",
            payload_sha256=sha,
        ),
        operation_type="apply",
        scope="combo",
        scope_ref="trend_following:15m",
        generation="generation-42",
        from_parameter_set_id="ps-old",
        to_parameter_set_id="ps-new",
        payload_sha256=sha,
        state=state,
        expected_process_roles=("execution", "strategy"),
        actor="operator-b",
        reason="approved candidate promotion",
        deadline_at=NOW + timedelta(minutes=5),
        created_at=NOW,
        updated_at=NOW,
        terminal_at=NOW if state in {"succeeded", "failed", "rolled_back"} else None,
    )


def _ack(
    op: ParameterActivationOperation,
    role: str,
    phase: str,
    *,
    status: str = "accepted",
    generation: str | None = None,
    sha: str | None = None,
    observed: str | None = None,
) -> ParameterRuntimeAck:
    return ParameterRuntimeAck(
        operation_id=op.operation_id,
        process_role=role,
        phase=phase,
        generation=generation or op.generation,
        payload_sha256=sha or op.payload_sha256,
        ack_status=status,
        observed_parameter_set_id=observed,
        ack_at=NOW,
    )


def test_full_prepare_commit_readback_lifecycle_requires_every_role() -> None:
    pending = _operation()
    assert advance_activation(pending, [], now=NOW).next_state == "preparing"

    preparing = replace(pending, state="preparing")
    prepare_acks = [_ack(preparing, role, "prepare") for role in preparing.expected_process_roles]
    assert advance_activation(preparing, prepare_acks, now=NOW).next_state == "prepared"

    prepared = replace(preparing, state="prepared")
    assert advance_activation(prepared, prepare_acks, now=NOW).next_state == "committing"

    committing = replace(prepared, state="committing")
    all_acks = prepare_acks + [
        _ack(committing, role, phase, observed="ps-new")
        for phase in ("commit", "readback")
        for role in committing.expected_process_roles
    ]
    result = advance_activation(committing, all_acks, now=NOW)
    assert result.next_state == "succeeded"
    assert result.terminal is True


def test_partial_prepare_waits_without_claiming_success() -> None:
    op = _operation("preparing")
    result = advance_activation(op, [_ack(op, "execution", "prepare")], now=NOW)
    assert result.next_state == "preparing"
    assert "ack_missing:prepare:strategy" in result.reason_codes


@pytest.mark.parametrize("defect", ["generation", "payload", "rejected"])
def test_prepare_mismatch_fails_closed(defect: str) -> None:
    op = _operation("preparing")
    bad = {
        "generation": _ack(op, "strategy", "prepare", generation="stale"),
        "payload": _ack(op, "strategy", "prepare", sha="0" * 64),
        "rejected": _ack(op, "strategy", "prepare", status="rejected"),
    }[defect]
    result = advance_activation(
        op,
        [_ack(op, "execution", "prepare"), bad],
        now=NOW,
    )
    assert result.next_state == "failed"
    assert result.terminal is True


def test_commit_readback_mismatch_requires_rollback() -> None:
    op = _operation("committing")
    acks = [
        _ack(op, role, "commit", observed="ps-new")
        for role in op.expected_process_roles
    ] + [
        _ack(op, role, "readback", observed="ps-old")
        for role in op.expected_process_roles
    ]
    result = advance_activation(op, acks, now=NOW)
    assert result.next_state == "rollback_required"
    assert any(code.startswith("readback_parameter_set_mismatch") for code in result.reason_codes)


def test_deadline_during_commit_requires_rollback() -> None:
    op = _operation("committing")
    result = advance_activation(op, [], now=op.deadline_at + timedelta(seconds=1))
    assert result.next_state == "rollback_required"


def test_rollback_needs_exact_previous_parameter_readback() -> None:
    op = _operation("rolling_back")
    acks = [
        _ack(op, role, "rollback", observed="ps-old")
        for role in op.expected_process_roles
    ]
    assert advance_activation(op, acks, now=NOW).next_state == "rolled_back"


def test_evidence_redacts_error_details_and_is_deterministic() -> None:
    op = _operation("failed")
    ack = replace(
        _ack(op, "execution", "prepare", status="rejected"),
        error_message="ConnectionError:postgresql://secret@host/db",
    )
    first = activation_evidence(op, [ack], evaluated_at=NOW)
    second = activation_evidence(op, [ack], evaluated_at=NOW + timedelta(seconds=1))
    assert first["acks"][0]["error_type"] == "ConnectionError"
    assert "secret" not in str(first)
    assert first["evidence_fingerprint"] == second["evidence_fingerprint"]


def test_non_hex_payload_fingerprint_is_rejected() -> None:
    with pytest.raises(ValueError, match="payload_sha256_must_be_sha256"):
        replace(_operation(), payload_sha256="g" * 64)
