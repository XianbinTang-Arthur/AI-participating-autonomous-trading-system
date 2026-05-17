"""Research-only evidence reference integrity checks."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aats.data_platform.research_factory.artifacts import normalize_relative_artifact_path
from aats.data_platform.research_factory.preapply import PreApplyEvidencePackage

INTEGRITY_REPORT_SCHEMA_VERSION = "research_evidence_reference_integrity_v1"


@dataclass(frozen=True, slots=True)
class EvidenceReferenceIntegrityReport:
    """Integrity report for research evidence refs."""

    subject_id: str
    subject_type: str
    artifact_root: str
    checked_refs: Mapping[str, str]
    passed: bool
    failures: Sequence[str] = field(default_factory=tuple)
    missing_refs: Sequence[str] = field(default_factory=tuple)
    unsafe_refs: Sequence[str] = field(default_factory=tuple)
    identity_mismatches: Sequence[str] = field(default_factory=tuple)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = INTEGRITY_REPORT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_safe_identifier(self.subject_id, "subject_id")
        _require_non_empty_text(self.subject_type, "subject_type")
        _require_non_empty_text(self.artifact_root, "artifact_root")
        if not isinstance(self.checked_refs, Mapping):
            raise ValueError("checked_refs must be a mapping")
        object.__setattr__(self, "checked_refs", dict(sorted(self.checked_refs.items())))
        failures = _normalize_text_sequence(self.failures, "failures", allow_empty=True)
        object.__setattr__(self, "failures", failures)
        object.__setattr__(self, "missing_refs", _normalize_text_sequence(self.missing_refs, "missing_refs", allow_empty=True))
        object.__setattr__(self, "unsafe_refs", _normalize_text_sequence(self.unsafe_refs, "unsafe_refs", allow_empty=True))
        object.__setattr__(
            self,
            "identity_mismatches",
            _normalize_text_sequence(self.identity_mismatches, "identity_mismatches", allow_empty=True),
        )
        if not isinstance(self.passed, bool):
            raise ValueError("passed must be a bool")
        if self.passed and failures:
            raise ValueError("passing integrity report must not contain failures")
        if not self.passed and not failures:
            raise ValueError("failing integrity report must contain failures")
        _require_timezone_aware_datetime(self.created_at, "created_at")
        if self.schema_version != INTEGRITY_REPORT_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {INTEGRITY_REPORT_SCHEMA_VERSION!r}")


def validate_preapply_package_reference_integrity(
    package: PreApplyEvidencePackage,
    artifact_root: str | Path,
    *,
    created_at: datetime | None = None,
) -> EvidenceReferenceIntegrityReport:
    """Check that a pre-apply package's evidence refs are present and internally consistent."""
    if not isinstance(package, PreApplyEvidencePackage):
        raise ValueError("package must be a PreApplyEvidencePackage")
    root = _require_research_artifact_directory(artifact_root)
    root_resolved = root.resolve(strict=False)
    refs = _prefixed_refs(package)
    checked_refs: dict[str, str] = {}
    payloads: dict[str, Mapping[str, Any]] = {}
    failures: list[str] = []
    missing_refs: list[str] = []
    unsafe_refs: list[str] = []
    identity_mismatches: list[str] = []

    for ref_name, ref_value in sorted(refs.items()):
        try:
            normalized_ref = normalize_relative_artifact_path(ref_value)
        except ValueError:
            reason = f"{ref_name}: unsafe ref {ref_value!r}"
            unsafe_refs.append(reason)
            failures.append(reason)
            continue
        target = (root / normalized_ref).resolve(strict=False)
        if not _is_relative_to(target, root_resolved):
            reason = f"{ref_name}: ref escapes artifact root"
            unsafe_refs.append(reason)
            failures.append(reason)
            continue
        if not target.exists() or not target.is_file():
            reason = f"{ref_name}: missing artifact {normalized_ref}"
            missing_refs.append(reason)
            failures.append(reason)
            continue
        checked_refs[ref_name] = normalized_ref
        try:
            payloads[ref_name] = _load_json_mapping(target, ref_name)
        except ValueError as exc:
            reason = f"{ref_name}: {exc}"
            identity_mismatches.append(reason)
            failures.append(reason)

    for mismatch in _identity_mismatches(package, payloads):
        identity_mismatches.append(mismatch)
        failures.append(mismatch)

    return EvidenceReferenceIntegrityReport(
        subject_id=package.package_id,
        subject_type="preapply_evidence_package",
        artifact_root=root.as_posix(),
        checked_refs=checked_refs,
        passed=not failures,
        failures=tuple(failures),
        missing_refs=tuple(missing_refs),
        unsafe_refs=tuple(unsafe_refs),
        identity_mismatches=tuple(identity_mismatches),
        created_at=created_at or datetime.now(UTC),
    )


