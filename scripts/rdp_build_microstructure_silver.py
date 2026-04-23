#!/usr/bin/env python3
"""P1-D Phase 1A — Microstructure Silver 15m ETL 手动触发入口.

参考: docs/design/p1d_phase1a_implementation_design_2026_04_20.md §7.3 / §9 W2 Day 1-3

用法:
    # 默认 dry-run (不 commit, 只看本 bar 数据规模)
    python scripts/rdp_build_microstructure_silver.py --symbol BTC-USDT-SWAP

    # 明确指定 bar (UTC ISO)
    python scripts/rdp_build_microstructure_silver.py --symbol BTC-USDT-SWAP \
        --bar-start 2026-04-20T00:00:00+00:00

    # 正式执行 (双层保护: 必须 --apply 和 --confirm 同时指定)
    python scripts/rdp_build_microstructure_silver.py --symbol BTC-USDT-SWAP \
        --apply --confirm

    # 批量补跑 N 个最近 bar (运维场景)
    python scripts/rdp_build_microstructure_silver.py --symbol BTC-USDT-SWAP \
        --backfill-bars 4 --apply --confirm

触发语义:
    - scheduler 走 rdp_task_daemon → workflow `microstructure_silver_15m` →
      这个脚本以 `--apply --confirm` 模式跑 (configs/rdp_workflows/
      microstructure_silver_15m.json)
    - 运维手动触发同样用这个 CLI

双层保护:
    - 默认 dry-run: 只打日志不 commit, 安全看数据
    - --apply 切开 dry-run, 但仍需 --confirm 才真正 commit
    - 走 workflow 时 configs 里两个都带, 守护进程直接跑

Exit codes (P0-a 后):
    0 = 所有 bar 全部表写入成功
    1 = 主 try/except 抛 uncaught exception (建 ingest_run / merger 入口失败)
    2 = partial fail (至少一 bar 有表 written=0 但至少一张表成功, 场景:
        Bug 1 NumericValueOutOfRange 只影响 volume_profile, 其他 4 张表
        仍然写入)
    3 = full fail (某 bar 所有表 written=0 + error 非空)
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_microstructure_silver")


def _parse_iso_utc(value: str) -> datetime:
    """Parse ISO 8601 to UTC-aware datetime.

    接受 "2026-04-20T00:00:00+00:00" / "2026-04-20T00:00:00Z" /
    "2026-04-20 00:00:00+00:00"。
    """
    # Python 3.11+ fromisoformat 支持 Z 后缀; 3.10- 需要手动替换
    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        raise argparse.ArgumentTypeError(
            f"Timestamp must be UTC-aware (e.g. '2026-04-20T00:00:00+00:00'), "
            f"got naive: {value!r}"
        )
    return dt.astimezone(timezone.utc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="P1-D Phase 1A microstructure Silver 15m ETL builder.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--symbol",
        type=str,
        required=True,
        help="Instrument id, e.g. BTC-USDT-SWAP",
    )
    parser.add_argument(
        "--bar-start",
        type=_parse_iso_utc,
        default=None,
        help=(
            "Bar 起点 (UTC ISO 8601, 必须对齐 15m). 默认取最近一个已完成的 bar."
        ),
    )
    parser.add_argument(
        "--bar-end",
        type=_parse_iso_utc,
        default=None,
        help="Bar 终点,默认 = bar_start + 15min;通常无需手动指定。",
    )
    parser.add_argument(
        "--dataset-version",
        type=str,
        default=None,
        help="Silver 写入的 dataset_version,默认 p1d_microstructure_v1.0",
    )
    parser.add_argument(
        "--backfill-bars",
        type=int,
        default=1,
        help=(
            "补跑 N 个最近 bar (>=1), 仅当未指定 --bar-start 时生效。"
            "默认 1 = 只跑最近一个已关的 bar。"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="切开 dry-run, 真正执行 UPSERT。仍需 --confirm 才 commit。",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="配合 --apply 使用, 确认 commit。",
    )
    return parser.parse_args()


# Default cap: 单次 watermark-backfill 最多补 64 根 bar (= 16h)。
# 超过这个缺口的 gap 需要运维手动跑 scripts/maintenance/microstructure_silver_catchup_*.py。
_WATERMARK_BACKFILL_CAP = 64

# Sentinel — 用于 _resolve_bars_from_watermark 的可选 watermark 参数区分
# "未传" vs "显式传 None" (后者表示调用方已确认无 watermark, 直接走 cold-start fallback)。
_WATERMARK_UNSET: Any = object()


def _detect_trade_flow_watermark(symbol: str) -> datetime | None:
    """查询 silver.market_trade_flow_15m 的 MAX(ts) 作为 watermark。

    返回 UTC-aware datetime; 若无 row / DB 不可达 / 查询失败, 返回 None
    (视作冷启动, 由 caller 走 --backfill-bars fallback)。
    """
    try:
        from sqlalchemy import text
        from aats.data_platform.db import get_session
    except Exception as exc:  # pragma: no cover - import time failure
        log.warning(
            "failed to import db layer for watermark detection: %r; "
            "falling back to --backfill-bars cold-start path", exc,
        )
        return None

    try:
        with get_session() as session:
            row = session.execute(
                text(
                    "SELECT MAX(ts) AS max_ts "
                    "FROM silver.market_trade_flow_15m "
                    "WHERE symbol = :sym"
                ),
                {"sym": symbol},
            ).fetchone()
    except Exception as exc:
        log.warning(
            "watermark probe on silver.market_trade_flow_15m failed for "
            "symbol=%s: %r; falling back to --backfill-bars cold-start path",
            symbol, exc,
        )
        return None

    if row is None or row.max_ts is None:
        return None
    max_ts = row.max_ts
    if max_ts.tzinfo is None:
        max_ts = max_ts.replace(tzinfo=timezone.utc)
    return max_ts


def _resolve_bars_from_watermark(
    *,
    symbol: str,
    backfill_bars: int,
    watermark_cap: int = _WATERMARK_BACKFILL_CAP,
    watermark: Any = _WATERMARK_UNSET,
    now: datetime | None = None,
) -> list[tuple[datetime, datetime]]:
    """在未显式指定 --bar-start 时, 解析本次 runner 要处理的 bar 列表。

    Policy:
      1. 上界 = latest_complete_bar(lookback_bars=1) 的 bar_start (最近已关 bar)。
      2. watermark = silver.market_trade_flow_15m 的 max(ts), 由
         `_detect_trade_flow_watermark` 自动探测; 测试可显式注入。
      3. watermark 存在且落后 → 枚举 [watermark + 15m, upper_bar_start] 所有 15m
         bar, 单次 cap 到最近 `watermark_cap` 根 (超出的老缺口交给 catchup 脚本)。
      4. watermark 不存在 → 冷启动 fallback: for i in 1..backfill_bars:
         latest_complete_bar(lookback_bars=i) (保留旧语义)。
    """
    from aats.data_platform.merge.microstructure_silver_merger import (
        BAR_SECONDS,
        latest_complete_bar,
    )

    if watermark is _WATERMARK_UNSET:
        watermark = _detect_trade_flow_watermark(symbol)

    # 上界: 最近一个已关闭 bar 的 bar_start
    upper_bar_start, _upper_bar_end = latest_complete_bar(
        now=now, lookback_bars=1,
    )

    if watermark is None:
        # 冷启动 fallback — 保留旧 --backfill-bars 语义
        bars: list[tuple[datetime, datetime]] = []
        for i in range(1, backfill_bars + 1):
            bs, be = latest_complete_bar(now=now, lookback_bars=i)
            bars.append((bs, be))
        return bars

    # watermark = 已提交 bar 的最大 ts; 下一根要补的 bar_start 就是 watermark + 15m
    next_start = watermark + timedelta(seconds=BAR_SECONDS)
    if next_start > upper_bar_start:
        log.info(
            "silver watermark %s already at or past latest closed bar %s; "
            "nothing to backfill",
            watermark.isoformat(), upper_bar_start.isoformat(),
        )
        return []

    bars = []
    cursor = next_start
    while cursor <= upper_bar_start:
        bars.append((cursor, cursor + timedelta(seconds=BAR_SECONDS)))
        cursor = cursor + timedelta(seconds=BAR_SECONDS)

    if len(bars) > watermark_cap:
        log.warning(
            "silver watermark gap = %d bars exceeds cap = %d; will backfill "
            "the most recent %d bars this run, older gap bars left for the "
            "next tick / manual catchup script",
            len(bars), watermark_cap, watermark_cap,
        )
        bars = bars[-watermark_cap:]

    return bars


def _run_one_bar(
    *, symbol: str, bar_start: datetime, bar_end: datetime,
    dataset_version: str | None, apply: bool, confirm: bool,
) -> dict[str, Any]:
    """Run ETL for a single bar, return summary dict."""
    from aats.data_platform.db import get_session
    from aats.data_platform.jobs.run_registry import (
        create_ingest_run,
        finish_ingest_run,
    )
    from aats.data_platform.merge.microstructure_silver_merger import (
        DEFAULT_DATASET_VERSION,
        build_silver_microstructure_15m,
    )

    version = dataset_version or DEFAULT_DATASET_VERSION
    mode = "apply" if (apply and confirm) else "dry-run"
    log.info(
        "start symbol=%s bar=[%s, %s) version=%s mode=%s",
        symbol, bar_start.isoformat(), bar_end.isoformat(), version, mode,
    )

    with get_session() as session:
        run_id = create_ingest_run(
            session,
            run_type="rolling",
            dataset_domain="microstructure",
            instrument_type="swap",
            symbol=symbol,
            timeframe="15m",
            trigger_mode="scheduler" if (apply and confirm) else "manual",
        )
        # Stage 3: ingest_run 的 create 先 commit 出去让 meta.ingest_runs
        # 能立刻被 observability 看到 (即便 ETL 本身 dry-run 不 commit)
        session.commit()

    # 切新 session 跑 ETL 本体
    with get_session() as session:
        try:
            result = build_silver_microstructure_15m(
                session=session,
                symbol=symbol,
                bar_start_ts=bar_start,
                bar_end_ts=bar_end,
                ingest_run_id=run_id,
                dataset_version=version,
            )
            # 分级记日志: build_silver_microstructure_15m 自己也会打
            # COMMITTED/PARTIAL/FAILED, runner 再打一层带 mode 的辅助
            # (给运维 grep 用)
            if apply and confirm:
                if result.tables_failed:
                    # partial / full fail 场景: 即便 apply+confirm, 也
                    # 把成功的 step 的写入 commit 掉 (SAVEPOINT 已隔离失败)
                    session.commit()
                    if result.error is not None and all(
                        rc == 0 for rc in result.tables_written.values()
                    ):
                        log.error(
                            "FAILED symbol=%s bar_start=%s tables_written=%s "
                            "tables_failed=%s flags=%s duration=%.3fs error=%s",
                            symbol, bar_start.isoformat(), result.tables_written,
                            result.tables_failed, result.quality_flags,
                            result.duration_seconds, result.error,
                        )
                    else:
                        log.warning(
                            "PARTIAL symbol=%s bar_start=%s tables_written=%s "
                            "tables_failed=%s flags=%s duration=%.3fs error=%s",
                            symbol, bar_start.isoformat(), result.tables_written,
                            result.tables_failed, result.quality_flags,
                            result.duration_seconds, result.error,
                        )
                else:
                    session.commit()
                    # 区分 "ETL 成功 + 有数据" vs "ETL 成功 + 输入源空":
                    # 后者保持 INFO + success 语义, 只把日志标签改为
                    # COMMITTED_BUT_EMPTY, 让 operator / Loki 能 grep 出来。
                    has_no_data_flag = any(
                        f.endswith("_no_data") for f in result.quality_flags
                    )
                    status_tag = (
                        "COMMITTED_BUT_EMPTY" if has_no_data_flag else "COMMITTED"
                    )
                    log.info(
                        "%s symbol=%s bar_start=%s tables_written=%s flags=%s "
                        "duration=%.3fs",
                        status_tag, symbol, bar_start.isoformat(),
                        result.tables_written, result.quality_flags,
                        result.duration_seconds,
                    )
            else:
                session.rollback()
                log.info(
                    "DRY-RUN (no commit) symbol=%s bar_start=%s tables_written=%s "
                    "tables_failed=%s flags=%s duration=%.3fs",
                    symbol, bar_start.isoformat(), result.tables_written,
                    result.tables_failed, result.quality_flags,
                    result.duration_seconds,
                )
        except Exception as exc:
            session.rollback()
            log.exception("build_silver_microstructure_15m failed")
            # 写 meta.ingest_runs 的 failed 状态
            with get_session() as run_session:
                finish_ingest_run(
                    run_session, run_id, status="failed",
                    error_message=repr(exc)[:500],
                )
                run_session.commit()
            raise

    # 写 meta.ingest_runs 状态 (独立 session): partial/full fail 也走
    # failed 让 observability 能区分 success vs degraded
    if apply and confirm:
        with get_session() as run_session:
            if result.tables_failed:
                finish_ingest_run(
                    run_session, run_id, status="failed",
                    error_message=(
                        f"tables_failed={result.tables_failed!r} "
                        f"error={result.error!r}"
                    )[:500],
                )
            else:
                finish_ingest_run(run_session, run_id, status="succeeded")
            run_session.commit()

    return {
        "symbol": symbol,
        "bar_start": bar_start.isoformat(),
        "bar_end": bar_end.isoformat(),
        "tables_written": dict(result.tables_written),
        "tables_failed": list(result.tables_failed),
        "quality_flags": list(result.quality_flags),
        "duration_seconds": result.duration_seconds,
        "ingest_run_id": run_id,
        "mode": mode,
        "error": result.error,
    }


def main() -> int:
    args = parse_args()

    if args.backfill_bars < 1:
        log.error("--backfill-bars must be >= 1, got %d", args.backfill_bars)
        return 2

    if args.apply and not args.confirm:
        log.error(
            "--apply requires --confirm for safety; rerun with both or drop --apply"
        )
        return 2
    if args.confirm and not args.apply:
        log.error("--confirm has no effect without --apply")
        return 2

    from aats.data_platform.merge.microstructure_silver_merger import (
        BAR_SECONDS,
    )

    # 决定 bar 窗口
    bars: list[tuple[datetime, datetime]] = []
    if args.bar_start is not None:
        bar_start = args.bar_start
        bar_end = args.bar_end or (bar_start + timedelta(seconds=BAR_SECONDS))
        bars.append((bar_start, bar_end))
    else:
        # 默认: 先看 silver.market_trade_flow_15m 水位线, 有 → 枚举 gap bars
        # (cap 64); 无 watermark → 冷启动 fallback 到 --backfill-bars。
        bars = _resolve_bars_from_watermark(
            symbol=args.symbol,
            backfill_bars=args.backfill_bars,
        )
        if not bars:
            log.info(
                "no bars to process (silver up-to-date or empty window); exit 0"
            )
            return 0

    # 从最早 bar 开始跑 (让 EMA 递归 / baseline 按时间顺序建立)
    bars.sort(key=lambda pair: pair[0])

    summaries: list[dict[str, Any]] = []
    had_uncaught_exception = False
    any_partial_fail = False
    any_full_fail = False
    for bar_start, bar_end in bars:
        try:
            summary = _run_one_bar(
                symbol=args.symbol,
                bar_start=bar_start,
                bar_end=bar_end,
                dataset_version=args.dataset_version,
                apply=args.apply,
                confirm=args.confirm,
            )
            summaries.append(summary)
            tf = summary.get("tables_failed") or []
            tw = summary.get("tables_written") or {}
            if tf:
                # 若所有表 written=0 + error 非空,视为 full fail; 否则 partial
                if summary.get("error") and all(rc == 0 for rc in tw.values()):
                    any_full_fail = True
                else:
                    any_partial_fail = True
        except Exception as exc:
            had_uncaught_exception = True
            log.error("bar %s failed: %r", bar_start.isoformat(), exc)
            # 后续 bar 仍然继续尝试 (batch 内独立失败)
            summaries.append({
                "symbol": args.symbol,
                "bar_start": bar_start.isoformat(),
                "bar_end": bar_end.isoformat(),
                "error": repr(exc),
                "mode": "apply" if (args.apply and args.confirm) else "dry-run",
            })

    log.info("=== SUMMARY ===")
    for s in summaries:
        log.info("%s", s)

    # Exit code 语义 (P0-a 修复后):
    #   0 = 所有 bar 全部表全部写入成功
    #   1 = 某 bar 主 try/except 抛 exception (historic 语义保留)
    #   2 = partial fail (某些表写 0 行 但至少一张表成功)
    #   3 = full fail (某 bar 所有表 written=0 + error 非空)
    # 打 stdout 显式信号,让 scheduler task log_tail 能 grep 而非只靠 exit code
    if had_uncaught_exception:
        print("TASK_UNCAUGHT_EXCEPTION: 见 log 顶部堆栈", flush=True)
        return 1
    if any_full_fail:
        tf_summary = [
            (s.get("bar_start"), s.get("tables_failed"))
            for s in summaries if s.get("tables_failed")
        ]
        print(f"TASK_FULL_FAIL: bars={tf_summary}", flush=True)
        return 3
    if any_partial_fail:
        tf_summary = [
            (s.get("bar_start"), s.get("tables_failed"))
            for s in summaries if s.get("tables_failed")
        ]
        print(f"TASK_PARTIAL_FAIL: bars={tf_summary}", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
