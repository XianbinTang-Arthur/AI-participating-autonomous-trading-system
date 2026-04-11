#!/usr/bin/env python3
"""Step 1 Calibration Orchestrator.

自动运行 Step 1 规定的 3 个 calibration batch，汇总结果，
应用规则化判断，生成 Step 1 结论文档。

固定范围（Step 1）：
  family   = independent
  symbol   = BTC-USDT-SWAP
  timeframe = 15m

Usage:
    python scripts/rdp_run_step1_calibration.py
    python scripts/rdp_run_step1_calibration.py --artifact-root artifacts/custom
    python scripts/rdp_run_step1_calibration.py --ensure-schema

Exit codes:
    0 = 全部成功（3 个 batch 均成功完成）
    1 = 参数错误 / 启动失败
    2 = 部分成功（至少 1 个 batch 有数据，但不是全部成功）
    3 = 全部失败
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import pathlib
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from aats.data_platform.replay.core.replay_context import ReplayCostConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_step1_calibration")

# =========================================================================
# Step 1 固定范围
# =========================================================================
_FAMILY = "independent"
_SYMBOL = "BTC-USDT-SWAP"
_TIMEFRAME = "15m"

_BATCH_DEFS: list[dict[str, str]] = [
    {
        "key": "scale_calibration",
        "file": "configs/research_batches/independent_scale_calibration_15m.json",
        "description": "Signal edge scale calibration",
    },
    {
        "key": "cost_sensitivity",
        "file": "configs/research_batches/independent_cost_sensitivity_15m.json",
        "description": "Cost model sensitivity test",
    },
    {
        "key": "confirm_ticks",
        "file": "configs/research_batches/independent_confirm_ticks_15m.json",
        "description": "Confirmation ticks sensitivity test",
    },
]

_DEFAULT_ARTIFACT_ROOT = pathlib.Path("artifacts/research/calibration_rounds")

# =========================================================================
# 1. 子进程调用 batch runner
# =========================================================================


def _list_subdirs(path: pathlib.Path) -> set[str]:
    if not path.exists():
        return set()
    return {d.name for d in path.iterdir() if d.is_dir()}


def _run_batch(
    batch_file: str,
    batch_artifact_root: pathlib.Path,
    *,
    ensure_schema: bool = False,
    stop_on_error: bool = False,
) -> dict[str, Any]:
    """通过子进程调用 rdp_run_calibration_batch.py 运行单个 batch。

    返回 dict 包含 status, batch_run_id, batch_dir, summary, exit_code。
    第一版以子进程方式调用，后续可模块化。
    """
    existing = _list_subdirs(batch_artifact_root)

    cmd = [
        sys.executable, "scripts/rdp_run_calibration_batch.py",
        "--batch-file", str(batch_file),
        "--artifact-root", str(batch_artifact_root),
        "--no-print-summary",
    ]
    if ensure_schema:
        cmd.append("--ensure-schema")
    if stop_on_error:
        cmd.append("--stop-on-error")

    log.info("  CMD: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        capture_output=True,
    )

    # 通过文件系统发现新目录（比解析 stdout 更可靠）
    new_dirs = _list_subdirs(batch_artifact_root) - existing
    if not new_dirs:
        # 安全解码 stderr（Windows 控制台可能是 GBK 或 UTF-8）
        stderr_raw = proc.stderr or b""
        stderr_tail = stderr_raw[-500:].decode("utf-8", errors="replace")
        return {
            "status": "failed",
            "exit_code": proc.returncode,
            "batch_run_id": None,
            "batch_dir": None,
            "summary": None,
            "error": f"No artifact dir created. exit={proc.returncode}. {stderr_tail}",
        }

    batch_run_id = sorted(new_dirs)[-1]
    batch_dir = batch_artifact_root / batch_run_id

    # 读 batch_summary.json
    summary_file = batch_dir / "batch_summary.json"
    summary: dict[str, Any] | None = None
    if summary_file.exists():
        with summary_file.open(encoding="utf-8") as f:
            summary = json.load(f)

    if proc.returncode == 0:
        status = "succeeded"
    elif proc.returncode == 2:
        status = "partial_success"
    else:
        status = "failed"

    return {
        "status": status,
        "exit_code": proc.returncode,
        "batch_run_id": batch_run_id,
        "batch_dir": str(batch_dir),
        "summary": summary,
        "error": None,
    }


# =========================================================================
# 2. Round Summary 构建
# =========================================================================

_ROUND_CSV_COLUMNS = [
    "batch_name",
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
    "summary_path",
    "report_path",
]


def _collect_all_experiments(batch_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从所有 batch 结果中收集全部实验行，附加 batch_name。"""
    all_rows: list[dict[str, Any]] = []
    for br in batch_results:
        summary = br.get("summary")
        if not summary:
            continue
        batch_name = summary.get("batch_name", "unknown")
        for exp in summary.get("experiments", []):
            row = dict(exp)
            row["batch_name"] = batch_name
            all_rows.append(row)
    return all_rows


