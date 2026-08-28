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
    2 = 输入/环境模式不合法
    3 = managed control-plane 发布失败（整事务回滚）
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import re
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
_DECISION_RESULT_PREFIX = "RDP_DECISION_RESULT_JSON="
_ROUND_ID_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{8}$")
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
    planned_registry_stats,
    publish_managed_decision_round,
    update_registries_from_round,
)
from aats.data_platform.decision_system.report_builder import (
    build_phase6_conclusion,
)
from aats.data_platform.governance._db_util import (
    has_explicit_governance_db_configuration,
)
from aats.data_platform.governance.parameter_registry import load_registry


def _research_outcome_from_readiness(readiness: str) -> str:
    return {
        "ready_for_next_live_test": "eligible",
        "not_ready_attribution_issue": "blocked_by_attribution",
        "not_ready_execution_issue": "blocked_by_execution",
        "not_ready_governance_issue": "not_eligible",
        "not_ready_more_research_needed": "not_eligible",
    }.get(str(readiness or ""), "inconclusive")


def _emit_decision_result(
    *,
    round_id: str,
    readiness_report: dict,
    upgrade_candidates: list[dict],
    ft_decisions: list[dict],
) -> None:
    readiness = str(readiness_report.get("readiness") or "unknown")
    decision_counts: dict[str, int] = {}
    for decision in ft_decisions:
        key = str(decision.get("decision") or "unknown")
        decision_counts[key] = decision_counts.get(key, 0) + 1
    payload = {
        "round_id": round_id,
        "readiness": readiness,
        "research_outcome": _research_outcome_from_readiness(readiness),
        "overall_confidence": readiness_report.get("overall_confidence"),
        "checks_passed": readiness_report.get("checks_passed"),
        "checks_total": readiness_report.get("checks_total"),
        "blockers": list(readiness_report.get("blockers") or []),
        "promote_candidate_count": sum(
            1
            for candidate in upgrade_candidates
            if candidate.get("decision") == "promote_candidate"
        ),
        "decision_counts": decision_counts,
    }
    print(
        _DECISION_RESULT_PREFIX
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        flush=True,
    )


def _load_decision_parameter_sets(
    project_root: pathlib.Path,
    *,
    include_draft: bool,
    offline_only: bool = False,
) -> list[dict]:
    """从 DB-first registry 读取本轮允许评估的参数集.

    JSON 文件只是审计副本，容器重建后可能不存在；不能在调用
    ``load_registry`` 前用文件存在性短路 DB 真源。
    """
    registry_path = project_root / "artifacts/governance/current_parameter_registry.json"
    registry = (
        load_registry(registry_path, skip_db=True)
        if offline_only
        else load_registry(registry_path)
    )
    allowed_statuses = {"frozen", "candidate"}
    if include_draft:
        allowed_statuses.add("draft")
    return [
        parameter_set
        for parameter_set in registry.get("parameter_sets", [])
        if parameter_set.get("status") in allowed_statuses
    ]


