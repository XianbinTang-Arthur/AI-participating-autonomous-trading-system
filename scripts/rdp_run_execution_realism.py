#!/usr/bin/env python3
"""One-shot Execution Realism Runner (Phase 4).

对给定 family / symbol / timeframe / time window 做一次 execution realism 分析，
回答"这笔单在真实市场微观结构下是否可成交、成本多少"。

分析链：
  Replay Decision
    -> Market Alignment (Gold bar 匹配)
    -> Fill Feasibility (volume-based 可成交性)
    -> Slippage Estimation (bar-proxy 滑点模型)
    -> Execution Cost Summary

V1 使用 Gold OHLCV bars 作为市场快照代理。

Usage:
    # 基础用法
    python scripts/rdp_run_execution_realism.py \
        --family independent \
        --symbol BTC-USDT-SWAP \
        --timeframe 15m \
        --start 2026-03-31 --end 2026-04-02

    # 指定 taker fee
    python scripts/rdp_run_execution_realism.py \
        --family independent \
        --symbol BTC-USDT-SWAP \
        --timeframe 15m \
        --start 2026-03-31 --end 2026-04-02 \
        --taker-fee-bps 3.0

    # 确保 schema 就绪
    python scripts/rdp_run_execution_realism.py \
        --family independent \
        --symbol BTC-USDT-SWAP \
        --timeframe 15m \
        --start 2026-03-31 --end 2026-04-02 \
        --ensure-schema

Exit codes:
    0 = 成功
    1 = 参数错误
    2 = 部分成功（replay 正常但无 bar 数据匹配）
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import pathlib
import sys
from datetime import datetime, timezone
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_execution_realism")

_DEFAULT_ARTIFACT_ROOT = pathlib.Path("artifacts/research/execution_rounds")


# =========================================================================
# Replay 执行（与 Phase 3 相同模式）
# =========================================================================


def _run_replay(
    session: Any,
    *,
    family: str,
    symbol: str,
    timeframe: str,
    dataset_version: str,
    start_ts: datetime,
    end_ts: datetime,
) -> list[dict[str, Any]]:
    """运行 replay 并返回 decisions 字典列表。"""
    from aats.data_platform.replay.adapters.directional_adapter import DirectionalReplayAdapter
    from aats.data_platform.replay.adapters.independent_adapter import IndependentReplayAdapter
    from aats.data_platform.replay.core.replay_context import ReplayParameterOverrides
    from aats.data_platform.replay.core.replay_runner import run_replay

    if family == "independent":
        adapter = IndependentReplayAdapter()
    elif family == "directional":
        adapter = DirectionalReplayAdapter()
    else:
        raise ValueError(f"Unknown family: {family}")

    params = ReplayParameterOverrides()
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

    rows: list[dict[str, Any]] = []
    for d in decisions:
        row = {
            "ts": d.ts.isoformat() if hasattr(d.ts, "isoformat") else str(d.ts),
            "family": d.family,
            "symbol": d.symbol,
            "timeframe": d.timeframe,
            "state": d.state,
            "action": d.action,
            "selectable": d.selectable,
            "execution_compatible": d.execution_compatible,
            "score_stable": d.score_stable,
            "long_score": d.long_score,
            "short_score": d.short_score,
            "blocking_reasons": "|".join(d.blocking_reasons) if d.blocking_reasons else "",
            "signal_edge_proxy_bps": d.signal_edge_proxy_bps,
            "funding_adjustment_bps": d.funding_adjustment_bps,
            "cost_bps": d.cost_bps,
            "expected_net_edge_bps": d.expected_net_edge_bps,
            "target_position_qty": float(d.target_position_qty),
            "delta_position_qty": float(d.delta_position_qty),
            "funding_rate": d.funding_rate,
            "close_price": d.close_price,
            "bar_index": d.bar_index,
        }
        rows.append(row)

    log.info("Replay completed: %d decisions, %d openings, %d closes",
             len(rows),
             sum(1 for r in rows if r["action"] == "open"),
             sum(1 for r in rows if r["action"] == "close"))
    return rows


# =========================================================================
# 产物写入
# =========================================================================


def _write_csv(
    rows: list[dict[str, Any]],
    output_path: pathlib.Path,
    fieldnames: list[str],
) -> pathlib.Path:
    """写入 CSV 文件。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        output_path.write_text("", encoding="utf-8")
        return output_path

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    log.info("Wrote CSV -> %s (%d rows)", output_path, len(rows))
    return output_path


