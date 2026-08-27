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

from aats.data_platform.research_factory.contract_lineage import (
    ContractAwareArtifactLineage,
)
from aats.domain.instrument_scope import (
    INSTRUMENT_SCOPE_UNSUPPORTED_REASON,
    classify_instrument_scope,
)


CURRENT_SELECTION_PROTOCOL = "train_valid_selection_test_holdout_v2"
CAPITAL_ELIGIBLE_EXECUTION_MODEL = "l2_event_replay_v1"
# Current Gold/Silver has no DB-backed source-content verifier.  Caller booleans
# and evidence references cannot create authority, so no supported spot or
# derivative candidate may become capital-eligible until that verifier is
# implemented and this constant is removed with its migration/tests.
_SOURCE_AWARE_ARTIFACT_VERIFIER_AVAILABLE = False
_DERIVATIVE_ARTIFACT_VERIFIER_AVAILABLE = False


@dataclass(frozen=True, slots=True)
class CapitalEligibilityEvidence:
    candidate_id: str
    dataset_fingerprint: str
    symbol: str
    timeframe: str
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
    source_contract_lineage: ContractAwareArtifactLineage | None = None

    def __post_init__(self) -> None:
        if not self.candidate_id.strip():
            raise ValueError("candidate_id_required")
        if not self.dataset_fingerprint.strip():
            raise ValueError("dataset_fingerprint_required")
        if not self.symbol.strip():
            raise ValueError("symbol_required")
        if not self.timeframe.strip():
            raise ValueError("timeframe_required")
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "timeframe", self.timeframe.strip().lower())
        if self.source_contract_lineage is not None and not isinstance(
            self.source_contract_lineage,
            ContractAwareArtifactLineage,
        ):
            raise ValueError(
                "source_contract_lineage_must_be_ContractAwareArtifactLineage"
            )
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
        "format_version": 2,
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
    instrument_scope = classify_instrument_scope(evidence.symbol)
    if instrument_scope == "unsupported":
        reasons.add(INSTRUMENT_SCOPE_UNSUPPORTED_REASON)
    else:
        if not _SOURCE_AWARE_ARTIFACT_VERIFIER_AVAILABLE:
            reasons.add("source_aware_research_artifact_unavailable")
    if instrument_scope == "swap":
        if not _DERIVATIVE_ARTIFACT_VERIFIER_AVAILABLE:
            reasons.add("contract_aware_derivative_artifact_unavailable")
        if evidence.source_contract_lineage is None:
            reasons.add("source_contract_lineage_missing")
        elif evidence.source_contract_lineage.verified is not True:
            reasons.add("source_contract_lineage_unverified")
        elif (
            evidence.source_contract_lineage.symbol != evidence.symbol
            or evidence.source_contract_lineage.timeframe != evidence.timeframe
        ):
            reasons.add("source_contract_lineage_scope_mismatch")

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
        format_version=2,
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
    symbol = str(payload.get("symbol") or candidate.get("symbol") or "").strip().upper()
    instrument_scope = classify_instrument_scope(symbol)
    if instrument_scope == "unsupported":
        reasons.add(INSTRUMENT_SCOPE_UNSUPPORTED_REASON)
    else:
        if not _SOURCE_AWARE_ARTIFACT_VERIFIER_AVAILABLE:
            reasons.add("source_aware_research_artifact_unavailable")
    if instrument_scope == "swap":
        if not _DERIVATIVE_ARTIFACT_VERIFIER_AVAILABLE:
            reasons.add("contract_aware_derivative_artifact_unavailable")
        raw_lineage = payload.get("source_contract_lineage")
        if not isinstance(raw_lineage, Mapping):
            reasons.add("source_contract_lineage_missing")
        else:
            try:
                lineage = ContractAwareArtifactLineage.from_mapping(raw_lineage)
            except (TypeError, ValueError):
                reasons.add("source_contract_lineage_invalid")
            else:
                if lineage.verified is not True:
                    reasons.add("source_contract_lineage_unverified")
                elif (
                    lineage.symbol != symbol
                    or lineage.timeframe
                    != str(payload.get("timeframe") or "").strip().lower()
                ):
                    reasons.add("source_contract_lineage_scope_mismatch")
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
