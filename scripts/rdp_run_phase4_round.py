#!/usr/bin/env python3
"""Phase 4 Round Runner — 批量 execution realism.

对 4 个 family × timeframe 组合批量运行 execution realism 分析，
生成统一汇总产物和结论文档。

固定范围：
  symbol     = BTC-USDT-SWAP
  families   = independent, directional
  timeframes = 15m, 1H

Usage:
    python scripts/rdp_run_phase4_round.py \
        --start 2026-03-31 --end 2026-04-02

    python scripts/rdp_run_phase4_round.py \
        --start 2026-03-31 --end 2026-04-02 \
        --taker-fee-bps 3.0

    python scripts/rdp_run_phase4_round.py \
        --start 2026-03-31 --end 2026-04-02 \
        --ensure-schema

Exit codes:
    0 = 全部成功
    2 = 部分成功
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_phase4_round")

_SYMBOL = "BTC-USDT-SWAP"

_COMBOS: list[dict[str, str]] = [
    {"key": "independent_15m", "family": "independent", "timeframe": "15m"},
    {"key": "independent_1h", "family": "independent", "timeframe": "1H"},
    {"key": "directional_15m", "family": "directional", "timeframe": "15m"},
    {"key": "directional_1h", "family": "directional", "timeframe": "1H"},
]

_DEFAULT_ARTIFACT_ROOT = pathlib.Path("artifacts/research/execution_rounds")


# =========================================================================
# 子进程调用 one-shot execution realism
# =========================================================================


def _list_subdirs(path: pathlib.Path) -> set[str]:
    if not path.exists():
        return set()
    return {d.name for d in path.iterdir() if d.is_dir()}


def _run_single_execution_realism(
    family: str,
    timeframe: str,
    *,
    symbol: str,
    start: str,
    end: str,
    artifact_root: pathlib.Path,
    taker_fee_bps: float,
    ensure_schema: bool,
    dataset_version: str,
    params_json: str | None = None,
) -> dict[str, Any]:
    """通过子进程调用 rdp_run_execution_realism.py。"""
    existing = _list_subdirs(artifact_root)

    cmd = [
        sys.executable, "scripts/rdp_run_execution_realism.py",
        "--family", family,
        "--symbol", symbol,
        "--timeframe", timeframe,
        "--start", start,
        "--end", end,
        "--dataset-version", dataset_version,
        "--taker-fee-bps", str(taker_fee_bps),
        "--artifact-root", str(artifact_root),
    ]
    if ensure_schema:
        cmd.append("--ensure-schema")
    # P0: 参数闭环 — 传递 Phase 2 推荐参数
    if params_json:
        ft_key = f"{family}_{timeframe.lower()}"
        cmd.extend(["--params-json", params_json, "--parameter-set", ft_key])

    log.info("  CMD: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True)

    # 始终记录 stderr 以便调试
    if proc.stderr:
        stderr_text = proc.stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 and stderr_text:
            log.error("  subprocess stderr (last 1000 chars):\n%s", stderr_text[-1000:])
        elif stderr_text:
            log.debug("  subprocess stderr (last 500 chars):\n%s", stderr_text[-500:])

    # 发现新目录
    new_dirs = _list_subdirs(artifact_root) - existing
    if not new_dirs:
        stderr_raw = proc.stderr or b""
        stderr_tail = stderr_raw[-500:].decode("utf-8", errors="replace")
        return {
            "family": family,
            "timeframe": timeframe,
            "status": "failed",
            "run_dir": None,
            "cost_summary": None,
            "slippage_rows": None,
            "error": f"No artifact dir. exit={proc.returncode}. {stderr_tail}",
        }

    run_dir = artifact_root / sorted(new_dirs)[-1]

    # 读 execution_cost_summary.json
    cost_summary = None
    cost_file = run_dir / "execution_cost_summary.json"
    if cost_file.exists():
        with cost_file.open(encoding="utf-8") as f:
            cost_summary = json.load(f)

    # 读 slippage_summary.csv
    slippage_rows: list[dict[str, Any]] = []
    slip_file = run_dir / "slippage_summary.csv"
    if slip_file.exists():
        with slip_file.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            slippage_rows = list(reader)
            # 数值字段转型
            for row in slippage_rows:
                for field in ("estimated_slippage_bps", "estimated_total_execution_cost_bps",
                              "cost_adjusted_edge_bps", "bar_range_bps", "bar_volume",
                              "half_spread_bps", "volume_impact_bps"):
                    if field in row and row[field]:
                        try:
                            row[field] = float(row[field])
                        except (ValueError, TypeError):
                            pass

    # 读 execution_alignment.csv 统计
    alignment_stats = {"total": 0, "matched": 0, "no_bar_data": 0}
    alignment_file = run_dir / "execution_alignment.csv"
    if alignment_file.exists():
        with alignment_file.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                alignment_stats["total"] += 1
                status = row.get("alignment_status", "")
                if status == "matched":
                    alignment_stats["matched"] += 1
                elif status == "no_bar_data":
                    alignment_stats["no_bar_data"] += 1

    return {
        "family": family,
        "timeframe": timeframe,
        # P1b: 保留 partial_success 语义（exit=2 表示 replay 正常但无 bar 匹配）
        "status": (
            "succeeded" if proc.returncode == 0
            else "partial_success" if proc.returncode == 2
            else "failed"
        ),
        "run_dir": str(run_dir),
        "cost_summary": cost_summary,
        "slippage_rows": slippage_rows,
        "alignment_stats": alignment_stats,
        "error": None,
    }


# =========================================================================
# 主流程
# =========================================================================


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 4 Round Runner: 批量 execution realism 分析",
    )
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
    parser.add_argument("--no-print-summary", action="store_true")
    # P0: 参数闭环 — 支持从 Phase 2 parameter_candidates.json 注入参数
    parser.add_argument(
        "--params-json", default=None,
        help="Phase 2 parameter_candidates.json 路径，自动按 family_tf 分发参数",
    )
    args = parser.parse_args()

    started_at = datetime.now(timezone.utc).isoformat()
    round_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
    artifact_root = pathlib.Path(args.artifact_root).resolve()
    round_dir = artifact_root / round_id
    per_combo_root = round_dir / "per_combo"

    log.info("=" * 60)
    log.info("Phase 4 Round Runner (Execution Realism)")
    log.info("  Round ID    : %s", round_id)
    log.info("  Symbol      : %s", _SYMBOL)
    log.info("  Window      : %s ~ %s", args.start, args.end)
    log.info("  Taker fee   : %.1f bps", args.taker_fee_bps)
    log.info("  Params JSON : %s", args.params_json or "(default)")
    log.info("  Combos      : %d", len(_COMBOS))
    log.info("  Output      : %s", round_dir)
    log.info("=" * 60)

    # ---- 运行 4 个 family/tf 组合 ----
    results: list[dict[str, Any]] = []

    for i, combo in enumerate(_COMBOS):
        log.info("")
        log.info("[%d/%d] %s / %s",
                 i + 1, len(_COMBOS), combo["family"], combo["timeframe"])

        result = _run_single_execution_realism(
            combo["family"], combo["timeframe"],
            symbol=_SYMBOL,
            start=args.start,
            end=args.end,
            artifact_root=per_combo_root,
            taker_fee_bps=args.taker_fee_bps,
            ensure_schema=args.ensure_schema and (i == 0),
            dataset_version=args.dataset_version,
            params_json=args.params_json,
        )
        result["key"] = combo["key"]
        results.append(result)

        if result["status"] == "succeeded":
            log.info("  -> SUCCEEDED: %s", result.get("run_dir"))
        elif result["status"] == "partial_success":
            log.warning("  -> PARTIAL: %s", result.get("run_dir"))
        else:
            log.error("  -> FAILED: %s", (result.get("error") or "")[:200])

    # ---- 聚合 ----
    log.info("")
    log.info("Aggregating results...")

    all_cost_summaries: dict[str, dict[str, Any]] = {}
    all_slippage_rows: dict[str, list[dict[str, Any]]] = {}

    for r in results:
        ft_key = r["key"]
        if r.get("cost_summary"):
            all_cost_summaries[ft_key] = r["cost_summary"]
        if r.get("slippage_rows"):
            all_slippage_rows[ft_key] = r["slippage_rows"]

    # 比较表
    from aats.data_platform.execution_realism.aggregation import (
        build_execution_realism_comparison,
        generate_cross_comparison_findings,
    )

    comparison_input = {
        ft_key: {
            "cost_summary": all_cost_summaries.get(ft_key, {}),
            "slippage_rows": all_slippage_rows.get(ft_key, []),
        }
        for ft_key in [c["key"] for c in _COMBOS]
        if ft_key in all_cost_summaries
    }

    comparison_rows = build_execution_realism_comparison(comparison_input)
    cross_findings = generate_cross_comparison_findings(comparison_rows)

    # 写入比较 CSV
    if comparison_rows:
        comp_csv_path = round_dir / "execution_realism_comparison.csv"
        comp_csv_path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = list(comparison_rows[0].keys())
        with comp_csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in comparison_rows:
                writer.writerow(row)
        log.info("Wrote comparison CSV -> %s", comp_csv_path)

    # ---- 结论文档 ----
    log.info("Building conclusion document...")
    from aats.data_platform.execution_realism.report_builder import build_phase4_conclusion

    build_phase4_conclusion(
        symbol=_SYMBOL,
        start=args.start,
        end=args.end,
        all_cost_summaries=all_cost_summaries,
        comparison_rows=comparison_rows,
        cross_findings=cross_findings,
        round_id=round_id,
        output_path=round_dir / "phase4_execution_realism_conclusion.md",
    )

    # ---- 统计 ----
    n_ok = sum(1 for r in results if r["status"] == "succeeded")
    n_partial = sum(1 for r in results if r["status"] == "partial_success")
    n_fail = sum(1 for r in results if r["status"] == "failed")

    # ---- Manifest ----
    finished_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        "round_id": round_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "symbol": _SYMBOL,
        "window": {"start": args.start, "end": args.end},
        "taker_fee_bps": args.taker_fee_bps,
        "model_version": "v1_bar_proxy",
        "overall_status": (
            "succeeded" if n_fail == 0
            else "failed" if (n_ok + n_partial) == 0
            else "partial_success"
        ),
        "combos": [
            {
                "key": r["key"],
                "family": r["family"],
                "timeframe": r["timeframe"],
                "status": r["status"],
                "run_dir": r.get("run_dir"),
                "candidates": (r.get("cost_summary") or {}).get("total_candidates", 0),
            }
            for r in results
        ],
    }
    manifest_path = round_dir / "round_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False, default=str)
    log.info("Wrote manifest -> %s", manifest_path)

    # ---- 最终汇总 ----

    log.info("")
    log.info("=" * 60)
    log.info("Phase 4 round completed: %d succeeded, %d partial, %d failed",
             n_ok, n_partial, n_fail)
    log.info("Round dir: %s", round_dir)
    log.info("=" * 60)

    if not args.no_print_summary:
        print("")
        print(f"=== Phase 4 Execution Realism Round: {round_id} ===")
        print(f"Symbol: {_SYMBOL}")
        print(f"Window: {args.start} ~ {args.end}")
        print(f"Combos: {n_ok} succeeded, {n_partial} partial, {n_fail} failed")
        print("")

        for r in results:
            status_icon = {"succeeded": "OK", "partial_success": "PART", "failed": "FAIL"}.get(r["status"], "??")
            cs = r.get("cost_summary", {})
            candidates = cs.get("total_candidates", 0)
            full_fill = cs.get("full_fill_ratio", 0)
            mean_slip = cs.get("slippage", {}).get("mean", 0)
            print(f"  [{status_icon}] {r['key']:<25s} "
                  f"candidates={candidates}, full_fill={full_fill:.1%}, "
                  f"mean_slip={mean_slip:.2f}bps")

        print("")
        print(f"Comparison: {round_dir / 'execution_realism_comparison.csv'}")
        print(f"Conclusion: {round_dir / 'phase4_execution_realism_conclusion.md'}")
        print(f"Artifacts : {round_dir}")

    # 退出码: 0=全部成功/partial, 2=部分失败, 3=全部失败
    if n_fail > 0 and (n_ok + n_partial) == 0:
        sys.exit(3)
    elif n_fail > 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
