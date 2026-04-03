from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase

from aats.bootstrap.settings import AATSSettings
from aats.schemas.decision import BaselineAssessment
from aats.schemas.strategy_runtime import StrategyCandidate, StrategySleeveIntent
from aats.services.strategy_engines.auto_parallel import StrategySleeveAutoController


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
            "default_symbol": "BTC-USDT",
            "allowed_symbols": ("BTC-USDT",),
            **overrides,
        }
    )


def _baseline(volatility_target_scale: float = 1.0) -> BaselineAssessment:
    return BaselineAssessment(
        decision_id="decision_auto_parallel",
        symbol="BTC-USDT",
        regime="range",
        direction_bias="flat",
        trend_strength=0.1,
        volatility_state="normal",
        confidence=0.6,
        volatility_target_scale=volatility_target_scale,
        holding_horizon="swing",
        engine_version="test",
    )


def _candidate(**overrides) -> StrategyCandidate:
    payload = {
        "family": "dca",
        "state": "ready",
        "enabled": True,
        "selectable": True,
        "execution_compatible": True,
        "route_action": "override_target",
        "family_action": "hold_family",
        "headline": "test candidate",
        "target_position_qty": Decimal("0.25"),
        "delta_position_qty": Decimal("0.25"),
        "score": 0.7,
        "confidence": 0.8,
        "metrics": {"current_price": Decimal("100")},
    }
    payload.update(overrides)
    return StrategyCandidate(**payload)


def _intent(**overrides) -> StrategySleeveIntent:
    payload = {
        "decision_id": "decision_auto_parallel",
        "family": "dca",
        "strategy_sleeve_id": "sleeve_dca",
        "state": "ready",
        "symbol": "BTC-USDT",
        "product_type": "spot",
        "margin_mode": "cash",
        "inventory_policy": "account_net_inventory",
        "route_action": "override_target",
        "family_action": "hold_family",
        "headline": "test intent",
        "selectable": True,
        "execution_compatible": True,
        "current_position_qty": Decimal("0"),
        "target_position_qty": Decimal("0.25"),
        "delta_position_qty": Decimal("0.25"),
        "account_current_position_qty": Decimal("0"),
        "target_notional": Decimal("25"),
        "metrics": {"current_price": Decimal("100")},
    }
    payload.update(overrides)
    return StrategySleeveIntent(**payload)


