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
log = logging.getLogger("rdp_deep_backfill_funding")


# ── OKX API 参数 ──────────────────────────────────────────────────────

API_LIMIT = 100  # OKX 每页最多 100 条

# Funding 的实际结算间隔可能随合约和时段变化，不能用固定 8 小时估算。
# 硬上限只限制请求/数据体量；实际覆盖范围由 fundingTime 决定。
MAX_PAGES_HARD_LIMIT = 200


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
) -> tuple[list[dict[str, Any]], bytes]:
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
    backoff = 1.0
    for attempt in range(6):
        try:
            resp = client.get(url, params=params, timeout=timeout)
            body = resp.json()
        except Exception as exc:
            if attempt == 5:
                raise RuntimeError(f"okx_funding_request_failed:{type(exc).__name__}") from exc
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            continue
        code = str(body.get("code", "")) if isinstance(body, dict) else ""
        if resp.status_code == 200 and code == "0":
            data = body.get("data")
            if not isinstance(data, list):
                raise RuntimeError("okx_funding_data_not_list")
            raw = (json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n").encode()
            return data, raw
        if resp.status_code == 429 or resp.status_code >= 500 or code == "50011":
            if attempt == 5:
                raise RuntimeError(
                    f"okx_funding_retry_exhausted:http={resp.status_code}:code={code}"
                )
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            continue
        raise RuntimeError(f"okx_funding_request_rejected:http={resp.status_code}:code={code}")
    raise AssertionError("unreachable")


def _split_page_result(value: Any) -> tuple[list[dict[str, Any]], bytes | None]:
    if isinstance(value, tuple) and len(value) == 2:
        rows, raw = value
        if not isinstance(rows, list) or not isinstance(raw, bytes):
            raise RuntimeError("okx_funding_page_result_invalid")
        return rows, raw
    if isinstance(value, list):
        return value, None
    raise RuntimeError("okx_funding_page_result_invalid")


def _parse_funding(item: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    """解析单条 OKX API funding 数据."""
    try:
        ts = datetime.fromtimestamp(int(item["fundingTime"]) / 1000, tz=timezone.utc)
        rate = Decimal(item["fundingRate"])
        realized_rate = (
            Decimal(item["realizedRate"]) if item.get("realizedRate") else None
        )
    except (InvalidOperation, KeyError, TypeError, ValueError, OSError):
        return None
    if not rate.is_finite() or (
        realized_rate is not None and not realized_rate.is_finite()
    ):
        return None
    raw_symbol = item.get("instId")
    if not isinstance(raw_symbol, str) or raw_symbol.upper() != symbol.upper():
        return None
    return {
        "symbol": symbol.upper(),
        "ts": ts,
        "funding_rate": rate,
        "inst_type": item.get("instType"),
        "formula_type": item.get("formulaType"),
        "method": item.get("method"),
        "realized_rate": realized_rate,
        "raw_symbol": raw_symbol,
        "raw_ts": item.get("fundingTime"),
    }


# ── 数据库操作 ─────────────────────────────────────────────────────────


def _query_existing_range(session: "Session", symbol: str) -> tuple[datetime | None, datetime | None]:
    """查询 silver.market_swap_funding 中已有数据的时间范围."""
    from sqlalchemy import text

    from aats.data_platform.models import funding_table_name

    table = funding_table_name("silver")
    row = session.execute(
        text(f"SELECT min(ts), max(ts) FROM {table} WHERE symbol = :sym"),
        {"sym": symbol.upper()},
    ).fetchone()
    if row and row[0] is not None:
        return row[0], row[1]
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
    raw_archive_dir: Path | None = None,
    refresh_existing: bool = False,
    refresh_end: datetime | None = None,
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
    if target_start.tzinfo is None or target_start.utcoffset() is None:
        raise ValueError("target_start must be timezone-aware")
    if refresh_existing:
        if refresh_end is None:
            raise ValueError("refresh_end is required when refresh_existing is enabled")
        if refresh_end.tzinfo is None or refresh_end.utcoffset() is None:
            raise ValueError("refresh_end must be timezone-aware")
        if refresh_end <= target_start:
            raise ValueError("refresh_end must be after target_start")
    if not dry_run:
        if raw_archive_dir is None:
            raise ValueError("raw_archive_dir is required for an applied backfill")
        if not raw_archive_dir.expanduser().is_absolute():
            raise ValueError("raw_archive_dir must be absolute")
    stats: dict[str, Any] = {
        "symbol": symbol,
        "target_start": target_start.isoformat(),
        "mode": "refresh_existing" if refresh_existing else "backfill_missing_history",
        "refresh_end": refresh_end.isoformat() if refresh_end is not None else None,
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
        "api_exhausted": False,
        "raw_partition_sha256": [],
        "gaps": [],
        "coverage_ratio": 0.0,
        "observed_settlement_intervals_seconds": [],
        "bundle": None,
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
    if not refresh_existing and existing_min and existing_min <= target_start:
        log.info(
            "[%s funding] 已有数据起点 %s 已早于目标 %s，无需回填",
            symbol,
            existing_min.strftime("%Y-%m-%d"),
            target_start.strftime("%Y-%m-%d"),
        )
        return stats

    # 2. 确定起始分页点
    if refresh_existing:
        assert refresh_end is not None
        page_from_ms = _ts_ms(refresh_end)
    elif existing_min:
        page_from_ms = _ts_ms(existing_min)
    else:
        page_from_ms = _ts_ms(datetime.now(timezone.utc))

    coverage_end = (
        refresh_end
        if refresh_existing
        else (existing_min or _ms_to_dt(page_from_ms))
    )
    if coverage_end <= target_start:
        return stats

    target_ms = _ts_ms(target_start)
    all_rows: list[dict[str, Any]] = []
    page = 0
    consecutive_empty = 0
    fetch_error: Exception | None = None
    seen_cursors: set[int] = set()
    observed: set[datetime] = set()
    oldest_source_ts: datetime | None = None
    completed = False

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
                raw_data, raw_response = _split_page_result(
                    _fetch_funding_page(
                        client,
                        base_url,
                        symbol,
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
                    f"funding_{token}_{page:06d}_{page_from_ms}.json",
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
                parsed = _parse_funding(item, symbol)
                if parsed is None:
                    fetch_error = RuntimeError("okx_funding_row_invalid")
                    break
                parsed_page.append(parsed)
            if fetch_error is not None:
                break

            for parsed in parsed_page:
                if target_start <= parsed["ts"] < coverage_end:
                    page_rows.append(parsed)
                    observed.add(parsed["ts"])

            all_rows.extend(page_rows)
            stats["rows_fetched"] += len(page_rows)

            # 获取本页最早时间戳
            oldest_dt = min(parsed["ts"] for parsed in parsed_page)
            oldest_ts_in_page = _ts_ms(oldest_dt)
            oldest_source_ts = (
                oldest_dt if oldest_source_ts is None else min(oldest_source_ts, oldest_dt)
            )

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
                completed = True
                break

            # 检查返回数据不足一页（已到 API 数据尽头）
            if len(raw_data) < API_LIMIT:
                log.info(
                    "[%s funding] API 返回不足一页 (%d/%d)，数据到底 (page=%d, 最早 %s)",
                    symbol, len(raw_data), API_LIMIT, page,
                    oldest_dt.strftime("%Y-%m-%d %H:%M"),
                )
                stats["api_exhausted"] = True
                completed = True
                break

            # 下一页的起始点
            next_cursor = oldest_ts_in_page - 1
            if next_cursor >= page_from_ms or next_cursor in seen_cursors:
                fetch_error = RuntimeError("okx_funding_pagination_stalled")
                break
            seen_cursors.add(page_from_ms)
            page_from_ms = next_cursor

            # 分批合并（避免内存积累过大）
            if not dry_run and len(all_rows) >= merge_every_n_pages * API_LIMIT:
                log.info("  中间合并: %d 行", len(all_rows))
                _flush_and_merge(all_rows, symbol, dataset_version, settings, stats)
                all_rows = []

            time.sleep(rate_limit_sleep)

    if not completed and fetch_error is None:
        fetch_error = RuntimeError("okx_funding_max_pages_exceeded")

    ordered_observed = sorted(observed)
    stats["unique_rows_observed"] = len(observed)
    stats["observed_settlement_intervals_seconds"] = sorted(
        {
            int((current - previous).total_seconds())
            for previous, current in zip(ordered_observed, ordered_observed[1:])
            if current > previous
        }
    )
    reached_target = oldest_source_ts is not None and oldest_source_ts <= target_start
    if not reached_target:
        shortfall_end = min(ordered_observed) if ordered_observed else coverage_end
        if shortfall_end > target_start:
            stats["gaps"] = [
                {
                    "gap_start": target_start.isoformat(),
                    "gap_end": shortfall_end.isoformat(),
                    "reason": "official_funding_history_coverage_shortfall",
                }
            ]
    stats["coverage_ratio"] = 1.0 if reached_target and observed else 0.0

    if dry_run:
        if fetch_error is not None:
            raise RuntimeError("funding_backfill_dry_run_failed") from fetch_error
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

    if fetch_error is not None:
        raise RuntimeError("funding_backfill_partial_failure_checkpointed") from fetch_error

    if stats["pages_fetched"] and not stats["raw_partition_sha256"]:
        raise RuntimeError("funding_backfill_raw_archive_empty")

    if stats["raw_partition_sha256"]:
        stats["bundle"] = _persist_funding_bundle(
            settings=settings,
            symbol=symbol,
            coverage_start=target_start,
            coverage_end=coverage_end,
            dataset_version=dataset_version,
            row_count=stats["unique_rows_observed"],
            raw_hashes=stats["raw_partition_sha256"],
            gaps=stats["gaps"],
            coverage_ratio=stats["coverage_ratio"],
        )

    log.info(
        "[%s funding] 回填完成: %d 页, staging=%d, bronze=%d, silver=%d",
        symbol,
        stats["pages_fetched"],
        stats["rows_written_staging"],
        stats["rows_merged_bronze"],
        stats["rows_merged_silver"],
    )
    return stats


def _persist_funding_bundle(
    *,
    settings: Any,
    symbol: str,
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
    source_key = "okx-rest:funding-rate-history:v5"
    with get_session(settings) as session:
        source_id = register_official_source(
            session,
            source_key=source_key,
            source_kind="okx_rest",
            source_locator="/api/v5/public/funding-rate-history",
            timestamp_semantics="exchange funding settlement time in milliseconds",
        )
        source = import_source_record(
            source_key=source_key,
            source_kind="okx_rest",
            provider="OKX",
            source_locator="/api/v5/public/funding-rate-history",
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            timestamp_semantics="exchange funding settlement time in milliseconds",
            schema_version="okx-v5",
            dataset_version=dataset_version,
            transform_version="funding-settlement-backfill-v2",
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
                    dataset_name="silver.market_swap_funding",
                    symbol=symbol.upper(),
                    channel="funding-rate-history",
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
            role="funding",
            purpose="funding_research",
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
        with get_session(settings) as session:
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


# ── CLI ────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
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
        "--refresh-existing",
        action="store_true",
        help="覆盖刷新已存在窗口，而不是只向已有最早时间之前回填",
    )
    parser.add_argument(
        "--refresh-end",
        type=str,
        help="覆盖刷新排他结束日期 (YYYY-MM-DD)，仅与 --refresh-existing 同用",
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
    parser.add_argument(
        "--raw-archive-dir",
        type=Path,
        help="原始 OKX 响应不可变归档的绝对目录；非 dry-run 必填",
    )

    args = parser.parse_args(argv)

    if args.target_start:
        target_start = datetime.strptime(args.target_start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    elif args.days:
        target_start = datetime.now(timezone.utc) - timedelta(days=args.days)
    else:
        parser.error("必须指定 --days 或 --target-start")
        return 2
    refresh_end = (
        datetime.strptime(args.refresh_end, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
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
    log.info("OKX API 深度 funding 回填")
    log.info("  交易对  : %s", ", ".join(args.symbols))
    log.info("  目标起点: %s", target_start.strftime("%Y-%m-%d %H:%M UTC"))
    log.info("  执行模式: %s", "覆盖刷新" if args.refresh_existing else "缺失历史回填")
    if refresh_end is not None:
        log.info("  刷新终点: %s (exclusive)", refresh_end.strftime("%Y-%m-%d %H:%M UTC"))
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
                raw_archive_dir=args.raw_archive_dir,
                refresh_existing=args.refresh_existing,
                refresh_end=refresh_end,
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
    return 1 if any("error" in item for item in all_stats) else 0


if __name__ == "__main__":
    raise SystemExit(main())
