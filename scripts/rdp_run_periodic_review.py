#!/usr/bin/env python3
"""运行 RDP 周期复盘.

用法:
    python scripts/rdp_run_periodic_review.py --window weekly
    python scripts/rdp_run_periodic_review.py --window monthly
    python scripts/rdp_run_periodic_review.py --window weekly --family independent --timeframe 15m
    python scripts/rdp_run_periodic_review.py --window weekly --json

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
    p = argparse.ArgumentParser(description="运行 RDP 周期复盘")
    p.add_argument(
        "--window",
        choices=["weekly", "monthly"],
        default="weekly",
        help="复盘窗口",
    )
    p.add_argument("--family", default=None, help="按 family 筛选")
    p.add_argument("--timeframe", default=None, help="按 timeframe 筛选")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    from aats.data_platform.metrics.periodic_review import run_periodic_review

    review = run_periodic_review(
        ROOT,
        window=args.window,
        family=args.family,
        timeframe=args.timeframe,
    )

    if args.json:
        print(json.dumps(review, indent=2, ensure_ascii=False))
    else:
        s = review.get("summary", {})
        print(f"RDP {args.window.capitalize()} Review")
        print(f"  Review ID: {review.get('review_id')}")
        print(f"  Period:    {review.get('window_start', '?')} ~ {review.get('window_end', '?')}")
        print()
        print(f"  Releases:  {s.get('total_releases', 0)} (success: {s.get('successful_releases', 0)})")
        print(f"  Applies:   {s.get('total_applies', 0)}")
        print(f"  Rollbacks: {s.get('total_rollbacks', 0)} (ratio: {s.get('rollback_ratio', 0):.1%})")
        print(f"  Workflows: {s.get('workflow_runs', 0)} (success: {s.get('workflow_success', 0)})")
        print(f"  Open Fail: {s.get('open_failures', 0)}")
        print()

        eff = s.get("effectiveness", {})
        if eff.get("total_evaluated", 0) > 0:
            print("  Effectiveness:")
            print(f"    Effective: {eff.get('effective', 0)}")
            print(f"    Mixed:     {eff.get('mixed', 0)}")
            print(f"    Ineffect:  {eff.get('ineffective', 0)}")
            print(f"    Insuff:    {eff.get('insufficient_evidence', 0)}")
            print()

        ranking = review.get("combo_ranking", [])
        if ranking:
            print("  Family/Timeframe Ranking:")
            for c in ranking:
                print(
                    f"    {c.get('combo_key', '?')}: "
                    f"{c.get('release_count', 0)} releases, "
                    f"{c.get('apply_success', 0)} success"
                )
            print()

        suggestions = review.get("improvement_suggestions", [])
        if suggestions:
            print(f"  Improvement Suggestions ({len(suggestions)}):")
            for sg in suggestions:
                print(
                    f"    [{sg.get('priority', '?').upper()}] "
                    f"({sg.get('category', '?')}) {sg.get('problem', '')}"
                )
                print(f"      → {sg.get('suggested_action', '')}")
            print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
