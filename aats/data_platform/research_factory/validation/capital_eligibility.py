"""Single fail-closed capital-eligibility decision for research candidates.

This decision does not authorize parameter activation or order submission.  It
only states whether a candidate has the minimum evidence needed to be *eligible
for a later, human-approved, isolated simulation/canary decision*.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Mapping


CURRENT_SELECTION_PROTOCOL = "train_valid_selection_test_holdout_v2"
CAPITAL_ELIGIBLE_EXECUTION_MODEL = "l2_event_replay_v1"


@dataclass(frozen=True, slots=True)
class CapitalEligibilityEvidence:
    candidate_id: str
    dataset_fingerprint: str
    selection_protocol_version: str | None
    benchmark_segment: str | None
    candidate_gate_passed: bool
    development_evidence_passed: bool
    microstructure_eligible: bool
    walk_forward_passed: bool
    statistical_evidence_passed: bool
    execution_model: str | None
    execution_calibration_passed: bool
    holdout_status: str | None
    holdout_passed: bool
    evidence_refs: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.candidate_id.strip():
            raise ValueError("candidate_id_required")
        if not self.dataset_fingerprint.strip():
            raise ValueError("dataset_fingerprint_required")
        object.__setattr__(self, "evidence_refs", dict(self.evidence_refs))


@dataclass(frozen=True, slots=True)
class CapitalEligibilityDecision:
    format_version: int
    evaluated_at: datetime
    candidate_id: str
    capital_eligible: bool
    reason_codes: tuple[str, ...]
    evidence_refs: Mapping[str, str]
    decision_fingerprint: str
    authorization_boundary: str = (
        "research evidence only; does not authorize live activation or order submission"
    )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evaluated_at"] = self.evaluated_at.isoformat()
        payload["reason_codes"] = list(self.reason_codes)
        return payload


def _decision_fingerprint(
    evidence: CapitalEligibilityEvidence,
    *,
    capital_eligible: bool,
    reasons: tuple[str, ...],
) -> str:
    payload = {
        "format_version": 1,
        "evidence": asdict(evidence),
        "capital_eligible": capital_eligible,
        "reason_codes": reasons,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evaluate_capital_eligibility(
    evidence: CapitalEligibilityEvidence,
    *,
    evaluated_at: datetime | None = None,
) -> CapitalEligibilityDecision:
    """Require every independent evidence class; unknown is always ineligible."""

    reasons: set[str] = set()
    if evidence.selection_protocol_version != CURRENT_SELECTION_PROTOCOL:
        reasons.add("selection_protocol_not_v2")
    if evidence.benchmark_segment != "valid":
        reasons.add("benchmark_segment_not_valid")
    checks = (
        ("candidate_gate_failed_or_unknown", evidence.candidate_gate_passed),
        ("development_evidence_failed_or_unknown", evidence.development_evidence_passed),
        ("microstructure_ineligible_or_unknown", evidence.microstructure_eligible),
        ("walk_forward_failed_or_unknown", evidence.walk_forward_passed),
        ("statistical_evidence_failed_or_unknown", evidence.statistical_evidence_passed),
        ("execution_calibration_failed_or_unknown", evidence.execution_calibration_passed),
        ("holdout_failed_or_unknown", evidence.holdout_passed),
    )
    reasons.update(reason for reason, passed in checks if passed is not True)
    if evidence.execution_model != CAPITAL_ELIGIBLE_EXECUTION_MODEL:
        reasons.add("execution_model_not_l2_event_replay")
    if evidence.holdout_status != "evaluated_pass":
        reasons.add("holdout_not_evaluated_pass")

    required_refs = {
        "candidate",
        "development",
        "microstructure",
        "walk_forward",
        "statistics",
        "l2_execution",
        "execution_calibration",
        "holdout",
    }
    missing_refs = sorted(required_refs - set(evidence.evidence_refs))
    reasons.update(f"evidence_ref_missing:{name}" for name in missing_refs)

    ordered_reasons = tuple(sorted(reasons))
    capital_eligible = not ordered_reasons
    timestamp = evaluated_at or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("evaluated_at_must_be_timezone_aware")
    return CapitalEligibilityDecision(
        format_version=1,
        evaluated_at=timestamp.astimezone(UTC),
        candidate_id=evidence.candidate_id,
        capital_eligible=capital_eligible,
        reason_codes=ordered_reasons,
        evidence_refs=dict(evidence.evidence_refs),
        decision_fingerprint=_decision_fingerprint(
            evidence,
            capital_eligible=capital_eligible,
            reasons=ordered_reasons,
        ),
    )


def legacy_candidate_reasons(candidate: Mapping[str, Any]) -> tuple[str, ...]:
    """Classify existing candidate JSON without trusting historical prose."""

    payload = candidate.get("payload")
    if not isinstance(payload, Mapping):
        return ("candidate_payload_missing",)
    reasons: set[str] = set()
    if payload.get("selection_protocol_version") != CURRENT_SELECTION_PROTOCOL:
        reasons.add("selection_protocol_not_v2")
    if payload.get("benchmark_segment") == "test":
        reasons.add("legacy_test_used_for_selection")
    elif payload.get("benchmark_segment") != "valid":
        reasons.add("benchmark_segment_not_valid")
    if payload.get("holdout_status") != "evaluated_pass":
        reasons.add("holdout_not_evaluated_pass")
    if payload.get("execution_model") != CAPITAL_ELIGIBLE_EXECUTION_MODEL:
        reasons.add("execution_model_not_l2_event_replay")
    # Historical candidate gates do not contain the new independent evidence set.
    for evidence_name in (
        "microstructure_eligibility_ref",
        "walk_forward_evidence_ref",
        "statistical_evidence_ref",
        "execution_calibration_ref",
    ):
        if not payload.get(evidence_name):
            reasons.add(f"evidence_ref_missing:{evidence_name}")
    return tuple(sorted(reasons))
