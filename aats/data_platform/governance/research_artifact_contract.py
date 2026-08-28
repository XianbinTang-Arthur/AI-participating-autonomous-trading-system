"""Strict validators for formal RDP research child artifacts.

The round manifest is an index, not proof by itself.  These helpers bind every
successful calibration/scan entry back to its canonical on-disk artifact and
verify the digest plus the business identity consumed by downstream stages.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import stat
from typing import Any


_BATCH_RUN_ID_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{8}$")
_SCAN_RUN_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FORMAL_RESEARCH_JSON_MAX_BYTES = 4 * 1024 * 1024


def _reject_non_finite_json(token: str) -> None:
    raise ValueError(f"research_artifact_json_non_finite:{token}")


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"research_artifact_json_duplicate_key:{key}")
        result[key] = value
    return result


def _json_value_is_strict(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int)):
        return True
    if isinstance(value, float):
        return value == value and value not in {float("inf"), float("-inf")}
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return True
    if isinstance(value, list):
        return all(_json_value_is_strict(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str)
            and _json_value_is_strict(key)
            and _json_value_is_strict(item)
            for key, item in value.items()
        )
    return False


def decode_strict_json_artifact(
    payload: bytes,
    *,
    expected_type: type[dict] | type[list] | None = None,
) -> Any:
    """Decode one finite, duplicate-free UTF-8 JSON artifact."""

    if type(payload) is not bytes:
        raise ValueError("research_artifact_json_bytes_invalid")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            parse_constant=_reject_non_finite_json,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
        strict_value = _json_value_is_strict(value)
    except (
        UnicodeDecodeError,
        UnicodeEncodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise ValueError("research_artifact_json_invalid") from exc
    if not strict_value or (
        expected_type is not None and type(value) is not expected_type
    ):
        raise ValueError("research_artifact_json_invalid")
    return value


def _canonical_json_identity(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("research_child_parameter_invalid") from exc


def _valid_identity_label(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def validate_calibration_batch_summary(
    summary: Any,
    *,
    expected_counts: tuple[Any, Any, Any] | None = None,
    expected_status: str | None = None,
    expected_experiments: Any = None,
) -> None:
    """Validate one canonical calibration summary and its experiment identity set."""

    if not isinstance(summary, dict):
        raise ValueError("research_calibration_summary_invalid")
    total = summary.get("total_experiments")
    succeeded = summary.get("succeeded")
    failed = summary.get("failed")
    experiments = summary.get("experiments")
    failures = summary.get("failures")
    if (
        type(total) is not int
        or type(succeeded) is not int
        or type(failed) is not int
        or total <= 0
        or succeeded < 0
        or failed < 0
        or succeeded + failed != total
        or not isinstance(experiments, list)
        or not isinstance(failures, list)
        or len(experiments) != succeeded
        or len(failures) != failed
    ):
        raise ValueError("research_calibration_summary_invalid")
    if expected_counts is not None:
        if any(type(value) is not int for value in expected_counts):
            raise ValueError("research_calibration_summary_count_mismatch")
        if (total, succeeded, failed) != expected_counts:
            raise ValueError("research_calibration_summary_count_mismatch")
    if (
        (expected_status == "succeeded" and (succeeded != total or failed != 0))
        or (
            expected_status == "partial_success"
            and not (0 < succeeded < total and 0 < failed < total)
        )
        or (expected_status == "failed" and (succeeded != 0 or failed != total))
        or expected_status not in {None, "succeeded", "partial_success", "failed"}
    ):
        raise ValueError("research_calibration_summary_status_mismatch")

    identities: dict[str, str] = {}
    for item in experiments:
        if (
            not isinstance(item, dict)
            or not _valid_identity_label(item.get("label"))
            or not isinstance(item.get("experiment_id"), str)
            or _SCAN_RUN_ID_RE.fullmatch(item["experiment_id"]) is None
            or item.get("status") != "succeeded"
            or not isinstance(item.get("params"), dict)
        ):
            raise ValueError("research_calibration_experiment_invalid")
        label = item["label"]
        if label in identities:
            raise ValueError("research_calibration_experiment_identity_duplicate")
        identities[label] = _canonical_json_identity(item["params"])
    for item in failures:
        if (
            not isinstance(item, dict)
            or not _valid_identity_label(item.get("label"))
            or not isinstance(item.get("params"), dict)
        ):
            raise ValueError("research_calibration_failure_invalid")
        label = item["label"]
        if label in identities:
            raise ValueError("research_calibration_experiment_identity_duplicate")
        identities[label] = _canonical_json_identity(item["params"])
    if len(identities) != total:
        raise ValueError("research_calibration_experiment_count_mismatch")

    if expected_experiments is not None:
        if not isinstance(expected_experiments, list) or len(expected_experiments) != total:
            raise ValueError("research_calibration_expected_experiments_invalid")
        expected_identities: dict[str, str] = {}
        for item in expected_experiments:
            if (
                not isinstance(item, dict)
                or not _valid_identity_label(item.get("label"))
                or not isinstance(item.get("params"), dict)
                or item["label"] in expected_identities
            ):
                raise ValueError("research_calibration_expected_experiments_invalid")
            expected_identities[item["label"]] = _canonical_json_identity(
                item["params"]
            )
        if identities != expected_identities:
            raise ValueError("research_calibration_parameter_identity_mismatch")


def validate_scan_comparison(
    comparison: Any,
    *,
    expected_counts: tuple[Any, Any, Any],
) -> None:
    """Validate canonical scan comparison rows against child result counts."""

    total, completed, failed = expected_counts
    if (
        type(total) is not int
        or type(completed) is not int
        or type(failed) is not int
        or total <= 0
        or completed <= 0
        or failed < 0
        or completed + failed != total
        or not isinstance(comparison, dict)
        or set(comparison) != {"experiment_count", "comparison"}
    ):
        raise ValueError("research_scan_comparison_invalid")
    experiment_count = comparison.get("experiment_count")
    rows = comparison.get("comparison")
    if (
        type(experiment_count) is not int
        or experiment_count != completed
        or not isinstance(rows, list)
        or len(rows) != experiment_count
        or not all(isinstance(item, dict) for item in rows)
    ):
        raise ValueError("research_scan_comparison_count_mismatch")
    labels: set[str] = set()
    for item in rows:
        label = item.get("label")
        if not _valid_identity_label(label) or label in labels:
            raise ValueError("research_scan_comparison_identity_invalid")
        _canonical_json_identity(item)
        labels.add(label)


def resolve_formal_round_dir(
    round_dir: pathlib.Path,
    *,
    phase_dir_name: str,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Return ``(resolved round, project root)`` for one canonical layout."""

    if phase_dir_name not in {"step2_rounds", "step3_rounds"}:
        raise ValueError("research_round_phase_invalid")
    supplied = round_dir.absolute()
    if supplied.is_symlink():
        raise ValueError("research_round_symlink_invalid")
    try:
        resolved = supplied.resolve(strict=True)
    except OSError as exc:
        raise ValueError("research_round_path_invalid") from exc
    if (
        not resolved.is_dir()
        or resolved.parent.name != phase_dir_name
        or resolved.parent.parent.name != "research"
        or resolved.parent.parent.parent.name != "artifacts"
        or resolved.parent.is_symlink()
    ):
        raise ValueError("research_round_layout_invalid")
    project_root = resolved.parents[3]
    expected = project_root / "artifacts" / "research" / phase_dir_name / resolved.name
    if resolved != expected:
        raise ValueError("research_round_layout_invalid")
    return resolved, project_root


