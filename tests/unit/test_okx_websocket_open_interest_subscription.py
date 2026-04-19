"""OKX WebSocket open-interest 订阅策略 (P1.6)."""

from __future__ import annotations

import unittest

from aats.bootstrap.settings import AATSSettings
from aats.services.market_gateway.okx_websocket import OKXPublicWebSocketClient


class OKXWebSocketOpenInterestSubscriptionTests(unittest.TestCase):
    def _public_channels_for(self, symbol: str) -> list[str]:
        settings = AATSSettings.model_validate(
            {"allowed_symbols": (symbol,), "default_symbol": symbol},
        )
        client = OKXPublicWebSocketClient(settings=settings)
        public_args, _ = client._subscription_args()  # type: ignore[attr-defined]
        return [a["channel"] for a in public_args if a.get("instId") == symbol]

    def test_swap_subscribes_open_interest(self) -> None:
        channels = self._public_channels_for("BTC-USDT-SWAP")
        self.assertIn("tickers", channels)
        self.assertIn("mark-price", channels)
        self.assertIn("funding-rate", channels)
        self.assertIn("open-interest", channels)

    def test_spot_does_not_subscribe_open_interest(self) -> None:
        channels = self._public_channels_for("BTC-USDT")
        self.assertIn("tickers", channels)
        self.assertNotIn("open-interest", channels)

    def test_open_interest_subscription_arg_shape(self) -> None:
        settings = AATSSettings.model_validate(
            {"allowed_symbols": ("BTC-USDT-SWAP",), "default_symbol": "BTC-USDT-SWAP"},
        )
        client = OKXPublicWebSocketClient(settings=settings)
        public_args, _ = client._subscription_args()  # type: ignore[attr-defined]
        oi_args = [a for a in public_args if a.get("channel") == "open-interest"]
        self.assertEqual(len(oi_args), 1)
        self.assertEqual(oi_args[0], {"channel": "open-interest", "instId": "BTC-USDT-SWAP"})


if __name__ == "__main__":
    unittest.main()
