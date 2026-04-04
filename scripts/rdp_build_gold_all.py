#!/usr/bin/env python3
"""Batch build Gold replay bars for all swap symbol x timeframe combinations.

自动查询每个 Silver 表的时间范围，然后触发 Gold 构建。
一次性把所有缺失的 Gold 表补齐。

Usage:
    # 构建所有 swap Gold 表（自动检测 Silver 数据范围）
    python scripts/rdp_build_gold_all.py

    # 只构建指定 symbol
    python scripts/rdp_build_gold_all.py --symbol BTC-USDT-SWAP

    # 只构建指定 timeframe
    python scripts/rdp_build_gold_all.py --timeframe 5m --timeframe 15m

    # dry-run：只显示会做什么，不实际构建
    python scripts/rdp_build_gold_all.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_gold_all")

# 默认 swap symbols 和 timeframes
_DEFAULT_SYMBOLS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
_DEFAULT_TIMEFRAMES = ["1m", "5m", "15m", "1H"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch build Gold replay bars for all swap combinations",
    )
    parser.add_argument(
        "--symbol", action="append", default=None,
        help="Swap symbol(s) to build (default: BTC-USDT-SWAP, ETH-USDT-SWAP)",
    )
    parser.add_argument(
        "--timeframe", action="append", default=None,
        help="Timeframe(s) to build (default: 1m, 5m, 15m, 1H)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Only show what would be built, do not execute",
    )
    args = parser.parse_args()

    from sqlalchemy import text

    from aats.data_platform.config import get_settings
    from aats.data_platform.db import get_session
    from aats.data_platform.gold.replay_bar_builder import build_gold_replay_bars
    from aats.data_platform.models import candle_table_name

    settings = get_settings()
    symbols = args.symbol or _DEFAULT_SYMBOLS
    timeframes = args.timeframe or _DEFAULT_TIMEFRAMES

    log.info("=== Gold batch build ===")
    log.info("  Symbols   : %s", symbols)
    log.info("  Timeframes: %s", timeframes)
    if args.dry_run:
        log.info("  Mode      : DRY RUN")

    succeeded = 0
    skipped = 0
    failed = 0

    for symbol in symbols:
        for tf in timeframes:
            try:
                with get_session(settings) as session:
                    # 查询 Silver 层的时间范围
                    table = candle_table_name("silver", symbol, tf)
                    row = session.execute(
                        text(f"SELECT min(ts), max(ts), count(*) FROM {table} WHERE symbol = :sym"),
                        {"sym": symbol.upper()},
                    ).fetchone()

                    if not row or row[0] is None or row[2] == 0:
                        log.info("  [SKIP] %s %s — Silver 无数据", symbol, tf)
                        skipped += 1
                        continue

                    ts_min: datetime = row[0]
                    ts_max: datetime = row[1]
                    count: int = row[2]

                    # 查询 Gold 层已有记录数
                    inst = "swap" if "SWAP" in symbol.upper() else "spot"
                    tf_lower = tf.lower()
                    gold_table = f"gold.market_{inst}_replay_bars_{tf_lower}"
                    gold_row = session.execute(
                        text(f"SELECT count(*) FROM {gold_table} WHERE symbol = :sym"),
                        {"sym": symbol.upper()},
                    ).fetchone()
                    gold_count = gold_row[0] if gold_row else 0

                    log.info(
                        "  %s %s: Silver %d bars [%s ~ %s], Gold 已有 %d bars",
                        symbol, tf, count,
                        ts_min.strftime("%Y-%m-%d %H:%M"),
                        ts_max.strftime("%Y-%m-%d %H:%M"),
                        gold_count,
                    )

                    if args.dry_run:
                        log.info("    -> [DRY RUN] 会构建 Gold: %s %s", symbol, tf)
                        succeeded += 1
                        continue

                    # 构建 Gold
                    run_id = build_gold_replay_bars(
                        session,
                        symbol=symbol.upper(),
                        timeframe=tf,
                        window_start=ts_min,
                        window_end=ts_max,
                    )
                    log.info("    -> OK (run_id=%s)", run_id)
                    succeeded += 1

            except Exception:
                log.exception("  [FAIL] %s %s", symbol, tf)
                failed += 1

    log.info("")
    log.info("=== 完成: succeeded=%d, skipped=%d, failed=%d ===", succeeded, skipped, failed)


if __name__ == "__main__":
    main()
