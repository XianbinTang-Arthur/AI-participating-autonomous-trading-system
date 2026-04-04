#!/usr/bin/env python3
"""Run a parameter scan.

Phase 2 入口脚本：批量扫描参数组合，生成 comparison summary。

Usage:
    # 使用默认参数网格 (3x3x3 = 27 组合)
    python scripts/rdp_run_parameter_scan.py \
        --family independent \
        --symbol BTC-USDT-SWAP \
        --timeframe 15m \
        --start 2026-01-01 \
        --end 2026-04-01

    # 自定义参数网格
    python scripts/rdp_run_parameter_scan.py \
        --family independent \
        --symbol BTC-USDT-SWAP \
        --timeframe 15m \
        --start 2026-01-01 \
        --end 2026-04-01 \
        --grid '{"min_confirm_ticks":[2,3],"min_safe_net_edge_bps":[5,10]}'
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_parameter_scan")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a parameter scan")
    parser.add_argument("--family", required=True, choices=["independent", "directional"])
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", required=True)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD (UTC)")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD (UTC)")
    parser.add_argument("--dataset-version", default="v1")
    parser.add_argument("--grid", default=None, help="JSON parameter grid")
    args = parser.parse_args()

    from aats.data_platform.config import get_settings
    from aats.data_platform.db import get_session, run_migrations
    from aats.data_platform.replay.adapters.directional_adapter import DirectionalReplayAdapter
    from aats.data_platform.replay.adapters.independent_adapter import IndependentReplayAdapter
    from aats.data_platform.replay.reports.markdown_report_builder import build_scan_comparison_report
    from aats.data_platform.replay.scan.scan_runner import run_parameter_scan

    settings = get_settings()
    run_migrations(settings)

    if args.family == "independent":
        adapter = IndependentReplayAdapter()
    else:
        adapter = DirectionalReplayAdapter()

    grid = json.loads(args.grid) if args.grid else None
    start_ts = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_ts = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    with get_session(settings) as session:
        scan_run_id = run_parameter_scan(
            session,
            adapter=adapter,
            symbol=args.symbol,
            timeframe=args.timeframe,
            dataset_version=args.dataset_version,
            start_ts=start_ts,
            end_ts=end_ts,
            parameter_grid=grid,
        )

        # 读取 comparison 并生成报告
        scan_dir = pathlib.Path("artifacts/research/experiments") / str(scan_run_id)
        comp_file = scan_dir / "comparison_summary.json"
        if comp_file.exists():
            comparison = json.loads(comp_file.read_text(encoding="utf-8"))
            report_path = build_scan_comparison_report(
                scan_info={
                    "scan_run_id": str(scan_run_id),
                    "family": args.family,
                    "symbol": args.symbol,
                    "timeframe": args.timeframe,
                },
                comparison=comparison,
                output_path=scan_dir / "comparison_report.md",
            )
            print(f"\nScan comparison report: {report_path}")

        print(f"\n=== Scan {scan_run_id} completed ===")
        print(f"Artifacts: {scan_dir}")


if __name__ == "__main__":
    main()
