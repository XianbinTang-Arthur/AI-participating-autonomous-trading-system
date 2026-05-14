from __future__ import annotations

import unittest
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.decision import PositionTarget
from aats.schemas.execution import LegOrderIntent, OrderIntent, OrderObligation
from aats.schemas.exchange import (
    ExchangeAccountRiskSnapshot,
    ExchangeAccountSnapshot,
    ExchangeBalance,
    ExchangeOpenOrder,
    ExchangePosition,
    InstrumentMetadata,
)
from aats.schemas.reconciliation import ReconciliationReport
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
        open_order_count: int | None = None,
        btc_available: float = 1.0,
        usdt_available: float = 100_000.0,
        symbol: str = "BTC-USDT",
        positions: list[ExchangePosition] | None = None,
        open_orders: list[ExchangeOpenOrder] | None = None,
        instruments: list[InstrumentMetadata] | None = None,
        risk_snapshot: ExchangeAccountRiskSnapshot | None = None,
        recent_bills: list[dict] | None = None,
    ) -> None:
        self._open_order_count = open_order_count
        instrument_rows = instruments or [self._default_instrument(symbol=symbol)]
        self._snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[
                ExchangeBalance(currency="BTC", total=btc_available, available=btc_available, frozen=0.0),
                ExchangeBalance(currency="USDT", total=usdt_available, available=usdt_available, frozen=0.0),
            ],
            positions=list(positions or []),
            open_orders=list(open_orders or []),
            instruments=instrument_rows,
            account_mode="cash",
            risk_snapshot=risk_snapshot,
        )
        self._recent_bills = list(recent_bills or [])

    async def refresh(self, *, force: bool = False):
        return self._snapshot

    def instrument_metadata(self, symbol: str):
        return next((item for item in self._snapshot.instruments if item.symbol == symbol), None)

    def open_order_count(self, symbol: str | None = None) -> int:
        if self._open_order_count is not None:
            return self._open_order_count
        if symbol is None:
            return len(self._snapshot.open_orders)
        return sum(
            1
            for order in self._snapshot.open_orders
            if str(getattr(order, "instrument_id", "") or "") == symbol
        )

    def latest_snapshot(self):
        return self._snapshot

    def latest_recent_bills(self):
        return list(self._recent_bills)

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

    @staticmethod
    def _default_instrument(*, symbol: str) -> InstrumentMetadata:
        parts = [part for part in symbol.split("-") if part]
        base_currency = parts[0] if len(parts) >= 1 else "BTC"
        quote_currency = parts[1] if len(parts) >= 2 else "USDT"
        return InstrumentMetadata(
            instrument_id=symbol,
            symbol=symbol,
            base_currency=base_currency if not symbol.endswith("-SWAP") else "",
            quote_currency=quote_currency if not symbol.endswith("-SWAP") else "",
            lot_size=Decimal("0.0001") if not symbol.endswith("-SWAP") else Decimal("0.01"),
            tick_size=Decimal("0.1"),
            min_size=Decimal("0.0001") if not symbol.endswith("-SWAP") else Decimal("0.01"),
            instrument_family=f"{base_currency}-{quote_currency}" if len(parts) >= 2 else None,
            settle_currency=quote_currency if symbol.endswith("-SWAP") else quote_currency,
            state="live",
        )


class FakeUnreadyAccountService(FakeAccountService):
    def status(self):
        status = super().status()
        status.update(
            {
                "connected": False,
                "fresh": False,
                "last_error": "balance_down",
                "ready": False,
                "blockers": ["account_refresh_failed"],
            }
        )
        return status


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


class FakeAdaptiveRuntimeGuardProvider:
    def snapshot(self):
        return {
            "status": "warning",
            "only_reduce_required": False,
            "auto_halt_required": False,
            "current_initial_margin_usage_fraction": Decimal("0.72"),
            "nearest_liquidation_gap_ratio": Decimal("0.12"),
        }


class FakeBreachedTrialGuardProvider:
    def snapshot(self):
        return {"status": "breached"}