def _write_round_summary_csv(
    rows: list[dict[str, Any]],
    output_path: pathlib.Path,
) -> pathlib.Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_ROUND_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    log.info("Wrote round_summary.csv → %s (%d rows)", output_path, len(rows))
    return output_path


def _write_round_summary_json(
    rows: list[dict[str, Any]],
    round_id: str,
    output_path: pathlib.Path,
) -> pathlib.Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "round_id": round_id,
        "total_experiments": len(rows),
        "experiments": rows,
    }
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    log.info("Wrote round_summary.json → %s", output_path)
    return output_path


# =========================================================================
# 3. 规则化推荐引擎
# =========================================================================


def _get_experiments_by_batch(
    all_rows: list[dict[str, Any]],
    batch_name: str,
) -> list[dict[str, Any]]:
    return [r for r in all_rows if r.get("batch_name") == batch_name]


def _recommend_signal_edge_scale(experiments: list[dict[str, Any]]) -> dict[str, Any]:
    """规则化推荐 signal_edge_scale_bps。

    规则：
    1. 按 scale 升序排列
    2. 筛选 viable（positive edge）
    3. 在 viable 中寻找"最佳平衡点"：
       - 起点 = 最低 viable scale
       - 向上走，若 opening 显著增长（>10%）或 positive_edge_ratio 显著改善（>15pp），则更新推荐
       - 否则停止（边际收益递减）
    """
    if not experiments:
        return {"value": None, "confidence": "low",
                "reason": "Scale calibration batch 未运行或无数据"}

    sorted_exps = sorted(
        experiments,
        key=lambda e: e.get("params", {}).get("signal_edge_scale_bps", 0),
    )

    # Phase 1: 筛选 viable（net edge > 0）
    viable = [e for e in sorted_exps if (e.get("mean_expected_edge_bps") or 0) > 0]

    if not viable:
        best = sorted_exps[-1]
        scale = best.get("params", {}).get("signal_edge_scale_bps")
        edge = best.get("mean_expected_edge_bps") or 0
        return {
            "value": scale,
            "confidence": "low",
            "reason": (
                f"No scale achieves positive net edge. "
                f"Highest tested scale ({scale}) has edge {edge:.2f} bps. "
                f"Signal may be too weak or cost model too conservative."
            ),
        }

    # Phase 2: 在 viable 中找最佳平衡点
    recommended = viable[0]
    for i in range(1, len(viable)):
        prev = viable[i - 1]
        curr = viable[i]
        prev_opens = max(prev.get("opening_count", 0), 1)
        curr_opens = curr.get("opening_count", 0)
        prev_pos = prev.get("positive_edge_ratio", 0)
        curr_pos = curr.get("positive_edge_ratio", 0)

        opens_growth = (curr_opens - prev_opens) / prev_opens
        pos_improvement = curr_pos - prev_pos

        # 仍有显著结构性改善？
        if opens_growth > 0.10 or pos_improvement > 0.15:
            recommended = curr
        else:
            break  # 边际递减，停在前一个

    scale = recommended.get("params", {}).get("signal_edge_scale_bps")
    edge = recommended.get("mean_expected_edge_bps", 0) or 0
    opens = recommended.get("opening_count", 0)
    pos_ratio = recommended.get("positive_edge_ratio", 0)

    if pos_ratio > 0.7:
        confidence = "high"
    elif pos_ratio > 0.4:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "value": scale,
        "confidence": confidence,
        "reason": (
            f"scale={scale} 是满足 positive edge 条件下结构最优的平衡点 "
            f"(edge={edge:.2f} bps, opens={opens}, positive_edge_ratio={pos_ratio:.1%}). "
            f"更高 scale 的边际改善不再显著."
        ),
    }


def _extract_total_cost(params: dict[str, Any]) -> float:
    """从实验 params 中提取总成本 bps（使用混合费率模型）。"""
    cc = params.get("cost_config")
    if isinstance(cc, dict):
        return ReplayCostConfig.from_dict(cc).total_cost_bps
    return ReplayCostConfig.from_dict(params).total_cost_bps


def _extract_cost_parts(params: dict[str, Any]) -> tuple[float, float]:
    """返回 (taker_fee_bps, slippage_bps)。"""
    cc = params.get("cost_config", {})
    if isinstance(cc, dict):
        return float(cc.get("taker_fee_bps", 5.0)), float(cc.get("slippage_bps", 2.0))
    return float(params.get("taker_fee_bps", 5.0)), float(params.get("slippage_bps", 2.0))


