#!/usr/bin/env python3
"""RDP 研究管线一键编排.

将 Phase 2 → 3 → 4 → 5 → 6 串联为单一命令,
自动将 Phase 2 产出的 parameter_candidates.json 传递给后续阶段.

Usage:
    # 完整管线 (Phase 2 → 3 → 4 → 5 → 6)
    python scripts/rdp_run_full_pipeline.py --start 2026-03-31 --end 2026-04-02

    # 从 Step 3 续跑（显式绑定已完成的 Step 2 result）
    python scripts/rdp_run_full_pipeline.py --start 2026-03-31 --end 2026-04-02 \\
        --start-from step3 --step2-result-ref <step2-round>/round_result.json

    # 只跑既有治理数据刷新 + Decision（无需研究日期）
    python scripts/rdp_run_full_pipeline.py --skip-phase2 --skip-step3 \
        --skip-import-candidates --skip-phase3 --skip-phase4

    # 只跑到 Phase 4（不做 decision）
    python scripts/rdp_run_full_pipeline.py --start 2026-03-31 --end 2026-04-02 \\
        --stop-after phase4

    # Dry run 查看将执行的命令
    python scripts/rdp_run_full_pipeline.py --start 2026-03-31 --end 2026-04-02 --dry-run

Exit codes:
    0 = 全部成功
    1 = 部分或全部阶段失败
    2 = 参数错误
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import queue
import re
import stat
import subprocess
import sys
import threading
import time as _time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from aats.data_platform.governance.auto_import_candidates import (
    AUTO_IMPORT_SUCCESS_STATUSES,
)

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
_PIPELINE_RESULT_PREFIX = "RDP_PIPELINE_RESULT_JSON="
_DECISION_RESULT_PREFIX = "RDP_DECISION_RESULT_JSON="
_STEP2_RESULT_PREFIX = "RDP_STEP2_RESULT_JSON="
_STEP3_RESULT_PREFIX = "RDP_STEP3_RESULT_JSON="
_PHASE3_RESULT_PREFIX = "RDP_PHASE3_RESULT_JSON="
_PHASE4_RESULT_PREFIX = "RDP_PHASE4_RESULT_JSON="
_ROUND_ID_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{8}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_PHASE34_COMBOS: dict[str, tuple[str, str]] = {
    "independent_15m": ("independent", "15m"),
    "independent_1h": ("independent", "1H"),
    "directional_15m": ("directional", "15m"),
    "directional_1h": ("directional", "1H"),
}
_PARTIAL_SUCCESS_PHASES = frozenset({"step2", "step3", "phase3", "phase4"})
_SUCCESSFUL_IMPORT_STATUSES = AUTO_IMPORT_SUCCESS_STATUSES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_full_pipeline")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non_finite_json:{value}")


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate_json_key:{key}")
        result[key] = value
    return result


def _strict_json_loads(value: str) -> object:
    return json.loads(
        value,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_reject_duplicate_json_keys,
    )

# =========================================================================
# 阶段定义
# =========================================================================
PHASE_ORDER = ["phase2", "step3", "import_candidates", "phase3", "phase4", "phase5", "decision"]
PHASE_LABELS = {
    "phase2": "Phase 2 — 参数研究 (Step 2)",
    "step3": "Phase 2 — 扩展参数扫描 (Step 3)",
    "import_candidates": "Step 3+ — 参数候选导入治理层",
    "phase3": "Phase 3 — 归因对照",
    "phase4": "Phase 4 — 执行可行性",
    "phase5": "Phase 5 — 治理刷新",
    "decision": "Phase 6 — 闭环决策",
}


# =========================================================================
# CLI
# =========================================================================
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="RDP 研究管线一键编排: Phase 2 -> 3 -> 4 -> 5 -> Decision",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 完整管线 (Phase 2 -> 3 -> 4 -> 5 -> Decision)
  %(prog)s --start 2026-03-31 --end 2026-04-02

  # 从 Phase 3 开始（显式绑定已完成的 Step 3 result）
  %(prog)s --start 2026-03-31 --end 2026-04-02 --start-from phase3 \\
      --step3-result-ref artifacts/research/step3_rounds/<round>/round_result.json

  # 只跑到 Phase 4 (不做治理和决策)
  %(prog)s --start 2026-03-31 --end 2026-04-02 --stop-after phase4

  # Dry run
  %(prog)s --start 2026-03-31 --end 2026-04-02 --dry-run
        """,
    )

    # 通用
    p.add_argument("--start", help="起始日期 YYYY-MM-DD (UTC), 研究阶段需要")
    p.add_argument("--end", help="结束日期 YYYY-MM-DD (UTC), 研究阶段需要")
    p.add_argument(
        "--lookback-days", type=int, default=None,
        help="自动从今天往回推 N 天作为 --start，--end 设为今天 (UTC)。"
             "优先级低于显式 --start/--end。"
             "示例: --lookback-days 90 等价于 --start <90天前> --end <今天>",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="仅显示将执行的命令, 不实际运行")
    p.add_argument("--ensure-schema", action="store_true",
                   help="兼容参数：首个阶段运行前只读校验 schema，不执行 DDL")
    p.add_argument("--no-stop-on-failure", action="store_true",
                   help="某阶段失败后继续运行后续阶段")
    p.add_argument(
        "--dataset-version",
        default="v1.0",
        help="Phase 2/3/4 使用的 candle dataset version (default: v1.0)",
    )

    # 阶段控制
    ctrl = p.add_argument_group("阶段控制")
    ctrl.add_argument(
        "--start-from", choices=PHASE_ORDER, default="phase2",
        help="从指定阶段开始 (默认: phase2)",
    )
    ctrl.add_argument(
        "--stop-after", choices=PHASE_ORDER, default="decision",
        help="在指定阶段后停止 (默认: decision)",
    )
    ctrl.add_argument("--skip-phase2", action="store_true", help="跳过 Phase 2 (Step 2)")
    ctrl.add_argument("--skip-step3", action="store_true", help="跳过 Step 3 扩展扫描")
    ctrl.add_argument("--skip-import-candidates", action="store_true",
                       help="跳过参数候选自动导入治理层")
    ctrl.add_argument("--skip-phase3", action="store_true", help="跳过 Phase 3")
    ctrl.add_argument("--skip-phase4", action="store_true", help="跳过 Phase 4")
    ctrl.add_argument("--skip-phase5", action="store_true", help="跳过 Phase 5 治理刷新")
    ctrl.add_argument("--skip-decision", action="store_true", help="跳过 Decision Round")

    # Phase 2
    p2 = p.add_argument_group("Phase 2 参数")
    p2.add_argument("--skip-calibration", action="store_true",
                    help="Phase 2: 跳过校准, 只跑 scan + 汇总")
    p2.add_argument("--skip-scan", action="store_true",
                    help="Phase 2: 跳过扫描, 只跑 calibration + 汇总")

    # Phase 3
    p3 = p.add_argument_group("Phase 3 参数")
    p3.add_argument("--live-db-url",
                    help="Phase 3: Live AATS 数据库 URL "
                         "(默认读取 RDP_LIVE_DATABASE_URL；缺失时失败关闭)")
    p3.add_argument("--replay-only", action="store_true",
                    help="Phase 3: 仅 replay 分析, 不连接 live DB")

    # Phase 4
    p4 = p.add_argument_group("Phase 4 参数")
    p4.add_argument("--taker-fee-bps", type=float, default=5.0,
                    help="Phase 4: Taker fee bps (默认: 5.0)")

    # Decision
    pd = p.add_argument_group("Decision 参数")
    pd.add_argument("--include-draft", action="store_true",
                    help="Decision: 同时评估 draft 状态的参数集")

    # 参数文件
    p.add_argument(
        "--params-json",
        help="手动指定 parameter_candidates.json 路径 "
             "(仅允许与显式绑定的 Step 3 candidate 为同一文件)",
    )
    p.add_argument(
        "--step2-result-ref",
        help=(
            "已完成 Step 2 round_result.json 的显式路径；"
            "跳过 Phase 2 但运行 Step 3 时必填"
        ),
    )
    p.add_argument(
        "--step3-result-ref",
        help=(
            "已完成 Step 3 round_result.json 的显式路径；"
            "跳过 Step 3 但运行候选导入或 Phase 3/4 时必填"
        ),
    )
    p.add_argument(
        "--phase3-result-ref",
        help=(
            "已完成 Phase 3 round_result.json 的显式路径；"
            "跳过 Phase 3 但运行治理刷新或 Decision 时必填"
        ),
    )
    p.add_argument(
        "--phase4-result-ref",
        help=(
            "已完成 Phase 4 round_result.json 的显式路径；"
            "跳过 Phase 4 但运行治理刷新或 Decision 时必填"
        ),
    )

    args = p.parse_args()
    if args.dataset_version == "v1":
        args.dataset_version = "v1.0"
    return args


