"""OKX public microstructure WebSocket collector.

Subscribes to the six public channels that feed the P1-D Phase 1A Bronze /
staging tables:

  - ``trades-all``        → bronze.market_trades
  - ``bbo-tbt``           → bronze.market_orderbook_bbo (client-side 1 Hz sampled)
  - ``books5``            → bronze.market_orderbook_books5 (client-side 2 Hz sampled)
  - ``open-interest``     → staging.market_oi_funding_ticks (tick_type='oi')
  - ``funding-rate``      → staging.market_oi_funding_ticks (tick_type='funding')
  - ``mark-price``        → staging.market_oi_funding_ticks (tick_type='mark')

Follows the :mod:`aats.data_platform.collectors.liquidations_ws_collector`
pattern: single public connection, parse → buffer → flush with
``INSERT ... ON CONFLICT DO NOTHING`` for DB-level idempotency. Phase 1A Q3
decision mandates "DB only, no NATS" — the collector deliberately carries no
pub/sub dependency.

Rate-limiting strategy (appendix E #5 of the implementation design):

* ``bbo-tbt`` pushes every 10 ms, but we only want 1 sample / sec / symbol
  for the Bronze BBO table. The client enforces this with a per-symbol
  ``_min_bbo_interval`` throttle.
* ``books5`` pushes every 100 ms; we sample at 2 Hz (every 500 ms).

Unsampled ``trades-all`` / open-interest / funding-rate / mark-price messages
are persisted verbatim.

Buffering (§6.6):
* 4 independent :class:`MicrostructureBronzeBuffer` instances — one per table.
* Each flushes on whichever threshold hits first: ``flush_max_rows`` or
  ``flush_max_seconds`` elapsed since the last flush.
* Swap-and-release under lock: DB round-trip runs outside the buffer lock so
  a slow write cannot stall the WS consumer during bursty traffic.

Metrics (§4.3):
  ``microstructure_ws_connect_total``, ``microstructure_ws_reconnect_total``,
  ``microstructure_ws_messages_total``, ``microstructure_bronze_rows_written_total``,
  ``microstructure_bronze_flush_total``, ``microstructure_bronze_flush_errors_total``.

The collector exposes status via :meth:`MicrostructureCollector.status` so the
daemon heartbeat loop can serialize current state into the heartbeat file.
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

from aats.bootstrap.metrics import MetricsRegistry
from aats.bootstrap.settings import AATSSettings
from aats.data_platform.db import get_session
from aats.data_platform.jobs.run_registry import (
    create_ingest_run,
    finish_ingest_run,
    mark_orphaned_ingest_runs,
)
from aats.data_platform.models import utc_now
from aats.data_platform.normalize.time_normalizer import ms_to_utc
from aats.services.market_gateway.okx_websocket import OKXWebSocketConsumerBase

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONNECTION = "microstructure"  # distinct from market's "public"/"business"

# Fix (2026-04-20): OKX `trades-all` 频道需要 VIP5+ (60018 for VIP0/1).
# AATS 生产账号 VIP0, 必须用 public `trades` 频道 (对所有 VIP 开放).
# 参见 Phase 1A 首次 deploy 的 OKX 60018 "channel doesn't exist" 错误.
_CHANNEL_TRADES = "trades"
_CHANNEL_BBO = "bbo-tbt"
_CHANNEL_BOOKS5 = "books5"
_CHANNEL_OI = "open-interest"
_CHANNEL_FUNDING = "funding-rate"
_CHANNEL_MARK = "mark-price"

_SUBSCRIBE_CHANNELS: tuple[str, ...] = (
    _CHANNEL_TRADES,
    _CHANNEL_BBO,
    _CHANNEL_BOOKS5,
    _CHANNEL_OI,
    _CHANNEL_FUNDING,
    _CHANNEL_MARK,
)

_DEFAULT_SYMBOLS: tuple[str, ...] = ("BTC-USDT-SWAP",)

# Client-side sampling (§6.2 / §6.3 + appendix E #5).
# bbo-tbt: OKX pushes every 10 ms → keep 1 / s / symbol.
# books5:  OKX pushes every 100 ms → keep 1 per 500 ms (2 Hz).
_BBO_MIN_INTERVAL_SECONDS: float = 1.0
_BOOKS5_MIN_INTERVAL_SECONDS: float = 0.5

# Per-table flush thresholds (§6.6).
_FLUSH_TRADES_ROWS: int = 500
_FLUSH_TRADES_SECONDS: float = 3.0
_FLUSH_BBO_ROWS: int = 100
_FLUSH_BBO_SECONDS: float = 5.0
_FLUSH_BOOKS5_ROWS: int = 200
_FLUSH_BOOKS5_SECONDS: float = 2.0
_FLUSH_OIF_ROWS: int = 100
_FLUSH_OIF_SECONDS: float = 3.0

# Hard cap to prevent OOM if DB is unavailable during an extended outage
# (§9 Day 4 aplokcker risk). When exceeded, rows are dropped with a critical
# log line — availability is preferred over data completeness under DB-down.
_BUFFER_HARD_CAP: int = 5000


# ---------------------------------------------------------------------------
# Parsed row dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TradeRow:
    """Row persisted to ``bronze.market_trades``."""

    symbol: str
    ts: datetime
    trade_id: str
    px: Decimal
    sz: Decimal
    side: str
    raw_payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BboRow:
    """Row persisted to ``bronze.market_orderbook_bbo``.

    The ``mid`` / ``spread`` / ``imbalance`` columns are GENERATED STORED in
    Postgres — the client never writes them. See §6.2 of the design doc.
    """

    symbol: str
    ts: datetime          # client-side sample instant (throttled to 1 Hz)
    source_ts: datetime   # OKX-pushed ts
    bid_px: Decimal
    bid_sz: Decimal
    ask_px: Decimal
    ask_sz: Decimal


@dataclass(frozen=True, slots=True)
class Books5Row:
    """Row persisted to ``bronze.market_orderbook_books5`` (5 levels flattened)."""

    symbol: str
    ts: datetime          # client-side sample instant (throttled to 2 Hz)
    source_ts: datetime   # OKX-pushed ts
    bid_px_1: Decimal
    bid_sz_1: Decimal
    bid_px_2: Decimal | None
    bid_sz_2: Decimal | None
    bid_px_3: Decimal | None
    bid_sz_3: Decimal | None
    bid_px_4: Decimal | None
    bid_sz_4: Decimal | None
    bid_px_5: Decimal | None
    bid_sz_5: Decimal | None
    ask_px_1: Decimal
    ask_sz_1: Decimal
    ask_px_2: Decimal | None
    ask_sz_2: Decimal | None
    ask_px_3: Decimal | None
    ask_sz_3: Decimal | None
    ask_px_4: Decimal | None
    ask_sz_4: Decimal | None
    ask_px_5: Decimal | None
    ask_sz_5: Decimal | None


@dataclass(frozen=True, slots=True)
class OiFundingMarkRow:
    """Row persisted to ``staging.market_oi_funding_ticks``.

    The discriminator is ``tick_type ∈ {'oi','funding','mark'}`` — each
    parser populates only the fields relevant to its channel; others stay
    ``None`` (NULL in Postgres). The BIGSERIAL ``id`` is assigned by the DB
    on insert, and the collector does not carry an ``ingest_run_id`` for
    this table (Stage 1 design — see the schema in §6.4).
    """

    ts: datetime
    symbol: str
    tick_type: str                                        # 'oi' | 'funding' | 'mark'
    oi: Decimal | None = None
    oi_ccy: Decimal | None = None
    funding_rate: Decimal | None = None
    next_funding_rate: Decimal | None = None
    next_funding_time: datetime | None = None
    mark_px: Decimal | None = None


# ---------------------------------------------------------------------------
# OKX WebSocket client
# ---------------------------------------------------------------------------

class MicrostructureWSClient(OKXWebSocketConsumerBase):
    """Single-connection OKX public WS client for Phase 1A microstructure.

    Subscribes to the six channels listed in the module docstring for each
    configured symbol. All reconnect / keepalive / ack-timeout semantics are
    inherited from :class:`OKXWebSocketConsumerBase`.
    """

    def __init__(
        self,
        *,
        settings: AATSSettings,
        symbols: Iterable[str] = _DEFAULT_SYMBOLS,
        channels: Iterable[str] = _SUBSCRIBE_CHANNELS,
    ) -> None:
        super().__init__(settings=settings, logger_name="aats.okx_microstructure_ws")
        self._symbols: tuple[str, ...] = tuple(dict.fromkeys(symbols))
        if not self._symbols:
            raise ValueError("MicrostructureWSClient requires at least one symbol")
        self._channels: tuple[str, ...] = tuple(dict.fromkeys(channels))
        if not self._channels:
            raise ValueError("MicrostructureWSClient requires at least one channel")
        self._register_connection(_CONNECTION)

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._symbols

    @property
    def channels(self) -> tuple[str, ...]:
        return self._channels

    def _connection_specs(self) -> list[tuple[str, str, list[dict[str, str]]]]:
        # One subscribe arg per (channel, symbol) combination. OKX accepts all
        # of them on a single public connection per IP, so we stay well under
        # the 3-connection-per-IP cap (§10.2).
        args: list[dict[str, str]] = [
            {"channel": channel, "instId": symbol}
            for symbol in self._symbols
            for channel in self._channels
        ]
        return [(_CONNECTION, self.settings.okx_public_ws_url, args)]


# ---------------------------------------------------------------------------
# Parser helpers
# ---------------------------------------------------------------------------

def _parse_ts_ms(ts_str: str | int | None) -> datetime | None:
    if ts_str in (None, ""):
        return None
    try:
        return ms_to_utc(ts_str)   # type: ignore[arg-type]
    except (ValueError, OSError, TypeError):
        return None


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _arg_channel(message: dict[str, Any]) -> str:
    arg = message.get("arg")
    if not isinstance(arg, dict):
        return ""
    return str(arg.get("channel", "") or "")


# ---------------------------------------------------------------------------
# Parsers — pure functions, one per channel family
# ---------------------------------------------------------------------------

def parse_trades_message(message: dict[str, Any]) -> list[TradeRow]:
    """Parse one OKX ``trades-all`` push into 0..N :class:`TradeRow`.

    OKX payload shape (per docs)::

        {"arg": {"channel": "trades-all", "instId": "BTC-USDT-SWAP"},
         "data": [
            {"instId": "BTC-USDT-SWAP", "tradeId": "130639474",
             "px": "95000.1", "sz": "0.01",
             "side": "buy", "ts": "1745000000123"}, ...]}

    Malformed trades (missing required field / unknown side) are dropped
    with a warning; the rest of the batch still lands.
    """
    rows: list[TradeRow] = []
    data = message.get("data")
    if not isinstance(data, list):
        return rows
    for entry in data:
        if not isinstance(entry, dict):
            continue
        symbol = str(entry.get("instId", "") or "")
        trade_id = str(entry.get("tradeId", "") or "")
        ts = _parse_ts_ms(str(entry.get("ts", "")))
        side = str(entry.get("side", "") or "").lower()
        px = _parse_decimal(entry.get("px"))
        sz = _parse_decimal(entry.get("sz"))
        if (
            not symbol
            or not trade_id
            or ts is None
            or side not in ("buy", "sell")
            or px is None
            or sz is None
        ):
            log.warning(
                "skipping malformed trade: symbol=%s trade_id=%s side=%s",
                symbol, trade_id, side,
            )
            continue
        rows.append(TradeRow(
            symbol=symbol,
            ts=ts,
            trade_id=trade_id,
            px=px,
            sz=sz,
            side=side,
            raw_payload=dict(entry),
        ))
    return rows


def parse_bbo_message(message: dict[str, Any]) -> list[BboRow]:
    """Parse one OKX ``bbo-tbt`` push into 0..N :class:`BboRow`.

    OKX payload shape::

        {"arg": {"channel": "bbo-tbt", "instId": "BTC-USDT-SWAP"},
         "data": [
            {"asks": [["95010", "2", "0", "3"]],
             "bids": [["95000", "1", "0", "5"]],
             "ts": "1745000000123",
             "seqId": 12345678}]}

    Each ``asks`` / ``bids`` entry is ``[px, sz, depr, count]``. We only need
    the top level for BBO (single ask, single bid). ``ts`` on the row is the
    **client sample instant** (set by the caller after throttling); the
    OKX-pushed ``ts`` lands in ``source_ts``.
    """
    data = message.get("data")
    if not isinstance(data, list):
        return []
    symbol = ""
    arg = message.get("arg")
    if isinstance(arg, dict):
        symbol = str(arg.get("instId", "") or "")
    rows: list[BboRow] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        # instId on entry can override the subscription arg
        entry_symbol = str(entry.get("instId", "") or "") or symbol
        source_ts = _parse_ts_ms(str(entry.get("ts", "")))
        bids = entry.get("bids")
        asks = entry.get("asks")
        if (
            not entry_symbol
            or source_ts is None
            or not isinstance(bids, list) or not bids
            or not isinstance(asks, list) or not asks
        ):
            log.warning("skipping malformed bbo entry for symbol=%s", entry_symbol)
            continue
        bid0, ask0 = bids[0], asks[0]
        if not isinstance(bid0, list) or len(bid0) < 2 or not isinstance(ask0, list) or len(ask0) < 2:
            log.warning("bbo bids/asks entry too short for symbol=%s", entry_symbol)
            continue
        bid_px = _parse_decimal(bid0[0])
        bid_sz = _parse_decimal(bid0[1])
        ask_px = _parse_decimal(ask0[0])
        ask_sz = _parse_decimal(ask0[1])
        if bid_px is None or bid_sz is None or ask_px is None or ask_sz is None:
            log.warning("bbo decimal parse failed for symbol=%s", entry_symbol)
            continue
        rows.append(BboRow(
            symbol=entry_symbol,
            ts=source_ts,          # caller overrides after throttling
            source_ts=source_ts,
            bid_px=bid_px,
            bid_sz=bid_sz,
            ask_px=ask_px,
            ask_sz=ask_sz,
        ))
    return rows


def _extract_level(levels: list[Any], idx: int) -> tuple[Decimal | None, Decimal | None]:
    """Return (px, sz) for the idx-th level, or (None, None) if missing."""
    if idx >= len(levels):
        return (None, None)
    lv = levels[idx]
    if not isinstance(lv, list) or len(lv) < 2:
        return (None, None)
    return (_parse_decimal(lv[0]), _parse_decimal(lv[1]))


def parse_books5_message(message: dict[str, Any]) -> list[Books5Row]:
    """Parse one OKX ``books5`` push into 0..N :class:`Books5Row`.

    OKX payload::

        {"arg": {"channel": "books5", "instId": "BTC-USDT-SWAP"},
         "data": [
            {"asks": [["95010","2","0","3"], ["95020","5","0","2"], ...],
             "bids": [["95000","1","0","5"], ["94990","3","0","4"], ...],
             "ts": "1745000000123", "seqId": ...}]}

    5 levels flattened to dedicated columns. OKX can return fewer than 5
    levels (thin book) — missing ones become NULL.
    """
    data = message.get("data")
    if not isinstance(data, list):
        return []
    symbol = ""
    arg = message.get("arg")
    if isinstance(arg, dict):
        symbol = str(arg.get("instId", "") or "")
    rows: list[Books5Row] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        entry_symbol = str(entry.get("instId", "") or "") or symbol
        source_ts = _parse_ts_ms(str(entry.get("ts", "")))
        bids_raw = entry.get("bids")
        asks_raw = entry.get("asks")
        if (
            not entry_symbol
            or source_ts is None
            or not isinstance(bids_raw, list) or not bids_raw
            or not isinstance(asks_raw, list) or not asks_raw
        ):
            log.warning("skipping malformed books5 entry for symbol=%s", entry_symbol)
            continue
        bid1_px, bid1_sz = _extract_level(bids_raw, 0)
        ask1_px, ask1_sz = _extract_level(asks_raw, 0)
        if bid1_px is None or bid1_sz is None or ask1_px is None or ask1_sz is None:
            log.warning("books5 top-of-book parse failed for symbol=%s", entry_symbol)
            continue
        bid2_px, bid2_sz = _extract_level(bids_raw, 1)
        bid3_px, bid3_sz = _extract_level(bids_raw, 2)
        bid4_px, bid4_sz = _extract_level(bids_raw, 3)
        bid5_px, bid5_sz = _extract_level(bids_raw, 4)
        ask2_px, ask2_sz = _extract_level(asks_raw, 1)
        ask3_px, ask3_sz = _extract_level(asks_raw, 2)
        ask4_px, ask4_sz = _extract_level(asks_raw, 3)
        ask5_px, ask5_sz = _extract_level(asks_raw, 4)
        rows.append(Books5Row(
            symbol=entry_symbol,
            ts=source_ts,          # caller overrides after throttling
            source_ts=source_ts,
            bid_px_1=bid1_px, bid_sz_1=bid1_sz,
            bid_px_2=bid2_px, bid_sz_2=bid2_sz,
            bid_px_3=bid3_px, bid_sz_3=bid3_sz,
            bid_px_4=bid4_px, bid_sz_4=bid4_sz,
            bid_px_5=bid5_px, bid_sz_5=bid5_sz,
            ask_px_1=ask1_px, ask_sz_1=ask1_sz,
            ask_px_2=ask2_px, ask_sz_2=ask2_sz,
            ask_px_3=ask3_px, ask_sz_3=ask3_sz,
            ask_px_4=ask4_px, ask_sz_4=ask4_sz,
            ask_px_5=ask5_px, ask_sz_5=ask5_sz,
        ))
    return rows


def parse_oi_funding_mark_message(message: dict[str, Any]) -> list[OiFundingMarkRow]:
    """Parse one OKX ``open-interest`` / ``funding-rate`` / ``mark-price`` push.

    The three channels ship structurally different payloads but all target
    the same ``staging.market_oi_funding_ticks`` table via ``tick_type``
    discriminator. We dispatch on ``message["arg"]["channel"]``.

    Schemas (per OKX docs)::

        open-interest: {"instId":..., "oi":..., "oiCcy":..., "ts":...}
        funding-rate:  {"instId":..., "fundingRate":..., "nextFundingRate":...,
                        "fundingTime":..., "nextFundingTime":..., ...}
        mark-price:    {"instId":..., "markPx":..., "ts":...}
    """
    channel = _arg_channel(message)
    data = message.get("data")
    if not isinstance(data, list) or channel not in (_CHANNEL_OI, _CHANNEL_FUNDING, _CHANNEL_MARK):
        return []
    rows: list[OiFundingMarkRow] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        symbol = str(entry.get("instId", "") or "")
        if not symbol:
            continue
        if channel == _CHANNEL_OI:
            ts = _parse_ts_ms(str(entry.get("ts", "")))
            oi = _parse_decimal(entry.get("oi"))
            if ts is None or oi is None:
                log.warning("skipping malformed open-interest for %s", symbol)
                continue
            rows.append(OiFundingMarkRow(
                ts=ts,
                symbol=symbol,
                tick_type="oi",
                oi=oi,
                oi_ccy=_parse_decimal(entry.get("oiCcy")),
            ))
        elif channel == _CHANNEL_FUNDING:
            # fundingTime = period-start timestamp; use it as the tick ts
            # (matches OKX normalizer convention — see okx_normalizer.py L341).
            ts = _parse_ts_ms(str(entry.get("fundingTime", "")))
            rate = _parse_decimal(entry.get("fundingRate"))
            if ts is None or rate is None:
                log.warning("skipping malformed funding-rate for %s", symbol)
                continue
            rows.append(OiFundingMarkRow(
                ts=ts,
                symbol=symbol,
                tick_type="funding",
                funding_rate=rate,
                next_funding_rate=_parse_decimal(entry.get("nextFundingRate")),
                next_funding_time=_parse_ts_ms(str(entry.get("nextFundingTime", ""))),
            ))
        elif channel == _CHANNEL_MARK:
            ts = _parse_ts_ms(str(entry.get("ts", "")))
            mark_px = _parse_decimal(entry.get("markPx"))
            if ts is None or mark_px is None:
                log.warning("skipping malformed mark-price for %s", symbol)
                continue
            rows.append(OiFundingMarkRow(
                ts=ts,
                symbol=symbol,
                tick_type="mark",
                mark_px=mark_px,
            ))
    return rows


# ---------------------------------------------------------------------------
# DB write helpers — one per table, all with ON CONFLICT DO NOTHING
# ---------------------------------------------------------------------------

def write_trades_batch(
    session: Session,
    rows: Iterable[TradeRow],
    *,
    ingest_run_id: str,
) -> int:
    batch = [
        {
            "symbol": r.symbol,
            "ts": r.ts,
            "trade_id": r.trade_id,
            "px": r.px,
            "sz": r.sz,
            "side": r.side,
            "raw_payload": json.dumps(r.raw_payload),
            "ingest_run_id": ingest_run_id,
        }
        for r in rows
    ]
    if not batch:
        return 0
    result = session.execute(
        text("""
            INSERT INTO bronze.market_trades
                (symbol, ts, trade_id, px, sz, side, raw_payload, ingest_run_id)
            VALUES
                (:symbol, :ts, :trade_id, :px, :sz, :side,
                 CAST(:raw_payload AS JSONB), CAST(:ingest_run_id AS UUID))
            ON CONFLICT ON CONSTRAINT pk_brz_market_trades DO NOTHING
        """),
        batch,
    )
    rowcount = getattr(result, "rowcount", None)
    return int(rowcount) if rowcount is not None and rowcount >= 0 else len(batch)


def write_bbo_batch(
    session: Session,
    rows: Iterable[BboRow],
    *,
    ingest_run_id: str,
) -> int:
    # mid / spread / imbalance are GENERATED STORED — never written client-side.
    batch = [
        {
            "symbol": r.symbol,
            "ts": r.ts,
            "source_ts": r.source_ts,
            "bid_px": r.bid_px,
            "bid_sz": r.bid_sz,
            "ask_px": r.ask_px,
            "ask_sz": r.ask_sz,
            "ingest_run_id": ingest_run_id,
        }
        for r in rows
    ]
    if not batch:
        return 0
    result = session.execute(
        text("""
            INSERT INTO bronze.market_orderbook_bbo
                (symbol, ts, source_ts, bid_px, bid_sz, ask_px, ask_sz, ingest_run_id)
            VALUES
                (:symbol, :ts, :source_ts, :bid_px, :bid_sz, :ask_px, :ask_sz,
                 CAST(:ingest_run_id AS UUID))
            ON CONFLICT ON CONSTRAINT pk_brz_market_orderbook_bbo DO NOTHING
        """),
        batch,
    )
    rowcount = getattr(result, "rowcount", None)
    return int(rowcount) if rowcount is not None and rowcount >= 0 else len(batch)


def write_books5_batch(
    session: Session,
    rows: Iterable[Books5Row],
    *,
    ingest_run_id: str,
) -> int:
    batch = [
        {
            "symbol": r.symbol,
            "ts": r.ts,
            "source_ts": r.source_ts,
            "bid_px_1": r.bid_px_1, "bid_sz_1": r.bid_sz_1,
            "bid_px_2": r.bid_px_2, "bid_sz_2": r.bid_sz_2,
            "bid_px_3": r.bid_px_3, "bid_sz_3": r.bid_sz_3,
            "bid_px_4": r.bid_px_4, "bid_sz_4": r.bid_sz_4,
            "bid_px_5": r.bid_px_5, "bid_sz_5": r.bid_sz_5,
            "ask_px_1": r.ask_px_1, "ask_sz_1": r.ask_sz_1,
            "ask_px_2": r.ask_px_2, "ask_sz_2": r.ask_sz_2,
            "ask_px_3": r.ask_px_3, "ask_sz_3": r.ask_sz_3,
            "ask_px_4": r.ask_px_4, "ask_sz_4": r.ask_sz_4,
            "ask_px_5": r.ask_px_5, "ask_sz_5": r.ask_sz_5,
            "ingest_run_id": ingest_run_id,
        }
        for r in rows
    ]
    if not batch:
        return 0
    result = session.execute(
        text("""
            INSERT INTO bronze.market_orderbook_books5
                (symbol, ts, source_ts,
                 bid_px_1, bid_sz_1, bid_px_2, bid_sz_2, bid_px_3, bid_sz_3,
                 bid_px_4, bid_sz_4, bid_px_5, bid_sz_5,
                 ask_px_1, ask_sz_1, ask_px_2, ask_sz_2, ask_px_3, ask_sz_3,
                 ask_px_4, ask_sz_4, ask_px_5, ask_sz_5,
                 ingest_run_id)
            VALUES
                (:symbol, :ts, :source_ts,
                 :bid_px_1, :bid_sz_1, :bid_px_2, :bid_sz_2, :bid_px_3, :bid_sz_3,
                 :bid_px_4, :bid_sz_4, :bid_px_5, :bid_sz_5,
                 :ask_px_1, :ask_sz_1, :ask_px_2, :ask_sz_2, :ask_px_3, :ask_sz_3,
                 :ask_px_4, :ask_sz_4, :ask_px_5, :ask_sz_5,
                 CAST(:ingest_run_id AS UUID))
            ON CONFLICT ON CONSTRAINT pk_brz_market_orderbook_books5 DO NOTHING
        """),
        batch,
    )
    rowcount = getattr(result, "rowcount", None)
    return int(rowcount) if rowcount is not None and rowcount >= 0 else len(batch)


def write_oif_batch(session: Session, rows: Iterable[OiFundingMarkRow]) -> int:
    """Write to ``staging.market_oi_funding_ticks``.

    This table is append-only (BIGSERIAL id PK, no natural-key UNIQUE) so
    there is no ON CONFLICT clause — duplicate OKX retransmits land as
    separate rows and Silver ETL aggregates per 15 m bar.
    """
    batch = [
        {
            "ts": r.ts,
            "symbol": r.symbol,
            "tick_type": r.tick_type,
            "oi": r.oi,
            "oi_ccy": r.oi_ccy,
            "funding_rate": r.funding_rate,
            "next_funding_rate": r.next_funding_rate,
            "next_funding_time": r.next_funding_time,
            "mark_px": r.mark_px,
        }
        for r in rows
    ]
    if not batch:
        return 0
    result = session.execute(
        text("""
            INSERT INTO staging.market_oi_funding_ticks
                (ts, symbol, tick_type, oi, oi_ccy, funding_rate,
                 next_funding_rate, next_funding_time, mark_px)
            VALUES
                (:ts, :symbol, :tick_type, :oi, :oi_ccy, :funding_rate,
                 :next_funding_rate, :next_funding_time, :mark_px)
        """),
        batch,
    )
    rowcount = getattr(result, "rowcount", None)
    return int(rowcount) if rowcount is not None and rowcount >= 0 else len(batch)


# ---------------------------------------------------------------------------
# Buffer
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FlushResult:
    """Outcome of a buffer flush."""

    attempted: int
    written: int
    reason: str            # 'max_rows' | 'timeout' | 'shutdown' | 'manual'
    error: str | None = None


class MicrostructureBronzeBuffer:
    """Generic buffer wrapping one of the four table writers.

    Not table-specific itself — the ``writer`` callable adapts to whichever
    table this buffer instance is bound to. Swap-and-release discipline:
    only the list swap is under ``_lock``; DB I/O runs outside.

    ``add()`` returns ``True`` if the caller should trigger a ``flush``
    because the buffer reached ``flush_max_rows``; the caller is responsible
    for driving the flush (we intentionally don't self-flush from ``add``
    to keep the call site in charge of the event loop ordering).

    Dropping on hard cap: if buffer size exceeds :data:`_BUFFER_HARD_CAP`,
    the oldest half is discarded with a critical log. This is the last line
    of defence against unbounded growth during a sustained DB outage.
    """

    def __init__(
        self,
        *,
        table: str,
        flush_max_rows: int,
        flush_max_seconds: float,
    ) -> None:
        if flush_max_rows < 1:
            raise ValueError("flush_max_rows must be >= 1")
        if flush_max_seconds <= 0:
            raise ValueError("flush_max_seconds must be > 0")
        self._table = table
        self._rows: list[Any] = []
        self._lock = asyncio.Lock()
        self._flush_max_rows = flush_max_rows
        self._flush_max_seconds = flush_max_seconds
        # 2026-04-20 code review B-H1: 跟踪 hard-cap drop 累计数, 供 collector
        # 在 shutdown 时推导 ingest_run status (非零 drop → retrying/failed).
        self._rows_dropped_total: int = 0

    @property
    def rows_dropped_total(self) -> int:
        """Cumulative rows dropped due to hard-cap (DB outage scenario)."""
        return self._rows_dropped_total

    @property
    def table(self) -> str:
        return self._table

    @property
    def flush_max_rows(self) -> int:
        return self._flush_max_rows

    @property
    def flush_max_seconds(self) -> float:
        return self._flush_max_seconds

    def buffered(self) -> int:
        return len(self._rows)

    async def add(self, row: Any) -> bool:
        """Append a row and return whether the max-rows threshold was reached.

        Also enforces the hard cap — if the buffer has grown past
        :data:`_BUFFER_HARD_CAP` rows (DB outage scenario), the oldest half
        is dropped before the new row is appended.
        """
        async with self._lock:
            if len(self._rows) >= _BUFFER_HARD_CAP:
                drop_n = _BUFFER_HARD_CAP // 2
                del self._rows[:drop_n]
                self._rows_dropped_total += drop_n  # B-H1 fix
                log.critical(
                    "microstructure buffer hard-cap hit on %s: dropped %d oldest rows",
                    self._table, drop_n,
                )
            self._rows.append(row)
            return len(self._rows) >= self._flush_max_rows

    async def add_many(self, rows: Iterable[Any]) -> bool:
        """Bulk-append; returns True if max-rows was reached after append."""
        rows_list = list(rows)
        if not rows_list:
            return False
        async with self._lock:
            if len(self._rows) + len(rows_list) > _BUFFER_HARD_CAP:
                # Make room by dropping oldest half first.
                drop_n = _BUFFER_HARD_CAP // 2
                del self._rows[:drop_n]
                self._rows_dropped_total += drop_n  # B-H1 fix
                log.critical(
                    "microstructure buffer hard-cap hit on %s: dropped %d oldest rows",
                    self._table, drop_n,
                )
            self._rows.extend(rows_list)
            return len(self._rows) >= self._flush_max_rows

    async def drain(self) -> list[Any]:
        """Remove and return all buffered rows under the lock.

        Callers perform DB I/O outside the lock with the returned list.
        """
        async with self._lock:
            if not self._rows:
                return []
            to_write, self._rows = self._rows, []
            return to_write


# ---------------------------------------------------------------------------
# Collector — glue: WS client → parsers → 4 buffers → 4 writers
# ---------------------------------------------------------------------------

class MicrostructureCollector:
    """High-level collector orchestrating WS subscription + buffered writes.

    Constructor dependencies (see also :class:`MicrostructureWSClient`):

    * ``settings``: parsed :class:`AATSSettings` (only WS URL + reconnect
      tunings are used — no profile / role validation is required).
    * ``symbols``: subscribed OKX instIds.
    * ``metrics_registry``: optional :class:`MetricsRegistry` for Prometheus
      counters; if ``None`` the collector runs without observability
      plumbing (primarily for tests).

    The constructor does not create an ingest run — that happens inside
    :meth:`run_forever` because creating a DB row in ``__init__`` would
    couple the object lifecycle to DB availability. A caller that only
    wants to exercise the parser or buffer in a test does not need a DB.
    """

    def __init__(
        self,
        *,
        settings: AATSSettings,
        symbols: Iterable[str] = _DEFAULT_SYMBOLS,
        metrics_registry: MetricsRegistry | None = None,
        # buffer tuning (Stage 2 commissioning knob; defaults align with §6.6)
        flush_trades_max_rows: int = _FLUSH_TRADES_ROWS,
        flush_trades_max_seconds: float = _FLUSH_TRADES_SECONDS,
        flush_bbo_max_rows: int = _FLUSH_BBO_ROWS,
        flush_bbo_max_seconds: float = _FLUSH_BBO_SECONDS,
        flush_books5_max_rows: int = _FLUSH_BOOKS5_ROWS,
        flush_books5_max_seconds: float = _FLUSH_BOOKS5_SECONDS,
        flush_oif_max_rows: int = _FLUSH_OIF_ROWS,
        flush_oif_max_seconds: float = _FLUSH_OIF_SECONDS,
        bbo_min_interval_seconds: float = _BBO_MIN_INTERVAL_SECONDS,
        books5_min_interval_seconds: float = _BOOKS5_MIN_INTERVAL_SECONDS,
    ) -> None:
        self._client = MicrostructureWSClient(settings=settings, symbols=symbols)
        self._metrics = metrics_registry
        self._ingest_run_id: str | None = None
        self._symbols = tuple(self._client.symbols)

        # 4 independent buffers, one per target table.
        self._buf_trades = MicrostructureBronzeBuffer(
            table="bronze.market_trades",
            flush_max_rows=flush_trades_max_rows,
            flush_max_seconds=flush_trades_max_seconds,
        )
        self._buf_bbo = MicrostructureBronzeBuffer(
            table="bronze.market_orderbook_bbo",
            flush_max_rows=flush_bbo_max_rows,
            flush_max_seconds=flush_bbo_max_seconds,
        )
        self._buf_books5 = MicrostructureBronzeBuffer(
            table="bronze.market_orderbook_books5",
            flush_max_rows=flush_books5_max_rows,
            flush_max_seconds=flush_books5_max_seconds,
        )
        self._buf_oif = MicrostructureBronzeBuffer(
            table="staging.market_oi_funding_ticks",
            flush_max_rows=flush_oif_max_rows,
            flush_max_seconds=flush_oif_max_seconds,
        )

        # Per-symbol sampling throttles.
        self._bbo_min_interval = max(0.0, float(bbo_min_interval_seconds))
        self._books5_min_interval = max(0.0, float(books5_min_interval_seconds))
        self._last_bbo_sample: dict[str, datetime] = {}
        self._last_books5_sample: dict[str, datetime] = {}

        # Per-table persisted counters (cumulative across the daemon run).
        self._written_counts: dict[str, int] = {
            "bronze.market_trades": 0,
            "bronze.market_orderbook_bbo": 0,
            "bronze.market_orderbook_books5": 0,
            "staging.market_oi_funding_ticks": 0,
        }
        # Flush-failure + hard-cap-drop counters used at shutdown to derive
        # ingest_run status (see 2026-04-20 code review B-H1: prior版本硬编码
        # `status="succeeded"` 会在 DB 下线几小时 + drop thousands of rows 的
        # 场景下仍 emit "succeeded", 复制 P0-a 假成功模式). 新语义:
        #   - 全部 flush 0 error + 0 drop → "succeeded"
        #   - 有 error 或 drop 但至少部分 written → "degraded" (新加的 status value)
        #   - 全部 error + 0 written → "failed"
        self._flush_errors_count: int = 0
        self._rows_dropped_hardcap: int = 0
        # 2026-04-20 code review Issue 5b fix: 区分 initial connect vs reconnect
        # 给 P1-D dashboard 的 microstructure_ws_reconnect_total counter 用.
        self._seen_initial_connect: bool = False

    @property
    def client(self) -> MicrostructureWSClient:
        return self._client

    @property
    def ingest_run_id(self) -> str | None:
        return self._ingest_run_id

    # -- Metrics helper ------------------------------------------------------

    def _metric_inc(self, name: str, value: int = 1) -> None:
        if self._metrics is not None:
            self._metrics.increment(name, value)

    # -- Status --------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Aggregated status snapshot for the daemon heartbeat."""
        conn = self._client.connection_status(_CONNECTION)
        return {
            "connected": conn["connected"],
            "last_message_ts": conn["last_message_ts"],
            "last_error": conn["last_error"],
            "ingest_run_id": self._ingest_run_id,
            "symbols": list(self._symbols),
            "buffered": {
                "trades": self._buf_trades.buffered(),
                "bbo": self._buf_bbo.buffered(),
                "books5": self._buf_books5.buffered(),
                "oi_funding_mark": self._buf_oif.buffered(),
            },
            "written_counts": dict(self._written_counts),
        }

    # -- Message dispatch ----------------------------------------------------

    async def _handle_message(self, message: dict[str, Any]) -> None:
        """Route one OKX message to the appropriate parser + buffer."""
        self._metric_inc("microstructure_ws_messages_total")
        channel = _arg_channel(message)
        if not channel:
            return
        try:
            if channel == _CHANNEL_TRADES:
                rows = parse_trades_message(message)
                if rows and await self._buf_trades.add_many(rows):
                    await self._flush_trades(reason="max_rows")
            elif channel == _CHANNEL_BBO:
                rows = parse_bbo_message(message)
                sampled = self._throttle_bbo(rows)
                if sampled and await self._buf_bbo.add_many(sampled):
                    await self._flush_bbo(reason="max_rows")
            elif channel == _CHANNEL_BOOKS5:
                rows = parse_books5_message(message)
                sampled = self._throttle_books5(rows)
                if sampled and await self._buf_books5.add_many(sampled):
                    await self._flush_books5(reason="max_rows")
            elif channel in (_CHANNEL_OI, _CHANNEL_FUNDING, _CHANNEL_MARK):
                rows = parse_oi_funding_mark_message(message)
                if rows and await self._buf_oif.add_many(rows):
                    await self._flush_oif(reason="max_rows")
            # else: control / unknown channel — ignore
        except Exception:       # noqa: BLE001 — defence in depth, per channel
            log.exception("microstructure message dispatch failed (channel=%s)", channel)

    def _throttle_bbo(self, rows: list[BboRow]) -> list[BboRow]:
        """Keep at most 1 sample per symbol per ``bbo_min_interval_seconds``.

        Returns a new list with the client-sample ``ts`` overwritten to the
        throttle-decision timestamp (so the Bronze row reflects the sampling
        instant, not the OKX push time — ``source_ts`` still carries the
        OKX original).
        """
        if self._bbo_min_interval <= 0:
            return rows
        out: list[BboRow] = []
        now = utc_now()
        for r in rows:
            last = self._last_bbo_sample.get(r.symbol)
            if last is not None and (now - last).total_seconds() < self._bbo_min_interval:
                continue
            self._last_bbo_sample[r.symbol] = now
            # client-side sample instant replaces OKX push ts in the `ts` column.
            out.append(BboRow(
                symbol=r.symbol,
                ts=now,
                source_ts=r.source_ts,
                bid_px=r.bid_px, bid_sz=r.bid_sz,
                ask_px=r.ask_px, ask_sz=r.ask_sz,
            ))
        return out

    def _throttle_books5(self, rows: list[Books5Row]) -> list[Books5Row]:
        if self._books5_min_interval <= 0:
            return rows
        out: list[Books5Row] = []
        now = utc_now()
        for r in rows:
            last = self._last_books5_sample.get(r.symbol)
            if last is not None and (now - last).total_seconds() < self._books5_min_interval:
                continue
            self._last_books5_sample[r.symbol] = now
            out.append(Books5Row(
                symbol=r.symbol,
                ts=now,
                source_ts=r.source_ts,
                bid_px_1=r.bid_px_1, bid_sz_1=r.bid_sz_1,
                bid_px_2=r.bid_px_2, bid_sz_2=r.bid_sz_2,
                bid_px_3=r.bid_px_3, bid_sz_3=r.bid_sz_3,
                bid_px_4=r.bid_px_4, bid_sz_4=r.bid_sz_4,
                bid_px_5=r.bid_px_5, bid_sz_5=r.bid_sz_5,
                ask_px_1=r.ask_px_1, ask_sz_1=r.ask_sz_1,
                ask_px_2=r.ask_px_2, ask_sz_2=r.ask_sz_2,
                ask_px_3=r.ask_px_3, ask_sz_3=r.ask_sz_3,
                ask_px_4=r.ask_px_4, ask_sz_4=r.ask_sz_4,
                ask_px_5=r.ask_px_5, ask_sz_5=r.ask_sz_5,
            ))
        return out

    # -- Flush ---------------------------------------------------------------

    async def _flush_with_writer(
        self,
        *,
        buffer: MicrostructureBronzeBuffer,
        reason: str,
        writer: Any,                          # callable(session, rows, **kw)
        needs_run_id: bool,
    ) -> FlushResult:
        to_write = await buffer.drain()
        if not to_write:
            return FlushResult(attempted=0, written=0, reason=reason)
        try:
            with get_session() as session:
                if needs_run_id:
                    if self._ingest_run_id is None:
                        # This path is only reachable if flush is driven
                        # outside of run_forever (e.g. tests). We fall back
                        # to a NULL-ish UUID string; the DB column is NOT
                        # NULL so the insert will fail and be caught below.
                        raise RuntimeError("ingest_run_id not initialized")
                    written = writer(session, to_write, ingest_run_id=self._ingest_run_id)
                else:
                    written = writer(session, to_write)
            self._written_counts[buffer.table] += written
            self._metric_inc("microstructure_bronze_flush_total")
            self._metric_inc("microstructure_bronze_rows_written_total", int(written))
            # 2026-04-20 code review Issue 5b fix:
            # P1-D dashboard 期望按表拆的 counter (trades/bbo/books5/oif),
            # 以前只有聚合 counter, dashboard panel 永无数据. 这里额外 emit
            # per-table counter, 保持向后兼容聚合版本.
            # buffer.table 形如 "bronze.market_trades" / "bronze.market_orderbook_bbo" /
            # "bronze.market_orderbook_books5" / "staging.market_oi_funding_ticks"
            # 把 "bronze.market_" / "staging.market_" 前缀去掉做 metric suffix.
            _table_suffix_map = {
                "bronze.market_trades": "trades",
                "bronze.market_orderbook_bbo": "bbo",
                "bronze.market_orderbook_books5": "books5",
                "staging.market_oi_funding_ticks": "oif",
            }
            _suffix = _table_suffix_map.get(buffer.table)
            if _suffix:
                self._metric_inc(
                    f"microstructure_bronze_rows_written_{_suffix}_total",
                    int(written),
                )
            log.info(
                "flushed %d/%d rows to %s (reason=%s, cumulative=%d)",
                written, len(to_write), buffer.table, reason,
                self._written_counts[buffer.table],
            )
            return FlushResult(attempted=len(to_write), written=int(written), reason=reason)
        except (SQLAlchemyError, OSError) as exc:
            # Narrow catch: DB / connectivity issues drop the batch. Same
            # trade-off as LiquidationsCollector — rows are not re-queued
            # because unbounded retry during a prolonged outage would grow
            # memory without bound.
            self._metric_inc("microstructure_bronze_flush_errors_total")
            # B-H1 fix (2026-04-20 code review): track errors to derive
            # ingest_run status at shutdown instead of hardcoded "succeeded".
            self._flush_errors_count += 1
            log.exception("%s flush failed; %d rows dropped", buffer.table, len(to_write))
            return FlushResult(
                attempted=len(to_write),
                written=0,
                reason=reason,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _flush_trades(self, *, reason: str) -> FlushResult:
        return await self._flush_with_writer(
            buffer=self._buf_trades,
            reason=reason,
            writer=write_trades_batch,
            needs_run_id=True,
        )

    async def _flush_bbo(self, *, reason: str) -> FlushResult:
        return await self._flush_with_writer(
            buffer=self._buf_bbo,
            reason=reason,
            writer=write_bbo_batch,
            needs_run_id=True,
        )

    async def _flush_books5(self, *, reason: str) -> FlushResult:
        return await self._flush_with_writer(
            buffer=self._buf_books5,
            reason=reason,
            writer=write_books5_batch,
            needs_run_id=True,
        )

    async def _flush_oif(self, *, reason: str) -> FlushResult:
        return await self._flush_with_writer(
            buffer=self._buf_oif,
            reason=reason,
            writer=write_oif_batch,
            needs_run_id=False,
        )

    async def _flush_all(self, *, reason: str) -> None:
        # Run the four flushes concurrently. Each handles its own errors.
        await asyncio.gather(
            self._flush_trades(reason=reason),
            self._flush_bbo(reason=reason),
            self._flush_books5(reason=reason),
            self._flush_oif(reason=reason),
            return_exceptions=False,
        )

    # -- Reconnect watcher ---------------------------------------------------

    async def _watch_reconnect_loop(self) -> None:
        """Poll ``connection_status`` 每 5s 一次, 检测 False→True transition.

        2026-04-20 code review Issue 5b: P1-D dashboard 期望
        ``aats_microstructure_ws_reconnect_total`` counter, 但 base class
        ``run_forever`` 阻塞不暴露 reconnect event. poll connection_status
        state 变化是最小侵入的方式 (不改 base class).

        初次 connect 不计 reconnect (已由 run_forever 开头 emit connect_total).
        5s cadence 对 dashboard 足够; 短于此的闪断合并计一次.
        """
        last_connected: bool = False
        while True:
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                return
            try:
                status = self._client.connection_status(_CONNECTION)
                now_connected = bool(status.get("connected", False))
                if not last_connected and now_connected:
                    if self._seen_initial_connect:
                        # 已经经过 initial connect, 这是一次 reconnect
                        self._metric_inc("microstructure_ws_reconnect_total")
                        log.info("microstructure WS reconnected (counter +1)")
                    else:
                        self._seen_initial_connect = True
                last_connected = now_connected
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                # 不让 watcher 因 transient 错误挂掉
                log.warning("reconnect watcher poll failed: %r", exc)

    # -- Periodic flush loop -------------------------------------------------

    async def _periodic_flush_loop(self) -> None:
        """Drive the four buffers' timeout-based flushes independently.

        Running four separate sleep loops rather than a single min-interval
        sleep keeps each table's flush cadence as tight as its own
        ``flush_max_seconds`` — a slow trades flush does not delay the
        books5 flush, which typically wants lower latency.
        """
        tasks = [
            asyncio.create_task(self._periodic_flush_for(self._buf_trades, self._flush_trades)),
            asyncio.create_task(self._periodic_flush_for(self._buf_bbo, self._flush_bbo)),
            asyncio.create_task(self._periodic_flush_for(self._buf_books5, self._flush_books5)),
            asyncio.create_task(self._periodic_flush_for(self._buf_oif, self._flush_oif)),
        ]
        try:
            await asyncio.gather(*tasks, return_exceptions=False)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await t
            raise

    async def _periodic_flush_for(
        self,
        buffer: MicrostructureBronzeBuffer,
        flush_callable: Any,
    ) -> None:
        stop = self._client.stop_event
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=buffer.flush_max_seconds)
                return
            except TimeoutError:
                pass
            await flush_callable(reason="timeout")

    # -- Lifecycle -----------------------------------------------------------

    async def run_forever(self) -> None:
        """Open the WS connection, create the ingest_run, and run until stop.

        Ingest-run lifecycle: one run per daemon process — the BTC trades
        rate (≥ 1 row / s peak) makes finer-grained runs (one per minute,
        etc.) inject thousands of rows per day into ``meta.ingest_runs``
        for little added provenance value.
        """
        # Create the run row. If DB is unreachable, we surface the error
        # immediately rather than silently running without provenance.
        try:
            with get_session() as session:
                orphaned_runs = mark_orphaned_ingest_runs(
                    session,
                    run_type="rolling",
                    dataset_domain="microstructure",
                    instrument_type="SWAP",
                    trigger_mode="daemon",
                    reason=(
                        "orphaned_by_microstructure_daemon_startup:"
                        " previous daemon did not close ingest_run"
                    ),
                )
                self._ingest_run_id = create_ingest_run(
                    session,
                    run_type="rolling",
                    dataset_domain="microstructure",
                    instrument_type="SWAP",
                    trigger_mode="daemon",
                )
            self._metric_inc("microstructure_ws_connect_total")
            log.info(
                "microstructure ingest_run created: run_id=%s symbols=%s orphaned_runs_closed=%d",
                self._ingest_run_id, list(self._symbols), orphaned_runs,
            )
        except SQLAlchemyError:
            log.exception("failed to create ingest_run — aborting collector startup")
            raise

        flush_task = asyncio.create_task(self._periodic_flush_loop())
        # 2026-04-20 code review Issue 5b fix: 独立 task poll 连接状态
        # 检测 reconnect. base class run_forever 阻塞不暴露 reconnect event,
        # 只能 poll connection_status. 5s cadence 对 dashboard 足够.
        reconnect_watch_task = asyncio.create_task(self._watch_reconnect_loop())
        try:
            await self._client.run_forever(on_message=self._handle_message)
        finally:
            # Drain any pending rows on the way out — best-effort.
            try:
                await self._flush_all(reason="shutdown")
            except Exception:    # noqa: BLE001
                log.exception("shutdown flush failed")
            flush_task.cancel()
            reconnect_watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await flush_task
            with contextlib.suppress(asyncio.CancelledError):
                await reconnect_watch_task
            # Close the ingest_run with **derived** status (not hardcoded).
            # 2026-04-20 code review B-H1 fix:
            #   之前: 硬编码 status="succeeded" → DB 下线数小时 drop thousands
            #         of rows 后 meta.ingest_runs 仍显示 "succeeded", 运营只能
            #         靠 Prometheus counter 发现真相, 是 P0-a 假成功模式在
            #         Bronze 层的残余.
            #   新语义:
            #     total_written == 0 AND flush_errors > 0   → "failed"
            #     flush_errors > 0 OR rows_dropped > 0      → "retrying"
            #       (语义上是 "partial success", chk_ir_status 允许 retrying,
            #        日批 / daily ingest 观察到 retrying 会记 audit + 人工 review)
            #     else                                       → "succeeded"
            if self._ingest_run_id is not None:
                total_written = sum(self._written_counts.values())
                # 从各 buffer 聚合 hard-cap drop 累计
                total_dropped = (
                    self._buf_trades.rows_dropped_total
                    + self._buf_bbo.rows_dropped_total
                    + self._buf_books5.rows_dropped_total
                    + self._buf_oif.rows_dropped_total
                )
                if total_written == 0 and self._flush_errors_count > 0:
                    derived_status = "failed"
                elif self._flush_errors_count > 0 or total_dropped > 0:
                    derived_status = "retrying"
                else:
                    derived_status = "succeeded"

                log.info(
                    "finishing ingest_run %s status=%s written=%d flush_errors=%d dropped=%d",
                    self._ingest_run_id,
                    derived_status,
                    total_written,
                    self._flush_errors_count,
                    total_dropped,
                )
                try:
                    with get_session() as session:
                        finish_ingest_run(
                            session,
                            self._ingest_run_id,
                            status=derived_status,
                        )
                except SQLAlchemyError:
                    log.exception("failed to close ingest_run %s", self._ingest_run_id)

    async def stop(self) -> None:
        await self._client.stop()


__all__ = [
    "BboRow",
    "Books5Row",
    "FlushResult",
    "MicrostructureBronzeBuffer",
    "MicrostructureCollector",
    "MicrostructureWSClient",
    "OiFundingMarkRow",
    "TradeRow",
    "parse_bbo_message",
    "parse_books5_message",
    "parse_oi_funding_mark_message",
    "parse_trades_message",
    "write_bbo_batch",
    "write_books5_batch",
    "write_oif_batch",
    "write_trades_batch",
]
