from __future__ import annotations

import unittest
from decimal import Decimal

from aats.services.strategy_engines.families.independent_family import _independent_execution_policy
from aats.services.strategy_engines.independent.execution_policy import resolve_execution_policy
from aats.services.strategy_engines.independent.models import IndependentBookEvaluation, IndependentBookExpectancy
from tests.support.strategy_family import make_derivatives_hedge_settings


class TestIndependentExecutionPolicy(unittest.TestCase):
    def test_resolve_execution_policy_matches_legacy_open_strong_edge_behavior(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_entry_execution_mode="adaptive",
            strategy_hedge_independent_passive_first_enabled=False,
            strategy_hedge_independent_min_safe_net_edge_bps=2.0,
            strategy_hedge_independent_expected_slippage_buffer_bps=1.0,
            strategy_hedge_independent_expected_execution_buffer_bps=1.0,
        )
        book = IndependentBookEvaluation(
            leg="long",
            expectancy=IndependentBookExpectancy(
                leg="long",
                expected_signal_edge_bps=18.0,
                expected_slippage_bps=1.0,
                expected_cost_bps=4.0,
                expected_net_edge_bps=14.0,
            ),
            score=0.82,
            current_qty=Decimal("0"),
            target_qty=Decimal("0.01"),
            state="opening",
            reason_codes=[],
            blocked_reasons=[],
            min_hold_remaining_seconds=0.0,
            rebalance_cooldown_remaining_seconds=0.0,
            book_action="open",
            liquidity_quality_score=0.95,
            execution_health_state="ok",
            weak_edge_report_only=False,
        )

        extracted = resolve_execution_policy(
            settings=settings,
            book=book,
            expectancy_cost_bps=4.0,
            expectancy_net_edge_bps=14.0,
            expectancy_slippage_bps=1.0,
            required_safe_net_edge_bps=4.0,
        )
        legacy = _independent_execution_policy(settings=settings, book=book)

        self.assertEqual(extracted, legacy)
        assert extracted is not None
        self.assertEqual(extracted.policy_reason, "independent_entry_strong_edge_aggressive")
        self.assertEqual(extracted.urgency, "medium")
        self.assertEqual(extracted.order_type_preference, "market")
        self.assertEqual(extracted.mode, "adaptive_entry_strong_edge_aggressive")
        self.assertEqual(extracted.price_style, "market")
        self.assertTrue(extracted.bounded_taker)

    def test_resolve_execution_policy_matches_legacy_failed_thesis_behavior(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_close_failed_thesis_execution_mode="adaptive",
        )
        book = IndependentBookEvaluation(
            leg="short",
            expectancy=IndependentBookExpectancy(
                leg="short",
                expected_signal_edge_bps=6.0,
                expected_slippage_bps=1.0,
                expected_cost_bps=3.0,
                expected_net_edge_bps=-4.0,
            ),
            score=0.40,
            current_qty=Decimal("0.01"),
            target_qty=Decimal("0"),
            state="closing",
            reason_codes=[],
            blocked_reasons=[],
            min_hold_remaining_seconds=0.0,
            rebalance_cooldown_remaining_seconds=0.0,
            book_action="close_failed_thesis",
            close_reason="failed_thesis",
            liquidity_quality_score=0.90,
            execution_health_state="ok",
        )

        extracted = resolve_execution_policy(
            settings=settings,
            book=book,
            expectancy_cost_bps=3.0,
            expectancy_net_edge_bps=-4.0,
            expectancy_slippage_bps=1.0,
            required_safe_net_edge_bps=4.0,
        )
        legacy = _independent_execution_policy(settings=settings, book=book)

        self.assertEqual(extracted, legacy)
        assert extracted is not None
        self.assertEqual(extracted.policy_reason, "independent_failed_thesis_force_exit")
        self.assertEqual(extracted.urgency, "high")
        self.assertEqual(extracted.mode, "adaptive_failed_thesis_force_exit")
        self.assertEqual(extracted.reason, "independent_failed_thesis_force_exit")


if __name__ == "__main__":
    unittest.main()
