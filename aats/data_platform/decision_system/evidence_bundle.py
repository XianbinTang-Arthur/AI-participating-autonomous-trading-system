"""Phase 6-A: Evidence Bundle 统一化。"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import pathlib
import re
from datetime import datetime, timezone
from typing import Any

from aats.data_platform.governance.snapshot_db import (
    ROUND_PHASE_STEP2,
    SNAPSHOT_ACTIVE_ROUND_INDEX,
    SNAPSHOT_ARTIFACT_INDEX,
    SNAPSHOT_QUALITY_MONITOR,
    is_snapshot_incomplete,
    load_governance_snapshot,
    load_research_round_snapshot,
    load_latest_research_round_snapshot,
)
from aats.data_platform.governance.parameter_registry import load_registry
from aats.data_platform.replay.backtest.equity_builder import (
    REPLAY_RISK_METRIC_POLICY_ID,
)
from aats.data_platform.replay.backtest.fill_simulator import FILL_MODEL_VERSION
from aats.data_platform.replay.diagnostics.replay_diagnostics import (
    extract_comparison_rows,
)
from aats.domain.instrument_contract import (
    INSTRUMENT_ARITHMETIC_POLICY_ID,
    InstrumentContract,
)

log = logging.getLogger(__name__)

COMBOS: list[dict[str, str]] = [
    {"key": "independent_15m", "family": "independent", "timeframe": "15m"},
    {"key": "independent_1h", "family": "independent", "timeframe": "1H"},
    {"key": "directional_15m", "family": "directional", "timeframe": "15m"},
    {"key": "directional_1h", "family": "directional", "timeframe": "1H"},
]

_UNTRUSTED_STATUSES: set[str] = {"deprecated", "failed"}
_ROUND_DIR_PATTERN = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{8}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_BACKTEST_MANIFEST_KIND = "backtest_run_manifest"
_BACKTEST_MANIFEST_SCHEMA = "backtest-run/v2"
_BACKTEST_MANIFEST_NAME = "manifest.json"
_BACKTEST_REQUIRED_ARTIFACTS = frozenset(
    {
        "summary.json",
        "equity_curve.csv",
        "cost_validation.json",
        "cost_diagnostics.json",
        "execution_timeline.json",
    }
)
_BACKTEST_MANIFEST_KEYS = frozenset(
    {
        "artifact_kind",
        "artifact_schema_version",
        "complete",
        "run_fingerprint",
        "artifact_set_fingerprint",
        "instrument_arithmetic_policy_id",
        "fill_model_version",
        "contract_lineage_status",
        "settlement_currency",
        "instrument_symbol",
        "instrument_contract_fingerprint",
        "instrument_contract",
        "resolved_parameters",
        "adapter_identity",
        "adapter_algorithm_version",
        "cadence_gap_count",
        "risk_metric_policy_id",
        "artifact_sha256",
    }
)
_BACKTEST_SUMMARY_KEYS = frozenset(
    {
        "artifact_kind",
        "artifact_schema_version",
        "config",
        "resolved_parameters",
        "adapter_identity",
        "adapter_algorithm_version",
        "cadence_gap_count",
        "summary",
        "decisions_count",
        "fills_count",
        "start_ts",
        "end_ts",
    }
)

_PHASE2_PROMOTION_METRICS_NAME = "phase2_promotion_metrics.json"
_PHASE2_PROMOTION_METRICS_KIND = "phase2_promotion_metrics"
PHASE2_PROMOTION_QUALIFICATION_POLICY = "phase2-promotion-metrics/v1"
_PHASE2_PROMOTION_METRICS_SCHEMA = PHASE2_PROMOTION_QUALIFICATION_POLICY
_BACKTEST_ALLOWED_ARTIFACTS = (
    _BACKTEST_REQUIRED_ARTIFACTS | {_PHASE2_PROMOTION_METRICS_NAME}
)
_PHASE2_PROMOTION_METRICS_KEYS = frozenset(
    {
        "artifact_kind",
        "artifact_schema_version",
        "family",
        "timeframe",
        "total_bars",
        "opening_count",
        "positive_edge_ratio",
        "mean_expected_edge_bps",
        "execution_compatible_ratio",
        "selectable_ratio",
    }
)


def _safe_load_json(path: pathlib.Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Failed to load JSON from %s: %s", path, exc)
        return None


def _strict_load_json_object(path: pathlib.Path) -> dict[str, Any] | None:
    """Load one standards-compliant, finite JSON object for evidence checks."""

    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(
                handle,
                parse_constant=lambda token: (_raise_json_constant(token)),
            )
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not _json_numbers_are_finite(payload):
        return None
    return payload


def _raise_json_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON constant: {token}")


def _json_numbers_are_finite(value: Any) -> bool:
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json_numbers_are_finite(child) for child in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _json_numbers_are_finite(child)
            for key, child in value.items()
        )
    return False


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_hash_bound_json_object(
    path: pathlib.Path,
    *,
    expected_hash: str,
) -> tuple[dict[str, Any] | None, str]:
    """Parse the exact bytes whose manifest hash is being trusted."""

    try:
        payload_bytes = path.read_bytes()
    except OSError:
        return None, "backtest_manifest_artifact_unreadable"
    if hashlib.sha256(payload_bytes).hexdigest() != expected_hash:
        return None, "backtest_manifest_artifact_changed_after_validation"
    try:
        payload = json.loads(
            payload_bytes.decode("utf-8"),
            parse_constant=lambda token: (_raise_json_constant(token)),
        )
    except (UnicodeDecodeError, ValueError):
        return None, "backtest_manifest_bound_json_invalid"
    if not isinstance(payload, dict) or not _json_numbers_are_finite(payload):
        return None, "backtest_manifest_bound_json_invalid"
    return payload, "qualified"


def _resolve_evidence_path(
    raw_path: str | pathlib.Path,
    *,
    project_root: pathlib.Path,
) -> pathlib.Path | None:
    root = project_root.resolve(strict=False)
    candidate = pathlib.Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved


def _manifest_reference(
    raw: dict[str, Any],
    *,
    project_root: pathlib.Path,
    default_artifact_dir: pathlib.Path | None,
) -> pathlib.Path | None:
    declared = raw.get("backtest_manifest_path")
    if declared is not None:
        if not isinstance(declared, str) or not declared.strip():
            return None
        candidate: str | pathlib.Path = declared.strip()
    elif default_artifact_dir is not None:
        candidate = default_artifact_dir / _BACKTEST_MANIFEST_NAME
    else:
        return None
    resolved = _resolve_evidence_path(candidate, project_root=project_root)
    if (
        resolved is None
        or resolved.name != _BACKTEST_MANIFEST_NAME
        or not resolved.is_file()
    ):
        return None
    return resolved


def _validate_backtest_manifest(
    manifest_path: pathlib.Path,
) -> tuple[dict[str, Any] | None, str]:
    manifest = _strict_load_json_object(manifest_path)
    if manifest is None:
        return None, "backtest_manifest_invalid_json"
    if set(manifest) != _BACKTEST_MANIFEST_KEYS:
        return None, "backtest_manifest_schema_mismatch"
    if (
        manifest.get("artifact_kind") != _BACKTEST_MANIFEST_KIND
        or manifest.get("artifact_schema_version") != _BACKTEST_MANIFEST_SCHEMA
        or manifest.get("complete") is not True
    ):
        return None, "backtest_manifest_contract_unsupported"
    if type(manifest.get("cadence_gap_count")) is not int:
        return None, "backtest_manifest_cadence_gap_invalid"
    if manifest["cadence_gap_count"] != 0:
        return None, "backtest_manifest_cadence_gap_present"
    for key in (
        "run_fingerprint",
        "artifact_set_fingerprint",
        "instrument_contract_fingerprint",
    ):
        if not isinstance(manifest.get(key), str) or not _SHA256_PATTERN.fullmatch(
            manifest[key]
        ):
            return None, f"backtest_manifest_{key}_invalid"
    for key in (
        "instrument_arithmetic_policy_id",
        "fill_model_version",
        "contract_lineage_status",
        "settlement_currency",
        "instrument_symbol",
        "adapter_identity",
        "adapter_algorithm_version",
        "risk_metric_policy_id",
    ):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            return None, f"backtest_manifest_{key}_invalid"
    if not isinstance(manifest.get("instrument_contract"), dict) or not isinstance(
        manifest.get("resolved_parameters"),
        dict,
    ):
        return None, "backtest_manifest_lineage_payload_invalid"
    if (
        manifest["instrument_arithmetic_policy_id"]
        != INSTRUMENT_ARITHMETIC_POLICY_ID
        or manifest["fill_model_version"] != FILL_MODEL_VERSION
        or manifest["risk_metric_policy_id"] != REPLAY_RISK_METRIC_POLICY_ID
    ):
        return None, "backtest_manifest_policy_unsupported"
    try:
        contract = InstrumentContract(**manifest["instrument_contract"])
    except (TypeError, ValueError):
        return None, "backtest_manifest_instrument_contract_invalid"
    if (
        contract.instrument_type != "SPOT"
        or contract.contract_type != "spot"
        or contract.symbol != manifest["instrument_symbol"]
        or contract.settle_currency != manifest["settlement_currency"]
        or contract.fingerprint != manifest["instrument_contract_fingerprint"]
    ):
        return None, "backtest_manifest_instrument_contract_mismatch"

    artifact_hashes = manifest.get("artifact_sha256")
    if not isinstance(artifact_hashes, dict):
        return None, "backtest_manifest_artifact_set_incomplete"
    artifact_names = set(artifact_hashes)
    if (
        not _BACKTEST_REQUIRED_ARTIFACTS <= artifact_names
        or not artifact_names <= _BACKTEST_ALLOWED_ARTIFACTS
    ):
        return None, "backtest_manifest_artifact_set_incomplete"
    manifest_dir = manifest_path.parent.resolve(strict=True)
    for name, expected_hash in artifact_hashes.items():
        if (
            not isinstance(name, str)
            or pathlib.Path(name).name != name
            or not isinstance(expected_hash, str)
            or not _SHA256_PATTERN.fullmatch(expected_hash)
        ):
            return None, "backtest_manifest_artifact_hash_schema_invalid"
        artifact_path = (manifest_dir / name).resolve(strict=False)
        try:
            artifact_path.relative_to(manifest_dir)
        except ValueError:
            return None, "backtest_manifest_artifact_path_escape"
        if not artifact_path.is_file():
            return None, "backtest_manifest_artifact_missing"
        try:
            actual_hash = _sha256_file(artifact_path)
        except OSError:
            return None, "backtest_manifest_artifact_unreadable"
        if actual_hash != expected_hash:
            return None, "backtest_manifest_artifact_hash_mismatch"

    fingerprint_payload = {
        "artifact_schema_version": _BACKTEST_MANIFEST_SCHEMA,
        "artifact_sha256": artifact_hashes,
    }
    expected_fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    if manifest["artifact_set_fingerprint"] != expected_fingerprint:
        return None, "backtest_manifest_artifact_set_fingerprint_mismatch"
    return manifest, "qualified"


def _validated_promotion_metrics(
    raw: dict[str, Any],
    *,
    project_root: pathlib.Path,
    default_artifact_dir: pathlib.Path | None,
    expected_family: str | None,
    expected_timeframe: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return only metrics that are schema-checked and hash-bound to v2 output."""

    qualification: dict[str, Any] = {
        "promotion_eligible": False,
        "promotion_qualification_reason": "backtest_manifest_missing",
        "backtest_manifest_path": None,
        "promotion_metrics_schema_version": None,
    }
    manifest_path = _manifest_reference(
        raw,
        project_root=project_root,
        default_artifact_dir=default_artifact_dir,
    )
    if manifest_path is None:
        return None, qualification
    qualification["backtest_manifest_path"] = str(manifest_path)
    manifest, reason = _validate_backtest_manifest(manifest_path)
    if manifest is None:
        qualification["promotion_qualification_reason"] = reason
        return None, qualification

    artifact_hashes = manifest["artifact_sha256"]
    if _PHASE2_PROMOTION_METRICS_NAME not in artifact_hashes:
        qualification["promotion_qualification_reason"] = (
            "phase2_promotion_metrics_not_manifest_bound"
        )
        return None, qualification
    metrics_path = manifest_path.parent / _PHASE2_PROMOTION_METRICS_NAME
    metrics, metrics_load_reason = _load_hash_bound_json_object(
        metrics_path,
        expected_hash=artifact_hashes[_PHASE2_PROMOTION_METRICS_NAME],
    )
    if metrics is None:
        qualification["promotion_qualification_reason"] = metrics_load_reason
        return None, qualification
    if set(metrics) != _PHASE2_PROMOTION_METRICS_KEYS:
        qualification["promotion_qualification_reason"] = (
            "phase2_promotion_metrics_schema_mismatch"
        )
        return None, qualification
    if (
        metrics.get("artifact_kind") != _PHASE2_PROMOTION_METRICS_KIND
        or metrics.get("artifact_schema_version")
        != _PHASE2_PROMOTION_METRICS_SCHEMA
    ):
        qualification["promotion_qualification_reason"] = (
            "phase2_promotion_metrics_contract_unsupported"
        )
        return None, qualification

    summary, summary_load_reason = _load_hash_bound_json_object(
        manifest_path.parent / "summary.json",
        expected_hash=artifact_hashes["summary.json"],
    )
    if summary is None:
        qualification["promotion_qualification_reason"] = summary_load_reason
        return None, qualification
    if (
        set(summary) != _BACKTEST_SUMMARY_KEYS
        or summary.get("artifact_kind") != "backtest_run_summary"
        or summary.get("artifact_schema_version") != _BACKTEST_MANIFEST_SCHEMA
        or not isinstance(summary.get("config"), dict)
        or not isinstance(summary.get("summary"), dict)
        or type(summary.get("decisions_count")) is not int
        or type(summary.get("fills_count")) is not int
        or summary["decisions_count"] < 0
        or summary["fills_count"] < 0
    ):
        qualification["promotion_qualification_reason"] = (
            "backtest_summary_schema_mismatch"
        )
        return None, qualification
    if (
        summary.get("adapter_identity") != manifest["adapter_identity"]
        or summary.get("adapter_algorithm_version")
        != manifest["adapter_algorithm_version"]
        or summary.get("resolved_parameters") != manifest["resolved_parameters"]
        or summary.get("cadence_gap_count") != manifest["cadence_gap_count"]
    ):
        qualification["promotion_qualification_reason"] = (
            "backtest_summary_manifest_lineage_mismatch"
        )
        return None, qualification

    config = summary["config"]
    summary_metrics = summary["summary"]
    if (
        config.get("symbol") != manifest["instrument_symbol"]
        or config.get("instrument_contract") != manifest["instrument_contract"]
        or config.get("fill_model_version") != manifest["fill_model_version"]
        or config.get("execution_model_version") != "next_bar_event_v2"
        or summary_metrics.get("settlement_currency")
        != manifest["settlement_currency"]
        or summary_metrics.get("instrument_symbol") != manifest["instrument_symbol"]
        or summary_metrics.get("instrument_contract_fingerprint")
        != manifest["instrument_contract_fingerprint"]
        or summary_metrics.get("risk_metric_policy_id")
        != manifest["risk_metric_policy_id"]
        or summary_metrics.get("bar_count") != summary["decisions_count"]
        or summary_metrics.get("fill_count") != summary["fills_count"]
    ):
        qualification["promotion_qualification_reason"] = (
            "backtest_summary_manifest_contract_mismatch"
        )
        return None, qualification

    family = metrics.get("family")
    timeframe = normalize_timeframe_value(metrics.get("timeframe"))
    raw_family = expected_family or raw.get("family")
    raw_timeframe = normalize_timeframe_value(
        expected_timeframe or raw.get("timeframe")
    )
    if (
        not isinstance(family, str)
        or not family.strip()
        or timeframe is None
        or make_combo_key(family, timeframe)
        not in {combo["key"] for combo in COMBOS}
        or (raw_family is not None and raw_family != family)
        or (raw_timeframe is not None and raw_timeframe != timeframe)
        or summary["config"].get("family") != family
        or normalize_timeframe_value(summary["config"].get("timeframe"))
        != timeframe
    ):
        qualification["promotion_qualification_reason"] = (
            "phase2_promotion_metrics_scope_mismatch"
        )
        return None, qualification

    total_bars = metrics.get("total_bars")
    opening_count = metrics.get("opening_count")
    if (
        type(total_bars) is not int
        or type(opening_count) is not int
        or total_bars < 0
        or not 0 <= opening_count <= total_bars
        or summary.get("decisions_count") != total_bars
    ):
        qualification["promotion_qualification_reason"] = (
            "phase2_promotion_metrics_counts_invalid"
        )
        return None, qualification
    for key in (
        "positive_edge_ratio",
        "execution_compatible_ratio",
        "selectable_ratio",
    ):
        value = metrics.get(key)
        if (key == "positive_edge_ratio" and value is None) or (
            value is not None
            and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0.0 <= float(value) <= 1.0
            )
        ):
            qualification["promotion_qualification_reason"] = (
                f"phase2_promotion_metrics_{key}_invalid"
            )
            return None, qualification
    expected_edge = metrics.get("mean_expected_edge_bps")
    if expected_edge is not None and (
        isinstance(expected_edge, bool)
        or not isinstance(expected_edge, (int, float))
    ):
        qualification["promotion_qualification_reason"] = (
            "phase2_promotion_metrics_mean_expected_edge_bps_invalid"
        )
        return None, qualification

    qualification.update(
        {
            "promotion_eligible": True,
            "promotion_qualification_reason": "qualified",
            "promotion_metrics_schema_version": _PHASE2_PROMOTION_METRICS_SCHEMA,
        }
    )
    return metrics, qualification