def _write_json(data: Any, output_path: pathlib.Path) -> pathlib.Path:
    """写入 JSON 文件。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    log.info("Wrote JSON -> %s", output_path)
    return output_path


# =========================================================================
# CSV 字段定义
# =========================================================================

_ALIGNMENT_FIELDS = [
    "family", "symbol", "timeframe",
    "candidate_ts", "candidate_source", "candidate_side",
    "candidate_qty", "candidate_notional_usd", "candidate_action",
    "snapshot_ts", "trades_window_start", "trades_window_end",
    "alignment_status",
    "bar_open", "bar_high", "bar_low", "bar_close",
    "bar_volume", "bar_quote_volume", "bar_range_bps",
    "aligned_funding_rate",
    "signal_edge_proxy_bps", "funding_adjustment_bps",
    "cost_bps", "expected_net_edge_bps",
]

_FEASIBILITY_FIELDS = [
    "candidate_id", "family", "timeframe",
    "candidate_ts", "candidate_side", "candidate_qty",
    "book_depth_available_qty", "fillable_qty", "fillable_ratio",
    "volume_ratio", "levels_consumed",
    "full_fill_possible", "partial_fill_possible",
    "feasibility_category",
]

_SLIPPAGE_FIELDS = [
    "candidate_id", "family", "timeframe",
    "candidate_ts", "candidate_side", "candidate_qty",
    "candidate_action",
    "arrival_mid_px", "estimated_fill_vwap_px",
    "half_spread_bps", "volume_impact_bps",
    "estimated_slippage_bps", "estimated_fee_bps",
    "estimated_total_execution_cost_bps",
    "cost_vs_assumed_bps", "cost_adjusted_edge_bps",
    "slippage_model", "slippage_data_quality",
    "bar_range_bps", "bar_volume",
    "signal_edge_proxy_bps", "expected_net_edge_bps",
]


# =========================================================================
# 主流程
# =========================================================================


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-shot Execution Realism: 市场微观结构可行性分析",
    )
    parser.add_argument("--family", required=True, choices=["independent", "directional"])
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", required=True)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD (UTC)")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD (UTC)")
    parser.add_argument("--dataset-version", default="v1.0")
    parser.add_argument(
        "--taker-fee-bps", type=float, default=5.0,
        help="Taker fee in bps (default: 5.0)",
    )
    parser.add_argument(
        "--artifact-root", type=str, default=str(_DEFAULT_ARTIFACT_ROOT),
    )
    parser.add_argument("--ensure-schema", action="store_true")
    args = parser.parse_args()

    start_ts = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_ts = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    # 产物目录
    artifact_root = pathlib.Path(args.artifact_root)
    run_id = f"{args.family}_{args.timeframe}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    run_dir = artifact_root / run_id

    log.info("=" * 60)
    log.info("Execution Realism Runner (Phase 4)")
    log.info("  Family    : %s", args.family)
    log.info("  Symbol    : %s", args.symbol)
    log.info("  Timeframe : %s", args.timeframe)
    log.info("  Window    : %s ~ %s", args.start, args.end)
    log.info("  Taker fee : %.1f bps", args.taker_fee_bps)
    log.info("  Output    : %s", run_dir)
    log.info("=" * 60)

    # ---- Step 1: DB 准备 + Replay ----
    from aats.data_platform.config import get_settings
    from aats.data_platform.db import get_session, run_migrations

    settings = get_settings()
    if args.ensure_schema:
        log.info("Running migrations...")
        run_migrations(settings)

    log.info("Step 1: Running replay...")
    with get_session(settings) as session:
        replay_decisions = _run_replay(
            session,
            family=args.family,
            symbol=args.symbol,
            timeframe=args.timeframe,
            dataset_version=args.dataset_version,
            start_ts=start_ts,
            end_ts=end_ts,
        )

    # ---- Step 2: Market Alignment ----
    log.info("Step 2: Market data alignment...")
    from aats.data_platform.execution_realism.market_alignment import (
        build_execution_alignment,
        query_gold_bars_for_window,
    )

    with get_session(settings) as session:
        gold_bars = query_gold_bars_for_window(
            session,
            symbol=args.symbol,
            timeframe=args.timeframe,
            start_ts=start_ts,
            end_ts=end_ts,
            dataset_version=args.dataset_version,
        )

    aligned_rows = build_execution_alignment(
        replay_decisions, gold_bars,
        family=args.family,
        symbol=args.symbol,
        timeframe=args.timeframe,
    )

    # ---- Step 3: Fill Feasibility ----
    log.info("Step 3: Fill feasibility evaluation...")
    from aats.data_platform.execution_realism.fill_feasibility import evaluate_fill_feasibility

    feasibility_rows = evaluate_fill_feasibility(aligned_rows)

    # ---- Step 4: Slippage Estimation ----
    log.info("Step 4: Slippage estimation...")
    from aats.data_platform.execution_realism.slippage_estimator import estimate_slippage

    slippage_rows = estimate_slippage(
        feasibility_rows,
        taker_fee_bps=args.taker_fee_bps,
    )

    # ---- Step 5: Execution Cost Summary ----
    log.info("Step 5: Building execution cost summary...")
    from aats.data_platform.execution_realism.execution_cost_model import build_execution_cost_summary

    cost_summary = build_execution_cost_summary(slippage_rows)

    # ---- Step 6: Write artifacts ----
    log.info("Step 6: Writing artifacts...")
    _write_csv(aligned_rows, run_dir / "execution_alignment.csv", _ALIGNMENT_FIELDS)
    _write_csv(feasibility_rows, run_dir / "fill_feasibility_summary.csv", _FEASIBILITY_FIELDS)
    _write_csv(slippage_rows, run_dir / "slippage_summary.csv", _SLIPPAGE_FIELDS)
    _write_json(cost_summary, run_dir / "execution_cost_summary.json")

    # ---- Step 7: Report ----
    log.info("Step 7: Building report...")
    from aats.data_platform.execution_realism.report_builder import build_execution_realism_report

    build_execution_realism_report(
        family=args.family,
        symbol=args.symbol,
        timeframe=args.timeframe,
        start=args.start,
        end=args.end,
        aligned_rows=aligned_rows,
        feasibility_rows=feasibility_rows,
        slippage_rows=slippage_rows,
        cost_summary=cost_summary,
        output_path=run_dir / "live_execution_realism_report.md",
    )

    # ---- Summary ----
    total = cost_summary.get("total_candidates", 0)
    full_fill = cost_summary.get("full_fill_ratio", 0)
    slip = cost_summary.get("slippage", {}).get("mean", 0)
    total_cost = cost_summary.get("total_execution_cost", {}).get("mean", 0)
    pe_ratio = cost_summary.get("positive_edge_ratio", 0)

    log.info("")
    log.info("=" * 60)
    log.info("Execution realism complete:")
    log.info("  Total candidates  : %d", total)
    log.info("  Full fill ratio   : %.1f%%", full_fill * 100)
    log.info("  Mean slippage     : %.2f bps", slip)
    log.info("  Mean total cost   : %.2f bps", total_cost)
    log.info("  Positive edge %%   : %.1f%%", pe_ratio * 100)
    log.info("  Artifacts: %s", run_dir)
    log.info("=" * 60)

    print("")
    print(f"=== Execution Realism: {args.family}/{args.timeframe} ===")
    print(f"Window: {args.start} ~ {args.end}")
    print(f"Candidates: {total} | Full fill: {full_fill:.1%} | Mean slip: {slip:.2f} bps")
    print(f"Mean total cost: {total_cost:.2f} bps | Positive edge: {pe_ratio:.1%}")
    print("")
    print(f"Report: {run_dir / 'live_execution_realism_report.md'}")
    print(f"Artifacts: {run_dir}")

    # 退出码
    matched = sum(1 for r in aligned_rows if r.get("alignment_status") == "matched")
    if total > 0 and matched == 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
