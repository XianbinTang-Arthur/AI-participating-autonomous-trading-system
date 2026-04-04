#!/usr/bin/env python3
"""Phase 6-D: 评估 Promotion Readiness.

综合所有证据，判断是否建议进入下一轮 live test。

Usage:
    python scripts/rdp_evaluate_promotion_readiness.py

    python scripts/rdp_evaluate_promotion_readiness.py --output readiness.json

Exit codes:
    0 = ready_for_next_live_test
    1 = not ready
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
log = logging.getLogger("rdp_promotion_readiness")

_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from aats.data_platform.decision_system.evidence_bundle import build_evidence_bundle
from aats.data_platform.decision_system.candidate_selector import (
    select_parameter_upgrade_candidates,
)
from aats.data_platform.decision_system.decision_engine import (
    decide_all_family_timeframes,
)
from aats.data_platform.decision_system.readiness_evaluator import (
    evaluate_promotion_readiness,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 6-D: Promotion Readiness 评估",
    )
    parser.add_argument("--artifact-root", default=str(_PROJECT_ROOT))
    parser.add_argument("--output", default=None, help="输出 JSON 路径")
    args = parser.parse_args()

    project_root = pathlib.Path(args.artifact_root)

    # 构建 evidence
    log.info("构建 evidence bundle...")
    evidence = build_evidence_bundle(project_root)

    # 加载 parameter sets
    reg_path = project_root / "artifacts/governance/current_parameter_registry.json"
    parameter_sets = []
    if reg_path.exists():
        with reg_path.open(encoding="utf-8") as f:
            reg = json.load(f)
        # 默认只评估治理确认的参数集，不包括 draft
        parameter_sets = [
            ps for ps in reg.get("parameter_sets", [])
            if ps["status"] in ("frozen", "candidate")
        ]

    # 评估
    log.info("评估参数升级候选...")
    upgrade_candidates = select_parameter_upgrade_candidates(parameter_sets, evidence)

    log.info("生成 family/timeframe 决策...")
    ft_decisions = decide_all_family_timeframes(evidence)

    log.info("评估 promotion readiness...")
    report = evaluate_promotion_readiness(evidence, upgrade_candidates, ft_decisions)

    # 输出
    readiness = report["readiness"]
    conf = report["overall_confidence"]
    icon = "READY" if readiness == "ready_for_next_live_test" else "NOT_READY"

    print(f"\n=== Promotion Readiness [{icon}] ===")
    print(f"Status: {readiness}")
    print(f"Confidence: {conf}")
    print(f"Checks: {report['checks_passed']}/{report['checks_total']} passed")
    print()

    for c in report.get("checks", []):
        ci = "OK" if c["passed"] else "FAIL"
        print(f"  [{ci}] {c['check']}: {c['detail']}")

    if report.get("blockers"):
        print(f"\nBlockers:")
        for b in report["blockers"]:
            print(f"  - {b}")

    if report.get("promoted_candidates"):
        print(f"\nPromoted candidates:")
        for p in report["promoted_candidates"]:
            print(f"  - {p['parameter_set_id']} (score={p['score_ratio']:.3f})")

    if args.output:
        out = pathlib.Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        print(f"\nOutput: {out}")

    # exit code
    if readiness != "ready_for_next_live_test":
        sys.exit(1)


if __name__ == "__main__":
    main()