def normalize_timeframe_value(timeframe: str | None) -> str | None:
    if timeframe is None:
        return None
    normalized = str(timeframe).strip().lower()
    if normalized in {"1h", "1hr", "1hour"}:
        return "1h"
    return normalized


def make_combo_key(family: str | None, timeframe: str | None) -> str | None:
    normalized_timeframe = normalize_timeframe_value(timeframe)
    if not family or not normalized_timeframe:
        return None
    return f"{family}_{normalized_timeframe}"


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _build_phase2_diag_entry(
    raw: dict[str, Any],
    *,
    diag_id: str,
    diag_type: str,
    project_root: pathlib.Path,
    default_artifact_dir: pathlib.Path | None = None,
    family: str | None = None,
    timeframe: str | None = None,
    label: str | None = None,
    scan_key: str | None = None,
    scan_run_id: str | None = None,
) -> dict[str, Any]:
    verified_metrics, qualification = _validated_promotion_metrics(
        raw,
        project_root=project_root,
        default_artifact_dir=default_artifact_dir,
        expected_family=family,
        expected_timeframe=timeframe,
    )
    metric_source = verified_metrics or raw
    resolved_family = family or metric_source.get("family") or raw.get("family")
    resolved_timeframe = (
        timeframe or metric_source.get("timeframe") or raw.get("timeframe")
    )
    combo_key = make_combo_key(resolved_family, resolved_timeframe)

    entry: dict[str, Any] = {
        "id": diag_id,
        "type": diag_type,
        "family": resolved_family,
        "timeframe": resolved_timeframe,
        "combo_key": combo_key,
        "total_bars": _coerce_int(metric_source.get("total_bars")),
        "opening_count": _coerce_int(metric_source.get("opening_count")),
        "positive_edge_ratio": (
            _coerce_float(metric_source.get("positive_edge_ratio")) or 0.0
        ),
        "mean_expected_edge_bps": _coerce_float(
            metric_source.get("mean_expected_edge_bps")
        ),
        "execution_compatible_ratio": _coerce_float(
            metric_source.get("execution_compatible_ratio"),
        ),
        "selectable_ratio": _coerce_float(metric_source.get("selectable_ratio")),
    }
    entry.update(qualification)
    if label or raw.get("label"):
        entry["label"] = label or raw.get("label")
    if scan_key:
        entry["scan_key"] = scan_key
    if scan_run_id:
        entry["scan_run_id"] = scan_run_id
    return entry


