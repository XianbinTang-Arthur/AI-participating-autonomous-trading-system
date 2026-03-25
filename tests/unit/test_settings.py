from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from aats.bootstrap.config import load_settings
from aats.bootstrap.settings import AATSSettings, is_placeholder_config_value


def _non_aats_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AATS_")
    }


class TestAATSSettings(unittest.TestCase):
    def test_placeholder_config_detection_marks_common_live_templates_as_unconfigured(self) -> None:
        self.assertTrue(is_placeholder_config_value("REPLACE_WITH_REAL_OKX_API_KEY"))
        self.assertTrue(is_placeholder_config_value("change_me_secret"))
        self.assertTrue(is_placeholder_config_value("<fill-me>"))
        self.assertFalse(is_placeholder_config_value("postgresql+psycopg://aats:aats@localhost:5432/aats"))
        self.assertFalse(is_placeholder_config_value("real-secret"))

    def test_placeholder_okx_credentials_do_not_count_as_configured(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "okx_api_key": "REPLACE_WITH_REAL_OKX_API_KEY",
                "okx_api_secret": "REPLACE_WITH_REAL_OKX_API_SECRET",
                "okx_api_passphrase": "REPLACE_WITH_REAL_OKX_API_PASSPHRASE",
                "database_url": "REPLACE_WITH_REAL_DATABASE_URL",
            }
        )

        self.assertFalse(settings.okx_credentials_configured)
        self.assertFalse(settings.database_url_configured)

    def test_placeholder_operator_session_secret_does_not_count_as_configured(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "operator_session_secret": "REPLACE_WITH_LONG_RANDOM_OPERATOR_SESSION_SECRET",
                "openai_api_key": "CHANGE_ME_OPENAI_KEY",
                "ai_provider": "openai",
            }
        )

        self.assertFalse(settings.operator_session_configured)
        self.assertFalse(settings.ai_provider_configured)

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

    def test_load_settings_reads_dedicated_derivatives_profile_and_startup_marker(self) -> None:
        with patch.object(AATSSettings, "model_config", {**AATSSettings.model_config, "env_file": None}):
            with patch.dict(
                os.environ,
                {
                    **_non_aats_environment(),
                    "AATS_CONFIG_PROFILE": "guarded_derivatives_enabled",
                    "AATS_STARTUP_PROFILE": "derivatives",
                },
                clear=True,
            ):
                settings = load_settings()

        self.assertEqual(settings.config_profile, "guarded_derivatives_enabled")
        self.assertEqual(settings.startup_profile, "derivatives")
        self.assertEqual(settings.mode, "guarded_live")
        self.assertEqual(settings.market_data_backend, "okx")
        self.assertEqual(settings.execution_backend, "okx")
        self.assertEqual(settings.account_backend, "okx")
        self.assertTrue(settings.account_read_enabled)
        self.assertEqual(settings.trading_product_type, "derivatives")
        self.assertEqual(settings.margin_mode, "cross")
        self.assertEqual(settings.max_gross_notional_per_symbol, 2500.0)
        self.assertEqual(settings.max_pending_notional_per_symbol, 1250.0)
        self.assertEqual(settings.max_total_open_notional, 5000.0)
        self.assertEqual(settings.max_daily_realized_loss_usdt, 100.0)
        self.assertEqual(settings.derivatives_only_reduce_trigger_margin_fraction, 0.7)

    def test_load_settings_reads_managed_profile_runtime_defaults_and_strategy_tuning(self) -> None:
        with patch.object(AATSSettings, "model_config", {**AATSSettings.model_config, "env_file": None}):
            with patch.dict(
                os.environ,
                {
                    **_non_aats_environment(),
                    "AATS_ENV_TEMPLATE_PROFILE": "spot_live",
                },
                clear=True,
            ):
                settings = load_settings()

        self.assertEqual(settings.env_template_profile, "spot_live")
        self.assertEqual(settings.config_profile, "guarded_spot_enabled")
        self.assertEqual(settings.startup_profile, "spot")
        self.assertEqual(settings.mode, "guarded_live")
        self.assertEqual(settings.market_data_backend, "okx")
        self.assertEqual(settings.execution_backend, "okx")
        self.assertEqual(settings.account_backend, "okx")
        self.assertEqual(settings.ai_execution_suggestion_mode, "diagnostic_only")
        self.assertFalse(settings.strategy_profile_auto_control_enabled)
        self.assertEqual(settings.decision_min_interval_seconds_15m, 60.0)
        self.assertEqual(settings.strategy_min_hold_seconds, 900.0)

    def test_load_settings_bootstraps_managed_derivatives_profile_before_runtime_validation(self) -> None:
        with patch.object(AATSSettings, "model_config", {**AATSSettings.model_config, "env_file": None}):
            with patch.dict(
                os.environ,
                {
                    **_non_aats_environment(),
                    "AATS_ENV_TEMPLATE_PROFILE": "derivatives_live",
                    "AATS_DEFAULT_ORDER_QTY": "0.01",
                    "AATS_MAX_ABS_POSITION_QTY": "0.02",
                    "AATS_MAX_NOTIONAL_PER_SYMBOL": "1000",
                    "AATS_MAX_TARGET_LEVERAGE": "10",
                    "AATS_DEFAULT_TARGET_LEVERAGE": "10",
                },
                clear=True,
            ):
                settings = load_settings()

        self.assertEqual(settings.env_template_profile, "derivatives_live")
        self.assertEqual(settings.trading_product_type, "derivatives")
        self.assertEqual(settings.margin_mode, "cross")
        self.assertEqual(settings.max_target_leverage, 10.0)
        self.assertEqual(settings.default_target_leverage, 10.0)

    def test_load_settings_ignores_deprecated_runtime_derivations_from_managed_env(self) -> None:
        with patch.object(AATSSettings, "model_config", {**AATSSettings.model_config, "env_file": None}):
            with patch.dict(
                os.environ,
                {
                    **_non_aats_environment(),
                    "AATS_ENV_TEMPLATE_PROFILE": "derivatives_live",
                    "AATS_MODE": "paper_live",
                    "AATS_TRADING_PRODUCT_TYPE": "spot",
                    "AATS_MARGIN_MODE": "cash",
                    "AATS_MARKET_DATA_BACKEND": "demo",
                    "AATS_DEFAULT_SYMBOL": "ETH-USDT-SWAP",
                },
                clear=True,
            ):
                settings = load_settings()

        self.assertEqual(settings.env_template_profile, "derivatives_live")
        self.assertEqual(settings.mode, "guarded_live")
        self.assertEqual(settings.trading_product_type, "derivatives")
        self.assertEqual(settings.margin_mode, "cross")
        self.assertEqual(settings.market_data_backend, "okx")
        self.assertEqual(settings.default_symbol, "ETH-USDT-SWAP")

    def test_spot_cash_runtime_rejects_non_unit_leverage(self) -> None:
        with self.assertRaisesRegex(ValueError, "spot_cash_runtime_requires_unit_leverage"):
            AATSSettings.model_validate(
                {
                    "trading_product_type": "spot",
                    "margin_mode": "cash",
                    "max_target_leverage": 3,
                    "default_target_leverage": 1,
                }
            )

    def test_current_runtime_rejects_non_implemented_timeframe_overrides(self) -> None:
        with self.assertRaisesRegex(ValueError, "primary_timeframe_currently_must_be_15m"):
            AATSSettings.model_validate({"primary_timeframe": "1h"})
        with self.assertRaisesRegex(ValueError, "secondary_timeframe_currently_must_be_1h"):
            AATSSettings.model_validate({"secondary_timeframe": "15m"})


if __name__ == "__main__":
    unittest.main()
