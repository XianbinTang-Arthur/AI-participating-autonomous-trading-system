from __future__ import annotations

import unittest

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent, OrderIntent
from aats.schemas.exchange import ExchangeAccountSnapshot, InstrumentMetadata
from aats.services.execution_engine.okx_adapter import OKXExecutionAdapter, OKXOrderPayloadBuilder
from aats.services.portfolio_service.positions import PortfolioState
from aats.services.governance_engine.health import SystemHealthService
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.governance_engine.mode import RuntimeModeController


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
        payload = OKXOrderPayloadBuilder().build(
            intent=make_intent(),
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
        self.assertAlmostEqual(portfolio.positions["BTC-USDT"].quantity, 0.0004)

        refreshed_states, refreshed_fills = await adapter.sync([state])
        self.assertEqual(refreshed_states[0].status, "FILLED")
        applied_count = 0
        for fill in refreshed_fills:
            if portfolio.apply_fill(fill).applied:
                applied_count += 1
        self.assertEqual(applied_count, 1)
        self.assertAlmostEqual(portfolio.positions["BTC-USDT"].quantity, 0.001)

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


if __name__ == "__main__":
    unittest.main()
