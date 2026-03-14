from __future__ import annotations

import asyncio
from typing import Any

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.market import MarketSnapshot
from aats.services.market_gateway.normalizer import MarketSnapshotNormalizer
from aats.services.market_gateway.publisher import MarketSnapshotPublisher


class MarketDataGateway:
    _PRICE_DELTAS: tuple[float, ...] = (320.0, 260.0, -380.0, -310.0, 240.0, -270.0)

    def __init__(
        self,
        *,
        settings: AATSSettings,
        normalizer: MarketSnapshotNormalizer,
        publisher: MarketSnapshotPublisher,
    ) -> None:
        self.settings = settings
        self.normalizer = normalizer
        self.publisher = publisher
        self._latest_snapshots: dict[str, MarketSnapshot] = {}
        self._tick_by_symbol: dict[str, int] = {}

    async def publish_local_snapshot(self, symbol: str | None = None) -> MarketSnapshot:
        trading_symbol = symbol or self.settings.default_symbol
        snapshot = self.normalizer.normalize(self._build_local_payload(trading_symbol))
        self._latest_snapshots[trading_symbol] = snapshot
        await self.publisher.publish(snapshot)
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

    def latest_price(self, symbol: str) -> float:
        snapshot = self._latest_snapshots.get(symbol)
        return snapshot.last_price if snapshot is not None else 0.0

    def _build_local_payload(self, symbol: str) -> dict[str, Any]:
        tick = self._tick_by_symbol.get(symbol, 0)
        previous_snapshot = self._latest_snapshots.get(symbol)
        previous_price = previous_snapshot.last_price if previous_snapshot is not None else 67_250.0
        delta = self._PRICE_DELTAS[tick % len(self._PRICE_DELTAS)]
        last_price = previous_price + delta
        high = max(previous_price, last_price) + 40.0
        low = min(previous_price, last_price) - 40.0
        self._tick_by_symbol[symbol] = tick + 1
        return {
            "symbol": symbol,
            "exchange": self.settings.exchange_name,
            "snapshot_ts": utc_now(),
            "best_bid": last_price - 5.0,
            "best_ask": last_price + 5.0,
            "last_price": last_price,
            "bid_size": 1.25 + (tick * 0.05),
            "ask_size": 1.10 + (tick * 0.04),
            "volume_24h": 128_500_000.0 + (tick * 250_000.0),
            "kline_15m": {
                "open": previous_price,
                "high": high,
                "low": low,
                "close": last_price,
                "volume": 1_250.0 + (tick * 50.0),
            },
            "kline_1h": {
                "open": 67_000.0,
                "high": max(67_000.0, high),
                "low": min(67_000.0, low),
                "close": last_price,
                "volume": 4_800.0 + (tick * 125.0),
            },
            "recent_trades": [
                {"price": last_price - 10.0, "qty": 0.05, "side": "buy"},
                {"price": last_price, "qty": 0.04, "side": "buy" if delta >= 0 else "sell"},
                {"price": last_price + 10.0, "qty": 0.03, "side": "sell"},
            ],
            "orderbook_depth": {
                "bids": [[last_price - 5.0, 1.25], [last_price - 10.0, 1.5]],
                "asks": [[last_price + 5.0, 1.10], [last_price + 10.0, 1.35]],
            },
        }
