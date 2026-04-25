from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import threading
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from aats.services.execution_engine.lifecycle_snapshot_refs import (
    LIFECYCLE_MARKET_CONTEXT_REF_KEYS,
    choose_lifecycle_market_context_refs,
)


_ORDERBOOK_REF_TABLES = (
    "bronze.market_orderbook_books5",
    "bronze.market_orderbook_bbo",
)
_DEFAULT_MAX_DISTANCE_SECONDS = 5.0
_MARKET_CONTEXT_DB_ENV_KEYS = (
    "AATS_MARKET_CONTEXT_DB_URL",
    "AATS_ACTIVE_PARAMETER_DB_URL",
    "RDP_DATABASE_URL",
)
_READ_ONLY_TRANSACTION_OPTION = "-c default_transaction_read_only=on"
_DEFAULT_SOURCE_LOCK = threading.Lock()
_DEFAULT_SOURCE_URL: str | None = None
_DEFAULT_SOURCE: OrderbookSnapshotReadSource | None = None


@dataclass(slots=True)
class OrderbookSnapshotReadSource:
    session_factory: Callable[[], Session]
    source_name: str
    engine: Engine | None = None


def resolve_orderbook_market_context_db_url(
    environ: Mapping[str, str] | None = None,
) -> str | None:
    source = environ if environ is not None else os.environ
    for env_key in _MARKET_CONTEXT_DB_ENV_KEYS:
        value = str(source.get(env_key) or "").strip()
        if value:
            return value
    return None


def default_orderbook_snapshot_read_source() -> OrderbookSnapshotReadSource | None:
    url = resolve_orderbook_market_context_db_url()
    if not url:
        return None
    global _DEFAULT_SOURCE_URL, _DEFAULT_SOURCE
    with _DEFAULT_SOURCE_LOCK:
        if _DEFAULT_SOURCE is not None and _DEFAULT_SOURCE_URL == url:
            return _DEFAULT_SOURCE
        if _DEFAULT_SOURCE is not None and _DEFAULT_SOURCE.engine is not None:
            try:
                _DEFAULT_SOURCE.engine.dispose()
            except Exception:
                pass
        try:
            _DEFAULT_SOURCE = build_orderbook_snapshot_read_source(url)
        except Exception:
            _DEFAULT_SOURCE = None
        _DEFAULT_SOURCE_URL = url
        return _DEFAULT_SOURCE


def build_orderbook_snapshot_read_source(database_url: str) -> OrderbookSnapshotReadSource:
    parsed = make_url(database_url)
    engine_kwargs: dict[str, Any] = {
        "future": True,
        "pool_pre_ping": True,
        "pool_size": 1,
        "max_overflow": 1,
        "pool_timeout": 2,
    }
    if parsed.get_backend_name() == "postgresql":
        engine_kwargs["connect_args"] = {
            "options": _merged_read_only_options(database_url),
        }
    engine = create_engine(database_url, **engine_kwargs)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return OrderbookSnapshotReadSource(
        session_factory=factory,
        source_name=_source_name_from_url(database_url),
        engine=engine,
    )


def reset_default_orderbook_snapshot_read_source_for_tests() -> None:
    global _DEFAULT_SOURCE_URL, _DEFAULT_SOURCE
    with _DEFAULT_SOURCE_LOCK:
        source = _DEFAULT_SOURCE
        _DEFAULT_SOURCE_URL = None
        _DEFAULT_SOURCE = None
    if source is not None and source.engine is not None:
        try:
            source.engine.dispose()
        except Exception:
            pass


def capture_orderbook_snapshot_refs_for_event(
    session: Session,
    *,
    symbol: str | None,
    event_time: datetime | None,
    existing_refs: Mapping[str, Any] | None = None,
    max_distance_seconds: float = _DEFAULT_MAX_DISTANCE_SECONDS,
    market_context_source: OrderbookSnapshotReadSource | None = None,
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

    if market_context_source is not None:
        captured = _capture_refs_from_read_source(
            market_context_source,
            captured_refs=captured,
            symbol=normalized_symbol,
            event_time=normalized_time,
            window_start=window_start,
            window_end=window_end,
        )
        if all(captured.get(key) is not None for key in LIFECYCLE_MARKET_CONTEXT_REF_KEYS):
            return choose_lifecycle_market_context_refs(normalized_existing, captured)

    captured = _capture_refs_from_session(
        session,
        captured_refs=captured,
        symbol=normalized_symbol,
        event_time=normalized_time,
        window_start=window_start,
        window_end=window_end,
        source_name=None,
    )

    return choose_lifecycle_market_context_refs(normalized_existing, captured)


def _capture_refs_from_read_source(
    source: OrderbookSnapshotReadSource,
    *,
    captured_refs: Mapping[str, str | None],
    symbol: str,
    event_time: datetime,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, str | None]:
    try:
        with _open_source_session(source) as source_session:
            return _capture_refs_from_session(
                source_session,
                captured_refs=captured_refs,
                symbol=symbol,
                event_time=event_time,
                window_start=window_start,
                window_end=window_end,
                source_name=source.source_name,
            )
    except Exception:
        return dict(captured_refs)


@contextmanager
def _open_source_session(source: OrderbookSnapshotReadSource):
    session = source.session_factory()
    try:
        yield session
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            close()


def _capture_refs_from_session(
    session: Session,
    *,
    captured_refs: Mapping[str, str | None],
    symbol: str,
    event_time: datetime,
    window_start: datetime,
    window_end: datetime,
    source_name: str | None,
) -> dict[str, str | None]:
    captured = dict(captured_refs)

    for table_name in _ORDERBOOK_REF_TABLES:
        if captured["pre_event_orderbook_snapshot_ref"] is None:
            captured["pre_event_orderbook_snapshot_ref"] = _nearest_orderbook_ref(
                session,
                table_name=table_name,
                source_name=source_name,
                symbol=symbol,
                event_time=event_time,
                window_bound=window_start,
                direction="before",
            )
        if captured["post_event_orderbook_snapshot_ref"] is None:
            captured["post_event_orderbook_snapshot_ref"] = _nearest_orderbook_ref(
                session,
                table_name=table_name,
                source_name=source_name,
                symbol=symbol,
                event_time=event_time,
                window_bound=window_end,
                direction="after",
            )
        if all(captured.get(key) is not None for key in LIFECYCLE_MARKET_CONTEXT_REF_KEYS):
            break

    return captured


def _nearest_orderbook_ref(
    session: Session,
    *,
    table_name: str,
    source_name: str | None,
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
    source_prefix = f"{source_name}." if source_name else ""
    return f"{source_prefix}{table_name}:{symbol}:{_format_snapshot_ts(ts)}"


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


def _merged_read_only_options(database_url: str) -> str:
    try:
        parsed = make_url(database_url)
        existing_options = str(parsed.query.get("options") or "").strip()
    except Exception:
        existing_options = ""
    if "default_transaction_read_only" in existing_options:
        return existing_options
    return f"{existing_options} {_READ_ONLY_TRANSACTION_OPTION}".strip()


def _source_name_from_url(database_url: str) -> str:
    try:
        database = str(make_url(database_url).database or "").strip()
    except Exception:
        database = ""
    return database or "market_context_db"
