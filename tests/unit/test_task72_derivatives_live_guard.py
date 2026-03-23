from __future__ import annotations

import unittest
from decimal import Decimal

from aats.bootstrap.metrics import MetricsRegistry
from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.decision import PositionTarget
from aats.schemas.exchange import (
    ExchangeAccountRiskSnapshot,
    ExchangeAccountSnapshot,
    ExchangeBalance,
    ExchangePosition,
)
from aats.schemas.reconciliation import ReconciliationReport
from aats.services.decision_engine.trigger_policy import DecisionTriggerPolicy
from aats.services.governance_engine.derivatives_live_guard import DerivativesLiveGuardService
from aats.services.governance_engine.health import SystemHealthService
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.governance_engine.mode import RuntimeModeController
from aats.services.governance_engine.risk import RiskEngine
from aats.storage.event_store import InMemoryEventStore
from aats.storage.obligation_repo import InMemoryExecutionObligationRepository


class _StubAccountService:
    def __init__(self, snapshot: ExchangeAccountSnapshot) -> None:
        self._snapshot = snapshot

    def latest_snapshot(self):
        return self._snapshot

    def status(self):
        return {
            "connected": True,
            "fresh": True,
            "ready": True,
            "last_update_ts": self._snapshot.fetched_at,
            "blockers": [],
            "detail": "stub_account",
        }

    def open_order_count(self, symbol: str | None = None) -> int:
        _ = symbol
        return 0

    def instrument_metadata(self, symbol: str):
        _ = symbol
        return None


class _HealthyMarketProvider:
    def status(self):
        return {"ready": True, "connected": True, "fresh": True, "blockers": [], "detail": "ok"}


class _HealthyExecutionProvider:
    def readiness(self):
        return {
            "ready": True,
            "connected": True,
            "fresh": True,
            "blockers": [],
            "exchange_submit_allowed": True,
            "submit_blocked_reasons": [],
            "detail": "ok",
        }


class _HealthyReconciliationRepo:
    def latest(self):
        return ReconciliationReport(
            reconciliation_id="recon_clean",
            as_of_ts=utc_now(),
            product_type="derivatives",
            margin_mode="cross",
            allowed_symbols=["BTC-USDT-SWAP"],
            exchange_comparison_enabled=True,
            order_diff={"reconstructed": {}, "exchange": {}},
            fill_diff={"replayed": {}, "exchange": {}},
            balance_diff={"reconstructed": {}, "exchange": {}},
            position_diff={"stored": {}, "reconstructed": {}, "exchange": {}},
            mismatch_categories=[],
            mismatch_reasons=[],
            safety_impacts=[],
            severity="CLEAN",
        )


def _snapshot(*, initial_margin: str, adjusted_equity: str, mark_price: str, liquidation_price: str) -> ExchangeAccountSnapshot:
    return ExchangeAccountSnapshot(
        account_source="okx",
        fetched_at=utc_now(),
        balances=[ExchangeBalance(currency="USDT", total=Decimal("1000"), available=Decimal("1000"), frozen=Decimal("0"))],
        positions=[
            ExchangePosition(
                instrument_id="BTC-USDT-SWAP",
                symbol="BTC-USDT-SWAP",
                quantity=Decimal("0.02"),
                side="long",
                margin_mode="cross",
                mark_price=Decimal(mark_price),
                liquidation_price=Decimal(liquidation_price),
                margin_allocated=Decimal("320"),
                maintenance_margin=Decimal("120"),
                margin_ratio=Decimal("5.0"),
            )
        ],
        account_mode="portfolio_margin",
        position_mode="net_mode",
        risk_snapshot=ExchangeAccountRiskSnapshot(
            adjusted_equity=Decimal(adjusted_equity),
            total_equity=Decimal(adjusted_equity),
            available_equity=Decimal("600"),
            initial_margin_requirement=Decimal(initial_margin),
            maintenance_margin_requirement=Decimal("120"),
            margin_ratio=Decimal("5.0"),
            notional_usd=Decimal("2000"),
        ),
    )


