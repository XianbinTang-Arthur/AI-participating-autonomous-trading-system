#!/usr/bin/env python3
"""OKX API 深度历史回填脚本.

直接通过 OKX REST API 分页拉取历史 candle 数据，突破 rolling collector
的 max_pages=10 限制，可回填数月甚至更长时间的 15m / 1H 数据。

流程:
  1. 查询数据库中已有数据的最早时间点
  2. 从该时间点向更早方向分页拉取 API 数据
  3. 分批写入 staging → merge 到 bronze → silver
  4. 可选自动重建 Gold replay bars

用法:
    # 回填 BTC-USDT-SWAP 的 15m 和 1H 数据，目标回填到 90 天前
    python scripts/rdp_deep_backfill_api.py --days 90

    # 只回填 15m
    python scripts/rdp_deep_backfill_api.py --timeframes 15m --days 120

    # 指定目标开始日期
    python scripts/rdp_deep_backfill_api.py --target-start 2025-12-01

    # 回填后自动重建 Gold
    python scripts/rdp_deep_backfill_api.py --days 90 --build-gold

    # 试运行（不写入数据库，只显示会拉取多少数据）
    python scripts/rdp_deep_backfill_api.py --days 90 --dry-run
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx
    from sqlalchemy.orm import Session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_deep_backfill")


# ── OKX API 参数 ──────────────────────────────────────────────────────

API_LIMIT = 100  # OKX 每页最多 100 条

_TF_DELTA = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1H": timedelta(hours=1),
}

# 每个 timeframe 的合理最大页数上限（安全阀）
# 15m: 300 pages × 100 bars = 30000 bars = 312.5 天
# 1H:  300 pages × 100 bars = 30000 bars = 1250 天
MAX_PAGES_HARD_LIMIT = 500


def _ts_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _parse_api_candle(item: list[str], symbol: str) -> dict[str, Any] | None:
    """解析单条 OKX API candle 数据."""
    if len(item) < 9:
        return None
    try:
        ts = datetime.fromtimestamp(int(item[0]) / 1000, tz=timezone.utc)
    except (ValueError, OSError):
        return None
    try:
        return {
            "symbol": symbol.upper(),
            "ts": ts,
            "open": Decimal(item[1]),
            "high": Decimal(item[2]),
            "low": Decimal(item[3]),
            "close": Decimal(item[4]),
            "vol": Decimal(item[5]) if item[5] else None,
            "vol_ccy": Decimal(item[6]) if item[6] else None,
            "vol_quote": Decimal(item[7]) if item[7] else None,
            "confirm": item[8] in ("1", "true", "True"),
            "raw_symbol": symbol,
            "raw_ts": item[0],
        }
    except (InvalidOperation, IndexError):
        return None


def _fetch_candles_page(
    client: "httpx.Client",
    base_url: str,
    symbol: str,
    timeframe: str,
    after_ms: int | None = None,
    timeout: float = 15.0,
) -> list[list[str]]:
    """调用 OKX history-candles API 获取一页数据.

    OKX 语义:
      after=X → 返回 ts < X 的数据（更早的数据）
      结果按 ts 降序排列（最新在前）
    """
    params: dict[str, str] = {
        "instId": symbol,
        "bar": timeframe,
        "limit": str(API_LIMIT),
    }
    if after_ms is not None:
        params["after"] = str(after_ms)

    url = f"{base_url}/api/v5/market/history-candles"
    resp = client.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != "0":
        raise RuntimeError(f"OKX API 错误: {body.get('msg', body)}")
    return body.get("data", [])


def _query_existing_range(session: "Session", symbol: str, timeframe: str) -> tuple[datetime | None, datetime | None]:
    """查询数据库中 silver 层已有数据的时间范围."""
    from sqlalchemy import text

    from aats.data_platform.models import candle_table_name

    table = candle_table_name("silver", symbol, timeframe)
    try:
        row = session.execute(
            text(f"SELECT min(ts), max(ts) FROM {table} WHERE symbol = :sym"),
            {"sym": symbol.upper()},
        ).fetchone()
        if row and row[0] is not None:
            return row[0], row[1]
    except Exception as e:
        log.warning("查询 %s 失败（表可能不存在）: %s", table, e)
        session.rollback()
    return None, None


def _write_staging_batch(
    session: "Session",
    symbol: str,
    timeframe: str,
    rows: list[dict[str, Any]],
    run_id: str,
    dataset_version: str,
) -> int:
    """将 candle 数据写入 staging 表."""
    from sqlalchemy import text

    from aats.data_platform.models import candle_table_name, utc_now

    if not rows:
        return 0

    table = candle_table_name("staging", symbol, timeframe)
    now = utc_now()

    values = [
        {
            **r,
            "source_file_id": None,
            "ingest_run_id": run_id,
            "dataset_version": dataset_version,
            "now": now,
        }
        for r in rows
    ]

    batch_size = 2000
    total = 0
    for i in range(0, len(values), batch_size):
        batch = values[i : i + batch_size]
        session.execute(
            text(f"""
                INSERT INTO {table}
                    (symbol, ts, open, high, low, close,
                     vol, vol_ccy, vol_quote, confirm,
                     raw_symbol, raw_ts, source_file_id,
                     ingest_run_id, dataset_version, created_at, updated_at)
                VALUES
                    (:symbol, :ts, :open, :high, :low, :close,
                     :vol, :vol_ccy, :vol_quote, :confirm,
                     :raw_symbol, :raw_ts, :source_file_id,
                     :ingest_run_id, :dataset_version, :now, :now)
            """),
            batch,
        )
        total += len(batch)
    return total


def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 (symbol, ts) 去重，保留第一次出现的行."""
    seen: set[tuple[str, datetime]] = set()
    result: list[dict[str, Any]] = []
    for r in rows:
        key = (r["symbol"], r["ts"])
        if key not in seen:
            seen.add(key)
            result.append(r)
    return result


