#!/usr/bin/env python3
"""Calibration batch runner: lightweight batch replay for research calibration.

读取一组预定义的研究实验配置，复用现有 replay 主链批量运行，
自动汇总 diagnostics，生成批次级 summary 和 report。

适用场景:
  - signal_edge_scale_bps 校准
  - cost sensitivity 测试
  - threshold sensitivity 测试

Usage:
    # JSON 文件输入（推荐）
    python scripts/rdp_run_calibration_batch.py \\
        --batch-file configs/research_batches/independent_scale_calibration_15m.json

    # 内置预设
    python scripts/rdp_run_calibration_batch.py --preset independent_scale_15m

    # 自定义产物目录 + 失败即停
    python scripts/rdp_run_calibration_batch.py \\
        --batch-file my_batch.json \\
        --artifact-root artifacts/custom \\
        --stop-on-error

    # 运行前先跑 migration
    python scripts/rdp_run_calibration_batch.py \\
        --batch-file my_batch.json \\
        --ensure-schema

Exit codes:
    0 = 全部成功
    1 = 参数错误 / 启动失败
    2 = 部分成功（有成功也有失败）
    3 = 全部失败
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import pathlib
import sys
import traceback
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_calibration_batch")

_DEFAULT_ARTIFACT_ROOT = pathlib.Path("artifacts/research/calibration_batches")

# ---------------------------------------------------------------------------
# 内置预设（方式 B）
# ---------------------------------------------------------------------------
_PRESETS: dict[str, dict[str, Any]] = {
    "independent_scale_15m": {
        "batch_name": "independent_scale_calibration_15m",
        "description": "Calibrate signal_edge_scale_bps for independent family",
        "family": "independent",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "15m",
        "dataset_version": "v1.0",
        "start": "2026-03-31",
        "end": "2026-04-02",
        "experiments": [
            {"label": "scale_8", "params": {"signal_edge_scale_bps": 8}},
            {"label": "scale_10", "params": {"signal_edge_scale_bps": 10}},
            {"label": "scale_12", "params": {"signal_edge_scale_bps": 12}},
            {"label": "scale_15", "params": {"signal_edge_scale_bps": 15}},
            {"label": "scale_20", "params": {"signal_edge_scale_bps": 20}},
        ],
    },
    "independent_cost_15m": {
        "batch_name": "independent_cost_sensitivity_15m",
        "description": "Cost sensitivity test: taker_fee_bps + slippage_bps",
        "family": "independent",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "15m",
        "dataset_version": "v1.0",
        "start": "2026-03-31",
        "end": "2026-04-02",
        "experiments": [
            {"label": "cost_3_1", "params": {"cost_config": {"taker_fee_bps": 3, "slippage_bps": 1}}},
            {"label": "cost_5_2", "params": {"cost_config": {"taker_fee_bps": 5, "slippage_bps": 2}}},
            {"label": "cost_7_3", "params": {"cost_config": {"taker_fee_bps": 7, "slippage_bps": 3}}},
        ],
    },
    "independent_confirm_ticks_15m": {
        "batch_name": "independent_confirm_ticks_15m",
        "description": "Threshold sensitivity test: min_confirm_ticks sweep",
        "family": "independent",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "15m",
        "dataset_version": "v1.0",
        "start": "2026-03-31",
        "end": "2026-04-02",
        "experiments": [
            {"label": "ticks_2", "params": {"min_confirm_ticks": 2}},
            {"label": "ticks_3", "params": {"min_confirm_ticks": 3}},
            {"label": "ticks_4", "params": {"min_confirm_ticks": 4}},
            {"label": "ticks_5", "params": {"min_confirm_ticks": 5}},
        ],
    },
}


# ---------------------------------------------------------------------------
# Batch spec 校验与加载
# ---------------------------------------------------------------------------

_REQUIRED_TOP_KEYS = {"batch_name", "family", "symbol", "timeframe", "start", "end", "experiments"}


def _normalize_dataset_version(value: str | None) -> str:
    normalized = (value or "v1.0").strip()
    return "v1.0" if normalized == "v1" else normalized


def _load_batch_spec(args: argparse.Namespace) -> dict[str, Any]:
    """从 --batch-file 或 --preset 加载 batch 规格。"""
    if args.batch_file:
        path = pathlib.Path(args.batch_file)
        if not path.exists():
            log.error("Batch file not found: %s", path)
            sys.exit(1)
        with path.open("r", encoding="utf-8") as f:
            spec = json.load(f)
    elif args.preset:
        if args.preset not in _PRESETS:
            log.error("Unknown preset: %s (available: %s)", args.preset, list(_PRESETS.keys()))
            sys.exit(1)
        spec = _PRESETS[args.preset]
    else:
        log.error("Must specify either --batch-file or --preset")
        sys.exit(1)

    # 校验必填字段
    missing = _REQUIRED_TOP_KEYS - set(spec.keys())
    if missing:
        log.error("Batch spec missing required keys: %s", missing)
        sys.exit(1)

    exps = spec["experiments"]
    if not isinstance(exps, list) or len(exps) == 0:
        log.error("Batch spec must contain at least one experiment")
        sys.exit(1)

    for i, exp in enumerate(exps):
        if "label" not in exp:
            log.error("Experiment #%d missing 'label'", i)
            sys.exit(1)
        if "params" not in exp:
            log.error("Experiment #%d (%s) missing 'params'", i, exp.get("label"))
            sys.exit(1)

    spec = dict(spec)
    if args.start:
        spec["start"] = args.start
    if args.end:
        spec["end"] = args.end
    spec["dataset_version"] = _normalize_dataset_version(
        args.dataset_version or spec.get("dataset_version", "v1.0"),
    )

    return spec


# ---------------------------------------------------------------------------
# 单实验执行（复用现有主链）
# ---------------------------------------------------------------------------

def _run_single_experiment(
    session: Any,
    *,
    adapter: Any,
    spec: dict[str, Any],
    exp_def: dict[str, Any],
    artifact_root: pathlib.Path,
    batch_run_id: str,
) -> dict[str, Any]:
    """运行单个校准实验，返回 summary 行字典。

    复用现有模块:
    - run_replay
    - compute_diagnostics
    - write_decisions_csv / write_summary_json
    - build_experiment_report
    - experiment_registry CRUD
    """
    from aats.data_platform.replay.core.replay_context import ReplayParameterOverrides
    from aats.data_platform.replay.core.replay_result_writer import (
        write_decisions_csv,
        write_summary_json,
    )
    from aats.data_platform.replay.core.replay_runner import run_replay
    from aats.data_platform.replay.diagnostics.replay_diagnostics import compute_diagnostics
    from aats.data_platform.replay.registry.experiment_registry import (
        create_experiment,
        mark_experiment_running,
        mark_experiment_succeeded,
        upsert_experiment_summary,
    )
    from aats.data_platform.replay.reports.markdown_report_builder import build_experiment_report

    label = exp_def["label"]
    raw_params = dict(exp_def["params"])

    family = spec["family"]
    symbol = spec["symbol"]
    timeframe = spec["timeframe"]
    dataset_version = spec.get("dataset_version", "v1.0")
    start_ts = datetime.strptime(spec["start"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_ts = datetime.strptime(spec["end"], "%Y-%m-%d").replace(tzinfo=timezone.utc)

    # ---------- P1-1: 显式参数归一化 ----------
    # 将扁平 cost keys 组装成 cost_config 嵌套结构后再传给 from_dict()，
    # 消除对 from_dict() 内部隐式收口逻辑的依赖。
    # 支持两种写法：
    #   (a) 扁平: {"taker_fee_bps": 3, "slippage_bps": 1}
    #   (b) 嵌套: {"cost_config": {"taker_fee_bps": 3, "slippage_bps": 1}}
    _FLAT_COST_KEYS = {"taker_fee_bps", "slippage_bps"}
    flat_cost_keys = _FLAT_COST_KEYS & set(raw_params.keys())
    if flat_cost_keys:
        cost_dict = raw_params.pop("cost_config", {}) if isinstance(raw_params.get("cost_config"), dict) else {}
        for ck in flat_cost_keys:
            cost_dict[ck] = raw_params.pop(ck)
        raw_params["cost_config"] = cost_dict
        log.debug("Normalized flat cost keys %s → cost_config=%s", flat_cost_keys, cost_dict)

    params = ReplayParameterOverrides.from_dict(raw_params)
    # 保留用户原始写法用于 summary（不含归一化后的结构）
    params_dict = exp_def["params"]

    # 1. 注册实验
    exp_id = create_experiment(
        session,
        family=family,
        symbol=symbol,
        timeframe=timeframe,
        dataset_version=dataset_version,
        parameter_overrides=params.to_dict(),
        window_start_ts=start_ts,
        window_end_ts=end_ts,
        notes=f"calibration batch: {spec['batch_name']}, label: {label}",
    )
    mark_experiment_running(session, exp_id)
    session.flush()

    # 2. 运行 replay
    decisions = run_replay(
        session,
        adapter=adapter,
        symbol=symbol,
        timeframe=timeframe,
        dataset_version=dataset_version,
        start_ts=start_ts,
        end_ts=end_ts,
        params=params,
    )

    # 3. 写单实验 artifacts
    exp_dir = artifact_root / batch_run_id / "experiments" / label
    result_path = write_decisions_csv(decisions, exp_dir / "replay_decisions.csv")
    diag = compute_diagnostics(decisions)
    summary_path = write_summary_json(diag, exp_dir / "diagnostics.json")

    # 4. 生成单实验 report
    exp_info = {
        "experiment_id": str(exp_id),
        "family": family,
        "symbol": symbol,
        "timeframe": timeframe,
        "dataset_version": dataset_version,
        "parameter_overrides": params.to_dict(),
        "window_start_ts": spec["start"],
        "window_end_ts": spec["end"],
        "status": "succeeded",
    }
    report_path = build_experiment_report(
        experiment_info=exp_info,
        diagnostics=diag,
        output_path=exp_dir / "report.md",
    )

    # 5. 更新 registry
    upsert_experiment_summary(session, exp_id, summary=diag)
    mark_experiment_succeeded(
        session, exp_id,
        bar_count=len(decisions),
        result_path=str(result_path),
        summary_path=str(summary_path),
        report_path=str(report_path),
    )

    # 6. 构造 summary 行
    top_reasons = diag.get("top_blocking_reasons", [])
    summary_row = {
        "label": label,
        "experiment_id": str(exp_id),
        "status": "succeeded",
        "params": params_dict,
        "opening_count": diag.get("opening_count", 0),
        "blocked_count": diag.get("blocked_count", 0),
        "selectable_ratio": diag.get("selectable_ratio", 0),
        "execution_compatible_ratio": diag.get("execution_compatible_ratio", 0),
        "mean_signal_edge_proxy_bps": diag.get("mean_signal_edge_proxy_bps"),
        "mean_funding_adjustment_bps": diag.get("mean_funding_adjustment_bps"),
        "mean_cost_bps": diag.get("mean_cost_bps"),
        "mean_expected_edge_bps": diag.get("mean_expected_edge_bps"),
        "positive_edge_ratio": diag.get("positive_edge_ratio", 0),
        "top_blocking_reason_1": top_reasons[0]["reason"] if len(top_reasons) > 0 else "",
        "top_blocking_reason_2": top_reasons[1]["reason"] if len(top_reasons) > 1 else "",
        "result_path": str(result_path),
        "summary_path": str(summary_path),
        "report_path": str(report_path),
        "diagnostics": diag,
    }

    return summary_row


# ---------------------------------------------------------------------------
# Batch 级产物写出
# ---------------------------------------------------------------------------

_CSV_COLUMNS = [
    "label",
    "experiment_id",
    "status",
    "opening_count",
    "blocked_count",
    "selectable_ratio",
    "execution_compatible_ratio",
    "mean_signal_edge_proxy_bps",
    "mean_funding_adjustment_bps",
    "mean_cost_bps",
    "mean_expected_edge_bps",
    "positive_edge_ratio",
    "top_blocking_reason_1",
    "top_blocking_reason_2",
    "result_path",
    "report_path",
]


def _write_batch_summary_csv(
    rows: list[dict[str, Any]],
    output_path: pathlib.Path,
) -> pathlib.Path:
    """写 batch_summary.csv。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    log.info("Wrote batch_summary.csv → %s", output_path)
    return output_path


