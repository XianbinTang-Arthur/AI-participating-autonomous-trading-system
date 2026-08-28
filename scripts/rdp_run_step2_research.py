#!/usr/bin/env python3
"""Step 2 Research Orchestrator — 正式研究闭环.

把 Step 1 的 independent/15m 单范围校准推进到覆盖
independent + directional、15m + 1H 的完整研究闭环。

固定范围（Step 2）：
  symbol     = BTC-USDT-SWAP
  families   = independent, directional
  timeframes = 15m, 1H

执行顺序（严格）：
  Phase A: Calibration rounds
    2-A  independent / 1H
    2-B  directional / 15m
    2-C  directional / 1H
  Phase B: Formal parameter scans
    independent / 15m, 1H
    directional / 15m, 1H
  Phase C: Aggregation + parameter candidates
  Phase D: Conclusion document

Usage:
    python scripts/rdp_run_step2_research.py
    python scripts/rdp_run_step2_research.py --artifact-root artifacts/custom
    python scripts/rdp_run_step2_research.py --ensure-schema
    python scripts/rdp_run_step2_research.py --skip-calibration   # 只跑 scan + 汇总
    python scripts/rdp_run_step2_research.py --skip-scan          # 只跑 calibration + 汇总

Exit codes:
    0 = 全部成功
    1 = 参数错误 / 启动失败
    2 = 部分成功（至少有数据产出，但不是全部成功）
    3 = 全部失败
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import pathlib
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from aats.data_platform.governance._atomic_io import (
    atomic_json_write,
    immutable_json_write,
)
from aats.data_platform.governance._db_util import (
    has_explicit_governance_db_configuration,
)
from aats.data_platform.governance.research_artifact_contract import (
    validate_calibration_batch_summary,
    validate_scan_comparison,
)
from aats.data_platform.governance.snapshot_db import (
    ROUND_PHASE_STEP2,
    save_research_round_snapshot,
)
from aats.data_platform.replay.core.replay_context import ReplayCostConfig
from aats.data_platform.replay.diagnostics.replay_diagnostics import (
    extract_comparison_rows,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_step2_research")

_STEP2_RESULT_PREFIX = "RDP_STEP2_RESULT_JSON="
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent

# =========================================================================
# Step 2 固定范围 & 定义
# =========================================================================
_SYMBOL = "BTC-USDT-SWAP"

# ---------- Calibration 定义 ----------
# 按任务书 §8 的固定执行顺序：independent/1H → directional/15m → directional/1H
# independent/15m 已在 Step 1 完成，Step 2 不重复
_CALIBRATION_DEFS: dict[str, dict[str, Any]] = {
    # ── Step 3 扩展 independent/15m 校准（补齐 Step 1 未覆盖的参数）──
    "independent_15m_expanded": {
        "family": "independent",
        "timeframe": "15m",
        "description": "Step 3: independent / 15m expanded calibration (entry/close/risk/timing/cost)",
        "batches": [
            {
                "key": "entry_threshold",
                "file": "configs/research_batches/independent_entry_threshold_15m.json",
                "description": "Entry threshold sensitivity test",
            },
            {
                "key": "close_threshold",
                "file": "configs/research_batches/independent_close_threshold_15m.json",
                "description": "Close threshold sensitivity test",
            },
            {
                "key": "de_risk_edge",
                "file": "configs/research_batches/independent_de_risk_edge_15m.json",
                "description": "De-risk net edge threshold sensitivity test",
            },
            {
                "key": "failed_thesis_edge",
                "file": "configs/research_batches/independent_failed_thesis_edge_15m.json",
                "description": "Failed thesis net edge threshold sensitivity test",
            },
            {
                "key": "timing",
                "file": "configs/research_batches/independent_timing_15m.json",
                "description": "Hold time and cooldown sensitivity test",
            },
            {
                "key": "cost_buffer",
                "file": "configs/research_batches/independent_cost_buffer_15m.json",
                "description": "Slippage/execution buffer sensitivity test",
            },
        ],
    },
    "independent_1h": {
        "family": "independent",
        "timeframe": "1H",
        "description": "Step 2-A: independent / 1H calibration",
        "batches": [
            {
                "key": "scale_calibration",
                "file": "configs/research_batches/independent_scale_calibration_1h.json",
                "description": "Signal edge scale calibration",
            },
            {
                "key": "cost_sensitivity",
                "file": "configs/research_batches/independent_cost_sensitivity_1h.json",
                "description": "Cost model sensitivity test",
            },
            {
                "key": "confirm_ticks",
                "file": "configs/research_batches/independent_confirm_ticks_1h.json",
                "description": "Confirmation ticks sensitivity test",
            },
        ],
    },
    # ── Step 3 扩展 independent/1H 校准 ──
    "independent_1h_expanded": {
        "family": "independent",
        "timeframe": "1H",
        "description": "Step 3: independent / 1H expanded calibration (entry/close/risk/timing/cost)",
        "batches": [
            {
                "key": "entry_threshold",
                "file": "configs/research_batches/independent_entry_threshold_1h.json",
                "description": "Entry threshold sensitivity test",
            },
            {
                "key": "close_threshold",
                "file": "configs/research_batches/independent_close_threshold_1h.json",
                "description": "Close threshold sensitivity test",
            },
            {
                "key": "de_risk_edge",
                "file": "configs/research_batches/independent_de_risk_edge_1h.json",
                "description": "De-risk net edge threshold sensitivity test",
            },
            {
                "key": "failed_thesis_edge",
                "file": "configs/research_batches/independent_failed_thesis_edge_1h.json",
                "description": "Failed thesis net edge threshold sensitivity test",
            },
            {
                "key": "timing",
                "file": "configs/research_batches/independent_timing_1h.json",
                "description": "Hold time and cooldown sensitivity test",
            },
            {
                "key": "cost_buffer",
                "file": "configs/research_batches/independent_cost_buffer_1h.json",
                "description": "Slippage/execution buffer sensitivity test",
            },
        ],
    },
    "directional_15m": {
        "family": "directional",
        "timeframe": "15m",
        "description": "Step 2-B: directional / 15m calibration",
        "batches": [
            {
                "key": "scale_calibration",
                "file": "configs/research_batches/directional_scale_calibration_15m.json",
                "description": "Signal edge scale calibration",
            },
            {
                "key": "cost_sensitivity",
                "file": "configs/research_batches/directional_cost_sensitivity_15m.json",
                "description": "Cost model sensitivity test",
            },
            {
                "key": "confirm_ticks",
                "file": "configs/research_batches/directional_confirm_ticks_15m.json",
                "description": "Confirmation ticks sensitivity test",
            },
            {
                "key": "trend_weight",
                "file": "configs/research_batches/directional_trend_weight_15m.json",
                "description": "Trend weight calibration (directional-specific)",
            },
            {
                "key": "return_clamp",
                "file": "configs/research_batches/directional_return_clamp_15m.json",
                "description": "Return clamp calibration (directional-specific)",
            },
        ],
    },
    "directional_1h": {
        "family": "directional",
        "timeframe": "1H",
        "description": "Step 2-C: directional / 1H calibration",
        "batches": [
            {
                "key": "scale_calibration",
                "file": "configs/research_batches/directional_scale_calibration_1h.json",
                "description": "Signal edge scale calibration",
            },
            {
                "key": "cost_sensitivity",
                "file": "configs/research_batches/directional_cost_sensitivity_1h.json",
                "description": "Cost model sensitivity test",
            },
            {
                "key": "confirm_ticks",
                "file": "configs/research_batches/directional_confirm_ticks_1h.json",
                "description": "Confirmation ticks sensitivity test",
            },
            {
                "key": "trend_weight",
                "file": "configs/research_batches/directional_trend_weight_1h.json",
                "description": "Trend weight calibration (directional-specific)",
            },
            {
                "key": "return_clamp",
                "file": "configs/research_batches/directional_return_clamp_1h.json",
                "description": "Return clamp calibration (directional-specific)",
            },
        ],
    },
}

# ---------- Scan 定义 ----------
_SCAN_MATRIX_FILE = "configs/research_rounds/step2_formal_scan_matrix.json"

# 内嵌 fallback（若配置文件缺失时使用）
_DEFAULT_SCAN_DEFS: dict[str, dict[str, Any]] = {
    "independent_15m": {
        "family": "independent", "timeframe": "15m",
        "grid": {
            "min_confirm_ticks": [2, 3, 4],
            "signal_edge_scale_bps": [10, 12, 15],
            "min_safe_net_edge_bps": [0, 5, 10],
        },
    },
    "independent_1h": {
        "family": "independent", "timeframe": "1H",
        "grid": {
            "min_confirm_ticks": [2, 3, 4],
            "signal_edge_scale_bps": [10, 12, 15],
            "min_safe_net_edge_bps": [0, 5, 10],
        },
    },
    "directional_15m": {
        "family": "directional", "timeframe": "15m",
        "grid": {
            "min_confirm_ticks": [2, 3],
            "directional_trend_weight": [0.5, 0.7, 0.9],
            "directional_return_clamp_bps": [15, 20, 30],
        },
    },
    "directional_1h": {
        "family": "directional", "timeframe": "1H",
        "grid": {
            "min_confirm_ticks": [2, 3],
            "directional_trend_weight": [0.5, 0.7, 0.9],
            "directional_return_clamp_bps": [15, 20, 30],
        },
    },
}

_DEFAULT_ARTIFACT_ROOT = pathlib.Path("artifacts/research/step2_rounds")
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
_EXPECTED_STEP2_CALIBRATION_TOPOLOGY = {
    key: (
        str(_CALIBRATION_DEFS[key]["family"]),
        str(_CALIBRATION_DEFS[key]["timeframe"]),
        tuple(str(batch["key"]) for batch in _CALIBRATION_DEFS[key]["batches"]),
    )
    for key in _EXPECTED_STEP2_CALIBRATION_KEYS
}
_EXPECTED_STEP2_SCAN_TOPOLOGY = {
    "independent_15m": ("independent", "15m"),
    "independent_1h": ("independent", "1H"),
    "directional_15m": ("directional", "15m"),
    "directional_1h": ("directional", "1H"),
}
_BATCH_RUN_ID_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{8}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _normalize_dataset_version(value: str | None) -> str:
    normalized = (value or "v1.0").strip()
    return "v1.0" if normalized == "v1" else normalized


def _result_set_is_exact_and_succeeded(
    results: list[dict[str, Any]],
    *,
    key_field: str,
    expected_keys: tuple[str, ...],
    expected_topology: dict[str, tuple[Any, ...]],
) -> bool:
    keys = [item.get(key_field) for item in results]
    if not (
        len(results) == len(expected_keys)
        and all(isinstance(key, str) and key for key in keys)
        and len(set(keys)) == len(keys)
        and set(keys) == set(expected_keys)
        and all(item.get("status") == "succeeded" for item in results)
    ):
        return False
    for item in results:
        key = str(item[key_field])
        expected = expected_topology[key]
        if item.get("family") != expected[0] or item.get("timeframe") != expected[1]:
            return False
        if key_field == "round_key":
            batches = item.get("batch_results")
            if batches is None:
                batches = item.get("batches")
            if not isinstance(batches, list) or not all(
                isinstance(batch, dict) for batch in batches
            ):
                return False
            batch_keys = [
                batch.get("_key", batch.get("key")) for batch in batches
            ]
            if (
                len(batch_keys) != len(expected[2])
                or len(set(batch_keys)) != len(batch_keys)
                or set(batch_keys) != set(expected[2])
                or any(batch.get("status") != "succeeded" for batch in batches)
                or any(
                    not isinstance(batch.get("batch_run_id"), str)
                    or _BATCH_RUN_ID_RE.fullmatch(batch["batch_run_id"]) is None
                    or not isinstance(batch.get("batch_dir"), str)
                    or not isinstance(batch.get("summary_sha256"), str)
                    or _SHA256_RE.fullmatch(batch["summary_sha256"]) is None
                    or type(batch.get("summary_size_bytes")) is not int
                    or batch["summary_size_bytes"] <= 0
                    or type(batch.get("total_experiments")) is not int
                    or type(batch.get("succeeded")) is not int
                    or type(batch.get("failed")) is not int
                    or batch["total_experiments"] <= 0
                    or batch["succeeded"] != batch["total_experiments"]
                    or batch["failed"] != 0
                    for batch in batches
                )
            ):
                return False
        else:
            if (
                not isinstance(item.get("scan_run_id"), str)
                or _UUID_RE.fullmatch(item["scan_run_id"]) is None
                or not isinstance(item.get("scan_dir"), str)
                or not isinstance(item.get("comparison_sha256"), str)
                or _SHA256_RE.fullmatch(item["comparison_sha256"]) is None
                or type(item.get("comparison_size_bytes")) is not int
                or item["comparison_size_bytes"] <= 0
                or not isinstance(item.get("window"), dict)
                or not isinstance(item.get("dataset_version"), str)
                or not isinstance(item.get("grid_sha256"), str)
                or _SHA256_RE.fullmatch(item["grid_sha256"]) is None
                or type(item.get("total_combinations")) is not int
                or type(item.get("completed_count")) is not int
                or type(item.get("failed_count")) is not int
                or item["total_combinations"] <= 0
                or item["completed_count"] != item["total_combinations"]
                or item["failed_count"] != 0
            ):
                return False
    return True


def _determine_step2_round_status(
    *,
    calibration_results: list[dict[str, Any]],
    scan_results: list[dict[str, Any]],
    parameter_candidates_payload: dict[str, Any],
    start: str | None,
    end: str | None,
) -> str:
    """Return status only from an exact, unique Step 2 evidence topology."""

    all_results = [*calibration_results, *scan_results]
    if all_results and all(item.get("status") == "failed" for item in all_results):
        return "failed"
    calibration_complete = _result_set_is_exact_and_succeeded(
        calibration_results,
        key_field="round_key",
        expected_keys=_EXPECTED_STEP2_CALIBRATION_KEYS,
        expected_topology=_EXPECTED_STEP2_CALIBRATION_TOPOLOGY,
    )
    scan_complete = _result_set_is_exact_and_succeeded(
        scan_results,
        key_field="scan_key",
        expected_keys=_EXPECTED_STEP2_SCAN_KEYS,
        expected_topology=_EXPECTED_STEP2_SCAN_TOPOLOGY,
    )
    candidates = parameter_candidates_payload.get("candidates")
    pending_validation = parameter_candidates_payload.get("pending_validation")
    candidate_complete = bool(
        isinstance(candidates, dict)
        and set(candidates) == _EXPECTED_STEP2_COMBO_KEYS
        and all(isinstance(values, dict) and values for values in candidates.values())
        and isinstance(pending_validation, list)
        and not pending_validation
    )
    if (
        calibration_complete
        and scan_complete
        and isinstance(start, str)
        and bool(start.strip())
        and isinstance(end, str)
        and bool(end.strip())
        and candidate_complete
    ):
        return "succeeded"
    return "partial_success"

# =========================================================================
# 1. 子进程：Calibration Batch
# =========================================================================


def _list_subdirs(path: pathlib.Path) -> set[str]:
    if not path.exists():
        return set()
    return {d.name for d in path.iterdir() if d.is_dir()}


def _run_batch(
    batch_file: str,
    batch_artifact_root: pathlib.Path,
    *,
    ensure_schema: bool = False,
    stop_on_error: bool = False,
    start: str | None = None,
    end: str | None = None,
    dataset_version: str | None = None,
    expected_family: str | None = None,
    expected_timeframe: str | None = None,
) -> dict[str, Any]:
    """通过子进程调用 rdp_run_calibration_batch.py 运行单个 batch。"""
    result_sidecar = (
        batch_artifact_root.parent
        / "batch_results"
        / f"{uuid4().hex}.json"
    )

    cmd = [
        sys.executable, "scripts/rdp_run_calibration_batch.py",
        "--batch-file", str(batch_file),
        "--artifact-root", str(batch_artifact_root),
        "--result-json", str(result_sidecar),
        "--no-print-summary",
    ]
    if ensure_schema:
        cmd.append("--ensure-schema")
    if stop_on_error:
        cmd.append("--stop-on-error")
    if start:
        cmd.extend(["--start", start])
    if end:
        cmd.extend(["--end", end])
    if dataset_version:
        cmd.extend(["--dataset-version", _normalize_dataset_version(dataset_version)])

    log.info("  CMD: %s", " ".join(cmd))
    proc = subprocess.run(cmd)

    try:
        result_payload = json.loads(result_sidecar.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {
            "status": "failed",
            "exit_code": proc.returncode,
            "batch_run_id": None,
            "batch_dir": None,
            "summary": None,
            "error": "calibration_batch_result_missing_or_invalid",
        }
    if not isinstance(result_payload, dict):
        return {
            "status": "failed",
            "exit_code": proc.returncode,
            "batch_run_id": None,
            "batch_dir": None,
            "summary": None,
            "error": "calibration_batch_result_missing_or_invalid",
        }
    expected_status = {0: "succeeded", 2: "partial_success", 3: "failed"}.get(
        proc.returncode
    )
    batch_run_id = result_payload.get("batch_run_id")
    batch_dir_raw = result_payload.get("batch_dir")
    summary_path_raw = result_payload.get("summary_path")
    summary_sha256 = result_payload.get("summary_sha256")
    summary_size_bytes = result_payload.get("summary_size_bytes")
    try:
        batch_spec = json.loads(pathlib.Path(batch_file).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        batch_spec = None
    if (
        result_payload.get("schema_version")
        != "aats.calibration_batch_result.v1"
        or expected_status is None
        or result_payload.get("status") != expected_status
        or not isinstance(batch_run_id, str)
        or _BATCH_RUN_ID_RE.fullmatch(batch_run_id) is None
        or not isinstance(batch_dir_raw, str)
        or not isinstance(summary_path_raw, str)
        or not isinstance(summary_sha256, str)
        or _SHA256_RE.fullmatch(summary_sha256) is None
        or type(summary_size_bytes) is not int
        or not isinstance(batch_spec, dict)
        or result_payload.get("batch_name") != batch_spec.get("batch_name")
        or result_payload.get("family")
        != (expected_family or batch_spec.get("family"))
        or result_payload.get("symbol") != _SYMBOL
        or result_payload.get("timeframe")
        != (expected_timeframe or batch_spec.get("timeframe"))
        or result_payload.get("dataset_version")
        != _normalize_dataset_version(
            dataset_version or batch_spec.get("dataset_version", "v1.0")
        )
        or result_payload.get("window")
        != {
            "start": start or batch_spec.get("start"),
            "end": end or batch_spec.get("end"),
        }
    ):
        return {
            "status": "failed",
            "exit_code": proc.returncode,
            "batch_run_id": None,
            "batch_dir": None,
            "summary": None,
            "error": "calibration_batch_result_contract_invalid",
        }
    batch_dir = pathlib.Path(batch_dir_raw)
    summary_file = pathlib.Path(summary_path_raw)
    expected_batch_dir = batch_artifact_root.resolve() / batch_run_id
    expected_summary_file = expected_batch_dir / "batch_summary.json"
    try:
        if batch_dir.is_symlink() or summary_file.is_symlink():
            raise ValueError
        resolved_batch_dir = batch_dir.resolve(strict=True)
        resolved_summary_file = summary_file.resolve(strict=True)
        summary_bytes = resolved_summary_file.read_bytes()
        summary = json.loads(summary_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return {
            "status": "failed",
            "exit_code": proc.returncode,
            "batch_run_id": None,
            "batch_dir": None,
            "summary": None,
            "error": "calibration_batch_artifact_invalid",
        }
    if (
        resolved_batch_dir != expected_batch_dir
        or resolved_summary_file != expected_summary_file
        or not isinstance(summary, dict)
        or len(summary_bytes) != summary_size_bytes
        or hashlib.sha256(summary_bytes).hexdigest() != summary_sha256
        or summary.get("batch_run_id") != batch_run_id
        or summary.get("batch_name") != result_payload.get("batch_name")
        or summary.get("family") != result_payload.get("family")
        or summary.get("symbol") != result_payload.get("symbol")
        or summary.get("timeframe") != result_payload.get("timeframe")
        or summary.get("dataset_version") != result_payload.get("dataset_version")
        or summary.get("window")
        != f"{result_payload['window']['start']} ~ {result_payload['window']['end']}"
        or summary.get("total_experiments")
        != result_payload.get("total_experiments")
        or summary.get("succeeded") != result_payload.get("succeeded")
        or summary.get("failed") != result_payload.get("failed")
    ):
        return {
            "status": "failed",
            "exit_code": proc.returncode,
            "batch_run_id": None,
            "batch_dir": None,
            "summary": None,
            "error": "calibration_batch_summary_identity_invalid",
        }
    try:
        validate_calibration_batch_summary(
            summary,
            expected_counts=(
                result_payload.get("total_experiments"),
                result_payload.get("succeeded"),
                result_payload.get("failed"),
            ),
            expected_status=expected_status,
            expected_experiments=batch_spec.get("experiments"),
        )
    except ValueError:
        return {
            "status": "failed",
            "exit_code": proc.returncode,
            "batch_run_id": None,
            "batch_dir": None,
            "summary": None,
            "error": "calibration_batch_summary_semantics_invalid",
        }

    return {
        "status": expected_status,
        "exit_code": proc.returncode,
        "batch_run_id": batch_run_id,
        "batch_dir": str(resolved_batch_dir),
        "summary": summary,
        "summary_sha256": summary_sha256,
        "summary_size_bytes": summary_size_bytes,
        "total_experiments": result_payload["total_experiments"],
        "succeeded": result_payload["succeeded"],
        "failed": result_payload["failed"],
        "error": None,
    }


def _run_calibration_round(
    round_key: str,
    cal_def: dict[str, Any],
    batch_artifact_root: pathlib.Path,
    *,
    ensure_schema: bool = False,
    stop_on_error: bool = False,
    start: str | None = None,
    end: str | None = None,
    dataset_version: str | None = None,
) -> dict[str, Any]:
    """运行一个 calibration round（一个 family/tf 组合的全部 batch）。"""
    family = cal_def["family"]
    timeframe = cal_def["timeframe"]
    batches = cal_def["batches"]

    log.info("")
    log.info("=" * 60)
    log.info("Calibration: %s (%s / %s), %d batches",
             round_key, family, timeframe, len(batches))
    log.info("=" * 60)

    batch_results: list[dict[str, Any]] = []
    for i, bdef in enumerate(batches):
        log.info("")
        log.info("  [Batch %d/%d] %s", i + 1, len(batches), bdef["description"])
        log.info("    File: %s", bdef["file"])

        ensure = ensure_schema and (i == 0) and (round_key == "independent_1h")
        result = _run_batch(
            bdef["file"], batch_artifact_root,
            ensure_schema=ensure,
            stop_on_error=stop_on_error,
            start=start,
            end=end,
            dataset_version=dataset_version,
            expected_family=family,
            expected_timeframe=timeframe,
        )
        result["_key"] = bdef["key"]

        if result["status"] == "succeeded":
            s = result.get("summary", {})
            log.info("    -> SUCCEEDED (%d experiments)", s.get("succeeded", 0))
        elif result["status"] == "partial_success":
            s = result.get("summary", {})
            log.info("    -> PARTIAL (%d ok, %d failed)",
                     s.get("succeeded", 0), s.get("failed", 0))
        else:
            log.error("    -> FAILED: %s", (result.get("error") or "")[:200])

        batch_results.append(result)
        if result["status"] == "failed" and stop_on_error:
            log.error("  --stop-on-error: aborting calibration round %s", round_key)
            break

    n_ok = sum(1 for b in batch_results if b["status"] == "succeeded")
    n_partial = sum(1 for b in batch_results if b["status"] == "partial_success")
    n_fail = sum(1 for b in batch_results if b["status"] == "failed")
    if n_fail > 0 and n_ok == 0 and n_partial == 0:
        round_status = "failed"
    elif n_fail > 0 or n_partial > 0:
        round_status = "partial_success"
    else:
        round_status = "succeeded"

    return {
        "round_key": round_key,
        "family": family,
        "timeframe": timeframe,
        "batch_results": batch_results,
        "status": round_status,
    }


# =========================================================================
# 2. 子进程：Formal Parameter Scan
# =========================================================================


def _run_scan(
    scan_key: str,
    scan_def: dict[str, Any],
    *,
    result_root: pathlib.Path,
    ensure_schema: bool = False,
) -> dict[str, Any]:
    """通过子进程调用 rdp_run_parameter_scan.py 运行正式 scan。"""
    family = scan_def["family"]
    timeframe = scan_def["timeframe"]
    grid = scan_def["grid"]

    log.info("")
    log.info("  [Scan] %s: %s / %s", scan_key, family, timeframe)
    grid_size = 1
    for vs in grid.values():
        grid_size *= len(vs)
    log.info("    Grid: %s (%d combos)", json.dumps(grid, ensure_ascii=False), grid_size)

    # 从 scan matrix 配置读 start/end 或使用默认
    start = scan_def.get("start", "2026-03-31")
    end = scan_def.get("end", "2026-04-02")
    dataset_version = _normalize_dataset_version(scan_def.get("dataset_version", "v1.0"))
    result_sidecar = result_root / f"{scan_key}.json"
    canonical_grid = json.dumps(
        grid,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    expected_grid_sha256 = hashlib.sha256(canonical_grid).hexdigest()

    cmd = [
        sys.executable, "scripts/rdp_run_parameter_scan.py",
        "--family", family,
        "--symbol", _SYMBOL,
        "--timeframe", timeframe,
        "--start", start,
        "--end", end,
        "--dataset-version", dataset_version,
        "--grid", json.dumps(grid, ensure_ascii=False),
        "--result-json", str(result_sidecar),
    ]
    if ensure_schema:
        cmd.append("--ensure-schema")

    log.info("    CMD: %s", " ".join(cmd))

    scan_artifact_root = pathlib.Path("artifacts/research/experiments")
    proc = subprocess.run(cmd)
    try:
        result_payload = json.loads(result_sidecar.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {
            "scan_key": scan_key,
            "family": family,
            "timeframe": timeframe,
            "status": "failed",
            "exit_code": proc.returncode,
            "scan_run_id": None,
            "scan_dir": None,
            "comparison": None,
            "error": "parameter_scan_result_missing_or_invalid",
        }
    if not isinstance(result_payload, dict):
        return {
            "scan_key": scan_key,
            "family": family,
            "timeframe": timeframe,
            "status": "failed",
            "exit_code": proc.returncode,
            "scan_run_id": None,
            "scan_dir": None,
            "comparison": None,
            "error": "parameter_scan_result_missing_or_invalid",
        }
    expected_status = {0: "succeeded", 2: "partial_success", 3: "failed"}.get(
        proc.returncode
    )
    scan_run_id = result_payload.get("scan_run_id")
    scan_dir_raw = result_payload.get("scan_dir")
    comparison_path_raw = result_payload.get("comparison_path")
    comparison_sha256 = result_payload.get("comparison_sha256")
    comparison_size_bytes = result_payload.get("comparison_size_bytes")
    if (
        result_payload.get("schema_version") != "aats.parameter_scan_result.v1"
        or expected_status is None
        or result_payload.get("status") != expected_status
        or not isinstance(scan_run_id, str)
        or _UUID_RE.fullmatch(scan_run_id) is None
        or not isinstance(scan_dir_raw, str)
        or result_payload.get("family") != family
        or result_payload.get("symbol") != _SYMBOL
        or result_payload.get("timeframe") != timeframe
        or result_payload.get("dataset_version") != dataset_version
        or result_payload.get("window") != {"start": start, "end": end}
        or result_payload.get("grid_sha256") != expected_grid_sha256
        or type(result_payload.get("total_combinations")) is not int
        or result_payload.get("total_combinations") != grid_size
        or type(result_payload.get("completed_count")) is not int
        or type(result_payload.get("failed_count")) is not int
        or result_payload.get("completed_count") < 0
        or result_payload.get("failed_count") < 0
        or result_payload.get("completed_count")
        + result_payload.get("failed_count")
        != grid_size
        or (
            expected_status == "succeeded"
            and (
                result_payload.get("completed_count") != grid_size
                or result_payload.get("failed_count") != 0
            )
        )
        or (
            expected_status == "partial_success"
            and not (
                0 < result_payload.get("completed_count") < grid_size
                and 0 < result_payload.get("failed_count") < grid_size
            )
        )
        or (
            expected_status == "failed"
            and (
                result_payload.get("completed_count") != 0
                or result_payload.get("failed_count") != grid_size
            )
        )
    ):
        return {
            "scan_key": scan_key,
            "family": family,
            "timeframe": timeframe,
            "status": "failed",
            "exit_code": proc.returncode,
            "scan_run_id": None,
            "scan_dir": None,
            "comparison": None,
            "error": "parameter_scan_result_contract_invalid",
        }
    scan_dir = pathlib.Path(scan_dir_raw)
    expected_scan_dir = scan_artifact_root.resolve() / scan_run_id
    comparison: dict[str, Any] | None = None
    try:
        if scan_dir.is_symlink():
            raise ValueError
        resolved_scan_dir = scan_dir.resolve(strict=True)
        if resolved_scan_dir != expected_scan_dir:
            raise ValueError
        if expected_status in {"succeeded", "partial_success"}:
            if (
                not isinstance(comparison_path_raw, str)
                or not isinstance(comparison_sha256, str)
                or _SHA256_RE.fullmatch(comparison_sha256) is None
                or type(comparison_size_bytes) is not int
            ):
                raise ValueError
            comp_file = pathlib.Path(comparison_path_raw)
            if comp_file.is_symlink():
                raise ValueError
            resolved_comp_file = comp_file.resolve(strict=True)
            if resolved_comp_file != resolved_scan_dir / "comparison_summary.json":
                raise ValueError
            comparison_bytes = resolved_comp_file.read_bytes()
            if (
                len(comparison_bytes) != comparison_size_bytes
                or hashlib.sha256(comparison_bytes).hexdigest()
                != comparison_sha256
            ):
                raise ValueError
            comparison = json.loads(comparison_bytes.decode("utf-8"))
            validate_scan_comparison(
                comparison,
                expected_counts=(
                    result_payload.get("total_combinations"),
                    result_payload.get("completed_count"),
                    result_payload.get("failed_count"),
                ),
            )
        elif any(
            value is not None
            for value in (
                comparison_path_raw,
                comparison_sha256,
                comparison_size_bytes,
            )
        ):
            raise ValueError
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return {
            "scan_key": scan_key,
            "family": family,
            "timeframe": timeframe,
            "status": "failed",
            "exit_code": proc.returncode,
            "scan_run_id": None,
            "scan_dir": None,
            "comparison": None,
            "error": "parameter_scan_artifact_identity_invalid",
        }

    return {
        "scan_key": scan_key,
        "family": family,
        "timeframe": timeframe,
        "status": expected_status,
        "exit_code": proc.returncode,
        "scan_run_id": scan_run_id,
        "scan_dir": str(resolved_scan_dir),
        "comparison": comparison,
        "comparison_sha256": comparison_sha256,
        "comparison_size_bytes": comparison_size_bytes,
        "window": {"start": start, "end": end},
        "dataset_version": dataset_version,
        "grid_sha256": expected_grid_sha256,
        "total_combinations": result_payload["total_combinations"],
        "completed_count": result_payload["completed_count"],
        "failed_count": result_payload["failed_count"],
        "error": None,
    }


# =========================================================================
# 3. 数据聚合
# =========================================================================

_FT_SUMMARY_COLUMNS = [
    "family", "symbol", "timeframe", "batch_name", "label", "experiment_id", "status",
    "opening_count", "blocked_count", "selectable_ratio",
    "execution_compatible_ratio",
    "mean_signal_edge_proxy_bps", "mean_funding_adjustment_bps",
    "mean_cost_bps", "mean_expected_edge_bps", "positive_edge_ratio",
    "top_blocking_reason_1", "top_blocking_reason_2",
    "result_path", "summary_path", "report_path",
]


def _collect_calibration_experiments(
    calibration_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从所有 calibration round 中汇总实验数据，附加 family / timeframe。"""
    all_rows: list[dict[str, Any]] = []
    for cr in calibration_results:
        family = cr["family"]
        timeframe = cr["timeframe"]
        for br in cr.get("batch_results", []):
            summary = br.get("summary")
            if not summary:
                continue
            batch_name = summary.get("batch_name", "unknown")
            for exp in summary.get("experiments", []):
                row = dict(exp)
                row["family"] = family
                row["symbol"] = _SYMBOL
                row["timeframe"] = timeframe
                row["batch_name"] = batch_name
                all_rows.append(row)
    return all_rows


