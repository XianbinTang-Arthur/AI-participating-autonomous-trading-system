#!/usr/bin/env python3
"""RDP 研究管线一键编排.

将 Phase 2 → 3 → 4 → 5 → 6 串联为单一命令,
自动将 Phase 2 产出的 parameter_candidates.json 传递给后续阶段.

Usage:
    # 完整管线 (Phase 2 → 3 → 4 → 5 → 6)
    python scripts/rdp_run_full_pipeline.py --start 2026-03-31 --end 2026-04-02

    # 跳过 Phase 2（已有研究结果, 自动查找最新 params）
    python scripts/rdp_run_full_pipeline.py --start 2026-03-31 --end 2026-04-02 \\
        --start-from phase3

    # 只跑 Phase 2 + Decision（无需日期）
    python scripts/rdp_run_full_pipeline.py --skip-phase3 --skip-phase4

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
import json
import logging
import subprocess
import sys
import time as _time
import traceback as _tb
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_full_pipeline")

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

  # 从 Phase 3 开始 (Phase 2 已完成, 自动查找最新参数)
  %(prog)s --start 2026-03-31 --end 2026-04-02 --start-from phase3

  # 只跑到 Phase 4 (不做治理和决策)
  %(prog)s --start 2026-03-31 --end 2026-04-02 --stop-after phase4

  # Dry run
  %(prog)s --start 2026-03-31 --end 2026-04-02 --dry-run
        """,
    )

    # 通用
    p.add_argument("--start", help="起始日期 YYYY-MM-DD (UTC), Phase 3/4 需要")
    p.add_argument("--end", help="结束日期 YYYY-MM-DD (UTC), Phase 3/4 需要")
    p.add_argument(
        "--lookback-days", type=int, default=None,
        help="自动从今天往回推 N 天作为 --start，--end 设为今天 (UTC)。"
             "优先级低于显式 --start/--end。"
             "示例: --lookback-days 90 等价于 --start <90天前> --end <今天>",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="仅显示将执行的命令, 不实际运行")
    p.add_argument("--ensure-schema", action="store_true",
                   help="首个阶段运行前执行 DB migration")
    p.add_argument("--no-stop-on-failure", action="store_true",
                   help="某阶段失败后继续运行后续阶段")

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
                         "(未指定时自动启用 --replay-only)")
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
             "(默认自动使用 Phase 2 产出或最新历史)",
    )

    return p.parse_args()


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


def _find_latest_params_json() -> Path | None:
    """查找最新��参数文件。

    在 step3_rounds 和 step2_rounds 中分别查找最新文件,
    然后按 round_id 时间戳 (YYYYMMDD_HHMMSS) 比较, 返回最晚的那个。
    Step 3 的 merged 文件更完整, 但如果 Step 2 重跑后更新,
    应返回更新的 Step 2 文件。
    """
    research_root = _PROJECT_ROOT / "artifacts" / "research"
    candidates: list[Path] = []

    # Step 3 merged params
    s3_dir = research_root / "step3_rounds"
    if s3_dir.exists():
        s3_files = sorted(
            s3_dir.glob("*/parameter_candidates_merged.json"), reverse=True,
        )
        if s3_files:
            candidates.append(s3_files[0])

    # Step 2 base params
    s2_dir = research_root / "step2_rounds"
    if s2_dir.exists():
        s2_files = sorted(
            s2_dir.glob("*/parameter_candidates.json"), reverse=True,
        )
        if s2_files:
            candidates.append(s2_files[0])

    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0]

    # 比较 round_id 时间戳: parent.name 格式 YYYYMMDD_HHMMSS_hexsuffix
    # 按 parent.name 倒排, 最新的在前
    best = max(candidates, key=lambda p: p.parent.name)

    # 如果 Step 2 更新但 Step 3 也存在, 给出提示
    if best.parent.parent.name == "step2_rounds" and len(candidates) == 2:
        other = [c for c in candidates if c != best][0]
        log.info(
            "Step 2 参数 (%s) 比 Step 3 (%s) 更新, 使用 Step 2 文件。"
            "如需 Step 3 合并参数请重跑 Step 3。",
            best.parent.name, other.parent.name,
        )

    return best