def require_regular_round_file(path: pathlib.Path, *, parent: pathlib.Path) -> bytes:
    """Compatibility entrypoint for one bounded, descriptor-stable child."""

    return read_stable_regular_round_file(path, parent=parent)


def read_stable_regular_artifact_file(
    path: pathlib.Path,
    *,
    parent: pathlib.Path,
    max_bytes: int = FORMAL_RESEARCH_JSON_MAX_BYTES,
) -> bytes:
    """Read a direct formal artifact while detecting replacement during the read."""

    if (
        type(max_bytes) is not int
        or max_bytes <= 0
        or path.parent != parent
    ):
        raise ValueError("research_round_file_path_invalid")
    try:
        parent_stat = parent.lstat()
        resolved_parent = parent.resolve(strict=True)
        path_stat = path.lstat()
    except OSError as exc:
        raise ValueError("research_round_file_unreadable") from exc
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or parent.is_symlink()
        or path.is_symlink()
        or resolved_parent != parent
        or not stat.S_ISREG(path_stat.st_mode)
    ):
        raise ValueError("research_round_file_path_invalid")
    if path_stat.st_size > max_bytes:
        raise ValueError("research_round_file_too_large")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("research_round_file_unreadable") from exc
    try:
        before = os.fstat(descriptor)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_size",
            "st_mtime_ns",
        )
        if not stat.S_ISREG(before.st_mode) or any(
            getattr(path_stat, field) != getattr(before, field)
            for field in stable_fields
        ):
            raise ValueError("research_round_file_changed_during_read")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("research_round_file_too_large")
        after = os.fstat(descriptor)
    except OSError as exc:
        raise ValueError("research_round_file_unreadable") from exc
    finally:
        os.close(descriptor)
    try:
        after_path = path.lstat()
        after_parent = parent.lstat()
        after_resolved_parent = parent.resolve(strict=True)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("research_round_file_changed_during_read") from exc
    parent_identity_fields = ("st_dev", "st_ino", "st_mode")
    if (
        resolved != resolved_parent / path.name
        or after_resolved_parent != resolved_parent
        or any(
            getattr(parent_stat, field) != getattr(after_parent, field)
            for field in parent_identity_fields
        )
        or not stat.S_ISREG(before.st_mode)
        or any(
            getattr(before, field) != getattr(after, field)
            for field in stable_fields
        )
        or any(
            getattr(after, field) != getattr(after_path, field)
            for field in stable_fields
        )
    ):
        raise ValueError("research_round_file_changed_during_read")
    return b"".join(chunks)


