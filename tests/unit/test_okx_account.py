from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.exchange import ExchangeAccountSnapshot, ExchangeBalance
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


class _FakeFutureScheduledSystemMaintenanceClient(_FakeOKXClient):
    async def get_system_status(self):
        return {
            "code": "0",
            "data": [
                {
                    "state": "scheduled",
                    "serviceType": "5",
                    "title": "Trading Service Scheduled Maintenance",
                    "begin": _ms_from_now(3600),
                    "end": _ms_from_now(4800),
                }
            ],
        }


class _FakeDueScheduledSystemMaintenanceClient(_FakeOKXClient):
    async def get_system_status(self):
        return {
            "code": "0",
            "data": [
                {
                    "state": "scheduled",
                    "serviceType": "5",
                    "title": "Trading Service Scheduled Maintenance",
                    "begin": _ms_from_now(-60),
                    "end": _ms_from_now(1200),
                }
            ],
        }


class _FakeTrailingStopSystemMaintenanceClient(_FakeOKXClient):
    async def get_system_status(self):
        return {
            "code": "0",
            "data": [
                {
                    "state": "ongoing",
                    "serviceType": "99",
                    "title": "Trailing Stop Scheduled Maintenance ",
                    "begin": _ms_from_now(-60),
                    "end": _ms_from_now(1200),
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


class _BalanceFailsAfterFirstClient(_CountingAuxClient):
    async def get_balance(self):
        self.balance_calls += 1
        if self.balance_calls > 1:
            raise RuntimeError("balance_down")
        return await _FakeOKXClient.get_balance(self)


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

    async def test_force_account_state_refresh_keeps_low_frequency_auxiliary_caches(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
                "okx_account_refresh_interval_seconds": 300,
                "okx_instruments_refresh_interval_seconds": 300,
                "okx_account_config_refresh_interval_seconds": 300,
                "okx_trade_fee_refresh_interval_seconds": 300,
                "okx_account_position_risk_refresh_interval_seconds": 300,
                "okx_system_status_refresh_interval_seconds": 300,
                "okx_bills_refresh_interval_seconds": 300,
            }
        )
        client = _CountingAuxClient()
        service = OKXAccountService(settings=settings, client=client)

        await service.refresh()
        await service.refresh(force_account_state=True)

        self.assertEqual(client.balance_calls, 2)
        self.assertEqual(client.open_order_calls, 2)
        self.assertEqual(client.fill_calls, 2)
        self.assertEqual(client.instrument_calls, 1)
        self.assertEqual(client.account_config_calls, 1)
        self.assertEqual(client.trade_fee_call_count, 1)
        self.assertEqual(client.account_risk_calls, 2)
        self.assertEqual(client.system_status_calls, 1)
        self.assertEqual(client.bills_calls, 1)

    async def test_force_account_state_refresh_failure_returns_none_instead_of_stale_snapshot(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
                "okx_account_refresh_interval_seconds": 300,
            }
        )
        client = _BalanceFailsAfterFirstClient()
        service = OKXAccountService(settings=settings, client=client)

        first = await service.refresh()
        second = await service.refresh(force_account_state=True)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertIs(service.latest_snapshot(), first)
        self.assertFalse(service.status()["ready"])
        self.assertIn("okx_account_state_refresh_failed:balance", service.status()["last_error"])

    async def test_force_refresh_core_state_failure_returns_none_instead_of_stale_snapshot(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
                "okx_account_refresh_interval_seconds": 300,
            }
        )
        client = _BalanceFailsAfterFirstClient()
        service = OKXAccountService(settings=settings, client=client)

        first = await service.refresh()
        second = await service.refresh(force=True)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertIs(service.latest_snapshot(), first)
        self.assertFalse(service.status()["ready"])
        self.assertIn("okx_account_state_refresh_failed:balance", service.status()["last_error"])

    async def test_force_refresh_credentials_missing_returns_none_instead_of_stale_snapshot(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
            }
        )
        service = OKXAccountService(settings=settings, client=_FakeOKXClient())
        stale = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=Decimal("1000"), available=Decimal("1000"))],
        )
        service._latest_snapshot = stale

        self.assertIs(await service.refresh(), stale)
        self.assertIsNone(await service.refresh(force=True))
        self.assertIsNone(await service.refresh(force_account_state=True))
        self.assertIs(service.latest_snapshot(), stale)
        self.assertFalse(service.status()["ready"])
        self.assertEqual(service.status()["last_error"], "credentials_missing")

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

    async def test_status_does_not_block_future_scheduled_okx_maintenance(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
            }
        )
        service = OKXAccountService(settings=settings, client=_FakeFutureScheduledSystemMaintenanceClient())

        await service.refresh(force=True)
        status = service.status()

        self.assertTrue(status["ready"])
        self.assertNotIn("okx_system_status_incident", status["blockers"])
        self.assertTrue(status["system_status_ok"])
        self.assertEqual(status["system_status_items"][0]["state"], "scheduled")

    async def test_status_blocks_when_scheduled_okx_maintenance_is_due(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
            }
        )
        service = OKXAccountService(settings=settings, client=_FakeDueScheduledSystemMaintenanceClient())

        await service.refresh(force=True)
        status = service.status()

        self.assertFalse(status["ready"])
        self.assertIn("okx_system_status_incident", status["blockers"])
        self.assertFalse(status["system_status_ok"])

    async def test_status_does_not_block_trailing_stop_only_okx_maintenance(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
            }
        )
        service = OKXAccountService(settings=settings, client=_FakeTrailingStopSystemMaintenanceClient())

        await service.refresh(force=True)
        status = service.status()

        self.assertTrue(status["ready"])
        self.assertNotIn("okx_system_status_incident", status["blockers"])
        self.assertTrue(status["system_status_ok"])
        self.assertEqual(status["system_status_items"][0]["service_type"], "99")

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


