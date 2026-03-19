from __future__ import annotations

import unittest
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.services.execution_engine.okx_account import OKXAccountService


class _FakeOKXClient:
    def __init__(self) -> None:
        self.get_positions_called = False
        self.trade_fee_calls: list[dict[str, object | None]] = []

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
        return {"code": "0", "data": [{"acctLv": "1", "posMode": "net_mode"}]}

    async def get_trade_fee(
        self,
        *,
        symbol: str | None = None,
        underlying: str | None = None,
        instrument_family: str | None = None,
    ):
        self.trade_fee_calls.append(
            {
                "symbol": symbol,
                "underlying": underlying,
                "instrument_family": instrument_family,
            }
        )
        return {"code": "0", "data": [{"maker": "-0.0008", "taker": "0.001"}]}

    async def get_account_position_risk(self):
        return {"code": "0", "data": [{"adjEq": "1000", "imr": "10"}]}

    async def get_system_status(self):
        return {"code": "0", "data": []}

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

    async def get_account_config(self):
        return {"code": "0", "data": [{"acctLv": "2", "posMode": "net_mode"}]}


class _FakeIncompatibleDerivativesClient(_FakeDerivativesOKXClient):
    async def get_account_config(self):
        return {"code": "0", "data": [{"acctLv": "1", "posMode": ""}]}


