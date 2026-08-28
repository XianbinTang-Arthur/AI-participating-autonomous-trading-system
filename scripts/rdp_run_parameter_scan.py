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
import hashlib
import json
import logging
import pathlib
import sys
from datetime import datetime, timezone

from aats.data_platform.governance._atomic_io import immutable_json_write

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_parameter_scan")

_SCAN_RESULT_PREFIX = "RDP_PARAMETER_SCAN_RESULT_JSON="


def _normalize_dataset_version(value: str | None) -> str:
    normalized = (value or "v1.0").strip()
    return "v1.0" if normalized == "v1" else normalized


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a parameter scan")
    parser.add_argument("--family", required=True, choices=["independent", "directional"])
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", required=True)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD (UTC)")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD (UTC)")
    parser.add_argument("--dataset-version", default="v1.0")
    parser.add_argument("--grid", default=None, help="JSON parameter grid")
    parser.add_argument(
        "--ensure-schema",
        action="store_true",
        help="Legacy name: validate schema before scan; does not run DDL",
    )
    parser.add_argument(
        "--result-json",
        help="可选的不可变运行结果 sidecar 路径，供父编排器精确绑定本次 scan",
    )
    args = parser.parse_args()
    args.dataset_version = _normalize_dataset_version(args.dataset_version)

    from aats.data_platform.config import get_settings
    from aats.data_platform.db import get_session, validate_rdp_schema
    from aats.data_platform.operations.strategy_tuning_registry import (
        get_combo_tuning_overrides,
    )
    from aats.data_platform.replay.adapters.directional_adapter import DirectionalReplayAdapter
    from aats.data_platform.replay.adapters.independent_adapter import IndependentReplayAdapter
    from aats.data_platform.replay.reports.markdown_report_builder import build_scan_comparison_report
    from aats.data_platform.replay.scan.scan_runner import run_parameter_scan

    settings = get_settings()
    if args.ensure_schema:
        log.info("Validating schema contract (--ensure-schema legacy flag)...")
        validate_rdp_schema(settings)

    if args.family == "independent":
        adapter = IndependentReplayAdapter()
    else:
        adapter = DirectionalReplayAdapter()

    grid = json.loads(args.grid) if args.grid else None
    base_params = get_combo_tuning_overrides(
        pathlib.Path(__file__).resolve().parent.parent,
        args.family,
        args.timeframe,
    )
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
            base_params=base_params,
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

    failed_path = scan_dir / "failed_combos.json"
    failed_payload = json.loads(failed_path.read_text(encoding="utf-8"))
    if (
        not isinstance(failed_payload, dict)
        or failed_payload.get("scan_run_id") != str(scan_run_id)
        or type(failed_payload.get("total_combinations")) is not int
        or type(failed_payload.get("completed_count")) is not int
        or type(failed_payload.get("failed_count")) is not int
    ):
        raise RuntimeError("parameter_scan_result_contract_invalid")
    total = failed_payload["total_combinations"]
    completed = failed_payload["completed_count"]
    failed = failed_payload["failed_count"]
    if completed + failed != total or total <= 0:
        raise RuntimeError("parameter_scan_result_contract_invalid")
    if failed == 0:
        status = "succeeded"
        exit_code = 0
    elif completed == 0:
        status = "failed"
        exit_code = 3
    else:
        status = "partial_success"
        exit_code = 2

    comparison_path = scan_dir / "comparison_summary.json"
    comparison_sha256: str | None = None
    comparison_size_bytes: int | None = None
    comparison_path_value: str | None = None
    if comparison_path.is_file():
        comparison_bytes = comparison_path.read_bytes()
        comparison_sha256 = hashlib.sha256(comparison_bytes).hexdigest()
        comparison_size_bytes = len(comparison_bytes)
        comparison_path_value = str(comparison_path.resolve())
    if status in {"succeeded", "partial_success"} and comparison_sha256 is None:
        raise RuntimeError("parameter_scan_comparison_missing")

    canonical_grid = json.dumps(
        grid,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    result_payload = {
        "schema_version": "aats.parameter_scan_result.v1",
        "scan_run_id": str(scan_run_id),
        "scan_dir": str(scan_dir.resolve()),
        "comparison_path": comparison_path_value,
        "comparison_sha256": comparison_sha256,
        "comparison_size_bytes": comparison_size_bytes,
        "status": status,
        "family": args.family,
        "symbol": args.symbol,
        "timeframe": args.timeframe,
        "dataset_version": args.dataset_version,
        "window": {"start": args.start, "end": args.end},
        "grid_sha256": hashlib.sha256(canonical_grid).hexdigest(),
        "total_combinations": total,
        "completed_count": completed,
        "failed_count": failed,
    }
    if args.result_json:
        immutable_json_write(result_payload, pathlib.Path(args.result_json))
    print(
        _SCAN_RESULT_PREFIX
        + json.dumps(
            result_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
