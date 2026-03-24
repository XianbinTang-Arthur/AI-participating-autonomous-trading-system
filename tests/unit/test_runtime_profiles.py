from __future__ import annotations

import unittest

from aats.bootstrap.settings import AATSSettings
from aats.services.operator.runtime_profiles import runtime_profile_resolution


class TestRuntimeProfiles(unittest.TestCase):
    def test_strategy_profile_auto_control_is_disabled_by_default(self) -> None:
        settings = AATSSettings.model_validate({"ai_operating_mode": "ai_decision_maker_with_profile_control"})
        self.assertFalse(settings.strategy_profile_auto_control_configured)
        self.assertFalse(settings.strategy_profile_auto_control_is_enabled_for_mode("ai_decision_maker_with_profile_control"))

    def test_strategy_profile_auto_control_can_be_enabled_independently(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "ai_operating_mode": "baseline_only",
                "strategy_profile_auto_control_enabled": True,
            }
        )
        self.assertTrue(settings.strategy_profile_auto_control_configured)
        self.assertTrue(settings.strategy_profile_auto_control_is_enabled_for_mode("baseline_only"))

    def test_resolution_is_env_only(self) -> None:
        settings = AATSSettings.model_validate({"default_symbol": "BTC-USDT"})

        resolution = runtime_profile_resolution(settings=settings)

        self.assertEqual(resolution.profile_source, "env_only")
        self.assertEqual(resolution.resolved_settings["default_symbol"], "BTC-USDT")


if __name__ == "__main__":
    unittest.main()
