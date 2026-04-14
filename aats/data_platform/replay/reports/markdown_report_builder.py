"""Markdown report builder: generate human-readable research reports.

Phase 2 设计决策 §13：
- 把每次 experiment 的结果转成可交付、可阅读的研究报告
- 必须支持 Markdown report, JSON summary, CSV summary
- Report Builder 不做 dashboard，不做交互式 UI

每份报告至少包含（§13.3）：
- experiment 基本信息 (family, symbol, timeframe, dataset_version, parameter_overrides)
- opening / blocked / selectable 统计
- blocking reasons top N
- edge summary
- 核心结论
- caveats
"""

from __future__ import annotations

import logging
import pathlib
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)


def build_experiment_report(
    *,
    experiment_info: dict[str, Any],
    diagnostics: dict[str, Any],
    output_path: pathlib.Path,
) -> pathlib.Path:
    """为单个 experiment 生成 Markdown 报告。

    参数:
        experiment_info: 实验元数据（来自 registry 或 caller）
        diagnostics: 诊断结果（来自 compute_diagnostics）
        output_path: 报告文件输出路径
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    _add = lines.append

    # --- Header ---
    _add("# Replay Experiment Report")
    _add("")
    _add(f"> Generated at: {datetime.utcnow().isoformat()}Z")
    _add("")

    # --- 基本信息 ---
    _add("## 1. Experiment Overview")
    _add("")
    _add("| Field | Value |")
    _add("|-------|-------|")
    _add(f"| Experiment ID | `{experiment_info.get('experiment_id', 'N/A')}` |")
    _add(f"| Family | {experiment_info.get('family', 'N/A')} |")
    _add(f"| Symbol | {experiment_info.get('symbol', 'N/A')} |")
    _add(f"| Timeframe | {experiment_info.get('timeframe', 'N/A')} |")
    _add(f"| Dataset Version | {experiment_info.get('dataset_version', 'N/A')} |")
    _add(f"| Window | {experiment_info.get('window_start_ts', '?')} ~ {experiment_info.get('window_end_ts', '?')} |")
    _add(f"| Status | {experiment_info.get('status', 'N/A')} |")
    _add("")

    # --- 参数覆盖 ---
    params = experiment_info.get("parameter_overrides", {})
    if isinstance(params, str):
        import json
        params = json.loads(params)
    _add("## 2. Parameter Overrides")
    _add("")
    _add("| Parameter | Value |")
    _add("|-----------|-------|")
    for k, v in sorted(params.items()):
        _add(f"| `{k}` | {v} |")
    _add("")

    # --- 统计概览 ---
    _add("## 3. Decision Statistics")
    _add("")
    total = diagnostics.get("total_bars", 0)
    _add(f"- **Total bars**: {total}")
    _add(f"- **Opening count**: {diagnostics.get('opening_count', 0)}")
    _add(f"- **Blocked count**: {diagnostics.get('blocked_count', 0)}")
    _add(f"- **Hold count**: {diagnostics.get('hold_count', 0)}")
    _add(f"- **Close count**: {diagnostics.get('close_count', 0)}")
    _add(f"- **Selectable ratio**: {_pct(diagnostics.get('selectable_ratio', 0))}")
    _add(f"- **Execution compatible ratio**: {_pct(diagnostics.get('execution_compatible_ratio', 0))}")
    _add("")

    # --- 状态分布 ---
    state_dist = diagnostics.get("state_distribution", {})
    if state_dist:
        _add("### State Distribution")
        _add("")
        _add("| State | Count | Ratio |")
        _add("|-------|-------|-------|")
        for s, c in sorted(state_dist.items(), key=lambda x: -x[1]):
            ratio = c / total if total > 0 else 0
            _add(f"| {s} | {c} | {_pct(ratio)} |")
        _add("")

    # --- 动作分布 ---
    action_dist = diagnostics.get("action_distribution", {})
    if action_dist:
        _add("### Action Distribution")
        _add("")
        _add("| Action | Count | Ratio |")
        _add("|--------|-------|-------|")
        for a, c in sorted(action_dist.items(), key=lambda x: -x[1]):
            ratio = c / total if total > 0 else 0
            _add(f"| {a} | {c} | {_pct(ratio)} |")
        _add("")

    # --- Blocking Reasons ---
    top_reasons = diagnostics.get("top_blocking_reasons", [])
    if top_reasons:
        _add("## 4. Top Blocking Reasons")
        _add("")
        _add("| Rank | Reason | Count |")
        _add("|------|--------|-------|")
        for i, item in enumerate(top_reasons, 1):
            _add(f"| {i} | `{item['reason']}` | {item['count']} |")
        _add("")

    # --- Edge Summary ---
    _add("## 5. Edge Summary")
    _add("")
    _add("### Edge Breakdown (Unified Contract)")
    _add("")
    _add("```")
    _add("expected_net_edge = signal_edge_proxy + funding_adjustment - cost")
    _add("```")
    _add("")
    _add("| Component | Mean (bps) |")
    _add("|-----------|-----------|")
    _add(f"| Signal edge proxy | {_fmt(diagnostics.get('mean_signal_edge_proxy_bps'))} |")
    _add(f"| Funding adjustment | {_fmt(diagnostics.get('mean_funding_adjustment_bps'))} |")
    _add(f"| Cost (taker + slippage) | {_fmt(diagnostics.get('mean_cost_bps'))} |")
    _add(f"| **Net expected edge** | **{_fmt(diagnostics.get('mean_expected_edge_bps'))}** |")
    _add("")
    _add("### Edge Distribution")
    _add("")
    _add("| Metric | Value |")
    _add("|--------|-------|")
    _add(f"| Mean expected edge (bps) | {_fmt(diagnostics.get('mean_expected_edge_bps'))} |")
    _add(f"| Median expected edge (bps) | {_fmt(diagnostics.get('median_expected_edge_bps'))} |")
    _add(f"| P25 expected edge (bps) | {_fmt(diagnostics.get('p25_expected_edge_bps'))} |")
    _add(f"| P75 expected edge (bps) | {_fmt(diagnostics.get('p75_expected_edge_bps'))} |")
    _add(f"| Positive edge ratio | {_pct(diagnostics.get('positive_edge_ratio', 0))} |")
    _add(f"| Mean positive edge (bps) | {_fmt(diagnostics.get('mean_positive_edge_bps'))} |")
    _add("")

    # --- Score Distribution ---
    score_dist = diagnostics.get("score_distribution", {})
    if score_dist and score_dist.get("count", 0) > 0:
        _add("## 6. Score Distribution")
        _add("")
        _add("| Metric | Value |")
        _add("|--------|-------|")
        for k in ("min", "p25", "median", "p75", "max", "mean", "std"):
            _add(f"| {k} | {_fmt(score_dist.get(k))} |")
        _add("")

    # --- 核心结论 ---
    _add("## 7. Key Findings")
    _add("")
    _add(_generate_findings(diagnostics, params))
    _add("")

    # --- Caveats ---
    _add("## 8. Caveats")
    _add("")
    _add("- Phase 2 replay 使用简化评分模型（不含 AI assessment），与生产系统评分存在偏差")
    _add("- 不包含撮合仿真、滑点模型和 orderbook realism（属于 Phase 4）")
    _add("- 持仓逻辑为简化版（固定 1 单位），不反映真实资金管理")
    _add("- funding rate 边际计算基于 as-of join 的历史 funding，不代表未来")
    _add("")

    content = "\n".join(lines)
    output_path.write_text(content, encoding="utf-8")
    log.info("Report written to %s (%d lines)", output_path, len(lines))
    return output_path


def build_scan_comparison_report(
    *,
    scan_info: dict[str, Any],
    comparison: dict[str, Any],
    output_path: pathlib.Path,
) -> pathlib.Path:
    """为参数扫描生成对比报告。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    _add = lines.append

    _add("# Parameter Scan Comparison Report")
    _add("")
    _add(f"> Scan Run ID: `{scan_info.get('scan_run_id', 'N/A')}`")
    _add(f"> Generated at: {datetime.utcnow().isoformat()}Z")
    _add("")
    _add(f"- **Family**: {scan_info.get('family', 'N/A')}")
    _add(f"- **Symbol**: {scan_info.get('symbol', 'N/A')}")
    _add(f"- **Timeframe**: {scan_info.get('timeframe', 'N/A')}")
    _add(f"- **Experiment count**: {comparison.get('experiment_count', 0)}")
    _add("")

    # --- 对比表 ---
    rows = comparison.get("comparison", [])
    if rows:
        _add("## Comparison Table")
        _add("")
        _add("```")
        _add("net_edge = signal_edge + funding_adj - cost")
        _add("```")
        _add("")
        headers = ["Label", "Bars", "Opens", "Blocks", "Sel%", "Exec%",
                   "Signal", "Funding", "Cost", "NetEdge", "PosEdge%", "TopBlock"]
        _add("| " + " | ".join(headers) + " |")
        _add("| " + " | ".join(["---"] * len(headers)) + " |")
        for r in rows:
            _add("| " + " | ".join([
                str(r.get("label", "")),
                str(r.get("total_bars", 0)),
                str(r.get("opening_count", 0)),
                str(r.get("blocked_count", 0)),
                _pct(r.get("selectable_ratio", 0)),
                _pct(r.get("execution_compatible_ratio", 0)),
                _fmt(r.get("mean_signal_edge_proxy_bps")),
                _fmt(r.get("mean_funding_adjustment_bps")),
                _fmt(r.get("mean_cost_bps")),
                _fmt(r.get("mean_expected_edge_bps")),
                _pct(r.get("positive_edge_ratio", 0)),
                f"`{r.get('top_blocking_reason', 'none')}`",
            ]) + " |")
        _add("")

    # --- Caveats ---
    _add("## Caveats")
    _add("")
    _add("- 以上对比基于 Phase 2 简化 replay，不含 AI 评分、撮合仿真和滑点模型")
    _add("- 参数变化的绝对效果可能与生产环境不同，重点关注相对变化趋势")
    _add("")

    content = "\n".join(lines)
    output_path.write_text(content, encoding="utf-8")
    log.info("Scan comparison report written to %s", output_path)
    return output_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pct(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v * 100:.2f}%"


