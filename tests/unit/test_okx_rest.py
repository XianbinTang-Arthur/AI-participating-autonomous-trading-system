from __future__ import annotations

import unittest

from aats.bootstrap.settings import AATSSettings
from aats.services.execution_engine.okx_rest import OKXRESTClient


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

    async def test_derivatives_runtime_uses_swap_inst_type(self) -> None:
        client = _CapturingOKXRESTClient(
            settings=AATSSettings.model_validate({"trading_product_type": "derivatives"})
        )

        await client.get_positions()
        await client.get_open_orders(symbol="BTC-USDT-SWAP")
        await client.get_instruments()
        await client.get_fills(symbol="BTC-USDT-SWAP", limit=10)

        self.assertEqual(client.calls[0][1]["instType"], "SWAP")
        self.assertEqual(client.calls[1][1]["instType"], "SWAP")
        self.assertEqual(client.calls[2][1]["instType"], "SWAP")
        self.assertEqual(client.calls[3][1]["instType"], "SWAP")


if __name__ == "__main__":
    unittest.main()
