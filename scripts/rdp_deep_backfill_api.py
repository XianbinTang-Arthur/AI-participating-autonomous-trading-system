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
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
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
        open_px = Decimal(item[1])
        high_px = Decimal(item[2])
        low_px = Decimal(item[3])
        close_px = Decimal(item[4])
        vol = Decimal(item[5]) if item[5] else None
        vol_ccy = Decimal(item[6]) if item[6] else None
        vol_quote = Decimal(item[7]) if item[7] else None
    except (InvalidOperation, TypeError, ValueError, OSError):
        return None

    prices = (open_px, high_px, low_px, close_px)
    volumes = (vol, vol_ccy, vol_quote)
    if any(not value.is_finite() or value <= 0 for value in prices):
        return None
    if high_px < max(open_px, low_px, close_px) or low_px > min(
        open_px, high_px, close_px
    ):
        return None
    if any(value is not None and (not value.is_finite() or value < 0) for value in volumes):
        return None

    confirmation = str(item[8]).strip().lower()
    if confirmation not in {"0", "1", "false", "true"}:
        return None
    return {
        "symbol": symbol.upper(),
        "ts": ts,
        "open": open_px,
        "high": high_px,
        "low": low_px,
        "close": close_px,
        "vol": vol,
        "vol_ccy": vol_ccy,
        "vol_quote": vol_quote,
        "confirm": confirmation in {"1", "true"},
        "raw_symbol": symbol,
        "raw_ts": item[0],
    }


