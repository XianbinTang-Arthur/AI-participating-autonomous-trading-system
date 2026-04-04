#!/usr/bin/env python3
"""手动记录 Workflow 失败.

用法:
    python scripts/rdp_record_workflow_failure.py \
        --workflow governance_cycle \
        --run-id wf_20260404_070000_abc \
        --task quality_monitor \
        --error "Connection timeout to PostgreSQL" \
        --exit-code 1

    python scripts/rdp_record_workflow_failure.py --list-open

退出码:
    0 = 记录成功 / 列出成功
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
    p = argparse.ArgumentParser(description="记录 Workflow 失败")
    p.add_argument("--workflow", help="Workflow 名称")
    p.add_argument("--run-id", help="运行 ID")
    p.add_argument("--task", help="失败的任务名")
    p.add_argument("--error", help="错误信息")
    p.add_argument("--exit-code", type=int, default=None, help="退出码")
    p.add_argument("--notes", default="", help="备注")
    p.add_argument("--list-open", action="store_true", help="列出所有 open 失败")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    from aats.data_platform.operations.failure_registry import (
        list_open_failures,
        record_failure,
    )

    if args.list_open:
        failures = list_open_failures(ROOT)
        if not failures:
            print("No open failures.")
            return 0
        print(f"Open failures: {len(failures)}")
        print()
        for f in failures:
            print(f"  [{f['failure_id']}]")
            print(f"    Workflow: {f['workflow']} / Task: {f['task_name']}")
            print(f"    Error: {f['error_message'][:120]}")
            print(f"    Recorded: {f['recorded_at']}")
            print(f"    Retries: {f['retry_count']}")
            print()
        return 0

    if not all([args.workflow, args.run_id, args.task, args.error]):
        print("[ERROR] 需要 --workflow, --run-id, --task, --error")
        return 2

    record = record_failure(
        ROOT,
        workflow=args.workflow,
        run_id=args.run_id,
        task_name=args.task,
        error_message=args.error,
        exit_code=args.exit_code,
        notes=args.notes,
    )

    print(f"Failure recorded: {record['failure_id']}")
    print(f"  Workflow: {record['workflow']}")
    print(f"  Task:     {record['task_name']}")
    print(f"  Error:    {record['error_message'][:120]}")
    print(f"  Status:   {record['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