def _run_phase(
    name: str,
    cmd: list[str],
    *,
    dry_run: bool = False,
) -> dict:
    """执行单个阶段, 返回结果 dict."""
    label = PHASE_LABELS[name]
    cmd_display = " ".join(cmd)

    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"{'=' * 60}")
    log.info("命令: %s", cmd_display)

    if dry_run:
        log.info("[DRY RUN] 跳过执行")
        return {"phase": name, "status": "dry_run", "exit_code": 0}

    started = datetime.now(timezone.utc)
    try:
        result = subprocess.run(cmd, cwd=str(_PROJECT_ROOT), timeout=7200)
        finished = datetime.now(timezone.utc)
        elapsed = (finished - started).total_seconds()

        if result.returncode == 0:
            log.info("%s 完成 (%.0fs)", label, elapsed)
            return {
                "phase": name, "status": "success",
                "exit_code": 0, "elapsed_s": elapsed,
            }
        elif result.returncode == 2:
            # exit code 2 = 部分成功 (某些 batch 通过, 仍有可用产物)
            log.warning(
                "%s 部分成功 (exit=2, %.0fs) — 部分 batch 失败但仍有产物",
                label, elapsed,
            )
            return {
                "phase": name, "status": "partial_success",
                "exit_code": 2, "elapsed_s": elapsed,
            }
        else:
            log.warning(
                "%s 失败 (exit=%d, %.0fs)",
                label, result.returncode, elapsed,
            )
            return {
                "phase": name, "status": "failed",
                "exit_code": result.returncode, "elapsed_s": elapsed,
            }

    except subprocess.TimeoutExpired:
        log.error("%s 超时 (>7200s)", label)
        return {"phase": name, "status": "timeout", "exit_code": -1}
    except Exception as exc:
        log.error("%s 异常: %s", label, exc)
        return {
            "phase": name, "status": "error",
            "exit_code": -1, "error": str(exc),
        }


# =========================================================================
# 命令构建
# =========================================================================
def _build_phase2_cmd(args: argparse.Namespace, *, ensure: bool) -> list[str]:
    cmd = [sys.executable, str(_SCRIPT_DIR / "rdp_run_step2_research.py")]
    if ensure:
        cmd.append("--ensure-schema")
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
    ]
    if ensure:
        cmd.append("--ensure-schema")
    if args.live_db_url:
        cmd.extend(["--live-db-url", args.live_db_url])
    if args.replay_only or not args.live_db_url:
        cmd.append("--replay-only")
        if not args.replay_only and not args.live_db_url:
            log.info("Phase 3: 未指定 --live-db-url, 自动启用 --replay-only 模式")
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


