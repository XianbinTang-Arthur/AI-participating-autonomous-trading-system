from __future__ import annotations

import unittest
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent, OrderIntent, OrderObligation, OrderState
from aats.schemas.exchange import ExchangeAccountSnapshot, ExchangeFill, InstrumentMetadata
from aats.schemas.reconciliation import ReconciliationReport
from aats.services.execution_engine.okx_adapter import OKXExecutionAdapter, OKXOrderPayloadBuilder
from aats.services.execution_engine.okx_account import datetime_from_ms
from aats.services.execution_engine.okx_rest import OKXRequestError
from aats.services.portfolio_service.positions import PortfolioState
from aats.services.governance_engine.health import SystemHealthService
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.governance_engine.mode import RuntimeModeController
from aats.storage.obligation_repo import InMemoryExecutionObligationRepository


class FakeAccountService:
    def __init__(self, *, open_order_count: int = 0, ready: bool = True) -> None:
        self._open_order_count = open_order_count
        self._ready = ready
        self._snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[],
            positions=[],
            open_orders=[],
            fills=[],
            instruments=[
                InstrumentMetadata(
                    instrument_id="BTC-USDT",
                    symbol="BTC-USDT",
                    base_currency="BTC",
                    quote_currency="USDT",
                    lot_size=0.0001,
                    tick_size=0.1,
                    min_size=0.0001,
                    state="live",
                )
            ],
            account_mode="cash",
            raw={"account_config": {"data": [{"posMode": "net_mode"}]}},
        )

    async def refresh(self, *, force: bool = False):
        return self._snapshot

    def instrument_metadata(self, symbol: str):
        return self._snapshot.instruments[0] if symbol == "BTC-USDT" else None

    def open_order_count(self, symbol: str | None = None) -> int:
        return self._open_order_count

    def latest_snapshot(self):
        return self._snapshot

    def status(self):
        return {
            "backend": "okx",
            "enabled": True,
            "credentials_configured": True,
            "connected": self._ready,
            "fresh": self._ready,
            "last_update_ts": self._snapshot.fetched_at,
            "last_error": None if self._ready else "not_ready",
            "ready": self._ready,
            "detail": "test_account",
            "blockers": [] if self._ready else ["account_not_ready"],
        }


class FakeHealthService:
    def __init__(self, blockers: list[str] | None = None) -> None:
        self._blockers = blockers or []

    def execution_blockers(self) -> list[str]:
        return list(self._blockers)


class FakeMarketProvider:
    def status(self):
        return {
            "connected": True,
            "fresh": True,
            "last_update_ts": utc_now(),
            "last_error": None,
            "ready": True,
            "detail": "test_market",
            "blockers": [],
        }


class FakeReconciliationRepo:
    def latest(self):
        return None


class ReviewRequiredReconciliationRepo:
    def latest(self):
        return ReconciliationReport(
            reconciliation_id="recon_review",
            portfolio_snapshot_ref="evt_portfolio",
            as_of_ts=utc_now(),
            order_diff={},
            fill_diff={},
            balance_diff={},
            position_diff={},
            mismatch_categories=["external_manual_activity_detected"],
            mismatch_reasons=["local_exchange_fill_set_diverges_from_exchange_fill_set"],
            safety_impacts=["operator_review_required_before_trading"],
            severity="REVIEW_REQUIRED",
            review_required=True,
            recommended_operator_action="review_and_rebaseline_if_expected",
            halt_required=False,
        )


class FakeOKXClient:
    def __init__(self) -> None:
        self.place_order_calls: list[dict] = []
        self.order_queries: list[dict] = []
        self.fill_queries: list[dict] = []

    async def place_order(self, payload):
        self.place_order_calls.append(dict(payload))
        return {"code": "0", "data": [{"ordId": "ord_1", "clOrdId": payload["clOrdId"], "sCode": "0"}]}

    async def get_order(self, *, symbol: str, order_id: str | None = None, client_order_id: str | None = None):
        self.order_queries.append({"symbol": symbol, "order_id": order_id, "client_order_id": client_order_id})
        return {
            "code": "0",
            "data": [
                {
                    "instId": symbol,
                    "ordId": order_id or "ord_1",
                    "clOrdId": client_order_id or "clord_1",
                    "state": "filled",
                    "sz": "0.001",
                    "accFillSz": "0.001",
                    "avgPx": "68000",
                    "cTime": "1700000000000",
                    "uTime": "1700000001000",
                }
            ],
        }

    async def get_fills(self, *, symbol: str | None = None, order_id: str | None = None, limit: int | None = None):
        self.fill_queries.append({"symbol": symbol, "order_id": order_id, "limit": limit})
        return {
            "code": "0",
            "data": [
                {
                    "instId": symbol,
                    "ordId": order_id or "ord_1",
                    "clOrdId": "clord_1",
                    "tradeId": "trade_1",
                    "side": "buy",
                    "fillSz": "0.001",
                    "fillPx": "68000",
                    "fee": "-0.068",
                    "feeCcy": "USDT",
                    "fillTime": "1700000001000",
                }
            ],
        }

    async def cancel_order(self, payload):
        return {"code": "0", "data": [{"ordId": payload["ordId"], "clOrdId": payload["clOrdId"], "sCode": "0"}]}

    async def get_max_order_quantity(self, *, symbol: str, td_mode: str, leverage=None, price=None):
        _ = symbol
        _ = td_mode
        _ = leverage
        _ = price
        return {"code": "0", "data": [{"maxBuy": "100", "maxSell": "100"}]}


