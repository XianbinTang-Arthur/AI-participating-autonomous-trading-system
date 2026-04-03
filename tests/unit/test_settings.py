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

    def test_operator_session_cookie_secure_defaults_to_true(self) -> None:
        settings = AATSSettings.model_validate({})

        self.assertTrue(settings.operator_session_cookie_secure)

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
        self.assertEqual(settings.decision_min_interval_seconds_15m, 60.0)
        self.assertEqual(settings.decision_min_interval_seconds_1h, 240.0)
        self.assertEqual(settings.decision_min_price_move_bps, 4.0)
        self.assertEqual(settings.decision_min_momentum_delta, 0.0003)

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
        self.assertEqual(settings.decision_min_interval_seconds_1h, 240.0)

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
        self.assertEqual(settings.strategy_min_hold_seconds, 720.0)

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
        self.assertEqual(settings.derivatives_position_mode, "hedge")
        self.assertEqual(settings.derivatives_hedge_transition_mode, "close_then_open")
        self.assertTrue(settings.derivatives_require_exchange_pos_mode_match)

    def test_independent_execution_policy_settings_default_to_adaptive_modes(self) -> None:
        settings = AATSSettings.model_validate({})

        self.assertEqual(settings.strategy_hedge_independent_entry_execution_mode, "adaptive")
        self.assertEqual(settings.strategy_hedge_independent_scale_in_execution_mode, "adaptive")
        self.assertEqual(settings.strategy_hedge_independent_de_risk_execution_mode, "adaptive")
        self.assertEqual(settings.strategy_hedge_independent_close_failed_thesis_execution_mode, "adaptive")
        self.assertEqual(settings.strategy_hedge_independent_close_stale_execution_mode, "adaptive")
        self.assertEqual(settings.strategy_hedge_independent_limit_offset_bps_entry, 1.5)
        self.assertEqual(settings.strategy_hedge_independent_limit_offset_bps_scale_in, 1.0)
        self.assertEqual(settings.strategy_hedge_independent_limit_offset_bps_stale_close, 0.8)

    def test_independent_execution_policy_offsets_reject_negative_values(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "strategy_hedge_independent_limit_offset_bps_entry_must_be_non_negative",
        ):
            AATSSettings.model_validate({"strategy_hedge_independent_limit_offset_bps_entry": -0.1})

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

    def test_strategy_sleeve_auto_execution_new_key_takes_precedence_over_deprecated_key(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "strategy_sleeve_auto_parallel_enabled": False,
                "strategy_sleeve_auto_execution_enabled": True,
            }
        )

        self.assertTrue(settings.effective_strategy_sleeve_auto_execution_enabled)
        self.assertTrue(settings.strategy_sleeve_auto_execution_enabled)
        self.assertTrue(settings.strategy_sleeve_auto_parallel_enabled)
        self.assertFalse(settings.strategy_sleeve_auto_execution_uses_deprecated_key)

    def test_strategy_sleeve_auto_execution_old_key_still_works_but_marks_deprecated_source(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "strategy_sleeve_auto_parallel_enabled": False,
            }
        )

        self.assertFalse(settings.effective_strategy_sleeve_auto_execution_enabled)
        self.assertFalse(settings.strategy_sleeve_auto_execution_enabled)
        self.assertFalse(settings.strategy_sleeve_auto_parallel_enabled)
        self.assertTrue(settings.strategy_sleeve_auto_execution_uses_deprecated_key)

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

    def test_spot_runtime_disallows_derivatives_hedge_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "non_derivatives_runtime_disallows_derivatives_hedge_position_mode"):
            AATSSettings.model_validate(
                {
                    "trading_product_type": "spot",
                    "margin_mode": "cash",
                    "derivatives_position_mode": "hedge",
                }
            )

    def test_derivatives_hedge_mode_requires_margin_runtime(self) -> None:
        with self.assertRaisesRegex(ValueError, "derivatives_hedge_position_mode_requires_margin_runtime"):
            AATSSettings.model_validate(
                {
                    "trading_product_type": "derivatives",
                    "margin_mode": "cash",
                    "derivatives_position_mode": "hedge",
                }
            )

    def test_current_runtime_rejects_non_implemented_timeframe_overrides(self) -> None:
        with self.assertRaisesRegex(ValueError, "primary_timeframe_currently_must_be_15m"):
            AATSSettings.model_validate({"primary_timeframe": "1h"})
        with self.assertRaisesRegex(ValueError, "secondary_timeframe_currently_must_be_1h"):
            AATSSettings.model_validate({"secondary_timeframe": "15m"})

    def test_allowed_regime_lists_ignore_blank_entries(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "strategy_entry_allowed_regimes": ["trend", None, " breakout ", ""],
                "strategy_short_entry_allowed_regimes": ["trend", "", None, "breakout"],
            }
        )

        self.assertEqual(settings.strategy_entry_allowed_regimes, ("trend", "breakout"))
        self.assertEqual(settings.strategy_short_entry_allowed_regimes, ("trend", "breakout"))

    def test_opportunistic_overlay_thresholds_reject_inverted_range(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "strategy_hedge_opportunistic_close_threshold_must_not_exceed_open_threshold",
        ):
            AATSSettings.model_validate(
                {
                    "strategy_hedge_opportunistic_open_threshold": 0.40,
                    "strategy_hedge_opportunistic_close_threshold": 0.50,
                }
            )

    def test_independent_overlay_thresholds_reject_entry_above_scale_in(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "strategy_hedge_independent_long_entry_threshold_must_not_exceed_scale_in_threshold",
        ):
            AATSSettings.model_validate(
                {
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "derivatives_position_mode": "hedge",
                    "strategy_hedge_overlay_mode": "independent",
                    "strategy_hedge_independent_long_entry_threshold": 0.72,
                    "strategy_hedge_independent_long_scale_in_threshold": 0.70,
                }
            )

    def test_independent_overlay_thresholds_reject_close_above_entry(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "strategy_hedge_independent_long_close_threshold_must_not_exceed_entry_threshold",
        ):
            AATSSettings.model_validate(
                {
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "derivatives_position_mode": "hedge",
                    "strategy_hedge_overlay_mode": "independent",
                    "strategy_hedge_independent_long_entry_threshold": 0.60,
                    "strategy_hedge_independent_long_close_threshold": 0.62,
                }
            )

    def test_independent_overlay_expected_edge_buffers_must_be_non_negative(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "strategy_hedge_independent_expected_execution_buffer_bps_must_be_non_negative",
        ):
            AATSSettings.model_validate(
                {
                    "strategy_hedge_independent_expected_execution_buffer_bps": -0.5,
                }
            )

    def test_independent_entry_quality_gate_settings_validate_ranges(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "strategy_hedge_independent_min_confirm_ticks_must_be_positive",
        ):
            AATSSettings.model_validate(
                {
                    "strategy_hedge_independent_min_confirm_ticks": 0,
                }
            )
        with self.assertRaisesRegex(
            ValueError,
            "strategy_hedge_independent_min_liquidity_quality_must_be_between_zero_and_one",
        ):
            AATSSettings.model_validate(
                {
                    "strategy_hedge_independent_min_liquidity_quality": 1.2,
                }
            )
        with self.assertRaisesRegex(
            ValueError,
            "strategy_hedge_independent_min_score_stability_bps_must_be_non_negative",
        ):
            AATSSettings.model_validate(
                {
                    "strategy_hedge_independent_min_score_stability_bps": -1.0,
                }
            )
        with self.assertRaisesRegex(
            ValueError,
            "strategy_hedge_independent_min_score_drawdown_bps_must_be_non_negative",
        ):
            AATSSettings.model_validate(
                {
                    "strategy_hedge_independent_min_score_drawdown_bps": -1.0,
                }
            )
        with self.assertRaisesRegex(
            ValueError,
            "strategy_hedge_independent_max_thesis_age_seconds_must_be_positive",
        ):
            AATSSettings.model_validate(
                {
                    "strategy_hedge_independent_max_thesis_age_seconds": 0,
                }
            )
        with self.assertRaisesRegex(
            ValueError,
            "strategy_hedge_independent_de_risk_net_edge_bps_must_be_non_negative",
        ):
            AATSSettings.model_validate(
                {
                    "strategy_hedge_independent_de_risk_net_edge_bps": -0.5,
                }
            )
        with self.assertRaisesRegex(
            ValueError,
            "strategy_hedge_independent_failed_thesis_net_edge_bps_must_not_exceed_de_risk_threshold",
        ):
            AATSSettings.model_validate(
                {
                    "strategy_hedge_independent_de_risk_net_edge_bps": 1.0,
                    "strategy_hedge_independent_failed_thesis_net_edge_bps": 2.0,
                }
            )

    def test_independent_overlay_rollout_can_be_set_to_live_after_task106_enablement(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "derivatives_position_mode": "hedge",
                "strategy_hedge_independent_rollout_stage": "live",
            }
        )

        self.assertEqual(settings.strategy_hedge_independent_rollout_stage, "live")

    def test_protective_overlay_independent_switch_defaults_to_true(self) -> None:
        settings = AATSSettings.model_validate({})

        self.assertTrue(settings.strategy_hedge_protective_enabled)

    def test_independent_diagnostics_emit_flags_default_to_true(self) -> None:
        settings = AATSSettings.model_validate({})

        self.assertTrue(settings.strategy_hedge_independent_emit_book_level_metrics)
        self.assertTrue(settings.strategy_hedge_independent_emit_expected_vs_realized_metrics)
        self.assertTrue(settings.strategy_hedge_independent_emit_close_reason_metrics)
        self.assertTrue(settings.strategy_hedge_independent_emit_execution_policy_metrics)


if __name__ == "__main__":
    unittest.main()
