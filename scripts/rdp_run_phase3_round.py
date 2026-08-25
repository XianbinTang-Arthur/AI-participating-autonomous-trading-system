#!/usr/bin/env python3
"""Phase 3 Round Runner — 批量 attribution.

对 4 个 family × timeframe 组合批量运行 live attribution，
生成统一汇总产物和结论文档。

固定范围：
  symbol     = BTC-USDT-SWAP
  families   = independent, directional
  timeframes = 15m, 1H

Usage:
    python scripts/rdp_run_phase3_round.py \
        --start 2026-03-31 --end 2026-04-02

    python scripts/rdp_run_phase3_round.py \
        --start 2026-03-31 --end 2026-04-02 \
        --live-db-url "postgresql+psycopg://localhost:5432/aats_derivatives"

    python scripts/rdp_run_phase3_round.py \
        --start 2026-03-31 --end 2026-04-02 \
        --replay-only

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
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from aats.data_platform.governance.snapshot_db import (
    ROUND_PHASE_PHASE3,
    save_research_round_snapshot,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_phase3_round")

_SYMBOL = "BTC-USDT-SWAP"

_COMBOS: list[dict[str, str]] = [
    {"key": "independent_15m", "family": "independent", "timeframe": "15m"},
    {"key": "independent_1h", "family": "independent", "timeframe": "1H"},
    {"key": "directional_15m", "family": "directional", "timeframe": "15m"},
    {"key": "directional_1h", "family": "directional", "timeframe": "1H"},
]

_DEFAULT_ARTIFACT_ROOT = pathlib.Path("artifacts/research/attribution_rounds")


# =========================================================================
# 子进程调用 one-shot attribution
# =========================================================================


def _list_subdirs(path: pathlib.Path) -> set[str]:
    if not path.exists():
        return set()
    return {d.name for d in path.iterdir() if d.is_dir()}


def _run_single_attribution(
    family: str,
    timeframe: str,
    *,
    symbol: str,
    start: str,
    end: str,
    artifact_root: pathlib.Path,
    live_db_url: str | None,
    replay_only: bool,
    ensure_schema: bool,
    dataset_version: str,
    params_json: str | None = None,
) -> dict[str, Any]:
    """通过子进程调用 rdp_run_live_attribution.py。"""
    existing = _list_subdirs(artifact_root)

    cmd = [
        sys.executable, "scripts/rdp_run_live_attribution.py",
        "--family", family,
        "--symbol", symbol,
        "--timeframe", timeframe,
        "--start", start,
        "--end", end,
        "--dataset-version", dataset_version,
        "--artifact-root", str(artifact_root),
    ]
    if live_db_url:
        cmd.extend(["--live-db-url", live_db_url])
    if replay_only:
        cmd.append("--replay-only")
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
            "attribution_summary": None,
            "top_failure_modes": None,
            "error": f"No artifact dir. exit={proc.returncode}. {stderr_tail}",
        }

    run_dir = artifact_root / sorted(new_dirs)[-1]

    # 读 attribution_summary.json
    summary = None
    summary_file = run_dir / "attribution_summary.json"
    if summary_file.exists():
        with summary_file.open(encoding="utf-8") as f:
            summary = json.load(f)

    # 读 top_failure_modes.json
    tfm = None
    tfm_file = run_dir / "top_failure_modes.json"
    if tfm_file.exists():
        with tfm_file.open(encoding="utf-8") as f:
            tfm = json.load(f)

    # 读 alignment CSV 统计
    alignment_stats = {"total": 0, "aligned": 0, "replay_only": 0, "live_only": 0}
    alignment_file = run_dir / "replay_live_alignment.csv"
    if alignment_file.exists():
        with alignment_file.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                alignment_stats["total"] += 1
                status = row.get("alignment_status", "")
                if status == "aligned":
                    alignment_stats["aligned"] += 1
                elif status == "replay_only":
                    alignment_stats["replay_only"] += 1
                elif status == "live_only":
                    alignment_stats["live_only"] += 1

    return {
        "family": family,
        "timeframe": timeframe,
        # P1b: 保留 partial_success 语义（exit=2 表示 replay 正常但 live 失败）
        "status": (
            "succeeded" if proc.returncode == 0
            else "partial_success" if proc.returncode == 2
            else "failed"
        ),
        "run_dir": str(run_dir),
        "attribution_summary": summary,
        "top_failure_modes": tfm,
        "alignment_stats": alignment_stats,
        "error": None,
    }


# =========================================================================
# 聚合
# =========================================================================


def _aggregate_summaries(
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """汇总所有 family/tf 的 attribution summary。"""
    all_rows: list[dict[str, Any]] = []
    for r in results:
        summary = r.get("attribution_summary")
        if not summary:
            continue
        if isinstance(summary, list):
            all_rows.extend(summary)
        elif isinstance(summary, dict) and "experiments" in summary:
            all_rows.extend(summary["experiments"])
    return all_rows


# =========================================================================
# 主流程
# =========================================================================


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 3 Round Runner: 批量 live attribution 归因",
    )
    parser.add_argument("--start", required=True, help="YYYY-MM-DD (UTC)")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD (UTC)")
    parser.add_argument("--dataset-version", default="v1.0")
    parser.add_argument(
        "--live-db-url", default=None,
        help="Live AATS database URL (default: env RDP_LIVE_DATABASE_URL)",
    )
    parser.add_argument("--replay-only", action="store_true")
    parser.add_argument(
        "--artifact-root", type=str, default=str(_DEFAULT_ARTIFACT_ROOT),
    )
    parser.add_argument(
        "--ensure-schema",
        action="store_true",
        help="Legacy name: validate schema before the first run; does not run DDL",
    )
    parser.add_argument("--no-print-summary", action="store_true")
    # P0: 参数闭环 — 支持从 Phase 2 parameter_candidates.json 注入参数
    parser.add_argument(
        "--params-json", default=None,
        help="Phase 2 parameter_candidates.json 路径，自动按 family_tf 分发参数",
    )
    args = parser.parse_args()

    live_db_url = args.live_db_url or os.environ.get("RDP_LIVE_DATABASE_URL")
    replay_only = args.replay_only or (not live_db_url)

    started_at = datetime.now(timezone.utc).isoformat()
    round_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
    artifact_root = pathlib.Path(args.artifact_root).resolve()
    round_dir = artifact_root / round_id
    per_combo_root = round_dir / "per_combo"

    log.info("=" * 60)
    log.info("Phase 3 Round Runner")
    log.info("  Round ID    : %s", round_id)
    log.info("  Symbol      : %s", _SYMBOL)
    log.info("  Window      : %s ~ %s", args.start, args.end)
    log.info("  Replay-only : %s", replay_only)
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

        result = _run_single_attribution(
            combo["family"], combo["timeframe"],
            symbol=_SYMBOL,
            start=args.start,
            end=args.end,
            artifact_root=per_combo_root,
            live_db_url=live_db_url,
            replay_only=replay_only,
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

    # 汇总 attribution summary
    all_summaries: dict[str, list[dict[str, Any]]] = {}
    all_failure_modes: dict[str, dict[str, Any]] = {}
    all_alignment_stats: dict[str, dict[str, int]] = {}

    for r in results:
        ft_key = r["key"]
        if r.get("attribution_summary"):
            all_summaries[ft_key] = r["attribution_summary"]
        if r.get("top_failure_modes"):
            all_failure_modes[ft_key] = r["top_failure_modes"]
        if r.get("alignment_stats"):
            all_alignment_stats[ft_key] = r["alignment_stats"]

    # 汇总 CSV
    all_summary_rows = _aggregate_summaries(results)
    if all_summary_rows:
        csv_path = round_dir / "family_timeframe_attribution_summary.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = list(all_summary_rows[0].keys())
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in all_summary_rows:
                writer.writerow(row)
        log.info("Wrote summary CSV -> %s", csv_path)

    # Layer analysis (读各 combo 的数据重新计算)
    all_layer_analyses: dict[str, dict[str, dict[str, int]]] = {}
    for r in results:
        ft_key = r["key"]
        run_dir_path = r.get("run_dir")
        if not run_dir_path:
            continue
        alignment_file = pathlib.Path(run_dir_path) / "replay_live_alignment.csv"
        if alignment_file.exists():
            classified_rows = []
            with alignment_file.open(encoding="utf-8") as f:
                reader = csv.DictReader(f)
                classified_rows = list(reader)
            if classified_rows:
                from aats.data_platform.attribution.aggregation import build_layer_analysis
                all_layer_analyses[ft_key] = build_layer_analysis(classified_rows)

    # ---- 结论文档 ----
    log.info("Building conclusion document...")
    from aats.data_platform.attribution.report_builder import build_phase3_conclusion

    conclusion_path = round_dir / "phase3_live_attribution_conclusion.md"
    build_phase3_conclusion(
        symbol=_SYMBOL,
        start=args.start,
        end=args.end,
        all_summaries=all_summaries,
        all_failure_modes=all_failure_modes,
        all_layer_analyses=all_layer_analyses,
        all_alignment_stats=all_alignment_stats,
        round_id=round_id,
        output_path=conclusion_path,
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
        "replay_only": replay_only,
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
            }
            for r in results
        ],
    }
    manifest_path = round_dir / "round_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False, default=str)
    log.info("Wrote manifest -> %s", manifest_path)
    combo_payload: dict[str, Any] = {}
    for r in results:
        combo_payload[r["key"]] = {
            "family": r["family"],
            "timeframe": r["timeframe"],
            "status": r["status"],
            "run_dir": r.get("run_dir"),
            "attribution_summary": r.get("attribution_summary"),
            "top_failure_modes": r.get("top_failure_modes"),
            "alignment_stats": r.get("alignment_stats"),
            "layer_analysis": all_layer_analyses.get(r["key"]),
        }
    if not save_research_round_snapshot(
        round_id=round_id,
        phase=ROUND_PHASE_PHASE3,
        status=manifest["overall_status"],
        round_path=str(round_dir),
        started_at=started_at,
        finished_at=finished_at,
        replay_only=replay_only,
        manifest_payload=manifest,
        summary_payload={
            "summary_rows": all_summary_rows,
            "all_summaries": all_summaries,
            "all_failure_modes": all_failure_modes,
            "all_alignment_stats": all_alignment_stats,
            "all_layer_analyses": all_layer_analyses,
            "combos": combo_payload,
        },
        conclusion_payload={
            "report_markdown_path": str(conclusion_path),
        },
        artifacts_payload={
            "round_dir": str(round_dir),
            "manifest_path": str(manifest_path),
            "conclusion_path": str(conclusion_path),
            "summary_csv_path": str(round_dir / "family_timeframe_attribution_summary.csv"),
        },
    ):
        log.warning("Phase3 round snapshot DB upsert failed; file artifacts remain authoritative fallback")

    # ---- 最终汇总 ----

    log.info("")
    log.info("=" * 60)
    log.info("Phase 3 round completed: %d succeeded, %d partial, %d failed",
             n_ok, n_partial, n_fail)
    log.info("Round dir: %s", round_dir)
    log.info("=" * 60)

    if not args.no_print_summary:
        print("")
        print(f"=== Phase 3 Attribution Round: {round_id} ===")
        print(f"Symbol: {_SYMBOL}")
        print(f"Window: {args.start} ~ {args.end}")
        print(f"Combos: {n_ok} succeeded, {n_partial} partial, {n_fail} failed")
        print("")

        for r in results:
            status_icon = {"succeeded": "OK", "partial_success": "PART", "failed": "FAIL"}.get(r["status"], "??")
            tfm = r.get("top_failure_modes", {})
            failures = tfm.get("total_failures", 0)
            success = tfm.get("total_success", 0)
            print(f"  [{status_icon}] {r['key']:<25s} "
                  f"failures={failures}, success={success}")

        print("")
        print(f"Conclusion: {round_dir / 'phase3_live_attribution_conclusion.md'}")
        print(f"Artifacts : {round_dir}")

    # 退出码: 0=全部成功/partial, 2=部分失败, 3=全部失败
    if n_fail > 0 and (n_ok + n_partial) == 0:
        sys.exit(3)
    elif n_fail > 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
