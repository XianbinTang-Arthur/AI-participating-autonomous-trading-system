"""Attribution 报告生成器.

生成 Markdown 格式的 live_attribution_report 和 phase3 conclusion。
"""

from __future__ import annotations

import logging
import pathlib
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


def build_attribution_report(
    *,
    family: str,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    alignment_rows: list[dict[str, Any]],
    classified_rows: list[dict[str, Any]],
    summary: list[dict[str, Any]],
    top_failure_modes: dict[str, Any],
    layer_analysis: dict[str, dict[str, int]],
    output_path: pathlib.Path,
) -> pathlib.Path:
    """生成单次 live_attribution_report.md。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    _add = lines.append
    now_str = datetime.now(timezone.utc).isoformat()

    _add("# Live Attribution Report")
    _add("")
    _add(f"> Family: `{family}` | Symbol: `{symbol}` | Timeframe: `{timeframe}`")
    _add(f"> Window: {start} ~ {end}")
    _add(f"> Generated: {now_str}")
    _add("")

    # ---- 1. Alignment Overview ----
    _add("## 1. Alignment Overview")
    _add("")
    total = len(alignment_rows)
    aligned = sum(1 for r in alignment_rows if r.get("alignment_status") == "aligned")
    replay_only = sum(1 for r in alignment_rows if r.get("alignment_status") == "replay_only")
    live_only = sum(1 for r in alignment_rows if r.get("alignment_status") == "live_only")
    _add(f"- Total events: **{total}**")
    _add(f"- Aligned (replay + live match): **{aligned}**")
    _add(f"- Replay-only (no live match): **{replay_only}**")
    _add(f"- Live-only (no replay match): **{live_only}**")
    _add("")

    # Replay openings count
    replay_openings = sum(1 for r in alignment_rows if r.get("replay_opening"))
    replay_selectable = sum(1 for r in alignment_rows if r.get("replay_selectable"))
    _add(f"- Replay openings: **{replay_openings}**")
    _add(f"- Replay selectable bars: **{replay_selectable}**")
    _add("")

    # ---- 2. Top Failure Modes ----
    _add("## 2. Top Failure Modes")
    _add("")
    tfm = top_failure_modes
    _add(f"- Total failures: **{tfm.get('total_failures', 0)}** "
         f"({tfm.get('failure_ratio', 0):.1%} of all events)")
    _add(f"- Live traded (success): **{tfm.get('total_success', 0)}**")
    _add(f"- Not applicable: **{tfm.get('total_not_applicable', 0)}**")
    _add("")

    _add("### Top Categories")
    _add("")
    _add("| Category | Count | Ratio |")
    _add("|----------|-------|-------|")
    for tc in tfm.get("top_categories", []):
        _add(f"| `{tc['category']}` | {tc['count']} | {tc['ratio']:.1%} |")
    _add("")

    _add("### Top Reasons")
    _add("")
    _add("| Reason | Count | Ratio |")
    _add("|--------|-------|-------|")
    for tr in tfm.get("top_reasons", []):
        _add(f"| `{tr['reason']}` | {tr['count']} | {tr['ratio']:.1%} |")
    _add("")

    # ---- 3. Layer Analysis ----
    _add("## 3. Layer Analysis")
    _add("")
    _add("| Layer | Passed | Failed | Not Reached |")
    _add("|-------|--------|--------|-------------|")
    for layer_name, stats in layer_analysis.items():
        _add(f"| {layer_name} | {stats['passed']} | {stats['failed']} | {stats['not_reached']} |")
    _add("")

    # ---- 4. Key Findings ----
    _add("## 4. Key Findings")
    _add("")
    findings = _generate_findings(top_failure_modes, layer_analysis, family, timeframe)
    for f in findings:
        _add(f"- {f}")
    _add("")

    content = "\n".join(lines)
    output_path.write_text(content, encoding="utf-8")
    log.info("Wrote attribution report -> %s", output_path)
    return output_path


def build_phase3_conclusion(
    *,
    symbol: str,
    start: str,
    end: str,
    all_summaries: dict[str, list[dict[str, Any]]],
    all_failure_modes: dict[str, dict[str, Any]],
    all_layer_analyses: dict[str, dict[str, dict[str, int]]],
    all_alignment_stats: dict[str, dict[str, int]],
    round_id: str,
    output_path: pathlib.Path,
) -> pathlib.Path:
    """生成 phase3_live_attribution_conclusion.md。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    _add = lines.append
    now_str = datetime.now(timezone.utc).isoformat()

    _add("# Phase 3: Live Attribution Conclusion")
    _add("")
    _add(f"> Round ID: `{round_id}`")
    _add(f"> Generated: {now_str}")
    _add("")

    # ---- 1. Scope ----
    _add("## 1. Scope")
    _add("")
    _add(f"- **Symbol**: {symbol}")
    _add("- **Families**: independent, directional")
    _add("- **Timeframes**: 15m, 1H")
    _add(f"- **Window**: {start} ~ {end}")
    _add("")

    # ---- 2. What Was Aligned ----
    _add("## 2. What Was Aligned")
    _add("")
    _add("| Family/TF | Total | Aligned | Replay-Only | Live-Only |")
    _add("|-----------|-------|---------|-------------|-----------|")
    for ft_key, stats in all_alignment_stats.items():
        _add(f"| {ft_key} | {stats.get('total', 0)} | {stats.get('aligned', 0)} "
             f"| {stats.get('replay_only', 0)} | {stats.get('live_only', 0)} |")
    _add("")

    # ---- 3. Top Failure Modes (all combos) ----
    _add("## 3. Top Failure Modes")
    _add("")
    for ft_key, tfm in all_failure_modes.items():
        _add(f"### {ft_key}")
        _add("")
        _add(f"- Failures: **{tfm.get('total_failures', 0)}** / {tfm.get('total_events', 0)}")
        _add("")
        _add("| Category | Count | Reason | Count |")
        _add("|----------|-------|--------|-------|")
        cats = tfm.get("top_categories", [])[:5]
        reasons = tfm.get("top_reasons", [])[:5]
        max_len = max(len(cats), len(reasons))
        for i in range(max_len):
            cat_str = f"`{cats[i]['category']}` | {cats[i]['count']}" if i < len(cats) else "— | —"
            rea_str = f"`{reasons[i]['reason']}` | {reasons[i]['count']}" if i < len(reasons) else "— | —"
            _add(f"| {cat_str} | {rea_str} |")
        _add("")

    # ---- 4. Layer Analysis ----
    _add("## 4. Layer Analysis")
    _add("")
    for ft_key, la in all_layer_analyses.items():
        _add(f"### {ft_key}")
        _add("")
        _add("| Layer | Passed | Failed |")
        _add("|-------|--------|--------|")
        for layer_name, stats in la.items():
            _add(f"| {layer_name} | {stats['passed']} | {stats['failed']} |")
        _add("")

    # ---- 5. Family / Timeframe Differences ----
    _add("## 5. Family / Timeframe Differences")
    _add("")
    diffs = _generate_cross_comparison(all_failure_modes, all_layer_analyses)
    for d in diffs:
        _add(f"- {d}")
    if not diffs:
        _add("- 数据不足，无法进行交叉比较")
    _add("")

    # ---- 6. Key Findings ----
    _add("## 6. Key Findings")
    _add("")
    all_findings = _generate_global_findings(all_failure_modes, all_layer_analyses)
    for f in all_findings:
        _add(f"- {f}")
    if not all_findings:
        _add("- 数据不足，无法生成关键发现")
    _add("")

    # ---- 7. Next Steps ----
    _add("## 7. Next Steps")
    _add("")
    _add("- Phase 4: Execution realism (orderbook, trades, fill simulation)")
    _add("- 延长 attribution 时间窗口，追踪失败模式变化趋势")
    _add("- 对高频失败 category 做专项 deep-dive")
    _add("- 对比 live 调参前后的 attribution 分布变化")
    _add("")

    content = "\n".join(lines)
    output_path.write_text(content, encoding="utf-8")
    log.info("Wrote Phase 3 conclusion -> %s", output_path)
    return output_path


