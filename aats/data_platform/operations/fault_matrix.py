"""Evidence contract for isolated simulation fault-injection drills.

This module evaluates observations; it never disconnects infrastructure.  A
drill runner must operate only on the isolated simulation topology and provide
independently addressable evidence references for injection, fail-closed
behaviour, recovery and cleanup.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping, Sequence


REQUIRED_FAULT_CASES = (
    "redis_disconnect",
    "nats_disconnect",
    "execution_restart",
    "stale_generation",
    "activation_ttl_expiry",
)
VALID_STATUS = frozenset({"PASS", "FAIL", "UNKNOWN"})
DEFAULT_MAX_CASE_AGE_SECONDS = 24 * 60 * 60
MAX_FUTURE_CLOCK_SKEW_SECONDS = 5.0
REQUIRED_CHECKS = (
    "baseline_healthy",
    "fault_observed",
    "new_risk_blocked",
    "no_unintended_order",
    "recovery_verified",
    "cleanup_verified",
)


@dataclass(frozen=True, slots=True)
class FaultCheck:
    name: str
    status: str
    evidence_ref: str | None
    observed_at: datetime | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("fault_check_name_required")
        if self.status not in VALID_STATUS:
            raise ValueError("invalid_fault_check_status")
        if self.status == "PASS" and not (self.evidence_ref or "").strip():
            raise ValueError("passing_fault_check_requires_evidence_ref")
        if self.status == "PASS" and self.observed_at is None:
            raise ValueError("passing_fault_check_requires_observed_at")
        if self.observed_at is not None and (
            self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None
        ):
            raise ValueError("observed_at_must_be_timezone_aware")


@dataclass(frozen=True, slots=True)
class FaultCaseObservation:
    case_name: str
    isolated_profile: str
    checks: tuple[FaultCheck, ...]
    started_at: datetime
    completed_at: datetime

    def __post_init__(self) -> None:
        if self.case_name not in REQUIRED_FAULT_CASES:
            raise ValueError(f"unsupported_fault_case:{self.case_name}")
        if self.isolated_profile != "derivatives":
            raise ValueError("fault_drills_are_derivatives_simulation_only")
        if self.started_at.tzinfo is None or self.started_at.utcoffset() is None:
            raise ValueError("started_at_must_be_timezone_aware")
        if self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None:
            raise ValueError("completed_at_must_be_timezone_aware")
        if self.completed_at < self.started_at:
            raise ValueError("fault_case_completion_precedes_start")
        names = [check.name for check in self.checks]
        if len(names) != len(set(names)):
            raise ValueError("duplicate_fault_check")
        for check in self.checks:
            if check.observed_at is None:
                continue
            if not (
                self.started_at - timedelta(seconds=MAX_FUTURE_CLOCK_SKEW_SECONDS)
                <= check.observed_at
                <= self.completed_at + timedelta(seconds=MAX_FUTURE_CLOCK_SKEW_SECONDS)
            ):
                raise ValueError(f"fault_check_outside_case_window:{check.name}")


@dataclass(frozen=True, slots=True)
class FaultCaseResult:
    case_name: str
    passed: bool
    reason_codes: tuple[str, ...]
    checks: tuple[FaultCheck, ...]


@dataclass(frozen=True, slots=True)
class FaultMatrixEvidence:
    format_version: int
    evaluated_at: datetime
    profile: str
    passed: bool
    reason_codes: tuple[str, ...]
    cases: tuple[FaultCaseResult, ...]
    evidence_fingerprint: str
    authorization_boundary: str = (
        "simulation fault evidence only; does not authorize live deployment"
    )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evaluated_at"] = self.evaluated_at.isoformat()
        payload["reason_codes"] = list(self.reason_codes)
        for case in payload["cases"]:
            case["reason_codes"] = list(case["reason_codes"])
            for check in case["checks"]:
                observed_at = check.get("observed_at")
                if isinstance(observed_at, datetime):
                    check["observed_at"] = observed_at.isoformat()
        return payload


def _case_result(observation: FaultCaseObservation) -> FaultCaseResult:
    by_name = {check.name: check for check in observation.checks}
    reasons: set[str] = set()
    for check_name in REQUIRED_CHECKS:
        check = by_name.get(check_name)
        if check is None:
            reasons.add(f"check_missing:{check_name}")
        elif check.status != "PASS":
            reasons.add(f"check_{check.status.lower()}:{check_name}")
    extra = sorted(set(by_name) - set(REQUIRED_CHECKS))
    reasons.update(f"unexpected_check:{name}" for name in extra)
    ordered = tuple(sorted(reasons))
    return FaultCaseResult(
        case_name=observation.case_name,
        passed=not ordered,
        reason_codes=ordered,
        checks=tuple(sorted(observation.checks, key=lambda item: item.name)),
    )


def evaluate_fault_matrix(
    observations: Sequence[FaultCaseObservation],
    *,
    evaluated_at: datetime | None = None,
    max_case_age_seconds: int = DEFAULT_MAX_CASE_AGE_SECONDS,
) -> FaultMatrixEvidence:
    """Require every fault case and every fail-closed/recovery check."""

    timestamp = evaluated_at or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("evaluated_at_must_be_timezone_aware")
    if max_case_age_seconds <= 0:
        raise ValueError("max_case_age_seconds_must_be_positive")
    names = [item.case_name for item in observations]
    if len(names) != len(set(names)):
        raise ValueError("duplicate_fault_case")
    by_name = {item.case_name: item for item in observations}
    results: list[FaultCaseResult] = []
    reasons: set[str] = set()
    for case_name in REQUIRED_FAULT_CASES:
        observation = by_name.get(case_name)
        if observation is None:
            reasons.add(f"case_missing:{case_name}")
            continue
        result = _case_result(observation)
        results.append(result)
        if not result.passed:
            reasons.add(f"case_failed:{case_name}")
        if (
            observation.completed_at - timestamp
        ).total_seconds() > MAX_FUTURE_CLOCK_SKEW_SECONDS:
            reasons.add(f"case_completed_in_future:{case_name}")
        if (timestamp - observation.completed_at).total_seconds() > max_case_age_seconds:
            reasons.add(f"case_stale:{case_name}")
    ordered_reasons = tuple(sorted(reasons))
    fingerprint_payload = {
        "format_version": 1,
        "profile": "derivatives",
        "reason_codes": ordered_reasons,
        "cases": [
            {
                "case_name": result.case_name,
                "passed": result.passed,
                "reason_codes": result.reason_codes,
                "checks": [
                    {
                        "name": check.name,
                        "status": check.status,
                        "evidence_ref": check.evidence_ref,
                        "observed_at": check.observed_at.isoformat()
                        if check.observed_at
                        else None,
                        "reason": check.reason,
                    }
                    for check in result.checks
                ],
            }
            for result in results
        ],
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return FaultMatrixEvidence(
        format_version=1,
        evaluated_at=timestamp.astimezone(UTC),
        profile="derivatives",
        passed=not ordered_reasons,
        reason_codes=ordered_reasons,
        cases=tuple(results),
        evidence_fingerprint=fingerprint,
    )


def parse_fault_observations(payload: Mapping[str, Any]) -> tuple[FaultCaseObservation, ...]:
    """Parse a strict JSON-compatible observation manifest."""

    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError("fault_cases_list_required")
    observations: list[FaultCaseObservation] = []
    for raw_case in raw_cases:
        if not isinstance(raw_case, Mapping):
            raise ValueError("fault_case_must_be_object")
        raw_checks = raw_case.get("checks")
        if not isinstance(raw_checks, list):
            raise ValueError("fault_checks_list_required")
        checks = tuple(
            FaultCheck(
                name=str(item["name"]),
                status=str(item["status"]),
                evidence_ref=str(item["evidence_ref"]) if item.get("evidence_ref") else None,
                observed_at=datetime.fromisoformat(str(item["observed_at"]))
                if item.get("observed_at")
                else None,
                reason=str(item["reason"]) if item.get("reason") else None,
            )
            for item in raw_checks
            if isinstance(item, Mapping)
        )
        observations.append(
            FaultCaseObservation(
                case_name=str(raw_case["case_name"]),
                isolated_profile=str(raw_case["isolated_profile"]),
                checks=checks,
                started_at=datetime.fromisoformat(str(raw_case["started_at"])),
                completed_at=datetime.fromisoformat(str(raw_case["completed_at"])),
            )
        )
    return tuple(observations)
