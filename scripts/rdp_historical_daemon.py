#!/usr/bin/env python3
"""Historical data aggregation — manual ZIP consumer.

⚠️ DEPRECATION NOTICE (2026-04-07)
─────────────────────────────────────────────────────────────────────
本脚本的"常驻 daemon 30s 扫描"模式已被废弃。原因:
  - historical 数据是人工拖放的 OKX 历史 ZIP, 不会自己产生新文件
  - 30s 轮询空目录纯属 CPU 浪费, 增加运维负担
  - 没有任何"实时"需求 — 拖完文件后手动跑一次即可

✅ 推荐用法 (Step 4 迁移路径):
    # 操作员拖完 ZIP 后, 手动执行一次:
    python scripts/rdp_historical_daemon.py --once

如果担心忘记手动触发, 可以加 cron 兜底 (低频):
    # 每 4 小时扫描一次, 静默处理
    0 */4 * * * python scripts/rdp_historical_daemon.py --once

详见 docs/operations/rdp_scheduling_strategy.md "数据采集迁移到日批" 章节。
─────────────────────────────────────────────────────────────────────

定时扫描 incoming 目录，发现新 ZIP 文件后自动消费：
  incoming/ → 解析 → staging → bronze → silver → Gold
  成功 → 移到 completed/
  失败 → 移到 failed/（附 .error 日志）

目录约定（timeframe 由子目录名推断）：
  incoming/
  ├── candles_spot/1m/   ← spot candle 1m
  ├── candles_spot/5m/
  ├── candles_swap/1m/   ← swap candle 1m
  ├── candles_swap/1h/
  ├── funding_swap/      ← swap funding（无需 timeframe）
  └── ...

Usage:
    # ✅ 推荐: 单次扫描 (cron 友好)
    python scripts/rdp_historical_daemon.py --once

    # ⚠️ deprecated: 持续运行 (会打印 deprecation 警告)
    python scripts/rdp_historical_daemon.py

    # ⚠️ deprecated: 自定义扫描间隔
    python scripts/rdp_historical_daemon.py --interval 60
"""

from __future__ import annotations

import logging
import shutil
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_historical")


# ---------------------------------------------------------------------------
# 目录名 → (domain, instrument_type, timeframe) 映射
# ---------------------------------------------------------------------------
_SUBDIR_MAP: dict[str, tuple[str, str, str | None]] = {
    # candles_spot
    "candles_spot/1m":  ("candles", "spot", "1m"),
    "candles_spot/5m":  ("candles", "spot", "5m"),
    "candles_spot/15m": ("candles", "spot", "15m"),
    "candles_spot/1h":  ("candles", "spot", "1H"),
    # candles_swap
    "candles_swap/1m":  ("candles", "swap", "1m"),
    "candles_swap/5m":  ("candles", "swap", "5m"),
    "candles_swap/15m": ("candles", "swap", "15m"),
    "candles_swap/1h":  ("candles", "swap", "1H"),
    # funding
    "funding_swap":     ("funding", "swap", None),
}


def _classify_zip(zip_path: Path, incoming_root: Path) -> tuple[str, str, str | None] | None:
    """从 ZIP 文件相对 incoming 根目录的路径推断 domain / instrument / timeframe。

    例: incoming/candles_swap/1m/BTC-USDT-SWAP-xxx.zip
        相对路径 = candles_swap/1m/BTC-USDT-SWAP-xxx.zip
        匹配 key = candles_swap/1m
    """
    try:
        rel = zip_path.relative_to(incoming_root)
    except ValueError:
        return None

    # 从最长前缀开始匹配
    parts = rel.parts[:-1]  # 去掉文件名
    for depth in range(len(parts), 0, -1):
        key = "/".join(parts[:depth])
        if key in _SUBDIR_MAP:
            return _SUBDIR_MAP[key]
    return None


def _move_file(src: Path, dest_root: Path, incoming_root: Path) -> Path:
    """将文件移到 completed/ 或 failed/，保留相对子目录结构。"""
    try:
        rel = src.relative_to(incoming_root)
    except ValueError:
        rel = Path(src.name)

    dest = dest_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    return dest


def _write_error_file(zip_path: Path, dest_root: Path, incoming_root: Path, error: str) -> None:
    """在 failed 目录中写入 .error 日志。"""
    try:
        rel = zip_path.relative_to(incoming_root)
    except ValueError:
        rel = Path(zip_path.name)

    err_file = dest_root / (str(rel) + ".error")
    err_file.parent.mkdir(parents=True, exist_ok=True)
    err_file.write_text(
        f"ts: {datetime.now(timezone.utc).isoformat()}\n"
        f"file: {zip_path}\n"
        f"error:\n{error}\n",
        encoding="utf-8",
    )


