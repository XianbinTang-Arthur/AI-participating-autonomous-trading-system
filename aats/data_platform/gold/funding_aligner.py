"""Funding alignment for swap replay bars.

Aligns Silver funding events to Silver swap candle bars by assigning
each bar the most recent funding rate effective at or before the bar's timestamp.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.models import funding_table_name


def load_silver_funding(
    session: Session,
    symbol: str,
    start_ts: datetime,
    end_ts: datetime,
) -> list[dict[str, Any]]:
    """Load funding events from silver for alignment.

    Includes the most recent funding event **before** ``start_ts`` so that
    bars at the beginning of the window can inherit a carry-forward rate.
    """
    table = funding_table_name("silver")

    # 1. Most recent funding event strictly before the window
    pre_row = session.execute(
        text(f"""
            SELECT ts, funding_rate, dataset_version
            FROM {table}
            WHERE symbol = :sym AND ts < :start
            ORDER BY ts DESC
            LIMIT 1
        """),
        dict(sym=symbol.upper(), start=start_ts),
    ).fetchone()

    # 2. All funding events within [start_ts, end_ts]
    window_rows = session.execute(
        text(f"""
            SELECT ts, funding_rate, dataset_version
            FROM {table}
            WHERE symbol = :sym AND ts >= :start AND ts <= :end_ts
            ORDER BY ts
        """),
        dict(sym=symbol.upper(), start=start_ts, end_ts=end_ts),
    ).fetchall()

    result = []
    if pre_row:
        result.append(
            {
                "ts": pre_row[0],
                "funding_rate": pre_row[1],
                "dataset_version": pre_row[2],
            }
        )
    result.extend(
        {"ts": r[0], "funding_rate": r[1], "dataset_version": r[2]}
        for r in window_rows
    )
    return result


def align_funding_to_bars(
    bar_timestamps: list[datetime],
    funding_events: list[dict[str, Any]],
) -> dict[datetime, tuple[Decimal | None, datetime | None, str | None]]:
    """For each bar ts, find the most recent funding event at or before it.

    This implements an **as-of join** (also called a point-in-time join):
    each bar inherits the latest-known funding rate as of its timestamp.
    This matches how a live system would observe funding — it only sees
    rates that have already been published.  Do NOT change this to an
    exact-match or interval-interior join without updating replay semantics.

    Returns {bar_ts: (aligned_funding_rate, funding_source_ts, dataset_version)}.
    """
    result: dict[datetime, tuple[Decimal | None, datetime | None, str | None]] = {}
    fi = 0
    funding_sorted = sorted(funding_events, key=lambda x: x["ts"])
    bar_sorted = sorted(bar_timestamps)

    for bar_ts in bar_sorted:
        while fi < len(funding_sorted) and funding_sorted[fi]["ts"] <= bar_ts:
            fi += 1
        if fi > 0:
            f = funding_sorted[fi - 1]
            result[bar_ts] = (
                f["funding_rate"],
                f["ts"],
                str(f["dataset_version"]) if f.get("dataset_version") is not None else None,
            )
        else:
            result[bar_ts] = (None, None, None)
    return result
