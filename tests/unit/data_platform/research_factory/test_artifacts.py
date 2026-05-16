import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aats.data_platform.research_factory.artifacts import (
    build_artifact_manifest,
    normalize_relative_artifact_path,
    validate_artifact_manifest,
    write_artifact_manifest_atomic,
)


def base_manifest() -> dict:
    return build_artifact_manifest(
        artifact_id="exp_20260516_000001",
        artifact_type="experiment",
        status="running",
        started_at=datetime(2026, 5, 16, 6, 0, tzinfo=timezone.utc),
        input_refs={"dataset_version": "v1.0"},
        output_refs={"summary_path": "reports/summary.json"},
        metrics_ref="metrics_snapshot.json",
        code_version="dirty",
        notes="research-only manifest",
    )


def test_build_artifact_manifest_normalizes_datetime_and_paths() -> None:
    manifest = base_manifest()

    assert manifest["started_at"] == "2026-05-16T06:00:00+00:00"
    assert manifest["output_refs"] == {"summary_path": "reports/summary.json"}
    assert manifest["metrics_ref"] == "metrics_snapshot.json"


def test_validate_artifact_manifest_rejects_missing_required_field() -> None:
    manifest = base_manifest()
    del manifest["artifact_id"]

    with pytest.raises(ValueError, match="missing required fields: artifact_id"):
        validate_artifact_manifest(manifest)


def test_validate_artifact_manifest_rejects_output_ref_traversal() -> None:
    manifest = base_manifest()
    manifest["output_refs"] = {"summary_path": "../outside.json"}

    with pytest.raises(ValueError, match="stay within artifact root"):
        validate_artifact_manifest(manifest)


def test_validate_artifact_manifest_rejects_invalid_status() -> None:
    manifest = base_manifest()
    manifest["status"] = "applied"

    with pytest.raises(ValueError, match="invalid research status"):
        validate_artifact_manifest(manifest)


def test_validate_artifact_manifest_rejects_invalid_artifact_type() -> None:
    manifest = base_manifest()
    manifest["artifact_type"] = "active_parameter"

    with pytest.raises(ValueError, match="artifact_type must be one of"):
        validate_artifact_manifest(manifest)


def test_normalize_relative_artifact_path_rejects_absolute_path() -> None:
    with pytest.raises(ValueError, match="relative"):
        normalize_relative_artifact_path("C:/secret/file.json")


def test_write_artifact_manifest_atomic_writes_stable_sorted_json() -> None:
    temp_dir = _workspace_temp_dir()
    try:
        target = temp_dir / "experiment_manifest.json"
        write_artifact_manifest_atomic(target, base_manifest())

        raw = target.read_text(encoding="utf-8")
        assert raw.endswith("\n")
        assert raw.index('"artifact_id"') < raw.index('"artifact_type"')
        assert json.loads(raw)["artifact_id"] == "exp_20260516_000001"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_write_artifact_manifest_atomic_rejects_invalid_manifest() -> None:
    temp_dir = _workspace_temp_dir()
    try:
        target = temp_dir / "experiment_manifest.json"
        manifest = base_manifest()
        manifest["output_refs"] = {"bad": "nested/../../escape.json"}

        with pytest.raises(ValueError, match="stay within artifact root"):
            write_artifact_manifest_atomic(target, manifest)

        assert not target.exists()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _workspace_temp_dir() -> Path:
    path = Path(".pytest_workspace_tmp") / f"research_factory_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    return path
