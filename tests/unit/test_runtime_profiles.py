from __future__ import annotations

import unittest

from aats.bootstrap.settings import AATSSettings
from aats.services.operator.runtime_profiles import readonly_runtime_profile_snapshot, runtime_profile_resolution


class TestRuntimeProfiles(unittest.TestCase):
    def test_ai_operating_mode_and_auto_control_are_fully_independent(self) -> None:
        # "AI 运行模式" (ai_operating_mode) 和 "策略档位自动换档"
        # (strategy_profile_auto_control_enabled) 是两个正交的开关：
        # 一个控制 AI 在单次决策里扮演什么角色，另一个控制 6 个策略档位
        # 是否由系统自动评估/切换。两者必须独立拍板——AI 模式是什么值，
        # 不应该隐式影响换档开关；反之亦然。
        for ai_mode in ("baseline_only", "ai_assisted", "ai_decision_maker"):
            with self.subTest(ai_mode=ai_mode, auto_control=False):
                settings = AATSSettings.model_validate(
                    {
                        "ai_operating_mode": ai_mode,
                        "strategy_profile_auto_control_enabled": False,
                    }
                )
                self.assertFalse(settings.strategy_profile_auto_control_configured)
            with self.subTest(ai_mode=ai_mode, auto_control=True):
                settings = AATSSettings.model_validate(
                    {
                        "ai_operating_mode": ai_mode,
                        "strategy_profile_auto_control_enabled": True,
                    }
                )
                self.assertTrue(settings.strategy_profile_auto_control_configured)

    def test_resolution_is_env_only(self) -> None:
        settings = AATSSettings.model_validate({"default_symbol": "BTC-USDT"})

        resolution = runtime_profile_resolution(settings=settings)

        self.assertEqual(resolution.profile_source, "env_only")
        self.assertEqual(resolution.resolved_settings["default_symbol"], "BTC-USDT")

    def test_spot_runtime_profile_snapshot_hides_derivatives_only_fields(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cash",
                "default_symbol": "BTC-USDT",
            }
        )

        snapshot = readonly_runtime_profile_snapshot(
            settings=settings,
            resolution=runtime_profile_resolution(settings=settings),
        )

        payload = snapshot["current_runtime_payload"]
        summary = snapshot["current_runtime_summary"]
        self.assertNotIn("strategy_short_bias_enabled", payload)
        self.assertNotIn("strategy_dynamic_leverage_enabled", payload)
        self.assertNotIn("max_target_leverage", payload)
        self.assertNotIn("default_target_leverage", payload)
        self.assertNotIn("derivatives_position_mode", payload)
        self.assertNotIn("derivatives_hedge_transition_mode", payload)
        self.assertNotIn("derivatives_require_exchange_pos_mode_match", payload)
        self.assertNotIn("strategy_short_bias_enabled", summary)
        self.assertNotIn("strategy_dynamic_leverage_enabled", summary)
        self.assertNotIn("max_target_leverage", summary)
        self.assertNotIn("default_target_leverage", summary)
        self.assertNotIn("derivatives_position_mode", summary)
        self.assertNotIn("derivatives_hedge_transition_mode", summary)
        self.assertNotIn("derivatives_require_exchange_pos_mode_match", summary)

    def test_derivatives_runtime_profile_snapshot_keeps_derivatives_only_fields(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "derivatives_position_mode": "hedge",
                "derivatives_hedge_transition_mode": "close_then_open",
                "derivatives_require_exchange_pos_mode_match": True,
                "strategy_short_bias_enabled": True,
                "strategy_dynamic_leverage_enabled": True,
                "max_target_leverage": 5.0,
                "default_target_leverage": 3.0,
            }
        )

        snapshot = readonly_runtime_profile_snapshot(
            settings=settings,
            resolution=runtime_profile_resolution(settings=settings),
        )

        payload = snapshot["current_runtime_payload"]
        summary = snapshot["current_runtime_summary"]
        self.assertTrue(payload["strategy_short_bias_enabled"])
        self.assertTrue(payload["strategy_dynamic_leverage_enabled"])
        self.assertEqual(payload["derivatives_position_mode"], "hedge")
        self.assertEqual(payload["derivatives_hedge_transition_mode"], "close_then_open")
        self.assertTrue(payload["derivatives_require_exchange_pos_mode_match"])
        self.assertEqual(payload["max_target_leverage"], 5.0)
        self.assertEqual(payload["default_target_leverage"], 3.0)
        self.assertTrue(summary["strategy_short_bias_enabled"])
        self.assertTrue(summary["strategy_dynamic_leverage_enabled"])
        self.assertEqual(summary["derivatives_position_mode"], "hedge")
        self.assertEqual(summary["derivatives_hedge_transition_mode"], "close_then_open")
        self.assertTrue(summary["derivatives_require_exchange_pos_mode_match"])
        self.assertEqual(summary["max_target_leverage"], 5.0)
        self.assertEqual(summary["default_target_leverage"], 3.0)


if __name__ == "__main__":
    unittest.main()