class TestTask72DerivativesLiveGuard(unittest.TestCase):
    def _settings(self) -> AATSSettings:
        return AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "startup_profile": "derivatives",
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "market_data_backend": "okx",
                "okx_simulated_trading": True,
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "default_symbol": "BTC-USDT-SWAP",
                "max_abs_position_qty": 1.0,
                "max_notional_per_symbol": 100000.0,
            }
        )

    def test_live_guard_marks_only_reduce_without_halting(self) -> None:
        settings = self._settings()
        kill_switch = KillSwitch()
        service = DerivativesLiveGuardService(
            settings=settings,
            kill_switch=kill_switch,
            account_service=_StubAccountService(
                _snapshot(
                    initial_margin="720",
                    adjusted_equity="1000",
                    mark_price="70000",
                    liquidation_price="61000",
                )
            ),
            event_store=InMemoryEventStore(),
            metrics=MetricsRegistry(),
        )

        payload = service.evaluate_now()

        self.assertEqual(payload["status"], "warning")
        self.assertTrue(payload["only_reduce_required"])
        self.assertIn("derivatives_margin_usage_requires_only_reduce", payload["only_reduce_reasons"])
        self.assertFalse(payload["auto_halt_required"])
        self.assertFalse(kill_switch.halted)

    def test_live_guard_auto_halts_when_liquidation_gap_is_too_small(self) -> None:
        settings = self._settings()
        kill_switch = KillSwitch()
        service = DerivativesLiveGuardService(
            settings=settings,
            kill_switch=kill_switch,
            account_service=_StubAccountService(
                _snapshot(
                    initial_margin="860",
                    adjusted_equity="1000",
                    mark_price="70000",
                    liquidation_price="65000",
                )
            ),
            event_store=InMemoryEventStore(),
            metrics=MetricsRegistry(),
        )

        payload = service.evaluate_now()

        self.assertEqual(payload["status"], "critical")
        self.assertTrue(payload["auto_halt_required"])
        self.assertIn("derivatives_liquidation_proximity_auto_halt", payload["auto_halt_reasons"])
        self.assertTrue(kill_switch.halted)
        self.assertEqual(kill_switch.status()["reason"], "derivatives_live_risk_auto_halt")

    def test_risk_engine_respects_runtime_guard_only_reduce_state(self) -> None:
        settings = self._settings()
        kill_switch = KillSwitch()
        controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        account_service = _StubAccountService(
            _snapshot(
                initial_margin="720",
                adjusted_equity="1000",
                mark_price="70000",
                liquidation_price="61000",
            )
        )
        runtime_guard = DerivativesLiveGuardService(
            settings=settings,
            kill_switch=kill_switch,
            account_service=account_service,
            event_store=InMemoryEventStore(),
            metrics=MetricsRegistry(),
        )
        runtime_guard.evaluate_now()
        health_service = SystemHealthService(
            settings=settings,
            mode_controller=controller,
            kill_switch=kill_switch,
            market_provider=_HealthyMarketProvider(),
            account_provider=account_service,
            execution_provider=_HealthyExecutionProvider(),
            reconciliation_repo=_HealthyReconciliationRepo(),
        )
        risk = RiskEngine(
            settings=settings,
            account_service=account_service,  # type: ignore[arg-type]
            health_service=health_service,
            trigger_policy=DecisionTriggerPolicy(settings=settings),
            price_provider=lambda symbol: Decimal("70000"),
            mode_controller=controller,
            obligation_repo=InMemoryExecutionObligationRepository(),
            reconciliation_repo=_HealthyReconciliationRepo(),
            live_runtime_guard_provider=runtime_guard,
        )

        decision = risk.evaluate(
            PositionTarget(
                decision_id="decision_guard_reduce_only",
                symbol="BTC-USDT-SWAP",
                current_position_qty=Decimal("0.02"),
                target_position_qty=Decimal("0.04"),
                delta_position_qty=Decimal("0.02"),
                current_notional=Decimal("1400"),
                target_notional=Decimal("2800"),
                rebalance_reason="runtime_guard_test",
                urgency="medium",
                max_slippage_tolerance_bps=20,
                source_mix={"baseline": 1.0},
                decision_expiry_ts=utc_now(),
                product_type="derivatives",
                current_exposure_side="long",
                target_exposure_side="long",
                position_intent="open_long",
                margin_mode="cross",
                target_leverage=3.0,
            )
        )

        self.assertTrue(decision.only_reduce_required)
        self.assertIn("derivatives_margin_usage_requires_only_reduce", decision.constraints_applied)
        self.assertFalse(decision.approved)
        self.assertIn("only_reduce_mode_active", decision.rejection_reasons)


if __name__ == "__main__":
    unittest.main()