def _write_family_timeframe_summary_csv(
    rows: list[dict[str, Any]],
    output_path: pathlib.Path,
) -> pathlib.Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_FT_SUMMARY_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    log.info("Wrote family_timeframe_summary.csv -> %s (%d rows)", output_path, len(rows))
    return output_path


def _write_family_timeframe_summary_json(
    rows: list[dict[str, Any]],
    round_id: str,
    output_path: pathlib.Path,
) -> pathlib.Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "round_id": round_id,
        "total_experiments": len(rows),
        "experiments": rows,
    }
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    log.info("Wrote family_timeframe_summary.json -> %s", output_path)
    return output_path


def _build_scan_comparison_summary(
    scan_results: list[dict[str, Any]],
    output_csv: pathlib.Path,
    output_json: pathlib.Path,
) -> None:
    """汇总全部 scan 结果到统一比较表。"""
    output_csv.parent.mkdir(parents=True, exist_ok=True)

    all_rows: list[dict[str, Any]] = []
    for sr in scan_results:
        comp = sr.get("comparison")
        if not comp:
            continue
        family = sr["family"]
        timeframe = sr["timeframe"]
        scan_key = sr["scan_key"]

        experiments = extract_comparison_rows(comp)
        for exp in experiments:
            row = dict(exp)
            row["family"] = family
            row["symbol"] = _SYMBOL
            row["timeframe"] = timeframe
            row["scan_key"] = scan_key
            row["scan_run_id"] = sr.get("scan_run_id")
            all_rows.append(row)

    # CSV
    if all_rows:
        fieldnames = list(all_rows[0].keys())
        with output_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in all_rows:
                writer.writerow(row)
        log.info("Wrote scan_comparison_summary.csv -> %s (%d rows)", output_csv, len(all_rows))
    else:
        log.warning("No scan data for comparison summary")

    # JSON
    data = {
        "total_scans": len(scan_results),
        "succeeded_scans": sum(1 for s in scan_results if s["status"] == "succeeded"),
        "total_experiments": len(all_rows),
        "experiments": all_rows,
    }
    with output_json.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    log.info("Wrote scan_comparison_summary.json -> %s", output_json)