def scan_and_consume_once(
    incoming_dir: Path,
    completed_dir: Path,
    failed_dir: Path,
    *,
    auto_build_gold: bool = True,
) -> dict[str, int]:
    """扫描 incoming 一次，消费所有新 ZIP。

    返回: {"discovered": N, "succeeded": N, "failed": N, "skipped": N}
    """

    from aats.data_platform.collectors.backfill.candles_backfill_collector import (
        collect_backfill_candle_file,
    )
    from aats.data_platform.collectors.backfill.file_discovery import (
        discover_files,
        mark_source_file_status,
        register_source_file,
    )
    from aats.data_platform.collectors.backfill.funding_backfill_collector import (
        collect_backfill_funding_file,
    )
    from aats.data_platform.config import get_settings
    from aats.data_platform.db import get_session
    from aats.data_platform.merge.merge_pipeline import (
        run_candle_merge_pipeline,
        run_funding_merge_pipeline,
    )

    settings = get_settings()
    stats = {"discovered": 0, "succeeded": 0, "failed": 0, "skipped": 0}

    # 扫描所有 ZIP
    zip_files = sorted(incoming_dir.rglob("*.zip"))
    if not zip_files:
        return stats

    stats["discovered"] = len(zip_files)
    log.info("Discovered %d ZIP file(s) in %s", len(zip_files), incoming_dir)

    # 记录哪些 (symbol, timeframe) 被处理了，用于 Gold 构建
    processed_pairs: set[tuple[str, str, str]] = set()  # (symbol, timeframe, instrument)

    for zip_path in zip_files:
        # 1. 从目录推断 domain/instrument/timeframe
        classification = _classify_zip(zip_path, incoming_dir)
        if classification is None:
            log.warning(
                "Cannot classify %s — not under known subdirectory. Skipping.",
                zip_path.name,
            )
            stats["skipped"] += 1
            continue

        domain, inst_type, timeframe = classification

        # 2. 从文件名推断 symbol
        file_info = discover_files(zip_path.parent)
        symbol = None
        for fi in file_info:
            if Path(fi["source_path"]).name == zip_path.name:
                symbol = fi["symbol_hint"]
                break

        if not symbol:
            log.warning("Cannot extract symbol from filename: %s. Skipping.", zip_path.name)
            stats["skipped"] += 1
            continue

        log.info("Processing: %s → domain=%s, inst=%s, tf=%s, symbol=%s",
                 zip_path.name, domain, inst_type, timeframe, symbol)

        try:
            with get_session(settings) as session:
                # 3. 注册文件
                file_id = register_source_file(
                    session,
                    source_path=str(zip_path.resolve()),
                    dataset_domain=domain,
                    symbol_hint=symbol,
                    timeframe_hint=timeframe,
                    source_granularity=None,
                    file_size_bytes=zip_path.stat().st_size,
                )

                if file_id is None:
                    log.info("File already registered (duplicate), skipping: %s", zip_path.name)
                    _move_file(zip_path, completed_dir, incoming_dir)
                    stats["skipped"] += 1
                    continue

                # 4. 消费
                if domain == "candles":
                    if not timeframe:
                        mark_source_file_status(
                            session, file_id,
                            ingested_status="skipped",
                            parse_error="Missing timeframe: place in candles_*/1m|5m|15m|1h/",
                        )
                        session.commit()
                        _move_file(zip_path, failed_dir, incoming_dir)
                        _write_error_file(zip_path, failed_dir, incoming_dir,
                                          "Missing timeframe directory")
                        stats["failed"] += 1
                        continue

                    run_id = collect_backfill_candle_file(
                        session,
                        source_file_id=file_id,
                        zip_path=str(zip_path.resolve()),
                        symbol_hint=symbol,
                        timeframe=timeframe,
                    )
                    session.commit()
                    run_candle_merge_pipeline(
                        session,
                        symbol=symbol,
                        timeframe=timeframe,
                        ingest_run_id=run_id,
                    )
                    processed_pairs.add((symbol, timeframe, inst_type))

                elif domain == "funding":
                    run_id = collect_backfill_funding_file(
                        session,
                        source_file_id=file_id,
                        zip_path=str(zip_path.resolve()),
                        symbol_hint=symbol,
                    )
                    session.commit()
                    run_funding_merge_pipeline(
                        session,
                        symbol=symbol,
                        ingest_run_id=run_id,
                    )
                else:
                    mark_source_file_status(
                        session, file_id,
                        ingested_status="skipped",
                        parse_error=f"Unsupported domain: {domain}",
                    )
                    session.commit()
                    stats["skipped"] += 1
                    _move_file(zip_path, failed_dir, incoming_dir)
                    continue

            # 5. 成功 → 移到 completed
            dest = _move_file(zip_path, completed_dir, incoming_dir)
            log.info("  OK → moved to %s", dest)
            stats["succeeded"] += 1

        except Exception:
            tb = traceback.format_exc()
            log.exception("Failed to process: %s", zip_path.name)
            _move_file(zip_path, failed_dir, incoming_dir)
            _write_error_file(zip_path, failed_dir, incoming_dir, tb)
            stats["failed"] += 1

    # 6. 自动 Gold 构建（对 swap 数据）
    if auto_build_gold and processed_pairs:
        _auto_build_gold(processed_pairs, settings)

    return stats


