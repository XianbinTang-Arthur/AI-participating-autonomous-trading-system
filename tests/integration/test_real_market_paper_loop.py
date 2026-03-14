from __future__ import annotations

import unittest

from aats.bootstrap.config import _build_execution_adapter, build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.schemas.common import new_id, utc_now
from aats.schemas.decision import PositionTarget
from aats.schemas.execution import OrderIntent
from aats.schemas.exchange import ExchangeAccountSnapshot, InstrumentMetadata
from aats.schemas.reconciliation import ReconciliationReport
from aats.services.decision_engine.trigger_policy import DecisionTriggerPolicy
from aats.services.execution_engine.okx_adapter import OKXExecutionAdapter
from aats.services.execution_engine.paper_adapter import PaperExecutionAdapter
from aats.services.governance_engine.health import SystemHealthService
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.governance_engine.mode import RuntimeModeController
from aats.services.governance_engine.policy import PolicyEngine
from aats.services.governance_engine.risk import RiskEngine
from aats.services.market_gateway.gateway import MarketDataGateway
from aats.services.market_gateway.normalizer import MarketSnapshotNormalizer
from aats.services.market_gateway.publisher import MarketSnapshotPublisher


class FakeAccountService:
    def __init__(self, *, open_order_count: int = 0) -> None:
        self._open_order_count = open_order_count
        self._snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[],
            positions=[],
            open_orders=[],
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

    async def refresh(self, *, force: bool = False) -> ExchangeAccountSnapshot:
        return self._snapshot

    def instrument_metadata(self, symbol: str) -> InstrumentMetadata | None:
        return self._snapshot.instruments[0] if symbol == "BTC-USDT" else None

    def open_order_count(self, symbol: str | None = None) -> int:
        return self._open_order_count

    def latest_snapshot(self) -> ExchangeAccountSnapshot:
        return self._snapshot

    def status(self) -> dict[str, object]:
        return {
            "backend": "okx",
            "enabled": True,
            "credentials_configured": True,
            "connected": True,
            "fresh": True,
            "last_update_ts": self._snapshot.fetched_at,
            "last_error": None,
            "ready": True,
            "detail": "test_account",
            "blockers": [],
        }


class FakeMarketProvider:
    def __init__(self, *, ready: bool) -> None:
        self.ready = ready

    def status(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "connected": True,
            "fresh": self.ready,
            "last_update_ts": utc_now() if self.ready else None,
            "detail": "market_ok" if self.ready else "market_stale",
            "blockers": [] if self.ready else ["market_data_stale"],
        }


class FakeExecutionProvider:
    def readiness(self) -> dict[str, object]:
        return {"ready": True, "connected": True, "fresh": True, "blockers": [], "detail": "ok"}


class FakeReconciliationRepo:
    def __init__(self, *, healthy: bool) -> None:
        self._healthy = healthy

    def latest(self) -> ReconciliationReport | None:
        if not self._healthy:
            return None
        return ReconciliationReport(
            reconciliation_id=new_id("recon"),
            decision_id=None,
            portfolio_snapshot_ref=None,
            as_of_ts=utc_now(),
            order_diff={},
            fill_diff={},
            balance_diff={},
            position_diff={"stored": {}, "reconstructed": {}, "mismatches": {}},
            severity="CLEAN",
            remediation_action=None,
            halt_required=False,
        )


class FakeOKXClient:
    def __init__(self) -> None:
        self.place_order_calls: list[dict[str, str]] = []

    async def place_order(self, payload: dict[str, str]) -> dict[str, object]:
        self.place_order_calls.append(dict(payload))
        return {"code": "0", "data": [{"ordId": "1"}]}


def make_target(symbol: str = "BTC-USDT") -> PositionTarget:
    return PositionTarget(
        decision_id="decision_test",
        symbol=symbol,
        current_position_qty=0.0,
        target_position_qty=0.01,
        delta_position_qty=0.01,
        current_notional=0.0,
        target_notional=670.0,
        rebalance_reason="test",
        urgency="medium",
        max_slippage_tolerance_bps=20,
        source_mix={"baseline": 1.0},
        decision_expiry_ts=utc_now(),
    )


