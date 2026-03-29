from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.services.execution_engine.okx_account import OKXAccountService, datetime_from_ms


def _ms_from_now(offset_seconds: int) -> str:
    return str(int((utc_now() + timedelta(seconds=offset_seconds)).timestamp() * 1000))


class _FakeOKXClient:
    def __init__(self) -> None:
        self.get_positions_called = False
        self.trade_fee_calls: list[dict[str, object | None]] = []
        self.funding_rate_calls: list[str] = []

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
        return {"code": "0", "data": [{"acctLv": "1", "posMode": "net_mode", "autoLoan": False}]}

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
        return {"code": "0", "data": [{"adjEq": "1000", "imr": "10", "mmr": "5", "mgnRatio": "100"}]}

    async def get_system_status(self):
        return {"code": "0", "data": []}

    async def get_funding_rate(self, *, symbol: str):
        self.funding_rate_calls.append(symbol)
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
                    "instType": "SWAP",
                    "instFamily": "BTC-USDT",
                    "baseCcy": "",
                    "quoteCcy": "",
                    "uly": "BTC-USDT",
                    "settleCcy": "USDT",
                    "ctValCcy": "BTC",
                    "ctVal": "0.01",
                    "lotSz": "0.01",
                    "tickSz": "0.1",
                    "minSz": "0.01",
                    "lever": "25",
                    "maxMktSz": "2000",
                    "maxLmtSz": "2500",
                    "listTime": "1700000000000",
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
                    "mgnMode": "cross",
                    "ccy": "USDT",
                    "lever": "12",
                    "margin": "320",
                    "mmr": "140",
                    "mgnRatio": "5.2",
                    "liqPx": "62000",
                    "upl": "12.5",
                }
            ],
        }

    async def get_account_config(self):
        return {
            "code": "0",
            "data": [{"acctLv": "2", "posMode": "net_mode", "autoLoan": True, "greeksType": "PA", "ctIsoMode": "automatic"}],
        }

    async def get_funding_rate(self, *, symbol: str):
        self.funding_rate_calls.append(symbol)
        if symbol != "BTC-USDT-SWAP":
            return {"code": "0", "data": []}
        return {
            "code": "0",
            "data": [
                {
                    "instId": symbol,
                    "fundingRate": "0.0001",
                    "fundingTime": "1700000000000",
                    "nextFundingTime": "1700014400000",
                    "ts": "1700003600000",
                }
            ],
        }


class _FakeIncompatibleDerivativesClient(_FakeDerivativesOKXClient):
    async def get_account_config(self):
        return {"code": "0", "data": [{"acctLv": "1", "posMode": ""}]}


class _FakeFuturesDerivativesClient(_FakeDerivativesOKXClient):
    async def get_instruments(self):
        return {
            "code": "0",
            "data": [
                {
                    "instId": "BTC-USDT-240329",
                    "instType": "FUTURES",
                    "instFamily": "BTC-USDT",
                    "baseCcy": "",
                    "quoteCcy": "",
                    "uly": "BTC-USDT",
                    "settleCcy": "USDT",
                    "ctValCcy": "BTC",
                    "ctVal": "0.01",
                    "lotSz": "1",
                    "tickSz": "0.1",
                    "minSz": "1",
                    "lever": "20",
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
                    "instId": "BTC-USDT-240329",
                    "pos": "2",
                    "avgPx": "80500",
                    "markPx": "80600",
                    "notionalUsd": "1612",
                    "posSide": "long",
                    "mgnMode": "cross",
                    "ccy": "USDT",
                }
            ],
        }


class _FakeShortDerivativesClient(_FakeDerivativesOKXClient):
    async def get_positions(self):
        self.get_positions_called = True
        return {
            "code": "0",
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "pos": "0.18",
                    "avgPx": "66404.8",
                    "markPx": "66838.5",
                    "notionalUsd": "120.22748967599999",
                    "posSide": "short",
                    "mgnMode": "cross",
                    "ccy": "USDT",
                    "lever": "10",
                    "imr": "12.03093",
                    "mmr": "0.4812372",
                    "mgnRatio": "214.10086239723972",
                    "liqPx": "130646.40339573975",
                    "upl": "-0.7806599999999948",
                }
            ],
        }