# =========================================================================
# 4. 规则化推荐引擎（扩展 Step 1，支持 directional 特有参数）
# =========================================================================


def _get_experiments_by_batch(
    all_rows: list[dict[str, Any]], batch_name: str,
) -> list[dict[str, Any]]:
    return [r for r in all_rows if r.get("batch_name") == batch_name]


def _get_experiments_by_family_tf(
    all_rows: list[dict[str, Any]], family: str, timeframe: str,
) -> list[dict[str, Any]]:
    return [
        r for r in all_rows
        if r.get("family") == family and r.get("timeframe") == timeframe
    ]


# ---------- 复用 Step 1 的核心推荐规则 ----------


def _recommend_signal_edge_scale(experiments: list[dict[str, Any]]) -> dict[str, Any]:
    """规则化推荐 signal_edge_scale_bps（与 Step 1 相同逻辑）。"""
    if not experiments:
        return {"value": None, "confidence": "low",
                "reason": "Scale calibration batch 未运行或无数据"}

    sorted_exps = sorted(
        experiments,
        key=lambda e: e.get("params", {}).get("signal_edge_scale_bps", 0),
    )
    viable = [
        e for e in sorted_exps
        if (e.get("mean_expected_edge_bps") or 0) > 0
        and (e.get("opening_count") or 0) > 0
        and (e.get("execution_compatible_ratio") or 0) > 0
    ]

    if not viable:
        best = sorted_exps[-1]
        scale = best.get("params", {}).get("signal_edge_scale_bps")
        edge = best.get("mean_expected_edge_bps") or 0
        opens = best.get("opening_count", 0)
        exec_ratio = best.get("execution_compatible_ratio", 0) or 0
        return {
            "value": None, "confidence": "low",
            "reason": (
                f"No scale is both positive-edge and tradable. "
                f"Best tested scale ({scale}) has edge {edge:.2f} bps, "
                f"opens={opens}, exec_ratio={exec_ratio:.1%}."
            ),
        }

    recommended = viable[0]
    for i in range(1, len(viable)):
        prev = viable[i - 1]
        curr = viable[i]
        prev_opens = max(prev.get("opening_count", 0), 1)
        curr_opens = curr.get("opening_count", 0)
        prev_pos = prev.get("positive_edge_ratio", 0)
        curr_pos = curr.get("positive_edge_ratio", 0)

        if (curr_opens - prev_opens) / prev_opens > 0.10 or curr_pos - prev_pos > 0.15:
            recommended = curr
        else:
            break

    scale = recommended.get("params", {}).get("signal_edge_scale_bps")
    edge = recommended.get("mean_expected_edge_bps", 0) or 0
    opens = recommended.get("opening_count", 0)
    pos_ratio = recommended.get("positive_edge_ratio", 0)

    confidence = "high" if pos_ratio > 0.7 else ("medium" if pos_ratio > 0.4 else "low")

    return {
        "value": scale, "confidence": confidence,
        "reason": (
            f"scale={scale} 是满足 positive edge 条件下结构最优的平衡点 "
            f"(edge={edge:.2f}bps, opens={opens}, pos_ratio={pos_ratio:.1%})."
        ),
    }