# ─────────────────────────────────────────────────────────────────────────
# Phase 1A deploy retro (2026-04-20): _merge_payloads 防御性 fix 锁定测试
# ─────────────────────────────────────────────────────────────────────────


class TestMergePayloadsDefensive(unittest.TestCase):
    """Phase 1A deploy 发现 OKX 401 时 gather(return_exceptions=True)
    会让 payloads 列表混入 OKXRequestError 对象, _merge_payloads 早先
    直接 .get('data') 引发 AttributeError, 触发 execution 进程 crash
    loop. 本测试锁定: 异常对象必须被跳过, 不让整个 refresh 流程崩溃.
    """

    def test_all_dict_payloads_merge_as_before(self) -> None:
        """baseline: 全部合法 dict payload, 数据合并不丢。"""
        payloads = [
            {"code": "0", "data": [{"id": "a"}, {"id": "b"}]},
            {"code": "0", "data": [{"id": "c"}]},
        ]
        merged = OKXAccountService._merge_payloads(payloads)
        self.assertEqual(merged["code"], "0")
        self.assertEqual([row["id"] for row in merged["data"]], ["a", "b", "c"])

    def test_exception_object_is_skipped(self) -> None:
        """单个 payload 是 Exception (如 OKXRequestError) → 跳过, 其余合并。"""
        class OKXRequestError(Exception):
            pass

        payloads = [
            {"code": "0", "data": [{"id": "ok1"}]},
            OKXRequestError("401 Unauthorized"),  # type: ignore[list-item]
            {"code": "0", "data": [{"id": "ok2"}]},
        ]
        # 关键: 不抛 AttributeError, 返回 2 行可用数据
        merged = OKXAccountService._merge_payloads(payloads)
        self.assertEqual([row["id"] for row in merged["data"]], ["ok1", "ok2"])

    def test_all_exceptions_returns_empty_data(self) -> None:
        """全部 OKX 失败 → merged 为空 list, 不抛异常。"""
        class OKXRequestError(Exception):
            pass

        payloads = [OKXRequestError("401"), OKXRequestError("429")]
        merged = OKXAccountService._merge_payloads(payloads)  # type: ignore[arg-type]
        self.assertEqual(merged["data"], [])

    def test_non_dict_non_exception_scalars_are_skipped(self) -> None:
        """防御性: None / 字符串 / 列表等非 dict 类型都安全跳过。"""
        payloads = [
            None,  # type: ignore[list-item]
            "error string",  # type: ignore[list-item]
            [],  # type: ignore[list-item]
            {"code": "0", "data": [{"id": "valid"}]},
        ]
        merged = OKXAccountService._merge_payloads(payloads)
        self.assertEqual([row["id"] for row in merged["data"]], ["valid"])

    def test_skip_logs_warning_with_context(self) -> None:
        """2026-04-20 code review A-H1: 非 dict 被 skip 必须 log warning.

        之前版本完全 silent, 导致"所有 gather 都 Exception → 返回空 data"
        被下游 reconciliation 误判为"无未成交单". 本测试锁定: 任一 skip
        必 emit structured log (event_name=okx_merge_payloads_skipped_exception),
        且含 context + skipped_non_dict 字段供运维追踪.
        """
        import io
        import logging

        # 捕获 aats.okx_account.merge_payloads logger 输出
        logger = logging.getLogger("aats.okx_account.merge_payloads")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.WARNING)
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)

        try:
            class OKXRequestError(Exception):
                pass

            payloads = [
                OKXRequestError("401"),  # type: ignore[list-item]
                {"code": "0", "data": [{"id": "ok"}]},
            ]
            merged = OKXAccountService._merge_payloads(payloads, context="unit_test_scope")  # type: ignore[arg-type]
            self.assertEqual([row["id"] for row in merged["data"]], ["ok"])
        finally:
            logger.removeHandler(handler)

        output = stream.getvalue()
        # 结构化 log 格式是 JSON; 这里检查关键字段出现即可 (不强求 JSON parse
        # 因 log handler 格式可能 wrap)
        self.assertIn("okx_merge_payloads_skipped_exception", output)
        self.assertIn("unit_test_scope", output)
        # skipped_non_dict=1, total_payloads=2, merged_rows=1 应在 log 里可追溯
        self.assertIn("skipped_non_dict", output)

    def test_no_skip_no_log(self) -> None:
        """Baseline path (全部 dict payload) 不应触发 warning log."""
        import io
        import logging

        logger = logging.getLogger("aats.okx_account.merge_payloads")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.WARNING)
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)

        try:
            payloads = [{"code": "0", "data": [{"id": "a"}]}]
            OKXAccountService._merge_payloads(payloads, context="baseline")
        finally:
            logger.removeHandler(handler)

        self.assertEqual(stream.getvalue(), "", "正常路径不应产生任何 warning log")