def _aggregate_phase2_stats(diags: list[dict[str, Any]]) -> dict[str, Any]:
    # No caller-supplied metric is promotion evidence until the centralized
    # verifier has bound it to a complete, hash-valid backtest-run/v2 bundle.
    diags = [diag for diag in diags if diag.get("promotion_eligible") is True]
    if not diags:
        return {
            "available": False,
            "total_experiments": 0,
            "experiments_with_openings": 0,
            "max_opening_count": 0,
            "mean_positive_edge_ratio": 0.0,
            "mean_expected_edge_bps": None,
            "mean_execution_compatible_ratio": None,
        }

    openings = [max(_coerce_int(d.get("opening_count")), 0) for d in diags]
    edge_ratios = [
        value
        for value in (_coerce_float(d.get("positive_edge_ratio")) for d in diags)
        if value is not None
    ]
    expected_edges = [
        value
        for value in (_coerce_float(d.get("mean_expected_edge_bps")) for d in diags)
        if value is not None
    ]
    exec_ratios = [
        value
        for value in (_coerce_float(d.get("execution_compatible_ratio")) for d in diags)
        if value is not None
    ]

    experiments_with_openings = sum(1 for opening in openings if opening > 0)
    # available 必须依赖真实开仓实验数：Phase2 扫描跑完但所有组合 opening_count=0
    # 属于"跑了但没证据"，不能让下游 selector / gate 把它当作"有证据可用"。
    return {
        "available": experiments_with_openings > 0,
        "total_experiments": len(diags),
        "experiments_with_openings": experiments_with_openings,
        "max_opening_count": max(openings) if openings else 0,
        "mean_positive_edge_ratio": round(
            sum(edge_ratios) / len(edge_ratios), 6,
        ) if edge_ratios else 0.0,
        "mean_expected_edge_bps": round(
            sum(expected_edges) / len(expected_edges), 6,
        ) if expected_edges else None,
        "mean_execution_compatible_ratio": round(
            sum(exec_ratios) / len(exec_ratios), 6,
        ) if exec_ratios else None,
    }


