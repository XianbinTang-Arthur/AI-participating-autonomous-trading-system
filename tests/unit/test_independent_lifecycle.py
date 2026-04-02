from __future__ import annotations

import unittest
from decimal import Decimal

from aats.services.strategy_engines.families.independent_family import (
    _independent_close_reason,
    _independent_de_risk_target_qty,
    _independent_min_hold_remaining_seconds,
    _independent_thesis_age_seconds,
)
from aats.services.strategy_engines.independent.lifecycle import (
    compute_de_risk_target_qty,
    compute_thesis_age_seconds,
    determine_close_reason,
    min_hold_remaining_seconds,
)
from tests.support.strategy_family import make_context, make_derivatives_hedge_settings


class TestIndependentLifecycle(unittest.TestCase):
    def test_compute_thesis_age_seconds_matches_legacy_wrapper(self) -> None:
        context = make_context(
            current_long_position_qty=0.02,
            current_exposure_side="long",
            current_long_leg_opened_seconds_ago=300,
        )

        extracted = compute_thesis_age_seconds(
            context=context,
            leg="long",
            current_qty=Decimal("0.02"),
        )
        legacy = _independent_thesis_age_seconds(
            context=context,
            leg="long",
            current_qty=Decimal("0.02"),
        )

        self.assertEqual(extracted, legacy)
        self.assertEqual(extracted, 300.0)

    def test_determine_close_reason_matches_legacy_failed_thesis_behavior(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_failed_thesis_net_edge_bps=-1.0,
            strategy_hedge_independent_de_risk_net_edge_bps=2.0,
        )

        extracted = determine_close_reason(
            settings=settings,
            score=0.70,
            close_threshold=0.50,
            expected_net_edge_bps=-2.0,
            liquidity_quality_score=0.95,
            execution_health_state="ok",
            age_seconds=120.0,
        )
        legacy = _independent_close_reason(
            settings=settings,
            score=0.70,
            close_threshold=0.50,
            expected_net_edge_bps=-2.0,
            liquidity_quality_score=0.95,
            execution_health_state="ok",
            age_seconds=120.0,
        )

        self.assertEqual(extracted, legacy)
        self.assertEqual(extracted, "failed_thesis")

    def test_compute_de_risk_target_qty_matches_legacy_wrapper(self) -> None:
        extracted = compute_de_risk_target_qty(
            current_qty=Decimal("0.08"),
            directional_leg_target_qty=Decimal("0.03"),
        )
        legacy = _independent_de_risk_target_qty(
            current_qty=Decimal("0.08"),
            directional_leg_target_qty=Decimal("0.03"),
        )
        self.assertEqual(extracted, legacy)
        self.assertEqual(extracted, Decimal("0.03"))

    def test_min_hold_remaining_seconds_matches_legacy_wrapper(self) -> None:
        settings = make_derivatives_hedge_settings(strategy_hedge_independent_long_min_hold_seconds=600.0)
        context = make_context(
            current_long_position_qty=0.02,
            current_exposure_side="long",
            current_long_leg_opened_seconds_ago=300,
        )

        extracted = min_hold_remaining_seconds(
            settings=settings,
            context=context,
            leg="long",
        )
        legacy = _independent_min_hold_remaining_seconds(
            settings=settings,
            context=context,
            leg="long",
        )

        self.assertEqual(extracted, legacy)
        self.assertEqual(extracted, 300.0)


if __name__ == "__main__":
    unittest.main()