def _fmt(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v:.4f}"


def _generate_findings(diagnostics: dict[str, Any], params: dict[str, Any]) -> str:
    """基于诊断结果自动生成核心发现。"""
    findings: list[str] = []

    total = diagnostics.get("total_bars", 0)
    opening = diagnostics.get("opening_count", 0)
    blocked = diagnostics.get("blocked_count", 0)
    edge_mean = diagnostics.get("mean_expected_edge_bps")

    # --- 开仓频率分析 ---
    if total > 0:
        open_ratio = opening / total
        if open_ratio < 0.01:
            findings.append(f"- Opening rate very low ({_pct(open_ratio)}), parameters may be too restrictive")
        elif open_ratio > 0.2:
            findings.append(f"- Opening rate high ({_pct(open_ratio)}), risk of over-trading")
        else:
            findings.append(f"- Opening rate: {_pct(open_ratio)} ({opening} opens in {total} bars)")

    if blocked > 0 and total > 0:
        block_ratio = blocked / total
        findings.append(f"- Blocked ratio: {_pct(block_ratio)} ({blocked} bars met score threshold but failed gates)")

    # --- Edge 分解分析 ---
    signal_mean = diagnostics.get("mean_signal_edge_proxy_bps")
    funding_mean = diagnostics.get("mean_funding_adjustment_bps")
    cost_mean = diagnostics.get("mean_cost_bps")

    if edge_mean is not None:
        if edge_mean < 0:
            findings.append(f"- Average net edge is **negative** ({edge_mean:.2f} bps), cost exceeds signal+funding")
        else:
            findings.append(f"- Average net edge: {edge_mean:.2f} bps")

    if signal_mean is not None and funding_mean is not None:
        # 判断 edge 的主要来源
        abs_signal = abs(signal_mean)
        abs_funding = abs(funding_mean)
        if abs_signal + abs_funding > 0:
            signal_share = abs_signal / (abs_signal + abs_funding)
            if signal_share > 0.7:
                findings.append(f"- Edge primarily driven by signal ({signal_mean:.2f} bps), funding is secondary ({funding_mean:.2f} bps)")
            elif signal_share < 0.3:
                findings.append(f"- **Warning**: Edge primarily driven by funding ({funding_mean:.2f} bps), signal contribution low ({signal_mean:.2f} bps)")
            else:
                findings.append(f"- Edge balanced: signal={signal_mean:.2f} bps, funding={funding_mean:.2f} bps")

    if cost_mean is not None and cost_mean > 0:
        findings.append(f"- Cost model: {cost_mean:.1f} bps per trade (taker + slippage)")

    # --- 阻断原因 ---
    top_reasons = diagnostics.get("top_blocking_reasons", [])
    if top_reasons:
        top = top_reasons[0]
        findings.append(f"- Primary blocking reason: `{top['reason']}` ({top['count']} occurrences)")

    if not findings:
        findings.append("- No significant findings (empty or trivial result set)")

    return "\n".join(findings)