# =========================================================================
# 内部辅助
# =========================================================================


def _generate_findings(
    tfm: dict[str, Any],
    layer_analysis: dict[str, dict[str, int]],
    family: str,
    timeframe: str,
) -> list[str]:
    """基于数据生成关键发现。"""
    findings: list[str] = []

    top_cats = tfm.get("top_categories", [])
    if top_cats:
        top = top_cats[0]
        findings.append(
            f"{family}/{timeframe} 最主要的未交易原因是 "
            f"`{top['category']}` (占 {top['ratio']:.1%})"
        )

    # 检查各层失败占比
    risk_failed = layer_analysis.get("risk", {}).get("failed", 0)
    exec_failed = layer_analysis.get("execution", {}).get("failed", 0)

    if risk_failed > 0:
        findings.append(f"Risk 层有 {risk_failed} 次拦截，需关注 reconciliation / margin 状态")
    if exec_failed > 0:
        findings.append(f"Execution 层有 {exec_failed} 次拦截，bundle 审批可能存在瓶颈")

    total_failures = tfm.get("total_failures", 0)
    total_success = tfm.get("total_success", 0)
    if total_success == 0 and total_failures > 0:
        findings.append("当前窗口内 live 无任何成功交易，需排查系统级阻断原因")

    if not findings:
        findings.append("当前窗口数据不足以生成具体发现")

    return findings


