from __future__ import annotations

import json
import unittest
from decimal import Decimal
from urllib.parse import parse_qs

import httpx

from aats.bootstrap.settings import AATSSettings
from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.exchange import InstrumentMetadata
from aats.schemas.execution import LegOrderIntent, OrderIntent
from aats.schemas.governance import RiskDecision
from aats.services.execution_engine.okx_account import OKXAccountService
from aats.services.execution_engine.okx_adapter import OKXExecutionAdapter
from aats.services.execution_engine.okx_rest import OKXRESTClient
from aats.services.execution_engine.order_manager import OrderManager
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.governance_engine.mode import RuntimeModeController
from aats.storage.event_store import InMemoryEventStore
from aats.storage.execution_repo import InMemoryExecutionRepository


class _ReadyAccountService:
    async def refresh(self, *, force: bool = False):
        _ = force
        raise AssertionError("account_refresh_should_not_run_when_leg_risk_blocks")

    def status(self):
        return {
            "backend": "okx",
            "enabled": True,
            "credentials_configured": True,
            "connected": True,
            "fresh": True,
            "last_update_ts": None,
            "last_error": None,
            "ready": True,
            "detail": "stub_ready_account",
            "blockers": [],
        }

    def open_order_count(self, symbol: str | None = None) -> int:
        _ = symbol
        return 0

    def instrument_metadata(self, symbol: str):
        return InstrumentMetadata(
            instrument_id=symbol,
            symbol=symbol,
            base_currency="BTC",
            quote_currency="USDT",
            lot_size=Decimal("0.01"),
            tick_size=Decimal("0.1"),
            min_size=Decimal("0.01"),
            settle_currency="USDT",
            state="live",
        )


