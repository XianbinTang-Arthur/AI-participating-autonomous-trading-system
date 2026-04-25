from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.services.execution_engine.lifecycle_snapshot_refs import (
    LIFECYCLE_MARKET_CONTEXT_REF_KEYS,
    choose_lifecycle_market_context_refs,
)


_ORDERBOOK_REF_TABLES = (
    "bronze.market_orderbook_books5",
    "bronze.market_orderbook_bbo",
)
_DEFAULT_MAX_DISTANCE_SECONDS = 5.0


def capture_orderbook_snapshot_refs_for_event(
    session: Session,
    *,
    symbol: str | None,
    event_time: datetime | None,
    existing_refs: Mapping[str, Any] | None = None,
    max_distance_seconds: float = _DEFAULT_MAX_DISTANCE_SECONDS,
) -> dict[str, str | None]:
    """Return lifecycle pre/post orderbook refs from persisted bronze rows.

    This is intentionally read-only and fail-soft: execution persistence must not
    fail just because the RDP/bronze orderbook schema is unavailable.
    """
    normalized_existing = choose_lifecycle_market_context_refs(existing_refs)
    if all(normalized_existing.get(key) is not None for key in LIFECYCLE_MARKET_CONTEXT_REF_KEYS):
        return normalized_existing

    normalized_symbol = str(symbol or "").strip()
    normalized_time = _normalize_event_time(event_time)
    if not normalized_symbol or normalized_time is None:
        return normalized_existing

    window_seconds = max(0.0, float(max_distance_seconds))
    window_start = normalized_time - timedelta(seconds=window_seconds)
    window_end = normalized_time + timedelta(seconds=window_seconds)
    captured: dict[str, str | None] = dict(normalized_existing)

    for table_name in _ORDERBOOK_REF_TABLES:
        if captured["pre_event_orderbook_snapshot_ref"] is None:
            captured["pre_event_orderbook_snapshot_ref"] = _nearest_orderbook_ref(
                session,
                table_name=table_name,
                symbol=normalized_symbol,
                event_time=normalized_time,
                window_bound=window_start,
                direction="before",
            )
        if captured["post_event_orderbook_snapshot_ref"] is None:
            captured["post_event_orderbook_snapshot_ref"] = _nearest_orderbook_ref(
                session,
                table_name=table_name,
                symbol=normalized_symbol,
                event_time=normalized_time,
                window_bound=window_end,
                direction="after",
            )
        if all(captured.get(key) is not None for key in LIFECYCLE_MARKET_CONTEXT_REF_KEYS):
            break

    return choose_lifecycle_market_context_refs(normalized_existing, captured)


def _nearest_orderbook_ref(
    session: Session,
    *,
    table_name: str,
    symbol: str,
    event_time: datetime,
    window_bound: datetime,
    direction: str,
) -> str | None:
    if direction == "before":
        comparator = "ts <= :event_time AND ts >= :window_bound"
        order = "ts DESC"
    elif direction == "after":
        comparator = "ts >= :event_time AND ts <= :window_bound"
        order = "ts ASC"
    else:
        raise ValueError(f"unsupported_orderbook_ref_direction:{direction}")

    try:
        row = _execute_orderbook_ref_query(
            session,
            table_name=table_name,
            comparator=comparator,
            order=order,
            symbol=symbol,
            event_time=event_time,
            window_bound=window_bound,
        )
    except Exception:
        return None
    if not row:
        return None
    ts = row.get("ts")
    if ts is None:
        return None
    return f"{table_name}:{symbol}:{_format_snapshot_ts(ts)}"


def _execute_orderbook_ref_query(
    session: Session,
    *,
    table_name: str,
    comparator: str,
    order: str,
    symbol: str,
    event_time: datetime,
    window_bound: datetime,
) -> Mapping[str, Any] | None:
    statement = text(
        f"""
        SELECT ts
        FROM {table_name}
        WHERE symbol = :symbol
          AND {comparator}
        ORDER BY {order}
        LIMIT 1
        """
    )
    params = {
        "symbol": symbol,
        "event_time": event_time,
        "window_bound": window_bound,
    }
    connection_factory = getattr(session, "connection", None)
    if callable(connection_factory):
        connection = connection_factory()
        begin_nested = getattr(connection, "begin_nested", None)
        execute = getattr(connection, "execute", None)
        if callable(begin_nested) and callable(execute):
            with begin_nested():
                return execute(statement, params).mappings().first()
    return session.execute(statement, params).mappings().first()


def _normalize_event_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_snapshot_ts(value: Any) -> str:
    if isinstance(value, datetime):
        normalized = _normalize_event_time(value)
        assert normalized is not None
        return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return str(value).strip()