def read_stable_regular_round_file(
    path: pathlib.Path,
    *,
    parent: pathlib.Path,
    max_bytes: int = FORMAL_RESEARCH_JSON_MAX_BYTES,
) -> bytes:
    """Backward-compatible round-specific alias for the formal bytes reader."""

    return read_stable_regular_artifact_file(
        path,
        parent=parent,
        max_bytes=max_bytes,
    )


def read_stable_json_artifact(
    path: pathlib.Path,
    *,
    parent: pathlib.Path,
    max_bytes: int = FORMAL_RESEARCH_JSON_MAX_BYTES,
    expected_type: type[dict] | type[list] | None = None,
) -> tuple[Any, bytes]:
    """Read and strictly decode one direct formal JSON artifact."""

    payload = read_stable_regular_artifact_file(
        path,
        parent=parent,
        max_bytes=max_bytes,
    )
    return (
        decode_strict_json_artifact(payload, expected_type=expected_type),
        payload,
    )


def _read_digest_bound_json(
    path: pathlib.Path,
    *,
    expected_sha256: Any,
    expected_size: Any,
) -> tuple[dict[str, Any], bytes]:
    if (
        not isinstance(expected_sha256, str)
        or _SHA256_RE.fullmatch(expected_sha256) is None
        or type(expected_size) is not int
        or expected_size <= 0
        or expected_size > FORMAL_RESEARCH_JSON_MAX_BYTES
    ):
        raise ValueError("research_child_identity_invalid")
    try:
        data, payload = read_stable_json_artifact(
            path,
            parent=path.parent,
            expected_type=dict,
        )
    except ValueError as exc:
        raise ValueError("research_child_artifact_invalid") from exc
    if (
        len(payload) != expected_size
        or hashlib.sha256(payload).hexdigest() != expected_sha256
    ):
        raise ValueError("research_child_digest_invalid")
    return data, payload


