"""Candle quality validation.

Checks: duplicate rows, missing intervals, out-of-order, OHLC validity,
volume non-negative, confirm legality.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.validate.report_writer import write_quality_report


def _to_utc(dt: datetime) -> datetime:
    """归一化为 UTC, 防止 DST fold 场景下比较误判."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    return dt

# Timeframe -> expected interval
_TF_DELTA = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
}


def validate_candles(
    session: Session,
    *,
    table: str,
    ingest_run_id: str,
    symbol: str,
    timeframe: str,
    dataset_version: str,
    dataset_layer: str = "staging",
    instrument_type: str | None = None,
) -> dict[str, Any]:
    """Run quality checks on candle data. Returns the quality summary dict."""
    delta = _TF_DELTA.get(timeframe.lower())

    # Fetch rows ordered by ts
    rows = session.execute(
        text(f"""
            SELECT ts, open, high, low, close, vol, vol_ccy, vol_quote, confirm
            FROM {table}
            WHERE ingest_run_id = :run_id AND symbol = :sym
            ORDER BY ts
        """),
        dict(run_id=ingest_run_id, sym=symbol),
    ).fetchall()

    total = len(rows)
    duplicates = 0
    missing = 0
    out_of_order = 0
    invalid_price = 0
    invalid_volume = 0
    suspect = 0

    seen_ts: set[int] = set()  # 用 UTC epoch 微秒做去重键
    prev_ts_utc = None

    for row in rows:
        ts, o, h, l, c, vol, vol_ccy, vol_quote, confirm = row

        # 归一化为 UTC，避免 DST fold 误判
        ts_utc = _to_utc(ts)

        # Duplicate check — 用 epoch 微秒比较，消除时区表示差异
        ts_epoch = int(ts_utc.timestamp() * 1_000_000)
        if ts_epoch in seen_ts:
            duplicates += 1
        seen_ts.add(ts_epoch)

        # Order check — UTC 比较
        if prev_ts_utc is not None and ts_utc < prev_ts_utc:
            out_of_order += 1

        # Missing interval check — UTC 比较
        if prev_ts_utc is not None and delta:
            expected = prev_ts_utc + delta
            if ts_utc > expected:
                gap_count = int((ts_utc - prev_ts_utc) / delta) - 1
                missing += max(gap_count, 0)

        prev_ts_utc = ts_utc

        # OHLC validity
        if h < l or o <= 0 or h <= 0 or l <= 0 or c <= 0:
            invalid_price += 1
        if h < o or h < c or l > o or l > c:
            suspect += 1

        # Volume non-negative
        for v in (vol, vol_ccy, vol_quote):
            if v is not None and v < 0:
                invalid_volume += 1
                break

    # Determine status
    if duplicates > 0 or invalid_price > 0 or out_of_order > 0:
        status = "fail"
    elif missing > 0 or suspect > 0 or invalid_volume > 0:
        status = "warn"
    else:
        status = "pass"

    window_start = rows[0][0] if rows else None
    window_end = rows[-1][0] if rows else None

    report_id = write_quality_report(
        session,
        ingest_run_id=ingest_run_id,
        dataset_layer=dataset_layer,
        dataset_domain="candles",
        instrument_type=instrument_type,
        symbol=symbol,
        timeframe=timeframe,
        dataset_version=dataset_version,
        window_start_ts=window_start,
        window_end_ts=window_end,
        total_rows=total,
        missing_intervals_count=missing,
        duplicate_rows_count=duplicates,
        out_of_order_rows_count=out_of_order,
        invalid_price_rows_count=invalid_price,
        invalid_volume_rows_count=invalid_volume,
        suspect_rows_count=suspect,
        quality_status=status,
    )

    return dict(
        quality_report_id=report_id,
        quality_status=status,
        total_rows=total,
        duplicates=duplicates,
        missing=missing,
        out_of_order=out_of_order,
        invalid_price=invalid_price,
        invalid_volume=invalid_volume,
        suspect=suspect,
    )