def _write_batch_summary_json(
    spec: dict[str, Any],
    rows: list[dict[str, Any]],
    failed: list[dict[str, Any]],
    batch_run_id: str,
    output_path: pathlib.Path,
) -> pathlib.Path:
    """写 batch_summary.json（机器可读）。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 剥离 diagnostics 原始字典中的 per-experiment 完整数据，保留关键指标
    per_exp = []
    for row in rows:
        entry = {k: v for k, v in row.items() if k != "diagnostics"}
        per_exp.append(entry)

    summary = {
        "batch_run_id": batch_run_id,
        "batch_name": spec.get("batch_name"),
        "description": spec.get("description"),
        "family": spec["family"],
        "symbol": spec["symbol"],
        "timeframe": spec["timeframe"],
        "dataset_version": spec.get("dataset_version", "v1.0"),
        "window": f"{spec['start']} ~ {spec['end']}",
        "total_experiments": len(spec["experiments"]),
        "succeeded": len(rows),
        "failed": len(failed),
        "experiments": per_exp,
        "failures": failed,
    }

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False, default=str)
    log.info("Wrote batch_summary.json → %s", output_path)
    return output_path


def _write_failed_experiments(
    failed: list[dict[str, Any]],
    output_path: pathlib.Path,
) -> pathlib.Path:
    """写 failed_experiments.json。

    注意：traceback 内联在 JSON 中，方便快速调试。
    如果后续文件体积过大，可改为只保留 error + error_type，
    traceback 落到独立文件 (traceback_path)。
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(failed, f, indent=2, ensure_ascii=False)
    log.info("Wrote failed_experiments.json → %s (%d failures)", output_path, len(failed))
    return output_path