def get_phase2_combo_stats(
    evidence: dict[str, Any],
    family: str,
    timeframe: str,
) -> dict[str, Any]:
    combo_key = make_combo_key(family, timeframe)
    if (
        evidence.get("promotion_qualification_policy")
        != PHASE2_PROMOTION_QUALIFICATION_POLICY
    ):
        fallback = _aggregate_phase2_stats([])
        fallback.update(
            {
                "family": family,
                "timeframe": timeframe,
                "combo_key": combo_key,
                "fallback_reason": "promotion_qualification_policy_unsupported",
            }
        )
        return fallback
    combo_stats = evidence.get("combo_stats", {})
    if combo_key and combo_key in combo_stats:
        return combo_stats[combo_key]

    # 没有该 family/timeframe 的独立统计 → 必须返回 unavailable，而不是把全局
    # 聚合伪装成这一 combo 的证据：这是曾经出现过"global_stats 把无关 combo 误判为
    # 有证据可用"的故障根因。selector/gate 应基于 combo-specific 证据决策。
    fallback = _aggregate_phase2_stats([])
    fallback["family"] = family
    fallback["timeframe"] = timeframe
    fallback["combo_key"] = combo_key
    fallback["fallback_reason"] = "combo_stats_missing"
    return fallback


def _find_latest_round_dir(root: pathlib.Path) -> pathlib.Path | None:
    if not root.exists():
        return None
    round_dirs = [subdir for subdir in root.iterdir() if subdir.is_dir()]
    if not round_dirs:
        return None

    canonical_round_dirs = [
        subdir for subdir in round_dirs if _ROUND_DIR_PATTERN.match(subdir.name)
    ]
    if canonical_round_dirs:
        return sorted(canonical_round_dirs, key=lambda path: path.name)[-1]

    return sorted(round_dirs, key=lambda path: path.stat().st_mtime)[-1]