# =========================================================================
# 工具函数
# =========================================================================
def _resolve_phases(args: argparse.Namespace) -> list[str]:
    """根据 --start-from / --stop-after / --skip-* 确定要运行的阶段."""
    start_idx = PHASE_ORDER.index(args.start_from)
    stop_idx = PHASE_ORDER.index(args.stop_after)
    if start_idx > stop_idx:
        return []
    phases = PHASE_ORDER[start_idx: stop_idx + 1]
    skip_map = {
        "phase2": args.skip_phase2,
        "step3": args.skip_step3,
        "import_candidates": args.skip_import_candidates,
        "phase3": args.skip_phase3,
        "phase4": args.skip_phase4,
        "phase5": args.skip_phase5,
        "decision": args.skip_decision,
    }
    return [p for p in phases if not skip_map.get(p, False)]


def _validate_research_window(start: str | None, end: str | None) -> dict[str, str]:
    """Return one strict non-empty UTC-day research window."""

    values: dict[str, str] = {}
    parsed: dict[str, date] = {}
    for name, raw in (("start", start), ("end", end)):
        if not isinstance(raw, str) or not raw or raw != raw.strip():
            raise ValueError("research_window_invalid")
        try:
            day = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("research_window_invalid") from exc
        if day.isoformat() != raw:
            raise ValueError("research_window_invalid")
        values[name] = raw
        parsed[name] = day
    if parsed["end"] <= parsed["start"]:
        raise ValueError("research_window_invalid")
    return values


def _extract_json_marker(text: str, prefix: str) -> dict | None:
    for line in reversed((text or "").splitlines()):
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        try:
            payload = _strict_json_loads(stripped[len(prefix):])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None
    return None


def _bind_research_result(
    phase_result: dict,
    *,
    phase: str,
    expected_dataset_version: str,
    expected_symbol: str,
    expected_window: dict[str, str],
    expected_step2: dict | None = None,
    project_root: Path | None = None,
) -> dict:
    """Validate and normalize one subprocess-bound research artifact marker."""

    if phase not in {"step2", "step3"}:
        raise ValueError("research_result_phase_invalid")
    marker = phase_result.get("structured_result")
    if not isinstance(marker, dict):
        raise ValueError("research_result_marker_missing")
    expected_status = {
        "success": "succeeded",
        "partial_success": "partial_success",
    }.get(phase_result.get("status"))
    schema = f"aats.{phase}_result.v1"
    round_id = marker.get("round_id")
    candidate_sha256 = marker.get("candidate_sha256")
    if (
        expected_status is None
        or marker.get("schema_version") != schema
        or marker.get("phase") != phase
        or marker.get("status") != expected_status
        or not isinstance(round_id, str)
        or _ROUND_ID_RE.fullmatch(round_id) is None
        or marker.get("symbol") != expected_symbol
        or marker.get("dataset_version") != expected_dataset_version
        or marker.get("window") != expected_window
        or not isinstance(candidate_sha256, str)
        or _SHA256_RE.fullmatch(candidate_sha256) is None
    ):
        raise ValueError("research_result_identity_invalid")

    root = (project_root or _PROJECT_ROOT).resolve()
    phase_dir = "step2_rounds" if phase == "step2" else "step3_rounds"
    candidate_name = (
        "parameter_candidates.json"
        if phase == "step2"
        else "parameter_candidates_merged.json"
    )
    expected_round_dir = (
        root / "artifacts" / "research" / phase_dir / round_id
    )
    expected_candidate = expected_round_dir / candidate_name
    round_dir_raw = marker.get("round_dir")
    candidate_raw = marker.get("candidate_path")
    if not isinstance(round_dir_raw, str) or not isinstance(candidate_raw, str):
        raise ValueError("research_result_path_invalid")
    round_dir_path = Path(round_dir_raw)
    candidate_path = Path(candidate_raw)
    if not round_dir_path.is_absolute() or not candidate_path.is_absolute():
        raise ValueError("research_result_path_invalid")
    try:
        if round_dir_path.is_symlink() or candidate_path.is_symlink():
            raise ValueError("research_result_path_invalid")
        resolved_round_dir = round_dir_path.resolve(strict=True)
        resolved_candidate = candidate_path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("research_result_path_invalid") from exc
    if (
        resolved_round_dir != expected_round_dir.resolve()
        or resolved_candidate != expected_candidate.resolve()
        or resolved_candidate.parent != resolved_round_dir
        or not resolved_candidate.is_file()
    ):
        raise ValueError("research_result_path_invalid")
    if hashlib.sha256(resolved_candidate.read_bytes()).hexdigest() != candidate_sha256:
        raise ValueError("research_result_digest_invalid")

    if phase == "step3":
        step2_round_id = marker.get("step2_round_id")
        step2_sha256 = marker.get("step2_candidate_sha256")
        if (
            not isinstance(step2_round_id, str)
            or _ROUND_ID_RE.fullmatch(step2_round_id) is None
            or not isinstance(step2_sha256, str)
            or _SHA256_RE.fullmatch(step2_sha256) is None
        ):
            raise ValueError("research_result_step2_identity_invalid")
        if expected_step2 is not None and (
            step2_round_id != expected_step2.get("round_id")
            or step2_sha256 != expected_step2.get("candidate_sha256")
        ):
            raise ValueError("research_result_step2_identity_invalid")

    bound = dict(marker)
    bound["round_dir"] = str(resolved_round_dir)
    bound["candidate_path"] = str(resolved_candidate)
    return bound