class FakeRejectedOKXClient(FakeOKXClient):
    async def place_order(self, payload):
        raise OKXRequestError(
            path="/api/v5/trade/order",
            code="1",
            msg="All operations failed",
            row_code="51008",
            row_message="Order amount too low",
            status_code=200,
            payload={"code": "1", "msg": "All operations failed", "data": [{"sCode": "51008", "sMsg": "Order amount too low"}]},
        )


class FakePartialFillOKXClient(FakeOKXClient):
    def __init__(self) -> None:
        super().__init__()
        self._fill_stage = 0

    async def get_order(self, *, symbol: str, order_id: str | None = None, client_order_id: str | None = None):
        self.order_queries.append({"symbol": symbol, "order_id": order_id, "client_order_id": client_order_id})
        if self._fill_stage == 0:
            return {
                "code": "0",
                "data": [
                    {
                        "instId": symbol,
                        "ordId": order_id or "ord_1",
                        "clOrdId": client_order_id or "clord_1",
                        "state": "partially_filled",
                        "sz": "0.001",
                        "accFillSz": "0.0004",
                        "avgPx": "68000",
                        "cTime": "1700000000000",
                        "uTime": "1700000001000",
                    }
                ],
            }
        return {
            "code": "0",
            "data": [
                {
                    "instId": symbol,
                    "ordId": order_id or "ord_1",
                    "clOrdId": client_order_id or "clord_1",
                    "state": "filled",
                    "sz": "0.001",
                    "accFillSz": "0.001",
                    "avgPx": "68100",
                    "cTime": "1700000000000",
                    "uTime": "1700000002000",
                }
            ],
        }

    async def get_fills(self, *, symbol: str | None = None, order_id: str | None = None, limit: int | None = None):
        self.fill_queries.append({"symbol": symbol, "order_id": order_id, "limit": limit})
        if self._fill_stage == 0:
            self._fill_stage = 1
            return {
                "code": "0",
                "data": [
                    {
                        "instId": symbol,
                        "ordId": order_id or "ord_1",
                        "clOrdId": "clord_1",
                        "tradeId": "trade_partial_1",
                        "side": "buy",
                        "fillSz": "0.0004",
                        "fillPx": "68000",
                        "fee": "-0.0272",
                        "feeCcy": "USDT",
                        "fillTime": "1700000001000",
                    }
                ],
            }
        return {
            "code": "0",
            "data": [
                {
                    "instId": symbol,
                    "ordId": order_id or "ord_1",
                    "clOrdId": "clord_1",
                    "tradeId": "trade_partial_1",
                    "side": "buy",
                    "fillSz": "0.0004",
                    "fillPx": "68000",
                    "fee": "-0.0272",
                    "feeCcy": "USDT",
                    "fillTime": "1700000001000",
                },
                {
                    "instId": symbol,
                    "ordId": order_id or "ord_1",
                    "clOrdId": "clord_1",
                    "tradeId": "trade_partial_2",
                    "side": "buy",
                    "fillSz": "0.0006",
                    "fillPx": "68150",
                    "fee": "-0.04089",
                    "feeCcy": "USDT",
                    "fillTime": "1700000002000",
                }
            ],
        }


class FakeTightMaxSizeOKXClient(FakeOKXClient):
    async def get_max_order_quantity(self, *, symbol: str, td_mode: str, leverage=None, price=None):
        _ = symbol
        _ = td_mode
        _ = leverage
        _ = price
        return {"code": "0", "data": [{"maxBuy": "0.0005", "maxSell": "0.0005"}]}


class FakeUnfilteredFillsOKXClient(FakeOKXClient):
    async def get_fills(self, *, symbol: str | None = None, order_id: str | None = None, limit: int | None = None):
        self.fill_queries.append({"symbol": symbol, "order_id": order_id, "limit": limit})
        return {
            "code": "0",
            "data": [
                {
                    "instId": symbol,
                    "ordId": "historical_ord",
                    "clOrdId": "historical_client_order",
                    "tradeId": "historical_trade",
                    "side": "buy",
                    "fillSz": "0.5",
                    "fillPx": "67000",
                    "fee": "-0.01",
                    "feeCcy": "USDT",
                    "fillTime": "1699999999000",
                },
                {
                    "instId": symbol,
                    "ordId": order_id or "ord_1",
                    "clOrdId": self.place_order_calls[-1]["clOrdId"] if self.place_order_calls else "clord_1",
                    "tradeId": "trade_1",
                    "side": "buy",
                    "fillSz": "0.001",
                    "fillPx": "68000",
                    "fee": "-0.068",
                    "feeCcy": "USDT",
                    "fillTime": "1700000001000",
                },
            ],
        }


class FakeEventuallyConsistentOKXClient(FakeOKXClient):
    async def get_order(self, *, symbol: str, order_id: str | None = None, client_order_id: str | None = None):
        raise OKXRequestError(
            path="/api/v5/trade/order",
            code="51603",
            msg="Order does not exist",
            status_code=200,
            payload={"code": "51603", "msg": "Order does not exist", "data": []},
        )


class FakeAcceptedOrderLookupFailureOKXClient(FakeOKXClient):
    async def get_order(self, *, symbol: str, order_id: str | None = None, client_order_id: str | None = None):
        raise RuntimeError("order_lookup_failed_after_accept")


class FakeAcceptedFillLookupFailureOKXClient(FakeOKXClient):
    async def get_fills(self, *, symbol: str | None = None, order_id: str | None = None, limit: int | None = None):
        raise RuntimeError("fill_lookup_failed_after_accept")


