from __future__ import annotations

import unittest

from aats.bootstrap.settings import AATSSettings
from aats.schemas.strategy_profiles import (
    StrategyProfilePayload,
    strategy_profile_axes_from_payload,
    strategy_profile_payload_from_settings,
    summarize_strategy_profile_payload,
)


class StrategyProfilePayloadSchemaTests(unittest.TestCase):
    @staticmethod
    def _base_payload() -> dict[str, object]:
        settings = AATSSettings.model_validate({})
        return strategy_profile_payload_from_settings(settings).model_dump(mode="python")

    def test_payload_backfills_missing_short_thresholds_from_legacy_long_fields(self) -> None:
        raw = self._base_payload()
        for field in (
            "strategy_short_entry_allowed_regimes",
            "strategy_short_entry_min_signal_edge_bps",
            "strategy_short_entry_alpha_min",
            "strategy_short_entry_confidence_min",
            "strategy_short_scale_in_min_signal_edge_bps",
            "strategy_short_scale_in_alpha_min",
            "strategy_short_scale_in_confidence_min",
            "strategy_short_reversal_min_signal_edge_bps",
            "strategy_short_reversal_alpha_min",
            "strategy_short_reversal_confidence_min",
        ):
            raw.pop(field)

        payload = StrategyProfilePayload.model_validate(raw)

        self.assertEqual(payload.strategy_short_entry_allowed_regimes, payload.strategy_entry_allowed_regimes)
        self.assertEqual(payload.strategy_short_entry_min_signal_edge_bps, payload.strategy_entry_min_signal_edge_bps)
        self.assertEqual(payload.strategy_short_entry_alpha_min, payload.strategy_entry_alpha_min)
        self.assertEqual(payload.strategy_short_entry_confidence_min, payload.strategy_entry_confidence_min)
        self.assertEqual(payload.strategy_short_scale_in_min_signal_edge_bps, payload.strategy_scale_in_min_signal_edge_bps)
        self.assertEqual(payload.strategy_short_scale_in_alpha_min, payload.strategy_scale_in_alpha_min)
        self.assertEqual(payload.strategy_short_scale_in_confidence_min, payload.strategy_scale_in_confidence_min)
        self.assertEqual(payload.strategy_short_reversal_min_signal_edge_bps, payload.strategy_reversal_min_signal_edge_bps)
        self.assertEqual(payload.strategy_short_reversal_alpha_min, payload.strategy_reversal_alpha_min)
        self.assertEqual(payload.strategy_short_reversal_confidence_min, payload.strategy_reversal_confidence_min)

    def test_profile_summary_includes_short_confidence_thresholds(self) -> None:
        summary = summarize_strategy_profile_payload(self._base_payload())

        self.assertIn("strategy_entry_confidence_min", summary)
        self.assertIn("strategy_short_entry_confidence_min", summary)
        self.assertIn("strategy_scale_in_confidence_min", summary)
        self.assertIn("strategy_short_scale_in_confidence_min", summary)
        self.assertIn("strategy_reversal_confidence_min", summary)
        self.assertIn("strategy_short_reversal_confidence_min", summary)

    def test_axes_consider_stricter_short_side_alpha_thresholds(self) -> None:
        raw = self._base_payload()
        raw.update(
            {
                "strategy_entry_alpha_min": 0.16,
                "strategy_short_entry_alpha_min": 0.28,
                "strategy_scale_in_alpha_min": 0.20,
                "strategy_short_scale_in_alpha_min": 0.34,
                "strategy_reversal_alpha_min": 0.26,
                "strategy_short_reversal_alpha_min": 0.40,
            }
        )

        axes = strategy_profile_axes_from_payload(raw)

        self.assertEqual(axes.entry_threshold, "strict")
        self.assertEqual(axes.scale_in_threshold, "strict")
        self.assertEqual(axes.reversal_threshold, "strict")

    def test_spot_payload_from_settings_normalizes_short_fields_back_to_shared_thresholds(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cash",
                "strategy_entry_allowed_regimes": ("trend", "breakout"),
                "strategy_entry_min_signal_edge_bps": 14.0,
                "strategy_entry_alpha_min": 0.18,
                "strategy_entry_confidence_min": 0.63,
                "strategy_scale_in_min_signal_edge_bps": 18.0,
                "strategy_scale_in_alpha_min": 0.24,
                "strategy_scale_in_confidence_min": 0.71,
                "strategy_reversal_min_signal_edge_bps": 24.0,
                "strategy_reversal_alpha_min": 0.34,
                "strategy_reversal_confidence_min": 0.79,
                "strategy_short_entry_allowed_regimes": ("uncertain",),
                "strategy_short_entry_min_signal_edge_bps": 11.0,
                "strategy_short_entry_alpha_min": 0.15,
                "strategy_short_entry_confidence_min": 0.55,
                "strategy_short_scale_in_min_signal_edge_bps": 16.0,
                "strategy_short_scale_in_alpha_min": 0.20,
                "strategy_short_scale_in_confidence_min": 0.64,
                "strategy_short_reversal_min_signal_edge_bps": 14.0,
                "strategy_short_reversal_alpha_min": 0.18,
                "strategy_short_reversal_confidence_min": 0.55,
            }
        )

        payload = strategy_profile_payload_from_settings(settings)

        self.assertEqual(payload.strategy_short_entry_allowed_regimes, payload.strategy_entry_allowed_regimes)
        self.assertEqual(payload.strategy_short_entry_min_signal_edge_bps, payload.strategy_entry_min_signal_edge_bps)
        self.assertEqual(payload.strategy_short_entry_alpha_min, payload.strategy_entry_alpha_min)
        self.assertEqual(payload.strategy_short_entry_confidence_min, payload.strategy_entry_confidence_min)
        self.assertEqual(payload.strategy_short_scale_in_min_signal_edge_bps, payload.strategy_scale_in_min_signal_edge_bps)
        self.assertEqual(payload.strategy_short_scale_in_alpha_min, payload.strategy_scale_in_alpha_min)
        self.assertEqual(payload.strategy_short_scale_in_confidence_min, payload.strategy_scale_in_confidence_min)
        self.assertEqual(payload.strategy_short_reversal_min_signal_edge_bps, payload.strategy_reversal_min_signal_edge_bps)
        self.assertEqual(payload.strategy_short_reversal_alpha_min, payload.strategy_reversal_alpha_min)
        self.assertEqual(payload.strategy_short_reversal_confidence_min, payload.strategy_reversal_confidence_min)

    def test_spot_profile_summary_omits_derivatives_only_short_fields(self) -> None:
        settings = AATSSettings.model_validate({"trading_product_type": "spot", "margin_mode": "cash"})

        payload = strategy_profile_payload_from_settings(settings)
        summary = summarize_strategy_profile_payload(payload, product_type="spot")

        self.assertIn("strategy_entry_confidence_min", summary)
        self.assertNotIn("strategy_short_entry_confidence_min", summary)
        self.assertNotIn("strategy_short_scale_in_confidence_min", summary)
        self.assertNotIn("strategy_short_reversal_confidence_min", summary)

    def test_spot_axes_ignore_stricter_short_side_alpha_thresholds(self) -> None:
        raw = self._base_payload()
        raw.update(
            {
                "strategy_entry_alpha_min": 0.16,
                "strategy_short_entry_alpha_min": 0.32,
                "strategy_scale_in_alpha_min": 0.20,
                "strategy_short_scale_in_alpha_min": 0.36,
                "strategy_reversal_alpha_min": 0.26,
                "strategy_short_reversal_alpha_min": 0.42,
            }
        )

        axes = strategy_profile_axes_from_payload(raw, product_type="spot")

        self.assertEqual(axes.entry_threshold, "relaxed")
        self.assertEqual(axes.scale_in_threshold, "relaxed")
        self.assertEqual(axes.reversal_threshold, "relaxed")


if __name__ == "__main__":
    unittest.main()