def _load_research_result_ref(
    result_ref: str,
    *,
    phase: str,
    expected_dataset_version: str,
    expected_symbol: str,
    expected_window: dict[str, str] | None,
    expected_step2: dict | None = None,
    project_root: Path | None = None,
) -> dict:
    """Load one explicit immutable ``round_result.json`` reference.

    Resume mode must never rediscover a global ``latest`` directory.  The
    caller names one sidecar and this loader verifies that the sidecar, round
    directory, candidate path, digest and (for Step 3) Step 2 identity all
    describe the same completed research round.
    """

    root = (project_root or _PROJECT_ROOT).resolve()
    ref_path = Path(result_ref)
    if not ref_path.is_absolute():
        ref_path = root / ref_path
    try:
        if ref_path.name != "round_result.json" or ref_path.is_symlink():
            raise ValueError("research_result_ref_path_invalid")
        if ref_path.parent.is_symlink():
            raise ValueError("research_result_ref_path_invalid")
        resolved_ref = ref_path.resolve(strict=True)
        marker = _strict_json_loads(resolved_ref.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("research_result_ref_invalid") from exc
    if not isinstance(marker, dict):
        raise ValueError("research_result_ref_invalid")

    if expected_window is None:
        marker_window = marker.get("window")
        if not isinstance(marker_window, dict):
            raise ValueError("research_result_ref_window_invalid")
        try:
            expected_window = _validate_research_window(
                marker_window.get("start"),
                marker_window.get("end"),
            )
        except ValueError as exc:
            raise ValueError("research_result_ref_window_invalid") from exc

    marker_status = marker.get("status")
    if marker_status == "succeeded":
        phase_status = "success"
    elif marker_status == "partial_success":
        phase_status = "partial_success"
    else:
        raise ValueError("research_result_ref_incomplete")
    bound = _bind_research_result(
        {
            "phase": phase,
            "status": phase_status,
            "structured_result": marker,
        },
        phase=phase,
        expected_dataset_version=expected_dataset_version,
        expected_symbol=expected_symbol,
        expected_window=expected_window,
        expected_step2=expected_step2,
        project_root=root,
    )
    expected_ref = Path(bound["round_dir"]) / "round_result.json"
    if resolved_ref != expected_ref.resolve(strict=True):
        raise ValueError("research_result_ref_path_invalid")
    return bound


def _read_stable_regular_file(path: Path, *, max_bytes: int, label: str) -> bytes:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError(f"{label}_path_invalid")
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label}_unreadable") from exc
    if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_size > max_bytes:
        raise ValueError(f"{label}_invalid")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"{label}_unreadable") from exc
    try:
        before = os.fstat(descriptor)
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"{label}_too_large")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after_path = path.lstat()
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if (
        not stat.S_ISREG(before.st_mode)
        or any(getattr(before, field) != getattr(after, field) for field in stable_fields)
        or any(getattr(after, field) != getattr(after_path, field) for field in stable_fields)
    ):
        raise ValueError(f"{label}_changed_during_read")
    return b"".join(chunks)