def validate_calibration_child_artifacts(
    *,
    round_dir: pathlib.Path,
    calibrations: Any,
    expected_topology: dict[str, tuple[Any, ...]],
    symbol: str,
    dataset_version: str,
    window: dict[str, str],
) -> None:
    """Verify every successful calibration batch referenced by a manifest."""

    if not isinstance(calibrations, list):
        raise ValueError("research_calibration_artifacts_invalid")
    for calibration in calibrations:
        if not isinstance(calibration, dict) or calibration.get("status") != "succeeded":
            continue
        round_key = calibration.get("round_key")
        if round_key not in expected_topology:
            raise ValueError("research_calibration_topology_invalid")
        family, timeframe, expected_batch_keys = expected_topology[round_key]
        batches = calibration.get("batches")
        if not isinstance(batches, list):
            raise ValueError("research_calibration_artifacts_invalid")
        by_key = {batch.get("key"): batch for batch in batches if isinstance(batch, dict)}
        if set(by_key) != set(expected_batch_keys) or len(by_key) != len(batches):
            raise ValueError("research_calibration_topology_invalid")
        for batch_key in expected_batch_keys:
            batch = by_key[batch_key]
            run_id = batch.get("batch_run_id")
            batch_dir_raw = batch.get("batch_dir")
            if (
                batch.get("status") != "succeeded"
                or not isinstance(run_id, str)
                or _BATCH_RUN_ID_RE.fullmatch(run_id) is None
                or not isinstance(batch_dir_raw, str)
            ):
                raise ValueError("research_calibration_identity_invalid")
            batch_dir = pathlib.Path(batch_dir_raw)
            expected_batch_dir = round_dir / "batches" / run_id
            if (
                not batch_dir.is_absolute()
                or batch_dir.is_symlink()
                or batch_dir.resolve(strict=False) != expected_batch_dir
            ):
                raise ValueError("research_calibration_path_invalid")
            summary, _ = _read_digest_bound_json(
                expected_batch_dir / "batch_summary.json",
                expected_sha256=batch.get("summary_sha256"),
                expected_size=batch.get("summary_size_bytes"),
            )
            total = summary.get("total_experiments")
            succeeded = summary.get("succeeded")
            failed = summary.get("failed")
            expected_batch_name = f"{family}_{batch_key}_{str(timeframe).lower()}"
            if (
                summary.get("batch_run_id") != run_id
                or summary.get("batch_name") != expected_batch_name
                or summary.get("family") != family
                or summary.get("symbol") != symbol
                or summary.get("timeframe") != timeframe
                or summary.get("dataset_version") != dataset_version
                or summary.get("window") != f"{window['start']} ~ {window['end']}"
                or type(total) is not int
                or type(succeeded) is not int
                or type(failed) is not int
                or total <= 0
                or succeeded != total
                or failed != 0
            ):
                raise ValueError("research_calibration_summary_invalid")
            validate_calibration_batch_summary(
                summary,
                expected_counts=(
                    batch.get("total_experiments"),
                    batch.get("succeeded"),
                    batch.get("failed"),
                ),
                expected_status="succeeded",
            )


def validate_scan_child_artifacts(
    *,
    project_root: pathlib.Path,
    scans: Any,
    expected_topology: dict[str, tuple[str, str]],
    symbol: str,
    dataset_version: str,
    window: dict[str, str],
) -> None:
    """Verify every successful formal scan comparison referenced by a manifest."""

    if not isinstance(scans, list):
        raise ValueError("research_scan_artifacts_invalid")
    for scan in scans:
        if not isinstance(scan, dict) or scan.get("status") != "succeeded":
            continue
        scan_key = scan.get("scan_key")
        if scan_key not in expected_topology:
            raise ValueError("research_scan_topology_invalid")
        family, timeframe = expected_topology[scan_key]
        run_id = scan.get("scan_run_id")
        scan_dir_raw = scan.get("scan_dir")
        if (
            not isinstance(run_id, str)
            or _SCAN_RUN_ID_RE.fullmatch(run_id) is None
            or not isinstance(scan_dir_raw, str)
            or scan.get("family") != family
            or scan.get("timeframe") != timeframe
            or scan.get("dataset_version") != dataset_version
            or scan.get("window") != window
        ):
            raise ValueError("research_scan_identity_invalid")
        scan_dir = pathlib.Path(scan_dir_raw)
        expected_scan_dir = project_root / "artifacts" / "research" / "experiments" / run_id
        if (
            not scan_dir.is_absolute()
            or scan_dir.is_symlink()
            or scan_dir.resolve(strict=False) != expected_scan_dir
        ):
            raise ValueError("research_scan_path_invalid")
        comparison, _ = _read_digest_bound_json(
            expected_scan_dir / "comparison_summary.json",
            expected_sha256=scan.get("comparison_sha256"),
            expected_size=scan.get("comparison_size_bytes"),
        )
        validate_scan_comparison(
            comparison,
            expected_counts=(
                scan.get("total_combinations"),
                scan.get("completed_count"),
                scan.get("failed_count"),
            ),
        )
