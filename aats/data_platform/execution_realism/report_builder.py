"""Execution Realism 报告生成器.

生成 Markdown 格式的:
  - live_execution_realism_report.md (单次 family/tf)
  - phase4_execution_realism_conclusion.md (全局汇总)
"""

from __future__ import annotations

import logging
import pathlib
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


def build_execution_realism_report(
    *,
    family: str,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    aligned_rows: list[dict[str, Any]],
    feasibility_rows: list[dict[str, Any]],
    slippage_rows: list[dict[str, Any]],
    cost_summary: dict[str, Any],
    output_path: pathlib.Path,
) -> pathlib.Path:
    """生成单次 execution realism report。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    _add = lines.append
    now_str = datetime.now(timezone.utc).isoformat()

    _add("# Execution Realism Report")
    _add("")
    _add(f"> Family: `{family}` | Symbol: `{symbol}` | Timeframe: `{timeframe}`")
    _add(f"> Window: {start} ~ {end}")
    _add(f"> Generated: {now_str}")
    _add(f"> Model: V1 Bar-Based Proxy (OHLCV only)")
    _add("")

    # ---- 1. Market Data Alignment ----
    _add("## 1. Market Data Alignment")
    _add("")
    total_aligned = len(aligned_rows)
    matched = sum(1 for r in aligned_rows if r.get("alignment_status") == "matched")
    no_data = total_aligned - matched
    openings = sum(1 for r in aligned_rows if r.get("candidate_action") == "open")
    closes = sum(1 for r in aligned_rows if r.get("candidate_action") == "close")
    _add(f"- Total candidates: **{total_aligned}**")
    _add(f"- Matched with bar data: **{matched}**")
    _add(f"- No bar data: **{no_data}**")
    _add(f"- Openings: **{openings}** | Closes: **{closes}**")
    _add("")

    # 市场条件概览
    bar_ranges = [r["bar_range_bps"] for r in aligned_rows if r.get("bar_range_bps") is not None]
    bar_volumes = [r["bar_volume"] for r in aligned_rows if r.get("bar_volume") is not None]
    if bar_ranges:
        avg_range = sum(bar_ranges) / len(bar_ranges)
        _add(f"- Average bar range: **{avg_range:.1f} bps**")
    if bar_volumes:
        avg_vol = sum(bar_volumes) / len(bar_volumes)
        _add(f"- Average bar volume: **{avg_vol:.0f} contracts**")
    _add("")

    # ---- 2. Fill Feasibility ----
    _add("## 2. Fill Feasibility")
    _add("")
    cs = cost_summary
    _add(f"- Fully fillable: **{cs.get('full_fill_count', 0)}** ({cs.get('full_fill_ratio', 0):.1%})")
    _add(f"- Partially fillable: **{cs.get('partial_fill_count', 0)}** ({cs.get('partial_fill_ratio', 0):.1%})")
    _add(f"- Not fillable: **{cs.get('not_fillable_count', 0)}** ({cs.get('not_fillable_ratio', 0):.1%})")
    _add(f"- Insufficient data: **{cs.get('insufficient_data_count', 0)}**")
    _add("")

    # Volume ratio 分布
    vol_ratios = [r["volume_ratio"] for r in feasibility_rows
                  if r.get("volume_ratio") is not None]
    if vol_ratios:
        avg_vr = sum(vol_ratios) / len(vol_ratios)
        max_vr = max(vol_ratios)
        _add(f"- Mean volume ratio: **{avg_vr:.6f}** (candidate / bar volume)")
        _add(f"- Max volume ratio: **{max_vr:.6f}**")
        _add("")
        _add("> **V1 注意**: 当前 BTC-USDT-SWAP 每笔 1 合约 (0.01 BTC)，"
             "volume ratio 极小，几乎全部 fully_fillable。"
             "当仓位规模增大时此指标更有意义。")
    _add("")

    # ---- 3. Slippage Analysis ----
    _add("## 3. Slippage Analysis")
    _add("")
    slip = cs.get("slippage", {})
    if slip:
        _add("| Metric | Value (bps) |")
        _add("|--------|------------|")
        _add(f"| Mean | {slip.get('mean', 0):.3f} |")
        _add(f"| Median | {slip.get('median', 0):.3f} |")
        _add(f"| P5 | {slip.get('p5', 0):.3f} |")
        _add(f"| P25 | {slip.get('p25', 0):.3f} |")
        _add(f"| P75 | {slip.get('p75', 0):.3f} |")
        _add(f"| P95 | {slip.get('p95', 0):.3f} |")
        _add(f"| Max | {slip.get('max', 0):.3f} |")
        _add("")

        # Slippage 分解
        half_spreads = [r["half_spread_bps"] for r in slippage_rows
                        if r.get("half_spread_bps") is not None]
        vol_impacts = [r["volume_impact_bps"] for r in slippage_rows
                       if r.get("volume_impact_bps") is not None]
        if half_spreads and vol_impacts:
            avg_hs = sum(half_spreads) / len(half_spreads)
            avg_vi = sum(vol_impacts) / len(vol_impacts)
            _add("### Slippage Decomposition")
            _add("")
            _add(f"- Average half-spread (proxy): **{avg_hs:.3f} bps**")
            _add(f"- Average volume impact: **{avg_vi:.3f} bps**")
            _add("")
    else:
        _add("- 无有效滑点数据")
        _add("")

    # ---- 4. Total Execution Cost ----
    _add("## 4. Total Execution Cost")
    _add("")
    tc = cs.get("total_execution_cost", {})
    if tc:
        _add("| Metric | Value (bps) |")
        _add("|--------|------------|")
        _add(f"| Mean | {tc.get('mean', 0):.3f} |")
        _add(f"| Median | {tc.get('median', 0):.3f} |")
        _add(f"| P95 | {tc.get('p95', 0):.3f} |")
        _add("")
        _add(f"> Total cost = estimated slippage + taker fee (5 bps)")
    else:
        _add("- 无有效成本数据")
    _add("")

    # ---- 5. Cost-Adjusted Edge ----
    _add("## 5. Cost-Adjusted Edge")
    _add("")
    edge = cs.get("cost_adjusted_edge", {})
    if edge:
        _add("| Metric | Value (bps) |")
        _add("|--------|------------|")
        _add(f"| Mean | {edge.get('mean', 0):.3f} |")
        _add(f"| Median | {edge.get('median', 0):.3f} |")
        _add(f"| P25 | {edge.get('p25', 0):.3f} |")
        _add(f"| P75 | {edge.get('p75', 0):.3f} |")
        _add("")
        pe = cs.get("positive_edge_ratio", 0)
        _add(f"- Positive edge ratio: **{pe:.1%}** "
             f"({cs.get('positive_edge_count', 0)}/{cs.get('positive_edge_count', 0) + cs.get('negative_edge_count', 0)})")
        _add("")

    # Phase 2 比较
    comp = cs.get("cost_comparison_with_phase2", {})
    if comp:
        _add("### Phase 2 Cost Assumption Comparison")
        _add("")
        _add(f"- {comp.get('interpretation', 'N/A')}")
        _add("")
    _add("")

    # ---- 6. Key Findings ----
    _add("## 6. Key Findings")
    _add("")
    findings = _generate_single_findings(cs, family, timeframe)
    for f in findings:
        _add(f"- {f}")
    if not findings:
        _add("- 数据不足，无法生成具体发现")
    _add("")

    content = "\n".join(lines)
    output_path.write_text(content, encoding="utf-8")
    log.info("Wrote execution realism report -> %s", output_path)
    return output_path


def build_phase4_conclusion(
    *,
    symbol: str,
    start: str,
    end: str,
    all_cost_summaries: dict[str, dict[str, Any]],
    comparison_rows: list[dict[str, Any]],
    cross_findings: list[str],
    round_id: str,
    output_path: pathlib.Path,
) -> pathlib.Path:
    """生成 Phase 4 conclusion document。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    _add = lines.append
    now_str = datetime.now(timezone.utc).isoformat()

    _add("# Phase 4: Execution Realism Conclusion")
    _add("")
    _add(f"> Round ID: `{round_id}`")
    _add(f"> Generated: {now_str}")
    _add(f"> Model: V1 Bar-Based Proxy (OHLCV only)")
    _add("")

    # ---- 1. Scope ----
    _add("## 1. Scope")
    _add("")
    _add(f"- **Symbol**: {symbol}")
    _add("- **Families**: independent, directional")
    _add("- **Timeframes**: 15m, 1H")
    _add(f"- **Window**: {start} ~ {end}")
    _add("- **Data source**: Gold OHLCV replay bars (volume, high-low range)")
    _add("")

    # ---- 2. What was analyzed ----
    _add("## 2. What Was Analyzed")
    _add("")
    _add("| Family/TF | Candidates | Openings | Closes | Matched |")
    _add("|-----------|-----------|----------|--------|---------|")
    for ft_key, cs in all_cost_summaries.items():
        total = cs.get("total_candidates", 0)
        opens = cs.get("total_openings", 0)
        closes = cs.get("total_closes", 0)
        matched = total - cs.get("insufficient_data_count", 0)
        _add(f"| {ft_key} | {total} | {opens} | {closes} | {matched} |")
    _add("")

    # ---- 3. Fill feasibility summary ----
    _add("## 3. Fill Feasibility Summary")
    _add("")
    _add("| Family/TF | Full Fill | Partial Fill | Not Fillable | No Data |")
    _add("|-----------|-----------|-------------|-------------|---------|")
    for ft_key, cs in all_cost_summaries.items():
        _add(f"| {ft_key} | {cs.get('full_fill_ratio', 0):.1%} "
             f"| {cs.get('partial_fill_ratio', 0):.1%} "
             f"| {cs.get('not_fillable_ratio', 0):.1%} "
             f"| {cs.get('insufficient_data_count', 0)} |")
    _add("")

    # ---- 4. Slippage summary ----
    _add("## 4. Slippage Summary")
    _add("")
    _add("| Family/TF | Mean (bps) | Median (bps) | P95 (bps) | Max (bps) |")
    _add("|-----------|-----------|-------------|----------|----------|")
    for ft_key, cs in all_cost_summaries.items():
        slip = cs.get("slippage", {})
        _add(f"| {ft_key} | {slip.get('mean', 0):.3f} "
             f"| {slip.get('median', 0):.3f} "
             f"| {slip.get('p95', 0):.3f} "
             f"| {slip.get('max', 0):.3f} |")
    _add("")

    # ---- 5. Cost-adjusted edge analysis ----
    _add("## 5. Cost-Adjusted Edge Analysis")
    _add("")
    _add("| Family/TF | Mean Edge (bps) | Positive Edge % | Cost Delta vs Phase 2 |")
    _add("|-----------|----------------|----------------|----------------------|")
    for ft_key, cs in all_cost_summaries.items():
        edge = cs.get("cost_adjusted_edge", {})
        pe_ratio = cs.get("positive_edge_ratio", 0)
        comp = cs.get("cost_comparison_with_phase2", {})
        delta = comp.get("mean_cost_delta_bps", 0)
        _add(f"| {ft_key} | {edge.get('mean', 0):.3f} "
             f"| {pe_ratio:.1%} "
             f"| {delta:+.3f} |")
    _add("")

    # ---- 6. Family / Timeframe Comparison ----
    _add("## 6. Family / Timeframe Comparison")
    _add("")
    if comparison_rows:
        _add("| Family | TF | Candidates | Full Fill % | Mean Slip | Mean Cost | Adj Edge | +Edge % |")
        _add("|--------|-----|-----------|-----------|----------|----------|---------|---------|")
        for row in comparison_rows:
            _add(f"| {row['family']} | {row['timeframe']} "
                 f"| {row['candidate_count']} "
                 f"| {row['full_fill_ratio']:.1%} "
                 f"| {row['mean_slippage_bps']:.3f} "
                 f"| {row['mean_total_execution_cost_bps']:.3f} "
                 f"| {row['cost_adjusted_edge_proxy']:.3f} "
                 f"| {row['positive_edge_ratio']:.1%} |")
        _add("")

    # ---- 7. Key Findings ----
    _add("## 7. Key Findings")
    _add("")
    all_findings = _generate_global_findings(all_cost_summaries)
    for f in cross_findings:
        _add(f"- {f}")
    for f in all_findings:
        _add(f"- {f}")
    if not cross_findings and not all_findings:
        _add("- 数据不足，无法生成关键发现")
    _add("")

    # ---- 8. Model Limitations ----
    _add("## 8. V1 Model Limitations")
    _add("")
    _add("- 无 orderbook depth 数据：spread 基于 bar range 粗略估计")
    _add("- 无 trades/tick 数据：无法验证实际成交序列")
    _add("- Volume 代理：bar volume 包含整个 bar 期间所有交易，非瞬时流动性")
    _add("- 仓位极小：BTC-USDT-SWAP 1 合约 = 0.01 BTC，volume ratio 接近 0")
    _add("- Square root impact 模型参数未经校准，后续需用真实数据回测")
    _add("")

    # ---- 9. Next Steps ----
    _add("## 9. Next Steps")
    _add("")
    _add("- 接入 OKX orderbook depth (`/api/v5/market/books-l2`) 实现真实 spread 估计")
    _add("- 接入 OKX trades (`/api/v5/market/trades`) 实现 fill simulation")
    _add("- 校准 impact model 参数（half_spread_fraction, impact_coefficient）")
    _add("- 用真实 fill 数据 (execution_fills) 做 slippage 回测")
    _add("- Execution-aware parameter selection（用 cost-adjusted edge 反向约束参数选择）")
    _add("- Phase 5: Platform governance / productionization")
    _add("")

    content = "\n".join(lines)
    output_path.write_text(content, encoding="utf-8")
    log.info("Wrote Phase 4 conclusion -> %s", output_path)
    return output_path


