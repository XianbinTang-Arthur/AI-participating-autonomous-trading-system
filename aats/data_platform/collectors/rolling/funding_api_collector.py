"""Rolling funding API collector.

Fetches incremental funding rate history from OKX REST API
GET /api/v5/public/funding-rate-history
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.config import ResearchPlatformSettings
from aats.data_platform.jobs.checkpoint_manager import get_checkpoint, upsert_checkpoint
from aats.data_platform.jobs.run_registry import (
    create_ingest_run,
    create_run_item,
    finish_ingest_run,
    finish_run_item,
)
from aats.data_platform.models import FundingRow, funding_table_name, utc_now

log = logging.getLogger(__name__)

# Sentinel timeframe for funding checkpoints.
# funding 没有 candle 意义上的 timeframe，用 sentinel 避免 NULL 在
# PostgreSQL 唯一约束中无法正确 ON CONFLICT 的问题。
_FUNDING_TIMEFRAME_SENTINEL = "funding"

BATCH_SIZE = 2000
_API_LIMIT = 100


def _ts_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _parse_api_funding(item: dict[str, Any], symbol: str) -> FundingRow | None:
    """Parse one funding record from API JSON object."""
    try:
        ts = datetime.fromtimestamp(int(item["fundingTime"]) / 1000, tz=timezone.utc)
    except (ValueError, KeyError, OSError):
        return None
    try:
        rate = Decimal(item["fundingRate"])
    except (InvalidOperation, KeyError):
        return None
    return FundingRow(
        symbol=symbol.upper(),
        ts=ts,
        funding_rate=rate,
        inst_type=item.get("instType"),
        formula_type=item.get("formulaType"),
        method=item.get("method"),
        realized_rate=Decimal(item["realizedRate"]) if item.get("realizedRate") else None,
        raw_symbol=item.get("instId", symbol),
        raw_ts=item.get("fundingTime"),
    )


def _fetch_funding(
    client: httpx.Client,
    settings: ResearchPlatformSettings,
    symbol: str,
    after_ms: int | None = None,
    before_ms: int | None = None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "instId": symbol,
        "limit": str(_API_LIMIT),
    }
    if after_ms is not None:
        params["after"] = str(after_ms)
    if before_ms is not None:
        params["before"] = str(before_ms)

    url = f"{settings.okx_rest_url}/api/v5/public/funding-rate-history"
    resp = client.get(url, params=params, timeout=settings.okx_timeout_seconds)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != "0":
        raise RuntimeError(f"OKX funding API error: {body.get('msg', body)}")
    return body.get("data", [])


def _write_staging(
    session: Session,
    table: str,
    rows: list[FundingRow],
    run_id: str,
    dataset_version: str,
) -> int:
    if not rows:
        return 0
    now = utc_now()
    values = [
        dict(
            symbol=r.symbol, ts=r.ts,
            funding_rate=r.funding_rate,
            inst_type=r.inst_type,
            formula_type=r.formula_type,
            method=r.method,
            realized_rate=r.realized_rate,
            raw_symbol=r.raw_symbol, raw_ts=r.raw_ts,
            source_file_id=None,
            ingest_run_id=run_id,
            dataset_version=dataset_version,
            now=now,
        )
        for r in rows
    ]
    total = 0
    for i in range(0, len(values), BATCH_SIZE):
        batch = values[i : i + BATCH_SIZE]
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


def _dedupe_funding_rows(rows: list[FundingRow]) -> list[FundingRow]:
    """Deduplicate funding rows by (symbol, ts).

    OKX returns results newest-first and backward pagination using ``after``
    can produce overlapping rows at page boundaries.  This removes duplicates
    while preserving the first occurrence.
    """
    seen: set[tuple[str, datetime]] = set()
    result: list[FundingRow] = []
    for r in rows:
        key = (r.symbol, r.ts)
        if key not in seen:
            seen.add(key)
            result.append(r)
    return result


def collect_funding_incremental(
    session: Session,
    settings: ResearchPlatformSettings,
    *,
    symbol: str,
    dataset_version: str = "v1.0",
    max_pages: int = 10,
) -> str:
    """Fetch recent funding rates and write to staging. Returns ingest_run_id."""
    table = funding_table_name("staging")

    # 一次性迁移: 如果存在 timeframe=NULL 的旧 checkpoint，更新为 sentinel
    _migrate_null_funding_checkpoint(session, symbol.upper())

    cp = get_checkpoint(
        session,
        dataset_domain="funding",
        instrument_type="swap",
        symbol=symbol.upper(),
        timeframe=_FUNDING_TIMEFRAME_SENTINEL,
    )

    run_id = create_ingest_run(
        session,
        run_type="rolling",
        dataset_domain="funding",
        instrument_type="swap",
        symbol=symbol.upper(),
        trigger_mode="scheduler",
    )
    item_id = create_run_item(
        session,
        ingest_run_id=run_id,
        dataset_domain="funding",
        instrument_type="swap",
        symbol=symbol.upper(),
    )

    try:
        all_rows: list[FundingRow] = []
        checkpoint_ts: datetime | None = None
        if cp and cp.get("last_successful_ts"):
            checkpoint_ts = cp["last_successful_ts"]

        # OKX funding-rate-history semantics (results newest-first):
        #   before=X -> records with fundingTime > X  (NEWER)
        #   after=X  -> records with fundingTime < X  (OLDER)
        #
        # Rolling: fetch latest, page backward toward checkpoint.

        with httpx.Client() as client:
            raw_data = _fetch_funding(client, settings, symbol)
            page = 0
            while raw_data and page < max_pages:
                for item in raw_data:
                    row = _parse_api_funding(item, symbol)
                    if row:
                        all_rows.append(row)
                oldest_ts = min(int(d["fundingTime"]) for d in raw_data)
                if checkpoint_ts and oldest_ts <= _ts_ms(checkpoint_ts):
                    break
                if len(raw_data) < _API_LIMIT:
                    break
                time.sleep(settings.okx_rate_limit_sleep)
                raw_data = _fetch_funding(
                    client, settings, symbol, after_ms=oldest_ts,
                )
                page += 1

        # Filter: only keep rows strictly newer than checkpoint
        if checkpoint_ts:
            all_rows = [r for r in all_rows if r.ts > checkpoint_ts]

        all_rows = _dedupe_funding_rows(all_rows)

        count = _write_staging(session, table, all_rows, run_id, dataset_version)

        if all_rows:
            newest_ts = max(r.ts for r in all_rows)
            upsert_checkpoint(
                session,
                dataset_domain="funding",
                instrument_type="swap",
                symbol=symbol.upper(),
                timeframe=_FUNDING_TIMEFRAME_SENTINEL,
                last_successful_ts=newest_ts,
                next_expected_ts=newest_ts + timedelta(hours=8),
                last_ingest_run_id=run_id,
            )

        finish_run_item(session, item_id, status="succeeded",
                        raw_rows_read=len(all_rows), rows_written_staging=count)
        finish_ingest_run(session, run_id, status="succeeded")
        log.info("Rolling funding OK: %s — %d rows", symbol, count)
    except Exception as exc:
        finish_run_item(session, item_id, status="failed", error_message=str(exc))
        finish_ingest_run(session, run_id, status="failed", error_message=str(exc))
        raise

    return run_id


# ── 一次性迁移: NULL timeframe → sentinel ─────────────────────────


def _migrate_null_funding_checkpoint(session: Session, symbol: str) -> None:
    """将旧 timeframe=NULL 的 funding checkpoint 迁移为 sentinel.

    历史上 NULL unique 约束缺陷可能导致同一 symbol 存在多条 NULL 行。
    本函数：
      1. sentinel 行已存在 → 仅删除残留 NULL 行
      2. sentinel 行不存在 → 取最新 NULL 行升级为 sentinel，删除其余
    幂等：无 NULL 行时直接返回。
    """
    # 查找所有 NULL 行，按 last_successful_ts 降序
    null_rows = session.execute(
        text("""
            SELECT checkpoint_id
            FROM meta.ingest_checkpoints
            WHERE dataset_domain = 'funding'
              AND instrument_type = 'swap'
              AND symbol = :sym
              AND timeframe IS NULL
            ORDER BY COALESCE(last_successful_ts, updated_at, created_at) DESC
        """),
        dict(sym=symbol),
    ).fetchall()

    if not null_rows:
        return  # 无需迁移

    # sentinel 行是否已存在
    sentinel_exists = session.execute(
        text("""
            SELECT 1 FROM meta.ingest_checkpoints
            WHERE dataset_domain = 'funding'
              AND instrument_type = 'swap'
              AND symbol = :sym
              AND timeframe = :tf
            LIMIT 1
        """),
        dict(sym=symbol, tf=_FUNDING_TIMEFRAME_SENTINEL),
    ).scalar()

    null_ids = [row[0] for row in null_rows]

    if sentinel_exists:
        # sentinel 已存在，所有 NULL 行都是残留，直接删除
        ids_to_delete = null_ids
    else:
        # 将最新 NULL 行升级为 sentinel
        best_id = null_ids[0]
        session.execute(
            text("""
                UPDATE meta.ingest_checkpoints
                SET timeframe = :tf
                WHERE checkpoint_id = :cp_id
            """),
            dict(tf=_FUNDING_TIMEFRAME_SENTINEL, cp_id=best_id),
        )
        ids_to_delete = null_ids[1:]  # 其余 NULL 行待删除
        log.info("Migrated funding checkpoint NULL→'%s' for %s (id=%s)",
                 _FUNDING_TIMEFRAME_SENTINEL, symbol, best_id)

    # 删除多余 NULL 行
    if ids_to_delete:
        session.execute(
            text("""
                DELETE FROM meta.ingest_checkpoints
                WHERE checkpoint_id = ANY(:ids)
            """),
            dict(ids=ids_to_delete),
        )
        log.info("Deleted %d orphan NULL funding checkpoint(s) for %s",
                 len(ids_to_delete), symbol)
