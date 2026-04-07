#!/usr/bin/env python3
"""统一 Workflow 调度入口.

工作包 A: 根据 --workflow 运行指定类型的流程，统一日志和退出码。

用法:
    # 列出可用 workflows
    python scripts/rdp_run_scheduled_workflow.py --list

    # 运行指定 workflow
    python scripts/rdp_run_scheduled_workflow.py --workflow governance_cycle

    # 预览（不实际执行）
    python scripts/rdp_run_scheduled_workflow.py --workflow data_maintenance --dry-run

    # 失败后继续执行后续任务
    python scripts/rdp_run_scheduled_workflow.py --workflow research_cycle --no-stop-on-failure

退出码:
    0 = 全部成功
    1 = 有任务失败
    2 = 配置或参数错误
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Force UTF-8 stdout/stderr on Windows so Chinese workflow descriptions
# (loaded from configs/rdp_workflows/*.json) render correctly instead
# of being mangled by the GBK console codec.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="RDP Workflow 统一调度入口")
    p.add_argument("--workflow", default=None, help="要运行的 workflow 名称")
    p.add_argument("--list", action="store_true", help="列出所有可用 workflow")
    p.add_argument("--dry-run", action="store_true", help="仅预览，不实际执行")
    p.add_argument("--no-stop-on-failure", action="store_true", help="失败后继续执行")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    from aats.data_platform.operations.workflow_dispatcher import (
        list_available_workflows,
        load_workflow_config,
        run_workflow,
    )

    if args.list:
        workflows = list_available_workflows(ROOT)
        print("Available Workflows:")
        for wf in workflows:
            try:
                config = load_workflow_config(ROOT, wf)
                desc = config.get("description", "")
                schedule = config.get("schedule_hint", "")
                tasks = config.get("tasks", [])
                enabled = sum(1 for t in tasks if t.get("enabled", True))
                print(f"  {wf}")
                print(f"    {desc}")
                print(f"    Schedule: {schedule}")
                print(f"    Tasks: {enabled}/{len(tasks)} enabled")
            except Exception as e:
                print(f"  {wf} (error: {e})")
            print()
        return 0

    if not args.workflow:
        print("[ERROR] 请指定 --workflow 或 --list")
        return 2

    print(f"Running workflow: {args.workflow}")
    if args.dry_run:
        print("  (DRY RUN mode)")
    print()

    try:
        report = run_workflow(
            ROOT,
            args.workflow,
            dry_run=args.dry_run,
            stop_on_failure=not args.no_stop_on_failure,
        )
    except FileNotFoundError as e:
        print(f"[ERROR] {e}")
        return 2

    # 输出报告
    print(f"Run ID:   {report['run_id']}")
    print(f"Workflow: {report['workflow']}")
    print(f"Status:   {report['overall_status'].upper()}")
    print(f"Tasks:    {report['succeeded']} ok, {report['failed']} failed, {report['skipped']} skipped")
    print()

    for task in report["tasks"]:
        status = task.get("status", "?")
        name = task.get("name", "?")
        icon = {
            "success": "[OK]  ",
            "dry_run": "[DRY] ",
            "failed": "[FAIL]",
            "timeout": "[TIME]",
            "error": "[ERR] ",
            "skipped": "[SKIP]",
            "disabled": "[OFF] ",
            "skipped_due_to_failure": "[SKIP]",
        }.get(status, "[?]   ")
        print(f"  {icon} {name}")
        if task.get("error"):
            print(f"         {task['error'][:120]}")

    if report["overall_status"] == "failed":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
