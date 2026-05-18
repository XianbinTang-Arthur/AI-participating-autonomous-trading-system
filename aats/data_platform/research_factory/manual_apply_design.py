"""Research-only manual apply design packages."""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

from aats.data_platform.research_factory.artifacts import (
    build_artifact_manifest,
    normalize_relative_artifact_path,
    write_artifact_manifest_atomic,
)
from aats.data_platform.research_factory.preapply import (
    PREAPPLY_PACKAGE_REF,
    PREAPPLY_REVIEW_DECISION_REF,
    PREAPPLY_REVIEW_REF,
    PreApplyEvidencePackage,
    PreApplyReview,
    PreApplyReviewDecision,
)

MANUAL_APPLY_DESIGN_SCHEMA_VERSION = "research_manual_apply_design_v1"
MANUAL_APPLY_DESIGN_PACKAGE_REF = "manual_apply_design_package.json"
MANUAL_APPLY_DESIGN_MANIFEST_REF = "manual_apply_design_manifest.json"

ALLOWED_MANUAL_APPLY_DESIGN_STATUSES = frozenset(
    {"design_draft", "design_ready_for_review", "design_rejected"}
)
REQUIRED_MANUAL_APPLY_DESIGN_EVIDENCE_REFS = (
    "preapply_evidence_package",
    "preapply_review",
    "preapply_review_decision",
    "rollback_plan",
)
DESIGN_PROMOTION_TERMS = (
    "active_parameter",
    "active parameter",
    "active_parameters",
    "active parameters",
    "approved_for_apply",
    "auto_apply",
    "auto apply",
    "direct_apply",
    "direct apply",
    "live_order",
    "live order",
    "okx_write",
    "okx write",
    "operator_write",
    "operator write",
    "production_config",
    "production config",
    "production_deploy",
    "production deploy",
    "runtime_config_write",
    "runtime config write",
    "runtime_mutation",
    "runtime mutation",
)


@dataclass(frozen=True, slots=True)
class ManualApplyDesignPackage:
    """Design-only artifact for a separate manual apply design review."""

    design_id: str
    source_preapply_review_id: str
    source_preapply_package_id: str
    candidate_id: str
    recommendation_id: str
    observation_id: str
    experiment_id: str
    candidate_type: str
    status: str
    proposed_change_summary: str
    parameter_or_config_delta: Mapping[str, Any]
    affected_runtime_components: Sequence[str]
    required_risk_guards: Sequence[str]
    required_dry_run_checks: Sequence[str]
    rollback_plan_ref: str
    evidence_refs: Mapping[str, str]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = MANUAL_APPLY_DESIGN_SCHEMA_VERSION
    runtime_mutation_allowed: bool = False
    operator_approval_required: bool = True
    recommended_next_step: str = "submit_manual_apply_design_for_separate_governance_review"
    notes: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_safe_identifier(self.design_id, "design_id")
        _require_safe_identifier(self.source_preapply_review_id, "source_preapply_review_id")
        _require_safe_identifier(self.source_preapply_package_id, "source_preapply_package_id")
        _require_safe_identifier(self.candidate_id, "candidate_id")
        _require_safe_identifier(self.recommendation_id, "recommendation_id")
        _require_safe_identifier(self.observation_id, "observation_id")
        _require_safe_identifier(self.experiment_id, "experiment_id")
        candidate_type = _require_non_empty_text(self.candidate_type, "candidate_type")
        _reject_promotion_text(candidate_type, "candidate_type")
        object.__setattr__(self, "candidate_type", candidate_type)
        if self.status not in ALLOWED_MANUAL_APPLY_DESIGN_STATUSES:
            allowed = ", ".join(sorted(ALLOWED_MANUAL_APPLY_DESIGN_STATUSES))
            raise ValueError(f"manual apply design status must be one of: {allowed}")

        proposed_change_summary = _require_non_empty_text(
            self.proposed_change_summary,
            "proposed_change_summary",
        )
        _reject_promotion_text(proposed_change_summary, "proposed_change_summary")
        object.__setattr__(self, "proposed_change_summary", proposed_change_summary)
        object.__setattr__(
            self,
            "parameter_or_config_delta",
            _normalize_json_mapping(
                self.parameter_or_config_delta,
                "parameter_or_config_delta",
            ),
        )
        object.__setattr__(
            self,
            "affected_runtime_components",
            _normalize_text_sequence(
                self.affected_runtime_components,
                "affected_runtime_components",
            ),
        )
        object.__setattr__(
            self,
            "required_risk_guards",
            _normalize_text_sequence(self.required_risk_guards, "required_risk_guards"),
        )
        object.__setattr__(
            self,
            "required_dry_run_checks",
            _normalize_text_sequence(self.required_dry_run_checks, "required_dry_run_checks"),
        )
        object.__setattr__(
            self,
            "rollback_plan_ref",
            _require_relative_ref(self.rollback_plan_ref, "rollback_plan_ref"),
        )
        object.__setattr__(
            self,
            "evidence_refs",
            _normalize_required_refs(
                self.evidence_refs,
                REQUIRED_MANUAL_APPLY_DESIGN_EVIDENCE_REFS,
                "evidence_refs",
            ),
        )
        _require_timezone_aware_datetime(self.created_at, "created_at")
        if self.schema_version != MANUAL_APPLY_DESIGN_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {MANUAL_APPLY_DESIGN_SCHEMA_VERSION!r}")
        if self.runtime_mutation_allowed is not False:
            raise ValueError("manual apply design package must not allow runtime mutation")
        if self.operator_approval_required is not True:
            raise ValueError("manual apply design package must require operator approval")
        next_step = _require_non_empty_text(self.recommended_next_step, "recommended_next_step")
        _reject_promotion_text(next_step, "recommended_next_step")
        object.__setattr__(self, "recommended_next_step", next_step)
        object.__setattr__(
            self,
            "notes",
            _normalize_text_sequence(self.notes, "notes", allow_empty=True),
        )


