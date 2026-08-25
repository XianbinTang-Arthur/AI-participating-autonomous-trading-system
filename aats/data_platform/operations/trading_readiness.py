"""Fail-closed trading-readiness evidence aggregation.

``simulation_ready`` means the local derivatives simulation evidence is
complete.  It is intentionally distinct from ``production_ready``.  The
future canary target remains hard-disabled until a later protocol version and
an independently reviewed deployment path exist.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence


VALID_FACT_STATUSES = frozenset({"PASS", "FAIL", "UNKNOWN", "DEGRADED"})
MAX_FUTURE_CLOCK_SKEW_SECONDS = 5.0
COMMON_REQUIRED_FACTS = (
    "git_revision",
    "image_identity",
    "schema_revision",
    "required_containers",
    "system_health",
    "recovery_state",
    "collector_freshness",
    "microstructure_eligibility",
    "candidate_capital_eligibility",
    "l2_execution",
    "simulation_calibration",
    "parameter_activation_readback",
    "fault_matrix",
    "kill_switch_propagation",
    "order_reconciliation",
)
FUTURE_CANARY_REQUIRED_FACTS = COMMON_REQUIRED_FACTS + (
    "canary_contract",
    "credentials_least_privilege",
    "dual_operator_approval",
    "exchange_account_reconciliation",
    "canary_deployment_authorized",
)


@dataclass(frozen=True, slots=True)
class ReadinessFact:
    name: str
    status: str
    evidence_ref: str | None
    observed_at: datetime | None
    max_age_seconds: int | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("readiness_fact_name_required")
        if self.status not in VALID_FACT_STATUSES:
            raise ValueError("invalid_readiness_fact_status")
        if self.status == "PASS" and not (self.evidence_ref or "").strip():
            raise ValueError("passing_readiness_fact_requires_evidence_ref")
        if self.observed_at is not None and (
            self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None
        ):
            raise ValueError("observed_at_must_be_timezone_aware")
        if self.max_age_seconds is not None and self.max_age_seconds <= 0:
            raise ValueError("max_age_seconds_must_be_positive")


@dataclass(frozen=True, slots=True)
class ReadinessEvidence:
    format_version: int
    target: str
    profile: str
    evaluated_at: datetime
    git_commit: str
    image_identity: str
    schema_revision: str
    facts: tuple[ReadinessFact, ...]
    reason_codes: tuple[str, ...]
    simulation_ready: bool
    production_ready: bool
    trading_ready: bool
    evidence_fingerprint: str
    authorization_boundary: str = (
        "readiness evidence only; no order or live-deployment authorization"
    )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evaluated_at"] = self.evaluated_at.isoformat()
        payload["reason_codes"] = list(self.reason_codes)
        for fact in payload["facts"]:
            observed_at = fact.get("observed_at")
            if isinstance(observed_at, datetime):
                fact["observed_at"] = observed_at.isoformat()
        return payload


def _required_facts(target: str) -> tuple[str, ...]:
    if target == "simulation":
        return COMMON_REQUIRED_FACTS
    if target == "future_canary":
        return FUTURE_CANARY_REQUIRED_FACTS
    raise ValueError("unsupported_readiness_target")


def evaluate_trading_readiness(
    *,
    target: str,
    profile: str,
    git_commit: str,
    image_identity: str,
    schema_revision: str,
    facts: Sequence[ReadinessFact],
    evaluated_at: datetime | None = None,
) -> ReadinessEvidence:
    """Aggregate exact evidence.  Missing, stale and degraded are NO-GO."""

    timestamp = evaluated_at or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("evaluated_at_must_be_timezone_aware")
    expected_profile = {
        "simulation": "derivatives",
        "future_canary": "future_derivatives_canary",
    }.get(target)
    required = _required_facts(target)
    if profile != expected_profile:
        raise ValueError("readiness_target_profile_mismatch")
    for field_name, value in (
        ("git_commit", git_commit),
        ("image_identity", image_identity),
        ("schema_revision", schema_revision),
    ):
        if not value.strip():
            raise ValueError(f"{field_name}_required")

    names = [fact.name for fact in facts]
    if len(names) != len(set(names)):
        raise ValueError("duplicate_readiness_fact")
    by_name = {fact.name: fact for fact in facts}
    reasons: set[str] = set()
    normalized: list[ReadinessFact] = []
    for name in required:
        fact = by_name.get(name)
        if fact is None:
            reasons.add(f"fact_missing:{name}")
            normalized.append(
                ReadinessFact(
                    name=name,
                    status="UNKNOWN",
                    evidence_ref=None,
                    observed_at=None,
                    reason="missing",
                )
            )
            continue
        normalized.append(fact)
        if fact.status != "PASS":
            reasons.add(f"fact_{fact.status.lower()}:{name}")
        if fact.status == "PASS" and fact.observed_at is None:
            reasons.add(f"fact_observation_time_missing:{name}")
        if fact.status == "PASS" and fact.max_age_seconds is None:
            reasons.add(f"fact_max_age_missing:{name}")
        if (
            fact.status == "PASS"
            and fact.observed_at is not None
            and (fact.observed_at - timestamp).total_seconds()
            > MAX_FUTURE_CLOCK_SKEW_SECONDS
        ):
            reasons.add(f"fact_observed_in_future:{name}")
        if (
            fact.status == "PASS"
            and fact.observed_at is not None
            and fact.max_age_seconds is not None
            and (timestamp - fact.observed_at).total_seconds() > fact.max_age_seconds
        ):
            reasons.add(f"fact_stale:{name}")
    extra = sorted(set(by_name) - set(required))
    reasons.update(f"unexpected_fact:{name}" for name in extra)
    if target == "future_canary":
        reasons.add("future_canary_activation_not_implemented")

    ordered_reasons = tuple(sorted(reasons))
    simulation_ready = target == "simulation" and not ordered_reasons
    # Format v1 has no production-enabling semantics by design.
    production_ready = False
    trading_ready = False
    fingerprint_payload: Mapping[str, Any] = {
        "format_version": 1,
        "target": target,
        "profile": profile,
        "git_commit": git_commit,
        "image_identity": image_identity,
        "schema_revision": schema_revision,
        "facts": [
            {
                "name": fact.name,
                "status": fact.status,
                "evidence_ref": fact.evidence_ref,
                "observed_at": fact.observed_at.isoformat() if fact.observed_at else None,
                "max_age_seconds": fact.max_age_seconds,
                "reason": fact.reason,
            }
            for fact in normalized
        ],
        "reason_codes": ordered_reasons,
        "simulation_ready": simulation_ready,
        "production_ready": production_ready,
        "trading_ready": trading_ready,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ReadinessEvidence(
        format_version=1,
        target=target,
        profile=profile,
        evaluated_at=timestamp.astimezone(UTC),
        git_commit=git_commit,
        image_identity=image_identity,
        schema_revision=schema_revision,
        facts=tuple(normalized),
        reason_codes=ordered_reasons,
        simulation_ready=simulation_ready,
        production_ready=production_ready,
        trading_ready=trading_ready,
        evidence_fingerprint=fingerprint,
    )


def parse_readiness_facts(payload: Mapping[str, Any]) -> tuple[ReadinessFact, ...]:
    raw_facts = payload.get("facts")
    if not isinstance(raw_facts, list):
        raise ValueError("readiness_facts_list_required")
    facts: list[ReadinessFact] = []
    for item in raw_facts:
        if not isinstance(item, Mapping):
            raise ValueError("readiness_fact_must_be_object")
        facts.append(
            ReadinessFact(
                name=str(item["name"]),
                status=str(item["status"]),
                evidence_ref=str(item["evidence_ref"]) if item.get("evidence_ref") else None,
                observed_at=datetime.fromisoformat(str(item["observed_at"]))
                if item.get("observed_at")
                else None,
                max_age_seconds=int(item["max_age_seconds"])
                if item.get("max_age_seconds") is not None
                else None,
                reason=str(item["reason"]) if item.get("reason") else None,
            )
        )
    return tuple(facts)
