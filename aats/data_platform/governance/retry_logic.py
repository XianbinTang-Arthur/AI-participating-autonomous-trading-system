"""失败 Round 重跑逻辑.

为 failed / partial_success 的 round 生成重跑计划，
包含命令行和说明。
"""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# ── 各 phase 的 runner 脚本 ──────────────────────────────────────────

PHASE_RUNNERS: dict[str, dict[str, str]] = {
    "phase3": {
        "round_script": "scripts/rdp_run_phase3_round.py",
        "single_script": "scripts/rdp_run_live_attribution.py",
        "description": "Phase 3 归因 round",
    },
    "phase4": {
        "round_script": "scripts/rdp_run_phase4_round.py",
        "single_script": "scripts/rdp_run_execution_realism.py",
        "description": "Phase 4 执行代理评估 round",
    },
}


# ── 重跑计划生成 ─────────────────────────────────────────────────────


def generate_retry_plan(
    round_dir: pathlib.Path,
    *,
    phase: str,
) -> dict[str, Any]:
    """为一个 round 生成重跑计划.

    Parameters
    ----------
    round_dir : pathlib.Path
        round 目录路径
    phase : str
        "phase3" | "phase4"

    Returns
    -------
    dict  重跑计划
    """
    plan: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "round_dir": str(round_dir),
        "phase": phase,
        "original_round_id": None,
        "original_status": None,
        "failed_combos": [],
        "retry_commands": [],
        "full_rerun_command": None,
        "notes": [],
    }

    # 读取 manifest
    manifest_path = round_dir / "round_manifest.json"
    if not manifest_path.exists():
        plan["notes"].append(f"round_manifest.json 不存在: {round_dir}")
        return plan

    try:
        with manifest_path.open(encoding="utf-8") as f:
            manifest = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        plan["notes"].append(f"无法解析 manifest: {exc}")
        return plan

    plan["original_round_id"] = manifest.get("round_id")

    # 提取窗口
    window = manifest.get("window", {})
    start = window.get("start")
    end = window.get("end")

    if not start or not end:
        plan["notes"].append("manifest 中缺少 window.start / window.end")

    # 提取 combos 状态
    combos = manifest.get("combos", [])
    combo_statuses: dict[str, str] = {}
    for c in combos:
        key = c.get("key", "unknown")
        status = c.get("status", "unknown")
        combo_statuses[key] = status

    # 确定整体状态
    all_statuses = set(combo_statuses.values())
    if all_statuses == {"succeeded"}:
        plan["original_status"] = "succeeded"
        plan["notes"].append("所有 combo 均成功，无需重跑")
        return plan
    elif "failed" not in all_statuses and "partial_success" not in all_statuses:
        plan["original_status"] = "succeeded"
        plan["notes"].append("无失败 combo")
        return plan
    elif "failed" in all_statuses and all_statuses - {"failed"} == set():
        plan["original_status"] = "failed"
    else:
        plan["original_status"] = "partial_success"

    # 找出失败 combo
    failed_combos = [
        {"key": k, "status": s}
        for k, s in combo_statuses.items()
        if s in ("failed", "partial_success")
    ]
    plan["failed_combos"] = failed_combos

    # 生成重跑命令
    runner_info = PHASE_RUNNERS.get(phase, {})
    round_script = runner_info.get("round_script")
    single_script = runner_info.get("single_script")

    # 提取额外参数
    extra_args: list[str] = []
    if phase == "phase4":
        taker_fee = manifest.get("taker_fee_bps")
        if taker_fee:
            extra_args.extend(["--taker-fee-bps", str(taker_fee)])
    if manifest.get("replay_only"):
        extra_args.append("--replay-only")

    # 1. 整轮重跑命令
    if round_script and start and end:
        cmd_parts = [
            "python", round_script,
            "--start", start,
            "--end", end,
        ]
        cmd_parts.extend(extra_args)
        plan["full_rerun_command"] = " ".join(cmd_parts)

    # 2. 单 combo 重跑命令
    if single_script and start and end:
        for fc in failed_combos:
            key = fc["key"]
            parts = key.rsplit("_", 1)
            if len(parts) == 2:
                family, timeframe = parts
            else:
                continue

            cmd_parts = [
                "python", single_script,
                "--family", family,
                "--timeframe", timeframe,
                "--symbol", manifest.get("symbol", "BTC-USDT-SWAP"),
                "--start", start,
                "--end", end,
            ]
            cmd_parts.extend(extra_args)

            plan["retry_commands"].append({
                "combo_key": key,
                "original_status": fc["status"],
                "command": " ".join(cmd_parts),
            })

    # 建议
    if len(failed_combos) == len(combos):
        plan["notes"].append("所有 combo 均失败，建议检查数据源后整轮重跑")
    else:
        plan["notes"].append(
            f"{len(failed_combos)}/{len(combos)} combo 失败，"
            "可选择单 combo 重跑或整轮重跑"
        )

    return plan


def find_retryable_rounds(
    project_root: pathlib.Path,
    *,
    phase: str | None = None,
) -> list[dict[str, Any]]:
    """扫描所有 round，找出可重跑的（failed / partial_success）."""
    from .round_status import PHASE_ARTIFACT_ROOTS

    retryable: list[dict[str, Any]] = []

    for p, rel_path in PHASE_ARTIFACT_ROOTS.items():
        if phase and p != phase:
            continue
        root = project_root / rel_path
        if not root.exists():
            continue

        for subdir in sorted(root.iterdir()):
            if not subdir.is_dir():
                continue
            manifest_file = subdir / "round_manifest.json"
            if not manifest_file.exists():
                continue

            try:
                with manifest_file.open(encoding="utf-8") as f:
                    manifest = json.load(f)
            except Exception:
                continue

            # 检查 combos 状态
            combos = manifest.get("combos", [])
            has_failure = any(
                c.get("status") in ("failed", "partial_success")
                for c in combos
            )
            if has_failure:
                retryable.append({
                    "round_id": manifest.get("round_id", subdir.name),
                    "phase": p,
                    "path": str(subdir),
                    "combo_statuses": {
                        c.get("key", "?"): c.get("status", "?")
                        for c in combos
                    },
                })

    return retryable