def _generate_cross_comparison(
    all_failure_modes: dict[str, dict[str, Any]],
    all_layer_analyses: dict[str, dict[str, dict[str, int]]],
) -> list[str]:
    """跨 family/timeframe 比较。"""
    diffs: list[str] = []

    keys = list(all_failure_modes.keys())
    if len(keys) < 2:
        return diffs

    # 比较 independent vs directional
    ind_keys = [k for k in keys if k.startswith("independent")]
    dir_keys = [k for k in keys if k.startswith("directional")]

    if ind_keys and dir_keys:
        ind_failures = sum(
            all_failure_modes[k].get("total_failures", 0) for k in ind_keys
        )
        dir_failures = sum(
            all_failure_modes[k].get("total_failures", 0) for k in dir_keys
        )
        if ind_failures != dir_failures:
            leader = "independent" if ind_failures > dir_failures else "directional"
            diffs.append(
                f"{leader} 的失败次数更多 "
                f"(independent: {ind_failures}, directional: {dir_failures})"
            )

    # 比较 15m vs 1H
    _15m_keys = [k for k in keys if k.endswith("15m")]
    _1h_keys = [k for k in keys if k.endswith("1h") or k.endswith("1H")]

    if _15m_keys and _1h_keys:
        _15m_failures = sum(
            all_failure_modes[k].get("total_failures", 0) for k in _15m_keys
        )
        _1h_failures = sum(
            all_failure_modes[k].get("total_failures", 0) for k in _1h_keys
        )
        if _15m_failures != _1h_failures:
            more = "15m" if _15m_failures > _1h_failures else "1H"
            diffs.append(
                f"{more} 的总失败次数更多 (15m: {_15m_failures}, 1H: {_1h_failures})"
            )

    # 比较 top category 差异
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            ka, kb = keys[i], keys[j]
            tc_a = all_failure_modes[ka].get("top_categories", [])
            tc_b = all_failure_modes[kb].get("top_categories", [])
            if tc_a and tc_b:
                top_a = tc_a[0]["category"]
                top_b = tc_b[0]["category"]
                if top_a != top_b:
                    diffs.append(
                        f"{ka} 的首要失败是 `{top_a}`, "
                        f"而 {kb} 是 `{top_b}`"
                    )

    return diffs


def _generate_global_findings(
    all_failure_modes: dict[str, dict[str, Any]],
    all_layer_analyses: dict[str, dict[str, dict[str, int]]],
) -> list[str]:
    """全局发现。"""
    findings: list[str] = []

    # 汇总所有失败原因
    from collections import Counter
    global_reasons: Counter[str] = Counter()
    for tfm in all_failure_modes.values():
        for tr in tfm.get("top_reasons", []):
            global_reasons[tr["reason"]] += tr["count"]

    if global_reasons:
        top_reason, top_count = global_reasons.most_common(1)[0]
        findings.append(f"全局最常见的失败原因是 `{top_reason}` ({top_count} 次)")

    # 检查是否全部都是 strategy_blocked
    global_cats: Counter[str] = Counter()
    for tfm in all_failure_modes.values():
        for tc in tfm.get("top_categories", []):
            global_cats[tc["category"]] += tc["count"]

    total_strategy = global_cats.get("strategy_blocked", 0)
    total_all = sum(global_cats.values())
    if total_all > 0 and total_strategy / total_all > 0.8:
        findings.append(
            "80%+ 的失败集中在 strategy 层，说明主要瓶颈在信号端而非执行/风控端"
        )

    risk_count = global_cats.get("risk_rejected", 0)
    if risk_count > 0:
        findings.append(
            f"Risk 层拦截 {risk_count} 次，建议检查 reconciliation / only_reduce 状态"
        )

    return findings