# =========================================================================
# 内部辅助
# =========================================================================


def _generate_single_findings(
    cs: dict[str, Any],
    family: str,
    timeframe: str,
) -> list[str]:
    """基于单次 cost summary 生成 key findings。"""
    findings: list[str] = []

    # 1. 成交可行性
    full_ratio = cs.get("full_fill_ratio", 0)
    if full_ratio > 0.95:
        findings.append(
            f"{family}/{timeframe}: 几乎所有候选订单均可完全成交 "
            f"(full fill ratio = {full_ratio:.1%})"
        )
    elif full_ratio < 0.5:
        findings.append(
            f"{family}/{timeframe}: 仅 {full_ratio:.1%} 的候选订单可完全成交，"
            "流动性可能不足"
        )

    # 2. 滑点水平
    slip = cs.get("slippage", {})
    mean_slip = slip.get("mean", 0)
    if mean_slip > 0:
        if mean_slip < 1.0:
            findings.append(
                f"平均滑点 {mean_slip:.2f} bps 较低，市场流动性充足"
            )
        elif mean_slip > 3.0:
            findings.append(
                f"平均滑点 {mean_slip:.2f} bps 偏高，需关注执行效率"
            )

    # 3. Cost vs Phase 2
    comp = cs.get("cost_comparison_with_phase2", {})
    if comp.get("cost_is_underestimated"):
        findings.append(
            f"Phase 2 默认 cost 被低估 {comp.get('mean_cost_delta_bps', 0):.2f} bps，"
            "建议上调默认滑点假设"
        )
    elif comp.get("cost_is_overestimated"):
        findings.append(
            f"Phase 2 默认 cost 被高估 {abs(comp.get('mean_cost_delta_bps', 0)):.2f} bps，"
            "当前保守假设仍有安全余量"
        )

    # 4. Positive edge ratio
    pe_ratio = cs.get("positive_edge_ratio", 0)
    total = cs.get("total_candidates", 0)
    if total > 0 and pe_ratio < 0.5:
        findings.append(
            f"成本调整后仅 {pe_ratio:.0%} 的机会有正 edge，"
            "execution cost 显著侵蚀策略价值"
        )
    elif total > 0 and pe_ratio > 0.8:
        findings.append(
            f"成本调整后 {pe_ratio:.0%} 的机会仍有正 edge，"
            "策略信号在 execution realism 下依然有效"
        )

    return findings


def _generate_global_findings(
    all_cost_summaries: dict[str, dict[str, Any]],
) -> list[str]:
    """从全局 cost summaries 生成 findings。"""
    findings: list[str] = []

    if not all_cost_summaries:
        return findings

    # 全局平均 slippage
    all_slippages: list[float] = []
    for cs in all_cost_summaries.values():
        slip = cs.get("slippage", {})
        if slip.get("mean") is not None:
            all_slippages.append(slip["mean"])

    if all_slippages:
        global_avg = sum(all_slippages) / len(all_slippages)
        findings.append(
            f"全局平均滑点为 {global_avg:.2f} bps "
            f"(Phase 2 默认滑点假设为 2.0 bps)"
        )

    # 全局 positive edge ratio
    total_pos = sum(cs.get("positive_edge_count", 0) for cs in all_cost_summaries.values())
    total_neg = sum(cs.get("negative_edge_count", 0) for cs in all_cost_summaries.values())
    total_all = total_pos + total_neg
    if total_all > 0:
        global_pe = total_pos / total_all
        findings.append(
            f"全局 {global_pe:.0%} 的候选机会在成本调整后仍有正 edge "
            f"({total_pos}/{total_all})"
        )

    return findings
