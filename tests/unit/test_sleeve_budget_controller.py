from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase

from aats.bootstrap.settings import AATSSettings
from aats.schemas.decision import BaselineAssessment
from aats.services.strategy_engines.sleeve_budget_controller import SleeveBudgetController
from aats.services.strategy_engines.sleeve_routing_models import RawSleeveCandidateInputs


def _settings(**overrides) -> AATSSettings:
    return AATSSettings.model_validate(
        {
            "config_profile": "local_demo",
            "mode": "paper_live",
            "market_data_backend": "demo",
            "execution_backend": "paper",
            "account_backend": "disabled",
            "account_read_enabled": False,
            "storage_mode": "memory",
            **overrides,
        }
    )


def _baseline(volatility_target_scale: float = 1.0) -> BaselineAssessment:
    return BaselineAssessment(
        decision_id="decision_budget",
        symbol="BTC-USDT",
        regime="range",
        direction_bias="flat",
        trend_strength=0.1,
        volatility_state="normal",
        confidence=0.5,
        volatility_target_scale=volatility_target_scale,
        holding_horizon="swing",
        engine_version="test",
    )


def _raw(**overrides) -> RawSleeveCandidateInputs:
    payload = {
        "family": "dca",
        "strategy_sleeve_id": "sleeve_dca",
        "symbol": "BTC-USDT",
        "current_position_qty": Decimal("0"),
        "target_position_qty": Decimal("0.25"),
        "delta_position_qty": Decimal("0.25"),
        "account_current_position_qty": Decimal("0"),
        "target_notional": Decimal("25"),
        "route_action": "override_target",
        "requested_legs": tuple(),
        "metrics": {},
        "candidate_state": "ready",
        "candidate_enabled": True,
        "candidate_selectable": True,
        "candidate_execution_compatible": True,
        "candidate_score": 0.7,
        "candidate_confidence": 0.8,
        "state_runtime_supported": True,
        "active_inventory": False,
        "current_inventory_notional": Decimal("0"),
        "protective_intent": False,
    }
    payload.update(overrides)
    return RawSleeveCandidateInputs(**payload)


class TestSleeveBudgetController(TestCase):
    def test_no_contraction_keeps_scale_one(self) -> None:
        controller = SleeveBudgetController(_settings())

        decision = controller.evaluate(
            raw=_raw(),
            baseline=_baseline(1.0),
            recent_net_pnl=Decimal("0"),
            latest_reconciliation=None,
        )

        self.assertEqual(decision.effective_scale, Decimal("1"))
        self.assertFalse(decision.budget_zero_suppressed)

    def test_pnl_contraction_reduces_scale(self) -> None:
        controller = SleeveBudgetController(_settings(strategy_sleeve_auto_soft_loss_usdt=10.0))

        decision = controller.evaluate(
            raw=_raw(),
            baseline=_baseline(1.0),
            recent_net_pnl=Decimal("-5"),
            latest_reconciliation=None,
        )

        self.assertLess(decision.effective_scale, Decimal("1"))
        self.assertIn("pnl_contraction_active", decision.contraction_reason_codes)

    def test_reconciliation_contraction_reduces_scale(self) -> None:
        controller = SleeveBudgetController(
            _settings(strategy_sleeve_auto_reconciliation_contraction_multiplier=0.4)
        )
        reconciliation = SimpleNamespace(
            halt_required=False,
            resume_blocking=False,
            only_reduce_required=True,
            review_required=False,
            severity="WARNING",
        )

        decision = controller.evaluate(
            raw=_raw(active_inventory=True, current_inventory_notional=Decimal("15")),
            baseline=_baseline(1.0),
            recent_net_pnl=Decimal("0"),
            latest_reconciliation=reconciliation,
        )

        self.assertEqual(decision.effective_scale, Decimal("0.4"))
        self.assertIn("reconciliation_contraction_active", decision.contraction_reason_codes)

    def test_multiple_contractions_take_min_effective_scale(self) -> None:
        controller = SleeveBudgetController(
            _settings(
                strategy_sleeve_auto_reconciliation_contraction_multiplier=0.6,
                strategy_sleeve_auto_soft_loss_usdt=10.0,
            )
        )
        reconciliation = SimpleNamespace(
            halt_required=False,
            resume_blocking=False,
            only_reduce_required=True,
            review_required=False,
            severity="WARNING",
        )

        decision = controller.evaluate(
            raw=_raw(),
            baseline=_baseline(0.7),
            recent_net_pnl=Decimal("-8"),
            latest_reconciliation=reconciliation,
        )

        self.assertLessEqual(decision.effective_scale, Decimal("0.6"))
        self.assertLessEqual(decision.effective_scale, Decimal("0.7"))

    def test_budget_can_contract_to_zero(self) -> None:
        controller = SleeveBudgetController(
            _settings(
                strategy_sleeve_auto_soft_loss_usdt=10.0,
                strategy_sleeve_auto_hard_loss_usdt=20.0,
            )
        )

        decision = controller.evaluate(
            raw=_raw(family="dca"),
            baseline=_baseline(1.0),
            recent_net_pnl=Decimal("-25"),
            latest_reconciliation=None,
        )

        self.assertEqual(decision.effective_scale, Decimal("0"))
        self.assertTrue(decision.budget_zero_suppressed)

    def test_below_min_tradeable_step_sets_zero_suppression_reason(self) -> None:
        controller = SleeveBudgetController(_settings())

        decision = controller.evaluate(
            raw=_raw(delta_position_qty=Decimal("0.0000000000001"), target_position_qty=Decimal("0.0000000000001")),
            baseline=_baseline(0.5),
            recent_net_pnl=Decimal("0"),
            latest_reconciliation=None,
        )

        self.assertTrue(decision.budget_zero_suppressed)
        self.assertIn("scale_below_min_tradeable_step", decision.contraction_reason_codes)
