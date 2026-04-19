"""OKX REST 历史数据 backfill collectors — P1-D Stage 5.

参考: docs/design/p1d_okx_historical_backfill_plan_2026_04_20.md §3

本模块实现 3 个 backfill collector, 从 OKX REST endpoint 分页拉取历史数据
并写入 Bronze 表. 所有 collector 都:
  - 异步语义 (但走同步 httpx.Client; collector 本身同步, 外层 CLI 可按需
    多线程编排. 单 IP rate limit 决定并发无意义)
  - 严守 20 req / 2s IP rate limit (默认 0.15s 间隔 ≈ 6.7 req/s)
  - 支持 `--dry-run` 计算预估而不发请求
  - INSERT ... ON CONFLICT (PK) DO NOTHING 幂等
  - 用 `aats.data_platform.jobs.run_registry.create_ingest_run` 记
    ingest_run (dataset_domain='microstructure', trigger_mode='manual')
  - 用 `aats.data_platform.jobs.checkpoint_manager.upsert_checkpoint` 记
    进度, 支持 resume
  - 每 N rows 打 progress 日志

3 个 endpoint:
  1. /api/v5/rubik/stat/contracts/open-interest-history (period=1H)
     → bronze.market_oi_history_1h
  2. /api/v5/market/mark-price-candles-history (period=1m)
     → bronze.market_mark_price_candles_1m
  3. /api/v5/rubik/stat/contracts/long-short-account-ratio (period=5m)
     → bronze.market_long_short_ratio_5m

参数名约定 (OKX v5):
  - OI history: instId, period, begin, end, limit (ts_ms based)
  - Mark candles: instId, bar, after, before, limit (ts_ms based, after=老于)
  - LS ratio: ccy, period, begin, end, limit (ts_ms based)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    import httpx
    from sqlalchemy.orm import Session


log = logging.getLogger("okx_rest_history_collectors")


# ─────────────────────────────────────────────────────────────────────
# 公用常量 & helpers
# ─────────────────────────────────────────────────────────────────────

API_LIMIT = 100  # OKX v5 public endpoints 最大返回条数
MAX_PAGES_HARD_LIMIT = 500  # 防御性: 500 × 100 = 50K rows / call, 足够

# 默认 rate limit: 0.15s/req = 6.7 req/s, 严守 20 req/2s = 10 req/s 上限
DEFAULT_RATE_LIMIT_SLEEP = 0.15

# 429 backoff 参数
BACKOFF_INITIAL = 1.0
BACKOFF_CAP = 30.0
BACKOFF_MULT = 2.0


def _ts_ms(dt: datetime) -> int:
    """UTC datetime → Unix ms."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _ms_to_dt(ms: int) -> datetime:
    """Unix ms → UTC datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _to_decimal_or_none(s: Any) -> Decimal | None:
    """安全 Decimal 转换."""
    if s is None or s == "":
        return None
    try:
        return Decimal(str(s))
    except (InvalidOperation, ValueError, TypeError):
        return None


@dataclass
class BackfillStats:
    """单个 collector 跑一次的统计."""
    endpoint: str
    symbol: str
    period: str
    target_start: datetime
    target_end: datetime
    pages_fetched: int = 0
    rows_fetched: int = 0
    rows_written: int = 0
    rows_skipped_conflict: int = 0
    earliest_ts: datetime | None = None
    latest_ts: datetime | None = None
    api_exhausted: bool = False
    rate_limit_hits: int = 0
    errors: list[str] = field(default_factory=list)
    elapsed_sec: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "symbol": self.symbol,
            "period": self.period,
            "target_start": self.target_start.isoformat(),
            "target_end": self.target_end.isoformat(),
            "pages_fetched": self.pages_fetched,
            "rows_fetched": self.rows_fetched,
            "rows_written": self.rows_written,
            "rows_skipped_conflict": self.rows_skipped_conflict,
            "earliest_ts": self.earliest_ts.isoformat() if self.earliest_ts else None,
            "latest_ts": self.latest_ts.isoformat() if self.latest_ts else None,
            "api_exhausted": self.api_exhausted,
            "rate_limit_hits": self.rate_limit_hits,
            "errors": self.errors,
            "elapsed_sec": round(self.elapsed_sec, 2),
        }


# ─────────────────────────────────────────────────────────────────────
# Pagination 通用骨架
# ─────────────────────────────────────────────────────────────────────


def _paged_request(
    client: "httpx.Client",
    base_url: str,
    path: str,
    params: dict[str, str],
    *,
    cursor_key: str,
    cursor_value: int,
    target_earliest_ms: int,
    parse_row: Callable[[list[str] | dict[str, Any]], dict[str, Any] | None],
    oldest_ts_from_row: Callable[[list[str] | dict[str, Any]], int],
    stats: BackfillStats,
    timeout: float,
    rate_limit_sleep: float,
    max_pages: int,
) -> list[dict[str, Any]]:
    """通用分页循环: 从 cursor_value 向更早方向翻, 直到 target_earliest_ms 或 max_pages.

    - 所有 OKX v5 endpoint 都返回按 ts 降序排列 (最新在前).
    - 我们通过 `cursor_key` (通常 'end' 或 'after') 设置 "上一页最老 ts - 1" 实现翻页.
    - 单页可能返回 < API_LIMIT 条, 表示接近 API 历史尾端.

    Returns: list of parsed row dicts (去重前, 上层再去重).
    """
    import httpx  # noqa: F401

    all_rows: list[dict[str, Any]] = []
    consecutive_empty = 0

    for page in range(max_pages):
        # 设置 cursor 参数
        params_page = dict(params)
        params_page[cursor_key] = str(cursor_value)

        url = f"{base_url}{path}"
        backoff = BACKOFF_INITIAL
        got_response = False
        retry_count = 0
        max_retries = 5

        while not got_response and retry_count < max_retries:
            try:
                resp = client.get(url, params=params_page, timeout=timeout)
            except Exception as exc:
                log.warning("[%s p=%d] 网络错误 %s, backoff %.1fs",
                            path, page, type(exc).__name__, backoff)
                stats.errors.append(f"page={page}: {type(exc).__name__}: {exc}")
                time.sleep(backoff)
                backoff = min(BACKOFF_CAP, backoff * BACKOFF_MULT)
                retry_count += 1
                continue

            # 429 或 50011 (OKX 的 rate limit 错误 code)
            if resp.status_code == 429:
                stats.rate_limit_hits += 1
                log.warning("[%s p=%d] 429 rate limit, backoff %.1fs",
                            path, page, backoff)
                time.sleep(backoff)
                backoff = min(BACKOFF_CAP, backoff * BACKOFF_MULT)
                retry_count += 1
                continue

            try:
                body = resp.json()
            except Exception as exc:
                log.warning("[%s p=%d] non-JSON response (status=%d): %s",
                            path, page, resp.status_code, exc)
                stats.errors.append(f"page={page}: non-JSON response status={resp.status_code}")
                time.sleep(backoff)
                backoff = min(BACKOFF_CAP, backoff * BACKOFF_MULT)
                retry_count += 1
                continue

            if resp.status_code != 200:
                code = body.get("code", "?")
                msg = body.get("msg", str(body)[:200])
                if code == "50011":  # OKX rate limit
                    stats.rate_limit_hits += 1
                    log.warning("[%s p=%d] OKX 50011 rate limit, backoff %.1fs",
                                path, page, backoff)
                    time.sleep(backoff)
                    backoff = min(BACKOFF_CAP, backoff * BACKOFF_MULT)
                    retry_count += 1
                    continue
                # 其他 4xx/5xx
                stats.errors.append(
                    f"page={page} http={resp.status_code} code={code} msg={msg}"
                )
                log.error("[%s p=%d] HTTP %d code=%s msg=%s",
                          path, page, resp.status_code, code, msg)
                if resp.status_code >= 500:
                    time.sleep(backoff)
                    backoff = min(BACKOFF_CAP, backoff * BACKOFF_MULT)
                    retry_count += 1
                    continue
                # 4xx non-rate-limit: 停止
                got_response = True
                return all_rows

            if body.get("code") != "0":
                stats.errors.append(
                    f"page={page} okx_code={body.get('code')} msg={body.get('msg')}"
                )
                log.error("[%s p=%d] OKX code=%s msg=%s",
                          path, page, body.get("code"), body.get("msg"))
                return all_rows

            got_response = True
            data = body.get("data", [])

        if not got_response:
            stats.errors.append(f"page={page} retries exhausted")
            log.error("[%s p=%d] 重试耗尽, 停止分页", path, page)
            return all_rows

        stats.pages_fetched = page + 1

        if not data:
            consecutive_empty += 1
            if consecutive_empty >= 3:
                log.info("[%s p=%d] 连续 %d 页无数据, API 耗尽",
                         path, page, consecutive_empty)
                stats.api_exhausted = True
                break
            time.sleep(rate_limit_sleep)
            continue
        consecutive_empty = 0

        # 解析
        parsed = []
        for item in data:
            row = parse_row(item)
            if row:
                parsed.append(row)
        all_rows.extend(parsed)
        stats.rows_fetched += len(parsed)

        # 计算本页最老 ts
        try:
            oldest_ms_in_page = min(oldest_ts_from_row(item) for item in data)
        except Exception as exc:
            stats.errors.append(f"page={page} oldest_ts extraction failed: {exc}")
            log.error("[%s p=%d] 无法提取最老 ts: %s", path, page, exc)
            break

        oldest_dt_in_page = _ms_to_dt(oldest_ms_in_page)
        if stats.earliest_ts is None or oldest_dt_in_page < stats.earliest_ts:
            stats.earliest_ts = oldest_dt_in_page

        if page % 10 == 0 or page < 3:
            log.info("[%s p=%d] +%d rows, 最老 %s, total=%d",
                     path, page, len(parsed),
                     oldest_dt_in_page.strftime("%Y-%m-%d %H:%M"),
                     len(all_rows))

        # 已达目标?
        if oldest_ms_in_page <= target_earliest_ms:
            log.info("[%s p=%d] 已到目标时间 %s", path, page,
                     _ms_to_dt(target_earliest_ms).strftime("%Y-%m-%d %H:%M"))
            break

        # API 返回不足一页 → 到底
        if len(data) < API_LIMIT:
            log.info("[%s p=%d] 返回 %d < %d, API 数据到底",
                     path, page, len(data), API_LIMIT)
            stats.api_exhausted = True
            break

        cursor_value = oldest_ms_in_page - 1  # 下次拿更老的
        time.sleep(rate_limit_sleep)

    return all_rows


def _dedupe_by_ts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 (symbol, ts) 去重, 保留第一次出现."""
    seen = set()
    out = []
    for r in rows:
        key = (r["symbol"], r["ts"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


# ─────────────────────────────────────────────────────────────────────
# 1. OI history (open-interest-history)
# ─────────────────────────────────────────────────────────────────────


OI_HISTORY_PATH = "/api/v5/rubik/stat/contracts/open-interest-history"


def _parse_oi_history_row(item: list[str]) -> dict[str, Any] | None:
    """OKX OI history 返回 schema (§13):
        [ts_ms, oi, oiCcy]  (3 元素)
    或 (部分文档版本):
        [ts_ms, oi, oiCcy, oiUsd]  (4 元素)
    """
    if not isinstance(item, list) or len(item) < 2:
        return None
    try:
        ts = _ms_to_dt(int(item[0]))
    except (ValueError, OSError, TypeError):
        return None
    oi = _to_decimal_or_none(item[1])
    if oi is None:
        return None
    oi_ccy = _to_decimal_or_none(item[2]) if len(item) > 2 else None
    oi_usd = _to_decimal_or_none(item[3]) if len(item) > 3 else None
    return {
        "ts": ts,
        "oi": oi,
        "oi_ccy": oi_ccy,
        "oi_usd": oi_usd,
    }


def _oldest_oi_ts_ms(item: list[str]) -> int:
    return int(item[0])


def estimate_oi_history_requests(target_days: int, period: str = "1H") -> dict[str, Any]:
    """预估下载 OI history 的请求数 + 行数 (dry-run 用).

    period 至 `_TF_MINUTES` 的映射决定每天行数.
    """
    tf_min = {"5m": 5, "15m": 15, "30m": 30, "1H": 60, "2H": 120, "4H": 240}.get(period)
    if tf_min is None:
        raise ValueError(f"unsupported period: {period}")
    rows_per_day = 1440 / tf_min
    total_rows = int(target_days * rows_per_day)
    pages = (total_rows + API_LIMIT - 1) // API_LIMIT
    return {
        "endpoint": OI_HISTORY_PATH,
        "period": period,
        "target_days": target_days,
        "rows_per_day": rows_per_day,
        "estimated_rows": total_rows,
        "estimated_pages": pages,
        "estimated_seconds_at_default_rate": pages * DEFAULT_RATE_LIMIT_SLEEP,
    }


def collect_oi_history(
    session: "Session",
    *,
    symbol: str,
    target_days: int = 90,
    period: str = "1H",
    base_url: str = "https://www.okx.com",
    rate_limit_sleep: float = DEFAULT_RATE_LIMIT_SLEEP,
    timeout: float = 15.0,
    max_pages: int = MAX_PAGES_HARD_LIMIT,
    dry_run: bool = False,
    ingest_run_id: str | None = None,
) -> BackfillStats:
    """回填 OKX `open-interest-history` 到 bronze.market_oi_history_1h.

    Parameters
    ----------
    session : SQLAlchemy session (must be able to insert into bronze schema)
    symbol : "BTC-USDT-SWAP" etc
    target_days : 从现在往前回填多少天
    period : OKX `period` param ('5m'/'15m'/'30m'/'1H'/'2H'/'4H')
    dry_run : 只预估不请求
    ingest_run_id : caller 已创建 run 则传入; 否则返回 stats 不落库
    """
    import httpx

    from aats.data_platform.jobs.checkpoint_manager import upsert_checkpoint
    from aats.data_platform.models import instrument_type_for_symbol, utc_now

    t0 = time.monotonic()
    end_ts = utc_now()
    start_ts = end_ts - timedelta(days=target_days)
    stats = BackfillStats(
        endpoint=OI_HISTORY_PATH,
        symbol=symbol,
        period=period,
        target_start=start_ts,
        target_end=end_ts,
    )

    if dry_run:
        est = estimate_oi_history_requests(target_days, period)
        stats.rows_fetched = est["estimated_rows"]
        stats.pages_fetched = est["estimated_pages"]
        stats.elapsed_sec = est["estimated_seconds_at_default_rate"]
        log.info("[DRY-RUN OI] %s period=%s days=%d → pages≈%d rows≈%d time≈%.1fs",
                 symbol, period, target_days,
                 est["estimated_pages"], est["estimated_rows"],
                 est["estimated_seconds_at_default_rate"])
        return stats

    target_earliest_ms = _ts_ms(start_ts)
    cursor_end_ms = _ts_ms(end_ts)

    with httpx.Client() as client:
        rows = _paged_request(
            client,
            base_url,
            OI_HISTORY_PATH,
            params={
                "instId": symbol,
                "period": period,
                "limit": str(API_LIMIT),
            },
            cursor_key="end",
            cursor_value=cursor_end_ms,
            target_earliest_ms=target_earliest_ms,
            parse_row=_parse_oi_history_row,
            oldest_ts_from_row=_oldest_oi_ts_ms,
            stats=stats,
            timeout=timeout,
            rate_limit_sleep=rate_limit_sleep,
            max_pages=max_pages,
        )

    # 去重并加 symbol + ingest_run_id
    for r in rows:
        r["symbol"] = symbol
    rows = _dedupe_by_ts(rows)

    if rows:
        stats.earliest_ts = min(r["ts"] for r in rows)
        stats.latest_ts = max(r["ts"] for r in rows)

    # 写入 bronze
    if rows and ingest_run_id is not None:
        written = _write_bronze_oi_history(session, rows, ingest_run_id)
        stats.rows_written = written
        stats.rows_skipped_conflict = len(rows) - written

        # advance checkpoint (optional — microstructure domain 只是 advisory)
        try:
            upsert_checkpoint(
                session,
                dataset_domain="microstructure",
                instrument_type=instrument_type_for_symbol(symbol),
                symbol=symbol,
                timeframe="oi_1h",
                last_successful_ts=stats.latest_ts,
                last_ingest_run_id=ingest_run_id,
            )
            session.commit()
        except Exception as exc:
            log.warning("checkpoint upsert 失败 (non-fatal, rows already committed): %s", exc)
            try:
                session.rollback()
            except Exception:
                pass

    stats.elapsed_sec = time.monotonic() - t0
    log.info("[OI] %s done: pages=%d rows_fetched=%d rows_written=%d elapsed=%.1fs",
             symbol, stats.pages_fetched, stats.rows_fetched,
             stats.rows_written, stats.elapsed_sec)
    return stats


def _write_bronze_oi_history(
    session: "Session",
    rows: list[dict[str, Any]],
    ingest_run_id: str,
) -> int:
    """批量写入 bronze.market_oi_history_1h, ON CONFLICT DO NOTHING.

    Returns: 实际新增行数 (通过 RETURNING 1 计 count).
    """
    from sqlalchemy import text

    if not rows:
        return 0

    sql = text("""
        INSERT INTO bronze.market_oi_history_1h
            (symbol, ts, oi, oi_ccy, oi_usd, ingest_run_id)
        VALUES
            (:symbol, :ts, :oi, :oi_ccy, :oi_usd, :ingest_run_id)
        ON CONFLICT (symbol, ts) DO NOTHING
        RETURNING 1
    """)
    total_written = 0
    batch_size = 500
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        for r in batch:
            result = session.execute(sql, {
                "symbol": r["symbol"],
                "ts": r["ts"],
                "oi": r["oi"],
                "oi_ccy": r["oi_ccy"],
                "oi_usd": r["oi_usd"],
                "ingest_run_id": ingest_run_id,
            })
            # RETURNING 1: 如果 insert 了返回 1 行, conflict 则 0 行
            if result.rowcount > 0:
                total_written += 1
        session.flush()
    session.commit()
    return total_written


# ─────────────────────────────────────────────────────────────────────
# 2. Mark-price candles history
# ─────────────────────────────────────────────────────────────────────


MARK_CANDLES_PATH = "/api/v5/market/history-mark-price-candles"


def _parse_mark_candle_row(item: list[str]) -> dict[str, Any] | None:
    """OKX mark-price-candles-history 返回:
        [ts_ms, open, high, low, close, confirm]  (6 元素)
    我们只要已确认 bar (confirm=='1').
    """
    if not isinstance(item, list) or len(item) < 5:
        return None
    try:
        ts = _ms_to_dt(int(item[0]))
    except (ValueError, OSError, TypeError):
        return None
    # confirm check (若 confirm 列存在)
    if len(item) >= 6 and item[5] not in ("1", "true", "True", 1, True):
        return None
    o = _to_decimal_or_none(item[1])
    h = _to_decimal_or_none(item[2])
    l = _to_decimal_or_none(item[3])
    c = _to_decimal_or_none(item[4])
    if None in (o, h, l, c):
        return None
    return {
        "ts": ts,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
    }


def _oldest_mark_ts_ms(item: list[str]) -> int:
    return int(item[0])


def estimate_mark_candles_requests(target_days: int, period: str = "1m") -> dict[str, Any]:
    tf_min = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1H": 60}.get(period)
    if tf_min is None:
        raise ValueError(f"unsupported period: {period}")
    rows_per_day = 1440 / tf_min
    total_rows = int(target_days * rows_per_day)
    pages = (total_rows + API_LIMIT - 1) // API_LIMIT
    return {
        "endpoint": MARK_CANDLES_PATH,
        "period": period,
        "target_days": target_days,
        "rows_per_day": rows_per_day,
        "estimated_rows": total_rows,
        "estimated_pages": pages,
        "estimated_seconds_at_default_rate": pages * DEFAULT_RATE_LIMIT_SLEEP,
    }


def collect_mark_candles_history(
    session: "Session",
    *,
    symbol: str,
    target_days: int = 30,
    period: str = "1m",
    base_url: str = "https://www.okx.com",
    rate_limit_sleep: float = DEFAULT_RATE_LIMIT_SLEEP,
    timeout: float = 15.0,
    max_pages: int = MAX_PAGES_HARD_LIMIT,
    dry_run: bool = False,
    ingest_run_id: str | None = None,
) -> BackfillStats:
    """回填 OKX `mark-price-candles-history` 到 bronze.market_mark_price_candles_1m."""
    import httpx

    from aats.data_platform.jobs.checkpoint_manager import upsert_checkpoint
    from aats.data_platform.models import instrument_type_for_symbol, utc_now

    t0 = time.monotonic()
    end_ts = utc_now()
    start_ts = end_ts - timedelta(days=target_days)
    stats = BackfillStats(
        endpoint=MARK_CANDLES_PATH,
        symbol=symbol,
        period=period,
        target_start=start_ts,
        target_end=end_ts,
    )

    if dry_run:
        est = estimate_mark_candles_requests(target_days, period)
        stats.rows_fetched = est["estimated_rows"]
        stats.pages_fetched = est["estimated_pages"]
        stats.elapsed_sec = est["estimated_seconds_at_default_rate"]
        log.info("[DRY-RUN MARK] %s period=%s days=%d → pages≈%d rows≈%d time≈%.1fs",
                 symbol, period, target_days,
                 est["estimated_pages"], est["estimated_rows"],
                 est["estimated_seconds_at_default_rate"])
        return stats

    target_earliest_ms = _ts_ms(start_ts)
    cursor_after_ms = _ts_ms(end_ts)

    with httpx.Client() as client:
        rows = _paged_request(
            client,
            base_url,
            MARK_CANDLES_PATH,
            params={
                "instId": symbol,
                "bar": period,
                "limit": str(API_LIMIT),
            },
            cursor_key="after",  # mark-candles 用 after=老于 ts
            cursor_value=cursor_after_ms,
            target_earliest_ms=target_earliest_ms,
            parse_row=_parse_mark_candle_row,
            oldest_ts_from_row=_oldest_mark_ts_ms,
            stats=stats,
            timeout=timeout,
            rate_limit_sleep=rate_limit_sleep,
            max_pages=max_pages,
        )

    for r in rows:
        r["symbol"] = symbol
    rows = _dedupe_by_ts(rows)

    if rows:
        stats.earliest_ts = min(r["ts"] for r in rows)
        stats.latest_ts = max(r["ts"] for r in rows)

    if rows and ingest_run_id is not None:
        written = _write_bronze_mark_candles(session, rows, ingest_run_id)
        stats.rows_written = written
        stats.rows_skipped_conflict = len(rows) - written

        try:
            upsert_checkpoint(
                session,
                dataset_domain="microstructure",
                instrument_type=instrument_type_for_symbol(symbol),
                symbol=symbol,
                timeframe="mark_1m",
                last_successful_ts=stats.latest_ts,
                last_ingest_run_id=ingest_run_id,
            )
            session.commit()
        except Exception as exc:
            log.warning("checkpoint upsert 失败 (non-fatal, rows already committed): %s", exc)
            try:
                session.rollback()
            except Exception:
                pass

    stats.elapsed_sec = time.monotonic() - t0
    log.info("[MARK] %s done: pages=%d rows_fetched=%d rows_written=%d elapsed=%.1fs",
             symbol, stats.pages_fetched, stats.rows_fetched,
             stats.rows_written, stats.elapsed_sec)
    return stats


def _write_bronze_mark_candles(
    session: "Session",
    rows: list[dict[str, Any]],
    ingest_run_id: str,
) -> int:
    from sqlalchemy import text

    if not rows:
        return 0

    sql = text("""
        INSERT INTO bronze.market_mark_price_candles_1m
            (symbol, ts, open, high, low, close, ingest_run_id)
        VALUES
            (:symbol, :ts, :open, :high, :low, :close, :ingest_run_id)
        ON CONFLICT (symbol, ts) DO NOTHING
        RETURNING 1
    """)
    total_written = 0
    batch_size = 500
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        for r in batch:
            result = session.execute(sql, {
                "symbol": r["symbol"],
                "ts": r["ts"],
                "open": r["open"],
                "high": r["high"],
                "low": r["low"],
                "close": r["close"],
                "ingest_run_id": ingest_run_id,
            })
            if result.rowcount > 0:
                total_written += 1
        session.flush()
    session.commit()
    return total_written


# ─────────────────────────────────────────────────────────────────────
# 3. Long-short ratio history
# ─────────────────────────────────────────────────────────────────────


LS_RATIO_PATH = "/api/v5/rubik/stat/contracts/long-short-account-ratio"


def normalize_ls_symbol(ccy: str) -> str:
    """OKX LS ratio 用 ccy (e.g. "BTC"); Bronze 统一 schema 用 "{ccy}-USDT-SWAP"."""
    c = ccy.upper()
    if c.endswith("-USDT-SWAP"):
        return c
    return f"{c}-USDT-SWAP"


def _parse_ls_ratio_row(item: list[str]) -> dict[str, Any] | None:
    """OKX long-short-account-ratio 返回:
        [ts_ms, longShortRatio]  (2 元素, account-based)
    实测部分 endpoint 也可能返回 position-based 在第 3 列 (兜底保留).
    """
    if not isinstance(item, list) or len(item) < 2:
        return None
    try:
        ts = _ms_to_dt(int(item[0]))
    except (ValueError, OSError, TypeError):
        return None
    account_ratio = _to_decimal_or_none(item[1])
    if account_ratio is None:
        return None
    position_ratio = _to_decimal_or_none(item[2]) if len(item) > 2 else None
    return {
        "ts": ts,
        "ls_ratio_accounts": account_ratio,
        "ls_ratio_positions": position_ratio,
    }


def _oldest_ls_ts_ms(item: list[str]) -> int:
    return int(item[0])


def estimate_ls_ratio_requests(target_days: int, period: str = "5m") -> dict[str, Any]:
    tf_min = {"5m": 5, "15m": 15, "30m": 30, "1H": 60}.get(period)
    if tf_min is None:
        raise ValueError(f"unsupported period: {period}")
    rows_per_day = 1440 / tf_min
    total_rows = int(target_days * rows_per_day)
    pages = (total_rows + API_LIMIT - 1) // API_LIMIT
    return {
        "endpoint": LS_RATIO_PATH,
        "period": period,
        "target_days": target_days,
        "rows_per_day": rows_per_day,
        "estimated_rows": total_rows,
        "estimated_pages": pages,
        "estimated_seconds_at_default_rate": pages * DEFAULT_RATE_LIMIT_SLEEP,
    }


def collect_ls_ratio_history(
    session: "Session",
    *,
    ccy: str,
    target_days: int = 30,
    period: str = "5m",
    base_url: str = "https://www.okx.com",
    rate_limit_sleep: float = DEFAULT_RATE_LIMIT_SLEEP,
    timeout: float = 15.0,
    max_pages: int = MAX_PAGES_HARD_LIMIT,
    dry_run: bool = False,
    ingest_run_id: str | None = None,
) -> BackfillStats:
    """回填 OKX `long-short-account-ratio` 到 bronze.market_long_short_ratio_5m.

    ccy: "BTC" / "ETH" (OKX 用 ccy 不 instId). 我们在 Bronze 里规范化为 "BTC-USDT-SWAP".
    """
    import httpx

    from aats.data_platform.jobs.checkpoint_manager import upsert_checkpoint
    from aats.data_platform.models import utc_now

    t0 = time.monotonic()
    end_ts = utc_now()
    start_ts = end_ts - timedelta(days=target_days)
    symbol = normalize_ls_symbol(ccy)
    stats = BackfillStats(
        endpoint=LS_RATIO_PATH,
        symbol=symbol,
        period=period,
        target_start=start_ts,
        target_end=end_ts,
    )

    if dry_run:
        est = estimate_ls_ratio_requests(target_days, period)
        stats.rows_fetched = est["estimated_rows"]
        stats.pages_fetched = est["estimated_pages"]
        stats.elapsed_sec = est["estimated_seconds_at_default_rate"]
        log.info("[DRY-RUN LS] ccy=%s period=%s days=%d → pages≈%d rows≈%d time≈%.1fs",
                 ccy, period, target_days,
                 est["estimated_pages"], est["estimated_rows"],
                 est["estimated_seconds_at_default_rate"])
        return stats

    target_earliest_ms = _ts_ms(start_ts)
    cursor_end_ms = _ts_ms(end_ts)

    with httpx.Client() as client:
        rows = _paged_request(
            client,
            base_url,
            LS_RATIO_PATH,
            params={
                "ccy": ccy.upper(),
                "period": period,
                "limit": str(API_LIMIT),
            },
            cursor_key="end",
            cursor_value=cursor_end_ms,
            target_earliest_ms=target_earliest_ms,
            parse_row=_parse_ls_ratio_row,
            oldest_ts_from_row=_oldest_ls_ts_ms,
            stats=stats,
            timeout=timeout,
            rate_limit_sleep=rate_limit_sleep,
            max_pages=max_pages,
        )

    for r in rows:
        r["symbol"] = symbol
    rows = _dedupe_by_ts(rows)

    if rows:
        stats.earliest_ts = min(r["ts"] for r in rows)
        stats.latest_ts = max(r["ts"] for r in rows)

    if rows and ingest_run_id is not None:
        written = _write_bronze_ls_ratio(session, rows, ingest_run_id)
        stats.rows_written = written
        stats.rows_skipped_conflict = len(rows) - written

        try:
            upsert_checkpoint(
                session,
                dataset_domain="microstructure",
                instrument_type="swap",
                symbol=symbol,
                timeframe="ls_5m",
                last_successful_ts=stats.latest_ts,
                last_ingest_run_id=ingest_run_id,
            )
            session.commit()
        except Exception as exc:
            log.warning("checkpoint upsert 失败 (non-fatal, rows already committed): %s", exc)
            try:
                session.rollback()
            except Exception:
                pass

    stats.elapsed_sec = time.monotonic() - t0
    log.info("[LS] %s done: pages=%d rows_fetched=%d rows_written=%d elapsed=%.1fs",
             symbol, stats.pages_fetched, stats.rows_fetched,
             stats.rows_written, stats.elapsed_sec)
    return stats


def _write_bronze_ls_ratio(
    session: "Session",
    rows: list[dict[str, Any]],
    ingest_run_id: str,
) -> int:
    from sqlalchemy import text

    if not rows:
        return 0

    sql = text("""
        INSERT INTO bronze.market_long_short_ratio_5m
            (symbol, ts, ls_ratio_positions, ls_ratio_accounts, ingest_run_id)
        VALUES
            (:symbol, :ts, :ls_ratio_positions, :ls_ratio_accounts, :ingest_run_id)
        ON CONFLICT (symbol, ts) DO NOTHING
        RETURNING 1
    """)
    total_written = 0
    batch_size = 500
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        for r in batch:
            result = session.execute(sql, {
                "symbol": r["symbol"],
                "ts": r["ts"],
                "ls_ratio_positions": r.get("ls_ratio_positions"),
                "ls_ratio_accounts": r.get("ls_ratio_accounts"),
                "ingest_run_id": ingest_run_id,
            })
            if result.rowcount > 0:
                total_written += 1
        session.flush()
    session.commit()
    return total_written
