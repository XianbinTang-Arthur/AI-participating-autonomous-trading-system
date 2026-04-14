#!/usr/bin/env python3
"""生成 Improvement Backlog.

用法:
    python scripts/rdp_generate_improvement_backlog.py
    python scripts/rdp_generate_improvement_backlog.py --json
    python scripts/rdp_generate_improvement_backlog.py --from-review REVIEW_ID
    python scripts/rdp_generate_improvement_backlog.py --update BACKLOG_ID --status resolved --notes "已修复"
    python scripts/rdp_generate_improvement_backlog.py --list

退出码: 0 = 成功
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="生成 Improvement Backlog")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    p.add_argument("--list", action="store_true", help="列出当前 backlog")
    p.add_argument("--from-review", metavar="REVIEW_ID", help="从 review 生成")
    p.add_argument("--update", metavar="BACKLOG_ID", help="更新 item 状态")
    p.add_argument(
        "--status",
        choices=["open", "in_progress", "resolved", "ignored"],
        help="新状态",
    )
    p.add_argument("--notes", default="", help="备注")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    from aats.data_platform.metrics.backlog_builder import (
        generate_improvement_backlog,
        load_backlog,
        update_backlog_item_status,
    )

    if args.update:
        if not args.status:
            print("[ERROR] --update 需要 --status")
            return 1
        result = update_backlog_item_status(
            ROOT, args.update, args.status, args.notes
        )
        if result:
            print(f"Updated {args.update} → {args.status}")
        else:
            print(f"[ERROR] {args.update} not found")
        return 0

    if args.list:
        backlog = load_backlog(ROOT)
        items = backlog.get("items", [])
        if args.json:
            print(json.dumps(backlog, indent=2, ensure_ascii=False))
        else:
            if not items:
                print("Improvement Backlog: empty")
                return 0
            stats = backlog.get("stats", {})
            print(f"Improvement Backlog ({stats.get('total', len(items))} items)")
            print(f"  Open: {stats.get('open', 0)}, In-progress: {stats.get('in_progress', 0)}, "
                  f"Resolved: {stats.get('resolved', 0)}, Ignored: {stats.get('ignored', 0)}")
            print()
            for item in items:
                status_icon = {
                    "open": "[OPEN]",
                    "in_progress": "[WIP] ",
                    "resolved": "[DONE]",
                    "ignored": "[IGN] ",
                }.get(item.get("status", ""), "[?]   ")
                prio = item.get("priority", "?").upper()
                print(f"  {status_icon} [{prio}] {item.get('backlog_id', '?')}")
                print(f"    {item.get('problem_statement', '')}")
                print(f"    → {item.get('suggested_action', '')}")
                if item.get("family"):
                    print(f"    Scope: {item.get('family')}/{item.get('timeframe', '*')}")
                print()
        return 0

    # 生成 backlog
    backlog = generate_improvement_backlog(ROOT)

    if args.json:
        print(json.dumps(backlog, indent=2, ensure_ascii=False))
    else:
        stats = backlog.get("stats", {})
        print("Improvement Backlog Generated")
        print(f"  Total:     {stats.get('total', 0)}")
        print(f"  Open:      {stats.get('open', 0)}")
        print(f"  High Prio: {stats.get('high_priority', 0)}")
        print()

        open_items = [i for i in backlog.get("items", []) if i.get("status") == "open"]
        if open_items:
            print("  Open Items:")
            for item in open_items:
                prio = item.get("priority", "?").upper()
                print(f"    [{prio}] {item.get('problem_statement', '')}")
                print(f"      → {item.get('suggested_action', '')}")
            print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
