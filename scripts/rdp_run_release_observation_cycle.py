#!/usr/bin/env python3
"""处理所有 observing 状态的 release."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run RDP release observation cycle")
    parser.add_argument("--dry-run", action="store_true", help="只评估，不写 observation/rollback/effectiveness 结果")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    from aats.data_platform.production_workflow.observation_cycle import (
        run_release_observation_cycle,
    )

    result = run_release_observation_cycle(ROOT, save_results=not args.dry_run)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Release Observation Cycle")
        print(f"  Processed:            {result.get('processed_count', 0)}")
        print(f"  Rollback Recommended: {result.get('rollback_recommended_count', 0)}")
        print(f"  Auto Rollbacks:       {result.get('auto_rollback_count', 0)}")
        print()
        for item in result.get("results", []):
            release_id = item.get("release_id")
            obs = item.get("observation", {})
            rollback = item.get("rollback_recommendation", {})
            effectiveness = item.get("effectiveness", {})
            print(f"- {release_id}")
            print(f"  observation={obs.get('status')} recommendation={obs.get('recommendation')}")
            print(f"  rollback_recommended={rollback.get('rollback_recommended')} severity={rollback.get('severity')}")
            print(f"  effectiveness={effectiveness.get('conclusion')}")
        if result.get("auto_rollbacks"):
            print()
            print("Auto Rollbacks:")
            for item in result["auto_rollbacks"]:
                print(f"  - {item.get('release_id')}: {'ok' if item.get('ok') else 'failed'}")

    if not result.get("ok", True):
        return 1
    if result.get("rollback_recommended_count", 0) > 0 or result.get("auto_rollback_count", 0) > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
