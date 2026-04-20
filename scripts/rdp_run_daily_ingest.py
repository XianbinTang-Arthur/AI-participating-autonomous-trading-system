#!/usr/bin/env python3
"""RDP 日批数据采集 — daemon 替代方案 (2026-04-07 起推荐).

目标:
  替代 rdp_realtime_daemon.py 的常驻 60s tick 模式。
  本脚本设计为通过 cron / Task Scheduler 每天调用一次,
  从 OKX REST 增量拉取昨日全部数据 (candles + funding),
  然后构建 Gold 层和检测 Gap。

为什么不再需要 daemon?
  - RDP 所有消费方都是 daily/weekly cadence (data_maintenance / governance_cycle
    / research_cycle / decision_cycle), 没有任何环节需要 intra-minute 新鲜度。
  - 实盘交易引擎自有 OKX websocket 直连, 从不读 RDP 的 Bronze/Silver/Gold。
  - daemon 60s tick 每天产生 ~7800 次 OKX REST 调用 (4 symbol × 5 tf), 99% 浪费。

工作内容 (一次执行):
  1. 对每个 (symbol, timeframe), 调用 collect_candles_incremental() 增量拉取
     从 checkpoint 之后的全部 bar (足以覆盖 24h+ 缺口)
  2. 对每个 swap symbol, 调用 collect_funding_incremental() 增量拉取
     从 checkpoint 之后的全部 funding 事件
  3. 对每个新增 (symbol, timeframe), 重建 Gold replay bars
  4. 在 silver 层运行 gap 检测 (lookback 由 settings.gap_auto_detect_window_hours)

退出码:
  0 = 全部成功
  1 = 至少一个 symbol/timeframe 失败 (但其他成功的会保留)
  2 = 配置或参数错误

Usage:
    # 拉取所有默认 symbol × timeframe (推荐 cron 用法)
    python scripts/rdp_run_daily_ingest.py

    # Dry run, 仅打印将执行的工作
    python scripts/rdp_run_daily_ingest.py --dry-run

    # 跳过 Gold 构建 (仅采集 Bronze/Silver)
    python scripts/rdp_run_daily_ingest.py --no-gold

    # 跳过 Gap 检测
    python scripts/rdp_run_daily_ingest.py --no-gap-check

    # 限制 symbol / timeframe (覆盖 settings 默认值)
    python scripts/rdp_run_daily_ingest.py --symbols BTC-USDT-SWAP --timeframes 15m 1H

    # 显式拉取窗口大小 (默认从 checkpoint, 用 max_pages 控制最远回拉)
    python scripts/rdp_run_daily_ingest.py --max-pages 30

Cron 示例:
    # 每天 04:00 UTC 自动拉取昨日数据
    0 4 * * * cd /path/to/aats && python scripts/rdp_run_daily_ingest.py \\
        >> /var/log/rdp/daily_ingest.log 2>&1

    # 或纳入 data_maintenance workflow (推荐, 已配置):
    0 4 * * * python scripts/rdp_run_scheduled_workflow.py --workflow data_maintenance
"""

from __future__ import annotations

