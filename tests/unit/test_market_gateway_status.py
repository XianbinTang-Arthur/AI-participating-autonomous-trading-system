from __future__ import annotations

import unittest

from aats.bootstrap.settings import AATSSettings
from aats.bus.memory_bus import InMemoryEventBus
from aats.schemas.common import utc_now
from aats.services.market_gateway.gateway import MarketDataGateway
from aats.services.market_gateway.normalizer import MarketSnapshotNormalizer
from aats.services.market_gateway.publisher import MarketSnapshotPublisher


class TestMarketGatewayStatus(unittest.IsolatedAsyncioTestCase):
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

        status = gateway.status()
        self.assertFalse(gateway.is_fresh("BTC-USDT"))
        self.assertIn("market_data_stale", status["blockers"])


if __name__ == "__main__":
    unittest.main()
