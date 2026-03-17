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
    async def get_balance(self):
        return {
            "code": "0",
            "data": [
                {
                    "details": [
                        {"ccy": "USDT", "eq": "1010", "cashBal": "1000", "availEq": "1000", "availBal": "1000"},
                    ]
                }
            ],
        }

    async def get_instruments(self):
        return {
            "code": "0",
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "baseCcy": "",
                    "quoteCcy": "",
                    "uly": "BTC-USDT",
                    "settleCcy": "USDT",
                    "ctValCcy": "BTC",
                    "ctVal": "0.01",
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


class _FakeMultiSymbolOKXClient(_FakeOKXClient):
    def __init__(self) -> None:
        super().__init__()
        self.open_order_symbols: list[str | None] = []
        self.fill_symbols: list[str | None] = []

    async def get_open_orders(self, *, symbol: str | None = None):
        self.open_order_symbols.append(symbol)
        payloads = {
            "BTC-USDT": {
                "code": "0",
                "data": [
                    {
                        "instId": "BTC-USDT",
                        "ordId": "ord_btc_1",
                        "clOrdId": "cl_btc_1",
                        "side": "buy",
                        "ordType": "limit",
                        "state": "live",
                        "sz": "0.01",
                        "accFillSz": "0",
                        "px": "68000",
                    }
                ],
            },
            "ETH-USDT": {
                "code": "0",
                "data": [
                    {
                        "instId": "ETH-USDT",
                        "ordId": "ord_eth_1",
                        "clOrdId": "cl_eth_1",
                        "side": "sell",
                        "ordType": "limit",
                        "state": "live",
                        "sz": "0.5",
                        "accFillSz": "0.1",
                        "px": "3500",
                    }
                ],
            },
        }
        return payloads[symbol or "BTC-USDT"]

    async def get_fills(self, *, symbol: str | None = None, limit: int | None = None):
        self.fill_symbols.append(symbol)
        payloads = {
            "BTC-USDT": {
                "code": "0",
                "data": [
                    {
                        "instId": "BTC-USDT",
                        "ordId": "ord_btc_1",
                        "clOrdId": "cl_btc_1",
                        "tradeId": "fill_btc_1",
                        "side": "buy",
                        "fillSz": "0.01",
                        "fillPx": "68000",
                        "fee": "-0.068",
                        "feeCcy": "USDT",
                        "fillTime": "1700000001000",
                    }
                ],
            },
            "ETH-USDT": {
                "code": "0",
                "data": [
                    {
                        "instId": "ETH-USDT",
                        "ordId": "ord_eth_1",
                        "clOrdId": "cl_eth_1",
                        "tradeId": "fill_eth_1",
                        "side": "sell",
                        "fillSz": "0.1",
                        "fillPx": "3500",
                        "fee": "-0.35",
                        "feeCcy": "USDT",
                        "fillTime": "1700000002000",
                    }
                ],
            },
        }
        return payloads[symbol or "BTC-USDT"]

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
                },
                {
                    "instId": "ETH-USDT",
                    "baseCcy": "ETH",
                    "quoteCcy": "USDT",
                    "lotSz": "0.00000001",
                    "tickSz": "0.01",
                    "minSz": "0.00001",
                    "state": "live",
                },
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
        self.assertEqual(snapshot.balances[0].total, 1000.0)
        instrument = service.instrument_metadata("BTC-USDT-SWAP")
        self.assertIsNotNone(instrument)
        self.assertEqual(instrument.base_currency, "BTC")
        self.assertEqual(instrument.quote_currency, "USDT")
        self.assertEqual(instrument.contract_value, 0.01)
        self.assertAlmostEqual(snapshot.positions[0].quantity, 0.0002)

    async def test_refresh_collects_open_orders_and_fills_for_all_allowed_symbols(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT", "ETH-USDT"),
            }
        )
        client = _FakeMultiSymbolOKXClient()
        service = OKXAccountService(settings=settings, client=client)

        snapshot = await service.refresh(force=True)

        self.assertIsNotNone(snapshot)
        self.assertEqual({order.instrument_id for order in snapshot.open_orders}, {"BTC-USDT", "ETH-USDT"})
        self.assertEqual({fill.symbol for fill in snapshot.fills}, {"BTC-USDT", "ETH-USDT"})
        self.assertEqual(service.open_order_count("ETH-USDT"), 1)
        self.assertEqual(client.open_order_symbols, ["BTC-USDT", "ETH-USDT"])
        self.assertEqual(client.fill_symbols, ["BTC-USDT", "ETH-USDT"])
        self.assertFalse(client.get_positions_called)


if __name__ == "__main__":
    unittest.main()
