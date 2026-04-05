#!/usr/bin/env python3
"""OKX API 深度历史 funding rate 回填脚本.

直接通过 OKX REST API 分页拉取历史 funding rate 数据，突破 rolling collector
的 max_pages=10 限制，可回填数月甚至更长时间的 funding 数据。

流程:
  1. 查询数据库中已有 funding 数据的最早时间点
  2. 从该时间点向更早方向分页拉取 API 数据
  3. 分批写入 staging → merge 到 bronze → silver
  4. 完成后可配合 rdp_build_gold.py 重建 Gold

用法:
    # 回填 BTC-USDT-SWAP 的 funding 数据到 365 天前
    python scripts/rdp_deep_backfill_funding.py --days 365

    # 回填到指定日期
    python scripts/rdp_deep_backfill_funding.py --target-start 2025-04-01

    # 多个交易对
    python scripts/rdp_deep_backfill_funding.py --symbols BTC-USDT-SWAP ETH-USDT-SWAP --days 365

    # 试运行（不写入数据库）
    python scripts/rdp_deep_backfill_funding.py --days 365 --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_deep_backfill_funding")


# ── OKX API 参数 ──────────────────────────────────────────────────────

API_LIMIT = 100  # OKX 每页最多 100 条

# Funding 每 8 小时一次 → 100 条 = 33 天/页
# 500 页 × 100 条 = 50,000 条 ≈ 45 年（远超需求）
MAX_PAGES_HARD_LIMIT = 200  # 200 页 ≈ 18 年，足够


def _ts_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


# ── API 调用 ──────────────────────────────────────────────────────────


def _fetch_funding_page(
    client: "httpx.Client",
    base_url: str,
    symbol: str,
    after_ms: int | None = None,
    timeout: float = 15.0,
) -> list[dict[str, Any]]:
    """调用 OKX funding-rate-history API 获取一页数据.

    OKX 语义:
      after=X → 返回 fundingTime < X 的数据（更早的数据）
      结果按 fundingTime 降序排列（最新在前）
    """
    params: dict[str, str] = {
        "instId": symbol,
        "limit": str(API_LIMIT),
    }
    if after_ms is not None:
        params["after"] = str(after_ms)

    url = f"{base_url}/api/v5/public/funding-rate-history"
    resp = client.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != "0":
        raise RuntimeError(f"OKX funding API 错误: {body.get('msg', body)}")
    return body.get("data", [])


def _parse_funding(item: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    """解析单条 OKX API funding 数据."""
    try:
        ts = datetime.fromtimestamp(int(item["fundingTime"]) / 1000, tz=timezone.utc)
    except (ValueError, KeyError, OSError):
        return None
    try:
        rate = Decimal(item["fundingRate"])
    except (InvalidOperation, KeyError):
        return None
    return {
        "symbol": symbol.upper(),
        "ts": ts,
        "funding_rate": rate,
        "inst_type": item.get("instType"),
        "formula_type": item.get("formulaType"),
        "method": item.get("method"),
        "realized_rate": Decimal(item["realizedRate"]) if item.get("realizedRate") else None,
        "raw_symbol": item.get("instId", symbol),
        "raw_ts": item.get("fundingTime"),
    }


# ── 数据库操作 ─────────────────────────────────────────────────────────


def _query_existing_range(session: "Session", symbol: str) -> tuple[datetime | None, datetime | None]:
    """查询 silver.market_swap_funding 中已有数据的时间范围."""
    from sqlalchemy import text

    from aats.data_platform.models import funding_table_name

    table = funding_table_name("silver")
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
    rows: list[dict[str, Any]],
    run_id: str,
    dataset_version: str,
) -> int:
    """将 funding 数据写入 staging 表."""
    from sqlalchemy import text

    from aats.data_platform.models import funding_table_name, utc_now

    if not rows:
        return 0

    table = funding_table_name("staging")
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
                    (symbol, ts, funding_rate, inst_type, formula_type,
                     method, realized_rate,
                     raw_symbol, raw_ts, source_file_id,
                     ingest_run_id, dataset_version, created_at, updated_at)
                VALUES
                    (:symbol, :ts, :funding_rate, :inst_type, :formula_type,
                     :method, :realized_rate,
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


# ── 核心回填逻辑 ──────────────────────────────────────────────────────


def deep_backfill_funding(
    symbol: str,
    target_start: datetime,
    *,
    base_url: str = "https://www.okx.com",
    rate_limit_sleep: float = 0.15,
    timeout: float = 15.0,
    dataset_version: str = "v1.0",
    dry_run: bool = False,
    merge_every_n_pages: int = 30,
) -> dict[str, Any]:
    """对单个 symbol 执行 funding 深度回填.

    Parameters
    ----------
    symbol : str
        例如 "BTC-USDT-SWAP"
    target_start : datetime
        目标回填到的最早时间
    dry_run : bool
        若为 True，不写入数据库
    merge_every_n_pages : int
        每隔 N 页做一次 staging→silver 合并

    Returns
    -------
    dict  包含统计信息
    """
    import httpx

    from aats.data_platform.config import get_settings
    from aats.data_platform.db import get_session

    settings = get_settings()
    stats: dict[str, Any] = {
        "symbol": symbol,
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
        "api_exhausted": False,
    }

    # 1. 查询已有数据范围
    with get_session(settings) as session:
        existing_min, existing_max = _query_existing_range(session, symbol)

    if existing_min:
        stats["existing_min_ts"] = existing_min.isoformat()
        stats["existing_max_ts"] = existing_max.isoformat() if existing_max else None
        log.info(
            "[%s funding] 数据库已有数据: %s ~ %s",
            symbol,
            existing_min.strftime("%Y-%m-%d %H:%M"),
            existing_max.strftime("%Y-%m-%d %H:%M") if existing_max else "?",
        )
    else:
        log.info("[%s funding] 数据库中无已有数据，将从当前时间开始向前回填", symbol)

    # 如果已有数据的最早时间已经早于目标，无需回填
    if existing_min and existing_min <= target_start:
        log.info(
            "[%s funding] 已有数据起点 %s 已早于目标 %s，无需回填",
            symbol,
            existing_min.strftime("%Y-%m-%d"),
            target_start.strftime("%Y-%m-%d"),
        )
        return stats

    # 2. 确定起始分页点
    if existing_min:
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
        "[%s funding] 开始深度回填: 从 %s 向前到 %s",
        symbol,
        _ms_to_dt(page_from_ms).strftime("%Y-%m-%d %H:%M"),
        target_start.strftime("%Y-%m-%d %H:%M"),
    )

    # 3. 分页拉取
    with httpx.Client() as client:
        while page < MAX_PAGES_HARD_LIMIT:
            try:
                raw_data = _fetch_funding_page(
                    client, base_url, symbol,
                    after_ms=page_from_ms,
                    timeout=timeout,
                )
            except Exception as e:
                log.error("API 请求失败 (page=%d): %s", page, e)
                if page == 0:
                    raise
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
                parsed = _parse_funding(item, symbol)
                if parsed:
                    page_rows.append(parsed)

            all_rows.extend(page_rows)

            # 获取本页最早时间戳
            oldest_ts_in_page = min(int(d["fundingTime"]) for d in raw_data)
            oldest_dt = _ms_to_dt(oldest_ts_in_page)

            # 进度报告
            if page % 5 == 0:
                log.info(
                    "  [%s funding] page=%d, 本页 %d 条, 累计 %d 条, 最早到 %s",
                    symbol, page, len(page_rows), len(all_rows),
                    oldest_dt.strftime("%Y-%m-%d %H:%M"),
                )

            # 检查是否已到目标时间
            if oldest_ts_in_page <= target_ms:
                log.info(
                    "[%s funding] 已到达目标时间 %s (page=%d)",
                    symbol, target_start.strftime("%Y-%m-%d"), page,
                )
                break

            # 检查返回数据不足一页（已到 API 数据尽头）
            if len(raw_data) < API_LIMIT:
                log.info(
                    "[%s funding] API 返回不足一页 (%d/%d)，数据到底 (page=%d, 最早 %s)",
                    symbol, len(raw_data), API_LIMIT, page,
                    oldest_dt.strftime("%Y-%m-%d %H:%M"),
                )
                stats["api_exhausted"] = True
                break

            # 下一页的起始点
            page_from_ms = oldest_ts_in_page

            # 分批合并（避免内存积累过大）
            if not dry_run and len(all_rows) >= merge_every_n_pages * API_LIMIT:
                log.info("  中间合并: %d 行", len(all_rows))
                _flush_and_merge(all_rows, symbol, dataset_version, settings, stats)
                all_rows = []

            time.sleep(rate_limit_sleep)

    stats["rows_fetched"] = stats.get("rows_fetched", 0) + len(all_rows)

    if dry_run:
        if all_rows:
            min_ts = min(r["ts"] for r in all_rows)
            max_ts = max(r["ts"] for r in all_rows)
            stats["new_min_ts"] = min_ts.isoformat()
            stats["new_max_ts"] = max_ts.isoformat()
        log.info(
            "[DRY RUN] [%s funding] 共 %d 页, %d 行, 范围 %s ~ %s",
            symbol,
            stats["pages_fetched"],
            len(all_rows),
            stats.get("new_min_ts", "N/A"),
            stats.get("new_max_ts", "N/A"),
        )
        return stats

    # 4. 最终 flush + merge
    if all_rows:
        _flush_and_merge(all_rows, symbol, dataset_version, settings, stats)

    log.info(
        "[%s funding] 回填完成: %d 页, staging=%d, bronze=%d, silver=%d",
        symbol,
        stats["pages_fetched"],
        stats["rows_written_staging"],
        stats["rows_merged_bronze"],
        stats["rows_merged_silver"],
    )
    return stats


def _flush_and_merge(
    rows: list[dict[str, Any]],
    symbol: str,
    dataset_version: str,
    settings: Any,
    stats: dict[str, Any],
) -> None:
    """将行写入 staging 并执行 funding merge pipeline."""
    from aats.data_platform.db import get_session
    from aats.data_platform.jobs.run_registry import (
        create_ingest_run,
        create_run_item,
        finish_ingest_run,
        finish_run_item,
    )
    from aats.data_platform.merge.merge_pipeline import run_funding_merge_pipeline

    # 去重
    rows = _dedupe_rows(rows)
    if not rows:
        return

    with get_session(settings) as session:
        run_id = create_ingest_run(
            session,
            run_type="backfill",
            dataset_domain="funding",
            instrument_type="swap",
            symbol=symbol.upper(),
            trigger_mode="manual",
        )
        item_id = create_run_item(
            session,
            ingest_run_id=run_id,
            dataset_domain="funding",
            instrument_type="swap",
            symbol=symbol.upper(),
        )

        try:
            written = _write_staging_batch(session, rows, run_id, dataset_version)
            session.flush()

            stats["rows_written_staging"] = stats.get("rows_written_staging", 0) + written

            min_ts = min(r["ts"] for r in rows)
            max_ts = max(r["ts"] for r in rows)
            if stats.get("new_min_ts") is None or min_ts.isoformat() < stats["new_min_ts"]:
                stats["new_min_ts"] = min_ts.isoformat()
            if stats.get("new_max_ts") is None or max_ts.isoformat() > stats["new_max_ts"]:
                stats["new_max_ts"] = max_ts.isoformat()

            merge_result = run_funding_merge_pipeline(
                session,
                symbol=symbol,
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


# ── CLI ────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="OKX API 深度历史 funding rate 回填",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 回填 365 天的 BTC-USDT-SWAP funding 数据
  python scripts/rdp_deep_backfill_funding.py --days 365

  # 回填到指定日期
  python scripts/rdp_deep_backfill_funding.py --target-start 2025-04-01

  # 多个交易对
  python scripts/rdp_deep_backfill_funding.py --symbols BTC-USDT-SWAP ETH-USDT-SWAP --days 365

  # 试运行
  python scripts/rdp_deep_backfill_funding.py --days 365 --dry-run
        """,
    )
    parser.add_argument(
        "--symbols", nargs="+", default=["BTC-USDT-SWAP"],
        help="交易对列表 (默认: BTC-USDT-SWAP)",
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
        "--rate-limit", type=float, default=0.15,
        help="API 请求间隔秒数 (默认: 0.15)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="试运行：只拉取数据统计，不写入数据库",
    )
    parser.add_argument(
        "--merge-every", type=int, default=30,
        help="每拉取多少页做一次 merge (默认: 30)",
    )

    args = parser.parse_args()

    if args.target_start:
        target_start = datetime.strptime(args.target_start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    elif args.days:
        target_start = datetime.now(timezone.utc) - timedelta(days=args.days)
    else:
        parser.error("必须指定 --days 或 --target-start")
        return

    log.info("=" * 60)
    log.info("OKX API 深度 funding 回填")
    log.info("  交易对  : %s", ", ".join(args.symbols))
    log.info("  目标起点: %s", target_start.strftime("%Y-%m-%d %H:%M UTC"))
    log.info("  请求间隔: %.2fs", args.rate_limit)
    log.info("  试运行  : %s", "是" if args.dry_run else "否")
    log.info("=" * 60)

    all_stats: list[dict[str, Any]] = []

    for symbol in args.symbols:
        log.info("")
        log.info("━" * 40)
        log.info("开始回填: %s", symbol)
        log.info("━" * 40)

        try:
            result = deep_backfill_funding(
                symbol=symbol,
                target_start=target_start,
                rate_limit_sleep=args.rate_limit,
                dry_run=args.dry_run,
                merge_every_n_pages=args.merge_every,
            )
            all_stats.append(result)
        except Exception as e:
            log.error("[%s] 回填失败: %s", symbol, e, exc_info=True)
            all_stats.append({"symbol": symbol, "error": str(e)})

    # 汇总报告
    log.info("")
    log.info("=" * 60)
    log.info("回填汇总")
    log.info("=" * 60)
    total_rows = 0
    for s in all_stats:
        if "error" in s:
            log.info("  %s: 失败 — %s", s["symbol"], s["error"])
        else:
            rows = s.get("rows_merged_silver", 0)
            total_rows += rows
            log.info(
                "  %s: %d 页, silver=%d 行, 范围 %s ~ %s%s",
                s["symbol"],
                s.get("pages_fetched", 0),
                rows,
                s.get("new_min_ts", "N/A"),
                s.get("new_max_ts", "N/A"),
                " (API耗尽)" if s.get("api_exhausted") else "",
            )
    log.info("  总计: %d 行", total_rows)


if __name__ == "__main__":
    main()
