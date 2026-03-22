from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from decimal import Decimal
from typing import Any

from aats.bootstrap.logging import get_logger, log_event
from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.market import MarketSnapshot
from aats.services.market_gateway.normalizer import MarketSnapshotNormalizer
from aats.services.market_gateway.okx_normalizer import (
    OKXInstrumentMarketState,
    OKXMarketSnapshotNormalizer,
)
from aats.services.market_gateway.okx_websocket import OKXPublicWebSocketClient
from aats.services.market_gateway.publisher import MarketSnapshotPublisher
from aats.services.execution_engine.okx_rest import OKXRESTClient


class MarketDataGateway:
    _PRICE_DELTAS: tuple[Decimal, ...] = (
        Decimal("320.0"),
        Decimal("260.0"),
        Decimal("-380.0"),
        Decimal("-310.0"),
        Decimal("240.0"),
        Decimal("-270.0"),
    )

    def __init__(
        self,
        *,
        settings: AATSSettings,
        normalizer: MarketSnapshotNormalizer,
        publisher: MarketSnapshotPublisher,
        okx_normalizer: OKXMarketSnapshotNormalizer | None = None,
        okx_ws_client: OKXPublicWebSocketClient | None = None,
        okx_rest_client: OKXRESTClient | None = None,
    ) -> None:
        self.settings = settings
        self.normalizer = normalizer
        self.publisher = publisher
        self.okx_normalizer = okx_normalizer or OKXMarketSnapshotNormalizer(exchange_name="OKX")
        self.okx_ws_client = okx_ws_client
        self.okx_rest_client = okx_rest_client
        self.logger = get_logger("aats.market_gateway")
        self._latest_snapshots: dict[str, MarketSnapshot] = {}
        self._latest_received_at: dict[str, Any] = {}
        self._tick_by_symbol: dict[str, int] = {}
        self._okx_states: dict[str, OKXInstrumentMarketState] = {}
        self._background_task: asyncio.Task[None] | None = None
        self._fallback_task: asyncio.Task[None] | None = None
        self._last_publish_ts = None
        self._last_error: str | None = None
        self._rest_fallback_last_success_ts = None
        self._rest_fallback_last_attempt_ts = None
        self._rest_fallback_last_error: str | None = None
        self._rest_fallback_active = False

    async def start(self) -> None:
        if self.settings.market_data_backend != "okx":
            return
        if self.okx_ws_client is not None:
            if self._background_task is None or self._background_task.done():
                self._background_task = asyncio.create_task(self._run_okx_stream(), name="aats_okx_market_stream")
        if (
            self.settings.okx_market_rest_fallback_enabled
            and self.okx_rest_client is not None
            and (self._fallback_task is None or self._fallback_task.done())
        ):
            self._fallback_task = asyncio.create_task(
                self._run_okx_rest_fallback_loop(),
                name="aats_okx_market_rest_fallback",
            )

    async def stop(self) -> None:
        if self.okx_ws_client is not None:
            await self.okx_ws_client.stop()
        if self._background_task is not None:
            self._background_task.cancel()
            try:
                await self._background_task
            except asyncio.CancelledError:
                pass
            self._background_task = None
        if self._fallback_task is not None:
            self._fallback_task.cancel()
            try:
                await self._fallback_task
            except asyncio.CancelledError:
                pass
            self._fallback_task = None

    async def publish_local_snapshot(self, symbol: str | None = None) -> MarketSnapshot:
        trading_symbol = symbol or self.settings.default_symbol
        snapshot = self.normalizer.normalize(self._build_local_payload(trading_symbol))
        await self._publish_snapshot(snapshot)
        return snapshot

    async def seed_demo_snapshot(self, symbol: str | None = None) -> MarketSnapshot:
        return await self.publish_local_snapshot(symbol=symbol)

    async def run_local_publisher(
        self,
        *,
        symbol: str | None = None,
        iterations: int,
        interval_seconds: float,
    ) -> list[MarketSnapshot]:
        snapshots: list[MarketSnapshot] = []
        for index in range(iterations):
            snapshots.append(await self.publish_local_snapshot(symbol=symbol))
            if interval_seconds > 0 and index + 1 < iterations:
                await asyncio.sleep(interval_seconds)
        return snapshots

    def latest_snapshot(self, symbol: str) -> MarketSnapshot | None:
        return self._latest_snapshots.get(symbol)

    def latest_price(self, symbol: str) -> Decimal:
        snapshot = self._latest_snapshots.get(symbol)
        return snapshot.last_price if snapshot is not None else Decimal("0")

    def is_fresh(self, symbol: str) -> bool:
        return self.receipt_is_fresh(symbol) and self.snapshot_is_fresh(symbol)

    def receipt_is_fresh(self, symbol: str) -> bool:
        received_at = self._latest_received_at.get(symbol)
        if received_at is None:
            return False
        age_seconds = (utc_now() - received_at).total_seconds()
        return age_seconds <= self.settings.market_data_stale_after_seconds

    def snapshot_is_fresh(self, symbol: str) -> bool:
        snapshot = self._latest_snapshots.get(symbol)
        if snapshot is None:
            return False
        age_seconds = (utc_now() - snapshot.snapshot_ts).total_seconds()
        return age_seconds <= self.settings.market_data_stale_after_seconds

    def status(self) -> dict[str, Any]:
        default_snapshot = self._latest_snapshots.get(self.settings.default_symbol)
        last_received_ts = self._latest_received_at.get(self.settings.default_symbol)
        connected = True
        last_update_ts = last_received_ts or (default_snapshot.snapshot_ts if default_snapshot is not None else None)
        detail = "demo_market_data"
        transport_connected = True
        transport_connected_public: bool | None = None
        transport_connected_business: bool | None = None
        if self.settings.market_data_backend == "okx" and self.okx_ws_client is not None:
            okx_status = self.okx_ws_client.status()
            transport_connected = bool(okx_status["connected"])
            transport_connected_public = bool(okx_status.get("connected_public", False))
            transport_connected_business = bool(okx_status.get("connected_business", False))
            last_update_ts = okx_status.get("last_message_ts") or last_update_ts
            detail = "okx_public_ws"
            self._last_error = okx_status.get("last_error")
        receipt_fresh = self.receipt_is_fresh(self.settings.default_symbol)
        snapshot_fresh = self.snapshot_is_fresh(self.settings.default_symbol)
        fresh = receipt_fresh and snapshot_fresh
        if self.settings.market_data_backend == "okx":
            connected = transport_connected or receipt_fresh
        blockers: list[str] = []
        if not transport_connected and not receipt_fresh:
            blockers.append("market_connection_down")
        if not fresh:
            blockers.append("market_data_stale")
        if not transport_connected and receipt_fresh:
            detail = f"{detail}_transport_degraded"
        if self._rest_fallback_active:
            detail = f"{detail}_rest_fallback"
        return {
            "backend": self.settings.market_data_backend,
            "connected": connected,
            "transport_connected": transport_connected,
            "transport_connected_public": transport_connected_public,
            "transport_connected_business": transport_connected_business,
            "fresh": fresh,
            "receipt_fresh": receipt_fresh,
            "snapshot_fresh": snapshot_fresh,
            "last_update_ts": last_update_ts,
            "market_snapshot_ts": default_snapshot.snapshot_ts if default_snapshot is not None else None,
            "last_error": self._last_error,
            "rest_fallback_enabled": bool(self.settings.okx_market_rest_fallback_enabled and self.okx_rest_client is not None),
            "rest_fallback_active": self._rest_fallback_active,
            "rest_fallback_last_attempt_ts": self._rest_fallback_last_attempt_ts,
            "rest_fallback_last_success_ts": self._rest_fallback_last_success_ts,
            "rest_fallback_last_error": self._rest_fallback_last_error,
            "detail": detail,
            "blockers": blockers,
            "ready": fresh,
        }

    async def _run_okx_stream(self) -> None:
        if self.okx_ws_client is None:
            return
        log_event(self.logger, "market_stream_started", backend="okx")
        await self.okx_ws_client.run_forever(on_message=self._handle_okx_message)

    async def _run_okx_rest_fallback_loop(self) -> None:
        while True:
            try:
                await self._run_okx_rest_fallback_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._rest_fallback_last_error = str(exc)
                log_event(
                    self.logger,
                    "okx_market_rest_fallback_error",
                    level="error",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            await asyncio.sleep(self.settings.okx_market_rest_fallback_poll_interval_seconds)

    async def _run_okx_rest_fallback_once(self) -> bool:
        if (
            self.settings.market_data_backend != "okx"
            or not self.settings.okx_market_rest_fallback_enabled
            or self.okx_rest_client is None
        ):
            self._rest_fallback_active = False
            return False
        symbol = self.settings.default_symbol
        if self.is_fresh(symbol):
            self._rest_fallback_active = False
            return False
        self._rest_fallback_last_attempt_ts = utc_now()
        snapshot = await self._fetch_okx_rest_snapshot(symbol=symbol)
        await self._publish_snapshot(snapshot)
        self._rest_fallback_last_success_ts = utc_now()
        self._rest_fallback_last_error = None
        self._rest_fallback_active = True
        log_event(
            self.logger,
            "okx_market_rest_fallback_published",
            symbol=symbol,
            snapshot_ts=snapshot.snapshot_ts.isoformat(),
        )
        return True

    async def _fetch_okx_rest_snapshot(self, *, symbol: str) -> MarketSnapshot:
        if self.okx_rest_client is None:
            raise RuntimeError("okx_rest_client_unavailable")
        ticker_payload, candle_15m_payload, candle_1h_payload = await asyncio.gather(
            self.okx_rest_client.get_market_ticker(symbol=symbol),
            self.okx_rest_client.get_market_candles(symbol=symbol, bar="15m", limit=1),
            self.okx_rest_client.get_market_candles(symbol=symbol, bar="1H", limit=1),
        )
        ticker_rows = ticker_payload.get("data", [])
        candle_15m_rows = candle_15m_payload.get("data", [])
        candle_1h_rows = candle_1h_payload.get("data", [])
        if not isinstance(ticker_rows, list) or not ticker_rows or not isinstance(ticker_rows[0], dict):
            raise RuntimeError("okx_market_rest_ticker_missing")
        if not isinstance(candle_15m_rows, list) or not candle_15m_rows or not isinstance(candle_15m_rows[0], list):
            raise RuntimeError("okx_market_rest_candle_15m_missing")
        if not isinstance(candle_1h_rows, list) or not candle_1h_rows or not isinstance(candle_1h_rows[0], list):
            raise RuntimeError("okx_market_rest_candle_1h_missing")
        return self.okx_normalizer.build_snapshot_from_rest_payloads(
            symbol=symbol,
            ticker_payload=ticker_rows[0],
            candle_15m_payload=candle_15m_rows[0],
            candle_1h_payload=candle_1h_rows[0],
        )

    async def _handle_okx_message(self, message: dict[str, Any]) -> None:
        try:
            snapshots = self.okx_normalizer.apply_message(message=message, states=self._okx_states)
            for snapshot in snapshots:
                await self._publish_snapshot(snapshot)
        except Exception as exc:
            self._last_error = str(exc)
            log_event(
                self.logger,
                "okx_market_message_error",
                level="error",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            # Market transport should stay alive even if a downstream consumer fails.
            return

    async def _publish_snapshot(self, snapshot: MarketSnapshot) -> None:
        received_at = utc_now()
        self._latest_snapshots[snapshot.symbol] = snapshot
        self._latest_received_at[snapshot.symbol] = received_at
        self._last_publish_ts = received_at
        await self.publisher.publish(snapshot)

    def _build_local_payload(self, symbol: str) -> dict[str, Any]:
        tick = self._tick_by_symbol.get(symbol, 0)
        previous_snapshot = self._latest_snapshots.get(symbol)
        previous_price = previous_snapshot.last_price if previous_snapshot is not None else Decimal("67250.0")
        delta = self._PRICE_DELTAS[tick % len(self._PRICE_DELTAS)]
        last_price = previous_price + delta
        high = max(previous_price, last_price) + Decimal("40.0")
        low = min(previous_price, last_price) - Decimal("40.0")
        self._tick_by_symbol[symbol] = tick + 1
        return {
            "symbol": symbol,
            "exchange": self.settings.exchange_name,
            "snapshot_ts": utc_now(),
            "best_bid": last_price - Decimal("5.0"),
            "best_ask": last_price + Decimal("5.0"),
            "last_price": last_price,
            "bid_size": Decimal("1.25") + (Decimal(tick) * Decimal("0.05")),
            "ask_size": Decimal("1.10") + (Decimal(tick) * Decimal("0.04")),
            "volume_24h": Decimal("128500000.0") + (Decimal(tick) * Decimal("250000.0")),
            "kline_15m": {
                "open": previous_price,
                "high": high,
                "low": low,
                "close": last_price,
                "volume": Decimal("1250.0") + (Decimal(tick) * Decimal("50.0")),
            },
            "kline_1h": {
                "open": Decimal("67000.0"),
                "high": max(Decimal("67000.0"), high),
                "low": min(Decimal("67000.0"), low),
                "close": last_price,
                "volume": Decimal("4800.0") + (Decimal(tick) * Decimal("125.0")),
            },
            "recent_trades": [
                {"price": last_price - Decimal("10.0"), "qty": Decimal("0.05"), "side": "buy"},
                {"price": last_price, "qty": Decimal("0.04"), "side": "buy" if delta >= 0 else "sell"},
                {"price": last_price + Decimal("10.0"), "qty": Decimal("0.03"), "side": "sell"},
            ],
            "orderbook_depth": {
                "bids": [
                    [last_price - Decimal("5.0"), Decimal("1.25")],
                    [last_price - Decimal("10.0"), Decimal("1.5")],
                ],
                "asks": [
                    [last_price + Decimal("5.0"), Decimal("1.10")],
                    [last_price + Decimal("10.0"), Decimal("1.35")],
                ],
            },
        }
