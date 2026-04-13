#!/usr/bin/env python3
"""Recommendation 审批并应用 active parameter set.

阶段 D 交付物：完整的 recommendation -> approval -> apply 流程。

流程:
    1. 审批指定 recommendation（draft -> approved）
    2. 如果是 parameter_upgrade 类型，自动生成 active parameter set
    3. 记录操作审计日志
    4. 输出后续重启/reload 指令

用法:
    # 查看待审批的 recommendations
    python scripts/approve_recommendation_and_apply.py --action list

    # 审批指定 recommendation
    python scripts/approve_recommendation_and_apply.py --action approve --rec-id rec_20260404_153614_abc123

    # 审批并自动应用关联的参数
    python scripts/approve_recommendation_and_apply.py --action approve-and-apply --rec-id rec_20260404_153614_abc123

    # 拒绝指定 recommendation
    python scripts/approve_recommendation_and_apply.py --action reject --rec-id rec_20260404_153614_abc123

    # 查看审批历史
    python scripts/approve_recommendation_and_apply.py --action history

退出码:
    0 = 成功
    1 = 错误
    2 = 无可操作内容
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── 路径常量 ───────────────────────────────────────────────────────

DECISION_SYSTEM_DIR = "artifacts/decision_system"
GOVERNANCE_DIR = "artifacts/governance"
APPROVAL_LOG_DIR = "artifacts/governance/approval_logs"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Recommendation 审批与参数应用",
    )
    p.add_argument(
        "--action",
        choices=("list", "approve", "approve-and-apply", "reject", "history"),
        required=True,
    )
    p.add_argument(
        "--rec-id",
        default=None,
        help="要操作的 recommendation_id",
    )
    p.add_argument(
        "--reason",
        default=None,
        help="审批/拒绝理由",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预览，不实际修改",
    )
    p.add_argument(
        "--force-warn",
        action="store_true",
        help="gate 返回 warn 时仍继续 apply（默认 warn 也会 apply，但会显式提示）",
    )
    p.add_argument(
        "--skip-gate",
        action="store_true",
        help="跳过 pre-apply gate（仅限 dev 环境调试用，生产环境禁止）",
    )
    return p.parse_args()


# ── 工具函数 ───────────────────────────────────────────────────────


def _load_rec_registry(project_root: Path) -> tuple[dict, Path]:
    from aats.data_platform.decision_system.recommendation_registry import (
        load_recommendation_registry,
    )
    path = project_root / DECISION_SYSTEM_DIR / "recommendation_registry.json"
    return load_recommendation_registry(path), path


def _save_rec_registry(registry: dict, path: Path) -> None:
    from aats.data_platform.decision_system.recommendation_registry import (
        save_recommendation_registry,
    )
    save_recommendation_registry(registry, path)


def _write_approval_log(
    project_root: Path,
    *,
    action: str,
    recommendation_id: str,
    recommendation_type: str | None = None,
    family: str | None = None,
    timeframe: str | None = None,
    parameter_set_applied: str | None = None,
    reason: str | None = None,
) -> None:
    """记录审批操作日志."""
    log_dir = project_root / APPROVAL_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / "recommendation_approval_history.jsonl"
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "recommendation_id": recommendation_id,
        "recommendation_type": recommendation_type,
        "family": family,
        "timeframe": timeframe,
        "parameter_set_applied": parameter_set_applied,
        "reason": reason,
    }

    with log_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


# ── 动作实现 ───────────────────────────────────────────────────────


def action_list(project_root: Path) -> int:
    """列出待审批的 recommendations."""
    registry, path = _load_rec_registry(project_root)
    recs = registry.get("recommendations", [])

    print(f"Recommendation Registry: {path}")
    print(f"Version: {registry.get('version', 'N/A')}")
    print(f"Total: {len(recs)}")
    print()

    # 按状态分组
    by_status: dict[str, list] = {}
    for r in recs:
        s = r.get("status", "unknown")
        by_status.setdefault(s, []).append(r)

    for status in ("draft", "approved", "rejected", "superseded"):
        group = by_status.get(status, [])
        if not group:
            continue
        print(f"  [{status.upper()}] ({len(group)} items)")
        for r in group:
            print(f"    {r['recommendation_id']}")
            print(f"      type: {r.get('recommendation_type')}")
            print(f"      family: {r.get('family')}/{r.get('timeframe')}")
            print(f"      confidence: {r.get('confidence')}")
            print(f"      reason: {r.get('reason', '')[:80]}")
            if r.get("target_parameter_set_id"):
                print(f"      target_ps: {r['target_parameter_set_id']}")
            print(f"      created: {r.get('created_at')}")
            print()

    # 操作提示
    draft_count = len(by_status.get("draft", []))
    if draft_count > 0:
        print(f"有 {draft_count} 个待审批 recommendation。")
        print("使用 --action approve --rec-id <id> 审批")
        print("使用 --action approve-and-apply --rec-id <id> 审批并应用参数")

    return 0


def action_approve(
    project_root: Path,
    args: argparse.Namespace,
    *,
    also_apply: bool = False,
) -> int:
    """审批 recommendation."""
    if not args.rec_id:
        print("[ERROR] --rec-id 必须指定")
        return 1

    registry, reg_path = _load_rec_registry(project_root)

    # 查找 recommendation
    target = None
    for r in registry.get("recommendations", []):
        if r["recommendation_id"] == args.rec_id:
            target = r
            break

    if target is None:
        print(f"[ERROR] 未找到 recommendation: {args.rec_id}")
        return 1

    if target["status"] != "draft":
        print(f"[WARNING] recommendation 状态为 '{target['status']}'（非 draft）")

    # 展示详情
    print("Recommendation 详情:")
    print(f"  ID: {target['recommendation_id']}")
    print(f"  Type: {target.get('recommendation_type')}")
    print(f"  Family: {target.get('family')}/{target.get('timeframe')}")
    print(f"  Confidence: {target.get('confidence')}")
    print(f"  Reason: {target.get('reason')}")
    if target.get("target_parameter_set_id"):
        print(f"  Target PS: {target['target_parameter_set_id']}")
    print()

    if args.dry_run:
        print("[DRY RUN] 将会:")
        print(f"  1. 将 recommendation 状态改为 approved")
        if also_apply and target.get("target_parameter_set_id"):
            print(f"  2. 运行 pre-apply gate")
            print(f"  3. gate 通过后应用 parameter set {target['target_parameter_set_id']} 为 active")
        return 0

    # ── Pre-Apply Gate（approve-and-apply 时必须运行）────────
    gate_result = None
    if also_apply and target.get("target_parameter_set_id"):
        if args.skip_gate:
            # Fix P2-10：--skip-gate 仅允许在 dev 环境使用，staging/prod 环境禁止
            from aats.data_platform.operations.environment_guard import (
                get_current_environment,
                get_policy,
            )
            _env = get_current_environment()
            if get_policy(_env).get("require_gate_pass", False):
                print(f"[ERROR] --skip-gate 在 {_env} 环境被禁止（require_gate_pass=True）")
                return 1
            print("[WARNING] --skip-gate: 跳过 pre-apply gate（仅限调试）")
            gate_result = {
                "gate_run_id": "skipped",
                "gate_status": "pass",
                "allow_apply": True,
                "warnings": [],
                "blocking_reasons": [],
            }
        else:
            from aats.data_platform.production_workflow.pre_apply_gate import (
                run_pre_apply_gate,
            )
            print("运行 Pre-Apply Gate ...")
            gate_result = run_pre_apply_gate(project_root, args.rec_id)
            gate_status = gate_result.get("gate_status", "unknown")
            print(f"  Gate Status: {gate_status.upper()}")
            print(f"  Checks: {gate_result.get('passed_checks', 0)}/{gate_result.get('total_checks', 0)} passed")

            if gate_result.get("blocking_reasons"):
                for reason in gate_result["blocking_reasons"]:
                    print(f"  [BLOCK] {reason}")

            if gate_result.get("warnings"):
                for warning in gate_result["warnings"]:
                    print(f"  [WARN]  {warning}")

            print()

            if gate_status == "block":
                print("[REJECTED] Gate 返回 BLOCK，拒绝 apply。")
                print("请先解决 blocking reasons 后重试。")
                # 仍然记录 gate 结果到日志
                _write_approval_log(
                    project_root,
                    action="approve-and-apply-blocked",
                    recommendation_id=args.rec_id,
                    recommendation_type=target.get("recommendation_type"),
                    family=target.get("family"),
                    timeframe=target.get("timeframe"),
                    reason=f"gate_blocked: {'; '.join(gate_result.get('blocking_reasons', []))}",
                )
                return 1

            if gate_status == "warn" and not args.force_warn:
                print("[INFO] Gate 返回 WARN。apply 将继续，但请注意以上 warnings。")
                print("  如需跳过此提示，使用 --force-warn")

    # 执行审批
    target["status"] = "approved"
    target["approved_at"] = datetime.now(timezone.utc).isoformat()
    target["approval_reason"] = args.reason or "operator_approved"

    _save_rec_registry(registry, reg_path)
    print(f"[OK] Recommendation {args.rec_id} 已审批为 approved")

    # 记录审批日志
    _write_approval_log(
        project_root,
        action="approve",
        recommendation_id=args.rec_id,
        recommendation_type=target.get("recommendation_type"),
        family=target.get("family"),
        timeframe=target.get("timeframe"),
        reason=args.reason,
    )

    # 如果需要同时应用参数
    parameter_applied = None
    if also_apply and target.get("target_parameter_set_id"):
        parameter_applied = _apply_parameter_from_recommendation(
            project_root, target,
            gate_result=gate_result,
        )

    # 输出后续指令
    print()
    print("后续操作:")
    if parameter_applied:
        print(f"  [INFO] 已应用参数: {parameter_applied}")
        if gate_result:
            print(f"  [INFO] Gate Run ID: {gate_result.get('gate_run_id', 'N/A')}")
        print("  如果主交易系统正在运行，需要重启或 reload 使新参数生效:")
        print("    方式 1: 重启 API gateway")
        print("    方式 2: 调用 POST /system/rebaseline")
    else:
        rec_type = target.get("recommendation_type", "")
        if rec_type == "pause":
            print(f"  [建议] 应暂停 {target.get('family')}/{target.get('timeframe')} 的实盘运行")
        elif rec_type == "lower_priority":
            print(f"  [建议] 应降低 {target.get('family')}/{target.get('timeframe')} 的预算/优先级")
        elif rec_type == "require_review":
            print(f"  [建议] 需要进一步人工审查 {target.get('family')}/{target.get('timeframe')}")

    return 0


def _apply_parameter_from_recommendation(
    project_root: Path,
    recommendation: dict,
    *,
    gate_result: dict | None = None,
) -> str | None:
    """从审批通过的 recommendation 应用参数.

    gate_result 如果提供，会将 gate_run_id 和 gate_status 一并
    记录到 approval log 和 apply history 中。
    """
    from aats.bootstrap.active_parameters import write_active_parameter_set
    from aats.data_platform.governance.parameter_registry import load_registry

    ps_id = recommendation.get("target_parameter_set_id")
    if not ps_id:
        return None

    # 从 registry 查找参数值
    reg_path = project_root / GOVERNANCE_DIR / "current_parameter_registry.json"
    registry = load_registry(reg_path)

    target_ps = None
    for ps in registry.get("parameter_sets", []):
        if ps["parameter_set_id"] == ps_id:
            target_ps = ps
            break

    if target_ps is None:
        print(f"  [WARNING] 未在 registry 中找到 parameter set {ps_id}")
        return None

    path = write_active_parameter_set(
        family=target_ps["family"],
        timeframe=target_ps["timeframe"],
        parameter_set_id=target_ps["parameter_set_id"],
        values=target_ps["values"],
        source_round_id=target_ps.get("source_round_id"),
        approval_recommendation_id=recommendation["recommendation_id"],
        applied_by="approve_recommendation_and_apply.py",
        project_root=project_root,
    )

    # 提取 gate 信息
    gate_run_id = gate_result.get("gate_run_id") if gate_result else None
    gate_status = gate_result.get("gate_status") if gate_result else None

    combo_key = f"{target_ps['family']}_{target_ps['timeframe'].lower()}"
    _write_approval_log(
        project_root,
        action="approve-and-apply",
        recommendation_id=recommendation["recommendation_id"],
        recommendation_type=recommendation.get("recommendation_type"),
        family=target_ps["family"],
        timeframe=target_ps["timeframe"],
        parameter_set_applied=ps_id,
        reason=f"gate={gate_status or 'not_run'}, gate_run_id={gate_run_id or 'N/A'}",
    )

    # 同步记录到 parameter_apply_history（如果模块可用）
    try:
        from aats.data_platform.decision_system.active_parameter_apply import (
            load_apply_history,
            save_apply_history,
        )
        history = load_apply_history(project_root)
        from uuid import uuid4
        op = {
            "operation_id": f"op_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}",
            "operation_type": "apply",
            "family": target_ps["family"],
            "timeframe": target_ps["timeframe"],
            "from_parameter_set_id": None,
            "to_parameter_set_id": ps_id,
            "recommendation_id": recommendation["recommendation_id"],
            "gate_run_id": gate_run_id,
            "gate_status": gate_status,
            "actor": "approve_recommendation_and_apply.py",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "notes": f"via approve-and-apply, gate={gate_status or 'not_run'}",
        }
        history.setdefault("operations", []).append(op)
        save_apply_history(history, project_root)
    except Exception as exc:
        print(f"  [WARNING] 未能同步 apply history: {exc}")

    return str(path)


def action_reject(project_root: Path, args: argparse.Namespace) -> int:
    """拒绝 recommendation."""
    if not args.rec_id:
        print("[ERROR] --rec-id 必须指定")
        return 1

    registry, reg_path = _load_rec_registry(project_root)

    target = None
    for r in registry.get("recommendations", []):
        if r["recommendation_id"] == args.rec_id:
            target = r
            break

    if target is None:
        print(f"[ERROR] 未找到 recommendation: {args.rec_id}")
        return 1

    if args.dry_run:
        print(f"[DRY RUN] 将拒绝: {args.rec_id}")
        return 0

    target["status"] = "rejected"
    target["rejected_at"] = datetime.now(timezone.utc).isoformat()
    target["rejection_reason"] = args.reason or "operator_rejected"

    _save_rec_registry(registry, reg_path)
    print(f"[OK] Recommendation {args.rec_id} 已拒绝")

    _write_approval_log(
        project_root,
        action="reject",
        recommendation_id=args.rec_id,
        recommendation_type=target.get("recommendation_type"),
        family=target.get("family"),
        timeframe=target.get("timeframe"),
        reason=args.reason,
    )

    return 0


def action_history(project_root: Path) -> int:
    """查看审批历史."""
    log_file = project_root / APPROVAL_LOG_DIR / "recommendation_approval_history.jsonl"

    if not log_file.exists():
        print("暂无审批历史记录")
        return 0

    print("审批历史:")
    print("=" * 60)

    with log_file.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                ts = entry.get("timestamp", "?")
                action = entry.get("action", "?")
                rec_id = entry.get("recommendation_id", "?")
                combo = f"{entry.get('family', '?')}/{entry.get('timeframe', '?')}"
                ps = entry.get("parameter_set_applied", "")

                print(f"  [{ts}] {action}")
                print(f"    recommendation: {rec_id}")
                print(f"    combo: {combo}")
                if ps:
                    print(f"    parameter_set_applied: {ps}")
                if entry.get("reason"):
                    print(f"    reason: {entry['reason']}")
                print()
            except json.JSONDecodeError:
                continue

    return 0


def main() -> int:
    args = parse_args()
    project_root = ROOT

    if args.action == "list":
        return action_list(project_root)
    elif args.action == "approve":
        return action_approve(project_root, args, also_apply=False)
    elif args.action == "approve-and-apply":
        return action_approve(project_root, args, also_apply=True)
    elif args.action == "reject":
        return action_reject(project_root, args)
    elif args.action == "history":
        return action_history(project_root)
    else:
        print(f"[ERROR] 未知操作: {args.action}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