class _FakeMultiPositionDerivativesClient(_FakeDerivativesOKXClient):
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
                },
                {
                    "instId": "ETH-USDT-SWAP",
                    "baseCcy": "",
                    "quoteCcy": "",
                    "uly": "ETH-USDT",
                    "settleCcy": "USDT",
                    "ctValCcy": "ETH",
                    "ctVal": "0.1",
                    "lotSz": "0.1",
                    "tickSz": "0.01",
                    "minSz": "0.1",
                    "state": "live",
                },
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
                },
                {
                    "instId": "ETH-USDT-SWAP",
                    "pos": "1",
                    "avgPx": "4000",
                    "markPx": "4010",
                    "notionalUsd": "4010",
                    "posSide": "long",
                },
            ],
        }


class _FakeSmartArbitrageMarginClient(_FakeDerivativesOKXClient):
    async def get_instruments(self):
        return {
            "code": "0",
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "instType": "SWAP",
                    "instFamily": "BTC-USDT",
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
                },
                {
                    "instId": "BTC-USDT",
                    "instType": "SPOT",
                    "baseCcy": "BTC",
                    "quoteCcy": "USDT",
                    "lotSz": "0.00000001",
                    "tickSz": "0.1",
                    "minSz": "0.00001",
                    "state": "live",
                },
            ],
        }

    async def get_positions(self):
        self.get_positions_called = True
        return {
            "code": "0",
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "instType": "SWAP",
                    "pos": "0.05",
                    "avgPx": "80000",
                    "markPx": "80100",
                    "notionalUsd": "4005",
                    "posSide": "long",
                    "mgnMode": "cross",
                    "ccy": "USDT",
                },
                {
                    "instId": "BTC-USDT",
                    "instType": "MARGIN",
                    "mgnMode": "cross",
                    "liab": "0.25",
                    "liabCcy": "BTC",
                    "avgPx": "79000",
                    "markPx": "79100",
                    "notionalUsd": "19775",
                },
            ],
        }


class _FakeNestedRiskPayloadDerivativesClient(_FakeDerivativesOKXClient):
    async def get_account_position_risk(self):
        return {
            "code": "0",
            "data": [
                {
                    "adjEq": "",
                    "balData": [
                        {"ccy": "USDT", "eq": "201.0016337876877", "availEq": "201.0016337876877"},
                        {"ccy": "BTC", "eq": "0.0000000086839915", "disEq": "0"},
                    ],
                    "posData": [
                        {
                            "instId": "BTC-USDT-SWAP",
                            "instType": "SWAP",
                            "mgnMode": "cross",
                            "notionalUsd": "120.5187115716000000",
                            "pos": "0.17",
                            "posSide": "long",
                        }
                    ],
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
                },
                {
                    "instId": "ETH-USDT-SWAP",
                    "pos": "1",
                    "avgPx": "4000",
                    "markPx": "4010",
                    "notionalUsd": "4010",
                    "posSide": "long",
                },
            ],
        }


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


