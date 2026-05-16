"""Research-only experiment recorder with artifact lineage manifests."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Any

from aats.data_platform.research_factory.artifacts import (
    build_artifact_manifest,
    normalize_relative_artifact_path,
    validate_artifact_manifest,
    write_artifact_manifest_atomic,
)
from aats.data_platform.research_factory.metrics.gates import CandidateArtifact
from aats.data_platform.research_factory.recommendations import ResearchRecommendation
from aats.data_platform.research_factory.specs import ExperimentSpec, MetricsSnapshot
from aats.data_platform.research_factory.status import is_terminal_status, require_valid_status

MANIFEST_REF = "experiment_manifest.json"
EXPERIMENT_SPEC_REF = "experiment_spec.json"
METRICS_REF = "metrics_snapshot.json"
FAILURE_REF = "failure.json"
CANDIDATE_REF = "candidate_artifact.json"
RECOMMENDATION_REF = "research_recommendation.json"
FORBIDDEN_OUTPUT_REF_NAME_TERMS = (
    "active_parameter",
    "active_parameters",
    "apply",
    "live_order",
    "okx_write",
    "operator_write",
    "production_config",
)

_SECRET_MARKERS = (
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "okx",
    "password",
    "passwd",
    "secret",
    "token",
)


class ExperimentRecorder:
    """Persist Research Factory experiment artifacts under a research-only root."""

    def __init__(
        self,
        root: str | Path = Path("artifacts") / "research" / "research_factory" / "experiments",
        *,
        code_version: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = _require_research_recorder_root(root)
        self.code_version = code_version
        self._clock = clock or _utc_now

    def start(self, experiment_spec: ExperimentSpec) -> dict[str, Any]:
        """Create an experiment directory, write the spec, and mark it running."""
        if not isinstance(experiment_spec, ExperimentSpec):
            raise ValueError("experiment_spec must be an ExperimentSpec")

        experiment_dir = self._experiment_dir(experiment_spec.experiment_id)
        if experiment_dir.exists():
            raise ValueError(f"experiment {experiment_spec.experiment_id!r} already exists")

        experiment_dir.mkdir(parents=True)
        started_at = self._now()
        _write_json_atomic(experiment_dir / EXPERIMENT_SPEC_REF, _to_jsonable(experiment_spec))

        manifest = build_artifact_manifest(
            artifact_id=experiment_spec.experiment_id,
            artifact_type="experiment",
            status="running",
            started_at=started_at,
            input_refs=_experiment_input_refs(experiment_spec),
            output_refs={"experiment_spec": EXPERIMENT_SPEC_REF},
            code_version=self.code_version,
        )
        self._write_manifest(experiment_spec.experiment_id, manifest)
        return manifest

    def replace_experiment_spec(self, experiment_spec: ExperimentSpec) -> dict[str, Any]:
        """Replace the stored experiment spec before the experiment is terminal."""
        if not isinstance(experiment_spec, ExperimentSpec):
            raise ValueError("experiment_spec must be an ExperimentSpec")
        manifest = self._read_manifest(experiment_spec.experiment_id)
        if is_terminal_status(manifest["status"]):
            raise ValueError(f"experiment {experiment_spec.experiment_id!r} is already terminal")

        experiment_dir = self._experiment_dir(experiment_spec.experiment_id)
        _write_json_atomic(experiment_dir / EXPERIMENT_SPEC_REF, _to_jsonable(experiment_spec))
        output_refs = dict(manifest["output_refs"])
        output_refs["experiment_spec"] = EXPERIMENT_SPEC_REF
        updated = build_artifact_manifest(
            artifact_id=manifest["artifact_id"],
            artifact_type=manifest["artifact_type"],
            status=manifest["status"],
            started_at=manifest["started_at"],
            finished_at=manifest.get("finished_at"),
            input_refs=_experiment_input_refs(experiment_spec),
            output_refs=output_refs,
            metrics_ref=manifest.get("metrics_ref"),
            code_version=manifest.get("code_version"),
            notes=manifest.get("notes"),
        )
        self._write_manifest(experiment_spec.experiment_id, updated)
        return updated

    def record_metrics(self, experiment_id: str, metrics: MetricsSnapshot) -> dict[str, Any]:
        """Write a metrics snapshot and attach it to the experiment manifest."""
        if not isinstance(metrics, MetricsSnapshot):
            raise ValueError("metrics must be a MetricsSnapshot")

        manifest = self._read_manifest(experiment_id)
        if is_terminal_status(manifest["status"]):
            raise ValueError(f"experiment {experiment_id!r} is already terminal")

        experiment_dir = self._experiment_dir(experiment_id)
        _write_json_atomic(experiment_dir / METRICS_REF, _to_jsonable(metrics))
        output_refs = dict(manifest["output_refs"])
        output_refs["metrics_snapshot"] = METRICS_REF

        updated = build_artifact_manifest(
            artifact_id=manifest["artifact_id"],
            artifact_type=manifest["artifact_type"],
            status=manifest["status"],
            started_at=manifest["started_at"],
            finished_at=manifest.get("finished_at"),
            input_refs=manifest["input_refs"],
            output_refs=output_refs,
            metrics_ref=METRICS_REF,
            code_version=manifest.get("code_version"),
            notes=manifest.get("notes"),
        )
        self._write_manifest(experiment_id, updated)
        return updated

    def record_candidate(self, experiment_id: str, candidate: CandidateArtifact) -> dict[str, Any]:
        """Write a research-only candidate artifact and attach it to the manifest."""
        if not isinstance(candidate, CandidateArtifact):
            raise ValueError("candidate must be a CandidateArtifact")
        if candidate.experiment_id != experiment_id:
            raise ValueError("candidate experiment_id must match the recorder experiment_id")

        manifest = self._read_manifest(experiment_id)
        if is_terminal_status(manifest["status"]):
            raise ValueError(f"experiment {experiment_id!r} is already terminal")
        if not manifest.get("metrics_ref"):
            raise ValueError("metrics must be recorded before candidate artifact generation")

        experiment_dir = self._experiment_dir(experiment_id)
        _write_json_atomic(experiment_dir / CANDIDATE_REF, _to_jsonable(candidate))
        output_refs = dict(manifest["output_refs"])
        output_refs["candidate_artifact"] = CANDIDATE_REF

        updated = build_artifact_manifest(
            artifact_id=manifest["artifact_id"],
            artifact_type=manifest["artifact_type"],
            status=manifest["status"],
            started_at=manifest["started_at"],
            finished_at=manifest.get("finished_at"),
            input_refs=manifest["input_refs"],
            output_refs=output_refs,
            metrics_ref=manifest.get("metrics_ref"),
            code_version=manifest.get("code_version"),
            notes=manifest.get("notes"),
        )
        self._write_manifest(experiment_id, updated)
        return updated

    def record_recommendation(
        self,
        experiment_id: str,
        recommendation: ResearchRecommendation,
    ) -> dict[str, Any]:
        """Write a research-only recommendation evidence package."""
        if not isinstance(recommendation, ResearchRecommendation):
            raise ValueError("recommendation must be a ResearchRecommendation")
        if recommendation.experiment_id != experiment_id:
            raise ValueError("recommendation experiment_id must match the recorder experiment_id")

        manifest = self._read_manifest(experiment_id)
        if is_terminal_status(manifest["status"]):
            raise ValueError(f"experiment {experiment_id!r} is already terminal")
        if not manifest.get("metrics_ref"):
            raise ValueError("metrics must be recorded before recommendation generation")
        output_refs = dict(manifest["output_refs"])
        if "candidate_artifact" not in output_refs:
            raise ValueError("candidate artifact must be recorded before recommendation generation")

        experiment_dir = self._experiment_dir(experiment_id)
        _write_json_atomic(experiment_dir / RECOMMENDATION_REF, _to_jsonable(recommendation))
        output_refs["research_recommendation"] = RECOMMENDATION_REF

        updated = build_artifact_manifest(
            artifact_id=manifest["artifact_id"],
            artifact_type=manifest["artifact_type"],
            status=manifest["status"],
            started_at=manifest["started_at"],
            finished_at=manifest.get("finished_at"),
            input_refs=manifest["input_refs"],
            output_refs=output_refs,
            metrics_ref=manifest.get("metrics_ref"),
            code_version=manifest.get("code_version"),
            notes=manifest.get("notes"),
        )
        self._write_manifest(experiment_id, updated)
        return updated

    def record_output_ref(
        self,
        experiment_id: str,
        ref_name: str,
        output_ref: str,
    ) -> dict[str, Any]:
        """Attach an additional relative output artifact ref to the manifest."""
        ref_name = _require_research_output_ref_name(ref_name)
        if not isinstance(output_ref, str) or not output_ref.strip():
            raise ValueError("output_ref must be a non-empty string")

        manifest = self._read_manifest(experiment_id)
        if is_terminal_status(manifest["status"]):
            raise ValueError(f"experiment {experiment_id!r} is already terminal")
        output_refs = dict(manifest["output_refs"])
        output_refs[ref_name] = output_ref

        updated = build_artifact_manifest(
            artifact_id=manifest["artifact_id"],
            artifact_type=manifest["artifact_type"],
            status=manifest["status"],
            started_at=manifest["started_at"],
            finished_at=manifest.get("finished_at"),
            input_refs=manifest["input_refs"],
            output_refs=output_refs,
            metrics_ref=manifest.get("metrics_ref"),
            code_version=manifest.get("code_version"),
            notes=manifest.get("notes"),
        )
        self._write_manifest(experiment_id, updated)
        return updated

    def record_json_artifact(
        self,
        experiment_id: str,
        ref_name: str,
        output_ref: str,
        payload: Any,
    ) -> dict[str, Any]:
        """Write an additional JSON artifact and attach it to the manifest."""
        normalized_ref = normalize_relative_artifact_path(output_ref)
        manifest = self._read_manifest(experiment_id)
        if is_terminal_status(manifest["status"]):
            raise ValueError(f"experiment {experiment_id!r} is already terminal")
        target = self._experiment_dir(experiment_id) / normalized_ref
        _write_json_atomic(target, _to_jsonable(payload))
        return self.record_output_ref(experiment_id, ref_name, normalized_ref)

    def finish(self, experiment_id: str, status: str) -> dict[str, Any]:
        """Move an experiment to a terminal status without writing failure details."""
        status = require_valid_status(status)
        if not is_terminal_status(status):
            raise ValueError("finish status must be terminal")
        return self._finish_with_outputs(experiment_id, status=status, output_refs=None)

    def fail(self, experiment_id: str, reason: str) -> dict[str, Any]:
        """Write a redacted failure artifact and mark the experiment failed."""
        reason = _redact_failure_reason(reason)
        experiment_dir = self._experiment_dir(experiment_id)
        failure_payload = {
            "recorded_at": self._now().isoformat(),
            "reason": reason,
            "redacted": reason == "[REDACTED]",
        }
        _write_json_atomic(experiment_dir / FAILURE_REF, failure_payload)
        return self._finish_with_outputs(
            experiment_id,
            status="failed",
            output_refs={"failure": FAILURE_REF},
        )

    def _finish_with_outputs(
        self,
        experiment_id: str,
        *,
        status: str,
        output_refs: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        manifest = self._read_manifest(experiment_id)
        if is_terminal_status(manifest["status"]):
            raise ValueError(f"experiment {experiment_id!r} is already terminal")

        merged_output_refs = dict(manifest["output_refs"])
        if output_refs:
            merged_output_refs.update(output_refs)

        updated = build_artifact_manifest(
            artifact_id=manifest["artifact_id"],
            artifact_type=manifest["artifact_type"],
            status=status,
            started_at=manifest["started_at"],
            finished_at=self._now(),
            input_refs=manifest["input_refs"],
            output_refs=merged_output_refs,
            metrics_ref=manifest.get("metrics_ref"),
            code_version=manifest.get("code_version"),
            notes=manifest.get("notes"),
        )
        self._write_manifest(experiment_id, updated)
        return updated

    def _experiment_dir(self, experiment_id: str) -> Path:
        return self.root / _require_safe_experiment_id(experiment_id)

    def _now(self) -> datetime:
        return self._clock()

    def _manifest_path(self, experiment_id: str) -> Path:
        return self._experiment_dir(experiment_id) / MANIFEST_REF

    def _read_manifest(self, experiment_id: str) -> dict[str, Any]:
        path = self._manifest_path(experiment_id)
        if not path.exists():
            raise ValueError(f"experiment {experiment_id!r} has no manifest")
        with path.open("r", encoding="utf-8") as handle:
            return validate_artifact_manifest(json.load(handle))

    def _write_manifest(self, experiment_id: str, manifest: Mapping[str, Any]) -> None:
        write_artifact_manifest_atomic(self._manifest_path(experiment_id), manifest)


def _experiment_input_refs(experiment_spec: ExperimentSpec) -> dict[str, Any]:
    return {
        "dataset_id": experiment_spec.dataset.dataset_id,
        "dataset_version": experiment_spec.dataset.dataset_version,
        "feature_names": [feature.name for feature in experiment_spec.features],
        "governance_mode": experiment_spec.governance_mode,
        "label_name": experiment_spec.label.name,
        "model_ref": experiment_spec.model_ref,
    }


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
        return {field.name: _to_jsonable(getattr(value, field.name)) for field in fields(value)}
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
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    raise TypeError(f"unsupported JSON artifact value: {type(value).__name__}")


def _redact_failure_reason(reason: str) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("failure reason must be a non-empty string")
    lowered = reason.lower()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        return "[REDACTED]"
    return reason


def _require_safe_experiment_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("experiment_id must be a non-empty string")
    if "/" in value or "\\" in value or value in {".", ".."} or ".." in value:
        raise ValueError("experiment_id must not contain path traversal or separators")
    return value


def _require_research_output_ref_name(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("ref_name must be a non-empty string")
    lowered = value.lower()
    for forbidden in FORBIDDEN_OUTPUT_REF_NAME_TERMS:
        if forbidden in lowered:
            raise ValueError(f"ref_name must remain research-only; forbidden term: {forbidden}")
    return value


def _require_research_recorder_root(value: str | Path) -> Path:
    path = Path(value)
    parts = path.parts
    if ".." in parts:
        raise ValueError("recorder root must not contain path traversal")
    has_research_artifact_root = any(
        parts[index] == "artifacts" and parts[index + 1] == "research"
        for index in range(len(parts) - 1)
    )
    if not has_research_artifact_root:
        raise ValueError("recorder root must be under artifacts/research")
    return path


def _utc_now() -> datetime:
    return datetime.now(UTC)
