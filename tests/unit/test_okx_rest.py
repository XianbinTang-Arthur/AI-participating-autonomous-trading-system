from __future__ import annotations

import unittest

import httpx

from aats.bootstrap.settings import AATSSettings
from aats.services.execution_engine.okx_rest import OKXRESTClient, OKXRequestError


class _CapturingOKXRESTClient(OKXRESTClient):
    def __init__(self, *, settings: AATSSettings) -> None:
        super().__init__(settings=settings)
        self.calls: list[tuple[str, dict | None]] = []

    async def request(
        self,
        *,
        method: str,
        path: str,
        params=None,
        json_body=None,
        require_auth: bool = False,
    ) -> dict:
        self.calls.append((path, dict(params) if params is not None else None))
        return {"code": "0", "data": []}


class TestOKXRESTClient(unittest.IsolatedAsyncioTestCase):
    async def test_client_handle_reuses_async_client_until_closed(self) -> None:
        client = OKXRESTClient(settings=AATSSettings.model_validate({}))

        first = await client._client_handle()
        second = await client._client_handle()

        self.assertIs(first, second)

        await client.aclose()

        third = await client._client_handle()
        self.assertIsNot(first, third)
        await client.aclose()

    async def test_spot_runtime_uses_spot_inst_type(self) -> None:
        client = _CapturingOKXRESTClient(
            settings=AATSSettings.model_validate({"trading_product_type": "spot"})
        )

        await client.get_open_orders(symbol="BTC-USDT")
        await client.get_instruments()
        await client.get_fills(symbol="BTC-USDT", limit=10)

        self.assertEqual(client.calls[0][1]["instType"], "SPOT")
        self.assertEqual(client.calls[1][1]["instType"], "SPOT")
        self.assertEqual(client.calls[2][1]["instType"], "SPOT")

    async def test_derivatives_runtime_infers_swap_and_futures_inst_types(self) -> None:
        client = _CapturingOKXRESTClient(
            settings=AATSSettings.model_validate({"trading_product_type": "derivatives"})
        )

        await client.get_positions()
        await client.get_open_orders(symbol="BTC-USDT-SWAP")
        await client.get_open_orders(symbol="BTC-USDT-240329")
        await client.get_instruments()
        await client.get_fills(symbol="BTC-USDT-SWAP", limit=10)
        await client.get_fills(symbol="BTC-USDT-240329", limit=10)

        self.assertEqual(client.calls[0][1]["instType"], "SWAP")
        self.assertEqual(client.calls[1][1]["instType"], "FUTURES")
        self.assertEqual(client.calls[2][1]["instType"], "SWAP")
        self.assertEqual(client.calls[3][1]["instType"], "FUTURES")
        self.assertEqual(client.calls[4][1]["instType"], "SWAP")
        self.assertEqual(client.calls[5][1]["instType"], "FUTURES")
        self.assertEqual(client.calls[6][1]["instType"], "SWAP")
        self.assertEqual(client.calls[7][1]["instType"], "FUTURES")

    async def test_live_request_does_not_send_simulated_header(self) -> None:
        captured_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal captured_headers
            captured_headers = {key.lower(): value for key, value in request.headers.items()}
            return httpx.Response(200, json={"code": "0", "data": []})

        client = OKXRESTClient(
            settings=AATSSettings.model_validate(
                {
                    "okx_simulated_trading": False,
                    "okx_api_key": "test_key",
                    "okx_api_secret": "test_secret",
                    "okx_api_passphrase": "test_passphrase",
                }
            )
        )
        client._client = httpx.AsyncClient(
            base_url=client.settings.okx_rest_url,
            transport=httpx.MockTransport(handler),
        )

        try:
            await client.get_market_ticker(symbol="BTC-USDT")
        finally:
            await client.aclose()

        self.assertNotIn("x-simulated-trading", captured_headers)

    async def test_demo_request_sends_simulated_header(self) -> None:
        captured_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal captured_headers
            captured_headers = {key.lower(): value for key, value in request.headers.items()}
            return httpx.Response(200, json={"code": "0", "data": []})

        client = OKXRESTClient(settings=AATSSettings.model_validate({"okx_simulated_trading": True}))
        client._client = httpx.AsyncClient(
            base_url=client.settings.okx_rest_url,
            transport=httpx.MockTransport(handler),
        )

        try:
            await client.get_market_ticker(symbol="BTC-USDT")
        finally:
            await client.aclose()

        self.assertEqual(captured_headers.get("x-simulated-trading"), "1")

    async def test_get_request_retries_transient_server_error_and_succeeds(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(500, json={"code": "500", "msg": "temporary"})
            return httpx.Response(200, json={"code": "0", "data": [{"instId": "BTC-USDT"}]})

        client = OKXRESTClient(
            settings=AATSSettings.model_validate(
                {
                    "okx_simulated_trading": False,
                    "okx_api_key": "test_key",
                    "okx_api_secret": "test_secret",
                    "okx_api_passphrase": "test_passphrase",
                }
            )
        )
        original_base_delay = OKXRESTClient._QUERY_RETRY_BASE_DELAY_SECONDS
        original_max_delay = OKXRESTClient._QUERY_RETRY_MAX_DELAY_SECONDS
        OKXRESTClient._QUERY_RETRY_BASE_DELAY_SECONDS = 0.0
        OKXRESTClient._QUERY_RETRY_MAX_DELAY_SECONDS = 0.0
        client._client = httpx.AsyncClient(
            base_url=client.settings.okx_rest_url,
            transport=httpx.MockTransport(handler),
        )

        try:
            payload = await client.get_market_ticker(symbol="BTC-USDT")
        finally:
            OKXRESTClient._QUERY_RETRY_BASE_DELAY_SECONDS = original_base_delay
            OKXRESTClient._QUERY_RETRY_MAX_DELAY_SECONDS = original_max_delay
            await client.aclose()

        self.assertEqual(payload["code"], "0")
        self.assertEqual(attempts, 2)

    async def test_post_request_does_not_retry_transient_server_error(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(500, json={"code": "500", "msg": "temporary"})

        client = OKXRESTClient(
            settings=AATSSettings.model_validate(
                {
                    "okx_simulated_trading": False,
                    "okx_api_key": "test_key",
                    "okx_api_secret": "test_secret",
                    "okx_api_passphrase": "test_passphrase",
                }
            )
        )
        original_base_delay = OKXRESTClient._QUERY_RETRY_BASE_DELAY_SECONDS
        original_max_delay = OKXRESTClient._QUERY_RETRY_MAX_DELAY_SECONDS
        OKXRESTClient._QUERY_RETRY_BASE_DELAY_SECONDS = 0.0
        OKXRESTClient._QUERY_RETRY_MAX_DELAY_SECONDS = 0.0
        client._client = httpx.AsyncClient(
            base_url=client.settings.okx_rest_url,
            transport=httpx.MockTransport(handler),
        )

        try:
            with self.assertRaises(OKXRequestError) as context:
                await client.place_order({"instId": "BTC-USDT", "tdMode": "cash", "side": "buy", "ordType": "market", "sz": "1"})
        finally:
            OKXRESTClient._QUERY_RETRY_BASE_DELAY_SECONDS = original_base_delay
            OKXRESTClient._QUERY_RETRY_MAX_DELAY_SECONDS = original_max_delay
            await client.aclose()

        self.assertEqual(attempts, 1)
        self.assertEqual(context.exception.classification, "server_error")


if __name__ == "__main__":
    unittest.main()
