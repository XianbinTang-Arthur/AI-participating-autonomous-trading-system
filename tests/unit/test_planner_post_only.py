"""planner post_only 定价 — 单元测试.

Layer 3: build_leg_plan + _apply_explicit_leg_execution_preference 对
order_type_preference="post_only" 的新分支.

锁定契约 (对应 docs/design/post_only_maker_exit_mode_2026_04_21.md §3.3):
1. preferred_order_type="post_only" 时:
   - plan.order_type == "limit"  (内部保持 limit, OrderIntent.order_type Literal 不含 post_only)
   - plan.execution_style == "post_only"  (signal carrier, okx_adapter 据此翻译)
   - plan.time_in_force == 默认 "GTC"  (post_only 是挂单, 必须 GTC, 不跨价)
   - plan.limit_price 非跨价: buy < ref, sell > ref  (与 bounded_limit_ioc 相反)
2. reference_price=None 或 limit_offset_bps<=0 → 保留原 limit_price (不触发 post_only 路径,
   Layer 4 orchestration 会根据 execution_style 信号走 fallback)
3. H2 regression: preferred_order_type="limit" 仍走 bounded_limit_ioc 跨价逻辑
   (buy=ref×(1+offset), sell=ref×(1-offset))
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.services.execution_engine.planner import ExecutionPlanner


class PostOnlyLimitPriceUnitTests(unittest.TestCase):
    """_post_only_limit_price 静态定价: buy=ref×(1-offset), sell=ref×(1+offset)."""

    def setUp(self) -> None:
        self.planner = ExecutionPlanner(settings=AATSSettings.model_validate({}))

    def test_buy_price_below_reference(self) -> None:
        price = self.planner._post_only_limit_price(
            side="buy",
            reference_price=Decimal("100"),
            limit_offset_bps=Decimal("10"),  # 10 bps = 0.1%
        )
        # ref × (1 − 10/10000) = 100 × 0.999 = 99.9
        self.assertEqual(price, Decimal("99.9000"))

    def test_sell_price_above_reference(self) -> None:
        price = self.planner._post_only_limit_price(
            side="sell",
            reference_price=Decimal("100"),
            limit_offset_bps=Decimal("10"),
        )
        # ref × (1 + 10/10000) = 100 × 1.001 = 100.1
        self.assertEqual(price, Decimal("100.1000"))

    def test_buy_is_opposite_direction_vs_bounded_limit_ioc(self) -> None:
        """post_only 买单价 < ref, bounded_limit_ioc 买单价 > ref — 确保方向相反."""
        post_only_buy = self.planner._post_only_limit_price(
            side="buy",
            reference_price=Decimal("100"),
            limit_offset_bps=Decimal("5"),
        )
        self.assertIsNotNone(post_only_buy)
        assert post_only_buy is not None
        self.assertLess(post_only_buy, Decimal("100"))

    def test_sell_is_opposite_direction_vs_bounded_limit_ioc(self) -> None:
        """post_only 卖单价 > ref, bounded_limit_ioc 卖单价 < ref."""
        post_only_sell = self.planner._post_only_limit_price(
            side="sell",
            reference_price=Decimal("100"),
            limit_offset_bps=Decimal("5"),
        )
        self.assertIsNotNone(post_only_sell)
        assert post_only_sell is not None
        self.assertGreater(post_only_sell, Decimal("100"))

    def test_none_reference_returns_none(self) -> None:
        self.assertIsNone(
            self.planner._post_only_limit_price(
                side="buy",
                reference_price=None,
                limit_offset_bps=Decimal("10"),
            )
        )

    def test_zero_offset_returns_none(self) -> None:
        self.assertIsNone(
            self.planner._post_only_limit_price(
                side="buy",
                reference_price=Decimal("100"),
                limit_offset_bps=Decimal("0"),
            )
        )

    def test_negative_offset_returns_none(self) -> None:
        self.assertIsNone(
            self.planner._post_only_limit_price(
                side="buy",
                reference_price=Decimal("100"),
                limit_offset_bps=Decimal("-1"),
            )
        )

    def test_none_offset_returns_none(self) -> None:
        self.assertIsNone(
            self.planner._post_only_limit_price(
                side="buy",
                reference_price=Decimal("100"),
                limit_offset_bps=None,
            )
        )

    def test_zero_reference_returns_none(self) -> None:
        self.assertIsNone(
            self.planner._post_only_limit_price(
                side="buy",
                reference_price=Decimal("0"),
                limit_offset_bps=Decimal("10"),
            )
        )


class BuildLegPlanPostOnlyPreferencesTests(unittest.TestCase):
    """build_leg_plan + order_type_preference="post_only" 的端到端契约."""

    def setUp(self) -> None:
        self.planner = ExecutionPlanner(settings=AATSSettings.model_validate({}))

    def _common_args(self) -> dict[str, object]:
        return dict(
            decision_id="decision_post_only",
            symbol="BTC-USDT-SWAP",
            quantity=Decimal("0.01"),
            max_slippage_tolerance_bps=20,
            product_type="derivatives",
            target_leverage=3.0,
            margin_mode="cross",
            td_mode="cross",
            position_mode="long_short_mode",
            instrument_family="BTC-USDT",
            settle_currency="USDT",
        )

    def test_post_only_sell_uses_non_crossing_price_above_reference(self) -> None:
        """卖单 post_only: price > ref (挂在卖侧, 等买方吃)."""
        plan = self.planner.build_leg_plan(
            **self._common_args(),
            side="sell",
            pos_side="long",
            action="close",
            urgency="medium",
            reference_price=Decimal("100"),
            execution_style_preference="post_only",
            order_type_preference="post_only",
            time_in_force_preference="GTC",
            limit_offset_bps_preference=Decimal("0.8"),
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        # order_type 降级为 "limit" (OrderIntent Literal 限制)
        # execution_style="post_only" 作为 adapter 翻译信号
        self.assertEqual(plan.order_type, "limit")
        self.assertEqual(plan.execution_style, "post_only")
        self.assertEqual(plan.time_in_force, "GTC")
        # sell price = 100 × (1 + 0.8/10000) = 100.008
        self.assertEqual(plan.limit_price, Decimal("100.0080"))

    def test_post_only_buy_uses_non_crossing_price_below_reference(self) -> None:
        """买单 post_only: price < ref (挂在买侧, 等卖方吃)."""
        plan = self.planner.build_leg_plan(
            **self._common_args(),
            side="buy",
            pos_side="long",
            action="open",
            urgency="low",
            reference_price=Decimal("100"),
            execution_style_preference="post_only",
            order_type_preference="post_only",
            time_in_force_preference="GTC",
            limit_offset_bps_preference=Decimal("0.8"),
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.order_type, "limit")
        self.assertEqual(plan.execution_style, "post_only")
        # buy price = 100 × (1 − 0.8/10000) = 99.992
        self.assertEqual(plan.limit_price, Decimal("99.9920"))

    def test_post_only_execution_style_flows_into_leg_intent(self) -> None:
        """execution_style="post_only" 在 build_leg_intent 后仍保留 — okx_adapter._order_type 会读它."""
        plan = self.planner.build_leg_plan(
            **self._common_args(),
            side="sell",
            pos_side="long",
            action="close",
            urgency="medium",
            reference_price=Decimal("100"),
            execution_style_preference="post_only",
            order_type_preference="post_only",
            time_in_force_preference="GTC",
            limit_offset_bps_preference=Decimal("0.8"),
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        leg_intent = self.planner.build_leg_intent(plan=plan)
        self.assertIsNotNone(leg_intent)
        assert leg_intent is not None
        self.assertEqual(leg_intent.execution_style, "post_only")
        self.assertEqual(leg_intent.order_type, "limit")
        self.assertEqual(leg_intent.time_in_force, "GTC")

    def test_post_only_without_reference_falls_through_to_default(self) -> None:
        """无 reference_price 时 _post_only_limit_price 返回 None → 保留原 execution_style/limit_price.

        Layer 4 orchestration 会检测到 execution_style 未落到 "post_only" → 走 fallback.
        Layer 3 不负责决定 fallback — 只负责: 能算出 non-crossing price 就用, 否则不改原值.
        """
        plan = self.planner.build_leg_plan(
            **self._common_args(),
            side="sell",
            pos_side="long",
            action="close",
            urgency="medium",
            reference_price=None,
            execution_style_preference="post_only",
            order_type_preference="post_only",
            time_in_force_preference="GTC",
            limit_offset_bps_preference=Decimal("0.8"),
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        # 因为 reference_price=None, _post_only_limit_price 返回 None,
        # 所以保留原值 (execution_style 和 order_type 是 build_leg_plan 里的默认)
        self.assertNotEqual(plan.execution_style, "post_only")

    def test_post_only_with_zero_offset_falls_through_to_default(self) -> None:
        """offset=0 时 _post_only_limit_price 返回 None → 保留原值."""
        plan = self.planner.build_leg_plan(
            **self._common_args(),
            side="sell",
            pos_side="long",
            action="close",
            urgency="medium",
            reference_price=Decimal("100"),
            execution_style_preference="post_only",
            order_type_preference="post_only",
            time_in_force_preference="GTC",
            limit_offset_bps_preference=Decimal("0"),
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertNotEqual(plan.execution_style, "post_only")


class PostOnlyDoesNotRegressBoundedLimitIocTests(unittest.TestCase):
    """**H2 regression guard**: preferred_order_type="limit" 仍走 bounded_limit_ioc 跨价逻辑."""

    def setUp(self) -> None:
        self.planner = ExecutionPlanner(settings=AATSSettings.model_validate({}))

    def test_bounded_limit_sell_still_crosses_the_spread(self) -> None:
        """H2 锁定: sell bounded_limit_ioc price < ref (跨价吃买盘)."""
        plan = self.planner.build_leg_plan(
            decision_id="decision_h2_guard",
            symbol="BTC-USDT-SWAP",
            side="sell",
            pos_side="long",
            action="close",
            quantity=Decimal("0.01"),
            urgency="medium",
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
        # bounded_limit_ioc sell: ref × (1 − offset/10000) → price < ref (跨价)
        self.assertIsNotNone(plan.limit_price)
        assert plan.limit_price is not None
        self.assertLess(plan.limit_price, Decimal("100"))

    def test_bounded_limit_buy_still_crosses_the_spread(self) -> None:
        """H2 锁定: buy bounded_limit_ioc price > ref (跨价吃卖盘)."""
        plan = self.planner.build_leg_plan(
            decision_id="decision_h2_guard_buy",
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
        self.assertIsNotNone(plan.limit_price)
        assert plan.limit_price is not None
        # bounded_limit_ioc buy: ref × (1 + offset/10000) → price > ref
        self.assertGreater(plan.limit_price, Decimal("100"))

    def test_market_preference_unchanged(self) -> None:
        """preferred_order_type="market" 仍走 taker 路径."""
        plan = self.planner.build_leg_plan(
            decision_id="decision_market_guard",
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
        self.assertIsNone(plan.limit_price)


if __name__ == "__main__":
    unittest.main()
