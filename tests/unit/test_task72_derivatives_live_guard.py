from __future__ import annotations

from datetime import timedelta
import unittest
from decimal import Decimal
from unittest.mock import patch

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


class _UnreadyAccountService(_StubAccountService):
    def __init__(self, snapshot: ExchangeAccountSnapshot) -> None:
        super().__init__(snapshot)
        self.latest_snapshot_calls = 0

    def latest_snapshot(self):
        self.latest_snapshot_calls += 1
        return self._snapshot

    def status(self):
        return {
            "connected": True,
            "fresh": False,
            "ready": False,
            "last_error": "okx_account_refresh_failed",
            "blockers": ["account_state_stale"],
            "detail": "okx_account_refresh_failed",
        }


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


def _snapshot(
    *,
    initial_margin: str,
    adjusted_equity: str,
    mark_price: str,
    liquidation_price: str,
    quantity: str = "0.02",
    side: str = "long",
) -> ExchangeAccountSnapshot:
    return ExchangeAccountSnapshot(
        account_source="okx",
        fetched_at=utc_now(),
        balances=[ExchangeBalance(currency="USDT", total=Decimal("1000"), available=Decimal("1000"), frozen=Decimal("0"))],
        positions=[
            ExchangePosition(
                instrument_id="BTC-USDT-SWAP",
                symbol="BTC-USDT-SWAP",
                quantity=Decimal(quantity),
                side=side,
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
        self.assertEqual(payload["current_derivatives_exposure"]["long_notional"], Decimal("1400"))
        self.assertEqual(payload["current_derivatives_exposure"]["gross_notional"], Decimal("1400"))
        self.assertEqual(payload["current_derivatives_exposure"]["net_notional"], Decimal("1400"))

    def test_live_guard_enters_grace_mode_before_only_reduce_when_risk_snapshot_temporarily_missing(self) -> None:
        settings = self._settings()
        snapshot = _snapshot(
            initial_margin="720",
            adjusted_equity="1000",
            mark_price="70000",
            liquidation_price="42000",
        ).model_copy(update={"risk_snapshot": None})
        kill_switch = KillSwitch()
        service = DerivativesLiveGuardService(
            settings=settings,
            kill_switch=kill_switch,
            account_service=_StubAccountService(snapshot),
            event_store=InMemoryEventStore(),
            metrics=MetricsRegistry(),
        )

        frozen_now = utc_now()
        with patch("aats.services.governance_engine.derivatives_live_guard.utc_now", return_value=frozen_now):
            payload = service.evaluate_now()

        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["risk_snapshot_stage"], "grace")
        self.assertFalse(payload["only_reduce_required"])
        self.assertFalse(payload["auto_halt_required"])
        self.assertIn("derivatives_risk_snapshot_missing_grace_active", payload["warnings"])

    def test_live_guard_escalates_missing_risk_snapshot_to_only_reduce_after_grace(self) -> None:
        settings = self._settings()
        snapshot = _snapshot(
            initial_margin="720",
            adjusted_equity="1000",
            mark_price="70000",
            liquidation_price="42000",
        ).model_copy(update={"risk_snapshot": None})
        kill_switch = KillSwitch()
        service = DerivativesLiveGuardService(
            settings=settings,
            kill_switch=kill_switch,
            account_service=_StubAccountService(snapshot),
            event_store=InMemoryEventStore(),
            metrics=MetricsRegistry(),
        )

        start = utc_now()
        with patch("aats.services.governance_engine.derivatives_live_guard.utc_now", return_value=start):
            service.evaluate_now()
        with patch(
            "aats.services.governance_engine.derivatives_live_guard.utc_now",
            return_value=start + timedelta(seconds=settings.derivatives_risk_snapshot_grace_seconds + 5),
        ):
            payload = service.evaluate_now()

        self.assertEqual(payload["status"], "warning")
        self.assertEqual(payload["risk_snapshot_stage"], "only_reduce")
        self.assertTrue(payload["only_reduce_required"])
        self.assertIn("derivatives_risk_snapshot_missing_requires_only_reduce", payload["only_reduce_reasons"])
        self.assertFalse(payload["auto_halt_required"])

    def test_live_guard_uses_position_margin_fallback_when_okx_risk_payload_lacks_top_level_imr(self) -> None:
        settings = self._settings()
        snapshot = _snapshot(
            initial_margin="320",
            adjusted_equity="1000",
            mark_price="70000",
            liquidation_price="42000",
        ).model_copy(
            update={
                "risk_snapshot": ExchangeAccountRiskSnapshot(
                    adjusted_equity=None,
                    total_equity=None,
                    available_equity=None,
                    initial_margin_requirement=None,
                    maintenance_margin_requirement=None,
                    margin_ratio=None,
                    notional_usd=Decimal("120.5187115716"),
                    raw={
                        "balData": [
                            {"ccy": "USDT", "eq": "201.0016337876877", "availEq": "201.0016337876877"},
                        ],
                        "posData": [
                            {"instId": "BTC-USDT-SWAP", "notionalUsd": "120.5187115716000000", "pos": "0.17"},
                        ],
                    },
                )
            }
        )
        service = DerivativesLiveGuardService(
            settings=settings,
            kill_switch=KillSwitch(),
            account_service=_StubAccountService(snapshot),
            event_store=InMemoryEventStore(),
            metrics=MetricsRegistry(),
        )

        payload = service.evaluate_now()

        self.assertTrue(payload["risk_snapshot_available"])
        self.assertEqual(payload["risk_snapshot_stage"], "healthy")
        self.assertEqual(payload["status"], "healthy")
        self.assertFalse(payload["auto_halt_required"])
        self.assertIsNotNone(payload["current_initial_margin_usage_fraction"])

    def test_live_guard_rejects_stale_snapshot_when_account_status_not_ready(self) -> None:
        settings = self._settings()
        account_service = _UnreadyAccountService(
            _snapshot(
                initial_margin="320",
                adjusted_equity="1000",
                mark_price="70000",
                liquidation_price="42000",
            )
        )
        service = DerivativesLiveGuardService(
            settings=settings,
            kill_switch=KillSwitch(),
            account_service=account_service,
            event_store=InMemoryEventStore(),
            metrics=MetricsRegistry(),
        )

        payload = service.evaluate_now()

        self.assertEqual(payload["status"], "unavailable")
        self.assertFalse(payload["ready"])
        self.assertFalse(payload["fresh"])
        self.assertFalse(payload["risk_snapshot_available"])
        self.assertEqual(payload["risk_snapshot_stage"], "unavailable")
        self.assertTrue(payload["only_reduce_required"])
        self.assertIn("account_state_unready", payload["only_reduce_reasons"])
        self.assertIn("account_state_stale", payload["blockers"])
        self.assertIn("account_state_unready", payload["blockers"])
        self.assertIsNone(payload["current_initial_margin_usage_fraction"])
        self.assertEqual(account_service.latest_snapshot_calls, 0)

    def test_live_guard_auto_halts_when_risk_snapshot_missing_for_too_long(self) -> None:
        settings = self._settings()
        snapshot = _snapshot(
            initial_margin="720",
            adjusted_equity="1000",
            mark_price="70000",
            liquidation_price="42000",
        ).model_copy(update={"risk_snapshot": None})
        kill_switch = KillSwitch()
        service = DerivativesLiveGuardService(
            settings=settings,
            kill_switch=kill_switch,
            account_service=_StubAccountService(snapshot),
            event_store=InMemoryEventStore(),
            metrics=MetricsRegistry(),
        )

        start = utc_now()
        with patch("aats.services.governance_engine.derivatives_live_guard.utc_now", return_value=start):
            service.evaluate_now()
        with patch(
            "aats.services.governance_engine.derivatives_live_guard.utc_now",
            return_value=start + timedelta(seconds=settings.derivatives_risk_snapshot_auto_halt_after_seconds + 5),
        ):
            payload = service.evaluate_now()

        self.assertEqual(payload["status"], "critical")
        self.assertEqual(payload["risk_snapshot_stage"], "auto_halt")
        self.assertTrue(payload["auto_halt_required"])
        self.assertIn("derivatives_risk_snapshot_missing_auto_halt", payload["auto_halt_reasons"])
        self.assertTrue(kill_switch.halted)

    def test_reset_transient_risk_snapshot_state_clears_missing_snapshot_auto_halt_timer(self) -> None:
        settings = self._settings()
        snapshot = _snapshot(
            initial_margin="720",
            adjusted_equity="1000",
            mark_price="70000",
            liquidation_price="42000",
        ).model_copy(update={"risk_snapshot": None})
        kill_switch = KillSwitch()
        service = DerivativesLiveGuardService(
            settings=settings,
            kill_switch=kill_switch,
            account_service=_StubAccountService(snapshot),
            event_store=InMemoryEventStore(),
            metrics=MetricsRegistry(),
        )

        start = utc_now()
        with patch("aats.services.governance_engine.derivatives_live_guard.utc_now", return_value=start):
            service.evaluate_now()
        with patch(
            "aats.services.governance_engine.derivatives_live_guard.utc_now",
            return_value=start + timedelta(seconds=settings.derivatives_risk_snapshot_auto_halt_after_seconds + 5),
        ):
            halted_payload = service.evaluate_now()

        self.assertTrue(halted_payload["auto_halt_required"])

        service.reset_transient_risk_snapshot_state(reason="operator_rebaseline")
        with patch(
            "aats.services.governance_engine.derivatives_live_guard.utc_now",
            return_value=start + timedelta(seconds=settings.derivatives_risk_snapshot_auto_halt_after_seconds + 6),
        ):
            recovered_payload = service.evaluate_now()

        self.assertEqual(recovered_payload["risk_snapshot_stage"], "grace")
        self.assertFalse(recovered_payload["auto_halt_required"])
        self.assertFalse(recovered_payload["only_reduce_required"])
        self.assertIn("derivatives_risk_snapshot_missing_grace_active", recovered_payload["warnings"])

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

    def test_live_guard_uses_position_side_for_short_positions_even_when_quantity_is_positive(self) -> None:
        settings = self._settings()
        kill_switch = KillSwitch()
        service = DerivativesLiveGuardService(
            settings=settings,
            kill_switch=kill_switch,
            account_service=_StubAccountService(
                _snapshot(
                    initial_margin="320",
                    adjusted_equity="1000",
                    mark_price="66838.5",
                    liquidation_price="130646.40339573975",
                    quantity="0.0018",
                    side="short",
                )
            ),
            event_store=InMemoryEventStore(),
            metrics=MetricsRegistry(),
        )

        payload = service.evaluate_now()

        self.assertEqual(payload["status"], "healthy")
        self.assertFalse(payload["only_reduce_required"])
        self.assertFalse(payload["auto_halt_required"])
        self.assertFalse(kill_switch.halted)
        self.assertGreater(payload["nearest_liquidation_gap_ratio"], Decimal("0.9"))
        self.assertEqual(payload["closest_position"]["pos_side"], "short")

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
                target_leverage=1.0,
            )
        )

        self.assertTrue(decision.only_reduce_required)
        self.assertIn("derivatives_margin_usage_requires_only_reduce", decision.constraints_applied)
        self.assertFalse(decision.approved)
        self.assertIn("only_reduce_mode_active", decision.rejection_reasons)

    def test_risk_engine_contracts_budget_during_risk_snapshot_grace_without_only_reduce(self) -> None:
        settings = self._settings()
        snapshot = _snapshot(
            initial_margin="720",
            adjusted_equity="1000",
            mark_price="70000",
            liquidation_price="42000",
        ).model_copy(update={"risk_snapshot": None})
        kill_switch = KillSwitch()
        controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        account_service = _StubAccountService(snapshot)
        runtime_guard = DerivativesLiveGuardService(
            settings=settings,
            kill_switch=kill_switch,
            account_service=account_service,
            event_store=InMemoryEventStore(),
            metrics=MetricsRegistry(),
        )
        frozen_now = utc_now()
        with patch("aats.services.governance_engine.derivatives_live_guard.utc_now", return_value=frozen_now):
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
                decision_id="decision_guard_grace",
                symbol="BTC-USDT-SWAP",
                current_position_qty=Decimal("0.006"),
                target_position_qty=Decimal("0.008"),
                delta_position_qty=Decimal("0.002"),
                current_notional=Decimal("420"),
                target_notional=Decimal("560"),
                rebalance_reason="runtime_guard_test",
                urgency="medium",
                max_slippage_tolerance_bps=20,
                source_mix={"baseline": 1.0},
                decision_expiry_ts=frozen_now,
                product_type="derivatives",
                current_exposure_side="long",
                target_exposure_side="long",
                position_intent="open_long",
                margin_mode="cross",
                target_leverage=1.0,
            )
        )

        self.assertTrue(decision.approved)
        self.assertFalse(decision.only_reduce_required)
        self.assertLess(decision.risk_budget_multiplier, Decimal("1"))
        self.assertLess(decision.execution_aggressiveness_multiplier, Decimal("1"))
        self.assertIn("risk_budget_multiplier_applied", decision.constraints_applied)
        self.assertIn("execution_aggressiveness_contracted", decision.constraints_applied)


if __name__ == "__main__":
    unittest.main()
