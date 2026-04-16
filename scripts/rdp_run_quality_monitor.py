#!/usr/bin/env python3
"""Phase 5-D: 质量监控与巡检.

运行全部质量检查项，生成 quality_monitor_summary.json。

Usage:
    python scripts/rdp_run_quality_monitor.py

    python scripts/rdp_run_quality_monitor.py \
        --output artifacts/governance/quality_monitor_summary.json

Exit codes:
    0 = healthy（无 critical failure）
    1 = unhealthy（有 critical failure）
    2 = degraded（有 warning failure，无 critical）
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
log = logging.getLogger("rdp_quality_monitor")

_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from aats.data_platform.governance.quality_monitor import run_quality_monitor
from aats.data_platform.governance.snapshot_db import (
    SNAPSHOT_QUALITY_MONITOR,
    save_governance_snapshot,
)

_DEFAULT_OUTPUT = "artifacts/governance/quality_monitor_summary.json"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 5-D: 质量监控与巡检",
    )
    parser.add_argument("--artifact-root", default=str(_PROJECT_ROOT))
    parser.add_argument("--output", default=_DEFAULT_OUTPUT)
    parser.add_argument("--no-print", action="store_true")
    args = parser.parse_args()

    project_root = pathlib.Path(args.artifact_root)

    log.info("开始质量巡检...")
    result = run_quality_monitor(project_root)

    # 保存
    output_path = project_root / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False, default=str)
    log.info("巡检结果: %s", output_path)
    if not save_governance_snapshot(snapshot_type=SNAPSHOT_QUALITY_MONITOR, payload=result):
        log.warning("quality_monitor_summary DB upsert failed; file artifact kept as audit copy")

    # 显示
    if not args.no_print:
        summary = result["summary"]
        health = summary["health"]
        health_icon = {
            "healthy": "OK",
            "degraded": "WARN",
            "unhealthy": "CRIT",
        }.get(health, "??")

        print()
        print(f"=== Quality Monitor [{health_icon}] ===")
        print(f"Health: {health}")
        print(f"Total checks: {summary['total_checks']}")
        print(f"Passed: {summary['passed']}")
        print(f"Failed: {summary['failed']}")
        print(f"  Critical: {summary['critical_failures']}")
        print(f"  Warning: {summary['warning_failures']}")
        print()

        # 按类别分组显示
        by_cat: dict[str, list] = {}
        for check in result["checks"]:
            cat = check["category"]
            by_cat.setdefault(cat, []).append(check)

        for cat, checks in by_cat.items():
            print(f"--- {cat} ---")
            for c in checks:
                icon = "OK" if c["passed"] else "FAIL"
                level = c["level"].upper()[:4]
                print(f"  [{icon}] [{level}] {c['name']}")
                if not c["passed"]:
                    print(f"         {c['detail']}")
            print()

        print(f"Output: {output_path}")

    # 退出码
    health = result["summary"]["health"]
    if health == "unhealthy":
        sys.exit(1)
    elif health == "degraded":
        sys.exit(2)


if __name__ == "__main__":
    main()