def _auto_build_gold(
    processed_pairs: set[tuple[str, str, str]],
    settings: object,
) -> None:
    """对刚入库的 swap 数据自动触发 Gold 构建。"""
    from aats.data_platform.db import get_session
    from aats.data_platform.gold.replay_bar_builder import build_gold_replay_bars
    from aats.data_platform.models import candle_table_name
    from sqlalchemy import text

    for symbol, timeframe, inst_type in processed_pairs:
        if inst_type != "swap":
            continue  # Gold replay bars 当前只对 swap 有意义（含 funding rate）

        try:
            with get_session(settings) as session:  # type: ignore[arg-type]
                # 查询该 symbol+timeframe 在 silver 层的时间范围
                table = candle_table_name("silver", symbol, timeframe)
                row = session.execute(
                    text(f"SELECT min(ts), max(ts) FROM {table} WHERE symbol = :sym"),
                    {"sym": symbol.upper()},
                ).fetchone()

                if not row or row[0] is None:
                    continue

                build_gold_replay_bars(
                    session,
                    symbol=symbol.upper(),
                    timeframe=timeframe,
                    window_start=row[0],
                    window_end=row[1],
                )
            log.info("  Gold auto-build: %s %s", symbol, timeframe)
        except Exception:
            log.exception("Gold auto-build failed: %s %s", symbol, timeframe)


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------

def run_historical_daemon(
    *,
    once: bool = False,
    interval: int | None = None,
) -> None:
    """启动历史数据聚合 daemon。

    ⚠️ 当 once=False 时打印 deprecation 警告。常驻轮询模式已被废弃,
    推荐操作员拖完 ZIP 后手动执行 --once 一次, 或 cron 低频兜底。
    """
    from aats.data_platform.config import get_settings
    from aats.data_platform.db import validate_rdp_schema

    if not once:
        log.warning("=" * 70)
        log.warning("DEPRECATION: historical daemon 常驻轮询模式已废弃。")
        log.warning("推荐用法 (拖完 ZIP 后手动一次):")
        log.warning("  python scripts/rdp_historical_daemon.py --once")
        log.warning("或 cron 低频兜底:")
        log.warning("  0 */4 * * * python scripts/rdp_historical_daemon.py --once")
        log.warning("详见 docs/operations/rdp_scheduling_strategy.md")
        log.warning("=" * 70)

    settings = get_settings()
    validate_rdp_schema(settings)

    incoming = Path(settings.historical_incoming_dir)
    completed = Path(settings.historical_completed_dir)
    failed = Path(settings.historical_failed_dir)

    # 确保目录存在
    for d in (incoming, completed, failed):
        d.mkdir(parents=True, exist_ok=True)

    scan_interval = interval or settings.historical_scan_interval_seconds

    log.info("Historical daemon started")
    log.info("  incoming  : %s", incoming.resolve())
    log.info("  completed : %s", completed.resolve())
    log.info("  failed    : %s", failed.resolve())
    log.info("  interval  : %ds%s", scan_interval, " (once)" if once else "")

    while True:
        try:
            stats = scan_and_consume_once(incoming, completed, failed)
            if stats["discovered"] > 0:
                log.info(
                    "Scan result: discovered=%d, succeeded=%d, failed=%d, skipped=%d",
                    stats["discovered"], stats["succeeded"],
                    stats["failed"], stats["skipped"],
                )
        except Exception:
            log.exception("Historical daemon scan error")

        if once:
            break

        time.sleep(scan_interval)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Historical data aggregation daemon")
    parser.add_argument("--once", action="store_true", help="Scan once and exit")
    parser.add_argument("--interval", type=int, default=None,
                        help="Scan interval in seconds (default: from config)")
    args = parser.parse_args()

    run_historical_daemon(once=args.once, interval=args.interval)


if __name__ == "__main__":
    main()
