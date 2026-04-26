"""OKX public ``liquidation-orders`` WebSocket collector.

Persists the raw liquidation event stream into ``staging.raw_liquidations`` for
long-term retention. OKX REST ``/api/v5/public/liquidation-orders`` only keeps
7 days of history, so this pipeline is the only way to accumulate the data
needed for future baseline contrarian-reversal signals.

Data flow::

    OKX WebSocket → _handle_message → parse_liquidation_message
                                   ↓
                                buffer  (swap-and-release under lock)
                                   ↓
                    flush (max-rows | periodic) → write_liquidation_batch
                                                 (INSERT ON CONFLICT DO NOTHING)

Natural-key uniqueness ``(inst_id, ts, side, bk_px, sz)`` handles OKX
retransmissions after reconnect without client-side dedup state.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from aats.bootstrap.settings import AATSSettings
from aats.data_platform.db import get_session
from aats.data_platform.models import utc_now
from aats.data_platform.normalize.time_normalizer import ms_to_utc
from aats.services.market_gateway.okx_websocket import OKXWebSocketConsumerBase

log = logging.getLogger(__name__)


# Constants
_CHANNEL = "liquidation-orders"
_CONNECTION = "public"  # OKX hosts liquidation-orders on the public WS URL.

# SWAP is the only inst_type we currently care about for baseline contrarian
# signals. Extendable to ("SWAP", "FUTURES") if someone wants the full
# derivatives tape — OKX accepts one subscription arg per inst_type.
_DEFAULT_INST_TYPES: tuple[str, ...] = ("SWAP",)
_FIXED_TRADING_INSTRUMENT = "BTC-USDT-SWAP"
_SOURCE_SCOPE_FIXED = "fixed_trading_scope"
_SOURCE_SCOPE_BROAD = "broad_market_context"

# Flush heuristics. Liquidation throughput is spiky but generally low; these
# numbers keep write latency bounded while still amortizing the round-trip per
# row. Whichever threshold hits first triggers a flush.
_FLUSH_MAX_ROWS: int = 100
_FLUSH_MAX_SECONDS: float = 5.0


@dataclass(frozen=True, slots=True)
class LiquidationRow:
    """Parsed per-details row from a liquidation-orders push."""

    ts: datetime
    inst_id: str
    inst_type: str
    inst_family: str | None
    side: str
    bk_px: Decimal
    sz: Decimal
    bk_loss: Decimal | None
    ccy: str | None
    raw_payload: dict[str, Any]
    source_scope: str | None = None


class OKXLiquidationsWSClient(OKXWebSocketConsumerBase):
    """OKX public WS client subscribed to ``liquidation-orders`` per inst type.

    Single-connection (public endpoint) consumer. Reconnect / keepalive /
    ack-timeout semantics are inherited from the base class.
    """

    def __init__(
        self,
        *,
        settings: AATSSettings,
        inst_types: Iterable[str] = _DEFAULT_INST_TYPES,
    ) -> None:
        super().__init__(settings=settings, logger_name="aats.okx_liquidation_ws")
        self._inst_types: tuple[str, ...] = tuple(dict.fromkeys(inst_types))
        if not self._inst_types:
            raise ValueError("OKXLiquidationsWSClient requires at least one inst_type")
        self._register_connection(_CONNECTION)

    @property
    def inst_types(self) -> tuple[str, ...]:
        return self._inst_types

    def _connection_specs(self) -> list[tuple[str, str, list[dict[str, str]]]]:
        args: list[dict[str, str]] = [
            {"channel": _CHANNEL, "instType": inst_type}
            for inst_type in self._inst_types
        ]
        return [(_CONNECTION, self.settings.okx_public_ws_url, args)]


def _parse_ts_ms(ts_str: str) -> datetime | None:
    if not ts_str:
        return None
    try:
        return ms_to_utc(ts_str)
    except (ValueError, OSError, TypeError):
        return None


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _source_scope_for_inst_id(inst_id: str) -> str:
    return _SOURCE_SCOPE_FIXED if inst_id == _FIXED_TRADING_INSTRUMENT else _SOURCE_SCOPE_BROAD


def parse_liquidation_message(message: dict[str, Any]) -> list[LiquidationRow]:
    """Extract per-details rows from one OKX ``liquidation-orders`` push.

    OKX payload shape::

        {"arg": {"channel": "liquidation-orders", "instType": "SWAP"},
         "data": [
            {"instType": "SWAP", "instFamily": "BTC-USDT",
             "instId": "BTC-USDT-SWAP",
             "details": [
                {"side": "sell", "bkPx": "95000", "sz": "1.5",
                 "bkLoss": "0", "ccy": "USDT", "ts": "1745000000000"}
             ]}
         ]}

    Malformed details (missing ts / inst_id / side / bk_px / sz, or unknown
    side value) are dropped with a warning — the rest of the batch still lands.
    Defensive against OKX schema evolution adding optional fields.

    ``raw_payload`` stores only the ``detail`` object; parent ``event``
    (inst_id/inst_type/inst_family) is already denormalized into dedicated
    columns, and ``arg`` is constant per subscription and never changes.
    Carrying either would 3–10× JSONB storage with no informational gain.
    """
    rows: list[LiquidationRow] = []
    data = message.get("data")
    if not isinstance(data, list):
        return rows
    for event in data:
        if not isinstance(event, dict):
            continue
        inst_id = str(event.get("instId", "") or "")
        inst_type = str(event.get("instType", "") or "")
        inst_family_raw = event.get("instFamily")
        inst_family = str(inst_family_raw) if inst_family_raw else None
        details = event.get("details")
        if not isinstance(details, list):
            continue
        for detail in details:
            if not isinstance(detail, dict):
                continue
            ts = _parse_ts_ms(str(detail.get("ts", "")))
            side = str(detail.get("side", "") or "").lower()
            bk_px = _parse_decimal(detail.get("bkPx"))
            sz = _parse_decimal(detail.get("sz"))
            if (
                not inst_id
                or not inst_type
                or ts is None
                or side not in ("buy", "sell")
                or bk_px is None
                or sz is None
            ):
                log.warning(
                    "skipping malformed liquidation detail: inst_id=%s ts=%s side=%s bk_px=%s sz=%s",
                    inst_id,
                    detail.get("ts"),
                    side,
                    bk_px,
                    sz,
                )
                continue
            bk_loss = _parse_decimal(detail.get("bkLoss"))
            ccy_raw = detail.get("ccy")
            ccy = str(ccy_raw) if ccy_raw else None
            rows.append(
                LiquidationRow(
                    ts=ts,
                    inst_id=inst_id,
                    inst_type=inst_type,
                    inst_family=inst_family,
                    side=side,
                    bk_px=bk_px,
                    sz=sz,
                    bk_loss=bk_loss,
                    ccy=ccy,
                    raw_payload=dict(detail),
                    source_scope=_source_scope_for_inst_id(inst_id),
                )
            )
    return rows


def write_liquidation_batch(session: Session, rows: Iterable[LiquidationRow]) -> int:
    """Batch-insert rows with ON CONFLICT DO NOTHING on the natural-key unique.

    Returns ``result.rowcount`` — the real number of rows persisted, not the
    number of row-dicts sent. With ``ON CONFLICT DO NOTHING`` silencing OKX
    retransmits, this is the metric callers actually want for observability.
    """
    batch = [
        {
            "ts": r.ts,
            "inst_id": r.inst_id,
            "inst_type": r.inst_type,
            "inst_family": r.inst_family,
            "side": r.side,
            "bk_px": r.bk_px,
            "sz": r.sz,
            "bk_loss": r.bk_loss,
            "ccy": r.ccy,
            "raw_payload": json.dumps(r.raw_payload),
            "source_scope": r.source_scope or _source_scope_for_inst_id(r.inst_id),
        }
        for r in rows
    ]
    if not batch:
        return 0
    result = session.execute(
        text("""
            INSERT INTO staging.raw_liquidations
                (ts, inst_id, inst_type, inst_family, side,
                 bk_px, sz, bk_loss, ccy, raw_payload, source_scope)
            VALUES
                (:ts, :inst_id, :inst_type, :inst_family, :side,
                 :bk_px, :sz, :bk_loss, :ccy, CAST(:raw_payload AS JSONB),
                 :source_scope)
            ON CONFLICT ON CONSTRAINT uq_raw_liquidations_natural_key DO NOTHING
        """),
        batch,
    )
    rowcount = getattr(result, "rowcount", None)
    return int(rowcount) if rowcount is not None and rowcount >= 0 else len(batch)


class LiquidationsCollector:
    """Glue: WS client → parsed rows → buffered DB writes + basic observability.

    Buffer is flushed whenever it reaches ``flush_max_rows`` OR when
    ``flush_max_seconds`` have elapsed since the last flush, whichever hits
    first. On shutdown, any remaining buffered rows are drained synchronously.

    Flush strategy: under the buffer lock we only swap the list; the DB
    roundtrip happens outside the lock so a slow write can't stall the WS
    consumer during a liquidation cascade (which is exactly when throughput
    matters).
    """

    def __init__(
        self,
        *,
        settings: AATSSettings,
        inst_types: Iterable[str] = _DEFAULT_INST_TYPES,
        flush_max_rows: int = _FLUSH_MAX_ROWS,
        flush_max_seconds: float = _FLUSH_MAX_SECONDS,
    ) -> None:
        self._client = OKXLiquidationsWSClient(settings=settings, inst_types=inst_types)
        self._buffer: list[LiquidationRow] = []
        self._buffer_lock = asyncio.Lock()
        self._flush_max_rows = flush_max_rows
        self._flush_max_seconds = flush_max_seconds
        # Per-UTC-day persisted count. Grows ≤1 entry/day (~0.5 KB/year).
        self._daily_counts: dict[str, int] = {}

    @property
    def client(self) -> OKXLiquidationsWSClient:
        return self._client

    def status(self) -> dict[str, Any]:
        today = utc_now().date().isoformat()
        conn = self._client.connection_status(_CONNECTION)
        return {
            "connected": conn["connected"],
            "last_message_ts": conn["last_message_ts"],
            "last_error": conn["last_error"],
            "buffered_rows": len(self._buffer),
            "daily_counts": dict(self._daily_counts),
            "today": today,
            "today_count": self._daily_counts.get(today, 0),
            "inst_types": list(self._client.inst_types),
        }

    async def _handle_message(self, message: dict[str, Any]) -> None:
        rows = parse_liquidation_message(message)
        if not rows:
            return
        should_flush = False
        async with self._buffer_lock:
            self._buffer.extend(rows)
            if len(self._buffer) >= self._flush_max_rows:
                should_flush = True
        if should_flush:
            await self._flush()

    async def _flush(self) -> None:
        # Swap-and-release: only the list swap is under the lock; DB I/O runs
        # outside so a slow write can't stall the WS consumer.
        async with self._buffer_lock:
            if not self._buffer:
                return
            to_write, self._buffer = self._buffer, []
        try:
            with get_session() as session:
                written = write_liquidation_batch(session, to_write)
            today = utc_now().date().isoformat()
            self._daily_counts[today] = self._daily_counts.get(today, 0) + written
            log.info(
                "flushed %d liquidations (today_total=%d, attempted=%d)",
                written,
                self._daily_counts[today],
                len(to_write),
            )
        except (SQLAlchemyError, OSError):
            # Narrow catch: DB errors and connection issues drop this batch.
            # Broader exceptions (bugs) should propagate so tests catch them.
            # Rows are not re-queued; OKX doesn't re-send and retrying in-band
            # risks unbounded buffer growth during prolonged outages.
            log.exception("liquidation flush failed; %d rows dropped", len(to_write))

    async def _periodic_flush(self) -> None:
        # Waits on the client's stop_event so a single shutdown signal drains
        # both the consumer loop and the flush loop.
        stop = self._client.stop_event
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._flush_max_seconds)
                return
            except TimeoutError:
                pass
            await self._flush()

    async def run_forever(self) -> None:
        flush_task = asyncio.create_task(self._periodic_flush())
        try:
            await self._client.run_forever(on_message=self._handle_message)
        finally:
            await self._flush()
            flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await flush_task

    async def stop(self) -> None:
        await self._client.stop()
