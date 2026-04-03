#!/usr/bin/env python3
"""Run rolling incremental ingestion for the Research Data Platform.

Can run as:
  - One-shot: python scripts/rdp_run_rolling.py --once
  - Continuous: python scripts/rdp_run_rolling.py --interval 60
"""

from __future__ import annotations

import argparse
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rdp_rolling")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run rolling incremental ingestion")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between cycles (continuous mode)")
    parser.add_argument("--max-iterations", type=int, default=None, help="Max cycles before exit")
    args = parser.parse_args()

    from aats.data_platform.config import get_settings
    from aats.data_platform.jobs.scheduler import run_one_rolling_cycle, run_scheduler_loop

    settings = get_settings()

    if args.once:
        log.info("Running single rolling cycle")
        run_one_rolling_cycle(settings)
        log.info("Single cycle complete.")
    else:
        log.info("Starting scheduler loop, interval=%ds", args.interval)
        run_scheduler_loop(settings, interval_seconds=args.interval, max_iterations=args.max_iterations)


if __name__ == "__main__":
    main()
