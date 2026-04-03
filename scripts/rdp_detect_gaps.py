#!/usr/bin/env python3
"""Detect gaps in Silver candle data and optionally create repair runs.

Usage:
    python scripts/rdp_detect_gaps.py --symbol BTC-USDT --timeframe 1m \
        --start 2026-01-01 --end 2026-04-01 [--repair]
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rdp_gaps")


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect gaps in Silver candle data")
    parser.add_argument("--symbol", type=str, required=True)
    parser.add_argument("--timeframe", type=str, required=True)
    parser.add_argument("--start", type=str, required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", type=str, required=True, help="YYYY-MM-DD")
    parser.add_argument("--repair", action="store_true", help="Create gap_repair runs")
    args = parser.parse_args()

    from aats.data_platform.config import get_settings
    from aats.data_platform.db import get_session
    from aats.data_platform.jobs.gap_repair import create_gap_repair_runs, detect_candle_gaps

    settings = get_settings()
    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    with get_session(settings) as session:
        gaps = detect_candle_gaps(
            session, symbol=args.symbol, timeframe=args.timeframe,
            window_start=start, window_end=end,
        )

    if not gaps:
        log.info("No gaps detected for %s %s", args.symbol, args.timeframe)
        return

    log.info("Detected %d gaps:", len(gaps))
    for g in gaps:
        log.info("  %s -> %s (%d missing bars)", g["gap_start"], g["gap_end"], g["missing_bars"])

    if args.repair:
        with get_session(settings) as session:
            run_ids = create_gap_repair_runs(
                session, symbol=args.symbol, timeframe=args.timeframe, gaps=gaps,
            )
        log.info("Created %d gap_repair runs", len(run_ids))


if __name__ == "__main__":
    main()
