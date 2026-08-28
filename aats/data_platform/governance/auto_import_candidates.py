"""自动导入最新 Step 3 参数候选到治理层 Registry.

解决的问题:
  Step 3 产出受 round manifest 绑定的完整参数候选集合，
  但此前从未自动导入到 current_parameter_registry.json，
  旧链路只保存了部分参数，entry_threshold、close_threshold 等治理参数可能丢失。

使用方式:
  # CLI 独立调用
  python -m aats.data_platform.governance.auto_import_candidates --run

  # 被 governance_cycle workflow 或 full pipeline 自动调用
  from aats.data_platform.governance.auto_import_candidates import (
      auto_import_latest_candidates,
  )
  result = auto_import_latest_candidates(project_root)
"""

from __future__ import annotations

import json
import hashlib
import logging
import pathlib
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from ._db_util import (
    ADVISORY_LOCK_KEYS,
    has_explicit_governance_db_configuration,
    try_governance_db,
)
from ._exceptions import DBConflictError, DBUnavailableError
from .parameter_identity import (
    parameter_set_immutable_identity,
    parameter_values_fingerprint,
)
from .parameter_registry import (
    add_parameter_set,
    deprecate_parameter_set,
    find_parameter_sets,
    import_from_parameter_candidates,
    load_registry,
    save_registry,
)
from .research_artifact_contract import (
    decode_strict_json_artifact,
    read_stable_regular_round_file,
    require_regular_round_file,
    resolve_formal_round_dir,
    validate_calibration_child_artifacts,
    validate_scan_child_artifacts,
)
from .snapshot_db import (
    ROUND_PHASE_STEP2,
    ROUND_PHASE_STEP3,
    load_research_round_snapshot,
    research_round_snapshot_fingerprint,
)
from .typed_json_identity import typed_json_sha256

log = logging.getLogger(__name__)

_STEP3_ARTIFACT_DIR = "artifacts/research/step3_rounds"
_REGISTRY_PATH = "artifacts/governance/current_parameter_registry.json"
_STEP3_ROUND_ID_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{8}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_STEP3_COMBO_KEYS = frozenset(
    {
        "independent_15m",
        "independent_1h",
        "directional_15m",
        "directional_1h",
    }
)
_EXPECTED_STEP2_CALIBRATION_KEYS = (
    "independent_1h",
    "directional_15m",
    "directional_1h",
)
_EXPECTED_STEP2_SCAN_KEYS = (
    "independent_15m",
    "independent_1h",
    "directional_15m",
    "directional_1h",
)
_EXPECTED_STEP2_COMBO_KEYS = frozenset(_EXPECTED_STEP2_CALIBRATION_KEYS)
_EXPECTED_STEP3_CALIBRATION_KEYS = (
    "independent_15m_expanded",
    "independent_1h_expanded",
)
_EXPECTED_STEP2_CALIBRATION_TOPOLOGY = {
    "independent_1h": (
        "independent",
        "1H",
        ("scale_calibration", "cost_sensitivity", "confirm_ticks"),
    ),
    "directional_15m": (
        "directional",
        "15m",
        (
            "scale_calibration",
            "cost_sensitivity",
            "confirm_ticks",
            "trend_weight",
            "return_clamp",
        ),
    ),
    "directional_1h": (
        "directional",
        "1H",
        (
            "scale_calibration",
            "cost_sensitivity",
            "confirm_ticks",
            "trend_weight",
            "return_clamp",
        ),
    ),
}
_EXPECTED_STEP2_SCAN_TOPOLOGY = {
    "independent_15m": ("independent", "15m"),
    "independent_1h": ("independent", "1H"),
    "directional_15m": ("directional", "15m"),
    "directional_1h": ("directional", "1H"),
}
_EXPECTED_STEP3_CALIBRATION_TOPOLOGY = {
    "independent_15m_expanded": (
        "independent",
        "15m",
        (
            "entry_threshold",
            "close_threshold",
            "de_risk_edge",
            "failed_thesis_edge",
            "timing",
            "cost_buffer",
        ),
    ),
    "independent_1h_expanded": (
        "independent",
        "1H",
        (
            "entry_threshold",
            "close_threshold",
            "de_risk_edge",
            "failed_thesis_edge",
            "timing",
            "cost_buffer",
        ),
    ),
}
_PARAMETER_IMPORT_LOCK_KEY = ADVISORY_LOCK_KEYS["parameter_candidate_import"]

# Public status contract shared by the standalone CLI and full-pipeline adapter.
AUTO_IMPORT_SUCCESS_STATUSES = frozenset(
    {
        "imported",
        "recovered_partial_import",
        "reconciled_import",
        "concurrent_transition_preserved",
        "already_imported",
    }
)


class _ParameterImportLockBusy(RuntimeError):
    """Another managed-DB importer owns the global Step 3 import lock."""


@dataclass(frozen=True, slots=True)
class ValidatedStep3CandidateArtifact:
    """One exact Step 3 candidate proven by the formal importer contract."""

    path: pathlib.Path
    candidate_bytes: bytes
    candidate_sha256: str
    payload: dict[str, Any]
    metadata: dict[str, Any]