def _prefixed_refs(package: PreApplyEvidencePackage) -> dict[str, str]:
    refs: dict[str, str] = {}
    for name, value in package.evidence_refs.items():
        refs[f"evidence_refs.{name}"] = value
    for name, value in package.gate_refs.items():
        refs[f"gate_refs.{name}"] = value
    return refs


def _identity_mismatches(
    package: PreApplyEvidencePackage,
    payloads: Mapping[str, Mapping[str, Any]],
) -> tuple[str, ...]:
    mismatches: list[str] = []
    expected_common = {
        "candidate_id": package.candidate_id,
        "experiment_id": package.experiment_id,
    }
    _check_expected_fields(
        payloads.get("evidence_refs.candidate_artifact"),
        "evidence_refs.candidate_artifact",
        expected_common,
        mismatches,
    )
    _check_expected_fields(
        payloads.get("gate_refs.candidate_gate"),
        "gate_refs.candidate_gate",
        expected_common,
        mismatches,
    )
    expected_recommendation = {
        "candidate_id": package.candidate_id,
        "recommendation_id": package.recommendation_id,
        "experiment_id": package.experiment_id,
    }
    _check_expected_fields(
        payloads.get("evidence_refs.research_recommendation"),
        "evidence_refs.research_recommendation",
        expected_recommendation,
        mismatches,
    )
    expected_observation = {
        "observation_id": package.observation_id,
        "candidate_id": package.candidate_id,
        "recommendation_id": package.recommendation_id,
        "experiment_id": package.experiment_id,
    }
    for ref_name in (
        "evidence_refs.observation_result",
        "evidence_refs.review_outcome",
        "gate_refs.observation_gate_result",
    ):
        _check_expected_fields(payloads.get(ref_name), ref_name, expected_observation, mismatches)

    evidence_bundle = payloads.get("evidence_refs.evidence_bundle")
    if evidence_bundle is not None:
        _check_bool_field(
            evidence_bundle,
            "evidence_refs.evidence_bundle",
            "passed",
            package.evidence_bundle_passed,
            mismatches,
        )
    observation_gate = payloads.get("gate_refs.observation_gate_result")
    if observation_gate is not None:
        _check_bool_field(
            observation_gate,
            "gate_refs.observation_gate_result",
            "passed",
            package.observation_gate_passed,
            mismatches,
        )
    review_outcome = payloads.get("evidence_refs.review_outcome")
    if review_outcome is not None:
        _check_expected_fields(
            review_outcome,
            "evidence_refs.review_outcome",
            {"decision": package.review_decision},
            mismatches,
        )
    return tuple(mismatches)


def _check_expected_fields(
    payload: Mapping[str, Any] | None,
    ref_name: str,
    expected: Mapping[str, Any],
    mismatches: list[str],
) -> None:
    if payload is None:
        return
    for field_name, expected_value in expected.items():
        actual_value = payload.get(field_name)
        if actual_value != expected_value:
            mismatches.append(
                f"{ref_name}: {field_name}={actual_value!r} does not match expected {expected_value!r}"
            )


def _check_bool_field(
    payload: Mapping[str, Any],
    ref_name: str,
    field_name: str,
    expected_value: bool,
    mismatches: list[str],
) -> None:
    actual_value = payload.get(field_name)
    if actual_value is not expected_value:
        mismatches.append(
            f"{ref_name}: {field_name}={actual_value!r} does not match expected {expected_value!r}"
        )


def _load_json_mapping(path: Path, ref_name: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("artifact is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("artifact JSON must be an object")
    return payload


def _require_research_artifact_directory(value: str | Path) -> Path:
    path = Path(value)
    parts = path.parts
    if ".." in parts:
        raise ValueError("artifact_root must not contain path traversal")
    if not any(parts[index] == "artifacts" and parts[index + 1] == "research" for index in range(len(parts) - 1)):
        raise ValueError("artifact_root must be under artifacts/research")
    return path


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _require_safe_identifier(value: Any, field_name: str) -> str:
    value = _require_non_empty_text(value, field_name)
    if "/" in value or "\\" in value or value in {".", ".."} or ".." in value:
        raise ValueError(f"{field_name} must not contain path traversal or separators")
    return value


def _require_non_empty_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _normalize_text_sequence(values: Sequence[str], field_name: str, *, allow_empty: bool) -> tuple[str, ...]:
    if isinstance(values, str | bytes | bytearray) or not isinstance(values, Sequence):
        raise ValueError(f"{field_name} must be a sequence of strings")
    normalized = tuple(_require_non_empty_text(value, field_name) for value in values)
    if not allow_empty and not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _require_timezone_aware_datetime(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