def _bind_phase34_result(
    phase_result: dict,
    *,
    phase: str,
    expected_dataset_version: str,
    expected_symbol: str,
    expected_window: dict[str, str],
    expected_step3: dict,
    expected_replay_only: bool | None = None,
    project_root: Path | None = None,
) -> dict:
    if phase not in {"phase3", "phase4"}:
        raise ValueError("phase34_result_phase_invalid")
    marker = phase_result.get("structured_result")
    expected_status = {
        "success": "succeeded",
        "partial_success": "partial_success",
    }.get(phase_result.get("status"))
    if not isinstance(marker, dict) or expected_status is None:
        raise ValueError("phase34_result_marker_invalid")
    round_id = marker.get("round_id")
    expected_exit_code = 0 if expected_status == "succeeded" else 2
    expected_schema = f"aats.{phase}_result.v1"
    if (
        marker.get("schema_version") != expected_schema
        or marker.get("phase") != phase
        or marker.get("status") != expected_status
        or marker.get("exit_code") != expected_exit_code
        or phase_result.get("exit_code") != expected_exit_code
        or not isinstance(round_id, str)
        or _ROUND_ID_RE.fullmatch(round_id) is None
        or marker.get("symbol") != expected_symbol
        or marker.get("dataset_version") != expected_dataset_version
        or marker.get("window") != expected_window
        or marker.get("source_step3_round_id") != expected_step3.get("round_id")
        or marker.get("source_step3_candidate_sha256")
        != expected_step3.get("candidate_sha256")
    ):
        raise ValueError("phase34_result_identity_invalid")
    if phase == "phase3" and (
        type(marker.get("replay_only")) is not bool
        or (
            expected_replay_only is not None
            and marker.get("replay_only") is not expected_replay_only
        )
    ):
        raise ValueError("phase34_result_replay_mode_invalid")

    root = (project_root or _PROJECT_ROOT).resolve()
    phase_dir = "attribution_rounds" if phase == "phase3" else "execution_rounds"
    expected_round_dir = root / "artifacts" / "research" / phase_dir / round_id
    expected_manifest_path = expected_round_dir / "round_manifest.json"
    round_dir_raw = marker.get("round_dir")
    manifest_path_raw = marker.get("manifest_path")
    if not isinstance(round_dir_raw, str) or not isinstance(manifest_path_raw, str):
        raise ValueError("phase34_result_path_invalid")
    round_dir = Path(round_dir_raw)
    manifest_path = Path(manifest_path_raw)
    try:
        if round_dir.is_symlink() or manifest_path.is_symlink():
            raise ValueError("phase34_result_path_invalid")
        resolved_round_dir = round_dir.resolve(strict=True)
        resolved_manifest = manifest_path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("phase34_result_path_invalid") from exc
    if (
        resolved_round_dir != expected_round_dir
        or resolved_manifest != expected_manifest_path
        or resolved_manifest.parent != resolved_round_dir
    ):
        raise ValueError("phase34_result_path_invalid")

    manifest_bytes = _read_stable_regular_file(
        resolved_manifest,
        max_bytes=4 * 1024 * 1024,
        label="phase34_manifest",
    )
    manifest_sha256 = marker.get("manifest_sha256")
    if (
        not isinstance(manifest_sha256, str)
        or _SHA256_RE.fullmatch(manifest_sha256) is None
        or hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha256
        or marker.get("manifest_size_bytes") != len(manifest_bytes)
    ):
        raise ValueError("phase34_result_manifest_digest_invalid")
    try:
        manifest = _strict_json_loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("phase34_result_manifest_invalid") from exc
    parameter_input = manifest.get("parameter_input") if isinstance(manifest, dict) else None
    scope = manifest.get("scope") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("round_id") != round_id
        or manifest.get("phase") != phase
        or manifest.get("status") != expected_status
        or manifest.get("overall_status") != expected_status
        or manifest.get("symbol") != expected_symbol
        or manifest.get("window") != expected_window
        or not isinstance(scope, dict)
        or scope.get("symbol") != expected_symbol
        or scope.get("window") != expected_window
        or not isinstance(parameter_input, dict)
        or parameter_input.get("source_step3_round_id") != expected_step3.get("round_id")
        or parameter_input.get("source_step3_candidate_sha256")
        != expected_step3.get("candidate_sha256")
    ):
        raise ValueError("phase34_result_manifest_identity_invalid")
    combos = manifest.get("combos")
    if not isinstance(combos, list) or len(combos) != len(
        _EXPECTED_PHASE34_COMBOS
    ):
        raise ValueError("phase34_result_manifest_topology_invalid")
    seen_combo_keys: set[str] = set()
    combo_statuses: list[str] = []
    for combo in combos:
        if not isinstance(combo, dict):
            raise ValueError("phase34_result_manifest_topology_invalid")
        combo_key = combo.get("key")
        expected_combo = _EXPECTED_PHASE34_COMBOS.get(combo_key)
        if (
            expected_combo is None
            or combo_key in seen_combo_keys
            or combo.get("family") != expected_combo[0]
            or combo.get("timeframe") != expected_combo[1]
        ):
            raise ValueError("phase34_result_manifest_topology_invalid")
        seen_combo_keys.add(combo_key)
        combo_status = combo.get("status")
        if combo_status not in {"succeeded", "partial_success", "failed"}:
            raise ValueError("phase34_result_manifest_topology_invalid")
        combo_statuses.append(combo_status)
        if (
            combo.get("source_step3_round_id") != expected_step3.get("round_id")
            or combo.get("source_step3_candidate_sha256")
            != expected_step3.get("candidate_sha256")
        ):
            raise ValueError("phase34_result_combo_lineage_invalid")
    if seen_combo_keys != set(_EXPECTED_PHASE34_COMBOS):
        raise ValueError("phase34_result_manifest_topology_invalid")
    recomputed_status = (
        "succeeded"
        if all(status == "succeeded" for status in combo_statuses)
        else "failed"
        if all(status == "failed" for status in combo_statuses)
        else "partial_success"
    )
    if recomputed_status != expected_status:
        raise ValueError("phase34_result_manifest_status_invalid")

    result_path = resolved_round_dir / "round_result.json"
    result_bytes = _read_stable_regular_file(
        result_path,
        max_bytes=1024 * 1024,
        label="phase34_round_result",
    )
    try:
        persisted_marker = _strict_json_loads(result_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("phase34_round_result_invalid") from exc
    if persisted_marker != marker:
        raise ValueError("phase34_result_marker_sidecar_mismatch")

    bound = dict(marker)
    bound["round_dir"] = str(resolved_round_dir)
    bound["manifest_path"] = str(resolved_manifest)
    bound["result_path"] = str(result_path)
    bound["result_sha256"] = hashlib.sha256(result_bytes).hexdigest()
    return bound


def _load_phase34_result_ref(
    result_ref: str,
    *,
    phase: str,
    expected_dataset_version: str,
    expected_symbol: str,
    expected_window: dict[str, str],
    expected_step3: dict,
    expected_replay_only: bool | None = None,
    project_root: Path | None = None,
) -> dict:
    root = (project_root or _PROJECT_ROOT).resolve()
    ref_path = Path(result_ref)
    if not ref_path.is_absolute():
        ref_path = root / ref_path
    try:
        resolved_ref = ref_path.resolve(strict=True)
        marker = _strict_json_loads(
            _read_stable_regular_file(
                resolved_ref,
                max_bytes=1024 * 1024,
                label="phase34_round_result",
            ).decode("utf-8")
        )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("phase34_result_ref_invalid") from exc
    if ref_path.name != "round_result.json" or not isinstance(marker, dict):
        raise ValueError("phase34_result_ref_invalid")
    marker_status = marker.get("status")
    status = (
        "success"
        if marker_status == "succeeded"
        else "partial_success"
        if marker_status == "partial_success"
        else None
    )
    if status is None:
        raise ValueError("phase34_result_ref_incomplete")
    bound = _bind_phase34_result(
        {
            "phase": phase,
            "status": status,
            "exit_code": marker.get("exit_code"),
            "structured_result": marker,
        },
        phase=phase,
        expected_dataset_version=expected_dataset_version,
        expected_symbol=expected_symbol,
        expected_window=expected_window,
        expected_step3=expected_step3,
        expected_replay_only=expected_replay_only,
        project_root=root,
    )
    if Path(bound["result_path"]) != resolved_ref:
        raise ValueError("phase34_result_ref_path_invalid")
    return bound


def _blocked_upstream_result(phase: str, *dependencies: str) -> dict:
    """Return a stable failure result without starting a dependent child."""

    return {
        "phase": phase,
        "status": "failed",
        "exit_code": -1,
        "error": "blocked_upstream",
        "blocked_by": list(dependencies),
    }


def _failed_prior_dependencies(
    phase: str,
    *,
    scheduled_phases: list[str],
    results: list[dict],
) -> list[str]:
    """Find completed scheduled prerequisites that failed or were blocked."""

    dependency_map = {
        "phase5": ("import_candidates", "phase3", "phase4"),
        "decision": tuple(PHASE_ORDER[:-1]),
    }
    dependencies = dependency_map.get(phase, ())
    result_by_phase = {str(item.get("phase")): item for item in results}
    return [
        dependency
        for dependency in dependencies
        if dependency in scheduled_phases
        and result_by_phase.get(dependency, {}).get("status")
        in {"failed", "timeout", "error"}
    ]


def _run_streaming_with_marker(
    cmd: list[str],
    *,
    result_prefix: str,
    child_env: dict[str, str] | None,
    timeout_s: float,
) -> tuple[int, dict | None]:
    """Stream child logs live while retaining exactly one result marker."""

    effective_env = os.environ.copy()
    if child_env is not None:
        effective_env.update(child_env)
    effective_env["PYTHONUTF8"] = "1"
    effective_env["PYTHONIOENCODING"] = "utf-8"
    process = subprocess.Popen(
        cmd,
        cwd=str(_PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=effective_env,
    )
    if process.stdout is None or process.stderr is None:  # pragma: no cover
        process.kill()
        raise RuntimeError("rdp_phase_stream_unavailable")

    events: queue.Queue[tuple[str, str | None]] = queue.Queue(maxsize=1024)

    def _pump(stream_name: str, stream: object) -> None:
        try:
            for line in iter(stream.readline, ""):  # type: ignore[attr-defined]
                events.put((stream_name, line))
        finally:
            events.put((stream_name, None))

    threads = [
        threading.Thread(
            target=_pump,
            args=("stdout", process.stdout),
            daemon=True,
        ),
        threading.Thread(
            target=_pump,
            args=("stderr", process.stderr),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()

    deadline = _time.monotonic() + timeout_s
    closed_streams = 0
    marker_count = 0
    marker: dict | None = None
    try:
        while closed_streams < len(threads):
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, timeout_s)
            try:
                stream_name, line = events.get(timeout=min(0.25, remaining))
            except queue.Empty:
                continue
            if line is None:
                closed_streams += 1
                continue
            print(
                line,
                end="" if line.endswith("\n") else "\n",
                file=sys.stdout if stream_name == "stdout" else sys.stderr,
                flush=True,
            )
            if line.strip().startswith(result_prefix):
                marker_count += 1
                parsed = _extract_json_marker(line, result_prefix)
                marker = parsed if marker_count == 1 and parsed is not None else None

        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(cmd, timeout_s)
        return_code = process.wait(timeout=remaining)
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        raise
    finally:
        process.stdout.close()
        process.stderr.close()

    if marker_count != 1:
        marker = None
    return return_code, marker


def _run_phase(
    name: str,
    cmd: list[str],
    *,
    dry_run: bool = False,
    result_prefix: str | None = None,
    child_env: dict[str, str] | None = None,
) -> dict:
    """执行单个阶段, 返回结果 dict."""
    label = PHASE_LABELS[name]
    display_parts: list[str] = []
    redact_next = False
    for part in cmd:
        if redact_next:
            display_parts.append("<redacted-live-db-url>")
            redact_next = False
            continue
        display_parts.append(part)
        if part == "--live-db-url":
            redact_next = True
    cmd_display = " ".join(display_parts)

    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"{'=' * 60}")
    log.info("命令: %s", cmd_display)

    if dry_run:
        log.info("[DRY RUN] 跳过执行")
        return {"phase": name, "status": "dry_run", "exit_code": 0}

    started = datetime.now(timezone.utc)
    try:
        if result_prefix:
            return_code, structured_result = _run_streaming_with_marker(
                cmd,
                result_prefix=result_prefix,
                child_env=child_env,
                timeout_s=7200,
            )
        else:
            result = subprocess.run(
                cmd,
                cwd=str(_PROJECT_ROOT),
                timeout=7200,
                env=child_env,
            )
            return_code = result.returncode
            structured_result = None
        finished = datetime.now(timezone.utc)
        elapsed = (finished - started).total_seconds()

        if (
            result_prefix
            and return_code in {0, 2}
            and structured_result is None
        ):
            error = f"missing structured result marker: {result_prefix}"
            log.error("%s 合同失败：%s", label, error)
            return {
                "phase": name,
                "status": "failed",
                "exit_code": return_code,
                "elapsed_s": elapsed,
                "error": error,
            }

        if return_code == 0:
            log.info("%s 完成 (%.0fs)", label, elapsed)
            phase_result = {
                "phase": name, "status": "success",
                "exit_code": 0, "elapsed_s": elapsed,
            }
            if structured_result is not None:
                phase_result["structured_result"] = structured_result
            return phase_result
        elif return_code == 2 and name in _PARTIAL_SUCCESS_PHASES:
            # exit code 2 = 部分成功 (某些 batch 通过, 仍有可用产物)
            log.warning(
                "%s 部分成功 (exit=2, %.0fs) — 部分 batch 失败但仍有产物",
                label, elapsed,
            )
            phase_result = {
                "phase": name, "status": "partial_success",
                "exit_code": 2, "elapsed_s": elapsed,
            }
            if structured_result is not None:
                phase_result["structured_result"] = structured_result
            return phase_result
        else:
            log.warning(
                "%s 失败 (exit=%d, %.0fs)",
                label, return_code, elapsed,
            )
            return {
                "phase": name, "status": "failed",
                "exit_code": return_code, "elapsed_s": elapsed,
            }

    except subprocess.TimeoutExpired:
        log.error("%s 超时 (>7200s)", label)
        return {"phase": name, "status": "timeout", "exit_code": -1}
    except Exception as exc:
        failure_type = type(exc).__name__
        log.error("%s 异常 (%s)", label, failure_type)
        return {
            "phase": name, "status": "error",
            "exit_code": -1,
            "error": "rdp_phase_execution_failed",
            "error_type": failure_type,
        }


def _emit_pipeline_result(
    *,
    pipeline_id: str,
    status: str,
    results: list[dict],
) -> None:
    first_issue = next(
        (
            item
            for item in results
            if item.get("status") in ("partial_success", "failed", "timeout", "error")
        ),
        None,
    )
    first_failure = None
    if first_issue is not None:
        phase = str(first_issue.get("phase") or "unknown")
        first_failure = {
            "phase": phase,
            "phase_label": PHASE_LABELS.get(phase, phase),
            "status": first_issue.get("status"),
            "exit_code": first_issue.get("exit_code"),
            "error": first_issue.get("error"),
        }
    decision_phase = next(
        (item for item in results if item.get("phase") == "decision"),
        {},
    )
    decision_result = decision_phase.get("structured_result")
    if not isinstance(decision_result, dict):
        decision_result = {}
    payload = {
        "pipeline_id": pipeline_id,
        "status": status,
        "first_failure": first_failure,
        "research_outcome": decision_result.get("research_outcome", "unknown"),
        "decision_round_id": decision_result.get("round_id"),
        "readiness": decision_result.get("readiness"),
        "decision_result": decision_result or None,
    }
    print(
        _PIPELINE_RESULT_PREFIX
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        flush=True,
    )


# =========================================================================
# 命令构建
# =========================================================================
def _build_phase2_cmd(args: argparse.Namespace, *, ensure: bool) -> list[str]:
    cmd = [sys.executable, str(_SCRIPT_DIR / "rdp_run_step2_research.py")]
    if ensure:
        cmd.append("--ensure-schema")
    if args.start:
        cmd.extend(["--start", args.start])
    if args.end:
        cmd.extend(["--end", args.end])
    if args.dataset_version:
        cmd.extend(["--dataset-version", args.dataset_version])
    if args.skip_calibration:
        cmd.append("--skip-calibration")
    if args.skip_scan:
        cmd.append("--skip-scan")
    cmd.append("--no-print-summary")
    return cmd


def _build_step3_cmd(
    args: argparse.Namespace,
    *,
    ensure: bool,
    step2_round_dir: str | None = None,
) -> list[str]:
    """构建 Step 3 扩展参数扫描命令。"""
    cmd = [sys.executable, str(_SCRIPT_DIR / "rdp_run_step3_research.py")]
    if ensure:
        cmd.append("--ensure-schema")
    if step2_round_dir:
        cmd.extend(["--step2-round-dir", step2_round_dir])
    if args.start:
        cmd.extend(["--start", args.start])
    if args.end:
        cmd.extend(["--end", args.end])
    if args.dataset_version:
        cmd.extend(["--dataset-version", args.dataset_version])
    cmd.append("--no-print-summary")
    return cmd


def _build_phase3_cmd(
    args: argparse.Namespace,
    *,
    ensure: bool,
    params_json: str | None,
) -> list[str]:
    cmd = [
        sys.executable, str(_SCRIPT_DIR / "rdp_run_phase3_round.py"),
        "--start", args.start,
        "--end", args.end,
        "--dataset-version", args.dataset_version,
    ]
    if ensure:
        cmd.append("--ensure-schema")
    if args.replay_only:
        cmd.append("--replay-only")
    if params_json:
        cmd.extend(["--params-json", params_json])
    cmd.append("--no-print-summary")
    return cmd


def _build_phase4_cmd(
    args: argparse.Namespace,
    *,
    ensure: bool,
    params_json: str | None,
) -> list[str]:
    cmd = [
        sys.executable, str(_SCRIPT_DIR / "rdp_run_phase4_round.py"),
        "--start", args.start,
        "--end", args.end,
        "--dataset-version", args.dataset_version,
        "--taker-fee-bps", str(args.taker_fee_bps),
    ]
    if ensure:
        cmd.append("--ensure-schema")
    if params_json:
        cmd.extend(["--params-json", params_json])
    cmd.append("--no-print-summary")
    return cmd


def _build_phase5_cmd() -> list[str]:
    """Phase 5 治理刷新: 调用 governance_cycle workflow."""
    return [
        sys.executable,
        str(_SCRIPT_DIR / "rdp_run_scheduled_workflow.py"),
        "--workflow", "governance_cycle",
    ]


def _build_decision_cmd(
    args: argparse.Namespace,
    *,
    step2_round_id: str,
    phase3_round_id: str,
    phase4_round_id: str,
) -> list[str]:
    cmd = [sys.executable, str(_SCRIPT_DIR / "rdp_run_decision_round.py")]
    cmd.extend(["--expected-step2-round-id", step2_round_id])
    cmd.extend(["--expected-phase3-round-id", phase3_round_id])
    cmd.extend(["--expected-phase4-round-id", phase4_round_id])
    if args.include_draft:
        cmd.append("--include-draft")
    cmd.append("--no-print-summary")
    return cmd


# =========================================================================
# 主流程
# =========================================================================
def main() -> int:
    args = parse_args()

    phases = _resolve_phases(args)
    if not phases:
        print("没有需要运行的阶段。请检查 --start-from / --stop-after / --skip-* 参数。")
        return 2

    # --lookback-days 自动计算 --start / --end（显式值优先）
    if args.lookback_days is not None and args.lookback_days > 0:
        today_utc = datetime.now(timezone.utc).date()
        if not args.start:
            args.start = (today_utc - timedelta(days=args.lookback_days)).isoformat()
            log.info("--lookback-days %d → --start %s", args.lookback_days, args.start)
        if not args.end:
            args.end = today_utc.isoformat()
            log.info("--lookback-days %d → --end %s", args.lookback_days, args.end)

    # 所有研究阶段必须在启动任何昂贵工作前绑定同一非空 UTC 窗口。
    needs_dates = any(
        p in phases for p in ("phase2", "step3", "phase3", "phase4")
    )
    if needs_dates:
        try:
            research_window = _validate_research_window(args.start, args.end)
        except ValueError:
            print(
                "错误: Phase 2/Step 3/Phase 3/4 研究阶段需要有效且递增的 "
                "YYYY-MM-DD --start/--end（或使用 --lookback-days）。"
            )
            return 2
    else:
        research_window = None

    if (
        "phase3" in phases
        and not args.replay_only
        and not (args.live_db_url or os.environ.get("RDP_LIVE_DATABASE_URL"))
    ):
        print(
            "错误: Phase 3 live attribution 需要 --live-db-url 或 "
            "RDP_LIVE_DATABASE_URL；如确需纯回放，必须显式传入 --replay-only。"
        )
        return 2

    phase2_will_run = "phase2" in phases
    step3_will_run = "step3" in phases
    phase3_will_run = "phase3" in phases
    phase4_will_run = "phase4" in phases
    governance_consumers = {"phase5", "decision"}.intersection(phases)
    step3_consumers = {
        "import_candidates",
        "phase3",
        "phase4",
        "phase5",
        "decision",
    }.intersection(phases)
    needs_step2_ref = step3_will_run and not phase2_will_run
    needs_step3_ref = bool(step3_consumers) and not step3_will_run
    needs_phase3_ref = bool(governance_consumers) and not phase3_will_run
    needs_phase4_ref = bool(governance_consumers) and not phase4_will_run

    if phase2_will_run and not step3_will_run and step3_consumers:
        print(
            "错误: 本次 Phase 2 产物无法跨过被跳过的 Step 3 "
            "与下游证据链绑定；请运行 Step 3 或从下游阶段续跑。"
        )
        return 2
    if args.step2_result_ref and phase2_will_run:
        print("错误: 本次将运行 Phase 2，不能同时指定 --step2-result-ref。")
        return 2
    if args.step3_result_ref and step3_will_run:
        print("错误: 本次将运行 Step 3，不能同时指定 --step3-result-ref。")
        return 2
    if needs_step2_ref and not args.step2_result_ref and not args.dry_run:
        print("错误: 跳过 Phase 2 后运行 Step 3 必须指定 --step2-result-ref。")
        return 2
    if needs_step3_ref and not args.step3_result_ref and not args.dry_run:
        print(
            "错误: 跳过 Step 3 后运行候选导入或 Phase 3/4 "
            "必须指定 --step3-result-ref。"
        )
        return 2
    if args.step2_result_ref and not (
        needs_step2_ref or args.step3_result_ref
    ):
        print("错误: --step2-result-ref 未被本次调度的阶段使用。")
        return 2
    if args.step3_result_ref and not needs_step3_ref:
        print("错误: --step3-result-ref 未被本次调度的阶段使用。")
        return 2
    if args.phase3_result_ref and phase3_will_run:
        print("错误: 本次将运行 Phase 3，不能同时指定 --phase3-result-ref。")
        return 2
    if args.phase4_result_ref and phase4_will_run:
        print("错误: 本次将运行 Phase 4，不能同时指定 --phase4-result-ref。")
        return 2
    if needs_phase3_ref and not args.phase3_result_ref and not args.dry_run:
        print(
            "错误: 跳过 Phase 3 后运行治理刷新或 Decision "
            "必须指定 --phase3-result-ref。"
        )
        return 2
    if needs_phase4_ref and not args.phase4_result_ref and not args.dry_run:
        print(
            "错误: 跳过 Phase 4 后运行治理刷新或 Decision "
            "必须指定 --phase4-result-ref。"
        )
        return 2
    if args.phase3_result_ref and not needs_phase3_ref:
        print("错误: --phase3-result-ref 未被本次调度的阶段使用。")
        return 2
    if args.phase4_result_ref and not needs_phase4_ref:
        print("错误: --phase4-result-ref 未被本次调度的阶段使用。")
        return 2
    if args.params_json and (phase2_will_run or step3_will_run):
        print(
            "错误: 运行 Phase 2/Step 3 时不能使用 --params-json；"
            "下游必须消费本次 Step 3 的精确产物。"
        )
        return 2

    bound_step2_result: dict | None = None
    bound_step3_result: dict | None = None
    bound_phase3_result: dict | None = None
    bound_phase4_result: dict | None = None
    try:
        if args.step2_result_ref:
            bound_step2_result = _load_research_result_ref(
                args.step2_result_ref,
                phase="step2",
                expected_dataset_version=args.dataset_version,
                expected_symbol="BTC-USDT-SWAP",
                expected_window=research_window,
            )
        if args.step3_result_ref:
            bound_step3_result = _load_research_result_ref(
                args.step3_result_ref,
                phase="step3",
                expected_dataset_version=args.dataset_version,
                expected_symbol="BTC-USDT-SWAP",
                expected_window=(
                    research_window
                    or (
                        bound_step2_result.get("window")
                        if bound_step2_result is not None
                        else None
                    )
                ),
                expected_step2=bound_step2_result,
            )
        if args.phase3_result_ref:
            if bound_step3_result is None:
                raise ValueError("phase3_result_step3_not_bound")
            bound_phase3_result = _load_phase34_result_ref(
                args.phase3_result_ref,
                phase="phase3",
                expected_dataset_version=args.dataset_version,
                expected_symbol="BTC-USDT-SWAP",
                expected_window=dict(bound_step3_result["window"]),
                expected_step3=bound_step3_result,
                expected_replay_only=args.replay_only,
            )
        if args.phase4_result_ref:
            if bound_step3_result is None:
                raise ValueError("phase4_result_step3_not_bound")
            bound_phase4_result = _load_phase34_result_ref(
                args.phase4_result_ref,
                phase="phase4",
                expected_dataset_version=args.dataset_version,
                expected_symbol="BTC-USDT-SWAP",
                expected_window=dict(bound_step3_result["window"]),
                expected_step3=bound_step3_result,
            )
    except (OSError, ValueError) as exc:
        print(f"错误: 研究结果引用无效 ({exc})。")
        return 2

    exact_step3_candidate = (
        Path(bound_step3_result["candidate_path"])
        if bound_step3_result is not None
        else None
    )
    if args.params_json:
        if exact_step3_candidate is None:
            print(
                "错误: --params-json 必须与 --step3-result-ref "
                "绑定的 candidate 为同一文件。"
            )
            return 2
        supplied_params = Path(args.params_json)
        if not supplied_params.is_absolute():
            supplied_params = _PROJECT_ROOT / supplied_params
        try:
            if supplied_params.is_symlink():
                raise ValueError("params_json_path_invalid")
            resolved_params = supplied_params.resolve(strict=True)
        except (OSError, ValueError):
            print("错误: --params-json 路径无效或不可受信。")
            return 2
        if resolved_params != exact_step3_candidate:
            print(
                "错误: --params-json 与 --step3-result-ref 的精确 "
                "candidate 不一致，拒绝分裂证据链。"
            )
            return 2

    pipeline_id = (
        datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        + "_" + uuid4().hex[:8]
    )
    pipeline_started = datetime.now(timezone.utc)

    print("=" * 60)
    print(f"  RDP 研究管线 | {pipeline_id}")
    print(f"  阶段: {' -> '.join(PHASE_LABELS[p] for p in phases)}")
    if args.dry_run:
        print("  [DRY RUN 模式]")
    print("=" * 60)

    results: list[dict] = []
    params_json: str | None = (
        str(exact_step3_candidate)
        if exact_step3_candidate is not None
        else None
    )
    schema_done = False

    # 追踪 Step 2 最新 round dir (供 Step 3 使用)
    step2_latest_round_dir: str | None = (
        str(bound_step2_result["round_dir"])
        if bound_step2_result is not None
        else None
    )

    for phase in phases:
        blocked_by: list[str] = []
        if not args.dry_run:
            if phase == "step3" and bound_step2_result is None:
                blocked_by = ["phase2"]
            elif phase in {"import_candidates", "phase3", "phase4"} and (
                bound_step3_result is None
            ):
                blocked_by = ["step3"]
            elif phase in {"phase5", "decision"}:
                missing_research = [
                    name
                    for name, bound in (
                        ("step3", bound_step3_result),
                        ("phase3", bound_phase3_result),
                        ("phase4", bound_phase4_result),
                    )
                    if bound is None
                ]
                blocked_by = missing_research or _failed_prior_dependencies(
                    phase,
                    scheduled_phases=phases,
                    results=results,
                )
        if blocked_by:
            r = _blocked_upstream_result(phase, *blocked_by)
            results.append(r)
            log.error(
                "%s 未启动：上游阶段未形成可受信证据 (%s)",
                PHASE_LABELS[phase],
                ", ".join(blocked_by),
            )
            if not args.no_stop_on_failure:
                break
            continue

        # ── Phase 2 (Step 2) ──
        if phase == "phase2":
            ensure = args.ensure_schema and not schema_done
            cmd = _build_phase2_cmd(args, ensure=ensure)
            if ensure:
                schema_done = True

            r = _run_phase(
                "phase2",
                cmd,
                dry_run=args.dry_run,
                result_prefix=_STEP2_RESULT_PREFIX,
            )
            results.append(r)

            # Phase 2 只消费本子进程显式返回并经 digest 绑定的产物。
            if not args.dry_run and r["status"] in ("success", "partial_success"):
                try:
                    bound_step2_result = _bind_research_result(
                        r,
                        phase="step2",
                        expected_dataset_version=args.dataset_version,
                        expected_symbol="BTC-USDT-SWAP",
                        expected_window=research_window,
                    )
                except (OSError, ValueError) as exc:
                    r["status"] = "failed"
                    r["error"] = str(exc)
                    log.error("Step 2 运行级产物绑定失败: %s", exc)
                else:
                    step2_latest_round_dir = bound_step2_result["round_dir"]
                    log.info("Step 2 round dir: %s", step2_latest_round_dir)

        # ── Step 3 (扩展参数扫描) ──
        elif phase == "step3":
            ensure = args.ensure_schema and not schema_done
            step2_round_dir_for_cmd = step2_latest_round_dir
            if (
                args.dry_run
                and step2_round_dir_for_cmd is None
                and phase2_will_run
            ):
                step2_round_dir_for_cmd = "<phase2-round-dir>"
            cmd = _build_step3_cmd(
                args, ensure=ensure,
                step2_round_dir=step2_round_dir_for_cmd,
            )
            if ensure:
                schema_done = True

            r = _run_phase(
                "step3",
                cmd,
                dry_run=args.dry_run,
                result_prefix=_STEP3_RESULT_PREFIX,
            )
            results.append(r)

            # Step 3 同样只消费本子进程返回的精确 round，禁止全局 latest 扫描。
            if (
                not args.dry_run
                and r["status"] in ("success", "partial_success")
            ):
                try:
                    bound_step3_result = _bind_research_result(
                        r,
                        phase="step3",
                        expected_dataset_version=args.dataset_version,
                        expected_symbol="BTC-USDT-SWAP",
                        expected_window=research_window,
                        expected_step2=bound_step2_result,
                    )
                except (OSError, ValueError) as exc:
                    r["status"] = "failed"
                    r["error"] = str(exc)
                    log.error("Step 3 运行级产物绑定失败: %s", exc)
                else:
                    exact_step3_candidate = Path(
                        bound_step3_result["candidate_path"]
                    )
                    params_json = str(exact_step3_candidate)
                    log.info("Step 3 合并参数文件: %s", params_json)

        # ── Import Candidates (Step 3 → Registry) ──
        elif phase == "import_candidates":
            if not args.dry_run:
                log.info("")
                log.info("=" * 60)
                log.info("  %s", PHASE_LABELS["import_candidates"])
                log.info("=" * 60)
                t0 = _time.monotonic()
                try:
                    from aats.data_platform.governance.auto_import_candidates import (
                        auto_import_latest_candidates,
                    )
                    if (
                        exact_step3_candidate is None
                        or bound_step3_result is None
                    ):
                        raise ValueError("step3_result_not_bound")
                    import_result = auto_import_latest_candidates(
                        _PROJECT_ROOT,
                        candidates_file=exact_step3_candidate,
                        expected_round_id=bound_step3_result["round_id"],
                        expected_candidate_sha256=bound_step3_result[
                            "candidate_sha256"
                        ],
                    )
                    elapsed = _time.monotonic() - t0
                    status = import_result["status"]
                    if status in _SUCCESSFUL_IMPORT_STATUSES:
                        source_file = import_result.get("source_file")
                        try:
                            source_file_path = (
                                Path(source_file)
                                if isinstance(source_file, str)
                                else None
                            )
                            if (
                                source_file_path is None
                                or source_file_path.is_symlink()
                            ):
                                raise ValueError("candidate_source_path_invalid")
                            source_path = source_file_path.resolve(strict=True)
                        except (OSError, ValueError):
                            source_path = None
                        if (
                            import_result.get("source_round_id")
                            != bound_step3_result["round_id"]
                            or import_result.get("source_candidate_sha256")
                            != bound_step3_result["candidate_sha256"]
                            or source_path != exact_step3_candidate
                        ):
                            raise ValueError("candidate_import_identity_mismatch")
                    log.info(
                        "参数导入结果: %s (导入 %d, 废弃 %d, %.1fs)",
                        status,
                        import_result["imported_count"],
                        import_result["deprecated_count"],
                        elapsed,
                    )
                    r = {
                        "phase": "import_candidates",
                        "status": (
                            "success"
                            if status in _SUCCESSFUL_IMPORT_STATUSES
                            else "failed"
                        ),
                        "duration_s": round(elapsed, 2),
                        "detail": import_result,
                    }
                except Exception as exc:
                    elapsed = _time.monotonic() - t0
                    failure_type = type(exc).__name__
                    log.error(
                        "参数导入失败 (%.1fs, %s)",
                        elapsed, failure_type,
                    )
                    r = {
                        "phase": "import_candidates",
                        "status": "error",
                        "duration_s": round(elapsed, 2),
                        "detail": {
                            "code": "parameter_candidate_import_failed",
                            "error_type": failure_type,
                        },
                    }
            else:
                log.info("[DRY-RUN] import_candidates: 自动导入 Step 3 参数到 registry")
                r = {"phase": "import_candidates", "status": "dry_run", "duration_s": 0}
            results.append(r)

        # ── Phase 3 ──
        elif phase == "phase3":
            ensure = args.ensure_schema and not schema_done
            params_for_cmd = params_json
            if args.dry_run and params_for_cmd is None and step3_will_run:
                params_for_cmd = "<step3-candidate-path>"
            cmd = _build_phase3_cmd(
                args, ensure=ensure, params_json=params_for_cmd,
            )
            if ensure:
                schema_done = True

            phase3_env = os.environ.copy()
            if args.live_db_url:
                phase3_env["RDP_LIVE_DATABASE_URL"] = args.live_db_url
            r = _run_phase(
                "phase3",
                cmd,
                dry_run=args.dry_run,
                result_prefix=_PHASE3_RESULT_PREFIX,
                child_env=phase3_env,
            )
            if (
                not args.dry_run
                and r.get("status") in {"success", "partial_success"}
            ):
                try:
                    if bound_step3_result is None or research_window is None:
                        raise ValueError("phase3_result_upstream_not_bound")
                    bound_phase3_result = _bind_phase34_result(
                        r,
                        phase="phase3",
                        expected_dataset_version=args.dataset_version,
                        expected_symbol="BTC-USDT-SWAP",
                        expected_window=research_window,
                        expected_step3=bound_step3_result,
                        expected_replay_only=args.replay_only,
                    )
                except (OSError, ValueError) as exc:
                    r = {
                        **r,
                        "status": "failed",
                        "error": f"phase3_result_binding_failed:{exc}",
                    }
            results.append(r)

        # ── Phase 4 ──
        elif phase == "phase4":
            ensure = args.ensure_schema and not schema_done
            params_for_cmd = params_json
            if args.dry_run and params_for_cmd is None and step3_will_run:
                params_for_cmd = "<step3-candidate-path>"
            cmd = _build_phase4_cmd(
                args, ensure=ensure, params_json=params_for_cmd,
            )
            if ensure:
                schema_done = True

            r = _run_phase(
                "phase4",
                cmd,
                dry_run=args.dry_run,
                result_prefix=_PHASE4_RESULT_PREFIX,
            )
            if (
                not args.dry_run
                and r.get("status") in {"success", "partial_success"}
            ):
                try:
                    if bound_step3_result is None or research_window is None:
                        raise ValueError("phase4_result_upstream_not_bound")
                    bound_phase4_result = _bind_phase34_result(
                        r,
                        phase="phase4",
                        expected_dataset_version=args.dataset_version,
                        expected_symbol="BTC-USDT-SWAP",
                        expected_window=research_window,
                        expected_step3=bound_step3_result,
                    )
                except (OSError, ValueError) as exc:
                    r = {
                        **r,
                        "status": "failed",
                        "error": f"phase4_result_binding_failed:{exc}",
                    }
            results.append(r)

        # ── Phase 5 (Governance) ──
        elif phase == "phase5":
            cmd = _build_phase5_cmd()
            r = _run_phase("phase5", cmd, dry_run=args.dry_run)
            results.append(r)

        # ── Decision ──
        elif phase == "decision":
            if (
                not args.dry_run
                and (bound_phase3_result is None or bound_phase4_result is None)
            ):
                r = _blocked_upstream_result("decision", "phase3", "phase4")
                results.append(r)
                continue
            cmd = _build_decision_cmd(
                args,
                step2_round_id=(
                    bound_step3_result["step2_round_id"]
                    if bound_step3_result is not None
                    else "<step2-round-id>"
                ),
                phase3_round_id=(
                    bound_phase3_result["round_id"]
                    if bound_phase3_result is not None
                    else "<phase3-round-id>"
                ),
                phase4_round_id=(
                    bound_phase4_result["round_id"]
                    if bound_phase4_result is not None
                    else "<phase4-round-id>"
                ),
            )
            r = _run_phase(
                "decision",
                cmd,
                dry_run=args.dry_run,
                result_prefix=_DECISION_RESULT_PREFIX,
            )
            results.append(r)

        # 失败停止检查
        last = results[-1]
        if (
            not args.no_stop_on_failure
            and last["status"] in ("failed", "timeout", "error")
        ):
            log.warning("")
            log.warning(
                "%s 失败, 管线中止。后续阶段跳过。", PHASE_LABELS[phase],
            )
            log.warning(
                "  恢复: --start-from %s  |  忽略失败: --no-stop-on-failure",
                phase,
            )
            break

    # ═════════════════════════════════════════════════════════════
    # 汇总
    # ═════════════════════════════════════════════════════════════
    pipeline_elapsed = (
        datetime.now(timezone.utc) - pipeline_started
    ).total_seconds()

    ok = sum(1 for r in results if r["status"] in ("success", "dry_run"))
    partial = sum(1 for r in results if r["status"] == "partial_success")
    fail = sum(1 for r in results if r["status"] in ("failed", "timeout", "error"))
    skipped = len(phases) - len(results)
    ran_phases = {r["phase"] for r in results}

    icon_map = {
        "success": "[OK]",
        "partial_success": "[~~]",
        "dry_run": "[--]",
        "failed": "[!!]",
        "timeout": "[!!]",
        "error": "[!!]",
    }

    print()
    print("=" * 60)
    print(f"  管线汇总 | {pipeline_id} | {pipeline_elapsed:.0f}s")
    print("=" * 60)

    for phase in phases:
        label = PHASE_LABELS[phase]
        matched = [r for r in results if r["phase"] == phase]
        if matched:
            r = matched[0]
            icon = icon_map.get(r["status"], "[??]")
            elapsed_str = ""
            if "elapsed_s" in r:
                m, s = divmod(int(r["elapsed_s"]), 60)
                elapsed_str = f"  {m}m{s:02d}s" if m else f"  {s}s"
            status_hint = "" if r["status"] in ("success", "dry_run") else f"  ({r['status']})"
            print(f"  {icon} {label}{elapsed_str}{status_hint}")
        else:
            print(f"  [--] {label}  (跳过)")

    print()
    parts = [f"{ok} 成功"]
    if partial:
        parts.append(f"{partial} 部分成功")
    parts.extend([f"{fail} 失败", f"{skipped} 跳过"])
    print(f"  结果: {', '.join(parts)}")

    if params_json:
        print(f"  参数: {params_json}")

    if fail > 0:
        # 给出恢复建议
        first_fail = next(r for r in results if r["status"] in ("failed", "timeout", "error"))
        print()
        print(f"  恢复命令: 添加 --start-from {first_fail['phase']} 从失败处重跑")
        _emit_pipeline_result(
            pipeline_id=pipeline_id,
            status="partially_succeeded" if ok > 0 or partial > 0 else "failed",
            results=results,
        )
        return 1

    print()
    if not args.dry_run and "decision" in ran_phases:
        print("  当前状态: 研究与治理建议已生成, 尚未应用到 live 参数")
        print("  下一步: 在 RDP 工作台审查 decision 与 gate；禁止使用已停用的直写 CLI")

    _emit_pipeline_result(
        pipeline_id=pipeline_id,
        status="succeeded_with_warnings" if partial > 0 else "succeeded",
        results=results,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
