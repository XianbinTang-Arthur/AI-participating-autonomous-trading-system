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
    ) -> None:
        self.settings = settings
        self.normalizer = normalizer
        self.publisher = publisher
        self.okx_normalizer = okx_normalizer or OKXMarketSnapshotNormalizer(exchange_name="OKX")
        self.okx_ws_client = okx_ws_client
        self.logger = get_logger("aats.market_gateway")
        self._latest_snapshots: dict[str, MarketSnapshot] = {}
        self._tick_by_symbol: dict[str, int] = {}
        self._okx_states: dict[str, OKXInstrumentMarketState] = {}
        self._background_task: asyncio.Task[None] | None = None
        self._last_publish_ts = None
        self._last_error: str | None = None

    async def start(self) -> None:
        if self.settings.market_data_backend != "okx" or self.okx_ws_client is None:
            return
        if self._background_task is not None and not self._background_task.done():
            return
        self._background_task = asyncio.create_task(self._run_okx_stream(), name="aats_okx_market_stream")

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
        snapshot = self._latest_snapshots.get(symbol)
        if snapshot is None:
            return False
        age_seconds = (utc_now() - snapshot.snapshot_ts).total_seconds()
        return age_seconds <= self.settings.market_data_stale_after_seconds

    def status(self) -> dict[str, Any]:
        default_snapshot = self._latest_snapshots.get(self.settings.default_symbol)
        connected = True
        last_update_ts = default_snapshot.snapshot_ts if default_snapshot is not None else None
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
        fresh = self.is_fresh(self.settings.default_symbol)
        if self.settings.market_data_backend == "okx":
            connected = transport_connected or fresh
        blockers: list[str] = []
        if not transport_connected and not fresh:
            blockers.append("market_connection_down")
        if not fresh:
            blockers.append("market_data_stale")
        if not transport_connected and fresh:
            detail = f"{detail}_transport_degraded"
        return {
            "backend": self.settings.market_data_backend,
            "connected": connected,
            "transport_connected": transport_connected,
            "transport_connected_public": transport_connected_public,
            "transport_connected_business": transport_connected_business,
            "fresh": fresh,
            "last_update_ts": last_update_ts,
            "last_error": self._last_error,
            "detail": detail,
            "blockers": blockers,
            "ready": fresh,
        }

    async def _run_okx_stream(self) -> None:
        if self.okx_ws_client is None:
            return
        log_event(self.logger, "market_stream_started", backend="okx")
        await self.okx_ws_client.run_forever(on_message=self._handle_okx_message)

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
        self._latest_snapshots[snapshot.symbol] = snapshot
        self._last_publish_ts = snapshot.snapshot_ts
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
