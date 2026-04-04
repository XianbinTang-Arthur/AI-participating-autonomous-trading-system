#!/usr/bin/env python3
"""运行 Post-Apply 观察窗口检查.

工作包 C: 参数生效后的观察与评估。

用法:
    python scripts/rdp_run_post_apply_observation.py \
        --release-id rel_xxx

    python scripts/rdp_run_post_apply_observation.py \
        --release-id rel_xxx --window-hours 48

    python scripts/rdp_run_post_apply_observation.py \
        --release-id rel_xxx --family independent --timeframe 15m

退出码:
    0 = observation ok (keep)
    1 = 错误
    2 = rollback_recommended
    3 = review needed
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
    p = argparse.ArgumentParser(description="Run post-apply observation")
    p.add_argument("--release-id", required=True)
    p.add_argument("--family", default=None, help="如不指定，从 release 记录推断")
    p.add_argument("--timeframe", default=None, help="如不指定，从 release 记录推断")
    p.add_argument("--window-hours", type=int, default=24)
    p.add_argument("--dry-run", action="store_true", help="不保存结果")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    from aats.data_platform.production_workflow.release_registry import (
        find_release,
        load_release_history,
    )

    # 从 release 推断 family/timeframe
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

    from aats.data_platform.production_workflow.observation_window import (
        run_observation,
    )

    result = run_observation(
        ROOT,
        release_id=args.release_id,
        family=family,
        timeframe=timeframe,
        window_hours=args.window_hours,
        save_result=not args.dry_run,
    )

    # 输出
    status = result["status"].upper()
    rec = result["recommendation"].upper()
    print(f"Observation Status:     {status}")
    print(f"Recommendation:         {rec}")
    print(f"Window Active:          {'Yes' if result['window_active'] else 'No'}")
    print(f"Regressions:            {result['regression_count']}")
    print(f"Warnings:               {result['warning_count']}")
    print()

    for check in result["checklist"]:
        icon = {"ok": "[OK]", "warn": "[WARN]", "regression": "[REGR]", "unknown": "[?]"}.get(
            check.get("status", "?"), "[?]"
        )
        print(f"  {icon} {check['name']}: {check.get('detail', '')}")

    if result["recommendation"] == "rollback_recommended":
        print()
        print("  !!! ROLLBACK RECOMMENDED !!!")
        print("  运行: python scripts/rdp_rollback_active_parameter_set.py")
        return 2
    elif result["recommendation"] == "review":
        print()
        print("  需要人工审查。")
        return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())