def _write_experiment_refs(
    rows: list[dict[str, Any]],
    failed: list[dict[str, Any]],
    batch_run_id: str,
    output_path: pathlib.Path,
) -> pathlib.Path:
    """写 experiment_refs.json：记录 label → experiment_id + 产物路径映射。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    refs = {
        "batch_run_id": batch_run_id,
        "refs": [],
    }
    for row in rows:
        refs["refs"].append({
            "label": row["label"],
            "experiment_id": row["experiment_id"],
            "status": "succeeded",
            "result_path": row.get("result_path"),
            "summary_path": row.get("summary_path"),
            "report_path": row.get("report_path"),
        })
    for f_item in failed:
        refs["refs"].append({
            "label": f_item["label"],
            "experiment_id": None,
            "status": "failed",
            "result_path": None,
            "summary_path": None,
            "report_path": None,
        })

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(refs, f, indent=2, ensure_ascii=False)
    log.info("Wrote experiment_refs.json → %s", output_path)
    return output_path


def _write_batch_spec(
    spec: dict[str, Any],
    output_path: pathlib.Path,
) -> pathlib.Path:
    """写 batch_spec.json（原始输入规格副本，便于复现）。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2, ensure_ascii=False)
    log.info("Wrote batch_spec.json → %s", output_path)
    return output_path


