#!/usr/bin/env python3
"""Build Gold replay bars from Silver data.

Usage:
    python scripts/rdp_build_gold.py --symbol BTC-USDT-SWAP --timeframe 15m \
        --start 2026-01-01 --end 2026-04-01
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rdp_gold")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Gold replay bars from Silver")
    parser.add_argument("--symbol", type=str, required=True, help="e.g. BTC-USDT-SWAP")
    parser.add_argument("--timeframe", type=str, required=True, help="e.g. 15m")
    parser.add_argument("--start", type=str, required=True, help="Window start YYYY-MM-DD")
    parser.add_argument("--end", type=str, required=True, help="Window end YYYY-MM-DD")
    args = parser.parse_args()

    from aats.data_platform.config import get_settings
    from aats.data_platform.db import get_session
    from aats.data_platform.gold.replay_bar_builder import build_gold_replay_bars

    settings = get_settings()
    window_start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    window_end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    log.info("Building Gold replay bars: %s %s [%s, %s]",
             args.symbol, args.timeframe, args.start, args.end)

    with get_session(settings) as session:
        run_id = build_gold_replay_bars(
            session,
            symbol=args.symbol,
            timeframe=args.timeframe,
            window_start=window_start,
            window_end=window_end,
        )

    log.info("Gold build complete. Run ID: %s", run_id)


if __name__ == "__main__":
    main()
