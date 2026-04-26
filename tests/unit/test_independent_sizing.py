from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path

import yaml

from aats.services.strategy_engines.independent.sizing import (
    build_sizing_outcome,
    compute_entry_target_qty,
    compute_size_down_multiplier,
)
from tests.support.strategy_family import make_derivatives_hedge_settings


class TestIndependentSizing(unittest.TestCase):
    def test_compute_entry_target_qty_preserves_legacy_max_default_vs_directional(self) -> None:
        settings = make_derivatives_hedge_settings(default_order_qty=0.01)
        self.assertEqual(
            compute_entry_target_qty(
                settings=settings,
                directional_leg_target_qty=Decimal("0.03"),
            ),
            Decimal("0.03"),
        )

    def test_compute_entry_target_qty_treats_balance_reference_as_family_gross_budget(self) -> None:
        settings = make_derivatives_hedge_settings(default_order_qty=0.004)

        self.assertEqual(
            compute_entry_target_qty(
                settings=settings,
                directional_leg_target_qty=Decimal("0"),
                balance_reference_qty=Decimal("0.014625"),
            ),
            Decimal("0.0073125"),
        )

    def test_compute_entry_target_qty_uses_legacy_base_when_balance_reference_is_zero(self) -> None:
        settings = make_derivatives_hedge_settings(default_order_qty=0.004)

        self.assertEqual(
            compute_entry_target_qty(
                settings=settings,
                directional_leg_target_qty=Decimal("0.03"),
                balance_reference_qty=Decimal("0"),
            ),
            Decimal("0.03"),
        )

    def test_build_sizing_outcome_marks_de_risk_multiplier(self) -> None:
        outcome = build_sizing_outcome(
            book_action="de_risk",
            current_qty=Decimal("0.04"),
            target_qty=Decimal("0.02"),
            base_target_qty=Decimal("0.04"),
        )
        self.assertEqual(outcome.size_multiplier, compute_size_down_multiplier(current_qty=Decimal("0.04"), target_qty=Decimal("0.02")))
        self.assertEqual(outcome.capital_multiplier, Decimal("0.5"))

    def test_derivatives_live_independent_scale_in_thresholds_exceed_entry_thresholds(self) -> None:
        profile = yaml.safe_load(
            Path("configs/strategy_profiles/derivatives_live.yaml").read_text(encoding="utf-8")
        )

        self.assertGreater(
            profile["strategy_hedge_independent_long_scale_in_threshold"],
            profile["strategy_hedge_independent_long_entry_threshold"],
        )
        self.assertGreater(
            profile["strategy_hedge_independent_short_scale_in_threshold"],
            profile["strategy_hedge_independent_short_entry_threshold"],
        )


if __name__ == "__main__":
    unittest.main()
