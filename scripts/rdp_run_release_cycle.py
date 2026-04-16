#!/usr/bin/env python3
"""Run the approved-only parameter release cycle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 RDP approved-only release cycle")
    parser.add_argument("--actor", default="release_cycle")
    parser.add_argument("--dry-run", action="store_true", help="只评估，不创建 release")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    from aats.data_platform.production_workflow.release_cycle import (
        run_release_cycle,
    )

    result = run_release_cycle(
        ROOT,
        actor=args.actor,
        dry_run=args.dry_run,
        save_results=not args.dry_run,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Release Cycle")
        print(f"  Cycle ID:         {result.get('cycle_id')}")
        print(f"  Environment:      {result.get('environment')}")
        print(f"  Dry Run:          {result.get('dry_run')}")
        print(f"  Reviewed:         {result.get('reviewed_count', 0)}")
        print(f"  Eligible:         {result.get('eligible_count', 0)}")
        print(f"  Created Releases: {result.get('created_release_count', 0)}")
        print(f"  Gate Blocked:     {result.get('blocked_count', 0)}")
        print(f"  Failures:         {result.get('failed_count', 0)}")
        print()
        for item in result.get("results", []):
            print(f"- {item.get('combo_key') or '-'} / {item.get('recommendation_id')}")
            print(f"  outcome={item.get('outcome')} detail={item.get('detail')}")
            if item.get("release_id"):
                print(f"  release_id={item.get('release_id')} apply_result={item.get('apply_result')}")
        if result.get("artifacts"):
            print()
            print(f"Summary: {result['artifacts'].get('summary_path')}")
            print(f"Report:  {result['artifacts'].get('report_path')}")

    print()
    print("Release cycle completed")

    if not result.get("ok", True):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
