#!/usr/bin/env python3
"""补跑 Workflow 失败任务.

用法:
    # 补跑单个失败任务
    python scripts/rdp_retry_workflow_failure.py \
        --failure-id fail_governance_cycle_quality_monitor_20260404_070000 \
        --mode task

    # 补跑整个 workflow
    python scripts/rdp_retry_workflow_failure.py \
        --failure-id fail_governance_cycle_quality_monitor_20260404_070000 \
        --mode workflow

    # 预览
    python scripts/rdp_retry_workflow_failure.py \
        --failure-id fail_governance_cycle_quality_monitor_20260404_070000 \
        --dry-run

退出码:
    0 = 补跑成功
    1 = 补跑失败
    2 = 参数错误
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="补跑 Workflow 失败任务")
    p.add_argument("--failure-id", required=True, help="失败记录 ID")
    p.add_argument(
        "--mode",
        choices=["task", "workflow"],
        default="task",
        help="补跑模式: task=单任务, workflow=整个 workflow",
    )
    p.add_argument("--timeout", type=int, default=None, help="超时覆盖（秒）")
    p.add_argument("--dry-run", action="store_true", help="仅预览")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    from aats.data_platform.operations.retry_manager import (
        retry_single_task,
        retry_workflow,
    )

    print(f"Retrying failure: {args.failure_id}")
    print(f"Mode: {args.mode}")
    if args.dry_run:
        print("  (DRY RUN)")
    print()

    if args.mode == "task":
        result = retry_single_task(
            ROOT,
            args.failure_id,
            timeout_override=args.timeout,
            dry_run=args.dry_run,
        )
    else:
        result = retry_workflow(
            ROOT,
            args.failure_id,
            dry_run=args.dry_run,
        )

    if result.get("success"):
        print("[OK] Retry succeeded")
        print(f"  Detail: {result.get('detail', '')[:200]}")
        return 0
    else:
        print("[FAIL] Retry failed")
        print(f"  Detail: {result.get('detail', '')[:200]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
