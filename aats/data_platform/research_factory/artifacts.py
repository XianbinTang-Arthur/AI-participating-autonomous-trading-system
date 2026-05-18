"""Artifact manifest helpers for Research Factory outputs."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path, PurePath
from typing import Any

from aats.data_platform.research_factory.status import require_valid_status

ALLOWED_ARTIFACT_TYPES = frozenset(
    {
        "experiment",
        "dataset",
        "benchmark",
        "proposal",
        "observation",
        "preapply",
        "preapply_review",
        "manual_apply_design",
        "workflow",
    }
)
REQUIRED_MANIFEST_FIELDS = frozenset(
    {
        "artifact_id",
        "artifact_type",
        "status",
        "started_at",
        "input_refs",
        "output_refs",
        "code_version",
    }
)


def normalize_relative_artifact_path(value: str) -> str:
    """Normalize a manifest path and reject traversal or absolute paths."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("artifact path must be a non-empty string")
    path = PurePath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("artifact path must be relative and stay within artifact root")
    return path.as_posix()


def build_artifact_manifest(
    *,
    artifact_id: str,
    artifact_type: str,
    status: str,
    started_at: datetime | str,
    input_refs: Mapping[str, Any],
    output_refs: Mapping[str, str],
    code_version: str | None,
    finished_at: datetime | str | None = None,
    metrics_ref: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Build and validate a Research Factory artifact manifest."""
    manifest: dict[str, Any] = {
        "artifact_id": artifact_id,
        "artifact_type": artifact_type,
        "status": status,
        "started_at": _format_timestamp(started_at, "started_at"),
        "finished_at": _format_optional_timestamp(finished_at, "finished_at"),
        "input_refs": dict(input_refs),
        "output_refs": dict(output_refs),
        "metrics_ref": metrics_ref,
        "code_version": code_version,
        "notes": notes,
    }
    return validate_artifact_manifest(manifest)


def validate_artifact_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a manifest and return a normalized copy."""
    if not isinstance(manifest, Mapping):
        raise ValueError("artifact manifest must be a mapping")

    missing = sorted(REQUIRED_MANIFEST_FIELDS - set(manifest))
    if missing:
        raise ValueError(f"artifact manifest missing required fields: {', '.join(missing)}")

    artifact_id = _require_safe_identifier(manifest["artifact_id"], "artifact_id")
    artifact_type = _require_artifact_type(manifest["artifact_type"])
    status = require_valid_status(_require_string(manifest["status"], "status"))
    started_at = _format_timestamp(manifest["started_at"], "started_at")
    finished_at = _format_optional_timestamp(manifest.get("finished_at"), "finished_at")
    input_refs = _require_mapping(manifest["input_refs"], "input_refs")
    output_refs = _normalize_output_refs(manifest["output_refs"])
    metrics_ref = manifest.get("metrics_ref")
    if metrics_ref is not None:
        metrics_ref = normalize_relative_artifact_path(_require_string(metrics_ref, "metrics_ref"))
    code_version = manifest.get("code_version")
    if code_version is not None:
        code_version = _require_string(code_version, "code_version")
    notes = manifest.get("notes")
    if notes is not None:
        notes = _require_string(notes, "notes")

    return {
        "artifact_id": artifact_id,
        "artifact_type": artifact_type,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "input_refs": input_refs,
        "output_refs": output_refs,
        "metrics_ref": metrics_ref,
        "code_version": code_version,
        "notes": notes,
    }


def write_artifact_manifest_atomic(path: str | Path, manifest: Mapping[str, Any]) -> None:
    """Atomically write a validated manifest as stable JSON."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    normalized = validate_artifact_manifest(manifest)
    payload = json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            delete=False,
            prefix=f".{target.name}.",
            suffix=".tmp",
        ) as handle:
            temp_path = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def _format_optional_timestamp(value: datetime | str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _format_timestamp(value, field_name)


def _format_timestamp(value: datetime | str, field_name: str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must be timezone-aware")
        return value.isoformat()
    return _require_string(value, field_name)


def _normalize_output_refs(value: Any) -> dict[str, str]:
    output_refs = _require_mapping(value, "output_refs")
    normalized: dict[str, str] = {}
    for key, path in output_refs.items():
        normalized[_require_string(key, "output_refs key")] = normalize_relative_artifact_path(
            _require_string(path, f"output_refs[{key!r}]")
        )
    return normalized


def _require_mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    return dict(value)


def _require_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_safe_identifier(value: Any, field_name: str) -> str:
    value = _require_string(value, field_name)
    if "/" in value or "\\" in value or ".." in value or value in {".", ".."}:
        raise ValueError(f"{field_name} must not contain path traversal or separators")
    return value


def _require_artifact_type(value: Any) -> str:
    value = _require_string(value, "artifact_type")
    if value not in ALLOWED_ARTIFACT_TYPES:
        allowed = ", ".join(sorted(ALLOWED_ARTIFACT_TYPES))
        raise ValueError(f"artifact_type must be one of: {allowed}")
    return value
