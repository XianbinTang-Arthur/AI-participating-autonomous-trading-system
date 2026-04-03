from __future__ import annotations

from decimal import Decimal
from unittest import TestCase

from aats.schemas.strategy_runtime import StrategyLegIntent
from aats.services.strategy_engines.sleeve_routing_composer import SleeveRoutingComposer
from aats.services.strategy_engines.sleeve_routing_models import (
    BudgetControlDecision,
    ExecutionPermissionDecision,
    RawSleeveCandidateInputs,
)


def _leg(delta_qty: str = "0.25") -> StrategyLegIntent:
    return StrategyLegIntent(
        symbol="BTC-USDT",
        product_type="spot",
        side="buy",
        current_position_qty=Decimal("0"),
        target_position_qty=Decimal(delta_qty),
        delta_position_qty=Decimal(delta_qty),
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
        "requested_legs": (_leg(),),
        "metrics": {},
        "candidate_state": "ready",
        "candidate_enabled": True,
        "candidate_selectable": True,
        "candidate_execution_compatible": True,
        "candidate_score": 0.7,
        "candidate_confidence": 0.8,
        "candidate_state_runtime_supported": True,
        "active_inventory": False,
        "current_inventory_notional": Decimal("0"),
        "protective_intent": False,
    }
    payload.update(overrides)
    return RawSleeveCandidateInputs(**payload)


def _permission(**overrides) -> ExecutionPermissionDecision:
    payload = {
        "configured_auto_execution_enabled": True,
        "candidate_state_runtime_supported": True,
        "candidate_enabled": True,
        "candidate_execution_compatible": True,
        "protective_intent": False,
        "approved_for_execution": True,
        "blocks_non_protective_execution": False,
        "permission_mode": "approved",
        "reason_codes": ("approved_for_non_protective_execution",),
        "human_summary": "ok",
    }
    payload.update(overrides)
    return ExecutionPermissionDecision(**payload)


def _budget(**overrides) -> BudgetControlDecision:
    payload = {
        "requested_delta_position_qty": Decimal("0.25"),
        "requested_target_position_qty": Decimal("0.25"),
        "base_scale": Decimal("1"),
        "effective_scale": Decimal("1"),
        "pnl_contraction_multiplier": Decimal("1"),
        "reconciliation_contraction_multiplier": Decimal("1"),
        "capacity_contraction_multiplier": Decimal("1"),
        "custom_contraction_multiplier": Decimal("1"),
        "scaled_delta_position_qty": Decimal("0.25"),
        "scaled_target_position_qty": Decimal("0.25"),
        "scaled_legs": (_leg(),),
        "contraction_reason_codes": ("no_budget_contraction",),
        "scale_trace": ("base_scale=1",),
        "budget_zero_suppressed": False,
    }
    payload.update(overrides)
    return BudgetControlDecision(**payload)


class TestSleeveRoutingComposer(TestCase):
    def test_permission_denied_without_inventory_composes_advisory_only(self) -> None:
        composer = SleeveRoutingComposer()

        decision = composer.compose(
            raw=_raw(active_inventory=False),
            permission=_permission(approved_for_execution=False, permission_mode="advisory_only"),
            budget=_budget(),
        )

        self.assertEqual(decision.route_action, "advisory_only")
        self.assertEqual(decision.execution_control_mode, "permission_denied")
        self.assertEqual(decision.execution_behavior, "advisory_only")
        self.assertEqual(decision.composed_delta_position_qty, Decimal("0"))
        self.assertEqual(
            decision.composed_legs[0].note,
            "composition:permission_denied:advisory_only",
        )

    def test_permission_denied_with_inventory_composes_hold_current(self) -> None:
        composer = SleeveRoutingComposer()

        decision = composer.compose(
            raw=_raw(active_inventory=True, current_inventory_notional=Decimal("10")),
            permission=_permission(approved_for_execution=False, permission_mode="hold_current"),
            budget=_budget(),
        )

        self.assertEqual(decision.route_action, "hold_current")
        self.assertEqual(decision.execution_control_mode, "permission_denied")
        self.assertEqual(decision.execution_behavior, "hold_current")
        self.assertEqual(
            decision.composed_legs[0].note,
            "composition:permission_denied:hold_current",
        )

    def test_approved_with_positive_scale_composes_override_target(self) -> None:
        composer = SleeveRoutingComposer()

        decision = composer.compose(
            raw=_raw(),
            permission=_permission(),
            budget=_budget(
                effective_scale=Decimal("0.5"),
                scaled_delta_position_qty=Decimal("0.125"),
                scaled_target_position_qty=Decimal("0.125"),
                scaled_legs=(_leg("0.125"),),
                contraction_reason_codes=("pnl_contraction_active",),
            ),
        )

        self.assertEqual(decision.route_action, "override_target")
        self.assertEqual(decision.execution_control_mode, "approved")
        self.assertEqual(decision.execution_behavior, "execute_target")
        self.assertEqual(decision.composed_delta_position_qty, Decimal("0.125"))
        self.assertEqual(
            decision.composed_legs[0].note,
            "composition:approved:override_target",
        )

    def test_approved_with_zero_budget_preserves_permission_but_suppresses_delta(self) -> None:
        composer = SleeveRoutingComposer()

        decision = composer.compose(
            raw=_raw(),
            permission=_permission(),
            budget=_budget(
                effective_scale=Decimal("0"),
                scaled_delta_position_qty=Decimal("0"),
                scaled_target_position_qty=Decimal("0"),
                scaled_legs=(_leg("0"),),
                contraction_reason_codes=("budget_contracted_to_zero",),
                budget_zero_suppressed=True,
            ),
        )

        self.assertTrue(decision.approved_for_execution)
        self.assertTrue(decision.budget_zero_suppressed)
        self.assertEqual(decision.execution_control_mode, "budget_zero_suppressed")
        self.assertEqual(decision.execution_behavior, "suppressed_after_approval")
        self.assertEqual(decision.route_action, "advisory_only")
        self.assertEqual(
            decision.composed_legs[0].note,
            "composition:budget_zero_suppressed:advisory_only",
        )

    def test_protective_override_path(self) -> None:
        composer = SleeveRoutingComposer()

        decision = composer.compose(
            raw=_raw(protective_intent=True, active_inventory=True),
            permission=_permission(
                approved_for_execution=True,
                permission_mode="protective_override",
                protective_intent=True,
                reason_codes=("protective_intent_override",),
            ),
            budget=_budget(),
        )

        self.assertEqual(decision.route_action, "override_target")
        self.assertEqual(decision.execution_control_mode, "protective_override")
        self.assertEqual(decision.execution_behavior, "protective_execute")
        self.assertEqual(decision.composed_delta_position_qty, Decimal("0.25"))
        self.assertEqual(
            decision.composed_legs[0].note,
            "composition:protective_override:override_target",
        )
