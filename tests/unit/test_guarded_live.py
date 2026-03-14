from __future__ import annotations

import unittest

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.decision import PositionTarget
from aats.schemas.execution import OrderIntent
from aats.schemas.exchange import ExchangeAccountSnapshot, InstrumentMetadata
from aats.schemas.governance import PolicyDecision
from aats.services.decision_engine.trigger_policy import DecisionTriggerPolicy
from aats.services.execution_engine.okx_adapter import OKXExecutionAdapter
from aats.services.governance_engine.health import SystemHealthService
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.governance_engine.mode import RuntimeModeController
from aats.services.governance_engine.policy import PolicyEngine
from aats.services.governance_engine.risk import RiskEngine


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
            "connected": True,
            "fresh": True,
            "last_update_ts": self._snapshot.fetched_at,
            "last_error": None,
            "ready": True,
            "detail": "test_account",
            "blockers": [],
        }


class FakeExecutionProvider:
    def readiness(self):
        return {"ready": True, "connected": True, "fresh": True, "blockers": [], "detail": "ok"}


class FakeMarketProvider:
    def status(self):
        return {"ready": False, "connected": True, "fresh": False, "blockers": ["market_data_stale"], "detail": "stale"}


class FakeReconciliationRepo:
    def latest(self):
        return None


class FakeOKXClient:
    def __init__(self) -> None:
        self.place_order_calls: list[dict] = []

    async def place_order(self, payload):
        self.place_order_calls.append(dict(payload))
        return {"code": "0", "data": [{"ordId": "1"}]}


class TestGuardedLive(unittest.IsolatedAsyncioTestCase):
    def test_mode_snapshot_makes_guarded_simulated_boundaries_explicit(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "guarded_simulated_submit_dry_run",
                "mode": "guarded_live",
                "market_data_backend": "okx",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": True,
            }
        )
        controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())

        snapshot = controller.snapshot()

        self.assertEqual(snapshot["operating_state"], "guarded_simulated_submit_dry_run")
        self.assertEqual(snapshot["market_data_source"], "okx")
        self.assertEqual(snapshot["account_read_source"], "okx")
        self.assertEqual(snapshot["execution_route"], "okx_demo_guarded")
        self.assertEqual(snapshot["exchange_submit_target"], "okx_demo")
        self.assertFalse(snapshot["exchange_submit_allowed"])
        self.assertIn("guarded_execution_dry_run", snapshot["submit_blocked_reasons"])

    async def test_okx_adapter_stays_in_dry_run_when_live_submit_disabled(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
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
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=FakeOKXClient(),  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
        )

        order_state, fills = await adapter.submit(
            OrderIntent(
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
        )

        self.assertEqual(order_state.status, "DRY_RUN")
        self.assertEqual(order_state.venue, "OKX")
        self.assertEqual(fills, [])
        readiness = adapter.readiness()
        self.assertFalse(readiness["exchange_submit_allowed"])
        self.assertIn("guarded_execution_dry_run", readiness["submit_blocked_reasons"])
        self.assertTrue(readiness["safety_gates"]["mode_is_guarded_live"])
        self.assertFalse(readiness["safety_gates"]["dry_run_disabled"])

    def test_policy_and_risk_block_unsafe_live_path(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "max_open_orders": 1,
                "okx_simulated_trading": False,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeMarketProvider(),  # type: ignore[arg-type]
            account_provider=FakeAccountService(),  # type: ignore[arg-type]
            execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=FakeReconciliationRepo(),  # type: ignore[arg-type]
        )
        policy = PolicyEngine(
            settings=settings,
            kill_switch=kill_switch,
            mode_controller=mode_controller,
            health_service=health_service,
        )
        trigger_policy = DecisionTriggerPolicy(settings=settings)
        risk = RiskEngine(
            settings=settings,
            account_service=FakeAccountService(open_order_count=1),  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=trigger_policy,
            price_provider=lambda _symbol: 67_000.0,
            mode_controller=mode_controller,
        )
        target = PositionTarget(
            decision_id="decision_1",
            symbol="BTC-USDT",
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

        policy_decision = policy.evaluate(target=target)
        self.assertFalse(policy_decision.allowed)
        self.assertIn("market_data_stale", policy_decision.rejection_reasons)

        risk_decision = risk.evaluate(target=target)
        self.assertFalse(risk_decision.approved)
        self.assertIn("max_open_orders_reached", risk_decision.rejection_reasons)

    def test_config_safety_defaults_remain_disabled(self) -> None:
        self.assertEqual(AATSSettings.model_fields["config_profile"].default, "local_demo")
        self.assertEqual(AATSSettings.model_fields["market_data_backend"].default, "demo")
        self.assertEqual(AATSSettings.model_fields["execution_backend"].default, "paper")
        self.assertFalse(AATSSettings.model_fields["account_read_enabled"].default)
        self.assertFalse(AATSSettings.model_fields["live_submit_enabled"].default)
        self.assertTrue(AATSSettings.model_fields["guarded_execution_dry_run"].default)
        self.assertFalse(AATSSettings.model_fields["okx_simulated_trading"].default)


if __name__ == "__main__":
    unittest.main()