class ManualApplyDesignRecorder:
    """Persist manual apply design packages under a research-only artifact root."""

    def __init__(
        self,
        root: str | Path = Path("artifacts")
        / "research"
        / "research_factory"
        / "manual_apply_designs",
        *,
        code_version: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = _require_research_manual_design_root(root)
        self.code_version = code_version
        self._clock = clock or _utc_now

    def record_package(self, package: ManualApplyDesignPackage) -> dict[str, Any]:
        """Write a manual apply design package and manifest."""
        if not isinstance(package, ManualApplyDesignPackage):
            raise ValueError("package must be a ManualApplyDesignPackage")
        design_dir = self._design_dir(package.design_id)
        if design_dir.exists():
            raise ValueError(f"manual apply design package {package.design_id!r} already exists")
        design_dir.mkdir(parents=True)
        _write_json_atomic(design_dir / MANUAL_APPLY_DESIGN_PACKAGE_REF, _to_jsonable(package))
        manifest = build_artifact_manifest(
            artifact_id=package.design_id,
            artifact_type="manual_apply_design",
            status="succeeded",
            started_at=package.created_at,
            finished_at=self._now(),
            input_refs={
                "source_preapply_review_id": package.source_preapply_review_id,
                "source_preapply_package_id": package.source_preapply_package_id,
                "candidate_id": package.candidate_id,
                "recommendation_id": package.recommendation_id,
                "observation_id": package.observation_id,
                "experiment_id": package.experiment_id,
                "candidate_type": package.candidate_type,
                "design_status": package.status,
            },
            output_refs={"manual_apply_design_package": MANUAL_APPLY_DESIGN_PACKAGE_REF},
            code_version=self.code_version,
            notes="research-only manual apply design package; does not authorize runtime mutation",
        )
        write_artifact_manifest_atomic(design_dir / MANUAL_APPLY_DESIGN_MANIFEST_REF, manifest)
        return manifest

    def _design_dir(self, design_id: str) -> Path:
        return self.root / _require_safe_identifier(design_id, "design_id")

    def _now(self) -> datetime:
        return self._clock()


def build_manual_apply_design_package(
    *,
    preapply_package: PreApplyEvidencePackage,
    preapply_review: PreApplyReview,
    preapply_review_decision: PreApplyReviewDecision,
    candidate_type: str,
    proposed_change_summary: str,
    parameter_or_config_delta: Mapping[str, Any],
    affected_runtime_components: Sequence[str],
    required_risk_guards: Sequence[str],
    required_dry_run_checks: Sequence[str],
    rollback_plan_ref: str,
    evidence_refs: Mapping[str, str] | None = None,
    design_id: str | None = None,
    status: str = "design_draft",
    created_at: datetime | None = None,
    notes: Sequence[str] = (),
) -> ManualApplyDesignPackage:
    """Build a research-only design draft from an approved pre-apply review decision."""
    if not isinstance(preapply_package, PreApplyEvidencePackage):
        raise ValueError("preapply_package must be a PreApplyEvidencePackage")
    if not isinstance(preapply_review, PreApplyReview):
        raise ValueError("preapply_review must be a PreApplyReview")
    if not isinstance(preapply_review_decision, PreApplyReviewDecision):
        raise ValueError("preapply_review_decision must be a PreApplyReviewDecision")
    _require_review_matches_package(preapply_review, preapply_package)
    _require_decision_matches_review(preapply_review_decision, preapply_review)
    if preapply_package.status != "preapply_ready":
        raise ValueError("manual apply design package requires a preapply_ready package")
    if preapply_review_decision.decision != "review_approved_for_manual_apply_design":
        raise ValueError("manual apply design package requires approved manual design review decision")
    if preapply_review.reference_integrity_ref is None:
        raise ValueError("manual apply design package requires reference integrity report")
    if preapply_review.reference_integrity_passed is not True:
        raise ValueError("manual apply design package requires passing reference integrity")

    normalized_rollback_ref = _require_relative_ref(rollback_plan_ref, "rollback_plan_ref")
    resolved_evidence_refs = _default_design_evidence_refs(
        rollback_plan_ref=normalized_rollback_ref,
        evidence_refs=evidence_refs,
    )
    return ManualApplyDesignPackage(
        design_id=design_id or f"manual_design_{preapply_review.review_id}",
        source_preapply_review_id=preapply_review.review_id,
        source_preapply_package_id=preapply_package.package_id,
        candidate_id=preapply_package.candidate_id,
        recommendation_id=preapply_package.recommendation_id,
        observation_id=preapply_package.observation_id,
        experiment_id=preapply_package.experiment_id,
        candidate_type=candidate_type,
        status=status,
        proposed_change_summary=proposed_change_summary,
        parameter_or_config_delta=parameter_or_config_delta,
        affected_runtime_components=affected_runtime_components,
        required_risk_guards=required_risk_guards,
        required_dry_run_checks=required_dry_run_checks,
        rollback_plan_ref=normalized_rollback_ref,
        evidence_refs=resolved_evidence_refs,
        created_at=created_at or datetime.now(UTC),
        notes=notes
        or (
            "manual apply design package is evidence-only",
            "separate operator and governance approval is required before any runtime change",
        ),
    )


def _default_design_evidence_refs(
    *,
    rollback_plan_ref: str,
    evidence_refs: Mapping[str, str] | None,
) -> Mapping[str, str]:
    if evidence_refs is not None:
        return evidence_refs
    return {
        "preapply_evidence_package": PREAPPLY_PACKAGE_REF,
        "preapply_review": PREAPPLY_REVIEW_REF,
        "preapply_review_decision": PREAPPLY_REVIEW_DECISION_REF,
        "rollback_plan": rollback_plan_ref,
    }


def _require_review_matches_package(review: PreApplyReview, package: PreApplyEvidencePackage) -> None:
    if review.package_id != package.package_id:
        raise ValueError("preapply review package_id must match package")
    if review.candidate_id != package.candidate_id:
        raise ValueError("preapply review candidate_id must match package")
    if review.recommendation_id != package.recommendation_id:
        raise ValueError("preapply review recommendation_id must match package")
    if review.observation_id != package.observation_id:
        raise ValueError("preapply review observation_id must match package")
    if review.experiment_id != package.experiment_id:
        raise ValueError("preapply review experiment_id must match package")
    if review.package_status != package.status:
        raise ValueError("preapply review package_status must match package status")


def _require_decision_matches_review(decision: PreApplyReviewDecision, review: PreApplyReview) -> None:
    if decision.review_id != review.review_id:
        raise ValueError("preapply review decision review_id must match review")
    if decision.package_id != review.package_id:
        raise ValueError("preapply review decision package_id must match review")
    if decision.candidate_id != review.candidate_id:
        raise ValueError("preapply review decision candidate_id must match review")
    if decision.recommendation_id != review.recommendation_id:
        raise ValueError("preapply review decision recommendation_id must match review")
    if decision.observation_id != review.observation_id:
        raise ValueError("preapply review decision observation_id must match review")
    if decision.experiment_id != review.experiment_id:
        raise ValueError("preapply review decision experiment_id must match review")
    if decision.package_ref != review.package_ref:
        raise ValueError("preapply review decision package_ref must match review")
    if decision.review_ref != PREAPPLY_REVIEW_REF:
        raise ValueError("preapply review decision review_ref must point to preapply review")


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            temp_path = handle.name
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {item.name: _to_jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime values must be timezone-aware")
        return value.isoformat()
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, Mapping):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("float values must be finite")
        return value
    if isinstance(value, int | str | bool) or value is None:
        return value
    raise TypeError(f"unsupported JSON artifact value: {type(value).__name__}")


def _normalize_required_refs(
    refs: Mapping[str, str],
    required_names: Sequence[str],
    field_name: str,
) -> dict[str, str]:
    if not isinstance(refs, Mapping) or not refs:
        raise ValueError(f"{field_name} must be a non-empty mapping")
    normalized: dict[str, str] = {}
    for name, value in refs.items():
        ref_name = _require_non_empty_text(name, f"{field_name} key")
        _reject_promotion_text(ref_name, f"{field_name} key")
        normalized[ref_name] = _require_relative_ref(value, f"{field_name}.{ref_name}")
    for required_ref in required_names:
        if required_ref not in normalized:
            raise ValueError(f"{field_name} missing required ref: {required_ref}")
    return dict(sorted(normalized.items()))


def _normalize_text_sequence(
    values: Sequence[str],
    field_name: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if isinstance(values, str | bytes | bytearray) or not isinstance(values, Sequence):
        raise ValueError(f"{field_name} must be a sequence of strings")
    normalized = tuple(_require_non_empty_text(value, field_name) for value in values)
    if not allow_empty and not normalized:
        raise ValueError(f"{field_name} must not be empty")
    for value in normalized:
        _reject_promotion_text(value, field_name)
    return normalized


def _normalize_json_mapping(value: Mapping[str, Any], field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{field_name} must be a non-empty mapping")
    normalized = _normalize_json_value(value, field_name)
    if not isinstance(normalized, dict):
        raise ValueError(f"{field_name} must be a mapping")
    return normalized


def _normalize_json_value(value: Any, field_name: str) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = _require_non_empty_text(key, f"{field_name} key")
            _reject_promotion_text(key_text, f"{field_name} key")
            normalized[key_text] = _normalize_json_value(item, f"{field_name}.{key_text}")
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_normalize_json_value(item, field_name) for item in value]
    if isinstance(value, str):
        _reject_promotion_text(value, field_name)
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} must contain only finite numbers")
        return value
    if isinstance(value, int | bool) or value is None:
        return value
    raise TypeError(f"{field_name} contains unsupported JSON value: {type(value).__name__}")


def _require_relative_ref(value: Any, field_name: str) -> str:
    ref = _require_non_empty_text(value, field_name)
    ref = normalize_relative_artifact_path(ref)
    if ref.startswith("~"):
        raise ValueError(f"{field_name} must be a relative artifact ref")
    posix_path = PurePosixPath(ref)
    windows_path = PureWindowsPath(ref)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError(f"{field_name} must be a relative artifact ref")
    _reject_promotion_text(ref, field_name)
    return ref


def _require_safe_identifier(value: Any, field_name: str) -> str:
    value = _require_non_empty_text(value, field_name)
    if "/" in value or "\\" in value or value in {".", ".."} or ".." in value:
        raise ValueError(f"{field_name} must not contain path traversal or separators")
    _reject_promotion_text(value, field_name)
    return value


def _require_non_empty_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_timezone_aware_datetime(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")


def _reject_promotion_text(value: str, field_name: str) -> None:
    lowered = value.lower()
    for term in DESIGN_PROMOTION_TERMS:
        if term in lowered:
            raise ValueError(f"{field_name} must not encode runtime promotion term: {term}")


def _require_research_manual_design_root(value: str | Path) -> Path:
    path = Path(value)
    parts = path.parts
    if ".." in parts:
        raise ValueError("manual apply design root must not contain path traversal")
    has_research_artifact_root = any(
        parts[index] == "artifacts" and parts[index + 1] == "research"
        for index in range(len(parts) - 1)
    )
    if not has_research_artifact_root:
        raise ValueError("manual apply design root must be under artifacts/research")
    return path


def _utc_now() -> datetime:
    return datetime.now(UTC)
