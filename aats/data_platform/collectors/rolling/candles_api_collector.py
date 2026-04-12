"""Rolling candles API collector.

Fetches incremental candle data from OKX REST API
GET /api/v5/market/history-candles
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
from aats.data_platform.jobs.checkpoint_manager import advance_checkpoint, get_checkpoint, upsert_checkpoint
from aats.data_platform.jobs.run_registry import (
    create_ingest_run,
    create_run_item,
    finish_ingest_run,
    finish_run_item,
)
from aats.data_platform.models import (
    CandleRow,
    candle_table_name,
    instrument_type_for_symbol,
    utc_now,
)

log = logging.getLogger(__name__)

BATCH_SIZE = 2000

# Timeframe -> timedelta mapping (大小写兼容)
_TF_DELTA = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1H": timedelta(hours=1),
    "1h": timedelta(hours=1),
}

# Max bars per single API request
_API_LIMIT = 100


def _ts_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _parse_api_candle(item: list[str], symbol: str) -> CandleRow | None:
    """Parse one candle row from API response array."""
    if len(item) < 9:
        return None
    try:
        ts = datetime.fromtimestamp(int(item[0]) / 1000, tz=timezone.utc)
    except (ValueError, OSError):
        return None
    try:
        return CandleRow(
            symbol=symbol.upper(),
            ts=ts,
            open=Decimal(item[1]),
            high=Decimal(item[2]),
            low=Decimal(item[3]),
            close=Decimal(item[4]),
            vol=Decimal(item[5]) if item[5] else None,
            vol_ccy=Decimal(item[6]) if item[6] else None,
            vol_quote=Decimal(item[7]) if item[7] else None,
            confirm=item[8] in ("1", "true", "True"),
            raw_symbol=symbol,
            raw_ts=item[0],
        )
    except (InvalidOperation, IndexError):
        return None


def _fetch_candles(
    client: httpx.Client,
    settings: ResearchPlatformSettings,
    symbol: str,
    timeframe: str,
    after_ms: int | None = None,
    before_ms: int | None = None,
) -> list[list[str]]:
    """Call OKX history-candles API once."""
    params: dict[str, Any] = {
        "instId": symbol,
        "bar": timeframe,
        "limit": str(_API_LIMIT),
    }
    if after_ms is not None:
        params["after"] = str(after_ms)
    if before_ms is not None:
        params["before"] = str(before_ms)

    url = f"{settings.okx_rest_url}/api/v5/market/history-candles"
    resp = client.get(url, params=params, timeout=settings.okx_timeout_seconds)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != "0":
        raise RuntimeError(f"OKX API error: {body.get('msg', body)}")
    return body.get("data", [])


def _write_staging(
    session: Session,
    table: str,
    rows: list[CandleRow],
    run_id: str,
    dataset_version: str,
) -> int:
    if not rows:
        return 0
    now = utc_now()
    values = [
        dict(
            symbol=r.symbol, ts=r.ts,
            open=r.open, high=r.high, low=r.low, close=r.close,
            vol=r.vol, vol_ccy=r.vol_ccy, vol_quote=r.vol_quote,
            confirm=r.confirm,
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


def _dedupe_candle_rows(rows: list[CandleRow]) -> list[CandleRow]:
    """Deduplicate candle rows by (symbol, ts).

    OKX returns results newest-first and backward pagination using ``after``
    can produce overlapping rows at page boundaries.  This removes duplicates
    while preserving the first occurrence.
    """
    seen: set[tuple[str, datetime]] = set()
    result: list[CandleRow] = []
    for r in rows:
        key = (r.symbol, r.ts)
        if key not in seen:
            seen.add(key)
            result.append(r)
    return result


def collect_candles_incremental(
    session: Session,
    settings: ResearchPlatformSettings,
    *,
    symbol: str,
    timeframe: str,
    dataset_version: str = "v1.0",
    max_pages: int = 10,
) -> str:
    """Fetch recent candles from API and write to staging. Returns ingest_run_id."""
    inst_type = instrument_type_for_symbol(symbol)
    table = candle_table_name("staging", symbol, timeframe)
    delta = _TF_DELTA.get(timeframe)
    if delta is None:
        raise ValueError(f"Unsupported timeframe '{timeframe}'. Valid: {sorted(_TF_DELTA)}")

    # Load checkpoint
    cp = get_checkpoint(
        session,
        dataset_domain="candles",
        instrument_type=inst_type,
        symbol=symbol.upper(),
        timeframe=timeframe,
    )

    run_id = create_ingest_run(
        session,
        run_type="rolling",
        dataset_domain="candles",
        instrument_type=inst_type,
        symbol=symbol.upper(),
        timeframe=timeframe,
        trigger_mode="scheduler",
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
        all_rows: list[CandleRow] = []
        checkpoint_ts: datetime | None = None
        if cp and cp.get("last_successful_ts"):
            checkpoint_ts = cp["last_successful_ts"]

        # OKX history-candles semantics (results newest-first):
        #   before=X -> returns records with ts > X  (NEWER than X)
        #   after=X  -> returns records with ts < X  (OLDER than X)
        #
        # Rolling strategy: fetch latest data, then page backward
        # toward the checkpoint to collect everything new.

        with httpx.Client() as client:
            # First request: get the latest bars (no params)
            raw_data = _fetch_candles(client, settings, symbol, timeframe)
            page = 0
            while raw_data and page < max_pages:
                for item in raw_data:
                    row = _parse_api_candle(item, symbol)
                    if row:
                        all_rows.append(row)
                # Check if we've reached data older than our checkpoint
                oldest_ts_in_page = min(int(d[0]) for d in raw_data)
                if checkpoint_ts and oldest_ts_in_page <= _ts_ms(checkpoint_ts):
                    break  # we've overlapped with existing data
                if len(raw_data) < _API_LIMIT:
                    break  # no more pages
                # Page backward: get records older than this page's oldest
                time.sleep(settings.okx_rate_limit_sleep)
                raw_data = _fetch_candles(
                    client, settings, symbol, timeframe,
                    after_ms=oldest_ts_in_page,
                )
                page += 1

        # Filter: only keep rows strictly newer than checkpoint
        if checkpoint_ts:
            all_rows = [r for r in all_rows if r.ts > checkpoint_ts]

        all_rows = _dedupe_candle_rows(all_rows)

        count = _write_staging(session, table, all_rows, run_id, dataset_version)

        # Advance checkpoint
        if all_rows:
            newest_ts = max(r.ts for r in all_rows)
            next_ts = newest_ts + delta
            upsert_checkpoint(
                session,
                dataset_domain="candles",
                instrument_type=inst_type,
                symbol=symbol.upper(),
                timeframe=timeframe,
                last_successful_ts=newest_ts,
                next_expected_ts=next_ts,
                last_ingest_run_id=run_id,
            )

        finish_run_item(session, item_id, status="succeeded",
                        raw_rows_read=len(all_rows), rows_written_staging=count)
        finish_ingest_run(session, run_id, status="succeeded")
        log.info("Rolling candles OK: %s %s — %d rows", symbol, timeframe, count)
    except Exception as exc:
        finish_run_item(session, item_id, status="failed", error_message=str(exc))
        finish_ingest_run(session, run_id, status="failed", error_message=str(exc))
        raise

    return run_id