def deep_backfill_one(
    symbol: str,
    timeframe: str,
    target_start: datetime,
    *,
    base_url: str = "https://www.okx.com",
    rate_limit_sleep: float = 0.15,
    timeout: float = 15.0,
    dataset_version: str = "v1.0",
    dry_run: bool = False,
    build_gold: bool = False,
    merge_every_n_pages: int = 50,
    refresh_existing: bool = False,
    refresh_end: datetime | None = None,
) -> dict[str, Any]:
    """对单个 symbol+timeframe 执行深度回填.

    Parameters
    ----------
    symbol : str
        例如 "BTC-USDT-SWAP"
    timeframe : str
        例如 "15m" 或 "1H"
    target_start : datetime
        目标回填到的最早时间
    base_url : str
        OKX REST API 基础 URL
    rate_limit_sleep : float
        API 请求间隔（秒）
    dry_run : bool
        若为 True，不写入数据库
    build_gold : bool
        完成后是否自动重建 Gold
    merge_every_n_pages : int
        每隔 N 页做一次 staging→silver 合并
    refresh_existing : bool
        覆盖刷新已存在的历史窗口，用于修复不完整或未确认的源行
    refresh_end : datetime | None
        覆盖刷新窗口的排他结束时间；refresh_existing=True 时必填

    Returns
    -------
    dict  包含统计信息
    """
    import httpx

    from aats.data_platform.config import get_settings
    from aats.data_platform.db import get_session

    settings = get_settings()
    if target_start.tzinfo is None or target_start.utcoffset() is None:
        raise ValueError("target_start must be timezone-aware")
    if refresh_existing:
        if refresh_end is None:
            raise ValueError("refresh_end is required when refresh_existing is enabled")
        if refresh_end.tzinfo is None or refresh_end.utcoffset() is None:
            raise ValueError("refresh_end must be timezone-aware")
        if refresh_end <= target_start:
            raise ValueError("refresh_end must be after target_start")
    stats = {
        "symbol": symbol,
        "timeframe": timeframe,
        "target_start": target_start.isoformat(),
        "pages_fetched": 0,
        "rows_fetched": 0,
        "rows_written_staging": 0,
        "rows_merged_bronze": 0,
        "rows_merged_silver": 0,
        "existing_min_ts": None,
        "existing_max_ts": None,
        "new_min_ts": None,
        "new_max_ts": None,
        "gold_built": False,
        "api_exhausted": False,
        "mode": "refresh_existing" if refresh_existing else "backfill_missing_history",
        "refresh_end": refresh_end.isoformat() if refresh_end is not None else None,
    }

    # 1. 查询已有数据范围
    with get_session(settings) as session:
        existing_min, existing_max = _query_existing_range(session, symbol, timeframe)

    if existing_min:
        stats["existing_min_ts"] = existing_min.isoformat()
        stats["existing_max_ts"] = existing_max.isoformat() if existing_max else None
        log.info(
            "[%s %s] 数据库已有数据: %s ~ %s",
            symbol, timeframe,
            existing_min.strftime("%Y-%m-%d %H:%M"),
            existing_max.strftime("%Y-%m-%d %H:%M") if existing_max else "?",
        )
    else:
        log.info("[%s %s] 数据库中无已有数据，将从当前时间开始向前回填", symbol, timeframe)

    # 如果已有数据的最早时间已经早于目标，无需回填
    if not refresh_existing and existing_min and existing_min <= target_start:
        log.info(
            "[%s %s] 已有数据起点 %s 已早于目标 %s，无需回填",
            symbol, timeframe,
            existing_min.strftime("%Y-%m-%d"),
            target_start.strftime("%Y-%m-%d"),
        )
        return stats

    # 2. 确定起始分页点
    # 从已有数据的最早时间开始向更早方向拉取
    # OKX after=X 返回 ts < X 的数据
    if refresh_existing:
        assert refresh_end is not None
        page_from_ms = _ts_ms(refresh_end)
    elif existing_min:
        page_from_ms = _ts_ms(existing_min)
    else:
        page_from_ms = _ts_ms(datetime.now(timezone.utc))

    target_ms = _ts_ms(target_start)
    all_rows: list[dict[str, Any]] = []
    page = 0
    consecutive_empty = 0

    if dry_run:
        log.info("[DRY RUN] 不会写入数据库")

    log.info(
        "[%s %s] 开始深度回填: 从 %s 向前到 %s",
        symbol, timeframe,
        _ms_to_dt(page_from_ms).strftime("%Y-%m-%d %H:%M"),
        target_start.strftime("%Y-%m-%d %H:%M"),
    )

    # 3. 分页拉取
    with httpx.Client() as client:
        while page < MAX_PAGES_HARD_LIMIT:
            try:
                raw_data = _fetch_candles_page(
                    client, base_url, symbol, timeframe,
                    after_ms=page_from_ms,
                    timeout=timeout,
                )
            except Exception as e:
                log.error("API 请求失败 (page=%d): %s", page, e)
                if page == 0:
                    raise
                # 非首页失败，保存已有数据
                log.warning("停止拉取，保存已获取的 %d 行数据", len(all_rows))
                break

            page += 1
            stats["pages_fetched"] = page

            if not raw_data:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    log.info("连续 %d 页无数据，API 数据已耗尽", consecutive_empty)
                    stats["api_exhausted"] = True
                    break
                time.sleep(rate_limit_sleep)
                continue

            consecutive_empty = 0

            # 解析数据
            page_rows = []
            for item in raw_data:
                parsed = _parse_api_candle(item, symbol)
                # Historical repair/backfill must never promote an open candle.
                # The rolling collector owns provisional current-bar updates.
                if parsed and parsed["confirm"] and (
                    not refresh_existing
                    or (
                        target_start <= parsed["ts"]
                        and refresh_end is not None
                        and parsed["ts"] < refresh_end
                    )
                ):
                    page_rows.append(parsed)

            all_rows.extend(page_rows)
            stats["rows_fetched"] += len(page_rows)

            # 获取本页最早时间戳
            oldest_ts_in_page = min(int(d[0]) for d in raw_data)
            oldest_dt = _ms_to_dt(oldest_ts_in_page)

            # 进度报告
            if page % 10 == 0:
                log.info(
                    "  [%s %s] page=%d, 本页 %d 条, 累计 %d 条, 最早到 %s",
                    symbol, timeframe, page, len(page_rows), len(all_rows),
                    oldest_dt.strftime("%Y-%m-%d %H:%M"),
                )

            # 检查是否已到目标时间
            if oldest_ts_in_page <= target_ms:
                log.info(
                    "[%s %s] 已到达目标时间 %s (page=%d)",
                    symbol, timeframe, target_start.strftime("%Y-%m-%d"), page,
                )
                break

            # 检查返回数据不足一页（已到 API 数据尽头）
            if len(raw_data) < API_LIMIT:
                log.info(
                    "[%s %s] API 返回不足一页 (%d/%d)，数据到底 (page=%d, 最早 %s)",
                    symbol, timeframe, len(raw_data), API_LIMIT, page,
                    oldest_dt.strftime("%Y-%m-%d %H:%M"),
                )
                stats["api_exhausted"] = True
                break

            # 下一页的起始点
            page_from_ms = oldest_ts_in_page

            # 分批合并（避免内存积累过大）
            if not dry_run and len(all_rows) >= merge_every_n_pages * API_LIMIT:
                log.info("  中间合并: %d 行", len(all_rows))
                _flush_and_merge(
                    all_rows, symbol, timeframe, dataset_version, settings, stats,
                )
                all_rows = []

            time.sleep(rate_limit_sleep)

    if dry_run:
        # 仅报告统计
        if all_rows:
            min_ts = min(r["ts"] for r in all_rows)
            max_ts = max(r["ts"] for r in all_rows)
            stats["new_min_ts"] = min_ts.isoformat()
            stats["new_max_ts"] = max_ts.isoformat()
            stats["rows_fetched"] = len(all_rows)
        log.info(
            "[DRY RUN] [%s %s] 共 %d 页, %d 行, 范围 %s ~ %s",
            symbol, timeframe,
            stats["pages_fetched"],
            len(all_rows),
            stats.get("new_min_ts", "N/A"),
            stats.get("new_max_ts", "N/A"),
        )
        return stats

    # 4. 最终 flush + merge
    if all_rows:
        _flush_and_merge(all_rows, symbol, timeframe, dataset_version, settings, stats)

    # 5. 重建 Gold
    if build_gold and stats["rows_merged_silver"] > 0:
        log.info("[%s %s] 开始重建 Gold replay bars...", symbol, timeframe)
        try:
            _rebuild_gold(symbol, timeframe, settings)
            stats["gold_built"] = True
            log.info("[%s %s] Gold 重建完成", symbol, timeframe)
        except Exception as e:
            log.error("[%s %s] Gold 重建失败: %s", symbol, timeframe, e)

    log.info(
        "[%s %s] 回填完成: %d 页, staging=%d, bronze=%d, silver=%d",
        symbol, timeframe,
        stats["pages_fetched"],
        stats["rows_written_staging"],
        stats["rows_merged_bronze"],
        stats["rows_merged_silver"],
    )
    return stats


