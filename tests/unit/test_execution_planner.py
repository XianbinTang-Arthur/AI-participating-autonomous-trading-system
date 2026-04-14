from __future__ import annotations

import unittest
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.exchange import InstrumentMetadata
from aats.schemas.execution import (
    AIExecutionParameterSuggestionEnvelope,
    ExecutionParameterSuggestion,
    ExecutionPlan,
    order_intent_from_leg_order_intent,
)
from aats.services.execution_engine.planner import ExecutionPlanner


class TestExecutionPlanner(unittest.TestCase):
    @staticmethod
    def _swap_instrument() -> InstrumentMetadata:
        return InstrumentMetadata(
            instrument_id="BTC-USDT-SWAP",
            symbol="BTC-USDT-SWAP",
            base_currency="BTC",
            quote_currency="USDT",
            lot_size=Decimal("0.01"),
            tick_size=Decimal("0.1"),
            min_size=Decimal("0.01"),
            contract_value=Decimal("0.01"),
            instrument_type="SWAP",
            instrument_family="BTC-USDT",
            settle_currency="USDT",
            state="live",
        )

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
        self.assertEqual(plan.position_intent, "scale_in_long")

        intent = planner.build_intent(plan=plan)

        self.assertIsNotNone(intent)
        self.assertEqual(intent.execution_action, "scale_in")
        self.assertEqual(intent.position_intent, "scale_in_long")

    def test_build_leg_plan_and_intent_preserve_derivatives_execution_semantics(self) -> None:
        planner = ExecutionPlanner(settings=AATSSettings.model_validate({}))

        plan = planner.build_leg_plan(
            decision_id="decision_derivatives_semantics",
            symbol="BTC-USDT-SWAP",
            side="sell",
            pos_side="long",
            action="close",
            quantity=Decimal("0.02"),
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
        assert plan is not None
        self.assertTrue(plan.reduce_only)
        self.assertTrue(plan.close_only)
        self.assertEqual(plan.action, "close")
        self.assertEqual(plan.position_intent, "close_long")
        self.assertEqual(plan.td_mode, "cross")
        self.assertEqual(plan.position_mode, "long_short_mode")
        self.assertEqual(plan.pos_side, "long")
        self.assertEqual(plan.reduce_only_reason, "explicit_leg_close_path")
        self.assertEqual(plan.close_only_reason, "explicit_leg_close_path")
        self.assertEqual(plan.instrument_family, "BTC-USDT")
        self.assertEqual(plan.settle_currency, "USDT")

        intent = planner.build_leg_intent(plan=plan)

        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertTrue(intent.reduce_only)
        self.assertTrue(intent.close_only)
        self.assertEqual(intent.td_mode, "cross")
        self.assertEqual(intent.position_mode, "long_short_mode")
        self.assertEqual(intent.pos_side, "long")
        self.assertEqual(intent.action, "close")
        self.assertEqual(intent.instrument_family, "BTC-USDT")
        self.assertEqual(intent.settle_currency, "USDT")

    def test_build_leg_plan_and_intent_preserve_scale_in_position_intent(self) -> None:
        planner = ExecutionPlanner(settings=AATSSettings.model_validate({}))

        plan = planner.build_leg_plan(
            decision_id="decision_scale_in_leg",
            symbol="BTC-USDT-SWAP",
            side="buy",
            pos_side="long",
            action="open",
            quantity=Decimal("0.01"),
            urgency="medium",
            max_slippage_tolerance_bps=25,
            product_type="derivatives",
            target_leverage=3.0,
            margin_mode="cross",
            td_mode="cross",
            position_mode="long_short_mode",
            instrument_family="BTC-USDT",
            settle_currency="USDT",
            position_intent="scale_in_long",
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.position_intent, "scale_in_long")
        self.assertEqual(plan.execution_action, "enter")

        leg_intent = planner.build_leg_intent(plan=plan)

        self.assertIsNotNone(leg_intent)
        assert leg_intent is not None
        self.assertEqual(leg_intent.position_intent, "scale_in_long")
        self.assertEqual(leg_intent.action, "open")

        order_intent = order_intent_from_leg_order_intent(leg_intent)

        self.assertEqual(order_intent.position_intent, "scale_in_long")
        self.assertEqual(order_intent.execution_action, "enter")
        self.assertEqual(order_intent.side, "buy")

    def test_build_leg_plan_and_intent_preserve_execution_chain_id(self) -> None:
        planner = ExecutionPlanner(settings=AATSSettings.model_validate({}))

        plan = planner.build_leg_plan(
            decision_id="decision_chain_preserve",
            symbol="BTC-USDT-SWAP",
            side="buy",
            pos_side="long",
            action="open",
            quantity=Decimal("0.01"),
            urgency="medium",
            max_slippage_tolerance_bps=25,
            product_type="derivatives",
            target_leverage=3.0,
            margin_mode="cross",
            td_mode="cross",
            position_mode="long_short_mode",
            execution_chain_id="independent:decision_chain_preserve:long:open",
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.execution_chain_id, "independent:decision_chain_preserve:long:open")

        leg_intent = planner.build_leg_intent(plan=plan)

        self.assertIsNotNone(leg_intent)
        assert leg_intent is not None
        self.assertEqual(leg_intent.execution_chain_id, "independent:decision_chain_preserve:long:open")

        order_intent = order_intent_from_leg_order_intent(leg_intent)

        self.assertEqual(order_intent.execution_chain_id, "independent:decision_chain_preserve:long:open")

    def test_build_leg_plan_applies_explicit_passive_first_preferences(self) -> None:
        planner = ExecutionPlanner(settings=AATSSettings.model_validate({}))

        plan = planner.build_leg_plan(
            decision_id="decision_passive_first",
            symbol="BTC-USDT-SWAP",
            side="buy",
            pos_side="long",
            action="open",
            quantity=Decimal("0.01"),
            urgency="low",
            max_slippage_tolerance_bps=20,
            reference_price=Decimal("100"),
            product_type="derivatives",
            target_leverage=3.0,
            margin_mode="cross",
            td_mode="cross",
            position_mode="long_short_mode",
            instrument_family="BTC-USDT",
            settle_currency="USDT",
            execution_style_preference="bounded_limit_ioc",
            order_type_preference="limit",
            time_in_force_preference="IOC",
            limit_offset_bps_preference=Decimal("1.5"),
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.execution_style, "bounded_limit_ioc")
        self.assertEqual(plan.order_type, "limit")
        self.assertEqual(plan.time_in_force, "IOC")
        self.assertEqual(plan.limit_price, Decimal("100.0150"))

        leg_intent = planner.build_leg_intent(plan=plan)

        self.assertIsNotNone(leg_intent)
        assert leg_intent is not None
        self.assertEqual(leg_intent.execution_style, "bounded_limit_ioc")
        self.assertEqual(leg_intent.order_type, "limit")
        self.assertEqual(leg_intent.time_in_force, "IOC")
        self.assertEqual(leg_intent.limit_price, Decimal("100.0150"))

    def test_build_leg_plan_applies_explicit_market_preferences(self) -> None:
        planner = ExecutionPlanner(settings=AATSSettings.model_validate({}))

        plan = planner.build_leg_plan(
            decision_id="decision_market_preference",
            symbol="BTC-USDT-SWAP",
            side="sell",
            pos_side="long",
            action="close",
            quantity=Decimal("0.01"),
            urgency="high",
            max_slippage_tolerance_bps=20,
            reference_price=Decimal("100"),
            product_type="derivatives",
            target_leverage=3.0,
            margin_mode="cross",
            td_mode="cross",
            position_mode="long_short_mode",
            instrument_family="BTC-USDT",
            settle_currency="USDT",
            execution_style_preference="taker",
            order_type_preference="market",
            time_in_force_preference="IOC",
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.execution_style, "taker")
        self.assertEqual(plan.order_type, "market")
        self.assertEqual(plan.time_in_force, "IOC")
        self.assertIsNone(plan.limit_price)

    def test_build_plan_rejects_signed_derivatives_flow_in_long_short_mode(self) -> None:
        planner = ExecutionPlanner(settings=AATSSettings.model_validate({}))

        plan = planner.build_plan(
            decision_id="decision_legacy_signed_hedge_path",
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
        )

        self.assertIsNone(plan)

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
            position_mode="net_mode",
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

    def test_build_plan_skips_derivatives_delta_below_exchange_minimum_trade_quantity(self) -> None:
        planner = ExecutionPlanner(settings=AATSSettings.model_validate({}))

        plan = planner.build_plan(
            decision_id="decision_below_min_trade_qty",
            symbol="BTC-USDT-SWAP",
            current_position_qty=Decimal("0.000300000000"),
            target_position_qty=Decimal("0.000264674954"),
            approved_target_position_qty=Decimal("0.000264674954"),
            delta_qty=Decimal("-0.000035325046"),
            urgency="medium",
            max_slippage_tolerance_bps=25,
            product_type="derivatives",
            margin_mode="cross",
            instrument_rule=self._swap_instrument(),
        )

        self.assertIsNone(plan)

    def test_build_plan_quantizes_derivatives_delta_to_exchange_step_before_intent(self) -> None:
        planner = ExecutionPlanner(settings=AATSSettings.model_validate({}))

        plan = planner.build_plan(
            decision_id="decision_quantized_trade_qty",
            symbol="BTC-USDT-SWAP",
            current_position_qty=Decimal("0"),
            target_position_qty=Decimal("0.000235"),
            approved_target_position_qty=Decimal("0.000235"),
            delta_qty=Decimal("0.000235"),
            urgency="medium",
            max_slippage_tolerance_bps=25,
            product_type="derivatives",
            margin_mode="cross",
            instrument_rule=self._swap_instrument(),
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.delta_qty, Decimal("0.0002"))
        self.assertEqual(plan.approved_target_position_qty, Decimal("0.0002"))

        intent = planner.build_intent(plan=plan)

        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.quantity, Decimal("0.0002"))


    # ── okx_submit_blocked 回归测试 ────────────────────────────────
    # 修复：close_only=True 时 reduce_long 必须升级为 close_long，
    # 否则 OKX adapter 在 long_short_mode 下会拒绝提交。

    def test_leg_order_intent_close_action_upgrades_reduce_long_to_close_long(self) -> None:
        """当 leg_action=close 且上游传入 position_intent=reduce_long 时，
        close_only 被推导为 True → position_intent 必须一致地升级为 close_long。"""
        planner = ExecutionPlanner(settings=AATSSettings.model_validate({}))

        plan = planner.build_leg_plan(
            decision_id="decision_reduce_close_fix",
            symbol="BTC-USDT-SWAP",
            side="sell",
            pos_side="long",
            action="close",
            quantity=Decimal("0.001"),
            urgency="high",
            max_slippage_tolerance_bps=30,
            product_type="derivatives",
            target_leverage=5.0,
            margin_mode="cross",
            td_mode="cross",
            position_mode="long_short_mode",
            position_intent="reduce_long",  # 上游传入 reduce_long
        )

        self.assertIsNotNone(plan)
        assert plan is not None

        leg_intent = planner.build_leg_intent(plan=plan)
        self.assertIsNotNone(leg_intent)
        assert leg_intent is not None
        self.assertEqual(leg_intent.action, "close")

        order_intent = order_intent_from_leg_order_intent(leg_intent)

        # close_only 由 leg_action="close" 推导
        self.assertTrue(order_intent.close_only)
        # position_intent 应被升级为 close_long（不能是 reduce_long）
        self.assertEqual(order_intent.position_intent, "close_long")

    def test_leg_order_intent_reduce_action_preserves_reduce_long(self) -> None:
        """当 leg_action=reduce 时，close_only=False，position_intent 保持 reduce_long。"""
        planner = ExecutionPlanner(settings=AATSSettings.model_validate({}))

        plan = planner.build_leg_plan(
            decision_id="decision_reduce_preserve",
            symbol="BTC-USDT-SWAP",
            side="sell",
            pos_side="long",
            action="reduce",
            quantity=Decimal("0.001"),
            urgency="medium",
            max_slippage_tolerance_bps=20,
            product_type="derivatives",
            target_leverage=5.0,
            margin_mode="cross",
            td_mode="cross",
            position_mode="long_short_mode",
            position_intent="reduce_long",
        )

        self.assertIsNotNone(plan)
        assert plan is not None

        leg_intent = planner.build_leg_intent(plan=plan)
        self.assertIsNotNone(leg_intent)
        assert leg_intent is not None

        order_intent = order_intent_from_leg_order_intent(leg_intent)

        self.assertFalse(order_intent.close_only)
        self.assertTrue(order_intent.reduce_only)
        self.assertEqual(order_intent.position_intent, "reduce_long")

    def test_leg_order_intent_close_action_upgrades_reduce_short_to_close_short(self) -> None:
        """对称测试：short 方向的 reduce_short + close 也要升级为 close_short。"""
        planner = ExecutionPlanner(settings=AATSSettings.model_validate({}))

        plan = planner.build_leg_plan(
            decision_id="decision_short_close_fix",
            symbol="BTC-USDT-SWAP",
            side="buy",
            pos_side="short",
            action="close",
            quantity=Decimal("0.001"),
            urgency="high",
            max_slippage_tolerance_bps=30,
            product_type="derivatives",
            target_leverage=5.0,
            margin_mode="cross",
            td_mode="cross",
            position_mode="long_short_mode",
            position_intent="reduce_short",
        )

        self.assertIsNotNone(plan)
        assert plan is not None

        leg_intent = planner.build_leg_intent(plan=plan)
        self.assertIsNotNone(leg_intent)
        assert leg_intent is not None

        order_intent = order_intent_from_leg_order_intent(leg_intent)

        self.assertTrue(order_intent.close_only)
        self.assertEqual(order_intent.position_intent, "close_short")


if __name__ == "__main__":
    unittest.main()
