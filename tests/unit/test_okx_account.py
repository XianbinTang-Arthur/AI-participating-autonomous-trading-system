from __future__ import annotations

import unittest

from aats.bootstrap.settings import AATSSettings
from aats.services.execution_engine.okx_account import OKXAccountService


class _FakeOKXClient:
    def __init__(self) -> None:
        self.get_positions_called = False

    async def get_balance(self):
        return {
            "code": "0",
            "data": [
                {
                    "details": [
                        {"ccy": "USDT", "eq": "1000", "availEq": "1000"},
                    ]
                }
            ],
        }

    async def get_open_orders(self, *, symbol: str | None = None):
        return {"code": "0", "data": []}

    async def get_fills(self, *, symbol: str | None = None, limit: int | None = None):
        return {"code": "0", "data": []}

    async def get_instruments(self):
        return {
            "code": "0",
            "data": [
                {
                    "instId": "BTC-USDT",
                    "baseCcy": "BTC",
                    "quoteCcy": "USDT",
                    "lotSz": "0.00000001",
                    "tickSz": "0.1",
                    "minSz": "0.00001",
                    "state": "live",
                }
            ],
        }

    async def get_account_config(self):
        return {"code": "0", "data": [{"acctLv": "1"}]}

    async def get_positions(self):
        self.get_positions_called = True
        raise AssertionError("spot account refresh should not call get_positions()")


class _FakeDerivativesOKXClient(_FakeOKXClient):
    async def get_instruments(self):
        return {
            "code": "0",
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "baseCcy": "BTC",
                    "quoteCcy": "USDT",
                    "lotSz": "0.01",
                    "tickSz": "0.1",
                    "minSz": "0.01",
                    "state": "live",
                }
            ],
        }

    async def get_positions(self):
        self.get_positions_called = True
        return {
            "code": "0",
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "pos": "0.02",
                    "avgPx": "80000",
                    "markPx": "80100",
                    "notionalUsd": "1602",
                    "posSide": "long",
                }
            ],
        }


class TestOKXAccountService(unittest.IsolatedAsyncioTestCase):
    async def test_spot_refresh_uses_balances_without_calling_positions_endpoint(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
            }
        )
        client = _FakeOKXClient()
        service = OKXAccountService(settings=settings, client=client)

        snapshot = await service.refresh(force=True)

        self.assertIsNotNone(snapshot)
        self.assertEqual(len(snapshot.balances), 1)
        self.assertEqual(snapshot.positions, [])
        self.assertFalse(client.get_positions_called)

    async def test_spot_refresh_prefers_avail_balance_when_avail_eq_is_zero(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
            }
        )

        class _AvailBalClient(_FakeOKXClient):
            async def get_balance(self):
                return {
                    "code": "0",
                    "data": [
                        {
                            "details": [
                                {"ccy": "USDT", "eq": "1000", "availEq": "0", "availBal": "1000"},
                            ]
                        }
                    ],
                }

        service = OKXAccountService(settings=settings, client=_AvailBalClient())

        snapshot = await service.refresh(force=True)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.balances[0].total, 1000.0)
        self.assertEqual(snapshot.balances[0].available, 1000.0)
        self.assertEqual(snapshot.balances[0].frozen, 0.0)

    async def test_derivatives_refresh_loads_positions_endpoint(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
                "trading_product_type": "derivatives",
                "default_symbol": "BTC-USDT-SWAP",
            }
        )
        client = _FakeDerivativesOKXClient()
        service = OKXAccountService(settings=settings, client=client)

        snapshot = await service.refresh(force=True)

        self.assertIsNotNone(snapshot)
        self.assertTrue(client.get_positions_called)
        self.assertEqual(len(snapshot.positions), 1)
        self.assertEqual(snapshot.positions[0].symbol, "BTC-USDT-SWAP")
        self.assertEqual(snapshot.positions[0].side, "long")


if __name__ == "__main__":
    unittest.main()
