from __future__ import annotations

from decimal import Decimal
from unittest import TestCase

from aats.bootstrap.settings import AATSSettings
from aats.services.strategy_engines.sleeve_execution_permission import SleeveExecutionPermissionPolicy
from aats.services.strategy_engines.sleeve_routing_models import RawSleeveCandidateInputs


def _settings(**overrides) -> AATSSettings:
    return AATSSettings.model_validate(
        {
            "config_profile": "local_demo",
            "mode": "paper_live",
            "market_data_backend": "demo",
            "execution_backend": "paper",
            "account_backend": "disabled",
            "account_read_enabled": False,
            "storage_mode": "memory",
            **overrides,
        }
    )


def _raw(**overrides) -> RawSleeveCandidateInputs:
    payload = {
        "family": "dca",
        "strategy_sleeve_id": "sleeve_dca",
        "symbol": "BTC-USDT",
        "current_position_qty": Decimal("0"),
        "target_position_qty": Decimal("0.25"),
        "delta_position_qty": Decimal("0.25"),
        "account_current_position_qty": Decimal("0"),
        "target_notional": Decimal("25"),
        "route_action": "override_target",
        "requested_legs": tuple(),
        "metrics": {},
        "candidate_state": "ready",
        "candidate_enabled": True,
        "candidate_selectable": True,
        "candidate_execution_compatible": True,
        "candidate_score": 0.7,
        "candidate_confidence": 0.8,
        "runtime_supported": True,
        "active_inventory": False,
        "current_inventory_notional": Decimal("0"),
        "protective_intent": False,
    }
    payload.update(overrides)
    return RawSleeveCandidateInputs(**payload)


class TestSleeveExecutionPermissionPolicy(TestCase):
    def test_auto_execution_on_allows_non_protective_execution(self) -> None:
        policy = SleeveExecutionPermissionPolicy(_settings(strategy_sleeve_auto_parallel_enabled=True))

        decision = policy.evaluate(raw=_raw())

        self.assertTrue(decision.approved_for_execution)
        self.assertEqual(decision.permission_mode, "approved")

    def test_auto_execution_off_denies_non_protective_without_inventory(self) -> None:
        policy = SleeveExecutionPermissionPolicy(_settings(strategy_sleeve_auto_parallel_enabled=False))

        decision = policy.evaluate(raw=_raw(active_inventory=False))

        self.assertFalse(decision.approved_for_execution)
        self.assertEqual(decision.permission_mode, "advisory_only")
        self.assertIn("auto_execution_disabled_by_profile", decision.reason_codes)

    def test_auto_execution_off_denies_non_protective_with_inventory_as_hold_current(self) -> None:
        policy = SleeveExecutionPermissionPolicy(_settings(strategy_sleeve_auto_parallel_enabled=False))

        decision = policy.evaluate(raw=_raw(active_inventory=True, current_inventory_notional=Decimal("10")))

        self.assertFalse(decision.approved_for_execution)
        self.assertEqual(decision.permission_mode, "hold_current")

    def test_protective_intent_overrides_disabled_auto_execution(self) -> None:
        policy = SleeveExecutionPermissionPolicy(_settings(strategy_sleeve_auto_parallel_enabled=False))

        decision = policy.evaluate(raw=_raw(protective_intent=True, active_inventory=True))

        self.assertTrue(decision.approved_for_execution)
        self.assertEqual(decision.permission_mode, "protective_override")
        self.assertIn("protective_intent_override", decision.reason_codes)

    def test_runtime_unsupported_denies_execution(self) -> None:
        policy = SleeveExecutionPermissionPolicy(_settings(strategy_sleeve_auto_parallel_enabled=True))

        decision = policy.evaluate(raw=_raw(runtime_supported=False))

        self.assertFalse(decision.approved_for_execution)
        self.assertEqual(decision.permission_mode, "unsupported")
        self.assertIn("runtime_not_supported", decision.reason_codes)

    def test_candidate_disabled_denies_execution(self) -> None:
        policy = SleeveExecutionPermissionPolicy(_settings(strategy_sleeve_auto_parallel_enabled=True))

        decision = policy.evaluate(raw=_raw(candidate_enabled=False))

        self.assertFalse(decision.approved_for_execution)
        self.assertEqual(decision.permission_mode, "advisory_only")
        self.assertIn("candidate_disabled", decision.reason_codes)
