#!/usr/bin/env python3
"""评估 Rollback Recommendation.

工作包 D: 基于规则的回滚建议评估。

用法:
    python scripts/rdp_evaluate_rollback_recommendation.py \
        --release-id rel_xxx

    python scripts/rdp_evaluate_rollback_recommendation.py \
        --release-id rel_xxx --family independent --timeframe 15m

退出码:
    0 = 不建议回滚
    1 = 错误
    2 = 建议回滚 (severity=high)
    3 = 建议回滚 (severity=medium)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Evaluate rollback recommendation")
    p.add_argument("--release-id", required=True)
    p.add_argument("--family", default=None)
    p.add_argument("--timeframe", default=None)
    p.add_argument("--dry-run", action="store_true", help="不保存结果")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    from aats.data_platform.production_workflow.release_registry import (
        find_release,
        load_release_history,
    )

    family = args.family
    timeframe = args.timeframe

    if not family or not timeframe:
        history = load_release_history(ROOT)
        release = find_release(history, args.release_id)
        if release:
            family = family or release.get("family")
            timeframe = timeframe or release.get("timeframe")

    if not family or not timeframe:
        print("[ERROR] 无法确定 family/timeframe，请用 --family 和 --timeframe 指定")
        return 1

    from aats.data_platform.production_workflow.rollback_policy import (
        evaluate_rollback_recommendation,
    )

    result = evaluate_rollback_recommendation(
        ROOT,
        release_id=args.release_id,
        family=family,
        timeframe=timeframe,
        save_result=not args.dry_run,
    )

    # 输出
    rec = "YES" if result["rollback_recommended"] else "NO"
    print(f"Rollback Recommended:  {rec}")
    print(f"Severity:              {result['severity']}")
    print(f"Fired Triggers:        {result['fired_trigger_count']}")
    if result.get("suggested_target_parameter_set_id"):
        print(f"Suggested Target:      {result['suggested_target_parameter_set_id']}")
    print()

    for t in result["triggers"]:
        icon = "[FIRED]" if t.get("fired") else "[OK]  "
        print(f"  {icon} {t['trigger']}: {t.get('detail', '')}")

    if result["reasons"]:
        print()
        print("Reasons:")
        for r in result["reasons"]:
            print(f"  - {r}")

    if result["rollback_recommended"]:
        print()
        print("  !!! ROLLBACK RECOMMENDED !!!")
        target = result.get("suggested_target_parameter_set_id")
        if target:
            print(f"  运行: python scripts/rdp_rollback_active_parameter_set.py "
                  f"--family {family} --timeframe {timeframe} "
                  f"--to-parameter-set-id {target}")
        return 2 if result["severity"] == "high" else 3

    return 0


if __name__ == "__main__":
    sys.exit(main())
