"""Official OKX trade, L2 and mark-price historical importers.

The adapters preserve source identity and raw-partition hashes. They never
write live-capture tables and all windows use ``[start, end)`` UTC semantics.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import stat
import tarfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

import httpx
from sqlalchemy import text

from aats.data_platform.governance._atomic_io import (
    _fsync_directory,
    immutable_bytes_write,
)


TRADE_HISTORY_PATH = "/api/v5/market/history-trades"
_TRADE_FILE_MAX_EDGE_GAP = timedelta(minutes=5)
MARK_HISTORY_PATH = "/api/v5/market/history-mark-price-candles"
_UTC = timezone.utc
_DATABASE_BATCH_SIZE = 5_000
_MAX_BUFFERED_JSON_BYTES = 256 * 1024 * 1024
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,63}$")


@dataclass(frozen=True)
class ImportStats:
    source_kind: str
    symbol: str
    start: datetime
    end: datetime
    pages_or_files: int
    rows_read: int
    rows_written: int
    raw_sha256: tuple[str, ...]
    gaps: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class L2Event:
    symbol: str
    ts: datetime
    action: str
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    sequence_id: int | None = None
    previous_sequence_id: int | None = None
    checksum: str | None = None


@dataclass(frozen=True)
class ResampledBook:
    symbol: str
    ts: datetime
    source_state_ts: datetime
    staleness_ms: int
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]


def register_official_source(
    session,
    *,
    source_key: str,
    source_kind: str,
    source_locator: str,
    timestamp_semantics: str,
    schema_version: str = "okx-v5",
) -> str:
    if source_kind not in {"okx_rest", "okx_bulk", "proxy"}:
        raise ValueError("official_source_kind_invalid")
    truth_tier = "proxy" if source_kind == "proxy" else "authoritative_external"
    value = session.execute(
        text(
            """
            INSERT INTO meta.data_source_registry (
                source_key, source_kind, provider, source_locator,
                schema_version, timestamp_semantics, truth_tier,
                license_usage_note, source_metadata
            ) VALUES (
                :source_key, :source_kind, 'OKX', :source_locator,
                :schema_version, :timestamp_semantics, :truth_tier,
                'OKX public historical market data; retain source terms and provenance',
                '{}'::jsonb
            )
            ON CONFLICT (source_key) DO UPDATE SET
                source_key = EXCLUDED.source_key
            WHERE meta.data_source_registry.source_kind = EXCLUDED.source_kind
              AND meta.data_source_registry.provider = EXCLUDED.provider
              AND meta.data_source_registry.source_locator = EXCLUDED.source_locator
              AND meta.data_source_registry.schema_version = EXCLUDED.schema_version
              AND meta.data_source_registry.timestamp_semantics = EXCLUDED.timestamp_semantics
              AND meta.data_source_registry.truth_tier = EXCLUDED.truth_tier
            RETURNING source_id
            """
        ),
        {
            "source_key": source_key,
            "source_kind": source_kind,
            "source_locator": source_locator,
            "schema_version": schema_version,
            "timestamp_semantics": timestamp_semantics,
            "truth_tier": truth_tier,
        },
    ).scalar_one_or_none()
    if value is None:
        raise RuntimeError("official_source_registry_immutable_conflict")
    return str(value)


def import_trade_rest(
    session,
    *,
    client: httpx.Client,
    base_url: str,
    symbol: str,
    start: datetime,
    end: datetime,
    source_id: str,
    ingest_run_id: str,
    raw_archive_dir: Path,
    max_pages: int = 10_000,
    request_interval_seconds: float = 0.11,
) -> ImportStats:
    _validate_window(start, end)
    symbol = _validated_symbol(symbol)
    _validate_pagination(max_pages, request_interval_seconds)
    cursor = int(end.timestamp() * 1000)
    seen_cursors: set[int] = set()
    pages = rows_read = rows_written = 0
    hashes: list[str] = []
    gaps: list[dict[str, Any]] = []
    completed = False
    start_ms = int(start.timestamp() * 1000)
    while pages < max_pages and cursor >= int(start.timestamp() * 1000):
        if cursor in seen_cursors:
            raise RuntimeError("trade_history_pagination_stalled")
        seen_cursors.add(cursor)
        body, raw = _get_okx_page(
            client,
            f"{base_url.rstrip('/')}{TRADE_HISTORY_PATH}",
            {"instId": symbol, "type": "2", "after": str(cursor), "limit": "100"},
        )
        data = body.get("data")
        if not isinstance(data, list):
            raise RuntimeError("trade_history_data_not_list")
        pages += 1
        digest = _archive_raw_page(
            raw_archive_dir,
            f"trade_{symbol}_{pages:06d}_{cursor}.json",
            raw,
        )
        hashes.append(digest)
        valid = []
        for item in data:
            row = _parse_trade(item, symbol)
            if row is None:
                raise ValueError("trade_history_row_invalid")
            if row["symbol"] != symbol:
                raise ValueError("trade_history_symbol_mismatch")
            valid.append(row)
        in_window = [row for row in valid if start <= row["ts"] < end]
        rows_read += len(in_window)
        rows_written += _write_trade_rows(
            session,
            in_window,
            source_id=source_id,
            ingest_run_id=ingest_run_id,
            raw_sha256=digest,
        )
        if not valid:
            gaps.append(
                {
                    "reason": "official_trade_history_coverage_unproven",
                    "gap_start": start.isoformat(),
                    "gap_end": end.isoformat(),
                }
            )
            completed = True
            break
        oldest_ms = min(int(row["ts"].timestamp() * 1000) for row in valid)
        if len(data) < 100:
            if oldest_ms > start_ms:
                gaps.append(
                    {
                        "reason": "official_trade_history_coverage_unproven",
                        "gap_start": start.isoformat(),
                        "gap_end": min(
                            end,
                            datetime.fromtimestamp(oldest_ms / 1000, tz=_UTC),
                        ).isoformat(),
                    }
                )
            completed = True
            break
        if oldest_ms < start_ms:
            completed = True
            break
        cursor = oldest_ms - 1
        time.sleep(request_interval_seconds)
    if not completed and cursor >= int(start.timestamp() * 1000):
        raise RuntimeError("trade_history_max_pages_exceeded")
    return ImportStats(
        source_kind="okx_rest",
        symbol=symbol,
        start=start,
        end=end,
        pages_or_files=pages,
        rows_read=rows_read,
        rows_written=rows_written,
        raw_sha256=tuple(hashes),
        gaps=tuple(gaps),
    )


def import_mark_price_rest(
    session,
    *,
    client: httpx.Client,
    base_url: str,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    source_id: str,
    ingest_run_id: str,
    raw_archive_dir: Path,
    max_pages: int = 1_000,
    request_interval_seconds: float = 0.11,
) -> ImportStats:
    if timeframe not in {"15m", "1H"}:
        raise ValueError("mark_proxy_timeframe_must_be_15m_or_1H")
    _validate_window(start, end)
    symbol = _validated_symbol(symbol)
    _validate_pagination(max_pages, request_interval_seconds)
    alignment_seconds = 900 if timeframe == "15m" else 3600
    if (
        int(start.timestamp()) % alignment_seconds
        or int(end.timestamp()) % alignment_seconds
    ):
        raise ValueError("mark_proxy_window_must_align_to_timeframe")
    cursor = int(end.timestamp() * 1000)
    pages = rows_read = rows_written = 0
    hashes: list[str] = []
    observed_timestamps: set[datetime] = set()
    previous_cursor: int | None = None
    completed = False
    table = "bronze.market_mark_price_candles_15m" if timeframe == "15m" else "bronze.market_mark_price_candles_1h"
    while pages < max_pages and cursor >= int(start.timestamp() * 1000):
        if previous_cursor == cursor:
            raise RuntimeError("mark_history_pagination_stalled")
        previous_cursor = cursor
        body, raw = _get_okx_page(
            client,
            f"{base_url.rstrip('/')}{MARK_HISTORY_PATH}",
            {"instId": symbol, "bar": timeframe, "after": str(cursor), "limit": "100"},
        )
        data = body.get("data")
        if not isinstance(data, list):
            raise RuntimeError("mark_history_data_not_list")
        pages += 1
        digest = _archive_raw_page(
            raw_archive_dir,
            f"mark_{timeframe}_{symbol}_{pages:05d}_{cursor}.json",
            raw,
        )
        hashes.append(digest)
        parsed = [_parse_mark(item, symbol) for item in data]
        if any(row is None for row in parsed):
            raise ValueError("mark_history_row_invalid")
        valid = [row for row in parsed if row is not None]
        confirmed = [row for row in valid if row["confirm"]]
        in_window = [
            row for row in confirmed
            if start <= row["ts"] < end
            and int(row["ts"].timestamp()) % alignment_seconds == 0
        ]
        rows_read += len(in_window)
        observed_timestamps.update(row["ts"] for row in in_window)
        rows_written += _write_mark_rows(
            session,
            table,
            in_window,
            source_id=source_id,
            ingest_run_id=ingest_run_id,
            raw_sha256=digest,
        )
        if not valid or len(data) < 100:
            completed = True
            break
        oldest_ms = min(int(row["ts"].timestamp() * 1000) for row in valid)
        if oldest_ms < int(start.timestamp() * 1000):
            completed = True
            break
        cursor = oldest_ms - 1
        time.sleep(request_interval_seconds)
    if not completed and cursor >= int(start.timestamp() * 1000):
        raise RuntimeError("mark_history_max_pages_exceeded")
    gaps = _missing_bar_ranges(
        start=start,
        end=end,
        interval_seconds=alignment_seconds,
        observed=observed_timestamps,
    )
    return ImportStats(
        source_kind="proxy",
        symbol=symbol,
        start=start,
        end=end,
        pages_or_files=pages,
        rows_read=rows_read,
        rows_written=rows_written,
        raw_sha256=tuple(hashes),
        gaps=tuple(gaps),
    )


def import_trade_file(
    session,
    *,
    path: Path,
    symbol: str,
    start: datetime,
    end: datetime,
    source_id: str,
    ingest_run_id: str,
    raw_archive_dir: Path,
) -> ImportStats:
    _validate_window(start, end)
    symbol = _validated_symbol(symbol)
    archived_path, digest = _archive_source_file(
        path,
        raw_archive_dir,
        prefix="official_trade",
    )

    rows_read = 0
    earliest_ts: datetime | None = None
    latest_ts: datetime | None = None

    def rows() -> Iterator[dict[str, Any]]:
        nonlocal earliest_ts, latest_ts, rows_read
        for item in _iter_records(archived_path):
            parsed = _parse_trade(item, symbol)
            if parsed is None:
                raise ValueError("trade_history_row_invalid")
            if parsed["symbol"] != symbol:
                raise ValueError("trade_history_symbol_mismatch")
            if start <= parsed["ts"] < end:
                rows_read += 1
                earliest_ts = (
                    parsed["ts"]
                    if earliest_ts is None
                    else min(earliest_ts, parsed["ts"])
                )
                latest_ts = (
                    parsed["ts"]
                    if latest_ts is None
                    else max(latest_ts, parsed["ts"])
                )
                yield parsed

    written = _write_trade_rows(
        session,
        rows(),
        source_id=source_id,
        ingest_run_id=ingest_run_id,
        raw_sha256=digest,
    )
    gaps = _trade_file_edge_gaps(
        start=start,
        end=end,
        earliest_ts=earliest_ts,
        latest_ts=latest_ts,
    )
    return ImportStats(
        source_kind="okx_bulk",
        symbol=symbol,
        start=start,
        end=end,
        pages_or_files=1,
        rows_read=rows_read,
        rows_written=written,
        raw_sha256=(digest,),
        gaps=tuple(gaps),
    )


def _trade_file_edge_gaps(
    *,
    start: datetime,
    end: datetime,
    earliest_ts: datetime | None,
    latest_ts: datetime | None,
) -> list[dict[str, str]]:
    """Fail closed when an operator-supplied file does not span its UTC window.

    OKX bulk files use an exchange-local calendar boundary while this importer
    accepts UTC half-open windows.  Row filtering alone therefore cannot prove
    that one daily file covers both UTC edges.  A short tolerance permits the
    normal absence of a trade exactly at midnight without treating a visibly
    truncated file as complete.
    """

    reason = "official_trade_history_coverage_unproven"
    if earliest_ts is None or latest_ts is None:
        return [
            {
                "reason": reason,
                "gap_start": start.isoformat(),
                "gap_end": end.isoformat(),
            }
        ]

    gaps: list[dict[str, str]] = []
    if earliest_ts - start > _TRADE_FILE_MAX_EDGE_GAP:
        gaps.append(
            {
                "reason": reason,
                "gap_start": start.isoformat(),
                "gap_end": earliest_ts.isoformat(),
            }
        )
    if end - latest_ts > _TRADE_FILE_MAX_EDGE_GAP:
        gaps.append(
            {
                "reason": reason,
                "gap_start": latest_ts.isoformat(),
                "gap_end": end.isoformat(),
            }
        )
    return gaps


def import_l2_file(
    session,
    *,
    path: Path,
    symbol: str,
    start: datetime,
    end: datetime,
    source_id: str,
    ingest_run_id: str,
    raw_archive_dir: Path,
) -> ImportStats:
    _validate_window(start, end)
    symbol = _validated_symbol(symbol)
    archived_path, digest = _archive_source_file(
        path,
        raw_archive_dir,
        prefix="official_l2",
    )
    rows_read = 0
    written = 0
    previous_sequence: int | None = None
    previous_event_ts: datetime | None = None
    out_of_order_reported = False
    gaps: list[dict[str, Any]] = []
    statement = text(
        """
        INSERT INTO staging.official_l2_history (
            source_id, symbol, ts, sequence_id, previous_sequence_id,
            action, bids, asks, checksum, source_row_hash,
            raw_partition_sha256, ingest_run_id
        ) VALUES (
            CAST(:source_id AS UUID), :symbol, :ts, :sequence_id,
            :previous_sequence_id, :action, CAST(:bids AS jsonb),
            CAST(:asks AS jsonb), :checksum, :source_row_hash,
            :raw_partition_sha256, CAST(:ingest_run_id AS UUID)
        ) ON CONFLICT ON CONSTRAINT uq_stg_official_l2_row DO NOTHING
        """
    )
    payloads: list[dict[str, Any]] = []
    for record in _iter_records(archived_path):
        event = _parse_l2(record, symbol)
        if event is None:
            raise ValueError("l2_history_row_invalid")
        if event.symbol != symbol:
            raise ValueError("l2_history_symbol_mismatch")
        if not (start <= event.ts < end):
            continue
        if previous_event_ts is not None and event.ts < previous_event_ts:
            if not out_of_order_reported:
                gaps.append(
                    {
                        "reason": "source_event_order_not_chronological",
                        "at": event.ts.isoformat(),
                        "previous_event_ts": previous_event_ts.isoformat(),
                    }
                )
                out_of_order_reported = True
            previous_sequence = None
        if (
            previous_sequence is not None
            and event.previous_sequence_id is not None
            and event.previous_sequence_id != previous_sequence
        ):
            gaps.append(
                {
                    "reason": "sequence_discontinuity",
                    "at": event.ts.isoformat(),
                    "expected_previous": previous_sequence,
                    "actual_previous": event.previous_sequence_id,
                }
            )
        if event.sequence_id is not None:
            previous_sequence = event.sequence_id
        previous_event_ts = event.ts
        rows_read += 1
        payload = _l2_payload(event)
        row_hash = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                default=_json_default,
            ).encode()
        ).hexdigest()
        payloads.append(
            {
                **payload,
                "source_id": source_id,
                "source_row_hash": row_hash,
                "raw_partition_sha256": digest,
                "ingest_run_id": ingest_run_id,
                "bids": json.dumps(payload["bids"], separators=(",", ":")),
                "asks": json.dumps(payload["asks"], separators=(",", ":")),
            }
        )
        if len(payloads) >= _DATABASE_BATCH_SIZE:
            result = session.execute(statement, payloads)
            written += max(int(result.rowcount or 0), 0)
            payloads = []
    if payloads:
        result = session.execute(statement, payloads)
        written += max(int(result.rowcount or 0), 0)
    stats = ImportStats(
        source_kind="okx_bulk",
        symbol=symbol,
        start=start,
        end=end,
        pages_or_files=1,
        rows_read=rows_read,
        rows_written=written,
        raw_sha256=(digest,),
        gaps=tuple(gaps),
    )
    return stats


def iter_l2_history(
    session,
    *,
    source_id: str,
    symbol: str,
    start: datetime,
    end: datetime,
    fetch_size: int = 10_000,
) -> Iterator[L2Event]:
    """Stream normalized L2 events in causal order from staging."""

    _validate_window(start, end)
    if fetch_size <= 0:
        raise ValueError("l2_history_fetch_size_must_be_positive")
    statement = text(
            "SELECT symbol, ts, action, bids, asks, sequence_id, "
            "previous_sequence_id, checksum FROM staging.official_l2_history "
            "WHERE source_id = CAST(:source_id AS UUID) AND symbol = :symbol "
            "AND ts >= :start AND ts < :end "
            "ORDER BY ts ASC, sequence_id ASC NULLS FIRST, id ASC"
        ).execution_options(stream_results=True, yield_per=fetch_size)
    result = session.execute(
        statement,
        {
            "source_id": source_id,
            "symbol": symbol,
            "start": start,
            "end": end,
        },
    )
    rows = result.mappings()
    try:
        while True:
            batch = rows.fetchmany(fetch_size)
            if not batch:
                break
            for row in batch:
                yield L2Event(
                    symbol=str(row["symbol"]),
                    ts=row["ts"],
                    action=str(row["action"]),
                    bids=tuple(_levels(row["bids"])),
                    asks=tuple(_levels(row["asks"])),
                    sequence_id=_optional_int(row["sequence_id"]),
                    previous_sequence_id=_optional_int(row["previous_sequence_id"]),
                    checksum=(
                        str(row["checksum"])
                        if row["checksum"] is not None
                        else None
                    ),
                )
    finally:
        result.close()


def causal_resample_l2(
    events: Sequence[L2Event],
    *,
    start: datetime,
    end: datetime,
    interval_ms: int,
    max_staleness_ms: int,
) -> tuple[list[ResampledBook], list[dict[str, Any]]]:
    """Sample only the latest state visible at or before each sample instant."""

    ordered = sorted(events, key=lambda event: (event.ts, event.sequence_id or -1))
    return causal_resample_l2_ordered(
        ordered,
        start=start,
        end=end,
        interval_ms=interval_ms,
        max_staleness_ms=max_staleness_ms,
    )


def causal_resample_l2_ordered(
    events: Iterable[L2Event],
    *,
    start: datetime,
    end: datetime,
    interval_ms: int,
    max_staleness_ms: int,
) -> tuple[list[ResampledBook], list[dict[str, Any]]]:
    """Causally resample an already time-ordered, streaming L2 event source."""

    _validate_window(start, end)
    if interval_ms <= 0 or max_staleness_ms < 0:
        raise ValueError("resample_interval_or_staleness_invalid")
    iterator = iter(events)
    pending = next(iterator, None)
    bids: dict[Decimal, Decimal] = {}
    asks: dict[Decimal, Decimal] = {}
    state_ts: datetime | None = None
    state_symbol: str | None = None
    has_snapshot = False
    previous_sequence: int | None = None
    previous_event_ts: datetime | None = None
    output: list[ResampledBook] = []
    gaps: list[dict[str, Any]] = []
    sample = start
    step = timedelta(milliseconds=interval_ms)
    while sample < end:
        while pending is not None and pending.ts <= sample:
            event = pending
            if previous_event_ts is not None and event.ts < previous_event_ts:
                raise ValueError("l2_events_not_time_ordered")
            if state_symbol is not None and event.symbol != state_symbol:
                raise ValueError("l2_stream_contains_multiple_symbols")
            state_symbol = event.symbol
            sequence_gap = (
                previous_sequence is not None
                and event.previous_sequence_id is not None
                and event.previous_sequence_id != previous_sequence
            )
            if sequence_gap:
                bids.clear()
                asks.clear()
                has_snapshot = False
                state_ts = None
            if event.action == "snapshot":
                bids.clear()
                asks.clear()
                has_snapshot = True
            _apply_levels(bids, event.bids)
            _apply_levels(asks, event.asks)
            if has_snapshot:
                state_ts = event.ts
            if event.sequence_id is not None:
                previous_sequence = event.sequence_id
            previous_event_ts = event.ts
            pending = next(iterator, None)
        if not has_snapshot or state_ts is None or not bids or not asks:
            _append_sample_gap(
                gaps,
                sample=sample,
                step=step,
                reason="state_unavailable",
            )
        else:
            staleness = int((sample - state_ts).total_seconds() * 1000)
            if staleness > max_staleness_ms:
                _append_sample_gap(
                    gaps,
                    sample=sample,
                    step=step,
                    reason="state_stale",
                    staleness_ms=staleness,
                )
            elif max(bids) >= min(asks):
                _append_sample_gap(
                    gaps,
                    sample=sample,
                    step=step,
                    reason="state_crossed_book",
                )
            else:
                output.append(
                    ResampledBook(
                        symbol=state_symbol or "",
                        ts=sample,
                        source_state_ts=state_ts,
                        staleness_ms=staleness,
                        bids=tuple(sorted(bids.items(), reverse=True)[:5]),
                        asks=tuple(sorted(asks.items())[:5]),
                    )
                )
        sample += step
    return output, gaps


def persist_resampled_l2(
    session,
    *,
    bundle_id: str,
    bbo_rows: Sequence[ResampledBook],
    books5_rows: Sequence[ResampledBook],
    transform_version: str,
) -> tuple[int, int]:
    bbo_statement = text(
        """
        INSERT INTO bronze.historical_orderbook_bbo_1hz (
            bundle_id, symbol, ts, source_state_ts, staleness_ms,
            bid_px, bid_sz, ask_px, ask_sz, transform_version
        ) VALUES (
            CAST(:bundle_id AS UUID), :symbol, :ts, :source_state_ts,
            :staleness_ms, :bid_px, :bid_sz, :ask_px, :ask_sz,
            :transform_version
        ) ON CONFLICT (bundle_id, symbol, ts) DO NOTHING
        """
    )
    bbo_payloads = (
        {
                "bundle_id": bundle_id,
                "symbol": row.symbol,
                "ts": row.ts,
                "source_state_ts": row.source_state_ts,
                "staleness_ms": row.staleness_ms,
                "bid_px": row.bids[0][0],
                "bid_sz": row.bids[0][1],
                "ask_px": row.asks[0][0],
                "ask_sz": row.asks[0][1],
                "transform_version": transform_version,
        }
        for row in bbo_rows
    )
    bbo_written = _execute_batched(session, bbo_statement, bbo_payloads)

    books_statement = text(
        """
        INSERT INTO bronze.historical_orderbook_books5_2hz (
            bundle_id, symbol, ts, source_state_ts, staleness_ms,
            bids, asks, transform_version
        ) VALUES (
            CAST(:bundle_id AS UUID), :symbol, :ts, :source_state_ts,
            :staleness_ms, CAST(:bids AS jsonb), CAST(:asks AS jsonb),
            :transform_version
        ) ON CONFLICT (bundle_id, symbol, ts) DO NOTHING
        """
    )
    books_payloads = (
        {
                "bundle_id": bundle_id,
                "symbol": row.symbol,
                "ts": row.ts,
                "source_state_ts": row.source_state_ts,
                "staleness_ms": row.staleness_ms,
                "bids": json.dumps([[str(px), str(sz)] for px, sz in row.bids]),
                "asks": json.dumps([[str(px), str(sz)] for px, sz in row.asks]),
                "transform_version": transform_version,
        }
        for row in books5_rows
    )
    books_written = _execute_batched(session, books_statement, books_payloads)
    return bbo_written, books_written


def _get_okx_page(client: httpx.Client, url: str, params: Mapping[str, str]) -> tuple[dict[str, Any], bytes]:
    backoff = 1.0
    for attempt in range(6):
        try:
            response = client.get(url, params=params, timeout=20.0)
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            if attempt == 5:
                raise RuntimeError(f"okx_request_failed:{type(exc).__name__}") from exc
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            continue
        code = str(body.get("code", "")) if isinstance(body, dict) else ""
        if response.status_code == 200 and code == "0":
            raw = (json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n").encode()
            return body, raw
        if response.status_code == 429 or code == "50011" or response.status_code >= 500:
            if attempt == 5:
                raise RuntimeError(f"okx_retry_exhausted:http={response.status_code}:code={code}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            continue
        raise RuntimeError(f"okx_request_rejected:http={response.status_code}:code={code}")
    raise AssertionError("unreachable")


def _archive_raw_page(directory: Path, filename: str, raw: bytes) -> str:
    expanded = directory.expanduser()
    if not expanded.is_absolute():
        raise ValueError("raw_archive_dir_must_be_absolute")
    if Path(filename).name != filename or filename in {"", ".", ".."}:
        raise ValueError("raw_archive_filename_invalid")
    directory = expanded.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    digest = hashlib.sha256(raw).hexdigest()
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError("raw_archive_existing_checksum_mismatch")
        return digest
    return immutable_bytes_write(raw, path)


def archive_raw_response_page(directory: Path, filename: str, raw: bytes) -> str:
    """Public immutable raw-response archive entrypoint for legacy importers."""

    return _archive_raw_page(directory, filename, raw)


def _parse_trade(item: Any, symbol: str) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    try:
        ts = _timestamp(item, ("ts", "timestamp", "created_time", "time"))
        trade_id = _field(item, ("tradeId", "trade_id", "id"))
        px = Decimal(_field(item, ("px", "price")))
        sz = Decimal(_field(item, ("sz", "size", "amount")))
        side = _field(item, ("side",)).lower()
    except (KeyError, ValueError, InvalidOperation, OSError):
        return None
    if (
        side not in {"buy", "sell"}
        or not px.is_finite()
        or not sz.is_finite()
        or px <= 0
        or sz <= 0
    ):
        return None
    return {
        "symbol": str(item.get("instId") or item.get("symbol") or symbol).upper(),
        "ts": ts,
        "trade_id": trade_id,
        "px": px,
        "sz": sz,
        "side": side,
        "source_order_type": item.get("source"),
        "raw_payload": dict(item),
    }


def _parse_mark(item: Any, symbol: str) -> dict[str, Any] | None:
    if not isinstance(item, list) or len(item) < 6:
        return None
    try:
        ts = datetime.fromtimestamp(int(item[0]) / 1000, tz=_UTC)
        prices = [Decimal(str(value)) for value in item[1:5]]
    except (ValueError, InvalidOperation, OSError):
        return None
    confirm_raw = str(item[5]).lower()
    if confirm_raw not in {"0", "1", "false", "true"} or any(
        not value.is_finite() or value <= 0 for value in prices
    ):
        return None
    open_price, high_price, low_price, close_price = prices
    if high_price < max(open_price, low_price, close_price) or low_price > min(
        open_price,
        close_price,
    ):
        return None
    return {
        "symbol": symbol,
        "ts": ts,
        "open": prices[0],
        "high": prices[1],
        "low": prices[2],
        "close": prices[3],
        "confirm": confirm_raw in {"1", "true"},
    }


def _parse_l2(item: Any, symbol: str) -> L2Event | None:
    if not isinstance(item, Mapping):
        return None
    try:
        ts = _timestamp(item, ("ts", "timestamp", "exchange_timestamp"))
        bids = _levels(item.get("bids"))
        asks = _levels(item.get("asks"))
        action = str(item.get("action") or item.get("type") or "snapshot").lower()
    except (ValueError, KeyError, InvalidOperation, OSError, TypeError):
        return None
    if action not in {"snapshot", "update"} or (not bids and not asks):
        return None
    return L2Event(
        symbol=str(item.get("instId") or item.get("symbol") or symbol).upper(),
        ts=ts,
        action=action,
        bids=tuple(bids),
        asks=tuple(asks),
        sequence_id=_optional_int(_first_present(item, "seqId", "sequence_id")),
        previous_sequence_id=_optional_int(
            _first_present(item, "prevSeqId", "previous_sequence_id")
        ),
        checksum=str(item["checksum"]) if item.get("checksum") is not None else None,
    )


def _write_trade_rows(session, rows: Iterable[dict[str, Any]], *, source_id: str, ingest_run_id: str, raw_sha256: str) -> int:
    statement = text(
        """
        INSERT INTO staging.official_trade_history (
            source_id, symbol, ts, trade_id, px, sz, side,
            source_order_type, raw_payload, raw_partition_sha256,
            ingest_run_id
        ) VALUES (
            CAST(:source_id AS UUID), :symbol, :ts, :trade_id,
            :px, :sz, :side, :source_order_type,
            CAST(:raw_payload AS jsonb), :raw_partition_sha256,
            CAST(:ingest_run_id AS UUID)
        ) ON CONFLICT (source_id, symbol, ts, trade_id) DO NOTHING
        """
    )
    written = 0
    payloads = (
        {
                **row,
                "source_id": source_id,
                "ingest_run_id": ingest_run_id,
                "raw_partition_sha256": raw_sha256,
                "raw_payload": json.dumps(row["raw_payload"], sort_keys=True),
        }
        for row in rows
    )
    for batch in _batches(payloads):
        result = session.execute(statement, batch)
        written += max(int(result.rowcount or 0), 0)
    return written


def _write_mark_rows(session, table: str, rows: Iterable[dict[str, Any]], *, source_id: str, ingest_run_id: str, raw_sha256: str) -> int:
    if table not in {"bronze.market_mark_price_candles_15m", "bronze.market_mark_price_candles_1h"}:
        raise ValueError("mark_proxy_table_not_allowlisted")
    statement = text(
        f"""
        INSERT INTO {table} (
            source_id, symbol, ts, open, high, low, close, confirm,
            raw_partition_sha256, ingest_run_id
        ) VALUES (
            CAST(:source_id AS UUID), :symbol, :ts, :open, :high,
            :low, :close, :confirm, :raw_partition_sha256,
            CAST(:ingest_run_id AS UUID)
        ) ON CONFLICT (source_id, symbol, ts) DO NOTHING
        """
    )
    written = 0
    payloads = (
        {
                **row,
                "source_id": source_id,
                "ingest_run_id": ingest_run_id,
                "raw_partition_sha256": raw_sha256,
        }
        for row in rows
    )
    for batch in _batches(payloads):
        result = session.execute(statement, batch)
        written += max(int(result.rowcount or 0), 0)
    return written


def _batches(
    rows: Iterable[dict[str, Any]],
    *,
    size: int = _DATABASE_BATCH_SIZE,
) -> Iterator[list[dict[str, Any]]]:
    if size <= 0:
        raise ValueError("database_batch_size_must_be_positive")
    batch: list[dict[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _execute_batched(session, statement, rows: Iterable[dict[str, Any]]) -> int:
    written = 0
    for batch in _batches(rows):
        result = session.execute(statement, batch)
        written += max(int(result.rowcount or 0), 0)
    return written


def _iter_records(path: Path) -> Iterator[dict[str, Any]]:
    archive_suffix = _official_source_suffix(path)
    supported_members = (".csv", ".json", ".jsonl", ".ndjson", ".data")
    if archive_suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            members = sorted(
                (
                    info
                    for info in archive.infolist()
                    if info.filename.lower().endswith(supported_members)
                ),
                key=lambda info: info.filename,
            )
            if not members:
                raise ValueError("official_archive_contains_no_supported_data_file")
            for member in members:
                with archive.open(member) as source:
                    yield from _records_from_binary(
                        member.filename,
                        source,
                        uncompressed_size=member.file_size,
                    )
        return
    if archive_suffix in {".tar.gz", ".tgz"}:
        supported_member_found = False
        with tarfile.open(path, mode="r:gz") as archive:
            for member in archive:
                if not member.isfile() or not member.name.lower().endswith(
                    supported_members
                ):
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("official_archive_member_unreadable")
                supported_member_found = True
                with source:
                    yield from _records_from_binary(
                        member.name,
                        source,
                        uncompressed_size=member.size,
                    )
        if not supported_member_found:
            raise ValueError("official_archive_contains_no_supported_data_file")
        return
    with path.open("rb") as source:
        yield from _records_from_binary(
            path.name,
            source,
            uncompressed_size=path.stat().st_size,
        )


def _records_from_binary(
    name: str,
    source,
    *,
    uncompressed_size: int,
) -> Iterator[dict[str, Any]]:
    lower = name.lower()
    text_source = io.TextIOWrapper(source, encoding="utf-8-sig", newline="")
    if lower.endswith(".csv"):
        yield from csv.DictReader(text_source)
    elif lower.endswith((".jsonl", ".ndjson", ".data")):
        for line in text_source:
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("official_jsonl_row_not_object")
                yield value
    elif lower.endswith(".json"):
        if uncompressed_size > _MAX_BUFFERED_JSON_BYTES:
            raise ValueError(
                "official_json_too_large_use_streaming_jsonl_or_csv"
            )
        value = json.load(text_source)
        rows = value.get("data") if isinstance(value, dict) else value
        if not isinstance(rows, list):
            raise ValueError("official_json_rows_not_list")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("official_json_row_not_object")
            yield row


def _archive_source_file(
    source_path: Path,
    archive_directory: Path,
    *,
    prefix: str,
) -> tuple[Path, str]:
    """Stream one operator-supplied official file into immutable storage."""

    source = source_path.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"official_source_file_missing:{source}")
    expanded = archive_directory.expanduser()
    if not expanded.is_absolute():
        raise ValueError("raw_archive_dir_must_be_absolute")
    directory = expanded.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".{prefix}.{uuid.uuid4().hex}.tmp"
    digest = hashlib.sha256()
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with source.open("rb") as input_handle, os.fdopen(descriptor, "wb") as output_handle:
            for chunk in iter(lambda: input_handle.read(1024 * 1024), b""):
                digest.update(chunk)
                output_handle.write(chunk)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        checksum = digest.hexdigest()
        suffix = _official_source_suffix(source)
        target = directory / f"{prefix}_{checksum}{suffix}"
        if target.exists():
            if _sha256_file(target) != checksum:
                raise RuntimeError("raw_archive_existing_checksum_mismatch")
        else:
            os.link(temporary, target)
            _fsync_directory(directory)
            temporary.unlink()
            target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        return target, checksum
    finally:
        temporary.unlink(missing_ok=True)


def _official_source_suffix(path: Path) -> str:
    lower = path.name.lower()
    for suffix in (
        ".tar.gz",
        ".tgz",
        ".zip",
        ".csv",
        ".json",
        ".jsonl",
        ".ndjson",
        ".data",
    ):
        if lower.endswith(suffix):
            return suffix
    raise ValueError("official_source_file_extension_unsupported")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _l2_payload(event: L2Event) -> dict[str, Any]:
    return {
        "symbol": event.symbol,
        "ts": event.ts,
        "sequence_id": event.sequence_id,
        "previous_sequence_id": event.previous_sequence_id,
        "action": event.action,
        "bids": [[str(px), str(sz)] for px, sz in event.bids],
        "asks": [[str(px), str(sz)] for px, sz in event.asks],
        "checksum": event.checksum,
    }


def _levels(value: Any) -> list[tuple[Decimal, Decimal]]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError("l2_levels_not_list")
    output = []
    for level in value:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            raise ValueError("l2_level_shape_invalid")
        price = Decimal(str(level[0]))
        size = Decimal(str(level[1]))
        if (
            not price.is_finite()
            or not size.is_finite()
            or price <= 0
            or size < 0
        ):
            raise ValueError("l2_level_price_or_size_invalid")
        output.append((price, size))
    return output


def _apply_levels(book: dict[Decimal, Decimal], levels: Iterable[tuple[Decimal, Decimal]]) -> None:
    for price, size in levels:
        if size == 0:
            book.pop(price, None)
        elif price > 0 and size > 0:
            book[price] = size


def _field(item: Mapping[str, Any], aliases: Sequence[str]) -> str:
    for alias in aliases:
        value = item.get(alias)
        if value not in (None, ""):
            return str(value)
    raise KeyError(aliases[0])


def _timestamp(item: Mapping[str, Any], aliases: Sequence[str]) -> datetime:
    raw = _field(item, aliases)
    if raw.isdigit():
        numeric = int(raw)
        divisor = 1_000_000_000 if numeric > 10**17 else (1_000_000 if numeric > 10**14 else (1_000 if numeric > 10**11 else 1))
        return datetime.fromtimestamp(numeric / divisor, tz=_UTC)
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("official_timestamp_must_be_timezone_aware")
    return value.astimezone(_UTC)


def _optional_int(value: Any) -> int | None:
    return None if value in (None, "") else int(value)


def _first_present(item: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    return None


def _validated_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if not _SYMBOL_PATTERN.fullmatch(normalized):
        raise ValueError("official_symbol_invalid")
    return normalized


def _validate_pagination(max_pages: int, request_interval_seconds: float) -> None:
    if max_pages <= 0:
        raise ValueError("official_max_pages_must_be_positive")
    if request_interval_seconds < 0:
        raise ValueError("official_request_interval_must_be_nonnegative")


def _validate_window(start: datetime, end: datetime) -> None:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("import_window_requires_timezone_aware_timestamps")
    if end <= start:
        raise ValueError("import_window_end_must_follow_start")


def _missing_bar_ranges(
    *,
    start: datetime,
    end: datetime,
    interval_seconds: int,
    observed: set[datetime],
) -> list[dict[str, Any]]:
    """Compress absent aligned bars into half-open ranges."""

    step = timedelta(seconds=interval_seconds)
    cursor = start
    missing_start: datetime | None = None
    gaps: list[dict[str, Any]] = []
    while cursor < end:
        if cursor not in observed and missing_start is None:
            missing_start = cursor
        if cursor in observed and missing_start is not None:
            gaps.append(
                {
                    "reason": "official_mark_bar_missing",
                    "gap_start": missing_start.isoformat(),
                    "gap_end": cursor.isoformat(),
                }
            )
            missing_start = None
        cursor += step
    if missing_start is not None:
        gaps.append(
            {
                "reason": "official_mark_bar_missing",
                "gap_start": missing_start.isoformat(),
                "gap_end": end.isoformat(),
            }
        )
    return gaps


def _append_sample_gap(
    gaps: list[dict[str, Any]],
    *,
    sample: datetime,
    step: timedelta,
    reason: str,
    staleness_ms: int | None = None,
) -> None:
    """Append or extend one contiguous resampling-gap range."""

    start = sample.isoformat()
    end = (sample + step).isoformat()
    if (
        gaps
        and gaps[-1].get("reason") == reason
        and gaps[-1].get("gap_end") == start
    ):
        gaps[-1]["gap_end"] = end
        gaps[-1]["missing_samples"] = int(gaps[-1]["missing_samples"]) + 1
        if staleness_ms is not None:
            gaps[-1]["max_staleness_ms"] = max(
                int(gaps[-1].get("max_staleness_ms") or 0),
                staleness_ms,
            )
        return
    gap: dict[str, Any] = {
        "sample_ts": start,
        "gap_start": start,
        "gap_end": end,
        "reason": reason,
        "missing_samples": 1,
    }
    if staleness_ms is not None:
        gap["max_staleness_ms"] = staleness_ms
    gaps.append(gap)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(_UTC).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported_json_value:{type(value).__name__}")