def _artifact_dir(
    raw_path: Any,
    *,
    project_root: pathlib.Path,
) -> pathlib.Path | None:
    if not isinstance(raw_path, (str, pathlib.Path)) or not str(raw_path).strip():
        return None
    candidate = pathlib.Path(raw_path)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return candidate


def _collect_latest_step2_round_diags(project_root: pathlib.Path) -> list[dict[str, Any]]:
    snapshot = load_latest_research_round_snapshot(
        phase=ROUND_PHASE_STEP2,
        project_root=project_root,
    )
    # 缺 round_manifest.json 的 Step2 目录（残留/半成品）不能进入 Phase 2 证据链，
    # 否则会让 collect_phase2_evidence / _aggregate_phase2_stats 把不完整目录
    # 当成"experiments_with_openings>=1"的可交易证据，污染 promotion readiness。
    if is_snapshot_incomplete(snapshot):
        log.warning(
            "Phase2 证据收集: Step2 最新 round snapshot 缺 round_manifest.json "
            "(round_id=%s)，按无可信证据处理",
            snapshot.get("round_id") if isinstance(snapshot, dict) else None,
        )
        return []
    if snapshot:
        diags: list[dict[str, Any]] = []
        round_dir = _artifact_dir(
            snapshot.get("round_path"),
            project_root=project_root,
        )
        summary = snapshot.get("summary", {}) or {}
        family_summary = summary.get("family_timeframe_summary", {}) or {}
        for index, item in enumerate(family_summary.get("experiments", [])):
            diags.append(
                _build_phase2_diag_entry(
                    item,
                    diag_id=f"{snapshot.get('round_id')}/calibration/{index}",
                    diag_type="calibration_experiment",
                    project_root=project_root,
                    default_artifact_dir=round_dir,
                ),
            )

        scan_summary = summary.get("scan_comparison_summary", {}) or {}
        for index, item in enumerate(extract_comparison_rows(scan_summary)):
            diags.append(
                _build_phase2_diag_entry(
                    item,
                    diag_id=f"{snapshot.get('round_id')}/scan/{index}",
                    diag_type="parameter_scan_item",
                    project_root=project_root,
                    default_artifact_dir=round_dir,
                    scan_key=item.get("scan_key"),
                    scan_run_id=item.get("scan_run_id"),
                ),
            )
        if diags:
            return diags

    return []


def _finalize_phase2_stats(
    evidence: dict[str, Any],
    all_diags: list[dict[str, Any]],
) -> None:
    eligible_diags = [
        diag for diag in all_diags if diag.get("promotion_eligible") is True
    ]
    combo_diags: dict[str, list[dict[str, Any]]] = {}
    for diag in eligible_diags:
        combo_key = diag.get("combo_key")
        if combo_key:
            combo_diags.setdefault(combo_key, []).append(diag)

    combo_stats: dict[str, Any] = {}
    for combo in COMBOS:
        combo_key = combo["key"]
        stats = _aggregate_phase2_stats(combo_diags.get(combo_key, []))
        stats["family"] = combo["family"]
        stats["timeframe"] = combo["timeframe"]
        stats["combo_key"] = combo_key
        combo_stats[combo_key] = stats

    global_stats = _aggregate_phase2_stats(eligible_diags)
    evidence["combo_stats"] = combo_stats
    evidence["global_stats"] = global_stats
    evidence["aggregate_stats"] = global_stats
    evidence["best_experiments"] = sorted(
        eligible_diags,
        key=lambda item: item.get("opening_count", 0),
        reverse=True,
    )[:3]
    evidence["audit_best_experiments"] = sorted(
        all_diags,
        key=lambda item: item.get("opening_count", 0),
        reverse=True,
    )[:3]
    evidence["promotion_eligible_experiment_count"] = len(eligible_diags)
    evidence["promotion_ineligible_experiment_count"] = (
        len(all_diags) - len(eligible_diags)
    )


