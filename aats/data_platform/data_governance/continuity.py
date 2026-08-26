"""Collector continuity ledger writes and historical window classification."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import text


_EVENT_TYPES = {
    "CONNECT",
    "DISCONNECT",
    "RECONNECT",
    "MESSAGE",
    "FLUSH",
    "DROP",
    "SHUTDOWN",
    "CLOCK_SKEW",
}


@dataclass(frozen=True)
class ContinuityEvent:
    collector: str
    channel: str
    symbol: str
    connection_generation: int
    event_type: str
    event_ts: datetime
    ingest_run_id: str
    event_key: str = ""
    exchange_event_ts: datetime | None = None
    local_received_ts: datetime | None = None
    sample_ts: datetime | None = None
    payload_sequence: int | None = None
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not all(str(value).strip() for value in (self.collector, self.channel, self.symbol)):
            raise ValueError("continuity_event_identity_must_be_nonempty")
        if self.event_type not in _EVENT_TYPES:
            raise ValueError("continuity_event_type_invalid")
        if self.connection_generation < 0:
            raise ValueError("connection_generation_must_be_non_negative")
        if self.event_ts.tzinfo is None or self.event_ts.utcoffset() is None:
            raise ValueError("continuity_event_ts_must_be_timezone_aware")
        try:
            uuid.UUID(self.ingest_run_id)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("continuity_ingest_run_id_invalid") from exc
        for field_name in (
            "exchange_event_ts",
            "local_received_ts",
            "sample_ts",
        ):
            value = getattr(self, field_name)
            if value is not None and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError(f"continuity_{field_name}_must_be_timezone_aware")


@dataclass(frozen=True)
class ContinuityWindowReport:
    collector: str
    channel: str
    symbol: str
    window_start: datetime
    window_end: datetime
    status: str
    generations: tuple[int, ...]
    ingest_run_ids: tuple[str, ...]
    event_counts: dict[str, int]
    first_message_ts: datetime | None
    last_message_ts: datetime | None
    reason_codes: tuple[str, ...]
    fingerprint: str


def record_continuity_events(session, events: Iterable[ContinuityEvent]) -> int:
    payload = [
        {
            **asdict(event),
            "details": json.dumps(event.details or {}, sort_keys=True),
        }
        for event in events
    ]
    if not payload:
        return 0
    result = session.execute(
        text(
            """
            INSERT INTO meta.collector_continuity_events (
                collector, channel, symbol, connection_generation,
                event_type, event_ts, event_key, exchange_event_ts,
                local_received_ts, sample_ts, payload_sequence,
                ingest_run_id, details
            ) VALUES (
                :collector, :channel, :symbol, :connection_generation,
                :event_type, :event_ts, :event_key, :exchange_event_ts,
                :local_received_ts, :sample_ts, :payload_sequence,
                CAST(:ingest_run_id AS UUID), CAST(:details AS jsonb)
            )
            ON CONFLICT ON CONSTRAINT uq_collector_continuity_event DO NOTHING
            """
        ),
        payload,
    )
    return int(result.rowcount or 0)


def classify_continuity_window(
    session,
    *,
    collector: str,
    channel: str,
    symbol: str,
    window_start: datetime,
    window_end: datetime,
    require_flush: bool = True,
    boundary_tolerance_seconds: int = 120,
) -> ContinuityWindowReport:
    if not all(str(value).strip() for value in (collector, channel, symbol)):
        raise ValueError("continuity_window_identity_must_be_nonempty")
    if (
        window_start.tzinfo is None
        or window_start.utcoffset() is None
        or window_end.tzinfo is None
        or window_end.utcoffset() is None
    ):
        raise ValueError("continuity_window_requires_timezone_aware_timestamps")
    if window_end <= window_start:
        raise ValueError("continuity_window_end_must_follow_start")
    if boundary_tolerance_seconds < 0:
        raise ValueError("continuity_boundary_tolerance_must_be_non_negative")
    rows = session.execute(
        text(
            "SELECT connection_generation, ingest_run_id, event_type, "
            "COUNT(*) AS count, MIN(event_ts) AS earliest_event_ts, "
            "MAX(event_ts) AS latest_event_ts "
            "FROM meta.collector_continuity_events "
            "WHERE collector = :collector AND channel = :channel "
            "AND symbol = :symbol AND event_ts >= :start AND event_ts < :end "
            "GROUP BY connection_generation, ingest_run_id, event_type"
        ),
        {
            "collector": collector,
            "channel": channel,
            "symbol": symbol,
            "start": window_start,
            "end": window_end,
        },
    ).mappings().all()
    generations = tuple(sorted({int(row["connection_generation"]) for row in rows}))
    ingest_run_ids = tuple(
        sorted(
            {
                str(row["ingest_run_id"])
                for row in rows
                if row.get("ingest_run_id") is not None
            }
        )
    )
    counts: dict[str, int] = {}
    message_starts: list[datetime] = []
    message_ends: list[datetime] = []
    for row in rows:
        event = str(row["event_type"])
        counts[event] = counts.get(event, 0) + int(row["count"])
        if event == "MESSAGE":
            if row.get("earliest_event_ts") is not None:
                message_starts.append(row["earliest_event_ts"])
            if row.get("latest_event_ts") is not None:
                message_ends.append(row["latest_event_ts"])
    first_message_ts = min(message_starts) if message_starts else None
    last_message_ts = max(message_ends) if message_ends else None
    reasons: set[str] = set()
    if not rows or counts.get("MESSAGE", 0) == 0:
        reasons.add("continuity_unknown")
    elif first_message_ts is None or last_message_ts is None:
        reasons.add("continuity_message_bounds_unknown")
    else:
        tolerance = timedelta(seconds=boundary_tolerance_seconds)
        if first_message_ts > window_start + tolerance:
            reasons.add("collector_window_start_unproven")
        if last_message_ts < window_end - tolerance:
            reasons.add("collector_window_end_unproven")
    if counts.get("DROP", 0):
        reasons.add("collector_drop_observed")
    if counts.get("DISCONNECT", 0):
        reasons.add("collector_disconnect_in_window")
    if counts.get("CLOCK_SKEW", 0):
        reasons.add("collector_clock_skew")
    if require_flush and counts.get("FLUSH", 0) == 0:
        reasons.add("collector_flush_unproven")
    if len(generations) > 1:
        reasons.add("multiple_connection_generations")
    if len(ingest_run_ids) != 1:
        reasons.add(
            "ingest_run_lineage_missing"
            if not ingest_run_ids
            else "multiple_ingest_runs"
        )
    ordered = tuple(sorted(reasons))
    status = "complete" if not ordered else (
        "unknown" if "continuity_unknown" in ordered else "known_gap"
    )
    fingerprint_payload = {
        "collector": collector,
        "channel": channel,
        "symbol": symbol,
        "window_start": window_start.astimezone(timezone.utc).isoformat(),
        "window_end": window_end.astimezone(timezone.utc).isoformat(),
        "status": status,
        "generations": generations,
        "ingest_run_ids": ingest_run_ids,
        "event_counts": dict(sorted(counts.items())),
        "first_message_ts": (
            first_message_ts.astimezone(timezone.utc).isoformat()
            if first_message_ts is not None
            else None
        ),
        "last_message_ts": (
            last_message_ts.astimezone(timezone.utc).isoformat()
            if last_message_ts is not None
            else None
        ),
        "reason_codes": ordered,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ContinuityWindowReport(
        collector=collector,
        channel=channel,
        symbol=symbol,
        window_start=window_start,
        window_end=window_end,
        status=status,
        generations=generations,
        ingest_run_ids=ingest_run_ids,
        event_counts=dict(sorted(counts.items())),
        first_message_ts=first_message_ts,
        last_message_ts=last_message_ts,
        reason_codes=ordered,
        fingerprint=fingerprint,
    )