class TestAutoParallelOrchestration(TestCase):
    def test_permission_denied_composes_advisory_only_end_to_end(self) -> None:
        controller = StrategySleeveAutoController(
            settings=_settings(strategy_sleeve_auto_execution_enabled=False)
        )

        controlled_candidates, controlled_intents, decisions = controller.apply(
            baseline=_baseline(),
            candidates_by_family={"dca": _candidate()},
            sleeve_intents=[_intent()],
        )

        decision = decisions[0]
        controlled_intent = controlled_intents[0]
        controlled_candidate = controlled_candidates["dca"]

        self.assertFalse(decision.approved_for_execution)
        self.assertFalse(decision.automatic_enabled)
        self.assertEqual(decision.execution_control_mode, "permission_denied")
        self.assertEqual(decision.execution_behavior, "advisory_only")
        self.assertEqual(decision.compatibility["legacy_automation_state"], "paused")
        self.assertIn("coarse projection", decision.compatibility["legacy_automation_state_note"])
        self.assertEqual(decision.automation_state, decision.compatibility["legacy_automation_state"])
        self.assertFalse(controlled_intent.execution_compatible)
        self.assertEqual(controlled_intent.route_action, "advisory_only")
        self.assertEqual(controlled_candidate.route_action, "advisory_only")
        self.assertEqual(controlled_intent.control_trace["execution_behavior"], "advisory_only")

    def test_approved_candidate_remains_execution_compatible_end_to_end(self) -> None:
        controller = StrategySleeveAutoController(settings=_settings())

        controlled_candidates, controlled_intents, decisions = controller.apply(
            baseline=_baseline(),
            candidates_by_family={"dca": _candidate()},
            sleeve_intents=[_intent()],
        )

        decision = decisions[0]
        controlled_intent = controlled_intents[0]
        controlled_candidate = controlled_candidates["dca"]

        self.assertTrue(decision.approved_for_execution)
        self.assertEqual(decision.execution_control_mode, "approved")
        self.assertEqual(decision.execution_behavior, "execute_target")
        self.assertEqual(decision.compatibility["legacy_automation_state"], "active")
        self.assertEqual(
            decision.compatibility["legacy_automation_projection"]["source_execution_control_mode"],
            "approved",
        )
        self.assertEqual(decision.automation_state, decision.compatibility["legacy_automation_state"])
        self.assertTrue(decision.automatic_enabled)
        self.assertTrue(controlled_intent.execution_compatible)
        self.assertTrue(controlled_candidate.execution_compatible)
        self.assertEqual(controlled_intent.route_action, "override_target")
        self.assertEqual(controlled_intent.control_trace["execution_control_mode"], "approved")
        self.assertTrue(controlled_intent.control_trace["permission"]["state_runtime_supported"])
        self.assertTrue(controlled_intent.control_trace["permission"]["candidate_state_runtime_supported"])

    def test_approved_budget_zero_is_explicitly_suppressed_after_approval(self) -> None:
        controller = StrategySleeveAutoController(settings=_settings())
        controller._latest_reconciliation = lambda: SimpleNamespace(  # type: ignore[method-assign]
            halt_required=True,
            resume_blocking=False,
            only_reduce_required=False,
            review_required=False,
            severity="WARNING",
        )

        _, controlled_intents, decisions = controller.apply(
            baseline=_baseline(),
            candidates_by_family={"dca": _candidate()},
            sleeve_intents=[_intent()],
        )

        decision = decisions[0]
        controlled_intent = controlled_intents[0]

        self.assertTrue(decision.approved_for_execution)
        self.assertTrue(decision.budget_zero_suppressed)
        self.assertEqual(decision.execution_control_mode, "budget_zero_suppressed")
        self.assertEqual(decision.execution_behavior, "suppressed_after_approval")
        self.assertEqual(decision.automation_state, "contracted")
        self.assertEqual(decision.compatibility["legacy_automation_state"], "contracted")
        self.assertEqual(decision.automation_state, decision.compatibility["legacy_automation_state"])
        self.assertTrue(decision.automatic_enabled)
        self.assertTrue(controlled_intent.execution_compatible)
        self.assertEqual(controlled_intent.route_action, "advisory_only")
        self.assertTrue(controlled_intent.control_trace["budget"]["budget_zero_suppressed"])

    def test_protective_override_continues_execution_when_auto_execution_disabled(self) -> None:
        controller = StrategySleeveAutoController(
            settings=_settings(strategy_sleeve_auto_execution_enabled=False)
        )

        _, controlled_intents, decisions = controller.apply(
            baseline=_baseline(),
            candidates_by_family={
                "dca": _candidate(
                    target_position_qty=Decimal("0.1"),
                    delta_position_qty=Decimal("-0.1"),
                )
            },
            sleeve_intents=[
                _intent(
                    current_position_qty=Decimal("0.2"),
                    target_position_qty=Decimal("0.1"),
                    delta_position_qty=Decimal("-0.1"),
                    account_current_position_qty=Decimal("0.2"),
                    target_notional=Decimal("10"),
                )
            ],
        )

        decision = decisions[0]
        controlled_intent = controlled_intents[0]

        self.assertTrue(decision.approved_for_execution)
        self.assertEqual(decision.execution_control_mode, "protective_override")
        self.assertEqual(decision.execution_behavior, "protective_execute")
        self.assertEqual(decision.automation_state, "protective_only")
        self.assertEqual(decision.compatibility["legacy_automation_state"], "protective_only")
        self.assertEqual(decision.automation_state, decision.compatibility["legacy_automation_state"])
        self.assertTrue(decision.automatic_enabled)
        self.assertTrue(controlled_intent.execution_compatible)
        self.assertEqual(controlled_intent.control_trace["execution_behavior"], "protective_execute")

    def test_execution_incompatible_candidate_is_denied_before_it_can_look_approved(self) -> None:
        controller = StrategySleeveAutoController(settings=_settings())

        _, controlled_intents, decisions = controller.apply(
            baseline=_baseline(),
            candidates_by_family={"dca": _candidate(execution_compatible=False)},
            sleeve_intents=[_intent(execution_compatible=False)],
        )

        decision = decisions[0]
        controlled_intent = controlled_intents[0]

        self.assertFalse(decision.approved_for_execution)
        self.assertEqual(decision.permission_mode, "unsupported")
        self.assertIn("candidate_execution_incompatible", decision.permission_reason_codes)
        self.assertFalse(decision.automatic_enabled)
        self.assertFalse(controlled_intent.execution_compatible)
        self.assertFalse(controlled_intent.control_trace["permission"]["candidate_execution_compatible"])
        self.assertTrue(controlled_intent.control_trace["permission"]["state_runtime_supported"])
        self.assertTrue(controlled_intent.control_trace["permission"]["candidate_state_runtime_supported"])
