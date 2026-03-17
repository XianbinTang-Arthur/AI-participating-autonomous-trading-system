from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from aats.bootstrap.config import load_settings
from aats.bootstrap.settings import AATSSettings


def _non_aats_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AATS_")
    }


class TestAATSSettings(unittest.TestCase):
    def test_model_validate_dict_ignores_process_environment(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AATS_MODE": "guarded_live",
                "AATS_MAX_NOTIONAL_PER_SYMBOL": "20",
                "AATS_LIVE_SUBMIT_ENABLED": "true",
            },
            clear=False,
        ):
            settings = AATSSettings.model_validate({"execution_backend": "paper"})

        self.assertEqual(settings.mode, "paper_live")
        self.assertEqual(settings.max_notional_per_symbol, 1_000.0)
        self.assertFalse(settings.live_submit_enabled)
        self.assertEqual(settings.execution_backend, "paper")

    def test_load_settings_preserves_yaml_profile_values_when_env_does_not_override(self) -> None:
        with patch.object(AATSSettings, "model_config", {**AATSSettings.model_config, "env_file": None}):
            with patch.dict(
                os.environ,
                {
                    **_non_aats_environment(),
                    "AATS_CONFIG_PROFILE": "guarded_simulated_submit_enabled",
                },
                clear=True,
            ):
                settings = load_settings()

        self.assertEqual(settings.config_profile, "guarded_simulated_submit_enabled")
        self.assertEqual(settings.decision_min_interval_seconds_15m, 30.0)
        self.assertEqual(settings.decision_min_interval_seconds_1h, 120.0)
        self.assertEqual(settings.decision_min_price_move_bps, 4.0)
        self.assertEqual(settings.decision_min_momentum_delta, 0.0005)

    def test_load_settings_allows_explicit_env_override_on_top_of_yaml_profile(self) -> None:
        with patch.object(AATSSettings, "model_config", {**AATSSettings.model_config, "env_file": None}):
            with patch.dict(
                os.environ,
                {
                    **_non_aats_environment(),
                    "AATS_CONFIG_PROFILE": "guarded_simulated_submit_enabled",
                    "AATS_DECISION_MIN_INTERVAL_SECONDS_15M": "5",
                },
                clear=True,
            ):
                settings = load_settings()

        self.assertEqual(settings.decision_min_interval_seconds_15m, 5.0)
        self.assertEqual(settings.decision_min_interval_seconds_1h, 120.0)


if __name__ == "__main__":
    unittest.main()
