from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
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
_ORDERBOOK_BOOKS5_CONTENT_FIELDS = (
    "bid_px_1",
    "bid_sz_1",
    "bid_px_2",
    "bid_sz_2",
    "bid_px_3",
    "bid_sz_3",
    "bid_px_4",
    "bid_sz_4",
    "bid_px_5",
    "bid_sz_5",
    "ask_px_1",
    "ask_sz_1",
    "ask_px_2",
    "ask_sz_2",
    "ask_px_3",
    "ask_sz_3",
    "ask_px_4",
    "ask_sz_4",
    "ask_px_5",
    "ask_sz_5",
)
_ORDERBOOK_BBO_CONTENT_FIELDS = (
    "bid_px",
    "bid_sz",
    "ask_px",
    "ask_sz",
)
_ORDERBOOK_ROW_CONTENT_FIELDS = {
    "bronze.market_orderbook_books5": _ORDERBOOK_BOOKS5_CONTENT_FIELDS,
    "bronze.market_orderbook_bbo": _ORDERBOOK_BBO_CONTENT_FIELDS,
}
_ORDERBOOK_ROW_AUDIT_FIELDS = (
    "source_ts",
    "ingest_run_id",
    "received_at",
)
_ORDERBOOK_CHECKSUM_VERSION = "orderbook_row_v1"
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


def parse_orderbook_snapshot_ref(ref: Any) -> dict[str, Any]:
    raw_ref = str(ref).strip() if ref is not None else ""
    if not raw_ref:
        return _orderbook_ref_parse_payload(
            raw_ref=None,
            parse_status="missing",
        )

    parts = raw_ref.split(":", 2)
    if len(parts) != 3:
        return _orderbook_ref_parse_payload(
            raw_ref=raw_ref,
            parse_status="unparseable",
        )

    raw_table, symbol, raw_ts = (part.strip() for part in parts)
    table_name: str | None = None
    source_name: str | None = None
    for table_suffix in _ORDERBOOK_REF_TABLES:
        if raw_table == table_suffix:
            table_name = table_suffix
            break
        qualified_suffix = f".{table_suffix}"
        if raw_table.endswith(qualified_suffix):
            table_name = table_suffix
            source_name = raw_table[: -len(qualified_suffix)] or None
            break

    parsed_ts = _parse_ref_timestamp(raw_ts)
    parse_status = "parsed"
    if table_name is None:
        parse_status = "unsupported_table"
    elif not symbol:
        parse_status = "missing_symbol"
    elif parsed_ts is None:
        parse_status = "unparseable_ts"

    return _orderbook_ref_parse_payload(
        raw_ref=raw_ref,
        parse_status=parse_status,
        source_name=source_name,
        table_name=table_name,
        symbol=symbol or None,
        ts=parsed_ts,
    )


