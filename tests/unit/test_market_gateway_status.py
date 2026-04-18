from __future__ import annotations

import unittest

from aats.bootstrap.settings import AATSSettings
from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.schemas.common import utc_now
from aats.schemas.market import MarketSnapshot
from aats.services.market_gateway.gateway import MarketDataGateway
from aats.services.market_gateway.normalizer import MarketSnapshotNormalizer
from aats.services.market_gateway.okx_websocket import OKXPublicWebSocketClient
from aats.services.market_gateway.publisher import MarketSnapshotPublisher


class _FakeWSClient:
    def __init__(self, *, connected_public: bool, connected_business: bool, last_error: str | None = None) -> None:
        self.connected_public = connected_public
        self.connected_business = connected_business
        self.last_error = last_error

    def status(self):
        return {
            "connected_public": self.connected_public,
            "connected_business": self.connected_business,
            "connected": self.connected_public and self.connected_business,
            "last_message_ts": utc_now(),
            "last_error": self.last_error,
        }


class _FakeOKXNormalizer:
    def __init__(self, snapshot: MarketSnapshot) -> None:
        self.snapshot = snapshot

    def apply_message(self, *, message, states):
        return [self.snapshot]


class _FakeOKXRESTClient:
    def __init__(self) -> None:
        self.ticker_calls = 0
        self.candle_calls = 0
        self.ticker_symbols: list[str] = []
        self.candle_symbols: list[tuple[str, str]] = []

    async def get_market_ticker(self, *, symbol: str):
        self.ticker_calls += 1
        self.ticker_symbols.append(symbol)
        return {
            "code": "0",
            "data": [
                {
                    "instId": symbol,
                    "ts": str(int(utc_now().timestamp() * 1000)),
                    "bidPx": "100000",
                    "askPx": "100010",
                    "last": "100005",
                    "bidSz": "1.2",
                    "askSz": "1.1",
                    "vol24h": "1000",
                }
            ],
        }

    async def get_market_candles(self, *, symbol: str, bar: str, limit: int = 1):
        self.candle_calls += 1
        self.candle_symbols.append((symbol, bar))
        snapshot_ms = str(int(utc_now().timestamp() * 1000))
        return {
            "code": "0",
            "data": [
                [snapshot_ms, "99900", "100100", "99800", "100005", "10", "10", "100000", "1"],
            ],
        }


