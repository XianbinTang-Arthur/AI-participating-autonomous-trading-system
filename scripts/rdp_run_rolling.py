#!/usr/bin/env python3
"""Run rolling incremental ingestion for the Research Data Platform.

⚠️ DEPRECATION NOTICE (2026-04-07)
─────────────────────────────────────────────────────────────────────
本脚本是 daemon 时代的 thin wrapper, 现已被 scripts/rdp_run_daily_ingest.py 取代。
区别:
  - rdp_run_daily_ingest.py: 完整的日批入口 (含 Gold 构建 + Gap 检测)
  - rdp_run_rolling.py: 仅做"采集 + merge", 不含 Gold/Gap

继续使用本脚本不会出错, 但推荐迁移到:
  python scripts/rdp_run_daily_ingest.py

详见 docs/operations/rdp_scheduling_strategy.md "数据采集迁移到日批" 章节。
─────────────────────────────────────────────────────────────────────

Usage:
  - One-shot: python scripts/rdp_run_rolling.py --once
  - Continuous mode (deprecated): python scripts/rdp_run_rolling.py --interval 60
"""

from __future__ import annotations

import argparse
import logging
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rdp_rolling")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run rolling incremental ingestion")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    parser.add_argument(
        "--interval", type=int, default=60,
        help="Seconds between cycles (deprecated continuous mode)",
    )
    parser.add_argument("--max-iterations", type=int, default=None, help="Max cycles before exit")
    parser.add_argument(
        "--max-pages", type=int, default=30,
        help="Max pagination pages per collect (default 30, covers ~24h+ data)",
    )
    args = parser.parse_args()

    from aats.data_platform.config import get_settings
    from aats.data_platform.jobs.scheduler import run_one_rolling_cycle

    settings = get_settings()

    if args.once:
        log.info("Running single rolling cycle")
        run_one_rolling_cycle(settings, max_pages=args.max_pages)
        log.info("Single cycle complete.")
        return

    # ── Continuous loop (deprecated) ──
    log.warning("=" * 70)
    log.warning("DEPRECATION: continuous loop mode 已废弃。")
    log.warning("推荐改用 cron 每天调用一次:")
    log.warning("  0 4 * * * python scripts/rdp_run_daily_ingest.py")
    log.warning("详见 docs/operations/rdp_scheduling_strategy.md")
    log.warning("=" * 70)

    iteration = 0
    log.info("Starting deprecated scheduler loop, interval=%ds", args.interval)
    while True:
        try:
            run_one_rolling_cycle(settings, max_pages=args.max_pages)
        except Exception:
            log.exception("Scheduler cycle error")

        iteration += 1
        if args.max_iterations is not None and iteration >= args.max_iterations:
            log.info("Reached max iterations (%d), exiting", args.max_iterations)
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
