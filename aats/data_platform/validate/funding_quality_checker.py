"""Funding quality validation.

Checks: duplicate rows, out-of-order, funding_rate validity.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.validate.report_writer import write_quality_report


def validate_funding(
    session: Session,
    *,
    table: str,
    ingest_run_id: str,
    symbol: str,
    dataset_version: str,
    dataset_layer: str = "staging",
    instrument_type: str | None = "swap",
) -> dict[str, Any]:
    """Run quality checks on funding data. Returns the quality summary dict."""
    rows = session.execute(
        text(f"""
            SELECT ts, funding_rate
            FROM {table}
            WHERE ingest_run_id = :run_id AND symbol = :sym
            ORDER BY ts
        """),
        dict(run_id=ingest_run_id, sym=symbol),
    ).fetchall()

    total = len(rows)
    duplicates = 0
    out_of_order = 0
    invalid_rate = 0

    seen_ts: set[str] = set()
    prev_ts = None

    for row in rows:
        ts, rate = row

        ts_key = str(ts)
        if ts_key in seen_ts:
            duplicates += 1
        seen_ts.add(ts_key)

        if prev_ts is not None and ts < prev_ts:
            out_of_order += 1
        prev_ts = ts

        if rate is None:
            invalid_rate += 1

    if duplicates > 0 or out_of_order > 0:
        status = "fail"
    elif invalid_rate > 0:
        status = "warn"
    else:
        status = "pass"

    window_start = rows[0][0] if rows else None
    window_end = rows[-1][0] if rows else None

    report_id = write_quality_report(
        session,
        ingest_run_id=ingest_run_id,
        dataset_layer=dataset_layer,
        dataset_domain="funding",
        instrument_type=instrument_type,
        symbol=symbol,
        dataset_version=dataset_version,
        window_start_ts=window_start,
        window_end_ts=window_end,
        total_rows=total,
        duplicate_rows_count=duplicates,
        out_of_order_rows_count=out_of_order,
        invalid_price_rows_count=invalid_rate,
        quality_status=status,
    )

    return dict(
        quality_report_id=report_id,
        quality_status=status,
        total_rows=total,
        duplicates=duplicates,
        out_of_order=out_of_order,
        invalid_rate=invalid_rate,
    )
