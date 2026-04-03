"""Bronze -> Silver merge pipeline.

Promotes validated bronze data to the canonical Silver layer.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.models import candle_table_name, funding_table_name, utc_now

log = logging.getLogger(__name__)


def merge_candles_to_silver(
    session: Session,
    *,
    symbol: str,
    timeframe: str,
    ingest_run_id: str,
) -> int:
    """Merge bronze candle rows into the Silver canonical table. Returns rows affected."""
    src = candle_table_name("bronze", symbol, timeframe)
    dst = candle_table_name("silver", symbol, timeframe)
    now = utc_now()

    result = session.execute(
        text(f"""
            INSERT INTO {dst}
                (symbol, ts, open, high, low, close,
                 vol, vol_ccy, vol_quote, confirm,
                 raw_symbol, raw_ts, source_file_id,
                 ingest_run_id, dataset_version, quality_flags,
                 created_at, updated_at)
            SELECT
                symbol, ts, open, high, low, close,
                vol, vol_ccy, vol_quote, confirm,
                raw_symbol, raw_ts, source_file_id,
                ingest_run_id, dataset_version, quality_flags,
                :now, :now
            FROM {src}
            WHERE ingest_run_id = :run_id AND symbol = :sym
            ON CONFLICT (symbol, ts) DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                vol = EXCLUDED.vol,
                vol_ccy = EXCLUDED.vol_ccy,
                vol_quote = EXCLUDED.vol_quote,
                confirm = EXCLUDED.confirm,
                raw_symbol = EXCLUDED.raw_symbol,
                raw_ts = EXCLUDED.raw_ts,
                source_file_id = COALESCE(EXCLUDED.source_file_id, {dst}.source_file_id),
                ingest_run_id = EXCLUDED.ingest_run_id,
                dataset_version = EXCLUDED.dataset_version,
                quality_flags = EXCLUDED.quality_flags,
                updated_at = EXCLUDED.updated_at
        """),
        dict(now=now, run_id=ingest_run_id, sym=symbol.upper()),
    )
    count = result.rowcount
    log.info("Merged %d candle rows to silver %s for %s", count, dst, symbol)
    return count


def merge_funding_to_silver(
    session: Session,
    *,
    symbol: str,
    ingest_run_id: str,
) -> int:
    """Merge bronze funding rows into Silver. Returns rows affected."""
    src = funding_table_name("bronze")
    dst = funding_table_name("silver")
    now = utc_now()

    result = session.execute(
        text(f"""
            INSERT INTO {dst}
                (symbol, ts, funding_rate, inst_type, formula_type,
                 method, realized_rate,
                 raw_symbol, raw_ts, source_file_id,
                 ingest_run_id, dataset_version, quality_flags,
                 created_at, updated_at)
            SELECT
                symbol, ts, funding_rate, inst_type, formula_type,
                method, realized_rate,
                raw_symbol, raw_ts, source_file_id,
                ingest_run_id, dataset_version, quality_flags,
                :now, :now
            FROM {src}
            WHERE ingest_run_id = :run_id AND symbol = :sym
            ON CONFLICT (symbol, ts) DO UPDATE SET
                funding_rate = EXCLUDED.funding_rate,
                inst_type = COALESCE(EXCLUDED.inst_type, {dst}.inst_type),
                formula_type = COALESCE(EXCLUDED.formula_type, {dst}.formula_type),
                method = COALESCE(EXCLUDED.method, {dst}.method),
                realized_rate = COALESCE(EXCLUDED.realized_rate, {dst}.realized_rate),
                raw_symbol = EXCLUDED.raw_symbol,
                raw_ts = EXCLUDED.raw_ts,
                source_file_id = COALESCE(EXCLUDED.source_file_id, {dst}.source_file_id),
                ingest_run_id = EXCLUDED.ingest_run_id,
                dataset_version = EXCLUDED.dataset_version,
                quality_flags = EXCLUDED.quality_flags,
                updated_at = EXCLUDED.updated_at
        """),
        dict(now=now, run_id=ingest_run_id, sym=symbol.upper()),
    )
    count = result.rowcount
    log.info("Merged %d funding rows to silver for %s", count, symbol)
    return count