def _validate_expected_research_rounds(
    evidence_bundle: dict,
    *,
    expected_step2_round_id: str | None,
    expected_phase3_round_id: str | None,
    expected_phase4_round_id: str | None,
) -> None:
    expected_research = {
        "phase3_evidence": expected_phase3_round_id,
        "phase4_evidence": expected_phase4_round_id,
    }
    provided = [
        expected_step2_round_id is not None,
        *(round_id is not None for round_id in expected_research.values()),
    ]
    if any(provided) and not all(provided):
        raise ValueError("expected_research_round_chain_required")
    if expected_step2_round_id is not None:
        if _ROUND_ID_RE.fullmatch(expected_step2_round_id) is None:
            raise ValueError("expected_research_round_id_invalid")
        phase2_evidence = evidence_bundle.get("phase2_evidence")
        if (
            not isinstance(phase2_evidence, dict)
            or phase2_evidence.get("round_selection_error") is not None
            or phase2_evidence.get("canonical_step2_round_id")
            != expected_step2_round_id
            or not isinstance(
                phase2_evidence.get("canonical_step2_snapshot_sha256"),
                str,
            )
        ):
            raise ValueError("phase2_evidence_expected_round_mismatch")
    for evidence_key, expected_round_id in expected_research.items():
        if expected_round_id is None:
            continue
        if _ROUND_ID_RE.fullmatch(expected_round_id) is None:
            raise ValueError("expected_research_round_id_invalid")
        phase_evidence = evidence_bundle.get(evidence_key)
        latest = (
            phase_evidence.get("latest_round")
            if isinstance(phase_evidence, dict)
            else None
        )
        if not isinstance(latest, dict) or latest.get("round_id") != expected_round_id:
            raise ValueError(f"{evidence_key}_expected_round_mismatch")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Phase 6: Closed-Loop Decision Round",
    )
    parser.add_argument("--artifact-root", default=str(_PROJECT_ROOT))
    parser.add_argument("--include-draft", action="store_true",
                        help="同时评估 draft 状态的参数集（默认只看 frozen + candidate）")
    parser.add_argument("--no-print-summary", action="store_true")
    parser.add_argument(
        "--expected-step2-round-id",
        help="本轮必须精确消费且与 Step 3 父身份一致的 Step 2 round ID",
    )
    parser.add_argument(
        "--expected-phase3-round-id",
        help="本轮必须精确消费的 Phase 3 round ID（与 Phase 4 参数成对使用）",
    )
    parser.add_argument(
        "--expected-phase4-round-id",
        help="本轮必须精确消费的 Phase 4 round ID（与 Phase 3 参数成对使用）",
    )
    parser.add_argument(
        "--offline-file-mode",
        action="store_true",
        help=(
            "仅用于无 managed governance DB 的显式离线开发；"
            "不生成 DB 控制面真值"
        ),
    )
    args = parser.parse_args()

    project_root = pathlib.Path(args.artifact_root)
    if args.offline_file_mode and has_explicit_governance_db_configuration(
        project_root
    ):
        log.error(
            "Offline file mode denied: managed governance DB is configured"
        )
        return 2
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
    evidence_bundle = build_evidence_bundle(
        project_root,
        expected_step2_round_id=args.expected_step2_round_id,
        expected_phase3_round_id=args.expected_phase3_round_id,
        expected_phase4_round_id=args.expected_phase4_round_id,
        expected_symbol="BTC-USDT-SWAP",
    )
    try:
        _validate_expected_research_rounds(
            evidence_bundle,
            expected_step2_round_id=args.expected_step2_round_id,
            expected_phase3_round_id=args.expected_phase3_round_id,
            expected_phase4_round_id=args.expected_phase4_round_id,
        )
    except ValueError as exc:
        log.error("Research round binding failed: %s", exc)
        return 2
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
    # 默认只评估治理确认的参数集（frozen + candidate），
    # draft 需要显式 --include-draft 才纳入
    allowed_statuses = {"frozen", "candidate"}
    if args.include_draft:
        allowed_statuses.add("draft")
    parameter_sets = _load_decision_parameter_sets(
        project_root,
        include_draft=args.include_draft,
        offline_only=args.offline_file_mode,
    )
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

    # ── Step 5: Prepare one publication ──
    log.info("")
    log.info("[5/6] Preparing control-plane publication...")
    registry_stats = planned_registry_stats(
        upgrade_candidates=upgrade_candidates,
        ft_decisions=ft_decisions,
    )

    # ── Step 6: Conclusion Document ──
    log.info("")
    log.info("[6/6] Building conclusion document...")

    conclusion_path = round_dir / "phase6_closed_loop_decision_conclusion.md"
    build_phase6_conclusion(
        round_id=round_id,
        evidence_bundle=evidence_bundle,
        upgrade_candidates=upgrade_candidates,
        ft_decisions=ft_decisions,
        readiness_report=readiness_report,
        registry_stats=registry_stats,
        output_path=conclusion_path,
    )
    log.info("  -> %s", conclusion_path)

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
        "publication_mode": (
            "offline_file_only"
            if args.offline_file_mode
            else "managed_db_atomic"
        ),
        "notes": (
            "Explicit offline development artifact; no managed DB truth was published."
            if args.offline_file_mode
            else None
        ),
    }

    # The succeeded manifest and machine-readable result marker are published
    # only after the complete control-plane operation succeeds.  A managed DB
    # outage or any child write failure therefore produces a non-zero process
    # with no succeeded round manifest and no active-decision partial commit.
    try:
        if args.offline_file_mode:
            registry_stats = update_registries_from_round(
                round_id=round_id,
                upgrade_candidates=upgrade_candidates,
                ft_decisions=ft_decisions,
                evidence_bundle=evidence_bundle,
                rec_registry_path=(
                    project_root
                    / "artifacts/decision_system/recommendation_registry.json"
                ),
                decision_registry_path=(
                    project_root
                    / "artifacts/decision_system/active_decision_registry.json"
                ),
                bundle_index_path=(
                    project_root
                    / "artifacts/decision_system/evidence_bundle_index.json"
                ),
                evidence_summary_path=str(evidence_path),
                offline_only=True,
            )
        else:
            registry_stats = publish_managed_decision_round(
                round_id=round_id,
                started_at=started_at,
                finished_at=finished_at,
                upgrade_candidates=upgrade_candidates,
                ft_decisions=ft_decisions,
                evidence_bundle=evidence_bundle,
                evidence_summary_path=str(evidence_path),
                readiness_report=readiness_report,
                manifest=manifest,
                conclusion_markdown=conclusion_path.read_text(encoding="utf-8"),
            )
    except Exception as exc:
        # Driver exception text can contain connection metadata.  The stable
        # failure type is enough for operators; detailed diagnostics remain in
        # controlled DB/service logs.
        log.error(
            "Decision round publication failed closed (failure_type=%s)",
            type(exc).__name__,
        )
        return 3

    if registry_stats != manifest["registry_stats"]:
        log.error("Decision round publication count mismatch; refusing manifest")
        return 3
    log.info("  Recommendations added: %d", registry_stats["recommendations_added"])
    log.info("  Decisions updated: %d", registry_stats["decisions_updated"])

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
    log.info("  State      : recommendations generated only; live parameters not applied")
    log.info("  Round dir  : %s", round_dir)
    log.info("=" * 60)

    _emit_decision_result(
        round_id=round_id,
        readiness_report=readiness_report,
        upgrade_candidates=upgrade_candidates,
        ft_decisions=ft_decisions,
    )

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
        print("State: recommendations generated only; live parameters were not applied")

    return 0


if __name__ == "__main__":
    sys.exit(main())
