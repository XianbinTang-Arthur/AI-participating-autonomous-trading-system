"""Unit tests for the clock-aligned strategy profile auto-switch schedule.

Targets:
  * ``ApplicationRuntime._seconds_until_next_half_hour_boundary`` —
    deterministic math, covers wraparound and exact-boundary edge cases.
  * ``ApplicationRuntime._run_profile_auto_switch_loop`` — verifies the
    loop sleeps, re-reads the enable flag, dispatches ``evaluate_now``,
    and cooperates with cancellation without leaking tasks.

The loop tests mock ``asyncio.sleep`` + ``utc_now`` to keep the test
fast and deterministic; they don't try to cover real clock drift, which
is untestable in unit scope.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from aats.bootstrap.config import ApplicationRuntime


def _at(h: int, m: int, s: int = 0, us: int = 0) -> datetime:
    return datetime(2026, 4, 24, h, m, s, us, tzinfo=timezone.utc)


class TestSecondsUntilNextHalfHourBoundary(unittest.TestCase):
    def test_mid_first_half_fires_at_half(self) -> None:
        self.assertEqual(
            ApplicationRuntime._seconds_until_next_half_hour_boundary(_at(10, 15, 30)),
            14 * 60 + 30,  # 14:30 until 10:30:00
        )

    def test_mid_second_half_fires_at_next_hour(self) -> None:
        self.assertEqual(
            ApplicationRuntime._seconds_until_next_half_hour_boundary(_at(10, 45, 30)),
            14 * 60 + 30,  # 14:30 until 11:00:00
        )

    def test_just_before_half_boundary_returns_tiny_positive(self) -> None:
        delta = ApplicationRuntime._seconds_until_next_half_hour_boundary(
            _at(10, 29, 59, 500_000)
        )
        self.assertAlmostEqual(delta, 0.5, places=3)

    def test_exact_top_of_hour_sleeps_to_half(self) -> None:
        self.assertEqual(
            ApplicationRuntime._seconds_until_next_half_hour_boundary(_at(10, 0, 0)),
            1800.0,
        )

    def test_exact_half_hour_sleeps_to_next_hour(self) -> None:
        self.assertEqual(
            ApplicationRuntime._seconds_until_next_half_hour_boundary(_at(10, 30, 0)),
            1800.0,
        )

    def test_just_before_top_of_hour_returns_tiny_positive(self) -> None:
        delta = ApplicationRuntime._seconds_until_next_half_hour_boundary(
            _at(10, 59, 59, 250_000)
        )
        self.assertAlmostEqual(delta, 0.75, places=3)


class TestProfileAutoSwitchLoop(unittest.IsolatedAsyncioTestCase):
    def _make_runtime(self, *, auto_enabled: bool = True) -> MagicMock:
        runtime = MagicMock()
        runtime.settings = MagicMock()
        runtime.settings.strategy_profile_auto_control_enabled = auto_enabled
        runtime.logger = MagicMock()
        runtime._seconds_until_next_half_hour_boundary = MagicMock(return_value=0.0)
        runtime._record_background_failure = AsyncMock()
        runtime._record_background_recovery = AsyncMock()
        return runtime

    async def test_loop_invokes_evaluate_now_at_boundary(self) -> None:
        runtime = self._make_runtime(auto_enabled=True)
        service = MagicMock()
        service.auto_switch_effective_enabled = MagicMock(return_value=True)
        service.evaluate_now = AsyncMock(return_value={"status": "ok"})

        # Run one iteration then cancel, so sleep(0) returns immediately and
        # we exit via CancelledError on the second iteration's sleep.
        sleep_calls = 0

        async def _fake_sleep(delay: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                raise asyncio.CancelledError()

        with patch("aats.bootstrap.config.asyncio.sleep", new=_fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await ApplicationRuntime._run_profile_auto_switch_loop(runtime, service)

        service.evaluate_now.assert_awaited_once_with(
            allow_auto_activation=True,
            use_ai_recommendation=True,
        )
        service.auto_switch_effective_enabled.assert_called_once_with()
        runtime._record_background_failure.assert_not_awaited()

    async def test_loop_skips_evaluate_when_auto_control_disabled(self) -> None:
        runtime = self._make_runtime(auto_enabled=False)
        service = MagicMock()
        service.auto_switch_effective_enabled = MagicMock(return_value=True)
        service.evaluate_now = AsyncMock()

        sleep_calls = 0

        async def _fake_sleep(delay: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                raise asyncio.CancelledError()

        with patch("aats.bootstrap.config.asyncio.sleep", new=_fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await ApplicationRuntime._run_profile_auto_switch_loop(runtime, service)

        service.evaluate_now.assert_not_awaited()
        service.auto_switch_effective_enabled.assert_not_called()

    async def test_loop_runs_readonly_evaluation_when_effective_auto_control_disabled(self) -> None:
        runtime = self._make_runtime(auto_enabled=True)
        service = MagicMock()
        service.auto_switch_effective_enabled = MagicMock(return_value=False)
        service.evaluate_now = AsyncMock()

        sleep_calls = 0

        async def _fake_sleep(delay: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                raise asyncio.CancelledError()

        with patch("aats.bootstrap.config.asyncio.sleep", new=_fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await ApplicationRuntime._run_profile_auto_switch_loop(runtime, service)

        service.auto_switch_effective_enabled.assert_called_once_with()
        service.evaluate_now.assert_awaited_once_with(
            allow_auto_activation=False,
            use_ai_recommendation=False,
        )

    async def test_loop_swallows_evaluate_exception_and_continues(self) -> None:
        runtime = self._make_runtime(auto_enabled=True)
        service = MagicMock()
        service.auto_switch_effective_enabled = MagicMock(return_value=True)
        service.evaluate_now = AsyncMock(side_effect=RuntimeError("ai_api_hiccup"))

        sleep_calls = 0

        async def _fake_sleep(delay: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            # First boundary: evaluate raises. Second boundary: we cancel.
            if sleep_calls >= 2:
                raise asyncio.CancelledError()

        with patch("aats.bootstrap.config.asyncio.sleep", new=_fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await ApplicationRuntime._run_profile_auto_switch_loop(runtime, service)

        service.evaluate_now.assert_awaited_once_with(
            allow_auto_activation=True,
            use_ai_recommendation=True,
        )
        runtime._record_background_failure.assert_awaited_once()
        failure_kwargs = runtime._record_background_failure.call_args.kwargs
        self.assertEqual(failure_kwargs["subsystem"], "strategy_profile_auto_switch")


if __name__ == "__main__":
    unittest.main()
