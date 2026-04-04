#!/usr/bin/env python3
"""One-shot Live Attribution Runner.

对给定 family / symbol / timeframe / time window 做一次 replay vs live 对照归因，
回答"最近为什么没下单"。

Usage:
    # 基础用法
    python scripts/rdp_run_live_attribution.py \
        --family independent \
        --symbol BTC-USDT-SWAP \
        --timeframe 15m \
        --start 2026-03-31 \
        --end 2026-04-02

    # 指定 live DB 连接
    python scripts/rdp_run_live_attribution.py \
        --family independent \
        --symbol BTC-USDT-SWAP \
        --timeframe 15m \
        --start 2026-03-31 \
        --end 2026-04-02 \
        --live-db-url "postgresql+psycopg://localhost:5432/aats_derivatives"

    # 确保 schema 就绪
    python scripts/rdp_run_live_attribution.py \
        --family independent \
        --symbol BTC-USDT-SWAP \
        --timeframe 15m \
        --start 2026-03-31 \
        --end 2026-04-02 \
        --ensure-schema

    # 跳过 live 查询（仅做 replay 分析）
    python scripts/rdp_run_live_attribution.py \
        --family independent \
        --symbol BTC-USDT-SWAP \
        --timeframe 15m \
        --start 2026-03-31 \
        --end 2026-04-02 \
        --replay-only

Exit codes:
    0 = 成功
    1 = 参数错误
    2 = 部分成功（replay 正常但 live 连接失败）
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import pathlib
import sys
from datetime import datetime, timezone
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_live_attribution")

_DEFAULT_ARTIFACT_ROOT = pathlib.Path("artifacts/research/attribution_rounds")


# =========================================================================
# Replay 执行
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

    # 转换�� dict 列表
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

    log.info("Replay completed: %d decisions, %d openings, %d selectable",
             len(rows),
             sum(1 for r in rows if r["action"] == "open"),
             sum(1 for r in rows if r["selectable"]))
    return rows


# =========================================================================
# Live 数据查询 & 对齐
# =========================================================================


def _create_live_session(live_db_url: str) -> Any:
    """创建 live DB session。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(live_db_url, echo=False)
    return Session(engine)


