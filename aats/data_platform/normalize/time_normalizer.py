"""Timestamp normalization utilities.

- Candles ts = bar open timestamp (UTC)
- Funding ts = funding event timestamp (UTC)
"""

from __future__ import annotations

from datetime import datetime, timezone


def ms_to_utc(ms_str: str | int) -> datetime:
    """Convert millisecond epoch (int or string) to UTC datetime."""
    return datetime.fromtimestamp(int(ms_str) / 1000, tz=timezone.utc)


def ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
