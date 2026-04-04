#!/usr/bin/env python3
"""Phase 6: 运行一轮完整闭环决策分析.

整合 Phase 2/3/4/5 证据，生成参数升级候选、family/timeframe 状态建议、
promotion readiness 评估，写入 recommendation / decision registry。

Usage:
    python scripts/rdp_run_decision_round.py

    python scripts/rdp_run_decision_round.py \
        --artifact-root D:/path/to/project

Exit codes:
    0 = 成功
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
from datetime import datetime, timezone
from uuid import uuid4

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_decision_round")

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
from aats.data_platform.decision_system.recommendation_registry import (
    update_registries_from_round,
)
from aats.data_platform.decision_system.report_builder import (
    build_phase6_conclusion,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 6: Closed-Loop Decision Round",
    )
    parser.add_argument("--artifact-root", default=str(_PROJECT_ROOT))
    parser.add_argument("--include-draft", action="store_true",
                        help="同时评估 draft 状态的参数集（默认只看 frozen + candidate）")
    parser.add_argument("--no-print-summary", action="store_true")
    args = parser.parse_args()

    project_root = pathlib.Path(args.artifact_root)
    started_at = datetime.now(timezone.utc).isoformat()
    round_id = (
        datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        + "_" + uuid4().hex[:8]
    )
    round_dir = project_root / "artifacts/decision_rounds" / round_id

    log.info("=" * 60)
    log.info("Phase 6 Decision Round")
    log.info("  Round ID: %s", round_id)
    log.info("  Project : %s", project_root)
    log.info("  Output  : %s", round_dir)
    log.info("=" * 60)

    # ── Step 1: Evidence Bundle ──
    log.info("")
    log.info("[1/6] Building evidence bundle...")
    evidence_bundle = build_evidence_bundle(project_root)
    completeness = evidence_bundle.get("evidence_completeness", {})
    log.info("  Phases with data: %s", completeness.get("phases_with_data", []))
    log.info("  Completeness: %.0f%%", completeness.get("completeness_ratio", 0) * 100)

    # 保存 evidence_summary.json
    round_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = round_dir / "evidence_summary.json"
    with evidence_path.open("w", encoding="utf-8") as f:
        json.dump(evidence_bundle, f, indent=2, ensure_ascii=False, default=str)
    log.info("  -> %s", evidence_path)

    # ── Step 2: Parameter Upgrade Candidates ──
    log.info("")
    log.info("[2/6] Selecting parameter upgrade candidates...")

    # 从 governance registry 获取 parameter sets
    gov_registry_path = project_root / "artifacts/governance/current_parameter_registry.json"
    parameter_sets: list[dict] = []
    # 默认只评估治理确认的参数集（frozen + candidate），
    # draft 需要显式 --include-draft 才纳入
    allowed_statuses = {"frozen", "candidate"}
    if args.include_draft:
        allowed_statuses.add("draft")
    if gov_registry_path.exists():
        with gov_registry_path.open(encoding="utf-8") as f:
            reg = json.load(f)
        parameter_sets = [
            ps for ps in reg.get("parameter_sets", [])
            if ps.get("status") in allowed_statuses
        ]
    log.info("  Parameter sets to evaluate: %d (statuses: %s)",
             len(parameter_sets), sorted(allowed_statuses))

    upgrade_candidates = select_parameter_upgrade_candidates(
        parameter_sets, evidence_bundle,
    )

    candidates_path = round_dir / "parameter_upgrade_candidates.json"
    with candidates_path.open("w", encoding="utf-8") as f:
        json.dump(upgrade_candidates, f, indent=2, ensure_ascii=False, default=str)
    log.info("  -> %s", candidates_path)

    for uc in upgrade_candidates:
        log.info("  [%s] %s/%s: %s (score=%.3f)",
                 uc["decision"].upper()[:4],
                 uc["family"], uc["timeframe"],
                 uc["decision"], uc["score_ratio"])

    # ── Step 3: Family/Timeframe Decisions ──
    log.info("")
    log.info("[3/6] Making family/timeframe decisions...")

    ft_decisions = decide_all_family_timeframes(evidence_bundle)

    ft_path = round_dir / "family_timeframe_decisions.json"
    with ft_path.open("w", encoding="utf-8") as f:
        json.dump(ft_decisions, f, indent=2, ensure_ascii=False, default=str)
    log.info("  -> %s", ft_path)

    for ftd in ft_decisions:
        log.info("  [%s] %s: %s (confidence=%s)",
                 ftd["decision"][:4].upper(),
                 ftd["combo_key"],
                 ftd["decision"],
                 ftd["confidence"])

    # ── Step 4: Promotion Readiness ──
    log.info("")
    log.info("[4/6] Evaluating promotion readiness...")

    readiness_report = evaluate_promotion_readiness(
        evidence_bundle, upgrade_candidates, ft_decisions,
    )

    readiness_path = round_dir / "promotion_readiness_report.json"
    with readiness_path.open("w", encoding="utf-8") as f:
        json.dump(readiness_report, f, indent=2, ensure_ascii=False, default=str)
    log.info("  Readiness: %s (confidence=%s)",
             readiness_report["readiness"],
             readiness_report["overall_confidence"])
    log.info("  Checks: %d/%d passed",
             readiness_report["checks_passed"],
             readiness_report["checks_total"])
    log.info("  -> %s", readiness_path)

    # ── Step 5: Update Registries ──
    log.info("")
    log.info("[5/6] Updating registries...")

    registry_stats = update_registries_from_round(
        round_id=round_id,
        upgrade_candidates=upgrade_candidates,
        ft_decisions=ft_decisions,
        evidence_bundle=evidence_bundle,
        rec_registry_path=project_root / "artifacts/decision_system/recommendation_registry.json",
        decision_registry_path=project_root / "artifacts/decision_system/active_decision_registry.json",
        bundle_index_path=project_root / "artifacts/decision_system/evidence_bundle_index.json",
        evidence_summary_path=str(evidence_path),
    )
    log.info("  Recommendations added: %d", registry_stats["recommendations_added"])
    log.info("  Decisions updated: %d", registry_stats["decisions_updated"])

    # ── Step 6: Conclusion Document ──
    log.info("")
    log.info("[6/6] Building conclusion document...")

    build_phase6_conclusion(
        round_id=round_id,
        evidence_bundle=evidence_bundle,
        upgrade_candidates=upgrade_candidates,
        ft_decisions=ft_decisions,
        readiness_report=readiness_report,
        registry_stats=registry_stats,
        output_path=round_dir / "phase6_closed_loop_decision_conclusion.md",
    )
    log.info("  -> %s", round_dir / "phase6_closed_loop_decision_conclusion.md")

    # ── Manifest ──
    finished_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        "round_id": round_id,
        "phase": "phase6",
        "status": "succeeded",
        "started_at": started_at,
        "finished_at": finished_at,
        "scope": {
            "symbol": "BTC-USDT-SWAP",
            "families": ["independent", "directional"],
            "timeframes": ["15m", "1H"],
        },
        "evidence_completeness": completeness,
        "readiness": readiness_report["readiness"],
        "upgrade_candidates_count": len(upgrade_candidates),
        "ft_decisions_count": len(ft_decisions),
        "registry_stats": registry_stats,
        "input_refs": {
            "artifact_index": "artifacts/governance/artifact_index.json",
            "parameter_registry": "artifacts/governance/current_parameter_registry.json",
            "quality_monitor": "artifacts/governance/quality_monitor_summary.json",
        },
        "output_refs": {
            "evidence_summary": str(evidence_path.relative_to(round_dir)),
            "upgrade_candidates": str(candidates_path.relative_to(round_dir)),
            "ft_decisions": str(ft_path.relative_to(round_dir)),
            "readiness_report": str(readiness_path.relative_to(round_dir)),
            "conclusion": "phase6_closed_loop_decision_conclusion.md",
        },
        "code_version": None,
        "notes": None,
    }
    manifest_path = round_dir / "round_manifest.json"
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False, default=str)
    log.info("  -> %s", manifest_path)

    # ── Summary ──
    log.info("")
    log.info("=" * 60)
    log.info("Phase 6 Decision Round completed")
    log.info("  Round ID   : %s", round_id)
    log.info("  Readiness  : %s", readiness_report["readiness"])
    log.info("  Round dir  : %s", round_dir)
    log.info("=" * 60)

    if not args.no_print_summary:
        print()
        print(f"=== Phase 6 Decision Round: {round_id} ===")
        print(f"Readiness: {readiness_report['readiness']} ({readiness_report['overall_confidence']})")
        print()

        print("Parameter Upgrade Candidates:")
        for uc in upgrade_candidates:
            icon = {"promote_candidate": "UP", "hold": "--", "reject": "NO"}.get(uc["decision"], "??")
            print(f"  [{icon}] {uc['family']}/{uc['timeframe']}: "
                  f"{uc['decision']} (score={uc['score_ratio']:.3f})")

        print()
        print("Family/Timeframe Decisions:")
        for ftd in ft_decisions:
            icon = {
                "keep_active": "ACT",
                "lower_priority": "LOW",
                "pause": "PAU",
                "require_review": "REV",
            }.get(ftd["decision"], "???")
            print(f"  [{icon}] {ftd['combo_key']}: {ftd['decision']} ({ftd['confidence']})")

        print()
        print(f"Conclusion: {round_dir / 'phase6_closed_loop_decision_conclusion.md'}")
        print(f"Artifacts:  {round_dir}")


if __name__ == "__main__":
    main()
