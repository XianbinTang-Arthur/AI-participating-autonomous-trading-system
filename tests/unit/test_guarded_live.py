from __future__ import annotations

import unittest

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.decision import PositionTarget
from aats.schemas.execution import OrderIntent, OrderObligation
from aats.schemas.exchange import ExchangeAccountSnapshot, ExchangeBalance, InstrumentMetadata
from aats.schemas.governance import PolicyDecision
from aats.services.decision_engine.trigger_policy import DecisionTriggerPolicy
from aats.services.execution_engine.okx_adapter import OKXExecutionAdapter
from aats.services.governance_engine.health import SystemHealthService
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.governance_engine.mode import RuntimeModeController
from aats.services.governance_engine.policy import PolicyEngine
from aats.services.governance_engine.risk import RiskEngine
from aats.storage.obligation_repo import InMemoryExecutionObligationRepository


class FakeAccountService:
    def __init__(
        self,
        *,
        open_order_count: int = 0,
        btc_available: float = 1.0,
        usdt_available: float = 100_000.0,
    ) -> None:
        self._open_order_count = open_order_count
        self._snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[
                ExchangeBalance(currency="BTC", total=btc_available, available=btc_available, frozen=0.0),
                ExchangeBalance(currency="USDT", total=usdt_available, available=usdt_available, frozen=0.0),
            ],
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


class FakeBlockedExecutionProvider:
    def readiness(self):
        return {
            "ready": True,
            "connected": True,
            "fresh": True,
            "blockers": [],
            "detail": "adapter_blocked",
            "exchange_submit_allowed": False,
            "submit_blocked_reasons": ["adapter_submit_blocked"],
        }


class FakeMarketProvider:
    def status(self):
        return {"ready": False, "connected": True, "fresh": False, "blockers": ["market_data_stale"], "detail": "stale"}


class FakeHealthyMarketProvider:
    def status(self):
        return {"ready": True, "connected": True, "fresh": True, "blockers": [], "detail": "ok"}


class FakeReconciliationRepo:
    def latest(self):
        return None


class FakeHealthyReconciliationRepo:
    def latest(self):
        return type(
            "_Report",
            (),
            {
                "severity": "CLEAN",
                "halt_required": False,
                "review_required": False,
                "as_of_ts": utc_now(),
            },
        )()