# ---------------------------------------------------------------------------
# Batch Report (Markdown)
# ---------------------------------------------------------------------------

def _build_batch_report(
    spec: dict[str, Any],
    rows: list[dict[str, Any]],
    failed: list[dict[str, Any]],
    batch_run_id: str,
    output_path: pathlib.Path,
) -> pathlib.Path:
    """生成 batch_report.md。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    _add = lines.append

    # --- Header ---
    _add("# Calibration Batch Report")
    _add("")
    _add(f"> Generated at: {datetime.now(timezone.utc).isoformat()}")
    _add("")

    # --- Batch Info ---
    _add("## 1. Batch Overview")
    _add("")
    _add("| Field | Value |")
    _add("|-------|-------|")
    _add(f"| Batch Run ID | `{batch_run_id}` |")
    _add(f"| Batch Name | {spec.get('batch_name', 'N/A')} |")
    _add(f"| Description | {spec.get('description', 'N/A')} |")
    _add(f"| Family | {spec['family']} |")
    _add(f"| Symbol | {spec['symbol']} |")
    _add(f"| Timeframe | {spec['timeframe']} |")
    _add(f"| Dataset Version | {spec.get('dataset_version', 'v1.0')} |")
    _add(f"| Window | {spec['start']} ~ {spec['end']} |")
    _add(f"| Total Experiments | {len(spec['experiments'])} |")
    _add(f"| Succeeded | {len(rows)} |")
    _add(f"| Failed | {len(failed)} |")
    _add("")

    # --- 实验结果表 ---
    _add("## 2. Experiment Results")
    _add("")
    _add("```")
    _add("net_edge = signal_edge + funding_adj - cost")
    _add("```")
    _add("")

    if rows:
        headers = [
            "Label", "Opens", "Blocks", "Sel%", "Exec%",
            "Signal(bps)", "Funding(bps)", "Cost(bps)", "NetEdge(bps)",
            "PosEdge%", "TopBlock",
        ]
        _add("| " + " | ".join(headers) + " |")
        _add("| " + " | ".join(["---"] * len(headers)) + " |")

        for r in rows:
            _add("| " + " | ".join([
                r["label"],
                str(r.get("opening_count", 0)),
                str(r.get("blocked_count", 0)),
                _pct(r.get("selectable_ratio", 0)),
                _pct(r.get("execution_compatible_ratio", 0)),
                _fmt(r.get("mean_signal_edge_proxy_bps")),
                _fmt(r.get("mean_funding_adjustment_bps")),
                _fmt(r.get("mean_cost_bps")),
                _fmt(r.get("mean_expected_edge_bps")),
                _pct(r.get("positive_edge_ratio", 0)),
                f"`{r.get('top_blocking_reason_1', 'none')}`",
            ]) + " |")
        _add("")
    else:
        _add("*(no succeeded experiments)*")
        _add("")

    # --- 失败实验 ---
    _add("## 3. Failed Experiments")
    _add("")
    if failed:
        _add("| Label | Error |")
        _add("|-------|-------|")
        for f_item in failed:
            err_short = str(f_item.get("error", "unknown"))[:120]
            _add(f"| {f_item['label']} | `{err_short}` |")
        _add("")
    else:
        _add("*(none)*")
        _add("")

    # --- 初步发现 ---
    _add("## 4. Preliminary Findings")
    _add("")
    findings = _generate_batch_findings(spec, rows)
    for finding in findings:
        _add(finding)
    _add("")

    # --- Caveats ---
    _add("## 5. Caveats")
    _add("")
    _add("- Phase 2 replay 使用简化评分模型（不含 AI assessment），与生产系统评分存在偏差")
    _add("- 不包含撮合仿真、滑点模型和 orderbook realism（属于 Phase 4）")
    _add("- 持仓逻辑为简化版（固定 1 单位），不反映真实资金管理")
    _add("- 以上对比重点关注相对变化趋势，而非绝对数值")
    _add("")

    content = "\n".join(lines)
    output_path.write_text(content, encoding="utf-8")
    log.info("Wrote batch_report.md → %s (%d lines)", output_path, len(lines))
    return output_path


def _generate_batch_findings(
    spec: dict[str, Any],
    rows: list[dict[str, Any]],
) -> list[str]:
    """基于批次结果自动生成简单规则化总结。

    限制：当前按单参数维度逐个分析趋势，适合单变量或少变量校准。
    如果一个实验同时变动多个参数，趋势归因能力有限——
    此时应使用 rdp_run_parameter_scan.py 的笛卡尔积 + compare_diagnostics。
    """
    findings: list[str] = []

    if len(rows) < 2:
        findings.append("- 成功实验不足 2 组，无法做趋势分析")
        return findings

    # 检测各参数的变化趋势
    # 提取每组实验的可变参数 key（从 params 字典）
    param_keys: set[str] = set()
    for r in rows:
        if isinstance(r.get("params"), dict):
            param_keys.update(r["params"].keys())

    for pk in sorted(param_keys):
        vals = []
        for r in rows:
            p = r.get("params", {})
            if pk in p:
                vals.append((p[pk], r))

        if len(vals) < 2:
            continue

        # 按参数值排序
        vals.sort(key=lambda x: float(x[0]) if isinstance(x[0], (int, float)) else 0)

        # 趋势分析: opening_count
        opens = [v[1].get("opening_count", 0) for v in vals]
        param_vals_str = [str(v[0]) for v in vals]

        if opens[-1] > opens[0]:
            findings.append(
                f"- 随着 `{pk}` 从 {param_vals_str[0]} 增至 {param_vals_str[-1]}，"
                f"opening_count 上升: {opens[0]} → {opens[-1]}"
            )
        elif opens[-1] < opens[0]:
            findings.append(
                f"- 随着 `{pk}` 从 {param_vals_str[0]} 增至 {param_vals_str[-1]}，"
                f"opening_count 下降: {opens[0]} → {opens[-1]}"
            )
        else:
            findings.append(
                f"- `{pk}` 变化（{param_vals_str[0]} → {param_vals_str[-1]}）"
                f"对 opening_count 无明显影响（均为 {opens[0]}）"
            )

        # 趋势分析: mean_expected_edge_bps
        edges = [v[1].get("mean_expected_edge_bps") for v in vals]
        if all(e is not None for e in edges):
            if edges[-1] > edges[0]:
                findings.append(
                    f"- 随着 `{pk}` 增大，mean_expected_edge_bps 上升: "
                    f"{edges[0]:.2f} → {edges[-1]:.2f}"
                )
            elif edges[-1] < edges[0]:
                findings.append(
                    f"- 随着 `{pk}` 增大，mean_expected_edge_bps 下降: "
                    f"{edges[0]:.2f} → {edges[-1]:.2f}"
                )

        # 特殊检测：某参数值导致 opening 清零
        zero_vals = [v[0] for v in vals if v[1].get("opening_count", 0) == 0]
        if zero_vals and len(zero_vals) < len(vals):
            findings.append(
                f"- **注意**: `{pk}` = {zero_vals} 时 opening_count 为 0（策略完全不触发）"
            )

    # blocked 趋势
    blocks = [r.get("blocked_count", 0) for r in rows]
    if max(blocks) > 0:
        max_block_label = rows[blocks.index(max(blocks))]["label"]
        findings.append(
            f"- blocked_count 最高的实验: {max_block_label} ({max(blocks)} bars)"
        )

    if not findings:
        findings.append("- 未检测到显著趋势变化")

    return findings


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


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Calibration batch runner: lightweight batch replay for research calibration",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--batch-file", type=str, help="Path to batch JSON file")
    group.add_argument(
        "--preset", type=str,
        choices=list(_PRESETS.keys()),
        help="Built-in preset batch name",
    )
    parser.add_argument(
        "--artifact-root", type=str,
        default=str(_DEFAULT_ARTIFACT_ROOT),
        help=f"Artifact output root (default: {_DEFAULT_ARTIFACT_ROOT})",
    )
    parser.add_argument(
        "--ensure-schema", action="store_true",
        help="Legacy name: validate schema before batch; does not run DDL",
    )
    parser.add_argument(
        "--stop-on-error", action="store_true",
        help="Stop entire batch on first experiment failure",
    )
    parser.add_argument(
        "--no-print-summary", action="store_true",
        help="Suppress final summary print to stdout",
    )
    parser.add_argument("--start", help="Override batch start date (YYYY-MM-DD, UTC)")
    parser.add_argument("--end", help="Override batch end date (YYYY-MM-DD, UTC)")
    parser.add_argument("--dataset-version", default="v1.0")
    args = parser.parse_args()

    # 1. 加载 batch 规格
    spec = _load_batch_spec(args)
    experiments = spec["experiments"]

    log.info("=" * 60)
    log.info("Starting calibration batch: %s", spec.get("batch_name", "unnamed"))
    log.info("  Family     : %s", spec["family"])
    log.info("  Symbol     : %s", spec["symbol"])
    log.info("  Timeframe  : %s", spec["timeframe"])
    log.info("  Window     : %s ~ %s", spec["start"], spec["end"])
    log.info("  Experiments: %d", len(experiments))
    log.info("=" * 60)

    # 2. 延迟导入
    from aats.data_platform.config import get_settings
    from aats.data_platform.db import get_session, validate_rdp_schema
    from aats.data_platform.replay.adapters.directional_adapter import DirectionalReplayAdapter
    from aats.data_platform.replay.adapters.independent_adapter import IndependentReplayAdapter

    settings = get_settings()
    if args.ensure_schema:
        log.info("Validating schema contract (--ensure-schema legacy flag)...")
        validate_rdp_schema(settings)

    # 创建 adapter
    family = spec["family"]
    if family == "independent":
        adapter = IndependentReplayAdapter()
    elif family == "directional":
        adapter = DirectionalReplayAdapter()
    else:
        log.error("Unsupported family: %s (must be 'independent' or 'directional')", family)
        sys.exit(1)

    # 3. 生成 batch run ID
    batch_run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
    artifact_root = pathlib.Path(args.artifact_root)
    batch_dir = artifact_root / batch_run_id

    log.info("Batch run ID: %s", batch_run_id)
    log.info("Artifact dir: %s", batch_dir)

    # 4. 保存 batch_spec.json
    _write_batch_spec(spec, batch_dir / "batch_spec.json")

    # 5. 逐实验运行
    succeeded_rows: list[dict[str, Any]] = []
    failed_list: list[dict[str, Any]] = []

    with get_session(settings) as session:
        for i, exp_def in enumerate(experiments):
            label = exp_def["label"]
            log.info("[%d/%d] Running %s ... params=%s",
                     i + 1, len(experiments), label, exp_def["params"])

            try:
                summary_row = _run_single_experiment(
                    session,
                    adapter=adapter,
                    spec=spec,
                    exp_def=exp_def,
                    artifact_root=artifact_root,
                    batch_run_id=batch_run_id,
                )
                succeeded_rows.append(summary_row)
                session.commit()
                log.info("[%d/%d] %s succeeded", i + 1, len(experiments), label)

            except Exception as exc:
                tb = traceback.format_exc()
                log.exception("[%d/%d] %s FAILED", i + 1, len(experiments), label)
                failed_list.append({
                    "label": label,
                    "params": exp_def["params"],
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "traceback": tb,
                })
                session.rollback()

                if args.stop_on_error:
                    log.error("--stop-on-error: aborting batch after failure at %s", label)
                    break

    # 6. 写 batch 级产物
    log.info("")
    log.info("Writing batch-level artifacts...")

    _write_batch_summary_csv(succeeded_rows, batch_dir / "batch_summary.csv")
    _write_batch_summary_json(spec, succeeded_rows, failed_list, batch_run_id,
                              batch_dir / "batch_summary.json")
    _write_failed_experiments(failed_list, batch_dir / "failed_experiments.json")
    _write_experiment_refs(succeeded_rows, failed_list, batch_run_id,
                           batch_dir / "experiment_refs.json")
    _build_batch_report(spec, succeeded_rows, failed_list, batch_run_id,
                        batch_dir / "batch_report.md")

    # 7. 打印最终汇总
    total = len(experiments)
    n_ok = len(succeeded_rows)
    n_fail = len(failed_list)

    log.info("")
    log.info("=" * 60)
    log.info("Batch completed: %d succeeded, %d failed (total %d)", n_ok, n_fail, total)
    log.info("Summary: %s", batch_dir / "batch_summary.csv")
    log.info("Report : %s", batch_dir / "batch_report.md")
    log.info("=" * 60)

    if not args.no_print_summary:
        print("")
        print(f"=== Calibration Batch: {spec.get('batch_name', 'unnamed')} ===")
        print(f"Batch Run ID: {batch_run_id}")
        print(f"Result: {n_ok} succeeded, {n_fail} failed / {total} total")
        print("")
        if succeeded_rows:
            # 简要表格
            print(f"{'Label':<20} {'Opens':>6} {'Blocks':>7} {'NetEdge(bps)':>13} {'PosEdge%':>9}")
            print("-" * 60)
            for r in succeeded_rows:
                edge = r.get("mean_expected_edge_bps")
                edge_str = f"{edge:.4f}" if edge is not None else "N/A"
                pos_ratio = r.get("positive_edge_ratio", 0)
                print(f"{r['label']:<20} {r.get('opening_count', 0):>6} "
                      f"{r.get('blocked_count', 0):>7} {edge_str:>13} "
                      f"{pos_ratio * 100:>8.2f}%")
        if failed_list:
            print("")
            print("Failed experiments:")
            for f_item in failed_list:
                print(f"  - {f_item['label']}: {f_item['error'][:80]}")
        print("")
        print(f"Artifacts: {batch_dir}")

    # 退出码策略：
    #   0 = 全部成功
    #   2 = 部分成功（有成功也有失败）
    #   3 = 全部失败
    if n_fail > 0 and n_ok > 0:
        sys.exit(2)
    elif n_fail > 0 and n_ok == 0:
        sys.exit(3)


if __name__ == "__main__":
    main()
