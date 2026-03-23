from __future__ import annotations

import unittest
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.execution import AIExecutionParameterSuggestionEnvelope, ExecutionParameterSuggestion, ExecutionPlan
from aats.services.execution_engine.planner import ExecutionPlanner


class TestExecutionPlanner(unittest.TestCase):
    def test_build_plan_exposes_abstract_execution_action_for_scale_in(self) -> None:
        planner = ExecutionPlanner(settings=AATSSettings.model_validate({}))

        plan = planner.build_plan(
            decision_id="decision_scale_in",
            symbol="BTC-USDT",
            current_position_qty=Decimal("1"),
            target_position_qty=Decimal("2"),
            approved_target_position_qty=Decimal("2"),
            delta_qty=Decimal("1"),
            urgency="medium",
            max_slippage_tolerance_bps=25,
        )

        self.assertIsNotNone(plan)
        self.assertEqual(plan.execution_action, "scale_in")
        self.assertEqual(plan.position_intent, "open_long")

        intent = planner.build_intent(plan=plan)

        self.assertIsNotNone(intent)
        self.assertEqual(intent.execution_action, "scale_in")
        self.assertEqual(intent.position_intent, "open_long")

    def test_build_plan_and_intent_preserve_derivatives_execution_semantics(self) -> None:
        planner = ExecutionPlanner(settings=AATSSettings.model_validate({}))

        plan = planner.build_plan(
            decision_id="decision_derivatives_semantics",
            symbol="BTC-USDT-SWAP",
            current_position_qty=Decimal("0.02"),
            target_position_qty=Decimal("0"),
            approved_target_position_qty=Decimal("0"),
            delta_qty=Decimal("-0.02"),
            urgency="medium",
            max_slippage_tolerance_bps=25,
            product_type="derivatives",
            target_leverage=3.0,
            margin_mode="cross",
            td_mode="cross",
            position_mode="long_short_mode",
            instrument_family="BTC-USDT",
            settle_currency="USDT",
        )

        self.assertIsNotNone(plan)
        self.assertTrue(plan.reduce_only)
        self.assertTrue(plan.close_only)
        self.assertEqual(plan.position_intent, "close_long")
        self.assertEqual(plan.td_mode, "cross")
        self.assertEqual(plan.position_mode, "long_short_mode")
        self.assertEqual(plan.pos_side, "long")
        self.assertEqual(plan.reduce_only_reason, "position_intent_close_path")
        self.assertEqual(plan.close_only_reason, "position_intent_close_path")
        self.assertEqual(plan.instrument_family, "BTC-USDT")
        self.assertEqual(plan.settle_currency, "USDT")

        intent = planner.build_intent(plan=plan)

        self.assertIsNotNone(intent)
        self.assertTrue(intent.reduce_only)
        self.assertTrue(intent.close_only)
        self.assertEqual(intent.td_mode, "cross")
        self.assertEqual(intent.position_mode, "long_short_mode")
        self.assertEqual(intent.pos_side, "long")
        self.assertEqual(intent.instrument_family, "BTC-USDT")
        self.assertEqual(intent.settle_currency, "USDT")

    def test_build_plan_and_intent_preserve_derivatives_risk_submission_context(self) -> None:
        planner = ExecutionPlanner(settings=AATSSettings.model_validate({}))

        plan = planner.build_plan(
            decision_id="decision_derivatives_risk_context",
            symbol="BTC-USDT-SWAP",
            current_position_qty=Decimal("0.02"),
            target_position_qty=Decimal("0"),
            approved_target_position_qty=Decimal("0"),
            delta_qty=Decimal("-0.02"),
            urgency="medium",
            max_slippage_tolerance_bps=25,
            product_type="derivatives",
            target_leverage=3.0,
            margin_mode="cross",
            td_mode="cross",
            position_mode="long_short_mode",
            required_initial_margin=Decimal("17.5"),
            projected_margin_usage=Decimal("0.81"),
            projected_notional=Decimal("1340"),
            only_reduce_required=True,
            risk_limit_breached=True,
            liquidation_buffer_remaining=Decimal("0.04"),
        )

        self.assertIsNotNone(plan)
        self.assertEqual(plan.required_initial_margin, Decimal("17.5"))
        self.assertEqual(plan.projected_margin_usage, Decimal("0.81"))
        self.assertEqual(plan.projected_notional, Decimal("1340"))
        self.assertTrue(plan.only_reduce_required)
        self.assertTrue(plan.risk_limit_breached)
        self.assertEqual(plan.liquidation_buffer_remaining, Decimal("0.04"))

        intent = planner.build_intent(plan=plan)

        self.assertIsNotNone(intent)
        self.assertEqual(intent.required_initial_margin, Decimal("17.5"))
        self.assertEqual(intent.projected_margin_usage, Decimal("0.81"))
        self.assertEqual(intent.projected_notional, Decimal("1340"))
        self.assertTrue(intent.only_reduce_required)
        self.assertTrue(intent.risk_limit_breached)
        self.assertEqual(intent.liquidation_buffer_remaining, Decimal("0.04"))

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
        self.assertEqual(intent.execution_action, "enter")
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
        self.assertEqual(plan.execution_action, "enter")
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
        self.assertEqual(plan.execution_action, "enter")
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
        self.assertEqual(intent.execution_action, "enter")
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
        self.assertEqual(plan.execution_action, "enter")
        self.assertEqual(plan.order_type, "market")
        self.assertIsNone(plan.limit_price)
        self.assertFalse(plan.ai_execution_parameter_suggestion.applied_to_live_execution)
        self.assertEqual(
            plan.ai_execution_parameter_suggestion.live_translation_fallback_reason,
            "live_translation_requires_reference_price",
        )

    def test_execution_aggressiveness_multiplier_contracts_slippage_and_execution_bounds(self) -> None:
        planner = ExecutionPlanner(
            settings=AATSSettings.model_validate(
                {
                    "ai_execution_suggestion_mode": "shadow_translation",
                    "ai_execution_max_cross_spread_bps": 6.0,
                    "ai_execution_max_slice_count": 4,
                    "ai_execution_max_participation_rate": 0.4,
                    "ai_execution_max_cancel_replace_patience_ms": 4000,
                }
            )
        )

        plan = planner.build_plan(
            decision_id="decision_execution_multiplier",
            symbol="BTC-USDT",
            current_position_qty=Decimal("0"),
            target_position_qty=Decimal("1"),
            approved_target_position_qty=Decimal("1"),
            delta_qty=Decimal("1"),
            urgency="medium",
            max_slippage_tolerance_bps=20,
            execution_aggressiveness_multiplier=Decimal("0.5"),
            execution_aggressiveness_state={"status": "contracted", "reasons": ["execution_errors_elevated"]},
            ai_execution_parameter_suggestion=AIExecutionParameterSuggestionEnvelope(
                status="diagnostic_only",
                diagnostic_only=True,
                requested_mode="diagnostic_only",
                suggestion=ExecutionParameterSuggestion(
                    passive_bias=Decimal("0.2"),
                    maker_taker_bias=Decimal("0.8"),
                    max_cross_spread_bps=Decimal("6"),
                    slice_count=4,
                    max_participation_rate=Decimal("0.4"),
                    cancel_replace_patience_ms=4000,
                ),
                accepted_by_execution_planner=False,
                rejection_reasons=[],
            ),
        )

        self.assertIsNotNone(plan)
        self.assertEqual(plan.max_slippage_tolerance_bps, 10)
        self.assertEqual(plan.execution_aggressiveness_multiplier, Decimal("0.5"))
        self.assertEqual(plan.execution_aggressiveness_state["status"], "contracted")
        self.assertEqual(plan.ai_execution_parameter_suggestion.suggestion.passive_bias, Decimal("0.60"))
        self.assertEqual(plan.ai_execution_parameter_suggestion.suggestion.max_cross_spread_bps, Decimal("3"))
        self.assertEqual(plan.ai_execution_parameter_suggestion.suggestion.slice_count, 2)
        self.assertEqual(plan.ai_execution_parameter_suggestion.suggestion.max_participation_rate, Decimal("0.2"))
        self.assertEqual(plan.ai_execution_parameter_suggestion.suggestion.cancel_replace_patience_ms, 2000)


if __name__ == "__main__":
    unittest.main()
