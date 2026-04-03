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

from aats.data_platform.models import candle_table_name, funding_table_name


def load_silver_funding(
    session: Session,
    symbol: str,
    start_ts: datetime,
    end_ts: datetime,
) -> list[dict[str, Any]]:
    """Load funding events from silver in the given window."""
    table = funding_table_name("silver")
    rows = session.execute(
        text(f"""
            SELECT ts, funding_rate
            FROM {table}
            WHERE symbol = :sym AND ts >= :start AND ts <= :end_ts
            ORDER BY ts
        """),
        dict(sym=symbol.upper(), start=start_ts, end_ts=end_ts),
    ).fetchall()
    return [{"ts": r[0], "funding_rate": r[1]} for r in rows]


def align_funding_to_bars(
    bar_timestamps: list[datetime],
    funding_events: list[dict[str, Any]],
) -> dict[datetime, tuple[Decimal | None, datetime | None]]:
    """For each bar ts, find the most recent funding event at or before it.

    Returns {bar_ts: (aligned_funding_rate, funding_source_ts)}.
    """
    result: dict[datetime, tuple[Decimal | None, datetime | None]] = {}
    fi = 0
    funding_sorted = sorted(funding_events, key=lambda x: x["ts"])
    bar_sorted = sorted(bar_timestamps)

    for bar_ts in bar_sorted:
        while fi < len(funding_sorted) and funding_sorted[fi]["ts"] <= bar_ts:
            fi += 1
        if fi > 0:
            f = funding_sorted[fi - 1]
            result[bar_ts] = (f["funding_rate"], f["ts"])
        else:
            result[bar_ts] = (None, None)
    return result