import argparse
import logging
import sys
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_daily_ingest")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="RDP 日批数据采集 (daemon 替代方案)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--symbols", nargs="*", default=None,
        help="覆盖默认 symbol 列表 (默认使用 settings.rolling_candles_symbols)",
    )
    p.add_argument(
        "--timeframes", nargs="*", default=None,
        help="覆盖默认 timeframe 列表 (默认使用 settings.rolling_candles_timeframes)",
    )
    p.add_argument(
        "--funding-symbols", nargs="*", default=None,
        help="覆盖默认 funding symbol 列表 (默认使用 settings.rolling_funding_symbols)",
    )
    p.add_argument(
        "--max-pages", type=int, default=30,
        help="单次 collect 的最大分页数 (默认 30, 可覆盖 ~3000 个 bar, "
             "足够 1m × 24h 或 15m × 30 天)",
    )
    p.add_argument(
        "--no-gold", action="store_true",
        help="跳过 Gold replay bars 构建",
    )
    p.add_argument(
        "--no-gap-check", action="store_true",
        help="跳过 Gap 检测",
    )
    p.add_argument(
        # 2026-04-20 P0-c Option A 新增: candles_rolling_15m workflow 每 15min
        # 只需要 candles, 不需要 funding (funding 由 data_maintenance 日批负责).
        "--no-funding", action="store_true",
        help="跳过 Funding 增量采集 (2026-04-20 P0-c: 给 candles_rolling_15m "
             "workflow 用, 避免 15min cadence 重复拉 funding)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="仅打印计划, 不实际执行",
    )
    p.add_argument(
        "--ensure-schema", action="store_true",
        help="执行前运行 DB migration",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# 单步函数 (各自捕获异常, 不让单点失败拖垮整个 run)
# ---------------------------------------------------------------------------

def _ingest_candles(
    settings,
    *,
    symbols: list[str],
    timeframes: list[str],
    max_pages: int,
    dry_run: bool,
) -> tuple[int, int, set[tuple[str, str]]]:
    """对每个 (symbol, timeframe) 增量采集 candles.

    Returns:
        (success_count, failure_count, processed_pairs)
        processed_pairs 用于后续 Gold 构建。
    """
    from aats.data_platform.collectors.rolling.candles_api_collector import (
        collect_candles_incremental,
    )
    from aats.data_platform.db import get_session
    from aats.data_platform.merge.merge_pipeline import (
        ValidationBlockedError,
        run_candle_merge_pipeline,
    )

    success = 0
    failure = 0
    processed: set[tuple[str, str]] = set()

    for symbol in symbols:
        for tf in timeframes:
            label = f"{symbol} {tf}"
            if dry_run:
                log.info("[DRY] Would ingest candles: %s (max_pages=%d)", label, max_pages)
                processed.add((symbol, tf))
                continue
            t0 = _time.monotonic()
            try:
                with get_session(settings) as session:
                    run_id = collect_candles_incremental(
                        session, settings,
                        symbol=symbol, timeframe=tf,
                        max_pages=max_pages,
                    )
                    run_candle_merge_pipeline(
                        session, symbol=symbol, timeframe=tf, ingest_run_id=run_id,
                    )
                elapsed = _time.monotonic() - t0
                log.info("[Candles] OK %s (%.1fs)", label, elapsed)
                success += 1
                processed.add((symbol, tf))
            except ValidationBlockedError:
                log.warning("[Candles] Quality gate blocked merge: %s", label)
                failure += 1
            except Exception:
                log.exception("[Candles] FAILED: %s", label)
                failure += 1

    return success, failure, processed


def _ingest_funding(
    settings,
    *,
    symbols: list[str],
    max_pages: int,
    dry_run: bool,
) -> tuple[int, int]:
    """对每个 swap symbol 增量采集 funding rate."""
    from aats.data_platform.collectors.rolling.funding_api_collector import (
        collect_funding_incremental,
    )
    from aats.data_platform.db import get_session
    from aats.data_platform.merge.merge_pipeline import (
        ValidationBlockedError,
        run_funding_merge_pipeline,
    )

    success = 0
    failure = 0

    for symbol in symbols:
        if dry_run:
            log.info("[DRY] Would ingest funding: %s (max_pages=%d)", symbol, max_pages)
            continue
        t0 = _time.monotonic()
        try:
            with get_session(settings) as session:
                run_id = collect_funding_incremental(
                    session, settings,
                    symbol=symbol, max_pages=max_pages,
                )
                run_funding_merge_pipeline(
                    session, symbol=symbol, ingest_run_id=run_id,
                )
            elapsed = _time.monotonic() - t0
            log.info("[Funding] OK %s (%.1fs)", symbol, elapsed)
            success += 1
        except ValidationBlockedError:
            log.warning("[Funding] Quality gate blocked merge: %s", symbol)
            failure += 1
        except Exception:
            log.exception("[Funding] FAILED: %s", symbol)
            failure += 1

    return success, failure


def _build_gold(
    settings,
    *,
    pairs: set[tuple[str, str]],
    dry_run: bool,
) -> tuple[int, int]:
    """对每个 (symbol, timeframe) 重建 Gold replay bars (仅 swap)."""
    from sqlalchemy import text

    from aats.data_platform.db import get_session
    from aats.data_platform.gold.replay_bar_builder import build_gold_replay_bars
    from aats.data_platform.models import candle_table_name, instrument_type_for_symbol

    success = 0
    failure = 0

    for symbol, tf in sorted(pairs):
        if instrument_type_for_symbol(symbol) != "swap":
            # Gold replay bars 当前只对 swap 有意义 (含 funding rate)
            continue
        label = f"{symbol} {tf}"
        if dry_run:
            log.info("[DRY] Would build Gold: %s", label)
            continue
        t0 = _time.monotonic()
        try:
            with get_session(settings) as session:
                table = candle_table_name("silver", symbol, tf)
                row = session.execute(
                    text(f"SELECT min(ts), max(ts) FROM {table} WHERE symbol = :sym"),
                    {"sym": symbol.upper()},
                ).fetchone()
                if not row or row[0] is None:
                    log.info("[Gold] Skipped %s (silver empty)", label)
                    continue
                build_gold_replay_bars(
                    session,
                    symbol=symbol.upper(),
                    timeframe=tf,
                    window_start=row[0],
                    window_end=row[1],
                )
            elapsed = _time.monotonic() - t0
            log.info("[Gold] OK %s (%.1fs)", label, elapsed)
            success += 1
        except Exception:
            log.exception("[Gold] FAILED: %s", label)
            failure += 1

    return success, failure


def _detect_gaps(
    settings,
    *,
    symbols: list[str],
    timeframes: list[str],
    dry_run: bool,
) -> tuple[int, int]:
    """运行 Gap 检测, 返回 (检测的 symbol×tf 数, 发现的 gap 总数)."""
    from aats.data_platform.db import get_session
    from aats.data_platform.jobs.gap_repair import (
        create_gap_repair_runs,
        detect_candle_gaps,
    )

    now = datetime.now(timezone.utc)
    lookback_h = getattr(settings, "gap_auto_detect_window_hours", 24)
    window_start = now - timedelta(hours=lookback_h)
    window_end = now

    checked = 0
    total_gaps = 0

    for symbol in symbols:
        for tf in timeframes:
            label = f"{symbol} {tf}"
            if dry_run:
                log.info("[DRY] Would detect gaps: %s (lookback=%dh)", label, lookback_h)
                checked += 1
                continue
            try:
                with get_session(settings) as session:
                    gaps = detect_candle_gaps(
                        session,
                        symbol=symbol,
                        timeframe=tf,
                        window_start=window_start,
                        window_end=window_end,
                    )
                    if gaps:
                        log.info("[Gap] %s: %d gap(s) detected", label, len(gaps))
                        create_gap_repair_runs(
                            session, symbol=symbol, timeframe=tf, gaps=gaps,
                        )
                        total_gaps += len(gaps)
                checked += 1
            except Exception:
                log.exception("[Gap] Detection failed: %s", label)

    return checked, total_gaps


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()

    from aats.data_platform.config import get_settings
    from aats.data_platform.db import run_migrations

    settings = get_settings()

    if args.ensure_schema:
        log.info("Running DB migrations...")
        run_migrations(settings)

    symbols = args.symbols or settings.rolling_candles_symbols
    timeframes = args.timeframes or settings.rolling_candles_timeframes
    funding_symbols = args.funding_symbols or settings.rolling_funding_symbols

    print("=" * 70)
    print(f"  RDP Daily Ingest | {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)
    print(f"  Candles symbols   : {symbols}")
    print(f"  Candles timeframes: {timeframes}")
    print(f"  Funding symbols   : {funding_symbols}")
    print(f"  Max pages         : {args.max_pages}")
    print(f"  Build Gold        : {not args.no_gold}")
    print(f"  Detect Gaps       : {not args.no_gap_check}")
    print(f"  Ingest Funding    : {not args.no_funding}")
    if args.dry_run:
        print("  [DRY RUN MODE]")
    print("=" * 70)

    pipeline_started = _time.monotonic()
    overall_failure = 0

    # ── 1. Candles 增量采集 ──
    log.info("")
    log.info("[1/4] Ingesting candles...")
    c_ok, c_fail, processed_pairs = _ingest_candles(
        settings,
        symbols=symbols,
        timeframes=timeframes,
        max_pages=args.max_pages,
        dry_run=args.dry_run,
    )
    overall_failure += c_fail
    log.info("[1/4] Candles done: %d ok, %d failed", c_ok, c_fail)

    # ── 2. Funding 增量采集 ──
    f_ok, f_fail = 0, 0  # 默认 0, 防止 --no-funding 时 summary 引用未绑定变量
    if not args.no_funding:
        log.info("")
        log.info("[2/4] Ingesting funding...")
        f_ok, f_fail = _ingest_funding(
            settings,
            symbols=funding_symbols,
            max_pages=args.max_pages,
            dry_run=args.dry_run,
        )
        overall_failure += f_fail
        log.info("[2/4] Funding done: %d ok, %d failed", f_ok, f_fail)
    else:
        log.info("")
        log.info("[2/4] Skipped funding ingest (--no-funding)")

    # ── 3. Gold 构建 ──
    if not args.no_gold and processed_pairs:
        log.info("")
        log.info("[3/4] Building Gold replay bars...")
        g_ok, g_fail = _build_gold(
            settings, pairs=processed_pairs, dry_run=args.dry_run,
        )
        overall_failure += g_fail
        log.info("[3/4] Gold done: %d ok, %d failed", g_ok, g_fail)
    else:
        log.info("")
        log.info("[3/4] Skipped Gold build (--no-gold or no candles processed)")

    # ── 4. Gap 检测 ──
    if not args.no_gap_check:
        log.info("")
        log.info("[4/4] Detecting gaps...")
        checked, gaps = _detect_gaps(
            settings,
            symbols=symbols,
            timeframes=timeframes,
            dry_run=args.dry_run,
        )
        log.info("[4/4] Gap detection done: %d checked, %d total gaps", checked, gaps)
    else:
        log.info("")
        log.info("[4/4] Skipped gap detection (--no-gap-check)")

    elapsed = _time.monotonic() - pipeline_started

    print()
    print("=" * 70)
    print(f"  Daily ingest summary | {elapsed:.0f}s")
    print("=" * 70)
    print(f"  Candles : {c_ok} ok, {c_fail} failed")
    if args.no_funding:
        print("  Funding : skipped (--no-funding)")
    else:
        print(f"  Funding : {f_ok} ok, {f_fail} failed")
    if overall_failure == 0:
        print("  Status  : SUCCESS")
        return 0
    print(f"  Status  : PARTIAL ({overall_failure} failures, see logs above)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
