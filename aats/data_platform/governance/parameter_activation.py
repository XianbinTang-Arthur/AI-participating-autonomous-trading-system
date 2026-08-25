"""Fail-closed parameter activation generation state machine.

The governance database is the audit authority, but an operation is never
successful merely because its row was updated.  Every expected runtime role
must acknowledge the exact generation and payload fingerprint during prepare,
commit and readback.  This module deliberately does not submit orders, restart
processes or grant a live-trading permission.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping, Sequence


NONTERMINAL_STATES = frozenset(
    {
        "pending",
        "preparing",
        "prepared",
        "committing",
        "rollback_required",
        "rolling_back",
    }
)
TERMINAL_STATES = frozenset({"succeeded", "failed", "rolled_back"})
ACK_PHASES = frozenset({"prepare", "commit", "readback", "rollback"})
ACK_STATUSES = frozenset({"accepted", "rejected", "mismatch", "timeout"})
_SHA256_HEX_LENGTH = 64


def _is_sha256_hex(value: str) -> bool:
    return len(value) == _SHA256_HEX_LENGTH and all(
        character in "0123456789abcdef" for character in value
    )


class ParameterActivationError(RuntimeError):
    """Base activation protocol error."""


class InvalidActivationTransition(ParameterActivationError):
    """Raised when a caller tries to skip a required protocol state."""


@dataclass(frozen=True, slots=True)
class ParameterActivationOperation:
    operation_id: str
    operation_type: str
    scope: str
    scope_ref: str
    generation: str
    from_parameter_set_id: str | None
    to_parameter_set_id: str | None
    payload_sha256: str
    state: str
    expected_process_roles: tuple[str, ...]
    actor: str
    reason: str
    deadline_at: datetime
    created_at: datetime
    updated_at: datetime
    error_message: str | None = None
    terminal_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "operation_id",
            "scope",
            "scope_ref",
            "generation",
            "actor",
            "reason",
        ):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name}_required")
        if self.operation_type not in {"apply", "rollback"}:
            raise ValueError("invalid_operation_type")
        if self.state not in NONTERMINAL_STATES | TERMINAL_STATES:
            raise ValueError("invalid_activation_state")
        if not _is_sha256_hex(self.payload_sha256):
            raise ValueError("payload_sha256_must_be_sha256")
        if not (self.to_parameter_set_id or "").strip():
            raise ValueError("to_parameter_set_id_required")
        roles = tuple(sorted({role.strip() for role in self.expected_process_roles if role.strip()}))
        if not roles:
            raise ValueError("expected_process_roles_required")
        object.__setattr__(self, "expected_process_roles", roles)
        for field_name in ("deadline_at", "created_at", "updated_at"):
            value = getattr(self, field_name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field_name}_must_be_timezone_aware")
        if self.deadline_at <= self.created_at:
            raise ValueError("deadline_must_be_after_created_at")
        if self.terminal_at is not None and (
            self.terminal_at.tzinfo is None or self.terminal_at.utcoffset() is None
        ):
            raise ValueError("terminal_at_must_be_timezone_aware")
        if (self.state in TERMINAL_STATES) != (self.terminal_at is not None):
            raise ValueError("terminal_state_timestamp_mismatch")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["expected_process_roles"] = list(self.expected_process_roles)
        for field_name in ("deadline_at", "created_at", "updated_at", "terminal_at"):
            value = payload[field_name]
            if isinstance(value, datetime):
                payload[field_name] = value.isoformat()
        return payload


@dataclass(frozen=True, slots=True)
class ParameterRuntimeAck:
    operation_id: str
    process_role: str
    phase: str
    generation: str
    payload_sha256: str
    ack_status: str
    observed_parameter_set_id: str | None
    ack_at: datetime
    details: Mapping[str, Any] | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if (
            not self.operation_id.strip()
            or not self.process_role.strip()
            or not self.generation.strip()
        ):
            raise ValueError("operation_id_and_process_role_required")
        if self.phase not in ACK_PHASES:
            raise ValueError("invalid_ack_phase")
        if self.ack_status not in ACK_STATUSES:
            raise ValueError("invalid_ack_status")
        if not _is_sha256_hex(self.payload_sha256):
            raise ValueError("payload_sha256_must_be_sha256")
        if self.ack_at.tzinfo is None or self.ack_at.utcoffset() is None:
            raise ValueError("ack_at_must_be_timezone_aware")
        if self.details is not None:
            object.__setattr__(self, "details", dict(self.details))


@dataclass(frozen=True, slots=True)
class ActivationAdvance:
    previous_state: str
    next_state: str
    reason_codes: tuple[str, ...]
    terminal: bool


def parameter_payload_sha256(payload: Mapping[str, Any]) -> str:
    """Return the canonical fingerprint workers must read back."""

    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def make_operation_id(
    *, scope: str, scope_ref: str, generation: str, payload_sha256: str
) -> str:
    """Produce a stable idempotency key without embedding parameter values."""

    material = "\x00".join((scope, scope_ref, generation, payload_sha256)).encode()
    return f"pa-{hashlib.sha256(material).hexdigest()[:32]}"


def _phase_acks(
    operation: ParameterActivationOperation,
    acks: Iterable[ParameterRuntimeAck],
    *,
    phase: str,
) -> dict[str, ParameterRuntimeAck]:
    selected: dict[str, ParameterRuntimeAck] = {}
    for ack in acks:
        if ack.operation_id != operation.operation_id or ack.phase != phase:
            continue
        if ack.process_role in selected:
            raise ParameterActivationError(
                f"duplicate_runtime_ack:{phase}:{ack.process_role}"
            )
        selected[ack.process_role] = ack
    return selected


def _validate_phase(
    operation: ParameterActivationOperation,
    acks: Sequence[ParameterRuntimeAck],
    *,
    phase: str,
    expected_parameter_set_id: str | None = None,
) -> tuple[bool, tuple[str, ...]]:
    by_role = _phase_acks(operation, acks, phase=phase)
    reasons: set[str] = set()
    unknown_roles = sorted(set(by_role) - set(operation.expected_process_roles))
    reasons.update(f"unexpected_role:{role}" for role in unknown_roles)
    for role in operation.expected_process_roles:
        ack = by_role.get(role)
        if ack is None:
            reasons.add(f"ack_missing:{phase}:{role}")
            continue
        if ack.generation != operation.generation:
            reasons.add(f"generation_mismatch:{phase}:{role}")
        if ack.payload_sha256 != operation.payload_sha256:
            reasons.add(f"payload_mismatch:{phase}:{role}")
        if ack.ack_status != "accepted":
            reasons.add(f"ack_not_accepted:{phase}:{role}:{ack.ack_status}")
        if (
            expected_parameter_set_id is not None
            and ack.observed_parameter_set_id != expected_parameter_set_id
        ):
            reasons.add(f"readback_parameter_set_mismatch:{phase}:{role}")
    ordered = tuple(sorted(reasons))
    return not ordered, ordered


def advance_activation(
    operation: ParameterActivationOperation,
    acks: Sequence[ParameterRuntimeAck],
    *,
    now: datetime | None = None,
) -> ActivationAdvance:
    """Evaluate one deterministic state transition from persisted evidence.

    Callers persist the returned state with optimistic locking or ``FOR UPDATE``.
    The explicit ``prepared`` and ``committing`` boundaries ensure the process
    that owns runtime activation decides when to perform the actual commit.
    """

    checked_at = now or datetime.now(UTC)
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise ValueError("now_must_be_timezone_aware")
    state = operation.state
    if state in TERMINAL_STATES:
        return ActivationAdvance(state, state, ("terminal_state",), True)
    if checked_at > operation.deadline_at:
        next_state = "rollback_required" if state == "committing" else "failed"
        return ActivationAdvance(state, next_state, ("operation_deadline_exceeded",), next_state == "failed")
    if state == "pending":
        return ActivationAdvance(state, "preparing", (), False)
    if state == "preparing":
        passed, reasons = _validate_phase(operation, acks, phase="prepare")
        if passed:
            return ActivationAdvance(state, "prepared", (), False)
        if any(
            reason.startswith(("generation_mismatch", "payload_mismatch", "ack_not_accepted", "unexpected_role"))
            for reason in reasons
        ):
            return ActivationAdvance(state, "failed", reasons, True)
        return ActivationAdvance(state, state, reasons, False)
    if state == "prepared":
        return ActivationAdvance(state, "committing", (), False)
    if state == "committing":
        commit_passed, commit_reasons = _validate_phase(operation, acks, phase="commit")
        readback_passed, readback_reasons = _validate_phase(
            operation,
            acks,
            phase="readback",
            expected_parameter_set_id=operation.to_parameter_set_id,
        )
        reasons = tuple(sorted(set(commit_reasons) | set(readback_reasons)))
        if commit_passed and readback_passed:
            return ActivationAdvance(state, "succeeded", (), True)
        if any(
            reason.startswith(("generation_mismatch", "payload_mismatch", "ack_not_accepted", "readback_parameter_set_mismatch", "unexpected_role"))
            for reason in reasons
        ):
            return ActivationAdvance(state, "rollback_required", reasons, False)
        return ActivationAdvance(state, state, reasons, False)
    if state == "rollback_required":
        return ActivationAdvance(state, "rolling_back", (), False)
    if state == "rolling_back":
        passed, reasons = _validate_phase(
            operation,
            acks,
            phase="rollback",
            expected_parameter_set_id=operation.from_parameter_set_id,
        )
        if passed:
            return ActivationAdvance(state, "rolled_back", (), True)
        return ActivationAdvance(state, state, reasons, False)
    raise InvalidActivationTransition(f"unsupported_activation_state:{state}")


def activation_evidence(
    operation: ParameterActivationOperation,
    acks: Sequence[ParameterRuntimeAck],
    *,
    evaluated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a secret-free, deterministic status/readback evidence document."""

    timestamp = evaluated_at or datetime.now(UTC)
    ack_payload = [
        {
            "process_role": ack.process_role,
            "phase": ack.phase,
            "generation": ack.generation,
            "payload_sha256": ack.payload_sha256,
            "ack_status": ack.ack_status,
            "observed_parameter_set_id": ack.observed_parameter_set_id,
            "ack_at": ack.ack_at.isoformat(),
            "error_type": ack.error_message.split(":", 1)[0] if ack.error_message else None,
        }
        for ack in sorted(acks, key=lambda item: (item.phase, item.process_role))
        if ack.operation_id == operation.operation_id
    ]
    payload: dict[str, Any] = {
        "format_version": 1,
        "evaluated_at": timestamp.isoformat(),
        "operation": operation.to_dict(),
        "acks": ack_payload,
        "activation_succeeded": operation.state == "succeeded",
        "authorization_boundary": (
            "activation evidence only; never grants live trading permission"
        ),
    }
    fingerprint_input = dict(payload)
    fingerprint_input.pop("evaluated_at")
    payload["evidence_fingerprint"] = hashlib.sha256(
        json.dumps(fingerprint_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return payload