class TestMarketGatewayStatus(unittest.IsolatedAsyncioTestCase):
    def test_okx_public_websocket_subscribes_all_tracked_symbols(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "market_data_backend": "okx",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_enabled": True,
            }
        )
        client = OKXPublicWebSocketClient(settings=settings)

        public_args, business_args = client._subscription_args()

        public_symbols = {item["instId"] for item in public_args}
        business_symbols = {item["instId"] for item in business_args}
        self.assertEqual(public_symbols, {"BTC-USDT", "BTC-USDT-SWAP"})
        self.assertEqual(business_symbols, {"BTC-USDT", "BTC-USDT-SWAP"})

    async def test_stale_market_data_is_detected(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "market_data_backend": "demo",
                "market_data_stale_after_seconds": 1.0,
            }
        )
        gateway = MarketDataGateway(
            settings=settings,
            normalizer=MarketSnapshotNormalizer(exchange_name="PAPER"),
            publisher=MarketSnapshotPublisher(bus=InMemoryEventBus()),
        )
        snapshot = await gateway.publish_local_snapshot(symbol="BTC-USDT")
        gateway._latest_snapshots["BTC-USDT"] = snapshot.model_copy(
            update={"snapshot_ts": utc_now().replace(year=utc_now().year - 1)}
        )
        gateway._latest_received_at["BTC-USDT"] = utc_now().replace(year=utc_now().year - 1)

        status = gateway.status()
        self.assertFalse(gateway.is_fresh("BTC-USDT"))
        self.assertIn("market_data_stale", status["blockers"])

    async def test_recently_received_snapshot_is_not_fresh_when_snapshot_timestamp_lags(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "market_data_backend": "okx",
                "default_symbol": "BTC-USDT",
                "market_data_stale_after_seconds": 30.0,
            }
        )
        gateway = MarketDataGateway(
            settings=settings,
            normalizer=MarketSnapshotNormalizer(exchange_name="OKX"),
            publisher=MarketSnapshotPublisher(bus=InMemoryEventBus()),
            okx_ws_client=_FakeWSClient(connected_public=True, connected_business=True),  # type: ignore[arg-type]
        )
        snapshot = await gateway.publish_local_snapshot(symbol="BTC-USDT")
        gateway._latest_snapshots["BTC-USDT"] = snapshot.model_copy(
            update={"snapshot_ts": utc_now().replace(year=utc_now().year - 1)}
        )
        gateway._latest_received_at["BTC-USDT"] = utc_now()

        status = gateway.status()

        self.assertFalse(gateway.is_fresh("BTC-USDT"))
        self.assertTrue(status["receipt_fresh"])
        self.assertFalse(status["snapshot_fresh"])
        self.assertIn("market_data_stale", status["blockers"])

    async def test_okx_transport_degradation_does_not_block_when_snapshot_is_fresh(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "market_data_backend": "okx",
                "default_symbol": "BTC-USDT",
                "market_data_stale_after_seconds": 30.0,
            }
        )
        gateway = MarketDataGateway(
            settings=settings,
            normalizer=MarketSnapshotNormalizer(exchange_name="OKX"),
            publisher=MarketSnapshotPublisher(bus=InMemoryEventBus()),
            okx_ws_client=_FakeWSClient(connected_public=True, connected_business=False, last_error="business reconnecting"),  # type: ignore[arg-type]
        )
        await gateway.publish_local_snapshot(symbol="BTC-USDT")

        status = gateway.status()

        self.assertTrue(status["fresh"])
        self.assertTrue(status["connected"])
        self.assertTrue(status["ready"])
        self.assertEqual(status["blockers"], [])
        self.assertEqual(status["transport_connected_public"], True)
        self.assertEqual(status["transport_connected_business"], False)
        self.assertIn("transport_degraded", status["detail"])

    async def test_okx_transport_down_blocks_when_no_fresh_snapshot_exists(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "market_data_backend": "okx",
                "default_symbol": "BTC-USDT",
                "market_data_stale_after_seconds": 30.0,
            }
        )
        gateway = MarketDataGateway(
            settings=settings,
            normalizer=MarketSnapshotNormalizer(exchange_name="OKX"),
            publisher=MarketSnapshotPublisher(bus=InMemoryEventBus()),
            okx_ws_client=_FakeWSClient(connected_public=False, connected_business=False, last_error="disconnected"),  # type: ignore[arg-type]
        )

        status = gateway.status()

        self.assertFalse(status["fresh"])
        self.assertFalse(status["connected"])
        self.assertFalse(status["ready"])
        self.assertIn("market_connection_down", status["blockers"])
        self.assertIn("market_data_stale", status["blockers"])

    async def test_market_stream_survives_downstream_publish_failure(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "market_data_backend": "okx",
                "default_symbol": "BTC-USDT",
            }
        )
        bus = InMemoryEventBus()

        async def broken_handler(_message) -> None:
            raise RuntimeError("downstream failed")

        await bus.subscribe(topics.MARKET_SNAPSHOTS, broken_handler)
        publisher = MarketSnapshotPublisher(bus=bus)
        snapshot = MarketSnapshot.model_validate(
            {
                "symbol": "BTC-USDT",
                "exchange": "OKX",
                "snapshot_ts": utc_now(),
                "best_bid": 100000.0,
                "best_ask": 100010.0,
                "last_price": 100005.0,
                "bid_size": 1.0,
                "ask_size": 1.0,
                "volume_24h": 1000.0,
                "kline_15m": {"open": 99900.0, "high": 100100.0, "low": 99800.0, "close": 100005.0, "volume": 10.0},
                "kline_1h": {"open": 99500.0, "high": 100500.0, "low": 99400.0, "close": 100005.0, "volume": 40.0},
                "recent_trades": [],
                "orderbook_depth": {"bids": [], "asks": []},
            }
        )
        gateway = MarketDataGateway(
            settings=settings,
            normalizer=MarketSnapshotNormalizer(exchange_name="OKX"),
            publisher=publisher,
            okx_normalizer=_FakeOKXNormalizer(snapshot),
            okx_ws_client=_FakeWSClient(connected_public=True, connected_business=True),  # type: ignore[arg-type]
        )

        await gateway._handle_okx_message({"arg": {"channel": "tickers"}, "data": []})

        self.assertEqual(gateway.latest_snapshot("BTC-USDT"), snapshot)
        self.assertEqual(gateway._last_error, "downstream failed")

    async def test_snapshot_is_fresh_clamps_negative_age_from_clock_skew(self) -> None:
        """R4-M7：本地时钟落后于 OKX 服务端时，utc_now() - snapshot_ts 可能得到负值。
        clamp 到非负后，负 age 仍判新鲜（数据本来就很新），并且不会误导下游。"""
        from datetime import timedelta

        settings = AATSSettings.model_validate(
            {
                "market_data_backend": "okx",
                "default_symbol": "BTC-USDT",
                "market_data_stale_after_seconds": 30.0,
            }
        )
        gateway = MarketDataGateway(
            settings=settings,
            normalizer=MarketSnapshotNormalizer(exchange_name="OKX"),
            publisher=MarketSnapshotPublisher(bus=InMemoryEventBus()),
        )
        snapshot = await gateway.publish_local_snapshot(symbol="BTC-USDT")
        # snapshot_ts 放到 10s 之后（本地时钟比 OKX 慢 10s）
        gateway._latest_snapshots["BTC-USDT"] = snapshot.model_copy(
            update={"snapshot_ts": utc_now() + timedelta(seconds=10)}
        )

        self.assertTrue(gateway.snapshot_is_fresh("BTC-USDT"))

    async def test_snapshot_is_fresh_logs_warning_on_large_clock_skew(self) -> None:
        """R4-M7：时钟偏差 > 60s 时写 warning log，便于 ops 发现时钟配置异常。"""
        import logging
        from datetime import timedelta

        settings = AATSSettings.model_validate(
            {
                "market_data_backend": "okx",
                "default_symbol": "BTC-USDT",
                "market_data_stale_after_seconds": 30.0,
            }
        )
        gateway = MarketDataGateway(
            settings=settings,
            normalizer=MarketSnapshotNormalizer(exchange_name="OKX"),
            publisher=MarketSnapshotPublisher(bus=InMemoryEventBus()),
        )
        snapshot = await gateway.publish_local_snapshot(symbol="BTC-USDT")
        # snapshot_ts 放到 120s 之后（本地时钟慢 2 分钟，明显异常）
        gateway._latest_snapshots["BTC-USDT"] = snapshot.model_copy(
            update={"snapshot_ts": utc_now() + timedelta(seconds=120)}
        )

        with self.assertLogs("aats.market_gateway", level="WARNING") as captured:
            gateway.snapshot_is_fresh("BTC-USDT")

        self.assertTrue(
            any("market_snapshot_clock_skew_detected" in line for line in captured.output),
            f"expected clock skew warning in logs, got: {captured.output}",
        )

    async def test_okx_rest_fallback_restores_freshness_when_ws_snapshot_is_stale(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "market_data_backend": "okx",
                "default_symbol": "BTC-USDT",
                "market_data_stale_after_seconds": 30.0,
                "okx_market_rest_fallback_enabled": True,
            }
        )
        rest_client = _FakeOKXRESTClient()
        gateway = MarketDataGateway(
            settings=settings,
            normalizer=MarketSnapshotNormalizer(exchange_name="OKX"),
            publisher=MarketSnapshotPublisher(bus=InMemoryEventBus()),
            okx_ws_client=_FakeWSClient(connected_public=False, connected_business=False, last_error="disconnected"),  # type: ignore[arg-type]
            okx_rest_client=rest_client,  # type: ignore[arg-type]
        )
        snapshot = await gateway.publish_local_snapshot(symbol="BTC-USDT")
        gateway._latest_snapshots["BTC-USDT"] = snapshot.model_copy(
            update={"snapshot_ts": utc_now().replace(year=utc_now().year - 1)}
        )
        gateway._latest_received_at["BTC-USDT"] = utc_now().replace(year=utc_now().year - 1)

        used = await gateway._run_okx_rest_fallback_once()
        status = gateway.status()

        self.assertTrue(used)
        self.assertTrue(gateway.is_fresh("BTC-USDT"))
        self.assertTrue(status["fresh"])
        self.assertTrue(status["rest_fallback_active"])
        self.assertIn("rest_fallback", status["detail"])
        self.assertEqual(rest_client.ticker_calls, 1)
        self.assertEqual(rest_client.candle_calls, 2)

    async def test_okx_rest_fallback_refreshes_all_stale_tracked_symbols(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "market_data_backend": "okx",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "market_data_stale_after_seconds": 30.0,
                "okx_market_rest_fallback_enabled": True,
                "smart_arbitrage_enabled": True,
            }
        )
        rest_client = _FakeOKXRESTClient()
        gateway = MarketDataGateway(
            settings=settings,
            normalizer=MarketSnapshotNormalizer(exchange_name="OKX"),
            publisher=MarketSnapshotPublisher(bus=InMemoryEventBus()),
            okx_ws_client=_FakeWSClient(connected_public=False, connected_business=False, last_error="disconnected"),  # type: ignore[arg-type]
            okx_rest_client=rest_client,  # type: ignore[arg-type]
        )

        used = await gateway._run_okx_rest_fallback_once()

        self.assertTrue(used)
        self.assertEqual(set(rest_client.ticker_symbols), {"BTC-USDT", "BTC-USDT-SWAP"})
        self.assertEqual({symbol for symbol, _ in rest_client.candle_symbols}, {"BTC-USDT", "BTC-USDT-SWAP"})
        self.assertIsNotNone(gateway.latest_snapshot("BTC-USDT"))
        self.assertIsNotNone(gateway.latest_snapshot("BTC-USDT-SWAP"))
        self.assertEqual(gateway.status()["tracked_symbol_count"], 2)

    async def test_okx_rest_fallback_does_not_run_when_market_is_already_fresh(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "market_data_backend": "okx",
                "default_symbol": "BTC-USDT",
                "market_data_stale_after_seconds": 30.0,
                "okx_market_rest_fallback_enabled": True,
            }
        )
        rest_client = _FakeOKXRESTClient()
        gateway = MarketDataGateway(
            settings=settings,
            normalizer=MarketSnapshotNormalizer(exchange_name="OKX"),
            publisher=MarketSnapshotPublisher(bus=InMemoryEventBus()),
            okx_ws_client=_FakeWSClient(connected_public=True, connected_business=True),  # type: ignore[arg-type]
            okx_rest_client=rest_client,  # type: ignore[arg-type]
        )
        await gateway.publish_local_snapshot(symbol="BTC-USDT")

        used = await gateway._run_okx_rest_fallback_once()

        self.assertFalse(used)
        self.assertEqual(rest_client.ticker_calls, 0)
        self.assertEqual(rest_client.candle_calls, 0)


if __name__ == "__main__":
    unittest.main()
