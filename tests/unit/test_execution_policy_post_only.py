"""post_only_with_timeout_fallback execution policy — 单元测试.

实施依据: docs/design/post_only_maker_exit_mode_2026_04_21.md §3.2

锁定契约:
1. resolve_execution_policy_from_mode("post_only_with_timeout_fallback", ...)
   返回一个 IndependentExecutionPolicy, 其:
   - execution_style_preference == "post_only"
   - order_type_preference == "post_only"
   - time_in_force_preference == "GTC" (非 IOC — post_only 必须挂单)
   - post_only == True
   - passive_first == True
   - bounded_limit_ioc == False (不是 IOC)
   - bounded_taker == False (不是 taker)
2. close_stale_thesis book_action + close_stale_execution_mode=post_only_with_timeout_fallback
   走 resolve_execution_policy_from_mode 新分支
3. 其他 mode 不受影响 (H2 bounded_limit_ioc 行为保持)
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from aats.services.strategy_engines.independent.execution_policy import (
    resolve_execution_policy,
    resolve_execution_policy_from_mode,
)
from aats.services.strategy_engines.independent.models import (
    IndependentBookDecision,
    IndependentBookExpectancy,
)
from tests.support.strategy_family import make_derivatives_hedge_settings


class PostOnlyExecutionPolicyModeTests(unittest.TestCase):
    """resolve_execution_policy_from_mode 新 post_only 分支的字段契约."""

    def test_post_only_mode_returns_post_only_policy(self) -> None:
        policy = resolve_execution_policy_from_mode(
            mode="post_only_with_timeout_fallback",
            edge_strength="medium",
            urgency="medium",
            limit_offset_bps=Decimal("0.8"),
            max_acceptable_cost_bps=7.5,
            policy_reason="independent_stale_thesis_configured_post_only_with_timeout_fallback",
        )
        self.assertEqual(policy.execution_style_preference, "post_only")
        self.assertEqual(policy.order_type_preference, "post_only")
        self.assertEqual(policy.time_in_force_preference, "GTC")
        self.assertEqual(policy.price_style, "post_only")
        self.assertEqual(policy.mode, "post_only_with_timeout_fallback")
        self.assertTrue(policy.passive_first)
        self.assertTrue(policy.post_only)
        self.assertFalse(policy.bounded_limit_ioc)
        self.assertFalse(policy.bounded_taker)
        self.assertEqual(policy.limit_offset_bps_preference, Decimal("0.8"))
        self.assertEqual(policy.max_acceptable_cost_bps, 7.5)

    def test_post_only_preserves_urgency_from_caller(self) -> None:
        """urgency 由 caller (close_stale=medium / entry=low) 决定, 不被覆盖."""
        for urgency in ("low", "medium", "high"):
            with self.subTest(urgency=urgency):
                policy = resolve_execution_policy_from_mode(
                    mode="post_only_with_timeout_fallback",
                    edge_strength="medium",
                    urgency=urgency,
                    limit_offset_bps=None,
                    max_acceptable_cost_bps=None,
                    policy_reason="test",
                )
                self.assertEqual(policy.urgency, urgency)

    def test_post_only_preserves_edge_strength_from_caller(self) -> None:
        for strength in ("weak", "medium", "strong"):
            with self.subTest(strength=strength):
                policy = resolve_execution_policy_from_mode(
                    mode="post_only_with_timeout_fallback",
                    edge_strength=strength,
                    urgency="medium",
                    limit_offset_bps=None,
                    max_acceptable_cost_bps=None,
                    policy_reason="test",
                )
                self.assertEqual(policy.edge_strength, strength)


class PostOnlyDoesNotAffectOtherModesTests(unittest.TestCase):
    """**H2 regression guard**: 其他 mode 的行为不变."""

    def test_bounded_limit_still_returns_bounded_limit_ioc(self) -> None:
        policy = resolve_execution_policy_from_mode(
            mode="bounded_limit",
            edge_strength="medium",
            urgency="medium",
            limit_offset_bps=Decimal("0.8"),
            max_acceptable_cost_bps=7.5,
            policy_reason="test",
        )
        self.assertEqual(policy.execution_style_preference, "bounded_limit_ioc")
        self.assertEqual(policy.order_type_preference, "limit")
        self.assertEqual(policy.time_in_force_preference, "IOC")
        self.assertTrue(policy.bounded_limit_ioc)
        self.assertFalse(policy.post_only)

    def test_passive_first_still_returns_bounded_limit_ioc(self) -> None:
        policy = resolve_execution_policy_from_mode(
            mode="passive_first",
            edge_strength="medium",
            urgency="low",
            limit_offset_bps=Decimal("0.5"),
            max_acceptable_cost_bps=None,
            policy_reason="test",
        )
        self.assertTrue(policy.bounded_limit_ioc)
        self.assertFalse(policy.post_only)
        self.assertTrue(policy.passive_first)

    def test_bounded_taker_unchanged(self) -> None:
        policy = resolve_execution_policy_from_mode(
            mode="bounded_taker",
            edge_strength="strong",
            urgency="medium",
            limit_offset_bps=None,
            max_acceptable_cost_bps=None,
            policy_reason="test",
        )
        self.assertEqual(policy.order_type_preference, "market")
        self.assertTrue(policy.bounded_taker)
        self.assertFalse(policy.post_only)

    def test_aggressive_bounded_taker_unchanged(self) -> None:
        policy = resolve_execution_policy_from_mode(
            mode="aggressive_bounded_taker",
            edge_strength="strong",
            urgency="high",
            limit_offset_bps=None,
            max_acceptable_cost_bps=None,
            policy_reason="test",
        )
        self.assertEqual(
            policy.execution_style_preference, "aggressive_bounded_taker_cap",
        )
        self.assertTrue(policy.bounded_taker)
        self.assertFalse(policy.post_only)


class PostOnlyCloseStalePathIntegrationTests(unittest.TestCase):
    """resolve_execution_policy (top-level) + close_stale_thesis 路径对接."""

    def _make_close_stale_book(self) -> IndependentBookDecision:
        return IndependentBookDecision(
            leg="long",
            expectancy=IndependentBookExpectancy(
                leg="long",
                expected_signal_edge_bps=3.0,
                expected_slippage_bps=0.5,
                expected_cost_bps=4.1,
                expected_net_edge_bps=-1.6,
            ),
            score=0.22,
            current_qty=Decimal("0.01"),
            target_qty=Decimal("0"),
            state="closing",
            reason_codes=[],
            blocked_reasons=[],
            min_hold_remaining_seconds=0.0,
            rebalance_cooldown_remaining_seconds=0.0,
            book_action="close_stale_thesis",
            close_reason="stale_thesis",
            liquidity_quality_score=0.85,
            execution_health_state="ok",
            weak_edge_report_only=False,
        )

    def test_close_stale_with_post_only_mode_routes_through_new_branch(self) -> None:
        """close_stale_execution_mode=post_only_with_timeout_fallback 时，
        resolve_execution_policy 应该返回 post_only 策略而不是 guarded_exit 默认."""
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_close_stale_execution_mode="post_only_with_timeout_fallback",
            strategy_hedge_independent_limit_offset_bps_stale_close=0.8,
            strategy_hedge_independent_max_acceptable_cost_bps=7.5,
            strategy_hedge_independent_min_safe_net_edge_bps=2.0,
            strategy_hedge_independent_expected_slippage_buffer_bps=1.0,
            strategy_hedge_independent_expected_execution_buffer_bps=1.0,
        )
        book = self._make_close_stale_book()
        policy = resolve_execution_policy(
            settings=settings,
            book=book,
            expectancy_cost_bps=4.1,
            expectancy_net_edge_bps=-1.6,
            expectancy_slippage_bps=0.5,
            required_safe_net_edge_bps=2.0,
        )
        assert policy is not None
        self.assertTrue(policy.post_only)
        self.assertEqual(policy.order_type_preference, "post_only")
        self.assertEqual(policy.time_in_force_preference, "GTC")
        self.assertEqual(policy.mode, "post_only_with_timeout_fallback")
        self.assertEqual(
            policy.policy_reason,
            "independent_stale_thesis_configured_post_only_with_timeout_fallback",
        )
        self.assertEqual(policy.urgency, "medium")  # close_stale 默认 medium

    def test_close_stale_with_bounded_limit_stays_bounded_limit_ioc(self) -> None:
        """回归保护: close_stale 配成原 bounded_limit 时行为不变."""
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_close_stale_execution_mode="bounded_limit",
            strategy_hedge_independent_limit_offset_bps_stale_close=0.8,
            strategy_hedge_independent_max_acceptable_cost_bps=7.5,
            strategy_hedge_independent_min_safe_net_edge_bps=2.0,
            strategy_hedge_independent_expected_slippage_buffer_bps=1.0,
            strategy_hedge_independent_expected_execution_buffer_bps=1.0,
        )
        book = self._make_close_stale_book()
        policy = resolve_execution_policy(
            settings=settings,
            book=book,
            expectancy_cost_bps=4.0,
            expectancy_net_edge_bps=-1.5,
            expectancy_slippage_bps=0.5,
            required_safe_net_edge_bps=2.0,
        )
        assert policy is not None
        self.assertFalse(policy.post_only)
        self.assertTrue(policy.bounded_limit_ioc)
        self.assertEqual(policy.time_in_force_preference, "IOC")


class EntryScaleInUrgencyWithPostOnlyTests(unittest.TestCase):
    """entry/scale_in 若配成 post_only 时 urgency 应落到 low (与 passive_first/bounded_limit 一致).

    Scope 注: evidence doc §1.3 明确 post_only_with_timeout_fallback 只用于 close_stale,
    但若未来扩至 entry/scale_in, urgency 语义要与其他 passive mode 对齐.
    """

    def test_entry_with_post_only_mode_has_low_urgency(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_entry_execution_mode="post_only_with_timeout_fallback",
            strategy_hedge_independent_limit_offset_bps_entry=1.5,
            strategy_hedge_independent_max_acceptable_cost_bps=7.5,
            strategy_hedge_independent_min_safe_net_edge_bps=2.0,
            strategy_hedge_independent_expected_slippage_buffer_bps=1.0,
            strategy_hedge_independent_expected_execution_buffer_bps=1.0,
        )
        book = IndependentBookDecision(
            leg="long",
            expectancy=IndependentBookExpectancy(
                leg="long",
                expected_signal_edge_bps=10.0,
                expected_slippage_bps=1.0,
                expected_cost_bps=4.1,
                expected_net_edge_bps=4.9,
            ),
            score=0.40,
            current_qty=Decimal("0"),
            target_qty=Decimal("0.01"),
            state="opening",
            reason_codes=[],
            blocked_reasons=[],
            min_hold_remaining_seconds=0.0,
            rebalance_cooldown_remaining_seconds=0.0,
            book_action="open",
            liquidity_quality_score=0.90,
            execution_health_state="ok",
            weak_edge_report_only=False,
        )
        policy = resolve_execution_policy(
            settings=settings,
            book=book,
            expectancy_cost_bps=4.1,
            expectancy_net_edge_bps=4.9,
            expectancy_slippage_bps=1.0,
            required_safe_net_edge_bps=2.0,
        )
        assert policy is not None
        self.assertTrue(policy.post_only)
        self.assertEqual(policy.urgency, "low")  # 与 passive_first/bounded_limit 对齐


if __name__ == "__main__":
    unittest.main()
