#!/usr/bin/env python3
"""Phase 6-B: 参数升级候选选择.

从 parameter registry 中筛选最值得推荐的参数集。

Usage:
    python scripts/rdp_select_parameter_upgrade.py

    python scripts/rdp_select_parameter_upgrade.py --family independent

    python scripts/rdp_select_parameter_upgrade.py --output candidates.json
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
log = logging.getLogger("rdp_select_param_upgrade")

_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from aats.data_platform.decision_system.evidence_bundle import build_evidence_bundle
from aats.data_platform.decision_system.candidate_selector import (
    select_parameter_upgrade_candidates,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 6-B: 参数升级候选选择",
    )
    parser.add_argument("--artifact-root", default=str(_PROJECT_ROOT))
    parser.add_argument("--family", default=None, help="限定 family")
    parser.add_argument("--timeframe", default=None, help="限定 timeframe")
    parser.add_argument("--status", default=None,
                        help="限定参数状态 (frozen / candidate / draft)")
    parser.add_argument("--output", default=None, help="输出 JSON 路径")
    args = parser.parse_args()

    project_root = pathlib.Path(args.artifact_root)

    # 加载 parameter sets
    reg_path = project_root / "artifacts/governance/current_parameter_registry.json"
    if not reg_path.exists():
        print("ERROR: parameter registry 不存在", file=sys.stderr)
        sys.exit(1)

    with reg_path.open(encoding="utf-8") as f:
        reg = json.load(f)

    parameter_sets = reg.get("parameter_sets", [])

    # 过滤
    if args.family:
        parameter_sets = [ps for ps in parameter_sets if ps["family"] == args.family]
    if args.timeframe:
        parameter_sets = [ps for ps in parameter_sets if ps["timeframe"] == args.timeframe]
    if args.status:
        parameter_sets = [ps for ps in parameter_sets if ps["status"] == args.status]
    else:
        parameter_sets = [
            ps for ps in parameter_sets
            if ps["status"] in ("frozen", "candidate", "draft")
        ]

    log.info("评估 %d 个 parameter sets...", len(parameter_sets))

    # 构建 evidence
    evidence = build_evidence_bundle(project_root)

    # 评估
    results = select_parameter_upgrade_candidates(parameter_sets, evidence)

    # 输出
    print(f"\n=== Parameter Upgrade Candidates ({len(results)}) ===\n")
    for uc in results:
        icon = {"promote_candidate": "UP", "hold": "--", "reject": "NO"}.get(uc["decision"], "??")
        print(f"  [{icon}] {uc['parameter_set_id']}")
        print(f"       {uc['family']} / {uc['timeframe']}: {uc['decision']} ({uc['confidence']})")
        print(f"       score: {uc['score_ratio']:.3f} ({uc['total_score']}/{uc['max_score']})")

        for ds in uc.get("dimension_scores", []):
            print(f"       [{ds['dimension']}] {ds['score']:.1f}/{ds['max_score']:.1f}")
            for d in ds.get("details", []):
                print(f"         - {d}")
        print()

    if args.output:
        out = pathlib.Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False, default=str)
        print(f"Output: {out}")


if __name__ == "__main__":
    main()
