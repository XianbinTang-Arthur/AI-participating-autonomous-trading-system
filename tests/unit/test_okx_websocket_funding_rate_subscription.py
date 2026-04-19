"""OKX WebSocket funding-rate 订阅策略 (P1.5).

锁定契约:
  1. derivative symbol 的 public_args 包含 funding-rate
  2. spot symbol 的 public_args 不包含 funding-rate（OKX 会拒）
  3. funding-rate 订阅参数形态正确
"""

from __future__ import annotations

import unittest

from aats.bootstrap.settings import AATSSettings
from aats.services.market_gateway.okx_websocket import OKXPublicWebSocketClient


class OKXWebSocketFundingRateSubscriptionTests(unittest.TestCase):
    def _public_channels_for(self, symbol: str) -> list[str]:
        settings = AATSSettings.model_validate(
            {"allowed_symbols": (symbol,), "default_symbol": symbol},
        )
        client = OKXPublicWebSocketClient(settings=settings)
        public_args, _ = client._subscription_args()  # type: ignore[attr-defined]
        return [a["channel"] for a in public_args if a.get("instId") == symbol]

    def test_swap_subscribes_funding_rate(self) -> None:
        channels = self._public_channels_for("BTC-USDT-SWAP")
        self.assertIn("tickers", channels)
        self.assertIn("mark-price", channels)
        self.assertIn("funding-rate", channels)

    def test_spot_does_not_subscribe_funding_rate(self) -> None:
        channels = self._public_channels_for("BTC-USDT")
        self.assertIn("tickers", channels)
        self.assertNotIn("mark-price", channels)
        self.assertNotIn("funding-rate", channels)

    def test_funding_subscription_arg_shape(self) -> None:
        settings = AATSSettings.model_validate(
            {"allowed_symbols": ("BTC-USDT-SWAP",), "default_symbol": "BTC-USDT-SWAP"},
        )
        client = OKXPublicWebSocketClient(settings=settings)
        public_args, _ = client._subscription_args()  # type: ignore[attr-defined]
        funding_args = [a for a in public_args if a.get("channel") == "funding-rate"]
        self.assertEqual(len(funding_args), 1)
        self.assertEqual(
            funding_args[0],
            {"channel": "funding-rate", "instId": "BTC-USDT-SWAP"},
        )


if __name__ == "__main__":
    unittest.main()