def collect_phase2_evidence(
    project_root: pathlib.Path,
    *,
    artifact_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """收集 Phase 2 (Step 1/2) 研究证据。"""
    project_root = project_root.resolve(strict=False)
    evidence: dict[str, Any] = {
        "source": "phase2",
        "evidence_source": "governance_index" if artifact_index else "directory_scan",
        "experiment_count": 0,
        "parameter_scan_count": 0,
        "experiments": [],
        "best_experiments": [],
        "audit_best_experiments": [],
        "combo_stats": {},
        "global_stats": {},
        "aggregate_stats": {},
        "promotion_eligible_experiment_count": 0,
        "promotion_ineligible_experiment_count": 0,
        "promotion_qualification_policy": _PHASE2_PROMOTION_METRICS_SCHEMA,
    }

    all_diags: list[dict[str, Any]] = []
    canonical_step2_diags = _collect_latest_step2_round_diags(project_root)
    if canonical_step2_diags:
        all_diags.extend(canonical_step2_diags)

    if artifact_index:
        for artifact in artifact_index.get("artifacts", []):
            if artifact.get("phase") not in ("phase2_step1", "phase2_step2"):
                continue

            artifact_type = artifact.get("artifact_type", "experiment")
            artifact_path = _artifact_dir(
                artifact.get("path"),
                project_root=project_root,
            )
            if artifact_path is None:
                continue

            if artifact_type == "parameter_scan":
                evidence["parameter_scan_count"] += 1
                comp = _safe_load_json(artifact_path / "comparison_summary.json")
                if not isinstance(comp, dict):
                    continue
                comparison = extract_comparison_rows(comp)
                evidence["experiments"].append({
                    "id": artifact["artifact_id"],
                    "type": "parameter_scan",
                    "experiment_count": comp.get("experiment_count"),
                    "comparison": comparison,
                })
                if not canonical_step2_diags:
                    for index, item in enumerate(comparison):
                        all_diags.append(
                            _build_phase2_diag_entry(
                                item,
                                diag_id=f"{artifact['artifact_id']}/scan/{index}",
                                diag_type="parameter_scan_item",
                                project_root=project_root,
                                default_artifact_dir=artifact_path,
                                family=artifact.get("family"),
                                timeframe=artifact.get("timeframe"),
                                scan_key=artifact.get("artifact_id"),
                                scan_run_id=artifact.get("artifact_id"),
                            ),
                        )
                continue

            summary = artifact.get("diagnostics_summary")
            if not summary:
                continue
            evidence["experiment_count"] += 1
            diag = _safe_load_json(artifact_path / "diagnostics.json")
            payload = dict(summary)
            if isinstance(diag, dict):
                payload.update(diag)
            entry = _build_phase2_diag_entry(
                payload,
                diag_id=artifact["artifact_id"],
                diag_type="experiment",
                project_root=project_root,
                default_artifact_dir=artifact_path,
                family=artifact.get("family"),
                timeframe=artifact.get("timeframe"),
            )
            evidence["experiments"].append(entry)
            all_diags.append(entry)
    else:
        log.warning("artifact_index 不存在，fallback 到目录扫描")
        exp_root = project_root / "artifacts/research/experiments"
        if exp_root.exists():
            for subdir in sorted(exp_root.iterdir()):
                if not subdir.is_dir():
                    continue

                comp_file = subdir / "comparison_summary.json"
                if comp_file.exists():
                    evidence["parameter_scan_count"] += 1
                    comp = _safe_load_json(comp_file)
                    if not isinstance(comp, dict):
                        continue
                    comparison = extract_comparison_rows(comp)
                    evidence["experiments"].append({
                        "id": subdir.name,
                        "type": "parameter_scan",
                        "experiment_count": comp.get("experiment_count"),
                        "comparison": comparison,
                    })
                    if not canonical_step2_diags:
                        for index, item in enumerate(comparison):
                            all_diags.append(
                                _build_phase2_diag_entry(
                                    item,
                                    diag_id=f"{subdir.name}/scan/{index}",
                                    diag_type="parameter_scan_item",
                                    project_root=project_root,
                                    default_artifact_dir=subdir,
                                    scan_key=subdir.name,
                                    scan_run_id=subdir.name,
                                ),
                            )
                    continue

                diag = _safe_load_json(subdir / "diagnostics.json")
                if not isinstance(diag, dict):
                    continue
                evidence["experiment_count"] += 1
                entry = _build_phase2_diag_entry(
                    diag,
                    diag_id=subdir.name,
                    diag_type="experiment",
                    project_root=project_root,
                    default_artifact_dir=subdir,
                )
                evidence["experiments"].append(entry)
                all_diags.append(entry)

    _finalize_phase2_stats(evidence, all_diags)
    return evidence


def _collect_round_evidence_from_index(
    active_round_index: dict[str, Any],
    phase: str,
) -> list[dict[str, Any]]:
    trusted: list[dict[str, Any]] = []
    for round_info in active_round_index.get("all_rounds", []):
        if round_info.get("phase") != phase:
            continue
        if round_info.get("status") in _UNTRUSTED_STATUSES:
            continue
        trusted.append(round_info)
    return trusted


def _enrich_round_from_manifest(
    round_info: dict[str, Any],
    phase: str,
    project_root: pathlib.Path,
) -> dict[str, Any]:
    round_id = round_info.get("round_id")
    snapshot = (
        load_research_round_snapshot(round_id=round_id, project_root=project_root)
        if round_id else None
    )
    if snapshot:
        summary = snapshot.get("summary", {}) or {}
        manifest_payload = snapshot.get("manifest", {}) or {}
        combos = summary.get("combos", {}) or {}
        enriched: dict[str, Any] = {
            "round_id": snapshot.get("round_id"),
            "started_at": snapshot.get("started_at"),
            "status": snapshot.get("status", "unknown"),
            "replay_only": bool(snapshot.get("replay_only", False)),
            "live_query_succeeded": bool(
                manifest_payload.get("live_query_succeeded", False)
            ),
            "combos": {},
        }
        for key, combo in combos.items():
            combo_data: dict[str, Any] = {"status": combo.get("status", "unknown")}
            if phase == "phase3":
                combo_data["live_query_succeeded"] = bool(
                    combo.get("live_query_succeeded", False)
                )
                if combo.get("alignment_stats") is not None:
                    combo_data["alignment_stats"] = combo.get("alignment_stats")
                if combo.get("attribution_summary") is not None:
                    combo_data["attribution_summary"] = combo.get("attribution_summary")
                if combo.get("top_failure_modes") is not None:
                    combo_data["top_failure_modes"] = combo.get("top_failure_modes")
            elif phase == "phase4" and combo.get("cost_summary") is not None:
                combo_data["cost_summary"] = combo.get("cost_summary")
            enriched["combos"][key] = combo_data
        return enriched

    round_dir = pathlib.Path(round_info.get("path", ""))
    manifest = _safe_load_json(round_dir / "round_manifest.json")
    if not isinstance(manifest, dict):
        return round_info

    enriched: dict[str, Any] = {
        "round_id": round_info.get("round_id", round_dir.name),
        "started_at": round_info.get("started_at"),
        "status": round_info.get("status", manifest.get("overall_status", "unknown")),
        "replay_only": bool(manifest.get("replay_only", False)),
        "live_query_succeeded": bool(manifest.get("live_query_succeeded", False)),
        "combos": {},
    }

    for combo in manifest.get("combos", []):
        key = combo.get("key", "?")
        combo_data: dict[str, Any] = {"status": combo.get("status", "unknown")}
        if phase == "phase3":
            combo_data["live_query_succeeded"] = bool(
                combo.get("live_query_succeeded", False)
            )
            if combo.get("alignment_stats") is not None:
                combo_data["alignment_stats"] = combo.get("alignment_stats")
        run_dir = combo.get("run_dir")
        if run_dir:
            run_path = pathlib.Path(run_dir)
            if not run_path.is_absolute():
                run_path = run_path.resolve()

            if phase == "phase3":
                summary = _safe_load_json(run_path / "attribution_summary.json")
                if summary:
                    combo_data["attribution_summary"] = summary
                tfm = _safe_load_json(run_path / "top_failure_modes.json")
                if tfm:
                    combo_data["top_failure_modes"] = tfm
            elif phase == "phase4":
                cost = _safe_load_json(run_path / "execution_cost_summary.json")
                if cost:
                    combo_data["cost_summary"] = {
                        "total_candidates": cost.get("total_candidates", 0),
                        "full_fill_ratio": cost.get("full_fill_ratio", 0),
                        "slippage_mean": cost.get("slippage", {}).get("mean", 0),
                        "total_cost_mean": cost.get("total_execution_cost", {}).get("mean", 0),
                        "cost_adjusted_edge_mean": cost.get("cost_adjusted_edge", {}).get("mean", 0),
                        "positive_edge_ratio": cost.get("positive_edge_ratio", 0),
                    }

        enriched["combos"][key] = combo_data

    return enriched


def collect_phase3_evidence(
    project_root: pathlib.Path,
    *,
    active_round_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "source": "phase3",
        "evidence_source": "governance_index" if active_round_index else "directory_scan",
        "round_count": 0,
        "trusted_round_count": 0,
        "skipped_untrusted": 0,
        "latest_round": None,
        "combo_results": {},
    }

    rounds: list[dict[str, Any]] = []
    if active_round_index:
        trusted = _collect_round_evidence_from_index(active_round_index, "phase3")
        evidence["trusted_round_count"] = len(trusted)
        all_rounds = [
            round_info for round_info in active_round_index.get("all_rounds", [])
            if round_info.get("phase") == "phase3"
        ]
        evidence["round_count"] = len(all_rounds)
        evidence["skipped_untrusted"] = len(all_rounds) - len(trusted)
        for round_info in trusted:
            rounds.append(_enrich_round_from_manifest(round_info, "phase3", project_root))
    else:
        log.warning("active_round_index 不存在，Phase 3 fallback 到目录扫描")
        attr_root = project_root / "artifacts/research/attribution_rounds"
        if attr_root.exists():
            for subdir in sorted(attr_root.iterdir()):
                if not subdir.is_dir():
                    continue
                manifest = _safe_load_json(subdir / "round_manifest.json")
                if not isinstance(manifest, dict):
                    continue
                evidence["round_count"] += 1
                round_status = manifest.get("overall_status", manifest.get("status"))
                if round_status in _UNTRUSTED_STATUSES:
                    evidence["skipped_untrusted"] += 1
                    continue
                evidence["trusted_round_count"] += 1
                rounds.append(
                    _enrich_round_from_manifest(
                        {
                            "round_id": manifest.get("round_id", subdir.name),
                            "started_at": manifest.get("started_at"),
                            "status": manifest.get("overall_status", manifest.get("status", "unknown")),
                            "path": str(subdir),
                        },
                        "phase3",
                        project_root,
                    ),
                )

    if rounds:
        rounds.sort(key=lambda item: item.get("started_at") or "", reverse=True)
        evidence["latest_round"] = rounds[0]
    return evidence


def collect_phase4_evidence(
    project_root: pathlib.Path,
    *,
    active_round_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "source": "phase4",
        "evidence_source": "governance_index" if active_round_index else "directory_scan",
        "round_count": 0,
        "trusted_round_count": 0,
        "skipped_untrusted": 0,
        "latest_round": None,
        "combo_results": {},
    }

    rounds: list[dict[str, Any]] = []
    if active_round_index:
        trusted = _collect_round_evidence_from_index(active_round_index, "phase4")
        evidence["trusted_round_count"] = len(trusted)
        all_rounds = [
            round_info for round_info in active_round_index.get("all_rounds", [])
            if round_info.get("phase") == "phase4"
        ]
        evidence["round_count"] = len(all_rounds)
        evidence["skipped_untrusted"] = len(all_rounds) - len(trusted)
        for round_info in trusted:
            rounds.append(_enrich_round_from_manifest(round_info, "phase4", project_root))
    else:
        log.warning("active_round_index 不存在，Phase 4 fallback 到目录扫描")
        exec_root = project_root / "artifacts/research/execution_rounds"
        if exec_root.exists():
            for subdir in sorted(exec_root.iterdir()):
                if not subdir.is_dir():
                    continue
                manifest = _safe_load_json(subdir / "round_manifest.json")
                if not isinstance(manifest, dict):
                    continue
                evidence["round_count"] += 1
                round_status = manifest.get("overall_status", manifest.get("status"))
                if round_status in _UNTRUSTED_STATUSES:
                    evidence["skipped_untrusted"] += 1
                    continue
                evidence["trusted_round_count"] += 1
                rounds.append(
                    _enrich_round_from_manifest(
                        {
                            "round_id": manifest.get("round_id", subdir.name),
                            "started_at": manifest.get("started_at"),
                            "status": manifest.get("overall_status", manifest.get("status", "unknown")),
                            "path": str(subdir),
                        },
                        "phase4",
                        project_root,
                    ),
                )

    if rounds:
        rounds.sort(key=lambda item: item.get("started_at") or "", reverse=True)
        evidence["latest_round"] = rounds[0]
    return evidence


def collect_phase5_evidence(project_root: pathlib.Path) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "source": "phase5_governance",
        "artifact_index_exists": False,
        "parameter_registry_exists": False,
        "quality_monitor_exists": False,
        "quality_health": None,
        "frozen_parameter_sets": [],
        "candidate_parameter_sets": [],
        "total_artifacts": 0,
        "critical_failures": 0,
    }

    artifact_index = load_governance_snapshot(
        project_root,
        snapshot_type=SNAPSHOT_ARTIFACT_INDEX,
    )
    if artifact_index:
        evidence["artifact_index_exists"] = True
        evidence["total_artifacts"] = artifact_index.get("summary", {}).get(
            "total_artifacts", 0,
        )

    gov_root = project_root / "artifacts/governance"
    registry_path = gov_root / "current_parameter_registry.json"
    try:
        registry = load_registry(registry_path)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Failed to load parameter registry via DB-first path: %s", exc)
        registry = _safe_load_json(registry_path)
    if isinstance(registry, dict):
        evidence["parameter_registry_exists"] = True
        for parameter_set in registry.get("parameter_sets", []):
            if parameter_set.get("status") == "frozen":
                evidence["frozen_parameter_sets"].append({
                    "parameter_set_id": parameter_set["parameter_set_id"],
                    "family": parameter_set["family"],
                    "timeframe": parameter_set["timeframe"],
                    "values": parameter_set.get("values", {}),
                    "frozen_at": parameter_set.get("frozen_at"),
                })
            elif parameter_set.get("status") == "candidate":
                evidence["candidate_parameter_sets"].append({
                    "parameter_set_id": parameter_set["parameter_set_id"],
                    "family": parameter_set["family"],
                    "timeframe": parameter_set["timeframe"],
                    "values": parameter_set.get("values", {}),
                })

    quality_monitor = load_governance_snapshot(
        project_root,
        snapshot_type=SNAPSHOT_QUALITY_MONITOR,
    )
    if isinstance(quality_monitor, dict):
        evidence["quality_monitor_exists"] = True
        summary = quality_monitor.get("summary", {})
        evidence["quality_health"] = summary.get("health")
        evidence["critical_failures"] = summary.get("critical_failures", 0)

    return evidence