def make_health_service(
    *,
    settings: AATSSettings,
    mode_controller: RuntimeModeController,
    kill_switch: KillSwitch,
    market_ready: bool,
    reconciliation_healthy: bool,
) -> SystemHealthService:
    return SystemHealthService(
        settings=settings,
        mode_controller=mode_controller,
        kill_switch=kill_switch,
        market_provider=FakeMarketProvider(ready=market_ready),  # type: ignore[arg-type]
        account_provider=FakeAccountService(),  # type: ignore[arg-type]
        execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
        reconciliation_repo=FakeReconciliationRepo(healthy=reconciliation_healthy),  # type: ignore[arg-type]
    )


class TestModeAwareExecution(unittest.IsolatedAsyncioTestCase):
    async def test_paper_live_okx_backend_routes_to_paper_adapter(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "paper_live",
                "market_data_backend": "okx",
                "execution_backend": "okx",
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        from aats.bus.memory_bus import InMemoryEventBus
        from aats.storage.event_store import InMemoryEventStore

        bus = InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict")
        gateway = MarketDataGateway(
            settings=settings,
            normalizer=MarketSnapshotNormalizer(exchange_name="OKX"),
            publisher=MarketSnapshotPublisher(bus=bus),
        )
        adapter = _build_execution_adapter(
            settings=settings,
            market_gateway=gateway,
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
        )

        self.assertIsInstance(adapter, PaperExecutionAdapter)
        self.assertEqual(mode_controller.operating_state(), "real_market_paper")

    async def test_guarded_live_okx_backend_routes_to_okx_adapter(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "market_data_backend": "okx",
                "execution_backend": "okx",
                "okx_simulated_trading": False,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        from aats.bus.memory_bus import InMemoryEventBus
        from aats.storage.event_store import InMemoryEventStore

        bus = InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict")
        gateway = MarketDataGateway(
            settings=settings,
            normalizer=MarketSnapshotNormalizer(exchange_name="OKX"),
            publisher=MarketSnapshotPublisher(bus=bus),
        )
        adapter = _build_execution_adapter(
            settings=settings,
            market_gateway=gateway,
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
        )

        self.assertIsInstance(adapter, OKXExecutionAdapter)
        self.assertEqual(mode_controller.operating_state(), "guarded_live_blocked")


class TestPolicyAndRiskGating(unittest.TestCase):
    def test_paper_live_ignores_guarded_live_health_blockers(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "paper_live",
                "market_data_backend": "okx",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        health_service = make_health_service(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_ready=False,
            reconciliation_healthy=False,
        )
        policy = PolicyEngine(
            settings=settings,
            kill_switch=kill_switch,
            mode_controller=mode_controller,
            health_service=health_service,
        )
        risk = RiskEngine(
            settings=settings,
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda _symbol: 67_000.0,
            mode_controller=mode_controller,
        )

        policy_decision = policy.evaluate(target=make_target())
        risk_decision = risk.evaluate(target=make_target())

        self.assertTrue(policy_decision.execution_allowed)
        self.assertFalse(policy_decision.dry_run_only)
        self.assertFalse(policy_decision.requires_human_approval)
        self.assertTrue(risk_decision.approved)
        self.assertNotIn("market_data_stale", policy_decision.rejection_reasons)
        self.assertNotIn("reconciliation_missing", policy_decision.rejection_reasons)
        self.assertNotIn("market_data_stale", risk_decision.rejection_reasons)

    def test_guarded_live_remains_blocked_when_health_is_unsafe(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "market_data_backend": "okx",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": False,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        health_service = make_health_service(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_ready=False,
            reconciliation_healthy=False,
        )
        policy = PolicyEngine(
            settings=settings,
            kill_switch=kill_switch,
            mode_controller=mode_controller,
            health_service=health_service,
        )
        risk = RiskEngine(
            settings=settings,
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda _symbol: 67_000.0,
            mode_controller=mode_controller,
        )

        policy_decision = policy.evaluate(target=make_target())
        risk_decision = risk.evaluate(target=make_target())

        self.assertFalse(policy_decision.execution_allowed)
        self.assertTrue(policy_decision.dry_run_only is False)
        self.assertIn("market_data_stale", policy_decision.rejection_reasons)
        self.assertFalse(risk_decision.approved)
        self.assertIn("market_data_stale", risk_decision.rejection_reasons)


class TestRealMarketPaperLoop(unittest.IsolatedAsyncioTestCase):
    async def test_real_market_paper_mode_runs_full_local_paper_execution_loop(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "real_market_paper",
                "mode": "paper_live",
                "market_data_backend": "demo",
                "execution_backend": "okx",
                "account_backend": "disabled",
                "account_read_enabled": False,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "enabled_decision_timeframes": ("15m",),
            }
        )
        runtime = await build_runtime(settings)

        self.assertIsInstance(runtime.execution_adapter, PaperExecutionAdapter)
        self.assertEqual(runtime.mode_controller.operating_state(), "local_demo")

        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=4,
            interval_seconds=0.0,
        )

        policy_event = runtime.event_store.latest(topics.POLICY_DECISIONS)
        risk_event = runtime.event_store.latest(topics.RISK_DECISIONS)
        audit_event = runtime.event_store.latest(topics.AUDIT_RECORDS)
        self.assertIsNotNone(policy_event)
        self.assertIsNotNone(risk_event)
        self.assertIsNotNone(audit_event)

        orders = runtime.execution_repo.order_states()
        fills = runtime.execution_repo.fills()
        self.assertGreater(len(orders), 0)
        self.assertGreater(len(fills), 0)
        self.assertTrue(all(order.venue == "PAPER" for order in orders))
        self.assertTrue(all(order.status == "FILLED" for order in orders))

        latest_portfolio = runtime.portfolio_repo.latest()
        self.assertIsNotNone(latest_portfolio)
        self.assertGreater(len(latest_portfolio.positions), 0)
        self.assertGreater(latest_portfolio.total_equity, 0.0)

        latest_reconciliation = runtime.reconciliation_repo.latest()
        self.assertIsNotNone(latest_reconciliation)
        self.assertEqual(latest_reconciliation.severity, "CLEAN")

        audited_records = runtime.audit_repo.all()
        self.assertGreater(len(audited_records), 0)
        executed_records = [record for record in audited_records if record.fill_event_refs]
        self.assertTrue(executed_records)
        for record in executed_records:
            self.assertIsNotNone(record.policy_decision_ref)
            self.assertIsNotNone(record.risk_decision_ref)
            self.assertIsNotNone(record.execution_plan_ref)
            self.assertTrue(record.order_intent_refs)
            self.assertTrue(record.order_state_refs)
            self.assertTrue(record.fill_event_refs)
            self.assertIsNotNone(record.portfolio_delta_ref)
            self.assertTrue(record.reconciliation_refs)

    async def test_guarded_live_adapter_stays_dry_run_and_never_submits(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "market_data_backend": "okx",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": True,
                "okx_simulated_trading": False,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        fake_client = FakeOKXClient()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=fake_client,  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
        )

        state, fills = await adapter.submit(
            OrderIntent(
                intent_id="intent_guarded",
                decision_id="decision_guarded",
                symbol="BTC-USDT",
                side="buy",
                quantity=0.001,
                execution_style="taker",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                idempotency_key="intent_guarded",
            )
        )

        self.assertEqual(state.status, "DRY_RUN")
        self.assertEqual(state.venue, "OKX")
        self.assertEqual(fills, [])
        self.assertEqual(fake_client.place_order_calls, [])


if __name__ == "__main__":
    unittest.main()