def _run_alignment_and_attribution(
    replay_decisions: list[dict[str, Any]],
    *,
    family: str,
    symbol: str,
    timeframe: str,
    start_ts: datetime,
    end_ts: datetime,
    live_db_url: str | None,
    replay_only: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """执行对齐 + ���因，返回 (alignment_rows, classified_rows)。"""
    from aats.data_platform.attribution.alignment import (
        align_replay_with_live,
        query_live_allocations,
        query_live_budget_snapshots,
        query_live_bundles,
        query_live_fills,
        query_live_intents,
        query_live_orders,
        query_reconciliation_snapshots,
    )
    from aats.data_platform.attribution.layer_classifier import classify_all

    if replay_only or not live_db_url:
        log.info("Replay-only mode: skipping live data queries")
        # 生成 replay_only 对齐行
        alignment_rows = align_replay_with_live(
            replay_decisions, [],
            timeframe=timeframe,
        )
        classified = classify_all(alignment_rows)
        return alignment_rows, classified

    log.info("Connecting to live DB...")
    live_session = _create_live_session(live_db_url)

    try:
        # Step 1: 查询 live intents
        log.info("Querying live intents...")
        live_intents = query_live_intents(
            live_session,
            family=family, symbol=symbol,
            start_ts=start_ts, end_ts=end_ts,
        )
        log.info("Found %d live intents", len(live_intents))

        # Step 2: 对齐
        log.info("Aligning replay with live...")
        alignment_rows = align_replay_with_live(
            replay_decisions, live_intents,
            timeframe=timeframe,
        )

        # Step 3: 批量查询关联数据
        alloc_ids = list({
            r["live_allocation_id"] for r in alignment_rows
            if r.get("live_allocation_id")
        })
        decision_ids = list({
            r["live_decision_id"] for r in alignment_rows
            if r.get("live_decision_id")
        })

        log.info("Querying allocations (%d)...", len(alloc_ids))
        allocations = query_live_allocations(live_session, allocation_ids=alloc_ids)

        log.info("Querying budget snapshots...")
        budgets = query_live_budget_snapshots(live_session, allocation_ids=alloc_ids)

        log.info("Querying bundles (%d decision_ids)...", len(decision_ids))
        bundles = query_live_bundles(live_session, decision_ids=decision_ids)

        log.info("Querying orders...")
        orders = query_live_orders(live_session, decision_ids=decision_ids)

        # 收集 order_ids for fills
        all_order_ids: list[str] = []
        for ords in orders.values():
            for o in ords:
                all_order_ids.append(str(o["order_id"]))

        log.info("Querying fills (%d orders)...", len(all_order_ids))
        fills = query_live_fills(live_session, order_ids=all_order_ids)

        log.info("Querying reconciliation snapshots...")
        recon = query_reconciliation_snapshots(
            live_session, symbol=symbol,
            start_ts=start_ts, end_ts=end_ts,
        )

        # Step 4: 分层归因
        log.info("Running layer classification...")
        classified = classify_all(
            alignment_rows,
            allocations=allocations,
            budgets=budgets,
            bundles=bundles,
            orders=orders,
            fills=fills,
            recon_snapshots=recon,
        )
        return alignment_rows, classified

    finally:
        live_session.close()


# =========================================================================
# 产物写入
# =========================================================================


def _write_alignment_csv(
    rows: list[dict[str, Any]],
    output_path: pathlib.Path,
) -> pathlib.Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        output_path.write_text("", encoding="utf-8")
        return output_path

    fieldnames = [
        "family", "symbol", "timeframe",
        "replay_ts", "live_ts", "alignment_status",
        "replay_opening", "live_opening",
        "final_attribution_category", "final_attribution_reason",
        "strategy_reason", "permission_reason",
        "allocator_reason", "budget_reason",
        "risk_reason", "execution_reason",
        "order_status", "fill_status",
        "replay_action", "replay_selectable", "replay_execution_compatible",
        "replay_blocking_reasons", "replay_expected_net_edge_bps",
        "live_state", "live_route_action", "live_automatic_enabled",
    ]

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    log.info("Wrote alignment CSV -> %s (%d rows)", output_path, len(rows))
    return output_path


def _write_json(data: Any, output_path: pathlib.Path) -> pathlib.Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    log.info("Wrote JSON -> %s", output_path)
    return output_path


# =========================================================================
# 主流程
# =========================================================================


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-shot Live Attribution: replay vs live 对照归因",
    )
    parser.add_argument("--family", required=True, choices=["independent", "directional"])
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", required=True)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD (UTC)")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD (UTC)")
    parser.add_argument("--dataset-version", default="v1.0")
    parser.add_argument(
        "--live-db-url", default=None,
        help="Live AATS database URL (default: env RDP_LIVE_DATABASE_URL)",
    )
    parser.add_argument(
        "--replay-only", action="store_true",
        help="Skip live data queries, only run replay analysis",
    )
    parser.add_argument(
        "--artifact-root", type=str, default=str(_DEFAULT_ARTIFACT_ROOT),
    )
    parser.add_argument("--ensure-schema", action="store_true")
    args = parser.parse_args()

    # Live DB URL: CLI > env > None
    live_db_url = args.live_db_url or os.environ.get("RDP_LIVE_DATABASE_URL")
    if not live_db_url and not args.replay_only:
        log.warning(
            "No --live-db-url or RDP_LIVE_DATABASE_URL set. "
            "Running in replay-only mode."
        )
        args.replay_only = True

    start_ts = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_ts = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    # 产物目录
    artifact_root = pathlib.Path(args.artifact_root)
    run_id = f"{args.family}_{args.timeframe}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    run_dir = artifact_root / run_id

    log.info("=" * 60)
    log.info("Live Attribution Runner")
    log.info("  Family    : %s", args.family)
    log.info("  Symbol    : %s", args.symbol)
    log.info("  Timeframe : %s", args.timeframe)
    log.info("  Window    : %s ~ %s", args.start, args.end)
    log.info("  Replay-only: %s", args.replay_only)
    log.info("  Output    : %s", run_dir)
    log.info("=" * 60)

    # ---- Step 1: Run replay ----
    from aats.data_platform.config import get_settings
    from aats.data_platform.db import get_session, run_migrations

    settings = get_settings()
    if args.ensure_schema:
        log.info("Running migrations...")
        run_migrations(settings)

    log.info("Running replay...")
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

    # ---- Step 2: Alignment + Attribution ----
    alignment_rows, classified_rows = _run_alignment_and_attribution(
        replay_decisions,
        family=args.family,
        symbol=args.symbol,
        timeframe=args.timeframe,
        start_ts=start_ts,
        end_ts=end_ts,
        live_db_url=live_db_url,
        replay_only=args.replay_only,
    )

    # 给分类后的 rows 补充 family/symbol/timeframe
    for row in classified_rows:
        row.setdefault("family", args.family)
        row.setdefault("symbol", args.symbol)
        row.setdefault("timeframe", args.timeframe)

    # ---- Step 3: Aggregation ----
    from aats.data_platform.attribution.aggregation import (
        build_attribution_summary,
        build_layer_analysis,
        build_top_failure_modes,
    )

    summary = build_attribution_summary(
        classified_rows, family=args.family, timeframe=args.timeframe,
    )
    top_fm = build_top_failure_modes(classified_rows)
    layer_analysis = build_layer_analysis(classified_rows)

    # ---- Step 4: Write artifacts ----
    _write_alignment_csv(classified_rows, run_dir / "replay_live_alignment.csv")
    _write_json(summary, run_dir / "attribution_summary.json")
    _write_json(top_fm, run_dir / "top_failure_modes.json")

    from aats.data_platform.attribution.report_builder import build_attribution_report

    build_attribution_report(
        family=args.family,
        symbol=args.symbol,
        timeframe=args.timeframe,
        start=args.start,
        end=args.end,
        alignment_rows=alignment_rows,
        classified_rows=classified_rows,
        summary=summary,
        top_failure_modes=top_fm,
        layer_analysis=layer_analysis,
        output_path=run_dir / "live_attribution_report.md",
    )

    # ---- Summary ----
    total = len(classified_rows)
    n_failures = top_fm.get("total_failures", 0)
    n_success = top_fm.get("total_success", 0)
    n_na = top_fm.get("total_not_applicable", 0)

    log.info("")
    log.info("=" * 60)
    log.info("Attribution complete:")
    log.info("  Total events: %d", total)
    log.info("  Failures: %d", n_failures)
    log.info("  Success (live traded): %d", n_success)
    log.info("  Not applicable: %d", n_na)
    log.info("  Artifacts: %s", run_dir)
    log.info("=" * 60)

    print("")
    print(f"=== Attribution: {args.family}/{args.timeframe} ===")
    print(f"Window: {args.start} ~ {args.end}")
    print(f"Total: {total} | Failures: {n_failures} | Success: {n_success}")
    print("")

    top_cats = top_fm.get("top_categories", [])[:5]
    if top_cats:
        print("Top failure categories:")
        for tc in top_cats:
            print(f"  {tc['category']:<30s} {tc['count']:>4d} ({tc['ratio']:.1%})")

    print("")
    print(f"Report: {run_dir / 'live_attribution_report.md'}")
    print(f"Artifacts: {run_dir}")

    if args.replay_only and n_failures == 0 and n_success == 0:
        sys.exit(0)
    elif not args.replay_only and live_db_url and n_failures > 0:
        sys.exit(0)


if __name__ == "__main__":
    main()
