#!/usr/bin/env python3
"""创建 Parameter Release.

工作包 B: 完整的 gate -> release -> apply 流程。

用法:
    # 完整流程: gate + release + apply
    python scripts/rdp_create_parameter_release.py \
        --recommendation-id rec_xxx --actor operator_name

    # 跳过 gate（紧急情况）
    python scripts/rdp_create_parameter_release.py \
        --recommendation-id rec_xxx --skip-gate

    # 只创建 release 不 apply
    python scripts/rdp_create_parameter_release.py \
        --recommendation-id rec_xxx --skip-apply

    # 指定观察窗口
    python scripts/rdp_create_parameter_release.py \
        --recommendation-id rec_xxx --window-hours 48

退出码:
    0 = 成功
    1 = 错误
    3 = gate blocked
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Create parameter release")
    p.add_argument("--recommendation-id", required=True)
    p.add_argument("--actor", default="operator")
    p.add_argument("--window-hours", type=int, default=24, help="观察窗口时长（小时）")
    p.add_argument("--notes", default=None)
    p.add_argument("--skip-gate", action="store_true", help="跳过 gate 检查")
    p.add_argument("--skip-apply", action="store_true", help="只创建 release 不 apply")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    from aats.data_platform.production_workflow.release_registry import (
        create_parameter_release,
    )

    result = create_parameter_release(
        ROOT,
        recommendation_id=args.recommendation_id,
        actor=args.actor,
        observation_window_hours=args.window_hours,
        notes=args.notes,
        run_gate=not args.skip_gate,
        run_apply=not args.skip_apply,
    )

    if not result["ok"]:
        # 区分 gate blocked 和其他错误
        release = result.get("release", {})
        if release.get("apply_result") == "blocked_by_gate":
            print(f"[BLOCKED] {result.get('message')}")
            gate = result.get("gate_result", {})
            if gate:
                print(f"  Gate Status: {gate.get('gate_status', '?').upper()}")
                for reason in gate.get("blocking_reasons", []):
                    print(f"  - {reason}")
            return 3
        print(f"[ERROR] {result.get('message')}")
        return 1

    release = result.get("release", {})
    print(f"[OK] {result.get('message')}")
    print()
    print(f"  Release ID:    {release.get('release_id')}")
    print(f"  Combo:         {release.get('combo_key')}")
    print(f"  Parameter Set: {release.get('parameter_set_id')}")
    print(f"  Previous:      {release.get('previous_parameter_set_id', 'none')}")
    print(f"  Gate Status:   {release.get('gate_status', 'skipped')}")
    print(f"  Apply Result:  {release.get('apply_result')}")
    print(f"  Observation:   {release.get('observation_status')}")
    print(f"  Window:        {release.get('observation_window_hours')}h")

    gate = result.get("gate_result")
    if gate and gate.get("warnings"):
        print()
        print("  Warnings:")
        for w in gate["warnings"]:
            print(f"    - {w}")

    if release.get("apply_result") == "success":
        print()
        print("  后续操作:")
        print("    1. 重启主交易系统使新参数生效")
        print(f"    2. 在 {release.get('observation_window_hours', 24)}h 内持续观察")
        print("    3. 运行 rdp_run_post_apply_observation.py 检查观察窗口")

    return 0


if __name__ == "__main__":
    sys.exit(main())
