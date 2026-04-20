from __future__ import annotations

import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from aats.schemas.common import utc_now
from aats.schemas.strategy_profiles import StrategyProfileActivationState
from aats.services.operator.strategy_profile_activation import StrategyProfileActivationFacade


def _make_activation_state(
    *,
    active_profile_id: str | None,
    frozen_until=None,
) -> StrategyProfileActivationState:
    return StrategyProfileActivationState(
        product_type="derivatives",
        margin_mode="cross",
        allowed_symbols=("BTC-USDT-SWAP",),
        active_profile_id=active_profile_id,
        frozen_until=frozen_until,
    )


def _make_owner(
    *,
    auto_control_enabled: bool,
    active_profile_id: str | None,
    frozen_until=None,
) -> MagicMock:
    owner = MagicMock()
    owner.settings = MagicMock()
    owner.settings.strategy_profile_auto_control_enabled = auto_control_enabled
    owner._activation_state = MagicMock(
        return_value=_make_activation_state(
            active_profile_id=active_profile_id,
            frozen_until=frozen_until,
        )
    )
    owner.evaluate_now = AsyncMock(
        return_value={"recommendation": {}, "auto_activation": {}}
    )
    return owner


class EvaluateMainlineProfileControlSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_with_active_profile_returns_system_decision_without_ai(self) -> None:
        owner = _make_owner(auto_control_enabled=False, active_profile_id="trend_aggressive")
        facade = StrategyProfileActivationFacade(owner=owner)

        decision = await facade.evaluate_mainline_profile_control(decision_id="d1")

        owner.evaluate_now.assert_not_awaited()
        assert decision is not None
        self.assertEqual(decision.requested_by, "system")
        self.assertEqual(decision.requested_profile_id, "trend_aggressive")
        self.assertEqual(decision.current_profile_id, "trend_aggressive")
        self.assertFalse(decision.applied)
        self.assertFalse(decision.frozen_by_admin_override)
        self.assertEqual(decision.blocked_reasons, [])
        self.assertEqual(decision.decision_reason_codes, [])
        self.assertIsNone(decision.activation_record_ref)

    async def test_disabled_without_active_profile_returns_none_without_ai(self) -> None:
        owner = _make_owner(auto_control_enabled=False, active_profile_id=None)
        facade = StrategyProfileActivationFacade(owner=owner)

        decision = await facade.evaluate_mainline_profile_control(decision_id="d2")

        owner.evaluate_now.assert_not_awaited()
        self.assertIsNone(decision)

    async def test_disabled_with_future_frozen_until_reports_admin_override(self) -> None:
        future = utc_now() + timedelta(hours=1)
        owner = _make_owner(
            auto_control_enabled=False,
            active_profile_id="trend_standard",
            frozen_until=future,
        )
        facade = StrategyProfileActivationFacade(owner=owner)

        decision = await facade.evaluate_mainline_profile_control(decision_id="d3")

        owner.evaluate_now.assert_not_awaited()
        assert decision is not None
        self.assertTrue(decision.frozen_by_admin_override)
        self.assertEqual(decision.freeze_until, future)

    async def test_disabled_with_past_frozen_until_does_not_report_admin_override(self) -> None:
        past = utc_now() - timedelta(hours=1)
        owner = _make_owner(
            auto_control_enabled=False,
            active_profile_id="trend_standard",
            frozen_until=past,
        )
        facade = StrategyProfileActivationFacade(owner=owner)

        decision = await facade.evaluate_mainline_profile_control(decision_id="d4")

        owner.evaluate_now.assert_not_awaited()
        assert decision is not None
        self.assertFalse(decision.frozen_by_admin_override)

    async def test_enabled_still_invokes_evaluate_now(self) -> None:
        owner = _make_owner(auto_control_enabled=True, active_profile_id="trend_aggressive")
        facade = StrategyProfileActivationFacade(owner=owner)

        await facade.evaluate_mainline_profile_control(decision_id="d5")

        owner.evaluate_now.assert_awaited_once_with(allow_auto_activation=True)


if __name__ == "__main__":
    unittest.main()