def _flush_and_merge(
    rows: list[dict[str, Any]],
    symbol: str,
    timeframe: str,
    dataset_version: str,
    settings: Any,
    stats: dict[str, Any],
) -> None:
    """将行写入 staging 并执行 merge pipeline."""
    from aats.data_platform.db import get_session
    from aats.data_platform.jobs.run_registry import (
        create_ingest_run,
        create_run_item,
        finish_ingest_run,
        finish_run_item,
    )
    from aats.data_platform.merge.merge_pipeline import run_candle_merge_pipeline
    from aats.data_platform.models import instrument_type_for_symbol

    # 去重
    rows = _dedupe_rows(rows)
    if not rows:
        return

    inst_type = instrument_type_for_symbol(symbol)

    with get_session(settings) as session:
        # 创建 ingest run
        run_id = create_ingest_run(
            session,
            run_type="backfill",
            dataset_domain="candles",
            instrument_type=inst_type,
            symbol=symbol.upper(),
            timeframe=timeframe,
            trigger_mode="manual",
        )
        item_id = create_run_item(
            session,
            ingest_run_id=run_id,
            dataset_domain="candles",
            instrument_type=inst_type,
            symbol=symbol.upper(),
            timeframe=timeframe,
        )

        try:
            # 写 staging
            written = _write_staging_batch(
                session, symbol, timeframe, rows, run_id, dataset_version,
            )
            session.flush()  # 确保 staging 数据可被 merge 读取

            # 更新统计
            stats["rows_written_staging"] = stats.get("rows_written_staging", 0) + written

            min_ts = min(r["ts"] for r in rows)
            max_ts = max(r["ts"] for r in rows)
            if stats.get("new_min_ts") is None or min_ts.isoformat() < stats["new_min_ts"]:
                stats["new_min_ts"] = min_ts.isoformat()
            if stats.get("new_max_ts") is None or max_ts.isoformat() > stats["new_max_ts"]:
                stats["new_max_ts"] = max_ts.isoformat()

            # merge pipeline: staging → bronze → silver
            merge_result = run_candle_merge_pipeline(
                session,
                symbol=symbol,
                timeframe=timeframe,
                ingest_run_id=run_id,
                dataset_version=dataset_version,
                run_item_id=item_id,
            )
            stats["rows_merged_bronze"] = stats.get("rows_merged_bronze", 0) + merge_result["bronze_count"]
            stats["rows_merged_silver"] = stats.get("rows_merged_silver", 0) + merge_result["silver_count"]

            finish_ingest_run(session, run_id, status="succeeded")
            log.info(
                "  flush: staging=%d, bronze=%d, silver=%d (范围 %s ~ %s)",
                written,
                merge_result["bronze_count"],
                merge_result["silver_count"],
                min_ts.strftime("%Y-%m-%d %H:%M"),
                max_ts.strftime("%Y-%m-%d %H:%M"),
            )

        except Exception as exc:
            finish_run_item(session, item_id, status="failed", error_message=str(exc))
            finish_ingest_run(session, run_id, status="failed", error_message=str(exc))
            raise