def _fetch_candles_page(
    client: "httpx.Client",
    base_url: str,
    symbol: str,
    timeframe: str,
    after_ms: int | None = None,
    timeout: float = 15.0,
) -> tuple[list[list[str]], bytes]:
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
    backoff = 1.0
    for attempt in range(6):
        try:
            resp = client.get(url, params=params, timeout=timeout)
            body = resp.json()
        except Exception as exc:
            if attempt == 5:
                raise RuntimeError(f"okx_candle_request_failed:{type(exc).__name__}") from exc
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            continue
        code = str(body.get("code", "")) if isinstance(body, dict) else ""
        if resp.status_code == 200 and code == "0":
            data = body.get("data")
            if not isinstance(data, list):
                raise RuntimeError("okx_candle_data_not_list")
            raw = (json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n").encode()
            return data, raw
        if resp.status_code == 429 or resp.status_code >= 500 or code == "50011":
            if attempt == 5:
                raise RuntimeError(
                    f"okx_candle_retry_exhausted:http={resp.status_code}:code={code}"
                )
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            continue
        raise RuntimeError(f"okx_candle_request_rejected:http={resp.status_code}:code={code}")
    raise AssertionError("unreachable")


def _split_page_result(value: Any) -> tuple[list[list[str]], bytes | None]:
    """Accept the historic list-only seam while the real client returns evidence bytes."""

    if isinstance(value, tuple) and len(value) == 2:
        rows, raw = value
        if not isinstance(rows, list) or not isinstance(raw, bytes):
            raise RuntimeError("okx_candle_page_result_invalid")
        return rows, raw
    if isinstance(value, list):
        return value, None
    raise RuntimeError("okx_candle_page_result_invalid")


def _aligned_floor(value: datetime, delta: timedelta) -> datetime:
    seconds = int(delta.total_seconds())
    epoch = int(value.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=timezone.utc)


def _aligned_ceil(value: datetime, delta: timedelta) -> datetime:
    floor = _aligned_floor(value, delta)
    return floor if floor == value else floor + delta


def _candle_gap_ranges(
    observed: set[datetime],
    *,
    start: datetime,
    end: datetime,
    delta: timedelta,
) -> list[dict[str, str]]:
    """Compress missing confirmed candle opens into half-open UTC ranges."""

    gaps: list[dict[str, str]] = []
    cursor = _aligned_ceil(start, delta)
    gap_start: datetime | None = None
    while cursor < end:
        if cursor not in observed and gap_start is None:
            gap_start = cursor
        elif cursor in observed and gap_start is not None:
            gaps.append(
                {
                    "gap_start": gap_start.isoformat(),
                    "gap_end": cursor.isoformat(),
                    "reason": "confirmed_candle_missing",
                }
            )
            gap_start = None
        cursor += delta
    if gap_start is not None:
        gaps.append(
            {
                "gap_start": gap_start.isoformat(),
                "gap_end": end.isoformat(),
                "reason": "confirmed_candle_missing",
            }
        )
    return gaps


def _query_existing_range(
    session: "Session", symbol: str, timeframe: str
) -> tuple[datetime | None, datetime | None]:
    """查询数据库中 silver 层已有数据的时间范围."""
    from sqlalchemy import text

    from aats.data_platform.models import candle_table_name

    table = candle_table_name("silver", symbol, timeframe)
    row = session.execute(
        text(f"SELECT min(ts), max(ts) FROM {table} WHERE symbol = :sym"),
        {"sym": symbol.upper()},
    ).fetchone()
    if row and row[0] is not None:
        return row[0], row[1]
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
    raw_archive_dir: Path | None = None,
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
    if not dry_run:
        if raw_archive_dir is None:
            raise ValueError("raw_archive_dir is required for an applied backfill")
        if not raw_archive_dir.expanduser().is_absolute():
            raise ValueError("raw_archive_dir must be absolute")
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
        "unique_rows_observed": 0,
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
        "raw_partition_sha256": [],
        "gaps": [],
        "coverage_ratio": 0.0,
        "bundle": None,
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

    delta = _TF_DELTA[timeframe]
    coverage_end = (
        refresh_end
        if refresh_existing
        else existing_min or _aligned_floor(_ms_to_dt(page_from_ms), delta)
    )
    coverage_start = _aligned_ceil(target_start, delta)
    if coverage_end <= coverage_start:
        raise ValueError("requested candle coverage window is empty")

    target_ms = _ts_ms(target_start)
    all_rows: list[dict[str, Any]] = []
    page = 0
    consecutive_empty = 0
    seen_cursors: set[int] = set()
    observed: set[datetime] = set()
    fetch_error: Exception | None = None
    completed = False

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
            if page_from_ms in seen_cursors:
                fetch_error = RuntimeError("okx_candle_pagination_stalled")
                break
            seen_cursors.add(page_from_ms)
            try:
                raw_data, raw_response = _split_page_result(
                    _fetch_candles_page(
                        client,
                        base_url,
                        symbol,
                        timeframe,
                        after_ms=page_from_ms,
                        timeout=timeout,
                    )
                )
            except Exception as e:
                log.error("API 请求失败 (page=%d): %s", page, e)
                if page == 0:
                    raise
                log.warning("停止拉取并保存检查点；本次操作最终仍会失败关闭")
                fetch_error = e
                break

            page += 1
            stats["pages_fetched"] = page
            if raw_response is not None and raw_archive_dir is not None:
                from aats.data_platform.collectors.backfill.official_history_importers import (
                    archive_raw_response_page,
                )

                token = "".join(ch if ch.isalnum() else "_" for ch in symbol.upper())
                digest = archive_raw_response_page(
                    raw_archive_dir,
                    f"candle_{token}_{timeframe}_{page:06d}_{page_from_ms}.json",
                    raw_response,
                )
                stats["raw_partition_sha256"].append(digest)
            elif not dry_run:
                raise RuntimeError("raw_response_evidence_missing")

            if not raw_data:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    log.info("连续 %d 页无数据，API 数据已耗尽", consecutive_empty)
                    stats["api_exhausted"] = True
                    completed = True
                    break
                time.sleep(rate_limit_sleep)
                continue

            consecutive_empty = 0

            # 解析数据
            page_rows = []
            parsed_page: list[dict[str, Any]] = []
            for item in raw_data:
                parsed = _parse_api_candle(item, symbol)
                if parsed is None:
                    fetch_error = RuntimeError("okx_candle_row_invalid")
                    break
                parsed_page.append(parsed)
            if fetch_error is not None:
                break

            for parsed in parsed_page:
                # Historical repair/backfill must never promote an open candle.
                # The rolling collector owns provisional current-bar updates.
                if (
                    parsed["confirm"]
                    and coverage_start <= parsed["ts"] < coverage_end
                ):
                    page_rows.append(parsed)
                    observed.add(parsed["ts"])

            all_rows.extend(page_rows)
            stats["rows_fetched"] += len(page_rows)

            # 获取本页最早时间戳
            oldest_dt = min(parsed["ts"] for parsed in parsed_page)
            oldest_ts_in_page = _ts_ms(oldest_dt)

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
                completed = True
                break

            # 检查返回数据不足一页（已到 API 数据尽头）
            if len(raw_data) < API_LIMIT:
                log.info(
                    "[%s %s] API 返回不足一页 (%d/%d)，数据到底 (page=%d, 最早 %s)",
                    symbol, timeframe, len(raw_data), API_LIMIT, page,
                    oldest_dt.strftime("%Y-%m-%d %H:%M"),
                )
                stats["api_exhausted"] = True
                completed = True
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

    if not completed and fetch_error is None:
        fetch_error = RuntimeError("okx_candle_max_pages_exceeded")

    expected_samples = int((coverage_end - coverage_start) / delta)
    stats["unique_rows_observed"] = len(observed)
    stats["gaps"] = _candle_gap_ranges(
        observed,
        start=coverage_start,
        end=coverage_end,
        delta=delta,
    )
    stats["coverage_ratio"] = (
        min(1.0, len(observed) / expected_samples) if expected_samples else 0.0
    )

    if dry_run:
        if fetch_error is not None:
            raise RuntimeError("candle_backfill_dry_run_failed") from fetch_error
        # 仅报告统计
        if all_rows:
            min_ts = min(r["ts"] for r in all_rows)
            max_ts = max(r["ts"] for r in all_rows)
            stats["new_min_ts"] = min_ts.isoformat()
            stats["new_max_ts"] = max_ts.isoformat()
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

    if fetch_error is not None:
        raise RuntimeError("candle_backfill_partial_failure_checkpointed") from fetch_error

    if stats["pages_fetched"] and not stats["raw_partition_sha256"]:
        raise RuntimeError("candle_backfill_raw_archive_empty")

    if stats["raw_partition_sha256"]:
        stats["bundle"] = _persist_candle_bundle(
            settings=settings,
            symbol=symbol,
            timeframe=timeframe,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            dataset_version=dataset_version,
            row_count=stats["unique_rows_observed"],
            raw_hashes=stats["raw_partition_sha256"],
            gaps=stats["gaps"],
            coverage_ratio=stats["coverage_ratio"],
        )

    # 5. 重建 Gold
    if build_gold and stats["rows_merged_silver"] > 0:
        log.info("[%s %s] 开始重建 Gold replay bars...", symbol, timeframe)
        try:
            _rebuild_gold(symbol, timeframe, settings)
            stats["gold_built"] = True
            log.info("[%s %s] Gold 重建完成", symbol, timeframe)
        except Exception as e:
            log.error("[%s %s] Gold 重建失败: %s", symbol, timeframe, e)
            raise RuntimeError("candle_gold_rebuild_failed") from e

    log.info(
        "[%s %s] 回填完成: %d 页, staging=%d, bronze=%d, silver=%d",
        symbol, timeframe,
        stats["pages_fetched"],
        stats["rows_written_staging"],
        stats["rows_merged_bronze"],
        stats["rows_merged_silver"],
    )
    return stats


def _persist_candle_bundle(
    *,
    settings: Any,
    symbol: str,
    timeframe: str,
    coverage_start: datetime,
    coverage_end: datetime,
    dataset_version: str,
    row_count: int,
    raw_hashes: list[str],
    gaps: list[dict[str, str]],
    coverage_ratio: float,
) -> dict[str, Any]:
    from aats.data_platform.collectors.backfill.official_history_importers import (
        register_official_source,
    )
    from aats.data_platform.data_governance.coverage import git_commit
    from aats.data_platform.data_governance.gaps import official_backfill_gap, record_data_gaps
    from aats.data_platform.data_governance.registry import (
        import_source_record,
        persist_historical_bundle,
    )
    from aats.data_platform.db import get_session

    root = Path(__file__).resolve().parent.parent
    source_key = f"okx-rest:history-candles:{timeframe}:v5"
    with get_session(settings) as session:
        source_id = register_official_source(
            session,
            source_key=source_key,
            source_kind="okx_rest",
            source_locator="/api/v5/market/history-candles",
            timestamp_semantics="confirmed candle opening time in milliseconds",
        )
        source = import_source_record(
            source_key=source_key,
            source_kind="okx_rest",
            provider="OKX",
            source_locator="/api/v5/market/history-candles",
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            timestamp_semantics="confirmed candle opening time in milliseconds",
            schema_version="okx-v5",
            dataset_version=dataset_version,
            transform_version="confirmed-candle-backfill-v2",
            git_commit=git_commit(str(root)),
            raw_partition_sha256=raw_hashes,
            row_count=row_count,
            gaps=gaps,
        )
        record_data_gaps(
            session,
            [
                official_backfill_gap(
                    source_id=source_id,
                    dataset_name=f"silver.candles_{timeframe}",
                    symbol=symbol.upper(),
                    channel=f"history-candles:{timeframe}",
                    gap_start=datetime.fromisoformat(item["gap_start"]),
                    gap_end=datetime.fromisoformat(item["gap_end"]),
                    reason_code=item["reason"],
                    evidence=item,
                )
                for item in gaps
            ],
        )
        bundle_id, report = persist_historical_bundle(
            session,
            source_id=source_id,
            source=source,
            symbol=symbol.upper(),
            role="candles",
            purpose="ohlcv_research",
            coverage_ratio=coverage_ratio,
            causal_time_check=True,
        )
    return {
        "bundle_id": bundle_id,
        "eligible": report.eligible,
        "reason_codes": list(report.reason_codes),
        "evidence_fingerprint": report.evidence_fingerprint,
    }


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
        with get_session(settings) as session:
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
        with get_session(settings) as failure_session:
            finish_run_item(
                failure_session,
                item_id,
                status="failed",
                error_message=type(exc).__name__,
            )
            finish_ingest_run(
                failure_session,
                run_id,
                status="failed",
                error_message=type(exc).__name__,
            )
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
    parser.add_argument(
        "--raw-archive-dir",
        type=Path,
        help="原始 OKX 响应不可变归档的绝对目录；非 dry-run 必填",
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
    if not args.dry_run and args.raw_archive_dir is None:
        parser.error("非 dry-run 必须指定 --raw-archive-dir")
    if args.raw_archive_dir is not None and not args.raw_archive_dir.expanduser().is_absolute():
        parser.error("--raw-archive-dir 必须是绝对路径")

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
                raw_archive_dir=args.raw_archive_dir,
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