class _CountingAuxClient(_FakeOKXClient):
    def __init__(self) -> None:
        super().__init__()
        self.balance_calls = 0
        self.open_order_calls = 0
        self.fill_calls = 0
        self.instrument_calls = 0
        self.account_config_calls = 0
        self.trade_fee_call_count = 0
        self.account_risk_calls = 0
        self.system_status_calls = 0
        self.bills_calls = 0

    async def get_balance(self):
        self.balance_calls += 1
        return await super().get_balance()

    async def get_open_orders(self, *, symbol: str | None = None):
        self.open_order_calls += 1
        return await super().get_open_orders(symbol=symbol)

    async def get_fills(self, *, symbol: str | None = None, limit: int | None = None):
        self.fill_calls += 1
        return await super().get_fills(symbol=symbol, limit=limit)

    async def get_instruments(self):
        self.instrument_calls += 1
        return await super().get_instruments()

    async def get_account_config(self):
        self.account_config_calls += 1
        return await super().get_account_config()

    async def get_trade_fee(
        self,
        *,
        symbol: str | None = None,
        underlying: str | None = None,
        instrument_family: str | None = None,
    ):
        self.trade_fee_call_count += 1
        return await super().get_trade_fee(
            symbol=symbol,
            underlying=underlying,
            instrument_family=instrument_family,
        )

    async def get_account_position_risk(self):
        self.account_risk_calls += 1
        return await super().get_account_position_risk()

    async def get_system_status(self):
        self.system_status_calls += 1
        return await super().get_system_status()

    async def get_bills_details(self, *, symbol: str | None = None, limit: int | None = None, begin=None, end=None):
        _ = symbol
        _ = limit
        _ = begin
        _ = end
        self.bills_calls += 1
        return {"code": "0", "data": []}


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
        self.assertEqual(instrument.instrument_type, "SWAP")
        self.assertEqual(instrument.instrument_family, "BTC-USDT")
        self.assertEqual(instrument.underlying, "BTC-USDT")
        self.assertEqual(instrument.settle_currency, "USDT")
        self.assertEqual(instrument.contract_value_currency, "BTC")
        self.assertEqual(instrument.max_leverage, Decimal("25"))
        self.assertEqual(instrument.max_market_size, Decimal("2000"))
        self.assertEqual(instrument.max_limit_size, Decimal("2500"))
        self.assertIsNotNone(instrument.list_ts)
        self.assertEqual(snapshot.positions[0].quantity, Decimal("0.0002"))
        self.assertEqual(snapshot.positions[0].margin_mode, "cross")
        self.assertEqual(snapshot.positions[0].margin_currency, "USDT")
        self.assertEqual(snapshot.positions[0].leverage, Decimal("12"))
        self.assertEqual(snapshot.positions[0].margin_allocated, Decimal("320"))
        self.assertEqual(snapshot.positions[0].maintenance_margin, Decimal("140"))
        self.assertEqual(snapshot.positions[0].margin_ratio, Decimal("5.2"))
        self.assertEqual(snapshot.positions[0].liquidation_price, Decimal("62000"))
        self.assertEqual(snapshot.positions[0].unrealized_pnl, Decimal("12.5"))
        self.assertEqual(snapshot.positions[0].instrument_family, "BTC-USDT")
        self.assertEqual(snapshot.positions[0].settle_currency, "USDT")
        self.assertEqual(snapshot.position_mode, "net_mode")
        self.assertEqual(snapshot.fee_rates["taker"], "0.001")
        self.assertIsNotNone(snapshot.account_configuration)
        schedule = service.funding_schedule(symbol="BTC-USDT-SWAP")
        self.assertTrue(schedule["available"])
        self.assertEqual(schedule["funding_time"], datetime_from_ms("1700000000000"))
        self.assertEqual(schedule["next_funding_time"], datetime_from_ms("1700014400000"))
        self.assertEqual(schedule["updated_at"], datetime_from_ms("1700003600000"))
        self.assertEqual(schedule["funding_interval_hours"], Decimal("4"))
        self.assertEqual(service.next_funding_time("BTC-USDT-SWAP"), datetime_from_ms("1700014400000"))
        self.assertEqual(service.funding_interval_hours("BTC-USDT-SWAP"), Decimal("4"))
        self.assertEqual(client.funding_rate_calls, ["BTC-USDT-SWAP"])

    async def test_derivatives_refresh_keeps_short_side_when_okx_reports_positive_position_size(self) -> None:
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
        client = _FakeShortDerivativesClient()
        service = OKXAccountService(settings=settings, client=client)

        snapshot = await service.refresh(force=True)

        self.assertIsNotNone(snapshot)
        self.assertTrue(client.get_positions_called)
        self.assertEqual(len(snapshot.positions), 1)
        self.assertEqual(snapshot.positions[0].symbol, "BTC-USDT-SWAP")
        self.assertEqual(snapshot.positions[0].side, "short")
        self.assertEqual(snapshot.positions[0].quantity, Decimal("0.0018"))
        self.assertEqual(snapshot.positions[0].mark_price, Decimal("66838.5"))
        self.assertEqual(snapshot.positions[0].liquidation_price, Decimal("130646.40339573975"))

    async def test_derivatives_refresh_converts_futures_contract_quantity_to_internal_units(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
                "trading_product_type": "derivatives",
                "default_symbol": "BTC-USDT-240329",
            }
        )
        client = _FakeFuturesDerivativesClient()
        service = OKXAccountService(settings=settings, client=client)

        snapshot = await service.refresh(force=True)

        self.assertIsNotNone(snapshot)
        self.assertTrue(client.get_positions_called)
        self.assertEqual(snapshot.positions[0].symbol, "BTC-USDT-240329")
        self.assertEqual(snapshot.positions[0].quantity, Decimal("0.02"))
        instrument = service.instrument_metadata("BTC-USDT-240329")
        self.assertIsNotNone(instrument)
        self.assertEqual(instrument.instrument_type, "FUTURES")
        self.assertEqual(instrument.contract_value, Decimal("0.01"))
        self.assertEqual(snapshot.account_configuration.account_level_code, "2")
        self.assertEqual(snapshot.account_configuration.account_level_label, "single_currency_margin")
        self.assertEqual(snapshot.account_configuration.position_mode_label, "net")
        self.assertTrue(snapshot.account_configuration.auto_loan_enabled)
        self.assertEqual(snapshot.account_configuration.greeks_type, "PA")
        self.assertEqual(snapshot.account_configuration.isolated_margin_mode, "automatic")
        self.assertIsNotNone(snapshot.fee_schedule)
        self.assertEqual(snapshot.fee_schedule.taker, Decimal("0.001"))
        self.assertEqual(snapshot.fee_schedule.maker, Decimal("-0.0008"))
        self.assertEqual(service.effective_maker_fee_bps(), Decimal("-8.0000"))
        self.assertIsNotNone(snapshot.risk_snapshot)
        self.assertEqual(snapshot.risk_snapshot.adjusted_equity, Decimal("1000"))
        self.assertEqual(snapshot.risk_snapshot.initial_margin_requirement, Decimal("10"))
        self.assertEqual(snapshot.risk_snapshot.maintenance_margin_requirement, Decimal("5"))
        self.assertEqual(snapshot.risk_snapshot.margin_ratio, Decimal("100"))
        self.assertEqual(len(snapshot.system_status_items), 0)
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

    async def test_refresh_caches_low_frequency_auxiliary_payloads_without_force(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
                "okx_account_refresh_interval_seconds": 0,
                "okx_instruments_refresh_interval_seconds": 300,
                "okx_account_config_refresh_interval_seconds": 300,
                "okx_trade_fee_refresh_interval_seconds": 300,
                "okx_account_position_risk_refresh_interval_seconds": 60,
                "okx_system_status_refresh_interval_seconds": 60,
                "okx_bills_refresh_interval_seconds": 60,
            }
        )
        client = _CountingAuxClient()
        service = OKXAccountService(settings=settings, client=client)

        await service.refresh()
        await service.refresh()

        self.assertEqual(client.balance_calls, 2)
        self.assertEqual(client.open_order_calls, 2)
        self.assertEqual(client.fill_calls, 2)
        self.assertEqual(client.instrument_calls, 1)
        self.assertEqual(client.account_config_calls, 1)
        self.assertEqual(client.trade_fee_call_count, 1)
        self.assertEqual(client.account_risk_calls, 1)
        self.assertEqual(client.system_status_calls, 1)
        self.assertEqual(client.bills_calls, 1)

    async def test_force_refresh_bypasses_low_frequency_auxiliary_payload_caches(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
                "okx_instruments_refresh_interval_seconds": 300,
                "okx_account_config_refresh_interval_seconds": 300,
                "okx_trade_fee_refresh_interval_seconds": 300,
                "okx_account_position_risk_refresh_interval_seconds": 60,
                "okx_system_status_refresh_interval_seconds": 60,
                "okx_bills_refresh_interval_seconds": 60,
            }
        )
        client = _CountingAuxClient()
        service = OKXAccountService(settings=settings, client=client)

        await service.refresh(force=True)
        await service.refresh(force=True)

        self.assertEqual(client.balance_calls, 2)
        self.assertEqual(client.open_order_calls, 2)
        self.assertEqual(client.fill_calls, 2)
        self.assertEqual(client.instrument_calls, 2)
        self.assertEqual(client.account_config_calls, 2)
        self.assertEqual(client.trade_fee_call_count, 2)
        self.assertEqual(client.account_risk_calls, 2)
        self.assertEqual(client.system_status_calls, 2)
        self.assertEqual(client.bills_calls, 2)

    async def test_refresh_parses_nested_okx_risk_payload_shape(self) -> None:
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
        service = OKXAccountService(settings=settings, client=_FakeNestedRiskPayloadDerivativesClient())

        snapshot = await service.refresh(force=True)

        self.assertIsNotNone(snapshot)
        self.assertIsNotNone(snapshot.risk_snapshot)
        self.assertEqual(snapshot.risk_snapshot.adjusted_equity, Decimal("201.0016337876877"))
        self.assertEqual(snapshot.risk_snapshot.available_equity, Decimal("201.0016337876877"))
        self.assertEqual(snapshot.risk_snapshot.notional_usd, Decimal("120.5187115716000000"))

    async def test_derivatives_refresh_parses_margin_liability_into_short_spot_position(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
                "trading_product_type": "derivatives",
                "default_symbol": "BTC-USDT-SWAP",
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_negative_basis_mode": "margin_backed",
                "smart_arbitrage_margin_short_enabled": True,
                "smart_arbitrage_margin_short_execution_ready": True,
                "smart_arbitrage_margin_short_spot_margin_mode": "cross",
            }
        )
        service = OKXAccountService(settings=settings, client=_FakeSmartArbitrageMarginClient())

        snapshot = await service.refresh(force=True)

        self.assertIsNotNone(snapshot)
        margin_position = next(item for item in snapshot.positions if item.symbol == "BTC-USDT")
        self.assertEqual(margin_position.side, "short")
        self.assertEqual(margin_position.quantity, Decimal("0.25"))
        self.assertEqual(margin_position.margin_mode, "cross")

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
        self.assertIsNotNone(status["account_configuration"])
        self.assertEqual(status["account_configuration"]["account_level_code"], "1")
        self.assertEqual(status["account_configuration"]["account_level_label"], "simple")

    async def test_status_surfaces_derivatives_position_mode_contract_and_blocks_mismatch(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "derivatives_position_mode": "hedge",
            }
        )
        service = OKXAccountService(settings=settings, client=_FakeDerivativesOKXClient())

        await service.refresh(force=True)
        status = service.status()

        self.assertFalse(status["ready"])
        self.assertIn("okx_position_mode_mismatch", status["blockers"])
        contract = status["position_mode_contract"]
        self.assertEqual(contract["configured_derivatives_position_mode"], "hedge")
        self.assertEqual(contract["required_exchange_position_mode"], "long_short_mode")
        self.assertEqual(contract["exchange_position_mode"], "net_mode")
        self.assertFalse(contract["exchange_position_mode_matches_configured"])
        self.assertEqual(contract["startup_error_code"], "derivatives_exchange_runtime_position_mode_mismatch")

    async def test_status_can_observe_position_mode_mismatch_without_blocking_when_match_requirement_disabled(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "derivatives_position_mode": "hedge",
                "derivatives_require_exchange_pos_mode_match": False,
            }
        )
        service = OKXAccountService(settings=settings, client=_FakeDerivativesOKXClient())

        await service.refresh(force=True)
        status = service.status()

        self.assertTrue(status["ready"])
        self.assertNotIn("okx_position_mode_mismatch", status["blockers"])
        contract = status["position_mode_contract"]
        self.assertEqual(contract["configured_derivatives_position_mode"], "hedge")
        self.assertEqual(contract["required_exchange_position_mode"], "long_short_mode")
        self.assertEqual(contract["exchange_position_mode"], "net_mode")
        self.assertFalse(contract["position_mode_match_required"])
        self.assertFalse(contract["exchange_position_mode_matches_configured"])
        self.assertIsNone(contract["startup_error_code"])

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
        self.assertEqual(status["system_status_items"][0]["state"], "ongoing")

    async def test_status_blocks_when_position_margin_mode_conflicts_with_runtime_margin_mode(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
                "trading_product_type": "derivatives",
                "margin_mode": "isolated",
                "default_symbol": "BTC-USDT-SWAP",
            }
        )
        service = OKXAccountService(settings=settings, client=_FakeDerivativesOKXClient())

        await service.refresh(force=True)
        status = service.status()

        self.assertFalse(status["ready"])
        self.assertIn("okx_position_margin_mode_conflicts_with_runtime_margin_mode", status["blockers"])

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
        update_ts = _ms_from_now(5)

        await service.handle_private_ws_message(
            {
                "arg": {"channel": "balance_and_position"},
                "data": [
                    {
                        "pTime": update_ts,
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

    async def test_private_balance_and_position_ws_empty_positions_do_not_clear_rest_snapshot(self) -> None:
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
        update_ts = _ms_from_now(5)

        await service.handle_private_ws_message(
            {
                "arg": {"channel": "balance_and_position"},
                "data": [
                    {
                        "pTime": update_ts,
                        "balData": [
                            {"ccy": "USDT", "cashBal": "1200", "availBal": "1180"},
                        ],
                        "posData": [],
                    }
                ],
            }
        )

        snapshot = service.latest_snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.balances[0].total, Decimal("1200"))
        self.assertEqual(len(snapshot.positions), 1)
        self.assertEqual(snapshot.positions[0].quantity, Decimal("0.0002"))

        refreshed = await service.refresh(force=True)
        self.assertIsNotNone(refreshed)
        self.assertEqual(len(refreshed.positions), 1)
        self.assertEqual(refreshed.positions[0].quantity, Decimal("0.0002"))

    async def test_private_balance_and_position_ws_ignores_stale_updates(self) -> None:
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
        latest_update_ts = _ms_from_now(10)
        stale_update_ts = _ms_from_now(-10)

        await service.handle_private_ws_message(
            {
                "arg": {"channel": "balance_and_position"},
                "data": [
                    {
                        "pTime": latest_update_ts,
                        "balData": [{"ccy": "USDT", "cashBal": "1250", "availBal": "1230"}],
                        "posData": [
                            {
                                "instId": "BTC-USDT-SWAP",
                                "pos": "0.04",
                                "avgPx": "81000",
                                "markPx": "81200",
                                "notionalUsd": "3248",
                                "posSide": "long",
                            }
                        ],
                    }
                ],
            }
        )

        await service.handle_private_ws_message(
            {
                "arg": {"channel": "balance_and_position"},
                "data": [
                    {
                        "pTime": stale_update_ts,
                        "balData": [{"ccy": "USDT", "cashBal": "900", "availBal": "900"}],
                        "posData": [],
                    }
                ],
            }
        )

        snapshot = service.latest_snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.balances[0].total, Decimal("1250"))
        self.assertEqual(snapshot.positions[0].quantity, Decimal("0.0004"))
        self.assertEqual(snapshot.positions[0].average_entry_price, Decimal("81000"))

        refreshed = await service.refresh(force=True)
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.balances[0].total, Decimal("1250"))
        self.assertEqual(refreshed.positions[0].quantity, Decimal("0.0004"))

    async def test_private_balance_and_position_ws_ignores_message_older_than_current_snapshot(self) -> None:
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
                        "pTime": _ms_from_now(-10),
                        "balData": [{"ccy": "USDT", "cashBal": "900", "availBal": "900"}],
                        "posData": [
                            {
                                "instId": "BTC-USDT-SWAP",
                                "pos": "0.01",
                                "avgPx": "79000",
                                "markPx": "79100",
                                "notionalUsd": "791",
                                "posSide": "long",
                            }
                        ],
                    }
                ],
            }
        )

        snapshot = service.latest_snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.balances[0].total, Decimal("1000"))
        self.assertEqual(snapshot.positions[0].quantity, Decimal("0.0002"))

    async def test_balance_and_position_ws_accepts_newer_balance_after_newer_orders_update(self) -> None:
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
                "arg": {"channel": "orders", "instType": "SWAP"},
                "data": [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "ordId": "ord_1",
                        "clOrdId": "cl_1",
                        "side": "buy",
                        "ordType": "limit",
                        "state": "live",
                        "sz": "0.02",
                        "accFillSz": "0",
                        "px": "68000",
                        "fillSz": "",
                        "fillPx": "",
                        "tradeId": "",
                        "fillTime": "",
                        "uTime": _ms_from_now(10),
                        "cTime": _ms_from_now(9),
                    }
                ],
            }
        )

        await service.handle_private_ws_message(
            {
                "arg": {"channel": "balance_and_position"},
                "data": [
                    {
                        "pTime": _ms_from_now(5),
                        "balData": [{"ccy": "USDT", "cashBal": "1200", "availBal": "1180"}],
                    }
                ],
            }
        )

        snapshot = service.latest_snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.balances[0].total, Decimal("1200"))
        self.assertEqual(len(snapshot.open_orders), 1)

    async def test_private_balance_and_position_ws_preserves_existing_position_fields_on_partial_delta(self) -> None:
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
        update_ts = _ms_from_now(5)

        await service.handle_private_ws_message(
            {
                "arg": {"channel": "balance_and_position"},
                "data": [
                    {
                        "pTime": update_ts,
                        "posData": [
                            {
                                "instId": "BTC-USDT-SWAP",
                                "pos": "0.03",
                                "posSide": "long",
                            }
                        ],
                    }
                ],
            }
        )

        snapshot = service.latest_snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.positions[0].quantity, Decimal("0.0003"))
        self.assertEqual(snapshot.positions[0].average_entry_price, Decimal("80000"))
        self.assertEqual(snapshot.positions[0].mark_price, Decimal("80100"))
        self.assertEqual(snapshot.positions[0].notional_usd, Decimal("1602"))

    async def test_private_balance_and_position_ws_zero_quantity_removes_only_target_position(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
                "trading_product_type": "derivatives",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP", "ETH-USDT-SWAP"),
            }
        )
        service = OKXAccountService(settings=settings, client=_FakeMultiPositionDerivativesClient())
        await service.refresh(force=True)
        update_ts = _ms_from_now(5)

        await service.handle_private_ws_message(
            {
                "arg": {"channel": "balance_and_position"},
                "data": [
                    {
                        "pTime": update_ts,
                        "posData": [
                            {
                                "instId": "BTC-USDT-SWAP",
                                "pos": "0",
                                "posSide": "long",
                            }
                        ],
                    }
                ],
            }
        )

        snapshot = service.latest_snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(len(snapshot.positions), 1)
        self.assertEqual(snapshot.positions[0].symbol, "ETH-USDT-SWAP")
        self.assertEqual(snapshot.positions[0].quantity, Decimal("0.1"))

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

    async def test_recent_funding_fee_summary_exposes_total_and_per_event_bps_proxy(self) -> None:
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

        class _FundingBillsClient(_FakeDerivativesOKXClient):
            async def get_bills_details(self, *, symbol: str | None = None, limit: int | None = None, begin=None, end=None):
                _ = symbol
                _ = limit
                _ = begin
                _ = end
                return {
                    "code": "0",
                    "data": [
                        {
                            "billId": "funding_1",
                            "instId": "BTC-USDT-SWAP",
                            "type": "8",
                            "subType": "173",
                            "ccy": "USDT",
                            "amount": "-1.602",
                            "ts": "1700000001000",
                        },
                        {
                            "billId": "funding_2",
                            "instId": "BTC-USDT-SWAP",
                            "type": "8",
                            "subType": "173",
                            "ccy": "USDT",
                            "amount": "-1.602",
                            "ts": "1700000002000",
                        },
                    ],
                }

        service = OKXAccountService(settings=settings, client=_FundingBillsClient())
        await service.refresh(force=True)
        await service.recent_bills(limit=10)

        summary = service.recent_funding_fee_summary(symbol="BTC-USDT-SWAP")

        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["funding_fee_bps_proxy"], Decimal("20"))
        self.assertEqual(summary["funding_fee_bps_proxy_per_event"], Decimal("10"))
        self.assertEqual(service.funding_fee_bps_proxy(symbol="BTC-USDT-SWAP"), Decimal("20"))
        self.assertEqual(service.funding_fee_bps_proxy_per_event(symbol="BTC-USDT-SWAP"), Decimal("10"))

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
        update_ts = _ms_from_now(5)

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
                        "fillTime": update_ts,
                        "uTime": update_ts,
                        "cTime": _ms_from_now(4),
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

    async def test_private_orders_ws_ignores_non_fill_updates_with_blank_fill_fields(self) -> None:
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
        update_ts = _ms_from_now(5)

        await service.handle_private_ws_message(
            {
                "arg": {"channel": "orders", "instType": "SPOT"},
                "data": [
                    {
                        "instId": "BTC-USDT",
                        "ordId": "ord_2",
                        "clOrdId": "cl_2",
                        "side": "buy",
                        "ordType": "limit",
                        "state": "live",
                        "sz": "0.01",
                        "accFillSz": "0",
                        "px": "68000",
                        "fillSz": "",
                        "fillPx": "",
                        "fillFee": "",
                        "fillFeeCcy": "",
                        "tradeId": "",
                        "fillTime": "",
                        "uTime": update_ts,
                        "cTime": _ms_from_now(4),
                    }
                ],
            }
        )

        snapshot = service.latest_snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(len(snapshot.open_orders), 1)
        self.assertEqual(len(snapshot.fills), 0)


if __name__ == "__main__":
    unittest.main()