def _rebuild_gold(symbol: str, timeframe: str, settings: Any) -> None:
    """重建整个 Silver 范围的 Gold replay bars."""
    from sqlalchemy import text

    from aats.data_platform.db import get_session
    from aats.data_platform.gold.replay_bar_builder import build_gold_replay_bars
    from aats.data_platform.models import candle_table_name

    with get_session(settings) as session:
        table = candle_table_name("silver", symbol, timeframe)
        row = session.execute(
            text(f"SELECT min(ts), max(ts) FROM {table} WHERE symbol = :sym"),
            {"sym": symbol.upper()},
        ).fetchone()

        if not row or row[0] is None:
            log.warning("Silver 层无数据，跳过 Gold 构建")
            return

        build_gold_replay_bars(
            session,
            symbol=symbol.upper(),
            timeframe=timeframe,
            window_start=row[0],
            window_end=row[1],
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="OKX API 深度历史 candle 回填",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 回填 90 天的 15m 和 1H 数据
  python scripts/rdp_deep_backfill_api.py --days 90

  # 只回填 15m，目标到 2025-12-01
  python scripts/rdp_deep_backfill_api.py --timeframes 15m --target-start 2025-12-01

  # 试运行
  python scripts/rdp_deep_backfill_api.py --days 90 --dry-run
        """,
    )
    parser.add_argument(
        "--symbol", default="BTC-USDT-SWAP",
        help="交易对 (默认: BTC-USDT-SWAP)",
    )
    parser.add_argument(
        "--timeframes", nargs="+", default=["15m", "1H"],
        help="要回填的时间框架 (默认: 15m 1H)",
    )
    parser.add_argument(
        "--days", type=int, default=None,
        help="目标回填天数（从今天往前推）",
    )
    parser.add_argument(
        "--target-start", type=str, default=None,
        help="目标回填起始日期 (YYYY-MM-DD)，与 --days 二选一",
    )
    parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="覆盖刷新已存在窗口，而不是只向已有最早时间之前回填",
    )
    parser.add_argument(
        "--refresh-end",
        type=str,
        default=None,
        help="覆盖刷新排他结束日期 (YYYY-MM-DD)，仅与 --refresh-existing 同用",
    )
    parser.add_argument(
        "--rate-limit", type=float, default=0.15,
        help="API 请求间隔秒数 (默认: 0.15)",
    )
    parser.add_argument(
        "--build-gold", action="store_true",
        help="回填后自动重建 Gold replay bars",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="试运行：只拉取数据统计，不写入数据库",
    )
    parser.add_argument(
        "--merge-every", type=int, default=50,
        help="每拉取多少页做一次 merge (默认: 50)",
    )

    args = parser.parse_args()

    # 解析目标时间
    if args.target_start:
        target_start = datetime.strptime(args.target_start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    elif args.days:
        target_start = datetime.now(timezone.utc) - timedelta(days=args.days)
    else:
        parser.error("必须指定 --days 或 --target-start")
        return
    refresh_end = (
        datetime.strptime(args.refresh_end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if args.refresh_end
        else None
    )
    if args.refresh_existing and refresh_end is None:
        parser.error("--refresh-existing 必须同时指定 --refresh-end")
    if not args.refresh_existing and refresh_end is not None:
        parser.error("--refresh-end 只能与 --refresh-existing 同用")

    log.info("=" * 60)
    log.info("OKX API 深度回填")
    log.info("  交易对  : %s", args.symbol)
    log.info("  时间框架: %s", ", ".join(args.timeframes))
    log.info("  目标起点: %s", target_start.strftime("%Y-%m-%d %H:%M UTC"))
    log.info("  执行模式: %s", "覆盖刷新" if args.refresh_existing else "缺失历史回填")
    if refresh_end is not None:
        log.info("  刷新终点: %s (exclusive)", refresh_end.strftime("%Y-%m-%d %H:%M UTC"))
    log.info("  请求间隔: %.2fs", args.rate_limit)
    log.info("  试运行  : %s", "是" if args.dry_run else "否")
    log.info("  重建Gold: %s", "是" if args.build_gold else "否")
    log.info("=" * 60)

    all_stats = []
    for tf in args.timeframes:
        if tf not in _TF_DELTA:
            log.error("不支持的时间框架: %s (支持: %s)", tf, ", ".join(_TF_DELTA.keys()))
            continue

        try:
            result = deep_backfill_one(
                symbol=args.symbol,
                timeframe=tf,
                target_start=target_start,
                rate_limit_sleep=args.rate_limit,
                dry_run=args.dry_run,
                build_gold=args.build_gold,
                merge_every_n_pages=args.merge_every,
                refresh_existing=args.refresh_existing,
                refresh_end=refresh_end,
            )
            all_stats.append(result)
        except Exception:
            log.exception("回填 %s %s 失败", args.symbol, tf)

    # 汇总报告
    log.info("")
    log.info("=" * 60)
    log.info("回填汇总:")
    log.info("-" * 60)
    for s in all_stats:
        log.info(
            "  %s %s: %d 页, staging=%d, bronze=%d, silver=%d | 范围 %s ~ %s%s%s",
            s["symbol"], s["timeframe"],
            s["pages_fetched"],
            s["rows_written_staging"],
            s.get("rows_merged_bronze", 0),
            s.get("rows_merged_silver", 0),
            s.get("new_min_ts", "N/A"),
            s.get("new_max_ts", "N/A"),
            " [API耗尽]" if s.get("api_exhausted") else "",
            " [Gold已建]" if s.get("gold_built") else "",
        )
    log.info("=" * 60)


if __name__ == "__main__":
    main()