def build_evidence_bundle(project_root: pathlib.Path) -> dict[str, Any]:
    artifact_index = load_governance_snapshot(
        project_root,
        snapshot_type=SNAPSHOT_ARTIFACT_INDEX,
    )
    active_round_index = load_governance_snapshot(
        project_root,
        snapshot_type=SNAPSHOT_ACTIVE_ROUND_INDEX,
    )

    if artifact_index:
        log.info("使用治理层 artifact_index 作为 Phase 2 证据来源")
    else:
        log.warning("artifact_index.json 不存在，Phase 2 将 fallback 到目录扫描")

    if active_round_index:
        log.info("使用治理层 active_round_index 作为 Phase 3/4 证据来源")
    else:
        log.warning("active_round_index.json 不存在，Phase 3/4 将 fallback 到目录扫描")

    p2 = collect_phase2_evidence(project_root, artifact_index=artifact_index)
    p3 = collect_phase3_evidence(project_root, active_round_index=active_round_index)
    p4 = collect_phase4_evidence(project_root, active_round_index=active_round_index)
    p5 = collect_phase5_evidence(project_root)

    phases_with_data: list[str] = []
    if p2.get("experiment_count", 0) > 0 or p2.get("parameter_scan_count", 0) > 0:
        phases_with_data.append("phase2")
    if p3.get("trusted_round_count", 0) > 0:
        phases_with_data.append("phase3")
    if p4.get("trusted_round_count", 0) > 0:
        phases_with_data.append("phase4")
    if p5.get("artifact_index_exists"):
        phases_with_data.append("phase5")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(project_root),
        "evidence_completeness": {
            "phases_with_data": phases_with_data,
            "total_phases": 4,
            "completeness_ratio": len(phases_with_data) / 4,
        },
        "governance_index_used": {
            "artifact_index": artifact_index is not None,
            "active_round_index": active_round_index is not None,
        },
        "phase2_evidence": p2,
        "phase3_evidence": p3,
        "phase4_evidence": p4,
        "phase5_governance_evidence": p5,
    }