class TestOKXLiveSubmitPath(unittest.IsolatedAsyncioTestCase):
    async def test_live_futures_submit_path_preserves_futures_inst_type_and_omits_demo_header(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "market_data_backend": "okx",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
                "okx_simulated_trading": False,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-240329",
                "allowed_symbols": ("BTC-USDT-240329",),
                "max_notional_per_symbol": 10_000.0,
                "max_open_orders": 2,
                "max_target_leverage": 5.0,
                "okx_api_key": "live_key",
                "okx_api_secret": "live_secret",
                "okx_api_passphrase": "live_passphrase",
            }
        )
        captured_requests: list[dict[str, object]] = []

        def response_payload(request: httpx.Request) -> httpx.Response:
            query = {key: values[-1] for key, values in parse_qs(request.url.query.decode("utf-8")).items()}
            request_body = request.content.decode("utf-8") if request.content else ""
            json_body = json.loads(request_body) if request_body else None
            captured_requests.append(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "query": query,
                    "headers": {key.lower(): value for key, value in request.headers.items()},
                    "json": json_body,
                }
            )

            if request.url.path == "/api/v5/account/balance":
                return httpx.Response(
                    200,
                    json={
                        "code": "0",
                        "data": [{"details": [{"ccy": "USDT", "eq": "100", "cashBal": "100", "availEq": "100", "availBal": "100"}]}],
                    },
                )
            if request.url.path == "/api/v5/account/instruments":
                inst_type = query.get("instType")
                if inst_type == "FUTURES":
                    return httpx.Response(
                        200,
                        json={
                            "code": "0",
                            "data": [
                                {
                                    "instId": "BTC-USDT-240329",
                                    "instType": "FUTURES",
                                    "instFamily": "BTC-USDT",
                                    "uly": "BTC-USDT",
                                    "settleCcy": "USDT",
                                    "ctValCcy": "BTC",
                                    "ctVal": "0.01",
                                    "lotSz": "1",
                                    "tickSz": "0.1",
                                    "minSz": "1",
                                    "lever": "20",
                                    "maxMktSz": "100",
                                    "maxLmtSz": "100",
                                    "state": "live",
                                }
                            ],
                        },
                    )
                return httpx.Response(200, json={"code": "0", "data": []})
            if request.url.path == "/api/v5/trade/orders-pending":
                return httpx.Response(200, json={"code": "0", "data": []})
            if request.url.path == "/api/v5/trade/fills":
                if query.get("ordId") == "ord_live_future_1":
                    return httpx.Response(
                        200,
                        json={
                            "code": "0",
                            "data": [
                                {
                                    "instId": "BTC-USDT-240329",
                                    "ordId": "ord_live_future_1",
                                    "clOrdId": json_body["clOrdId"] if isinstance(json_body, dict) and "clOrdId" in json_body else "unknown",
                                    "tradeId": "trade_live_future_1",
                                    "side": "buy",
                                    "fillSz": "2",
                                    "fillPx": "80000",
                                    "fee": "-0.12",
                                    "feeCcy": "USDT",
                                    "fillTime": "1700000001000",
                                }
                            ],
                        },
                    )
                return httpx.Response(200, json={"code": "0", "data": []})
            if request.url.path == "/api/v5/account/positions":
                if query.get("instType") == "FUTURES":
                    return httpx.Response(
                        200,
                        json={
                            "code": "0",
                            "data": [
                                {
                                    "instId": "BTC-USDT-240329",
                                    "pos": "0",
                                    "avgPx": "0",
                                    "markPx": "80000",
                                    "notionalUsd": "0",
                                    "posSide": "net",
                                    "mgnMode": "cross",
                                    "ccy": "USDT",
                                    "lever": "3",
                                }
                            ],
                        },
                    )
                return httpx.Response(200, json={"code": "0", "data": []})
            if request.url.path == "/api/v5/account/config":
                return httpx.Response(
                    200,
                    json={"code": "0", "data": [{"acctLv": "2", "posMode": "net_mode", "autoLoan": False}]},
                )
            if request.url.path == "/api/v5/account/trade-fee":
                return httpx.Response(200, json={"code": "0", "data": [{"maker": "-0.0008", "taker": "0.001"}]})
            if request.url.path == "/api/v5/account/account-position-risk":
                return httpx.Response(
                    200,
                    json={"code": "0", "data": [{"adjEq": "100", "availEq": "100", "imr": "5", "mmr": "2", "mgnRatio": "50"}]},
                )
            if request.url.path == "/api/v5/system/status":
                return httpx.Response(200, json={"code": "0", "data": []})
            if request.url.path == "/api/v5/account/max-size":
                return httpx.Response(200, json={"code": "0", "data": [{"maxBuy": "10", "maxSell": "10"}]})
            if request.url.path == "/api/v5/trade/order" and request.method == "POST":
                assert isinstance(json_body, dict)
                return httpx.Response(
                    200,
                    json={"code": "0", "data": [{"sCode": "0", "sMsg": "", "ordId": "ord_live_future_1", "clOrdId": json_body["clOrdId"]}]},
                )
            if request.url.path == "/api/v5/trade/order" and request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "code": "0",
                        "data": [
                            {
                                "instId": "BTC-USDT-240329",
                                "ordId": "ord_live_future_1",
                                "clOrdId": query.get("clOrdId", "unknown"),
                                "state": "filled",
                                "sz": "2",
                                "accFillSz": "2",
                                "avgPx": "80000",
                                "cTime": "1700000000000",
                                "uTime": "1700000001000",
                            }
                        ],
                    },
                )
            raise AssertionError(f"unexpected_okx_request:{request.method}:{request.url}")

        kill_switch = KillSwitch()
        controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        rest_client = OKXRESTClient(settings=settings)
        rest_client._client = httpx.AsyncClient(
            base_url=settings.okx_rest_url,
            transport=httpx.MockTransport(response_payload),
        )
        account_service = OKXAccountService(settings=settings, client=rest_client)
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=rest_client,
            account_service=account_service,
            mode_controller=controller,
            price_provider=lambda _symbol: Decimal("80000"),
        )
        execution_repo = InMemoryExecutionRepository()
        bus = InMemoryEventBus(event_store=InMemoryEventStore())
        manager = OrderManager(
            settings=settings,
            bus=bus,
            adapter=adapter,
            execution_repo=execution_repo,
            kill_switch=kill_switch,
        )

        intent = OrderIntent(
            intent_id="intent_live_future_1",
            decision_id="decision_live_future_1",
            symbol="BTC-USDT-240329",
            side="buy",
            quantity=Decimal("0.02"),
            execution_style="taker",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            idempotency_key="intent_live_future_1",
            product_type="derivatives",
            margin_mode="cross",
            target_leverage=3.0,
            exposure_side="long",
            position_intent="open_long",
        )

        try:
            await manager.handle_order_intent(
                {
                    "topic": topics.ORDER_INTENTS,
                    "key": intent.intent_id,
                    "payload": build_envelope(
                        topic=topics.ORDER_INTENTS,
                        key=intent.intent_id,
                        payload_model=intent,
                        source_component="test",
                    ).model_dump(mode="json"),
                }
            )
        finally:
            await rest_client.aclose()

        stored_states = execution_repo.order_states()
        stored_fills = execution_repo.fills()
        self.assertEqual(len(stored_states), 1)
        self.assertEqual(len(stored_fills), 1)
        self.assertEqual(stored_states[0].status, "FILLED")
        self.assertEqual(stored_states[0].submission_mode, "guarded_live_submit")
        self.assertEqual(stored_states[0].symbol, "BTC-USDT-240329")
        self.assertEqual(stored_states[0].requested_qty, Decimal("0.02"))
        self.assertEqual(stored_fills[0].fill_qty, Decimal("0.02"))
        self.assertEqual(stored_fills[0].symbol, "BTC-USDT-240329")

        place_order_requests = [
            request
            for request in captured_requests
            if request["path"] == "/api/v5/trade/order" and request["method"] == "POST"
        ]
        self.assertEqual(len(place_order_requests), 1)
        self.assertEqual(place_order_requests[0]["json"]["sz"], "2")
        self.assertNotIn("x-simulated-trading", place_order_requests[0]["headers"])

        queried_inst_types = {
            (request["path"], request["query"].get("instType"))
            for request in captured_requests
            if request["path"] in {"/api/v5/account/instruments", "/api/v5/account/positions"}
        }
        self.assertIn(("/api/v5/account/instruments", "SWAP"), queried_inst_types)
        self.assertIn(("/api/v5/account/instruments", "FUTURES"), queried_inst_types)
        self.assertIn(("/api/v5/account/positions", "SWAP"), queried_inst_types)
        self.assertIn(("/api/v5/account/positions", "FUTURES"), queried_inst_types)

    async def test_live_futures_leg_order_path_preserves_explicit_pos_side_and_close_semantics(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "market_data_backend": "okx",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
                "okx_simulated_trading": False,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "derivatives_position_mode": "hedge",
                "default_symbol": "BTC-USDT-240329",
                "allowed_symbols": ("BTC-USDT-240329",),
                "max_notional_per_symbol": 10_000.0,
                "max_open_orders": 2,
                "max_target_leverage": 5.0,
                "okx_api_key": "live_key",
                "okx_api_secret": "live_secret",
                "okx_api_passphrase": "live_passphrase",
            }
        )
        captured_requests: list[dict[str, object]] = []

        def response_payload(request: httpx.Request) -> httpx.Response:
            query = {key: values[-1] for key, values in parse_qs(request.url.query.decode("utf-8")).items()}
            request_body = request.content.decode("utf-8") if request.content else ""
            json_body = json.loads(request_body) if request_body else None
            captured_requests.append(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "query": query,
                    "headers": {key.lower(): value for key, value in request.headers.items()},
                    "json": json_body,
                }
            )

            if request.url.path == "/api/v5/account/balance":
                return httpx.Response(
                    200,
                    json={
                        "code": "0",
                        "data": [{"details": [{"ccy": "USDT", "eq": "100", "cashBal": "100", "availEq": "100", "availBal": "100"}]}],
                    },
                )
            if request.url.path == "/api/v5/account/instruments":
                inst_type = query.get("instType")
                if inst_type == "FUTURES":
                    return httpx.Response(
                        200,
                        json={
                            "code": "0",
                            "data": [
                                {
                                    "instId": "BTC-USDT-240329",
                                    "instType": "FUTURES",
                                    "instFamily": "BTC-USDT",
                                    "uly": "BTC-USDT",
                                    "settleCcy": "USDT",
                                    "ctValCcy": "BTC",
                                    "ctVal": "0.01",
                                    "lotSz": "1",
                                    "tickSz": "0.1",
                                    "minSz": "1",
                                    "lever": "20",
                                    "maxMktSz": "100",
                                    "maxLmtSz": "100",
                                    "state": "live",
                                }
                            ],
                        },
                    )
                return httpx.Response(200, json={"code": "0", "data": []})
            if request.url.path == "/api/v5/trade/orders-pending":
                return httpx.Response(200, json={"code": "0", "data": []})
            if request.url.path == "/api/v5/trade/fills":
                if query.get("ordId") == "ord_live_future_leg_1":
                    return httpx.Response(
                        200,
                        json={
                            "code": "0",
                            "data": [
                                {
                                    "instId": "BTC-USDT-240329",
                                    "ordId": "ord_live_future_leg_1",
                                    "clOrdId": query.get("clOrdId", "unknown"),
                                    "tradeId": "trade_live_future_leg_1",
                                    "side": "sell",
                                    "fillSz": "2",
                                    "fillPx": "80000",
                                    "fee": "-0.12",
                                    "feeCcy": "USDT",
                                    "fillTime": "1700000001000",
                                }
                            ],
                        },
                    )
                return httpx.Response(200, json={"code": "0", "data": []})
            if request.url.path == "/api/v5/account/positions":
                if query.get("instType") == "FUTURES":
                    return httpx.Response(
                        200,
                        json={
                            "code": "0",
                            "data": [
                                {
                                    "instId": "BTC-USDT-240329",
                                    "pos": "2",
                                    "avgPx": "80000",
                                    "markPx": "80000",
                                    "notionalUsd": "1600",
                                    "posSide": "long",
                                    "mgnMode": "cross",
                                    "ccy": "USDT",
                                    "lever": "3",
                                }
                            ],
                        },
                    )
                return httpx.Response(200, json={"code": "0", "data": []})
            if request.url.path == "/api/v5/account/config":
                return httpx.Response(
                    200,
                    json={"code": "0", "data": [{"acctLv": "2", "posMode": "long_short_mode", "autoLoan": False}]},
                )
            if request.url.path == "/api/v5/account/trade-fee":
                return httpx.Response(200, json={"code": "0", "data": [{"maker": "-0.0008", "taker": "0.001"}]})
            if request.url.path == "/api/v5/account/account-position-risk":
                return httpx.Response(
                    200,
                    json={"code": "0", "data": [{"adjEq": "100", "availEq": "100", "imr": "5", "mmr": "2", "mgnRatio": "50"}]},
                )
            if request.url.path == "/api/v5/system/status":
                return httpx.Response(200, json={"code": "0", "data": []})
            if request.url.path == "/api/v5/account/max-size":
                return httpx.Response(200, json={"code": "0", "data": [{"maxBuy": "10", "maxSell": "10"}]})
            if request.url.path == "/api/v5/trade/order" and request.method == "POST":
                assert isinstance(json_body, dict)
                return httpx.Response(
                    200,
                    json={"code": "0", "data": [{"sCode": "0", "sMsg": "", "ordId": "ord_live_future_leg_1", "clOrdId": json_body["clOrdId"]}]},
                )
            if request.url.path == "/api/v5/trade/order" and request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "code": "0",
                        "data": [
                            {
                                "instId": "BTC-USDT-240329",
                                "ordId": "ord_live_future_leg_1",
                                "clOrdId": query.get("clOrdId", "unknown"),
                                "state": "filled",
                                "posSide": "long",
                                "sz": "2",
                                "accFillSz": "2",
                                "avgPx": "80000",
                                "cTime": "1700000000000",
                                "uTime": "1700000001000",
                            }
                        ],
                    },
                )
            raise AssertionError(f"unexpected_okx_request:{request.method}:{request.url}")

        kill_switch = KillSwitch()
        controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        rest_client = OKXRESTClient(settings=settings)
        rest_client._client = httpx.AsyncClient(
            base_url=settings.okx_rest_url,
            transport=httpx.MockTransport(response_payload),
        )
        account_service = OKXAccountService(settings=settings, client=rest_client)
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=rest_client,
            account_service=account_service,
            mode_controller=controller,
            price_provider=lambda _symbol: Decimal("80000"),
        )
        execution_repo = InMemoryExecutionRepository()
        bus = InMemoryEventBus(event_store=InMemoryEventStore())
        manager = OrderManager(
            settings=settings,
            bus=bus,
            adapter=adapter,
            execution_repo=execution_repo,
            kill_switch=kill_switch,
        )

        leg_intent = LegOrderIntent(
            leg_intent_id="leg_live_future_1",
            decision_id="decision_leg_live_future_1",
            symbol="BTC-USDT-240329",
            side="sell",
            pos_side="long",
            action="close",
            quantity=Decimal("0.02"),
            execution_style="taker",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            idempotency_key="leg_live_future_1",
            product_type="derivatives",
            margin_mode="cross",
            td_mode="cross",
            target_leverage=3.0,
            exposure_side="flat",
        )

        try:
            await manager.submit_leg_order(leg_intent=leg_intent)
        finally:
            await rest_client.aclose()

        stored_states = execution_repo.order_states()
        stored_fills = execution_repo.fills()
        self.assertEqual(len(stored_states), 1)
        self.assertEqual(len(stored_fills), 1)
        self.assertEqual(stored_states[0].status, "FILLED")
        self.assertEqual(stored_states[0].position_mode, "long_short_mode")
        self.assertEqual(stored_states[0].pos_side, "long")
        self.assertEqual(stored_states[0].leg_action, "close")
        self.assertEqual(stored_fills[0].leg_action, "close")

        place_order_requests = [
            request
            for request in captured_requests
            if request["path"] == "/api/v5/trade/order" and request["method"] == "POST"
        ]
        self.assertEqual(len(place_order_requests), 1)
        assert isinstance(place_order_requests[0]["json"], dict)
        self.assertEqual(place_order_requests[0]["json"]["side"], "sell")
        self.assertEqual(place_order_requests[0]["json"]["posSide"], "long")
        self.assertEqual(place_order_requests[0]["json"]["reduceOnly"], "true")

    async def test_live_futures_leg_order_risk_block_prevents_exchange_submission(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "market_data_backend": "okx",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
                "okx_simulated_trading": False,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "derivatives_position_mode": "hedge",
                "default_symbol": "BTC-USDT-240329",
                "allowed_symbols": ("BTC-USDT-240329",),
                "okx_api_key": "live_key",
                "okx_api_secret": "live_secret",
                "okx_api_passphrase": "live_passphrase",
            }
        )
        kill_switch = KillSwitch()
        controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=object(),  # type: ignore[arg-type]
            account_service=_ReadyAccountService(),  # type: ignore[arg-type]
            mode_controller=controller,
            price_provider=lambda _symbol: Decimal("80000"),
        )
        execution_repo = InMemoryExecutionRepository()
        bus = InMemoryEventBus(event_store=InMemoryEventStore())
        manager = OrderManager(
            settings=settings,
            bus=bus,
            adapter=adapter,
            execution_repo=execution_repo,
            leg_risk_evaluator=lambda _leg_intent: RiskDecision(
                decision_id="leg_risk_blocked_live_path",
                approved=False,
                modified=True,
                capped_target_position_qty=Decimal("0"),
                capped_target_notional=Decimal("0"),
                projected_notional=Decimal("0"),
                risk_score=1.0,
                only_reduce_required=True,
                risk_limit_breached=True,
                rejection_reasons=["risk_max_long_notional_exceeded", "leg_only_reduce_mode_active"],
            ),
            kill_switch=kill_switch,
        )

        await manager.submit_leg_order(
            leg_intent=LegOrderIntent(
                leg_intent_id="leg_live_future_blocked",
                decision_id="decision_leg_live_future_blocked",
                symbol="BTC-USDT-240329",
                side="buy",
                pos_side="long",
                action="open",
                quantity=Decimal("0.02"),
                execution_style="taker",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                idempotency_key="leg_live_future_blocked",
                product_type="derivatives",
                margin_mode="cross",
                td_mode="cross",
                target_leverage=3.0,
                exposure_side="long",
            )
        )

        stored_states = execution_repo.order_states()
        self.assertEqual(len(stored_states), 1)
        self.assertEqual(stored_states[0].status, "BLOCKED")
        self.assertEqual(stored_states[0].submission_mode, "leg_risk_blocked")
        self.assertIn("leg_risk_blocked", stored_states[0].execution_error)


if __name__ == "__main__":
    unittest.main()