class FakePartialOrderFillLookupFailureOKXClient(FakeOKXClient):
    async def get_order(self, *, symbol: str, order_id: str | None = None, client_order_id: str | None = None):
        self.order_queries.append({"symbol": symbol, "order_id": order_id, "client_order_id": client_order_id})
        return {
            "code": "0",
            "data": [
                {
                    "instId": symbol,
                    "ordId": order_id or "ord_1",
                    "clOrdId": client_order_id or "clord_1",
                    "state": "partially_filled",
                    "sz": "0.001",
                    "accFillSz": "0.0004",
                    "avgPx": "68000",
                    "cTime": "1700000000000",
                    "uTime": "1700000001000",
                }
            ],
        }

    async def get_fills(self, *, symbol: str | None = None, order_id: str | None = None, limit: int | None = None):
        raise RuntimeError("fill_lookup_failed_after_partial_order_detail")


class FakeCancelOrderLookupFailureOKXClient(FakeOKXClient):
    async def get_order(self, *, symbol: str, order_id: str | None = None, client_order_id: str | None = None):
        raise RuntimeError("order_lookup_failed_after_cancel_accept")


class FakeRoundedDerivativeOKXClient(FakeOKXClient):
    async def get_order(self, *, symbol: str, order_id: str | None = None, client_order_id: str | None = None):
        self.order_queries.append({"symbol": symbol, "order_id": order_id, "client_order_id": client_order_id})
        submitted_size = self.place_order_calls[-1]["sz"]
        return {
            "code": "0",
            "data": [
                {
                    "instId": symbol,
                    "ordId": order_id or "ord_round_1",
                    "clOrdId": client_order_id or self.place_order_calls[-1]["clOrdId"],
                    "state": "filled",
                    "sz": submitted_size,
                    "accFillSz": submitted_size,
                    "avgPx": "68000",
                    "cTime": "1700000000000",
                    "uTime": "1700000001000",
                }
            ],
        }

    async def get_fills(self, *, symbol: str | None = None, order_id: str | None = None, limit: int | None = None):
        self.fill_queries.append({"symbol": symbol, "order_id": order_id, "limit": limit})
        submitted_size = self.place_order_calls[-1]["sz"]
        return {
            "code": "0",
            "data": [
                {
                    "instId": symbol,
                    "ordId": order_id or "ord_round_1",
                    "clOrdId": self.place_order_calls[-1]["clOrdId"],
                    "tradeId": "trade_round_1",
                    "side": self.place_order_calls[-1]["side"],
                    "fillSz": submitted_size,
                    "fillPx": "68000",
                    "fee": "-0.0476",
                    "feeCcy": "USDT",
                    "fillTime": "1700000001000",
                }
            ],
        }


def make_settings(overrides: dict | None = None) -> AATSSettings:
    payload = {
        "mode": "guarded_live",
        "execution_backend": "okx",
        "account_backend": "okx",
        "account_read_enabled": True,
        "okx_simulated_trading": True,
        "live_submit_enabled": False,
        "guarded_execution_dry_run": True,
        "allowed_symbols": ("BTC-USDT",),
    }
    if overrides:
        payload.update(overrides)
    return AATSSettings.model_validate(payload)


def make_intent() -> OrderIntent:
    return OrderIntent(
        intent_id="intent_1",
        decision_id="decision_1",
        symbol="BTC-USDT",
        side="buy",
        quantity=0.001,
        execution_style="taker",
        order_type="market",
        urgency="medium",
        time_in_force="IOC",
        idempotency_key="intent_1",
    )


