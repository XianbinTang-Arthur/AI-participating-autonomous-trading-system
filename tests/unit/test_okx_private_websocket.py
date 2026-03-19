from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
