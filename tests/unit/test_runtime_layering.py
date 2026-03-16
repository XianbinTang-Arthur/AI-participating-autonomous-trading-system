from __future__ import annotations

import unittest

from aats.bootstrap.settings import AATSSettings
from aats.services.governance_engine.runtime_layers import resolve_runtime_layering


class TestRuntimeLayering(unittest.TestCase):
    def test_paper_local_profile_resolution(self) -> None:
        layering = resolve_runtime_layering(
            AATSSettings.model_validate(
                {
                    "mode": "paper_live",
                    "market_data_backend": "demo",
                    "execution_backend": "paper",
                    "account_backend": "disabled",
                    "account_read_enabled": False,
                }
            )
        )

        self.assertEqual(layering.runtime_profile.name, "paper_local")
        self.assertEqual(layering.operating_state, "local_demo")
        self.assertEqual(layering.environment_capabilities.execution_adapter_kind, "paper")
        self.assertFalse(layering.environment_capabilities.exchange_submission_possible)
        self.assertFalse(layering.recovery_policy.operator_rebaseline_supported)

    def test_exchange_simulated_profile_resolution(self) -> None:
        layering = resolve_runtime_layering(
            AATSSettings.model_validate(
                {
                    "mode": "guarded_live",
                    "market_data_backend": "okx",
                    "execution_backend": "okx",
                    "account_backend": "okx",
                    "account_read_enabled": True,
                    "okx_simulated_trading": True,
                    "live_submit_enabled": True,
                    "guarded_execution_dry_run": False,
                    "bootstrap_portfolio_from_exchange": True,
                }
            )
        )

        self.assertEqual(layering.runtime_profile.name, "exchange_simulated_spot")
        self.assertEqual(layering.operating_state, "guarded_simulated_submit_spot_enabled")
        self.assertTrue(layering.environment_capabilities.exchange_submission_enabled)
        self.assertTrue(layering.policy_profile.exchange_submission_allowed_in_principle)
        self.assertTrue(layering.recovery_policy.operator_rebaseline_supported)
        self.assertEqual(layering.environment_capabilities.position_directionality, "long_only")
        self.assertEqual(layering.environment_capabilities.leverage_support, "none")

    def test_exchange_live_reserved_profile_remains_blocked(self) -> None:
        layering = resolve_runtime_layering(
            AATSSettings.model_validate(
                {
                    "mode": "guarded_live",
                    "market_data_backend": "okx",
                    "execution_backend": "okx",
                    "account_backend": "okx",
                    "account_read_enabled": True,
                    "okx_simulated_trading": False,
                    "live_submit_enabled": True,
                    "guarded_execution_dry_run": False,
                }
            )
        )

        self.assertEqual(layering.runtime_profile.name, "exchange_live_reserved")
        self.assertEqual(layering.operating_state, "guarded_live_enabled")
        self.assertTrue(layering.runtime_profile.live_trading_blocked)
        self.assertTrue(layering.policy_profile.real_money_submission_structurally_blocked)
        self.assertFalse(layering.environment_capabilities.exchange_submission_enabled)

    def test_exchange_simulated_derivatives_profile_resolution(self) -> None:
        layering = resolve_runtime_layering(
            AATSSettings.model_validate(
                {
                    "mode": "guarded_live",
                    "market_data_backend": "okx",
                    "execution_backend": "okx",
                    "account_backend": "okx",
                    "account_read_enabled": True,
                    "okx_simulated_trading": True,
                    "live_submit_enabled": True,
                    "guarded_execution_dry_run": False,
                    "bootstrap_portfolio_from_exchange": True,
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "max_target_leverage": 3.0,
                }
            )
        )

        self.assertEqual(layering.runtime_profile.name, "exchange_simulated_derivatives")
        self.assertEqual(layering.operating_state, "guarded_simulated_submit_derivatives_enabled")
        self.assertEqual(layering.environment_capabilities.position_directionality, "bi_directional")
        self.assertEqual(layering.environment_capabilities.leverage_support, "supported")
        self.assertEqual(layering.environment_capabilities.margin_model, "cross")
        self.assertTrue(layering.policy_profile.shorting_allowed)
        self.assertTrue(layering.policy_profile.leverage_allowed)
        self.assertEqual(layering.policy_profile.max_target_leverage, 3.0)


if __name__ == "__main__":
    unittest.main()
