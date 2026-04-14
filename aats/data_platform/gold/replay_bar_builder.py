"""Gold replay bar builder.

Reads Silver candles (and optionally Silver funding for swaps),
builds replay-ready Gold bars, and writes them to gold tables.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.gold.funding_aligner import align_funding_to_bars, load_silver_funding
from aats.data_platform.jobs.run_registry import (
    create_ingest_run,
    create_run_item,
    finish_ingest_run,
    finish_run_item,
)
from aats.data_platform.models import (
    candle_table_name,
    instrument_type_for_symbol,
    replay_bar_table_name,
    utc_now,
)

log = logging.getLogger(__name__)

BATCH_SIZE = 2000


def build_gold_replay_bars(
    session: Session,
    *,
    symbol: str,
    timeframe: str,
    window_start: datetime,
    window_end: datetime,
    candle_dataset_version: str = "v1.0",
    funding_dataset_version: str | None = None,
) -> str:
    """Build Gold replay bars from Silver. Returns build_run_id."""
    inst_type = instrument_type_for_symbol(symbol)
    is_swap = inst_type == "swap"
    silver_candle_table = candle_table_name("silver", symbol, timeframe)
    gold_table = replay_bar_table_name(symbol, timeframe)

    # Create gold_build run
    run_id = create_ingest_run(
        session,
        run_type="gold_build",
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
        window_start_ts=window_start,
        window_end_ts=window_end,
    )

    try:
        # Load silver candles
        candles = session.execute(
            text(f"""
                SELECT symbol, ts, open, high, low, close,
                       vol, vol_quote, confirm
                FROM {silver_candle_table}
                WHERE symbol = :sym AND ts >= :start AND ts <= :end_ts
                ORDER BY ts
            """),
            dict(sym=symbol.upper(), start=window_start, end_ts=window_end),
        ).fetchall()

        if not candles:
            finish_run_item(session, item_id, status="succeeded", rows_written_gold=0)
            finish_ingest_run(session, run_id, status="succeeded")
            return run_id

        # Prepare funding alignment for swaps
        funding_map: dict[datetime, tuple] = {}
        if is_swap:
            funding_events = load_silver_funding(session, symbol, window_start, window_end)
            bar_timestamps = [c[1] for c in candles]
            funding_map = align_funding_to_bars(bar_timestamps, funding_events)

        # Build and insert gold bars
        #
        # Volume semantics note:
        #   Gold `volume`       <- Silver `vol`       (base currency for spot, contracts for swap)
        #   Gold `quote_volume` <- Silver `vol_quote` (quote currency volume)
        #   These are NOT semantically identical across spot/swap.  Spot `vol` is
        #   base-asset quantity; swap `vol` is number of contracts.  Downstream
        #   replay/analytics must account for instrument type when interpreting
        #   these fields.  Phase 1 preserves the raw mapping without unification.
        now = utc_now()
        values: list[dict[str, Any]] = []
        for c in candles:
            sym, ts, o, h, low, cl, vol, qvol, confirm = c
            aligned_rate, funding_ts = funding_map.get(ts, (None, None))
            # Gold is_closed derives from Silver confirm — explicit bool cast
            values.append(dict(
                symbol=sym, ts=ts,
                open=o, high=h, low=low, close=cl,
                volume=vol, quote_volume=qvol,
                is_closed=bool(confirm),
                aligned_funding_rate=aligned_rate,
                funding_source_ts=funding_ts,
                source_candle_dataset_version=candle_dataset_version,
                source_funding_dataset_version=funding_dataset_version if is_swap else None,
                build_run_id=run_id,
                now=now,
            ))

        total = 0
        for i in range(0, len(values), BATCH_SIZE):
            batch = values[i : i + BATCH_SIZE]
            session.execute(
                text(f"""
                    INSERT INTO {gold_table}
                        (symbol, ts, open, high, low, close,
                         volume, quote_volume, is_closed,
                         aligned_funding_rate, funding_source_ts,
                         source_candle_dataset_version, source_funding_dataset_version,
                         build_run_id, created_at, updated_at)
                    VALUES
                        (:symbol, :ts, :open, :high, :low, :close,
                         :volume, :quote_volume, :is_closed,
                         :aligned_funding_rate, :funding_source_ts,
                         :source_candle_dataset_version, :source_funding_dataset_version,
                         :build_run_id, :now, :now)
                    ON CONFLICT (symbol, ts) DO UPDATE SET
                        open = EXCLUDED.open,
                        high = EXCLUDED.high,
                        low = EXCLUDED.low,
                        close = EXCLUDED.close,
                        volume = EXCLUDED.volume,
                        quote_volume = EXCLUDED.quote_volume,
                        is_closed = EXCLUDED.is_closed,
                        aligned_funding_rate = EXCLUDED.aligned_funding_rate,
                        funding_source_ts = EXCLUDED.funding_source_ts,
                        source_candle_dataset_version = EXCLUDED.source_candle_dataset_version,
                        source_funding_dataset_version = EXCLUDED.source_funding_dataset_version,
                        build_run_id = EXCLUDED.build_run_id,
                        updated_at = EXCLUDED.updated_at
                """),
                batch,
            )
            total += len(batch)

        finish_run_item(session, item_id, status="succeeded", rows_written_gold=total)
        finish_ingest_run(session, run_id, status="succeeded")
        log.info("Gold replay bars built: %s %s — %d rows", symbol, timeframe, total)
    except Exception as exc:
        finish_run_item(session, item_id, status="failed", error_message=str(exc))
        finish_ingest_run(session, run_id, status="failed", error_message=str(exc))
        raise

    return run_id
