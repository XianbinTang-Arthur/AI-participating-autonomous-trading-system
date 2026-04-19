"""OKX WebSocket public 频道 mark-price 订阅策略 (P1.4).

锁定契约:
  1. derivative symbol (SWAP / dated FUTURES) 的 public_args 包含 mark-price
  2. 现货 symbol (BASE-QUOTE) 的 public_args **不**包含 mark-price —— 否则
     OKX 会拒订阅触发 subscription_error 死循环重连
  3. _is_derivative_symbol 正确识别两类衍生品
  4. tickers 频道对所有 symbol 都订阅（不被影响）
"""

from __future__ import annotations

import unittest

from aats.bootstrap.settings import AATSSettings
from aats.services.market_gateway.okx_websocket import OKXPublicWebSocketClient


class OKXWebSocketMarkPriceSubscriptionTests(unittest.TestCase):
    def _client_with_symbols(self, symbols: tuple[str, ...]) -> OKXPublicWebSocketClient:
        settings = AATSSettings.model_validate(
            {
                "allowed_symbols": symbols,
                "default_symbol": symbols[0] if symbols else "BTC-USDT-SWAP",
            }
        )
        return OKXPublicWebSocketClient(settings=settings)

    def _public_channels_for(self, symbol: str) -> list[str]:
        client = self._client_with_symbols((symbol,))
        public_args, _business = client._subscription_args()  # type: ignore[attr-defined]
        return [a["channel"] for a in public_args if a.get("instId") == symbol]

    def test_swap_symbol_subscribes_mark_price(self) -> None:
        channels = self._public_channels_for("BTC-USDT-SWAP")
        self.assertIn("tickers", channels)
        self.assertIn("mark-price", channels)

    def test_dated_futures_symbol_subscribes_mark_price(self) -> None:
        """6 位数字后缀 = dated futures (e.g. BTC-USDT-251226)."""
        self.assertTrue(
            OKXPublicWebSocketClient._is_derivative_symbol("BTC-USDT-251226")
        )

    def test_spot_symbol_does_not_subscribe_mark_price(self) -> None:
        """BASE-QUOTE 两段 = 现货，OKX 不推 mark-price，订阅会被拒."""
        self.assertFalse(
            OKXPublicWebSocketClient._is_derivative_symbol("BTC-USDT")
        )

    def test_tickers_still_subscribed_for_all_symbol_types(self) -> None:
        """tickers 对现货也推，mark-price 的过滤不应影响 tickers 订阅."""
        spot_channels = self._public_channels_for("BTC-USDT")
        self.assertIn("tickers", spot_channels)
        self.assertNotIn("mark-price", spot_channels)

    def test_mark_price_subscription_arg_shape(self) -> None:
        """订阅参数对象形态：{"channel": "mark-price", "instId": ...}."""
        client = self._client_with_symbols(("BTC-USDT-SWAP",))
        public_args, _business = client._subscription_args()  # type: ignore[attr-defined]
        mark_args = [a for a in public_args if a.get("channel") == "mark-price"]
        self.assertEqual(len(mark_args), 1)
        self.assertEqual(mark_args[0], {"channel": "mark-price", "instId": "BTC-USDT-SWAP"})


if __name__ == "__main__":
    unittest.main()
