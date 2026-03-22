from __future__ import annotations

import unittest

from aats.bootstrap.settings import AATSSettings
from aats.schemas.execution import OrderState
from aats.schemas.runtime_profiles import RuntimeProfileActivationState, RuntimeProfileRevision
from aats.services.operator.runtime_profiles import RuntimeProfileControlService, RuntimeProfileError, runtime_profile_resolution
from aats.storage.execution_repo import InMemoryExecutionRepository
from aats.storage.runtime_profile_repo import InMemoryRuntimeProfileRepository
from tests.support.postgres import postgres_example_url


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

    def test_resolution_falls_back_to_env_when_no_active_revision_exists(self) -> None:
        settings = AATSSettings.model_validate({"default_symbol": "BTC-USDT"})
        repo = InMemoryRuntimeProfileRepository()

        resolution = runtime_profile_resolution(settings=settings, repo=repo)

        self.assertEqual(resolution.profile_source, "env_fallback")
        self.assertEqual(resolution.resolved_settings["default_symbol"], "BTC-USDT")
        self.assertIsNone(resolution.active_revision)

    def test_resolution_overlays_active_revision_and_preserves_boot_critical_values(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT",
                "database_url": postgres_example_url(database_name="env"),
                "operator_session_secret": "session-secret",
            }
        )
        repo = InMemoryRuntimeProfileRepository()
        revision = RuntimeProfileRevision(
            profile_label="derivatives-primary",
            payload={
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "max_abs_position_qty": 0.02,
                "max_notional_per_symbol": 500.0,
                "max_open_orders": 3,
                "default_order_qty": 0.001,
                "max_target_leverage": 3.0,
                "default_target_leverage": 2.0,
                "strategy_short_bias_enabled": True,
                "strategy_dynamic_leverage_enabled": True,
                "decision_min_interval_seconds_15m": 0.0,
                "decision_min_interval_seconds_1h": 0.0,
                "decision_min_price_move_bps": 0.0,
                "decision_min_momentum_delta": 0.0,
                "database_url": postgres_example_url(database_name="malicious"),
            },
            summary={"default_symbol": "BTC-USDT-SWAP"},
            status="active",
            change_classification="product_posture_change",
        )
        repo.save_revision(revision)
        repo.save_activation_state(
            RuntimeProfileActivationState(
                active_revision_id=revision.revision_id,
                active_profile_label=revision.profile_label,
            )
        )

        resolution = runtime_profile_resolution(settings=settings, repo=repo)

        self.assertEqual(resolution.profile_source, "env_fallback")
        self.assertEqual(resolution.resolved_settings["default_symbol"], "BTC-USDT")
        self.assertEqual(resolution.resolved_settings["database_url"], postgres_example_url(database_name="env"))
        self.assertEqual(resolution.resolved_settings["operator_session_secret"], "session-secret")

    def test_resolution_ignores_pending_revision_when_env_switch_mode_is_active(self) -> None:
        settings = AATSSettings.model_validate({})
        repo = InMemoryRuntimeProfileRepository()
        bad = RuntimeProfileRevision(
            profile_label="invalid",
            payload={"trading_product_type": "impossible"},
            summary={},
        )
        repo.save_revision(bad)
        repo.save_activation_state(
            RuntimeProfileActivationState(
                pending_revision_id=bad.revision_id,
                pending_profile_label=bad.profile_label,
                restart_required=True,
            )
        )

        resolution = runtime_profile_resolution(settings=settings, repo=repo)

        self.assertEqual(resolution.profile_source, "env_fallback")
        self.assertEqual(resolution.resolved_settings["trading_product_type"], "spot")

    def test_runtime_profile_control_is_disabled_in_env_switch_mode(self) -> None:
        settings = AATSSettings.model_validate({"trading_product_type": "spot", "margin_mode": "cash"})
        repo = InMemoryRuntimeProfileRepository()
        execution_repo = InMemoryExecutionRepository()
        execution_repo.save_order_state(
            OrderState(
                decision_id="decision_1",
                intent_id="intent_1",
                symbol="BTC-USDT",
                client_order_id="order_1",
                status="SUBMITTED",
                requested_qty=0.001,
                remaining_qty=0.001,
            )
        )
        service = RuntimeProfileControlService(settings=settings, repo=repo, execution_repo=execution_repo)
        with self.assertRaises(RuntimeProfileError):
            service.create_draft(profile_label="switch", actor_identity="admin")


if __name__ == "__main__":
    unittest.main()
