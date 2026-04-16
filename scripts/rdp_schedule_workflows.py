#!/usr/bin/env python3
"""Evaluate workflow schedules and enqueue due workflows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="调度到期的 RDP workflows")
    parser.add_argument("--actor", default="scheduler")
    parser.add_argument("--dry-run", action="store_true", help="只评估，不写入队列或状态")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    from aats.data_platform.operations.workflow_scheduler import enqueue_due_workflows

    result = enqueue_due_workflows(
        ROOT,
        actor=args.actor,
        dry_run=args.dry_run,
        save_state=not args.dry_run,
        initialize_if_missing=True,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Workflow Scheduler")
        print(f"  At:          {result.get('scheduler_at')}")
        print(f"  Dry Run:     {result.get('dry_run')}")
        print(f"  Initialized: {result.get('initialized')}")
        print(f"  Enqueued:    {len(result.get('enqueued', []))}")
        print(f"  Skipped:     {len(result.get('skipped', []))}")
        print(f"  Errors:      {len(result.get('errors', []))}")
        print()
        if result.get("enqueued"):
            print("Enqueued:")
            for item in result["enqueued"]:
                print(f"  - {item['workflow']} -> {item.get('task_id')} ({item['slot']})")
        if result.get("skipped"):
            print("Skipped:")
            for item in result["skipped"]:
                print(f"  - {item.get('workflow')}: {item.get('reason')}")
        if result.get("errors"):
            print("Errors:")
            for item in result["errors"]:
                print(f"  - {item['workflow']}: {item['error']}")

    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
