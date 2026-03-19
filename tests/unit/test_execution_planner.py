from __future__ import annotations

import unittest
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.execution import AIExecutionParameterSuggestionEnvelope, ExecutionParameterSuggestion, ExecutionPlan
from aats.services.execution_engine.planner import ExecutionPlanner


class TestExecutionPlanner(unittest.TestCase):
    def test_build_intent_preserves_disabled_ai_execution_suggestion_boundary(self) -> None:
        planner = ExecutionPlanner(settings=AATSSettings.model_validate({}))
        plan = ExecutionPlan(
            plan_id="plan_test",
            decision_id="decision_test",
            symbol="BTC-USDT",
            current_position_qty=Decimal("0"),
            target_position_qty=Decimal("1"),
            approved_target_position_qty=Decimal("1"),
            delta_qty=Decimal("1"),
            side="buy",
            execution_style="taker",
            order_type="market",
            urgency="medium",
            max_slippage_tolerance_bps=25,
            ai_execution_parameter_suggestion=AIExecutionParameterSuggestionEnvelope(
                status="diagnostic_only",
                diagnostic_only=True,
                suggestion=ExecutionParameterSuggestion(
                    passive_bias=Decimal("0.6"),
                    slice_count=3,
                ),
                accepted_by_execution_planner=False,
                rejection_reasons=["diagnostic_input_only"],
            ),
        )

        translated_plan = planner.build_plan(
            decision_id=plan.decision_id,
            symbol=plan.symbol,
            current_position_qty=plan.current_position_qty,
            target_position_qty=plan.target_position_qty,
            approved_target_position_qty=plan.approved_target_position_qty,
            delta_qty=plan.delta_qty,
            urgency=plan.urgency,
            max_slippage_tolerance_bps=plan.max_slippage_tolerance_bps,
            ai_execution_parameter_suggestion=plan.ai_execution_parameter_suggestion,
        )
        self.assertIsNotNone(translated_plan)
        intent = planner.build_intent(plan=translated_plan)

        self.assertIsNotNone(intent)
        self.assertIsNotNone(intent.ai_execution_parameter_suggestion)
        self.assertEqual(intent.ai_execution_parameter_suggestion.status, "reserved_not_enabled")
        self.assertFalse(intent.ai_execution_parameter_suggestion.accepted_by_execution_planner)
        self.assertEqual(
            intent.ai_execution_parameter_suggestion.rejection_reasons,
            ["execution_parameter_suggestions_disabled"],
        )
        self.assertEqual(intent.ai_execution_parameter_suggestion.suggestion.slice_count, 3)

    def test_build_plan_generates_shadow_translation_preview_without_live_apply(self) -> None:
        planner = ExecutionPlanner(
            settings=AATSSettings.model_validate(
                {
                    "ai_execution_suggestion_mode": "shadow_translation",
                    "ai_execution_max_cross_spread_bps": 4.0,
                    "ai_execution_max_slice_count": 3,
                }
            )
        )

        plan = planner.build_plan(
            decision_id="decision_shadow_translation",
            symbol="BTC-USDT",
            current_position_qty=Decimal("0"),
            target_position_qty=Decimal("1"),
            approved_target_position_qty=Decimal("1"),
            delta_qty=Decimal("1"),
            urgency="medium",
            max_slippage_tolerance_bps=20,
            ai_execution_parameter_suggestion=AIExecutionParameterSuggestionEnvelope(
                status="diagnostic_only",
                diagnostic_only=True,
                requested_mode="diagnostic_only",
                suggestion=ExecutionParameterSuggestion(
                    passive_bias=Decimal("0.8"),
                    maker_taker_bias=Decimal("-0.4"),
                    max_cross_spread_bps=Decimal("8"),
                    slice_count=6,
                ),
                accepted_by_execution_planner=False,
                rejection_reasons=[],
            ),
        )

        self.assertIsNotNone(plan)
        self.assertIsNotNone(plan.ai_execution_parameter_suggestion)
        self.assertEqual(plan.ai_execution_parameter_suggestion.status, "shadow_translation")
        self.assertTrue(plan.ai_execution_parameter_suggestion.accepted_by_execution_planner)
        self.assertFalse(plan.ai_execution_parameter_suggestion.applied_to_live_execution)
        self.assertIn("max_cross_spread_bps", plan.ai_execution_parameter_suggestion.clipped_fields)
        self.assertIn("slice_count", plan.ai_execution_parameter_suggestion.clipped_fields)
        self.assertEqual(plan.ai_execution_parameter_suggestion.translation_preview.order_type, "limit")
        self.assertEqual(plan.ai_execution_parameter_suggestion.translation_preview.limit_offset_bps, Decimal("4"))

    def test_build_plan_enables_bounded_live_translation_when_reference_price_is_available(self) -> None:
        planner = ExecutionPlanner(
            settings=AATSSettings.model_validate(
                {
                    "ai_execution_suggestion_mode": "enabled_live",
                    "ai_execution_max_cross_spread_bps": 6.0,
                    "max_slippage_tolerance_bps": 20,
                }
            )
        )

        plan = planner.build_plan(
            decision_id="decision_live_translation",
            symbol="BTC-USDT",
            current_position_qty=Decimal("0"),
            target_position_qty=Decimal("1"),
            approved_target_position_qty=Decimal("1"),
            delta_qty=Decimal("1"),
            urgency="medium",
            max_slippage_tolerance_bps=20,
            reference_price=Decimal("100"),
            ai_execution_parameter_suggestion=AIExecutionParameterSuggestionEnvelope(
                status="diagnostic_only",
                diagnostic_only=True,
                requested_mode="diagnostic_only",
                suggestion=ExecutionParameterSuggestion(
                    passive_bias=Decimal("0.9"),
                    maker_taker_bias=Decimal("-0.6"),
                    max_cross_spread_bps=Decimal("8"),
                    slice_count=2,
                ),
                accepted_by_execution_planner=False,
                rejection_reasons=[],
            ),
        )

        self.assertIsNotNone(plan)
        self.assertEqual(plan.order_type, "limit")
        self.assertEqual(plan.time_in_force, "IOC")
        self.assertEqual(plan.execution_style, "bounded_limit_ioc")
        self.assertEqual(plan.limit_price, Decimal("100.0600"))
        self.assertTrue(plan.ai_execution_parameter_suggestion.applied_to_live_execution)
        self.assertEqual(
            plan.ai_execution_parameter_suggestion.applied_live_fields,
            ["execution_style", "order_type", "limit_price", "time_in_force"],
        )

        intent = planner.build_intent(plan=plan)

        self.assertIsNotNone(intent)
        self.assertEqual(intent.order_type, "limit")
        self.assertEqual(intent.time_in_force, "IOC")
        self.assertEqual(intent.limit_price, Decimal("100.0600"))
        self.assertTrue(intent.ai_execution_parameter_suggestion.applied_to_live_execution)

    def test_enabled_live_without_reference_price_falls_back_to_shadow_translation(self) -> None:
        planner = ExecutionPlanner(
            settings=AATSSettings.model_validate(
                {
                    "ai_execution_suggestion_mode": "enabled_live",
                }
            )
        )

        plan = planner.build_plan(
            decision_id="decision_live_fallback",
            symbol="BTC-USDT",
            current_position_qty=Decimal("0"),
            target_position_qty=Decimal("1"),
            approved_target_position_qty=Decimal("1"),
            delta_qty=Decimal("1"),
            urgency="medium",
            max_slippage_tolerance_bps=20,
            reference_price=None,
            ai_execution_parameter_suggestion=AIExecutionParameterSuggestionEnvelope(
                status="diagnostic_only",
                diagnostic_only=True,
                requested_mode="diagnostic_only",
                suggestion=ExecutionParameterSuggestion(
                    passive_bias=Decimal("0.9"),
                    max_cross_spread_bps=Decimal("4"),
                    slice_count=2,
                ),
                accepted_by_execution_planner=False,
                rejection_reasons=[],
            ),
        )

        self.assertIsNotNone(plan)
        self.assertEqual(plan.order_type, "market")
        self.assertIsNone(plan.limit_price)
        self.assertFalse(plan.ai_execution_parameter_suggestion.applied_to_live_execution)
        self.assertEqual(
            plan.ai_execution_parameter_suggestion.live_translation_fallback_reason,
            "live_translation_requires_reference_price",
        )


if __name__ == "__main__":
    unittest.main()
