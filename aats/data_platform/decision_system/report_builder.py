"""Phase 6 结论文档生成.

生成 phase6_closed_loop_decision_conclusion.md。
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timezone
from typing import Any


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v:.1%}"


def _fmt_bps(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v:.2f} bps"


def build_phase6_conclusion(
    *,
    round_id: str,
    evidence_bundle: dict[str, Any],
    upgrade_candidates: list[dict[str, Any]],
    ft_decisions: list[dict[str, Any]],
    readiness_report: dict[str, Any],
    registry_stats: dict[str, int],
    output_path: pathlib.Path,
) -> None:
    """生成 Phase 6 闭环决策结论文档."""
    now = datetime.now(timezone.utc).isoformat()
    completeness = evidence_bundle.get("evidence_completeness", {})

    lines: list[str] = []
    w = lines.append

    w("# Phase 6 Closed-Loop Decision Conclusion")
    w("")
    w(f"**Round ID**: `{round_id}`")
    w(f"**Generated**: {now}")
    w("")

    # ── 1. Scope ──
    w("## 1. Scope")
    w("")
    w("- **Symbol**: BTC-USDT-SWAP")
    w("- **Families**: independent, directional")
    w("- **Timeframes**: 15m, 1H")
    w(f"- **Evidence phases**: {', '.join(completeness.get('phases_with_data', []))}")
    w(f"- **Completeness**: {_fmt_pct(completeness.get('completeness_ratio'))}")
    w("")

    # ── 2. Evidence Summary ──
    w("## 2. Evidence Summary")
    w("")

    p2 = evidence_bundle.get("phase2_evidence", {})
    p2_agg = p2.get("aggregate_stats", {})
    w("### Phase 2 (Research)")
    w(f"- Experiments: {p2.get('experiment_count', 0)}")
    w(f"- Parameter scans: {p2.get('parameter_scan_count', 0)}")
    w(f"- Experiments with openings: {p2_agg.get('experiments_with_openings', 0)}")
    w(f"- Mean positive edge ratio: {_fmt_pct(p2_agg.get('mean_positive_edge_ratio'))}")
    w("")

    p3 = evidence_bundle.get("phase3_evidence", {})
    w("### Phase 3 (Attribution)")
    w(f"- Rounds: {p3.get('round_count', 0)}")
    latest3 = p3.get("latest_round")
    if latest3:
        w(f"- Latest round: `{latest3.get('round_id')}` (status: {latest3.get('status')})")
    else:
        w("- Latest round: (none)")
    w("")

    p4 = evidence_bundle.get("phase4_evidence", {})
    w("### Phase 4 (Execution Realism)")
    w(f"- Rounds: {p4.get('round_count', 0)}")
    latest4 = p4.get("latest_round")
    if latest4:
        w(f"- Latest round: `{latest4.get('round_id')}` (status: {latest4.get('status')})")
    else:
        w("- Latest round: (none)")
    w("")

    p5 = evidence_bundle.get("phase5_governance_evidence", {})
    w("### Phase 5 (Governance)")
    w(f"- Quality health: {p5.get('quality_health', 'unknown')}")
    w(f"- Frozen parameter sets: {len(p5.get('frozen_parameter_sets', []))}")
    w(f"- Candidate parameter sets: {len(p5.get('candidate_parameter_sets', []))}")
    w(f"- Total artifacts indexed: {p5.get('total_artifacts', 0)}")
    w("")

    # ── 3. Parameter Upgrade Candidates ──
    w("## 3. Parameter Upgrade Candidates")
    w("")

    if upgrade_candidates:
        w("| Parameter Set | Family | Timeframe | Decision | Confidence | Score |")
        w("|:---|:---|:---|:---|:---|---:|")
        for uc in upgrade_candidates:
            w(f"| `{uc['parameter_set_id'][:20]}...` | {uc['family']} | {uc['timeframe']} | **{uc['decision']}** | {uc['confidence']} | {uc['score_ratio']:.3f} |")
        w("")

        # 详细评分
        for uc in upgrade_candidates:
            w(f"### {uc['family']} / {uc['timeframe']}: {uc['decision']}")
            w("")
            for ds in uc.get("dimension_scores", []):
                score_str = f"{ds['score']:.1f}/{ds['max_score']:.1f}"
                w(f"**{ds['dimension']}** ({score_str}):")
                for d in ds.get("details", []):
                    w(f"- {d}")
                w("")
    else:
        w("(no parameter sets evaluated)")
        w("")

    # ── 4. Family / Timeframe Decisions ──
    w("## 4. Family / Timeframe Decisions")
    w("")

    if ft_decisions:
        w("| Combo | Decision | Confidence | Positive | Negative | Absent |")
        w("|:---|:---|:---|---:|---:|---:|")
        for ftd in ft_decisions:
            ss = ftd.get("signal_summary", {})
            w(f"| {ftd['combo_key']} | **{ftd['decision']}** | {ftd['confidence']} | {ss.get('positive', 0)} | {ss.get('negative', 0)} | {ss.get('absent', 0)} |")
        w("")

        for ftd in ft_decisions:
            w(f"### {ftd['combo_key']}: {ftd['decision']}")
            w("")
            w("**Reasons:**")
            for r in ftd.get("reasons", []):
                w(f"- {r}")
            w("")
            w("**Signals:**")
            for s in ftd.get("signals", []):
                icon = {"positive": "+", "negative": "-", "severe_negative": "!!", "neutral": "~", "absent": "?"}.get(s["signal"], "?")
                w(f"- [{icon}] [{s['source']}] {s['detail']}")
            w("")
    else:
        w("(no decisions)")
        w("")

    # ── 5. Promotion Readiness ──
    w("## 5. Promotion Readiness")
    w("")

    readiness = readiness_report.get("readiness", "unknown")
    conf = readiness_report.get("overall_confidence", "unknown")
    w(f"**Readiness**: `{readiness}`")
    w(f"**Confidence**: {conf}")
    w(f"**Checks**: {readiness_report.get('checks_passed', 0)}/{readiness_report.get('checks_total', 0)} passed")
    w("")

    blockers = readiness_report.get("blockers", [])
    if blockers:
        w("### Blockers")
        for b in blockers:
            w(f"- {b}")
        w("")

    w("### Check Details")
    for c in readiness_report.get("checks", []):
        icon = "PASS" if c["passed"] else "FAIL"
        w(f"- [{icon}] {c['check']}: {c['detail']}")
    w("")

    # ── 6. Governance Notes ──
    w("## 6. Governance Notes")
    w("")
    w(f"- Recommendations added: {registry_stats.get('recommendations_added', 0)}")
    w(f"- Decisions updated: {registry_stats.get('decisions_updated', 0)}")
    w(f"- Evidence bundles registered: {registry_stats.get('bundles_registered', 0)}")
    w("")

    # ── 7. Next Steps ──
    w("## 7. Next Steps")
    w("")

    if readiness == "ready_for_next_live_test":
        promoted = [uc for uc in upgrade_candidates if uc.get("decision") == "promote_candidate"]
        if promoted:
            w("**建议进入下一轮 live test：**")
            for p in promoted:
                w(f"- {p['family']} / {p['timeframe']}: `{p['parameter_set_id']}`")
        else:
            w("证据充足但无参数达到 promote 标准，建议重新运行 Step 2 研究。")
    elif readiness == "not_ready_more_research_needed":
        w("**需要更多研究：** 回到 Phase 2 Step 1/2 继续实验。")
    elif readiness == "not_ready_attribution_issue":
        w("**归因问题：** 回到 Phase 3 排查 replay/live 差异。")
    elif readiness == "not_ready_execution_issue":
        w("**执行问题：** 回到 Phase 4 改善 execution realism 假设或策略。")
    elif readiness == "not_ready_governance_issue":
        w("**治理问题：** 运行 Phase 5 质量巡检并修复问题。")
    else:
        w("状态未知，建议人工审查所有证据。")

    w("")
    w("---")
    w("")
    w("*Generated by Phase 6 Closed-Loop Decision System*")

    # 写文件
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
