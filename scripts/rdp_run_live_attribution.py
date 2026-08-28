#!/usr/bin/env python3
"""One-shot Live Attribution Runner.

对给定 family / symbol / timeframe / time window 做一次 replay vs live 对照归因，
回答"最近为什么没下单"。

Usage:
    # 基础用法（默认参数）
    python scripts/rdp_run_live_attribution.py \
        --family independent \
        --symbol BTC-USDT-SWAP \
        --timeframe 15m \
        --start 2026-03-31 \
        --end 2026-04-02

    # 使用 Phase 2 推荐参数（parameter_candidates.json）
    python scripts/rdp_run_live_attribution.py \
        --family independent \
        --symbol BTC-USDT-SWAP \
        --timeframe 15m \
        --start 2026-03-31 \
        --end 2026-04-02 \
        --params-json artifacts/research/step2_rounds/<id>/parameter_candidates.json \
        --parameter-set independent_15m

    # 手动覆盖参数（可叠加在 --params-json 之上）
    python scripts/rdp_run_live_attribution.py \
        --family independent \
        --symbol BTC-USDT-SWAP \
        --timeframe 15m \
        --start 2026-03-31 \
        --end 2026-04-02 \
        --param min_confirm_ticks=3 \
        --param signal_edge_scale_bps=12

    # 指定 live DB 连接
    python scripts/rdp_run_live_attribution.py \
        --family independent \
        --symbol BTC-USDT-SWAP \
        --timeframe 15m \
        --start 2026-03-31 \
        --end 2026-04-02 \
        --live-db-url "postgresql+psycopg://localhost:5432/aats_derivatives"

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
    2 = live attribution 配置缺失（未显式选择 replay-only）
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import pathlib
import sys
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from aats.data_platform.governance._atomic_io import immutable_json_write
from aats.data_platform.governance.parameter_identity import (
    parameter_values_fingerprint,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_live_attribution")

_DEFAULT_ARTIFACT_ROOT = pathlib.Path("artifacts/research/attribution_rounds")
_RESULT_SCHEMA_VERSION = "aats.live_attribution_result.v1"
_RESULT_MARKER_PREFIX = "RDP_LIVE_ATTRIBUTION_RESULT_JSON="
_RESULT_OUTPUT_FILES = {
    "replay_live_alignment": "replay_live_alignment.csv",
    "attribution_summary": "attribution_summary.json",
    "top_failure_modes": "top_failure_modes.json",
    "replay_params_used": "replay_params_used.json",
    "live_attribution_report": "live_attribution_report.md",
}


# =========================================================================
# 参数加载（P0: Phase 2 -> Phase 3 参数闭环）
# =========================================================================


def _parse_params(param_strs: list[str]) -> dict[str, object]:
    """解析 --param key=value 参数（复用 rdp_run_replay.py 逻辑）。"""
    result: dict[str, object] = {}
    for s in param_strs:
        if "=" not in s:
            log.warning("Ignoring malformed param: %s", s)
            continue
        k, v = s.split("=", 1)
        try:
            result[k] = int(v)
        except ValueError:
            try:
                result[k] = float(v)
            except ValueError:
                result[k] = v
    return result


def _sanitize_loaded_params(
    raw_params: dict[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    if "_note" in raw_params:
        log.warning("  %s is a placeholder candidate and will be ignored", source)
        return {}

    sanitized: dict[str, Any] = {}
    for key, value in raw_params.items():
        if key.startswith("_"):
            continue
        if value is None:
            log.warning("  Ignore %s[%s]=None", source, key)
            continue
        sanitized[key] = value
    return sanitized


def _load_replay_params(
    params_json: str | None,
    parameter_set: str | None,
    param_overrides: list[str],
    *,
    family: str,
) -> Any:
    """加载 replay 参数。

    优先级（从低到高）：
      1. 默认值（ReplayParameterOverrides 默认构造）
      2. --params-json 文件（支持 parameter_candidates.json 格式或平坦 dict）
      3. --param key=value 手动覆盖

    Returns:
        ReplayParameterOverrides 实例。
    """
    from aats.data_platform.replay.core.replay_context import ReplayParameterOverrides

    param_dict: dict[str, Any] = {}

    # 1. 从文件加载
    if params_json:
        log.info("Loading params from: %s", params_json)
        with open(params_json, encoding="utf-8") as f:
            data = json.load(f)

        if "candidates" in data and parameter_set:
            # parameter_candidates.json 格式
            candidates = data["candidates"]
            if parameter_set in candidates:
                param_dict = _sanitize_loaded_params(
                    dict(candidates[parameter_set]),
                    source=f"candidate:{parameter_set}",
                )
                log.info("  Loaded parameter set '%s': %s", parameter_set, param_dict)
            else:
                available = list(candidates.keys())
                log.warning("  Parameter set '%s' not found. Available: %s",
                            parameter_set, available)
        elif "candidates" in data and not parameter_set:
            log.warning("  --params-json contains 'candidates' but no --parameter-set specified. "
                        "Using default params.")
        elif "recommendations" in data and parameter_set is None:
            # parameter_recommendations.json 格式（Step 1 输出）
            recs = data.get("recommendations", {})
            for k, v in recs.items():
                if isinstance(v, dict) and "value" in v and v["value"] is not None:
                    param_dict[k] = v["value"]
            log.info("  Loaded recommendations: %s", param_dict)
        else:
            # 平坦 params dict
            param_dict = _sanitize_loaded_params(
                {
                    k: v for k, v in data.items()
                    if not k.startswith("_") and k not in ("round_id", "scope", "pending_validation")
                },
                source="flat-params",
            )
            log.info("  Loaded flat params: %s", param_dict)

    # 2. 应用 CLI 覆盖（最高优先级）
    cli_overrides = _parse_params(param_overrides)
    if cli_overrides:
        param_dict.update(cli_overrides)
        log.info("  Applied CLI overrides: %s", cli_overrides)

    # 3. 构造 ReplayParameterOverrides
    baseline = ReplayParameterOverrides.for_family(family)
    if param_dict:
        params = ReplayParameterOverrides.from_dict(param_dict, base=baseline)
        log.info("  Final params: %s", params.to_dict())
        return params

    log.info("  Using default ReplayParameterOverrides")
    return baseline


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
    params: Any = None,
) -> list[dict[str, Any]]:
    """运行 replay 并返回 decisions 字典列表。

    Args:
        params: ReplayParameterOverrides 实例。None 则使用默认参数。
    """
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

    if params is None:
        params = ReplayParameterOverrides.for_family(family)

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

    # 转换为 dict 列表
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
    """创建强制只读的 live DB session。"""
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import Session

    from aats.storage.connection_budget import RDP_LIVE_QUERY_POOL

    parsed = make_url(live_db_url)
    engine_options: dict[str, Any] = {
        "echo": False,
        "pool_pre_ping": True,
    }
    if parsed.get_backend_name() == "postgresql":
        existing_options = str(parsed.query.get("options") or "").strip()
        readonly_option = "-c default_transaction_read_only=on"
        merged_options = (
            existing_options
            if "default_transaction_read_only" in existing_options
            else f"{existing_options} {readonly_option}".strip()
        )
        engine_options.update({
            "pool_size": RDP_LIVE_QUERY_POOL.pool_size,
            "max_overflow": RDP_LIVE_QUERY_POOL.max_overflow,
            "connect_args": {"options": merged_options},
        })
    engine = create_engine(live_db_url, **engine_options)
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
    """执行对齐 + 归因，返回 (alignment_rows, classified_rows)。"""
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
    from aats.data_platform.governance._time_util import parse_iso_datetime_utc

    if replay_only or not live_db_url:
        log.info("Replay-only mode: skipping live data queries")
        alignment_rows = align_replay_with_live(
            replay_decisions, [],
            timeframe=timeframe,
        )
        classified = classify_all(alignment_rows)
        return alignment_rows, classified

    log.info("Connecting to live DB...")
    live_session = _create_live_session(live_db_url)

    try:
        log.info("Querying live intents...")
        live_intents = query_live_intents(
            live_session,
            family=family, symbol=symbol,
            timeframe=timeframe,
            start_ts=start_ts, end_ts=end_ts,
        )
        log.info("Found %d live intents", len(live_intents))

        log.info("Aligning replay with live...")
        alignment_rows = align_replay_with_live(
            replay_decisions, live_intents,
            timeframe=timeframe,
        )

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

        all_order_ids: list[str] = []
        for ords in orders.values():
            for o in ords:
                all_order_ids.append(str(o["order_id"]))

        log.info("Querying fills (%d orders)...", len(all_order_ids))
        fills = query_live_fills(live_session, order_ids=all_order_ids)

        log.info("Querying reconciliation snapshots...")
        attribution_event_times = sorted({
            event_ts
            for row in alignment_rows
            if row.get("alignment_status") == "aligned" and row.get("live_ts")
            if (
                event_ts := parse_iso_datetime_utc(
                    row["live_ts"],
                    context="rdp_run_live_attribution.live_ts",
                )
            ) is not None
        })
        recon = query_reconciliation_snapshots(
            live_session, symbol=symbol,
            event_times=attribution_event_times,
        )

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
        bind = live_session.get_bind()
        live_session.close()
        bind.dispose()


# =========================================================================
# 产物写入
# =========================================================================


def _write_alignment_csv(
    rows: list[dict[str, Any]],
    output_path: pathlib.Path,
) -> pathlib.Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "family", "symbol", "timeframe",
        "replay_ts", "live_ts", "alignment_status",
        "lineage_error",
        "replay_opening", "live_opening",
        "final_attribution_category", "final_attribution_reason",
        "strategy_reason", "permission_reason",
        "allocator_reason", "budget_reason",
        "risk_reason", "execution_reason",
        "order_status", "fill_status",
        "replay_action", "replay_selectable", "replay_execution_compatible",
        "replay_blocking_reasons", "replay_expected_net_edge_bps",
        "live_state", "live_route_action", "live_automatic_enabled",
        "live_intent_id", "live_decision_id", "live_allocation_id",
        "live_parameter_set_id", "live_runtime_generation", "live_code_version",
        "live_market_snapshot_ref", "live_feature_snapshot_ref",
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


def _result_output_evidence(run_dir: pathlib.Path) -> dict[str, dict[str, Any]]:
    """Bind every output consumed by the round runner to exact immutable metadata."""

    evidence: dict[str, dict[str, Any]] = {}
    for key, filename in _RESULT_OUTPUT_FILES.items():
        path = run_dir / filename
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"live_attribution_result_output_invalid:{key}")
        payload = path.read_bytes()
        evidence[key] = {
            "path": str(path.resolve(strict=True)),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }
    return evidence


def _publish_result_sidecar(
    *,
    result_json: str | None,
    run_id: str,
    run_dir: pathlib.Path,
    family: str,
    symbol: str,
    timeframe: str,
    dataset_version: str,
    start: str,
    end: str,
    replay_only: bool,
    replay_params: dict[str, Any],
    status: str,
    exit_code: int,
) -> dict[str, Any]:
    result_payload = {
        "schema_version": _RESULT_SCHEMA_VERSION,
        "status": status,
        "exit_code": exit_code,
        "run_id": run_id,
        "run_dir": str(run_dir.resolve(strict=True)),
        "family": family,
        "symbol": symbol,
        "timeframe": timeframe,
        "dataset_version": dataset_version,
        "window": {"start": start, "end": end},
        "replay_only": replay_only,
        "resolved_parameter_values_fingerprint": parameter_values_fingerprint(
            replay_params
        ),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "outputs": _result_output_evidence(run_dir),
    }
    if result_json:
        immutable_json_write(result_payload, pathlib.Path(result_json))
    print(
        _RESULT_MARKER_PREFIX
        + json.dumps(
            result_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
    )
    return result_payload


# =========================================================================
# 主流程
# =========================================================================


def main() -> int:
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
    parser.add_argument(
        "--result-json",
        help="可选的不可变运行结果 sidecar 路径，供父编排器精确绑定本次运行",
    )
    parser.add_argument(
        "--ensure-schema",
        action="store_true",
        help="Legacy name: validate schema before attribution; does not run DDL",
    )
    # ---- P0: 参数注入 ----
    parser.add_argument(
        "--params-json", default=None,
        help="JSON 文件路径，支持 parameter_candidates.json 或平坦 dict 格式",
    )
    parser.add_argument(
        "--parameter-set", default=None,
        help="parameter_candidates.json 中的 key (e.g. independent_15m)",
    )
    parser.add_argument(
        "--param", action="append", default=[],
        help="手动覆盖参数 key=value (可多次指定，优先级最高)",
    )
    args = parser.parse_args()

    # Live DB URL: CLI > env > None
    live_db_url = args.live_db_url or os.environ.get("RDP_LIVE_DATABASE_URL")
    if not live_db_url and not args.replay_only:
        log.error(
            "Phase 3 live attribution requires --live-db-url or "
            "RDP_LIVE_DATABASE_URL. Use --replay-only explicitly for replay-only analysis."
        )
        return 2

    start_ts = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_ts = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    # 加载 replay 参数 (P0: Phase 2 -> Phase 3 闭环)
    replay_params = _load_replay_params(
        args.params_json, args.parameter_set, args.param,
        family=args.family,
    )

    # 产物目录
    artifact_root = pathlib.Path(args.artifact_root).resolve()
    run_id = (
        f"{args.family}_{args.timeframe}_"
        f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex}"
    )
    run_dir = artifact_root / run_id

    log.info("=" * 60)
    log.info("Live Attribution Runner")
    log.info("  Family    : %s", args.family)
    log.info("  Symbol    : %s", args.symbol)
    log.info("  Timeframe : %s", args.timeframe)
    log.info("  Window    : %s ~ %s", args.start, args.end)
    log.info("  Replay-only: %s", args.replay_only)
    log.info("  Params    : %s", replay_params.to_dict())
    log.info("  Output    : %s", run_dir)
    log.info("=" * 60)

    # ---- Step 1: Run replay ----
    from aats.data_platform.config import get_settings
    from aats.data_platform.db import get_session, validate_rdp_schema

    settings = get_settings()
    if args.ensure_schema:
        log.info("Validating schema contract (--ensure-schema legacy flag)...")
        validate_rdp_schema(settings)

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
            params=replay_params,
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
    # 保存实际使用的参数，确保可追溯
    _write_json(replay_params.to_dict(), run_dir / "replay_params_used.json")

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

    exit_code = 0
    _publish_result_sidecar(
        result_json=args.result_json,
        run_id=run_id,
        run_dir=run_dir,
        family=args.family,
        symbol=args.symbol,
        timeframe=args.timeframe,
        dataset_version=args.dataset_version,
        start=args.start,
        end=args.end,
        replay_only=args.replay_only,
        replay_params=replay_params.to_dict(),
        status="succeeded",
        exit_code=exit_code,
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
