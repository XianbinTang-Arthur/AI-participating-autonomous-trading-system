#!/usr/bin/env python3
"""Phase 5-C: 列出 Active Rounds.

扫描所有 round 目录，构建 active_round_index.json 并显示概览。

Usage:
    python scripts/rdp_list_active_rounds.py

    python scripts/rdp_list_active_rounds.py --phase phase3

    python scripts/rdp_list_active_rounds.py --status failed

    python scripts/rdp_list_active_rounds.py \
        --output artifacts/governance/active_round_index.json
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_list_active_rounds")

_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from aats.data_platform.governance.round_status import (
    build_active_round_index,
    list_rounds_by_status,
    scan_experiments,
)
from aats.data_platform.governance.snapshot_db import (
    SNAPSHOT_ACTIVE_ROUND_INDEX,
    save_governance_snapshot,
)

_DEFAULT_OUTPUT = "artifacts/governance/active_round_index.json"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 5-C: 列出 Active Rounds",
    )
    parser.add_argument("--artifact-root", default=str(_PROJECT_ROOT))
    parser.add_argument("--phase", default=None, help="限定 phase")
    parser.add_argument("--status", default=None, help="筛选状态")
    parser.add_argument("--output", default=_DEFAULT_OUTPUT)
    parser.add_argument("--include-deprecated", action="store_true")
    parser.add_argument("--include-experiments", action="store_true",
                        help="同时列出 experiments 目录")
    args = parser.parse_args()

    project_root = pathlib.Path(args.artifact_root)
    phases = [args.phase] if args.phase else None

    log.info("构建 active round index...")
    index = build_active_round_index(
        project_root,
        phases=phases,
        include_deprecated=args.include_deprecated,
    )

    # 筛选
    if args.status:
        rounds = list_rounds_by_status(index, args.status)
    else:
        rounds = index.get("all_rounds", [])

    # 输出
    output_path = project_root / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False, default=str)
    if not save_governance_snapshot(snapshot_type=SNAPSHOT_ACTIVE_ROUND_INDEX, payload=index):
        log.warning("active_round_index DB upsert failed; file artifact kept as audit copy")

    # 显示
    summary = index["summary"]
    print()
    print("=== Active Round Index ===")
    print(f"Total rounds: {summary['total_rounds']}")
    print(f"Status distribution: {json.dumps(summary['status_distribution'])}")
    print(f"Phases: {', '.join(summary['phases_with_rounds']) or 'none'}")
    print()

    # 最近 round
    latest = index.get("latest_by_phase", {})
    if latest:
        print("--- Latest by Phase ---")
        for phase, info in latest.items():
            status_icon = {
                "succeeded": "OK",
                "partial_success": "PART",
                "failed": "FAIL",
            }.get(info.get("status", ""), "??")
            print(f"  [{status_icon}] {phase}: {info['round_id']}")
            print(f"         started: {info.get('started_at', '?')}")
            print(f"         combos: {info.get('combo_count', 0)}")
            print(f"         statuses: {json.dumps(info.get('combo_statuses', {}))}")
            print()

    # 所有 round
    if rounds:
        print(f"--- All Rounds ({len(rounds)}) ---")
        for r in rounds:
            status_icon = {
                "succeeded": "OK",
                "partial_success": "PART",
                "failed": "FAIL",
                "deprecated": "DEPR",
            }.get(r.get("status", ""), "??")
            print(f"  [{status_icon}] {r['phase']} / {r['round_id']}")
            print(f"         path: {r['path']}")
    else:
        print("  (no rounds found)")

    # experiments
    if args.include_experiments:
        experiments = scan_experiments(project_root)
        if experiments:
            print(f"\n--- Experiments ({len(experiments)}) ---")
            for e in experiments:
                etype = e.get("type", "experiment")
                oc = e.get("opening_count", "?")
                per = e.get("positive_edge_ratio", "?")
                print(f"  [{etype[:4].upper()}] {e['experiment_id']}")
                print(f"         opening={oc}, positive_edge={per}")

    print(f"\nOutput: {output_path}")


if __name__ == "__main__":
    main()
