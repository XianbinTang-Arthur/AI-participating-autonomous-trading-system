"""Audit historical candidates and build deterministic v2 replay plans."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from aats.data_platform.research_factory.validation.capital_eligibility import (
    CURRENT_SELECTION_PROTOCOL,
    legacy_candidate_reasons,
)


CLASSIFIER_VERSION = "capital_eligibility_classifier_v1"


@dataclass(frozen=True, slots=True)
class HistoricalCandidateAudit:
    audit_id: str
    evaluated_at: datetime
    classifier_version: str
    candidate_id: str
    experiment_id: str
    source_candidate_ref: str
    source_candidate_sha256: str
    capital_eligible: bool
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evaluated_at"] = self.evaluated_at.isoformat()
        payload["reason_codes"] = list(self.reason_codes)
        return payload


@dataclass(frozen=True, slots=True)
class CandidateReplayPlan:
    format_version: int
    plan_id: str
    created_at: datetime
    source_candidate_id: str
    source_experiment_id: str
    source_candidate_ref: str
    source_experiment_spec_ref: str
    source_candidate_sha256: str
    source_experiment_spec_sha256: str
    symbol: str
    timeframe: str
    start: str
    end: str
    dataset_version: str
    factor_expression: str
    label_horizon_bars: int
    fee_bps: float
    slippage_bps: float
    funding_bps: float
    research_profile: str
    selection_protocol_version: str
    train_ratio: float
    valid_ratio: float
    test_ratio: float
    status: str
    reason_codes: tuple[str, ...]
    authorization_boundary: str = (
        "research replay plan only; no active parameters or live orders"
    )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["created_at"] = self.created_at.isoformat()
        payload["reason_codes"] = list(self.reason_codes)
        return payload


def _load_json_mapping(path: Path, *, label: str) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label}_must_be_mapping")
    return dict(payload)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_id(prefix: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:24]}"


def audit_historical_candidate(
    candidate_path: Path,
    *,
    artifact_root: Path,
    evaluated_at: datetime | None = None,
) -> HistoricalCandidateAudit:
    candidate = _load_json_mapping(candidate_path, label="candidate")
    candidate_id = str(candidate.get("candidate_id", "")).strip()
    experiment_id = str(candidate.get("experiment_id", "")).strip()
    if not candidate_id or not experiment_id:
        raise ValueError("candidate_identity_missing")
    try:
        source_ref = candidate_path.resolve().relative_to(artifact_root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("candidate_path_outside_artifact_root") from exc
    source_sha = _sha256_file(candidate_path)
    reasons = legacy_candidate_reasons(candidate)
    timestamp = evaluated_at or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("evaluated_at_must_be_timezone_aware")
    audit_id = _stable_id(
        "capaudit",
        {
            "classifier_version": CLASSIFIER_VERSION,
            "candidate_id": candidate_id,
            "source_candidate_sha256": source_sha,
        },
    )
    return HistoricalCandidateAudit(
        audit_id=audit_id,
        evaluated_at=timestamp.astimezone(UTC),
        classifier_version=CLASSIFIER_VERSION,
        candidate_id=candidate_id,
        experiment_id=experiment_id,
        source_candidate_ref=source_ref,
        source_candidate_sha256=source_sha,
        # Historical artifacts never have the full new evidence set.
        capital_eligible=False,
        reason_codes=reasons or ("historical_candidate_requires_current_revalidation",),
    )


def build_candidate_v2_replay_plan(
    *,
    audit: HistoricalCandidateAudit,
    candidate_path: Path,
    experiment_spec_path: Path,
    artifact_root: Path,
    created_at: datetime | None = None,
) -> CandidateReplayPlan:
    candidate = _load_json_mapping(candidate_path, label="candidate")
    spec = _load_json_mapping(experiment_spec_path, label="experiment_spec")
    dataset = spec.get("dataset")
    features = spec.get("features")
    label = spec.get("label")
    payload = candidate.get("payload")
    if not isinstance(dataset, Mapping):
        raise ValueError("experiment_dataset_missing")
    if not isinstance(features, list) or len(features) != 1 or not isinstance(features[0], Mapping):
        raise ValueError("experiment_requires_exactly_one_feature")
    if not isinstance(label, Mapping) or not isinstance(payload, Mapping):
        raise ValueError("experiment_label_or_candidate_payload_missing")
    expression = str(features[0].get("expression", "")).strip()
    if expression != str(payload.get("factor_expression", "")).strip():
        raise ValueError("candidate_factor_expression_mismatch")
    try:
        spec_ref = experiment_spec_path.resolve().relative_to(artifact_root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("experiment_spec_path_outside_artifact_root") from exc
    timestamp = created_at or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("created_at_must_be_timezone_aware")
    identity = {
        "source_candidate_sha256": audit.source_candidate_sha256,
        "source_experiment_spec_sha256": _sha256_file(experiment_spec_path),
        "selection_protocol_version": CURRENT_SELECTION_PROTOCOL,
    }
    return CandidateReplayPlan(
        format_version=1,
        plan_id=_stable_id("v2replay", identity),
        created_at=timestamp.astimezone(UTC),
        source_candidate_id=audit.candidate_id,
        source_experiment_id=audit.experiment_id,
        source_candidate_ref=audit.source_candidate_ref,
        source_experiment_spec_ref=spec_ref,
        source_candidate_sha256=audit.source_candidate_sha256,
        source_experiment_spec_sha256=identity["source_experiment_spec_sha256"],
        symbol=str(dataset.get("symbol", "")).strip().upper(),
        timeframe=str(dataset.get("timeframe", "")).strip(),
        start=str(dataset.get("window_start", "")).strip(),
        end=str(dataset.get("window_end", "")).strip(),
        dataset_version=str(dataset.get("dataset_version", "")).strip(),
        factor_expression=expression,
        label_horizon_bars=int(label.get("horizon_bars", 0)),
        fee_bps=float(label.get("fee_bps", 0.0)),
        slippage_bps=float(label.get("slippage_bps", 0.0)),
        funding_bps=float(label.get("funding_bps", 0.5)),
        research_profile="real_factor_research",
        selection_protocol_version=CURRENT_SELECTION_PROTOCOL,
        train_ratio=0.6,
        valid_ratio=0.2,
        test_ratio=0.2,
        status="planned_not_run",
        reason_codes=audit.reason_codes,
    )