def _managed_step3_snapshot_matches(
    project_root: pathlib.Path,
    *,
    candidate_path: pathlib.Path,
    candidate_bytes: bytes,
    candidate_sha256: str,
    candidate_payload: dict[str, Any],
    metadata: dict[str, Any],
) -> bool:
    """Require one exact immutable DB root for a managed Step 3 artifact."""

    if not has_explicit_governance_db_configuration(project_root):
        return True
    round_id = metadata.get("round_id")
    if not isinstance(round_id, str):
        return False
    try:
        snapshot = load_research_round_snapshot(
            round_id=round_id,
            project_root=project_root,
            require_managed_db_truth=True,
        )
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("data_source") != "db"
            or snapshot.get("round_id") != round_id
            or snapshot.get("phase") != ROUND_PHASE_STEP3
            or snapshot.get("status") != metadata.get("status")
        ):
            return False
        round_dir = candidate_path.parent
        manifest_path = round_dir / "round_manifest.json"
        stable_candidate_bytes = read_stable_regular_round_file(
            candidate_path,
            parent=round_dir,
        )
        manifest_bytes = read_stable_regular_round_file(
            manifest_path,
            parent=round_dir,
        )
        manifest = decode_strict_json_artifact(
            manifest_bytes,
            expected_type=dict,
        )
        snapshot_summary = snapshot.get("summary")
        if (
            stable_candidate_bytes != candidate_bytes
            or hashlib.sha256(stable_candidate_bytes).hexdigest()
            != candidate_sha256
            or not isinstance(manifest, dict)
            or typed_json_sha256(snapshot.get("manifest"))
            != typed_json_sha256(manifest)
            or not isinstance(snapshot_summary, dict)
            or typed_json_sha256(
                snapshot_summary.get("parameter_candidates_merged")
            )
            != typed_json_sha256(candidate_payload)
        ):
            return False
        artifacts = snapshot.get("artifacts")
        if not isinstance(artifacts, dict):
            return False
        expected_round_dir = round_dir.resolve(strict=True)
        if (
            pathlib.Path(str(snapshot.get("round_path"))).resolve(strict=True)
            != expected_round_dir
            or pathlib.Path(str(artifacts.get("round_dir"))).resolve(strict=True)
            != expected_round_dir
            or pathlib.Path(str(artifacts.get("manifest_path"))).resolve(strict=True)
            != manifest_path
            or pathlib.Path(str(artifacts.get("candidate_path"))).resolve(strict=True)
            != candidate_path
            or artifacts.get("manifest_sha256")
            != hashlib.sha256(manifest_bytes).hexdigest()
            or artifacts.get("manifest_size_bytes") != len(manifest_bytes)
            or artifacts.get("manifest_utf8") != manifest_bytes.decode("utf-8")
            or artifacts.get("candidate_sha256") != candidate_sha256
            or artifacts.get("candidate_size_bytes") != len(candidate_bytes)
            or artifacts.get("candidate_utf8") != candidate_bytes.decode("utf-8")
            or artifacts.get("step2_round_id")
            != metadata.get("source_step2_round_id")
            or artifacts.get("step2_candidate_sha256")
            != metadata.get("source_step2_candidate_sha256")
        ):
            return False
        step2_round_id = artifacts.get("step2_round_id")
        if not isinstance(step2_round_id, str):
            return False
        step2_snapshot = load_research_round_snapshot(
            round_id=step2_round_id,
            project_root=project_root,
            require_managed_db_truth=True,
        )
        step2_manifest = (
            step2_snapshot.get("manifest")
            if isinstance(step2_snapshot, dict)
            else None
        )
        step2_digests = (
            step2_manifest.get("artifact_sha256")
            if isinstance(step2_manifest, dict)
            else None
        )
        if (
            not isinstance(step2_snapshot, dict)
            or step2_snapshot.get("data_source") != "db"
            or step2_snapshot.get("phase") != ROUND_PHASE_STEP2
            or step2_snapshot.get("round_id") != step2_round_id
            or step2_snapshot.get("status") != "succeeded"
            or not isinstance(step2_digests, dict)
            or step2_digests.get("parameter_candidates.json")
            != artifacts.get("step2_candidate_sha256")
            or artifacts.get("step2_snapshot_sha256")
            != research_round_snapshot_fingerprint(step2_snapshot)
        ):
            return False
    except (
        DBUnavailableError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return False
    return True


def _manifest_result_set_is_valid(
    results: Any,
    *,
    key_field: str,
    expected_keys: tuple[str, ...],
    require_complete_success: bool,
    expected_topology: dict[str, tuple[Any, ...]] | None = None,
) -> bool:
    if not isinstance(results, list) or not all(
        isinstance(item, dict) for item in results
    ):
        return False
    keys = [item.get(key_field) for item in results]
    statuses = [item.get("status") for item in results]
    if (
        not all(isinstance(key, str) and key for key in keys)
        or len(set(keys)) != len(keys)
        or not set(keys).issubset(expected_keys)
        or not all(
            status in {"succeeded", "partial_success", "failed"}
            for status in statuses
        )
    ):
        return False
    if require_complete_success and not (
            len(results) == len(expected_keys)
            and set(keys) == set(expected_keys)
            and all(status == "succeeded" for status in statuses)
    ):
        return False
    if expected_topology is None:
        return True
    for item in results:
        key = item[key_field]
        expected = expected_topology[key]
        if item.get("family") != expected[0] or item.get("timeframe") != expected[1]:
            return False
        if key_field == "round_key" and item.get("status") == "succeeded":
            batches = item.get("batches")
            if not isinstance(batches, list) or not all(
                isinstance(batch, dict) for batch in batches
            ):
                return False
            batch_keys = [batch.get("key") for batch in batches]
            if (
                len(batch_keys) != len(expected[2])
                or len(set(batch_keys)) != len(batch_keys)
                or set(batch_keys) != set(expected[2])
                or any(batch.get("status") != "succeeded" for batch in batches)
                or any(
                    not isinstance(batch.get("batch_run_id"), str)
                    or not isinstance(batch.get("batch_dir"), str)
                    or not isinstance(batch.get("summary_sha256"), str)
                    or _SHA256_RE.fullmatch(batch["summary_sha256"]) is None
                    or type(batch.get("summary_size_bytes")) is not int
                    or batch["summary_size_bytes"] <= 0
                    for batch in batches
                )
            ):
                return False
        if key_field == "scan_key" and item.get("status") == "succeeded":
            if (
                not isinstance(item.get("scan_run_id"), str)
                or not isinstance(item.get("scan_dir"), str)
                or not isinstance(item.get("comparison_sha256"), str)
                or _SHA256_RE.fullmatch(item["comparison_sha256"]) is None
                or type(item.get("comparison_size_bytes")) is not int
                or item["comparison_size_bytes"] <= 0
                or not isinstance(item.get("window"), dict)
                or not isinstance(item.get("dataset_version"), str)
                or not isinstance(item.get("grid_sha256"), str)
                or _SHA256_RE.fullmatch(item["grid_sha256"]) is None
            ):
                return False
    return True


def _empty_result(*, status: str = "no_candidates") -> dict[str, Any]:
    return {
        "status": status,
        "imported_count": 0,
        "published_count": 0,
        "deprecated_count": 0,
        "deprecation_skipped_count": 0,
        "status_conflict_count": 0,
        "source_file": None,
        "source_round_id": None,
        "source_candidate_sha256": None,
        "round_status": None,
        "effective_initial_status": None,
        "parameter_sets": [],
    }


def _resolve_expected_parameter_sets(
    registry: dict[str, Any],
    expected_parameter_sets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return canonical expected rows without masking lifecycle progression."""

    canonical_by_id = {
        parameter_set.get("parameter_set_id"): parameter_set
        for parameter_set in registry.get("parameter_sets", [])
    }
    resolved: list[dict[str, Any]] = []
    for expected in expected_parameter_sets:
        canonical = canonical_by_id.get(expected["parameter_set_id"])
        if (
            canonical is None
            or parameter_set_immutable_identity(canonical)
            != parameter_set_immutable_identity(expected)
        ):
            raise DBConflictError("parameter_candidate_publication_readback_conflict")
        resolved.append(canonical)
    return resolved


def _summarize_parameter_sets(
    parameter_sets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "id": parameter_set["parameter_set_id"],
            "family": parameter_set["family"],
            "timeframe": parameter_set["timeframe"],
            "param_count": len(parameter_set.get("values", {})),
            "status": parameter_set["status"],
        }
        for parameter_set in parameter_sets
    ]


def _publish_managed_round_candidates(
    expected_parameter_sets: list[dict[str, Any]],
) -> int:
    """Publish a complete draft round to candidate in one DB transaction."""

    engine, ok = try_governance_db()
    if not ok or engine is None:
        raise DBUnavailableError(
            "governance DB unavailable while publishing parameter candidates"
        )
    try:
        from sqlalchemy.exc import IntegrityError, OperationalError
        from sqlalchemy.orm import Session

        from .parameter_sets_db import db_publish_parameter_set_candidates

        with Session(engine) as session, session.begin():
            return db_publish_parameter_set_candidates(
                session,
                expected_parameter_sets=expected_parameter_sets,
            )
    except DBConflictError:
        raise
    except IntegrityError as exc:
        raise DBConflictError(
            "parameter_candidate_publication_constraint_violation"
        ) from exc
    except OperationalError as exc:
        raise DBUnavailableError(
            "governance DB unavailable while publishing parameter candidates"
        ) from exc
    finally:
        engine.dispose()


def _refresh_registry_mirror_if_needed(
    registry: dict[str, Any],
    registry_path: pathlib.Path,
) -> bool:
    """Repair a missing/stale audit mirror without rewriting an exact mirror."""

    try:
        with registry_path.open(encoding="utf-8") as handle:
            mirrored = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        mirrored = None
    if (
        isinstance(mirrored, dict)
        and mirrored.get("parameter_sets") == registry.get("parameter_sets")
    ):
        return False
    save_registry(registry, registry_path)
    return True


@contextmanager
def _parameter_candidate_import_lock(
    project_root: pathlib.Path,
) -> Iterator[bool]:
    """Serialize the complete Step 3 import against the managed DB truth source.

    The lock is session-level because the existing registry helpers intentionally
    use their own short transactions.  Keeping this dedicated connection open
    protects artifact selection, canonical DB reread, inserts and lifecycle CAS
    updates as one importer-level critical section.
    """

    db_is_managed = has_explicit_governance_db_configuration(project_root)
    engine, ok = try_governance_db()
    if not ok or engine is None:
        if db_is_managed:
            raise DBUnavailableError(
                "governance DB unavailable while acquiring parameter import lock"
            )
        yield False
        return

    connection = None
    acquired = False
    try:
        try:
            from sqlalchemy import text

            connection = engine.connect()
            acquired = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": _PARAMETER_IMPORT_LOCK_KEY},
                ).scalar_one()
            )
            connection.commit()
            if not acquired:
                raise _ParameterImportLockBusy(
                    "parameter_candidate_import_lock_busy"
                )
        except _ParameterImportLockBusy:
            raise
        except Exception as exc:
            raise DBUnavailableError(
                "governance DB error while acquiring parameter import lock"
            ) from exc
        yield True
    finally:
        if connection is not None:
            if acquired:
                try:
                    from sqlalchemy import text

                    if connection.in_transaction():
                        connection.rollback()
                    connection.execute(
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": _PARAMETER_IMPORT_LOCK_KEY},
                    )
                    connection.commit()
                except Exception as exc:  # pragma: no cover - defensive
                    log.warning(
                        "parameter candidate import lock release failed (%s)",
                        type(exc).__name__,
                    )
            connection.close()
        engine.dispose()


def _parse_manifest_time(value: Any, *, field: str) -> datetime:
    """Parse an explicitly timezone-aware manifest timestamp into UTC."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"step3_manifest_{field}_missing")
    token = value.strip()
    if token.endswith("Z"):
        token = token[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(token)
    except ValueError as exc:
        raise ValueError(f"step3_manifest_{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"step3_manifest_{field}_timezone_missing")
    return parsed.astimezone(timezone.utc)


def _load_round_manifest(
    project_root: pathlib.Path,
    *,
    round_id: str,
    expected_symbol: str,
    expected_candidate_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Load a completed Step 3 manifest bound to one round and symbol."""

    if not _STEP3_ROUND_ID_RE.fullmatch(round_id):
        return None
    manifest_path = (
        project_root / _STEP3_ARTIFACT_DIR / round_id / "round_manifest.json"
    )
    try:
        round_dir, resolved_project_root = resolve_formal_round_dir(
            manifest_path.parent,
            phase_dir_name="step3_rounds",
        )
        if resolved_project_root != project_root.resolve():
            return None
        manifest = decode_strict_json_artifact(
            require_regular_round_file(
                manifest_path,
                parent=round_dir,
            ),
            expected_type=dict,
        )
        if manifest.get("round_id") != round_id:
            return None
        if manifest.get("schema_version") != "aats.step3_round.v1":
            return None
        if manifest.get("symbol") != expected_symbol:
            return None
        if manifest.get("phase") != "step3":
            return None
        if manifest.get("status") not in {"succeeded", "partial_success"}:
            return None
        require_complete_step3 = manifest.get("status") == "succeeded"
        if not _manifest_result_set_is_valid(
            manifest.get("calibrations"),
            key_field="round_key",
            expected_keys=_EXPECTED_STEP3_CALIBRATION_KEYS,
            require_complete_success=require_complete_step3,
            expected_topology=_EXPECTED_STEP3_CALIBRATION_TOPOLOGY,
        ) or not _manifest_result_set_is_valid(
            manifest.get("scans"),
            key_field="scan_key",
            expected_keys=(),
            require_complete_success=require_complete_step3,
        ):
            return None
        dataset_version = manifest.get("dataset_version")
        if (
            not isinstance(dataset_version, str)
            or not dataset_version.strip()
            or dataset_version != dataset_version.strip()
        ):
            return None
        manifest_scope = manifest.get("scope")
        if (
            not isinstance(manifest_scope, dict)
            or manifest_scope.get("symbol") != expected_symbol
        ):
            return None
        combo_keys = manifest_scope.get("combo_keys")
        combo_count = manifest_scope.get("combo_count")
        window = manifest_scope.get("window")
        if (
            not isinstance(combo_keys, list)
            or not combo_keys
            or len(combo_keys) != len(set(combo_keys))
            or not set(combo_keys).issubset(_EXPECTED_STEP3_COMBO_KEYS)
            or combo_count != len(combo_keys)
            or not isinstance(window, dict)
            or set(window) != {"start", "end"}
            or not all(isinstance(window.get(key), str) and window[key].strip()
                       for key in ("start", "end"))
            or (
                manifest.get("status") == "succeeded"
                and set(combo_keys) != _EXPECTED_STEP3_COMBO_KEYS
            )
        ):
            return None
        validate_calibration_child_artifacts(
            round_dir=round_dir,
            calibrations=manifest.get("calibrations"),
            expected_topology=_EXPECTED_STEP3_CALIBRATION_TOPOLOGY,
            symbol=expected_symbol,
            dataset_version=dataset_version,
            window=window,
        )
        input_refs = manifest.get("input_refs")
        if (
            not isinstance(input_refs, dict)
            or input_refs.get("dataset_version") != dataset_version
            or input_refs.get("window") != window
        ):
            return None
        step2_ref = input_refs.get("step2")
        if not isinstance(step2_ref, dict):
            return None
        step2_round_id = step2_ref.get("round_id")
        step2_sha = step2_ref.get("candidate_sha256")
        if manifest.get("status") == "succeeded":
            if (
                step2_ref.get("status") != "succeeded"
                or step2_ref.get("symbol") != expected_symbol
                or step2_ref.get("dataset_version") != dataset_version
                or step2_ref.get("window") != window
                or not isinstance(step2_round_id, str)
                or not _STEP3_ROUND_ID_RE.fullmatch(step2_round_id)
                or not isinstance(step2_sha, str)
                or not _SHA256_RE.fullmatch(step2_sha)
            ):
                return None
            step2_path = (
                project_root
                / "artifacts"
                / "research"
                / "step2_rounds"
                / step2_round_id
                / "parameter_candidates.json"
            )
            step2_round_dir, step2_project_root = resolve_formal_round_dir(
                step2_path.parent,
                phase_dir_name="step2_rounds",
            )
            if step2_project_root != project_root.resolve():
                return None
            step2_manifest_path = step2_path.with_name("round_manifest.json")
            step2_manifest = decode_strict_json_artifact(
                require_regular_round_file(
                    step2_manifest_path,
                    parent=step2_round_dir,
                ),
                expected_type=dict,
            )
            if (
                not isinstance(step2_manifest, dict)
                or step2_manifest.get("schema_version")
                != "aats.step2_round.v1"
                or step2_manifest.get("phase") != "step2"
                or step2_manifest.get("round_id") != step2_round_id
                or step2_manifest.get("status") != "succeeded"
                or step2_manifest.get("symbol") != expected_symbol
                or step2_manifest.get("dataset_version") != dataset_version
                or not _manifest_result_set_is_valid(
                    step2_manifest.get("calibrations"),
                    key_field="round_key",
                    expected_keys=_EXPECTED_STEP2_CALIBRATION_KEYS,
                    require_complete_success=True,
                    expected_topology=_EXPECTED_STEP2_CALIBRATION_TOPOLOGY,
                )
                or not _manifest_result_set_is_valid(
                    step2_manifest.get("scans"),
                    key_field="scan_key",
                    expected_keys=_EXPECTED_STEP2_SCAN_KEYS,
                    require_complete_success=True,
                    expected_topology=_EXPECTED_STEP2_SCAN_TOPOLOGY,
                )
            ):
                return None
            step2_scope = step2_manifest.get("scope")
            step2_input_refs = step2_manifest.get("input_refs")
            step2_digests = step2_manifest.get("artifact_sha256")
            step2_sizes = step2_manifest.get("artifact_size_bytes")
            step2_bytes = require_regular_round_file(
                step2_path,
                parent=step2_round_dir,
            )
            step2_candidate = decode_strict_json_artifact(
                step2_bytes,
                expected_type=dict,
            )
            step2_candidate_scope = (
                step2_candidate.get("scope")
                if isinstance(step2_candidate, dict)
                else None
            )
            step2_candidates = (
                step2_candidate.get("candidates")
                if isinstance(step2_candidate, dict)
                else None
            )
            step2_pending = (
                step2_candidate.get("pending_validation")
                if isinstance(step2_candidate, dict)
                else None
            )
            step2_manifest_keys = (
                step2_scope.get("combo_keys")
                if isinstance(step2_scope, dict)
                else None
            )
            step2_candidate_keys = (
                step2_candidate_scope.get("combo_keys")
                if isinstance(step2_candidate_scope, dict)
                else None
            )
            if (
                not isinstance(step2_scope, dict)
                or step2_scope.get("symbol") != expected_symbol
                or step2_scope.get("window") != window
                or not isinstance(step2_manifest_keys, list)
                or len(step2_manifest_keys) != len(set(step2_manifest_keys))
                or set(step2_manifest_keys) != _EXPECTED_STEP2_COMBO_KEYS
                or step2_scope.get("combo_count")
                != len(_EXPECTED_STEP2_COMBO_KEYS)
                or not isinstance(step2_input_refs, dict)
                or step2_input_refs.get("dataset_version") != dataset_version
                or step2_input_refs.get("window") != window
                or not isinstance(step2_candidate, dict)
                or step2_candidate.get("schema_version")
                != "aats.step2_candidates.v1"
                or step2_candidate.get("round_id") != step2_round_id
                or step2_candidate.get("dataset_version") != dataset_version
                or not isinstance(step2_candidate_scope, dict)
                or step2_candidate_scope.get("symbol") != expected_symbol
                or step2_candidate_scope.get("step") != "step2_candidates"
                or not isinstance(step2_candidate_keys, list)
                or len(step2_candidate_keys) != len(set(step2_candidate_keys))
                or set(step2_candidate_keys) != _EXPECTED_STEP2_COMBO_KEYS
                or step2_candidate_scope.get("combo_count")
                != len(_EXPECTED_STEP2_COMBO_KEYS)
                or not isinstance(step2_candidates, dict)
                or set(step2_candidates) != _EXPECTED_STEP2_COMBO_KEYS
                or any(
                    not isinstance(values, dict) or not values
                    for values in step2_candidates.values()
                )
                or not isinstance(step2_pending, list)
                or bool(step2_pending)
                or not isinstance(step2_digests, dict)
                or step2_digests.get(step2_path.name) != step2_sha
                or not isinstance(step2_sizes, dict)
                or step2_sizes.get(step2_path.name) != len(step2_bytes)
            ):
                return None
            try:
                for values in step2_candidates.values():
                    parameter_values_fingerprint(values)
            except ValueError:
                return None
            if hashlib.sha256(step2_bytes).hexdigest() != step2_sha:
                return None
            validate_calibration_child_artifacts(
                round_dir=step2_round_dir,
                calibrations=step2_manifest.get("calibrations"),
                expected_topology=_EXPECTED_STEP2_CALIBRATION_TOPOLOGY,
                symbol=expected_symbol,
                dataset_version=dataset_version,
                window=window,
            )
            validate_scan_child_artifacts(
                project_root=project_root.resolve(),
                scans=step2_manifest.get("scans"),
                expected_topology=_EXPECTED_STEP2_SCAN_TOPOLOGY,
                symbol=expected_symbol,
                dataset_version=dataset_version,
                window=window,
            )
        artifact_sha256 = manifest.get("artifact_sha256")
        if not isinstance(artifact_sha256, dict):
            return None
        declared_candidate_sha256 = artifact_sha256.get(
            "parameter_candidates_merged.json"
        )
        if (
            not isinstance(declared_candidate_sha256, str)
            or not _SHA256_RE.fullmatch(declared_candidate_sha256)
        ):
            return None
        artifact_size_bytes = manifest.get("artifact_size_bytes")
        declared_candidate_size = (
            artifact_size_bytes.get("parameter_candidates_merged.json")
            if isinstance(artifact_size_bytes, dict)
            else None
        )
        if type(declared_candidate_size) is not int or declared_candidate_size < 0:
            return None
        candidate_path = manifest_path.parent / "parameter_candidates_merged.json"
        candidate_bytes = require_regular_round_file(
            candidate_path,
            parent=round_dir,
        )
        if len(candidate_bytes) != declared_candidate_size:
            return None
        actual_candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
        if declared_candidate_sha256 != actual_candidate_sha256:
            return None
        if (
            expected_candidate_sha256 is not None
            and declared_candidate_sha256 != expected_candidate_sha256
        ):
            return None
        started_at = _parse_manifest_time(
            manifest.get("started_at"), field="started_at"
        )
        finished_at = _parse_manifest_time(
            manifest.get("finished_at"), field="finished_at"
        )
        round_id_started_at = datetime.strptime(
            round_id[:15], "%Y%m%d_%H%M%S"
        ).replace(tzinfo=timezone.utc)
        if (
            finished_at < started_at
            or finished_at > datetime.now(timezone.utc)
            or abs((started_at - round_id_started_at).total_seconds()) > 2.0
        ):
            return None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return {
        "round_id": round_id,
        "symbol": expected_symbol,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": manifest["status"],
        "dataset_version": dataset_version,
        "candidate_sha256": declared_candidate_sha256,
        "source_step2_round_id": step2_round_id,
        "source_step2_candidate_sha256": step2_sha,
        "combo_keys": tuple(combo_keys),
        "window": dict(window),
    }


def _validate_incoming_round(
    project_root: pathlib.Path,
    candidates_file: pathlib.Path,
    data: Any,
    *,
    candidate_sha256: str,
) -> dict[str, Any] | None:
    """Bind the candidate payload to its directory and completed manifest."""

    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != "aats.step3_candidates.v1":
        return None
    round_id = data.get("round_id")
    scope = data.get("scope")
    if not isinstance(round_id, str) or round_id != candidates_file.parent.name:
        return None
    if not isinstance(scope, dict) or scope.get("step") != "step3_merged":
        return None
    candidates = data.get("candidates")
    if not isinstance(candidates, dict) or not candidates:
        return None
    candidate_keys = set(candidates)
    declared_keys = scope.get("combo_keys")
    if (
        not all(isinstance(key, str) for key in candidates)
        or not candidate_keys.issubset(_EXPECTED_STEP3_COMBO_KEYS)
        or not isinstance(declared_keys, list)
        or len(declared_keys) != len(set(declared_keys))
        or set(declared_keys) != candidate_keys
        or scope.get("combo_count") != len(candidate_keys)
        or any(not isinstance(values, dict) or not values for values in candidates.values())
    ):
        return None
    try:
        for values in candidates.values():
            parameter_values_fingerprint(values)
    except ValueError:
        return None
    symbol = scope.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip() or symbol != symbol.strip():
        return None
    metadata = _load_round_manifest(
        project_root,
        round_id=round_id,
        expected_symbol=symbol,
        expected_candidate_sha256=candidate_sha256,
    )
    pending = data.get("pending_validation")
    constraint_check = data.get("constraint_check")
    if (
        metadata is None
        or data.get("dataset_version") != metadata["dataset_version"]
        or candidate_keys != set(metadata["combo_keys"])
        or not isinstance(pending, list)
        or any(
            not isinstance(item, str)
            or not item.strip()
            or item != item.strip()
            for item in pending
        )
        or len(pending) != len(set(pending))
        or not isinstance(constraint_check, dict)
        or (
            metadata["status"] == "succeeded"
            and (
                pending
                or constraint_check.get("all_passed") is not True
                or constraint_check.get("violation_count") != 0
                or constraint_check.get("auto_fix_count") != 0
                or candidate_keys != _EXPECTED_STEP3_COMBO_KEYS
            )
        )
    ):
        return None
    return metadata


def _combo_key(parameter_set: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(parameter_set.get("family") or ""),
        str(parameter_set.get("symbol") or "BTC-USDT-SWAP"),
        str(parameter_set.get("timeframe") or "").lower(),
    )


def _stable_step3_parameter_set_id(parameter_set: dict[str, Any]) -> str:
    """Derive one concurrency-safe ID from the immutable Step 3 content."""

    payload = {
        "schema": "aats.step3_parameter_set_id.v1",
        "family": parameter_set.get("family"),
        "symbol": parameter_set.get("symbol", "BTC-USDT-SWAP"),
        "timeframe": str(parameter_set.get("timeframe") or "").lower(),
        "source_round_id": parameter_set.get("source_round_id"),
        "source_phase": parameter_set.get("source_phase"),
        "dataset_version": parameter_set.get("dataset_version", "v1.0"),
        "confidence": parameter_set.get("confidence"),
        "parameter_values_fingerprint": parameter_values_fingerprint(
            parameter_set.get("values")
        ),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"ps_step3_{hashlib.sha256(canonical).hexdigest()[:32]}"


# ── 查找最新候选文件 ───────────────────────────────────────────────────


def _resolve_exact_step3_candidates(
    project_root: pathlib.Path,
    candidates_file: pathlib.Path,
) -> pathlib.Path | None:
    """Resolve one caller-bound Step 3 candidate without any latest fallback."""

    project_root = project_root.resolve()
    expected_root = (project_root / _STEP3_ARTIFACT_DIR).resolve()
    supplied = candidates_file
    if not supplied.is_absolute():
        supplied = project_root / supplied
    try:
        if supplied.is_symlink() or supplied.parent.is_symlink():
            return None
        resolved = supplied.resolve(strict=True)
    except OSError:
        return None
    round_id = supplied.parent.name
    expected = expected_root / round_id / "parameter_candidates_merged.json"
    if (
        supplied.name != "parameter_candidates_merged.json"
        or _STEP3_ROUND_ID_RE.fullmatch(round_id) is None
        or resolved != expected
        or resolved.parent.parent != expected_root
        or not resolved.is_file()
    ):
        return None
    return resolved


def load_validated_formal_step3_candidate(
    project_root: pathlib.Path,
    candidates_file: pathlib.Path,
    *,
    expected_round_id: str | None = None,
    expected_candidate_sha256: str | None = None,
) -> ValidatedStep3CandidateArtifact | None:
    """Load one exact Step 3 candidate through the canonical importer contract.

    This is the single trust entrypoint shared by importer, downstream research
    lineage, and promotion qualification.  A caller-supplied path must resolve
    to the current project's formal Step 3 tree; the candidate, Step 3 manifest,
    Step 2 input chain, child artifacts, topology, timestamps, scope, digest and
    size are then validated as one unit.
    """

    resolved = _resolve_exact_step3_candidates(project_root, candidates_file)
    if resolved is None:
        return None
    try:
        candidate_bytes = read_stable_regular_round_file(
            resolved,
            parent=resolved.parent,
        )
        candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
        payload = decode_strict_json_artifact(
            candidate_bytes,
            expected_type=dict,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if (
        expected_candidate_sha256 is not None
        and candidate_sha256 != expected_candidate_sha256
    ):
        return None
    metadata = _validate_incoming_round(
        project_root.resolve(),
        resolved,
        payload,
        candidate_sha256=candidate_sha256,
    )
    if (
        metadata is None
        or (
            expected_round_id is not None
            and metadata.get("round_id") != expected_round_id
        )
    ):
        return None
    if not _managed_step3_snapshot_matches(
        project_root.resolve(),
        candidate_path=resolved,
        candidate_bytes=candidate_bytes,
        candidate_sha256=candidate_sha256,
        candidate_payload=payload,
        metadata=metadata,
    ):
        return None
    return ValidatedStep3CandidateArtifact(
        path=resolved,
        candidate_bytes=candidate_bytes,
        candidate_sha256=candidate_sha256,
        payload=payload,
        metadata=metadata,
    )


def materialize_validated_step3_parameter_sets(
    artifact: ValidatedStep3CandidateArtifact,
    *,
    initial_status: str,
) -> list[dict[str, Any]]:
    """Derive the exact governed parameter-set identities used by importer."""

    parameter_sets = import_from_parameter_candidates(
        artifact.path,
        source_round_id=str(artifact.metadata["round_id"]),
        source_phase="step3_merged",
        dataset_version=str(artifact.metadata["dataset_version"]),
        symbol=str(artifact.metadata["symbol"]),
        initial_status=initial_status,
        candidate_data=artifact.payload,
    )
    for parameter_set in parameter_sets:
        parameter_set["parameter_set_id"] = _stable_step3_parameter_set_id(
            parameter_set
        )
    return parameter_sets


def find_latest_step3_candidates(
    project_root: pathlib.Path,
) -> pathlib.Path | None:
    """查找最新的 Step 3 parameter_candidates_merged.json.

    先按生成器 round-id 的 UTC 秒确定最新一组，再用已绑定 manifest 的
    started_at 精确排序。同一秒内任一 round 未完成/无效时返回该无效路径，
    让调用方失败关闭，而不是悄悄回退并导入较旧研究结果。
    """
    step3_dir = project_root / _STEP3_ARTIFACT_DIR
    if not step3_dir.exists():
        log.warning("Step 3 artifact 目录不存在: %s", step3_dir)
        return None

    round_dirs = [item for item in step3_dir.iterdir() if item.is_dir()]
    if not round_dirs:
        log.warning("未找到任何 Step 3 round 目录")
        return None

    sequenced: list[tuple[datetime, pathlib.Path]] = []
    untrusted_dirs: list[pathlib.Path] = []
    for round_dir in round_dirs:
        round_id = round_dir.name
        if round_dir.is_symlink() or not _STEP3_ROUND_ID_RE.fullmatch(round_id):
            log.warning("发现非标准 Step 3 round 目录，拒绝回退: %s", round_id)
            untrusted_dirs.append(round_dir)
            continue
        try:
            generated_at = datetime.strptime(
                round_id[:15], "%Y%m%d_%H%M%S"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            log.warning("时间无效的 Step 3 round 目录，拒绝回退: %s", round_id)
            untrusted_dirs.append(round_dir)
            continue
        sequenced.append((generated_at, round_dir))

    if untrusted_dirs:
        # 非标准目录没有可信 UTC 排序键，无法证明它不是更新但损坏的发布。
        latest_untrusted = sorted(untrusted_dirs, key=lambda item: item.name)[-1]
        return latest_untrusted / "parameter_candidates_merged.json"

    if not sequenced:
        # 返回一个明确的预期路径，让主流程报告 metadata invalid；不能把
        # “存在 round 但都不可信”误报成 no_candidates。
        latest_untrusted = sorted(round_dirs, key=lambda item: item.name)[-1]
        return latest_untrusted / "parameter_candidates_merged.json"

    newest_second = max(item[0] for item in sequenced)
    newest_group = [
        round_dir
        for generated_at, round_dir in sequenced
        if generated_at == newest_second
    ]
    validated: list[tuple[datetime, pathlib.Path]] = []
    for round_dir in newest_group:
        candidates_file = round_dir / "parameter_candidates_merged.json"
        if not candidates_file.is_file():
            # 最新 round 尚在运行或在候选发布前失败；禁止回退旧 round。
            return candidates_file
        artifact = load_validated_formal_step3_candidate(
            project_root,
            candidates_file,
        )
        if artifact is None:
            return candidates_file
        validated.append((artifact.metadata["started_at"], candidates_file))

    latest_started_at = max(item[0] for item in validated)
    latest_matches = [item for item in validated if item[0] == latest_started_at]
    if len(latest_matches) != 1:
        # 同一秒内两个完整 round 使用了相同 started_at，无法确定唯一最新项。
        ambiguous_dir = sorted(
            (path.parent for _, path in latest_matches),
            key=lambda item: item.name,
        )[-1]
        return ambiguous_dir / "ambiguous_parameter_candidates_merged.json"
    latest = latest_matches[0][1]
    log.info("找到最新已完成 Step 3 候选文件: %s", latest)
    return latest


# ── 自动导入主逻辑 ──────────────────────────────────────────────────


def _auto_import_latest_candidates_locked(
    project_root: pathlib.Path,
    *,
    initial_status: str = "candidate",
    deprecate_old: bool = True,
    db_truth_locked: bool = False,
    candidates_file: pathlib.Path | None = None,
    expected_round_id: str | None = None,
    expected_candidate_sha256: str | None = None,
) -> dict[str, Any]:
    """自动导入最新 Step 3 参数候选到 registry.

    流程:
      1. 在 importer lock 内查找最新 parameter_candidates_merged.json
      2. 用 round manifest 绑定 round/symbol/完成时间，再从 DB 真源重读
      3. 按 round + combo + 内容身份核验已导入集合，恢复部分导入
      4. 按 artifact 声明的 param_count 先插入/核验全部新参数
      5. 可选：仅用更新的可信 round 废弃同 family/symbol/timeframe 的旧 candidate

    Returns:
        {
            "status": "imported" | "recovered_partial_import" |
                      "reconciled_import" | "concurrent_transition_preserved" |
                      "already_imported" | "round_content_conflict" |
                      "round_metadata_invalid" | "supersession_deferred" |
                      "import_lock_busy" | "no_candidates",
            "imported_count": int,
            "deprecated_count": int,
            "deprecation_skipped_count": int,
            "status_conflict_count": int,
            "source_file": str | None,
            "source_round_id": str | None,
            "round_status": str | None,
            "effective_initial_status": str | None,
            "parameter_sets": [...]
        }
    """
    result = _empty_result()

    # Step 1: 查找最新候选文件
    if candidates_file is None:
        selected_candidates_file = find_latest_step3_candidates(project_root)
    else:
        selected_candidates_file = _resolve_exact_step3_candidates(
            project_root,
            candidates_file,
        )
        if selected_candidates_file is None:
            result["source_file"] = str(candidates_file)
            result["status"] = "round_metadata_invalid"
            log.error(
                "显式 Step 3 candidate 路径不属于受信任 round: %s",
                candidates_file,
            )
            return result
    if selected_candidates_file is None:
        return result

    candidates_file = selected_candidates_file

    result["source_file"] = str(candidates_file)

    artifact = load_validated_formal_step3_candidate(
        project_root,
        candidates_file,
        expected_round_id=expected_round_id,
        expected_candidate_sha256=expected_candidate_sha256,
    )
    if artifact is None:
        if _STEP3_ROUND_ID_RE.fullmatch(candidates_file.parent.name):
            result["source_round_id"] = candidates_file.parent.name
        result["status"] = "round_metadata_invalid"
        log.error(
            "Step 3 candidate round/symbol/manifest 绑定无效，拒绝导入: %s",
            candidates_file,
        )
        return result

    result["source_candidate_sha256"] = artifact.candidate_sha256
    incoming_metadata = artifact.metadata
    source_round_id = str(incoming_metadata["round_id"])
    source_symbol = str(incoming_metadata["symbol"])
    effective_initial_status = (
        initial_status
        if incoming_metadata["status"] == "succeeded"
        else "draft"
    )
    result["source_round_id"] = source_round_id
    result["round_status"] = incoming_metadata["status"]
    result["effective_initial_status"] = effective_initial_status
    if effective_initial_status != initial_status:
        log.warning(
            "Step 3 round %s 状态为 %s；候选仅以 draft 导入且不替换旧候选",
            source_round_id,
            incoming_metadata["status"],
        )

    # Step 2: 加载 registry
    registry_path = project_root / _REGISTRY_PATH
    registry = load_registry(registry_path)

    # Step 3: 解析完整期望集合。不能在看到同 round 任意一行后就宣称完成；
    # 中途崩溃留下的部分集合必须可恢复，内容冲突则失败关闭。
    staged_for_atomic_publication = bool(
        db_truth_locked and effective_initial_status == "candidate"
    )
    new_sets = materialize_validated_step3_parameter_sets(
        artifact,
        initial_status=(
            "draft" if staged_for_atomic_publication else effective_initial_status
        ),
    )

    if not new_sets:
        log.warning("从 %s 未能解析到参数集 (文件存在但 candidates 为空)", candidates_file)
        result["status"] = "parse_empty"
        return result

    expected_by_combo: dict[tuple[str, str, str], dict[str, Any]] = {}
    for parameter_set in new_sets:
        combo = _combo_key(parameter_set)
        if combo in expected_by_combo:
            log.error("Round %s 含重复参数 combo: %s", source_round_id, combo)
            result["status"] = "round_content_conflict"
            return result
        expected_by_combo[combo] = parameter_set

    existing_round_sets = [
        ps
        for ps in registry.get("parameter_sets", [])
        if ps.get("source_round_id") == source_round_id
        and ps.get("source_phase") == "step3_merged"
    ]
    existing_by_combo: dict[tuple[str, str, str], dict[str, Any]] = {}
    for existing in existing_round_sets:
        combo = _combo_key(existing)
        expected = expected_by_combo.get(combo)
        if (
            expected is None
            or combo in existing_by_combo
            or parameter_set_immutable_identity(existing)
            != parameter_set_immutable_identity(expected)
        ):
            log.error(
                "Round %s 已有集合与当前 artifact 内容冲突: %s",
                source_round_id,
                combo,
            )
            result["status"] = "round_content_conflict"
            return result
        existing_by_combo[combo] = existing

    missing_sets = [
        parameter_set
        for combo, parameter_set in expected_by_combo.items()
        if combo not in existing_by_combo
    ]
    # Step 4: 先补齐全部新参数，再废弃旧候选。若某次写入中断，下一轮根据内容
    # 身份只补缺失 combo；旧候选仍保留，不会形成无候选窗口。
    for parameter_set in missing_sets:
        add_parameter_set(registry, parameter_set)
        log.info(
            "导入参数集: %s (%s/%s, %d 个参数)",
            parameter_set["parameter_set_id"],
            parameter_set["family"],
            parameter_set["timeframe"],
            len(parameter_set.get("values", {})),
        )

    published_count = 0
    if staged_for_atomic_publication:
        published_count = _publish_managed_round_candidates(new_sets)
        # publication 之后必须从 DB 真源重读。精确重试时成员可能已经由正常
        # 生命周期推进为 frozen/released/deprecated，内存镜像绝不能把它们
        # 伪装或回退成 candidate。
        registry = load_registry(
            registry_path,
            fail_closed_on_db_error=True,
        )
        _resolve_expected_parameter_sets(registry, new_sets)

    current_round_sets = _resolve_expected_parameter_sets(registry, new_sets)

    # Step 5: 可选 — CAS 废弃同 family/symbol/timeframe 且时间更早的旧 candidate。
    # draft 导入不能移走可晋级候选；旧 round manifest 缺失或时间不可证明时
    # 失败关闭（保留旧 candidate），不根据可伪造的目录名猜测顺序。
    deprecated_count = 0
    deprecation_skipped_count = 0
    status_conflict_count = 0
    old_round_metadata: dict[str, dict[str, Any] | None] = {}
    if deprecate_old and effective_initial_status == "candidate":
        for new_ps in current_round_sets:
            if new_ps.get("status") != "candidate":
                log.info(
                    "Round %s 参数集 %s 当前状态为 %s；"
                    "幂等重试不再触发候选替代",
                    source_round_id,
                    new_ps.get("parameter_set_id"),
                    new_ps.get("status"),
                )
                continue
            old_candidates = find_parameter_sets(
                registry,
                family=new_ps["family"],
                timeframe=new_ps["timeframe"],
                status="candidate",
            )
            for old_ps in old_candidates:
                # 只废弃同源(step3_merged)的旧候选，避免误废弃手动创建的 A/B test 候选
                if (
                    old_ps["parameter_set_id"] != new_ps["parameter_set_id"]
                    and old_ps.get("symbol") == new_ps.get("symbol")
                    and old_ps.get("source_phase") == "step3_merged"
                    and old_ps.get("source_round_id") != source_round_id
                ):
                    old_round_id = old_ps.get("source_round_id")
                    if not isinstance(old_round_id, str):
                        deprecation_skipped_count += 1
                        continue
                    if old_round_id not in old_round_metadata:
                        old_candidate_path = (
                            project_root
                            / _STEP3_ARTIFACT_DIR
                            / old_round_id
                            / "parameter_candidates_merged.json"
                        )
                        old_artifact = load_validated_formal_step3_candidate(
                            project_root,
                            old_candidate_path,
                            expected_round_id=old_round_id,
                        )
                        old_round_metadata[old_round_id] = (
                            old_artifact.metadata
                            if old_artifact is not None
                            and old_artifact.metadata.get("symbol") == source_symbol
                            else None
                        )
                    old_metadata = old_round_metadata[old_round_id]
                    if (
                        old_metadata is None
                        or incoming_metadata["started_at"]
                        <= old_metadata["started_at"]
                    ):
                        deprecation_skipped_count += 1
                        log.warning(
                            "旧候选 %s 的 round 顺序无法证明早于 %s，保留候选",
                            old_ps["parameter_set_id"],
                            source_round_id,
                        )
                        continue
                    deprecated = deprecate_parameter_set(
                        registry,
                        old_ps["parameter_set_id"],
                        notes=(
                            f"被 round {source_round_id} 的新候选替代 "
                            f"({datetime.now(timezone.utc).isoformat()})"
                        ),
                        replacement_parameter_set=new_ps,
                    )
                    if deprecated:
                        deprecated_count += 1
                    else:
                        status_conflict_count += 1
                        log.warning(
                            "旧候选 %s 生命周期已变化，跳过废弃；"
                            "不会覆盖并发 apply 的 released 状态",
                            old_ps["parameter_set_id"],
                        )
    elif deprecate_old:
        log.info(
            "effective_initial_status=%s 不是 candidate；不会废弃任何现有候选",
            effective_initial_status,
        )

    changed = bool(
        missing_sets
        or published_count
        or deprecated_count
        or status_conflict_count
    )
    result["published_count"] = published_count
    result["deprecation_skipped_count"] = deprecation_skipped_count
    if not changed:
        if db_truth_locked or has_explicit_governance_db_configuration(project_root):
            registry = load_registry(
                registry_path,
                fail_closed_on_db_error=True,
            )
            _refresh_registry_mirror_if_needed(registry, registry_path)
        result["parameter_sets"] = _summarize_parameter_sets(
            _resolve_expected_parameter_sets(registry, new_sets)
        )
        if deprecation_skipped_count:
            log.warning(
                "Round %s 参数集合已存在，但 %d 个旧候选缺少可信 supersession 证据",
                source_round_id,
                deprecation_skipped_count,
            )
            result["status"] = "supersession_deferred"
        else:
            log.info("Round %s 的完整参数集合已导入，跳过", source_round_id)
            result["status"] = "already_imported"
        return result

    # 受管 DB 是真源。CAS 冲突意味着事务外镜像已过期；写文件前统一重读，
    # 避免把 stale candidate 状态重新落到审计镜像。
    if db_truth_locked or has_explicit_governance_db_configuration(project_root):
        registry = load_registry(
            registry_path,
            fail_closed_on_db_error=True,
        )
    result["parameter_sets"] = _summarize_parameter_sets(
        _resolve_expected_parameter_sets(registry, new_sets)
    )
    save_registry(registry, registry_path)

    if existing_round_sets and missing_sets:
        result["status"] = "recovered_partial_import"
    elif not missing_sets and deprecated_count:
        result["status"] = "reconciled_import"
    elif status_conflict_count:
        result["status"] = "concurrent_transition_preserved"
    else:
        result["status"] = "imported"
    if deprecation_skipped_count:
        result["status"] = "supersession_deferred"
    result["imported_count"] = len(missing_sets)
    result["deprecated_count"] = deprecated_count
    result["status_conflict_count"] = status_conflict_count
    log.info(
        "自动导入完成: %d 个参数集导入, %d 个旧候选废弃",
        len(missing_sets),
        deprecated_count,
    )
    return result


def auto_import_latest_candidates(
    project_root: pathlib.Path,
    *,
    initial_status: str = "candidate",
    deprecate_old: bool = True,
    candidates_file: pathlib.Path | None = None,
    expected_round_id: str | None = None,
    expected_candidate_sha256: str | None = None,
) -> dict[str, Any]:
    """Run one serialized Step 3 candidate import.

    When ``candidates_file`` is supplied, that exact formal Step 3 artifact is
    validated and imported without consulting the global latest selector.
    A busy managed-DB importer is a stable, retryable failure result; DB outage
    remains an exception so callers cannot silently fall back to a stale file.
    """

    try:
        with _parameter_candidate_import_lock(project_root) as db_truth_locked:
            return _auto_import_latest_candidates_locked(
                project_root,
                initial_status=initial_status,
                deprecate_old=deprecate_old,
                db_truth_locked=bool(db_truth_locked),
                candidates_file=candidates_file,
                expected_round_id=expected_round_id,
                expected_candidate_sha256=expected_candidate_sha256,
            )
    except _ParameterImportLockBusy:
        log.warning("parameter candidate import lock is already held")
        return _empty_result(status="import_lock_busy")


# ── CLI 入口 ──────────────────────────────────────────────────────────


def main() -> None:
    """CLI 入口: python -m aats.data_platform.governance.auto_import_candidates"""
    import argparse
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="自动导入最新 Step 3 参数候选到治理层 registry",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="执行导入",
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="项目根目录 (默认: 当前目录)",
    )
    parser.add_argument(
        "--initial-status",
        default="candidate",
        choices=["draft", "candidate"],
        help="新导入参数集的初始状态 (默认: candidate)",
    )
    parser.add_argument(
        "--no-deprecate-old",
        action="store_true",
        help="不废弃旧 candidate 参数集",
    )
    args = parser.parse_args()

    if not args.run:
        print("使用 --run 执行导入")
        sys.exit(0)

    project_root = pathlib.Path(args.project_root).resolve()
    result = auto_import_latest_candidates(
        project_root,
        initial_status=args.initial_status,
        deprecate_old=not args.no_deprecate_old,
    )

    print("\n=== 自动导入结果 ===")
    print(f"  状态: {result['status']}")
    print(f"  来源: {result['source_file']}")
    print(f"  Round ID: {result['source_round_id']}")
    print(f"  导入数量: {result['imported_count']}")
    print(f"  废弃数量: {result['deprecated_count']}")

    if result["parameter_sets"]:
        print("\n  新参数集:")
        for ps in result["parameter_sets"]:
            print(f"    [{ps['status'].upper()}] {ps['id']}")
            print(f"      {ps['family']}/{ps['timeframe']} — {ps['param_count']} 个参数")

    sys.exit(0 if result["status"] in AUTO_IMPORT_SUCCESS_STATUSES else
             2 if result["status"] == "parse_empty" else 1)


if __name__ == "__main__":
    main()