class FakeOKXClient:
    def __init__(self) -> None:
        self.place_order_calls: list[dict] = []

    async def place_order(self, payload):
        self.place_order_calls.append(dict(payload))
        return {"code": "0", "data": [{"ordId": "1"}]}

    async def get_max_order_quantity(self, *, symbol: str, td_mode: str, leverage=None, price=None):
        _ = symbol
        _ = td_mode
        _ = leverage
        _ = price
        return {"code": "0", "data": [{"maxBuy": "100", "maxSell": "100"}]}


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

        self.assertEqual(snapshot["operating_state"], "guarded_simulated_submit_spot_dry_run")
        self.assertEqual(snapshot["market_data_source"], "okx")
        self.assertEqual(snapshot["account_read_source"], "okx")
        self.assertEqual(snapshot["execution_route"], "okx_demo_guarded")
        self.assertEqual(snapshot["exchange_submit_target"], "okx_demo_spot")
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

    def test_policy_and_risk_include_execution_adapter_submit_blockers(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
            account_provider=FakeAccountService(),  # type: ignore[arg-type]
            execution_provider=FakeBlockedExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=FakeHealthyReconciliationRepo(),  # type: ignore[arg-type]
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
        target = PositionTarget(
            decision_id="decision_adapter_blocked",
            symbol="BTC-USDT",
            current_position_qty=0.0,
            target_position_qty=0.001,
            delta_position_qty=0.001,
            current_notional=0.0,
            target_notional=67.0,
            rebalance_reason="test",
            urgency="medium",
            max_slippage_tolerance_bps=20,
            source_mix={"baseline": 1.0},
            decision_expiry_ts=utc_now(),
        )

        policy_decision = policy.evaluate(target)
        risk_decision = risk.evaluate(target)

        self.assertIn("adapter_submit_blocked", policy_decision.rejection_reasons)
        self.assertIn("adapter_submit_blocked", risk_decision.rejection_reasons)

    def test_config_safety_defaults_remain_disabled(self) -> None:
        self.assertEqual(AATSSettings.model_fields["config_profile"].default, "local_demo")
        self.assertEqual(AATSSettings.model_fields["market_data_backend"].default, "demo")
        self.assertEqual(AATSSettings.model_fields["execution_backend"].default, "paper")
        self.assertFalse(AATSSettings.model_fields["account_read_enabled"].default)
        self.assertFalse(AATSSettings.model_fields["live_submit_enabled"].default)

    def test_policy_rejection_reasons_are_deduplicated(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
            }
        )
        kill_switch = KillSwitch()
        kill_switch.halt(reason="manual_test")
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
        target = PositionTarget(
            decision_id="decision_dedupe",
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

        decision = policy.evaluate(target=target)

        self.assertEqual(decision.rejection_reasons.count("kill_switch_active"), 1)

    def test_derivatives_margin_check_uses_quote_currency_from_swap_symbol(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "max_target_leverage": 5.0,
                "default_target_leverage": 2.0,
                "okx_simulated_trading": True,
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
            account_provider=FakeAccountService(usdt_available=75_000.0),  # type: ignore[arg-type]
            execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=FakeHealthyReconciliationRepo(),  # type: ignore[arg-type]
        )
        trigger_policy = DecisionTriggerPolicy(settings=settings)
        account_service = FakeAccountService(usdt_available=75_000.0)
        account_service._snapshot.instruments = [
            InstrumentMetadata(
                instrument_id="BTC-USDT-SWAP",
                symbol="BTC-USDT-SWAP",
                base_currency="",
                quote_currency="",
                lot_size=0.01,
                tick_size=0.1,
                min_size=0.01,
                state="live",
            )
        ]
        risk = RiskEngine(
            settings=settings,
            account_service=account_service,  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=trigger_policy,
            price_provider=lambda _symbol: 74_000.0,
            mode_controller=mode_controller,
        )
        target = PositionTarget(
            decision_id="decision_derivatives_margin",
            symbol="BTC-USDT-SWAP",
            current_position_qty=0.000048,
            target_position_qty=0.03086244189970906,
            delta_position_qty=0.03081444189970906,
            current_notional=0.0,
            target_notional=2283.0,
            rebalance_reason="test_derivatives_margin",
            urgency="medium",
            max_slippage_tolerance_bps=20,
            source_mix={"baseline": 1.0},
            decision_expiry_ts=utc_now(),
            product_type="derivatives",
            current_exposure_side="long",
            target_exposure_side="long",
            position_intent="open_long",
            target_leverage=2.45,
            margin_mode="cross",
        )

        risk_decision = risk.evaluate(target=target)

        self.assertTrue(risk_decision.approved)
        self.assertNotIn("insufficient_initial_margin", risk_decision.rejection_reasons)
        self.assertTrue(AATSSettings.model_fields["guarded_execution_dry_run"].default)
        self.assertFalse(AATSSettings.model_fields["okx_simulated_trading"].default)

    def test_risk_blocks_buy_when_quote_balance_is_insufficient(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
            account_provider=FakeAccountService(usdt_available=10.0),  # type: ignore[arg-type]
            execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=FakeReconciliationRepo(),  # type: ignore[arg-type]
        )
        risk = RiskEngine(
            settings=settings,
            account_service=FakeAccountService(usdt_available=10.0),  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda _symbol: 67_000.0,
            mode_controller=mode_controller,
        )

        decision = risk.evaluate(
            PositionTarget(
                decision_id="decision_buy_balance",
                symbol="BTC-USDT",
                current_position_qty=0.0,
                target_position_qty=0.001,
                delta_position_qty=0.001,
                current_notional=0.0,
                target_notional=67.0,
                rebalance_reason="test",
                urgency="medium",
                max_slippage_tolerance_bps=20,
                source_mix={"baseline": 1.0},
                decision_expiry_ts=utc_now(),
            )
        )

        self.assertFalse(decision.approved)
        self.assertIn("insufficient_quote_balance", decision.rejection_reasons)

    def test_risk_uses_slippage_and_fee_budget_for_spot_buy_balance_check(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
            account_provider=FakeAccountService(usdt_available=67.05),  # type: ignore[arg-type]
            execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=FakeReconciliationRepo(),  # type: ignore[arg-type]
        )
        risk = RiskEngine(
            settings=settings,
            account_service=FakeAccountService(usdt_available=67.05),  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda _symbol: 67_000.0,
            mode_controller=mode_controller,
        )

        decision = risk.evaluate(
            PositionTarget(
                decision_id="decision_buy_slippage_budget",
                symbol="BTC-USDT",
                current_position_qty=0.0,
                target_position_qty=0.001,
                delta_position_qty=0.001,
                current_notional=0.0,
                target_notional=67.0,
                rebalance_reason="test",
                urgency="medium",
                max_slippage_tolerance_bps=20,
                source_mix={"baseline": 1.0},
                decision_expiry_ts=utc_now(),
            )
        )

        self.assertFalse(decision.approved)
        self.assertIn("insufficient_quote_balance", decision.rejection_reasons)

    def test_risk_subtracts_local_quote_obligations_before_balance_check(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
            account_provider=FakeAccountService(usdt_available=100.0),  # type: ignore[arg-type]
            execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=FakeReconciliationRepo(),  # type: ignore[arg-type]
        )
        obligation_repo = InMemoryExecutionObligationRepository()
        obligation_repo.save_obligation(
            OrderObligation(
                client_order_id="cl_pending_quote",
                decision_id="decision_pending_quote",
                intent_id="intent_pending_quote",
                symbol="BTC-USDT",
                side="buy",
                reserve_currency="USDT",
                reserved_amount=50.0,
                status="ACTIVE",
            )
        )
        risk = RiskEngine(
            settings=settings,
            account_service=FakeAccountService(usdt_available=100.0),  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda _symbol: 67_000.0,
            mode_controller=mode_controller,
            obligation_repo=obligation_repo,
        )

        decision = risk.evaluate(
            PositionTarget(
                decision_id="decision_buy_after_local_hold",
                symbol="BTC-USDT",
                current_position_qty=0.0,
                target_position_qty=0.001,
                delta_position_qty=0.001,
                current_notional=0.0,
                target_notional=67.0,
                rebalance_reason="test",
                urgency="medium",
                max_slippage_tolerance_bps=20,
                source_mix={"baseline": 1.0},
                decision_expiry_ts=utc_now(),
            )
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.current_open_order_count, 1)
        self.assertIn("insufficient_quote_balance", decision.rejection_reasons)

    def test_risk_blocks_sell_when_base_balance_is_insufficient(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
            account_provider=FakeAccountService(btc_available=0.0),  # type: ignore[arg-type]
            execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=FakeReconciliationRepo(),  # type: ignore[arg-type]
        )
        risk = RiskEngine(
            settings=settings,
            account_service=FakeAccountService(btc_available=0.0),  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda _symbol: 67_000.0,
            mode_controller=mode_controller,
        )

        decision = risk.evaluate(
            PositionTarget(
                decision_id="decision_sell_balance",
                symbol="BTC-USDT",
                current_position_qty=0.001,
                target_position_qty=0.0,
                delta_position_qty=-0.001,
                current_notional=67.0,
                target_notional=0.0,
                rebalance_reason="test",
                urgency="medium",
                max_slippage_tolerance_bps=20,
                source_mix={"baseline": 1.0},
                decision_expiry_ts=utc_now(),
            )
        )

        self.assertFalse(decision.approved)
        self.assertIn("insufficient_base_balance", decision.rejection_reasons)

    def test_derivatives_risk_allows_short_without_base_inventory_but_enforces_margin_and_leverage(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "max_target_leverage": 3.0,
                "max_margin_usage_fraction": 0.8,
                "liquidation_buffer_fraction": 0.15,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
            account_provider=FakeAccountService(btc_available=0.0, usdt_available=200.0),  # type: ignore[arg-type]
            execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=FakeHealthyReconciliationRepo(),  # type: ignore[arg-type]
        )
        risk = RiskEngine(
            settings=settings,
            account_service=FakeAccountService(btc_available=0.0, usdt_available=200.0),  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda _symbol: 67_000.0,
            mode_controller=mode_controller,
        )

        accepted = risk.evaluate(
            PositionTarget(
                decision_id="decision_short_derivatives",
                symbol="BTC-USDT",
                current_position_qty=0.0,
                target_position_qty=-0.001,
                delta_position_qty=-0.001,
                current_notional=0.0,
                target_notional=-67.0,
                rebalance_reason="test",
                urgency="medium",
                max_slippage_tolerance_bps=20,
                source_mix={"baseline": 1.0},
                decision_expiry_ts=utc_now(),
                product_type="derivatives",
                current_exposure_side="flat",
                target_exposure_side="short",
                position_intent="open_short",
                target_leverage=3.0,
                margin_mode="cross",
            )
        )
        self.assertTrue(accepted.approved)

        rejected = risk.evaluate(
            PositionTarget(
                decision_id="decision_short_derivatives_overlevered",
                symbol="BTC-USDT",
                current_position_qty=0.0,
                target_position_qty=-0.01,
                delta_position_qty=-0.01,
                current_notional=0.0,
                target_notional=-670.0,
                rebalance_reason="test",
                urgency="medium",
                max_slippage_tolerance_bps=20,
                source_mix={"baseline": 1.0},
                decision_expiry_ts=utc_now(),
                product_type="derivatives",
                current_exposure_side="flat",
                target_exposure_side="short",
                position_intent="open_short",
                target_leverage=5.0,
                margin_mode="cross",
            )
        )
        self.assertFalse(rejected.approved)
        self.assertIn("max_target_leverage_exceeded", rejected.rejection_reasons)


if __name__ == "__main__":
    unittest.main()