class TestGuardedSimulatedExecution(unittest.IsolatedAsyncioTestCase):
    async def test_simulated_submit_stays_blocked_by_default(self) -> None:
        settings = make_settings()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())
        client = FakeOKXClient()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=client,  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )

        state, fills = await adapter.submit(make_intent())

        self.assertEqual(state.status, "DRY_RUN")
        self.assertEqual(state.submission_mode, "guarded_simulated_dry_run")
        self.assertEqual(fills, [])
        self.assertEqual(client.place_order_calls, [])

    async def test_submit_is_blocked_when_safety_gate_fails(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
                "max_notional_per_symbol": 10.0,
            }
        )
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())
        client = FakeOKXClient()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=client,  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )

        state, fills = await adapter.submit(make_intent())

        self.assertEqual(state.status, "BLOCKED")
        self.assertEqual(state.execution_error, "max_notional_per_symbol_exceeded")
        self.assertEqual(fills, [])
        self.assertEqual(client.place_order_calls, [])

    async def test_submit_is_blocked_by_local_open_order_obligation_before_exchange_snapshot_catches_up(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
                "max_notional_per_symbol": 1_000.0,
                "max_open_orders": 1,
            }
        )
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())
        client = FakeOKXClient()
        obligation_repo = InMemoryExecutionObligationRepository()
        obligation_repo.save_obligation(
            OrderObligation(
                client_order_id="cl_pending_gate",
                decision_id="decision_pending_gate",
                intent_id="intent_pending_gate",
                symbol="BTC-USDT",
                side="buy",
                reserve_currency="USDT",
                reserved_amount=68.0,
                status="ACTIVE",
            )
        )
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=client,  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
            obligation_repo=obligation_repo,
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )

        state, fills = await adapter.submit(make_intent())

        self.assertEqual(state.status, "BLOCKED")
        self.assertEqual(state.execution_error, "max_open_orders_reached")
        self.assertEqual(fills, [])
        self.assertEqual(client.place_order_calls, [])

    async def test_submit_is_allowed_only_when_all_guards_satisfied(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
                "max_notional_per_symbol": 1_000.0,
            }
        )
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())
        client = FakeOKXClient()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=client,  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )

        state, fills = await adapter.submit(make_intent())

        self.assertEqual(len(client.place_order_calls), 1)
        self.assertEqual(state.status, "FILLED")
        self.assertEqual(state.venue, "OKX")
        self.assertEqual(state.submission_mode, "guarded_simulated_submit")
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].venue, "OKX")
        self.assertEqual(fills[0].fill_id, "trade_1")

    def test_readiness_does_not_recurse_through_health_service(self) -> None:
        settings = make_settings()
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        account_service = FakeAccountService()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=FakeOKXClient(),  # type: ignore[arg-type]
            account_service=account_service,  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=None,
            price_provider=lambda _symbol: 68_000.0,
        )
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeMarketProvider(),
            account_provider=account_service,  # type: ignore[arg-type]
            execution_provider=adapter,
            reconciliation_repo=FakeReconciliationRepo(),  # type: ignore[arg-type]
        )
        adapter.health_service = health_service

        readiness = adapter.readiness()
        snapshot = health_service.snapshot()

        self.assertIn("submit_blocked_reasons", readiness)
        self.assertIsInstance(snapshot.blockers, list)
        self.assertIn("reconciliation_missing", snapshot.blockers)

    def test_order_payload_generation_is_okx_compatible(self) -> None:
        intent = make_intent()
        payload = OKXOrderPayloadBuilder().build(
            intent=intent,
            instrument=InstrumentMetadata(
                instrument_id="BTC-USDT",
                symbol="BTC-USDT",
                base_currency="BTC",
                quote_currency="USDT",
                lot_size=0.0001,
                tick_size=0.1,
                min_size=0.0001,
                state="live",
            ),
        )

        self.assertEqual(payload["instId"], "BTC-USDT")
        self.assertEqual(payload["tdMode"], "cash")
        self.assertEqual(payload["side"], "buy")
        self.assertEqual(payload["ordType"], "market")
        self.assertEqual(payload["tgtCcy"], "base_ccy")
        self.assertTrue(payload["clOrdId"].isalnum())

    def test_derivatives_market_buy_payload_omits_spot_only_target_currency(self) -> None:
        intent = OrderIntent(
            intent_id="intent_derivatives",
            decision_id="decision_derivatives",
            symbol="BTC-USDT-SWAP",
            side="buy",
            quantity=0.03,
            execution_style="taker",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            idempotency_key="intent_derivatives",
            product_type="derivatives",
            target_leverage=2.5,
            margin_mode="cross",
            exposure_side="long",
            position_intent="open_long",
        )
        payload = OKXOrderPayloadBuilder().build(
            intent=intent,
            instrument=InstrumentMetadata(
                instrument_id="BTC-USDT-SWAP",
                symbol="BTC-USDT-SWAP",
                base_currency="BTC",
                quote_currency="USDT",
                lot_size=0.01,
                tick_size=0.1,
                min_size=0.01,
                contract_value=0.01,
                state="live",
            ),
        )

        self.assertEqual(payload["instId"], "BTC-USDT-SWAP")
        self.assertEqual(payload["tdMode"], "cross")
        self.assertEqual(payload["side"], "buy")
        self.assertEqual(payload["ordType"], "market")
        self.assertEqual(payload["sz"], "3")
        self.assertNotIn("tgtCcy", payload)
        self.assertLessEqual(len(payload["clOrdId"]), 32)
        self.assertNotIn("_", payload["clOrdId"])
        self.assertNotEqual(payload["clOrdId"], f"cl{intent.idempotency_key}".replace("_", "")[:32])

    def test_limit_ioc_payload_uses_ioc_order_type_and_price_cap(self) -> None:
        intent = OrderIntent(
            intent_id="intent_limit_ioc",
            decision_id="decision_limit_ioc",
            symbol="BTC-USDT",
            side="buy",
            quantity=Decimal("0.01"),
            execution_style="bounded_limit_ioc",
            order_type="limit",
            limit_price=Decimal("100.1"),
            reference_price=Decimal("100"),
            urgency="medium",
            time_in_force="IOC",
            max_slippage_tolerance_bps=20,
            idempotency_key="intent_limit_ioc",
            product_type="spot",
            exposure_side="long",
            position_intent="open_long",
        )
        payload = OKXOrderPayloadBuilder().build(
            intent=intent,
            instrument=InstrumentMetadata(
                instrument_id="BTC-USDT",
                symbol="BTC-USDT",
                base_currency="BTC",
                quote_currency="USDT",
                lot_size=0.0001,
                tick_size=0.1,
                min_size=0.0001,
                state="live",
            ),
        )

        self.assertEqual(payload["ordType"], "ioc")
        self.assertEqual(payload["px"], "100.1")

    async def test_submit_omits_pos_side_for_net_mode_derivatives_accounts(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "max_notional_per_symbol": 10_000.0,
            }
        )
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())
        account_service = FakeAccountService()
        account_service._snapshot = account_service._snapshot.model_copy(
            update={
                "instruments": [
                    InstrumentMetadata(
                        instrument_id="BTC-USDT-SWAP",
                        symbol="BTC-USDT-SWAP",
                        base_currency="BTC",
                        quote_currency="USDT",
                        lot_size=0.01,
                        tick_size=0.1,
                        min_size=0.01,
                        contract_value=0.01,
                        state="live",
                    )
                ],
                "account_mode": "2",
                "raw": {"account_config": {"data": [{"posMode": "net_mode"}]}},
            }
        )
        account_service.instrument_metadata = lambda symbol: account_service._snapshot.instruments[0] if symbol == "BTC-USDT-SWAP" else None
        client = FakeOKXClient()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=client,  # type: ignore[arg-type]
            account_service=account_service,  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )
        intent = OrderIntent(
            intent_id="intent_derivatives_net_mode",
            decision_id="decision_derivatives_net_mode",
            symbol="BTC-USDT-SWAP",
            side="buy",
            quantity=0.03,
            execution_style="taker",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            idempotency_key="intent_derivatives_net_mode",
            product_type="derivatives",
            target_leverage=2.5,
            margin_mode="cross",
            exposure_side="long",
            position_intent="open_long",
        )

        await adapter.submit(intent)

        self.assertNotIn("posSide", client.place_order_calls[0])

    async def test_submit_blocks_when_okx_max_order_quantity_is_exceeded(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
                "okx_max_order_quantity_precheck_enabled": True,
                "max_notional_per_symbol": 10_000.0,
            }
        )
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())
        client = FakeTightMaxSizeOKXClient()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=client,  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: Decimal("68000"),
        )

        state, fills = await adapter.submit(make_intent())

        self.assertEqual(state.status, "BLOCKED")
        self.assertIn("okx_max_order_quantity_exceeded", state.execution_error or "")
        self.assertEqual(fills, [])
        self.assertEqual(client.place_order_calls, [])

    async def test_submit_marks_fill_complete_when_derivatives_size_is_rounded_down(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "max_notional_per_symbol": 10_000.0,
            }
        )
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())
        account_service = FakeAccountService()
        account_service._snapshot = account_service._snapshot.model_copy(
            update={
                "instruments": [
                    InstrumentMetadata(
                        instrument_id="BTC-USDT-SWAP",
                        symbol="BTC-USDT-SWAP",
                        base_currency="BTC",
                        quote_currency="USDT",
                        lot_size=0.01,
                        tick_size=0.1,
                        min_size=0.01,
                        contract_value=0.01,
                        state="live",
                    )
                ],
                "account_mode": "2",
                "raw": {"account_config": {"data": [{"posMode": "net_mode"}]}},
            }
        )
        account_service.instrument_metadata = (
            lambda symbol: account_service._snapshot.instruments[0] if symbol == "BTC-USDT-SWAP" else None
        )
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=FakeRoundedDerivativeOKXClient(),  # type: ignore[arg-type]
            account_service=account_service,  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )
        intent = OrderIntent(
            intent_id="intent_derivatives_rounded_fill",
            decision_id="decision_derivatives_rounded_fill",
            symbol="BTC-USDT-SWAP",
            side="buy",
            quantity=0.00078,
            execution_style="taker",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            idempotency_key="intent_derivatives_rounded_fill",
            product_type="derivatives",
            target_leverage=2.5,
            margin_mode="cross",
            exposure_side="long",
            position_intent="open_long",
        )

        state, fills = await adapter.submit(intent)

        self.assertEqual(state.status, "FILLED")
        self.assertEqual(state.requested_qty, Decimal("0.0007"))
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].fill_qty, Decimal("0.0007"))
        self.assertEqual(fills[0].order_status_after_fill, "FILLED")

    async def test_submit_filters_out_unrelated_recent_exchange_fills(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
                "max_notional_per_symbol": 1_000.0,
            }
        )
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())
        client = FakeUnfilteredFillsOKXClient()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=client,  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )

        state, fills = await adapter.submit(make_intent())

        self.assertEqual(state.status, "FILLED")
        self.assertEqual([fill.fill_id for fill in fills], ["trade_1"])

    async def test_submit_tolerates_eventual_consistency_when_order_lookup_lags(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
                "max_notional_per_symbol": 1_000.0,
            }
        )
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())
        client = FakeEventuallyConsistentOKXClient()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=client,  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )

        state, fills = await adapter.submit(make_intent())

        self.assertEqual(state.status, "SUBMITTED")
        self.assertEqual(state.venue, "OKX")
        self.assertEqual(fills, [])

    async def test_submit_keeps_trackable_submitted_state_when_order_lookup_fails_after_accept(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
                "max_notional_per_symbol": 1_000.0,
            }
        )
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())
        client = FakeAcceptedOrderLookupFailureOKXClient()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=client,  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )

        state, fills = await adapter.submit(make_intent())

        self.assertEqual(state.status, "SUBMITTED")
        self.assertEqual(state.exchange_order_id, "ord_1")
        self.assertEqual(fills, [])
        self.assertEqual(state.execution_error, "order_lookup_failed_after_accept")

    async def test_submit_keeps_trackable_submitted_state_when_fill_lookup_fails_after_accept(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
                "max_notional_per_symbol": 1_000.0,
            }
        )
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())
        client = FakeAcceptedFillLookupFailureOKXClient()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=client,  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )

        state, fills = await adapter.submit(make_intent())

        self.assertEqual(state.status, "FILLED")
        self.assertEqual(state.exchange_order_id, "ord_1")
        self.assertEqual(fills, [])
        self.assertEqual(state.execution_error, "fill_lookup_failed_after_accept")

    async def test_submit_preserves_mapped_order_state_when_fill_lookup_fails(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
                "max_notional_per_symbol": 1_000.0,
            }
        )
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())
        client = FakePartialOrderFillLookupFailureOKXClient()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=client,  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )

        state, fills = await adapter.submit(make_intent())

        self.assertEqual(state.status, "PARTIALLY_FILLED")
        self.assertEqual(state.exchange_order_id, "ord_1")
        self.assertEqual(state.filled_qty, Decimal("0.0004"))
        self.assertEqual(state.execution_error, "fill_lookup_failed_after_partial_order_detail")
        self.assertEqual(fills, [])

    async def test_sync_maps_exchange_order_state_into_local_state(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
            }
        )
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())
        client = FakeOKXClient()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=client,  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )

        state, _fills = await adapter.submit(make_intent())
        state = state.model_copy(update={"status": "SUBMITTED", "filled_qty": 0.0, "remaining_qty": 0.001})
        refreshed_states, refreshed_fills = await adapter.sync([state])

        self.assertEqual(len(refreshed_states), 1)
        self.assertEqual(refreshed_states[0].status, "FILLED")
        self.assertEqual(len(refreshed_fills), 1)
        self.assertEqual(refreshed_fills[0].fill_id, "trade_1")
        self.assertEqual(refreshed_fills[0].fee_currency, "USDT")

    async def test_sync_prefers_private_ws_order_confirmation_when_available(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
            }
        )
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())

        class _AccountWithPrivateOrders(FakeAccountService):
            def latest_private_order_row(self, *, symbol: str, order_id: str | None = None, client_order_id: str | None = None):
                _ = symbol
                _ = order_id
                _ = client_order_id
                return {
                    "instId": "BTC-USDT",
                    "ordId": "ord_ws_1",
                    "clOrdId": "clord_sync_1",
                    "side": "buy",
                    "ordType": "limit",
                    "state": "filled",
                    "sz": "0.001",
                    "accFillSz": "0.001",
                    "avgPx": "68000",
                    "fee": "-0.068",
                    "uTime": "1700000002000",
                    "cTime": "1700000001000",
                }

            def latest_private_order_fills(self, *, symbol: str, order_id: str | None = None, client_order_id: str | None = None):
                _ = symbol
                _ = order_id
                _ = client_order_id
                return [
                    ExchangeFill(
                        fill_id="trade_ws_sync_1",
                        exchange_order_id="ord_ws_1",
                        client_order_id="clord_sync_1",
                        instrument_id="BTC-USDT",
                        symbol="BTC-USDT",
                        side="buy",
                        fill_qty=Decimal("0.001"),
                        fill_price=Decimal("68000"),
                        fee_amount=Decimal("0.068"),
                        fee_currency="USDT",
                        fill_ts=utc_now(),
                    )
                ]

        client = FakeOKXClient()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=client,  # type: ignore[arg-type]
            account_service=_AccountWithPrivateOrders(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )
        now = utc_now()
        state = OrderState(
            decision_id="decision_1",
            intent_id="intent_1",
            symbol="BTC-USDT",
            client_order_id="clord_sync_1",
            venue="OKX",
            exchange_order_id="ord_ws_1",
            status="SUBMITTED",
            submission_mode="guarded_simulated_submit",
            submitted_ts=now,
            last_update_ts=now,
            last_exchange_update_ts=now,
            requested_qty=Decimal("0.001"),
            filled_qty=Decimal("0"),
            remaining_qty=Decimal("0.001"),
            average_fill_price=None,
            fees=Decimal("0"),
            product_type="spot",
            target_leverage=1.0,
            margin_mode="cash",
            exposure_side="long",
            position_intent="open_long",
            submission_payload={"instId": "BTC-USDT", "clOrdId": "clord_sync_1"},
        )

        refreshed_states, refreshed_fills = await adapter.sync([state])

        self.assertEqual(len(refreshed_states), 1)
        self.assertEqual(refreshed_states[0].status, "FILLED")
        self.assertEqual(len(refreshed_fills), 1)
        self.assertEqual(refreshed_fills[0].fill_id, "trade_ws_sync_1")
        self.assertEqual(client.order_queries, [])
        self.assertEqual(client.fill_queries, [])

    async def test_sync_uses_private_ws_for_submitting_order_without_exchange_id(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
            }
        )
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())

        class _AccountWithPendingPrivateOrder(FakeAccountService):
            def latest_private_order_row(self, *, symbol: str, order_id: str | None = None, client_order_id: str | None = None):
                _ = symbol
                _ = order_id
                _ = client_order_id
                return {
                    "instId": "BTC-USDT",
                    "ordId": "ord_ws_pending_1",
                    "clOrdId": "clord_ws_pending_1",
                    "side": "buy",
                    "ordType": "limit",
                    "state": "live",
                    "sz": "0.001",
                    "accFillSz": "0",
                    "uTime": "1700000002000",
                    "cTime": "1700000001000",
                }

            def latest_private_order_fills(self, *, symbol: str, order_id: str | None = None, client_order_id: str | None = None):
                _ = symbol
                _ = order_id
                _ = client_order_id
                return []

        client = FakeOKXClient()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=client,  # type: ignore[arg-type]
            account_service=_AccountWithPendingPrivateOrder(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )
        now = utc_now()
        state = OrderState(
            decision_id="decision_1",
            intent_id="intent_1",
            symbol="BTC-USDT",
            client_order_id="clord_ws_pending_1",
            venue="OKX",
            exchange_order_id=None,
            status="SUBMITTING",
            submission_mode="guarded_simulated_submit",
            submitted_ts=now,
            last_update_ts=now,
            last_exchange_update_ts=now,
            requested_qty=Decimal("0.001"),
            filled_qty=Decimal("0"),
            remaining_qty=Decimal("0.001"),
            average_fill_price=None,
            fees=Decimal("0"),
            product_type="spot",
            target_leverage=1.0,
            margin_mode="cash",
            exposure_side="long",
            position_intent="open_long",
            submission_payload={"instId": "BTC-USDT", "clOrdId": "clord_ws_pending_1"},
        )

        refreshed_states, refreshed_fills = await adapter.sync([state])

        self.assertEqual(len(refreshed_states), 1)
        self.assertEqual(refreshed_states[0].status, "SUBMITTED")
        self.assertEqual(refreshed_states[0].exchange_order_id, "ord_ws_pending_1")
        self.assertEqual(len(refreshed_fills), 1)
        self.assertEqual(refreshed_fills[0].exchange_order_id, "ord_ws_pending_1")
        self.assertEqual(client.order_queries, [])
        self.assertEqual(client.fill_queries, [{"symbol": "BTC-USDT", "order_id": "ord_ws_pending_1", "limit": 100}])

    async def test_sync_skips_local_submitting_orders_without_exchange_identity(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
            }
        )
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())
        client = FakeOKXClient()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=client,  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )
        now = utc_now()
        state = OrderState(
            decision_id="decision_1",
            intent_id="intent_1",
            symbol="BTC-USDT",
            client_order_id="cl_local_only",
            venue="OKX",
            exchange_order_id=None,
            status="SUBMITTING",
            submission_mode="local_order_manager",
            submitted_ts=None,
            last_update_ts=now,
            last_exchange_update_ts=now,
            requested_qty=0.001,
            filled_qty=0.0,
            remaining_qty=0.001,
            average_fill_price=None,
            fees=0.0,
            product_type="spot",
            target_leverage=1.0,
            margin_mode="cash",
            exposure_side="flat",
            position_intent="open_long",
            submission_payload={},
        )

        refreshed_states, refreshed_fills = await adapter.sync([state])

        self.assertEqual(refreshed_states, [])
        self.assertEqual(refreshed_fills, [])
        self.assertEqual(client.order_queries, [])
        self.assertEqual(client.fill_queries, [])

    async def test_sync_keeps_open_order_trackable_when_exchange_lookup_fails(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
            }
        )
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())
        client = FakeAcceptedOrderLookupFailureOKXClient()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=client,  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )
        now = utc_now()
        state = OrderState(
            decision_id="decision_1",
            intent_id="intent_1",
            symbol="BTC-USDT",
            client_order_id="clord_sync_1",
            venue="OKX",
            exchange_order_id="ord_1",
            status="SUBMITTED",
            submission_mode="guarded_simulated_submit",
            submitted_ts=now,
            last_update_ts=now,
            last_exchange_update_ts=now,
            requested_qty=0.001,
            filled_qty=0.0,
            remaining_qty=0.001,
            average_fill_price=None,
            fees=0.0,
            product_type="spot",
            target_leverage=1.0,
            margin_mode="cash",
            exposure_side="flat",
            position_intent="open_long",
            submission_payload={"instId": "BTC-USDT", "side": "buy", "ordType": "market"},
        )

        refreshed_states, refreshed_fills = await adapter.sync([state])

        self.assertEqual(len(refreshed_states), 1)
        self.assertEqual(refreshed_states[0].status, "SUBMITTED")
        self.assertEqual(refreshed_states[0].exchange_order_id, "ord_1")
        self.assertEqual(refreshed_states[0].execution_error, "order_lookup_failed_after_accept")
        self.assertEqual(refreshed_fills, [])

    async def test_sync_preserves_mapped_order_state_when_fill_lookup_fails(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
            }
        )
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())
        client = FakePartialOrderFillLookupFailureOKXClient()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=client,  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )
        now = utc_now()
        state = OrderState(
            decision_id="decision_1",
            intent_id="intent_1",
            symbol="BTC-USDT",
            client_order_id="clord_sync_partial",
            venue="OKX",
            exchange_order_id="ord_1",
            status="SUBMITTED",
            submission_mode="guarded_simulated_submit",
            submitted_ts=now,
            last_update_ts=now,
            last_exchange_update_ts=now,
            requested_qty=0.001,
            filled_qty=0.0,
            remaining_qty=0.001,
            average_fill_price=None,
            fees=0.0,
            product_type="spot",
            target_leverage=1.0,
            margin_mode="cash",
            exposure_side="flat",
            position_intent="open_long",
            submission_payload={"instId": "BTC-USDT", "side": "buy", "ordType": "market"},
        )

        refreshed_states, refreshed_fills = await adapter.sync([state])

        self.assertEqual(len(refreshed_states), 1)
        self.assertEqual(refreshed_states[0].status, "PARTIALLY_FILLED")
        self.assertEqual(refreshed_states[0].filled_qty, Decimal("0.0004"))
        self.assertEqual(refreshed_states[0].execution_error, "fill_lookup_failed_after_partial_order_detail")
        self.assertEqual(refreshed_states[0].last_exchange_update_ts, datetime_from_ms("1700000001000"))
        self.assertEqual(refreshed_fills, [])

    async def test_partial_fill_ingestion_updates_portfolio_incrementally(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
            }
        )
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=FakePartialFillOKXClient(),  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )
        portfolio = PortfolioState(initial_usdt_balance=10_000.0)

        state, fills = await adapter.submit(make_intent())
        self.assertEqual(state.status, "PARTIALLY_FILLED")
        self.assertEqual(len(fills), 1)
        first = portfolio.apply_fill(fills[0])
        self.assertTrue(first.applied)
        self.assertEqual(portfolio.positions["BTC-USDT"].quantity, Decimal("0.0004"))

        refreshed_states, refreshed_fills = await adapter.sync([state])
        self.assertEqual(refreshed_states[0].status, "FILLED")
        applied_count = 0
        for fill in refreshed_fills:
            if portfolio.apply_fill(fill).applied:
                applied_count += 1
        self.assertEqual(applied_count, 1)
        self.assertEqual(portfolio.positions["BTC-USDT"].quantity, Decimal("0.001"))

    async def test_cancel_returns_canceled_state(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
            }
        )
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())
        client = FakePartialFillOKXClient()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=client,  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )

        state, _fills = await adapter.submit(make_intent())
        state = state.model_copy(update={"status": "PARTIALLY_FILLED"})
        canceled, fills = await adapter.cancel(state)

        self.assertIn(canceled.status, {"PARTIALLY_FILLED", "CANCELED", "FILLED"})
        self.assertIsNotNone(canceled.cancellation_requested_ts)
        self.assertIsInstance(fills, list)

    async def test_cancel_keeps_cancel_pending_state_when_order_lookup_fails_after_ack(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
            }
        )
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=FakeCancelOrderLookupFailureOKXClient(),  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )

        state, _fills = await adapter.submit(make_intent())
        canceled, fills = await adapter.cancel(state)

        self.assertEqual(canceled.status, "CANCEL_PENDING")
        self.assertEqual(canceled.execution_error, "order_lookup_failed_after_cancel_accept")
        self.assertIsNotNone(canceled.cancellation_requested_ts)
        self.assertEqual(fills, [])

    async def test_cancel_preserves_mapped_order_state_when_fill_lookup_fails(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
            }
        )
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=FakePartialOrderFillLookupFailureOKXClient(),  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )

        state, _fills = await adapter.submit(make_intent())
        canceled, fills = await adapter.cancel(state)

        self.assertEqual(canceled.status, "PARTIALLY_FILLED")
        self.assertEqual(canceled.execution_error, "fill_lookup_failed_after_partial_order_detail")
        self.assertIsNotNone(canceled.cancellation_requested_ts)
        self.assertEqual(fills, [])

    async def test_submit_is_blocked_when_health_freshness_is_unhealthy(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
            }
        )
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())
        client = FakeOKXClient()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=client,  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=FakeHealthService(["market_data_stale"]),
            price_provider=lambda _symbol: 68_000.0,
        )

        state, fills = await adapter.submit(make_intent())

        self.assertEqual(state.status, "BLOCKED")
        self.assertEqual(state.execution_error, "market_data_stale")
        self.assertEqual(fills, [])
        self.assertEqual(client.place_order_calls, [])
        readiness = adapter.readiness()
        self.assertFalse(readiness["exchange_submit_allowed"])
        self.assertIn("market_data_stale", readiness["submit_blocked_reasons"])

    async def test_submit_failure_surfaces_okx_row_error_details(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
                "max_notional_per_symbol": 1_000.0,
            }
        )
        mode_controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=FakeRejectedOKXClient(),  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )

        state, fills = await adapter.submit(make_intent())

        self.assertEqual(state.status, "FAILED")
        self.assertEqual(fills, [])
        self.assertIn("code=1", state.execution_error)
        self.assertIn("sCode=51008", state.execution_error)
        self.assertIn("sMsg=Order amount too low", state.execution_error)

    async def test_submit_is_blocked_when_reconciliation_requires_rebaseline(self) -> None:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
                "max_notional_per_symbol": 1_000.0,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        account_service = FakeAccountService()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=FakeOKXClient(),  # type: ignore[arg-type]
            account_service=account_service,  # type: ignore[arg-type]
            mode_controller=mode_controller,
            health_service=None,
            price_provider=lambda _symbol: 68_000.0,
        )
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeMarketProvider(),
            account_provider=account_service,  # type: ignore[arg-type]
            execution_provider=adapter,
            reconciliation_repo=ReviewRequiredReconciliationRepo(),  # type: ignore[arg-type]
        )
        adapter.health_service = health_service

        state, fills = await adapter.submit(make_intent())

        self.assertEqual(state.status, "BLOCKED")
        self.assertEqual(state.execution_error, "operator_rebaseline_required")
        self.assertEqual(fills, [])
        self.assertFalse(adapter.readiness()["exchange_submit_allowed"])
        self.assertIn("operator_rebaseline_required", adapter.readiness()["submit_blocked_reasons"])

    def test_select_exchange_fills_does_not_fallback_to_unrelated_recent_history(self) -> None:
        fills = [
            ExchangeFill(
                fill_id="trade_other",
                exchange_order_id="ord_other",
                client_order_id="cl_other",
                instrument_id="BTC-USDT",
                symbol="BTC-USDT",
                side="buy",
                fill_qty=0.001,
                fill_price=100.0,
                fee_amount=0.1,
                fill_ts=utc_now(),
            )
        ]

        selected = OKXExecutionAdapter._select_exchange_fills(
            exchange_fills=fills,
            order_id="ord_expected",
            client_order_id="cl_expected",
        )

        self.assertEqual(selected, [])


if __name__ == "__main__":
    unittest.main()