def _build_decision_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [sys.executable, str(_SCRIPT_DIR / "rdp_run_decision_round.py")]
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

    # Phase 3/4 需要 --start 和 --end
    needs_dates = any(p in phases for p in ("phase3", "phase4"))
    if needs_dates and (not args.start or not args.end):
        print("错误: Phase 3/4 需要 --start 和 --end 参数（或使用 --lookback-days）。")
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
    params_json: str | None = args.params_json
    schema_done = False

    # 如果跳过 Phase 2 和 Step 3, 尝试自动查找已有的 params-json
    if "phase2" not in phases and "step3" not in phases and not params_json:
        found = _find_latest_params_json()
        if found:
            params_json = str(found)
            log.info("自动检测到已有参数文件: %s", params_json)
        else:
            log.info("未找到历史参数文件, Phase 3/4 将使用默认参数")

    # 追踪 Step 2 最新 round dir (供 Step 3 使用)
    step2_latest_round_dir: str | None = None

    for phase in phases:
        # ── Phase 2 (Step 2) ──
        if phase == "phase2":
            ensure = args.ensure_schema and not schema_done
            cmd = _build_phase2_cmd(args, ensure=ensure)
            if ensure:
                schema_done = True

            r = _run_phase("phase2", cmd, dry_run=args.dry_run)
            results.append(r)

            # Phase 2 有产物时 (success 或 partial_success), 记录目录并更新 params
            if not args.dry_run and r["status"] in ("success", "partial_success"):
                s2_dir = _PROJECT_ROOT / "artifacts" / "research" / "step2_rounds"
                if s2_dir.exists():
                    latest = sorted(
                        [d for d in s2_dir.iterdir() if d.is_dir()],
                        reverse=True,
                    )
                    if latest:
                        step2_latest_round_dir = str(latest[0])
                        log.info("Step 2 round dir: %s", step2_latest_round_dir)

                if not args.params_json:
                    found = _find_latest_params_json()
                    if found:
                        params_json = str(found)
                        log.info("Phase 2 参数文件: %s", params_json)

        # ── Step 3 (扩展参数扫描) ──
        elif phase == "step3":
            ensure = args.ensure_schema and not schema_done
            cmd = _build_step3_cmd(
                args, ensure=ensure,
                step2_round_dir=step2_latest_round_dir,
            )
            if ensure:
                schema_done = True

            r = _run_phase("step3", cmd, dry_run=args.dry_run)
            results.append(r)

            # Step 3 有产物时, 用合并参数覆盖 params_json
            if (
                not args.dry_run
                and r["status"] in ("success", "partial_success")
                and not args.params_json
            ):
                found = _find_latest_params_json()
                if found:
                    params_json = str(found)
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
                    import_result = auto_import_latest_candidates(_PROJECT_ROOT)
                    elapsed = _time.monotonic() - t0
                    status = import_result["status"]
                    log.info(
                        "参数导入结果: %s (导入 %d, 废弃 %d, %.1fs)",
                        status,
                        import_result["imported_count"],
                        import_result["deprecated_count"],
                        elapsed,
                    )
                    r = {
                        "phase": "import_candidates",
                        "status": "success" if status in ("imported", "already_imported", "parse_empty") else "failed",
                        "duration_s": round(elapsed, 2),
                        "detail": import_result,
                    }
                except Exception as exc:
                    elapsed = _time.monotonic() - t0
                    log.error(
                        "参数导入失败 (%.1fs): %s\n%s",
                        elapsed, exc, _tb.format_exc(),
                    )
                    r = {
                        "phase": "import_candidates",
                        "status": "error",
                        "duration_s": round(elapsed, 2),
                        "detail": str(exc),
                    }
            else:
                log.info("[DRY-RUN] import_candidates: 自动导入 Step 3 参数到 registry")
                r = {"phase": "import_candidates", "status": "dry_run", "duration_s": 0}
            results.append(r)

        # ── Phase 3 ──
        elif phase == "phase3":
            ensure = args.ensure_schema and not schema_done
            cmd = _build_phase3_cmd(
                args, ensure=ensure, params_json=params_json,
            )
            if ensure:
                schema_done = True

            r = _run_phase("phase3", cmd, dry_run=args.dry_run)
            results.append(r)

        # ── Phase 4 ──
        elif phase == "phase4":
            ensure = args.ensure_schema and not schema_done
            cmd = _build_phase4_cmd(
                args, ensure=ensure, params_json=params_json,
            )
            if ensure:
                schema_done = True

            r = _run_phase("phase4", cmd, dry_run=args.dry_run)
            results.append(r)

        # ── Phase 5 (Governance) ──
        elif phase == "phase5":
            cmd = _build_phase5_cmd()
            r = _run_phase("phase5", cmd, dry_run=args.dry_run)
            results.append(r)

        # ── Decision ──
        elif phase == "decision":
            cmd = _build_decision_cmd(args)
            r = _run_phase("decision", cmd, dry_run=args.dry_run)
            results.append(r)

        # 失败停止检查
        last = results[-1]
        if (
            not args.no_stop_on_failure
            and last["status"] in ("failed", "timeout", "error")
        ):
            curr_idx = PHASE_ORDER.index(phase)
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
        return 1

    print()
    if not args.dry_run and "decision" in ran_phases:
        print("  下一步: 查看 decision 报告, 如需应用参数:")
        print("    python scripts/approve_recommendation_and_apply.py --rec-id <ID>")

    return 0


if __name__ == "__main__":
    sys.exit(main())