def _extract_total_cost(params: dict[str, Any]) -> float:
    """从实验 params 中提取总成本 bps（使用混合费率模型）。"""
    cc = params.get("cost_config")
    if isinstance(cc, dict):
        return ReplayCostConfig.from_dict(cc).total_cost_bps
    return ReplayCostConfig.from_dict(params).total_cost_bps


def _extract_cost_parts(params: dict[str, Any]) -> tuple[float, float]:
    cc = params.get("cost_config", {})
    if isinstance(cc, dict):
        return float(cc.get("taker_fee_bps", 5.0)), float(cc.get("slippage_bps", 1.0))
    return float(params.get("taker_fee_bps", 5.0)), float(params.get("slippage_bps", 1.0))


def _recommend_cost_model(experiments: list[dict[str, Any]]) -> dict[str, Any]:
    """规则化推荐 cost model（与 Step 1 相同逻辑）。"""
    if not experiments:
        return {
            "taker_fee_bps": {"value": 5.0, "confidence": "low",
                              "reason": "Cost batch 未运行或无数据, 保留默认值"},
            "slippage_bps": {"value": 1.0, "confidence": "low",
                             "reason": "Cost batch 未运行或无数据, 保留默认值"},
            "overall_confidence": "low",
            "overall_reason": "Cost sensitivity batch 无数据",
        }

    sorted_exps = sorted(
        experiments,
        key=lambda e: _extract_total_cost(e.get("params", {})),
    )

    # 找默认值实验
    # 容差 0.5 bps：混合费率默认值 ≈5.605 为浮点数，
    # 实验参数经 JSON 序列化往返后可能有微小精度偏移，0.01 不够稳健。
    default_total_cost = ReplayCostConfig().total_cost_bps
    default_exp = None
    for e in sorted_exps:
        if abs(_extract_total_cost(e.get("params", {})) - default_total_cost) < 0.5:
            default_exp = e
            break

    # 敏感度分析
    if len(sorted_exps) >= 2:
        first, last = sorted_exps[0], sorted_exps[-1]
        first_opens = first.get("opening_count", 0)
        last_opens = last.get("opening_count", 0)
        cost_range = _extract_total_cost(last.get("params", {})) - _extract_total_cost(first.get("params", {}))
        if first_opens > 0 and cost_range > 0:
            drop_pct = (first_opens - last_opens) / first_opens
            sensitivity_note = (
                f"Opening drops {drop_pct:.0%} across {cost_range:.0f}bps cost range "
                f"({first_opens}->{last_opens})."
            )
        else:
            sensitivity_note = "Insufficient data for sensitivity analysis."
    else:
        sensitivity_note = "Only 1 cost point tested."

    taker, slip = 5.0, 1.0
    confidence = "low"
    reason = sensitivity_note

    if default_exp:
        default_edge = default_exp.get("mean_expected_edge_bps") or 0
        default_opens = default_exp.get("opening_count", 0)
        taker, slip = _extract_cost_parts(default_exp.get("params", {}))

        if default_edge > 0:
            confidence = "medium"
            reason = (
                f"Default cost (taker={taker}, slip={slip}, total={taker + slip}bps) "
                f"achieves positive edge ({default_edge:.2f}bps) with {default_opens} opens. "
                f"{sensitivity_note}"
            )
        else:
            lower_positive = [
                e for e in sorted_exps
                if _extract_total_cost(e.get("params", {})) < default_total_cost
                and (e.get("mean_expected_edge_bps") or 0) > 0
            ]
            if lower_positive:
                confidence = "medium"
                reason = (
                    f"Default cost ({taker + slip}bps) yields negative edge ({default_edge:.2f}bps), "
                    f"but lower cost achieves positive edge — edge fragile. "
                    f"{sensitivity_note}"
                )
            else:
                confidence = "low"
                reason = (
                    f"Default cost ({taker + slip}bps) yields negative edge ({default_edge:.2f}bps) "
                    f"and no cost achieves positive edge. {sensitivity_note}"
                )
    else:
        balanced = sorted_exps[len(sorted_exps) // 2]
        taker, slip = _extract_cost_parts(balanced.get("params", {}))
        confidence = "low"
        reason = f"Default (5,2) not in tested range. Median cost used. {sensitivity_note}"

    return {
        "taker_fee_bps": {"value": taker, "confidence": confidence, "reason": reason},
        "slippage_bps": {"value": slip, "confidence": confidence, "reason": reason},
        "overall_confidence": confidence,
        "overall_reason": reason,
    }


def _recommend_confirm_ticks(experiments: list[dict[str, Any]]) -> dict[str, Any]:
    """规则化推荐 min_confirm_ticks（与 Step 1 相同逻辑）。"""
    if not experiments:
        return {
            "value": None,
            "confidence": "low",
            "reason": "Confirm ticks batch 未运行或无数据，无法给出可交易推荐",
        }

    sorted_exps = sorted(
        experiments,
        key=lambda e: e.get("params", {}).get("min_confirm_ticks", 0),
    )
    tradable = [
        e for e in sorted_exps
        if (e.get("opening_count") or 0) > 0
        and (e.get("execution_compatible_ratio") or 0) > 0
    ]
    if not tradable:
        return {
            "value": None,
            "confidence": "low",
            "reason": "All confirm-ticks candidates are non-tradable (opens=0 or exec_ratio=0).",
        }

    recommended = tradable[0]
    for i in range(1, len(tradable)):
        prev = tradable[i - 1]
        curr = tradable[i]
        prev_opens = max(prev.get("opening_count", 0), 1)
        curr_opens = curr.get("opening_count", 0)
        drop_ratio = (prev_opens - curr_opens) / prev_opens

        if drop_ratio > 0.40:
            break
        top_reason = curr.get("top_blocking_reason_1", "")
        if top_reason == "score_not_stable" and curr_opens == 0:
            break
        recommended = curr

    ticks = recommended.get("params", {}).get("min_confirm_ticks", 2)
    opens = recommended.get("opening_count", 0)
    first_opens = tradable[0].get("opening_count", 0)
    last_opens = tradable[-1].get("opening_count", 0)

    if first_opens > 0 and last_opens > 0:
        confidence = "high"
    elif first_opens > 0:
        confidence = "medium"
    else:
        confidence = "low"

    ticks_opens = [(
        e.get("params", {}).get("min_confirm_ticks"),
        e.get("opening_count", 0),
    ) for e in tradable]

    return {
        "value": ticks, "confidence": confidence,
        "reason": (
            f"ticks={ticks} 是在保持可交易前提下最保守的选择 "
            f"(opens={opens}). 候选 ticks: {ticks_opens}."
        ),
    }


# ---------- Directional 特有参数推荐 ----------


def _recommend_trend_weight(experiments: list[dict[str, Any]]) -> dict[str, Any]:
    """规则化推荐 directional_trend_weight。

    规则：
    1. 按 weight 升序排列
    2. 优先选 positive edge 且 opening_count 最高的值
    3. 若多个 weight 的 positive_edge_ratio 接近（<5pp），选更保守（较高 weight = 更多趋势）
    """
    if not experiments:
        return {"value": 0.7, "confidence": "low",
                "reason": "Trend weight batch 未运行或无数据, 保留默认值 0.7"}

    sorted_exps = sorted(
        experiments,
        key=lambda e: e.get("params", {}).get("directional_trend_weight", 0),
    )

    viable = [e for e in sorted_exps if (e.get("mean_expected_edge_bps") or 0) > 0]

    if not viable:
        # 无 positive edge，选 edge 最不差的
        best = max(sorted_exps, key=lambda e: e.get("mean_expected_edge_bps") or -999)
        tw = best.get("params", {}).get("directional_trend_weight", 0.7)
        edge = best.get("mean_expected_edge_bps") or 0
        return {
            "value": tw, "confidence": "low",
            "reason": (
                f"No weight achieves positive edge. "
                f"Least negative weight ({tw}) has edge {edge:.2f} bps."
            ),
        }

    # 在 viable 中找最佳平衡：opening 最多 or positive_edge_ratio 最高
    # 若多个接近，prefer 较高 weight（更保守 = 更依赖趋势）
    best = viable[0]
    for e in viable[1:]:
        e_opens = e.get("opening_count", 0)
        best_opens = best.get("opening_count", 0)
        e_pos = e.get("positive_edge_ratio", 0)
        best_pos = best.get("positive_edge_ratio", 0)

        # 显著更好：opens 多 10%+ 或 pos_ratio 高 5pp+
        if e_opens > best_opens * 1.10 or e_pos - best_pos > 0.05:
            best = e
        elif abs(e_pos - best_pos) < 0.05 and abs(e_opens - best_opens) <= max(best_opens * 0.10, 1):
            # 接近时选较高 weight（更保守）
            e_tw = e.get("params", {}).get("directional_trend_weight", 0)
            best_tw = best.get("params", {}).get("directional_trend_weight", 0)
            if e_tw > best_tw:
                best = e

    tw = best.get("params", {}).get("directional_trend_weight", 0.7)
    edge = best.get("mean_expected_edge_bps", 0) or 0
    opens = best.get("opening_count", 0)
    pos_ratio = best.get("positive_edge_ratio", 0)

    confidence = "high" if pos_ratio > 0.7 else ("medium" if pos_ratio > 0.4 else "low")

    tw_summary = [(
        e.get("params", {}).get("directional_trend_weight"),
        e.get("opening_count", 0),
        round(e.get("mean_expected_edge_bps") or 0, 2),
    ) for e in sorted_exps]

    return {
        "value": tw, "confidence": confidence,
        "reason": (
            f"trend_weight={tw} 在 positive edge 条件下表现最优 "
            f"(edge={edge:.2f}bps, opens={opens}, pos_ratio={pos_ratio:.1%}). "
            f"各 weight 的 (opens, edge): {tw_summary}."
        ),
    }


def _recommend_return_clamp(experiments: list[dict[str, Any]]) -> dict[str, Any]:
    """规则化推荐 directional_return_clamp_bps。

    规则：
    1. 按 clamp 升序排列
    2. 太低的 clamp 可能限制信号表达，太高的可能引入噪声
    3. 选 positive edge 下 opening_count 最稳定的值
    4. 检查 clamp 升高时 edge 是否大幅波动（不稳定信号）
    """
    if not experiments:
        return {"value": 20.0, "confidence": "low",
                "reason": "Return clamp batch 未运行或无数据, 保留默认值 20.0"}

    sorted_exps = sorted(
        experiments,
        key=lambda e: e.get("params", {}).get("directional_return_clamp_bps", 0),
    )

    viable = [e for e in sorted_exps if (e.get("mean_expected_edge_bps") or 0) > 0]

    if not viable:
        best = max(sorted_exps, key=lambda e: e.get("mean_expected_edge_bps") or -999)
        clamp = best.get("params", {}).get("directional_return_clamp_bps", 20.0)
        edge = best.get("mean_expected_edge_bps") or 0
        return {
            "value": clamp, "confidence": "low",
            "reason": (
                f"No clamp achieves positive edge. "
                f"Least negative clamp ({clamp}) has edge {edge:.2f} bps."
            ),
        }

    # 在 viable 中选中间偏保守的值：
    # 不要最低（可能过度限制），不要最高（可能引入噪声）
    # 优先 positive_edge_ratio 最高
    best = max(viable, key=lambda e: (
        e.get("positive_edge_ratio", 0),
        e.get("opening_count", 0),
    ))

    clamp = best.get("params", {}).get("directional_return_clamp_bps", 20.0)
    edge = best.get("mean_expected_edge_bps", 0) or 0
    opens = best.get("opening_count", 0)
    pos_ratio = best.get("positive_edge_ratio", 0)

    confidence = "high" if pos_ratio > 0.7 else ("medium" if pos_ratio > 0.4 else "low")

    # 检查稳定性：viable 中 edge 的 max-min 差异
    viable_edges = [(e.get("mean_expected_edge_bps") or 0) for e in viable]
    edge_spread = max(viable_edges) - min(viable_edges) if len(viable_edges) > 1 else 0
    stability_note = ""
    if edge_spread > 5:
        stability_note = f" Edge 跨 clamp 范围波动 {edge_spread:.1f}bps, 信号对 clamp 敏感."
        if confidence == "high":
            confidence = "medium"

    clamp_summary = [(
        e.get("params", {}).get("directional_return_clamp_bps"),
        e.get("opening_count", 0),
        round(e.get("mean_expected_edge_bps") or 0, 2),
    ) for e in sorted_exps]

    return {
        "value": clamp, "confidence": confidence,
        "reason": (
            f"clamp={clamp} 在 positive edge 下表现最优 "
            f"(edge={edge:.2f}bps, opens={opens}, pos_ratio={pos_ratio:.1%}).{stability_note} "
            f"各 clamp: {clamp_summary}."
        ),
    }


def _recommend_net_edge_threshold() -> dict[str, Any]:
    """min_safe_net_edge_bps — 标记为 pending。"""
    return {
        "value": None,
        "confidence": "low",
        "reason": "需要专项 validation batch 或更宽时间窗口数据.",
    }


# ---------- Phase 1 扩展参数推荐 ----------


def _recommend_entry_threshold(experiments: list[dict[str, Any]]) -> dict[str, Any]:
    """规则化推荐 entry_threshold。

    规则：选择 positive edge 条件下 opening_count 最多的阈值。
    阈值过低会产生噪声交易，过高会错过机会。
    """
    if not experiments:
        return {"value": 0.30, "confidence": "low",
                "reason": "Entry threshold batch 未运行或无数据, 保留默认值 0.30"}

    sorted_exps = sorted(
        experiments,
        key=lambda e: e.get("params", {}).get("entry_threshold", 0),
    )
    viable = [e for e in sorted_exps if (e.get("mean_expected_edge_bps") or 0) > 0]

    if not viable:
        best = max(sorted_exps, key=lambda e: e.get("mean_expected_edge_bps") or -999)
        val = best.get("params", {}).get("entry_threshold", 0.30)
        edge = best.get("mean_expected_edge_bps") or 0
        return {
            "value": val, "confidence": "low",
            "reason": (
                f"No entry_threshold achieves positive edge. "
                f"Least negative ({val}) has edge {edge:.2f} bps."
            ),
        }

    # 在 viable 中找最优平衡：positive_edge_ratio 最高且 opening 合理
    best = max(viable, key=lambda e: (
        e.get("positive_edge_ratio", 0),
        e.get("opening_count", 0),
    ))

    val = best.get("params", {}).get("entry_threshold", 0.30)
    edge = best.get("mean_expected_edge_bps", 0) or 0
    opens = best.get("opening_count", 0)
    pos_ratio = best.get("positive_edge_ratio", 0)
    confidence = "high" if pos_ratio > 0.7 else ("medium" if pos_ratio > 0.4 else "low")

    summary = [(
        e.get("params", {}).get("entry_threshold"),
        e.get("opening_count", 0),
        round(e.get("mean_expected_edge_bps") or 0, 2),
    ) for e in sorted_exps]

    return {
        "value": val, "confidence": confidence,
        "reason": (
            f"entry_threshold={val} 在 positive edge 条件下表现最优 "
            f"(edge={edge:.2f}bps, opens={opens}, pos_ratio={pos_ratio:.1%}). "
            f"各阈值: {summary}."
        ),
    }


def _recommend_close_threshold(experiments: list[dict[str, Any]]) -> dict[str, Any]:
    """规则化推荐 close_threshold。

    规则：close 阈值过低会持仓过久（亏损扩大），过高会过早退出（利润不充分）。
    选择 positive_edge_ratio 最高的值。
    """
    if not experiments:
        return {"value": 0.15, "confidence": "low",
                "reason": "Close threshold batch 未运行或无数据, 保留默认值 0.15"}

    sorted_exps = sorted(
        experiments,
        key=lambda e: e.get("params", {}).get("close_threshold", 0),
    )
    viable = [e for e in sorted_exps if (e.get("mean_expected_edge_bps") or 0) > 0]

    if not viable:
        best = max(sorted_exps, key=lambda e: e.get("mean_expected_edge_bps") or -999)
        val = best.get("params", {}).get("close_threshold", 0.15)
        edge = best.get("mean_expected_edge_bps") or 0
        return {
            "value": val, "confidence": "low",
            "reason": f"No close_threshold achieves positive edge. Best ({val}) edge {edge:.2f} bps.",
        }

    best = max(viable, key=lambda e: (
        e.get("positive_edge_ratio", 0),
        e.get("mean_expected_edge_bps") or 0,
    ))

    val = best.get("params", {}).get("close_threshold", 0.15)
    edge = best.get("mean_expected_edge_bps", 0) or 0
    opens = best.get("opening_count", 0)
    pos_ratio = best.get("positive_edge_ratio", 0)
    confidence = "high" if pos_ratio > 0.7 else ("medium" if pos_ratio > 0.4 else "low")

    summary = [(
        e.get("params", {}).get("close_threshold"),
        e.get("opening_count", 0),
        round(e.get("mean_expected_edge_bps") or 0, 2),
    ) for e in sorted_exps]

    return {
        "value": val, "confidence": confidence,
        "reason": (
            f"close_threshold={val} 综合 edge 和 pos_ratio 最优 "
            f"(edge={edge:.2f}bps, opens={opens}, pos_ratio={pos_ratio:.1%}). "
            f"各阈值: {summary}."
        ),
    }


def _recommend_de_risk_edge(experiments: list[dict[str, Any]]) -> dict[str, Any]:
    """规则化推荐 de_risk_net_edge_bps。

    规则：de_risk 阈值越高越保守（更快触发降风险）。
    选择在 positive edge 条件下 opening_count 不显著下降的最高值。
    """
    if not experiments:
        return {"value": 2.0, "confidence": "low",
                "reason": "De-risk edge batch 未运行或无数据, 保留默认值 2.0"}

    sorted_exps = sorted(
        experiments,
        key=lambda e: e.get("params", {}).get("de_risk_net_edge_bps", 0),
    )

    # de_risk 在 replay 层目前不直接影响 opening/edge（它是持仓期风控），
    # 但通过 net_edge 阈值间接影响。选择与 net_edge 最匹配的值。
    viable = [e for e in sorted_exps if (e.get("mean_expected_edge_bps") or 0) > 0]

    if not viable:
        return {"value": 2.0, "confidence": "low",
                "reason": "No de_risk value achieves positive edge. 保留默认值 2.0."}

    # 以最宽松值（最低 de_risk）的开仓数为基线，向上搜索最保守的值
    recommended = viable[0]
    baseline_opens = recommended.get("opening_count", 0)
    for e in viable[1:]:
        e_opens = e.get("opening_count", 0)
        if baseline_opens > 0 and (baseline_opens - e_opens) / baseline_opens < 0.30:
            recommended = e  # 选更高（更保守）的 de_risk

    val = recommended.get("params", {}).get("de_risk_net_edge_bps", 2.0)
    edge = recommended.get("mean_expected_edge_bps", 0) or 0
    opens = recommended.get("opening_count", 0)
    # P2-2: 动态计算置信度（与其他推荐函数对齐）
    pos_ratio = recommended.get("positive_edge_ratio", 0)
    confidence = "high" if pos_ratio > 0.7 else ("medium" if pos_ratio > 0.4 else "low")

    return {
        "value": val, "confidence": confidence,
        "reason": (
            f"de_risk={val} bps: opening 不显著下降前提下最保守 "
            f"(edge={edge:.2f}bps, opens={opens}, pos_ratio={pos_ratio:.1%}, "
            f"baseline_opens={baseline_opens})."
        ),
    }


def _recommend_failed_thesis_edge(experiments: list[dict[str, Any]]) -> dict[str, Any]:
    """规则化推荐 failed_thesis_net_edge_bps。

    规则：failed_thesis 越低（更负）越宽松，越高越激进（更快退出）。
    选择在 positive edge 条件下不过度减少开仓次数的最高（最激进）值。
    约束: 必须 <= de_risk_net_edge_bps（由 __post_init__ 强制执行）。
    """
    if not experiments:
        return {"value": -1.0, "confidence": "low",
                "reason": "Failed thesis edge batch 未运行或无数据, 保留默认值 -1.0"}

    sorted_exps = sorted(
        experiments,
        key=lambda e: e.get("params", {}).get("failed_thesis_net_edge_bps", 0),
    )

    viable = [e for e in sorted_exps if (e.get("mean_expected_edge_bps") or 0) > 0]

    if not viable:
        return {"value": -1.0, "confidence": "low",
                "reason": "No failed_thesis value achieves positive edge. 保留默认值 -1.0."}

    # 以最宽松值（最低/最负 failed_thesis）的开仓数为基线
    recommended = viable[0]
    baseline_opens = recommended.get("opening_count", 0)
    for e in viable[1:]:
        e_opens = e.get("opening_count", 0)
        if baseline_opens > 0 and (baseline_opens - e_opens) / baseline_opens < 0.30:
            recommended = e  # 选更高（更激进）的 failed_thesis

    val = recommended.get("params", {}).get("failed_thesis_net_edge_bps", -1.0)
    edge = recommended.get("mean_expected_edge_bps", 0) or 0
    opens = recommended.get("opening_count", 0)
    pos_ratio = recommended.get("positive_edge_ratio", 0)
    confidence = "high" if pos_ratio > 0.7 else ("medium" if pos_ratio > 0.4 else "low")

    return {
        "value": val, "confidence": confidence,
        "reason": (
            f"failed_thesis={val} bps: opening 不显著下降前提下最激进 "
            f"(edge={edge:.2f}bps, opens={opens}, pos_ratio={pos_ratio:.1%}, "
            f"baseline_opens={baseline_opens})."
        ),
    }


def _recommend_timing(experiments: list[dict[str, Any]]) -> dict[str, Any]:
    """规则化推荐 min_hold_seconds 和 rebalance_cooldown_seconds。

    规则：hold 和 cooldown 越长越保守，选择 positive edge 最高的组合。
    """
    if not experiments:
        return {
            "min_hold_seconds": {"value": 300.0, "confidence": "low",
                                 "reason": "Timing batch 未运行或无数据, 保留默认值"},
            "rebalance_cooldown_seconds": {"value": 120.0, "confidence": "low",
                                           "reason": "Timing batch 未运行或无数据, 保留默认值"},
        }

    viable = [e for e in experiments if (e.get("mean_expected_edge_bps") or 0) > 0]

    if not viable:
        best = max(experiments, key=lambda e: e.get("mean_expected_edge_bps") or -999)
        hold = best.get("params", {}).get("min_hold_seconds", 300.0)
        cool = best.get("params", {}).get("rebalance_cooldown_seconds", 120.0)
        return {
            "min_hold_seconds": {"value": hold, "confidence": "low",
                                 "reason": "No timing combo achieves positive edge."},
            "rebalance_cooldown_seconds": {"value": cool, "confidence": "low",
                                           "reason": "No timing combo achieves positive edge."},
        }

    best = max(viable, key=lambda e: (
        e.get("positive_edge_ratio", 0),
        e.get("mean_expected_edge_bps") or 0,
    ))

    hold = best.get("params", {}).get("min_hold_seconds", 300.0)
    cool = best.get("params", {}).get("rebalance_cooldown_seconds", 120.0)
    edge = best.get("mean_expected_edge_bps", 0) or 0
    pos_ratio = best.get("positive_edge_ratio", 0)
    confidence = "high" if pos_ratio > 0.7 else ("medium" if pos_ratio > 0.4 else "low")

    return {
        "min_hold_seconds": {
            "value": hold, "confidence": confidence,
            "reason": f"hold={hold}s + cooldown={cool}s 组合在 positive edge 下表现最优 (edge={edge:.2f}bps).",
        },
        "rebalance_cooldown_seconds": {
            "value": cool, "confidence": confidence,
            "reason": f"hold={hold}s + cooldown={cool}s 组合在 positive edge 下表现最优 (edge={edge:.2f}bps).",
        },
    }


def _recommend_cost_buffers(experiments: list[dict[str, Any]]) -> dict[str, Any]:
    """规则化推荐 expected_slippage_buffer_bps 和 expected_execution_buffer_bps。"""
    if not experiments:
        return {
            "expected_slippage_buffer_bps": {"value": 0.5, "confidence": "low",
                                             "reason": "Cost buffer batch 未运行或无数据"},
            "expected_execution_buffer_bps": {"value": 0.5, "confidence": "low",
                                              "reason": "Cost buffer batch 未运行或无数据"},
        }

    viable = [e for e in experiments if (e.get("mean_expected_edge_bps") or 0) > 0]

    if not viable:
        best = max(experiments, key=lambda e: e.get("mean_expected_edge_bps") or -999)
        slip = best.get("params", {}).get("expected_slippage_buffer_bps", 0.5)
        exec_buf = best.get("params", {}).get("expected_execution_buffer_bps", 0.5)
        return {
            "expected_slippage_buffer_bps": {"value": slip, "confidence": "low",
                                             "reason": "No buffer combo achieves positive edge."},
            "expected_execution_buffer_bps": {"value": exec_buf, "confidence": "low",
                                              "reason": "No buffer combo achieves positive edge."},
        }

    # 选 positive edge 最高且 buffer 不为零的组合（有 buffer 更保守）
    best = max(viable, key=lambda e: (
        e.get("positive_edge_ratio", 0),
        e.get("mean_expected_edge_bps") or 0,
    ))

    slip = best.get("params", {}).get("expected_slippage_buffer_bps", 0.5)
    exec_buf = best.get("params", {}).get("expected_execution_buffer_bps", 0.5)
    edge = best.get("mean_expected_edge_bps", 0) or 0
    pos_ratio = best.get("positive_edge_ratio", 0)
    confidence = "high" if pos_ratio > 0.7 else ("medium" if pos_ratio > 0.4 else "low")

    return {
        "expected_slippage_buffer_bps": {
            "value": slip, "confidence": confidence,
            "reason": f"slippage_buf={slip}, exec_buf={exec_buf} 组合 edge={edge:.2f}bps, pos_ratio={pos_ratio:.1%}.",
        },
        "expected_execution_buffer_bps": {
            "value": exec_buf, "confidence": confidence,
            "reason": f"slippage_buf={slip}, exec_buf={exec_buf} 组合 edge={edge:.2f}bps, pos_ratio={pos_ratio:.1%}.",
        },
    }


def _generate_single_ft_recommendations(
    all_rows: list[dict[str, Any]],
    calibration_result: dict[str, Any],
    family: str,
    timeframe: str,
) -> dict[str, Any]:
    """为单个 family/timeframe 生成参数推荐。"""
    ft_rows = _get_experiments_by_family_tf(all_rows, family, timeframe)

    # 按 batch key 找对应实验
    batch_names: dict[str, str] = {}
    for br in calibration_result.get("batch_results", []):
        s = br.get("summary")
        if s:
            batch_names[br.get("_key", "")] = s.get("batch_name", "")

    scale_name = batch_names.get("scale_calibration", "")
    cost_name = batch_names.get("cost_sensitivity", "")
    ticks_name = batch_names.get("confirm_ticks", "")

    scale_exps = _get_experiments_by_batch(ft_rows, scale_name) if scale_name else []
    cost_exps = _get_experiments_by_batch(ft_rows, cost_name) if cost_name else []
    ticks_exps = _get_experiments_by_batch(ft_rows, ticks_name) if ticks_name else []

    rec: dict[str, Any] = {
        "signal_edge_scale_bps": _recommend_signal_edge_scale(scale_exps),
    }

    cost_rec = _recommend_cost_model(cost_exps)
    rec["taker_fee_bps"] = cost_rec.get("taker_fee_bps", {})
    rec["slippage_bps"] = cost_rec.get("slippage_bps", {})
    rec["_cost_overall"] = {
        "confidence": cost_rec.get("overall_confidence"),
        "reason": cost_rec.get("overall_reason"),
    }

    rec["min_confirm_ticks"] = _recommend_confirm_ticks(ticks_exps)
    rec["min_safe_net_edge_bps"] = _recommend_net_edge_threshold()

    # Directional 特有参数
    if family == "directional":
        tw_name = batch_names.get("trend_weight", "")
        tw_exps = _get_experiments_by_batch(ft_rows, tw_name) if tw_name else []
        rec["directional_trend_weight"] = _recommend_trend_weight(tw_exps)

        clamp_name = batch_names.get("return_clamp", "")
        clamp_exps = _get_experiments_by_batch(ft_rows, clamp_name) if clamp_name else []
        rec["directional_return_clamp_bps"] = _recommend_return_clamp(clamp_exps)

    # ── Phase 1 扩展参数推荐（independent 家族）──
    if family == "independent":
        entry_name = batch_names.get("entry_threshold", "")
        entry_exps = _get_experiments_by_batch(ft_rows, entry_name) if entry_name else []
        rec["entry_threshold"] = _recommend_entry_threshold(entry_exps)

        close_name = batch_names.get("close_threshold", "")
        close_exps = _get_experiments_by_batch(ft_rows, close_name) if close_name else []
        rec["close_threshold"] = _recommend_close_threshold(close_exps)

        derisk_name = batch_names.get("de_risk_edge", "")
        derisk_exps = _get_experiments_by_batch(ft_rows, derisk_name) if derisk_name else []
        rec["de_risk_net_edge_bps"] = _recommend_de_risk_edge(derisk_exps)

        ft_edge_name = batch_names.get("failed_thesis_edge", "")
        ft_edge_exps = _get_experiments_by_batch(ft_rows, ft_edge_name) if ft_edge_name else []
        rec["failed_thesis_net_edge_bps"] = _recommend_failed_thesis_edge(ft_edge_exps)

        timing_name = batch_names.get("timing", "")
        timing_exps = _get_experiments_by_batch(ft_rows, timing_name) if timing_name else []
        timing_rec = _recommend_timing(timing_exps)
        rec["min_hold_seconds"] = timing_rec["min_hold_seconds"]
        rec["rebalance_cooldown_seconds"] = timing_rec["rebalance_cooldown_seconds"]

        cost_buf_name = batch_names.get("cost_buffer", "")
        cost_buf_exps = _get_experiments_by_batch(ft_rows, cost_buf_name) if cost_buf_name else []
        cost_buf_rec = _recommend_cost_buffers(cost_buf_exps)
        rec["expected_slippage_buffer_bps"] = cost_buf_rec["expected_slippage_buffer_bps"]
        rec["expected_execution_buffer_bps"] = cost_buf_rec["expected_execution_buffer_bps"]

    return rec


# =========================================================================
# 5. Parameter Candidates 构建
# =========================================================================


def _build_parameter_candidates(
    all_recommendations: dict[str, dict[str, Any]],
    round_id: str,
    output_path: pathlib.Path,
    *,
    dataset_version: str = "v1.0",
) -> pathlib.Path:
    """生成 parameter_candidates.json。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    candidates: dict[str, dict[str, Any]] = {}
    pending_validation: list[str] = []

    for ft_key, recs in all_recommendations.items():
        c: dict[str, Any] = {}
        for pname, prec in recs.items():
            if pname.startswith("_"):
                continue
            if isinstance(prec, dict) and "value" in prec:
                if prec["value"] is None:
                    pending_validation.append(f"{pname} in {ft_key}")
                    continue
                c[pname] = prec["value"]
                if prec.get("confidence") == "low":
                    pending_validation.append(f"{pname} in {ft_key}")
            # else: nested structure (e.g. _cost_overall), skip
        if c:
            candidates[ft_key] = c
        else:
            pending_validation.append(f"candidate set missing in {ft_key}")
            log.warning(
                "Skip empty parameter candidate set for %s; downstream must treat it as unavailable",
                ft_key,
            )

    combo_keys = sorted(candidates)
    data = {
        "schema_version": "aats.step2_candidates.v1",
        "round_id": round_id,
        "dataset_version": dataset_version,
        "scope": {
            "symbol": _SYMBOL,
            "step": "step2_candidates",
            "combo_keys": combo_keys,
            "combo_count": len(combo_keys),
        },
        "candidates": candidates,
        "pending_validation": pending_validation,
    }

    immutable_json_write(data, output_path)
    log.info("Wrote parameter_candidates.json -> %s", output_path)
    return output_path


# =========================================================================
# 6. 结论文档生成
# =========================================================================


def _pct(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v * 100:.2f}%"


def _fmt(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v:.4f}"


def _build_conclusion_report(
    calibration_results: list[dict[str, Any]],
    scan_results: list[dict[str, Any]],
    all_rows: list[dict[str, Any]],
    all_recommendations: dict[str, dict[str, Any]],
    round_id: str,
    output_path: pathlib.Path,
) -> pathlib.Path:
    """生成 phase2_step2_research_conclusion.md。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    _add = lines.append
    now_str = datetime.now(timezone.utc).isoformat()

    # ==== Header ====
    _add("# Phase 2 Step 2: Research Conclusion")
    _add("")
    _add(f"> Round ID: `{round_id}`")
    _add(f"> Generated at: {now_str}")
    _add("")

    # ==== 1. Scope ====
    _add("## 1. Scope")
    _add("")
    _add(f"- **Symbol**: {_SYMBOL}")
    _add("- **Families**: independent, directional")
    _add("- **Timeframes**: 15m, 1H")
    # 从第一个有数据的 batch 取 window
    for cr in calibration_results:
        for br in cr.get("batch_results", []):
            s = br.get("summary")
            if s and s.get("window"):
                _add(f"- **Window**: {s['window']}")
                break
        else:
            continue
        break
    _add("")

    # ==== 2. What Was Executed ====
    _add("## 2. What Was Executed")
    _add("")
    _add("### 2.1 Calibration Rounds")
    _add("")
    _add("| Round | Family | Timeframe | Batches | Status |")
    _add("|-------|--------|-----------|---------|--------|")
    for cr in calibration_results:
        n_batches = len(cr.get("batch_results", []))
        n_ok = sum(1 for b in cr.get("batch_results", []) if b["status"] == "succeeded")
        _add(f"| {cr['round_key']} | {cr['family']} | {cr['timeframe']} "
             f"| {n_ok}/{n_batches} succeeded | {cr['status']} |")
    _add("")

    _add("### 2.2 Formal Parameter Scans")
    _add("")
    if scan_results:
        _add("| Scan | Family | Timeframe | Status | Scan Dir |")
        _add("|------|--------|-----------|--------|----------|")
        for sr in scan_results:
            scan_dir = sr.get("scan_dir", "N/A")
            _add(f"| {sr['scan_key']} | {sr['family']} | {sr['timeframe']} "
                 f"| {sr['status']} | `{scan_dir}` |")
    else:
        _add("*(scan phase skipped or no data)*")
    _add("")

    # ==== 3. Key Comparisons ====
    _add("## 3. Key Comparisons")
    _add("")

    # 3.1 Independent vs Directional on 15m
    _add("### 3.1 Independent vs Directional on 15m")
    _add("")
    _build_comparison_section(lines, all_rows, "independent", "directional", "15m")

    # 3.2 Independent vs Directional on 1H
    _add("### 3.2 Independent vs Directional on 1H")
    _add("")
    _build_comparison_section(lines, all_rows, "independent", "directional", "1H")

    # 3.3 15m vs 1H within Independent
    _add("### 3.3 15m vs 1H within Independent")
    _add("")
    _build_timeframe_comparison_section(lines, all_rows, "independent", "15m", "1H")

    # 3.4 15m vs 1H within Directional
    _add("### 3.4 15m vs 1H within Directional")
    _add("")
    _build_timeframe_comparison_section(lines, all_rows, "directional", "15m", "1H")

    # ==== 4. Parameter Candidates ====
    _add("## 4. Parameter Candidates by Family/Timeframe")
    _add("")
    for ft_key, recs in all_recommendations.items():
        _add(f"### {ft_key}")
        _add("")
        _add("| Parameter | Value | Confidence | Reason |")
        _add("|-----------|-------|------------|--------|")
        for pname, prec in recs.items():
            if pname.startswith("_"):
                continue
            if not isinstance(prec, dict) or "value" not in prec:
                continue
            val = prec.get("value")
            val_str = str(val) if val is not None else "*(pending)*"
            conf = prec.get("confidence", "N/A")
            reason = prec.get("reason", "")
            reason_short = reason[:100] + "..." if len(reason) > 100 else reason
            _add(f"| `{pname}` | {val_str} | {conf} | {reason_short} |")
        _add("")

    # ==== 5. Stable Conclusions ====
    _add("## 5. Stable Conclusions")
    _add("")
    stable = _collect_stable_conclusions(all_recommendations)
    for s in stable:
        _add(f"- {s}")
    if not stable:
        _add("- 无 high-confidence 结论（数据窗口较短或信号偏弱）")
    _add("")

    # ==== 6. Pending Items ====
    _add("## 6. Pending Items")
    _add("")
    pending = _collect_pending_items(all_recommendations, calibration_results, scan_results)
    for p in pending:
        _add(f"- {p}")
    _add("")

    # ==== 7. Next Steps ====
    _add("## 7. Next Steps")
    _add("")
    _add("- Step 3: Live attribution (纸盘模拟实盘验证)")
    _add("- 在更长时间窗口（1 个月+）上重复验证参数稳定性")
    _add("- 扩展到 ETH-USDT-SWAP 等其他 symbol")
    _add("- 引入 orderbook realism / execution simulation (Phase 4)")
    _add("- 对 `min_safe_net_edge_bps` 补充专项 calibration batch")
    _add("")

    # ==== 8. Caveats ====
    _add("## 8. Caveats")
    _add("")
    _add("- Phase 2 replay 使用简化评分模型（不含 AI assessment），与生产系统评分存在偏差")
    _add("- 不包含撮合仿真、滑点模型和 orderbook realism（属于 Phase 4）")
    _add("- 持仓逻辑为简化版（固定 1 单位），不反映真实资金管理")
    _add("- 以上推荐基于规则化判断，重点关注相对趋势而非绝对值")
    _add("- 当前数据窗口较短（2 天），推荐结论需在更长窗口上验证")
    _add("")

    content = "\n".join(lines)
    output_path.write_text(content, encoding="utf-8")
    log.info("Wrote conclusion -> %s (%d lines)", output_path, len(lines))
    return output_path


def _build_comparison_section(
    lines: list[str],
    all_rows: list[dict[str, Any]],
    family_a: str,
    family_b: str,
    timeframe: str,
) -> None:
    """构建两个 family 在同一 timeframe 上的比较。"""
    rows_a = _get_experiments_by_family_tf(all_rows, family_a, timeframe)
    rows_b = _get_experiments_by_family_tf(all_rows, family_b, timeframe)

    if not rows_a and not rows_b:
        lines.append(f"*(no data for {family_a} or {family_b} on {timeframe})*")
        lines.append("")
        return

    def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {"total": 0}
        opens = [r.get("opening_count", 0) for r in rows]
        edges = [r.get("mean_expected_edge_bps") or 0 for r in rows]
        pos_ratios = [r.get("positive_edge_ratio") or 0 for r in rows]
        return {
            "total": len(rows),
            "avg_opens": sum(opens) / len(opens),
            "avg_edge": sum(edges) / len(edges),
            "avg_pos_ratio": sum(pos_ratios) / len(pos_ratios),
        }

    sa = _summarize(rows_a)
    sb = _summarize(rows_b)

    lines.append(f"| Metric | {family_a} | {family_b} |")
    lines.append("|--------|" + "-" * (len(family_a) + 2) + "|" + "-" * (len(family_b) + 2) + "|")
    lines.append(f"| Experiments | {sa.get('total', 0)} | {sb.get('total', 0)} |")
    lines.append(f"| Avg opening_count | {sa.get('avg_opens', 0):.1f} | {sb.get('avg_opens', 0):.1f} |")
    lines.append(f"| Avg net_edge_bps | {sa.get('avg_edge', 0):.2f} | {sb.get('avg_edge', 0):.2f} |")
    lines.append(f"| Avg positive_edge_ratio | {sa.get('avg_pos_ratio', 0):.1%} | {sb.get('avg_pos_ratio', 0):.1%} |")
    lines.append("")

    # 结构差异注释
    if sa.get("total", 0) > 0 and sb.get("total", 0) > 0:
        edge_diff = (sa.get("avg_edge", 0) or 0) - (sb.get("avg_edge", 0) or 0)
        if abs(edge_diff) > 1:
            leader = family_a if edge_diff > 0 else family_b
            lines.append(f"- **{leader}** 在 {timeframe} 上的平均 net_edge 更高 (差异 {abs(edge_diff):.2f} bps)")
        else:
            lines.append(f"- 两个 family 在 {timeframe} 上的 net_edge 差异不大 ({abs(edge_diff):.2f} bps)")
    lines.append("")


def _build_timeframe_comparison_section(
    lines: list[str],
    all_rows: list[dict[str, Any]],
    family: str,
    tf_a: str,
    tf_b: str,
) -> None:
    """构建同一 family 在两个 timeframe 上的比较。"""
    rows_a = _get_experiments_by_family_tf(all_rows, family, tf_a)
    rows_b = _get_experiments_by_family_tf(all_rows, family, tf_b)

    if not rows_a and not rows_b:
        lines.append(f"*(no data for {family} on {tf_a} or {tf_b})*")
        lines.append("")
        return

    def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {"total": 0}
        opens = [r.get("opening_count", 0) for r in rows]
        edges = [r.get("mean_expected_edge_bps") or 0 for r in rows]
        pos_ratios = [r.get("positive_edge_ratio") or 0 for r in rows]
        return {
            "total": len(rows),
            "avg_opens": sum(opens) / len(opens),
            "avg_edge": sum(edges) / len(edges),
            "avg_pos_ratio": sum(pos_ratios) / len(pos_ratios),
        }

    sa = _summarize(rows_a)
    sb = _summarize(rows_b)

    lines.append(f"| Metric | {tf_a} | {tf_b} |")
    lines.append("|--------|------|------|")
    lines.append(f"| Experiments | {sa.get('total', 0)} | {sb.get('total', 0)} |")
    lines.append(f"| Avg opening_count | {sa.get('avg_opens', 0):.1f} | {sb.get('avg_opens', 0):.1f} |")
    lines.append(f"| Avg net_edge_bps | {sa.get('avg_edge', 0):.2f} | {sb.get('avg_edge', 0):.2f} |")
    lines.append(f"| Avg positive_edge_ratio | {sa.get('avg_pos_ratio', 0):.1%} | {sb.get('avg_pos_ratio', 0):.1%} |")
    lines.append("")

    if sa.get("total", 0) > 0 and sb.get("total", 0) > 0:
        opens_ratio = (sa.get("avg_opens", 0) or 1) / max(sb.get("avg_opens", 0) or 1, 0.01)
        if opens_ratio > 2:
            lines.append(f"- {tf_a} 的平均 opening 是 {tf_b} 的 {opens_ratio:.1f}x（更短周期 bar 数更多）")
        elif opens_ratio < 0.5:
            lines.append(f"- {tf_b} 的平均 opening 是 {tf_a} 的 {1/opens_ratio:.1f}x")
        else:
            lines.append(f"- 两个 timeframe 的 opening 量级接近 (ratio={opens_ratio:.2f}x)")
    lines.append("")


def _collect_stable_conclusions(
    all_recommendations: dict[str, dict[str, Any]],
) -> list[str]:
    """收集所有 high-confidence 结论。"""
    stable: list[str] = []
    for ft_key, recs in all_recommendations.items():
        for pname, prec in recs.items():
            if pname.startswith("_"):
                continue
            if isinstance(prec, dict) and prec.get("confidence") == "high":
                val = prec.get("value")
                stable.append(f"`{pname}` = {val} in **{ft_key}** (high confidence)")
    return stable


def _collect_pending_items(
    all_recommendations: dict[str, dict[str, Any]],
    calibration_results: list[dict[str, Any]],
    scan_results: list[dict[str, Any]],
) -> list[str]:
    """收集待验证项。"""
    pending: list[str] = []

    # low-confidence 参数
    for ft_key, recs in all_recommendations.items():
        for pname, prec in recs.items():
            if pname.startswith("_"):
                continue
            if isinstance(prec, dict) and prec.get("confidence") == "low":
                pending.append(f"`{pname}` in {ft_key}: confidence=low, 需要更多数据")

    # 失败的 calibration batch
    for cr in calibration_results:
        for br in cr.get("batch_results", []):
            if br["status"] == "failed":
                pending.append(
                    f"Calibration batch `{br.get('_key')}` in {cr['round_key']} 失败, "
                    f"相关参数推荐缺失"
                )

    # 失败的 scan
    for sr in scan_results:
        if sr["status"] == "failed":
            pending.append(f"Scan `{sr['scan_key']}` 失败, 无法生成对应组合比较")

    # 固定项
    pending.append("`min_safe_net_edge_bps`: 所有 family/timeframe 均为 pending, 需要专项 batch")
    pending.append("数据窗口仅 2 天, 所有结论需在更长窗口上验证")

    return pending


# =========================================================================
# 7. Round Manifest
# =========================================================================


def _write_manifest(
    calibration_results: list[dict[str, Any]],
    scan_results: list[dict[str, Any]],
    round_id: str,
    started_at: str,
    finished_at: str,
    output_path: pathlib.Path,
    *,
    extra_manifest: dict[str, Any] | None = None,
    immutable: bool = False,
) -> pathlib.Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cal_summary = []
    for cr in calibration_results:
        batch_info = []
        for br in cr.get("batch_results", []):
            batch_info.append({
                "key": br.get("_key"),
                "batch_run_id": br.get("batch_run_id"),
                "batch_dir": br.get("batch_dir"),
                "status": br["status"],
                "summary_sha256": br.get("summary_sha256"),
                "summary_size_bytes": br.get("summary_size_bytes"),
                "total_experiments": br.get("total_experiments"),
                "succeeded": br.get("succeeded"),
                "failed": br.get("failed"),
            })
        cal_summary.append({
            "round_key": cr["round_key"],
            "family": cr["family"],
            "timeframe": cr["timeframe"],
            "status": cr["status"],
            "batches": batch_info,
        })

    scan_summary = []
    for sr in scan_results:
        scan_summary.append({
            "scan_key": sr["scan_key"],
            "family": sr["family"],
            "timeframe": sr["timeframe"],
            "status": sr["status"],
            "scan_run_id": sr.get("scan_run_id"),
            "scan_dir": sr.get("scan_dir"),
            "comparison_sha256": sr.get("comparison_sha256"),
            "comparison_size_bytes": sr.get("comparison_size_bytes"),
            "window": sr.get("window"),
            "dataset_version": sr.get("dataset_version"),
            "grid_sha256": sr.get("grid_sha256"),
            "total_combinations": sr.get("total_combinations"),
            "completed_count": sr.get("completed_count"),
            "failed_count": sr.get("failed_count"),
        })

    manifest = {
        "round_id": round_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "symbol": _SYMBOL,
        "calibrations": cal_summary,
        "scans": scan_summary,
    }
    if extra_manifest:
        protected = {"round_id", "started_at", "finished_at", "symbol"}
        overlap = protected.intersection(extra_manifest)
        if overlap:
            raise ValueError(
                f"extra_manifest cannot replace protected fields: {sorted(overlap)}"
            )
        manifest.update(extra_manifest)

    if immutable:
        immutable_json_write(manifest, output_path)
    else:
        atomic_json_write(manifest, output_path)
    log.info("Wrote round_manifest.json -> %s", output_path)
    return output_path


# =========================================================================
# 8. Scan 定义加载
# =========================================================================


def _load_scan_defs(
    *,
    start: str | None = None,
    end: str | None = None,
    dataset_version: str | None = None,
) -> dict[str, dict[str, Any]]:
    """从配置文件加载 scan matrix，若不存在则使用内嵌默认值。"""
    matrix_path = pathlib.Path(_SCAN_MATRIX_FILE)
    if matrix_path.exists():
        with matrix_path.open(encoding="utf-8") as f:
            data = json.load(f)
        scans = data.get("scans", {})
        # 补充全局字段
        for key, sdef in scans.items():
            sdef.setdefault("start", data.get("start", "2026-03-31"))
            sdef.setdefault("end", data.get("end", "2026-04-02"))
            sdef.setdefault(
                "dataset_version",
                _normalize_dataset_version(data.get("dataset_version", "v1.0")),
            )
            if start:
                sdef["start"] = start
            if end:
                sdef["end"] = end
            if dataset_version:
                sdef["dataset_version"] = _normalize_dataset_version(dataset_version)
        log.info("Loaded scan matrix from %s (%d scans)", matrix_path, len(scans))
        return scans

    log.info("Scan matrix file not found, using built-in defaults")
    return dict(_DEFAULT_SCAN_DEFS)


# =========================================================================
# 主流程
# =========================================================================


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Step 2 Research Orchestrator: "
                    "independent + directional, 15m + 1H 正式研究闭环",
    )
    parser.add_argument(
        "--artifact-root", type=str, default=str(_DEFAULT_ARTIFACT_ROOT),
        help=f"Artifact output root (default: {_DEFAULT_ARTIFACT_ROOT})",
    )
    parser.add_argument("--start", type=str, default=None, help="Override research start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default=None, help="Override research end date (YYYY-MM-DD)")
    parser.add_argument(
        "--dataset-version",
        type=str,
        default="v1.0",
        help="Override candle dataset version for calibration + scan",
    )
    parser.add_argument(
        "--ensure-schema", action="store_true",
        help="Legacy name: validate schema before first batch; does not run DDL",
    )
    parser.add_argument(
        "--stop-on-error", action="store_true",
        help="Stop entire round on first batch failure",
    )
    parser.add_argument(
        "--skip-calibration", action="store_true",
        help="Skip calibration phase (only run scans + aggregation)",
    )
    parser.add_argument(
        "--skip-scan", action="store_true",
        help="Skip scan phase (only run calibration + aggregation)",
    )
    parser.add_argument(
        "--no-print-summary", action="store_true",
        help="Suppress final summary to stdout",
    )
    args = parser.parse_args()
    args.dataset_version = _normalize_dataset_version(args.dataset_version)

    started_at = datetime.now(timezone.utc).isoformat()
    round_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
    artifact_root = pathlib.Path(args.artifact_root)
    round_dir = artifact_root / round_id

    log.info("=" * 66)
    log.info("Step 2 Research Orchestrator")
    log.info("  Round ID  : %s", round_id)
    log.info("  Symbol    : %s", _SYMBOL)
    log.info("  Scope     : independent+directional / 15m+1H")
    log.info("  Output    : %s", round_dir)
    log.info("=" * 66)

    # ================================================================
    # Phase A: Calibration Rounds
    # ================================================================
    calibration_results: list[dict[str, Any]] = []

    if not args.skip_calibration:
        log.info("")
        log.info("=" * 66)
        log.info("Phase A: Calibration Rounds")
        log.info("=" * 66)

        batch_artifact_root = round_dir / "batches"

        for round_key in _EXPECTED_STEP2_CALIBRATION_KEYS:
            cal_def = _CALIBRATION_DEFS[round_key]
            result = _run_calibration_round(
                round_key, cal_def, batch_artifact_root,
                ensure_schema=args.ensure_schema,
                stop_on_error=args.stop_on_error,
                start=args.start,
                end=args.end,
                dataset_version=args.dataset_version,
            )
            calibration_results.append(result)

            if result["status"] == "failed" and args.stop_on_error:
                log.error("--stop-on-error: aborting after calibration round %s", round_key)
                break
    else:
        log.info("Phase A: SKIPPED (--skip-calibration)")

    # ================================================================
    # Phase B: Formal Parameter Scans
    # ================================================================
    scan_results: list[dict[str, Any]] = []

    if not args.skip_scan:
        log.info("")
        log.info("=" * 66)
        log.info("Phase B: Formal Parameter Scans")
        log.info("=" * 66)

        scan_defs = _load_scan_defs(
            start=args.start,
            end=args.end,
            dataset_version=args.dataset_version,
        )

        for scan_key in _EXPECTED_STEP2_SCAN_KEYS:
            if scan_key not in scan_defs:
                log.warning("Scan def for %s not found, skipping", scan_key)
                continue
            result = _run_scan(
                scan_key, scan_defs[scan_key],
                result_root=round_dir / "scan_results",
                ensure_schema=args.ensure_schema,
            )
            scan_results.append(result)

            if result["status"] == "failed":
                log.warning("Scan %s failed, continuing...", scan_key)
    else:
        log.info("Phase B: SKIPPED (--skip-scan)")

    # ================================================================
    # Phase C: Aggregation + Recommendations
    # ================================================================
    log.info("")
    log.info("=" * 66)
    log.info("Phase C: Aggregation + Recommendations")
    log.info("=" * 66)

    # C.1 汇总 calibration 实验
    all_rows = _collect_calibration_experiments(calibration_results)
    log.info("Total calibration experiments: %d", len(all_rows))

    _write_family_timeframe_summary_csv(
        all_rows, round_dir / "family_timeframe_summary.csv",
    )
    family_timeframe_summary_payload = {
        "round_id": round_id,
        "total_experiments": len(all_rows),
        "experiments": all_rows,
    }
    _write_family_timeframe_summary_json(
        all_rows, round_id, round_dir / "family_timeframe_summary.json",
    )

    # C.2 汇总 scan 结果
    scan_comparison_rows: list[dict[str, Any]] = []
    for sr in scan_results:
        comp = sr.get("comparison")
        if not comp:
            continue
        for exp in extract_comparison_rows(comp):
            row = dict(exp)
            row["family"] = sr["family"]
            row["symbol"] = _SYMBOL
            row["timeframe"] = sr["timeframe"]
            row["scan_key"] = sr["scan_key"]
            row["scan_run_id"] = sr.get("scan_run_id")
            scan_comparison_rows.append(row)
    scan_comparison_summary_payload = {
        "total_scans": len(scan_results),
        "succeeded_scans": sum(1 for s in scan_results if s["status"] == "succeeded"),
        "total_experiments": len(scan_comparison_rows),
        "experiments": scan_comparison_rows,
    }
    _build_scan_comparison_summary(
        scan_results,
        round_dir / "scan_comparison_summary.csv",
        round_dir / "scan_comparison_summary.json",
    )

    # C.3 为每个 calibrated family/tf 生成推荐
    all_recommendations: dict[str, dict[str, Any]] = {}
    for cr in calibration_results:
        ft_key = cr["round_key"]
        recs = _generate_single_ft_recommendations(
            all_rows, cr, cr["family"], cr["timeframe"],
        )
        all_recommendations[ft_key] = recs
        log.info("Generated recommendations for %s", ft_key)

    # C.4 输出 parameter_candidates
    _build_parameter_candidates(
        all_recommendations, round_id,
        round_dir / "parameter_candidates.json",
        dataset_version=args.dataset_version,
    )
    parameter_candidates_path = round_dir / "parameter_candidates.json"
    parameter_candidates_payload = json.loads(
        parameter_candidates_path.read_text(encoding="utf-8")
    )

    # ================================================================
    # Phase D: Conclusion Document
    # ================================================================
    log.info("")
    log.info("=" * 66)
    log.info("Phase D: Conclusion Document")
    log.info("=" * 66)

    conclusion_path = round_dir / "phase2_step2_research_conclusion.md"
    _build_conclusion_report(
        calibration_results, scan_results, all_rows,
        all_recommendations, round_id,
        conclusion_path,
    )

    round_status = _determine_step2_round_status(
        calibration_results=calibration_results,
        scan_results=scan_results,
        parameter_candidates_payload=parameter_candidates_payload,
        start=args.start,
        end=args.end,
    )

    # Manifest is published last and binds the immutable Step 2 candidate.
    finished_at = datetime.now(timezone.utc).isoformat()
    manifest_path = round_dir / "round_manifest.json"
    parameter_candidates_bytes = parameter_candidates_path.read_bytes()
    parameter_candidates_sha256 = hashlib.sha256(
        parameter_candidates_bytes
    ).hexdigest()
    _write_manifest(
        calibration_results, scan_results,
        round_id, started_at, finished_at,
        manifest_path,
        extra_manifest={
            "schema_version": "aats.step2_round.v1",
            "phase": "step2",
            "status": round_status,
            "dataset_version": args.dataset_version,
            "scope": {
                "symbol": _SYMBOL,
                "families": ["directional", "independent"],
                "timeframes": ["15m", "1h"],
                "combo_keys": sorted(
                    parameter_candidates_payload.get("candidates", {})
                ),
                "combo_count": len(
                    parameter_candidates_payload.get("candidates", {})
                ),
                "window": {"start": args.start, "end": args.end},
            },
            "input_refs": {
                "dataset_version": args.dataset_version,
                "window": {"start": args.start, "end": args.end},
            },
            "artifact_sha256": {
                parameter_candidates_path.name: parameter_candidates_sha256
            },
            "artifact_size_bytes": {
                parameter_candidates_path.name: len(parameter_candidates_bytes)
            },
        },
        immutable=True,
    )
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    snapshot_saved = save_research_round_snapshot(
        round_id=round_id,
        phase=ROUND_PHASE_STEP2,
        status=round_status,
        round_path=str(round_dir),
        started_at=started_at,
        finished_at=finished_at,
        replay_only=False,
        manifest_payload=manifest_payload,
        summary_payload={
            "family_timeframe_summary": family_timeframe_summary_payload,
            "scan_comparison_summary": scan_comparison_summary_payload,
            "parameter_candidates": parameter_candidates_payload,
        },
        conclusion_payload={
            "report_markdown_path": str(conclusion_path),
        },
        artifacts_payload={
            "round_dir": str(round_dir),
            "family_timeframe_summary_json": str(round_dir / "family_timeframe_summary.json"),
            "scan_comparison_summary_json": str(round_dir / "scan_comparison_summary.json"),
            "parameter_candidates_json": str(round_dir / "parameter_candidates.json"),
            "round_manifest_json": str(manifest_path),
        },
    )
    if not snapshot_saved:
        if has_explicit_governance_db_configuration(_PROJECT_ROOT):
            log.error(
                "Managed Step2 snapshot publication failed; refusing result marker "
                "for round %s",
                round_id,
            )
            sys.exit(3)
        log.warning(
            "Step2 round snapshot DB upsert unavailable; continuing in explicit "
            "offline file mode"
        )

    round_result_payload = {
        "schema_version": "aats.step2_result.v1",
        "phase": "step2",
        "round_id": round_id,
        "round_dir": str(round_dir.resolve()),
        "candidate_path": str(parameter_candidates_path.resolve()),
        "candidate_sha256": parameter_candidates_sha256,
        "status": round_status,
        "symbol": _SYMBOL,
        "dataset_version": args.dataset_version,
        "window": {"start": args.start, "end": args.end},
    }
    immutable_json_write(round_result_payload, round_dir / "round_result.json")
    print(
        _STEP2_RESULT_PREFIX
        + json.dumps(
            round_result_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )

    # ================================================================
    # 最终汇总
    # ================================================================
    cal_ok = sum(1 for cr in calibration_results if cr["status"] == "succeeded")
    cal_partial = sum(1 for cr in calibration_results if cr["status"] == "partial_success")
    cal_fail = sum(1 for cr in calibration_results if cr["status"] == "failed")
    scan_ok = sum(1 for sr in scan_results if sr["status"] == "succeeded")
    scan_fail = sum(1 for sr in scan_results if sr["status"] == "failed")

    log.info("")
    log.info("=" * 66)
    log.info("Step 2 completed:")
    log.info("  Calibration: %d succeeded, %d partial, %d failed",
             cal_ok, cal_partial, cal_fail)
    log.info("  Scans: %d succeeded, %d failed", scan_ok, scan_fail)
    log.info("  Total calibration experiments: %d", len(all_rows))
    log.info("  Round dir: %s", round_dir)
    log.info("=" * 66)

    if not args.no_print_summary:
        print("")
        print(f"=== Step 2 Research: {round_id} ===")
        print(f"Symbol: {_SYMBOL}")
        print(f"Calibration: {cal_ok} ok, {cal_partial} partial, {cal_fail} failed")
        print(f"Scans: {scan_ok} ok, {scan_fail} failed")
        print(f"Total experiments: {len(all_rows)}")
        print("")

        print("Parameter Candidates:")
        for ft_key, recs in all_recommendations.items():
            print(f"\n  [{ft_key}]")
            for pname, prec in recs.items():
                if pname.startswith("_"):
                    continue
                if not isinstance(prec, dict) or "value" not in prec:
                    continue
                val = prec.get("value")
                conf = prec.get("confidence", "?")
                val_str = str(val) if val is not None else "(pending)"
                print(f"    {pname:<35s} = {val_str:<10s} [{conf}]")

        print("")
        print(f"Conclusion: {round_dir / 'phase2_step2_research_conclusion.md'}")
        print(f"Candidates: {round_dir / 'parameter_candidates.json'}")
        print(f"Artifacts : {round_dir}")

    # 退出码必须与持久化的 round status 一致。
    if round_status == "failed":
        sys.exit(3)
    if round_status == "partial_success":
        sys.exit(2)


if __name__ == "__main__":
    main()