class _FakeSystemIncidentClient(_FakeOKXClient):
    async def get_system_status(self):
        return {"code": "0", "data": [{"state": "ongoing", "serviceType": "5"}]}


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
        self.assertEqual(client.trade_fee_calls[0]["symbol"], settings.default_symbol)
        self.assertIsNone(client.trade_fee_calls[0]["underlying"])

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
        self.assertEqual(snapshot.balances[0].total, Decimal("1000.0"))
        self.assertEqual(snapshot.balances[0].available, Decimal("1000.0"))
        self.assertEqual(snapshot.balances[0].frozen, Decimal("0.0"))

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
        self.assertEqual(snapshot.balances[0].total, Decimal("1000.0"))
        instrument = service.instrument_metadata("BTC-USDT-SWAP")
        self.assertIsNotNone(instrument)
        self.assertEqual(instrument.base_currency, "BTC")
        self.assertEqual(instrument.quote_currency, "USDT")
        self.assertEqual(instrument.contract_value, Decimal("0.01"))
        self.assertEqual(snapshot.positions[0].quantity, Decimal("0.0002"))
        self.assertEqual(snapshot.position_mode, "net_mode")
        self.assertEqual(snapshot.fee_rates["taker"], "0.001")
        self.assertEqual(client.trade_fee_calls[0]["underlying"], "BTC-USDT")

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

    async def test_status_blocks_incompatible_derivatives_account_configuration(self) -> None:
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
        service = OKXAccountService(settings=settings, client=_FakeIncompatibleDerivativesClient())

        await service.refresh(force=True)
        status = service.status()

        self.assertFalse(status["ready"])
        self.assertIn("okx_account_mode_incompatible_with_derivatives", status["blockers"])
        self.assertIn("okx_position_mode_missing", status["blockers"])

    async def test_status_blocks_when_okx_system_status_reports_incident(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
            }
        )
        service = OKXAccountService(settings=settings, client=_FakeSystemIncidentClient())

        await service.refresh(force=True)
        status = service.status()

        self.assertFalse(status["ready"])
        self.assertIn("okx_system_status_incident", status["blockers"])

    async def test_private_balance_and_position_ws_updates_latest_snapshot(self) -> None:
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
        service = OKXAccountService(settings=settings, client=_FakeDerivativesOKXClient())
        await service.refresh(force=True)

        await service.handle_private_ws_message(
            {
                "arg": {"channel": "balance_and_position"},
                "data": [
                    {
                        "pTime": "1700000003000",
                        "balData": [
                            {"ccy": "USDT", "cashBal": "1200", "availBal": "1180"},
                        ],
                        "posData": [
                            {
                                "instId": "BTC-USDT-SWAP",
                                "pos": "0.03",
                                "avgPx": "80500",
                                "markPx": "80600",
                                "notionalUsd": "2418",
                                "posSide": "long",
                            }
                        ],
                    }
                ],
            }
        )

        snapshot = service.latest_snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.balances[0].total, Decimal("1200"))
        self.assertEqual(snapshot.balances[0].available, Decimal("1180"))
        self.assertEqual(snapshot.positions[0].quantity, Decimal("0.0003"))

    async def test_recent_bills_fetches_and_caches_rows(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
            }
        )

        class _BillsClient(_FakeOKXClient):
            async def get_bills_details(self, *, symbol: str | None = None, limit: int | None = None, begin=None, end=None):
                _ = symbol
                _ = limit
                _ = begin
                _ = end
                return {
                    "code": "0",
                    "data": [
                        {"billId": "bill_1", "type": "1", "subType": "173", "ccy": "USDT", "bal": "1000"},
                        {"billId": "bill_2", "type": "2", "subType": "174", "ccy": "USDT", "bal": "998"},
                    ],
                }

        service = OKXAccountService(settings=settings, client=_BillsClient())

        bills = await service.recent_bills(limit=10)
        status = service.status()

        self.assertEqual(len(bills), 2)
        self.assertEqual(bills[0]["billId"], "bill_1")
        self.assertEqual(status["recent_bills_count"], 2)

    async def test_recent_bills_summary_groups_categories(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
            }
        )

        class _BillsClient(_FakeOKXClient):
            async def get_bills_details(self, *, symbol: str | None = None, limit: int | None = None, begin=None, end=None):
                _ = symbol
                _ = limit
                _ = begin
                _ = end
                return {
                    "code": "0",
                    "data": [
                        {"billId": "bill_1", "type": "1", "subType": "173", "ccy": "USDT", "ts": "1700000001000"},
                        {"billId": "bill_2", "type": "1", "subType": "173", "ccy": "USDT", "ts": "1700000002000"},
                        {"billId": "bill_3", "type": "2", "subType": "174", "ccy": "BTC", "ts": "1700000003000"},
                    ],
                }

        service = OKXAccountService(settings=settings, client=_BillsClient())
        await service.recent_bills(limit=10)

        summary = service.recent_bills_summary()

        self.assertTrue(summary["available"])
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["latest_bill_id"], "bill_3")
        self.assertEqual(summary["currencies"], ["BTC", "USDT"])
        self.assertEqual(summary["top_categories"][0]["count"], 2)
        self.assertEqual(summary["top_categories"][0]["semantic_group"], "funding_fee")
        self.assertEqual(summary["top_categories"][0]["sub_type_label"], "funding_fee_expense")

    async def test_private_orders_ws_updates_latest_snapshot_and_fill_cache(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
            }
        )
        service = OKXAccountService(settings=settings, client=_FakeOKXClient())
        await service.refresh(force=True)

        await service.handle_private_ws_message(
            {
                "arg": {"channel": "orders", "instType": "SPOT"},
                "data": [
                    {
                        "instId": "BTC-USDT",
                        "ordId": "ord_1",
                        "clOrdId": "cl_1",
                        "side": "buy",
                        "ordType": "limit",
                        "state": "partially_filled",
                        "sz": "0.01",
                        "accFillSz": "0.004",
                        "px": "68000",
                        "fillSz": "0.004",
                        "fillPx": "67990",
                        "fillFee": "-0.27196",
                        "fillFeeCcy": "USDT",
                        "tradeId": "trade_ws_1",
                        "fillTime": "1700000005000",
                        "uTime": "1700000005000",
                        "cTime": "1700000004000",
                    }
                ],
            }
        )

        snapshot = service.latest_snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(len(snapshot.open_orders), 1)
        self.assertEqual(snapshot.open_orders[0].exchange_order_id, "ord_1")
        self.assertEqual(snapshot.fills[-1].fill_id, "trade_ws_1")
        self.assertIsNotNone(service.latest_private_order_row(symbol="BTC-USDT", order_id="ord_1"))
        self.assertEqual(len(service.latest_private_order_fills(symbol="BTC-USDT", order_id="ord_1")), 1)


if __name__ == "__main__":
    unittest.main()
