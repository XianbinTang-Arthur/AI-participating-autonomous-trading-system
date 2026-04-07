#!/usr/bin/env python3
"""Realtime data aggregation daemon.

⚠️ DEPRECATION NOTICE (2026-04-07)
─────────────────────────────────────────────────────────────────────
本 daemon 的"常驻 60s tick"模式已被废弃。原因:
  - RDP 所有消费方都是 daily/weekly cadence (data_maintenance / governance_cycle
    / research_cycle / decision_cycle), 没有任何环节需要 intra-minute 新鲜度。
  - 实盘交易引擎自有 OKX websocket 直连, 从不读 RDP 的 Bronze/Silver/Gold。
  - 60s tick 每天产生 ~7800 次 OKX REST 调用 (4 symbol × 5 tf), 99% 浪费。

✅ 推荐用法 (Step 1 迁移路径):
  改用 cron / Task Scheduler 每天调用 scripts/rdp_run_daily_ingest.py 一次。

  # Linux crontab (UTC 04:00 拉取昨日全部数据)
  0 4 * * * cd /path/to/aats && python scripts/rdp_run_daily_ingest.py

  # 或纳入 data_maintenance workflow (Step 3 推荐):
  0 4 * * * python scripts/rdp_run_scheduled_workflow.py --workflow data_maintenance

详见: docs/operations/rdp_scheduling_strategy.md "数据采集迁移到日批" 章节。
─────────────────────────────────────────────────────────────────────

历史功能 (保留以兼容旧代码路径):
  1. 滚动 candles 采集 （所有 symbol × 所有 timeframe）
  2. 滚动 funding 采集  （所有 swap symbol）
  3. 定期 Gold 层构建   （每 N 个周期）
  4. 定期 Gap 检测+修复  （每 M 个周期）

Usage (legacy, deprecated):
    # ⚠️ 不再推荐: 持续运行 60s tick (会打印 deprecation 警告)
    python scripts/rdp_realtime_daemon.py

    # ✅ 兼容用法: 只跑一个周期 (cron 友好)
    python scripts/rdp_realtime_daemon.py --once

    # 自定义间隔 (legacy)
    python scripts/rdp_realtime_daemon.py --interval 30
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_realtime")


def run_one_cycle(settings: object, *, cycle_count: int) -> None:
    """执行一个完整的实时聚合周期。

    包含：
    1. 滚动 candles/funding 采集（cadence 控制）
    2. 周期性 Gold 构建
    3. 周期性 Gap 检测
    """
    from aats.data_platform.config import ResearchPlatformSettings
    from aats.data_platform.jobs.scheduler import run_one_rolling_cycle

    assert isinstance(settings, ResearchPlatformSettings)

    # --- 1. 滚动采集（原 scheduler 逻辑，cadence 自动判断） ---
    run_one_rolling_cycle(settings)

    # --- 2. 定期 Gold 构建 ---
    gold_interval = settings.gold_auto_build_interval_cycles
    if (
        settings.gold_replay_build_enabled
        and gold_interval > 0
        and cycle_count > 0
        and cycle_count % gold_interval == 0
    ):
        _auto_build_gold_all(settings)

    # --- 3. 定期 Gap 检测 ---
    gap_interval = settings.gap_auto_detect_interval_cycles
    if (
        settings.auto_gap_repair_enabled
        and gap_interval > 0
        and cycle_count > 0
        and cycle_count % gap_interval == 0
    ):
        _auto_detect_gaps(settings)


def _auto_build_gold_all(settings: object) -> None:
    """对所有 swap symbol × timeframe 自动构建 Gold 层。"""
    from sqlalchemy import text

    from aats.data_platform.config import ResearchPlatformSettings
    from aats.data_platform.db import get_session
    from aats.data_platform.gold.replay_bar_builder import build_gold_replay_bars
    from aats.data_platform.models import SUPPORTED_TIMEFRAMES, candle_table_name

    assert isinstance(settings, ResearchPlatformSettings)

    log.info("[Gold] Auto-building Gold replay bars for all swap pairs...")
    symbols = settings.rolling_funding_symbols  # swap symbols 才有 funding
    timeframes = settings.rolling_candles_timeframes

    for symbol in symbols:
        for tf in timeframes:
            try:
                with get_session(settings) as session:
                    table = candle_table_name("silver", symbol, tf)
                    row = session.execute(
                        text(f"SELECT min(ts), max(ts) FROM {table} WHERE symbol = :sym"),
                        {"sym": symbol.upper()},
                    ).fetchone()

                    if not row or row[0] is None:
                        continue

                    build_gold_replay_bars(
                        session,
                        symbol=symbol.upper(),
                        timeframe=tf,
                        window_start=row[0],
                        window_end=row[1],
                    )
                log.info("[Gold] Built: %s %s", symbol, tf)
            except Exception:
                log.exception("[Gold] Failed: %s %s", symbol, tf)


def _auto_detect_gaps(settings: object) -> None:
    """对所有 symbol × timeframe 自动检测 Silver 层 gap。"""
    from aats.data_platform.config import ResearchPlatformSettings
    from aats.data_platform.db import get_session
    from aats.data_platform.jobs.gap_repair import create_gap_repair_runs, detect_candle_gaps
    from aats.data_platform.models import SUPPORTED_SYMBOLS

    assert isinstance(settings, ResearchPlatformSettings)

    log.info("[Gap] Auto-detecting gaps...")
    now = datetime.now(timezone.utc)
    lookback = timedelta(hours=settings.gap_auto_detect_window_hours)
    window_start = now - lookback
    window_end = now

    symbols = settings.rolling_candles_symbols
    timeframes = settings.rolling_candles_timeframes

    total_gaps = 0
    for symbol in symbols:
        for tf in timeframes:
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
                        log.info("[Gap] %s %s: %d gap(s) detected", symbol, tf, len(gaps))
                        create_gap_repair_runs(
                            session, symbol=symbol, timeframe=tf, gaps=gaps,
                        )
                        total_gaps += len(gaps)
            except Exception:
                log.exception("[Gap] Detection failed: %s %s", symbol, tf)

    if total_gaps == 0:
        log.info("[Gap] No gaps detected across all pairs.")


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------

def run_realtime_daemon(
    *,
    once: bool = False,
    interval: int = 60,
    max_iterations: int | None = None,
) -> None:
    """启动实时数据聚合 daemon。

    ⚠️ 当 once=False 时打印 deprecation 警告。常驻 60s tick 模式已被废弃,
    推荐改用 cron + scripts/rdp_run_daily_ingest.py 每天一次。
    """
    from aats.data_platform.config import get_settings
    from aats.data_platform.db import run_migrations

    if not once:
        log.warning("=" * 70)
        log.warning("DEPRECATION: realtime daemon 60s tick 模式已废弃。")
        log.warning("推荐改用 cron 每天调用一次:")
        log.warning("  0 4 * * * python scripts/rdp_run_daily_ingest.py")
        log.warning("或纳入 data_maintenance workflow:")
        log.warning("  0 4 * * * python scripts/rdp_run_scheduled_workflow.py --workflow data_maintenance")
        log.warning("详见 docs/operations/rdp_scheduling_strategy.md")
        log.warning("=" * 70)

    settings = get_settings()
    run_migrations(settings)

    log.info("Realtime daemon started")
    log.info("  interval      : %ds%s", interval, " (once)" if once else "")
    log.info("  candle symbols: %s", settings.rolling_candles_symbols)
    log.info("  candle tf     : %s", settings.rolling_candles_timeframes)
    log.info("  funding symbols: %s", settings.rolling_funding_symbols)
    log.info("  gold build    : every %d cycles", settings.gold_auto_build_interval_cycles)
    log.info("  gap detect    : every %d cycles", settings.gap_auto_detect_interval_cycles)

    cycle_count = 0

    while True:
        try:
            run_one_cycle(settings, cycle_count=cycle_count)
        except Exception:
            log.exception("Realtime daemon cycle error")

        cycle_count += 1

        if once:
            break
        if max_iterations is not None and cycle_count >= max_iterations:
            log.info("Reached max iterations (%d), exiting", max_iterations)
            break

        time.sleep(interval)

    log.info("Realtime daemon stopped after %d cycles.", cycle_count)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Realtime data aggregation daemon")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--interval", type=int, default=60, help="Cycle interval in seconds")
    parser.add_argument("--max-iterations", type=int, default=None)
    args = parser.parse_args()

    run_realtime_daemon(
        once=args.once,
        interval=args.interval,
        max_iterations=args.max_iterations,
    )


if __name__ == "__main__":
    main()
