#!/usr/bin/env python3
"""Phase 5-C: 失败 Round 重跑.

为失败或部分成功的 round 生成重跑计划，可选择执行。

Usage:
    # 查看所有可重跑 round
    python scripts/rdp_retry_failed_round.py --action list

    # 生成重跑计划
    python scripts/rdp_retry_failed_round.py --action plan \
        --round-dir artifacts/research/attribution_rounds/20260403_123456_abcd1234 \
        --phase phase3

    # 执行整轮重跑
    python scripts/rdp_retry_failed_round.py --action rerun \
        --round-dir artifacts/research/attribution_rounds/20260403_123456_abcd1234 \
        --phase phase3

Exit codes:
    0 = 成功
    1 = 参数错误或无可操作项
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import subprocess
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_retry_failed_round")

_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from aats.data_platform.governance.retry_logic import (
    generate_retry_plan,
    find_retryable_rounds,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 5-C: 失败 Round 重跑",
    )
    parser.add_argument("--action", required=True,
                        choices=["list", "plan", "rerun"],
                        help="操作: list / plan / rerun")
    parser.add_argument("--round-dir", default=None,
                        help="Round 目录路径")
    parser.add_argument("--phase", default=None,
                        help="Phase 标识 (phase3 / phase4)")
    parser.add_argument("--output", default=None,
                        help="输出 retry plan JSON 路径")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.action == "list":
        retryable = find_retryable_rounds(_PROJECT_ROOT, phase=args.phase)
        print(f"\n=== 可重跑 Rounds ({len(retryable)}) ===\n")
        if not retryable:
            print("  (none)")
        else:
            for r in retryable:
                print(f"  {r['phase']} / {r['round_id']}")
                print(f"    path: {r['path']}")
                print(f"    combos: {json.dumps(r['combo_statuses'])}")
                print()
        return

    if args.action in ("plan", "rerun"):
        if not args.round_dir:
            print("ERROR: --round-dir 必须指定", file=sys.stderr)
            sys.exit(1)
        if not args.phase:
            print("ERROR: --phase 必须指定", file=sys.stderr)
            sys.exit(1)

        round_dir = pathlib.Path(args.round_dir)
        plan = generate_retry_plan(round_dir, phase=args.phase)

        print(f"\n=== Retry Plan ===")
        print(f"Round: {plan.get('original_round_id', '?')}")
        print(f"Phase: {plan['phase']}")
        print(f"Original status: {plan.get('original_status', '?')}")
        print(f"Failed combos: {len(plan.get('failed_combos', []))}")
        print()

        for fc in plan.get("failed_combos", []):
            print(f"  [FAIL] {fc['combo_key']} ({fc['original_status']})")

        if plan.get("full_rerun_command"):
            print(f"\nFull rerun command:")
            print(f"  {plan['full_rerun_command']}")

        if plan.get("retry_commands"):
            print(f"\nPer-combo retry commands:")
            for rc in plan["retry_commands"]:
                print(f"  [{rc['combo_key']}] {rc['command']}")

        for note in plan.get("notes", []):
            print(f"\nNote: {note}")

        # 保存 plan
        if args.output:
            output_path = pathlib.Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with output_path.open("w", encoding="utf-8") as f:
                json.dump(plan, f, indent=2, ensure_ascii=False, default=str)
            print(f"\nPlan saved: {output_path}")

        # 执行重跑
        if args.action == "rerun" and plan.get("full_rerun_command"):
            if args.dry_run:
                print(f"\n[DRY-RUN] 将执行: {plan['full_rerun_command']}")
            else:
                print(f"\n执行重跑...")
                cmd = plan["full_rerun_command"]
                log.info("CMD: %s", cmd)
                result = subprocess.run(cmd, shell=True)
                print(f"\n重跑完成, exit code: {result.returncode}")
                sys.exit(result.returncode)


if __name__ == "__main__":
    main()
