"""Gap detection and automatic repair job creation."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.jobs.checkpoint_manager import upsert_checkpoint
from aats.data_platform.jobs.run_registry import create_ingest_run
from aats.data_platform.models import candle_table_name, instrument_type_for_symbol

log = logging.getLogger(__name__)

_TF_DELTA = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
}


def detect_candle_gaps(
    session: Session,
    *,
    symbol: str,
    timeframe: str,
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    """Scan silver candles for missing intervals. Returns list of gap dicts."""
    timeframe = timeframe.lower()  # canonical — 与 candle_table_name 一致
    table = candle_table_name("silver", symbol, timeframe)
    delta = _TF_DELTA.get(timeframe)
    if delta is None:
        return []

    rows = session.execute(
        text(f"""
            SELECT ts FROM {table}
            WHERE symbol = :sym AND ts >= :start AND ts <= :end_ts
            ORDER BY ts
        """),
        dict(sym=symbol.upper(), start=window_start, end_ts=window_end),
    ).fetchall()

    gaps: list[dict[str, Any]] = []
    prev_ts = None
    for (ts,) in rows:
        if prev_ts is not None:
            expected = prev_ts + delta
            if ts > expected:
                gaps.append({
                    "gap_start": expected,
                    "gap_end": ts - delta,
                    "missing_bars": int((ts - prev_ts) / delta) - 1,
                })
        prev_ts = ts

    return gaps


def create_gap_repair_runs(
    session: Session,
    *,
    symbol: str,
    timeframe: str | None,
    dataset_domain: str = "candles",
    gaps: list[dict[str, Any]],
) -> list[str]:
    """Create gap_repair ingest_run entries for each detected gap. Returns run_ids."""
    if timeframe is not None:
        timeframe = timeframe.lower()  # canonical — 与 checkpoint / table name 一致
    inst_type = instrument_type_for_symbol(symbol)
    run_ids: list[str] = []
    for gap in gaps:
        run_id = create_ingest_run(
            session,
            run_type="gap_repair",
            dataset_domain=dataset_domain,
            instrument_type=inst_type,
            symbol=symbol.upper(),
            timeframe=timeframe,
            trigger_mode="auto_gap_repair",
        )
        run_ids.append(run_id)
        log.info("Gap repair run created: %s %s %s gap_start=%s gap_end=%s",
                 run_id, symbol, timeframe, gap["gap_start"], gap["gap_end"])

    # Update checkpoint gap status
    if gaps:
        earliest_gap = min(g["gap_start"] for g in gaps)
        latest_gap = max(g["gap_end"] for g in gaps)
        upsert_checkpoint(
            session,
            dataset_domain=dataset_domain,
            instrument_type=inst_type,
            symbol=symbol.upper(),
            timeframe=timeframe,
            gap_detected=True,
            gap_start_ts=earliest_gap,
            gap_end_ts=latest_gap,
            checkpoint_status="gap_detected",
        )

    return run_ids
