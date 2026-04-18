from __future__ import annotations

import json
import unittest

from aats.bootstrap.settings import AATSSettings
from aats.services.execution_engine.okx_private_websocket import OKXPrivateWebSocketClient


class _FakeWebSocket:
    def __init__(self, response: str) -> None:
        self.response = response
        self.sent_messages: list[str] = []

    async def send(self, message: str) -> None:
        self.sent_messages.append(message)

    async def recv(self) -> str:
        return self.response


class TestOKXPrivateWebSocketClient(unittest.IsolatedAsyncioTestCase):
    async def test_simulated_trading_switches_private_ws_to_wspap_and_waits_for_login_ack(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
                "okx_simulated_trading": True,
            }
        )
        client = OKXPrivateWebSocketClient(settings=settings)
        websocket = _FakeWebSocket('{"event":"login","code":"0","msg":"","connId":"abc"}')

        await client._login(websocket)

        self.assertEqual(client._resolved_private_ws_url(), "wss://wspap.okx.com:8443/ws/v5/private")
        self.assertEqual(len(websocket.sent_messages), 1)

    async def test_login_ack_raises_on_error_response(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
            }
        )
        client = OKXPrivateWebSocketClient(settings=settings)
        websocket = _FakeWebSocket('{"event":"login","code":"60009","msg":"Login failed."}')

        with self.assertRaisesRegex(RuntimeError, "okx_private_ws_login_error"):
            await client._login(websocket)

    def test_spot_subscription_args_subscribe_only_spot_orders(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
            }
        )
        client = OKXPrivateWebSocketClient(settings=settings)

        self.assertEqual(
            client._subscription_args(),
            [
                {"channel": "balance_and_position"},
                {"channel": "orders", "instType": "SPOT"},
            ],
        )

    def test_derivatives_subscription_args_subscribe_swap_and_futures_orders(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
            }
        )
        client = OKXPrivateWebSocketClient(settings=settings)

        self.assertEqual(
            client._subscription_args(),
            [
                {"channel": "balance_and_position"},
                {"channel": "orders", "instType": "SWAP"},
                {"channel": "orders", "instType": "FUTURES"},
            ],
        )

    async def test_subscribe_serializes_swap_and_futures_order_channels(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
            }
        )
        client = OKXPrivateWebSocketClient(settings=settings)
        websocket = _FakeWebSocket('{"event":"login","code":"0","msg":"","connId":"abc"}')

        await client._subscribe(websocket, client._subscription_args())

        self.assertEqual(len(websocket.sent_messages), 1)
        self.assertEqual(
            json.loads(websocket.sent_messages[0]),
            {
                "op": "subscribe",
                "args": [
                    {"channel": "balance_and_position"},
                    {"channel": "orders", "instType": "SWAP"},
                    {"channel": "orders", "instType": "FUTURES"},
                ],
            },
        )


class TestOKXPrivateWebSocketSubscriptionAck(unittest.IsolatedAsyncioTestCase):
    """R3-P1-U-D：private WS 订阅 ack 追踪。

    镜像 public WS `_is_control_message` 的订阅 ack / error 语义，确保
    balance_and_position / orders 在静默失败时能被上层主动重连发现。
    """

    def _make_client(self) -> OKXPrivateWebSocketClient:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
            }
        )
        return OKXPrivateWebSocketClient(settings=settings)

    async def test_subscribe_populates_pending_subscriptions(self) -> None:
        client = self._make_client()
        websocket = _FakeWebSocket("")

        await client._subscribe(websocket, client._subscription_args())

        self.assertEqual(
            client._pending_subscriptions,
            {("balance_and_position", ""), ("orders", "SWAP"), ("orders", "FUTURES")},
        )
        self.assertEqual(client._subscription_errors, [])
        self.assertIsNotNone(client._subscription_sent_ts)

    async def test_subscribe_ack_discards_pending(self) -> None:
        client = self._make_client()
        websocket = _FakeWebSocket("")
        await client._subscribe(websocket, client._subscription_args())

        self.assertTrue(
            client._is_control_message(
                {"event": "subscribe", "arg": {"channel": "orders", "instType": "SWAP"}}
            )
        )

        self.assertEqual(
            client._pending_subscriptions,
            {("balance_and_position", ""), ("orders", "FUTURES")},
        )

    async def test_subscribe_ack_matches_balance_and_position_without_inst_type(self) -> None:
        """balance_and_position ack arg 不带 instType，用空字符串占位匹配。"""
        client = self._make_client()
        websocket = _FakeWebSocket("")
        await client._subscribe(websocket, client._subscription_args())

        self.assertTrue(
            client._is_control_message(
                {"event": "subscribe", "arg": {"channel": "balance_and_position"}}
            )
        )

        self.assertNotIn(("balance_and_position", ""), client._pending_subscriptions)

    async def test_subscription_error_accumulates(self) -> None:
        client = self._make_client()
        websocket = _FakeWebSocket("")
        await client._subscribe(websocket, client._subscription_args())

        self.assertTrue(
            client._is_control_message(
                {
                    "event": "error",
                    "code": "60012",
                    "msg": "Illegal request",
                    "arg": {"channel": "orders", "instType": "SWAP"},
                }
            )
        )

        self.assertEqual(len(client._subscription_errors), 1)
        self.assertEqual(client._subscription_errors[0]["code"], "60012")
        self.assertEqual(client._subscription_errors[0]["channel"], "orders")
        self.assertEqual(client._subscription_errors[0]["instType"], "SWAP")
        self.assertIsNotNone(client._last_error)

    async def test_subscription_error_for_unknown_key_does_not_accumulate(self) -> None:
        """OKX 偶发错误如果不属于本次 subscribe 的期望集合，只记 log 不计入错误列表，
        避免把全局系统噪音 error 当成订阅失败触发误重连。"""
        client = self._make_client()
        websocket = _FakeWebSocket("")
        await client._subscribe(websocket, client._subscription_args())

        self.assertTrue(
            client._is_control_message(
                {
                    "event": "error",
                    "code": "60013",
                    "msg": "Unknown channel",
                    "arg": {"channel": "account-greeks", "instType": "SPOT"},
                }
            )
        )

        self.assertEqual(client._subscription_errors, [])

    async def test_non_control_message_returns_false(self) -> None:
        """正常 data push（没有 event 字段）不能被 _is_control_message 吞掉，否则
        上层 on_message 永远收不到 balance/orders 推送。"""
        client = self._make_client()

        self.assertFalse(
            client._is_control_message({"arg": {"channel": "orders"}, "data": [{"ordId": "1"}]})
        )
        self.assertFalse(client._is_control_message({}))


if __name__ == "__main__":
    unittest.main()
