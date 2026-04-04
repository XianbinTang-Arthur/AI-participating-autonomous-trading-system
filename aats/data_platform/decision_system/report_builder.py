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

    w(f"# Phase 6 Closed-Loop Decision Conclusion")
    w(f"")
    w(f"**Round ID**: `{round_id}`")
    w(f"**Generated**: {now}")
    w(f"")

    # ── 1. Scope ──
    w(f"## 1. Scope")
    w(f"")
    w(f"- **Symbol**: BTC-USDT-SWAP")
    w(f"- **Families**: independent, directional")
    w(f"- **Timeframes**: 15m, 1H")
    w(f"- **Evidence phases**: {', '.join(completeness.get('phases_with_data', []))}")
    w(f"- **Completeness**: {_fmt_pct(completeness.get('completeness_ratio'))}")
    w(f"")

    # ── 2. Evidence Summary ──
    w(f"## 2. Evidence Summary")
    w(f"")

    p2 = evidence_bundle.get("phase2_evidence", {})
    p2_agg = p2.get("aggregate_stats", {})
    w(f"### Phase 2 (Research)")
    w(f"- Experiments: {p2.get('experiment_count', 0)}")
    w(f"- Parameter scans: {p2.get('parameter_scan_count', 0)}")
    w(f"- Experiments with openings: {p2_agg.get('experiments_with_openings', 0)}")
    w(f"- Mean positive edge ratio: {_fmt_pct(p2_agg.get('mean_positive_edge_ratio'))}")
    w(f"")

    p3 = evidence_bundle.get("phase3_evidence", {})
    w(f"### Phase 3 (Attribution)")
    w(f"- Rounds: {p3.get('round_count', 0)}")
    latest3 = p3.get("latest_round")
    if latest3:
        w(f"- Latest round: `{latest3.get('round_id')}` (status: {latest3.get('status')})")
    else:
        w(f"- Latest round: (none)")
    w(f"")

    p4 = evidence_bundle.get("phase4_evidence", {})
    w(f"### Phase 4 (Execution Realism)")
    w(f"- Rounds: {p4.get('round_count', 0)}")
    latest4 = p4.get("latest_round")
    if latest4:
        w(f"- Latest round: `{latest4.get('round_id')}` (status: {latest4.get('status')})")
    else:
        w(f"- Latest round: (none)")
    w(f"")

    p5 = evidence_bundle.get("phase5_governance_evidence", {})
    w(f"### Phase 5 (Governance)")
    w(f"- Quality health: {p5.get('quality_health', 'unknown')}")
    w(f"- Frozen parameter sets: {len(p5.get('frozen_parameter_sets', []))}")
    w(f"- Candidate parameter sets: {len(p5.get('candidate_parameter_sets', []))}")
    w(f"- Total artifacts indexed: {p5.get('total_artifacts', 0)}")
    w(f"")

    # ── 3. Parameter Upgrade Candidates ──
    w(f"## 3. Parameter Upgrade Candidates")
    w(f"")

    if upgrade_candidates:
        w(f"| Parameter Set | Family | Timeframe | Decision | Confidence | Score |")
        w(f"|:---|:---|:---|:---|:---|---:|")
        for uc in upgrade_candidates:
            w(f"| `{uc['parameter_set_id'][:20]}...` | {uc['family']} | {uc['timeframe']} | **{uc['decision']}** | {uc['confidence']} | {uc['score_ratio']:.3f} |")
        w(f"")

        # 详细评分
        for uc in upgrade_candidates:
            w(f"### {uc['family']} / {uc['timeframe']}: {uc['decision']}")
            w(f"")
            for ds in uc.get("dimension_scores", []):
                score_str = f"{ds['score']:.1f}/{ds['max_score']:.1f}"
                w(f"**{ds['dimension']}** ({score_str}):")
                for d in ds.get("details", []):
                    w(f"- {d}")
                w(f"")
    else:
        w(f"(no parameter sets evaluated)")
        w(f"")

    # ── 4. Family / Timeframe Decisions ──
    w(f"## 4. Family / Timeframe Decisions")
    w(f"")

    if ft_decisions:
        w(f"| Combo | Decision | Confidence | Positive | Negative | Absent |")
        w(f"|:---|:---|:---|---:|---:|---:|")
        for ftd in ft_decisions:
            ss = ftd.get("signal_summary", {})
            w(f"| {ftd['combo_key']} | **{ftd['decision']}** | {ftd['confidence']} | {ss.get('positive', 0)} | {ss.get('negative', 0)} | {ss.get('absent', 0)} |")
        w(f"")

        for ftd in ft_decisions:
            w(f"### {ftd['combo_key']}: {ftd['decision']}")
            w(f"")
            w(f"**Reasons:**")
            for r in ftd.get("reasons", []):
                w(f"- {r}")
            w(f"")
            w(f"**Signals:**")
            for s in ftd.get("signals", []):
                icon = {"positive": "+", "negative": "-", "severe_negative": "!!", "neutral": "~", "absent": "?"}.get(s["signal"], "?")
                w(f"- [{icon}] [{s['source']}] {s['detail']}")
            w(f"")
    else:
        w(f"(no decisions)")
        w(f"")

    # ── 5. Promotion Readiness ──
    w(f"## 5. Promotion Readiness")
    w(f"")

    readiness = readiness_report.get("readiness", "unknown")
    conf = readiness_report.get("overall_confidence", "unknown")
    w(f"**Readiness**: `{readiness}`")
    w(f"**Confidence**: {conf}")
    w(f"**Checks**: {readiness_report.get('checks_passed', 0)}/{readiness_report.get('checks_total', 0)} passed")
    w(f"")

    blockers = readiness_report.get("blockers", [])
    if blockers:
        w(f"### Blockers")
        for b in blockers:
            w(f"- {b}")
        w(f"")

    w(f"### Check Details")
    for c in readiness_report.get("checks", []):
        icon = "PASS" if c["passed"] else "FAIL"
        w(f"- [{icon}] {c['check']}: {c['detail']}")
    w(f"")

    # ── 6. Governance Notes ──
    w(f"## 6. Governance Notes")
    w(f"")
    w(f"- Recommendations added: {registry_stats.get('recommendations_added', 0)}")
    w(f"- Decisions updated: {registry_stats.get('decisions_updated', 0)}")
    w(f"- Evidence bundles registered: {registry_stats.get('bundles_registered', 0)}")
    w(f"")

    # ── 7. Next Steps ──
    w(f"## 7. Next Steps")
    w(f"")

    if readiness == "ready_for_next_live_test":
        promoted = [uc for uc in upgrade_candidates if uc.get("decision") == "promote_candidate"]
        if promoted:
            w(f"**建议进入下一轮 live test：**")
            for p in promoted:
                w(f"- {p['family']} / {p['timeframe']}: `{p['parameter_set_id']}`")
        else:
            w(f"证据充足但无参数达到 promote 标准，建议重新运行 Step 2 研究。")
    elif readiness == "not_ready_more_research_needed":
        w(f"**需要更多研究：** 回到 Phase 2 Step 1/2 继续实验。")
    elif readiness == "not_ready_attribution_issue":
        w(f"**归因问题：** 回到 Phase 3 排查 replay/live 差异。")
    elif readiness == "not_ready_execution_issue":
        w(f"**执行问题：** 回到 Phase 4 改善 execution realism 假设或策略。")
    elif readiness == "not_ready_governance_issue":
        w(f"**治理问题：** 运行 Phase 5 质量巡检并修复问题。")
    else:
        w(f"状态未知，建议人工审查所有证据。")

    w(f"")
    w(f"---")
    w(f"")
    w(f"*Generated by Phase 6 Closed-Loop Decision System*")

    # 写文件
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