def _recommend_cost_model(experiments: list[dict[str, Any]]) -> dict[str, Any]:
    """规则化推荐 cost model (blended fee + slippage)。

    规则：
    1. 按 total_cost 升序排列
    2. 检查默认值（blended fee≈3.6 + slippage=2 ≈ 5.6bps）是否在测试范围内
    3. 若默认值的 edge 为正 → 保持，medium confidence
    4. 若默认值的 edge 为负但低 cost 为正 → edge fragile，仍推荐默认但标记脆弱性
    5. 观察 opening_count 对 cost 的敏感度
    """
    if not experiments:
        return {
            "taker_fee_bps": {"value": 5.0, "confidence": "low",
                              "reason": "Cost batch 未运行或无数据, 保留默认值"},
            "slippage_bps": {"value": 2.0, "confidence": "low",
                             "reason": "Cost batch 未运行或无数据, 保留默认值"},
            "overall_confidence": "low",
            "overall_reason": "Cost sensitivity batch 无数据",
        }

    sorted_exps = sorted(
        experiments,
        key=lambda e: _extract_total_cost(e.get("params", {})),
    )

    # 找默认值实验
    # 容差 0.5 bps：混合费率默认值 ≈5.605 为浮点数，
    # 实验参数经 JSON 序列化往返后可能有微小精度偏移，0.01 不够稳健。
    default_total_cost = ReplayCostConfig().total_cost_bps
    default_exp = None
    for e in sorted_exps:
        tc = _extract_total_cost(e.get("params", {}))
        if abs(tc - default_total_cost) < 0.5:
            default_exp = e
            break

    # 敏感度分析
    if len(sorted_exps) >= 2:
        first = sorted_exps[0]
        last = sorted_exps[-1]
        first_opens = first.get("opening_count", 0)
        last_opens = last.get("opening_count", 0)
        cost_range = _extract_total_cost(last.get("params", {})) - _extract_total_cost(first.get("params", {}))
        if first_opens > 0 and cost_range > 0:
            drop_pct = (first_opens - last_opens) / first_opens
            sensitivity_note = (
                f"Opening drops {drop_pct:.0%} across {cost_range:.0f}bps cost range "
                f"({first_opens}→{last_opens})."
            )
        else:
            sensitivity_note = "Insufficient data for sensitivity analysis."
    else:
        sensitivity_note = "Only 1 cost point tested."

    # 推荐逻辑
    if default_exp:
        default_edge = default_exp.get("mean_expected_edge_bps") or 0
        default_opens = default_exp.get("opening_count", 0)
        taker, slip = _extract_cost_parts(default_exp.get("params", {}))

        if default_edge > 0:
            confidence = "medium"
            reason = (
                f"Default cost (taker={taker}, slip={slip}, total={taker + slip}bps) "
                f"achieves positive edge ({default_edge:.2f}bps) with {default_opens} opens. "
                f"{sensitivity_note}"
            )
        else:
            # 检查是否有更低 cost 能达到 positive edge
            lower_positive = [
                e for e in sorted_exps
                if _extract_total_cost(e.get("params", {})) < default_total_cost
                and (e.get("mean_expected_edge_bps") or 0) > 0
            ]
            if lower_positive:
                confidence = "medium"
                reason = (
                    f"Default cost ({taker + slip}bps) yields negative edge ({default_edge:.2f}bps), "
                    f"but lower cost achieves positive edge — edge is fragile under current signal strength. "
                    f"保留保守默认值以避免过度乐观. {sensitivity_note}"
                )
            else:
                confidence = "low"
                reason = (
                    f"Default cost ({taker + slip}bps) yields negative edge ({default_edge:.2f}bps) "
                    f"and no tested cost achieves positive edge. Signal too weak for this cost level. "
                    f"{sensitivity_note}"
                )
    else:
        # 默认值不在测试范围 → 选最平衡的
        balanced = sorted_exps[len(sorted_exps) // 2]
        taker, slip = _extract_cost_parts(balanced.get("params", {}))
        confidence = "low"
        reason = (
            f"Default (5,2) not in tested range. "
            f"Median tested cost (taker={taker}, slip={slip}) used as fallback. "
            f"{sensitivity_note}"
        )

    return {
        "taker_fee_bps": {"value": taker, "confidence": confidence, "reason": reason},
        "slippage_bps": {"value": slip, "confidence": confidence, "reason": reason},
        "overall_confidence": confidence,
        "overall_reason": reason,
    }


def _recommend_confirm_ticks(experiments: list[dict[str, Any]]) -> dict[str, Any]:
    """规则化推荐 min_confirm_ticks。

    规则：
    1. 按 ticks 升序排列
    2. 找 opening_count 显著下降的拐点
    3. 检查 score_not_stable 在高 ticks 下是否成为主要 blocker
    4. 推荐拐点前的最后一个 reasonable ticks
    """
    if not experiments:
        return {"value": 2, "confidence": "low",
                "reason": "Confirm ticks batch 未运行或无数据, 保留默认值"}

    sorted_exps = sorted(
        experiments,
        key=lambda e: e.get("params", {}).get("min_confirm_ticks", 0),
    )

    # 检查 opening 变化和 blocking 结构
    recommended = sorted_exps[0]
    for i in range(1, len(sorted_exps)):
        prev = sorted_exps[i - 1]
        curr = sorted_exps[i]
        prev_opens = max(prev.get("opening_count", 0), 1)
        curr_opens = curr.get("opening_count", 0)

        drop_ratio = (prev_opens - curr_opens) / prev_opens

        # 若 opening 下降 > 40%，太严了
        if drop_ratio > 0.40:
            break

        # 若 top_blocking_reason 变成 score_not_stable 且占比大 → 门槛过严
        top_reason = curr.get("top_blocking_reason_1", "")
        if top_reason == "score_not_stable" and curr_opens == 0:
            break

        recommended = curr

    ticks = recommended.get("params", {}).get("min_confirm_ticks", 2)
    opens = recommended.get("opening_count", 0)

    # 信心判断
    first_opens = sorted_exps[0].get("opening_count", 0)
    last_opens = sorted_exps[-1].get("opening_count", 0)
    if first_opens > 0 and last_opens > 0:
        # 两端都有 opening → 区间合理
        confidence = "high"
    elif first_opens > 0:
        confidence = "medium"
    else:
        confidence = "low"

    # 构造 reason
    ticks_opens = [(
        e.get("params", {}).get("min_confirm_ticks"),
        e.get("opening_count", 0),
    ) for e in sorted_exps]
    reason = (
        f"ticks={ticks} 是在 opening_count 不显著下降的前提下最保守的选择 "
        f"(opens={opens}). "
        f"各 ticks 的 opening: {ticks_opens}."
    )

    return {"value": ticks, "confidence": confidence, "reason": reason}


def _recommend_net_edge_threshold() -> dict[str, Any]:
    """min_safe_net_edge_bps — 第一版标记为 pending。"""
    return {
        "value": None,
        "confidence": "low",
        "reason": (
            "Requires dedicated validation batch or broader time window evidence. "
            "Step 1 当前 3 个 batch 不直接扫此参数."
        ),
    }


def _generate_recommendations(
    all_rows: list[dict[str, Any]],
    batch_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """生成完整的 parameter_recommendations。"""
    # 按 batch 名分组
    batch_names = {}
    for br in batch_results:
        s = br.get("summary")
        if s:
            batch_names[br.get("_key")] = s.get("batch_name", "")

    scale_name = batch_names.get("scale_calibration", "")
    cost_name = batch_names.get("cost_sensitivity", "")
    ticks_name = batch_names.get("confirm_ticks", "")

    scale_exps = _get_experiments_by_batch(all_rows, scale_name) if scale_name else []
    cost_exps = _get_experiments_by_batch(all_rows, cost_name) if cost_name else []
    ticks_exps = _get_experiments_by_batch(all_rows, ticks_name) if ticks_name else []

    scale_rec = _recommend_signal_edge_scale(scale_exps)
    cost_rec = _recommend_cost_model(cost_exps)
    ticks_rec = _recommend_confirm_ticks(ticks_exps)
    edge_rec = _recommend_net_edge_threshold()

    return {
        "signal_edge_scale_bps": scale_rec,
        "taker_fee_bps": cost_rec.get("taker_fee_bps", {}),
        "slippage_bps": cost_rec.get("slippage_bps", {}),
        "min_confirm_ticks": ticks_rec,
        "min_safe_net_edge_bps": edge_rec,
        "_cost_overall": {
            "confidence": cost_rec.get("overall_confidence"),
            "reason": cost_rec.get("overall_reason"),
        },
    }


def _write_recommendations(
    recommendations: dict[str, Any],
    round_id: str,
    output_path: pathlib.Path,
) -> pathlib.Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "round_id": round_id,
        "scope": {
            "family": _FAMILY,
            "symbol": _SYMBOL,
            "timeframe": _TIMEFRAME,
        },
        "recommendations": recommendations,
    }
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info("Wrote parameter_recommendations.json → %s", output_path)
    return output_path


# =========================================================================
# 4. 结论文档生成
# =========================================================================


def _build_conclusion_report(
    batch_results: list[dict[str, Any]],
    all_rows: list[dict[str, Any]],
    recommendations: dict[str, Any],
    round_id: str,
    output_path: pathlib.Path,
) -> pathlib.Path:
    """生成 phase2_step1_calibration_conclusion.md。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    _add = lines.append

    now_str = datetime.now(timezone.utc).isoformat()

    # ---- Header ----
    _add("# Phase 2 Step 1: Parameter Calibration Conclusion")
    _add("")
    _add(f"> Round ID: `{round_id}`")
    _add(f"> Generated at: {now_str}")
    _add("")

    # ---- 1. Scope ----
    _add("## 1. Scope")
    _add("")
    _add(f"- **Family**: {_FAMILY}")
    _add(f"- **Symbol**: {_SYMBOL}")
    _add(f"- **Timeframe**: {_TIMEFRAME}")
    # 从第一个有数据的 batch 取 window
    for br in batch_results:
        s = br.get("summary")
        if s and s.get("window"):
            _add(f"- **Window**: {s['window']}")
            break
    _add("")

    # ---- 2. Execution Summary ----
    _add("## 2. Execution Summary")
    _add("")
    _add("| Batch | Experiments | Succeeded | Failed | Status |")
    _add("|-------|-------------|-----------|--------|--------|")
    for br in batch_results:
        s = br.get("summary")
        if s:
            name = s.get("batch_name", "?")
            total = s.get("total_experiments", 0)
            ok = s.get("succeeded", 0)
            fail = s.get("failed", 0)
            status = br["status"]
        else:
            name = br.get("_key", "?")
            total = ok = fail = 0
            status = br["status"]
        _add(f"| {name} | {total} | {ok} | {fail} | {status} |")
    _add("")

    # ---- 3. Results Overview ----
    _add("## 3. Results Overview")
    _add("")
    if all_rows:
        _add("```")
        _add("net_edge = signal_edge + funding_adj - cost")
        _add("```")
        _add("")
        headers = [
            "Batch", "Label", "Opens", "Blocks", "Sel%",
            "Signal(bps)", "Cost(bps)", "NetEdge(bps)", "PosEdge%", "TopBlock",
        ]
        _add("| " + " | ".join(headers) + " |")
        _add("| " + " | ".join(["---"] * len(headers)) + " |")
        for r in all_rows:
            bn = r.get("batch_name", "?")
            # 缩短 batch_name 便于显示
            bn_short = bn.replace("independent_", "").replace("_15m", "")
            _add("| " + " | ".join([
                bn_short,
                r.get("label", ""),
                str(r.get("opening_count", 0)),
                str(r.get("blocked_count", 0)),
                _pct(r.get("selectable_ratio")),
                _fmt(r.get("mean_signal_edge_proxy_bps")),
                _fmt(r.get("mean_cost_bps")),
                _fmt(r.get("mean_expected_edge_bps")),
                _pct(r.get("positive_edge_ratio")),
                f"`{r.get('top_blocking_reason_1', 'none')}`",
            ]) + " |")
        _add("")
    else:
        _add("*(no experiment data available)*")
        _add("")

    # ---- 4. Key Observations ----
    _add("## 4. Key Observations")
    _add("")
    observations = _generate_observations(all_rows, batch_results, recommendations)
    for obs in observations:
        _add(obs)
    _add("")

    # ---- 5. Recommended Default Parameters ----
    _add("## 5. Recommended Default Parameters")
    _add("")
    _add("| Parameter | Value | Confidence | Reason |")
    _add("|-----------|-------|------------|--------|")
    param_keys = [
        ("signal_edge_scale_bps", "signal_edge_scale_bps"),
        ("taker_fee_bps", "taker_fee_bps"),
        ("slippage_bps", "slippage_bps"),
        ("min_confirm_ticks", "min_confirm_ticks"),
        ("min_safe_net_edge_bps", "min_safe_net_edge_bps"),
    ]
    for display_name, key in param_keys:
        rec = recommendations.get(key, {})
        val = rec.get("value", "N/A")
        val_str = str(val) if val is not None else "*(pending)*"
        conf = rec.get("confidence", "N/A")
        reason = rec.get("reason", "")
        # 截断过长 reason 用于表格
        reason_short = reason[:100] + "..." if len(reason) > 100 else reason
        _add(f"| `{display_name}` | {val_str} | {conf} | {reason_short} |")
    _add("")

    # ---- 6. Confidence Summary ----
    _add("## 6. Confidence Summary")
    _add("")
    confs = {}
    for _, key in param_keys:
        rec = recommendations.get(key, {})
        c = rec.get("confidence", "N/A")
        confs.setdefault(c, []).append(key)
    for level in ["high", "medium", "low"]:
        if level in confs:
            params_str = ", ".join(f"`{p}`" for p in confs[level])
            _add(f"- **{level}**: {params_str}")
    _add("")

    # ---- 7. Unresolved Issues ----
    _add("## 7. Unresolved Issues")
    _add("")
    unresolved = []
    net_edge_rec = recommendations.get("min_safe_net_edge_bps", {})
    if net_edge_rec.get("value") is None:
        unresolved.append(
            "- `min_safe_net_edge_bps`: 需要专项 validation batch 或更宽时间窗口数据才能推荐"
        )
    # 检查是否有 low confidence 参数
    for _, key in param_keys:
        rec = recommendations.get(key, {})
        if rec.get("confidence") == "low" and key != "min_safe_net_edge_bps":
            unresolved.append(
                f"- `{key}`: confidence=low, 当前数据不足以做出可靠推荐"
            )
    # 检查是否有 batch 失败
    for br in batch_results:
        if br["status"] == "failed":
            unresolved.append(f"- Batch `{br.get('_key')}` 执行失败, 相关参数推荐可能缺失")
    if not unresolved:
        unresolved.append("- 无重大未解决问题")
    for u in unresolved:
        _add(u)
    _add("")

    # ---- 8. Next Steps ----
    _add("## 8. Next Steps")
    _add("")
    _add("- 扩展 `min_safe_net_edge_bps` 专项 calibration batch")
    _add("- 扩展到 `1H` timeframe 验证参数稳定性")
    _add("- 扩展到 `directional` family")
    _add("- 扩展到多 symbol（ETH-USDT-SWAP 等）")
    _add("- 在更宽时间窗口（1 个月+）上重复验证")
    _add("")

    # ---- 9. Caveats ----
    _add("## 9. Caveats")
    _add("")
    _add("- Phase 2 replay 使用简化评分模型（不含 AI assessment），与生产系统评分存在偏差")
    _add("- 不包含撮合仿真、滑点模型和 orderbook realism（属于 Phase 4）")
    _add("- 持仓逻辑为简化版（固定 1 单位），不反映真实资金管理")
    _add("- 以上推荐基于规则化判断，重点关注相对趋势而非绝对值")
    _add("- 当前数据窗口较短（2 天），推荐结论需在更长窗口上验证")
    _add("")

    content = "\n".join(lines)
    output_path.write_text(content, encoding="utf-8")
    log.info("Wrote conclusion → %s (%d lines)", output_path, len(lines))
    return output_path


def _generate_observations(
    all_rows: list[dict[str, Any]],
    batch_results: list[dict[str, Any]],
    recommendations: dict[str, Any],
) -> list[str]:
    """基于数据生成关键观察列表。"""
    obs: list[str] = []

    # -- Scale 观察 --
    scale_rec = recommendations.get("signal_edge_scale_bps", {})
    scale_val = scale_rec.get("value")
    if scale_val is not None:
        obs.append("### 4.1 Signal Edge Scale")
        obs.append("")
        obs.append(f"- 推荐 scale = **{scale_val}** (confidence: {scale_rec.get('confidence', '?')})")
        # 找默认 scale=10 的 edge
        default_edge = None
        for r in all_rows:
            if r.get("params", {}).get("signal_edge_scale_bps") == 10:
                default_edge = r.get("mean_expected_edge_bps")
                break
        if default_edge is not None:
            if default_edge < 0:
                obs.append(f"- 默认 scale=10 的 net edge 为负 ({default_edge:.2f} bps), 说明当前默认值偏低")
            else:
                obs.append(f"- 默认 scale=10 的 net edge 为正 ({default_edge:.2f} bps)")
        obs.append("")

    # -- Cost 观察 --
    cost_info = recommendations.get("_cost_overall", {})
    if cost_info.get("reason"):
        obs.append("### 4.2 Cost Model")
        obs.append("")
        obs.append(f"- {cost_info['reason']}")
        # 检查 edge 对 cost 的敏感度
        cost_rows = [
            r for r in all_rows
            if r.get("batch_name", "").startswith("independent_cost")
        ]
        if len(cost_rows) >= 2:
            edges = [(
                _extract_total_cost(r.get("params", {})),
                r.get("mean_expected_edge_bps") or 0,
            ) for r in cost_rows]
            edges.sort()
            if edges[-1][1] < 0 and edges[0][1] < 0:
                obs.append("- **警告**: 所有成本水平下 net edge 均为负, 当前 signal 强度不足以覆盖任何合理成本")
            elif edges[0][1] > 0 and edges[-1][1] < 0:
                obs.append("- Edge 对成本敏感: 低成本时为正, 高成本时为负 → signal-cost 平衡脆弱")
        obs.append("")

    # -- Ticks 观察 --
    ticks_rec = recommendations.get("min_confirm_ticks", {})
    ticks_val = ticks_rec.get("value")
    if ticks_val is not None:
        obs.append("### 4.3 Confirmation Ticks")
        obs.append("")
        obs.append(f"- 推荐 ticks = **{ticks_val}** (confidence: {ticks_rec.get('confidence', '?')})")
        ticks_rows = [
            r for r in all_rows
            if r.get("batch_name", "").startswith("independent_confirm")
        ]
        if ticks_rows:
            sorted_t = sorted(ticks_rows, key=lambda r: r.get("params", {}).get("min_confirm_ticks", 0))
            first_opens = sorted_t[0].get("opening_count", 0)
            last_opens = sorted_t[-1].get("opening_count", 0)
            if first_opens > 0 and last_opens == 0:
                obs.append(
                    f"- 最严 ticks={sorted_t[-1].get('params', {}).get('min_confirm_ticks')} "
                    f"导致 opening=0 (策略完全不触发), 当前区间偏严"
                )
            elif first_opens > 0 and last_opens > 0:
                drop = (first_opens - last_opens) / first_opens
                obs.append(
                    f"- Opening 从 ticks={sorted_t[0].get('params', {}).get('min_confirm_ticks')} 到 "
                    f"ticks={sorted_t[-1].get('params', {}).get('min_confirm_ticks')} 下降 {drop:.0%}"
                )
        obs.append("")

    # -- Net edge threshold --
    obs.append("### 4.4 Net Edge Threshold (`min_safe_net_edge_bps`)")
    obs.append("")
    obs.append("- 当前 Step 1 不包含专项 batch, 状态: **pending**")
    obs.append("- 需要在 Step 2 或后续轮次中补充专项 calibration")
    obs.append("")

    # -- 总体敏感度排名 --
    obs.append("### 4.5 Parameter Sensitivity Ranking")
    obs.append("")
    # 按 batch 看 opening_count 的变化幅度
    sensitivity: list[tuple[str, float]] = []
    for br in batch_results:
        s = br.get("summary")
        if not s:
            continue
        exps = s.get("experiments", [])
        opens_list = [e.get("opening_count", 0) for e in exps]
        if opens_list and max(opens_list) > 0:
            spread = max(opens_list) - min(opens_list)
            norm = spread / max(max(opens_list), 1)
            sensitivity.append((s.get("batch_name", "?"), norm))

    sensitivity.sort(key=lambda x: -x[1])
    for rank, (name, norm) in enumerate(sensitivity, 1):
        name_short = name.replace("independent_", "").replace("_15m", "")
        obs.append(f"- {rank}. **{name_short}**: opening 变化幅度 {norm:.0%}")
    obs.append("")

    if not obs:
        obs.append("- 无足够数据生成观察")

    return obs


# =========================================================================
# 5. Round Manifest
# =========================================================================


def _write_manifest(
    batch_results: list[dict[str, Any]],
    round_id: str,
    started_at: str,
    finished_at: str,
    output_path: pathlib.Path,
) -> pathlib.Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    batch_runs = []
    for br in batch_results:
        batch_runs.append({
            "key": br.get("_key"),
            "batch_run_id": br.get("batch_run_id"),
            "batch_dir": br.get("batch_dir"),
            "status": br["status"],
            "exit_code": br.get("exit_code"),
        })

    n_ok = sum(1 for br in batch_results if br["status"] == "succeeded")
    n_fail = len(batch_results) - n_ok
    if n_fail == 0:
        overall = "succeeded"
    elif n_ok == 0:
        overall = "failed"
    else:
        overall = "partial_success"

    manifest = {
        "round_id": round_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "family": _FAMILY,
        "symbol": _SYMBOL,
        "timeframe": _TIMEFRAME,
        "batch_runs": batch_runs,
        "status": overall,
    }

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    log.info("Wrote round_manifest.json → %s", output_path)
    return output_path


# =========================================================================
# Helpers
# =========================================================================

def _pct(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v * 100:.2f}%"


def _fmt(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v:.4f}"


# =========================================================================
# 主流程
# =========================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Step 1 Calibration Orchestrator: "
                    "自动运行 3 个 calibration batch + 规则化推荐 + 结论文档",
    )
    parser.add_argument(
        "--artifact-root", type=str, default=str(_DEFAULT_ARTIFACT_ROOT),
        help=f"Artifact output root (default: {_DEFAULT_ARTIFACT_ROOT})",
    )
    parser.add_argument(
        "--ensure-schema", action="store_true",
        help="Run DB migrations before first batch",
    )
    parser.add_argument(
        "--stop-on-error", action="store_true",
        help="Stop entire round on first batch failure",
    )
    parser.add_argument(
        "--no-print-summary", action="store_true",
        help="Suppress final summary to stdout",
    )
    args = parser.parse_args()

    started_at = datetime.now(timezone.utc).isoformat()
    round_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
    artifact_root = pathlib.Path(args.artifact_root)
    round_dir = artifact_root / round_id

    log.info("=" * 66)
    log.info("Step 1 Calibration Orchestrator")
    log.info("  Round ID  : %s", round_id)
    log.info("  Scope     : %s / %s / %s", _FAMILY, _SYMBOL, _TIMEFRAME)
    log.info("  Batches   : %d", len(_BATCH_DEFS))
    log.info("  Output    : %s", round_dir)
    log.info("=" * 66)

    # ---- 10.2 运行 3 个固定 batch ----
    batch_results: list[dict[str, Any]] = []
    batch_artifact_root = round_dir / "batches"

    for i, bdef in enumerate(_BATCH_DEFS):
        log.info("")
        log.info("[Batch %d/%d] %s", i + 1, len(_BATCH_DEFS), bdef["description"])
        log.info("  File: %s", bdef["file"])

        # 只在第一个 batch 时跑 migration
        ensure = args.ensure_schema and (i == 0)

        result = _run_batch(
            bdef["file"],
            batch_artifact_root,
            ensure_schema=ensure,
            stop_on_error=args.stop_on_error,
        )
        result["_key"] = bdef["key"]

        if result["status"] == "succeeded":
            s = result.get("summary", {})
            log.info("  Result: SUCCEEDED (%d experiments)", s.get("succeeded", 0))
        elif result["status"] == "partial_success":
            s = result.get("summary", {})
            log.info("  Result: PARTIAL (%d ok, %d failed)",
                     s.get("succeeded", 0), s.get("failed", 0))
        else:
            log.error("  Result: FAILED — %s", (result.get("error") or "")[:200])

        batch_results.append(result)

        if result["status"] == "failed" and args.stop_on_error:
            log.error("--stop-on-error: aborting after batch failure")
            break

    # ---- 10.4 构建 round_summary ----
    log.info("")
    log.info("Building round summary...")
    all_rows = _collect_all_experiments(batch_results)
    _write_round_summary_csv(all_rows, round_dir / "round_summary.csv")
    _write_round_summary_json(all_rows, round_id, round_dir / "round_summary.json")

    # ---- 10.5 规则化推荐 ----
    log.info("Running recommendation engine...")
    recommendations = _generate_recommendations(all_rows, batch_results)
    _write_recommendations(recommendations, round_id,
                           round_dir / "parameter_recommendations.json")

    # ---- 10.6 结论文档 ----
    log.info("Building conclusion report...")
    _build_conclusion_report(
        batch_results, all_rows, recommendations, round_id,
        round_dir / "phase2_step1_calibration_conclusion.md",
    )

    # ---- Manifest ----
    finished_at = datetime.now(timezone.utc).isoformat()
    _write_manifest(
        batch_results, round_id, started_at, finished_at,
        round_dir / "round_manifest.json",
    )

    # ---- 最终汇总 ----
    n_ok = sum(1 for br in batch_results if br["status"] == "succeeded")
    n_partial = sum(1 for br in batch_results if br["status"] == "partial_success")
    n_fail = sum(1 for br in batch_results if br["status"] == "failed")
    total_exps = len(all_rows)

    log.info("")
    log.info("=" * 66)
    log.info("Step 1 completed: %d batches succeeded, %d partial, %d failed",
             n_ok, n_partial, n_fail)
    log.info("Total experiments: %d", total_exps)
    log.info("Round dir: %s", round_dir)
    log.info("=" * 66)

    if not args.no_print_summary:
        print("")
        print(f"=== Step 1 Calibration: {round_id} ===")
        print(f"Scope: {_FAMILY} / {_SYMBOL} / {_TIMEFRAME}")
        print(f"Batches: {n_ok} succeeded, {n_partial} partial, {n_fail} failed")
        print(f"Total experiments: {total_exps}")
        print("")

        # 推荐参数汇总
        print("Recommended Parameters:")
        for pname in ["signal_edge_scale_bps", "taker_fee_bps", "slippage_bps",
                       "min_confirm_ticks", "min_safe_net_edge_bps"]:
            rec = recommendations.get(pname, {})
            val = rec.get("value", "N/A")
            conf = rec.get("confidence", "?")
            val_str = str(val) if val is not None else "(pending)"
            print(f"  {pname:<30s} = {val_str:<10s} [{conf}]")

        print("")
        print(f"Conclusion: {round_dir / 'phase2_step1_calibration_conclusion.md'}")
        print(f"Artifacts : {round_dir}")

    # 退出码
    if n_fail > 0 and n_ok + n_partial == 0:
        sys.exit(3)
    elif n_fail > 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
