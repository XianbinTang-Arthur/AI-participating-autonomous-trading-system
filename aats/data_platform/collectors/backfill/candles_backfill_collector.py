"""Candle historical backfill collector.

Orchestrates: file discovery -> parse -> staging write -> run tracking.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.collectors.backfill.file_parser import parse_candle_zip
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


def resolve_candle_timeframe(
    *,
    cli_timeframe: str | None,
    timeframe_hint: str | None,
) -> str | None:
    """Single-point timeframe decision for candle backfill.

    Priority:
      1. Explicit CLI --timeframe override
      2. Directory-inferred timeframe_hint from file discovery
      3. None (caller must handle as skip/fail)
    """
    return cli_timeframe or timeframe_hint or None


def _write_candle_staging_batch(
    session: Session,
    table: str,
    rows: list[CandleRow],
    ingest_run_id: str,
    dataset_version: str,
    source_file_id: str | None,
) -> int:
    """Bulk-insert candle rows into a staging table. Returns row count."""
    if not rows:
        return 0
    values: list[dict[str, Any]] = []
    for r in rows:
        values.append(dict(
            symbol=r.symbol, ts=r.ts,
            open=r.open, high=r.high, low=r.low, close=r.close,
            vol=r.vol, vol_ccy=r.vol_ccy, vol_quote=r.vol_quote,
            confirm=r.confirm,
            raw_symbol=r.raw_symbol, raw_ts=r.raw_ts,
            source_file_id=source_file_id,
            ingest_run_id=ingest_run_id,
            dataset_version=dataset_version,
            now=utc_now(),
        ))
    # Batch insert
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


def collect_backfill_candle_file(
    session: Session,
    *,
    source_file_id: str,
    zip_path: str,
    symbol_hint: str,
    timeframe: str,
    dataset_version: str = "v1.0",
) -> str:
    """Run a complete backfill for one candle ZIP file. Returns ingest_run_id."""
    inst_type = instrument_type_for_symbol(symbol_hint)
    table = candle_table_name("staging", symbol_hint, timeframe)

    run_id = create_ingest_run(
        session,
        run_type="backfill",
        dataset_domain="candles",
        instrument_type=inst_type,
        symbol=symbol_hint.upper(),
        timeframe=timeframe,
        trigger_mode="manual",
    )
    item_id = create_run_item(
        session,
        ingest_run_id=run_id,
        dataset_domain="candles",
        instrument_type=inst_type,
        symbol=symbol_hint.upper(),
        timeframe=timeframe,
        source_file_id=source_file_id,
    )

    try:
        rows = parse_candle_zip(zip_path, symbol_hint)
        count = _write_candle_staging_batch(
            session, table, rows, run_id, dataset_version, source_file_id,
        )
        # Update source file status
        session.execute(
            text("""
                UPDATE meta.raw_source_files
                SET parse_status = 'parsed', raw_row_count = :cnt,
                    ingested_status = 'ingested', updated_at = :now
                WHERE source_file_id = :fid
            """),
            dict(cnt=len(rows), now=utc_now(), fid=source_file_id),
        )
        finish_run_item(session, item_id, status="succeeded",
                        raw_rows_read=len(rows), rows_written_staging=count)
        finish_ingest_run(session, run_id, status="succeeded")
        log.info("Backfill candle OK: %s rows -> %s", count, table)
    except Exception as exc:
        finish_run_item(session, item_id, status="failed", error_message=str(exc))
        finish_ingest_run(session, run_id, status="failed", error_message=str(exc))
        session.execute(
            text("""
                UPDATE meta.raw_source_files
                SET parse_status = 'failed', parse_error = :err, updated_at = :now
                WHERE source_file_id = :fid
            """),
            dict(err=str(exc), now=utc_now(), fid=source_file_id),
        )
        raise

    return run_id
