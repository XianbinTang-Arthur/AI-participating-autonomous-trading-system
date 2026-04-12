#!/usr/bin/env python3
"""Recommendation 审批脚本.

工作包 B 交付物：将 recommendation 从"结果文件"升级为"受控可审批对象"。

支持动作:
  approve    — draft → approved
  reject     — draft → rejected
  supersede  — 任意 → superseded（旧建议被新建议替代）

用法:
    # 审批
    python scripts/rdp_approve_recommendation.py \
        --recommendation-id rec_xxx --action approve --actor operator_name

    # 拒绝
    python scripts/rdp_approve_recommendation.py \
        --recommendation-id rec_xxx --action reject --actor operator_name --notes "风险过高"

    # 替代
    python scripts/rdp_approve_recommendation.py \
        --recommendation-id rec_old --action supersede --notes "被 rec_new 替代"

    # 预览
    python scripts/rdp_approve_recommendation.py \
        --recommendation-id rec_xxx --action approve --dry-run

退出码:
    0 = 成功
    1 = 错误
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


DECISION_SYSTEM_DIR = "artifacts/decision_system"
APPROVAL_LOG_DIR = "artifacts/governance/approval_logs"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Recommendation 审批")
    p.add_argument(
        "--recommendation-id",
        required=True,
        help="要操作的 recommendation_id",
    )
    p.add_argument(
        "--action",
        choices=("approve", "reject", "supersede"),
        required=True,
    )
    p.add_argument(
        "--actor",
        default="operator",
        help="操作人（默认 operator）",
    )
    p.add_argument(
        "--notes",
        default=None,
        help="审批备注",
    )
    p.add_argument(
        "--superseded-by-id",
        default=None,
        help="替代此 recommendation 的新 recommendation_id（用于 supersede）",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预览，不实际修改",
    )
    return p.parse_args()


def _write_approval_log(
    project_root: Path,
    *,
    action: str,
    recommendation_id: str,
    actor: str,
    recommendation_type: str | None = None,
    family: str | None = None,
    timeframe: str | None = None,
    notes: str | None = None,
    superseded_by_id: str | None = None,
) -> None:
    """记录审批操作到 JSONL 审计日志."""
    log_dir = project_root / APPROVAL_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / "recommendation_approval_history.jsonl"
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "recommendation_id": recommendation_id,
        "actor": actor,
        "recommendation_type": recommendation_type,
        "family": family,
        "timeframe": timeframe,
        "notes": notes,
    }
    if superseded_by_id:
        entry["superseded_by_recommendation_id"] = superseded_by_id

    with log_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def _show_recommendation(rec: dict) -> None:
    """打印 recommendation 详情."""
    print("Recommendation 详情:")
    print(f"  ID:         {rec.get('recommendation_id')}")
    print(f"  Type:       {rec.get('recommendation_type')}")
    print(f"  Family:     {rec.get('family')}/{rec.get('timeframe')}")
    print(f"  Confidence: {rec.get('confidence')}")
    print(f"  Status:     {rec.get('status')}")
    print(f"  Reason:     {rec.get('reason', '')[:120]}")
    if rec.get("target_parameter_set_id"):
        print(f"  Target PS:  {rec['target_parameter_set_id']}")
    print(f"  Created:    {rec.get('created_at')}")
    if rec.get("approved_at"):
        print(f"  Approved:   {rec['approved_at']} by {rec.get('approved_by')}")
    if rec.get("rejected_at"):
        print(f"  Rejected:   {rec['rejected_at']} by {rec.get('rejected_by')}")
    if rec.get("superseded_at"):
        print(f"  Superseded: {rec['superseded_at']} by {rec.get('superseded_by')}")
    if rec.get("review_notes") or rec.get("approval_notes"):
        print(f"  Notes:      {rec.get('review_notes') or rec.get('approval_notes')}")
    print()


def main() -> int:
    args = parse_args()
    project_root = ROOT

    from aats.data_platform.decision_system.recommendation_registry import (
        approve_recommendation,
        find_recommendation,
        load_recommendation_registry,
        reject_recommendation,
        save_recommendation_registry,
        supersede_recommendation,
    )

    reg_path = project_root / DECISION_SYSTEM_DIR / "recommendation_registry.json"
    registry = load_recommendation_registry(reg_path)

    # 查找目标
    rec = find_recommendation(registry, args.recommendation_id)
    if rec is None:
        print(f"[ERROR] 未找到 recommendation: {args.recommendation_id}")
        return 1

    _show_recommendation(rec)

    if args.dry_run:
        print(f"[DRY RUN] 将执行 {args.action}（不实际修改）")
        return 0

    # 执行状态流转
    if args.action == "approve":
        result = approve_recommendation(
            registry, args.recommendation_id,
            approved_by=args.actor,
            notes=args.notes,
        )
    elif args.action == "reject":
        result = reject_recommendation(
            registry, args.recommendation_id,
            rejected_by=args.actor,
            notes=args.notes,
        )
    elif args.action == "supersede":
        result = supersede_recommendation(
            registry, args.recommendation_id,
            superseded_by_id=args.superseded_by_id,
            actor=args.actor,
            notes=args.notes,
        )
    else:
        print(f"[ERROR] 未知动作: {args.action}")
        return 1

    if result is None:
        print(f"[ERROR] {args.action} 操作失败")
        return 1

    # 保存
    save_recommendation_registry(registry, reg_path)
    print(f"[OK] Recommendation {args.recommendation_id} -> {args.action}d")
    print()
    _show_recommendation(result)

    # 审计日志
    _write_approval_log(
        project_root,
        action=args.action,
        recommendation_id=args.recommendation_id,
        actor=args.actor,
        recommendation_type=rec.get("recommendation_type"),
        family=rec.get("family"),
        timeframe=rec.get("timeframe"),
        notes=args.notes,
        superseded_by_id=args.superseded_by_id,
    )
    print("[OK] 审计日志已记录")

    return 0


if __name__ == "__main__":
    sys.exit(main())