class FakeOnlyReduceReconciliationRepo:
    def latest(self):
        return ReconciliationReport(
            reconciliation_id="recon_only_reduce_live",
            as_of_ts=utc_now(),
            product_type="derivatives",
            margin_mode="cross",
            allowed_symbols=["BTC-USDT-SWAP"],
            exchange_comparison_enabled=True,
            order_diff={"reconstructed": {}, "exchange": {}},
            fill_diff={"replayed": {}, "exchange": {}},
            balance_diff={"reconstructed": {}, "exchange": {}},
            position_diff={
                "stored": {},
                "reconstructed": {},
                "reconstructed_mismatches": {},
                "exchange": {"BTC-USDT-SWAP": "0.03"},
                "exchange_mismatches": {"BTC-USDT-SWAP": {"stored": "0", "exchange": "0.03"}},
            },
            mismatch_categories=["derivatives_exchange_position_without_local_execution_chain"],
            mismatch_reasons=["derivatives_exchange_position_not_replayed_locally"],
            safety_impacts=["derivatives_only_reduce_until_position_reconciled"],
            severity="SOFT_MISMATCH",
            recovery_classification="derivatives_only_reduce",
            only_reduce_required=True,
            only_reduce_reasons=["derivatives_exchange_position_without_local_execution_chain"],
            recommended_operator_action="go_close_position_on_exchange",
        )


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
        self.assertEqual(order_state.submission_mode, "guarded_live_dry_run")
        self.assertEqual(order_state.venue, "OKX")
        self.assertEqual(fills, [])
        readiness = adapter.readiness()
        self.assertFalse(readiness["exchange_submit_allowed"])
        self.assertIn("guarded_execution_dry_run", readiness["submit_blocked_reasons"])
        self.assertTrue(readiness["safety_gates"]["mode_is_guarded_live"])
        self.assertFalse(readiness["safety_gates"]["dry_run_disabled"])

    def test_mode_snapshot_exposes_guarded_live_spot_submit_route(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "market_data_backend": "okx",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": False,
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
            }
        )
        controller = RuntimeModeController(settings=settings, kill_switch=KillSwitch())

        snapshot = controller.snapshot()

        self.assertEqual(snapshot["operating_state"], "guarded_live_enabled")
        self.assertEqual(snapshot["execution_route"], "okx_live_guarded")
        self.assertEqual(snapshot["exchange_submit_target"], "okx_live_spot")
        self.assertTrue(snapshot["exchange_submit_allowed"])
        self.assertEqual(snapshot["submit_blocked_reasons"], [])

    async def test_okx_adapter_allows_live_submission_when_guards_satisfied(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
                "okx_simulated_trading": False,
                "max_notional_per_symbol": 1_000.0,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        client = FakeOKXClient()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=client,  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=mode_controller,
            price_provider=lambda _symbol: 68_000.0,
        )

        order_state, fills = await adapter.submit(
            OrderIntent(
                intent_id="intent_live_1",
                decision_id="decision_live_1",
                symbol="BTC-USDT",
                side="buy",
                quantity=0.001,
                execution_style="taker",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                idempotency_key="intent_live_1",
            )
        )

        self.assertEqual(len(client.place_order_calls), 1)
        self.assertEqual(order_state.status, "SUBMITTED")
        self.assertEqual(order_state.submission_mode, "guarded_live_submit")
        self.assertEqual(fills, [])
        readiness = adapter.readiness()
        self.assertTrue(readiness["exchange_submit_allowed"])
        self.assertEqual(readiness["execution_mode"], "guarded_live_submit")
        self.assertTrue(readiness["safety_gates"]["submission_target_is_live"])
        self.assertTrue(readiness["safety_gates"]["okx_environment_matches_target"])
        self.assertNotIn("okx_simulated_trading_required", readiness["submit_blocked_reasons"])

    def test_risk_engine_exposes_adaptive_budget_and_execution_contraction(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "live_submit_enabled": True,
                "max_abs_position_qty": 0.2,
                "max_notional_per_symbol": 5_000,
                "max_gross_notional_per_symbol": 5_000,
                "max_pending_notional_per_symbol": 5_000,
                "max_total_open_notional": 10_000,
                "max_target_leverage": 5,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        account_service = FakeAccountService(symbol="BTC-USDT-SWAP")
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
            account_provider=account_service,  # type: ignore[arg-type]
            execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=FakeHealthyReconciliationRepo(),  # type: ignore[arg-type]
        )
        risk = RiskEngine(
            settings=settings,
            account_service=account_service,  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda _symbol: Decimal("30000"),
            mode_controller=mode_controller,
            obligation_repo=InMemoryExecutionObligationRepository(),
            live_runtime_guard_provider=FakeAdaptiveRuntimeGuardProvider(),
            trial_guard_provider=FakeBreachedTrialGuardProvider(),
        )

        decision = risk.evaluate(
            PositionTarget(
                decision_id="adaptive_risk_budget",
                symbol="BTC-USDT-SWAP",
                target_position_qty=Decimal("0.05"),
                current_position_qty=Decimal("0"),
                delta_position_qty=Decimal("0.05"),
                current_notional=Decimal("0"),
                target_notional=Decimal("1500"),
                rebalance_reason="test_adaptive_controls",
                urgency="medium",
                max_slippage_tolerance_bps=20,
                source_mix={"baseline": 1.0},
                decision_expiry_ts=utc_now(),
                product_type="derivatives",
                target_leverage=3.0,
                margin_mode="cross",
            )
        )

        self.assertTrue(decision.approved)
        self.assertLess(decision.risk_budget_multiplier, Decimal("1"))
        self.assertLess(decision.execution_aggressiveness_multiplier, Decimal("1"))
        self.assertIn("risk_budget_multiplier_applied", decision.constraints_applied)
        self.assertIn("execution_aggressiveness_contracted", decision.constraints_applied)

    def test_risk_engine_ignores_stale_snapshot_when_account_status_not_ready(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "live_submit_enabled": True,
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        account_service = FakeUnreadyAccountService(symbol="BTC-USDT-SWAP", usdt_available=100_000.0)
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
            account_provider=account_service,  # type: ignore[arg-type]
            execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=FakeHealthyReconciliationRepo(),  # type: ignore[arg-type]
        )
        risk = RiskEngine(
            settings=settings,
            account_service=account_service,  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda _symbol: Decimal("30000"),
            mode_controller=mode_controller,
            obligation_repo=InMemoryExecutionObligationRepository(),
        )

        self.assertIsNone(risk._snapshot())
        self.assertEqual(
            risk._available_derivatives_equity(
                snapshot=risk._snapshot(),
                settle_currency="USDT",
            ),
            Decimal("0"),
        )

    def test_leg_risk_engine_inherits_adaptive_budget_and_execution_contraction(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "live_submit_enabled": True,
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "max_abs_position_qty": 0.2,
                "max_notional_per_symbol": 5_000,
                "max_gross_notional_per_symbol": 5_000,
                "max_pending_notional_per_symbol": 5_000,
                "max_total_open_notional": 10_000,
                "max_target_leverage": 5,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        account_service = FakeAccountService(symbol="BTC-USDT-SWAP")
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
            account_provider=account_service,  # type: ignore[arg-type]
            execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=FakeHealthyReconciliationRepo(),  # type: ignore[arg-type]
        )
        risk = RiskEngine(
            settings=settings,
            account_service=account_service,  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda _symbol: Decimal("30000"),
            mode_controller=mode_controller,
            obligation_repo=InMemoryExecutionObligationRepository(),
            live_runtime_guard_provider=FakeAdaptiveRuntimeGuardProvider(),
            trial_guard_provider=FakeBreachedTrialGuardProvider(),
        )

        decision = risk.evaluate_leg_order(
            LegOrderIntent(
                leg_intent_id="adaptive_leg_risk",
                decision_id="adaptive_leg_risk",
                symbol="BTC-USDT-SWAP",
                side="buy",
                pos_side="long",
                action="open",
                quantity=Decimal("0.01"),
                execution_style="taker",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                idempotency_key="adaptive_leg_risk",
                product_type="derivatives",
                margin_mode="cross",
                td_mode="cross",
                target_leverage=3.0,
                exposure_side="long",
            )
        )

        self.assertTrue(decision.approved)
        self.assertLess(decision.risk_budget_multiplier, Decimal("1"))
        self.assertLess(decision.execution_aggressiveness_multiplier, Decimal("1"))
        self.assertIn("risk_budget_multiplier_applied", decision.constraints_applied)
        self.assertIn("execution_aggressiveness_contracted", decision.constraints_applied)
        self.assertTrue(decision.modified)
        self.assertEqual(decision.risk_budget_state.get("source"), "risk_engine_snapshot")

    def test_leg_bundle_risk_engine_inherits_adaptive_budget_and_execution_contraction(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "live_submit_enabled": True,
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "max_abs_position_qty": 0.2,
                "max_notional_per_symbol": 5_000,
                "max_gross_notional_per_symbol": 5_000,
                "max_pending_notional_per_symbol": 5_000,
                "max_total_open_notional": 10_000,
                "max_target_leverage": 5,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        account_service = FakeAccountService(symbol="BTC-USDT-SWAP")
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
            account_provider=account_service,  # type: ignore[arg-type]
            execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=FakeHealthyReconciliationRepo(),  # type: ignore[arg-type]
        )
        risk = RiskEngine(
            settings=settings,
            account_service=account_service,  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda _symbol: Decimal("30000"),
            mode_controller=mode_controller,
            obligation_repo=InMemoryExecutionObligationRepository(),
            live_runtime_guard_provider=FakeAdaptiveRuntimeGuardProvider(),
            trial_guard_provider=FakeBreachedTrialGuardProvider(),
        )

        decision = risk.evaluate_leg_order_bundle(
            [
                LegOrderIntent(
                    leg_intent_id="adaptive_bundle_long",
                    decision_id="adaptive_bundle",
                    symbol="BTC-USDT-SWAP",
                    side="buy",
                    pos_side="long",
                    action="open",
                    quantity=Decimal("0.01"),
                    execution_style="taker",
                    order_type="market",
                    urgency="medium",
                    time_in_force="IOC",
                    idempotency_key="adaptive_bundle_long",
                    product_type="derivatives",
                    margin_mode="cross",
                    td_mode="cross",
                    target_leverage=3.0,
                    exposure_side="long",
                ),
                LegOrderIntent(
                    leg_intent_id="adaptive_bundle_short",
                    decision_id="adaptive_bundle",
                    symbol="BTC-USDT-SWAP",
                    side="sell",
                    pos_side="short",
                    action="open",
                    quantity=Decimal("0.005"),
                    execution_style="taker",
                    order_type="market",
                    urgency="medium",
                    time_in_force="IOC",
                    idempotency_key="adaptive_bundle_short",
                    product_type="derivatives",
                    margin_mode="cross",
                    td_mode="cross",
                    target_leverage=3.0,
                    exposure_side="short",
                ),
            ]
        )

        self.assertTrue(decision.approved)
        self.assertLess(decision.risk_budget_multiplier, Decimal("1"))
        self.assertLess(decision.execution_aggressiveness_multiplier, Decimal("1"))
        self.assertIn("risk_budget_multiplier_applied", decision.constraints_applied)
        self.assertIn("execution_aggressiveness_contracted", decision.constraints_applied)
        self.assertTrue(decision.modified)
        self.assertEqual(decision.execution_aggressiveness_state.get("source"), "risk_engine_snapshot")

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

    def test_risk_allows_margin_backed_smart_arbitrage_spot_short_without_base_inventory(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "smart_arbitrage_margin_short_enabled": True,
                "smart_arbitrage_margin_short_execution_ready": True,
                "smart_arbitrage_margin_short_spot_margin_mode": "cross",
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
            account_provider=FakeAccountService(btc_available=0.0, usdt_available=500.0),  # type: ignore[arg-type]
            execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=FakeHealthyReconciliationRepo(),  # type: ignore[arg-type]
        )
        risk = RiskEngine(
            settings=settings,
            account_service=FakeAccountService(btc_available=0.0, usdt_available=500.0),  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda _symbol: 67_000.0,
            mode_controller=mode_controller,
        )

        decision = risk.evaluate(
            PositionTarget(
                decision_id="decision_margin_backed_spot_short",
                symbol="BTC-USDT",
                current_position_qty=0.0,
                target_position_qty=-0.001,
                delta_position_qty=-0.001,
                current_notional=0.0,
                target_notional=67.0,
                rebalance_reason="smart_arbitrage_margin_short",
                urgency="medium",
                max_slippage_tolerance_bps=20,
                source_mix={"smart_arbitrage": 1.0},
                decision_expiry_ts=utc_now(),
                product_type="spot",
                current_exposure_side="flat",
                target_exposure_side="short",
                position_intent="open_short",
                margin_mode="cross",
                strategy_family="smart_arbitrage",
                strategy_execution_mode="margin_reverse_carry",
            )
        )

        self.assertTrue(decision.approved)
        self.assertNotIn("insufficient_base_balance", decision.rejection_reasons)

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

    def test_derivatives_risk_caps_reverse_to_close_only_when_margin_usage_requires_only_reduce(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "max_target_leverage": 3.0,
                "max_margin_usage_fraction": 0.85,
                "derivatives_only_reduce_trigger_margin_fraction": 0.70,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        account_service = FakeAccountService(
            symbol="BTC-USDT-SWAP",
            btc_available=0.0,
            usdt_available=100.0,
            positions=[
                ExchangePosition(
                    instrument_id="BTC-USDT-SWAP",
                    symbol="BTC-USDT-SWAP",
                    quantity=Decimal("0.002"),
                    average_entry_price=Decimal("66000"),
                    mark_price=Decimal("67000"),
                    notional_usd=Decimal("134"),
                    side="long",
                )
            ],
            risk_snapshot=ExchangeAccountRiskSnapshot(
                adjusted_equity=Decimal("100"),
                total_equity=Decimal("100"),
                available_equity=Decimal("100"),
                initial_margin_requirement=Decimal("50"),
                maintenance_margin_requirement=Decimal("30"),
                margin_ratio=Decimal("0.5"),
                notional_usd=Decimal("134"),
            ),
        )
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
            account_provider=account_service,  # type: ignore[arg-type]
            execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=FakeHealthyReconciliationRepo(),  # type: ignore[arg-type]
        )
        risk = RiskEngine(
            settings=settings,
            account_service=account_service,  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda _symbol: 67_000.0,
            mode_controller=mode_controller,
        )

        decision = risk.evaluate(
            PositionTarget(
                decision_id="decision_derivatives_only_reduce",
                symbol="BTC-USDT-SWAP",
                current_position_qty=Decimal("0.002"),
                target_position_qty=Decimal("-0.001"),
                delta_position_qty=Decimal("-0.003"),
                current_notional=Decimal("134"),
                target_notional=Decimal("-67"),
                rebalance_reason="test_only_reduce",
                urgency="medium",
                max_slippage_tolerance_bps=20,
                source_mix={"baseline": 1.0},
                decision_expiry_ts=utc_now(),
                product_type="derivatives",
                current_exposure_side="long",
                target_exposure_side="short",
                position_intent="reverse_to_short",
                target_leverage=3.0,
                margin_mode="cross",
            )
        )

        self.assertTrue(decision.approved)
        self.assertTrue(decision.only_reduce_required)
        self.assertTrue(decision.flatten_required)
        self.assertEqual(decision.capped_target_position_qty, Decimal("0"))
        self.assertIn("only_reduce_required", decision.constraints_applied)
        self.assertIn("derivatives_margin_usage_requires_only_reduce", decision.constraints_applied)
        self.assertIsNotNone(decision.projected_margin_usage)
        self.assertGreater(decision.projected_margin_usage or Decimal("0"), Decimal("0.7"))

    def test_derivatives_risk_inherits_only_reduce_constraint_from_reconciliation_state(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        account_service = FakeAccountService(
            symbol="BTC-USDT-SWAP",
            btc_available=0.0,
            usdt_available=500.0,
            risk_snapshot=ExchangeAccountRiskSnapshot(
                adjusted_equity=Decimal("500"),
                total_equity=Decimal("500"),
                available_equity=Decimal("500"),
                initial_margin_requirement=Decimal("0"),
                maintenance_margin_requirement=Decimal("0"),
                margin_ratio=Decimal("0"),
                notional_usd=Decimal("0"),
            ),
        )
        reconciliation_repo = FakeOnlyReduceReconciliationRepo()
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
            account_provider=account_service,  # type: ignore[arg-type]
            execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=reconciliation_repo,  # type: ignore[arg-type]
        )
        risk = RiskEngine(
            settings=settings,
            account_service=account_service,  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda _symbol: 67_000.0,
            mode_controller=mode_controller,
            reconciliation_repo=reconciliation_repo,  # type: ignore[arg-type]
        )

        decision = risk.evaluate(
            PositionTarget(
                decision_id="decision_derivatives_recovery_only_reduce",
                symbol="BTC-USDT-SWAP",
                current_position_qty=Decimal("0"),
                target_position_qty=Decimal("0.002"),
                delta_position_qty=Decimal("0.002"),
                current_notional=Decimal("0"),
                target_notional=Decimal("134"),
                rebalance_reason="test_recovery_only_reduce",
                urgency="medium",
                max_slippage_tolerance_bps=20,
                source_mix={"baseline": 1.0},
                decision_expiry_ts=utc_now(),
                product_type="derivatives",
                current_exposure_side="flat",
                target_exposure_side="long",
                position_intent="open_long",
                target_leverage=2.0,
                margin_mode="cross",
            )
        )

        self.assertFalse(decision.approved)
        self.assertTrue(decision.only_reduce_required)
        self.assertTrue(decision.risk_limit_breached)
        self.assertEqual(decision.capped_target_position_qty, Decimal("0"))
        self.assertIn("derivatives_exchange_position_without_local_execution_chain", decision.constraints_applied)
        self.assertIn("derivatives_exchange_position_without_local_execution_chain", decision.rejection_reasons)
        self.assertIn("only_reduce_mode_active", decision.rejection_reasons)

    def test_derivatives_risk_blocks_new_exposure_after_daily_realized_loss_limit(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "max_target_leverage": 3.0,
                "max_daily_realized_loss_usdt": 100.0,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        now_ms = int(utc_now().timestamp() * 1000)
        account_service = FakeAccountService(
            symbol="BTC-USDT-SWAP",
            btc_available=0.0,
            usdt_available=500.0,
            recent_bills=[
                {"ts": str(now_ms), "pnl": "-125", "ccy": "USDT", "instId": "BTC-USDT-SWAP"},
            ],
            risk_snapshot=ExchangeAccountRiskSnapshot(
                adjusted_equity=Decimal("500"),
                total_equity=Decimal("500"),
                available_equity=Decimal("500"),
                initial_margin_requirement=Decimal("0"),
                maintenance_margin_requirement=Decimal("0"),
                margin_ratio=Decimal("0"),
                notional_usd=Decimal("0"),
            ),
        )
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
            account_provider=account_service,  # type: ignore[arg-type]
            execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=FakeHealthyReconciliationRepo(),  # type: ignore[arg-type]
        )
        risk = RiskEngine(
            settings=settings,
            account_service=account_service,  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda _symbol: 67_000.0,
            mode_controller=mode_controller,
        )

        decision = risk.evaluate(
            PositionTarget(
                decision_id="decision_derivatives_daily_loss",
                symbol="BTC-USDT-SWAP",
                current_position_qty=Decimal("0"),
                target_position_qty=Decimal("0.001"),
                delta_position_qty=Decimal("0.001"),
                current_notional=Decimal("0"),
                target_notional=Decimal("67"),
                rebalance_reason="test_daily_loss_limit",
                urgency="medium",
                max_slippage_tolerance_bps=20,
                source_mix={"baseline": 1.0},
                decision_expiry_ts=utc_now(),
                product_type="derivatives",
                current_exposure_side="flat",
                target_exposure_side="long",
                position_intent="open_long",
                target_leverage=2.0,
                margin_mode="cross",
            )
        )

        self.assertFalse(decision.approved)
        self.assertTrue(decision.only_reduce_required)
        self.assertTrue(decision.risk_limit_breached)
        self.assertIn("max_daily_realized_loss_usdt_exceeded", decision.rejection_reasons)
        self.assertIn("only_reduce_mode_active", decision.rejection_reasons)

    def test_derivatives_risk_blocks_when_pending_symbol_notional_exceeds_limit(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "max_pending_notional_per_symbol": 1200.0,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        account_service = FakeAccountService(
            symbol="BTC-USDT-SWAP",
            btc_available=0.0,
            usdt_available=800.0,
            open_orders=[
                ExchangeOpenOrder(
                    instrument_id="BTC-USDT-SWAP",
                    symbol="BTC-USDT-SWAP",
                    exchange_order_id="ord_pending_1",
                    side="buy",
                    order_type="limit",
                    status="live",
                    quantity=Decimal("0.015"),
                    filled_quantity=Decimal("0"),
                    price=Decimal("65000"),
                )
            ],
            risk_snapshot=ExchangeAccountRiskSnapshot(
                adjusted_equity=Decimal("800"),
                total_equity=Decimal("800"),
                available_equity=Decimal("800"),
                initial_margin_requirement=Decimal("50"),
                maintenance_margin_requirement=Decimal("20"),
                margin_ratio=Decimal("0.1"),
                notional_usd=Decimal("975"),
            ),
        )
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
            account_provider=account_service,  # type: ignore[arg-type]
            execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=FakeHealthyReconciliationRepo(),  # type: ignore[arg-type]
        )
        risk = RiskEngine(
            settings=settings,
            account_service=account_service,  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda _symbol: 67_000.0,
            mode_controller=mode_controller,
        )

        decision = risk.evaluate(
            PositionTarget(
                decision_id="decision_derivatives_pending_notional",
                symbol="BTC-USDT-SWAP",
                current_position_qty=Decimal("0"),
                target_position_qty=Decimal("0.005"),
                delta_position_qty=Decimal("0.005"),
                current_notional=Decimal("0"),
                target_notional=Decimal("335"),
                rebalance_reason="test_pending_notional_limit",
                urgency="medium",
                max_slippage_tolerance_bps=20,
                source_mix={"baseline": 1.0},
                decision_expiry_ts=utc_now(),
                product_type="derivatives",
                current_exposure_side="flat",
                target_exposure_side="long",
                position_intent="open_long",
                target_leverage=2.0,
                margin_mode="cross",
            )
        )

        self.assertFalse(decision.approved)
        self.assertTrue(decision.risk_limit_breached)
        self.assertIn("max_pending_notional_per_symbol_exceeded", decision.rejection_reasons)

    def test_derivatives_risk_still_allows_reducing_position_after_daily_loss_limit(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "max_daily_realized_loss_usdt": 100.0,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        now_ms = int(utc_now().timestamp() * 1000)
        account_service = FakeAccountService(
            symbol="BTC-USDT-SWAP",
            btc_available=0.0,
            usdt_available=400.0,
            positions=[
                ExchangePosition(
                    instrument_id="BTC-USDT-SWAP",
                    symbol="BTC-USDT-SWAP",
                    quantity=Decimal("0.002"),
                    average_entry_price=Decimal("66000"),
                    mark_price=Decimal("67000"),
                    notional_usd=Decimal("134"),
                    side="long",
                )
            ],
            recent_bills=[
                {"ts": str(now_ms), "pnl": "-125", "ccy": "USDT", "instId": "BTC-USDT-SWAP"},
            ],
            risk_snapshot=ExchangeAccountRiskSnapshot(
                adjusted_equity=Decimal("400"),
                total_equity=Decimal("400"),
                available_equity=Decimal("400"),
                initial_margin_requirement=Decimal("20"),
                maintenance_margin_requirement=Decimal("8"),
                margin_ratio=Decimal("0.05"),
                notional_usd=Decimal("134"),
            ),
        )
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
            account_provider=account_service,  # type: ignore[arg-type]
            execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=FakeHealthyReconciliationRepo(),  # type: ignore[arg-type]
        )
        risk = RiskEngine(
            settings=settings,
            account_service=account_service,  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda _symbol: 67_000.0,
            mode_controller=mode_controller,
        )

        decision = risk.evaluate(
            PositionTarget(
                decision_id="decision_derivatives_reduce_after_loss",
                symbol="BTC-USDT-SWAP",
                current_position_qty=Decimal("0.002"),
                target_position_qty=Decimal("0.001"),
                delta_position_qty=Decimal("-0.001"),
                current_notional=Decimal("134"),
                target_notional=Decimal("67"),
                rebalance_reason="test_reduce_after_loss",
                urgency="medium",
                max_slippage_tolerance_bps=20,
                source_mix={"baseline": 1.0},
                decision_expiry_ts=utc_now(),
                product_type="derivatives",
                current_exposure_side="long",
                target_exposure_side="long",
                position_intent="reduce_long",
                target_leverage=2.0,
                margin_mode="cross",
            )
        )

        self.assertTrue(decision.approved)
        self.assertFalse(decision.only_reduce_required)
        self.assertEqual(decision.rejection_reasons, [])

    def test_derivatives_leg_risk_blocks_same_side_expansion_when_long_leg_is_only_reduce(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "derivatives_position_mode": "hedge",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "risk_max_long_notional": 100.0,
                "risk_max_gross_notional": 500.0,
                "risk_max_net_notional": 100.0,
                "max_target_leverage": 3.0,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        account_service = FakeAccountService(
            symbol="BTC-USDT-SWAP",
            btc_available=0.0,
            usdt_available=500.0,
            positions=[
                ExchangePosition(
                    instrument_id="BTC-USDT-SWAP",
                    symbol="BTC-USDT-SWAP",
                    quantity=Decimal("0.002"),
                    average_entry_price=Decimal("66000"),
                    mark_price=Decimal("67000"),
                    notional_usd=Decimal("134"),
                    side="long",
                )
            ],
            risk_snapshot=ExchangeAccountRiskSnapshot(
                adjusted_equity=Decimal("500"),
                total_equity=Decimal("500"),
                available_equity=Decimal("500"),
                initial_margin_requirement=Decimal("25"),
                maintenance_margin_requirement=Decimal("10"),
                margin_ratio=Decimal("0.05"),
                notional_usd=Decimal("134"),
            ),
        )
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
            account_provider=account_service,  # type: ignore[arg-type]
            execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=FakeHealthyReconciliationRepo(),  # type: ignore[arg-type]
        )
        risk = RiskEngine(
            settings=settings,
            account_service=account_service,  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda _symbol: Decimal("67000"),
            mode_controller=mode_controller,
        )

        decision = risk.evaluate_leg_order(
            LegOrderIntent(
                leg_intent_id="leg_open_long_blocked",
                decision_id="leg_open_long_blocked",
                symbol="BTC-USDT-SWAP",
                side="buy",
                pos_side="long",
                action="open",
                quantity=Decimal("0.001"),
                execution_style="taker",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                idempotency_key="leg_open_long_blocked",
                product_type="derivatives",
                margin_mode="cross",
                td_mode="cross",
                target_leverage=2.0,
                exposure_side="long",
            )
        )

        self.assertFalse(decision.approved)
        self.assertTrue(decision.only_reduce_required)
        self.assertIn("risk_max_long_notional_exceeded", decision.rejection_reasons)
        self.assertIn("leg_only_reduce_mode_active", decision.rejection_reasons)
        self.assertIsNotNone(decision.current_derivatives_exposure)
        self.assertIsNotNone(decision.projected_derivatives_exposure)
        self.assertEqual(decision.current_derivatives_exposure.long_notional, Decimal("134"))
        self.assertEqual(decision.projected_derivatives_exposure.long_notional, Decimal("201"))
        constraints = {item.leg: item.reasons for item in decision.leg_only_reduce_constraints}
        self.assertIn("long", constraints)
        self.assertIn("risk_max_long_notional_exceeded", constraints["long"])

    def test_derivatives_leg_risk_allows_protective_short_hedge_when_long_leg_is_only_reduce(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "derivatives_position_mode": "hedge",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "risk_max_long_notional": 100.0,
                "risk_max_gross_notional": 500.0,
                "risk_max_net_notional": 100.0,
                "max_target_leverage": 3.0,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        account_service = FakeAccountService(
            symbol="BTC-USDT-SWAP",
            btc_available=0.0,
            usdt_available=500.0,
            positions=[
                ExchangePosition(
                    instrument_id="BTC-USDT-SWAP",
                    symbol="BTC-USDT-SWAP",
                    quantity=Decimal("0.002"),
                    average_entry_price=Decimal("66000"),
                    mark_price=Decimal("67000"),
                    notional_usd=Decimal("134"),
                    side="long",
                )
            ],
            risk_snapshot=ExchangeAccountRiskSnapshot(
                adjusted_equity=Decimal("500"),
                total_equity=Decimal("500"),
                available_equity=Decimal("500"),
                initial_margin_requirement=Decimal("25"),
                maintenance_margin_requirement=Decimal("10"),
                margin_ratio=Decimal("0.05"),
                notional_usd=Decimal("134"),
            ),
        )
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
            account_provider=account_service,  # type: ignore[arg-type]
            execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=FakeHealthyReconciliationRepo(),  # type: ignore[arg-type]
        )
        risk = RiskEngine(
            settings=settings,
            account_service=account_service,  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda _symbol: Decimal("67000"),
            mode_controller=mode_controller,
        )

        decision = risk.evaluate_leg_order(
            LegOrderIntent(
                leg_intent_id="leg_open_short_protective",
                decision_id="leg_open_short_protective",
                symbol="BTC-USDT-SWAP",
                side="sell",
                pos_side="short",
                action="open",
                quantity=Decimal("0.001"),
                execution_style="taker",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                idempotency_key="leg_open_short_protective",
                product_type="derivatives",
                margin_mode="cross",
                td_mode="cross",
                target_leverage=2.0,
                exposure_side="short",
            )
        )

        self.assertTrue(decision.approved)
        self.assertTrue(decision.only_reduce_required)
        self.assertEqual(decision.rejection_reasons, [])
        self.assertIsNotNone(decision.projected_derivatives_exposure)
        self.assertEqual(decision.projected_derivatives_exposure.short_notional, Decimal("67"))
        self.assertEqual(decision.projected_derivatives_exposure.net_notional, Decimal("67"))
        constraints = {item.leg: item.reasons for item in decision.leg_only_reduce_constraints}
        self.assertIn("long", constraints)
        self.assertNotIn("short", constraints)

    def test_derivatives_leg_risk_ignores_current_bundle_recovery_tracking_for_new_bundle_submit(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "derivatives_position_mode": "hedge",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "risk_max_long_notional": 1_000.0,
                "risk_max_short_notional": 1_000.0,
                "risk_max_gross_notional": 2_000.0,
                "risk_max_net_notional": 1_000.0,
                "max_target_leverage": 3.0,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        account_service = FakeAccountService(
            symbol="BTC-USDT-SWAP",
            btc_available=0.0,
            usdt_available=1_000.0,
            positions=[],
            risk_snapshot=ExchangeAccountRiskSnapshot(
                adjusted_equity=Decimal("1000"),
                total_equity=Decimal("1000"),
                available_equity=Decimal("1000"),
                initial_margin_requirement=Decimal("0"),
                maintenance_margin_requirement=Decimal("0"),
                margin_ratio=Decimal("0"),
                notional_usd=Decimal("0"),
            ),
        )
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
            account_provider=account_service,  # type: ignore[arg-type]
            execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=FakeHealthyReconciliationRepo(),  # type: ignore[arg-type]
        )
        risk = RiskEngine(
            settings=settings,
            account_service=account_service,  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda _symbol: Decimal("67000"),
            mode_controller=mode_controller,
            recovery_status_provider=lambda: {
                "only_reduce_required": True,
                "only_reduce_reasons": ["strategy_bundle_recovery_in_progress"],
                "unbundled_open_order_count": 0,
                "bundle_summaries": [
                    {
                        "bundle_id": "bundle_protective_submit",
                        "recoverable": True,
                        "recovery_state": "structured_open_orders",
                    }
                ],
            },
        )

        decision = risk.evaluate_leg_order(
            LegOrderIntent(
                leg_intent_id="leg_open_long_bundle_submit",
                decision_id="leg_open_long_bundle_submit",
                symbol="BTC-USDT-SWAP",
                side="buy",
                pos_side="long",
                action="open",
                quantity=Decimal("0.001"),
                execution_style="taker",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                idempotency_key="leg_open_long_bundle_submit",
                product_type="derivatives",
                margin_mode="cross",
                td_mode="cross",
                target_leverage=2.0,
                exposure_side="long",
                strategy_bundle_id="bundle_protective_submit",
                strategy_leg_role="primary",
            )
        )

        self.assertTrue(decision.approved)
        self.assertFalse(decision.only_reduce_required)
        self.assertEqual(decision.rejection_reasons, [])
        self.assertEqual(decision.leg_only_reduce_constraints, [])

    def test_derivatives_leg_risk_ignores_current_bundle_partial_fill_recovery_for_new_submit(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "derivatives_position_mode": "hedge",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "risk_max_long_notional": 1_000.0,
                "risk_max_short_notional": 1_000.0,
                "risk_max_gross_notional": 2_000.0,
                "risk_max_net_notional": 1_000.0,
                "max_target_leverage": 3.0,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        account_service = FakeAccountService(
            symbol="BTC-USDT-SWAP",
            btc_available=0.0,
            usdt_available=1_000.0,
            positions=[],
            risk_snapshot=ExchangeAccountRiskSnapshot(
                adjusted_equity=Decimal("1000"),
                total_equity=Decimal("1000"),
                available_equity=Decimal("1000"),
                initial_margin_requirement=Decimal("0"),
                maintenance_margin_requirement=Decimal("0"),
                margin_ratio=Decimal("0"),
                notional_usd=Decimal("0"),
            ),
        )
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
            account_provider=account_service,  # type: ignore[arg-type]
            execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=FakeHealthyReconciliationRepo(),  # type: ignore[arg-type]
        )
        risk = RiskEngine(
            settings=settings,
            account_service=account_service,  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda _symbol: Decimal("67000"),
            mode_controller=mode_controller,
            recovery_status_provider=lambda: {
                "only_reduce_required": True,
                "only_reduce_reasons": ["strategy_bundle_recovery_in_progress"],
                "unbundled_open_order_count": 0,
                "bundle_summaries": [
                    {
                        "bundle_id": "bundle_independent_submit",
                        "recoverable": True,
                        "recovery_state": "partial_fill_recovery",
                    }
                ],
            },
        )

        decision = risk.evaluate_leg_order(
            LegOrderIntent(
                leg_intent_id="leg_open_short_bundle_partial_fill",
                decision_id="leg_open_short_bundle_partial_fill",
                symbol="BTC-USDT-SWAP",
                side="sell",
                pos_side="short",
                action="open",
                quantity=Decimal("0.001"),
                execution_style="taker",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                idempotency_key="leg_open_short_bundle_partial_fill",
                product_type="derivatives",
                margin_mode="cross",
                td_mode="cross",
                target_leverage=2.0,
                exposure_side="short",
                strategy_bundle_id="bundle_independent_submit",
                strategy_leg_role="primary",
            )
        )

        self.assertTrue(decision.approved)
        self.assertFalse(decision.only_reduce_required)
        self.assertEqual(decision.rejection_reasons, [])
        self.assertEqual(decision.leg_only_reduce_constraints, [])

    def test_derivatives_leg_risk_keeps_other_bundle_recovery_block_on_new_submit(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "derivatives_position_mode": "hedge",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "risk_max_long_notional": 1_000.0,
                "risk_max_short_notional": 1_000.0,
                "risk_max_gross_notional": 2_000.0,
                "risk_max_net_notional": 1_000.0,
                "max_target_leverage": 3.0,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        account_service = FakeAccountService(
            symbol="BTC-USDT-SWAP",
            btc_available=0.0,
            usdt_available=1_000.0,
            positions=[],
            risk_snapshot=ExchangeAccountRiskSnapshot(
                adjusted_equity=Decimal("1000"),
                total_equity=Decimal("1000"),
                available_equity=Decimal("1000"),
                initial_margin_requirement=Decimal("0"),
                maintenance_margin_requirement=Decimal("0"),
                margin_ratio=Decimal("0"),
                notional_usd=Decimal("0"),
            ),
        )
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
            account_provider=account_service,  # type: ignore[arg-type]
            execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=FakeHealthyReconciliationRepo(),  # type: ignore[arg-type]
        )
        risk = RiskEngine(
            settings=settings,
            account_service=account_service,  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda _symbol: Decimal("67000"),
            mode_controller=mode_controller,
            recovery_status_provider=lambda: {
                "only_reduce_required": True,
                "only_reduce_reasons": ["strategy_bundle_recovery_in_progress"],
                "unbundled_open_order_count": 0,
                "bundle_summaries": [
                    {
                        "bundle_id": "bundle_other_open",
                        "recoverable": True,
                        "recovery_state": "structured_open_orders",
                    }
                ],
            },
        )

        decision = risk.evaluate_leg_order(
            LegOrderIntent(
                leg_intent_id="leg_open_long_other_bundle_blocked",
                decision_id="leg_open_long_other_bundle_blocked",
                symbol="BTC-USDT-SWAP",
                side="buy",
                pos_side="long",
                action="open",
                quantity=Decimal("0.001"),
                execution_style="taker",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                idempotency_key="leg_open_long_other_bundle_blocked",
                product_type="derivatives",
                margin_mode="cross",
                td_mode="cross",
                target_leverage=2.0,
                exposure_side="long",
                strategy_bundle_id="bundle_protective_submit",
                strategy_leg_role="primary",
            )
        )

        self.assertFalse(decision.approved)
        self.assertTrue(decision.only_reduce_required)
        self.assertIn("strategy_bundle_recovery_in_progress", decision.rejection_reasons)
        self.assertIn("leg_only_reduce_mode_active", decision.rejection_reasons)

    def test_derivatives_leg_bundle_risk_blocks_combined_gross_expansion(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "derivatives_position_mode": "hedge",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "risk_max_long_notional": 1_000.0,
                "risk_max_short_notional": 1_000.0,
                "risk_max_gross_notional": 200.0,
                "risk_max_net_notional": 1_000.0,
                "max_target_leverage": 3.0,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        account_service = FakeAccountService(
            symbol="BTC-USDT-SWAP",
            btc_available=0.0,
            usdt_available=1_000.0,
            positions=[],
            risk_snapshot=ExchangeAccountRiskSnapshot(
                adjusted_equity=Decimal("1000"),
                total_equity=Decimal("1000"),
                available_equity=Decimal("1000"),
                initial_margin_requirement=Decimal("0"),
                maintenance_margin_requirement=Decimal("0"),
                margin_ratio=Decimal("0"),
                notional_usd=Decimal("0"),
            ),
        )
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=mode_controller,
            kill_switch=kill_switch,
            market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
            account_provider=account_service,  # type: ignore[arg-type]
            execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
            reconciliation_repo=FakeHealthyReconciliationRepo(),  # type: ignore[arg-type]
        )
        risk = RiskEngine(
            settings=settings,
            account_service=account_service,  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda _symbol: Decimal("67000"),
            mode_controller=mode_controller,
        )

        decision = risk.evaluate_leg_order_bundle(
            [
                LegOrderIntent(
                    leg_intent_id="leg_bundle_long",
                    decision_id="leg_bundle_risk",
                    symbol="BTC-USDT-SWAP",
                    side="buy",
                    pos_side="long",
                    action="open",
                    quantity=Decimal("0.002"),
                    execution_style="taker",
                    order_type="market",
                    urgency="medium",
                    time_in_force="IOC",
                    idempotency_key="leg_bundle_long",
                    product_type="derivatives",
                    margin_mode="cross",
                    td_mode="cross",
                    target_leverage=2.0,
                    exposure_side="long",
                ),
                LegOrderIntent(
                    leg_intent_id="leg_bundle_short",
                    decision_id="leg_bundle_risk",
                    symbol="BTC-USDT-SWAP",
                    side="sell",
                    pos_side="short",
                    action="open",
                    quantity=Decimal("0.002"),
                    execution_style="taker",
                    order_type="market",
                    urgency="medium",
                    time_in_force="IOC",
                    idempotency_key="leg_bundle_short",
                    product_type="derivatives",
                    margin_mode="cross",
                    td_mode="cross",
                    target_leverage=2.0,
                    exposure_side="short",
                ),
            ]
        )

        self.assertFalse(decision.approved)
        self.assertTrue(decision.risk_limit_breached)
        self.assertIn("risk_max_gross_notional_exceeded", decision.rejection_reasons)
        self.assertIsNotNone(decision.projected_derivatives_exposure)
        assert decision.projected_derivatives_exposure is not None
        self.assertEqual(decision.projected_derivatives_exposure.gross_notional, Decimal("268.000"))


if __name__ == "__main__":
    unittest.main()