def resolve_orderbook_snapshot_ref_row(
    ref: Any,
    *,
    expected_symbol: str | None = None,
    market_context_source: OrderbookSnapshotReadSource | None = None,
    use_default_source: bool = True,
) -> dict[str, Any]:
    payload = parse_orderbook_snapshot_ref(ref)
    payload.update(
        {
            "row_lookup_status": None,
            "row_exists": False,
            "source_ts": None,
            "received_at": None,
            "ingest_run_id": None,
            "content_checksum": None,
            "checksum_source": None,
            "checksum_version": _ORDERBOOK_CHECKSUM_VERSION,
            "sequence_key": None,
            "missing_evidence": [],
        }
    )

    missing_evidence: list[str] = []
    if payload["parse_status"] != "parsed":
        missing_evidence.append(f"ref_{payload['parse_status']}")
        payload["row_lookup_status"] = payload["parse_status"]
        payload["missing_evidence"] = missing_evidence
        return payload

    expected_symbol_text = str(expected_symbol or "").strip()
    if expected_symbol_text and payload.get("symbol") != expected_symbol_text:
        missing_evidence.append("snapshot_ref_symbol_mismatch")
        payload["row_lookup_status"] = "symbol_mismatch"
        payload["missing_evidence"] = missing_evidence
        return payload

    source = market_context_source or (default_orderbook_snapshot_read_source() if use_default_source else None)
    if source is None:
        missing_evidence.append("orderbook_row_truth_source_unavailable")
        payload["row_lookup_status"] = "source_unavailable"
        payload["missing_evidence"] = missing_evidence
        return payload

    try:
        with _open_source_session(source) as session:
            row = _execute_orderbook_row_lookup(
                session,
                table_name=str(payload["table_name"]),
                symbol=str(payload["symbol"]),
                ts=parse_orderbook_ref_ts_for_query(payload["ts"]),
            )
    except Exception:
        missing_evidence.append("orderbook_row_truth_source_unavailable")
        payload["row_lookup_status"] = "source_unavailable"
        payload["missing_evidence"] = missing_evidence
        return payload

    if not row:
        missing_evidence.append("orderbook_row_missing")
        payload["row_lookup_status"] = "row_missing"
        payload["missing_evidence"] = missing_evidence
        return payload

    normalized_row = dict(row)
    source_ts = normalized_row.get("source_ts")
    received_at = normalized_row.get("received_at")
    ingest_run_id = normalized_row.get("ingest_run_id")
    content_checksum = _orderbook_row_checksum(str(payload["table_name"]), normalized_row)
    payload.update(
        {
            "row_lookup_status": "row_resolved",
            "row_exists": True,
            "source_ts": _format_snapshot_ts(source_ts) if source_ts is not None else None,
            "received_at": _format_snapshot_ts(received_at) if received_at is not None else None,
            "ingest_run_id": str(ingest_run_id) if ingest_run_id is not None else None,
            "content_checksum": content_checksum,
            "checksum_source": "computed_from_flattened_row",
            "sequence_key": {
                "table_name": payload["table_name"],
                "symbol": payload["symbol"],
                "source_ts": _format_snapshot_ts(source_ts) if source_ts is not None else None,
                "ts": payload["ts"],
                "content_checksum": content_checksum,
            },
            "missing_evidence": [],
        }
    )
    return payload


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


def _execute_orderbook_row_lookup(
    session: Session,
    *,
    table_name: str,
    symbol: str,
    ts: datetime | None,
) -> Mapping[str, Any] | None:
    fields = _ORDERBOOK_ROW_CONTENT_FIELDS.get(table_name)
    if fields is None or ts is None:
        return None
    select_fields = ", ".join(
        [
            "symbol",
            "ts",
            *_ORDERBOOK_ROW_AUDIT_FIELDS,
            *fields,
        ]
    )
    statement = text(
        f"""
        SELECT {select_fields}
        FROM {table_name}
        WHERE symbol = :symbol
          AND ts = :ts
        LIMIT 1
        """
    )
    params = {
        "symbol": symbol,
        "ts": ts,
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


def parse_orderbook_ref_ts_for_query(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _normalize_event_time(value)
    return _parse_ref_timestamp(value)


def _parse_ref_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _normalize_event_time(parsed)


def _format_snapshot_ts(value: Any) -> str:
    if isinstance(value, datetime):
        normalized = _normalize_event_time(value)
        assert normalized is not None
        return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return str(value).strip()


def _orderbook_ref_parse_payload(
    *,
    raw_ref: str | None,
    parse_status: str,
    source_name: str | None = None,
    table_name: str | None = None,
    symbol: str | None = None,
    ts: datetime | None = None,
) -> dict[str, Any]:
    return {
        "raw_ref": raw_ref,
        "parse_status": parse_status,
        "source_name": source_name,
        "table_name": table_name,
        "symbol": symbol,
        "ts": _format_snapshot_ts(ts) if ts is not None else None,
    }


def _orderbook_row_checksum(table_name: str, row: Mapping[str, Any]) -> str:
    fields = _ORDERBOOK_ROW_CONTENT_FIELDS[table_name]
    payload = {
        "checksum_version": _ORDERBOOK_CHECKSUM_VERSION,
        "table_name": table_name,
        "symbol": _canonical_checksum_value(row.get("symbol")),
        "ts": _canonical_checksum_value(row.get("ts")),
        "source_ts": _canonical_checksum_value(row.get("source_ts")),
        "fields": {
            field: _canonical_checksum_value(row.get(field))
            for field in fields
        },
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


def _canonical_checksum_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _format_snapshot_ts(value)
    if isinstance(value, Decimal):
        if value.is_zero():
            return "0"
        return format(value.normalize(), "f")
    return str(value)


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