# ─────────────────────────────────────────────────────────────────────────
# 2026-04-22 OKX 429 rate-limit 治理: P-B 根治锁定测试
#   命中 OKX classification=rate_limited 时, _cached_aux_payload_optional
#   把对应 cache_key 的 fetched_at 推到未来, 后续 refresh tick 在 backoff
#   窗口内直接命中缓存, 不再访问 OKX 公共端点 → 限流自愈.
# ─────────────────────────────────────────────────────────────────────────


class _RateLimitedSystemStatusClient(_CountingAuxClient):
    def __init__(self, *, raise_n_times: int = 1) -> None:
        super().__init__()
        self.raise_n_times = raise_n_times

    async def get_system_status(self):
        self.system_status_calls += 1
        if self.system_status_calls <= self.raise_n_times:
            from aats.services.execution_engine.okx_rest import OKXRequestError

            raise OKXRequestError(
                path="/api/v5/system/status",
                code="50011",
                msg="Requests too frequent.",
                status_code=429,
                classification="rate_limited",
                retryable=True,
            )
        return await super().get_system_status()


class _RateLimitedAccountRiskClient(_CountingAuxClient):
    async def get_account_position_risk(self):
        self.account_risk_calls += 1
        from aats.services.execution_engine.okx_rest import OKXRequestError

        raise OKXRequestError(
            path="/api/v5/account/account-position-risk",
            code="50011",
            msg="Requests too frequent.",
            status_code=429,
            classification="rate_limited",
            retryable=True,
        )


class TestOKXRateLimitedBackoff(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limited_pushes_cache_fetched_at_forward(self) -> None:
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
        client = _RateLimitedSystemStatusClient(raise_n_times=1)
        service = OKXAccountService(settings=settings, client=client)

        await service.refresh()
        await service.refresh()
        await service.refresh()

        # 三次 refresh 调用, 但 system_status 因 backoff 仅打 1 次 (首次触发限流).
        self.assertEqual(
            client.system_status_calls,
            1,
            "rate_limited 后续 tick 必须命中 backoff 缓存, 不能再次访问 OKX",
        )
        # 其他 cache_key 不受影响, instruments / account_config 仍仅 1 次.
        self.assertEqual(client.instrument_calls, 1)
        self.assertEqual(client.account_config_calls, 1)

    async def test_rate_limited_falls_back_to_empty_payload_when_no_prior_cache(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
                "okx_account_refresh_interval_seconds": 0,
                "okx_system_status_refresh_interval_seconds": 60,
            }
        )
        client = _RateLimitedSystemStatusClient(raise_n_times=10)
        service = OKXAccountService(settings=settings, client=client)

        # 首次限流时无历史缓存, 必须降级为空 payload (默认非阻塞).
        snapshot = await service.refresh()
        self.assertIsNotNone(snapshot)
        # 后续 refresh 不能再次打公共端点.
        await service.refresh()
        await service.refresh()
        self.assertEqual(
            client.system_status_calls,
            1,
            "无历史缓存场景下也应进入 backoff, 不能让 429 持续打到 OKX",
        )

    async def test_force_account_state_refresh_honors_account_risk_rate_limit_backoff(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
                "okx_account_refresh_interval_seconds": 0,
                "okx_account_position_risk_refresh_interval_seconds": 60,
            }
        )
        client = _RateLimitedAccountRiskClient()
        service = OKXAccountService(settings=settings, client=client)

        await service.refresh()
        await service.refresh(force_account_state=True)
        await service.refresh(force_account_state=True)

        self.assertEqual(
            client.account_risk_calls,
            1,
            "force_account_state 也必须尊重 account_position_risk 的 rate-limit backoff",
        )


if __name__ == "__main__":
    unittest.main()
